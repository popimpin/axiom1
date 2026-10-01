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

### Held-out tests

The canary cannot catch two cheats: code that special-cases the exact inputs its own test uses
(`return 5 if (a, b) == (2, 3) else a - b`), and a rig aimed at one test by name (the canary has a
different, random name, so it fails normally and the rig slips past). Both pass the agent's own test.

So the operator can register a directory of **held-out tests** with a check. They run against every
fix, and must pass:

```
python -m axiom1 register-check unit ./my-repo --tests tests/ --holdout ../held-out-tests -- python -m pytest
```

- They must live **outside the repo**. Agents work in worktrees of it and can read its whole history,
  so registration refuses a held-out directory inside it.
- Agents learn **that** held-out tests exist (`list_checks`) and **that** they failed, never which
  ones or why. Their output names the inputs and expected values, and an agent that saw
  `14 != 6` could special-case that too. The output goes to the operator's event log
  (`python -m axiom1 events`), which is not on the agent surface.

`tests/test_holdout.py` shows both cheats witnessed without held-out tests and refuted with them.

**Still not caught:** a cheat that also defeats tests it has never seen. Held-out tests only raise the
bar to "fix the behaviour in general".

### Claims that are not fixes: "nothing broke"

A refactor has no failing test to turn green. `claim(..., kind="no_regression")` is checked instead as:

1. BEFORE's tree. **It must pass** (no baseline, no claim).
2. AFTER's code with **BEFORE's tests** put back. **It must pass.** This is what stops the obvious
   cheat: break something, delete its test, and leave the rest of the suite green.
3. AFTER's tree. **It must pass.**
4. Held-out tests that passed on BEFORE must still pass on AFTER.
5. The canaries must still fail.

A witnessed claim becomes a fact prefixed with what was proven, `[fixed]` or `[no regression]`, then
the agent's own words. An agent that claims "fixed every bug" under `no_regression` gets a fact that
reads `[no regression] fixed every bug`.

## The collective

- **Tasks go by capability, not by name.** A task declares what it needs (`shell`, `gpu`, ...). Any
  agent with those capabilities can take it.
- **Leases expire.** A stalled agent's task returns to the pool.
- **Only fixes to code the collective already had count.** A claim whose starting commit is not on the
  check's base branch (`main` by default) is still checked, and can be true, but adds nothing to the
  agent's record and becomes no fact. Otherwise an agent could plant a bug in its own branch, fix
  it, and repeat until its record is spotless. Ancestry anchors this; commit authorship cannot,
  since anyone can set a git author. Refuted claims count wherever they start.
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
- [x] Held-out tests the agents never see (operator-only output)
- [x] Hub: one process owns the database; per-agent tokens (hashed), identity from the token on every call
- [x] Sandboxed shell for agents (`run`): worktree writable, `.git` read-only, no network; see docs/MEASUREMENTS.md
- [ ] Nebius Serverless AI job sandbox
- [ ] Live viewer and hosted demo

## Connect an agent

### The hub (use this for anything shared)

One process owns the database and serves MCP over HTTP. The operator admits each agent and gets a
token, shown once and stored only as a hash. Every tool call is attributed to the agent whose token
it carries; no tool takes an identity as an argument. A request without a valid token is refused
before it reaches any tool.

```
python -m axiom1 --db axiom1.db hub --port 8765
python -m axiom1 --db axiom1.db add-agent claude --caps shell      # prints claude's token
python -m axiom1 --db axiom1.db revoke-agent claude                # that token stops working
```

```json
{
  "mcpServers": {
    "axiom1": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp",
      "headers": { "Authorization": "Bearer axm_..." }
    }
  }
}
```

Why a hub and not a shared file: agents never see the database, so an agent with a shell cannot
open it and write `witnessed` around the rules, or start a server under someone else's name.
`tests/test_hub.py` has agents trying to act as each other, with revoked and reissued tokens, and
checks that no token appears in the database file.

### stdio (local development)

Each agent runs its own server process on a shared database file, named by its launch environment.
That trusts whoever launches the process, so keep it to your own machine.

```json
{ "mcpServers": { "axiom1": { "command": "python",
    "args": ["-m", "axiom1", "--db", "/path/to/axiom1.db", "serve"],
    "env": { "AXIOM_AGENT": "claude", "AXIOM_CAPS": "shell" } } } }
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
AXIOM_TOKEN=axm_... python -m axiom1 agent --id nemotron-1 --workspace ../wt-nemotron-1 --hub http://127.0.0.1:8765/mcp
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
