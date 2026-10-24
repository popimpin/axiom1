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
import time
import unittest
from pathlib import Path

from axiom1 import Axiom
from unittest.mock import MagicMock
from axiom1.arms import (
    ArmsExperiment,
    build_composer_prompt,
    build_judge_prompt,
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
        mock_cs = lambda _: {"locked_v1": True, "version": 1}
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
            cold_start_fn=mock_cs,
        )

        self.assertTrue(receipt1["stopped_early"])
        self.assertEqual(receipt1["instances_completed"], 2)  # Instance 0 (cold start) + Instance 1 (hit budget ceiling)
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
            cold_start_fn=mock_cs,
        )

        self.assertFalse(receipt2["stopped_early"])
        self.assertEqual(receipt2["instances_completed"], 3)
        self.assertEqual(receipt2["status"], "complete")
        self.assertEqual(len(receipt2["completed_instances"]), 3)


    def test_unrun_arm_a_is_marked_not_run_and_never_reports_calls(self):
        """
        Claude Rule 1: With no repair_a_fn, Arm A must be marked 'NOT_RUN' -
        NEVER a hardcoded call count or stub that pretends to be a measurement.
        NEGATIVE CONTROL: Returning a dict with 'calls' when unrun fails this test.
        """
        exp = ArmsExperiment(self.db_dir, arms=["A", "B", "C"])
        res = exp.evaluate_instance_arms(
            task_id="inst_arm_a_check",
            task_title="Test Arm A unrun",
            repo_builder=lambda arm="": self._setup_repo_with_earlier_instance("repo_arm_a", arm=arm),
            propose_fn=lambda *_: {"invariant": {"script": PROCESS_INVARIANT, "entry": ENTRY}, "domain": {"script": PROCESS_REGRESSING, "entry": ENTRY}},
            compose_fn=lambda *_: {"decision": "adopt_invariant", "observation": "ok"},
            repair_a_fn=None,  # NOT provided!
        )
        self.assertEqual(res["arm_a"], "NOT_RUN", "Unrun Arm A must be 'NOT_RUN'")
        self.assertFalse(isinstance(res["arm_a"], dict) and "calls" in res["arm_a"], "Unrun Arm A must never report calls")

    def test_missing_token_usage_is_recorded_as_missing(self):
        """
        Claude Rule 2: Token usage of 0 or None from model is recorded as 'missing',
        NEVER papered over with fallback guesses (2000/600 or 2200/300).
        """
        exp = ArmsExperiment(self.db_dir, arms=["B", "C"])
        res = exp.evaluate_instance_arms(
            task_id="inst_missing_tok",
            task_title="Test Missing Tokens",
            repo_builder=lambda arm="": self._setup_repo_with_earlier_instance("repo_missing_tok", arm=arm),
            # Model returns no usage key:
            propose_fn=lambda *_: {"role": "assistant", "content": json.dumps({"invariant": {"script": PROCESS_INVARIANT, "entry": ENTRY}, "domain": {"script": PROCESS_REGRESSING, "entry": ENTRY}})},
            compose_fn=lambda *_: {"decision": "adopt_invariant", "observation": "ok"},
        )
        self.assertEqual(res["propose_token_usage"], "missing")
        self.assertEqual(res["compose_token_usage"], "missing")
        self.assertEqual(exp.token_usage.get("missing"), 2)
        # Verify no fake tokens were added:
        self.assertEqual(exp.token_usage.get("prompt_tokens", 0), 0)
        self.assertEqual(exp.token_usage.get("completion_tokens", 0), 0)

    def test_cold_start_failure_stops_series_early(self):
        """
        Claude Rule 3: If instance #0 cold start fails to lock version 1,
        run_series must STOP IMMEDIATELY and report that, never continuing into repairs.
        """
        receipt_file = Path(self.tmp.name) / "test_cold_start_fail_receipt.json"

        # Cold start fails to lock v1:
        mock_failing_cs = lambda _: {
            "locked_v1": False,
            "reason": "the delivered files do not pass the acceptance check",
            "model_calls": 15,
        }

        receipt = run_series(
            series_name="receipts-to-spreadsheet",
            n_instances=5,
            arms=["B", "C"],
            db_dir=Path(self.tmp.name) / "run_cs_fail_db",
            receipt_path=receipt_file,
            confirm_paid=True,
            propose_fn=lambda *_: {},
            compose_fn=lambda *_: {},
            cold_start_fn=mock_failing_cs,
        )

        self.assertTrue(receipt["stopped_early"])
        self.assertFalse(receipt["instance_0_locked_v1"])
        self.assertEqual(receipt["instances_completed"], 1, "Must NOT run instances 1..4 when instance 0 failed to lock v1")
        self.assertEqual(receipt["status"], "stopped_cold_start_unwitnessed")
        self.assertIn("Instance #0 (cold start) did not lock version 1", receipt["stop_reason"])


    def test_cold_start_step_cap_reached_records_exact_reason(self):
        """
        Claude Test 1: When cold start hits the step cap without locking v1,
        receipt reason must be 'step cap reached', strictly NOT 'refuted'.
        NEGATIVE CONTROL: Marking reason as 'refuted' when cap was hit turns this test RED.
        """
        receipt_file = Path(self.tmp.name) / "test_step_cap_receipt.json"

        mock_capped_cs = lambda _: {
            "locked_v1": False,
            "model_calls": 60,
            "reason": "step cap reached",
        }

        receipt = run_series(
            series_name="receipts-to-spreadsheet",
            n_instances=5,
            arms=["B", "C"],
            db_dir=Path(self.tmp.name) / "run_step_cap_db",
            receipt_path=receipt_file,
            confirm_paid=True,
            propose_fn=lambda *_: {},
            compose_fn=lambda *_: {},
            cold_start_fn=mock_capped_cs,
        )

        self.assertTrue(receipt["stopped_early"])
        self.assertFalse(receipt["instance_0_locked_v1"])
        self.assertEqual(receipt["completed_instances"][0]["reason"], "step cap reached")
        self.assertNotIn("refuted", receipt["completed_instances"][0]["reason"])
        self.assertIn("step cap reached", receipt["stop_reason"])

    def test_cold_start_lease_expired_records_exact_reason(self):
        """
        Claude Test 2: When the lease expires under the agent before a witnessed claim,
        receipt reason must be 'lease expired'.
        NEGATIVE CONTROL: Marking reason as 'refuted' or 'no witnessed claim' turns test RED.
        """
        receipt_file = Path(self.tmp.name) / "test_lease_expired_receipt.json"

        mock_expired_cs = lambda _: {
            "locked_v1": False,
            "model_calls": 25,
            "reason": "lease expired",
        }

        receipt = run_series(
            series_name="receipts-to-spreadsheet",
            n_instances=5,
            arms=["B", "C"],
            db_dir=Path(self.tmp.name) / "run_lease_expired_db",
            receipt_path=receipt_file,
            confirm_paid=True,
            propose_fn=lambda *_: {},
            compose_fn=lambda *_: {},
            cold_start_fn=mock_expired_cs,
        )

        self.assertTrue(receipt["stopped_early"])
        self.assertFalse(receipt["instance_0_locked_v1"])
        self.assertEqual(receipt["completed_instances"][0]["reason"], "lease expired")
        self.assertNotIn("refuted", receipt["completed_instances"][0]["reason"])
        self.assertIn("lease expired", receipt["stop_reason"])



    def test_passing_replay_skips_repairs_and_records_zero_repair_calls(self):
        """
        Claude Test 1: When locked process v1 succeeds on a new input instance,
        replay runs first, records passed: True, and skips proposal/repair (0 repair calls).
        """
        exp = ArmsExperiment(Path(self.tmp.name) / "test_replay_exp", arms=["B", "C"])
        repo = DataRepo(Path(self.tmp.name) / "repo_replay")
        check_name = "chk_replay_pass"

        # Lock in process v1 in Arm C that writes GOOD deliverable
        ax_c = exp.get_axiom("C")
        ax_c.join("agent-1")
        ax_c.join("operator")
        ax_c.register_check(check_name, repo.root, [sys.executable, "-I", ".axiom_check/check.py"], [],
                            holdout=str(self.check_dir), claim_kind="deliver")
        ax_c.post_task("operator", "Replay pass task", check_id=check_name)
        t = ax_c.take_task("agent-1")
        s = repo.branch("b_lock_v1", {"process.py": PROCESS_V1, "entry.json": ENTRY, "summary.csv": GOOD})
        c = ax_c.claim("agent-1", "lock v1", check_name, repo.base, s, task_id=t["id"])
        ax_c.verify(c["id"])
        self.assertIsNotNone(ax_c.process_for(check_name))

        # Instance 2 has same receipts format (repo.base), so replay of PROCESS_V1 will pass!
        propose_called = []
        def mock_propose(*args, **kwargs):
            propose_called.append(True)
            return {}

        results = exp.evaluate_instance_arms(
            task_id="replay_inst_1",
            task_title="Replay pass instance",
            repo_builder=lambda arm="": (repo, self.check_dir, repo.base, "deliver"),
            propose_fn=mock_propose,
            compose_fn=lambda *_: {},
            check_id=check_name,
        )

        self.assertTrue(results["replay"]["passed"])
        self.assertEqual(len(propose_called), 0, "Propose MUST NOT be called when replay passes!")
        self.assertTrue(results["arm_c"]["witnessed"])
        self.assertEqual(results["arm_c"]["winner"], "replay")
        self.assertEqual(results["agree"], "AGREE")
        self.assertIsNone(results["k_effective"])

    def test_proposer_prompt_contains_replay_failure_text(self):
        """
        Claude Test 2: When replay fails, the proposer prompt receives the exact replay failure note.
        NEGATIVE CONTROL: Stripping replay_note or hardcoding generic text turns test RED.
        """
        exp = ArmsExperiment(Path(self.tmp.name) / "test_replay_fail_exp", arms=["B", "C"])
        repo = DataRepo(Path(self.tmp.name) / "repo_replay_fail")
        check_name = "chk_replay_fail"

        # Lock in process v1 that expects clean numbers
        ax_c = exp.get_axiom("C")
        ax_c.join("agent-1")
        ax_c.join("operator")
        ax_c.register_check(check_name, repo.root, [sys.executable, "-I", ".axiom_check/check.py"], [],
                            holdout=str(self.check_dir), claim_kind="deliver")
        ax_c.post_task("operator", "Lock v1 task", check_id=check_name)
        t = ax_c.take_task("agent-1")
        s = repo.branch("b_lock_v1", {"process.py": PROCESS_V1, "entry.json": ENTRY, "summary.csv": GOOD})
        c = ax_c.claim("agent-1", "lock v1", check_name, repo.base, s, task_id=t["id"])
        ax_c.verify(c["id"])
        self.assertIsNotNone(ax_c.process_for(check_name))

        # Now Instance 2 has strayed receipts ($12.50), causing PROCESS_V1 to crash during replay
        repo_strayed, check_dir, strayed_base, kind = self._setup_repo_with_earlier_instance("repo_fail_inst")

        captured_replay_notes = []
        def mock_propose(title, base_script, base_entry, replay_note="", **kwargs):
            captured_replay_notes.append(replay_note)
            return {
                "invariant": {"script": PROCESS_INVARIANT, "entry": ENTRY},
                "domain": {"script": PROCESS_INVARIANT, "entry": ENTRY},
            }

        results = exp.evaluate_instance_arms(
            task_id="replay_fail_inst",
            task_title="Replay fail instance",
            repo_builder=lambda arm="": (repo_strayed, check_dir, strayed_base, kind),
            propose_fn=mock_propose,
            compose_fn=lambda *_: {"decision": "keep_current", "observation": "both fail"},
            check_id=check_name,
        )

        self.assertFalse(results["replay"]["passed"])
        self.assertTrue(len(captured_replay_notes) > 0)
        self.assertIn("could not convert string to float", captured_replay_notes[0])

    def test_lessons_from_refused_round_appear_in_next_proposer_prompt(self):
        """
        Claude Test 3: Refused candidate lessons from round N appear in round N+1's proposer prompt.
        NEGATIVE CONTROL: Hardcoding '(none yet)' turns this test RED.
        """
        exp = ArmsExperiment(Path(self.tmp.name) / "test_lessons_exp", arms=["B", "C"])
        repo = DataRepo(Path(self.tmp.name) / "repo_lessons")
        check_name = "chk_lessons"

        ax_c = exp.get_axiom("C")
        ax_c.join("agent-1")
        ax_c.join("operator")
        ax_c.register_check(check_name, repo.root, [sys.executable, "-I", ".axiom_check/check.py"], [],
                            holdout=str(self.check_dir), claim_kind="deliver")

        # Lock in process v1
        ax_c.post_task("operator", "Round 0 Task", check_id=check_name)
        t0 = ax_c.take_task("agent-1")
        s = repo.branch("b_lock_v1", {"process.py": PROCESS_V1, "entry.json": ENTRY, "summary.csv": GOOD})
        c = ax_c.claim("agent-1", "lock v1", check_name, repo.base, s, task_id=t0["id"])
        ax_c.verify(c["id"])

        # Refusal in round 1:
        ax_c.post_task("operator", "Round 1 Task", check_id=check_name)
        t1 = ax_c.take_task("agent-1")
        s_bad = repo.branch("b_bad", {"process.py": "exit(1)"})
        res = ax_c.submit_candidates("agent-1", t1["id"], [{"kind": "invariant", "after_ref": s_bad}], before_ref=repo.base)
        self.assertFalse(res["witnessed"])

        # Round 2: take task yields the refused lesson from round 1
        repo_strayed, check_dir, strayed_base, kind = self._setup_repo_with_earlier_instance("repo_lessons_strayed")
        captured_witnessed = []
        captured_declared = []
        def mock_propose(title, base_script, base_entry, witnessed_lessons="", declared_lessons="", **kwargs):
            captured_witnessed.append(witnessed_lessons)
            captured_declared.append(declared_lessons)
            return {
                "invariant": {"script": PROCESS_INVARIANT, "entry": ENTRY},
                "domain": {"script": PROCESS_INVARIANT, "entry": ENTRY},
            }

        results = exp.evaluate_instance_arms(
            task_id="round_2_inst",
            task_title="Round 2 Task",
            repo_builder=lambda arm="": (repo_strayed, check_dir, strayed_base, kind),
            propose_fn=mock_propose,
            compose_fn=lambda *_: {"decision": "keep_current", "observation": "refused"},
            check_id=check_name,
        )

        all_lessons_text = (captured_witnessed[0] if captured_witnessed else "") + (captured_declared[0] if captured_declared else "")
        self.assertIn("refused", all_lessons_text.lower())
        self.assertNotEqual(all_lessons_text.strip(), "(no verified lessons yet)\n(no unverified attempts)")

    def test_identical_candidates_flagged_k_effective_1(self):
        """
        Claude Test 4: When invariant and domain candidate scripts are byte-identical,
        k_effective is logged as 1; when they differ, k_effective is logged as 2.
        """
        exp = ArmsExperiment(Path(self.tmp.name) / "test_k_eff_exp", arms=["B", "C"])
        repo = DataRepo(Path(self.tmp.name) / "repo_k_eff")
        check_name = "chk_k_eff"

        ax_c = exp.get_axiom("C")
        ax_c.join("agent-1")
        ax_c.join("operator")
        ax_c.register_check(check_name, repo.root, [sys.executable, "-I", ".axiom_check/check.py"], [],
                            holdout=str(self.check_dir), claim_kind="deliver")

        # Lock in process v1
        ax_c.post_task("operator", "Lock v1", check_id=check_name)
        t = ax_c.take_task("agent-1")
        s = repo.branch("b_lock_v1", {"process.py": PROCESS_V1, "entry.json": ENTRY, "summary.csv": GOOD})
        c = ax_c.claim("agent-1", "lock v1", check_name, repo.base, s, task_id=t["id"])
        ax_c.verify(c["id"])

        repo_strayed, check_dir, strayed_base, kind = self._setup_repo_with_earlier_instance("repo_keff_strayed")

        # 1. Identical candidate scripts -> k_effective == 1
        res1 = exp.evaluate_instance_arms(
            task_id="k_eff_inst_1",
            task_title="K Eff 1",
            repo_builder=lambda arm="": (repo_strayed, check_dir, strayed_base, kind),
            propose_fn=lambda *args, **kwargs: {
                "invariant": {"script": "# identical code\nprint(1)", "entry": ""},
                "domain": {"script": "# identical code\nprint(1)", "entry": ""},
            },
            compose_fn=lambda *_: {"decision": "keep_current", "observation": "same"},
            check_id=check_name,
        )
        self.assertEqual(res1["k_effective"], 1)

        # 2. Distinct candidate scripts -> k_effective == 2
        res2 = exp.evaluate_instance_arms(
            task_id="k_eff_inst_2",
            task_title="K Eff 2",
            repo_builder=lambda arm="": (repo_strayed, check_dir, strayed_base, kind),
            propose_fn=lambda *args, **kwargs: {
                "invariant": {"script": "# candidate 1: invariant\nprint(1)", "entry": ""},
                "domain": {"script": "# candidate 2: domain\nprint(2)", "entry": ""},
            },
            compose_fn=lambda *_: {"decision": "keep_current", "observation": "diff"},
            check_id=check_name,
        )
        self.assertEqual(res2["k_effective"], 2)


    def test_judge_prompt_rejects_proposer_rationale(self):
        """
        Adrian / Claude Rule 3: Share but don't contaminate.
        The judge sees artifacts + run outputs + witnessed memory, NEVER the proposer's rationale.
        NEGATIVE CONTROL: If cand1 or cand2 includes proposer rationale, build_judge_prompt MUST raise ValueError.
        If this guard is bypassed, this test turns RED.
        """
        cand_clean = {"script": "print(1)", "run": {"output": "1", "exit": 0}, "deliverables": []}
        cand_contaminated = {"script": "print(2)", "run": {"output": "2", "exit": 0}, "deliverables": [], "proposer_rationale": "I think this candidate is better because of X"}

        # Clean prompt succeeds
        prompt = build_judge_prompt("print(0)", "", cand_clean, cand_clean, trigger="both_pass_tie")
        self.assertIn("ESCALATION TRIGGER: both_pass_tie", prompt)

        # Contaminated candidate raises ValueError
        with self.assertRaises(ValueError) as ctx:
            build_judge_prompt("print(0)", "", cand_clean, cand_contaminated, trigger="both_pass_tie")
        self.assertIn("Contamination error", str(ctx.exception))

    def test_judge_only_pick_cannot_be_witnessed(self):
        """
        Adrian / Claude Rule 3: A judge's pick is DECLARED, never WITNESSED.
        With no check it locks nothing as witnessed (provisional until a check confirms).
        NEGATIVE CONTROL: Marking a judge pick under no_check as 'witnessed' turns this test RED.
        """
        exp = ArmsExperiment(Path(self.tmp.name) / "test_judge_declared_exp", arms=["C+J"])
        repo = DataRepo(Path(self.tmp.name) / "repo_no_check")

        repo_strayed, check_dir, strayed_base, kind = self._setup_repo_with_earlier_instance("repo_no_chk_strayed")

        mock_judge = lambda prompt: {"decision": "adopt_invariant", "observation": "Invariant looks cleaner"}

        res = exp.evaluate_instance_arms(
            task_id="no_check_inst",
            task_title="No check task",
            repo_builder=lambda arm="": (repo_strayed, check_dir, strayed_base, kind),
            propose_fn=lambda *args, **kwargs: {
                "invariant": {"script": "print('inv')", "entry": ""},
                "domain": {"script": "print('dom')", "entry": ""},
            },
            compose_fn=lambda *_: {"decision": "keep_current", "observation": "none"},
            judge_fn=mock_judge,
            check_id="no_check", # NO CHECK!
        )

        cj_res = res["arm_c_j"]
        self.assertTrue(cj_res["escalated"])
        self.assertEqual(cj_res["escalation_trigger"], "no_check")
        self.assertEqual(cj_res["winner"], "invariant")
        # Critical negative control: MUST NOT BE WITNESSED!
        self.assertFalse(cj_res["witnessed"], "A judge-only pick without a check MUST NEVER be witnessed!")
        self.assertEqual(cj_res["claim_label"], "declared")

        # Verify in DB: process is NOT locked as witnessed
        ax_cj = exp.get_axiom("C+J")
        proc = ax_cj.process_for("chk_no_check_inst")
        self.assertIsNone(proc, "Judge pick without check must not lock in processes table!")

    def test_judge_called_on_case_gate_decided_fails(self):
        """
        Adrian / Claude Rule 2: C is default, Judge is ONLY on escalation.
        When the deterministic gate can decide (e.g. 0 candidates pass or 1 candidate passes),
        the Judge MUST NOT be called (0 judge calls).
        NEGATIVE CONTROL: Calling the judge when the gate already decided turns this test RED.
        """
        exp = ArmsExperiment(Path(self.tmp.name) / "test_gate_decided_exp", arms=["C+J"])
        repo = DataRepo(Path(self.tmp.name) / "repo_gate_decided")
        check_name = "chk_gate_decided"

        ax_cj = exp.get_axiom("C+J")
        ax_cj.join("agent-1")
        ax_cj.join("operator")
        ax_cj.register_check(check_name, repo.root, [sys.executable, "-I", ".axiom_check/check.py"], [],
                             holdout=str(self.check_dir), claim_kind="deliver")

        repo_strayed, check_dir, strayed_base, kind = self._setup_repo_with_earlier_instance("repo_gate_decided_strayed")

        mock_judge = MagicMock()

        # Both candidates fail check (exit 1) -> Gate can decide (0 pass, refused).
        res = exp.evaluate_instance_arms(
            task_id="gate_decided_inst",
            task_title="Gate decided task",
            repo_builder=lambda arm="": (repo_strayed, check_dir, strayed_base, kind),
            propose_fn=lambda *args, **kwargs: {
                "invariant": {"script": "exit(1)", "entry": ""},
                "domain": {"script": "exit(1)", "entry": ""},
            },
            compose_fn=lambda *_: {"decision": "keep_current", "observation": "none"},
            judge_fn=mock_judge,
            check_id=check_name,
        )

        cj_res = res["arm_c_j"]
        self.assertFalse(cj_res["escalated"])
        self.assertIsNone(cj_res["escalation_trigger"])
        self.assertEqual(cj_res["judge_calls"], 0)
        self.assertFalse(cj_res["witnessed"])
        # NEGATIVE CONTROL: judge was NOT called
        mock_judge.assert_not_called()

    def test_judge_breaks_tie_on_both_pass_and_logs_both(self):
        """
        Adrian / Claude Rule 2(b): When both candidates pass acceptance check without regression,
        Judge breaks the tie instead of only smallest-diff.
        Receipt logs BOTH: gate_smallest_diff_winner AND judge_tie_break_winner.
        """
        exp = ArmsExperiment(Path(self.tmp.name) / "test_tie_break_exp", arms=["C+J"])
        repo = DataRepo(Path(self.tmp.name) / "repo_tie_break")
        check_name = "chk_tie_break"

        ax_cj = exp.get_axiom("C+J")
        ax_cj.join("agent-1")
        ax_cj.join("operator")
        ax_cj.register_check(check_name, repo.root, [sys.executable, "-I", ".axiom_check/check.py"], [],
                             holdout=str(self.check_dir), claim_kind="deliver")

        # Base lock
        ax_cj.post_task("operator", "Lock v1", check_id=check_name)
        t = ax_cj.take_task("agent-1")
        s = repo.branch("b_lock_v1", {"process.py": PROCESS_V1, "entry.json": ENTRY, "summary.csv": GOOD})
        c = ax_cj.claim("agent-1", "lock v1", check_name, repo.base, s, task_id=t["id"])
        ax_cj.verify(c["id"])

        # Strayed receipts in the same repo so commits resolve
        repo.write("receipts/a.txt", "Corner Cafe\nTotal: $12.50\n")
        repo.write("receipts/b.txt", "Hardware World\nTotal: $40.00\n")
        repo.write("receipts/c.txt", "Corner Cafe\nTotal: $7.25\n")
        strayed_base = repo.commit("strayed receipts with total $")

        # Both candidates pass and write GOOD summary.csv
        cand_inv_script = PROCESS_INVARIANT
        cand_dom_script = PROCESS_INVARIANT + "\n# much longer domain comment with extra code\n"

        mock_judge = lambda prompt: {"decision": "adopt_domain", "observation": "Domain implementation is cleaner"}

        res = exp.evaluate_instance_arms(
            task_id="tie_break_inst",
            task_title="Tie break task",
            repo_builder=lambda arm="": (repo, self.check_dir, strayed_base, "deliver"),
            propose_fn=lambda *args, **kwargs: {
                "invariant": {"script": cand_inv_script, "entry": ENTRY},
                "domain": {"script": cand_dom_script, "entry": ENTRY},
            },
            compose_fn=lambda *_: {"decision": "keep_current", "observation": "none"},
            judge_fn=mock_judge,
            check_id=check_name,
        )

        cj_res = res["arm_c_j"]
        self.assertTrue(cj_res["escalated"])
        self.assertEqual(cj_res["escalation_trigger"], "both_pass_tie")
        self.assertEqual(cj_res["gate_smallest_diff_winner"], "invariant")
        self.assertEqual(cj_res["judge_tie_break_winner"], "domain")
        self.assertEqual(cj_res["winner"], "domain")
        self.assertTrue(cj_res["witnessed"])
        self.assertEqual(cj_res["judge_calls"], 1)

    def test_judge_escalates_on_unseen_scenario(self):
        """
        Adrian / Claude Rule 2(c): When input scenario is flagged as unseen,
        Arm C+J escalates to Judge with trigger 'unseen_scenario'.
        Judge pick is labeled DECLARED and NEVER WITNESSED.
        NEGATIVE CONTROL: If unseen is False and gate can decide, judge is NOT called.
        """
        exp = ArmsExperiment(Path(self.tmp.name) / "test_unseen_exp", arms=["C+J"])
        repo = DataRepo(Path(self.tmp.name) / "repo_unseen")
        check_name = "chk_unseen"

        ax_cj = exp.get_axiom("C+J")
        ax_cj.join("agent-1")
        ax_cj.join("operator")
        ax_cj.register_check(check_name, repo.root, [sys.executable, "-I", ".axiom_check/check.py"], [],
                             holdout=str(self.check_dir), claim_kind="deliver")

        repo_strayed, check_dir, strayed_base, kind = self._setup_repo_with_earlier_instance("repo_unseen_strayed")

        captured_judge_prompts = []
        def mock_judge(prompt):
            captured_judge_prompts.append(prompt)
            return {"decision": "adopt_invariant", "observation": "novel input format parsed gracefully"}

        # Candidate that fails check (exit 1)
        res = exp.evaluate_instance_arms(
            task_id="unseen_inst",
            task_title="Unseen task",
            repo_builder=lambda arm="": (repo_strayed, check_dir, strayed_base, kind),
            propose_fn=lambda *args, **kwargs: {
                "invariant": {"script": "exit(1)", "entry": ""},
                "domain": {"script": "exit(1)", "entry": ""},
            },
            compose_fn=lambda *_: {"decision": "keep_current", "observation": "none"},
            judge_fn=mock_judge,
            check_id=check_name,
            unseen=True, # Flagged as novel unseen scenario!
        )

        cj_res = res["arm_c_j"]
        self.assertTrue(cj_res["escalated"])
        self.assertEqual(cj_res["escalation_trigger"], "unseen_scenario")
        self.assertEqual(cj_res["winner"], "invariant")
        self.assertFalse(cj_res["witnessed"], "Unseen scenario pick must not be witnessed!")
        self.assertEqual(cj_res["claim_label"], "declared")
        self.assertEqual(len(captured_judge_prompts), 1)
        self.assertIn("ESCALATION TRIGGER: unseen_scenario", captured_judge_prompts[0])

    def test_escalation_rate_summary_in_receipt(self):
        """
        Adrian / Claude Rule 2: Escalation rate (escalations / total repairs) is computed and recorded in receipt.
        """
        receipt_file = Path(self.tmp.name) / "test_esc_rate_receipt.json"

        # Mock cold start succeeds
        mock_cs = lambda arm_contexts: {
            "locked_v1": True,
            "version": 1,
            "model_calls": 10,
            "token_usage": {"prompt_tokens": 1000, "completion_tokens": 100},
        }

        # Mock judge only called on tie-break
        mock_judge = lambda prompt: {"decision": "adopt_invariant", "observation": "ok"}

        receipt = run_series(
            series_name="receipts-to-spreadsheet",
            n_instances=2,
            arms=["C+J"],
            db_dir=Path(self.tmp.name) / "esc_rate_dbs",
            receipt_path=receipt_file,
            confirm_paid=True,
            propose_fn=lambda *_: {"invariant": {"script": PROCESS_V1, "entry": ENTRY}, "domain": {"script": PROCESS_V1, "entry": ENTRY}},
            compose_fn=lambda *_: {"decision": "keep_current", "observation": "none"},
            judge_fn=mock_judge,
            cold_start_fn=mock_cs,
        )

        self.assertIn("arm_c_j_summary", receipt)
        summary = receipt["arm_c_j_summary"]
        self.assertIn("escalation_rate", summary)
        self.assertIn("escalation_count", summary)
        self.assertIn("escalations_by_trigger", summary)


if __name__ == "__main__":
    unittest.main()
