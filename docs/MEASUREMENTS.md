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

## Calendar on Bee (local, $0): engines, a manual, then form mode (2026-10-01)

Same 5 inboxes (seeds 100-104) throughout. Bee = Radeon 890M iGPU, Ollama. Thinking off
(`reasoning_effort: none`; with thinking on, Ornith spent whole 4096-token replies deliberating and was
cut off before any tool call).

| run | model | witnessed | model calls | tokens | wall |
|---|---|---|---|---|---|
| agent, engines, function names only | Ornith 1.5 35B-A3B | 0/2 (stopped) | 80 | 0.90M | 16 min |
| agent, engines + manual | Ornith 1.5 35B-A3B | **5/5** | 58 (34, 21, 1, 1, 1) | 0.86M | 17 min |
| form mode | Ornith 1.5 35B-A3B | **4/5** | **7** | **14k** | 9 min |
| form mode | ornith:9b | 1/5 (4 refuted) | 13 | 28k | 12 min |
| form mode | qwen3:1.7b | 0/5 (none delivered) | 15 | 28k | 2 min |
For reference: Lightning 0/5 and Super 0/5 on Nebius, without engines.

- **The manual is what made the agent work.** With only the list of function names, the agent never got
  to a delivery (inbox 1: 40 steps, no claim). With each engine's guide (how to call it, what a refusal
  means, what it combines with) it solved inbox 1, repaired the process once, then replayed it.
- **Form mode does the job in one call:** 4 of 5 witnessed at 1 call each with no corrections, 60x fewer
  tokens than the agent. The one miss: a move and a cancel linked to meetings the form did not have.
- **Small models did not hold up on this form.** Every miss by the 9B and the 1.7B is one of two kinds:
  copying one email's date or time onto another email's line (the copied-text check catches it) and
  linking a move or cancel to the wrong meeting (the ledger refuses, naming the key). The 1.7B also
  stopped submitting forms after corrections. Nothing wrong was accepted: the 9B's four wrong
  calendars were all refuted by the hidden check.
- So on this form, size mattered. But the failures are about the form's shape (nine emails in one list,
  meetings linked by retyped names), which is the next thing to change, not a verdict on small models.

## The presentation, not the model: one email at a time (2026-10-01)

Adrian: *"so then its how the task is presnted to the small model"*. Same 5 calendar inboxes, Bee, thinking
off. Each version changed only how the task was shown to the model, one failure at a time, each found by
reading the model's actual answer (rows keep answers and truth from v5 on).

| version | change | qwen3:1.7b | ornith:9b | Ornith 35B |
|---|---|---|---|---|
| whole-inbox form | all 9 emails in one form | 0/5 | 1/5 | 4/5 |
| v4 | one email per call; replies pick the earlier email from a menu; titles from subjects | 0/5 | | |
| v5 | blank fields; a move's duration only if that email states one | 2/5 | | |
| v6 | the whole job is background, "your only part: this one file" | 1/5 (partial) | 1/2 (partial) | |
| v7 | the email's own dates/times/durations are the menu; labels about agreeing | 0/4 (partial) | | |
| v8 | the model copies "my reply" | 0/4 (partial) | | |
| **v9** | **the harness reads who wrote what; the agreed meeting with the same subject is marked** | **4/5** | **4/5** | **5/5** |

v9 per model: 1.7B 49 calls, 40k tokens, **20.5 s per inbox**; 9B 46 calls, 48k tokens, 94 s; 35B 51 calls,
52k tokens, 75 s. The open-ended agent with the manual: 5/5, 856k tokens, 3.5 min per inbox.

- **A 1.7B model ties the 35B on the form and runs 4x faster.** It went 0/5 on every version until the
  presentation stopped asking it to do what is not reading: keeping nine emails straight, retyping names,
  reformatting times, telling who wrote a line.
- **What each model still misses is a reading.** 1.7B: "Sorry, I can't make that" filed as agreed. 9B: a
  cancellation filed as not_agreed (the context said "My reply: none", and a cancellation needs none: the
  label should say so). Both were refuted by the hidden check; nothing wrong was accepted, in any version.
- **The 35B made more presentation slips than the 1.7B** (converting a date to 2025-05-16, a time in the
  date field); the menus caught them as corrections.
- Every version's fixes were found in the model's own output. Several were the harness being pedantic
  (spacing, a field that did not apply, a field left out), not the model being wrong.

