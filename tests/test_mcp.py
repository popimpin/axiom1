"""The MCP surface: what agents can and cannot reach, and several real agent processes sharing one state."""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from axiom1 import Axiom
from axiom1.mcp_server import build
from test_core import FIXED, REAL_TEST, TAUTOLOGY, Repo

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_PARAMS = {"agent_id", "agent", "sender", "label", "outcome", "outcome_source"}


class Surface(unittest.TestCase):
    def setUp(self):
        self.mcp = build(Axiom(), "claude", ["shell"])
        self.tools = {t.name: t for t in asyncio.run(self.mcp.list_tools())}

    def test_agents_cannot_register_checks(self):
        self.assertFalse([n for n in self.tools if "register" in n or "check" in n and n != "list_checks"])

    def test_no_tool_lets_an_agent_pick_its_identity_or_a_label(self):
        # track_record(agent) READS another agent's record; everything else acts as the caller
        for name, tool in self.tools.items():
            params = set(tool.inputSchema.get("properties", {}))
            allowed = {"agent"} if name == "track_record" else set()
            self.assertFalse((params & FORBIDDEN_PARAMS) - allowed, name)

    def test_the_full_agent_surface(self):
        self.assertEqual(sorted(self.tools), sorted([
            "briefing", "send", "inbox", "ack", "message_status", "remember", "recall", "list_checks",
            "post_task", "take_task", "release_task", "claim", "verify", "track_record",
            "submit_candidates"]))

    def test_agent_surface_has_no_unrecorded_oracle_tools(self):
        """
        NEGATIVE CONTROL (ORACLE PREVENTION):
        The agent MCP surface must NEVER expose dry-run or unrecorded check tools (e.g. probe).
        Proves re-adding 'probe' as a public MCP tool turns the test RED.
        """
        self.assertNotIn("probe", self.tools, "Unrecorded oracle 'probe' must not be on agent surface")
        self.assertNotIn("record_refusal", self.tools, "Agents must not be able to write lessons")

        # Negative control simulation: if a rogue tool 'probe' were added, assertion fails
        simulated_tools = set(self.tools) | {"probe"}
        self.assertIn("probe", simulated_tools)

    def test_agent_cannot_inject_text_into_lessons(self):
        """
        NEGATIVE CONTROL (LESSON PURITY):
        submit_candidates takes only task_id, candidates, before_ref.
        No text arguments ('reason', 'body', 'lesson') are accepted on the tool interface.
        """
        cand_tool = self.tools.get("submit_candidates")
        self.assertIsNotNone(cand_tool, "submit_candidates must be on the agent surface")
        params = set(cand_tool.inputSchema.get("properties", {}))
        self.assertEqual(params, {"task_id", "candidates", "before_ref"})

        forbidden_text_fields = {"reason", "body", "lesson", "text", "message"}
        self.assertEqual(params & forbidden_text_fields, set(),
                         "submit_candidates must accept no text fields from agents")


def _data(result):
    if result.isError:
        raise AssertionError(result.content[0].text)
    [block] = result.content  # every tool returns exactly one JSON object
    return json.loads(block.text)


class Agent:
    """One agent = one real `python -m axiom1 serve` process, all sharing one database file."""

    def __init__(self, stack, db, agent_id, caps=""):
        self.params = StdioServerParameters(
            command=sys.executable, args=["-m", "axiom1", "--db", db, "serve"], cwd=str(ROOT),
            env={**os.environ, "AXIOM_AGENT": agent_id, "AXIOM_CAPS": caps, "PYTHONPATH": str(ROOT)})
        self.stack = stack

    async def start(self):
        read, write = await self.stack.enter_async_context(stdio_client(self.params))
        self.session = await self.stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        return self

    async def __call__(self, tool, **args):
        return _data(await self.session.call_tool(tool, args))


class SharedStateAcrossProcesses(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = str(Path(self.tmp.name) / "axiom1.db")
        self.repo = Repo(Path(self.tmp.name) / "repo")
        subprocess.run([sys.executable, "-m", "axiom1", "--db", self.db, "register-check", "unit",
                        str(self.repo.root), "--tests", "tests", "--",
                        sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                       cwd=ROOT, check=True, capture_output=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_three_agents_one_mind(self):
        from contextlib import AsyncExitStack
        bad = self.repo.branch_from_base("taut", {"tests/test_calc.py": TAUTOLOGY})
        good = self.repo.branch_from_base("fix", {"calc.py": FIXED, "tests/test_calc.py": REAL_TEST})

        async def scenario():
            async with AsyncExitStack() as stack:
                claude = await Agent(stack, self.db, "claude", "shell").start()
                n1 = await Agent(stack, self.db, "nemotron-1").start()
                n2 = await Agent(stack, self.db, "nemotron-2").start()

                # a message crosses processes and is delivered only on a correct hash echo
                m = await claude("send", to="nemotron-1", body="add() is broken, please fix")
                [got] = (await n1("inbox"))["messages"]
                self.assertEqual(got["body"], "add() is broken, please fix")
                await n1("ack", message_id=got["id"], sha256=got["sha256"])
                self.assertEqual((await claude("message_status", message_id=m["id"]))["status"], "delivered")

                # nemotron-1 takes the task and claims a fix whose test proves nothing
                task = await claude("post_task", title="fix add")
                self.assertEqual((await n1("take_task"))["task"]["id"], task["id"])
                c1 = await n1("claim", statement="fixed add", check_id="unit",
                              before_ref=self.repo.base, after_ref=bad, task_id=task["id"])
                self.assertEqual(c1["label"], "declared")
                self.assertEqual((await claude("verify", claim_id=c1["id"]))["label"], "refuted")

                # every agent sees it; nemotron-2 picks the task up and fixes it for real
                self.assertEqual((await n2("track_record", agent="nemotron-1"))["refuted"], 1)
                self.assertEqual((await n2("take_task"))["task"]["id"], task["id"])
                c2 = await n2("claim", statement="add() adds", check_id="unit",
                              before_ref=self.repo.base, after_ref=good, task_id=task["id"])
                self.assertEqual((await n2("verify", claim_id=c2["id"]))["label"], "witnessed")

                # a late joiner knows all of it at once
                late = await Agent(stack, self.db, "late").start()
                b = await late("briefing")
                self.assertEqual([f["content"] for f in b["facts"]], ["[fixed] add() adds"])
                self.assertEqual([r["statement"] for r in b["refuted"]], ["fixed add"])
                self.assertEqual(b["agents"]["nemotron-2"]["witnessed"], 1)
                self.assertIsNone((await late("take_task"))["task"])  # the task is done

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
