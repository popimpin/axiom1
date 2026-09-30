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
**human registered** (agents cannot add or edit checks):

1. BEFORE's code with AFTER's tests laid over it. **It must fail.**
2. AFTER's code and tests. **It must pass.**
3. AFTER's code and tests plus a canary test with a random name and random values. **It must fail.**
   Once per assertion style (`assertEqual`, `assertTrue`, bare `assert`).

Run 1 closes the oracle hole. An agent that writes both the fix and the test can write a test that
asserts nothing. That test passes on the unfixed code, so the claim is refuted.

Run 3 is run 2's control. The fix runs in full, so it could leave the bug alone and rig *how* tests
run instead: shadow the test runner, or disable assertions the moment a test imports it. Then its own
test "passes", and so would a canary that must fail. That claim is refuted as tampering. Before
trusting run 3, the server checks that the command actually runs a file shaped like a canary (a
command limited to one test file never would), so an honest fix is never accused.

Refs are pinned to commit hashes when the claim is made, so moving a branch afterwards cannot change
the verdict.

### Where it runs

Verification executes code an agent wrote, so every check names a sandbox, and every verdict's
evidence records which one judged it:

- `docker`: a throwaway container with no network, capped memory, CPU and process count, no Linux
  capabilities, an unprivileged user, a read-only root, and the tree mounted **read-only**. A probe
  that tries to reach the network, read a file outside the tree, write into the tree or run as root
  is blocked on all four (`tests/test_sandbox.py`, which also shows the same probe getting out when
  unsandboxed).
- `local`: runs on the server's own machine. For development only; evidence says `isolated: false`.

```
python -m axiom1 register-check unit ./my-repo --tests tests/ --sandbox docker --image python:3.13-slim \
    -- python -m unittest discover -s tests
```

A Nebius Serverless AI job backend (each verification in a disposable cloud container) is on the
roadmap.

**What this does not catch yet:** a rig aimed at one specific test by name, and code that special-cases
the exact inputs its test uses. Both need tests the agent never sees (held-out tests, on the roadmap).

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
- [x] Model-driven agent runner: Nemotron on Nebius Token Factory (Lightning by default), joined over MCP
- [ ] External-fact claims labelled `sourced` (Tavily), never `witnessed`
- [x] Sandboxed verification: Docker (no network, read-only, unprivileged, capped)
- [ ] Nebius Serverless AI job sandbox
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

## Run a Nemotron agent

The runner joins a model to Axiom-1 over MCP, like any other agent, and adds four workspace tools
(`list_files`, `read_file`, `write_file`, `commit`) confined to one git worktree of the registered
repo. Any OpenAI-compatible endpoint works; the default is Nemotron on Nebius Token Factory.

```
export NEBIUS_API_KEY=...                       # never commit this
python examples/live_agent.py                   # one agent, one real bug, one verdict
python -m axiom1 --db axiom1.db agent --id nemotron-1 --workspace ../wt-nemotron-1
```

`NEBIUS_BASE_URL` and `AXIOM_MODEL` override the endpoint and model
(default `nvidia/Nemotron-3_5-Lightning`).

## Run the tests

Needs Python 3.12+ and git. The core uses only the standard library; the MCP server needs `mcp`.

```
python -m unittest discover -s tests -v
```

Most of the tests are an agent trying to cheat: writing its own label, acking bytes it did not
receive, claiming a task it does not hold, shipping a test that proves nothing.

## Licence

Apache-2.0. See [LICENSE](LICENSE).
