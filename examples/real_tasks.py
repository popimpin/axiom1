"""Real tasks: bugs we actually hit while building Axiom-1, given to agents the way we met them.

Each task is a bug from this repository's own history:

  * base     the code as it was before the fix. Four bugs reached a commit, so their base is that
             commit. Four were fixed before committing; their base is the fixed code with the bug put
             back, and they are labelled "reconstructed".
  * title    the symptom as it was observed, worded as a bug report. No hint at the cause.
  * gold     the real fix (files from the fixing commit). Used ONLY to validate the task.
  * holdout  the regression test, hidden from the agent. It must FAIL on base and PASS on gold, or the
             task is not used: a task that does not separate the two measures nothing.

Each task repo is a fresh repository with a single commit, so no fix can be read from its history.
Checks run in the `axiom1-tasks:py313` image (examples/tasks/Dockerfile) with no network.

    python examples/real_tasks.py validate          # every holdout fails on base, passes on gold
    python examples/real_tasks.py run --agents 3    # agents work the tasks (needs NEBIUS_API_KEY)
"""
import argparse
import asyncio
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from axiom1 import Axiom  # noqa: E402
from axiom1.sandbox import DockerSandbox  # noqa: E402

IMAGE = "axiom1-tasks:py313"
TEST_DIR = "tests/task/"
CHECK = ["python", "-m", "unittest", "discover", "-s", "tests/task"]

# shared by several holdouts: a tiny git repo with one commit
_REPO = r'''
import subprocess
from pathlib import Path


def tiny_repo(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    run = lambda *a: subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                    check=True, capture_output=True)
    run("init", "-q", "-b", "main")
    (root / "a.txt").write_text("x\n")
    run("add", "-A")
    run("commit", "-q", "-m", "a")
    return root
'''

