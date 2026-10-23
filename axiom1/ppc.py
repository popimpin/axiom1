"""
Harness^2 P-P-C (Propose-Probe-Compose) Repair Engine for Axiom-1.

Adapts Google Research's Harness^2 (arXiv:2610.14224) to Axiom-1's sovereign verifier:
  1. PROPOSE (Model Slot): Generates K=2 candidate repairs from Axiom-1 memory
     (verified skills/lessons separated from unverified attempts):
       - Candidate 1 ('invariant'): Cross-instance robustness, resilient parsing, defensive guards.
       - Candidate 2 ('domain'): Domain-specific logic and extraction heuristics.
  2. PROBE (0 model calls): Executes both candidates in isolated sandboxes on the task.
  3. COMPOSE (The Gate): Decided deterministically by Axiom-1's acceptance check,
     tamper canaries, and historical regression verification (_regression across process_instances).
     Tie-break: smallest diff against the baseline process.
  4. MEMORY IN/OUT & ANTI-DRIFT:
     - Propose reads verified memory (witnessed facts vs declared attempts).
     - Winning repair locks as v+1; refused candidates and reasons write back to memory.
     - Workspace is reset to verified baseline on failure so unverified candidate drift
       never leaks into round N+1.
"""
from __future__ import annotations

import difflib
import json
from typing import Any, Callable

from .verifier import ENTRY_FILE, PROCESS_FILE, process_argv


PPC_PROPOSAL_PROMPT = """You are repairing a locked process for an AI task that failed on new input files.

CURRENT LOCKED PROCESS (process.py):
```python
{base_script}
```

EXAMPLE ENTRY (entry.json):
```json
{entry_example}
```

FAILURE FEEDBACK FROM REPLAY:
{replay_note}

THIS TASK'S INPUT FILES:
{input_files}

VERIFIED MEMORY (Proven Skills / Witnessed Facts):
{witnessed_lessons}

UNVERIFIED ATTEMPTS (Declared Only):
{declared_lessons}

PROPOSE K=2 CANDIDATES to repair process.py (and optionally entry.json):
1. Invariant Candidate: Focus on structural robustness across instances (handling formatting
   variances, defensive parsing, fallback defaults). Must preserve compatibility with earlier tasks.
2. Domain Candidate: Focus on domain-specific extraction rules tailored to this specific job's inputs.

Return ONLY a JSON object with this exact shape:
{{
  "invariant": {{
    "script": "<repaired python code for process.py>",
    "entry": "<repaired entry.json content or empty string>"
  }},
  "domain": {{
    "script": "<repaired python code for process.py>",
    "entry": "<repaired entry.json content or empty string>"
  }}
}}
"""


def format_memory(lessons: list[dict[str, Any]] | None) -> tuple[str, str]:
    """Separate verified lessons (witnessed) from unverified attempts (declared)."""
    witnessed, declared = [], []
    for l in lessons or []:
        body = l.get("lesson") or l.get("body") or l.get("reason") or str(l)
        label = l.get("label", "declared")
        if label == "witnessed":
            witnessed.append(f"- [WITNESSED] {body}")
        else:
            declared.append(f"- [UNVERIFIED] {body}")
    w_text = "\n".join(witnessed) if witnessed else "(no verified lessons yet)"
    d_text = "\n".join(declared) if declared else "(no unverified attempts)"
    return w_text, d_text


def diff_size(base_script: str, cand_script: str) -> int:
    """Compute character edit distance / diff size against baseline for tie-breaking."""
    matcher = difflib.SequenceMatcher(None, base_script, cand_script)
    return int((1.0 - matcher.ratio()) * 10000)


