"""Axiom-1 core tests. Most of these are an agent trying to cheat, and the rules refusing."""
import inspect
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from axiom1 import Axiom, AxiomError, DECLARED, WITNESSED, REFUTED, sha256

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
STILL_WRONG = "def add(a, b):\n    return a * b\n"
REAL_TEST = ("import unittest\nfrom calc import add\n\n"
             "class T(unittest.TestCase):\n    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
TAUTOLOGY = ("import unittest\n\n"
             "class T(unittest.TestCase):\n    def test_nothing(self):\n        self.assertTrue(True)\n")


class Repo:
    """A throwaway git repo with a buggy calc.py at commit `base`."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")
        self.write("calc.py", BUGGY)
        self.base = self.commit("buggy add")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), "-c", "user.name=t", "-c", "user.email=t@t",
                               *args], capture_output=True, text=True, check=True).stdout.strip()

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")

    def branch_from_base(self, name, files):
        self.git("checkout", "-q", "-b", name, self.base)
        for rel, text in files.items():
            self.write(rel, text)
        sha = self.commit(name)
        self.git("checkout", "-q", "main")
        return sha


CHECK_ARGV = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repo(self.tmp.name)
        self.now = [1000.0]
        self.ax = Axiom(clock=lambda: self.now[0])
        self.ax.register_check("unit", self.repo.root, CHECK_ARGV, ["tests"])
        self.ax.join("claude", ["shell"])
        self.ax.join("nemotron-1", [])
        self.ax.join("nemotron-2", [])

    def tearDown(self):
        self.ax.db.close()
        self.tmp.cleanup()


class AgentsCannotWitness(Base):
    def test_remember_is_always_declared(self):
        self.assertEqual(self.ax.remember("claude", "k", "the build is green")["label"], DECLARED)
        self.assertEqual(self.ax.recall("k")[0]["label"], DECLARED)

    def test_no_agent_facing_call_accepts_a_label(self):
        for name in ("remember", "claim", "send", "post_task", "take_task", "ack"):
            params = inspect.signature(getattr(self.ax, name)).parameters
            self.assertFalse({"label", "outcome", "outcome_source"} & set(params), name)
        with self.assertRaises(TypeError):
            self.ax.remember("claude", "k", "green", label=WITNESSED)

    def test_unknown_agent_is_refused(self):
        with self.assertRaises(AxiomError):
            self.ax.remember("ghost", "k", "hi")


class Messages(Base):
    def test_ack_must_echo_the_exact_bytes(self):
        m = self.ax.send("claude", "nemotron-1", "run the tests")
        with self.assertRaises(AxiomError):
            self.ax.ack("nemotron-1", m["id"], sha256("run the test"))
        self.assertEqual(self.ax.message_status(m["id"]), "sent")
        self.ax.ack("nemotron-1", m["id"], m["sha256"])
        self.assertEqual(self.ax.message_status(m["id"]), "delivered")

    def test_only_the_recipient_can_ack(self):
        m = self.ax.send("claude", "nemotron-1", "hello")
        with self.assertRaises(AxiomError):
            self.ax.ack("nemotron-2", m["id"], m["sha256"])
        self.assertEqual(self.ax.message_status(m["id"]), "sent")

    def test_acked_messages_leave_the_inbox(self):
        m = self.ax.send("claude", "nemotron-1", "hello")
        self.assertEqual(len(self.ax.inbox("nemotron-1")), 1)
        self.ax.ack("nemotron-1", m["id"], m["sha256"])
        self.assertEqual(self.ax.inbox("nemotron-1"), [])


class Verification(Base):
    def test_real_fix_is_witnessed(self):
        fix = self.repo.branch_from_base("fix", {"calc.py": FIXED, "tests/test_calc.py": REAL_TEST})
        c = self.ax.claim("nemotron-1", "add() adds", "unit", self.repo.base, fix)
        self.assertEqual(c["label"], DECLARED)
        v = self.ax.verify(c["id"])
        self.assertEqual(v["label"], WITNESSED, v)
        self.assertNotEqual(v["evidence"]["before"]["exit"], 0)
        self.assertEqual(v["evidence"]["after"]["exit"], 0)
        facts = self.ax.briefing("nemotron-2")["facts"]
        self.assertEqual([f["content"] for f in facts], ["add() adds"])

    def test_a_test_that_passes_on_the_unfixed_code_is_refuted(self):
        # The oracle hole: the "fix" ships its own test, which asserts nothing. The test file does
        # not exist at base, so without the overlay the before-run would error and look like a fail.
        fix = self.repo.branch_from_base("taut", {"tests/test_calc.py": TAUTOLOGY})
        v = self.ax.verify(self.ax.claim("nemotron-1", "fixed it", "unit", self.repo.base, fix)["id"])
        self.assertEqual(v["label"], REFUTED)
        self.assertIn("without the fix", v["reason"])
        self.assertEqual(v["evidence"]["before"]["exit"], 0)

    def test_a_fix_that_does_not_fix_is_refuted(self):
        fix = self.repo.branch_from_base("wrong", {"calc.py": STILL_WRONG, "tests/test_calc.py": REAL_TEST})
        v = self.ax.verify(self.ax.claim("nemotron-1", "fixed it", "unit", self.repo.base, fix)["id"])
        self.assertEqual(v["label"], REFUTED)
        self.assertIn("still fails", v["reason"])

    def test_a_fix_with_no_tests_is_refuted(self):
        fix = self.repo.branch_from_base("notest", {"calc.py": FIXED})
        v = self.ax.verify(self.ax.claim("nemotron-1", "fixed it", "unit", self.repo.base, fix)["id"])
        self.assertEqual(v["label"], REFUTED)

    def test_refs_are_pinned_at_claim_time(self):
        self.repo.branch_from_base("fix", {"calc.py": FIXED, "tests/test_calc.py": REAL_TEST})
        c = self.ax.claim("nemotron-1", "add() adds", "unit", self.repo.base, "fix")
        # after claiming, the agent points the branch at a tautology; the verdict must not move
        self.repo.git("checkout", "-q", "fix")
        self.repo.write("tests/test_calc.py", TAUTOLOGY)
        self.repo.write("calc.py", BUGGY)
        self.repo.commit("swap")
        self.repo.git("checkout", "-q", "main")
        self.assertEqual(self.ax.verify(c["id"])["label"], WITNESSED)

    def test_unregistered_check_is_refused(self):
        with self.assertRaises(AxiomError):
            self.ax.claim("nemotron-1", "x", "my-own-check", self.repo.base, self.repo.base)

    def test_verdict_is_final(self):
        fix = self.repo.branch_from_base("taut", {"tests/test_calc.py": TAUTOLOGY})
        c = self.ax.claim("nemotron-1", "fixed it", "unit", self.repo.base, fix)
        self.ax.verify(c["id"])
        self.assertEqual(self.ax.verify(c["id"])["label"], REFUTED)


class TasksAndTrust(Base):
    def test_tasks_go_by_capability(self):
        self.ax.post_task("claude", "run the shell job", ["shell"])
        self.assertIsNone(self.ax.take_task("nemotron-1"))
        self.assertEqual(self.ax.take_task("claude")["title"], "run the shell job")

    def test_expired_lease_returns_task_and_counts_against_reliability_not_honesty(self):
        t = self.ax.post_task("claude", "fix add")
        self.ax.take_task("nemotron-1", lease_s=60)
        self.now[0] += 61
        self.assertEqual(self.ax.take_task("nemotron-2")["id"], t["id"])
        rec = self.ax.track_record("nemotron-1")
        self.assertEqual((rec["leases_expired"], rec["refuted"]), (1, 0))

    def test_only_the_lease_holder_can_claim_a_task(self):
        t = self.ax.post_task("claude", "fix add")
        self.ax.take_task("nemotron-1")
        fix = self.repo.branch_from_base("fix", {"calc.py": FIXED, "tests/test_calc.py": REAL_TEST})
        with self.assertRaises(AxiomError):
            self.ax.claim("nemotron-2", "fixed", "unit", self.repo.base, fix, task_id=t["id"])

    def test_refuted_claim_reopens_task_and_the_next_agent_finishes_it(self):
        t = self.ax.post_task("claude", "fix add")
        self.ax.take_task("nemotron-1")
        bad = self.repo.branch_from_base("taut", {"tests/test_calc.py": TAUTOLOGY})
        c1 = self.ax.claim("nemotron-1", "fixed", "unit", self.repo.base, bad, task_id=t["id"])
        self.assertEqual(self.ax.verify(c1["id"])["label"], REFUTED)
        self.assertEqual(self.ax.track_record("nemotron-1")["refuted"], 1)

        self.assertEqual(self.ax.take_task("nemotron-2")["id"], t["id"])
        good = self.repo.branch_from_base("fix", {"calc.py": FIXED, "tests/test_calc.py": REAL_TEST})
        c2 = self.ax.claim("nemotron-2", "add() adds", "unit", self.repo.base, good, task_id=t["id"])
        self.assertEqual(self.ax.verify(c2["id"])["label"], WITNESSED)
        self.assertIsNone(self.ax.take_task("claude"))  # task is done, nothing left in the pool

    def test_late_joiner_is_briefed_from_state(self):
        t = self.ax.post_task("claude", "fix add")
        self.ax.take_task("nemotron-1")
        bad = self.repo.branch_from_base("taut", {"tests/test_calc.py": TAUTOLOGY})
        self.ax.verify(self.ax.claim("nemotron-1", "fixed", "unit", self.repo.base, bad, task_id=t["id"])["id"])
        b = self.ax.join("late", [])
        self.assertEqual([r["statement"] for r in b["refuted"]], ["fixed"])
        self.assertEqual([x["title"] for x in b["tasks_you_can_take"]], ["fix add"])
        self.assertEqual(b["agents"]["nemotron-1"]["refuted"], 1)


if __name__ == "__main__":
    unittest.main()
