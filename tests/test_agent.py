"""The agent runner, driven by a scripted model so the test is free and deterministic.

A live run against Nemotron on Nebius is examples/live_agent.py; this file tests the plumbing:
MCP tools reach the model, workspace tools stay inside the worktree, and a commit made in the
worktree is visible to the verifier.
"""
import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from axiom1 import Axiom
from axiom1.agent import Workspace, run_agent
from test_core import FIXED, REAL_TEST, TAUTOLOGY, Repo

ROOT = Path(__file__).resolve().parents[1]


def _last_result(messages, tool):
    ids = {c["id"] for m in messages if m["role"] == "assistant" for c in m.get("tool_calls", [])
           if c["function"]["name"] == tool}
    for m in reversed(messages):
        if m["role"] == "tool" and m["tool_call_id"] in ids:
            return json.loads(m["content"])
    raise AssertionError(f"{tool} was never called")


class ScriptedModel:
    """Plays a fixed sequence of tool calls, filling ids and shas from earlier results."""

    def __init__(self, test_body, code_body=None):
        self.test_body, self.code_body, self.step, self.seen_tools = test_body, code_body, 0, None

    def __call__(self, messages, tools):
        self.seen_tools = {t["function"]["name"] for t in tools}
        start_sha = messages[0]["content"].split("before_ref = ")[1].split(",")[0]
        # the harness took the task before the first call and put it in the opening message
        task_id = json.loads(messages[1]["content"].split(": ", 1)[1])["id"]
        plan = [("briefing", lambda: {}), ("list_checks", lambda: {}),
                ("read_file", lambda: {"path": "calc.py"})]
        if self.code_body:
            plan.append(("write_file", lambda: {"path": "calc.py", "content": self.code_body}))
        plan += [
            ("write_file", lambda: {"path": "tests/test_calc.py", "content": self.test_body}),
            ("commit", lambda: {"message": "fix add"}),
            ("claim", lambda: {"statement": "add() adds", "check_id": "unit", "before_ref": start_sha,
                               "after_ref": _last_result(messages, "commit")["sha"],
                               "task_id": task_id}),
            ("verify", lambda: {"claim_id": _last_result(messages, "claim")["id"]}),
        ]
        if self.step == len(plan):
            return {"role": "assistant", "content": "verdict: " + _last_result(messages, "verify")["label"]}
        name, args = plan[self.step]
        self.step += 1
        return {"role": "assistant", "content": "",
                "tool_calls": [{"id": f"c{self.step}", "type": "function",
                                "function": {"name": name, "arguments": json.dumps(args())}}]}


