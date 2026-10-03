"""Filing and the timeline: structure only, so every rule here is deterministic and checked without a model."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

import inbox_timeline as T  # noqa: E402


def thread(subject, *msgs):
    out = []
    for frm, date, body in msgs:
        out.append(f"From: {frm}\nDate: {date}\nSubject: {subject}\n\n{body}\n")
    return "\n".join(out)


ANA = "Ana Lopez <ana@example.com>"
ME = "Me <me@example.com>"


class Filing(unittest.TestCase):
    def test_reply_and_forward_share_the_folder_of_their_topic(self):
        tl = T.build({
            "a": thread("Budget review", (ANA, "Mon, 4 May 2026 09:00", "Can we meet Tuesday at 2pm?")),
            "b": thread("RE: Fwd: budget review", (ME, "Tue, 5 May 2026 10:00", "Attached the numbers.")),
        })
        self.assertEqual([f["folder"] for f in tl["folders"]], ["budget review"])
        self.assertEqual(len(tl["folders"][0]["threads"]), 2)
        self.assertEqual(tl["folders"][0]["span"], ["2026-05-04T09:00", "2026-05-05T10:00"])

    def test_no_subject_is_filed_by_the_people_in_it(self):
        tl = T.build({"a": thread("(no subject)", (ANA, "Mon, 4 May 2026 09:00", "hi"), (ME, "Mon, 4 May 2026 10:00", "hey"))})
        self.assertEqual(tl["folders"][0]["folder"], "with Ana Lopez")

    def test_every_thread_is_filed_even_with_no_outcome(self):
        tl = T.build({"a": thread("Notes", (ANA, "Mon, 4 May 2026 09:00", "FYI"))})
        self.assertEqual(tl["folders"][0]["threads"][0]["outcome"], {"kind": "filed"})


class Timeline(unittest.TestCase):
    def test_a_hand_off_to_a_call_is_a_gap_not_a_guess(self):
        tl = T.build({"a": thread("Presentation",
                                  (ME, "Mon, 4 May 2026 09:00", "I can do November 13 in the morning or November 14.\n"
                                                                "Please give me a call and let me know if any of these work."))})
        ev = tl["folders"][0]["threads"][0]["events"][0]
        self.assertEqual(ev["kind"], "call")
        self.assertIn("not in the email", ev["gap"])

    def test_a_sign_off_or_a_signature_number_is_not_a_gap(self):
        for body in ("Invoice attached. If you have any questions, please do not hesitate to give me a call.",
                     "See you at the hotel at 6:00 p.m. My cell phone number is 713 410 5396."):
            ev = T.build({"a": thread("x", (ANA, "Mon, 4 May 2026 09:00", body))})["folders"][0]["threads"][0]["events"][0]
            self.assertNotEqual(ev["kind"], "call", body)

    def test_proposals_name_their_phrases_and_undated_messages_keep_their_place(self):
        text = thread("Sync", (ANA, "Mon, 4 May 2026 09:00", "How about next Tuesday at 2pm?")) + \
            "\nFrom: Me <me@example.com>\nSubject: Sync\n\nWorks for me.\n"
        evs = T.build({"a": text})["folders"][0]["threads"][0]["events"]
        self.assertEqual([e["kind"] for e in evs], ["proposed", "message"])
        self.assertEqual(evs[0]["whens"], ["next Tuesday", "2pm"])
        self.assertIsNone(evs[1]["at"])
        self.assertEqual(evs[1]["who"], "me")

    def test_outcome_carries_its_receipt(self):
        rows = {"a": {"got": "on", "placed": ["2026-05-12", "14:00", ""], "tier": "one model + table"},
                "b": {"got": "ask", "tier": "you", "reminder": {"kind": "finalize", "why": "no time",
                                                                "tentative": {"date": "2026-05-12"}}}}
        tl = T.build({n: thread(n, (ANA, "Mon, 4 May 2026 09:00", "Tuesday?")) for n in ("a", "b", "c")}, rows)
        got = {t["thread"]: t["outcome"] for f in tl["folders"] for t in f["threads"]}
        self.assertEqual(got["a"]["kind"], "calendar")
        self.assertEqual(got["a"]["decided_by"], "one model + table")
        self.assertEqual(got["b"]["kind"], "reminder:finalize")
        self.assertEqual(got["b"]["tentative"], {"date": "2026-05-12"})
        self.assertEqual(got["c"]["kind"], "filed")


if __name__ == "__main__":
    unittest.main()