def extract_candidates(reply_content: str | dict) -> dict[str, dict[str, str]]:
    """Parse candidate proposals from model output or structured dict."""
    if isinstance(reply_content, dict):
        data = reply_content
    else:
        text = reply_content.strip()
        if "```json" in text:
            text = text.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in text:
            text = text.split("```", 1)[1].split("```", 1)[0].strip()
        data = json.loads(text)

    candidates = {}
    for kind in ("invariant", "domain"):
        if kind in data and isinstance(data[kind], dict):
            c = data[kind]
            script = c.get("script", "")
            entry = c.get("entry", "")
            if script:
                candidates[kind] = {
                    "kind": kind,
                    "script": script,
                    "entry": entry if entry else None,
                }
    return candidates


async def repair_ppc(
    agent_id: str,
    task: dict[str, Any],
    replay: dict[str, Any],
    model: Callable | Any,
    ws: Any,
    session: Any,
    start_sha: str,
    log: Callable[[str], None] = print,
    anti_drift: bool = True,
) -> dict[str, Any]:
    """
    Run K=2 Propose-Probe-Compose repair with memory in/out and anti-drift reset.
    Returns:
      {
        "witnessed": bool,
        "winner": dict | None,
        "refused": list[dict],
        "messages": list[dict],
        "note": str
      }
    """
    proc = task["process"]
    base_script = proc["script"]
    entry_example = proc.get("entry_example", "")

    # 1. MEMORY IN: Propose from verified memory vs declared attempts
    w_mem, d_mem = format_memory(task.get("lessons"))
    from .agent import _input_files
    input_files_text = _input_files(ws)

    prompt = PPC_PROPOSAL_PROMPT.format(
        base_script=base_script,
        entry_example=entry_example,
        replay_note=replay.get("note", ""),
        input_files=input_files_text,
        witnessed_lessons=w_mem,
        declared_lessons=d_mem,
    )

    log(f"[{agent_id}] Harness^2 PPC: Proposing K=2 candidates (invariant + domain)")
    try:
        reply = model([{"role": "system", "content": "You are a software repair engine for verified processes."},
                       {"role": "user", "content": prompt}], [])
        content = reply.get("content") if isinstance(reply, dict) else str(reply)
        candidates = extract_candidates(content)
    except Exception as e:
        log(f"[{agent_id}] PPC proposal failed: {e}")
        candidates = {}

    if not candidates:
        return {"witnessed": False, "winner": None, "refused": [], "messages": [],
                "note": "PPC proposal yielded no valid candidates"}

    # 2. PROBE K=2 in parallel/isolated sandboxes (0 model calls)
    probed_results = []
    messages = [{"role": "user", "content": f"PPC repair round for task {task['id']}"}]

    for kind in ("invariant", "domain"):
        cand = candidates.get(kind)
        if not cand:
            continue
        log(f"[{agent_id}] Probing candidate: {kind}")

        # Write candidate files to workspace
        ws.write_file(PROCESS_FILE, cand["script"])
        if cand["entry"]:
            ws.write_file(ENTRY_FILE, cand["entry"] if isinstance(cand["entry"], str) else json.dumps(cand["entry"]))
        elif entry_example:
            ws.write_file(ENTRY_FILE, entry_example)

        # Run process to verify basic execution
        ran = ws.run(" ".join(process_argv("python")))
        if ran.get("exit") != 0:
            probed_results.append({
                "candidate": cand,
                "passed": False,
                "reproduced": False,
                "regression_failed": 0,
                "reason": f"process execution failed with exit {ran.get('exit')}: {ran.get('output', '')[-200:]}",
                "diff": diff_size(base_script, cand["script"]),
                "regression": {},
            })
            continue

        # Commit and claim through the gate (probe without task_id so task remains open during K=2 probing)
        sha = ws.commit(f"PPC candidate {kind}")["sha"]
        try:
            res = await session.call_tool("claim", {
                "statement": f"PPC probe ({kind})",
                "check_id": task["check_id"],
                "before_ref": start_sha,
                "after_ref": sha,
            })
            claim = json.loads(res.content[0].text) if res.content else {}
            res_v = await session.call_tool("verify", {"claim_id": claim.get("id")})
            verdict = json.loads(res_v.content[0].text) if res_v.content else {}
        except Exception as e:
            verdict = {"label": "error", "reason": str(e)}

        is_witnessed = verdict.get("label") == "witnessed"
        ev = verdict.get("evidence") or {}
        p_ev = ev.get("process") or {}
        regression = p_ev.get("regression") or {}
        reproduced = p_ev.get("reproduced", False)
        reg_failed = regression.get("failed", 0)

        probed_results.append({
            "candidate": cand,
            "sha": sha,
            "passed": is_witnessed,
            "reproduced": reproduced,
            "regression_failed": reg_failed,
            "regression": regression,
            "reason": verdict.get("reason", ""),
            "diff": diff_size(base_script, cand["script"]),
            "verdict": verdict,
            "claim_id": claim.get("id"),
        })

    # 3. COMPOSE = THE GATE (Check + Tamper + Regression over past instances)
    # A candidate wins ONLY if it passes the acceptance check AND does not regress any earlier instance
    valid_winners = [
        r for r in probed_results
        if r["passed"] and r["reproduced"] and r["regression_failed"] == 0
    ]

    winner = None
    refused = []

    if valid_winners:
        # Tie-break on smallest diff against base process
        valid_winners.sort(key=lambda r: r["diff"])
        winner = valid_winners[0]
        # Any other candidates are refused / runners-up
        for r in probed_results:
            if r is not winner:
                refused.append(r)
    else:
        refused = probed_results

    # 4. MEMORY WRITE-BACK & ANTI-DRIFT
    # Log refused candidates and write reasons to memory so next round knows what failed
    for r in refused:
        cand_kind = r["candidate"]["kind"]
        ref_reason = r["reason"]
        if r.get("regression_failed", 0) > 0:
            ref_reason = f"regression: broke {r['regression_failed']} earlier instance(s)"
        log(f"[{agent_id}] PPC candidate refused ({cand_kind}): {ref_reason}")
        try:
            await session.call_tool("remember", {
                "key": f"ppc:refused:{task['id']}:{cand_kind}",
                "content": f"[refused] Candidate {cand_kind} failed: {ref_reason}",
            })
        except Exception:
            pass

    if winner:
        log(f"[{agent_id}] PPC Winner chosen: {winner['candidate']['kind']} (diff={winner['diff']}) -> locking via task claim")
        # Ensure winning script is active in workspace
        ws.write_file(PROCESS_FILE, winner["candidate"]["script"])
        if winner["candidate"]["entry"]:
            ws.write_file(ENTRY_FILE, winner["candidate"]["entry"])
        win_sha = ws.commit(f"PPC winner {winner['candidate']['kind']}")["sha"]
        # Claim with task_id to complete the task lease and attribute winner
        try:
            res_win = await session.call_tool("claim", {
                "statement": f"PPC winner ({winner['candidate']['kind']})",
                "check_id": task["check_id"],
                "before_ref": start_sha,
                "after_ref": win_sha,
                "task_id": task["id"],
            })
            claim_win = json.loads(res_win.content[0].text) if res_win.content else {}
            await session.call_tool("verify", {"claim_id": claim_win.get("id")})
        except Exception as e:
            log(f"[{agent_id}] Error finalizing winner claim: {e}")

        messages.append({"role": "assistant", "content": f"PPC Winner: {winner['candidate']['kind']}"})
        return {
            "witnessed": True,
            "winner": winner,
            "refused": refused,
            "messages": messages,
            "note": f"PPC winner locked: {winner['candidate']['kind']}",
        }

    # ANTI-DRIFT RESET: Neither candidate was verified without regression.
    # Restore workspace process.py to the verified baseline so round N+1 does NOT leak drift!
    if anti_drift:
        log(f"[{agent_id}] Anti-drift: resetting workspace to verified baseline process")
        ws.write_file(PROCESS_FILE, base_script)
        if entry_example:
            ws.write_file(ENTRY_FILE, entry_example)

    return {
        "witnessed": False,
        "winner": None,
        "refused": refused,
        "messages": messages,
        "note": "No PPC candidate passed check and regression suite",
    }