class Runner(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Repo(Path(self.tmp.name) / "repo")
        self.wt = Path(self.tmp.name) / "wt"
        self.repo.git("worktree", "add", "-q", "--detach", str(self.wt), self.repo.base)
        self.db = str(Path(self.tmp.name) / "axiom1.db")
        ax = Axiom(self.db)
        ax.register_check("unit", self.repo.root, [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                          ["tests"])
        ax.join("claude", ["shell"])
        ax.post_task("claude", "fix add")
        ax.db.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _run(self, model):
        return asyncio.run(run_agent("nemotron-1", self.wt, self.db, model, log=lambda *_: None))

    def test_real_fix_through_the_runner_is_witnessed(self):
        model = ScriptedModel(REAL_TEST, FIXED)
        messages = self._run(model)
        self.assertEqual(messages[-1]["content"], "verdict: witnessed")
        self.assertTrue({"briefing", "claim", "verify", "read_file", "commit"} <= model.seen_tools)
        self.assertNotIn("register_check", model.seen_tools)

    def test_tautology_through_the_runner_is_refuted(self):
        messages = self._run(ScriptedModel(TAUTOLOGY))
        self.assertEqual(messages[-1]["content"], "verdict: refuted")
        ax = Axiom(self.db)
        self.assertEqual(ax.track_record("nemotron-1")["refuted"], 1)
        ax.db.close()


class TextToolCalls(Runner):
    """Seen live: the model 'answered' with the text `<tool_call>` and the run ended without a claim."""

    def test_a_tool_call_written_as_text_is_not_a_final_answer(self):
        inner = ScriptedModel(REAL_TEST, FIXED)
        state = {"slipped": 0}

        def model(messages, tools):
            if state["slipped"] < 2:      # the first two replies are calls written as text
                state["slipped"] += 1
                return {"role": "assistant", "content": '<tool_call>\n{"name": "briefing", "arguments": {}}'}
            return inner(messages, tools)

        messages = self._run(model)
        self.assertEqual(messages[-1]["content"], "verdict: witnessed")
        nudges = [m for m in messages if m["role"] == "user" and "written as text" in m["content"]]
        self.assertEqual(len(nudges), 2)

    def test_a_real_final_answer_still_ends_the_run(self):
        messages = self._run(lambda m, t: {"role": "assistant", "content": "No task to do."})
        self.assertEqual(messages[-1]["content"], "No task to do.")


class HarnessDecidesWhenItIsDone(Runner):
    """Seen live: the agent 'thought out loud' in a message with no tool call and the run ended there."""

    def test_thinking_out_loud_does_not_end_an_unverified_run(self):
        inner = ScriptedModel(REAL_TEST, FIXED)
        state = {"narrated": False}

        def model(messages, tools):
            if not state["narrated"]:
                state["narrated"] = True
                return {"role": "assistant", "content": "We need to fix add. Let's look at calc.py first."}
            return inner(messages, tools)

        messages = self._run(model)
        self.assertEqual(messages[-1]["content"], "verdict: witnessed")
        nudges = [m for m in messages if m["role"] == "user" and "not finished" in m["content"]]
        self.assertEqual(len(nudges), 1)

    def test_handing_the_task_back_ends_the_run_at_once(self):
        state = {"step": 0}

        def model(messages, tools):
            state["step"] += 1
            if state["step"] == 1:
                task_id = json.loads(messages[1]["content"].split(": ", 1)[1])["id"]
                return {"role": "assistant", "content": "", "tool_calls": [{"id": "r", "type": "function", "function": {
                    "name": "release_task", "arguments": json.dumps({"task_id": task_id, "note": "stuck"})}}]}
            return {"role": "assistant", "content": "I could not finish; I handed it back."}

        messages = self._run(model)
        self.assertEqual(state["step"], 2)                       # no nudges after an honest hand-back
        self.assertFalse([m for m in messages if m["role"] == "user" and "not finished" in m["content"]])

    def test_a_model_that_only_narrates_is_stopped_and_its_task_released(self):
        messages = self._run(lambda m, t: {"role": "assistant", "content": "Still thinking about it."})
        nudges = [m for m in messages if m["role"] == "user" and "not finished" in m["content"]]
        self.assertEqual(len(nudges), 5)
        ax = Axiom(self.db)
        self.assertEqual(ax.track_record("nemotron-1")["released"], 1)
        ax.db.close()


class WorkspaceConfinement(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Repo(Path(self.tmp.name) / "repo")
        self.ws = Workspace(self.repo.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_cannot_escape_the_worktree(self):
        for bad in ("../outside.txt", "/etc/passwd", "C:/Windows/win.ini", "sub/../../x"):
            with self.assertRaises(ValueError, msg=bad):
                self.ws.write_file(bad, "x")

    def test_cannot_touch_git_internals(self):
        with self.assertRaises(ValueError):
            self.ws.write_file(".git/hooks/pre-commit", "x")
        with self.assertRaises(ValueError):
            self.ws.read_file(".git/config")

    def test_a_file_in_the_way_is_named_and_can_be_removed(self):
        # the trap seen live: write_file("tests", "") makes a FILE, then tests/test_x.py can never be created
        self.ws.write_file("tests", "")
        with self.assertRaises(ValueError) as e:
            self.ws.write_file("tests/test_add.py", "x = 1\n")
        self.assertIn("'tests' is a file", str(e.exception))
        self.ws.delete_file("tests")
        self.ws.write_file("tests/test_add.py", "x = 1\n")
        self.assertTrue((self.repo.root / "tests" / "test_add.py").is_file())

    def test_delete_file_is_confined(self):
        for bad in ("../outside.txt", ".git/config"):
            with self.assertRaises(ValueError, msg=bad):
                self.ws.delete_file(bad)
        with self.assertRaises(ValueError):
            self.ws.delete_file(".")

    def test_inside_is_fine(self):
        self.ws.write_file("pkg/new.py", "x = 1\n")
        self.assertEqual(self.ws.read_file("pkg/new.py")["content"], "x = 1\n")


if __name__ == "__main__":
    unittest.main()
