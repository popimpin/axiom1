"""A model-driven agent that joins Axiom-1 the same way any other agent does: over MCP.

The runner gives a chat model two kinds of tools:

  * every tool the Axiom-1 MCP server offers (briefing, take_task, claim, verify, ...);
  * four workspace tools (list_files, read_file, write_file, commit) confined to one git
    worktree of the repo the operator registered, so the agent's commits are visible to
    the verifier.

The model is any OpenAI-compatible chat endpoint. By default that is Nemotron on Nebius
Token Factory, configured from the environment:

    NEBIUS_API_KEY    required for the default endpoint
    NEBIUS_BASE_URL   default https://api.tokenfactory.nebius.com/v1
    AXIOM_MODEL       default nvidia/Nemotron-3_5-Lightning
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"
DEFAULT_MODEL = "nvidia/Nemotron-3_5-Lightning"

SYSTEM_PROMPT = """You are {agent_id}, one agent in a collective that shares state through Axiom-1.

Rules of the collective:
- Anything you say is stored as `declared`. Only the server's `verify` makes a claim `witnessed`.
- `verify` runs a human-registered test command twice: on the code BEFORE your fix with your tests
  laid over it (it must FAIL), then on your fix (it must PASS). A test that passes without your fix
  proves nothing and gets your claim refuted.
- Every agent can see your track record.

How to work:
1. Call `briefing`, then `take_task`. If there is no task, stop.
2. Call `list_checks`. It tells you the exact test COMMAND that will judge you (write tests that
   command actually runs) and the test paths (one ending in / is a directory: create files inside it).
3. Use list_files / read_file to understand the code. Fix it with write_file, and add or update a
   test under the check's test path that FAILS on the old code and PASSES on yours.
4. Call `commit`, then `claim` with before_ref = {start_sha}, after_ref = the sha `commit` returned,
   the check id, and the task id.
