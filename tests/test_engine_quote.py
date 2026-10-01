from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axiom1.engines import EngineError, quote  # noqa: E402


class TestFindLines(unittest.TestCase):
    def test_find_lines_multiple_terms(self):
        text = "Alpha beta gamma\nDelta alpha\nGamma epsilon"
        res = quote.find_lines(text, ["alpha", "delta"])
        self.assertEqual(res, [{"line": 2, "text": "Delta alpha"}])

    def test_find_lines_case_insensitive(self):
        text = "First Section Title\nsecond section\nTHIRD SECTION"
        res = quote.find_lines(text, ["section"])
        self.assertEqual(len(res), 3)

    def test_refuses_empty_terms(self):
        with self.assertRaisesRegex(EngineError, "non-empty"):
            quote.find_lines("hello", [])
        with self.assertRaisesRegex(EngineError, "non-empty"):
            quote.find_lines("hello", [""])


class TestVerifyQuote(unittest.TestCase):
    def test_exact_single_line_quote(self):
        text = "Line 1: Hello.\nLine 2: The quick brown fox jumps.\nLine 3: Goodbye."
        res = quote.verify_quote(text, "The quick brown fox jumps.")
        self.assertEqual(res, {"found": True, "line": 2})

    def test_multiline_and_whitespace_collapse(self):
        text = "Header\n  We agree to provide\n  coverage for damages   in full.\nFooter"
        res = quote.verify_quote(text, "We agree to provide coverage for damages in full.")
        self.assertEqual(res, {"found": True, "line": 2})

    def test_refuses_under_three_words(self):
        with self.assertRaisesRegex(EngineError, "3 words"):
            quote.verify_quote("The quick brown fox", "The quick")

    def test_refuses_quote_not_found(self):
        with self.assertRaisesRegex(EngineError, "not found in the source"):
            quote.verify_quote("Coverage applies to bodily injury.", "Coverage applies to vehicle theft.")


class TestExcerpt(unittest.TestCase):
    def test_excerpt_lines(self):
        text = "A\nB\nC\nD\nE"
        self.assertEqual(quote.excerpt(text, 2, 4), "B\nC\nD")
        self.assertEqual(quote.excerpt(text, 1, 1), "A")

    def test_refuses_out_of_range(self):
        text = "A\nB\nC"
        with self.assertRaisesRegex(EngineError, "out of range"):
            quote.excerpt(text, 1, 5)
        with self.assertRaisesRegex(EngineError, "cannot be greater than"):
            quote.excerpt(text, 3, 2)


if __name__ == "__main__":
    unittest.main()
