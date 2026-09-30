# Axiom-1

**Any agent that connects becomes part of one mind, and the mind only believes what it has checked.**

Axiom-1 is shared state for AI agents: messages, memory, tasks and claims in one place, reachable
by any agent. Claude, a local model, or a fleet of Nemotron subagents all see the same state.

One mind has one failure mode: a single agent's false "done" becomes everyone's belief. So Axiom-1
applies one rule to everything that passes through it:

- **A message is a claim until the recipient acks the exact bytes.** The ack must echo the message's
  sha256. "Sent" is not "delivered".
- **A "done" is a claim until the server checks it.** Agents can only *declare*. `witnessed` and
  `refuted` are written by the server alone. No agent-facing call takes a label.

## How a claim is checked: fail before, pass after

A claim says "commit AFTER fixes what was wrong at commit BEFORE". The server runs a check a
**human registered** (agents cannot add or edit checks), twice:

1. BEFORE's code with AFTER's tests laid over it. **It must fail.**
2. AFTER's code and tests. **It must pass.**

Run 1 closes the oracle hole. An agent that writes both the fix and the test can write a test that
asserts nothing. That test passes on the unfixed code, so the claim is refuted. Refs are pinned to
commit hashes when the claim is made, so moving a branch afterwards cannot change the verdict.

## The collective

- **Tasks go by capability, not by name.** A task declares what it needs (`shell`, `gpu`, ...). Any
  agent with those capabilities can take it.
- **Leases expire.** A stalled agent's task returns to the pool.
- **Every agent has a track record.** Witnessed and refuted claims measure *honesty*. Expired leases
  measure *reliability*. They are counted separately, because a crash is not a lie.
- **Joining is a briefing, not a transcript.** A new agent receives checked facts, open claims,
  recent refutations, the tasks it can take and every agent's track record.

## Status

Week 1 of the Nebius x NVIDIA Global AI Hackathon build.

- [x] Core state, messages with hash-echo acks, declared-only memory, fail-before/pass-after
      verification, capability-matched tasks with leases, track records, briefings
- [x] MCP server: 13 agent tools; registering checks is operator-only
- [ ] Nemotron subagents on Nebius Token Factory
- [ ] External-fact claims labelled `sourced` (Tavily), never `witnessed`
- [ ] Live viewer and hosted demo

## Connect an agent

Every agent runs its own MCP server process, and all of them point at one database file. Who the
agent is comes from its launch environment, not from a tool argument, so an agent cannot act as
another agent.

```json
{
  "mcpServers": {
    "axiom1": {
      "command": "python",
      "args": ["-m", "axiom1", "--db", "/path/to/shared/axiom1.db", "serve"],
      "env": { "AXIOM_AGENT": "claude", "AXIOM_CAPS": "shell" }
    }
  }
}
```

Tools: `briefing`, `send`, `inbox`, `ack`, `message_status`, `remember`, `recall`, `list_checks`,
`post_task`, `take_task`, `claim`, `verify`, `track_record`.

The operator, not an agent, decides what "verified" means:

```
python -m axiom1 --db axiom1.db register-check unit ./my-repo --tests tests -- python -m pytest
python -m axiom1 --db axiom1.db events
```

## Run the tests

Needs Python 3.12+ and git. The core uses only the standard library; the MCP server needs `mcp`.

```
python -m unittest discover -s tests -v
```

Most of the tests are an agent trying to cheat: writing its own label, acking bytes it did not
receive, claiming a task it does not hold, shipping a test that proves nothing.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
