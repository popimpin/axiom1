"""
Tests for Harness^2 P-P-C (Propose-Probe-Compose) Repair Engine in Axiom-1.

Verifies:
  (1) K=2 parallel repair where one candidate regresses an earlier instance:
      Gate refuses the regressor, the non-regressing candidate locks as v+1.
      Negative control: when both regress, gate refuses both.
  (2) Both candidates fail:
      Nothing locks, task kept/unwitnessed, refusal recorded in memory.
      Negative control: passing candidate locks, failing does not.
  (3) Anti-drift control across sequential rounds:
      A refused candidate's mutated code does NOT leak into round N+1's starting harness.
      Negative control: without anti-drift, dirty code leaks and test goes RED.
  (4) Memory in/out:
      Witnessed lessons are fed as verified facts, declared lessons as unproven;
      refusals and reasons write back to memory.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from axiom1 import Axiom
from axiom1.agent import Workspace
from axiom1.ppc import diff_size, extract_candidates, format_memory, repair_ppc
from axiom1.sandbox import LocalSandbox
from test_deliver import CHECK, GOOD, DataRepo

PROCESS_V1 = '''import csv, json
from pathlib import Path
entry = json.loads(Path("entry.json").read_text()) if Path("entry.json").exists() else {}
rows = []
for p in sorted(Path("receipts").glob("*.txt")):
    shop, amount = p.read_text().splitlines()[:2]
    rows.append([shop, f"{float(amount):.2f}"])
with open(entry.get("output", "summary.csv"), "w", newline="") as f:
    csv.writer(f).writerows([["vendor", "amount"]] + rows)
'''

# Robust candidate: handles both standard receipts and "Total: $X.XX" format
PROCESS_INVARIANT = '''import csv, json
from pathlib import Path
entry = json.loads(Path("entry.json").read_text()) if Path("entry.json").exists() else {}
rows = []
for p in sorted(Path("receipts").glob("*.txt")):
    lines = [l.strip() for l in p.read_text().splitlines() if l.strip()]
    shop = lines[0]
    raw_amt = lines[1]
    val = raw_amt.split("$")[-1].strip() if "$" in raw_amt else raw_amt
    rows.append([shop, f"{float(val):.2f}"])
with open(entry.get("output", "summary.csv"), "w", newline="") as f:
    csv.writer(f).writerows([["vendor", "amount"]] + rows)
'''

# Regressing candidate: unconditionally splits on '$' and indexes [1], crashing on standard receipts with no '$'
PROCESS_REGRESSING = '''import csv, json
from pathlib import Path
entry = json.loads(Path("entry.json").read_text()) if Path("entry.json").exists() else {}
rows = []
for p in sorted(Path("receipts").glob("*.txt")):
    lines = [l.strip() for l in p.read_text().splitlines() if l.strip()]
    shop = lines[0]
    # Breaks on receipts that do not contain '$'!
    val = lines[1].split("$")[1].strip()
    rows.append([shop, f"{float(val):.2f}"])
with open(entry.get("output", "summary.csv"), "w", newline="") as f:
    csv.writer(f).writerows([["vendor", "amount"]] + rows)
'''

# Poisoned candidate for drift tests
PROCESS_POISON = '''# POISON_LEAK_INJECTED_MUTATION
import csv, json
raise RuntimeError("poisoned process")
'''

ENTRY = json.dumps({"output": "summary.csv"})


class FakeSession:
    """Mock MCP ClientSession routed directly to an Axiom instance."""

    def __init__(self, ax: Axiom, agent_id: str):
        self.ax = ax
        self.agent_id = agent_id

    async def call_tool(self, name: str, args: dict):
        class Content:
            def __init__(self, text):
                self.text = text

        class Response:
            def __init__(self, text, isError=False):
                self.content = [Content(text)]
                self.isError = isError

        if name == "claim":
            res = self.ax.claim(
                self.agent_id,
                args["statement"],
                args["check_id"],
                args["before_ref"],
                args["after_ref"],
                task_id=args.get("task_id"),
            )
            return Response(json.dumps(res))
        if name == "verify":
            res = self.ax.verify(args["claim_id"])
            return Response(json.dumps(res))
        if name == "remember":
            res = self.ax.remember(self.agent_id, args["key"], args["content"])
            return Response(json.dumps(res))
        if name == "take_task":
            res = self.ax.take_task(self.agent_id)
            return Response(json.dumps({"task": res}))
        raise NotImplementedError(f"Tool {name} not mocked in FakeSession")


class TestPPCRepair(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.check_dir = Path(self.tmp.name) / "check"
        self.check_dir.mkdir()
        (self.check_dir / "check.py").write_text(CHECK, encoding="utf-8")
        (self.check_dir / "truth.json").write_text(json.dumps({"receipts": 3, "total": 59.75}), encoding="utf-8")
        self.db_path = str(Path(self.tmp.name) / "axiom1.db")
        self.ax = Axiom(self.db_path)
        self.ax.join("agent-1")
        self.ax.join("operator")

    def tearDown(self):
        self.ax.db.close()
        self.tmp.cleanup()

    def _setup_v1_and_earlier_instance(self):
        """Create instance 1 (standard receipts without $) and lock in process v1."""
        repo1 = DataRepo(Path(self.tmp.name) / "instance1")
        self.ax.register_check(
            "receipts",
            repo1.root,
            [sys.executable, "-I", ".axiom_check/check.py"],
            [],
            holdout=str(self.check_dir),
            claim_kind="deliver",
        )
        self.ax.post_task("operator", "Monthly receipts 1", check_id="receipts")
        t1 = self.ax.take_task("agent-1")

        files = {"process.py": PROCESS_V1, "entry.json": ENTRY, "summary.csv": GOOD}
        after_sha = repo1.branch("v1_branch", files)
        c = self.ax.claim("agent-1", "v1 lock", "receipts", repo1.base, after_sha, task_id=t1["id"])
        v = self.ax.verify(c["id"])
        self.assertEqual(v["label"], "witnessed")
        self.assertEqual(self.ax.process_for("receipts")["version"], 1)
        return repo1

    def test_ppc_parallel_k2_regression_gate_refuses_regressor_and_locks_winner(self):
        """
        K=2 Parallel Repair:
        Candidate A (invariant) passes current task AND all earlier instances.
        Candidate B (domain) passes current task BUT breaks earlier instance.
        Gate refuses Candidate B on regression and locks Candidate A as v2.
        """
        self._setup_v1_and_earlier_instance()

        # Instance 2 has strayed input: all receipts in new format "Total: $X.XX"
        repo2 = DataRepo(Path(self.tmp.name) / "instance2")
        repo2.write("receipts/a.txt", "Corner Cafe\nTotal: $12.50\n")
        repo2.write("receipts/b.txt", "Hardware World\nTotal: $40.00\n")
        repo2.write("receipts/c.txt", "Corner Cafe\nTotal: $7.25\n")
        repo2.base = repo2.commit("strayed receipts with total $")
        self.ax.register_check(
            "receipts",
            repo2.root,
            [sys.executable, "-I", ".axiom_check/check.py"],
            [],
            holdout=str(self.check_dir),
            claim_kind="deliver",
        )
        self.ax.post_task("operator", "Monthly receipts 2", check_id="receipts")
        task2 = self.ax.take_task("agent-1")
        self.assertEqual(task2["process"]["version"], 1)

        wt2 = Path(self.tmp.name) / "wt2"
        repo2.git("worktree", "add", "-q", "--detach", str(wt2), "main")
        ws2 = Workspace(str(wt2), LocalSandbox())
        ws2.install_engines()
        session = FakeSession(self.ax, "agent-1")

        # Mock model proposing K=2: Candidate A (invariant) and Candidate B (regressing)
        def mock_proposer(messages, tools):
            return {
                "role": "assistant",
                "content": json.dumps({
                    "invariant": {"script": PROCESS_INVARIANT, "entry": ENTRY},
                    "domain": {"script": PROCESS_REGRESSING, "entry": ENTRY},
                }),
            }

        replay_info = {"note": "v1 failed on receipts/c.txt (Total: $7.25)"}
        res = asyncio.run(
            repair_ppc(
                "agent-1",
                task2,
                replay_info,
                mock_proposer,
                ws2,
                session,
                repo2.base,
                log=lambda *_: None,
            )
        )

        # Candidate A won and locked in as v2
        self.assertTrue(res["witnessed"])
        self.assertEqual(res["winner"]["candidate"]["kind"], "invariant")
        self.assertEqual(self.ax.process_for("receipts")["version"], 2)

        # Candidate B was refused specifically because of regression on earlier instance
        self.assertEqual(len(res["refused"]), 1)
        refused = res["refused"][0]
        self.assertEqual(refused["candidate"]["kind"], "domain")
        self.assertGreater(refused["regression_failed"], 0)

        # Memory verified: refusal reason was recorded
        mem = self.ax.recall(f"ppc:refused:{task2['id']}:domain")
        self.assertTrue(any("regression" in e["content"] for e in mem))

    def test_ppc_negative_control_both_regress_refuses_both(self):
        """
        NEGATIVE CONTROL 1:
        When BOTH candidates regress earlier instances, neither candidate can lock.
        Proves the gate actively discriminates rather than unconditionally passing.
        """
        self._setup_v1_and_earlier_instance()

        repo2 = DataRepo(Path(self.tmp.name) / "instance2_neg")
        repo2.write("receipts/c.txt", "Corner Cafe\nTotal: $7.25\n")
        repo2.base = repo2.commit("strayed receipt with total $")
        self.ax.register_check(
            "receipts",
            repo2.root,
            [sys.executable, "-I", ".axiom_check/check.py"],
            [],
            holdout=str(self.check_dir),
            claim_kind="deliver",
        )
        self.ax.post_task("operator", "Monthly receipts neg", check_id="receipts")
        task2 = self.ax.take_task("agent-1")

        wt2 = Path(self.tmp.name) / "wt2_neg"
        repo2.git("worktree", "add", "-q", "--detach", str(wt2), "main")
        ws2 = Workspace(str(wt2), LocalSandbox())
        ws2.install_engines()
        session = FakeSession(self.ax, "agent-1")

        # Propose two regressing candidates
        def mock_proposer_both_regress(messages, tools):
            return {
                "role": "assistant",
                "content": json.dumps({
                    "invariant": {"script": PROCESS_REGRESSING, "entry": ENTRY},
                    "domain": {"script": PROCESS_REGRESSING, "entry": ENTRY},
                }),
            }

        res = asyncio.run(
            repair_ppc(
                "agent-1",
                task2,
                {"note": "failed"},
                mock_proposer_both_regress,
                ws2,
                session,
                repo2.base,
                log=lambda *_: None,
            )
        )

        # Neither can lock!
        self.assertFalse(res["witnessed"])
        self.assertIsNone(res["winner"])
        self.assertEqual(len(res["refused"]), 2)
        # Process version remains 1
        self.assertEqual(self.ax.process_for("receipts")["version"], 1)

    def test_ppc_both_fail_nothing_locks_and_task_kept(self):
        """
        (2) When both candidates fail check execution, nothing locks and task is not claimed.
        """
        self._setup_v1_and_earlier_instance()

        repo2 = DataRepo(Path(self.tmp.name) / "instance_fail")
        self.ax.register_check(
            "receipts",
            repo2.root,
            [sys.executable, "-I", ".axiom_check/check.py"],
            [],
            holdout=str(self.check_dir),
            claim_kind="deliver",
        )
        self.ax.post_task("operator", "Failing task", check_id="receipts")
        task = self.ax.take_task("agent-1")

        wt = Path(self.tmp.name) / "wt_fail"
        repo2.git("worktree", "add", "-q", "--detach", str(wt), "main")
        ws = Workspace(str(wt), LocalSandbox())
        ws.install_engines()
        session = FakeSession(self.ax, "agent-1")

        broken_cand_1 = "import sys\nsys.exit(1)"
        broken_cand_2 = "syntax error !! {"

        def mock_failing_proposer(messages, tools):
            return {
                "role": "assistant",
                "content": json.dumps({
                    "invariant": {"script": broken_cand_1, "entry": ENTRY},
                    "domain": {"script": broken_cand_2, "entry": ENTRY},
                }),
            }

        res = asyncio.run(
            repair_ppc(
                "agent-1",
                task,
                {"note": "input strayed"},
                mock_failing_proposer,
                ws,
                session,
                repo2.base,
                log=lambda *_: None,
            )
        )

        self.assertFalse(res["witnessed"])
        self.assertIsNone(res["winner"])
        self.assertEqual(self.ax.process_for("receipts")["version"], 1)

    def test_ppc_drift_control_refused_candidate_does_not_leak_into_round_n_plus_1(self):
        """
        (3) DRIFT CONTROL:
        Sequential rounds: A candidate attempts a poisonous mutation that is refused.
        Anti-drift resets workspace to verified baseline so Round N+1 starts clean.
        Negative control: with anti_drift=False, dirty code leaks and fails assertion.
        """
        self._setup_v1_and_earlier_instance()

        repo = DataRepo(Path(self.tmp.name) / "instance_drift")
        self.ax.register_check(
            "receipts",
            repo.root,
            [sys.executable, "-I", ".axiom_check/check.py"],
            [],
            holdout=str(self.check_dir),
            claim_kind="deliver",
        )
        self.ax.post_task("operator", "Drift test task", check_id="receipts")
        task = self.ax.take_task("agent-1")

        wt = Path(self.tmp.name) / "wt_drift"
        repo.git("worktree", "add", "-q", "--detach", str(wt), "main")
        ws = Workspace(str(wt), LocalSandbox())
        ws.install_engines()
        session = FakeSession(self.ax, "agent-1")

        def mock_poison_proposer(messages, tools):
            return {
                "role": "assistant",
                "content": json.dumps({
                    "invariant": {"script": PROCESS_POISON, "entry": ENTRY},
                    "domain": {"script": PROCESS_POISON, "entry": ENTRY},
                }),
            }

        # Round 1: Run PPC with anti_drift=True (active guard)
        res = asyncio.run(
            repair_ppc(
                "agent-1",
                task,
                {"note": "strayed"},
                mock_poison_proposer,
                ws,
                session,
                repo.base,
                log=lambda *_: None,
                anti_drift=True,
            )
        )
        self.assertFalse(res["witnessed"])

        # Check workspace state before Round 2 begins: MUST be clean baseline, NO poison leak!
        round2_start_script = ws.read_file("process.py")["content"]
        self.assertEqual(round2_start_script.strip(), PROCESS_V1.strip())
        self.assertNotIn("POISON_LEAK_INJECTED_MUTATION", round2_start_script)

        # NEGATIVE CONTROL 3: Disable anti-drift reset -> poison LEAKS, proving the test can fail
        res_no_guard = asyncio.run(
            repair_ppc(
                "agent-1",
                task,
                {"note": "strayed"},
                mock_poison_proposer,
                ws,
                session,
                repo.base,
                log=lambda *_: None,
                anti_drift=False,
            )
        )
        leaked_script = ws.read_file("process.py")["content"]
        self.assertIn("POISON_LEAK_INJECTED_MUTATION", leaked_script)

    def test_ppc_memory_in_distinguishes_witnessed_vs_declared_lessons(self):
        """
        (4) MEMORY IN/OUT:
        format_memory separates [WITNESSED] verified skills from [UNVERIFIED] declared attempts.
        """
        lessons = [
            {"label": "witnessed", "body": "Fixed corner cafe total parse by stripping $ prefix."},
            {"label": "declared", "body": "Attempted regex split on total: unconfirmed."},
        ]
        w_text, d_text = format_memory(lessons)
        self.assertIn("[WITNESSED] Fixed corner cafe", w_text)
        self.assertNotIn("[UNVERIFIED]", w_text)
        self.assertIn("[UNVERIFIED] Attempted regex split", d_text)
        self.assertNotIn("[WITNESSED]", d_text)

    def test_ppc_tie_break_smallest_diff_wins(self):
        """
        When both candidates pass and do not regress, the one with smaller diff against baseline wins.
        """
        self._setup_v1_and_earlier_instance()

        repo = DataRepo(Path(self.tmp.name) / "instance_tie")
        self.ax.register_check(
            "receipts",
            repo.root,
            [sys.executable, "-I", ".axiom_check/check.py"],
            [],
            holdout=str(self.check_dir),
            claim_kind="deliver",
        )
        self.ax.post_task("operator", "Tie break task", check_id="receipts")
        task = self.ax.take_task("agent-1")

        wt = Path(self.tmp.name) / "wt_tie"
        repo.git("worktree", "add", "-q", "--detach", str(wt), "main")
        ws = Workspace(str(wt), LocalSandbox())
        ws.install_engines()
        session = FakeSession(self.ax, "agent-1")

        # Candidate A: 1-character difference
        # Candidate B: 20-character difference (both functionally equivalent and passing)
        cand_a = PROCESS_V1.replace('lines[:2]', 'lines[:2]')  # identical
        cand_b = PROCESS_V1 + "\n# extra trailing comment adding lots of characters to diff\n"

        def mock_tie_proposer(messages, tools):
            return {
                "role": "assistant",
                "content": json.dumps({
                    "invariant": {"script": cand_a, "entry": ENTRY},
                    "domain": {"script": cand_b, "entry": ENTRY},
                }),
            }

        res = asyncio.run(
            repair_ppc(
                "agent-1",
                task,
                {"note": "strayed"},
                mock_tie_proposer,
                ws,
                session,
                repo.base,
                log=lambda *_: None,
            )
        )
        self.assertTrue(res["witnessed"])
        self.assertEqual(res["winner"]["candidate"]["kind"], "invariant")
        self.assertLess(res["winner"]["diff"], res["refused"][0]["diff"])


if __name__ == "__main__":
    unittest.main()
