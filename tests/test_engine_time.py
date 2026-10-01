import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axiom1.engines import EngineError, time  # noqa: E402


class TestParseTime(unittest.TestCase):
    def test_standard_times(self):
        self.assertEqual(time.parse_time("1pm"), "13:00")
        self.assertEqual(time.parse_time("1 pm"), "13:00")
        self.assertEqual(time.parse_time("1:00 pm"), "13:00")
        self.assertEqual(time.parse_time("1:00pm"), "13:00")
        self.assertEqual(time.parse_time("9:30am"), "09:30")
        self.assertEqual(time.parse_time("9.30am"), "09:30")
        self.assertEqual(time.parse_time("13:00"), "13:00")
        self.assertEqual(time.parse_time("11:00"), "11:00")
        self.assertEqual(time.parse_time("noon"), "12:00")
        self.assertEqual(time.parse_time("midnight"), "00:00")
        self.assertEqual(time.parse_time("12:00 am"), "00:00")
        self.assertEqual(time.parse_time("12:00 pm"), "12:00")
        self.assertEqual(time.parse_time("12:30 am"), "00:30")

    def test_refuses_ambiguous_and_invalid(self):
        # Bare hour without am/pm or colon is ambiguous
        with self.assertRaisesRegex(EngineError, "ambiguous"):
            time.parse_time("11")
        with self.assertRaisesRegex(EngineError, "ambiguous"):
            time.parse_time("9")

        # Invalid 12-hour clock
        with self.assertRaisesRegex(EngineError, "invalid"):
            time.parse_time("13pm")
        with self.assertRaisesRegex(EngineError, "invalid"):
            time.parse_time("0pm")

        # Invalid 24-hour clock
        with self.assertRaisesRegex(EngineError, "invalid hour"):
            time.parse_time("24:00")
        with self.assertRaisesRegex(EngineError, "invalid hour"):
            time.parse_time("25:00")
        with self.assertRaisesRegex(EngineError, "invalid minute"):
            time.parse_time("9:75")

        # Empty and non-string
        with self.assertRaisesRegex(EngineError, "non-empty"):
            time.parse_time("")
        with self.assertRaisesRegex(EngineError, "non-empty"):
            time.parse_time("   ")


class TestParseDate(unittest.TestCase):
    def test_valid_dates(self):
        self.assertEqual(time.parse_date("Friday May 8", 2026), "2026-05-08")
        self.assertEqual(time.parse_date("May 8", 2026), "2026-05-08")
        self.assertEqual(time.parse_date("8 May", 2026), "2026-05-08")
        self.assertEqual(time.parse_date("May 8th", 2026), "2026-05-08")
        self.assertEqual(time.parse_date("2026-05-08", 2026), "2026-05-08")
        self.assertEqual(time.parse_date("25/12/2026", 2026), "2026-12-25")
        self.assertEqual(time.parse_date("Feb 29", 2024), "2024-02-29")

    def test_refuses_ambiguous_and_invalid(self):
        # Ambiguous slash date (both <= 12)
        with self.assertRaisesRegex(EngineError, "ambiguous"):
            time.parse_date("05/08/2026", 2026)
        with self.assertRaisesRegex(EngineError, "ambiguous"):
            time.parse_date("08/05/2026", 2026)

        # Weekday mismatch
        with self.assertRaisesRegex(EngineError, "different day"):
            time.parse_date("Thursday May 8", 2026)

        # Invalid calendar dates
        with self.assertRaisesRegex(EngineError, "invalid date"):
            time.parse_date("Feb 30", 2026)
        with self.assertRaisesRegex(EngineError, "invalid date"):
            time.parse_date("Feb 29", 2026)

        # Year mismatch
        with self.assertRaisesRegex(EngineError, "does not match reference year"):
            time.parse_date("2025-05-08", 2026)
        with self.assertRaisesRegex(EngineError, "does not match reference year"):
            time.parse_date("May 8 2025", 2026)


class TestParseDuration(unittest.TestCase):
    def test_valid_durations(self):
        self.assertEqual(time.parse_duration("45 minutes"), 45)
        self.assertEqual(time.parse_duration("about 45 minutes"), 45)
        self.assertEqual(time.parse_duration("an hour"), 60)
        self.assertEqual(time.parse_duration("1 hour"), 60)
        self.assertEqual(time.parse_duration("1.5 hours"), 90)
        self.assertEqual(time.parse_duration("1h30"), 90)
        self.assertEqual(time.parse_duration("1h 30m"), 90)
        self.assertEqual(time.parse_duration("90 min"), 90)
        self.assertEqual(time.parse_duration("half an hour"), 30)

    def test_refuses_vague_and_invalid(self):
        with self.assertRaisesRegex(EngineError, "vague"):
            time.parse_duration("a while")
        with self.assertRaisesRegex(EngineError, "greater than 0"):
            time.parse_duration("0 minutes")
        with self.assertRaisesRegex(EngineError, "negative"):
            time.parse_duration("-15 minutes")
        with self.assertRaisesRegex(EngineError, "non-empty"):
            time.parse_duration("")


class TestAddMinutes(unittest.TestCase):
    def test_addition(self):
        self.assertEqual(time.add_minutes("09:00", 45), "09:45")
        self.assertEqual(time.add_minutes("1:00 pm", 30), "13:30")
        self.assertEqual(time.add_minutes("00:00", 60), "01:00")
        self.assertEqual(time.add_minutes("10:00", -30), "09:30")

    def test_crosses_midnight(self):
        with self.assertRaisesRegex(EngineError, "midnight"):
            time.add_minutes("23:30", 45)
        with self.assertRaisesRegex(EngineError, "midnight"):
            time.add_minutes("00:15", -30)

    def test_invalid_arguments(self):
        with self.assertRaisesRegex(EngineError, "integer"):
            time.add_minutes("09:00", 1.5)
        with self.assertRaisesRegex(EngineError, "integer"):
            time.add_minutes("09:00", True)


class TestDateCalculations(unittest.TestCase):
    def test_days_between(self):
        self.assertEqual(time.days_between("2026-05-08", "2026-05-10"), 2)
        self.assertEqual(time.days_between("2026-05-10", "2026-05-08"), -2)
        self.assertEqual(time.days_between("2026-05-08", "2026-05-08"), 0)
        with self.assertRaisesRegex(EngineError, "invalid ISO date"):
            time.days_between("invalid", "2026-05-10")

    def test_is_overdue(self):
        self.assertTrue(time.is_overdue("2026-05-01", "2026-05-08"))
        self.assertFalse(time.is_overdue("2026-05-08", "2026-05-08"))
        self.assertFalse(time.is_overdue("2026-05-10", "2026-05-08"))
        with self.assertRaisesRegex(EngineError, "invalid ISO date"):
            time.is_overdue("2026-05-01", "not-a-date")


if __name__ == "__main__":
    unittest.main()
