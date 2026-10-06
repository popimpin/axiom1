# A decision harness  ·  working name: ______

**Agent harnesses let the model run the process. This one runs the process itself and asks a model only for the
decisions it cannot make — and nothing counts until it is checked.**

| | agent harness | decision harness |
|---|---|---|
| who runs the process | the model: plans, picks tools, loops | the harness; a proven process is locked and replayed |
| what the model does | generates the work | makes the smallest decisions, from menus |
| who does the work | the model, in code or text | **engines**: tested tools that refuse instead of guessing |
| what is remembered | whatever was said | only what was proven: checked, or agreed by two models |
| when it is unsure | it guesses | it asks the person, once per wording |
| over time | about as costly on run 100 as on run 1 | **cheaper and more reliable**: proven decisions stop needing a model |

## The four parts

**1. Engines — the model operates tools; it does not do their work.** Dates, times, money, matching, merging,
tables, a calendar ledger, mail threads: one tested, self-describing module each, standard library only. An engine
refuses rather than guesses ("Thursday May 8" when the 8th is a Friday, "1.234,50", "sometime next week"). A
contract checks every engine before it may join; its manual is generated from its own worked examples.

**2. The smallest decisions — presentation decides what a small model can do.** A job is broken into one decision
per item, each shown only what it needs, with closed menus of what the input actually says. What is structure —
who wrote a message, whether I replied at all, a menu with one real option — is read by the harness, not asked.

**3. A shared memory of proven decisions.** Each decision is routed to the cheapest resolver that gets it right:
a frozen table of proven answers → a small model → a larger one → the person. An answer is frozen only when it
is proven: the delivery was checked, or two *different* models agreed. One model agreeing with itself proves
nothing. Every agent using the harness inherits what any agent proved, plus lessons from failures and a track
record per agent.

**4. The handoff as a ledger (next).** Long tasks and changes of agent do not drag the whole history forward.
*Done* comes from what was checked (commits, witnessed claims), *left to do* from the task's own plan, and a
small model only **selects** the context that matters for the next step — it never writes a summary.

## The rules, each with the number that showed it

1. **The harness owns the process.** A process proven once is locked and replayed: 0.4 model calls vs 29, 6.5 s vs 101 s.
2. **Engines, not generation.** Without them a 120B model scored 0/5 on a calendar; with engines and their manual an agent scored 5/5.
3. **Smallest decisions, shown only what they need.** One email per call took a 1.7B model from 0/5 to 4/5, tying a 35B. Showing only the thread up to a reply took a 9B from 5/10 to 9/10.
4. **Read structure, don't ask.** "Did I reply?", "who wrote this?", single-option menus: zero model calls, and the mistakes they used to cause are gone.
5. **Route each decision to its cheapest proven resolver.** On Nemotron (Nebius), the share of decisions answered from the table rose from 43% to 94% over 20 inboxes, and big-model calls fell to zero.
6. **Freeze only what is proven.** With no answer key, two Nemotrons had to agree: 0 wrong answers frozen. One model's frozen mistake, once allowed, spread to every matching thread.
7. **Refuse, don't guess; nothing is done until checked.** On realistic email: 29/29 unclear threads asked, none guessed; across every run, 0 wrong deliveries accepted.

## The result, on realistic email (NVIDIA Nemotron on Nebius)

Threads with headers, relative dates, forwards, group replies, moves, cancellations, newsletters, and threads that
need asking. 20 inboxes never used in tuning: **20/20 correct, 29/29 asks right, 0 questions to the person,
4.2 s per inbox.** Nemotron 3 Nano does the work; Nemotron 3 Super is consulted only where Nano can be wrong.

## Why the handoff is next, and why it is built this way

The same design has run our own work for two months as a shared memory across three AI agents and a person: 43
session handoffs, every session starting from one. It works for continuity. Its failures were all handoffs
written as prose: a "scheduled" note outranked the live work; a handoff lagged the repository it described; a note
from one agent never reached another. A ledger built from checked state, with a small model selecting from it,
removes each of those failure modes.

## Setup: done together with the mail client

Axiom-1 reads the inbox a person's mail setup leaves, and the two are set up together. On raw Enron mailboxes,
which nobody had curated, a blind audit put 57.6% of the short list right; most of the rest was junk the setup is
meant to handle. Setup is three things:

1. **Folders and filters in the mail client** route newsletters, marketing, and company-wide notices away from the
   inbox. Axiom-1 does not try to recognise junk; in the audit, junk read as an invitation was the largest error.
2. **The address book.** Mail from outside the organisation, from someone the owner has never written to, is
   filed and flagged before any model reads it - a stranger cannot put anything on the owner's lists. Today the
   address book is the owner's sent mail, and that misses people the owner deals with through an assistant or by
   phone: on the audited mailboxes it filed 5 correct calendar entries from outside counsel and board contacts.
   Setup has to add those contacts and partner domains. (Not wired yet: the rule reads sent mail only.)
3. **Who "me" is.** An executive's mail is often sent by an assistant; the assistant's address belongs to the
   owner, or replies they sent read as unanswered invitations.

## Honest limits

- The audits are on raw, uncurated Enron mail; a configured setup is designed, not yet measured.
- Each job needs its own decisions and pipeline written once (seven exist besides email).
- Two models agreeing on a wrong answer **has been seen**: on 2026-10-05 Nano and Super both called a generated
  newsletter a meeting, and the stored answer was replayed on eight inboxes. A stored answer should be replayed
  only after a check, not because two models agreed - the next fix.
- The handoff ledger is designed, not built.

Every number above is reproducible from `docs/MEASUREMENTS.md` and the files in `docs/measurements/`.
