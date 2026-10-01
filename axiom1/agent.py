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
import textwrap
import time
import urllib.error
import urllib.request
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from . import engines
from .verifier import ENGINES_DIR, install_engines, process_argv

DEFAULT_BASE_URL = "https://api.tokenfactory.nebius.com/v1"
DEFAULT_MODEL = "nvidia/Nemotron-3_5-Lightning"

SYSTEM_PROMPT = """You are {agent_id}, one agent in a collective that shares state through Axiom-1.

Rules of the collective:
- Anything you say is stored as `declared`. Only the server's `verify` makes a claim `witnessed`.
- `verify` runs a check a human registered. You cannot see or change it, and its verdict is final.
- Every agent can see your track record.

How to work:
1. Your task is already leased to you: it is in the first message, with its id and any `lessons`. Read
   the lessons first: they are the server's own record of earlier attempts at this task, what was
   tried, why it was refuted, and what worked. Do not repeat a refuted approach.
2. Call `list_checks`. Its `claim_kind` says what kind of job this is:
   - "deliver": produce the files the task asks for, by writing process.py: a Python program (standard
     library only) that reads the original files and writes the requested ones. Put the values that
     would change between similar jobs (a date, a file name) in entry.json and read them from there.
     Run it, and commit process.py, entry.json and its output. The server re-runs process.py on the
     original files: a process that reproduces the result is locked in, so this kind of job never has to
     be worked out again. You write no tests. Never delete or rename a file the task did not ask you
     to; a lost file fails the check.
     Do not hand-write parsing or arithmetic. ENGINES do the parts with one right answer (times, dates,
     durations, amounts, writing tables) and refuse with an error instead of guessing. process.py
     imports them: `from axiom_engines import table` (they are installed in your workspace; do not
     edit or commit them). Try any pure function first with the `engine` tool. Your part is reading
     the files and deciding what each item is; the engines' part is everything else. Engines:
{engines}
   - "fix": a code change. Add a test under the check's test path (one ending in / is a directory) that
     FAILS on the old code and PASSES on yours, using the test COMMAND the check names.
3. Use list_files / read_file to understand what is there, and write_file to do the work.{shell_hint}
4. Call `commit`, then `claim` with before_ref = {start_sha}, after_ref = the sha `commit` returned,
   the check id, and the task id.
5. Call `verify`. If it is refuted, read the reason and any `feedback`, fix what it names, commit, and
   claim again. Then give a one-line final answer with the verdict.
Use tools for everything; do not describe work you have not done with write_file."""

SHELL_HINT = """
   You have `run`: a shell in an isolated container with your workspace at /work and no network.
   Use it to process files (python is available) and to check your own work before you claim: for
   code, see your test fail and then pass. A claim you have not checked is a guess."""


