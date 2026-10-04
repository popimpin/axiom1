"""The calendar connector: read .ics, reconcile outcomes against it. Structural, so every rule is checked exactly."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from axiom1 import calendar_sync as CAL  # noqa: E402

ME = "me@example.com"


def ics(*events):
    body = []
    for uid, title, start, partstat in events:
        body += ["BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:{title}", f"DTSTART:{start}",
                 "ATTENDEE;CN=Ana:mailto:ana@example.com"]
        if partstat:
            body.append(f"ATTENDEE;PARTSTAT={partstat}:mailto:{ME}")
        body.append("END:VEVENT")
    return "BEGIN:VCALENDAR\r\n" + "\r\n".join(body) + "\r\nEND:VCALENDAR\r\n"


EVENTS = CAL.read_ics(ics(("1", "Budget review", "20260512T140000", "ACCEPTED"),
                          ("2", "Car service", "20260513T100000", "DECLINED"),
                          ("3", "Design review", "20260514T090000", "TENTATIVE"),
                          ("4", "Quarterly planning", "20260515T153000", "ACCEPTED")), ME)


class Reading(unittest.TestCase):
    def test_reads_title_time_and_my_response(self):
        e = EVENTS[0]
        self.assertEqual((e.title, e.start, e.response), ("Budget review", "2026-05-12T14:00", "accepted"))
        self.assertEqual(EVENTS[1].response, "declined")

    def test_folded_lines_are_unfolded(self):
        e = CAL.read_ics("BEGIN:VEVENT\r\nUID:9\r\nSUMMARY:Long ti\r\n tle\r\nDTSTART:20260101T080000\r\nEND:VEVENT", ME)[0]
        self.assertEqual(e.title, "Long title")


class Reconciling(unittest.TestCase):
    def test_an_unanswered_invitation_accepted_in_the_calendar_goes_on_the_calendar(self):
        r = CAL.reconcile({"subject": "Re: Budget review", "kind": "follow_up", "days": ["2026-05-12"]}, EVENTS)
        self.assertEqual(r["kind"], "calendar")
        self.assertIn("accepted in your calendar", r["receipt"])

    def test_declined_in_the_calendar_is_filed(self):
        r = CAL.reconcile({"subject": "Car service", "kind": "follow_up", "days": ["2026-05-13"]}, EVENTS)
        self.assertEqual(r["kind"], "filed")

    def test_tentative_stays_open_and_says_so(self):
        r = CAL.reconcile({"subject": "Design review", "kind": "follow_up", "days": ["2026-05-14"]}, EVENTS)
        self.assertEqual(r["kind"], "follow_up")
        self.assertIn("tentative", r["receipt"])

    def test_our_calendar_entry_is_verified_or_flagged(self):
        ok = CAL.reconcile({"subject": "Quarterly planning", "kind": "calendar", "start": "2026-05-15T15:30",
                            "days": ["2026-05-15"]}, EVENTS)
        self.assertIn("matches your calendar", ok["receipt"])
        bad = CAL.reconcile({"subject": "Quarterly planning", "kind": "calendar", "start": "2026-05-15T09:00",
                             "days": ["2026-05-15"]}, EVENTS)
        self.assertTrue(bad.get("conflict"))

    def test_a_different_day_or_topic_is_not_a_match(self):
        self.assertIsNone(CAL.reconcile({"subject": "Budget review", "kind": "follow_up", "days": ["2026-05-20"]},
                                        EVENTS)["receipt"])
        self.assertIsNone(CAL.reconcile({"subject": "Lunch", "kind": "follow_up", "days": ["2026-05-12"]},
                                        EVENTS)["receipt"])

    def test_two_events_that_fit_equally_are_not_guessed(self):
        twins = CAL.read_ics(ics(("a", "Budget review", "20260512T140000", "ACCEPTED"),
                                 ("b", "Budget review", "20260512T160000", "DECLINED")), ME)
        r = CAL.reconcile({"subject": "Budget review", "kind": "follow_up", "days": ["2026-05-12"]}, twins)
        self.assertEqual(r["kind"], "follow_up")
        self.assertIn("not resolved by guessing", r["receipt"])

    def test_a_title_without_words_still_matches(self):
        evs = CAL.read_ics(ics(("o", "1:1", "20260505T090000", "ACCEPTED")), ME)
        r = CAL.reconcile({"subject": "1:1", "kind": "calendar", "start": "2026-05-05T09:00", "days": ["2026-05-05"]}, evs)
        self.assertIn("matches your calendar", r["receipt"])

    def test_one_event_explains_one_thread(self):
        evs = CAL.read_ics(ics(("cs", "Car service", "20260513T140000", "ACCEPTED")), ME)
        out = CAL.reconcile_all({
            "firm": {"subject": "Car service", "kind": "calendar", "start": "2026-05-13T14:00", "days": ["2026-05-13"]},
            "invite": {"subject": "Car service", "kind": "follow_up", "days": ["2026-05-13"]}}, evs)
        self.assertIn("matches your calendar", out["firm"]["receipt"])
        self.assertEqual(out["invite"]["kind"], "follow_up")          # not "accepted" on another thread's event

    def test_an_event_two_open_threads_fit_is_given_to_neither(self):
        evs = CAL.read_ics(ics(("l", "Lunch", "20260506T120000", "DECLINED")), ME)
        out = CAL.reconcile_all({"a": {"subject": "Lunch", "kind": "follow_up", "days": ["2026-05-06"]},
                                 "b": {"subject": "Lunch?", "kind": "follow_up", "days": ["2026-05-06"]}}, evs)
        self.assertEqual({out["a"]["kind"], out["b"]["kind"]}, {"follow_up"})
        self.assertIn("not resolved by guessing", out["a"]["receipt"])

    def test_no_day_no_match(self):
        evs = CAL.read_ics(ics(("n", "Networking event", "20240530T180000", "ACCEPTED")), ME)
        r = CAL.reconcile({"subject": "Follow-up: our event packages", "kind": "follow_up", "days": []}, evs)
        self.assertEqual((r["kind"], r["receipt"]), ("follow_up", None))


if __name__ == "__main__":
    unittest.main()
