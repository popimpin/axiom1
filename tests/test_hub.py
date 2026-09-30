"""The hub: one process owns the database, agents reach it over HTTP with a token each.

Identity comes from the token on every call. These tests are agents trying to be someone else.
"""
import asyncio
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from contextlib import AsyncExitStack
from pathlib import Path

import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from axiom1 import Axiom
from axiom1.agent import run_agent
from axiom1.mcp_server import hub_app
from test_agent import ScriptedModel
from test_core import FIXED, REAL_TEST, Repo


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Hub:
    def __init__(self, ax):
        self.port = _free_port()
        self.url = f"http://127.0.0.1:{self.port}/mcp"
        self.server = uvicorn.Server(uvicorn.Config(hub_app(ax), host="127.0.0.1", port=self.port,
                                                    log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        deadline = time.time() + 15
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("hub did not start")
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(10)


async def _connect(stack, url, token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    read, write, _ = await stack.enter_async_context(streamablehttp_client(url, headers=headers))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()

    async def call(tool, **args):
        res = await session.call_tool(tool, args)
        if res.isError:
            raise PermissionError(res.content[0].text)
        return json.loads(res.content[0].text)
    return call


def _post_status(url, token):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).encode()
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


class HubIdentity(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = str(Path(self.tmp.name) / "axiom1.db")
        self.ax = Axiom(self.db_path)
        self.claude = self.ax.issue_token("claude", ["shell"])
        self.nemo = self.ax.issue_token("nemotron-1")

    def tearDown(self):
        self.ax.db.close()
        self.tmp.cleanup()

    def test_no_token_or_a_wrong_one_never_reaches_a_tool(self):
        with Hub(self.ax) as hub:
            self.assertEqual(_post_status(hub.url, None), 401)
            self.assertEqual(_post_status(hub.url, "axm_not-a-real-token"), 401)
            self.assertEqual(_post_status(hub.url, self.claude[:-1]), 401)

    def test_every_call_is_the_tokens_agent(self):
        async def scenario(url):
            async with AsyncExitStack() as stack:
                claude = await _connect(stack, url, self.claude)
                nemo = await _connect(stack, url, self.nemo)
                self.assertEqual((await claude("briefing"))["you"], "claude")
                self.assertEqual((await nemo("briefing"))["you"], "nemotron-1")
                m = await claude("send", to="nemotron-1", body="hello")
                # claude cannot ack a message addressed to nemotron-1, even with the right hash
                with self.assertRaises(PermissionError):
                    await claude("ack", message_id=m["id"], sha256=m["sha256"])
                [got] = (await nemo("inbox"))["messages"]
                self.assertEqual(got["sender"], "claude")
                await nemo("ack", message_id=got["id"], sha256=got["sha256"])
                self.assertEqual((await claude("message_status", message_id=m["id"]))["status"], "delivered")
        with Hub(self.ax) as hub:
            asyncio.run(scenario(hub.url))

    def test_revoked_and_reissued_tokens_stop_working(self):
        old = self.nemo
        new = self.ax.issue_token("nemotron-1")
        with Hub(self.ax) as hub:
            self.assertEqual(_post_status(hub.url, old), 401)
            self.assertNotEqual(_post_status(hub.url, new), 401)
            self.ax.revoke_token("nemotron-1")
            self.assertEqual(_post_status(hub.url, new), 401)

    def test_tokens_are_stored_only_as_hashes(self):
        self.ax.db.execute("PRAGMA wal_checkpoint(FULL)")
        raw = b"".join(p.read_bytes() for p in Path(self.tmp.name).glob("axiom1.db*"))
        for token in (self.claude, self.nemo):
            self.assertNotIn(token.encode(), raw)


class HubAgentRunner(unittest.TestCase):
    """The model-driven runner, connected to a hub instead of starting its own server."""

    def test_runner_through_the_hub(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            repo = Repo(Path(tmp) / "repo")
            wt = Path(tmp) / "wt"
            repo.git("worktree", "add", "-q", "--detach", str(wt), repo.base)
            ax = Axiom(str(Path(tmp) / "axiom1.db"))
            ax.register_check("unit", repo.root, [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                              ["tests/"])
            ax.issue_token("operator", ["shell"])
            ax.post_task("operator", "fix add")
            token = ax.issue_token("nemotron-1")
            with Hub(ax) as hub:
                messages = asyncio.run(run_agent("nemotron-1", wt, None, ScriptedModel(REAL_TEST, FIXED),
                                                 log=lambda *_: None, hub_url=hub.url, token=token))
            self.assertEqual(messages[-1]["content"], "verdict: witnessed")
            self.assertEqual(ax.track_record("nemotron-1")["witnessed"], 1)
            ax.db.close()


if __name__ == "__main__":
    unittest.main()
