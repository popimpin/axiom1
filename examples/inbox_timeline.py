"""Every thread filed, and a timeline of what happened in it: continuity before calendars.

Adrian, 2026-10-03: "some of these just need a folder for the thread to make sense to the human, the individual will
know what was important from the thread alone, so continuity is sometimes more important than a calendar date or
reminder". And: "my bet email will converge to simple task".

So the harness's last step is not a judgement. Every thread goes into a folder, and its messages become events on a
timeline; a calendar entry or a reminder is something extra a thread MAY carry. No model is in this path: folders and
events are read from structure (subject, people, dates, the day/time phrases, a hand-off to a phone call), so a
filing can't be wrong in the way a guess can.

Events, in order:
  proposed  a message names a day or a time (the phrases it names are listed)
  call      the arranging moved to a phone call: a GAP. What was decided there is not in the email, and the
            timeline says so instead of guessing across it
  message   anything else
and one outcome per thread, with the receipt of what decided it: calendar (placed), reminder (finalize/confirm,
with its tentative hold), or filed.

    python examples/inbox_timeline.py --rows docs/measurements/2026-10-03_mailex_bee_v9.json --out timeline.json
"""
import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

import real_inbox as R  # noqa: E402

PREFIX = re.compile(r"^\s*(?:(?:re|fwd?|fw)\s*:\s*)+", re.I)


def subject_root(subject):
    """The thread's topic: Re:/Fwd: taken off, case and spacing aside. "(no subject)" has none."""
    s = re.sub(r"\s+", " ", PREFIX.sub("", subject or "")).strip().strip(".").lower()
    return "" if s in ("", "(no subject)") else s


def who(msg):
    """'me', or the sender's name (the part before <address>), so a folder reads as people, not addresses."""
    if R.M.mine([msg], R.ME):
        return "me"
    # Enron headers: '"Jackie Gallagher" <JGallagher@epsa.org>@ENRON' -> Jackie Gallagher
    frm = re.sub(r"<[^>]*>|\S*@\S*|\"", " ", msg.get("from") or "")
    frm = re.sub(r"\s+", " ", frm).strip(" ,'")
    return frm or (msg.get("from") or "someone").strip()


def when(msg):
    """The message's Date header as ISO minutes, or None (a message whose date could not be recovered)."""
    m = re.search(r"(\d{1,2}) ([A-Za-z]{3}) (\d{4}) (\d{1,2}):(\d{2})", msg.get("date") or "")
    if not m:
        return None
    try:
        return datetime.strptime(" ".join(m.groups()), "%d %b %Y %H %M").strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return None


def events(msgs):
    """One event per message, in order. A message without a date keeps its place in the thread."""
    out = []
    for k, m in enumerate(msgs, 1):
        whens = [x.group(0) for p in (R.DATE_RE, R.TIME_RE) for x in re.finditer(p, m["body"], re.I)]
        kind = "call" if R.hands_off(m) else ("proposed" if whens else "message")
        said = re.sub(r"\s+", " ", m["body"]).strip()
        ev = {"message": k, "at": when(m), "who": who(m), "kind": kind, "said": said[:160] + ("..." if len(said) > 160 else "")}
        if whens:
            ev["whens"] = list(dict.fromkeys(whens))
        if kind == "call":
            ev["gap"] = "the arranging moved to a phone call: what was decided there is not in the email"
        out.append(ev)
    return out


def outcome(row):
    """What the harness did with the thread, and the receipt of what decided it. Default: filed, nothing more."""
    if not row:
        return {"kind": "filed"}
    if row.get("got") == "on" and row.get("placed"):
        d, s, e = row["placed"]
        return {"kind": "calendar", "date": d, "start": s, "end": e, "decided_by": row.get("tier")}
    rem = row.get("reminder")
    if rem:
        return {"kind": f"reminder:{rem.get('kind')}", "why": rem.get("why"), "tentative": rem.get("tentative") or {},
                "decided_by": row.get("tier")}
    return {"kind": "filed", "decided_by": row.get("tier")}


def build(threads, rows=None):
    """threads: {name: thread text}; rows: {name: eval row} (optional). Returns {"folders": [...]}: threads with the
    same topic share a folder; a thread with no subject is filed by the people in it."""
    rows = rows or {}
    folders = {}
    for name in sorted(threads):
        msgs = R.M.split_thread(threads[name])
        if not msgs:
            continue
        root = subject_root(msgs[0]["subject"])
        people = sorted({who(m) for m in msgs} - {"me"})
        key = root or "with " + ", ".join(people)
        evs = events(msgs)
        dated = [e["at"] for e in evs if e["at"]]
        f = folders.setdefault(key, {"folder": key, "people": set(), "threads": []})
        f["people"].update(people)
        f["threads"].append({"thread": name, "subject": msgs[0]["subject"], "messages": len(msgs),
                             "span": [min(dated), max(dated)] if dated else None, "events": evs,
                             "outcome": outcome(rows.get(name))})
    out = []
    for f in folders.values():
        spans = [t["span"] for t in f["threads"] if t["span"]]
        out.append({"folder": f["folder"], "people": sorted(f["people"]), "threads": f["threads"],
                    "span": [min(s[0] for s in spans), max(s[1] for s in spans)] if spans else None})
    out.sort(key=lambda f: (f["span"] or ["9999"])[0])
    return {"folders": out}


def summary(tl):
    from collections import Counter
    ts = [t for f in tl["folders"] for t in f["threads"]]
    return {"threads": len(ts), "folders": len(tl["folders"]),
            "outcomes": dict(Counter(t["outcome"]["kind"] for t in ts)),
            "call_gaps": sum(e["kind"] == "call" for t in ts for e in t["events"])}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--rows", help="an eval receipt (mailex_eval --out) whose rows give each thread's outcome")
    p.add_argument("--out")
    p.add_argument("--js", help="also write the timeline as a script (window.AXIOM_TIMELINE) for web/timeline, so "
                                "the page opens from disk without a server")
    a = p.parse_args()
    import mailex_eval as X
    key = json.load(open(X.KEY, encoding="utf-8"))["threads"]
    owners = json.load(open(X.OWNERS, encoding="utf-8"))
    threads = {}
    for name in key:
        d = json.load(open(X.DATED / f"{name}.json", encoding="utf-8"))
        threads[name] = X.thread_text(d, owners[d["owner"]])
    rows = {r["thread"]: r for r in json.load(open(a.rows, encoding="utf-8"))["rows"]} if a.rows else {}
    tl = build(threads, rows)
    print(json.dumps(summary(tl)))
    if a.out:
        Path(a.out).write_text(json.dumps(tl, indent=1) + "\n", encoding="utf-8")
    if a.js:
        tl["source"] = {"threads": "MailEx, 50 Enron threads (CC BY-SA 4.0)", "outcomes": a.rows or None,
                        "summary": summary(tl)}
        Path(a.js).write_text("window.AXIOM_TIMELINE = " + json.dumps(tl, ensure_ascii=False) + ";\n", encoding="utf-8")


if __name__ == "__main__":
    main()
