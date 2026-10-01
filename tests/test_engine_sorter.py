import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axiom1.engines import EngineError, sorter  # noqa: E402


class TestPlanMoves(unittest.TestCase):
    def test_plan_moves_standard(self):
        files = ["doc.PDF", "notes.txt", "photo.png", "Documents/already_there.pdf"]
        rules = [
            {"match": "*.pdf", "folder": "Documents"},
            {"match": "*.txt", "folder": "Notes"},
        ]
        plan = sorter.plan_moves(files, rules)
        self.assertEqual(
            plan,
            [
                {"from": "doc.PDF", "to": "Documents/doc.PDF"},
                {"from": "notes.txt", "to": "Notes/notes.txt"},
            ],
        )

    def test_conflicting_rules_refused(self):
        files = ["invoice.pdf"]
        rules = [
            {"match": "*.pdf", "folder": "Documents"},
            {"match": "invoice.*", "folder": "Finance"},
        ]
        with self.assertRaisesRegex(EngineError, "conflicting folders"):
            sorter.plan_moves(files, rules)

    def test_destination_collision_refused(self):
        files = ["subA/file.txt", "subB/file.txt"]
        rules = [{"match": "*.txt", "folder": "Archive"}]
        with self.assertRaisesRegex(EngineError, "collision"):
            sorter.plan_moves(files, rules)


class TestApplyMoves(unittest.TestCase):
    def setUp(self):
        self.old_cwd = os.getcwd()
        self.tmp_dir = tempfile.mkdtemp()
        os.chdir(self.tmp_dir)

    def tearDown(self):
        os.chdir(self.old_cwd)

    def test_atomic_apply_moves(self):
        # Create source files
        Path("file1.txt").write_text("content 1", encoding="utf-8")
        Path("file2.pdf").write_text("content 2", encoding="utf-8")

        moves = [
            {"from": "file1.txt", "to": "docs/file1.txt"},
            {"from": "file2.pdf", "to": "docs/file2.pdf"},
        ]
        res = sorter.apply_moves(moves)
        self.assertEqual(res, {"moved": 2})
        self.assertFalse(Path("file1.txt").exists())
        self.assertFalse(Path("file2.pdf").exists())
        self.assertEqual(Path("docs/file1.txt").read_text(encoding="utf-8"), "content 1")
        self.assertEqual(Path("docs/file2.pdf").read_text(encoding="utf-8"), "content 2")

    def test_refuse_if_source_missing(self):
        Path("present.txt").write_text("hi", encoding="utf-8")
        moves = [
            {"from": "present.txt", "to": "docs/present.txt"},
            {"from": "missing.txt", "to": "docs/missing.txt"},
        ]
        with self.assertRaisesRegex(EngineError, "does not exist"):
            sorter.apply_moves(moves)
        # All-or-nothing: present.txt must NOT have moved
        self.assertTrue(Path("present.txt").exists())
        self.assertFalse(Path("docs/present.txt").exists())

    def test_refuse_if_destination_already_exists(self):
        Path("a.txt").write_text("new content", encoding="utf-8")
        Path("docs").mkdir()
        Path("docs/a.txt").write_text("existing content", encoding="utf-8")

        moves = [{"from": "a.txt", "to": "docs/a.txt"}]
        with self.assertRaisesRegex(EngineError, "already exists"):
            sorter.apply_moves(moves)
        # Existing file is preserved
        self.assertEqual(Path("docs/a.txt").read_text(encoding="utf-8"), "existing content")
        self.assertTrue(Path("a.txt").exists())


if __name__ == "__main__":
    unittest.main()
