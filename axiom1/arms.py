"""
Axiom-1 Arms Experiment Runner (Harness^2 vs Axiom-1 Gate Comparison)

Compares three repair strategies on identical task sequences:
  - Arm A: Today's single repair (baseline)
  - Arm B: K=2 candidates decided by a Harness^2-style Model Composer
  - Arm C: K=2 candidates decided by Axiom-1's Verifier Gate (submit_candidates)

Rules:
  1. SAME PROPOSALS: Arms B and C evaluate the exact same candidate proposals per instance.
  2. COMPOSER SEES NO GATE TRUTH: Composer sees reference, candidate scripts, diffs,
     and candidate run outputs (stdout/stderr/exit/deliverables), but NO check labels,
     holdout outputs, canaries, or regression results.
  3. ARM B SHADOW EVALUATION: Arm B's choice is evaluated in shadow via side-effect-free
     probing to record ground truth without corrupting state.
  4. ISOLATED DATABASES: Each arm operates in its own separate Axiom DB and worktree.
  5. PER-INSTANCE RECEIPTS: Logs picks, agreement/disagreement, reasons, calls, and tokens.
"""
import argparse
import asyncio
import csv
import difflib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

from . import Axiom
from .agent import Workspace, process_argv
from .ppc import diff_size, extract_candidates
from .verifier import PROCESS_FILE, ENTRY_FILE

COMPOSER_SYSTEM_PROMPT = (
    "You are a software repair composer. You compare candidate patches against a reference "
    "baseline and determine which patch should be adopted based on code changes and execution results."
)

COMPOSER_USER_PROMPT = """You are deciding which process candidate to adopt for a automated data task.

REFERENCE BASELINE (Current Process):
```python
{reference_script}
```
Reference Entry Example:
```json
{reference_entry}
```

CANDIDATE 1 (invariant proposal):
Code diff vs reference:
{cand1_diff}
Execution exit code: {cand1_exit}
Execution output:
{cand1_output}
Deliverable files produced: {cand1_files}

CANDIDATE 2 (domain proposal):
Code diff vs reference:
{cand2_diff}
Execution exit code: {cand2_exit}
Execution output:
{cand2_output}
Deliverable files produced: {cand2_files}

Instructions:
Evaluate the two candidates based solely on their code changes and execution outputs.
Decide between:
- "adopt_invariant": Adopt Candidate 1
- "adopt_domain": Adopt Candidate 2
- "keep_current": Reject both candidates and keep the reference baseline

Respond with valid JSON:
```json
{{
  "decision": "adopt_invariant" | "adopt_domain" | "keep_current",
  "observation": "detailed rationale explaining the code quality and run output differences"
}}
```"""


def compute_unified_diff(base_text: str, cand_text: str, name: str = "process.py") -> str:
    """Compute unified diff between reference and candidate script."""
    diff = difflib.unified_diff(
        base_text.splitlines(keepends=True),
        cand_text.splitlines(keepends=True),
        fromfile=f"a/{name}",
        tofile=f"b/{name}",
    )
    res = "".join(diff).strip()
    return res if res else "(no code changes)"


def build_composer_prompt(
    reference_script: str,
    reference_entry: str,
    cand1: dict[str, Any],
    cand2: dict[str, Any],
) -> str:
    """
    Construct the prompt for Arm B's Model Composer.
    Enforces Rule 2: strictly excludes any check/holdout/canary/regression verdicts.
    """
    cand1_diff = compute_unified_diff(reference_script, cand1.get("script", ""))
    cand2_diff = compute_unified_diff(reference_script, cand2.get("script", ""))

    c1_run = cand1.get("run", {})
    c2_run = cand2.get("run", {})

    prompt = COMPOSER_USER_PROMPT.format(
        reference_script=reference_script.strip(),
        reference_entry=reference_entry.strip() if reference_entry else "(none)",
        cand1_diff=cand1_diff,
        cand1_exit=c1_run.get("exit", "unknown"),
        cand1_output=(c1_run.get("output") or "(no output)").strip()[:1000],
        cand1_files=", ".join(cand1.get("deliverables", [])) or "none",
        cand2_diff=cand2_diff,
        cand2_exit=c2_run.get("exit", "unknown"),
        cand2_output=(c2_run.get("output") or "(no output)").strip()[:1000],
        cand2_files=", ".join(cand2.get("deliverables", [])) or "none",
    )

    # Sanity assertion: verifier oracle information must never be present in composer prompt
    lowered = prompt.lower()
    for forbidden in ["holdout", "canary", "regression_failed", "acceptance check", "verdict"]:
        if forbidden in lowered:
            raise ValueError(f"Forbidden gate oracle info in composer prompt: {forbidden!r}")

    return prompt


