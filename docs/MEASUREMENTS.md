# Measurements

**Every number before 2026-10-01 comes from ONE trivial task** (fix `add()` in a two-file repo), repeated.
Those runs test mechanics, not usefulness, and they sit at ceiling. The sections from 2026-10-01 on use
real work: bugs from this repository's history, and everyday jobs a non-coder would hand off.

Every number here comes from a script in `examples/` and a results file in `docs/measurements/`.
Runs that turned out to measure the wrong thing stay in, labelled, so nobody re-quotes them.

## Does a sandboxed shell change how often an agent claims something false? (2026-09-30)

`examples/shell_ablation.py`. Model `nvidia/Nemotron-3_5-Lightning` on Nebius Token Factory. Task: fix
`add()` in a tiny repo. Checks in Docker, with held-out tests. Arms interleaved. 5 runs each.

### v2 (the one to quote)

`docs/measurements/2026-09-30_shell_ablation_v2.json`

| | with shell | without shell |
|---|---|---|
| fix witnessed | 5/5 | 4/5 |
| false claims before success | 0 | 1 (in 1 run) |
| median tool calls | 20 | 17 |
| median seconds | 32.8 | 23.5 |

With the shell the agent ran its tests 6-8 times per run and never claimed falsely. At n=5 the
difference from the no-shell arm (4/5, one false claim) is **not** significant, and the task is too easy
to separate the arms: both are near the ceiling. One no-shell run made no claim at all and used its 30
steps; not yet explained. One shell run took 341 s; not yet explained. A harder, multi-file task set is
needed before this says anything strong.

### v1 (do not quote: it measured a tool trap)

`docs/measurements/2026-09-30_shell_ablation_v1_TRAP.json`. Shell 5/5 witnessed, no shell 0/5 with 6
false claims. The split was extreme, so it was checked before being reported. Logged replays showed the
no-shell agent creating an empty FILE named `tests` with `write_file`, after which every write to
`tests/...` failed with a bare `FileExistsError` and the agent retried until its step budget ran out. The
shell arm escaped by deleting the file. Fixed for both arms (`write_file` names the blocking file;
`delete_file` added), then v2 was run.

**The larger effect was the fix, not the shell.** In live runs before it, the no-shell agent claimed a
fix before making it 4 times out of 4; in v2 it was witnessed on its first claim in 3 of 5 runs. One
clear error message moved the result more than adding a tool did.

## Do recorded failures make the next agent better? Lessons on vs off (2026-09-30)

`examples/lessons_ablation.py`. Lightning, no shell, fix `add()` with held-out tests in Docker. Three
sequences of four fresh agents per arm, the same job repeated, arms interleaved.

### v1: inconclusive, and the script had two flaws

`docs/measurements/2026-09-30_lessons_ablation_v1_INCONCLUSIVE.json`

| | lessons on | lessons off |
|---|---|---|
| witnessed | 9/12 | 10/12 |
| false claims | 2 | 2 |
| agents after the first: witnessed | 8/9 | 8/9 |

No measurable difference. Do not read it as "lessons do not help" either:

- **The script counted unanchored wins as wins.** A fix that starts from the agent's own commit is true
  but earns no skill. That is the likely reason the first sequence of both arms shows no lessons
  accumulating, but it is not verified: this run did not record anchoring. Fixed: only anchored wins
  count now, and unanchored ones are reported separately.
- **It never recorded whether an agent actually received lessons.** Fixed: `lessons_received`.
- **The task is at ceiling** (later agents 8/9 in both arms).
- **The commonest failure left no record at all.** In 4 of 24 runs the agent stopped without ever
  claiming. A failure that never becomes a claim produces no verdict, so it produces no lesson. For
  "every failure makes the next attempt easier" to hold, abandoned attempts have to be recorded too.

A replay of one sequence with the database inspected after each agent confirmed lessons and skills
are written on every verdict and are present for the next agent.

### v2: lessons x thinking, with abandoned attempts recorded (2026-09-30)

`docs/measurements/2026-09-30_lessons_x_thinking_v2.json`. Same task. Four arms, two sequences of four
fresh agents each, arms interleaved. Since v1: abandoned attempts leave lessons, only anchored wins
count, lessons received are recorded, and a thinking dial exists (`auto` = the harness turns thinking
off when a task arrives with a verified skill, back on after a refutation). Thinking is switched with
`chat_template_kwargs.enable_thinking`, the only control that turned Lightning's reasoning off.

