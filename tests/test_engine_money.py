from decimal import Decimal
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axiom1.engines import EngineError, money  # noqa: E402


class TestParseAmount(unittest.TestCase):
    def test_valid_amounts(self):
        self.assertEqual(money.parse_amount("$1,234.50"), Decimal("1234.50"))
        self.assertEqual(money.parse_amount("1234.5"), Decimal("1234.50"))
        self.assertEqual(money.parse_amount("USD 12"), Decimal("12.00"))
        self.assertEqual(money.parse_amount("12.00 USD"), Decimal("12.00"))
        self.assertEqual(money.parse_amount("-$5.00"), Decimal("-5.00"))
        self.assertEqual(money.parse_amount("($5.00)"), Decimal("-5.00"))
        self.assertEqual(money.parse_amount("(5.00)"), Decimal("-5.00"))
        self.assertEqual(money.parse_amount("1,234"), Decimal("1234.00"))
        self.assertEqual(money.parse_amount("10"), Decimal("10.00"))
        self.assertEqual(money.parse_amount(Decimal("25.50")), Decimal("25.50"))
        self.assertEqual(money.parse_amount(100), Decimal("100.00"))

    def test_refuses_ambiguous_and_invalid(self):
        # European format
        with self.assertRaisesRegex(EngineError, "European"):
            money.parse_amount("1.234,50")

        # Ambiguous comma grouping
        with self.assertRaisesRegex(EngineError, "ambiguous"):
            money.parse_amount("12,5")
        with self.assertRaisesRegex(EngineError, "ambiguous"):
            money.parse_amount("1,23")

        # More than 2 decimal places
        with self.assertRaisesRegex(EngineError, "more than 2 decimal places"):
            money.parse_amount("1.005")

        # Multiple currency symbols / codes
        with self.assertRaisesRegex(EngineError, "multiple currency"):
            money.parse_amount("$12 USD")
        with self.assertRaisesRegex(EngineError, "multiple currency"):
            money.parse_amount("$$12")

        # Missing or non-numeric
        with self.assertRaisesRegex(EngineError, "no numeric amount"):
            money.parse_amount("abc")
        with self.assertRaisesRegex(EngineError, "non-empty"):
            money.parse_amount("")

        # Floats are forbidden
        with self.assertRaisesRegex(EngineError, "float"):
            money.parse_amount(12.50)


class TestAdd(unittest.TestCase):
    def test_add_sums_correctly(self):
        self.assertEqual(money.add([Decimal("10.00"), "$5.50"]), Decimal("15.50"))
        self.assertEqual(money.add(["$1,200.00", "50.00", "USD 10"]), Decimal("1260.00"))
        self.assertEqual(money.add([]), Decimal("0.00"))
        self.assertEqual(money.add([10, "5.25"]), Decimal("15.25"))

    def test_add_refuses_floats(self):
        with self.assertRaisesRegex(EngineError, "float"):
            money.add([1.5])
        with self.assertRaisesRegex(EngineError, "float"):
            money.add([Decimal("1.00"), 2.5])


class TestTotalBy(unittest.TestCase):
    def test_grouping(self):
        rows = [
            {"dept": "eng", "cost": "$10.00"},
            {"dept": "eng", "cost": "20.50"},
            {"dept": "hr", "cost": "5"},
        ]
        got = money.total_by(rows, "dept", "cost")
        self.assertEqual(got, {"eng": Decimal("30.50"), "hr": Decimal("5.00")})

    def test_missing_fields_refused(self):
        with self.assertRaisesRegex(EngineError, "missing key"):
            money.total_by([{"cost": "$10.00"}], "dept", "cost")
        with self.assertRaisesRegex(EngineError, "missing amount"):
            money.total_by([{"dept": "eng"}], "dept", "cost")

    def test_float_in_row_refused(self):
        with self.assertRaisesRegex(EngineError, "float"):
            money.total_by([{"dept": "eng", "cost": 10.5}], "dept", "cost")


class TestFormatAmount(unittest.TestCase):
    def test_formatting(self):
        self.assertEqual(money.format_amount(Decimal("1234.50")), "1234.50")
        self.assertEqual(money.format_amount("$1,234.50"), "1234.50")
        self.assertEqual(money.format_amount(Decimal("-5.00")), "-5.00")
        self.assertEqual(money.format_amount("-$5.00"), "-5.00")
        self.assertEqual(money.format_amount(100), "100.00")

    def test_refuses_float(self):
        with self.assertRaisesRegex(EngineError, "float"):
            money.format_amount(12.34)


if __name__ == "__main__":
    unittest.main()
