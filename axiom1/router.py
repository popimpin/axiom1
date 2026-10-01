"""Route each decision to the cheapest thing that gets it right.

A job is several kinds of decision, and they are not equally hard. Measured on the calendar: a 1.7B model does
every decision on the form except one ("is my reply a yes?"), which it answers "yes" to everything; a 9B model
answers it. So the unit to size is the decision, not the job:

  tier 0  a frozen table of answers already proven: zero model calls
  tier 1+ the models assigned to this decision, cheapest first

An answer is frozen only after the delivery it was part of is WITNESSED (the server's check passed). A refuted
delivery freezes nothing, so a wrong answer never becomes a rule. That is the same law as OhmOS's voice routing:
verification-gated freeze.
"""
import json
import re
from pathlib import Path


def norm(text):
    """The key a decision is remembered by: case, spacing and edge punctuation aside."""
    return re.sub(r"\s+", " ", text).strip().strip(".!?,;:").lower()


class Table:
    """Frozen answers, one JSON file. key: '<decision>|<normalised text>' -> answer."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.frozen = json.loads(self.path.read_text(encoding="utf-8")) if self.path and self.path.exists() else {}

    @staticmethod
    def key(decision, text):
        return f"{decision}|{norm(text)}"

    def get(self, decision, text):
        return self.frozen.get(self.key(decision, text))

    def freeze(self, pending):
        """pending: [(decision, text, answer)] from a witnessed delivery."""
        for decision, text, answer in pending:
            self.frozen[self.key(decision, text)] = answer
        if self.path:
            self.path.write_text(json.dumps(self.frozen, indent=1, sort_keys=True), encoding="utf-8")

    def __len__(self):
        return len(self.frozen)


class Router:
    """decide() answers one decision from the table or the decision's models; it remembers what it answered so
    the caller can freeze() it if the delivery is witnessed, or forget() it if not."""

    def __init__(self, table, routes):
        self.table = table
        self.routes = routes             # decision -> [model, ...], cheapest first
        self.pending = []
        self.log = []                    # (decision, text, answer, tier) for this delivery

    def decide(self, decision, text, question, options):
        frozen = self.table.get(decision, text)
        if frozen is not None:
            self.pending.append((decision, text, frozen))
            self.log.append((decision, text, frozen, "table"))
            return frozen, "table"
        for model in self.routes[decision]:
            answer = self._ask(model, question, options)
            if answer in options:
                self.pending.append((decision, text, answer))
                self.log.append((decision, text, answer, getattr(model, "model", "model")))
                return answer, getattr(model, "model", "model")
        self.log.append((decision, text, None, "unanswered"))
        return None, "unanswered"

    @staticmethod
    def _ask(model, question, options):
        if hasattr(model, "thinking"):
            model.thinking = False
        if hasattr(model, "max_tokens"):
            model.max_tokens = min(model.max_tokens, 256)
        tool = {"type": "function", "function": {
            "name": "answer", "description": "Give the answer.",
            "parameters": {"type": "object", "required": ["answer"],
                           "properties": {"answer": {"type": "string", "enum": list(options)}}}}}
        reply = model([{"role": "system", "content": "Answer the question by calling `answer` with one of the "
                                                     "options. Nothing else."},
                       {"role": "user", "content": question}], [tool])
        for call in reply.get("tool_calls") or []:
            try:
                return json.loads(call["function"].get("arguments") or "{}").get("answer")
            except json.JSONDecodeError:
                return None
        content = (reply.get("content") or "").strip().lower()
        return next((o for o in options if re.fullmatch(rf"\W*{re.escape(o)}\W*", content)), None)

    def freeze(self):
        self.table.freeze(self.pending)
        self.pending = []

    def forget(self):
        self.pending = []

    def start(self):
        self.pending, self.log = [], []
