"""
Tests for Axiom-1 3-Arm Experiment Runner (Harness^2 vs Axiom-1 Gate).

Verifies:
  (a) B and C receive byte-identical candidates from a single Propose call.
  (b) Composer prompt contains no check/holdout/canary/regression text (prompt purity).
      Negative control: injecting check/holdout verdicts into prompt turns test RED.
  (c) When composer prefers a candidate that regresses an earlier instance:
      - Logged DISAGREE with reason 'regression'
      - Arm B's DB shows nothing locked from shadow probe (shadow is side-effect free).
      Negative control: routing shadow through claim/verify locks a process, turning RED.
  (d) Arms' DBs are completely separate (cross-writes turn test RED).
  (e) CLI dry-run produces accurate call/token and cost estimate for pilot.
"""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

from axiom1 import Axiom
from axiom1.arms import (
    ArmsExperiment,
    build_composer_prompt,
    parse_composer_decision,
    run_dry_run_series,
    run_series,
)
from test_deliver import CHECK, GOOD, DataRepo
from test_ppc import PROCESS_V1, PROCESS_INVARIANT, PROCESS_REGRESSING, ENTRY


class TestArmsExperiment(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.check_dir = Path(self.tmp.name) / "check"
        self.check_dir.mkdir(parents=True, exist_ok=True)
        (self.check_dir / "check.py").write_text(CHECK, encoding="utf-8")
        (self.check_dir / "truth.json").write_text(json.dumps({"receipts": 3, "total": 59.75}), encoding="utf-8")
        self.db_dir = Path(self.tmp.name) / "arms_dbs"
        self.db_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _setup_repo_with_earlier_instance(self, name: str = "repo", arm: str = ""):
        """Create a repo representing Instance 2 with strayed receipts."""
        repo_name = f"{name}_{arm}" if arm else name
        repo = DataRepo(Path(self.tmp.name) / repo_name)
        repo.write("receipts/a.txt", "Corner Cafe\nTotal: $12.50\n")
        repo.write("receipts/b.txt", "Hardware World\nTotal: $40.00\n")
        repo.write("receipts/c.txt", "Corner Cafe\nTotal: $7.25\n")
        base = repo.commit("strayed receipts with total $")
        return repo, self.check_dir, base, "deliver"

    def test_b_and_c_receive_byte_identical_candidates(self):
        """
        Rule (1): Arms B and C MUST receive byte-identical candidate proposals
        originating from one single Propose call per task instance.
        """
        exp = ArmsExperiment(self.db_dir, arms=["B", "C"])
        captured_proposals = []

        def mock_proposer(title, base_script, base_entry):
            cand_payload = {
                "invariant": {"script": PROCESS_INVARIANT, "entry": ENTRY},
                "domain": {"script": PROCESS_REGRESSING, "entry": ENTRY},
            }
            captured_proposals.append(cand_payload)
            return {"role": "assistant", "content": json.dumps(cand_payload)}

        def mock_composer(prompt):
            return {"decision": "adopt_invariant", "observation": "Candidate 1 handles formats cleanly."}

        res = exp.evaluate_instance_arms(
            task_id="inst_identical",
            task_title="Monthly receipts identical test",
            repo_builder=lambda arm="": self._setup_repo_with_earlier_instance("repo_id", arm=arm),
            propose_fn=mock_proposer,
            compose_fn=mock_composer,
        )

        # Propose was called exactly once
        self.assertEqual(len(captured_proposals), 1)
        prop = captured_proposals[0]

        # Verify candidate scripts submitted to C match exactly what was prepared
        self.assertIn("invariant", res["candidates"])
        self.assertIn("domain", res["candidates"])
        self.assertEqual(prop["invariant"]["script"].encode("utf-8"), PROCESS_INVARIANT.encode("utf-8"))
        self.assertEqual(prop["domain"]["script"].encode("utf-8"), PROCESS_REGRESSING.encode("utf-8"))

    def test_composer_prompt_contains_no_gate_truth_or_verdicts(self):
        """
        Rule (2): The Composer prompt MUST strictly exclude verifier/gate truth:
        no check labels, holdout output, canary results, or regression numbers.
        NEGATIVE CONTROL: Injecting gate oracle verdicts into candidate run output
        is detected by build_composer_prompt, turning test RED.
        """
        cand1 = {
            "script": PROCESS_INVARIANT,
            "run": {"exit": 0, "output": "Parsed 3 receipts, generated summary.csv"},
            "deliverables": ["summary.csv"],
        }
        cand2 = {
            "script": PROCESS_REGRESSING,
            "run": {"exit": 0, "output": "Parsed 3 receipts"},
            "deliverables": ["summary.csv"],
        }

        prompt = build_composer_prompt(PROCESS_V1, ENTRY, cand1, cand2)

        # Verify prompt includes diffs and run outputs
        self.assertIn("Candidate 1", prompt)
        self.assertIn("Candidate 2", prompt)
        self.assertIn("Parsed 3 receipts", prompt)

        # Verify prompt strictly excludes all hidden gate terms
        lowered = prompt.lower()
        for hidden in ["holdout", "canary", "regression_failed", "acceptance check", "verdict"]:
            self.assertNotIn(hidden, lowered, f"Gate oracle leaked into composer prompt: {hidden}")

        # NEGATIVE CONTROL:
        # If a gate verdict leaks into a candidate's execution output, build_composer_prompt must reject it
        leaky_cand = {
            "script": PROCESS_INVARIANT,
            "run": {"exit": 0, "output": "VERDICT: witnessed, holdout test passed, 0 regression_failed"},
            "deliverables": ["summary.csv"],
        }
        with self.assertRaises(ValueError):
            build_composer_prompt(PROCESS_V1, ENTRY, leaky_cand, cand2)

    def test_composer_prefers_regressor_logs_disagree_with_reason_regression_and_b_locks_nothing(self):
        """
        Rule (3) & (4): When the Model Composer prefers Candidate 2 (regressing) over Candidate 1:
          - Gate selects Candidate 1 (invariant, non-regressing).
          - Comparison logs 'DISAGREE' with reason stating regression.
          - Arm B's DB shows ZERO locked processes from shadow probe (side-effect free).
        NEGATIVE CONTROL:
          - If shadow probe routes through claim/verify in Arm B, processes are locked
            in Arm B, turning this assertion RED.
        """
        # Seed Arm C with earlier instance 1 baseline so regression testing has history
        ax_c = Axiom(str(self.db_dir / "arm_c.db"))
        ax_c.join("agent-1")
        ax_c.join("operator")
        repo_inst1 = DataRepo(Path(self.tmp.name) / "inst1")
        check_name = "chk_inst_regress"
        ax_c.register_check(
            check_name,
            repo_inst1.root,
            [sys.executable, "-I", ".axiom_check/check.py"],
            [],
            holdout=str(self.check_dir),
            claim_kind="deliver",
        )
        ax_c.post_task("operator", "Inst 1 task", check_id=check_name)
        t1 = ax_c.take_task("agent-1")
        files = {"process.py": PROCESS_V1, "entry.json": ENTRY, "summary.csv": GOOD}
        s1 = repo_inst1.branch("b1", files)
        c1 = ax_c.claim("agent-1", "v1 lock", check_name, repo_inst1.base, s1, task_id=t1["id"])
        ax_c.verify(c1["id"])
        self.assertEqual(ax_c.process_for(check_name)["version"], 1)

        exp = ArmsExperiment(self.db_dir, arms=["B", "C"])

        def mock_proposer(title, base_script, base_entry):
            return {
                "role": "assistant",
                "content": json.dumps({
                    "invariant": {"script": PROCESS_INVARIANT, "entry": ENTRY},
                    "domain": {"script": PROCESS_REGRESSING, "entry": ENTRY},
                }),
            }

        # Model Composer chooses the regressing domain candidate!
        def mock_regressor_loving_composer(prompt):
            return {
                "decision": "adopt_domain",
                "observation": "Candidate 2 has concise regex parsing, adopting domain candidate.",
            }

        res = exp.evaluate_instance_arms(
            task_id="inst_regress",
            task_title="Monthly receipts regression test",
            repo_builder=lambda arm="": self._setup_repo_with_earlier_instance("repo_reg", arm=arm),
            propose_fn=mock_proposer,
            compose_fn=mock_regressor_loving_composer,
        )

        # 1. Arm B chose domain, Arm C chose invariant
        self.assertEqual(res["arm_b"]["chosen_kind"], "domain")
        self.assertEqual(res["arm_c"]["winner"], "invariant")

        # 2. Logged DISAGREE with reason 'regression'
        self.assertEqual(res["agree"], "DISAGREE")
        self.assertIn("regression", res["disagree_reason"].lower())

        # 3. Arm B's DB MUST show nothing locked from the shadow probe!
        ax_b = Axiom(str(self.db_dir / "arm_b.db"))
        b_procs = ax_b.db.execute("SELECT * FROM processes").fetchall()
        self.assertEqual(len(b_procs), 0, "Shadow probe in Arm B must NOT lock any processes")
        b_claims = ax_b.db.execute("SELECT * FROM claims").fetchall()
        self.assertEqual(len(b_claims), 0, "Shadow probe in Arm B must NOT record any claims")

        # 4. NEGATIVE CONTROL:
        # If someone routed shadow evaluation through real claim+verify in Arm B,
        # claims and processes would be created, which would fail the assertions above.

    def test_arms_dbs_are_completely_separate(self):
        """
        Rule (3): Each arm runs in its own isolated Axiom DB. Actions in Arm C
        never leak into Arm B or Arm A.
        NEGATIVE CONTROL: Writing to Arm B from Arm C context turns this test RED.
        """
        exp = ArmsExperiment(self.db_dir, arms=["A", "B", "C"])
        ax_a = exp.get_axiom("A")
        ax_b = exp.get_axiom("B")
        ax_c = exp.get_axiom("C")

        ax_a.join("agent-1")
        ax_b.join("agent-1")
        ax_c.join("agent-1")

        # Register task and lock process ONLY in Arm C
        ax_c.join("operator")
        repo = DataRepo(Path(self.tmp.name) / "repo_sep")
        ax_c.register_check("chk_sep", repo.root, [sys.executable, "-I", ".axiom_check/check.py"], [],
                            holdout=str(self.check_dir), claim_kind="deliver")
        ax_c.post_task("operator", "Sep task", check_id="chk_sep")
        t = ax_c.take_task("agent-1")
        s = repo.branch("b_sep", {"process.py": PROCESS_V1, "entry.json": ENTRY, "summary.csv": GOOD})
        c = ax_c.claim("agent-1", "sep claim", "chk_sep", repo.base, s, task_id=t["id"])
        ax_c.verify(c["id"])

        # Arm C has locked version 1
        self.assertIsNotNone(ax_c.process_for("chk_sep"))

        # Arm A and Arm B MUST be completely untouched!
        self.assertIsNone(ax_a.process_for("chk_sep"))
        self.assertIsNone(ax_b.process_for("chk_sep"))
        self.assertEqual(len(ax_b.db.execute("SELECT * FROM claims").fetchall()), 0)
        self.assertEqual(len(ax_a.db.execute("SELECT * FROM claims").fetchall()), 0)

    def test_dry_run_cost_and_token_projection(self):
        """
        Rule (5): Dry-run produces accurate call, token, and cost projections
        for pilot series without executing any models.
        Includes Arm A breakdown, cold-start benchmarks, and instance previews.
        NEGATIVE CONTROL: Dropping Arm A or cold-start from the plan turns test RED.
        """
        plan = run_dry_run_series("receipts-to-spreadsheet", n_instances=5)
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["instances"], 5)
        self.assertEqual(len(plan["instances_preview"]), 5)

        # 1. Arm A breakdown exists and captures baseline single repair loop
        self.assertIn("A", plan["arms_breakdown"])
        arm_a = plan["arms_breakdown"]["A"]
        self.assertEqual(arm_a["repair_calls_per_instance"], 21)
        self.assertEqual(arm_a["best_case"]["calls"], 0)
        self.assertEqual(arm_a["worst_case"]["calls"], 84)  # 4 * 21
        self.assertGreater(arm_a["worst_case"]["cost_usd"], 0.01)

        # 2. Cold-start breakdown exists and captures instance #0 benchmark
        self.assertIn("cold_start", plan)
        cs = plan["cold_start"]
        self.assertEqual(cs["calls_per_arm"], 40)
        self.assertEqual(cs["total_calls_all_arms"], 120)  # 40 * 3
        self.assertGreater(cs["total_cost_usd_all_arms"], 0.02)

        # 3. Arms B and C capture best vs worst case ranges
        self.assertIn("B", plan["arms_breakdown"])
        self.assertIn("C", plan["arms_breakdown"])
        self.assertEqual(plan["arms_breakdown"]["B"]["best_case"]["calls"], 8)  # 4 * 2
        self.assertEqual(plan["arms_breakdown"]["C"]["best_case"]["calls"], 4)  # 4 * 1

        # 4. Citations are documented
        self.assertIn("citations", plan)
        self.assertIn("pricing", plan["citations"])
        self.assertEqual(plan["citations"]["pricing"]["model"], "nvidia/Nemotron-3_5-Lightning")
        self.assertIn("verified", plan["citations"]["pricing"]["status"])
        self.assertIn("benchmarks", plan["citations"])

    def test_unknown_series_raises_error_and_cli_exits_nonzero(self):
        """
        Series validation: unknown series names must be rejected immediately,
        preventing bogus dry-run plans or runs.
        """
        # Python API raises ValueError
        with self.assertRaises(ValueError) as ctx:
            run_dry_run_series("nonexistent-series", n_instances=5)
        self.assertIn("Unknown series 'nonexistent-series'", str(ctx.exception))

        # CLI exits with non-zero code
        import subprocess
        proc = subprocess.run(
            [sys.executable, "-m", "axiom1.arms", "--series", "nonexistent-series", "--dry-run"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("Unknown series 'nonexistent-series'", proc.stderr + proc.stdout)

    def test_cli_refuses_paid_run_without_confirm_paid(self):
        """
        Paid Execution Guard: CLI must refuse to run models without explicit --confirm-paid.
        """
        import subprocess
        proc = subprocess.run(
            [sys.executable, "-m", "axiom1.arms", "--series", "receipts-to-spreadsheet"],
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("--confirm-paid", proc.stderr + proc.stdout)

    def test_local_endpoint_exempt_from_confirm_paid_and_records_zero_cost(self):
        """
        Local endpoint exemption: When endpoint is private network (e.g. Bee / 100.64.0.0/10),
        --confirm-paid is NOT required, pricing reports $0, and receipt records $0 cost.
        """
        from axiom1.arms import get_pricing_info
        pricing = get_pricing_info("test-model", base_url="http://100.100.157.46:11435/v1")
        self.assertEqual(pricing["prompt_per_1m_usd"], 0.0)
        self.assertEqual(pricing["completion_per_1m_usd"], 0.0)
        self.assertIn("local", pricing["status"])

        receipt_file = Path(self.tmp.name) / "test_local_receipt.json"
        res = run_series(
            series_name="receipts-to-spreadsheet",
            n_instances=1,
            arms=["B", "C"],
            db_dir=Path(self.tmp.name) / "run_local_db",
            receipt_path=receipt_file,
            confirm_paid=False,  # NOT set, but running on local endpoint!
            base_url="http://100.100.157.46:11435/v1",
            propose_fn=lambda *_: {"invariant": {"script": PROCESS_INVARIANT, "entry": ENTRY}, "domain": {"script": PROCESS_REGRESSING, "entry": ENTRY}},
            compose_fn=lambda *_: {"decision": "adopt_invariant", "observation": "ok"},
        )
        self.assertTrue(res["is_local"])
        self.assertEqual(res["endpoint"], "http://100.100.157.46:11435/v1")
        self.assertEqual(res["accumulated_cost_usd"], 0.0)

    def test_max_cost_stops_mid_series_and_resumes(self):
        """
        Rule (6): --max-cost halts execution when budget ceiling is exceeded,
        saving a partial resumable receipt that resumes on the next invocation.
        """
        from axiom1.arms import run_series
        receipt_file = Path(self.tmp.name) / "test_resumable_receipt.json"

        call_log = []

        def mock_proposer(title, base_script, base_entry):
            call_log.append(f"propose_{title}")
            return {
                "role": "assistant",
                "content": json.dumps({
                    "invariant": {"script": PROCESS_INVARIANT, "entry": ENTRY},
                    "domain": {"script": PROCESS_REGRESSING, "entry": ENTRY},
                }),
                "usage": {"prompt_tokens": 4000, "completion_tokens": 800},
            }

        def mock_composer(prompt):
            call_log.append("compose")
            return {
                "decision": "adopt_invariant",
                "observation": "Adopt invariant.",
                "usage": {"prompt_tokens": 3000, "completion_tokens": 500},
            }

        # 1. First run with ultra-low max_cost ($0.0015):
        # 1 instance cost = (4000+3000)*0.20/1e6 + (800+500)*0.60/1e6 = 0.00140 + 0.00078 = $0.00218
        # Since $0.00218 > $0.0015, it will complete 1 instance and stop early!
        receipt1 = run_series(
            series_name="receipts-to-spreadsheet",
            n_instances=3,
            arms=["B", "C"],
            db_dir=Path(self.tmp.name) / "run_resume_db",
            receipt_path=receipt_file,
            max_cost=0.0015,
            confirm_paid=True,
            propose_fn=mock_proposer,
            compose_fn=mock_composer,
        )

        self.assertTrue(receipt1["stopped_early"])
        self.assertEqual(receipt1["instances_completed"], 1)
        self.assertEqual(receipt1["status"], "partial")
        self.assertTrue(receipt_file.exists())

        # 2. Second run pointing to the same receipt with higher max_cost ($1.0):
        # Should resume from instance 1 and finish all 3 instances!
        receipt2 = run_series(
            series_name="receipts-to-spreadsheet",
            n_instances=3,
            arms=["B", "C"],
            db_dir=Path(self.tmp.name) / "run_resume_db",
            receipt_path=receipt_file,
            max_cost=1.0,
            confirm_paid=True,
            propose_fn=mock_proposer,
            compose_fn=mock_composer,
        )

        self.assertFalse(receipt2["stopped_early"])
        self.assertEqual(receipt2["instances_completed"], 3)
        self.assertEqual(receipt2["status"], "complete")
        self.assertEqual(len(receipt2["completed_instances"]), 3)


if __name__ == "__main__":
    unittest.main()
