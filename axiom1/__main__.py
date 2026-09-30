"""Operator commands. These are for the human running Axiom-1, not for agents.

    python -m axiom1 serve                      # MCP server for one agent (see mcp_server.py)
    python -m axiom1 register-check ID REPO --tests tests -- python -m pytest
    python -m axiom1 events [--since N]         # the raw event log, as JSON lines
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

    ax = Axiom(a.db)
    if a.cmd == "register-check":
        if not command:
            p.error("give the test command after --")
        ax.register_check(a.check_id, os.path.abspath(a.repo), command, a.tests)
        print(f"registered {a.check_id}: {command} over {a.tests}")
    elif a.cmd == "events":
        for e in ax.events(a.since):
            print(json.dumps(e))


if __name__ == "__main__":
    sys.exit(main())
