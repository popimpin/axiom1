"""The calendar connector on the generated inbox: what reading the calendar does to the harness's output.

Enron has no usable calendar (its "calendar" folders are Outlook-migration stubs with no times or responses), and a
calendar must not be invented for real people. The generated inbox knows its own truth, so its owner gets a GENERATED
calendar, written as real .ics files (docs/measurements/calendar_demo/seed_N.ics):
  - every meeting the inbox agreed on is in it, accepted - except one in five, later moved in the calendar (it happens:
    rescheduled with a click, never in email), which the reconciler must flag as a conflict, not paper over
  - every invitation the owner never answered in email was answered (or not) in the calendar: accepted, declined, or
    left without a response, chosen with a fixed seed
Then the Nemotron outcomes (docs/measurements/2026-10-04_real_inbox_nebius_v4.json) are reconciled against it and
every change is checked against what the calendar says.

    python examples/calendar_demo.py [--receipt FILE] [--out FILE]
"""
import argparse
import json
import random
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

import real_inbox as R  # noqa: E402
from axiom1 import calendar_sync as CAL  # noqa: E402

ME = R.ME
OUT_ICS = ROOT / "docs" / "measurements" / "calendar_demo"


def days_named(msgs):
    """Every day the thread names, resolved from the day its message was sent."""
    out = set()
    for k, m in enumerate(msgs, 1):
        for x in re.finditer(R.DATE_RE, m["body"], re.I):
            if x.group(0).lower().startswith("sometime"):      # a vague phrase names no day; it must not pin one
                continue
            try:
                out.add(R.T.resolve_date(R.email_date(x.group(0)), R.M.sent_on(m["date"])))
            except Exception:
                pass
    return sorted(out)


def first_proposal(msgs):
    """(day, HH:MM) of the first day and time the thread names, for the event an invitation would create."""
    for m in msgs:
        d = re.search(R.DATE_RE, m["body"], re.I)
        t = re.search(R.TIME_RE, m["body"], re.I)
        if d and t:
            try:
                return R.T.resolve_date(R.email_date(d.group(0)), R.M.sent_on(m["date"])), R.T.parse_time(R.email_time(t.group(0)))
            except Exception:
                return None
    return None


def stamp(day, hhmm):
    return day.replace("-", "") + "T" + hhmm.replace(":", "") + "00"


