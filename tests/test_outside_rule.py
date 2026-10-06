"""From outside, never written to: filed and flagged, before any model reads it (Adrian, 2026-10-05)."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))
import real_inbox as RI  # noqa: E402
from axiom1 import provenance as PV  # noqa: E402

OWNER = "kay.lee@enron.com"
KNOWN = {"friend@gmail.com"}


def outside(sender):
    return PV.outside_flag(sender, OWNER, KNOWN)


def thread(*msgs):
    return "\n".join(f"From: {frm}\nDate: Mon, 5 Nov 2001 09:00\nSubject: Invitation\n\n{body}\n" for frm, body in msgs)


class Router:
    """Records every routed decision; answers 'no' so a thread that reaches it is filed as not a meeting."""
    def __init__(self):
        self.calls = []

    def decide(self, decision, key, prompt, options):
        self.calls.append(decision)
        return "no", "stub"


class Person:
    truth = {}


def decide(text, rule=outside):
    router = Router()
    answer, _ = RI.decide_thread("threads/t1.txt", text, router, None, Person(), lambda *a: None, outside=rule)
    return answer, router.calls


INVITE = "You're invited to our security summit on Friday at 3pm. Register now."


class OutsideRule(unittest.TestCase):
    def test_stranger_from_outside_is_filed_flagged_and_no_model_reads_it(self):
        answer, calls = decide(thread(("Events <promo@summit-events.com>", INVITE)))
        self.assertIn("from outside", answer["tier"])
        self.assertTrue(answer["outside"])
        self.assertFalse(answer["agreed"])
        self.assertEqual(calls, [], "a model was asked about a stranger's mail")

    def test_a_colleague_in_the_thread_keeps_it_out_of_the_rule(self):
        # an outside invitation forwarded by a colleague: someone inside is involved, so it is decided as usual
        _, calls = decide(thread(("Events <promo@summit-events.com>", INVITE),
                                 ("Pat Doe <pat.doe@enron.com>", "FYI - worth going? Friday at 3pm.")))
        self.assertIn("is_meeting", calls)

    def test_someone_the_owner_wrote_to_is_not_a_stranger(self):
        answer, calls = decide(thread(("Sam <friend@gmail.com>", "Birthday party Saturday at 7pm - come!")))
        self.assertNotIn("from outside", answer.get("tier", ""))
        self.assertIn("is_meeting", calls)

    def test_without_an_address_book_nothing_changes(self):
        answer, calls = decide(thread(("Events <promo@summit-events.com>", INVITE)), rule=None)
        self.assertNotIn("from outside", answer.get("tier", ""))
        self.assertIn("is_meeting", calls)


if __name__ == "__main__":
    unittest.main()
