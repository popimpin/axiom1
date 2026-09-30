"""Plant a bug, fix it, collect a witnessed verdict, repeat. Every verdict would be genuine.

A claim only counts toward an agent's record, and only becomes a fact, when it starts from code the
collective already had: a commit on the check's base branch. Commit authorship cannot anchor this
(anyone can set a git author); ancestry can.
"""
import sys
import tempfile
import unittest
from pathlib import Path

from axiom1 import Axiom, WITNESSED
from test_core import BUGGY, FIXED, REAL_TEST, Repo

HEALTHY_TEST = REAL_TEST


class Anchoring(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Repo(Path(self.tmp.name) / "repo")
        # main moves to healthy, tested code: add() works
        self.repo.write("calc.py", FIXED)
        self.repo.write("tests/test_calc.py", HEALTHY_TEST)
        self.main = self.repo.commit("healthy")
        self.ax = Axiom()
        self.ax.register_check("unit", self.repo.root, [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                               ["tests/"])
        self.ax.join("nemotron-1")

    def tearDown(self):
        self.ax.db.close()
        self.tmp.cleanup()

    def _plant_and_fix(self, n):
        """The agent's own branch: break add(), then fix it. Its test fails before, passes after."""
        self.repo.git("checkout", "-q", "-b", f"farm{n}", self.main)
        self.repo.write("calc.py", BUGGY)
        self.repo.git("rm", "-q", "tests/test_calc.py")
        planted = self.repo.commit(f"plant {n}")
        self.repo.write("calc.py", FIXED)
        self.repo.write("tests/test_calc.py", HEALTHY_TEST)
        fixed = self.repo.commit(f"fix {n}")
        self.repo.git("checkout", "-q", "main")
        return planted, fixed

    def test_a_planted_bug_does_not_build_a_record(self):
        for n in range(3):
            planted, fixed = self._plant_and_fix(n)
            v = self.ax.verify(self.ax.claim("nemotron-1", "fixed add", "unit", planted, fixed)["id"])
            self.assertEqual(v["label"], WITNESSED)          # the verdict is true...
            self.assertFalse(v["counts"])                     # ...and worth nothing
        rec = self.ax.track_record("nemotron-1")
        self.assertEqual((rec["witnessed"], rec["unanchored"]), (0, 3))
        self.assertEqual(self.ax.briefing("nemotron-1")["facts"], [])

    def test_a_fix_to_code_on_main_counts(self):
        # a real bug that reached main, fixed by the agent
        self.repo.write("calc.py", BUGGY)
        self.repo.git("rm", "-q", "tests/test_calc.py")
        buggy_main = self.repo.commit("regression lands on main")
        self.repo.git("checkout", "-q", "-b", "realfix", buggy_main)
        self.repo.write("calc.py", FIXED)
        self.repo.write("tests/test_calc.py", HEALTHY_TEST)
        after = self.repo.commit("real fix")
        self.repo.git("checkout", "-q", "main")
        v = self.ax.verify(self.ax.claim("nemotron-1", "fixed add", "unit", buggy_main, after)["id"])
        self.assertEqual(v["label"], WITNESSED, v["reason"])
        self.assertTrue(v["counts"])
        self.assertEqual(self.ax.track_record("nemotron-1")["witnessed"], 1)
        self.assertEqual(len(self.ax.briefing("nemotron-1")["facts"]), 1)

    def test_the_base_branch_is_what_the_operator_registered(self):
        [c] = self.ax.list_checks()
        self.assertEqual(c["base"], "main")


if __name__ == "__main__":
    unittest.main()
