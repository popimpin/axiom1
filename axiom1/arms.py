"""
Axiom-1 Arms Experiment Runner (Harness^2 vs Axiom-1 Gate Comparison)

Compares three repair strategies on identical task sequences from examples/everyday_tasks.py:
  - Arm A: Today's single repair (baseline multi-turn repair loop)
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
  6. BUDGET ENFORCEMENT & RESUMABILITY: Enforces --max-cost and --call-cap on real usage;
     saves incremental resumable receipts after each step.
"""
import argparse
import asyncio
import csv
import difflib
import json
import os
import random
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

# Ensure examples directory is on sys.path for task loading
EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
if str(EXAMPLES_DIR) not in sys.path:
    sys.path.insert(0, str(EXAMPLES_DIR))

from . import Axiom
from .agent import Workspace, process_argv, DEFAULT_MODEL, DEFAULT_BASE_URL, endpoint_is_local
from .ppc import PPC_PROPOSAL_PROMPT, diff_size, extract_candidates
from .verifier import PROCESS_FILE, ENTRY_FILE


def get_runner_model() -> str:
    """Return model configured for Axiom-1 runs (env AXIOM_MODEL or agent.py DEFAULT_MODEL)."""
    return os.environ.get("AXIOM_MODEL", DEFAULT_MODEL)


