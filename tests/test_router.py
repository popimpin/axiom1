"""Route each decision to the cheapest thing that gets it right; freeze an answer only from a witnessed delivery."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axiom1.router import Router, Table, norm  # noqa: E402


class Model:
    """Answers from a dict, counting its calls."""
    def __init__(self, answers, name="m"):
        self.answers, self.model, self.calls = answers, name, 0

    def __call__(self, messages, tools):
        self.calls += 1
        q = messages[-1]["content"]
        ans = next((a for k, a in self.answers.items() if k in q), "no")
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "x", "type": "function", "function": {"name": "answer", "arguments": f'{{"answer": "{ans}"}}'}}]}


class Routing(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "table.json"
        self.model = Model({"Works for me": "yes", "Sadly I'm away": "no"})
        self.router = Router(Table(self.path), {"reply_is_yes": [self.model]})

    def ask(self, reply):
        return self.router.decide("reply_is_yes", reply, f"reply: {reply}", ["yes", "no"])

    def test_a_witnessed_answer_is_frozen_and_then_costs_no_call(self):
        self.router.start()
        self.assertEqual(self.ask("Works for me!"), ("yes", "m"))
        self.router.freeze()
        self.router.start()
        self.assertEqual(self.ask("works for me"), ("yes", "table"))     # case and punctuation aside
        self.assertEqual(self.model.calls, 1)

    def test_a_refuted_delivery_freezes_nothing(self):
        self.router.start()
        self.ask("Works for me!")
        self.router.forget()
        self.assertEqual(len(self.router.table), 0)
        self.assertEqual(self.ask("Works for me!")[1], "m")              # asked again, not from a table

    def test_the_table_survives_a_restart(self):
        self.router.start()
        self.ask("Sadly I'm away.")
        self.router.freeze()
        again = Router(Table(self.path), {"reply_is_yes": [Model({})]})
        self.assertEqual(again.decide("reply_is_yes", "sadly i'm away", "q", ["yes", "no"]), ("no", "table"))

    def test_an_answer_outside_the_options_goes_to_the_next_tier(self):
        bad = Model({"x": "maybe"}, name="small")
        bad.__call__ = None
        class Wrong(Model):
            def __call__(self, messages, tools):
                self.calls += 1
                return {"role": "assistant", "content": "", "tool_calls": [
                    {"id": "x", "type": "function", "function": {"name": "answer", "arguments": '{"answer": "maybe"}'}}]}
        small, large = Wrong({}, "small"), Model({"Deal": "yes"}, "large")
        r = Router(Table(), {"reply_is_yes": [small, large]})
        self.assertEqual(r.decide("reply_is_yes", "Deal.", "reply: Deal.", ["yes", "no"]), ("yes", "large"))
        self.assertEqual((small.calls, large.calls), (1, 1))

    def test_norm(self):
        self.assertEqual(norm("  Works  for me!! "), "works for me")


if __name__ == "__main__":
    unittest.main()