TASKS = [
    {
        "id": "file-in-the-way",
        "provenance": "committed: bug in ef4e8ec, fixed in 6939611",
        "base": "ef4e8ec", "plant": [],
        "gold": "6939611", "gold_files": ["axiom1/agent.py"],
        "title": "An agent called write_file(\"tests\", \"\"), which created an empty FILE named tests. Every later "
                 "write_file to tests/test_add.py then failed with a bare FileExistsError, and the agent retried until "
                 "it ran out of steps. Make the workspace tools let an agent recover from this.",
        "holdout": _REPO + r'''
import tempfile, unittest
from axiom1.agent import Workspace


class FileInTheWay(unittest.TestCase):
    def test_an_agent_can_recover_from_a_file_where_a_directory_belongs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = tiny_repo(Path(tmp) / "r")
            ws = Workspace(root)
            ws.write_file("tests", "")
            names = [t["name"] for t in Workspace.TOOLS]
            remover = next((n for n in names if "delete" in n or "remove" in n), None)
            self.assertIsNotNone(remover, f"no workspace tool removes a file: {names}")
            getattr(ws, remover)(path="tests")
            ws.write_file("tests/test_x.py", "x = 1\n")
            self.assertTrue((root / "tests" / "test_x.py").is_file())
''',
    },
    {
        "id": "unicode-log-crash",
        "provenance": "committed: bug in ef4e8ec, fixed in 6939611",
        "base": "ef4e8ec", "plant": [],
        "gold": "6939611", "gold_files": ["axiom1/agent.py"],
        "title": "run_agent crashed at the end of a run on a Windows console: UnicodeEncodeError: 'charmap' codec can't "
                 "encode character '\\u2011' in position 147, raised while logging the model's final answer. A run "
                 "must not die because of how its log is displayed.",
        "holdout": _REPO + r'''
import asyncio, tempfile, unittest
from axiom1.agent import run_agent


class UnicodeFinal(unittest.TestCase):
    def test_a_final_answer_the_console_cannot_encode_does_not_crash_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = tiny_repo(Path(tmp) / "r")
            answer = "done ‑ all good"

            def model(messages, tools):
                return {"role": "assistant", "content": answer}

            def cp1252_console(line):
                line.encode("cp1252")      # what a Windows console does; raises on U+2011

            messages = asyncio.run(run_agent("a", root, str(Path(tmp) / "a.db"), model, log=cp1252_console))
            self.assertEqual(messages[-1]["content"], answer)
''',
    },
    {
        "id": "tool-call-as-text",
        "provenance": "committed: bug in 6939611, fixed in 4aa6220",
        "base": "6939611", "plant": [],
        "gold": "4aa6220", "gold_files": ["axiom1/agent.py"],
        "title": "Live run: an agent stopped after 13 steps without ever making a claim. Its logged final answer was "
                 "just `<tool_call>`. The next agent then had nothing to learn from.",
        "holdout": _REPO + r'''
import asyncio, tempfile, unittest
from axiom1.agent import run_agent


class TextToolCall(unittest.TestCase):
    def test_a_tool_call_written_as_text_does_not_end_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = tiny_repo(Path(tmp) / "r")
            calls = []

            def model(messages, tools):
                calls.append(1)
                if len(calls) == 1:
                    return {"role": "assistant", "content": '<tool_call>\n{"name": "briefing", "arguments": {}}'}
                return {"role": "assistant", "content": "done"}

            asyncio.run(run_agent("a", root, str(Path(tmp) / "a.db"), model, log=lambda *_: None))
            self.assertEqual(len(calls), 2, "the run ended on a tool call written as text")
''',
    },
    {
        "id": "dial-off-after-refutation",
        "provenance": "committed: bug in e79c18f, fixed in 9eaee9c",
        "base": "e79c18f", "plant": [],
        "gold": "9eaee9c", "gold_files": ["axiom1/agent.py"],
        "title": "With thinking='auto', a per-call trace of a live run read: `f:claim f:verify(refuted) T:write_file "
                 "T:commit T:claim T:take_task f:write_file f:commit ...`. Thinking switched back OFF straight after "
                 "the agent re-took its task, although that run had just been refuted.",
        "holdout": r'''
import json, unittest
from axiom1.agent import _observe


class StickyRefutation(unittest.TestCase):
    def test_a_re_take_after_a_refutation_keeps_thinking_on(self):
        state = {"task": None, "witnessed": False, "think": True}
        skill = json.dumps({"task": {"id": "t1", "lessons": [{"kind": "skill"}]}})
        quiet = lambda *_: None
        _observe("take_task", skill, state, "auto", quiet, "a")
        self.assertFalse(state["think"])
        _observe("verify", json.dumps({"label": "refuted"}), state, "auto", quiet, "a")
        self.assertTrue(state["think"])
        _observe("take_task", skill, state, "auto", quiet, "a")
        self.assertTrue(state["think"])
''',
    },
    {
        "id": "tamper-verdict-crash",
        "provenance": "reconstructed: hit and fixed before committing 9f35fe6",
        "base": "9eaee9c",
        "plant": [("axiom1/verifier.py", '        return "refuted", reason, evidence, private\n',
                   '        return ("refuted", reason), evidence, private\n')],
        "gold": "9eaee9c", "gold_files": ["axiom1/verifier.py"],
        "title": "verify crashes with `ValueError: not enough values to unpack (expected 4, got 3)` when the claimed "
                 "fix ships a local `unittest/` package that shadows the standard library's. That fix should be "
                 "refuted as tampering, not crash the server.",
        "holdout": r'''
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, "tests")
from test_core import REAL_TEST, SHADOW_UNITTEST, Repo
from axiom1 import Axiom


class TamperVerdict(unittest.TestCase):
    def test_a_fix_that_shadows_the_test_runner_is_refuted_not_a_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp) / "repo")
            ax = Axiom()
            ax.register_check("unit", repo.root, [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                              ["tests/"])
            ax.join("a")
            fix = repo.branch_from_base("shadow", {**SHADOW_UNITTEST, "tests/test_calc.py": REAL_TEST})
            v = ax.verify(ax.claim("a", "fixed", "unit", repo.base, fix)["id"])
            self.assertEqual(v["label"], "refuted")
            self.assertIn("tamper", v["reason"])
''',
    },
    {
        "id": "event-kind-clash",
        "provenance": "reconstructed: hit and fixed before committing d6a3d1f",
        "base": "9eaee9c",
        "plant": [("axiom1/core.py", "statement=statement, claim_kind=kind, anchored=anchored)",
                   "statement=statement, kind=kind, anchored=anchored)")],
        "gold": "9eaee9c", "gold_files": ["axiom1/core.py"],
        "title": "Every claim fails with `TypeError: Axiom._event() got multiple values for argument 'kind'`.",
        "holdout": r'''
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, "tests")
from test_core import FIXED, REAL_TEST, Repo
from axiom1 import Axiom


class ClaimRecorded(unittest.TestCase):
    def test_a_claim_is_recorded_with_its_kind(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp) / "repo")
            ax = Axiom()
            ax.register_check("unit", repo.root, [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                              ["tests/"])
            ax.join("a")
            fix = repo.branch_from_base("fix", {"calc.py": FIXED, "tests/test_calc.py": REAL_TEST})
            self.assertEqual(ax.claim("a", "add() adds", "unit", repo.base, fix)["label"], "declared")
            [event] = [e for e in ax.events() if e["kind"] == "claim"]
            self.assertIn("fix", event["detail"])
''',
    },
    {
        "id": "calibration-accuses-honest-fix",
        "provenance": "reconstructed: hit and fixed before committing 9f35fe6",
        "base": "9eaee9c",
        "plant": [("axiom1/verifier.py", '_canary("assertEqual", passing=True)', '_canary("assertEqual")'),
                  ("axiom1/verifier.py", "        return sandbox.run(ctrl, argv).returncode == 0\n",
                   "        return sandbox.run(ctrl, argv).returncode != 0\n")],
        "gold": "9eaee9c", "gold_files": ["axiom1/verifier.py"],
        "title": "An honest fix was refuted as tampering. Its check's command was "
                 "`python -m unittest discover -s tests -p test_calc.py`, which runs only one test file.",
        "holdout": r'''
import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, "tests")
from test_core import FIXED, REAL_TEST, Repo
from axiom1 import Axiom


class HonestOneFileFix(unittest.TestCase):
    def test_a_command_that_never_runs_canaries_does_not_accuse_an_honest_fix(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp) / "repo")
            ax = Axiom()
            ax.register_check("one", repo.root,
                              [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_calc.py"],
                              ["tests/"])
            ax.join("a")
            fix = repo.branch_from_base("fix", {"calc.py": FIXED, "tests/test_calc.py": REAL_TEST})
            v = ax.verify(ax.claim("a", "add() adds", "one", repo.base, fix)["id"])
            self.assertEqual(v["label"], "witnessed", v.get("reason"))
''',
    },
    {
        "id": "cli-swallows-tests-flag",
        "provenance": "reconstructed: hit and fixed before committing 95f8b6d",
        "base": "9eaee9c",
        "plant": [("axiom1/__main__.py",
                   "    argv = list(sys.argv[1:] if argv is None else argv)\n"
                   "    # everything after the first \"--\" is the test command, passed through untouched\n"
                   "    command = argv[argv.index(\"--\") + 1:] if \"--\" in argv else []\n"
                   "    argv = argv[:argv.index(\"--\")] if \"--\" in argv else argv\n"
                   "    a = p.parse_args(argv)\n",
                   "    rc.add_argument(\"command\", nargs=argparse.REMAINDER, help=\"after --, the test command\")\n"
                   "    a = p.parse_args(argv)\n"
                   "    command = getattr(a, \"command\", None) or []\n"
                   "    command = command[1:] if command[:1] == [\"--\"] else command\n")],
        "gold": "9eaee9c", "gold_files": ["axiom1/__main__.py"],
        "title": "`python -m axiom1 register-check unit ./repo --tests tests/ -- python -m unittest` fails with "
                 "`error: the following arguments are required: --tests`, although --tests is right there.",
        "holdout": r'''
import subprocess, sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, "tests")
from test_core import Repo
from axiom1 import Axiom


class RegisterCheckCli(unittest.TestCase):
    def test_register_check_takes_the_command_after_a_double_dash(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Repo(Path(tmp) / "repo")
            db = str(Path(tmp) / "a.db")
            r = subprocess.run([sys.executable, "-m", "axiom1", "--db", db, "register-check", "unit", str(repo.root),
                                "--tests", "tests/", "--", sys.executable, "-m", "unittest"],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            ax = Axiom(db)
            [check] = ax.list_checks()
            self.assertIn("unittest", check["command"])
            ax.db.close()
''',
    },
]