def get_runner_endpoint(base_url: str = None) -> str:
    """Return endpoint configured for Axiom-1 runs (env NEBIUS_BASE_URL or agent.py DEFAULT_BASE_URL)."""
    return (base_url or os.environ.get("NEBIUS_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")


def get_pricing_info(model_name: str = None, base_url: str = None) -> dict[str, Any]:
    """Return pricing info for the model and endpoint used by the runner."""
    model = model_name or get_runner_model()
    endpoint = get_runner_endpoint(base_url)
    is_local = endpoint_is_local(endpoint)
    if is_local:
        return {
            "model": model,
            "endpoint": endpoint,
            "prompt_per_1m_usd": 0.0,
            "completion_per_1m_usd": 0.0,
            "status": "local (private network, cost $0.00)",
            "citation": f"Local endpoint {endpoint} ({model}): $0.00 / free",
        }
    return {
        "model": model,
        "endpoint": endpoint,
        "prompt_per_1m_usd": 0.20,
        "completion_per_1m_usd": 0.60,
        "status": "verified (Nebius Token Factory standard Nemotron tier)" if "Nemotron" in model else "unverified",
        "citation": f"Nebius Token Factory pay-as-you-go pricing for {model} ($0.20/1M prompt, $0.60/1M completion)",
    }


BENCHMARK_CITATIONS = {
    "cold_start": "Axiom-1 2026-10-01 everyday tasks smoke receipt: 40 calls / 137s to solve cold start from scratch",
    "single_repair": "Axiom-1 2026-10-01 smoke receipt: 21 calls / 96s for single repair round (Arm A baseline)",
}

COMPOSER_SYSTEM_PROMPT = (
    "You are a software repair composer. You compare candidate patches against a reference "
    "baseline and determine which patch should be adopted based on code changes and execution results."
)

COMPOSER_USER_PROMPT = """You are deciding which process candidate to adopt for an automated data task.

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


def get_available_series() -> list[str]:
    """List all supported task series from examples/everyday_tasks.py."""
    try:
        import everyday_tasks as et
        return [t["id"] for t in et.TASKS]
    except Exception:
        return [
            "receipts-to-spreadsheet", "tidy-downloads", "merge-contacts",
            "policy-answer-with-source", "reconcile-bank-statement",
            "fill-claim-form", "calendar-from-inbox", "overdue-invoices",
        ]


def get_task_def(series_name: str) -> dict[str, Any]:
    """Load and validate task definition from everyday_tasks.py."""
    try:
        import everyday_tasks as et
        for t in et.TASKS:
            if t["id"] == series_name:
                return t
    except Exception as e:
        raise ValueError(f"Could not load everyday_tasks: {e}")
    valid = get_available_series()
    raise ValueError(f"Unknown series {series_name!r}. Available series: {valid}")


def derive_tokens_for_series(task_def: dict[str, Any]) -> dict[str, int]:
    """Derive token counts from real formatted prompts built for the series."""
    files, _ = task_def["data"](random.Random(1000))
    sample_inputs = "\n".join(f"=== {k} ===\n{v[:300]}" for k, v in sorted(files.items())[:4])
    sample_prop = PPC_PROPOSAL_PROMPT.format(
        base_script="# sample baseline script\nimport csv\n",
        entry_example='{"output": "summary.csv"}',
        replay_note=f"Task: {task_def.get('ask', '')[:200]}",
        input_files=sample_inputs,
        witnessed_lessons="- [WITNESSED] sample verified rule",
        declared_lessons="- [UNVERIFIED] sample unverified attempt",
    )
    sample_comp = COMPOSER_USER_PROMPT.format(
        reference_script="# sample baseline script\nimport csv\n",
        reference_entry='{"output": "summary.csv"}',
        cand1_diff="+ # patch 1",
        cand1_exit=0,
        cand1_output="Processed files successfully",
        cand1_files="summary.csv",
        cand2_diff="+ # patch 2",
        cand2_exit=0,
        cand2_output="Processed files",
        cand2_files="summary.csv",
    )
    # Character-based approximation: ~3.8 chars per token
    propose_prompt_tokens = max(1500, int(len(sample_prop) / 3.8))
    compose_prompt_tokens = max(1800, int(len(sample_comp) / 3.8))
    return {
        "propose_prompt_tokens": propose_prompt_tokens,
        "propose_completion_tokens": 650,
        "compose_prompt_tokens": compose_prompt_tokens,
        "compose_completion_tokens": 320,
        "cold_start_prompt_tokens_per_call": 1800,
        "cold_start_comp_tokens_per_call": 250,
        "repair_a_prompt_tokens_per_call": 2200,
        "repair_a_comp_tokens_per_call": 300,
    }


def git_run(repo: Any, *args) -> str:
    """Run git command on repo whether it's a DataRepo or a Path/str."""
    if hasattr(repo, "git") and callable(repo.git):
        return repo.git(*args)
    root = repo.root if hasattr(repo, "root") and not isinstance(repo, Path) else Path(repo)
    import subprocess
    cmd = ["git", "-C", str(root), "-c", "user.name=axiom1", "-c", "user.email=a@axiom1.invalid", *args]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return res.stdout.strip()


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
    Construct Model Composer prompt from reference script and candidate run observations.
    Rule 2: MUST NOT include check labels, holdout outputs, canary results, or regression numbers.
    """
    FORBIDDEN_GATE_TERMS = [
        "witnessed", "refuted", "holdout", "canary", "regression_failed",
        "acceptance check", "universal guard", "private:", "public:",
    ]

    c1_out = str(cand1.get("run", {}).get("output", ""))
    c2_out = str(cand2.get("run", {}).get("output", ""))

    for forbidden in FORBIDDEN_GATE_TERMS:
        if forbidden in c1_out.lower() or forbidden in c2_out.lower():
            raise ValueError(f"Verifier gate secret leaked into candidate output: {forbidden!r}")

    diff1 = compute_unified_diff(reference_script, cand1.get("script", ""))
    diff2 = compute_unified_diff(reference_script, cand2.get("script", ""))

    prompt = COMPOSER_USER_PROMPT.format(
        reference_script=(reference_script or "").strip() or "(empty process.py)",
        reference_entry=(reference_entry or "").strip() or "(empty entry.json)",
        cand1_diff=diff1,
        cand1_exit=cand1.get("run", {}).get("exit", 0),
        cand1_output=c1_out.strip() or "(no stdout/stderr)",
        cand1_files=", ".join(cand1.get("deliverables", [])) or "(none)",
        cand2_diff=diff2,
        cand2_exit=cand2.get("run", {}).get("exit", 0),
        cand2_output=c2_out.strip() or "(no stdout/stderr)",
        cand2_files=", ".join(cand2.get("deliverables", [])) or "(none)",
    )
    return prompt


def parse_composer_decision(reply: str | dict) -> dict[str, str]:
    """Parse JSON decision from model composer response."""
    if isinstance(reply, dict) and "decision" in reply:
        data = reply
    elif isinstance(reply, dict) and "content" in reply:
        return parse_composer_decision(reply["content"])
    else:
        text = str(reply).strip()
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
    """Manages 3-arm comparison runs across isolated databases, shared proposals, and cost tracking."""

    def __init__(
        self,
        db_dir: Path | str,
        arms: list[str] = None,
        max_cost: float = 1.0,
        call_cap: int = 100,
        receipt_path: Path | str = None,
        model_name: str = None,
        base_url: str = None,
    ):
        self.db_dir = Path(db_dir)
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.arms = arms or ["A", "B", "C"]
        self.max_cost = float(max_cost)
        self.call_cap = int(call_cap)
        self.receipt_path = Path(receipt_path) if receipt_path else None
        self.model = model_name or get_runner_model()
        self.base_url = base_url or get_runner_endpoint()
        self.call_count = 0
        self.token_usage = {"prompt_tokens": 0, "completion_tokens": 0}
        self.accumulated_cost_usd = 0.0
        self.stopped_early = False
        self.stop_reason = None

    def get_axiom(self, arm: str) -> Axiom:
        """Returns isolated Axiom instance for a specific arm."""
        db_path = self.db_dir / f"arm_{arm.lower()}.db"
        return Axiom(str(db_path))

    @staticmethod
    def calculate_cost(prompt_tokens: int, completion_tokens: int, model_name: str = None, base_url: str = None) -> float:
        """Calculate USD cost using standard Nebius pricing for the active runner model."""
        pricing = get_pricing_info(model_name, base_url)
        p_cost = (prompt_tokens / 1_000_000.0) * pricing["prompt_per_1m_usd"]
        c_cost = (completion_tokens / 1_000_000.0) * pricing["completion_per_1m_usd"]
        return p_cost + c_cost

    def record_usage(self, prompt_tokens: int | None, completion_tokens: int | None):
        """Record real token usage; if missing or 0, record as 'missing' without guessing."""
        if prompt_tokens is None or completion_tokens is None or (prompt_tokens == 0 and completion_tokens == 0):
            self.token_usage["missing"] = self.token_usage.get("missing", 0) + 1
            return "missing"
        self.token_usage["prompt_tokens"] = self.token_usage.get("prompt_tokens", 0) + prompt_tokens
        self.token_usage["completion_tokens"] = self.token_usage.get("completion_tokens", 0) + completion_tokens
        cost = self.calculate_cost(prompt_tokens, completion_tokens, self.model, self.base_url)
        self.accumulated_cost_usd += cost
        if self.accumulated_cost_usd >= self.max_cost:
            self.stopped_early = True
            self.stop_reason = f"Max cost reached: ${self.accumulated_cost_usd:.5f} >= ${self.max_cost:.5f}"
        return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}

    def load_receipt(self) -> dict[str, Any]:
        """Load existing receipt from disk if present for resumability."""
        if self.receipt_path and self.receipt_path.exists():
            try:
                return json.loads(self.receipt_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "series": None,
            "instances_completed": 0,
            "completed_instances": [],
            "accumulated_cost_usd": 0.0,
            "total_model_calls": 0,
            "token_usage": {"prompt_tokens": 0, "completion_tokens": 0},
            "stopped_early": False,
            "status": "in_progress",
        }

    def save_receipt(self, receipt_data: dict[str, Any]):
        """Persist current state to JSON receipt file."""
        if self.receipt_path:
            self.receipt_path.parent.mkdir(parents=True, exist_ok=True)
            self.receipt_path.write_text(json.dumps(receipt_data, indent=2), encoding="utf-8")

    def evaluate_instance_arms(
        self,
        task_id: str,
        task_title: str,
        repo_builder: Callable,
        propose_fn: Callable,
        compose_fn: Callable,
        repair_a_fn: Callable = None,
        check_id: str = None,
    ) -> dict[str, Any]:
        """
        Run one task instance through active arms.
        Ensures Arm B and Arm C evaluate byte-identical candidate proposals.
        Enforces call_cap and max_cost.
        """
        if self.stopped_early:
            raise RuntimeError(f"Experiment halted early: {self.stop_reason}")

        if self.call_count >= self.call_cap:
            self.stopped_early = True
            self.stop_reason = f"Model call cap reached ({self.call_cap})"
            raise RuntimeError(self.stop_reason)

        results = {
            "task_id": task_id,
            "task_title": task_title,
            "timestamp": time.time(),
        }

        # Setup separate repos and checks for each arm (Rule 4)
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
            check_name = check_id or f"chk_{task_id}"
            repo_path = repo.root if hasattr(repo, "root") and not isinstance(repo, Path) else Path(repo)
            ax.register_check(
                check_name,
                repo_path,
                [sys.executable, "-I", ".axiom_check/check.py"],
                [],
                holdout=str(check_dir),
                claim_kind=claim_kind,
            )
            ax.post_task("operator", task_title, check_id=check_name)
            task = ax.take_task("agent-1", lease_s=int(os.environ.get("AXIOM_LEASE_SECONDS", 3600)))
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
        base_script = (proc.get("script") or "") if proc else ""
        base_entry = (proc.get("entry_example") or "") if proc else ""

        # Collect sample input files for proposal context
        input_files_text = ""
        c_repo = arm_contexts.get("C", {}).get("repo") or arm_contexts.get("B", {}).get("repo")
        if c_repo:
            repo_root = c_repo.root if hasattr(c_repo, "root") and not isinstance(c_repo, Path) else Path(c_repo)
            samples = []
            for p in sorted(repo_root.glob("receipts/*.txt")):
                try:
                    samples.append(f"--- {p.relative_to(repo_root)}\n{p.read_text(encoding='utf-8').strip()}")
                except Exception:
                    pass
            if samples:
                input_files_text = "\n".join(samples[:4])

        # Step 1: Shared Propose Call for B & C (Rule 1)
        import inspect
        sig = inspect.signature(propose_fn)
        if "input_files" in sig.parameters:
            proposal_reply = propose_fn(task_title, base_script, base_entry, input_files=input_files_text)
        else:
            proposal_reply = propose_fn(task_title, base_script, base_entry)
        self.call_count += 1
        prop_content = proposal_reply.get("content") if isinstance(proposal_reply, dict) and "content" in proposal_reply else proposal_reply
        raw_candidates = extract_candidates(prop_content)

        # Track real usage: if missing, record as 'missing' (NEVER a guess or default)
        u = proposal_reply.get("usage") if isinstance(proposal_reply, dict) else None
        if u and (u.get("prompt_tokens") or u.get("completion_tokens")):
            p_tok = u.get("prompt_tokens")
            c_tok = u.get("completion_tokens")
            rec = self.record_usage(p_tok, c_tok)
            results["propose_token_usage"] = rec
        else:
            self.record_usage(None, None)
            results["propose_token_usage"] = "missing"

        # Build candidate worktree commits for both invariant and domain
        cand_details = {}
        for kind in ("invariant", "domain"):
            cand = raw_candidates.get(kind, {})
            cand_script = cand.get("script", "")
            cand_entry = cand.get("entry", "")

            # Execute candidate in an isolated scratch worktree to obtain execution outputs (Rule 2)
            c_repo = arm_contexts.get("C", {}).get("repo") or arm_contexts["B"]["repo"]
            wt_path = Path(tempfile.mkdtemp(prefix=f"scratch_{task_id}_{kind}_", dir=str(self.db_dir)))
            git_run(c_repo, "worktree", "add", "-q", "--detach", str(wt_path), "main")
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

        # Step 3: Arm A - Baseline Single Repair (if active)
        if "A" in self.arms:
            a_ctx = arm_contexts["A"]
            if repair_a_fn:
                a_res = repair_a_fn(a_ctx)
                results["arm_a"] = a_res
            else:
                results["arm_a"] = "NOT_RUN"

        # Step 4: Arm B - Model Composer (Rule 2 & 3)
        if "B" in self.arms:
            prompt_b = build_composer_prompt(
                base_script,
                base_entry,
                cand_details.get("invariant", {}),
                cand_details.get("domain", {}),
            )
            composer_reply = compose_fn(prompt_b)
            self.call_count += 1
            u = composer_reply.get("usage") if isinstance(composer_reply, dict) else None
            if u and (u.get("prompt_tokens") or u.get("completion_tokens")):
                p_tok = u.get("prompt_tokens")
                c_tok = u.get("completion_tokens")
                rec = self.record_usage(p_tok, c_tok)
                results["compose_token_usage"] = rec
            else:
                self.record_usage(None, None)
                results["compose_token_usage"] = "missing"

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

        # Step 5: Arm C - Axiom Gate (Rule 1 & 3)
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

        # Step 6: Comparison & Agreement (Rule 4)
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


    def evaluate_cold_start_instance(
        self,
        task_id: str,
        task_title: str,
        repo_builder: Callable,
        check_id: str = None,
        cold_start_fn: Callable = None,
        max_steps: int = 60,
    ) -> dict[str, Any]:
        """
        Execute cold start on Instance #0 to attempt to lock version 1.
        If locked_v1 is False, runner must stop early.
        """
        results = {
            "task_id": task_id,
            "task_title": task_title,
            "timestamp": time.time(),
            "cold_start": True,
        }
        check_name = check_id or f"chk_{task_id}"

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
            repo_path = repo.root if hasattr(repo, "root") and not isinstance(repo, Path) else Path(repo)
            ax.register_check(
                check_name,
                repo_path,
                [sys.executable, "-I", ".axiom_check/check.py"],
                [],
                holdout=str(check_dir),
                claim_kind=claim_kind,
            )
            ax.join("agent-cold", ["shell"])
            ax.post_task("operator", task_title, check_id=check_name)
            arm_contexts[arm] = {
                "ax": ax,
                "repo": repo,
                "check_id": check_name,
                "task": None,
                "base_sha": base_sha,
                "claim_kind": claim_kind,
            }

        if cold_start_fn:
            for arm in self.arms:
                arm_contexts[arm]["task"] = arm_contexts[arm]["ax"].take_task("agent-1", lease_s=int(os.environ.get("AXIOM_LEASE_SECONDS", 3600)))
            cs_res = cold_start_fn(arm_contexts)
            results.update(cs_res)
        else:
            from .agent import ChatModel, run_agent
            from .sandbox import LocalSandbox
            model = ChatModel(model=self.model, base_url=self.base_url)
            model.thinking = False

            c_ctx = arm_contexts.get("C") or list(arm_contexts.values())[0]
            wt_path = Path(tempfile.mkdtemp(prefix=f"cs_{task_id}_", dir=str(self.db_dir)))
            git_run(c_ctx["repo"], "worktree", "add", "-q", "--detach", str(wt_path), c_ctx["base_sha"])

            u0 = dict(model.usage)
            t0 = time.time()
            # Ensure lease covers the step budget at measured per-call time (~35s-60s/call)
            lease_duration = max(3600, max_steps * 60)
            os.environ["AXIOM_LEASE_SECONDS"] = str(lease_duration)
            try:
                import asyncio
                asyncio.run(run_agent(
                    "agent-cold",
                    wt_path,
                    str(self.db_dir / "arm_c.db"),
                    model,
                    max_steps=max_steps,
                    log=lambda *_: None,
                    shell=LocalSandbox(),
                    thinking="off",
                ))
            except Exception as e:
                results["error"] = str(e)

            wall_time = round(time.time() - t0, 1)
            p_used = model.usage["prompt_tokens"] - u0["prompt_tokens"]
            c_used = model.usage["completion_tokens"] - u0["completion_tokens"]
            calls_used = model.usage["calls"] - u0["calls"]
            self.call_count += calls_used
            self.record_usage(p_used, c_used)

            proc = c_ctx["ax"].process_for(check_name)
            locked_v1 = proc is not None
            results["locked_v1"] = locked_v1
            results["wall_time_seconds"] = wall_time
            results["model_calls"] = calls_used
            results["token_usage"] = {"prompt_tokens": p_used, "completion_tokens": c_used}
            if locked_v1:
                results["version"] = proc.get("version", 1)
            else:
                # Distinguish exact failure reason:
                # 1. Lease expired under agent
                lease_ev = c_ctx["ax"].db.execute("SELECT * FROM events WHERE kind='lease_expired'").fetchone()
                if lease_ev:
                    reason = "lease expired"
                # 2. Step cap reached mid-work without claim
                elif calls_used >= max_steps:
                    reason = "step cap reached"
                # 3. Refuted on acceptance check
                else:
                    claims = c_ctx["ax"].db.execute("SELECT label, reason FROM claims WHERE agent=?", ("agent-cold",)).fetchall()
                    refuted_claims = [c for c in claims if c["label"] == "refuted"]
                    if refuted_claims:
                        reason = f"refuted: {refuted_claims[-1]['reason']}"
                    else:
                        reason = "no witnessed claim made"
                results["reason"] = reason

        return results


def run_dry_run_series(
    series_name: str,
    n_instances: int = 5,
    arms: list[str] = ("A", "B", "C"),
    base_seed: int = 1000,
    model_name: str = None,
    base_url: str = None,
) -> dict[str, Any]:
    """Execute dry-run for series: load real task series, derive tokens, and output per-arm ranges."""
    task_def = get_task_def(series_name)
    tokens = derive_tokens_for_series(task_def)
    pricing = get_pricing_info(model_name, base_url)

    # List real instances
    instances_preview = []
    for i in range(n_instances):
        seed = base_seed + i
        rng = random.Random(seed)
        files, truth = task_def["data"](rng)
        instances_preview.append({
            "instance": i,
            "seed": seed,
            "file_count": len(files),
            "sample_files": sorted(list(files.keys()))[:3],
        })

    repair_instances = max(0, n_instances - 1)

    # Cold Start: Instance #0 (40 calls, ~137s per arm)
    cs_calls_per_arm = 40
    cs_prompt_tokens = cs_calls_per_arm * tokens["cold_start_prompt_tokens_per_call"]
    cs_comp_tokens = cs_calls_per_arm * tokens["cold_start_comp_tokens_per_call"]
    cs_cost_per_arm = round((cs_prompt_tokens * pricing["prompt_per_1m_usd"] +
                             cs_comp_tokens * pricing["completion_per_1m_usd"]) / 1_000_000.0, 5)

    cold_start_info = {
        "calls_per_arm": cs_calls_per_arm,
        "prompt_tokens_per_arm": cs_prompt_tokens,
        "completion_tokens_per_arm": cs_comp_tokens,
        "cost_usd_per_arm": cs_cost_per_arm,
        "total_calls_all_arms": cs_calls_per_arm * len(arms),
        "total_cost_usd_all_arms": round(cs_cost_per_arm * len(arms), 5),
        "citation": BENCHMARK_CITATIONS["cold_start"],
    }

    # Breakdown per arm for instances 1..N-1
    arms_breakdown = {}

    # Arm A: Today's single repair (multi-turn tool loop, 21 calls / round)
    if "A" in arms:
        calls_per_repair = 21
        rep_p_tokens = calls_per_repair * tokens["repair_a_prompt_tokens_per_call"]
        rep_c_tokens = calls_per_repair * tokens["repair_a_comp_tokens_per_call"]
        rep_cost = (rep_p_tokens * pricing["prompt_per_1m_usd"] +
                    rep_c_tokens * pricing["completion_per_1m_usd"]) / 1_000_000.0
        arms_breakdown["A"] = {
            "arm_name": "Arm A (Baseline: Single Repair Loop)",
            "repair_calls_per_instance": calls_per_repair,
            "best_case": {
                "calls": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "cost_usd": 0.0,
                "note": "All instances 1..N replay successfully after cold start without repair",
            },
            "worst_case": {
                "calls": repair_instances * calls_per_repair,
                "prompt_tokens": repair_instances * rep_p_tokens,
                "completion_tokens": repair_instances * rep_c_tokens,
                "cost_usd": round(repair_instances * rep_cost, 5),
                "note": "Every instance 1..N strays and triggers full 21-call repair loop",
            },
            "citation": BENCHMARK_CITATIONS["single_repair"],
        }

    # Arm B: K=2 Model Composer (1 propose + 1 compose per instance; fallback 21 calls)
    if "B" in arms:
        prop_p = tokens["propose_prompt_tokens"]
        prop_c = tokens["propose_completion_tokens"]
        comp_p = tokens["compose_prompt_tokens"]
        comp_c = tokens["compose_completion_tokens"]
        b_inst_p = prop_p + comp_p
        b_inst_c = prop_c + comp_c
        b_inst_cost = (b_inst_p * pricing["prompt_per_1m_usd"] +
                       b_inst_c * pricing["completion_per_1m_usd"]) / 1_000_000.0
        fallback_calls = 21
        arms_breakdown["B"] = {
            "arm_name": "Arm B (Harness^2-style Model Composer)",
            "propose_calls_per_instance": 1,
            "compose_calls_per_instance": 1,
            "fallback_repair_calls": fallback_calls,
            "best_case": {
                "calls": repair_instances * 2,
                "prompt_tokens": repair_instances * b_inst_p,
                "completion_tokens": repair_instances * b_inst_c,
                "cost_usd": round(repair_instances * b_inst_cost, 5),
                "note": "Model composer selects valid winner every round; 0 fallbacks",
            },
            "worst_case": {
                "calls": repair_instances * (2 + fallback_calls),
                "prompt_tokens": repair_instances * (b_inst_p + fallback_calls * tokens["repair_a_prompt_tokens_per_call"]),
                "completion_tokens": repair_instances * (b_inst_c + fallback_calls * tokens["repair_a_comp_tokens_per_call"]),
                "cost_usd": round(repair_instances * (b_inst_cost + (fallback_calls * 2200 * 0.20 + fallback_calls * 300 * 0.60) / 1e6), 5),
                "note": "No PPC winner found; every round triggers full fallback repair",
            },
        }

    # Arm C: K=2 Axiom Gate (1 propose + 0 compose; gate is local Python verifier; fallback 21 calls)
    if "C" in arms:
        prop_p = tokens["propose_prompt_tokens"]
        prop_c = tokens["propose_completion_tokens"]
        c_inst_cost = (prop_p * pricing["prompt_per_1m_usd"] +
                       prop_c * pricing["completion_per_1m_usd"]) / 1_000_000.0
        fallback_calls = 21
        arms_breakdown["C"] = {
            "arm_name": "Arm C (Axiom Gate: 0-model-call verifier & regression gate)",
            "propose_calls_per_instance": 1,
            "compose_calls_per_instance": 0,
            "fallback_repair_calls": fallback_calls,
            "best_case": {
                "calls": repair_instances * 1,
                "prompt_tokens": repair_instances * prop_p,
                "completion_tokens": repair_instances * prop_c,
                "cost_usd": round(repair_instances * c_inst_cost, 5),
                "note": "Axiom gate selects verified winner; 0 model compose calls; 0 fallbacks",
            },
            "worst_case": {
                "calls": repair_instances * (1 + fallback_calls),
                "prompt_tokens": repair_instances * (prop_p + fallback_calls * tokens["repair_a_prompt_tokens_per_call"]),
                "completion_tokens": repair_instances * (prop_c + fallback_calls * tokens["repair_a_comp_tokens_per_call"]),
                "cost_usd": round(repair_instances * (c_inst_cost + (fallback_calls * 2200 * 0.20 + fallback_calls * 300 * 0.60) / 1e6), 5),
                "note": "No PPC candidate passes gate; every round triggers full fallback repair",
            },
        }

    # Sum overall totals
    best_calls_repairs = sum(arms_breakdown[a]["best_case"]["calls"] for a in arms if a in arms_breakdown)
    best_cost_repairs = sum(arms_breakdown[a]["best_case"]["cost_usd"] for a in arms if a in arms_breakdown)
    worst_calls_repairs = sum(arms_breakdown[a]["worst_case"]["calls"] for a in arms if a in arms_breakdown)
    worst_cost_repairs = sum(arms_breakdown[a]["worst_case"]["cost_usd"] for a in arms if a in arms_breakdown)

    plan = {
        "series": series_name,
        "task_ask": task_def.get("ask", ""),
        "instances": n_instances,
        "dry_run": True,
        "arms": list(arms),
        "citations": {
            "pricing": pricing,
            "benchmarks": BENCHMARK_CITATIONS,
        },
        "derived_tokens": tokens,
        "cold_start": cold_start_info,
        "arms_breakdown": arms_breakdown,
        "overall_projection_excluding_cold_start": {
            "best_case_calls": best_calls_repairs,
            "best_case_cost_usd": round(best_cost_repairs, 5),
            "worst_case_calls": worst_calls_repairs,
            "worst_case_cost_usd": round(worst_cost_repairs, 5),
        },
        "overall_projection_including_cold_start": {
            "best_case_calls": best_calls_repairs + cold_start_info["total_calls_all_arms"],
            "best_case_cost_usd": round(best_cost_repairs + cold_start_info["total_cost_usd_all_arms"], 5),
            "worst_case_calls": worst_calls_repairs + cold_start_info["total_calls_all_arms"],
            "worst_case_cost_usd": round(worst_cost_repairs + cold_start_info["total_cost_usd_all_arms"], 5),
        },
        "instances_preview": instances_preview,
    }
    return plan


def run_series(
    series_name: str,
    n_instances: int = 5,
    arms: list[str] = ("A", "B", "C"),
    db_dir: Path | str = ".axiom_arms_run",
    receipt_path: Path | str = "arms_receipt.json",
    max_cost: float = 1.0,
    call_cap: int = 100,
    confirm_paid: bool = False,
    propose_fn: Callable = None,
    compose_fn: Callable = None,
    repair_a_fn: Callable = None,
    cold_start_fn: Callable = None,
    model_name: str = None,
    base_url: str = None,
    base_seed: int = 1000,
) -> dict[str, Any]:
    """Execute series runner across instances with resumable receipts and paid-run authorization guard."""
    task_def = get_task_def(series_name)
    import everyday_tasks as et

    endpoint = get_runner_endpoint(base_url)
    is_local = endpoint_is_local(endpoint)

    # Guard: Require explicit authorization for paid execution (local endpoints exempt)
    if not is_local and not confirm_paid and (propose_fn is None or compose_fn is None):
        sys.stderr.write(
            "Error: Refusing to execute paid model calls without --confirm-paid.\n"
            "Pass --dry-run to view cost/token projections, or pass --confirm-paid to authorize execution.\n"
        )
        sys.exit(1)

    model = model_name or get_runner_model()

    # Wire live model client if not provided
    if propose_fn is None or compose_fn is None:
        from .agent import ChatModel
        chat_model = ChatModel(model=model, base_url=endpoint)
        chat_model.thinking = False

        def live_propose(title, base_script, base_entry, input_files="(see workspace)"):
            u0 = dict(chat_model.usage)
            prompt = PPC_PROPOSAL_PROMPT.format(
                base_script=base_script,
                entry_example=base_entry,
                replay_note=f"Task: {title}",
                input_files=input_files or "(see workspace)",
                witnessed_lessons="(none yet)",
                declared_lessons="(none yet)",
            )
            msg = chat_model([
                {"role": "system", "content": "You are a software repair engine for verified processes."},
                {"role": "user", "content": prompt},
            ], [])
            p_used = chat_model.usage["prompt_tokens"] - u0["prompt_tokens"]
            c_used = chat_model.usage["completion_tokens"] - u0["completion_tokens"]
            res = dict(msg) if isinstance(msg, dict) else {"content": str(msg)}
            if p_used > 0 or c_used > 0:
                res["usage"] = {"prompt_tokens": p_used, "completion_tokens": c_used}
            return res

        def live_compose(prompt):
            u0 = dict(chat_model.usage)
            msg = chat_model([
                {"role": "system", "content": COMPOSER_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ], [])
            p_used = chat_model.usage["prompt_tokens"] - u0["prompt_tokens"]
            c_used = chat_model.usage["completion_tokens"] - u0["completion_tokens"]
            content = msg.get("content", "") if isinstance(msg, dict) else str(msg)
            res = parse_composer_decision(content)
            if p_used > 0 or c_used > 0:
                res["usage"] = {"prompt_tokens": p_used, "completion_tokens": c_used}
            return res

        propose_fn = live_propose
        compose_fn = live_compose

    exp = ArmsExperiment(
        db_dir=db_dir,
        arms=arms,
        max_cost=max_cost,
        call_cap=call_cap,
        receipt_path=receipt_path,
        model_name=model,
        base_url=endpoint,
    )

    receipt = exp.load_receipt()
    model = model_name or get_runner_model()
    receipt.update({
        "series": series_name,
        "instances_requested": n_instances,
        "arms": list(arms),
        "endpoint": endpoint,
        "model": model,
        "is_local": is_local,
    })

    completed_ids = {inst["task_id"] for inst in receipt.get("completed_instances", [])}
    if f"{series_name}_0" in completed_ids and not receipt.get("instance_0_locked_v1", False):
        exp.stopped_early = True
        exp.stop_reason = receipt.get("stop_reason") or f"Instance #0 (cold start) did not lock version 1; stopped series"
        receipt["stopped_early"] = True
        receipt["stop_reason"] = exp.stop_reason
        return receipt

    for i in range(n_instances):
        if exp.stopped_early:
            break

        task_id = f"{series_name}_{i}"
        if task_id in completed_ids:
            continue

        seed = base_seed + i

        def repo_builder(arm=""):
            tmp_root = Path(tempfile.mkdtemp(prefix=f"inst_{i}_{arm}_", dir=str(exp.db_dir)))
            repo, check_dir, files, truth = et.build(task_def, tmp_root, seed)
            base_sha = git_run(repo, "rev-parse", "HEAD")
            return repo, check_dir, base_sha, "deliver"

        # Instance 0 check: if no locked process exists, run cold start
        if i == 0:
            c_ax = exp.get_axiom("C")
            proc = c_ax.process_for(f"chk_{series_name}")
            if proc is None:
                inst_res = exp.evaluate_cold_start_instance(
                    task_id=task_id,
                    task_title=f"{task_def.get('ask', series_name)} (instance {i})",
                    repo_builder=repo_builder,
                    check_id=f"chk_{series_name}",
                    cold_start_fn=cold_start_fn,
                )
                receipt["completed_instances"].append(inst_res)
                receipt["instances_completed"] = len(receipt["completed_instances"])
                receipt["instance_0_locked_v1"] = inst_res.get("locked_v1", False)
                receipt["accumulated_cost_usd"] = 0.0 if is_local else round(exp.accumulated_cost_usd, 5)
                receipt["total_model_calls"] = exp.call_count
                receipt["token_usage"] = dict(exp.token_usage)

                if not inst_res.get("locked_v1", False):
                    exp.stopped_early = True
                    exp.stop_reason = f"Instance #0 (cold start) did not lock version 1 ({inst_res.get('reason', 'unwitnessed')}); stopped series"
                    receipt["stopped_early"] = True
                    receipt["stop_reason"] = exp.stop_reason
                    receipt["status"] = "stopped_cold_start_unwitnessed"
                    exp.save_receipt(receipt)
                    break

                exp.save_receipt(receipt)
                continue

        inst_res = exp.evaluate_instance_arms(
            task_id=task_id,
            task_title=f"{task_def.get('ask', series_name)} (instance {i})",
            repo_builder=repo_builder,
            propose_fn=propose_fn,
            compose_fn=compose_fn,
            repair_a_fn=repair_a_fn,
            check_id=f"chk_{series_name}",
        )

        receipt["completed_instances"].append(inst_res)
        receipt["instances_completed"] = len(receipt["completed_instances"])
        receipt["accumulated_cost_usd"] = round(exp.accumulated_cost_usd, 5)
        receipt["total_model_calls"] = exp.call_count
        receipt["token_usage"] = dict(exp.token_usage)
        receipt["stopped_early"] = exp.stopped_early
        receipt["stop_reason"] = exp.stop_reason
        receipt["status"] = "partial" if exp.stopped_early or receipt["instances_completed"] < n_instances else "complete"

        exp.save_receipt(receipt)

    return receipt


def main():
    parser = argparse.ArgumentParser(description="Axiom-1 3-Arm Experiment Runner (Harness^2 vs Gate)")
    parser.add_argument("--series", default="receipts-to-spreadsheet", help="Task series name from everyday_tasks")
    parser.add_argument("--instances", type=int, default=5, help="Number of instances in series")
    parser.add_argument("--arms", default="A,B,C", help="Comma-separated arms to run (e.g. A,B,C)")
    parser.add_argument("--db-dir", default=".axiom_arms_run", help="Directory for arm database files")
    parser.add_argument("--dry-run", action="store_true", help="Print experiment plan and cost estimate without running models")
    parser.add_argument("--max-cost", type=float, default=1.0, help="Max cost cap in USD")
    parser.add_argument("--call-cap", type=int, default=100, help="Maximum allowed model API calls")
    parser.add_argument("--receipt", default="arms_receipt.json", help="Path to write experiment receipt")
    parser.add_argument("--confirm-paid", action="store_true", help="Authorize execution of paid Nebius model calls")

    args = parser.parse_args()
    arms_list = [a.strip().upper() for a in args.arms.split(",") if a.strip()]

    if args.dry_run:
        try:
            plan = run_dry_run_series(args.series, args.instances, arms=arms_list)
        except ValueError as e:
            sys.exit(f"Error: {e}")
        print("=== AXIOM-1 3-ARM EXPERIMENT DRY-RUN PLAN ===")
        print(json.dumps(plan, indent=2))
        return

    endpoint = get_runner_endpoint()
    is_local = endpoint_is_local(endpoint)

    # Hard safety guard: Explicitly reject paid model execution without --confirm-paid (local endpoints exempt)
    if not is_local and not args.confirm_paid:
        sys.stderr.write(
            "Error: Refusing to execute paid model calls without --confirm-paid.\n"
            "Pass --dry-run to view cost/token projections, or pass --confirm-paid to authorize execution.\n"
        )
        sys.exit(1)

    # Live runner execution path
    try:
        receipt = run_series(
            series_name=args.series,
            n_instances=args.instances,
            arms=arms_list,
            db_dir=args.db_dir,
            receipt_path=args.receipt,
            max_cost=args.max_cost,
            call_cap=args.call_cap,
            confirm_paid=args.confirm_paid,
        )
        print("=== AXIOM-1 3-ARM EXPERIMENT RECEIPT ===")
        print(json.dumps(receipt, indent=2))
    except SystemExit as e:
        sys.exit(str(e))
    except Exception as e:
        sys.exit(f"Execution error: {e}")


if __name__ == "__main__":
    main()
