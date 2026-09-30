"""Claims that are not bug fixes: "I changed this and nothing broke".

A refactor has no failing test to turn green, so fail-before/pass-after does not apply. The rule is
instead: the suite passed before, the OLD tests still pass on the NEW code, and the new tree's own
tests pass. Running the old tests on the new code is what stops the obvious cheat: delete the
tests your change breaks.
"""
import sys
import tempfile
import unittest
from pathlib import Path

from axiom1 import Axiom, AxiomError, REFUTED, WITNESSED
from test_core import FIXED, REAL_TEST, Repo

EQUIVALENT = "def add(a, b):\n    return sum((a, b))\n"      # same behaviour, different code
BROKEN = "def add(a, b):\n    return a * b\n"                  # a "refactor" that breaks add
OTHER_TEST = ("import unittest\n\n"
              "class Other(unittest.TestCase):\n    def test_other(self):\n        self.assertEqual(1 + 1, 2)\n")
HOLDOUT = ("import unittest\nfrom calc import add\n\n"
           "class Held(unittest.TestCase):\n    def test_general(self):\n"
           "        for a, b in [(10, -4), (0, 0), (-7, -8)]:\n            self.assertEqual(add(a, b), a + b)\n")


class NoRegression(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Repo(Path(self.tmp.name) / "repo")
        # a healthy starting point: add() works and is tested
        self.good = self.repo.branch_from_base("good", {"calc.py": FIXED, "tests/test_calc.py": REAL_TEST})
        self.repo.git("merge", "-q", "--ff-only", "good")   # the healthy code is what main holds
        self.holdout = Path(self.tmp.name) / "holdout"
        self.holdout.mkdir()
        (self.holdout / "test_held.py").write_text(HOLDOUT, encoding="utf-8")
        self.ax = Axiom()
        self.ax.register_check("unit", self.repo.root, [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                               ["tests/"], holdout=str(self.holdout))
        self.ax.join("nemotron-1")

    def tearDown(self):
        self.ax.db.close()
        self.tmp.cleanup()

    def _change(self, name, files, delete=()):
        self.repo.git("checkout", "-q", "-b", name, self.good)
        for rel, text in files.items():
            self.repo.write(rel, text)
        for rel in delete:
            self.repo.git("rm", "-q", rel)
        sha = self.repo.commit(name)
        self.repo.git("checkout", "-q", "main")
        return sha

    def _verify(self, before, after, statement="refactored add"):
        c = self.ax.claim("nemotron-1", statement, "unit", before, after, kind="no_regression")
        return self.ax.verify(c["id"])

    def test_an_equivalent_refactor_is_witnessed(self):
        v = self._verify(self.good, self._change("eq", {"calc.py": EQUIVALENT}))
        self.assertEqual(v["label"], WITNESSED, v["reason"])
        self.assertIn("no regression", v["reason"])

    def test_a_refactor_that_breaks_behaviour_is_refuted(self):
        v = self._verify(self.good, self._change("broken", {"calc.py": BROKEN}))
        self.assertEqual(v["label"], REFUTED)

    def test_deleting_the_tests_you_broke_is_refuted(self):
        # the cheat: break add(), delete its test, leave an unrelated test so the new suite is green
        after = self._change("delete", {"calc.py": BROKEN, "tests/test_other.py": OTHER_TEST},
                             delete=["tests/test_calc.py"])
        v = self._verify(self.good, after)
        self.assertEqual(v["label"], REFUTED)
        self.assertIn("tests that passed before", v["reason"])

    def test_breaking_only_what_the_hidden_tests_check_is_refuted(self):
        # add(2, 3) still 5, everything else wrong: the visible test passes, the held-out ones do not
        sneaky = "def add(a, b):\n    return 5 if (a, b) == (2, 3) else 0\n"
        v = self._verify(self.good, self._change("sneaky", {"calc.py": sneaky}))
        self.assertEqual(v["label"], REFUTED)
        self.assertIn("held-out", v["reason"])

    def test_no_baseline_no_claim(self):
        # the starting point already fails its own tests: "nothing broke" has nothing to compare to
        broken_start = self.repo.branch_from_base("bad-start", {"tests/test_calc.py": REAL_TEST})
        self.repo.git("checkout", "-q", "-b", "bad-refactor", broken_start)
        self.repo.write("calc.py", "def add(a, b):\n    return -(b - a)\n")
        after = self.repo.commit("bad-refactor")
        self.repo.git("checkout", "-q", "main")
        v = self._verify(broken_start, after)
        self.assertEqual(v["label"], REFUTED)
        self.assertIn("already fail", v["reason"])

    def test_the_fact_says_what_was_proven_not_what_was_claimed(self):
        v = self._verify(self.good, self._change("eq", {"calc.py": EQUIVALENT}),
                         statement="fixed every bug in the project")
        self.assertEqual(v["label"], WITNESSED)
        [fact] = self.ax.briefing("nemotron-1")["facts"]
        self.assertTrue(fact["content"].startswith("[no regression]"), fact["content"])

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(AxiomError):
            self.ax.claim("nemotron-1", "x", "unit", self.repo.base, self.good, kind="trust_me")


if __name__ == "__main__":
    unittest.main()