def git(repo, *args, **kw):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=operator", "-c", "user.email=op@axiom1.invalid",
                           *args], capture_output=True, check=True, **kw)


def export(sha, dest):
    data = git(ROOT, "archive", "--format=tar", sha).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")


def build_tree(task, dest, gold=False):
    """The task's code as an agent sees it (base), or with the real fix applied (gold)."""
    dest = Path(dest)
    export(task["base"], dest)
    for rel, old, new in task["plant"]:
        p = dest / rel
        text = p.read_text(encoding="utf-8")
        if text.count(old) != 1:
            raise SystemExit(f"{task['id']}: plant text found {text.count(old)}x in {rel} (need exactly 1)")
        p.write_text(text.replace(old, new), encoding="utf-8")
    if gold:
        for rel in task["gold_files"]:
            (dest / rel).write_bytes(git(ROOT, "show", f"{task['gold']}:{rel}").stdout)
    # agents' tests go here; the check runs only this directory
    (dest / TEST_DIR).mkdir(parents=True, exist_ok=True)
    (dest / TEST_DIR / "README.md").write_text("Tests for this task go in this directory.\n", encoding="utf-8")
    for junk in ("docs/measurements",):
        shutil.rmtree(dest / junk, ignore_errors=False) if (dest / junk).exists() else None
    return dest