def parse_composer_decision(content: str | dict) -> dict[str, str]:
    """Parse composer decision JSON."""
    if isinstance(content, dict):
        data = content
    else:
        text = content.strip()
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in text:
            text = text.split("```", 1)[1].split("```", 1)[0].strip()
        try:
            data = json.loads(text)
        except Exception:
            data = {"decision": "keep_current", "observation": f"Unparseable response: {text[:200]}"}

    decision = data.get("decision", "keep_current")
    if decision not in ("adopt_invariant", "adopt_domain", "keep_current"):
        decision = "keep_current"
    observation = str(data.get("observation", "")).strip()
    return {"decision": decision, "observation": observation}


class ArmsExperiment:
    """Manages 3-arm comparison runs across isolated databases and shared proposals."""

    def __init__(self, db_dir: Path | str, arms: list[str] = None):
        self.db_dir = Path(db_dir)
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.arms = arms or ["A", "B", "C"]
        self.call_count = 0
        self.token_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    def get_axiom(self, arm: str) -> Axiom:
        """Returns isolated Axiom instance for a specific arm."""
        db_path = self.db_dir / f"arm_{arm.lower()}.db"
        return Axiom(str(db_path))

    def evaluate_instance_arms(
        self,
        task_id: str,
        task_title: str,
        repo_builder: Callable[[], tuple[Any, Path, str, str]],
        propose_fn: Callable,
        compose_fn: Callable,
        max_cost: float = 1.0,
        call_cap: int = 50,
    ) -> dict[str, Any]:
        """
        Run one task instance through active arms.
        Ensures Arm B and Arm C evaluate byte-identical candidate proposals.
        """
        results = {
            "task_id": task_id,
            "task_title": task_title,
            "timestamp": time.time(),
        }

        # Setup separate repos and checks for each arm
        arm_contexts = {}
        for arm in self.arms:
            ax = self.get_axiom(arm)
            ax.join("agent-1")
            ax.join("operator")
            if callable(repo_builder):
                import inspect
                sig = inspect.signature(repo_builder)
                if len(sig.parameters) > 0:
                    repo, check_dir, base_sha, claim_kind = repo_builder(arm)
                else:
                    repo, check_dir, base_sha, claim_kind = repo_builder()
            else:
                repo, check_dir, base_sha, claim_kind = repo_builder
            check_name = f"chk_{task_id}"
            ax.register_check(
                check_name,
                repo.root,
                [sys.executable, "-I", ".axiom_check/check.py"],
                [],
                holdout=str(check_dir),
                claim_kind=claim_kind,
            )
            ax.post_task("operator", task_title, check_id=check_name)
            task = ax.take_task("agent-1")
            arm_contexts[arm] = {
                "ax": ax,
                "repo": repo,
                "check_id": check_name,
                "task": task,
                "base_sha": base_sha,
                "claim_kind": claim_kind,
            }

        # Reference baseline from Arm C (or B)
        c_ax = arm_contexts["C"]["ax"] if "C" in arm_contexts else self.get_axiom("C")
        proc = c_ax.process_for(arm_contexts["C"]["check_id"]) if "C" in arm_contexts else None
        base_script = proc["script"] if proc else ""
        base_entry = proc.get("entry_example", "") if proc else ""

        # Step 1: Shared Propose Call for B & C (Rule 1)
        if self.call_count >= call_cap:
            raise RuntimeError(f"Model call cap reached ({call_cap})")

        proposal_reply = propose_fn(task_title, base_script, base_entry)
        self.call_count += 1
        prop_content = proposal_reply.get("content") if isinstance(proposal_reply, dict) and "content" in proposal_reply else proposal_reply
        raw_candidates = extract_candidates(prop_content)

        # Build candidate worktree commits for both invariant and domain
        cand_details = {}
        for kind in ("invariant", "domain"):
            cand = raw_candidates.get(kind, {})
            cand_script = cand.get("script", "")
            cand_entry = cand.get("entry", "")

            # Execute candidate in an isolated scratch worktree to obtain execution outputs (Rule 2)
            c_repo = arm_contexts.get("C", {}).get("repo") or arm_contexts["B"]["repo"]
            wt_path = self.db_dir / f"scratch_{task_id}_{kind}"
            c_repo.git("worktree", "add", "-q", "--detach", str(wt_path), "main")
            from .sandbox import LocalSandbox
            ws = Workspace(str(wt_path), LocalSandbox())
            ws.install_engines()
            ws.write_file(PROCESS_FILE, cand_script)
            if cand_entry:
                ws.write_file(ENTRY_FILE, cand_entry if isinstance(cand_entry, str) else json.dumps(cand_entry))

            ran = ws.run(" ".join(process_argv("python")))
            deliverables = [f.name for f in Path(wt_path).glob("*.csv")] + [f.name for f in Path(wt_path).glob("*.json") if f.name != "entry.json"]
            sha = ws.commit(f"cand {kind}")["sha"]

            cand_details[kind] = {
                "kind": kind,
                "script": cand_script,
                "entry": cand_entry,
                "after_ref": sha,
                "run": ran,
                "deliverables": deliverables,
            }

        # Step 2: Shadow Evaluation (Rule 3)
        # Compute ground truth verdicts for both candidates using private side-effect-free probe
        shadow_verdicts = {}
        ref_ax = arm_contexts.get("C", {}).get("ax") or arm_contexts["B"]["ax"]
        ref_check = ref_ax.db.execute("SELECT * FROM checks WHERE id=?", (arm_contexts["C"]["check_id"],)).fetchone()
        for kind in ("invariant", "domain"):
            cd = cand_details[kind]
            probe_res = ref_ax._probe(
                "agent-1",
                ref_check,
                arm_contexts["C"]["base_sha"],
                cd["after_ref"],
                kind=arm_contexts["C"]["claim_kind"],
            )
            ev = probe_res.get("evidence") or {}
            p_ev = ev.get("process") or {}
            reproduced = p_ev.get("reproduced", False)
            reg = p_ev.get("regression") or {}
            reg_failed = reg.get("failed", 0)

            shadow_verdicts[kind] = {
                "passed": probe_res.get("passed", False),
                "reproduced": reproduced,
                "regression_failed": reg_failed,
                "reason": probe_res.get("reason", ""),
            }

        results["candidates"] = {
            k: {"after_ref": v["after_ref"], "exit": v["run"].get("exit")}
            for k, v in cand_details.items()
        }
        results["shadow_verdicts"] = shadow_verdicts

        # Step 3: Arm B - Model Composer (Rule 2 & 3)
        if "B" in self.arms:
            prompt_b = build_composer_prompt(
                base_script,
                base_entry,
                cand_details.get("invariant", {}),
                cand_details.get("domain", {}),
            )
            composer_reply = compose_fn(prompt_b)
            self.call_count += 1
            b_decision = parse_composer_decision(composer_reply)
            dec = b_decision["decision"]
            pick_kind = "invariant" if dec == "adopt_invariant" else ("domain" if dec == "adopt_domain" else None)

            # Ground truth of B's pick evaluated via shadow probe
            if pick_kind:
                sv = shadow_verdicts.get(pick_kind, {})
                b_gate_valid = bool(sv.get("passed") and sv.get("reproduced") and sv.get("regression_failed") == 0)
            else:
                b_gate_valid = False

            results["arm_b"] = {
                "composer_pick": dec,
                "chosen_kind": pick_kind,
                "observation": b_decision["observation"],
                "shipped_by_harness2": dec != "keep_current",
                "judged_by_gate": b_gate_valid,
            }

        # Step 4: Arm C - Axiom Gate (Rule 1 & 3)
        if "C" in self.arms:
            c_ctx = arm_contexts["C"]
            candidates_payload = [
                {"kind": "invariant", "after_ref": cand_details["invariant"]["after_ref"]},
                {"kind": "domain", "after_ref": cand_details["domain"]["after_ref"]},
            ]
            gate_res = c_ctx["ax"].submit_candidates(
                "agent-1",
                c_ctx["task"]["id"],
                candidates_payload,
                c_ctx["base_sha"],
            )
            results["arm_c"] = {
                "witnessed": gate_res.get("witnessed", False),
                "winner": gate_res.get("winner"),
                "refused": gate_res.get("refused", []),
            }

        # Step 5: Comparison & Agreement (Rule 4)
        if "B" in self.arms and "C" in self.arms:
            b_pick = results["arm_b"]["chosen_kind"]
            c_pick = results["arm_c"]["winner"]
            agrees = (b_pick == c_pick)
            results["agree"] = "AGREE" if agrees else "DISAGREE"

            if not agrees:
                if b_pick is not None:
                    b_sv = shadow_verdicts.get(b_pick, {})
                    if not b_sv.get("passed"):
                        reason = f"check failed: {b_sv.get('reason')}"
                    elif not b_sv.get("reproduced"):
                        reason = "process did not reproduce output"
                    elif b_sv.get("regression_failed", 0) > 0:
                        reason = f"regression on {b_sv['regression_failed']} earlier instance(s)"
                    else:
                        reason = "tie-break diff preference"
                else:
                    reason = "composer kept current while gate selected winner"
                results["disagree_reason"] = reason
            else:
                results["disagree_reason"] = None

        return results


