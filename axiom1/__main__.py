"""Operator commands. These are for the human running Axiom-1, not for agents.

    python -m axiom1 serve                      # MCP server for one agent (see mcp_server.py)
    python -m axiom1 register-check ID REPO --tests tests -- python -m pytest
    python -m axiom1 events [--since N]         # the raw event log, as JSON lines
    python -m axiom1 agent --id nemotron-1 --workspace ./wt-nemotron-1   # a model-driven agent

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
    sub.add_parser("serve")
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
    ag.add_argument("--caps", default="", help="comma-separated capabilities")
    ag.add_argument("--model", default=None, help="overrides AXIOM_MODEL")
    ag.add_argument("--max-steps", type=int, default=30)
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
        asyncio.run(run_agent(a.id, a.workspace, os.path.abspath(a.db), ChatModel(a.model), caps,
                              a.max_steps))
        return 0

    ax = Axiom(a.db)
    if a.cmd == "register-check":
        if not command:
            p.error("give the test command after --")
        ax.register_check(a.check_id, os.path.abspath(a.repo), command, a.tests,
                          sandbox=a.sandbox, image=a.image, holdout=a.holdout)
        print(f"registered {a.check_id}: {command} over {a.tests} in {a.sandbox}"
              + (f" ({a.image})" if a.image else ""))
    elif a.cmd == "events":
        for e in ax.events(a.since):
            print(json.dumps(e))


if __name__ == "__main__":
    sys.exit(main())
