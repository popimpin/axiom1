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

## Eight everyday job types, three agents each (2026-10-01)

Four more job types joined the first four: reconcile a budget against the bank statement, fill an
insurance claim form from a letter (leaving blank what it does not say), a calendar from an inbox with
moved, cancelled, declined and newsletter emails, and overdue invoices with per-client totals. Gold
solutions read only the visible files; all 8 types validate on six data seeds; 29 wrong-but-plausible
deliveries are each rejected for their own reason. Lightning, thinking auto, lessons, shell.

| run | done (verified) | first try | wrong caught | wrong accepted | quit without claiming | $/job |
|---|---|---|---|---|---|---|
| v1 `2026-10-01_everyday8_lightning_v1.json` | 11/24 | 8 | 13 | 0 | 6 | 0.0078 |
| v2 `2026-10-01_everyday8_lightning_v2.json` | **17/24** | 11 | 19 | **0** | **0** | 0.0132 |

Between them, one change: the harness, not the model, decides when a run is over. In v1, six agents
"thought out loud" in a message with no tool call and the runner took that as the final answer. v2
failures are the model's own limits: the calendar job 0/3, overdue invoices 1/3, contacts and the bank
reconciliation 2/3 each, all ending at the 40-step limit.

## Getting good at a job: one witnessed pass, then replay (2026-10-01)

`examples/everyday_tasks.py series`: the 8 job types, 5 instances each, fresh data per instance (seeds
100-104). Instance 1 is worked out by the model; a `process.py` that reproduces a witnessed delivery is
locked in. Later instances replay it (the model fills only the entry, if any); if the check fails, the
model repairs it, and the repair is locked only if it also passes every earlier instance. Lightning,
thinking auto, 40 steps. `2026-10-01_everyday8_series_lightning_v1.json`.

Paths per instance: M = worked out by the model, R = replayed, F = replay failed, model repaired; + verified, - not.

| job | verified | paths | final process | model calls |
|---|---|---|---|---|
| merge-contacts | 5/5 | M+ R+ R+ R+ R+ | v1 | 36 |
| overdue-invoices | 5/5 | M+ R+ R+ R+ R+ | v1 | 14 |
| reconcile-bank-statement | 5/5 | M+ M+ R+ R+ R+ | v1 | 52 |
| fill-claim-form | 5/5 | M+ F+ F+ F+ F+ | v2 | 67 |
| receipts-to-spreadsheet | 4/5 | M- M+ F+ R+ R+ | v2 | 91 |
| policy-answer-with-source | 4/5 | M- M+ F+ R+ F+ | v2 | 74 |
| tidy-downloads | 3/5 | M+ M- M+ M+ M- | none | 118 |
| calendar-from-inbox | 0/5 | M- M- M- M- M- | none | 186 |
| **all** | **31/40** | 19 M · 14 R · 7 F | | 638 |

- **A replay costs almost nothing.** 14 replays: mean **0.43 model calls, 6.5 s**. 19 model-worked
  instances: mean **28.9 calls, 101 s**. Five replays of merge-contacts and overdue-invoices made zero calls.
- **Replay turns the model's weak jobs into solved ones.** overdue-invoices was 1/3 for fresh agents in
  v2; here one pass at instance 1 locked a process and the other four replayed: 5/5. Bank went 2/3 -> 5/5.
- **The regression guard fired.** 4 repairs (3 on fill-claim-form, 1 on policy) were refused with
  "process not locked in: it breaks N of M earlier instance(s) the current version handles". Each of those
  deliveries was still accepted; only the method was refused. fill-claim-form never converged: every new
  letter strayed from v2, so every instance after the first needed the model (10-14 calls each, not 0).
  The series runner cut those notes at "(s)" (it took the last '('), so N and M are missing from these
  rows; fixed for later runs (`trailing_note`).
- **Two jobs never lock a process.** tidy-downloads succeeded 3/5 but the agent moved files by hand and
  left no `process.py` that reproduces it, so each instance starts over (2 hit the step limit, no claim).
  calendar-from-inbox is 0/5 as in v2: never a witnessed pass, so nothing to replay. It used **47% of all
  tokens** (3.69M of 7.78M).
- Cost: 7.36M prompt + 0.41M completion tokens, about **$0.54** (~$0.0135/job) at the prices implied by
  the earlier receipts (~$0.06/M in, ~$0.24/M out). Without calendar, about $0.29 for 35 jobs.
- Wrong deliveries accepted: 0 (only a server-witnessed claim counts as done).
- Small: one model, one seed range, synthetic data, 5 instances per type.

## A bigger model on the job Lightning cannot do: Super, calendar-from-inbox (2026-10-01)

`series --only calendar-from-inbox --learn-model nvidia/nemotron-3-super-120b-a12b`: Super works the job
until a process is locked, then Lightning replays. Same inboxes (seeds 100-104) as Lightning's 0/5.
`2026-10-01_calendar_series_super_learns_lightning_replays.json`.

**Super: 0/5.** Every instance used all 40 steps and ended with one refuted claim; nothing was locked, so
Lightning never ran. 2.11M prompt + 0.14M completion tokens, 15 minutes. A model ten times the size did
not get further on its own: the job as posed asks the model to write a parser (four time formats,
durations, moves and cancellations linked back to the original) and to get every row exact.
That is the case for engines (`axiom1/engines/`): the deterministic parts are written once and tested,
and the model only reads each email and says what it is.