def run_dry_run_series(series_name: str, n_instances: int = 5) -> dict[str, Any]:
    """Execute dry-run for series: compute cost and token projections without model execution."""
    proposer_tokens_per_inst = {"prompt": 2100, "completion": 650}
    composer_tokens_per_inst = {"prompt": 2400, "completion": 320}

    # Instance 1: cold-start setup; instances 2..N: repairs
    propose_calls = max(1, n_instances - 1)
    compose_calls = max(1, n_instances - 1)
    total_calls = propose_calls + compose_calls

    total_prompt_tokens = (propose_calls * proposer_tokens_per_inst["prompt"] +
                           compose_calls * composer_tokens_per_inst["prompt"])
    total_comp_tokens = (propose_calls * proposer_tokens_per_inst["completion"] +
                         compose_calls * composer_tokens_per_inst["completion"])

    # Nemotron-3_5-Lightning on Nebius Token Factory pricing:
    # $0.20 / 1M prompt tokens, $0.60 / 1M completion tokens
    cost_prompt = (total_prompt_tokens / 1_000_000.0) * 0.20
    cost_comp = (total_comp_tokens / 1_000_000.0) * 0.60
    estimated_cost_usd = round(cost_prompt + cost_comp, 5)

    return {
        "series": series_name,
        "instances": n_instances,
        "dry_run": True,
        "arms": ["A", "B", "C"],
        "propose_calls": propose_calls,
        "compose_calls": compose_calls,
        "total_model_calls": total_calls,
        "projected_prompt_tokens": total_prompt_tokens,
        "projected_completion_tokens": total_comp_tokens,
        "estimated_cost_usd": estimated_cost_usd,
    }