| arm | witnessed | false claims | median seconds | later agents' median tool calls | calls thinking |
|---|---|---|---|---|---|
| lessons on, thinking auto | **8/8** | **0** | **23.1** | **13.5** | 66% |
| lessons on, thinking on | 8/8 | 3 | 24.2 | 15.5 | 100% |
| lessons off, thinking on | 8/8 | 2 | 27.9 | 14.0 | 100% |
| lessons off, thinking off | 7/8 | 4 | 29.8 | 19.5 | 0% |

- Lessons reached later agents (1-5 each) only in the lessons-on arms, as designed.
- Lessons + harness-decided thinking is the only arm with no false claim, and the fastest. No lessons
  and no thinking is the worst on every measure: with no memory, thinking matters on new work.
- **Small sample.** If all arms were equal, 0 of 8 runs with a false claim in one arm would happen by
  chance roughly 7% of the time. A signal, not proof.
- **Resolved (2026-10-01):** in some `auto` runs the agent received a skill yet thought on most calls
  (e.g. 10 of 13). Live per-call traces showed the dial worked as designed; the agents took the task
  late. Some explored first (`T:list_files T:read_file ... T:take_task f:write_file`), one only after a
  claim bounced off the lease check. The dial cannot act on a skill it has not seen. Fixed in the
  harness: the runner takes the task before the model's first call and puts it, with its lessons, in
  the opening message, so thinking is off from call 1 when a skill exists. A second interaction showed
  up in the same traces: a refuted task returns to the pool, the agent re-takes it, and the re-take
  (still carrying the skill) switched thinking straight back off. A refutation now keeps `auto`
  thinking for the rest of the run. **The v2 numbers above predate both fixes.**
- Prompt tokens dominate cost in this loop (median 54k-93k per run vs 1.2k-1.9k completion), because
  the transcript is resent on every call.

## Everyday jobs for people who will not check the result (2026-10-01)

`examples/everyday_tasks.py`. Four jobs asked the way a person would ask: receipts into a spreadsheet
for an accountant, tidy a downloads folder, merge two contact exports, answer a customer from the
policy documents quoting the exact sentence. Messy synthetic data. Each check is written once per task
type and derives the truth from the data; its feedback says what is wrong, never the answer. Every
delivery also passes the universal guards (nothing lost, well-formed CSV/JSON). Validated before use:
untouched data fails, a solver that reads only the visible files passes, and 14 wrong-but-plausible
deliveries are each rejected for their own reason.

Model `nvidia/Nemotron-3_5-Lightning`, thinking `auto`, lessons on, sandboxed shell, two agents per job.

| | |
|---|---|
| jobs completed, verified by the harness | **8/8** |
| completed on the first delivery | 3/8 |
| wrong deliveries caught before acceptance | **6** |
| wrong results accepted | **0** |
| median time per job | 36 s |
| cost per job | **$0.0073** (844k tokens in, 34k out, all 8 jobs) |

Files: `2026-10-01_everyday_lightning_v1.json` (receipts, downloads, policy) and
`2026-10-01_everyday_contacts_v2.json` (contacts).

- **The contacts task was first unsolvable, by our mistake.** Both agents "failed" it in v1. Three
  people appear only in the email export, whose phone column is blank, yet the check demanded their
  number. Validation had passed because the gold solutions were written from the hidden truth. Fixed:
  the check requires a phone only where a source has one (inventing one now fails), and every gold
  is a solver that reads only the files the agent sees. The old check, run against that solver, is
  refuted, so validation now rejects an unsolvable task. v1's contacts rows are excluded above.
- Small: 4 task types, 2 agents each, synthetic data. A demonstration that the loop works for a
  non-coder's errand, not a rate.

## Real bugs from this repository, Lightning alone (2026-10-01)

`examples/real_tasks.py`: 8 bugs we hit building Axiom-1, given as the symptom we observed, in a fresh
one-commit repo, judged by the real regression test (hidden). Validated: each holdout fails on the buggy
code for the bug's own reason and passes on the real fix.

`2026-10-01_real_code_tasks_lightning_pilot.json`: one agent per bug, 40 steps, the code-only
instructions of the time. **0/8 solved.** One claim, refuted ("the test still fails with the fix").
6 of 8 agents hit the step limit. 3.6M prompt tokens (about $0.22). The smallest model does not fix
real bugs in this codebase unaided; this is the baseline for larger models.