def write_ics(events):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Axiom-1//generated demo calendar//EN"]
    for uid, title, start, partstat, other in events:
        lines += ["BEGIN:VEVENT", f"UID:{uid}", f"SUMMARY:{title}", f"DTSTART:{start}",
                  f"ATTENDEE;CN=Other:mailto:{other}"]
        if partstat:
            lines.append(f"ATTENDEE;PARTSTAT={partstat}:mailto:{ME}")
        lines.append("END:VEVENT")
    return "\r\n".join(lines + ["END:VCALENDAR"]) + "\r\n"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--receipt", default=str(ROOT / "docs" / "measurements" / "2026-10-04_real_inbox_nebius_v4.json"))
    p.add_argument("--out", default=str(ROOT / "docs" / "measurements" / "2026-10-04_calendar_demo.json"))
    a = p.parse_args()
    receipt = json.loads(Path(a.receipt).read_text(encoding="utf-8"))
    OUT_ICS.mkdir(parents=True, exist_ok=True)
    tally = {"follow_ups_before": 0, "follow_ups_after": 0, "accepted_to_calendar": 0, "declined_to_filed": 0,
             "still_open_said_why": 0, "entries_verified": 0, "conflicts_flagged": 0, "left_open_ambiguous": 0, "wrong": 0}
    rows = []
    for r in receipt["rows"]:
        seed = r["seed"]
        rng = random.Random(1000 + seed)
        files, truth = R.inbox(random.Random(seed))
        threads = {n: R.M.split_thread(t) for n, t in files.items()}
        subject = {n: re.sub(r"^(?:(?:re|fwd?):\s*)+", "", ms[0]["subject"], flags=re.I) for n, ms in threads.items()}
        other = {n: next((m["from"].split("<")[-1].strip(">") for m in ms if ME not in m["from"]), "x@example.com")
                 for n, ms in threads.items()}
        # the generated calendar, and what it says about each thread (the truth the reconciler is checked against)
        events, says = [], {}
        for n, exp in truth["threads"].items():
            if exp["expect"] == "on":
                s = exp["slot"]
                moved = rng.random() < 0.2
                start = (datetime.fromisoformat(f"{s['date']}T{s['start']}") + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M") \
                    if moved else f"{s['date']}T{s['start']}"
                events.append((f"{seed}-{n}", subject[n], stamp(start[:10], start[11:]), "ACCEPTED", other[n]))
                says[n] = ("conflict" if moved else "verified", start)
        for ask in r["asks"]:
            if ask.get("kind") == "follow_up" and "invited" in (ask.get("why") or ""):
                n = ask["thread"]
                fp = first_proposal(threads[n])
                if not fp:
                    continue
                click = rng.choice(["ACCEPTED", "DECLINED", "NEEDS-ACTION"])
                events.append((f"{seed}-{n}", subject[n], stamp(*fp), click, other[n]))
                says[n] = ({"ACCEPTED": "calendar", "DECLINED": "filed", "NEEDS-ACTION": "follow_up"}[click], f"{fp[0]}T{fp[1]}")
        ics_path = OUT_ICS / f"seed_{seed}.ics"
        ics_path.write_text(write_ics(events), encoding="utf-8")
        cal = CAL.read_ics(ics_path.read_text(encoding="utf-8"), ME)
        # reconcile every outcome the harness produced, the whole inbox at once (one event explains one thread)
        todo = {}
        for n, ms in threads.items():
            exp = truth["threads"][n]
            ask = next((x for x in r["asks"] if x["thread"] == n), None)
            if exp["expect"] == "on":
                todo[n] = {"subject": subject[n], "kind": "calendar", "start": f"{exp['slot']['date']}T{exp['slot']['start']}",
                           "days": days_named(ms), "people": [other[n]]}
            elif ask:
                todo[n] = {"subject": subject[n], "kind": ask["kind"], "start": None, "days": days_named(ms),
                           "people": [other[n]]}
        results = CAL.reconcile_all(todo, cal)
        for n, t in todo.items():
            kind = t["kind"]
            tally["follow_ups_before"] += kind == "follow_up"
            got = results[n]
            want = says.get(n)
            tally["follow_ups_after"] += got["kind"] == "follow_up"
            if kind == "calendar":
                ok = (got.get("conflict") and want and want[0] == "conflict") or ("matches your calendar" in (got["receipt"] or "") and want and want[0] == "verified")
                tally["entries_verified" if not got.get("conflict") else "conflicts_flagged"] += 1
            else:
                ok = (want is None and got["kind"] == kind) or (want is not None and got["kind"] == want[0])
                if kind == "follow_up" and got["kind"] == "calendar": tally["accepted_to_calendar"] += 1
                if kind == "follow_up" and got["kind"] == "filed": tally["declined_to_filed"] += 1
                if kind == "follow_up" and got["kind"] == "follow_up" and got["receipt"]: tally["still_open_said_why"] += 1
            # left open because two threads fit one event: not resolved, and said so - not a wrong change
            left_open = (not ok) and got["kind"] == kind and "not resolved by guessing" in (got["receipt"] or "")
            tally["left_open_ambiguous" if left_open else "wrong"] += (not ok)
            rows.append({"seed": seed, "thread": n, "subject": subject[n], "before": kind, "after": got["kind"],
                         "receipt": got["receipt"], "calendar_says": want, "right": bool(ok)})
    result = {"tally": tally, "rows": rows, "receipt": a.receipt, "calendars": str(OUT_ICS)}
    Path(a.out).write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(tally))


if __name__ == "__main__":
    main()
