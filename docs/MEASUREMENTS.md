# Measurements

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
- **Open:** in some `auto` runs the agent received a skill yet thought on most calls (e.g. 10 of 13);
  in others the dial switched as designed (2 of 13). The dial's logic passes its tests; the cause in
  the live runs is not yet known.
- Prompt tokens dominate cost in this loop (median 54k-93k per run vs 1.2k-1.9k completion), because
  the transcript is resent on every call.
