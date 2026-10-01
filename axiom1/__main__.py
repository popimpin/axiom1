"""Operator commands. These are for the human running Axiom-1, not for agents.

    python -m axiom1 hub --port 8765            # THE shared server: owns the database, HTTP + tokens
    python -m axiom1 add-agent nemotron-1 --caps shell   # admit an agent; prints its token once
    python -m axiom1 revoke-agent nemotron-1     # its token stops working at once
    python -m axiom1 register-check ID REPO --tests tests/ -- python -m pytest
    python -m axiom1 events [--since N]         # the raw event log, as JSON lines (operator-only)
    python -m axiom1 export-okf ./bundle         # facts, skills and lessons as an OKF bundle
    python -m axiom1 agent --id nemotron-1 --workspace ./wt --hub http://127.0.0.1:8765/mcp
                                                # a model-driven agent; token from AXIOM_TOKEN
    python -m axiom1 serve                      # stdio server for one agent (local development)

A workspace is a git worktree of the registered repo, so the agent's commits are visible to the
verifier:  git -C my-repo worktree add ../wt-nemotron-1
"""
import argparse
import json
import os
import sys

from .core import Axiom


def main(argv=None):
    p = argparse.ArgumentParser(prog="axiom1")
    p.add_argument("--db", default=os.environ.get("AXIOM_DB", "axiom1.db"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="stdio MCP server for one agent (local development)")
    hb = sub.add_parser("hub", help="the shared server: owns the database, serves MCP over HTTP")
    hb.add_argument("--host", default="127.0.0.1")
    hb.add_argument("--port", type=int, default=8765)
    aa = sub.add_parser("add-agent", help="admit an agent and print its token (shown once)")
    aa.add_argument("agent_id")
    aa.add_argument("--caps", default="", help="comma-separated capabilities")
    ra = sub.add_parser("revoke-agent", help="stop an agent's token working")
    ra.add_argument("agent_id")
    rc = sub.add_parser("register-check", help="define what 'verified' means for a repo")
    rc.add_argument("check_id")
    rc.add_argument("repo")
    rc.add_argument("--tests", action="append", required=True,
                    help="test path(s) laid over the pre-fix code; repeatable")
    rc.add_argument("--sandbox", default="local", choices=["local", "docker"],
                    help="where the command runs; local is for development only")
    rc.add_argument("--image", help="container image for --sandbox docker, e.g. python:3.13-slim")
    rc.add_argument("--holdout", help="directory of tests agents never see, run against every fix; "
                                      "must live outside the repo")
    ag = sub.add_parser("agent", help="run a model-driven agent (Nemotron on Nebius by default)")
    ag.add_argument("--id", required=True)
    ag.add_argument("--workspace", required=True, help="a git worktree of the registered repo")
    ag.add_argument("--hub", help="hub URL, e.g. http://127.0.0.1:8765/mcp (token from AXIOM_TOKEN)")
    ag.add_argument("--caps", default="", help="comma-separated capabilities (stdio mode only)")
    ag.add_argument("--model", default=None, help="overrides AXIOM_MODEL")
    ag.add_argument("--max-steps", type=int, default=30)
    ex = sub.add_parser("export-okf", help="write the record (facts, skills, lessons) as an OKF bundle")
    ex.add_argument("directory")
    ev = sub.add_parser("events")
    ev.add_argument("--since", type=int, default=0)
    argv = list(sys.argv[1:] if argv is None else argv)
    # everything after the first "--" is the test command, passed through untouched
    command = argv[argv.index("--") + 1:] if "--" in argv else []
    argv = argv[:argv.index("--")] if "--" in argv else argv
    a = p.parse_args(argv)

    if a.cmd == "serve":
        os.environ["AXIOM_DB"] = a.db
        from .mcp_server import main as serve
        return serve()

    if a.cmd == "agent":
        import asyncio
        from .agent import ChatModel, run_agent
        caps = [c.strip() for c in a.caps.split(",") if c.strip()]
        token = os.environ.get("AXIOM_TOKEN")
        if a.hub and not token:
            p.error("set AXIOM_TOKEN to the token add-agent printed")
        asyncio.run(run_agent(a.id, a.workspace, os.path.abspath(a.db), ChatModel(a.model), caps,
                              a.max_steps, hub_url=a.hub, token=token))
        return 0

    ax = Axiom(a.db)
    if a.cmd == "hub":
        from .mcp_server import serve_hub
        print(f"axiom1 hub on http://{a.host}:{a.port}/mcp  (database {os.path.abspath(a.db)})")
        serve_hub(ax, a.host, a.port)
    elif a.cmd == "add-agent":
        caps = [c.strip() for c in a.caps.split(",") if c.strip()]
        token = ax.issue_token(a.agent_id, caps)
        print(f"{a.agent_id} admitted. Its token, shown once (only a hash is stored):\n{token}")
    elif a.cmd == "revoke-agent":
        ax.revoke_token(a.agent_id)
        print(f"{a.agent_id}: token revoked")
    elif a.cmd == "register-check":
        if not command:
            p.error("give the test command after --")
        ax.register_check(a.check_id, os.path.abspath(a.repo), command, a.tests,
                          sandbox=a.sandbox, image=a.image, holdout=a.holdout)
        print(f"registered {a.check_id}: {command} over {a.tests} in {a.sandbox}"
              + (f" ({a.image})" if a.image else ""))
    elif a.cmd == "export-okf":
        print(json.dumps(ax.export_okf(a.directory)))
    elif a.cmd == "events":
        for e in ax.events(a.since):
            print(json.dumps(e))


if __name__ == "__main__":
    sys.exit(main())