## Fresh seeds, and the one decision that does not shrink (2026-10-01)

v9 on seeds 500-504 (never used while tuning): qwen3:1.7b 3/5, ornith:9b 0/3. Every miss was the agree/decline
reading inside the 4-way kind (1.7B: "Sorry, I can't make that" as agreed; 9B: "Sounds good" for a school play
and a car service as not agreed). v10 asks it alone and first: said_yes yes/no, with my reply quoted (asked only
when I replied); the harness derives the kind from it.

| v10, seeds 500-504 | witnessed | calls | s/inbox |
|---|---|---|---|
| qwen3:1.7b | 3/5 | 45 | 21 |
| ornith:9b | **5/5** (v9: 0/3) | 73 | 140 |
| Ornith 35B | **5/5** | 45 | ~70 |

- The 9B's misreadings all went away when the decision stood alone. It spent extra rounds writing the reply
  text into said_yes; the field's name invites copying.
- **qwen3:1.7b cannot make this decision.** It answered said_yes = yes for every email, including "Sorry, I
  can't make that". Asked the bare question with nothing else in the prompt, it said "yes" to "I can't, sorry."
  This is a limit of the model, not of the presentation: everything else in the calendar form it now does.
- So the unit to size is the decision, not the job: kind, links and menu picks on the 1.7B; yes/no on a reply
  from a frozen table of witnessed answers, else a model that can read it. That is the mesh's routing
  (tier 0 table / tier 1 small / tier 2 large), applied per decision. Not built yet.