def run_holdout(task, tree):
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "w"
        shutil.copytree(tree, work)
        (work / TEST_DIR / f"test_holdout_{task['id'].replace('-', '_')}.py").write_text(task["holdout"], encoding="utf-8")
        return DockerSandbox(IMAGE).run(str(work), CHECK)


def validate():
    ok = True
    for task in TASKS:
        with tempfile.TemporaryDirectory() as tmp:
            base = run_holdout(task, build_tree(task, Path(tmp) / "base"))
            gold = run_holdout(task, build_tree(task, Path(tmp) / "gold", gold=True))
        good = base.returncode != 0 and gold.returncode == 0
        ok &= good
        print(f"{'OK  ' if good else 'BAD '} {task['id']:32s} base exit {base.returncode:>3}  gold exit {gold.returncode:>3}"
              f"  [{task['provenance']}]")
        if not good:
            print("   base:", " ".join(base.output.split())[-300:])
            print("   gold:", " ".join(gold.output.split())[-300:])
    return ok


def make_repo(task, tmp):
    """A fresh repository, one commit, so no fix can be read from history."""
    repo = Path(tmp) / "repo"
    build_tree(task, repo)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "current code")
    holdout = Path(tmp) / "holdout"
    holdout.mkdir()
    (holdout / f"test_holdout_{task['id'].replace('-', '_')}.py").write_text(task["holdout"], encoding="utf-8")
    return repo, holdout


def run(n_agents, max_steps, model_name, thinking, share, only):
    from axiom1.agent import ChatModel, run_agent
    import os
    os.environ["AXIOM_SHARE_LESSONS"] = "1" if share else "0"
    model = ChatModel(model_name)
    rows = []
    for task in TASKS:
        if only and task["id"] not in only:
            continue
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            repo, holdout = make_repo(task, tmp)
            db = str(Path(tmp) / "axiom1.db")
            ax = Axiom(db, share_lessons=share)
            ax.register_check("task", repo, CHECK, [TEST_DIR], sandbox="docker", image=IMAGE, holdout=str(holdout))
            ax.join("operator", ["shell"])
            ax.db.close()
            for i in range(n_agents):
                ax = Axiom(db, share_lessons=share)
                if not ax.db.execute("SELECT 1 FROM tasks WHERE status='open'").fetchone():
                    ax.post_task("operator", task["title"])
                ax.db.close()
                wt = Path(tmp) / f"wt{i}"
                git(repo, "worktree", "add", "-q", "--detach", str(wt), "main")
                before = dict(model.usage)
                t0 = time.time()
                messages = asyncio.run(run_agent(f"agent{i}", wt, db, model, max_steps=max_steps, log=lambda *_: None,
                                                 shell=DockerSandbox(IMAGE), thinking=thinking))
                used = {k: model.usage[k] - before[k] for k in model.usage}
                ax = Axiom(db)
                claims = [("witnessed" if r["label"] == "witnessed" and r["anchored"] else r["label"]) for r in
                          ax.db.execute("SELECT label, anchored FROM claims WHERE agent=? ORDER BY made_at", (f"agent{i}",))]
                reasons = [r["reason"] for r in ax.db.execute(
                    "SELECT reason FROM claims WHERE agent=? AND reason IS NOT NULL ORDER BY made_at", (f"agent{i}",))]
                ax.db.close()
                opening = next((m["content"] for m in messages if m["role"] == "user"), "")
                received = 0
                if opening.startswith("Your task (leased to you): "):
                    received = len(json.loads(opening.split(": ", 1)[1]).get("lessons", []))
                row = {"task": task["id"], "position": i, "witnessed": "witnessed" in claims, "claims": claims,
                       "reasons": reasons, "lessons_received": received, "seconds": round(time.time() - t0, 1),
                       "model_calls": used["calls"], "thinking_calls": used["thinking_calls"],
                       "prompt_tokens": used["prompt_tokens"], "completion_tokens": used["completion_tokens"]}
                rows.append(row)
                print(json.dumps(row), flush=True)
    return {"model": model.model, "thinking": thinking, "lessons": share, "agents_per_task": n_agents, "rows": rows}


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate")
    r = sub.add_parser("run")
    r.add_argument("--agents", type=int, default=3)
    r.add_argument("--max-steps", type=int, default=40)
    r.add_argument("--model", default=None)
    r.add_argument("--thinking", default="auto", choices=["auto", "on", "off"])
    r.add_argument("--no-lessons", action="store_true")
    r.add_argument("--only", nargs="*")
    r.add_argument("--out", default=None)
    a = p.parse_args()
    if a.cmd == "validate":
        sys.exit(0 if validate() else 1)
    result = run(a.agents, a.max_steps, a.model, a.thinking, not a.no_lessons, a.only)
    if a.out:
        Path(a.out).write_text(json.dumps(result, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
