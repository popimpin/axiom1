"""Axiom-1 over MCP: the surface agents use.

Each agent runs its own server process, and every process points at the same
database, so they all share one state. Who the agent IS comes from the process
environment, set by whoever launches it, never from a tool argument: an agent
cannot act as another agent by passing a different name.

    AXIOM_AGENT  this agent's id (required)
    AXIOM_CAPS   comma-separated capabilities, e.g. "shell,gpu"
    AXIOM_DB     path to the shared SQLite file (default ./axiom1.db)

Deliberately absent: registering checks. What "verified" means is decided by a
human, through `python -m axiom1 register-check`, not by the agents it judges.
"""
import os

from mcp.server.fastmcp import FastMCP

from .core import Axiom

INSTRUCTIONS = """You are one of several agents sharing one state through Axiom-1.
Anything you write is stored as `declared`. Only the server's `verify` makes a claim `witnessed`
or `refuted`, by running a human-registered test that must FAIL on the code before your fix and
PASS after it. Build on `witnessed` facts; treat `declared` ones as unconfirmed.
Start with `briefing`. Ack every message you read with the sha256 you were given."""


def build(ax: Axiom, agent_id: str, caps=()) -> FastMCP:
    ax.join(agent_id, caps)
    mcp = FastMCP("axiom1", instructions=INSTRUCTIONS)

    @mcp.tool()
    def briefing() -> dict:
        """The shared state that matters to you: witnessed facts, open claims, recent refutations,
        tasks you can take, every agent's track record, and your unread message count."""
        return ax.briefing(agent_id)

    @mcp.tool()
    def send(to: str, body: str) -> dict:
        """Send a message to another agent. It stays `sent` until they ack its sha256."""
        return ax.send(agent_id, to, body)

    @mcp.tool()
    def inbox() -> dict:
        """Messages to you that you have not acked yet."""
        return {"messages": ax.inbox(agent_id)}

    @mcp.tool()
    def ack(message_id: str, sha256: str) -> dict:
        """Confirm you received a message by echoing its sha256."""
        return ax.ack(agent_id, message_id, sha256)

    @mcp.tool()
    def message_status(message_id: str) -> dict:
        """`sent` or `delivered`."""
        return {"id": message_id, "status": ax.message_status(message_id)}

    @mcp.tool()
    def remember(key: str, content: str) -> dict:
        """Write to shared memory. Always stored as `declared`."""
        return ax.remember(agent_id, key, content)

    @mcp.tool()
    def recall(key: str) -> dict:
        """Every entry under a key, newest first, each with its label."""
        return {"key": key, "entries": ax.recall(key)}

    @mcp.tool()
    def list_checks() -> dict:
        """The registered checks you can claim against, and the test paths each one runs."""
        return {"checks": ax.list_checks()}

    @mcp.tool()
    def post_task(title: str, caps: list[str] | None = None) -> dict:
        """Post work to the collective. `caps` are what an agent needs to take it."""
        return ax.post_task(agent_id, title, caps or [])

    @mcp.tool()
    def take_task(lease_seconds: int = 600) -> dict:
        """Lease the oldest open task you are capable of. `task` is null if there is none."""
        return {"task": ax.take_task(agent_id, lease_seconds)}

    @mcp.tool()
    def claim(statement: str, check_id: str, before_ref: str, after_ref: str,
              task_id: str | None = None) -> dict:
        """Claim that commit `after_ref` fixes what was wrong at `before_ref`, under a registered
        check. Stored as `declared` until `verify` runs."""
        return ax.claim(agent_id, statement, check_id, before_ref, after_ref, task_id)

    @mcp.tool()
    def verify(claim_id: str) -> dict:
        """Have the server check a claim. The verdict is the server's, and it is final."""
        return ax.verify(claim_id)

    @mcp.tool()
    def track_record(agent: str) -> dict:
        """An agent's witnessed/refuted claims (honesty) and expired leases (reliability)."""
        return ax.track_record(agent)

    return mcp


def main():
    agent_id = os.environ.get("AXIOM_AGENT")
    if not agent_id:
        raise SystemExit("set AXIOM_AGENT to this agent's id")
    caps = [c.strip() for c in os.environ.get("AXIOM_CAPS", "").split(",") if c.strip()]
    ax = Axiom(os.environ.get("AXIOM_DB", "axiom1.db"))
    build(ax, agent_id, caps).run()


if __name__ == "__main__":
    main()