def main():
    parser = argparse.ArgumentParser(description="Axiom-1 3-Arm Experiment Runner (Harness^2 vs Gate)")
    parser.add_argument("--series", default="receipts-to-spreadsheet", help="Task series name")
    parser.add_argument("--instances", type=int, default=5, help="Number of instances in series")
    parser.add_argument("--arms", default="A,B,C", help="Comma-separated arms to run (e.g. A,B,C)")
    parser.add_argument("--db-dir", default=".axiom_arms_run", help="Directory for arm database files")
    parser.add_argument("--dry-run", action="store_true", help="Print experiment plan and cost estimate without running models")
    parser.add_argument("--max-cost", type=float, default=1.0, help="Max cost cap in USD")
    parser.add_argument("--call-cap", type=int, default=50, help="Maximum allowed model API calls")
    parser.add_argument("--receipt", default="arms_receipt.json", help="Path to write experiment receipt")

    args = parser.parse_args()

    if args.dry_run:
        plan = run_dry_run_series(args.series, args.instances)
        print("=== AXIOM-1 3-ARM EXPERIMENT DRY-RUN PLAN ===")
        print(json.dumps(plan, indent=2))
        return

    print(f"Running 3-arm experiment on series {args.series!r} ({args.instances} instances)...")
    # Live pilot execution is pending Adrian's approval.
    print("Pilot paused: Waiting for explicit user approval before paid Nebius execution.")


if __name__ == "__main__":
    main()