5. Call `verify` on your claim, then give a one-line final answer with the verdict.
Use tools for everything; do not describe code you have not written with write_file."""


class Workspace:
    """File access confined to one git worktree. The model cannot reach outside it, or into .git."""

    def __init__(self, root):
        self.root = Path(root).resolve()

    def _path(self, rel):
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f"{rel!r} is outside the workspace")
        if ".git" in p.relative_to(self.root).parts:
            raise ValueError("the .git directory is off limits")
        return p

    def _git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, check=True).stdout.strip()

    def head(self):
        return self._git("rev-parse", "HEAD")

    def list_files(self) -> dict:
        return {"files": self._git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()}

    def read_file(self, path: str) -> dict:
        return {"path": path, "content": self._path(path).read_text(encoding="utf-8")}

    def write_file(self, path: str, content: str) -> dict:
        p = self._path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"path": path, "bytes": len(content.encode("utf-8"))}

    def commit(self, message: str) -> dict:
        self._git("add", "-A")
        self._git("-c", "user.name=axiom1-agent", "-c", "user.email=agent@axiom1.invalid",
                  "commit", "-q", "--allow-empty", "-m", message)
        return {"sha": self.head()}

    TOOLS = [
        {"name": "list_files", "description": "List the files in your workspace.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "read_file", "description": "Read a file from your workspace.",
         "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
        {"name": "write_file", "description": "Create or overwrite a file in your workspace.",
         "parameters": {"type": "object", "properties": {"path": {"type": "string"},
                                                         "content": {"type": "string"}},
                        "required": ["path", "content"]}},
        {"name": "commit", "description": "Commit everything in your workspace. Returns the commit sha.",
         "parameters": {"type": "object", "properties": {"message": {"type": "string"}},
                        "required": ["message"]}},
    ]


class ChatModel:
    """An OpenAI-compatible /chat/completions endpoint (Nebius Token Factory by default)."""

    def __init__(self, model=None, base_url=None, api_key=None, temperature=0.0, max_tokens=4096):
        self.model = model or os.environ.get("AXIOM_MODEL", DEFAULT_MODEL)
        self.base_url = (base_url or os.environ.get("NEBIUS_BASE_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.api_key = api_key or os.environ.get("NEBIUS_API_KEY")
        if not self.api_key:
            raise SystemExit("set NEBIUS_API_KEY")
        self.temperature, self.max_tokens = temperature, max_tokens

    def __call__(self, messages, tools):
        body = {"model": self.model, "messages": messages, "tools": tools,
                "temperature": self.temperature, "max_tokens": self.max_tokens}
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.api_key}",
                                              "Content-Type": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    return json.load(r)["choices"][0]["message"]
            except urllib.error.HTTPError as e:
                if e.code < 500 and e.code != 429 or attempt == 2:
                    raise RuntimeError(f"model endpoint returned HTTP {e.code}: "
                                       f"{e.read()[:300].decode(errors='replace')}") from None
            time.sleep(2 ** attempt)


def _openai_tool(name, description, parameters):
    return {"type": "function", "function": {"name": name, "description": description or "",
                                             "parameters": parameters or {"type": "object", "properties": {}}}}


async def run_agent(agent_id, workspace, db, model, caps=(), max_steps=30, log=print, hub_url=None,
                    token=None):
    """Run one agent until it gives a final answer or runs out of steps. Returns the transcript.

    With `hub_url` the agent connects to a hub over HTTP and is whoever `token` says it is; `db` and
    `caps` are ignored (the hub owns the database, the operator set the caps). Without it, the
    runner starts a stdio server on `db` for local development."""
    ws = Workspace(workspace)
    start_sha = ws.head()
    async with AsyncExitStack() as stack:
        if hub_url:
            from mcp.client.streamable_http import streamablehttp_client
            read, write, _ = await stack.enter_async_context(
                streamablehttp_client(hub_url, headers={"Authorization": f"Bearer {token}"}, timeout=60,
                                      sse_read_timeout=900))
        else:
            server = StdioServerParameters(
                command=sys.executable, args=["-m", "axiom1", "--db", str(db), "serve"],
                env={**os.environ, "AXIOM_AGENT": agent_id, "AXIOM_CAPS": ",".join(caps),
                     "PYTHONPATH": os.pathsep.join([str(Path(__file__).resolve().parents[1]),
                                                    os.environ.get("PYTHONPATH", "")])})
            read, write = await stack.enter_async_context(stdio_client(server))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        mcp_tools = (await session.list_tools()).tools
        tools = [_openai_tool(t.name, t.description, t.inputSchema) for t in mcp_tools]
        tools += [_openai_tool(t["name"], t["description"], t["parameters"]) for t in Workspace.TOOLS]
        mcp_names = {t.name for t in mcp_tools}

        messages = [{"role": "system", "content": SYSTEM_PROMPT.format(agent_id=agent_id, start_sha=start_sha)},
                    {"role": "user", "content": "Begin."}]
        for step in range(max_steps):
            reply = model(messages, tools)
            calls = reply.get("tool_calls") or []
            messages.append({"role": "assistant", "content": reply.get("content") or "",
                             **({"tool_calls": calls} if calls else {})})
            if not calls:
                log(f"[{agent_id}] final: {(reply.get('content') or '').strip()[:300]}")
                return messages
            for call in calls:
                name = call["function"]["name"]
                try:
                    args = json.loads(call["function"].get("arguments") or "{}")
                    if name in mcp_names:
                        res = await session.call_tool(name, args)
                        text = res.content[0].text if res.content else "{}"
                        if res.isError:
                            text = json.dumps({"error": text})
                    elif name in {t["name"] for t in Workspace.TOOLS}:
                        text = json.dumps(getattr(ws, name)(**args))
                    else:
                        text = json.dumps({"error": f"no tool named {name!r}"})
                except Exception as e:  # a bad call is reported back to the model, not fatal
                    text = json.dumps({"error": f"{type(e).__name__}: {e}"})
                log(f"[{agent_id}] {name}({_short(call['function'].get('arguments'))}) -> {_short(text)}")
                messages.append({"role": "tool", "tool_call_id": call["id"], "content": text})
        log(f"[{agent_id}] stopped after {max_steps} steps")
        return messages


def _short(s, n=160):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n] + "..."