- Seeds 500-504 informed v10 (the 1.7B's decline error was also seen on the tuning seeds); a confirmation on
  untouched seeds is still owed.

## Routing per decision: the yes/no goes to a table, the rest to a 1.7B (2026-10-01)

`axiom1/router.py`. Calendar with my replies worded 40 ways (20 yes, 20 no, incl. "I can't wait!", "Can't say no
to that", "I wouldn't miss it"; truth unchanged). The form on qwen3:1.7b; "is my reply a yes?" routed to a frozen
table, else ornith:9b. An answer is frozen only after its inbox is witnessed. 20 inboxes in sequence (seeds
700-719), one table throughout. `2026-10-01_routing_curve.json`, table `..._routing_curve_table.json`.

| inboxes | 9B calls per inbox | yes/no answered by the table |
|---|---|---|
| 1-5 | 7, 4, 3, 4, 3 | 40% |
| 6-10 | 3, 2, 1, 1, 2 | 74% |
| 11-15 | 0, 1, 3, 1, 0 | 86% |
| 16-20 | 0, 0, 1, 0, 1 | **94%** |

- **19/20 witnessed, 0 wrong accepted.** 140 yes/no decisions: 37 needed the 9B, 103 came from the table (32
  entries). The 1.7B did every other decision: 181 calls, ~9 per inbox, 26.9 s per inbox.
- The calls fall toward zero and only a wording not seen before reaches a model: the mesh's routing result
  (frozen table / small model / large model), per decision.
- **The one miss was not the routed decision**: every yes/no in it was right, including "Can't say no to that".
  The 1.7B pointed "Cancelled: Coffee with Sam" at the landlord call, with the coffee meeting marked "same
  subject" above it. A refuted inbox freezes nothing, so the table stayed clean. Now caught without the hidden
  check: a move or cancel that points away from the agreed meeting with its own subject gets a correction.
- Caveat: synthetic replies, 41 wordings; real mail has a longer tail, so the curve flattens above zero.

## On Nemotron, on Nebius: 20/20 (2026-10-02)

The routed calendar from the Bee section, moved to Nebius Token Factory with nothing else changed: the same 20
varied inboxes (seeds 700-719). The form on **Nemotron 3 Nano** (30B, 3B active); "is my reply a yes?" from the
frozen table, else **Nemotron 3 Super**. `2026-10-02_nebius_routing_curve.json`.

Choosing the tiers was measured, not guessed (`2026-10-02_yes_no_*_by_model.json`): with the email in the question,
Super 42/42, Nano 41/42 ("No problem at all, see you." -> no), Lightning 39/42. A wrong answer that is still a
valid option does not fall through to the next tier, so the yes/no goes to the one model with no errors.

| | Nemotron (Nebius) | Bee (qwen3:1.7b + ornith:9b) |
|---|---|---|
| witnessed | **20/20** | 19/20 |
| corrections needed | **0** | 0 |
| form calls (one per email) | 180 | 181 |
| yes/no from the table, by fives | 43% · 74% · 86% · **94%** | 40% · 74% · 86% · 94% |
| large-model calls for yes/no | 36 of 140 | 37 of 140 |
| seconds per inbox | **12.5** | 26.9 |

Nano used 196.5k tokens for all 20 inboxes (~9.8k per inbox); Super 17.8k tokens for its 36 calls.

**What it took to get there.** The first Nebius run refuted two inboxes: the routed question asked about my reply
without the email, and Super read "No problem, moved." as a "no" to an invitation (8/8); told it answered a move,
"yes" (4/4). A reply means something only next to what it answers, so the question now carries the email. The
Bee curve ran on the old question; ornith:9b happened not to fail on it.

## Who writes the check? Planting every mistake, with no answer key (2026-10-02)

`examples/who_checks.py`, no model. A real user has no hidden check, so every plausible single mistake (the
kinds models made today) was planted into otherwise-right per-email answers, 30 inboxes (seeds 900-929), and run
through the production layers in order. `2026-10-02_who_checks.json`.

| mistake | menu | rule | fixed | reaches the user |
|---|---|---|---|---|
| move / cancel linked to the wrong meeting | | 240 | | 0 |
| cancellation filed as not agreed | | 30 | | 0 |
| move filed as a new meeting | 30 | | | 0 |
| a move's yes read as no | | 30 | | 0 |
| agreed filed as not agreed; length left out | | 300 | | 0 |
| decline / newsletter filed as agreed | | | 60 | 0 |
| **yes/no on an invitation read wrong (the router)** | 60 | | | **120** |
| **all 870** | 90 | 630 | 60 | **120** |
| *(rerun with "invitation filed as a move" added, after the consensus run found it: 150/150 stopped; all 1,020: 120 reach the user, the same 120)* | | | | |

- **Everything except one decision is checked by the harness itself.** What reaches the user is a wrong
  yes/no on an invitation: the rules trust that answer, so nothing downstream can contradict it. That decision
  is measured on its own: Super 42/42, Nano 41/42 (with the email in the question).
- One rule was added because of this run: a cancellation filed as unrelated mail used to reach the user; now an
  email whose subject matches an agreed meeting must be a move or a cancel.
- Correct answers pass every layer on all 30 inboxes (asserted).
- **Production consequence:** the router's freeze-on-witnessed has no witness in production. There, a reply's
  answer should be frozen when two independent resolvers agree (Nano and Super) or the user confirms it once,
  and a disagreement goes to the user instead of being guessed. Not built yet.

## Production mode: two models must agree, else ask the person (2026-10-02)

The router with no answer key (`consensus=True`): on a wording the table has not seen, Nano and Super both answer
"is my reply a yes?"; agreement is frozen; a disagreement goes to the person as one tap (simulated here by someone
who knows their own replies). The form on Nano. 20 inboxes never used before (seeds 720-739), empty table.
`2026-10-02_consensus_curve.json`.

- **19/20 witnessed, 0 taps, 0 wrong answers frozen** (all 33 table entries checked against the true meanings).
  Nano and Super agreed on every new wording, including the trap Nano gets wrong alone ("No problem at all, see
  you." -> agreed yes; on its own, Nano said no). The table answered 43%, 86%, 86%, 91% of yes/no by fives.
- Cost of consensus: 33 calls each to Nano and Super for 140 yes/no decisions (12.6k + 16.3k tokens). The form:
  Nano 180 calls, 196k tokens, 0 corrections, 18.1 s per inbox.
- **The one miss was on the form**: Nano filed a new invitation ("Call with the landlord") as a move of the
  dentist appointment. Fixed from structure: a move or cancel must be about an agreed meeting with the same
  subject. Planted 150 times since: 150 stopped.
- What a real user carries now: two different Nemotrons agreeing on a wrong yes/no (not seen in 140), and form
  mistakes no rule yet covers (each one found so far has become a rule).

## A realistic inbox: threads, relative dates, and what to ask about (2026-10-02)

`examples/real_inbox.py`. ~10 threads per inbox, each a conversation with From/Date headers: an invitation I
accept or decline, no reply, a move (accepted, or turned down: "let's keep the original time"), a cancellation, a
forward, a group thread (with and without my reply), a newsletter, a time I proposed that they confirm, and two
kinds to ASK about rather than guess ("sometime next week", no time given). Days are relative ("next Thursday",
"the 12th", "tomorrow") and read from the day each message was sent (`time.resolve_date`, `mail` engine).
The truth lists the calendar AND the threads to ask about. A correct solver passes 40/40 inboxes.

Iterated on Bee (qwen3:1.7b slots, ornith:9b agreement), each change from a model's own output: v1 0/5 ->
v5 9/10 (see the commit). Then **on Nemotron via Nebius, 20 inboxes never used in tuning (seeds 101-120):**

| | |
|---|---|
| witnessed | **20/20** (200 threads, 0 wrong accepted) |
| asked, not guessed | **29/29** expected asks, no others |
| "agreed?" answered by | table 132, structure ("I never wrote") 32, Nano+Super agreed 24, **the person 12** |
| slot form | Nano: 15 calls for 200 threads (single-option menus are filled by the harness), 0 corrections |
| time | 4.8 s per inbox |

- **All 12 taps were the same disagreement: Nano "yes", Super "no", Super right** - 9 cancellations ("So sorry
  - I have to cancel our lunch") and 3 declines. Consensus sent Nano's misreadings to the person instead of the
  calendar. They did not taper because each cancellation names its meeting, so each was a new wording.
- Next: key the table by the message with the thread's subject taken out (one tap would teach every
  cancellation), and split Nano's question ("did I say yes?" / "cancelled or turned down since?").
- The first Nebius attempt was spoiled by the simulation, not the system: the stand-in person answered by the
  meeting's title, and two threads shared one. It now answers per thread.

## The same 20 inboxes after splitting the decision: 12 taps -> 3 (2026-10-02)

Agreement split into its two smallest decisions ("does the first answer say yes?", shown only the thread up to
that answer; then, only if messages followed, "Is the meeting cancelled?"), table keys with the meeting's name
replaced by <meeting>, and no freezing from a single model. Same seeds 101-120, Nano slots, Nano+Super agreement.
`2026-10-02_real_inbox_nebius_v2.json`.

| | one question | split |
|---|---|---|
| witnessed | 20/20 | **20/20** |
| asked, not guessed | 29/29 | **29/29** |
| taps | 12 | **3** |
| decisions from the table | 132 | 159 |
| seconds per inbox | 4.8 | 6.7 |

- The 3 taps: Nano "no" to "Sounds good." and Nano reading two move requests ("could we move it to Thursday at
  9:00 am instead?") as cancellations; Super right each time. The move requests did not teach each other because
  each names its own day and time: removing those from the key, as the meeting's name now is, is the next step.
- Found on Bee on the way: the plain question "Is the meeting cancelled?" (21/21 on moves, turned-down moves,
  cancellations) beat a longer one with a clause about moves (the 9B said no 4/4 on a cancellation); and one
  model's frozen answer spread under the more general keys until the router stopped freezing single-model answers.
- **Then day, time and length phrases left the key too** ("could we move it to <when> at <when> instead"): same
  20 inboxes, **20/20, 29/29 asks, 0 taps**, 4.2 s per inbox (`2026-10-02_real_inbox_nebius_v3.json`). The
  models are not perfectly repeatable run to run (the split run's 3 taps were Nano misreadings that did not
  recur), so read 0 as "a handful at most", not a guarantee.

## Real email: 50 Enron threads (MailEx), on Bee (2026-10-02)

The calendar on real mail for the first time: 50 threads from MailEx (CC BY-SA 4.0), their dates recovered from the
original Enron corpus, the final calendar state keyed by hand (`docs/data/mailex_calendar_key.json`, **read by
Claude, not yet spot-checked by Adrian**). Same `decide_thread()` as the generated inbox; qwen3:1.7b fills slots,
ornith:9b answers the yes/no decisions, temperature 0, thinking off. Scored by harm.
`python examples/mailex_eval.py --model qwen3:1.7b --agree ornith:9b`

| | v1 (first run) | **v7 (final)** |
|---|---|---|
| right | 40/50 | **43/50** |
| **wrong meeting on the calendar** | 3 | **0** |
| wrong time | 0 | **0** |
| missed (should have been placed or asked) | 4 | 3 |
| asked when it need not have | 3 | 4 |
| taps (questions to the person) | 0 | 0 |

What real mail has that the generated inbox did not, and what changed:
- **Threads that mention a meeting without arranging one for me** (a notice sent for my boss, "IF they are free",
  a meeting that already happened): a routed `is_meeting` decision, asked only of threads with a day or time in
  them; a thread with none is settled by structure (23 of 50, zero model calls).
- **The day and the time in different messages**: each is chosen from its own menu, with "not settled" as an
  answer. "Which proposal stands" (first or latest) is asked only for an offer and one counter-offer: with three
  or more, "the latest" was someone else's "I have a 10:00 AM meeting" and an unanswered "3 or 3:30?" (v6: 1 wrong
  on the calendar, 1 wrong time).
- **No stated length**: the meeting goes on with its end left blank, not a guessed length.
- **The router passed off-menu answers through.** A frozen answer learned on one thread ("the 14th") was used on a
  thread that never says it, and a model's "Tuesday" for the option "next Tuesday" landed a week early. Every
  vote and every table hit is now checked against the current menu (`Router.on_menu`). The 0 wrong on the
  calendar comes from this check: no `is_meeting` wording stopped v1's wrong placements.

**The `is_meeting` wording was chosen on all 103 threads that reach it, not on a probe** (`examples/is_meeting_wording.py`,
`2026-10-02_is_meeting_wording.json`):

| wording | generated kept (must be yes) | real meetings kept | real "off" said no |
|---|---|---|---|
| **"Is this thread arranging a meeting or call that I will attend? (No if ...)"** | **76/76** | **8/8** | 9/19 |
| "...trying to put something on my calendar..." | 65/76 (drops every forward) | 5/8 | 13/19 |
| "...that I am being asked to go to?" | 68/76 (drops vague ones) | 4/8 | 14/19 |
| "...that I might go to?" | 27/76 | 7/8 | 14/19 |

Generated inbox on Bee, same code (10 inboxes, seed 1): **7/10, the same as before these changes** (v8 -> v13). The
3 refuted are all the same 9B misreading: "I can't wait!" answered "no" to "does message 2 say yes?", 8 times out
of 8 at temperature 0. Nemotron Super answers it correctly (the 20/20 runs above).

**Runs not to quote:** mailex v2/v3 and real_inbox v9/v10 ran with thinking ON (Ollama's /v1 needs
`AXIOM_THINKING_SWITCH=reasoning_effort`): the 9B spent its answer budget thinking and returned nothing, every
empty vote went to the simulated person, who answers from the key, so v2/v3's 47/50 is inflated. v3/v10 also ran
with half the edits applied. v4 (42/50, 0 wrong) and v5 (43/50, 0 wrong) are valid steps; v6 is the regression
described above. A diagnosis drawn from the thinking-on runs ("meeting or call" drops appointments) was wrong too,
which is how the wording table above came to be measured.

## Reminders: finalize, confirm, and the phone call (2026-10-03)

An unclear thread used to have three outcomes: on the calendar, a question now, or nothing. Now a question is a
**reminder** of one of two kinds, carrying a **tentative hold** for whatever is settled. Holds are never written to
`calendar.csv`; a hold on the wrong day is scored as harm (`WRONG_HOLD`).
- **finalize**: the people have not finished deciding. A field is "not settled" (steffes: "how does 3 or 3:30
  look?", never answered -> hold Tue 2001-10-30, missing: what time), or **the arranging moved to a phone call**.
- **confirm**: a decision nobody could make (models disagree, no person to ask). Before this, an undecided "is this
  a meeting?" or "did we agree?" was **silently dropped** as "no".

Why the phone call: part of why AI misreads email is that a call breaks the timeline. What was decided on it never
comes back into the thread, which then reads as unfinished, or resumes already decided. 21 of the 50 threads
mention a phone, but the mention alone is noise (signatures, "call me" about something else). The signal is a
hand-off while arranging ("Please give me a call and let me know if any of these work", "left a message with his
secretary", "Have you tried reaching me on my mobile phone?").

Same 50 threads, Bee, `2026-10-03_mailex_bee_v8.json`:

| | v7 | **v8 (reminders)** |
|---|---|---|
| right | 43 | 40 |
| **wrong meeting on the calendar** | 0 | **0** |
| **hold on the wrong day** | - | **0** |
| missed | 3 | **2** (ybarbo now caught by the hand-off) |
| reminder not needed | 4 | 8 |

- The 5 threads keyed "ask" that were caught all became `finalize`, 3 with the right day held.
- **The cost:** 4 new reminders that were not needed, all from the hand-off pattern matching a sign-off ("If you
  have any questions, please do not hesitate to give me a call"). Not harm (a reminder does not block or place
  anything), but noise; the pattern should require the call to be about the arranging. Some keyed "off" may
  honestly be reminders (blair-l_inbox_66: dates offered, then "give me a call") - the key is Adrian's to revise.
- Generated inbox unchanged: 7/10 (`2026-10-03_real_inbox_bee_v14.json`), the same three "I can't wait!" misses.

## Sign-offs, "3:00", and a gate that failed (2026-10-03)

Two fixes, same 50 threads, Bee (`2026-10-03_mailex_bee_v9.json`):
- **A call counts as a hand-off only when it is part of the arranging**: the same message names a day or time or
  talks about meeting. The three sign-offs ("If you have any questions, please do not hesitate to give me a call")
  no longer make reminders; ybarbo is still caught.
- **A bare "3:00" in email is 3 in the afternoon** (hours 1-6 without am/pm). It had been held as 03:00.

| | v8 | **v9** |
|---|---|---|
| right | 40 | **43** |
| wrong meeting on the calendar | 0 | **0** |
| hold on the wrong day | 0 | **0** |
| missed | 2 | 2 |
| reminder not needed | 8 | **5** |

Generated inbox unchanged: 7/10 (`2026-10-03_real_inbox_bee_v15.json`).

**Tried and rejected: a model gate before every reminder ("is anything left for me to do?").** Asked of 23
threads that need a reminder and 5 that do not (`examples/followup_wording.py`, `2026-10-03_followup_wording.json`),
the 9B kept **0/23** with "still waiting on me" and "something I need to do", 9/23 with "still being arranged". It
says "no" to nearly everything: whether something is owed is an obligation judgement, not a reading of the email,
and the gate would silently lose real meetings. The 5 needless reminders left come from one model's "is this a
meeting?" or "was it called off?"; the fix for that is a second model (Nano and Super must agree, else the person),
not another question to the same one.

## Filed, and a timeline (2026-10-03)

Every thread is now filed whole (folder = its topic without Re:/Fwd:, or the people in it), and every message is an
event: a day or time named, a hand-off to a call (a gap: what was decided there is not in the email), or anything
else. The outcome sits at the end with the receipt of what decided it. No model in this path
(`examples/inbox_timeline.py`, tests 7/7; with the hand-off check sabotaged, 2 go red).

On the 50 threads with v9's outcomes: **37 filed and nothing more**, 11 reminders, 2 on the calendar, 5 call gaps.
Every folder holds one thread here, because MailEx draws each thread from a different mailbox; grouping by topic
needs one person's inbox.

The page: `web/timeline/` (3D; depth is when a thread started, left to right is time inside it, a call is a break
in the line). Measured with the lint engine's own script at 360/768/1280/1920 px: 0 findings, 0 contrast failures.

## Follow-up needed: the open-ended threads get their own section (2026-10-03)

Adrian's ruling on the key: most open-ended threads "will be handled by a call", so they are not reminders and not
nothing; they need their own section. The rule is structural:
- **reminder (finalize)**: something is settled (a day or a time is held) and one named piece is missing
- **follow-up needed**: open-ended. Nothing is settled, or the arranging moved to a phone call
Key rulings (Adrian): 5 threads -> follow-up (blair-l_66, watson-k_471, shackleton-s_677, ybarbo-p_214, wolfe-j_436);
3 -> nothing (tycholiz-b_368, heard-m_118, reitmeyer-j_52). The scorer now also checks the section (`wrong_section`).

Same 50 threads, Bee (`2026-10-03_mailex_bee_v10.json`):

| | v9 | **v10** |
|---|---|---|
| right | 43 | **45** |
| wrong meeting on the calendar | 0 | **0** |
| hold on the wrong day / wrong section | 0 / - | **0 / 0** |
| missed | 2 | 2 |
| reminder not needed | 5 | **3** |

4 of the 5 open-ended threads land in follow-up; wolfe-j_436 is dropped by one model's "is this a meeting?". The 3
reminders not needed are the 3 Adrian ruled "nothing", each from a single model's yes. Generated inbox unchanged:
7/10 (`2026-10-03_real_inbox_bee_v16.json`).

**Whole mailboxes, first look (structure only, no model):** one person's mailbox (Steffes, regulatory affairs):
3,331 messages -> 2,130 threads; **76% need no model call at all** (no day or time named, or I am not in it); 508
threads would reach the first yes/no question. Loader `examples/mailbox.py`, runner `examples/mailbox_run.py`
(resumable; production mode: no key, no stand-in person, an undecided decision becomes "confirm").

## Dates as real mail writes them, and invitations nobody answered (2026-10-03)

- **Dates by month and by number** ("July 18th", "Nov. 13", "11/20"): the date engine already resolved them, but
  the detector did not see them. Found in whole Enron mailboxes. "1/2 hour" and "1/2 of the volume" stay ignored;
  a past date is refused, not guessed.
- **Invitations never answered in email go to follow-up** (Adrian's call): "is this a meeting for me?" is now
  asked of every thread that names a day or time, mine or not. Mine and agreed -> the calendar path as before; not
  mine but a meeting for me -> follow-up needed ("invited, no reply in email - probably answered on a call or by a
  calendar click"). Never on the calendar: I did not agree in writing. In the generated inbox, an unanswered
  invitation and a group invitation I stayed silent on are now follow-ups; the newsletter stays filed.

Same 50 threads, Bee (`2026-10-03_mailex_bee_v11.json`): **0 wrong on the calendar, 0 wrong holds, 0 in the wrong
section**; 43/50 right (was 45). The 2 new misses are the rule's own cost, both threads the owner never wrote in
that the 9B read as an invitation for him: family plans he was not part of, and an invitation already declined on
his behalf (by an assistant). Generated inbox: 7/10 (`2026-10-03_real_inbox_bee_v17.json`), the same three "I can't
wait!" misses; every unanswered and group-invite scenario lands in follow-up.

**Whole mailboxes (structure, deduplicated):** five employees, 9,124 threads, **82.7% need no model call**. Lay
(CEO) 90.7%, Haedicke (general counsel) 87.2%, Giron (trading) 79.2%, Heard 75.1%, Steffes (regulatory) 74.6%. The
CEO's sent mail is written by his assistant. Two loader fixes the numbers depended on: Enron files one email in
`all_documents` and `sent` under different message ids (1,345 duplicate pairs in Haedicke alone), and keeping only
one copy flipped the owner to another alias (his own threads 343 -> 49) - every folder a copy was filed in is kept.

## Four whole mailboxes (2026-10-03)

The harness over four Enron employees' entire mailboxes, as it would run for them: no answer key, no stand-in
person (`examples/mailbox_run.py`, Bee: qwen3:1.7b slots, ornith:9b decisions). Every thread filed; the rest:

| employee | threads | filed only | follow-up needed | reminder | on calendar | call gaps |
|---|---|---|---|---|---|---|
| Heard | 890 | 875 | 14 | 0 | 1 | 75 |
| Giron (trading) | 1,373 | 1,314 | 57 | 0 | 2 | 51 |
| Steffes (regulatory) | 2,130 | 1,997 | 113 | 8 | 12 | 96 |
| Haedicke (general counsel) | 2,537 | 2,360 | 170 | 2 | 5 | 120 |
| **all four** | **6,930** | **6,546 (94%)** | **354** | **10** | **20** | **342** |

0 pipeline errors. Per thread: `docs/measurements/mailboxes/<box>.jsonl`; the timeline (folders, events, call gaps,
outcomes with receipts) in `<box>.timeline.json`. No key exists for whole mailboxes, so these are counts, not
scores: correctness is the 50-thread set above (0 wrong on the calendar); here the claim is what one person is
left with. Open for review: one of Heard's calendar entries ("meeting regarding master netting agreements") is the
topic of a thread ruled "nothing" in the 50-thread set; some follow-ups are company-wide announcements.

**A bug of mine, caught before anything was committed:** the runner named threads `t00001`, the pipeline reads only
`threads/*.txt` and refused, and every thread that should have become a calendar entry, reminder or follow-up fell
back to "filed" (Heard 16, Giron 60), showing only as "890/890 filed". The 20-thread smoke run had passed because
none of its threads reached the pipeline. Fixed; a pipeline failure now prints and stops the run.
