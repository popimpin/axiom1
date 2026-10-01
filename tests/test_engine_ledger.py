from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axiom1.engines import EngineError, ledger  # noqa: E402


class TestLedger(unittest.TestCase):
    def test_canonical_inbox_flow(self):
        # The real case from Claude:
        # add retro Thu 9:30 45min -> move to Fri 11:30 -> result has Fri 11:30-12:15;
        # add landlord call -> cancel -> absent;
        # cancel of an unknown key refused.
        events = [
            {
                "kind": "add",
                "key": "retro",
                "title": "Team Retro",
                "date": "2026-05-07",
                "start": "09:30",
                "minutes": 45,
            },
            {
                "kind": "move",
                "key": "retro",
                "date": "2026-05-08",
                "start": "11:30",
            },
            {
                "kind": "add",
                "key": "landlord",
                "title": "Call Landlord",
                "date": "2026-05-09",
                "start": "14:00",
                "minutes": 30,
            },
            {
                "kind": "cancel",
                "key": "landlord",
            },
        ]
        result = ledger.apply_events(events)
        self.assertEqual(len(result), 1)
        entry = result[0]
        self.assertEqual(entry["key"], "retro")
        self.assertEqual(entry["title"], "Team Retro")
        self.assertEqual(entry["date"], "2026-05-08")
        self.assertEqual(entry["start"], "11:30")
        self.assertEqual(entry["end"], "12:15")

    def test_sorting_order(self):
        events = [
            {"kind": "add", "key": "m2", "title": "B", "date": "2026-05-10", "start": "10:00", "minutes": 30},
            {"kind": "add", "key": "m1", "title": "A", "date": "2026-05-08", "start": "15:00", "minutes": 30},
            {"kind": "add", "key": "m3", "title": "C", "date": "2026-05-08", "start": "09:00", "minutes": 30},
        ]
        res = ledger.apply_events(events)
        keys = [r["key"] for r in res]
        self.assertEqual(keys, ["m3", "m1", "m2"])

    def test_refuse_cancel_unknown_key_names_key(self):
        events = [{"kind": "cancel", "key": "secret_sync"}]
        with self.assertRaisesRegex(EngineError, "secret_sync"):
            ledger.apply_events(events)

    def test_refuse_move_unknown_key_names_key(self):
        events = [{"kind": "move", "key": "phantom_sync", "start": "10:00"}]
        with self.assertRaisesRegex(EngineError, "phantom_sync"):
            ledger.apply_events(events)

    def test_refuse_duplicate_add(self):
        events = [
            {"kind": "add", "key": "k1", "title": "T1", "date": "2026-05-08", "start": "10:00", "minutes": 30},
            {"kind": "add", "key": "k1", "title": "T2", "date": "2026-05-08", "start": "11:00", "minutes": 30},
        ]
        with self.assertRaisesRegex(EngineError, "already exists"):
            ledger.apply_events(events)

    def test_refuse_crosses_midnight(self):
        events = [
            {"kind": "add", "key": "night", "title": "Night", "date": "2026-05-08", "start": "23:30", "minutes": 60}
        ]
        with self.assertRaisesRegex(EngineError, "midnight"):
            ledger.apply_events(events)

    def test_refuse_minutes_non_positive(self):
        events = [
            {"kind": "add", "key": "k", "title": "T", "date": "2026-05-08", "start": "10:00", "minutes": 0}
        ]
        with self.assertRaisesRegex(EngineError, "minutes"):
            ledger.apply_events(events)

    def test_refuse_unknown_kind(self):
        events = [{"kind": "postpone", "key": "k"}]
        with self.assertRaisesRegex(EngineError, "unknown event kind"):
            ledger.apply_events(events)


if __name__ == "__main__":
    unittest.main()
