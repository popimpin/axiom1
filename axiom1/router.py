"""Route each decision to the cheapest thing that gets it right.

A job is several kinds of decision, and they are not equally hard. Measured on the calendar: a 1.7B model does
every decision on the form except one ("is my reply a yes?"), which it answers "yes" to everything; a 9B model
answers it. So the unit to size is the decision, not the job:

  tier 0  a frozen table of answers already proven: zero model calls
  tier 1+ the models assigned to this decision, cheapest first

An answer is frozen only after the delivery it was part of is WITNESSED (the server's check passed). A refuted
delivery freezes nothing, so a wrong answer never becomes a rule. That is the same law as OhmOS's voice routing:
verification-gated freeze.

In production there is no server check to witness against, so `consensus=True` changes what counts as proof: every
model routed to the decision answers, an answer they all agree on is frozen, and a disagreement goes to the person
(`ask_user`) instead of being guessed. The person's answer is frozen too: they are asked once per wording.
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

    def __init__(self, table, routes, consensus=False, ask_user=None):
        self.table = table
        self.routes = routes             # decision -> [model, ...], cheapest first
        self.consensus = consensus       # all models must agree; agreement is the proof, and is frozen at once
        self.ask_user = ask_user         # (decision, text, question, options, answers) -> answer; asked on disagreement
        self.pending = []
        self.log = []                    # (decision, text, answer, tier) for this delivery
        self.asked = []                  # every question that reached the person

    def decide(self, decision, text, question, options):
        frozen = self.table.get(decision, text)
        # a remembered answer that is not on THIS menu is someone else's (seen live: "the 14th", learned on one
        # thread, was put on a thread that never says it)
        if frozen is not None and frozen in options:
            self.pending.append((decision, text, frozen))
            self.log.append((decision, text, frozen, "table"))
            return frozen, "table"
        if self.consensus:
            return self._agree(decision, text, question, options)
        for model in self.routes[decision]:
            answer = self._ask(model, question, options)
            if answer in options:
                self.pending.append((decision, text, answer))
                self.log.append((decision, text, answer, getattr(model, "model", "model")))
                return answer, getattr(model, "model", "model")
        self.log.append((decision, text, None, "unanswered"))
        return None, "unanswered"

    def _agree(self, decision, text, question, options):
        votes = {getattr(m, "model", "model"): self._ask(m, question, options) for m in self.routes[decision]}
        votes = {k: v if v in options else None for k, v in votes.items()}
        answers = set(votes.values())
        if len(answers) == 1 and None not in answers:
            answer = answers.pop()
            if len(votes) >= 2:
                self.table.freeze([(decision, text, answer)])      # agreement is the proof in production
                self.log.append((decision, text, answer, "agreed"))
                return answer, "agreed"
            # one model agreeing with itself proves nothing: use the answer, never freeze it (seen live on Bee: a 9B's
            # one wrong "no" was frozen and, under a key general enough to teach many threads, spread to all of them)
            self.log.append((decision, text, answer, "one model"))
            return answer, "one model"
        self.asked.append({"decision": decision, "text": text, "question": question, "votes": votes})
        if self.ask_user is None:
            self.log.append((decision, text, None, "needs you"))
            return None, "needs you"
        answer = self.ask_user(decision, text, question, options, votes)
        if answer in options:
            self.table.freeze([(decision, text, answer)])          # asked once per wording, then remembered
        self.log.append((decision, text, answer, "you"))
        return answer, "you"

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
                return Router.on_menu(json.loads(call["function"].get("arguments") or "{}").get("answer"), options)
            except json.JSONDecodeError:
                return None
        return Router.on_menu(reply.get("content"), options)

    @staticmethod
    def on_menu(answer, options):
        """The option the answer names, or None. Not pedantic about case or punctuation, and a shortened option
        counts when it names exactly one (seen live: a 9B wrote "Tuesday" for the option "next Tuesday"); anything
        else is off the menu, never passed through (it was, and "Tuesday" became the wrong week)."""
        if not isinstance(answer, str):
            return None
        a = answer.strip().strip(".!?,;:'\"").lower()
        if not a:
            return None
        exact = [o for o in options if o.lower() == a]
        if exact:
            return exact[0]
        part = [o for o in options if re.search(rf"(?<!\w){re.escape(a)}(?!\w)", o.lower())]
        return part[0] if len(part) == 1 else None

    def freeze(self):
        self.table.freeze(self.pending)
        self.pending = []

    def forget(self):
        self.pending = []

    def start(self):
        self.pending, self.log = [], []
