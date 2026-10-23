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



PPC_INVARIANT_PROMPT = """You are the Invariant Proposer repairing a locked process for an AI task that failed on new input files.
Focus on structural robustness across instances (handling formatting variances, defensive parsing, fallback defaults). Must preserve compatibility with earlier tasks.

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

Return ONLY a JSON object with this shape:
{{
  "script": "<repaired python code for process.py>",
  "entry": "<repaired entry.json content or empty string>"
}}
"""

PPC_DOMAIN_PROMPT = """You are the Domain Proposer repairing a locked process for an AI task that failed on new input files.
Focus on domain-specific extraction rules tailored specifically to this job's inputs and observed receipt formats.

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

Return ONLY a JSON object with this shape:
{{
  "script": "<repaired python code for process.py>",
  "entry": "<repaired entry.json content or empty string>"
}}
"""


def extract_single_candidate(reply_content: str | dict, default_kind: str = "invariant") -> dict[str, str]:
    """Parse a single candidate proposal (script and entry) from model reply."""
    if isinstance(reply_content, dict):
        data = reply_content
    else:
        raw_text = str(reply_content).strip()
        if "```json" in raw_text:
            text = raw_text.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in raw_text:
            text = raw_text.split("```", 1)[1].split("```", 1)[0].strip()
        else:
            text = raw_text
        try:
            data = json.loads(text)
        except Exception:
            import re
            m = re.search(r"\{.*\}", text, re.DOTALL)
            data = json.loads(m.group(0)) if m else {}

    if default_kind in data and isinstance(data[default_kind], dict):
        return {
            "script": str(data[default_kind].get("script", "")),
            "entry": str(data[default_kind].get("entry", "")),
        }
    return {
        "script": str(data.get("script", "")),
        "entry": str(data.get("entry", "")),
    }

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
        raw_text = str(reply_content).strip()
        if "```json" in raw_text:
            text = raw_text.split("```json", 1)[1].split("```", 1)[0].strip()
        elif "```" in raw_text:
            text = raw_text.split("```", 1)[1].split("```", 1)[0].strip()
        else:
            text = raw_text
        try:
            data = json.loads(text)
        except Exception:
            import re
            m = re.search(r"\{.*\}", text, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group(0))
                except Exception:
                    data = {}
            else:
                data = {}

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

    # 2. PREPARE CANDIDATES IN WORKTREE
    candidates_payload = []
    messages = [{"role": "user", "content": f"PPC repair round for task {task['id']}"}]

    for kind in ("invariant", "domain"):
        cand = candidates.get(kind)
        if not cand:
            continue
        log(f"[{agent_id}] Preparing candidate commit: {kind}")

        # Write candidate files to workspace
        ws.write_file(PROCESS_FILE, cand["script"])
        if cand["entry"]:
            ws.write_file(ENTRY_FILE, cand["entry"] if isinstance(cand["entry"], str) else json.dumps(cand["entry"]))
        elif entry_example:
            ws.write_file(ENTRY_FILE, entry_example)

        # Run process in workspace to generate any delivered artifacts
        ran = ws.run(" ".join(process_argv("python")))
        if ran.get("exit") != 0:
            log(f"[{agent_id}] Candidate {kind} basic execution exit {ran.get('exit')}")

        sha = ws.commit(f"PPC candidate {kind}")["sha"]
        candidates_payload.append({"kind": kind, "after_ref": sha})

    if not candidates_payload:
        if anti_drift:
            log(f"[{agent_id}] Anti-drift: resetting workspace to verified baseline process")
            ws.write_file(PROCESS_FILE, base_script)
            if entry_example:
                ws.write_file(ENTRY_FILE, entry_example)
        return {
            "witnessed": False,
            "winner": None,
            "refused": [],
            "messages": messages,
            "note": "No candidates passed basic execution",
        }

    # 3. SUBMIT TO SERVER GATE VIA SINGLE MCP TOOL
    # The server probes both, applies the gate + regression, locks winner as v+1,
    # and writes refusal lessons using verifier reasons.
    try:
        res = await session.call_tool("submit_candidates", {
            "task_id": task["id"],
            "before_ref": start_sha,
            "candidates": candidates_payload,
        })
        data = json.loads(res.content[0].text) if res.content else {}
    except Exception as e:
        log(f"[{agent_id}] submit_candidates call failed: {e}")
        data = {"witnessed": False, "reason": str(e)}

    if data.get("witnessed"):
        win_kind = data.get("winner")
        win_cand = candidates.get(win_kind)
        if win_cand:
            ws.write_file(PROCESS_FILE, win_cand["script"])
            if win_cand.get("entry"):
                ws.write_file(ENTRY_FILE, win_cand["entry"])
        messages.append({"role": "assistant", "content": f"PPC Winner: {win_kind}"})
        return {
            "witnessed": True,
            "winner": {"candidate": {"kind": win_kind}, "diff": data.get("winner_diff", 0)},
            "refused": data.get("refused", []),
            "messages": messages,
            "note": f"PPC winner locked: {win_kind}",
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
        "refused": data.get("refused", []),
        "messages": messages,
        "note": data.get("reason", "No PPC candidate passed check and regression suite"),
    }
