"""Axiom-1 over MCP: the surface agents use. Two ways to run it.

hub (use this for anything shared):
    One process owns the database and serves MCP over HTTP. Each agent holds a token the operator
    issued (`python -m axiom1 add-agent ID`); every tool call is attributed to the token's agent.
    Agents never see the database file, so they cannot edit it around the rules.

        python -m axiom1 --db axiom1.db hub --port 8765
        agent config: url http://host:8765/mcp, header "Authorization: Bearer <token>"

stdio (local development):
    Each agent runs its own server process on a shared database file, and its identity comes from
    the launch environment. That trusts whoever launches the process, and any agent with a shell
    could open the file directly. Fine on your own machine, not for a shared one.

        AXIOM_AGENT  this agent's id (required)
        AXIOM_CAPS   comma-separated capabilities, e.g. "shell,gpu"
        AXIOM_DB     path to the shared SQLite file (default ./axiom1.db)

In both, no tool takes an agent's identity as an argument, and registering checks is absent: what
"verified" means is decided by a human, through `python -m axiom1 register-check`.
"""
import json
import os

import anyio
from mcp.server.fastmcp import Context, FastMCP

from .core import Axiom, AxiomError

INSTRUCTIONS = """You are one of several agents sharing one state through Axiom-1.
Anything you write is stored as `declared`. Only the server's `verify` makes a claim `witnessed`
or `refuted`, by running a human-registered test that must FAIL on the code before your fix and
PASS after it. Build on `witnessed` facts; treat `declared` ones as unconfirmed.
Start with `briefing`. Ack every message you read with the sha256 you were given."""


def build(ax: Axiom, agent_id: str | None = None, caps=(), identify=None) -> FastMCP:
    """`identify(ctx)` names the calling agent. Without one, every call is `agent_id` (stdio mode)."""
    if identify is None:
        ax.join(agent_id, caps)
        identify = lambda ctx: agent_id  # noqa: E731
    mcp = FastMCP("axiom1", instructions=INSTRUCTIONS, log_level="WARNING")

    async def call(ctx, fn):
        me = identify(ctx)
        if me is None:
            raise AxiomError("not authenticated")
        # worker thread: a verify runs tests for seconds and must not stall other agents' calls
        return await anyio.to_thread.run_sync(lambda: fn(me))

    @mcp.tool()
    async def briefing(ctx: Context) -> dict:
        """The shared state that matters to you: witnessed facts, open claims, recent refutations,
        tasks you can take, every agent's track record, and your unread message count."""
        return await call(ctx, lambda me: ax.briefing(me))

    @mcp.tool()
    async def send(to: str, body: str, ctx: Context) -> dict:
        """Send a message to another agent. It stays `sent` until they ack its sha256."""
        return await call(ctx, lambda me: ax.send(me, to, body))

    @mcp.tool()
    async def inbox(ctx: Context) -> dict:
        """Messages to you that you have not acked yet."""
        return await call(ctx, lambda me: {"messages": ax.inbox(me)})

    @mcp.tool()
    async def ack(message_id: str, sha256: str, ctx: Context) -> dict:
        """Confirm you received a message by echoing its sha256."""
        return await call(ctx, lambda me: ax.ack(me, message_id, sha256))

    @mcp.tool()
    async def message_status(message_id: str, ctx: Context) -> dict:
        """`sent` or `delivered`."""
        return await call(ctx, lambda me: {"id": message_id, "status": ax.message_status(message_id)})

    @mcp.tool()
    async def remember(key: str, content: str, ctx: Context) -> dict:
        """Write to shared memory. Always stored as `declared`."""
        return await call(ctx, lambda me: ax.remember(me, key, content))

    @mcp.tool()
    async def recall(key: str, ctx: Context) -> dict:
        """Every entry under a key, newest first, each with its label."""
        return await call(ctx, lambda me: {"key": key, "entries": ax.recall(key)})

    @mcp.tool()
    async def list_checks(ctx: Context) -> dict:
        """The registered checks you can claim against: the command that judges you, where tests
        live, where they run, and whether hidden tests also apply."""
        return await call(ctx, lambda me: {"checks": ax.list_checks()})

    @mcp.tool()
    async def post_task(title: str, ctx: Context, caps: list[str] | None = None) -> dict:
        """Post work to the collective. `caps` are what an agent needs to take it."""
        return await call(ctx, lambda me: ax.post_task(me, title, caps or []))

    @mcp.tool()
    async def take_task(ctx: Context, lease_seconds: int = 600) -> dict:
        """Lease the oldest open task you are capable of. `task` is null if there is none.
        You must hold a task's lease to claim it."""
        return await call(ctx, lambda me: {"task": ax.take_task(me, lease_seconds)})

    @mcp.tool()
    async def claim(statement: str, check_id: str, before_ref: str, after_ref: str, ctx: Context,
                    task_id: str | None = None) -> dict:
        """Claim that commit `after_ref` fixes what was wrong at `before_ref`, under a registered
        check. Stored as `declared` until `verify` runs."""
        return await call(ctx, lambda me: ax.claim(me, statement, check_id, before_ref, after_ref, task_id))

    @mcp.tool()
    async def verify(claim_id: str, ctx: Context) -> dict:
        """Have the server check a claim. The verdict is the server's, and it is final."""
        return await call(ctx, lambda me: ax.verify(claim_id))

    @mcp.tool()
    async def track_record(agent: str, ctx: Context) -> dict:
        """An agent's witnessed/refuted claims (honesty) and expired leases (reliability)."""
        return await call(ctx, lambda me: ax.track_record(agent))

    return mcp


# ---- hub: one process, HTTP, a token per agent ------------------------------------------------------

def _bearer(headers):
    value = headers.get("authorization", "")
    return value[7:].strip() if value.lower().startswith("bearer ") else None


def hub_app(ax: Axiom):
    """The MCP app behind a gate: a request without a valid token never reaches a tool."""
    def identify(ctx):
        request = ctx.request_context.request
        return ax.agent_for_token(_bearer(request.headers)) if request is not None else None

    app = build(ax, identify=identify).streamable_http_app()

    async def gate(scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            if ax.agent_for_token(_bearer(headers)) is None:
                # read the request body before refusing: answering mid-upload can reset the
                # connection on the client's side (seen on Windows) instead of delivering the 401
                while (await receive()).get("more_body", False):
                    pass
                body = json.dumps({"error": "a valid agent token is required"}).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"),
                                        (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": body})
                return
        await app(scope, receive, send)

    return gate


def serve_hub(ax: Axiom, host="127.0.0.1", port=8765):
    import uvicorn
    uvicorn.run(hub_app(ax), host=host, port=port, log_level="warning")


def main():
    agent_id = os.environ.get("AXIOM_AGENT")
    if not agent_id:
        raise SystemExit("set AXIOM_AGENT to this agent's id")
    caps = [c.strip() for c in os.environ.get("AXIOM_CAPS", "").split(",") if c.strip()]
    ax = Axiom(os.environ.get("AXIOM_DB", "axiom1.db"))
    build(ax, agent_id, caps).run()


if __name__ == "__main__":
    main()