class Workspace:
    """File access confined to one git worktree. The model cannot reach outside it, or into .git.

    With a `shell` (a DockerSandbox) the model also gets `run`: commands execute in a container that
    sees only this worktree, writable, with its `.git` file mounted read-only on top. Without one
    there is no shell tool at all; it never falls back to running on the host."""

    def __init__(self, root, shell=None):
        self.root = Path(root).resolve()
        self.shell = shell

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
        # a FILE where a directory must go is a dead end the model cannot see: "FileExistsError" alone
        # sent agents into write-retry loops until their step budget ran out. Name the blocker.
        for parent in reversed(p.relative_to(self.root).parents):
            blocker = self.root / parent
            if parent != Path(".") and blocker.is_file():
                raise ValueError(f"cannot create {path!r}: {parent.as_posix()!r} is a file, not a directory. "
                                 f"Delete it with delete_file first.")
        if p.is_dir():
            raise ValueError(f"{path!r} is a directory; write a file inside it")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"path": path, "bytes": len(content.encode("utf-8"))}

    def delete_file(self, path: str) -> dict:
        p = self._path(path)
        if p == self.root or p.is_dir():
            raise ValueError(f"{path!r} is a directory; delete_file removes files only")
        if not p.exists():
            raise ValueError(f"{path!r} does not exist")
        p.unlink()
        return {"deleted": path}

    def commit(self, message: str) -> dict:
        self._git("add", "-A")
        self._git("-c", "user.name=axiom1-agent", "-c", "user.email=agent@axiom1.invalid",
                  "commit", "-q", "--allow-empty", "-m", message)
        return {"sha": self.head()}

    def install_engines(self):
        """Put the engines in the workspace for process.py to import, kept out of git so they are never
        part of a delivery (the verifier installs its own copy before running a process anyway)."""
        install_engines(self.root)
        exclude = Path(self._git("rev-parse", "--git-path", "info/exclude"))
        exclude = exclude if exclude.is_absolute() else self.root / exclude
        lines = exclude.read_text(encoding="utf-8").splitlines() if exclude.exists() else []
        if f"/{ENGINES_DIR}/" not in lines:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text("\n".join(lines + [f"/{ENGINES_DIR}/"]) + "\n", encoding="utf-8")

    def engine(self, name: str, function: str, args: dict | None = None) -> dict:
        result = engines.call(name, function, args or {})
        return {"result": json.loads(json.dumps(result, default=str))}   # Decimals come back as text

    def run(self, command: str) -> dict:
        if self.shell is None:
            raise ValueError("no shell in this workspace")
        result = self.shell.run(str(self.root), ["sh", "-c", command], writable=True, protect=(".git",))
        return {"exit": result.returncode, "output": result.output[-3000:]}

    def tools(self):
        return self.TOOLS + ([self.RUN_TOOL] if self.shell is not None else [])

    RUN_TOOL = {"name": "run", "description": "Run a shell command in an isolated container: your workspace "
                "is /work (writable), there is no network. Returns the exit code and the last of the output.",
                "parameters": {"type": "object", "properties": {"command": {"type": "string"}},
                               "required": ["command"]}}

    TOOLS = [
        {"name": "list_files", "description": "List the files in your workspace.",
         "parameters": {"type": "object", "properties": {}}},
        {"name": "read_file", "description": "Read a file from your workspace.",
         "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
        {"name": "write_file", "description": "Create or overwrite a file in your workspace.",
         "parameters": {"type": "object", "properties": {"path": {"type": "string"},
                                                         "content": {"type": "string"}},
                        "required": ["path", "content"]}},
        {"name": "delete_file", "description": "Delete a file from your workspace (files only).",
         "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}},
        {"name": "commit", "description": "Commit everything in your workspace. Returns the commit sha.",
         "parameters": {"type": "object", "properties": {"message": {"type": "string"}},
                        "required": ["message"]}},
        {"name": "engine", "description": "Call a pure engine function to try it, e.g. name='table', "
         "function='csv_text', args={...}. An engine that cannot be sure returns an error instead of guessing. "
         "In process.py, import the same engines from axiom_engines.",
         "parameters": {"type": "object", "properties": {"name": {"type": "string"},
                                                         "function": {"type": "string"},
                                                         "args": {"type": "object"}},
                        "required": ["name", "function"]}},
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
        # None = the model's default. True/False is set per call by the runner's thinking dial. The switch
        # differs by server: on Nemotron (Nebius) only chat_template_kwargs.enable_thinking works (system-
        # prompt toggles and reasoning_effort did not, measured 2026-09-30); on Ollama's /v1 only
        # reasoning_effort "none" does (enable_thinking and think:false were ignored, measured 2026-10-01:
        # a reply went from 162 completion tokens to 18). AXIOM_THINKING_SWITCH picks one.
        self.thinking = None
        self.thinking_switch = os.environ.get("AXIOM_THINKING_SWITCH", "chat_template_kwargs")
        if self.thinking_switch not in ("chat_template_kwargs", "reasoning_effort"):
            raise SystemExit(f"AXIOM_THINKING_SWITCH must be chat_template_kwargs or reasoning_effort")
        self.usage = {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "thinking_calls": 0,
                      "cut_off": 0}   # replies that hit max_tokens (finish_reason "length")

    def __call__(self, messages, tools):
        body = {"model": self.model, "messages": messages, "tools": tools,
                "temperature": self.temperature, "max_tokens": self.max_tokens}
        if self.thinking is not None and self.thinking_switch == "chat_template_kwargs":
            body["chat_template_kwargs"] = {"enable_thinking": bool(self.thinking)}
        elif self.thinking is False:                       # reasoning_effort: thinking on = the model's default
            body["reasoning_effort"] = "none"
        req = urllib.request.Request(f"{self.base_url}/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Authorization": f"Bearer {self.api_key}",
                                              "Content-Type": "application/json"})
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = json.load(r)
                u = data.get("usage") or {}
                self.usage["calls"] += 1
                self.usage["thinking_calls"] += self.thinking is not False
                self.usage["prompt_tokens"] += u.get("prompt_tokens") or 0
                self.usage["completion_tokens"] += u.get("completion_tokens") or 0
                self.usage["cut_off"] += data["choices"][0].get("finish_reason") == "length"
                return data["choices"][0]["message"]
            except urllib.error.HTTPError as e:
                if e.code < 500 and e.code != 429 or attempt == 2:
                    raise RuntimeError(f"model endpoint returned HTTP {e.code}: "
                                       f"{e.read()[:300].decode(errors='replace')}") from None
            time.sleep(2 ** attempt)


def _looks_like_a_tool_call(content):
    """Text that is a call the model meant to make: a <tool_call> tag, or a bare JSON object naming a
    function. Seen live from Nemotron Lightning: the whole "final answer" was `<tool_call>`."""
    text = content.strip()
    return "<tool_call" in text or text.startswith('{"name"') or '"arguments"' in text[:200]


def _openai_tool(name, description, parameters):
    return {"type": "function", "function": {"name": name, "description": description or "",
                                             "parameters": parameters or {"type": "object", "properties": {}}}}


async def run_agent(agent_id, workspace, db, model, caps=(), max_steps=30, log=print, hub_url=None,
                    token=None, shell=None, thinking="auto"):
    """Run one agent until it gives a final answer or runs out of steps. Returns the transcript.

    With `hub_url` the agent connects to a hub over HTTP and is whoever `token` says it is; `db` and
    `caps` are ignored (the hub owns the database, the operator set the caps). Without it, the
    runner starts a stdio server on `db` for local development.

    `thinking` is decided by the harness, not the model: "on", "off", or "auto". Auto thinks while
    the work is new, stops once the task arrives with a verified skill (replay is cheap, and the
    verdict still catches a skill that no longer fits), and thinks again after a refutation.

    A run that ends while holding a task without a witnessed claim releases it, with a note the
    runner writes from what it observed, so the attempt leaves a lesson instead of nothing."""
    raw_log = log

    def log(line):  # a console that cannot encode the model's text must not end the run
        try:
            raw_log(line)
        except UnicodeEncodeError:
            raw_log(line.encode("ascii", "replace").decode("ascii"))

    ws = Workspace(workspace, shell)
    start_sha = ws.head()
    ws.install_engines()
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
        tools += [_openai_tool(t["name"], t["description"], t["parameters"]) for t in ws.tools()]
        mcp_names = {t.name for t in mcp_tools}

        state = {"task": None, "witnessed": False, "think": thinking != "off"}
        # The harness takes the task, not the model. Measured: agents that explored first took it late
        # (sometimes only after a claim bounced off the lease check), so the thinking dial learned about
        # a verified skill only after most of the run had already been paid for.
        res = await session.call_tool("take_task", {})
        taken = json.loads(res.content[0].text) if res.content and not res.isError else {}
        task = taken.get("task") if isinstance(taken, dict) else None
        if not task:
            log(f"[{agent_id}] no task to take")
            return [{"role": "assistant", "content": "No task available."}]
        _observe("take_task", json.dumps(taken), state, thinking, log, agent_id)
        opening = "Your task (leased to you): " + json.dumps({k: v for k, v in task.items() if k != "process"})
        if task.get("process") and shell is not None:
            # One witnessed pass locked this job's process in: run it, and let the model fill only the entry.
            replay = await _replay(agent_id, task, model, ws, session, start_sha, log)
            if replay["witnessed"]:
                state["witnessed"] = True
                return replay["messages"]
            # The input strayed from the locked process: the model repairs it, with thinking on.
            state["think"] = thinking != "off"
            state["refuted"] = True
            res = await session.call_tool("take_task", {})       # the refutation returned the task to the pool
            again = json.loads(res.content[0].text) if res.content and not res.isError else {}
            if isinstance(again, dict) and again.get("task"):
                task = again["task"]
                state["task"] = task["id"]
            opening = ("Your task (leased to you): " + json.dumps({k: v for k, v in task.items() if k != "process"})
                       + f"\n\nA locked process (version {replay['version']}) exists for this kind of job. It was run on "
                       f"these files and did not pass: {replay['note']}\nprocess.py and entry.json from that run are in "
                       "your workspace. Repair process.py (or entry.json), run it, then commit, claim and verify.")
        messages = [{"role": "system", "content": SYSTEM_PROMPT.format(
                        agent_id=agent_id, start_sha=start_sha, shell_hint=SHELL_HINT if shell else "",
                        engines=textwrap.indent(engines.summary(), "       "))},
                    {"role": "user", "content": opening}]
        try:
            return await _loop(agent_id, model, tools, messages, session, mcp_names, ws, max_steps, log,
                               state, thinking)
        finally:
            if state["task"] and not state["witnessed"]:
                note = _attempt_summary(messages)
                try:
                    await session.call_tool("release_task", {"task_id": state["task"], "note": note})
                    log(f"[{agent_id}] released task {state['task']} without a witnessed claim")
                except Exception as e:  # releasing is best effort; the lease expiry is the backstop
                    log(f"[{agent_id}] could not release task: {e}")


ENTRY_PROMPT = """A known, verified process will do this job. Your only part is its entry: the values that change
from one run to the next. Return ONLY a JSON object with exactly the same keys as the example, filled in
for THIS task. No other text."""


def _json_object(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").split("\n", 1)[-1]
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start:end + 1]) if start != -1 and end > start else None


async def _replay(agent_id, task, model, ws, session, start_sha, log):
    """Run a locked process on this task's files. The model only fills the entry, and only if the
    process has one. Returns {"witnessed", "messages", "note", "version"}."""
    proc = task["process"]
    messages = [{"role": "user", "content": f"replaying locked process v{proc['version']}"}]
    ws.write_file("process.py", proc["script"])
    if proc.get("entry_example"):
        entry = None
        if hasattr(model, "thinking"):
            model.thinking = False                     # filling a form, not working out a process
        try:
            reply = model([{"role": "system", "content": ENTRY_PROMPT},
                           {"role": "user", "content": f"Task: {task['title']}\n\nExample entry from an earlier run:\n"
                                                       f"{proc['entry_example']}"}], [])
            entry = _json_object(reply.get("content"))
            messages.append({"role": "assistant", "content": reply.get("content") or ""})
        except (ValueError, RuntimeError):
            entry = None
        ws.write_file("entry.json", json.dumps(entry) if isinstance(entry, dict) else proc["entry_example"])
    ran = ws.run(" ".join(process_argv("python")))
    sha = ws.commit(f"replay locked process v{proc['version']}")["sha"]
    note = ""
    try:
        res = await session.call_tool("claim", {"statement": f"ran the locked process (v{proc['version']})",
                                                "check_id": task["check_id"], "before_ref": start_sha,
                                                "after_ref": sha, "task_id": task["id"]})
        claim = json.loads(res.content[0].text)
        res = await session.call_tool("verify", {"claim_id": claim["id"]})
        verdict = json.loads(res.content[0].text)
    except (ValueError, KeyError, IndexError) as e:
        verdict = {"label": "error", "reason": str(e)}
    witnessed = verdict.get("label") == "witnessed"
    if not witnessed:
        feedback = (verdict.get("evidence") or {}).get("after", {}).get("feedback") or []
        note = f"{verdict.get('reason')}. " + " ".join(feedback)
        if ran.get("exit"):
            note += f" The process itself exited {ran['exit']}: {ran['output'][-300:]}"
    log(f"[{agent_id}] replayed locked process v{proc['version']}: {verdict.get('label')}")
    messages.append({"role": "assistant", "content": f"replayed v{proc['version']}: {verdict.get('label')}"})
    return {"witnessed": witnessed, "messages": messages, "note": note, "version": proc["version"]}


def _attempt_summary(messages):
    """What the runner saw this agent do: files written, commands run, verdicts, the last error.
    Observed by the harness, but it lands in the record as the agent's declared account."""
    wrote, ran, verdicts, last_error = [], [], [], ""
    calls = {c["id"]: c for m in messages if m["role"] == "assistant" for c in m.get("tool_calls", [])}
    for m in messages:
        if m["role"] != "tool":
            continue
        call = calls.get(m.get("tool_call_id"), {}).get("function", {})
        try:
            args = json.loads(call.get("arguments") or "{}")
            out = json.loads(m["content"])
        except (ValueError, TypeError):
            continue
        name = call.get("name")
        if isinstance(out, dict) and out.get("error"):
            last_error = f"{name}: {str(out['error'])[:200]}"
        if name == "write_file" and isinstance(args, dict):
            wrote.append(args.get("path"))
        elif name == "run" and isinstance(out, dict):
            ran.append(f"{str(args.get('command'))[:60]} -> exit {out.get('exit')}")
        elif name == "verify" and isinstance(out, dict) and out.get("label"):
            verdicts.append(f"{out['label']}: {str(out.get('reason'))[:120]}")
    parts = [f"wrote {', '.join(dict.fromkeys(p for p in wrote if p)) or 'nothing'}"]
    if ran:
        parts.append("ran " + "; ".join(ran[-3:]))
    if verdicts:
        parts.append("verdicts " + "; ".join(verdicts[-2:]))
    if last_error:
        parts.append("last error " + last_error)
    return ". ".join(parts)


async def _loop(agent_id, model, tools, messages, session, mcp_names, ws, max_steps, log, state, thinking):
    nudges = 0
    unfinished = 0
    for step in range(max_steps):
        if hasattr(model, "thinking") and thinking != "default":
            model.thinking = state["think"]
        reply = model(messages, tools)
        calls = reply.get("tool_calls") or []
        content = reply.get("content") or ""
        messages.append({"role": "assistant", "content": content,
                         **({"tool_calls": calls} if calls else {})})
        if not calls and _looks_like_a_tool_call(content) and nudges < 3:
            # the model wrote a call as text instead of making it: that is not a final answer
            nudges += 1
            log(f"[{agent_id}] tool call written as text, not made; asking again ({nudges}/3)")
            messages.append({"role": "user", "content": "Your last message contains a tool call written "
                             "as text, so it was not executed. Make the call through the tool interface."})
            continue
        if not calls and state["task"] and not state["witnessed"] and unfinished < 5:
            # The harness decides when a run is over, not the model. Measured: agents "thought out loud"
            # in a message with no tool call ("We need to reconcile... Let's parse.") and the run ended
            # there, after 3 calls, with nothing delivered and nothing handed back.
            unfinished += 1
            log(f"[{agent_id}] stopped without a verified result; asking it to continue ({unfinished}/5)")
            messages.append({"role": "user", "content": "Your task is not finished: nothing has been verified yet. "
                             "Keep working with tool calls (write the files, commit, claim, verify). If you truly "
                             "cannot finish, call release_task with a note saying what you tried and where you "
                             "got stuck."})
            continue
        if not calls:
            log(f"[{agent_id}] final: {content.strip()[:300]}")
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
                elif name in {t["name"] for t in ws.tools()}:
                    text = json.dumps(getattr(ws, name)(**args))
                else:
                    text = json.dumps({"error": f"no tool named {name!r}"})
            except Exception as e:  # a bad call is reported back to the model, not fatal
                text = json.dumps({"error": f"{type(e).__name__}: {e}"})
            log(f"[{agent_id}] {name}({_short(call['function'].get('arguments'))}) -> {_short(text)}")
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": text})
            _observe(name, text, state, thinking, log, agent_id)
    log(f"[{agent_id}] stopped after {max_steps} steps")
    return messages


def _observe(name, text, state, thinking, log, agent_id):
    """Track what the harness needs: the task held, whether it was witnessed, and the thinking dial."""
    try:
        out = json.loads(text)
    except ValueError:
        return
    if not isinstance(out, dict):
        return
    if name == "take_task" and isinstance(out.get("task"), dict):
        state["task"] = out["task"]["id"]
        if thinking == "auto" and not state.get("refuted"):
            # a refutation earlier in this run is fresher evidence than any skill: keep thinking on.
            # Measured: a refuted task returns to the pool, the agent re-takes it, and the re-take
            # (still carrying the skill) used to switch thinking straight back off.
            has_skill = any(lesson.get("kind") == "skill" for lesson in out["task"].get("lessons", []))
            state["think"] = not has_skill
            log(f"[{agent_id}] thinking {'off: a verified skill exists' if has_skill else 'on: new work'}")
    elif name == "verify" and out.get("label") == "witnessed":
        state["witnessed"] = True
    elif name == "verify" and out.get("label") == "refuted":
        state["refuted"] = True
        if thinking == "auto" and not state["think"]:
            state["think"] = True
            log(f"[{agent_id}] thinking on: refuted")
    elif name == "release_task" and out.get("status") == "open":
        state["task"] = None


def _short(s, n=160):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n] + "..."
