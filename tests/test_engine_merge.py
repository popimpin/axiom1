from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axiom1.engines import EngineError, merge  # noqa: E402


class TestNormalizeEmail(unittest.TestCase):
    def test_valid_emails(self):
        self.assertEqual(merge.normalize_email(" Alice@example.com "), "alice@example.com")
        self.assertEqual(merge.normalize_email("bob.builder@corp.co.uk"), "bob.builder@corp.co.uk")

    def test_refuse_invalid_emails(self):
        with self.assertRaisesRegex(EngineError, "exactly one '@'"):
            merge.normalize_email("not-an-email")
        with self.assertRaisesRegex(EngineError, "exactly one '@'"):
            merge.normalize_email("a@b@c.com")
        with self.assertRaisesRegex(EngineError, "dot"):
            merge.normalize_email("user@domain")
        with self.assertRaisesRegex(EngineError, "dot"):
            merge.normalize_email("user@.com")


class TestNormalizePhone(unittest.TestCase):
    def test_valid_phones(self):
        self.assertEqual(merge.normalize_phone("+1 (555) 123-4567"), "+15551234567")
        self.assertEqual(merge.normalize_phone("555.1234"), "5551234")
        self.assertEqual(merge.normalize_phone("+44 20 7946 0991"), "+442079460991")

    def test_refuse_invalid_phones(self):
        with self.assertRaisesRegex(EngineError, "letters"):
            merge.normalize_phone("1-800-FLOWERS")
        with self.assertRaisesRegex(EngineError, "fewer than 7 digits"):
            merge.normalize_phone("12345")


class TestMergeRecords(unittest.TestCase):
    def test_transitive_merge(self):
        # r1 and r2 share email; r2 and r3 share phone -> all 3 merged
        records = [
            {"name": "Alice", "email": "alice@corp.com", "phone": ""},
            {"name": "Alice", "email": "alice@corp.com", "phone": "555-1234", "title": "Eng"},
            {"name": "", "email": "", "phone": "555-1234", "loc": "NYC"},
        ]
        res = merge.merge_records(records, ["email", "phone"])
        self.assertEqual(len(res["conflicts"]), 0)
        self.assertEqual(len(res["merged"]), 1)
        m = res["merged"][0]
        self.assertEqual(m["name"], "Alice")
        self.assertEqual(m["email"], "alice@corp.com")
        self.assertEqual(m["phone"], "5551234")
        self.assertEqual(m["title"], "Eng")
        self.assertEqual(m["loc"], "NYC")
        self.assertEqual(m["sources"], 3)

    def test_conflict_detection(self):
        records = [
            {"id": "1", "email": "dev@corp.com", "dept": "Engineering"},
            {"id": "1", "email": "dev@corp.com", "dept": "Sales"},
        ]
        res = merge.merge_records(records, ["email"])
        self.assertEqual(len(res["merged"]), 0)
        self.assertEqual(len(res["conflicts"]), 1)
        conf = res["conflicts"][0]
        self.assertEqual(conf["field"], "dept")
        self.assertEqual(conf["values"], ["Engineering", "Sales"])
        self.assertEqual(conf["records"], records)

    def test_blank_does_not_match(self):
        records = [
            {"name": "Alice", "email": "", "phone": "555-0001"},
            {"name": "Bob", "email": "", "phone": "555-0002"},
        ]
        res = merge.merge_records(records, ["email", "phone"])
        self.assertEqual(len(res["merged"]), 2)
        names = [r["name"] for r in res["merged"]]
        self.assertEqual(names, ["Alice", "Bob"])

    def test_refuse_empty_match_on(self):
        with self.assertRaisesRegex(EngineError, "non-empty"):
            merge.merge_records([], [])


if __name__ == "__main__":
    unittest.main()
