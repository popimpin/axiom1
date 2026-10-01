from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axiom1.engines import EngineError, matcher  # noqa: E402


class TestMatchOn(unittest.TestCase):
    def test_one_to_one_match(self):
        left = [{"id": "1", "name": "Alice"}, {"id": "2", "name": "Bob"}]
        right = [{"id": "1", "role": "Eng"}, {"id": "3", "role": "PM"}]
        res = matcher.match_on(left, right, ["id"])
        self.assertEqual(res["pairs"], [[{"id": "1", "name": "Alice"}, {"id": "1", "role": "Eng"}]])
        self.assertEqual(res["left_only"], [{"id": "2", "name": "Bob"}])
        self.assertEqual(res["right_only"], [{"id": "3", "role": "PM"}])
        self.assertEqual(res["ambiguous"], [])

    def test_ambiguous_when_duplicates_exist(self):
        left = [{"id": "1", "seq": "A"}, {"id": "1", "seq": "B"}]
        right = [{"id": "1", "seq": "C"}]
        res = matcher.match_on(left, right, ["id"])
        self.assertEqual(res["pairs"], [])
        self.assertEqual(res["left_only"], [])
        self.assertEqual(res["right_only"], [])
        self.assertEqual(len(res["ambiguous"]), 1)
        self.assertEqual(res["ambiguous"][0]["left"], left)
        self.assertEqual(res["ambiguous"][0]["right"], right)

    def test_multiple_keys(self):
        left = [{"date": "2026-05-01", "ref": "INV-1", "val": 10}]
        right = [{"date": "2026-05-01", "ref": "INV-1", "paid": True}]
        res = matcher.match_on(left, right, ["date", "ref"])
        self.assertEqual(len(res["pairs"]), 1)

    def test_refuses_missing_keys(self):
        left = [{"id": "1"}]
        right = [{"other": "1"}]
        with self.assertRaisesRegex(EngineError, "missing key 'id'"):
            matcher.match_on(left, right, ["id"])

    def test_refuses_empty_keys(self):
        with self.assertRaisesRegex(EngineError, "non-empty"):
            matcher.match_on([], [], [])


class TestMatchAmountDate(unittest.TestCase):
    def test_exact_match_within_window(self):
        left = [{"inv": "A", "amount": "$100.00", "date": "2026-05-01"}]
        right = [{"pay": "1", "amount": "100.00", "date": "2026-05-03"}]
        res = matcher.match_amount_date(left, right, "amount", "date", 2)
        self.assertEqual(res["pairs"], [[left[0], right[0]]])
        self.assertEqual(res["left_only"], [])
        self.assertEqual(res["right_only"], [])
        self.assertEqual(res["ambiguous"], [])

    def test_outside_window_goes_to_only(self):
        left = [{"inv": "A", "amount": "$100.00", "date": "2026-05-01"}]
        right = [{"pay": "1", "amount": "100.00", "date": "2026-05-10"}]
        res = matcher.match_amount_date(left, right, "amount", "date", 2)
        self.assertEqual(res["pairs"], [])
        self.assertEqual(res["left_only"], [left[0]])
        self.assertEqual(res["right_only"], [right[0]])
        self.assertEqual(res["ambiguous"], [])

    def test_multiple_candidates_go_to_ambiguous(self):
        # Two identical payments match one invoice within the date window
        left = [{"inv": "A", "amount": "$50.00", "date": "2026-05-01"}]
        right = [
            {"pay": "1", "amount": "$50.00", "date": "2026-05-01"},
            {"pay": "2", "amount": "$50.00", "date": "2026-05-02"},
        ]
        res = matcher.match_amount_date(left, right, "amount", "date", 3)
        self.assertEqual(res["pairs"], [])
        self.assertEqual(res["left_only"], [])
        self.assertEqual(res["right_only"], [])
        self.assertEqual(len(res["ambiguous"]), 1)
        self.assertEqual(res["ambiguous"][0]["left"], left)
        self.assertEqual(res["ambiguous"][0]["right"], right)

    def test_refuses_floats_and_negative_days(self):
        left = [{"amount": 10.50, "date": "2026-05-01"}]
        right = [{"amount": "10.50", "date": "2026-05-01"}]
        with self.assertRaisesRegex(EngineError, "float"):
            matcher.match_amount_date(left, right, "amount", "date", 1)

        with self.assertRaisesRegex(EngineError, "non-negative"):
            matcher.match_amount_date([], [], "amount", "date", -1)

    def test_refuses_missing_fields(self):
        left = [{"date": "2026-05-01"}]
        right = [{"amount": "$10.00", "date": "2026-05-01"}]
        with self.assertRaisesRegex(EngineError, "missing amount field"):
            matcher.match_amount_date(left, right, "amount", "date", 1)


if __name__ == "__main__":
    unittest.main()
