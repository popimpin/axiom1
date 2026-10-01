"""Does the collective get better because failures are recorded? Lessons on vs off.

Four arms: lessons on + thinking auto (the harness decides), lessons on + thinking always on, lessons
off + thinking on, lessons off + thinking off. Tokens and time are recorded per agent.

A sequence of fresh agents takes the same job one after another (when one succeeds, the job comes
round again). With lessons ON, each agent's take_task carries the server's record of the earlier
attempts. With lessons OFF, the record is kept but not handed out. Same model, same checks, no shell.
Per agent position it records whether the fix was witnessed, false claims before success, tool calls.
Needs NEBIUS_API_KEY and Docker.

    python examples/lessons_ablation.py --agents 4 --sequences 3 [--out results.json]
"""
import argparse
import asyncio
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from axiom1 import Axiom  # noqa: E402
from axiom1.agent import ChatModel, run_agent  # noqa: E402

IMAGE = "python:3.13-slim"
TITLE = "add(2, 3) returns -1; it should return 5. Fix calc.py."
BUGGY = "def add(a, b):\n    return a - b\n"
HOLDOUT = ("import unittest\nfrom calc import add\n\n"
           "class Held(unittest.TestCase):\n"
           "    def test_general(self):\n"
           "        for a, b in [(10, -4), (0, 0), (-7, -8), (123, 456)]:\n"
           "            self.assertEqual(add(a, b), a + b)\n")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=operator", "-c", "user.email=op@axiom1.invalid",
                           *args], capture_output=True, text=True, check=True).stdout.strip()


def sequence(model, share, n_agents, max_steps, thinking="on"):
    os.environ["AXIOM_SHARE_LESSONS"] = "1" if share else "0"
    rows = []
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo, db = Path(tmp) / "repo", str(Path(tmp) / "axiom1.db")
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        (repo / "calc.py").write_text(BUGGY, encoding="utf-8")
        (repo / "README.md").write_text("A tiny calculator. add(a, b) should return a + b.\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "calculator")
        holdout = Path(tmp) / "holdout"
        holdout.mkdir()
        (holdout / "test_held.py").write_text(HOLDOUT, encoding="utf-8")
        ax = Axiom(db, share_lessons=share)
        ax.register_check("unit", repo, ["python", "-m", "unittest", "discover", "-s", "tests"], ["tests/"],
                          sandbox="docker", image=IMAGE, holdout=str(holdout))
        ax.join("operator", ["shell"])
        ax.db.close()

        for i in range(n_agents):
            ax = Axiom(db, share_lessons=share)
            if not ax.db.execute("SELECT 1 FROM tasks WHERE status='open'").fetchone():
                ax.post_task("operator", TITLE)                 # the job comes round again
            lessons_before = ax.db.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
            ax.db.close()
            wt = Path(tmp) / f"wt{i}"
            git(repo, "worktree", "add", "-q", "--detach", str(wt), "main")
            agent = f"agent{i}"
            before = dict(model.usage)
            t0 = time.time()
            messages = asyncio.run(run_agent(agent, wt, db, model, max_steps=max_steps, log=lambda *_: None,
                                             thinking=thinking))
            seconds = round(time.time() - t0, 1)
            used = {k: model.usage[k] - before[k] for k in model.usage}
            calls = [c["function"]["name"] for m in messages if m["role"] == "assistant"
                     for c in m.get("tool_calls", [])]
            # what the agent was actually handed: lessons inside its take_task results
            received = 0
            # the harness takes the task before the first call: its lessons arrive in the opening message
            opening = next((m["content"] for m in messages if m["role"] == "user"), "")
            if opening.startswith("Your task (leased to you): "):
                try:
                    received = len(json.loads(opening.split(": ", 1)[1]).get("lessons", []))
                except ValueError:
                    pass
            for m in messages:
                if m["role"] == "tool" and '"lessons"' in m["content"]:
                    try:
                        received = max(received, len((json.loads(m["content"]).get("task") or {}).get("lessons", [])))
                    except (ValueError, AttributeError):
                        pass
            ax = Axiom(db)
            # only ANCHORED wins count: a fix that starts from the agent's own commit earns no skill
            labels = ["witnessed" if r["label"] == "witnessed" and r["anchored"] else
                      ("witnessed_unanchored" if r["label"] == "witnessed" else r["label"])
                      for r in ax.db.execute("SELECT label, anchored FROM claims WHERE agent=? ORDER BY made_at",
                                             (agent,))]
            ax.db.close()
            first = labels.index("witnessed") if "witnessed" in labels else None
            rows.append({"arm": f"lessons_{'on' if share else 'off'}+thinking_{thinking}", "share": share,
                         "thinking": thinking, "seconds": seconds, "completion_tokens": used["completion_tokens"],
                         "prompt_tokens": used["prompt_tokens"], "thinking_calls": used["thinking_calls"],
                         "model_calls": used["calls"],
                         "position": i, "lessons_in_record": lessons_before,
                         "lessons_received": received,
                         "witnessed": first is not None,
                         "false_claims": first if first is not None else labels.count("refuted"),
                         "claims": labels, "tool_calls": len(calls)})
            print(json.dumps(rows[-1]), flush=True)
    return rows


ARMS = [(True, "auto"), (True, "on"), (False, "on"), (False, "off")]


def summary(rows):
    out = {}
    for share, thinking in ARMS:
        arm = [r for r in rows if r["share"] is share and r["thinking"] == thinking]
        if not arm:
            continue
        later = [r for r in arm if r["position"] > 0]
        med = lambda xs: statistics.median(xs) if xs else None  # noqa: E731
        out[f"lessons_{'on' if share else 'off'}+thinking_{thinking}"] = {
            "witnessed": f"{sum(r['witnessed'] for r in arm)}/{len(arm)}",
            "false_claims": sum(r["false_claims"] for r in arm),
            "later_agents_witnessed": f"{sum(r['witnessed'] for r in later)}/{len(later)}",
            "later_agents_median_tool_calls": med([r["tool_calls"] for r in later]),
            "median_seconds": med([r["seconds"] for r in arm]),
            "median_completion_tokens": med([r["completion_tokens"] for r in arm]),
            "median_prompt_tokens": med([r["prompt_tokens"] for r in arm]),
            "share_of_calls_thinking": round(sum(r["thinking_calls"] for r in arm) /
                                             max(1, sum(r["model_calls"] for r in arm)), 2),
            "lessons_received_by_later_agents": [r["lessons_received"] for r in later],
        }
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--agents", type=int, default=4)
    p.add_argument("--sequences", type=int, default=2)
    p.add_argument("--model", default=None)
    p.add_argument("--max-steps", type=int, default=30)
    p.add_argument("--out", default=None)
    a = p.parse_args()
    model = ChatModel(a.model)
    rows = []
    for _ in range(a.sequences):           # interleaved, so drift hits every arm alike
        for share, thinking in ARMS:
            rows += sequence(model, share, a.agents, a.max_steps, thinking)
    result = {"model": model.model, "task": TITLE, "arms": [f"lessons_{'on' if s else 'off'}+thinking_{t}"
                                                           for s, t in ARMS],
              "summary": summary(rows), "rows": rows}
    print(json.dumps(result["summary"], indent=1))
    if a.out:
        Path(a.out).write_text(json.dumps(result, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
