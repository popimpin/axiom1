"""One live Nemotron agent on a real bug.

Builds a throwaway repo whose add() subtracts, registers a check, posts the task, and lets a
Nemotron model on Nebius Token Factory take it. Needs NEBIUS_API_KEY in the environment.

    python examples/live_agent.py [--model nvidia/Nemotron-3_5-Lightning]
"""
import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from axiom1 import Axiom  # noqa: E402
from axiom1.agent import ChatModel, run_agent  # noqa: E402

BUGGY = "def add(a, b):\n    return a - b\n"


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=operator", "-c", "user.email=op@axiom1.invalid",
                           *args], capture_output=True, text=True, check=True).stdout.strip()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", default=None)
    p.add_argument("--max-steps", type=int, default=30)
    a = p.parse_args()

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        repo, wt, db = Path(tmp) / "repo", Path(tmp) / "wt", str(Path(tmp) / "axiom1.db")
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        (repo / "calc.py").write_text(BUGGY, encoding="utf-8")
        (repo / "README.md").write_text("A tiny calculator. add(a, b) should return a + b.\n", encoding="utf-8")
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "calculator")
        git(repo, "worktree", "add", "-q", "--detach", str(wt), "main")

        ax = Axiom(db)
        ax.register_check("unit", repo, [sys.executable, "-m", "unittest", "discover", "-s", "tests"], ["tests/"])
        ax.join("operator", ["shell"])
        ax.post_task("operator", "add(2, 3) returns -1; it should return 5. Fix calc.py.")
        ax.db.close()

        model = ChatModel(a.model)
        print(f"model: {model.model}\n")
        t = time.time()
        asyncio.run(run_agent("nemotron-1", wt, db, model, max_steps=a.max_steps))
        ax = Axiom(db)
        print(f"\n{time.time() - t:.1f}s  track record: {json.dumps(ax.track_record('nemotron-1'))}")
        for c in ax.db.execute("SELECT label, reason FROM claims"):
            print(f"claim: {c['label']} - {c['reason']}")
        ax.db.close()


if __name__ == "__main__":
    main()
