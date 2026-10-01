"""Deliver claims: the job is files, not code, and the person who asked should not have to check them.

Universal guards run on every delivery (nothing lost, well-formed CSV/JSON), then the task's own
acceptance check, which the agent never sees and cannot replace. Most of these tests are a bad
delivery trying to pass.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from axiom1 import Axiom, AxiomError

CHECK = r'''
import csv, json, sys
from pathlib import Path
truth = json.loads(Path(".axiom_check/truth.json").read_text())
out = Path("summary.csv")
if not out.exists():
    print("PUBLIC: summary.csv was not found in the workspace")
    sys.exit(1)
rows = list(csv.DictReader(out.open()))
total = round(sum(float(r["amount"]) for r in rows), 2)
if len(rows) != truth["receipts"]:
    print(f"PUBLIC: summary.csv has {len(rows)} receipts; it should have one row per receipt")
    sys.exit(1)
if total != truth["total"]:
    print(f"PUBLIC: the amounts do not add up to the receipts' total")
    print(f"expected {truth['total']} got {total}")          # private: the hidden truth
    sys.exit(1)
print("PUBLIC: every receipt is in the summary and the total matches")
'''
RECEIPTS = {"receipts/a.txt": "Corner Cafe\n12.50\n", "receipts/b.txt": "Hardware World\n40.00\n",
            "receipts/c.txt": "Corner Cafe\n7.25\n"}
GOOD = "vendor,amount\nCorner Cafe,12.50\nHardware World,40.00\nCorner Cafe,7.25\n"
SHORT = "vendor,amount\nCorner Cafe,12.50\nHardware World,40.00\n"
WRONG_TOTAL = "vendor,amount\nCorner Cafe,12.50\nHardware World,4.00\nCorner Cafe,7.25\n"


class DataRepo:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True)
        self.git("init", "-q", "-b", "main")
        for rel, text in RECEIPTS.items():
            self.write(rel, text)
        self.base = self.commit("my receipts")

    def git(self, *a):
        return subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                              capture_output=True, text=True, check=True).stdout.strip()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def branch(self, name, files=None, delete=(), rename=()):
        self.git("checkout", "-q", "-b", name, self.base)
        for rel, text in (files or {}).items():
            self.write(rel, text)
        for rel in delete:
            self.git("rm", "-q", rel)
        for src, dst in rename:
            (self.root / dst).parent.mkdir(parents=True, exist_ok=True)
            self.git("mv", src, dst)
        sha = self.commit(name)
        self.git("checkout", "-q", "main")
        return sha


class Deliver(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = DataRepo(Path(self.tmp.name) / "data")
        self.check = Path(self.tmp.name) / "check"
        self.check.mkdir()
        (self.check / "check.py").write_text(CHECK, encoding="utf-8")
        (self.check / "truth.json").write_text(json.dumps({"receipts": 3, "total": 59.75}), encoding="utf-8")
        self.ax = Axiom()
        self.ax.register_check("receipts", self.repo.root, [sys.executable, "-I", ".axiom_check/check.py"], [],
                               holdout=str(self.check), claim_kind="deliver")
        self.ax.join("agent")

    def tearDown(self):
        self.ax.db.close()
        self.tmp.cleanup()

    def _verify(self, after):
        return self.ax.verify(self.ax.claim("agent", "made the summary", "receipts", self.repo.base, after)["id"])

    def test_a_correct_delivery_is_witnessed_and_says_why(self):
        v = self._verify(self.repo.branch("good", {"summary.csv": GOOD}))
        self.assertEqual(v["label"], "witnessed", v["reason"])
        self.assertIn("total matches", " ".join(v["evidence"]["after"]["feedback"]))
        [fact] = self.ax.briefing("agent")["facts"]
        self.assertTrue(fact["content"].startswith("[delivered]"))

    def test_a_missing_receipt_is_refuted_with_feedback_but_not_the_answer(self):
        v = self._verify(self.repo.branch("short", {"summary.csv": SHORT}))
        self.assertEqual(v["label"], "refuted")
        self.assertIn("one row per receipt", " ".join(v["evidence"]["after"]["feedback"]))
        self.assertNotIn("59.75", json.dumps(v))            # the hidden total never reaches the agent

    def test_a_wrong_amount_is_refuted_and_the_truth_stays_private(self):
        v = self._verify(self.repo.branch("wrong", {"summary.csv": WRONG_TOTAL}))
        self.assertEqual(v["label"], "refuted")
        self.assertNotIn("59.75", json.dumps(v))
        [e] = [e for e in self.ax.events() if e["kind"] == "check_output"]
        self.assertIn("59.75", e["detail"])                  # the operator can see it

    def test_a_planted_check_never_runs(self):
        fake = {".axiom_check/check.py": "print('PUBLIC: all good')\n", "summary.csv": SHORT}
        v = self._verify(self.repo.branch("plant", fake))
        self.assertEqual(v["label"], "refuted")

    def test_a_lost_file_is_refuted_by_the_universal_guard(self):
        v = self._verify(self.repo.branch("lose", {"summary.csv": GOOD}, delete=["receipts/c.txt"]))
        self.assertEqual(v["label"], "refuted")
        self.assertIn("receipts/c.txt", v["reason"])

    def test_moving_a_file_is_not_losing_it(self):
        v = self._verify(self.repo.branch("move", {"summary.csv": GOOD},
                                          rename=[("receipts/c.txt", "receipts/cafe/c.txt")]))
        self.assertEqual(v["label"], "witnessed", v["reason"])

    def test_a_malformed_csv_is_refuted_before_the_check_runs(self):
        ragged = "vendor,amount\nCorner Cafe,12.50,extra\nHardware World,40.00\nCorner Cafe,7.25\n"
        v = self._verify(self.repo.branch("ragged", {"summary.csv": ragged}))
        self.assertEqual(v["label"], "refuted")
        self.assertIn("different numbers of columns", v["reason"])

    def test_broken_json_is_refuted(self):
        v = self._verify(self.repo.branch("json", {"summary.csv": GOOD, "notes.json": "{not json"}))
        self.assertEqual(v["label"], "refuted")
        self.assertIn("notes.json", v["reason"])

    def test_a_task_that_means_deletion_can_allow_it(self):
        (self.check / "guards.json").write_text(json.dumps({"allow_deleting": ["receipts/*"]}), encoding="utf-8")
        v = self._verify(self.repo.branch("allowed", {"summary.csv": GOOD}, delete=["receipts/c.txt"]))
        self.assertEqual(v["label"], "witnessed", v["reason"])

    def test_nothing_done_is_refuted(self):
        v = self._verify(self.repo.branch("nothing", {"other.txt": "hi\n"}))
        self.assertEqual(v["label"], "refuted")
        self.assertIn("not found", " ".join(v["evidence"]["after"]["feedback"]))


class Registration(unittest.TestCase):
    def test_deliver_needs_an_acceptance_check_and_kinds_do_not_mix(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            repo = DataRepo(Path(tmp) / "data")
            ax = Axiom()
            with self.assertRaises(AxiomError):
                ax.register_check("x", repo.root, ["python"], [], claim_kind="deliver")
            ax.register_check("code", repo.root, ["python"], ["tests/"])
            ax.join("a")
            other = repo.branch("b", {"x.txt": "1\n"})
            with self.assertRaises(AxiomError):
                ax.claim("a", "x", "code", repo.base, other, kind="deliver")
            ax.db.close()


if __name__ == "__main__":
    unittest.main()
