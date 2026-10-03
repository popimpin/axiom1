"""The calendar harness on real email: 50 Enron threads from MailEx (CC BY-SA 4.0), dates recovered from the Enron
corpus (examples/mailex_dates.py), final state read by hand (docs/data/mailex_calendar_key.json).

Each thread goes through the same decide_thread() as the generated inbox. The owner's messages become "Me"; dates
are shown in Houston time (a reply stamped 03:17 UTC was written the evening before). A message whose date could
not be recovered has none, so a relative day in it is asked about, not guessed. Meetings without a stated length go
on the calendar with the end left open.

Scored by harm: a wrong meeting on the calendar, a meeting missed, a question that was not needed.

    python examples/mailex_eval.py --model qwen3:1.7b --agree ornith:9b [--only NAME ...] [--out FILE]
"""
import argparse
import json
import re
import sys
import time as _time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

import real_inbox as R  # noqa: E402
from axiom1 import forms  # noqa: E402

HOUSTON = ZoneInfo("America/Chicago")
DATED = ROOT / "external" / "mailex_dated"
KEY = ROOT / "docs" / "data" / "mailex_calendar_key.json"
OWNERS = ROOT / "external" / "mailex_owner_emails.json"


def owner_matcher(email):
    """The owner's own messages: their address, or their last name with their first name or initial
    ("Martin, Thomas A." for a..martin; not "Lyne Martin", which last name alone would match)."""
    local = email.split("@")[0]
    parts = [p for p in re.split(r"\.+", local) if p]
    last, firsts = parts[-1], parts[:-1]

    def is_owner(frm):
        f = frm.lower()
        if email in f:
            return True
        # another address of the same person (seen: Vince Kaminski writing as VKaminski@aol.com)
        for addr_local in re.findall(r"([a-z0-9._'-]+)@", f):
            flat = re.sub(r"[^a-z]", "", addr_local)
            if flat.endswith(re.sub(r"[^a-z]", "", last)) and firsts and (
                    flat.startswith(firsts[0][0]) or flat.startswith(re.sub(r"[^a-z]", "", firsts[0]))):
                return True
        if not re.search(rf"\b{re.escape(last)}\b", f):
            return False
        return not firsts or any(re.search(rf"\b{re.escape(x)}\b", f) for x in firsts)
    return is_owner


def thread_text(d, email):
    """A MailEx thread (dates recovered) in the harness's format, oldest first."""
    is_owner = owner_matcher(email)
    out = []
    for m in d["messages"]:
        frm = "Me <me@example.com>" if is_owner(m["from"]) else (re.sub(r"\[mailto:[^\]]*\]", "", m["from"]).strip()
                                                                 or "Someone <unknown@example.com>")
        head = [f"From: {frm}"]
        if m["date"]:
            local = datetime.fromisoformat(m["date"]).astimezone(HOUSTON)
            head.append(f"Date: {local.strftime('%a')}, {local.day} {local.strftime('%b %Y %H:%M')}")
        head.append(f"Subject: {m['subject'] or '(no subject)'}")
        body = re.sub(r"(?m)^From:", "> From:", m["body"] or "")      # a quoted header must not start a message
        out.append("\n".join(head) + "\n\n" + body + "\n")
    return "\n".join(out)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--agree", nargs="+", required=True)
    p.add_argument("--only", nargs="*")
    p.add_argument("--out")
    a = p.parse_args()
    from axiom1.agent import ChatModel
    from axiom1.router import Router, Table
    key = json.load(open(KEY, encoding="utf-8"))["threads"]
    owners = json.load(open(OWNERS, encoding="utf-8"))
    model, agree = ChatModel(a.model), [ChatModel(x) for x in a.agree]
    taps = []

    def person(decision, text, question, opts, votes):
        taps.append({"text": text[:120], "votes": votes})
        return person.truth.get(text, "no")
    router = Router(Table(), {d: agree for d in ("is_meeting", "said_yes", "called_off", "which_time", "agreed_day", "agreed_time")},
                    consensus=True, ask_user=person)
    rows, score = [], {"right": 0, "WRONG_ON_CALENDAR": 0, "missed": 0, "needless_ask": 0, "wrong_time": 0, "wrong_section": 0}
    for name in sorted(a.only or key):
        exp = key[name]
        d = json.load(open(DATED / f"{name}.json", encoding="utf-8"))
        text = thread_text(d, owners[d["owner"]])
        def teach(dec, opts, resolver, e=exp):
            if dec == "agreed_day":
                return R.oracle(opts, resolver, e.get("date"))
            if dec == "agreed_time":
                return R.oracle(opts, resolver, e.get("start"))
            if dec == "which_time":
                # the proposal that names the keyed day or time; with neither keyed, the latest stands
                return next((o for o in opts if {e.get("date"), e.get("start")} & resolver(o) - {None}),
                            "the latest proposal")
            yes = e["expect"] in ("on", "ask")
            return {"is_meeting": "yes" if yes else "no", "said_yes": "yes" if yes else "no",
                    "called_off": "no" if yes else "yes"}[dec]
        t0 = _time.time()
        answer, corrections = R.decide_thread(f"threads/{name}.txt", text, router, model, person, teach)
        ok, msg, produced = forms.run_pipeline(R.PIPELINE, {"threads": [answer], "open_end": True},
                                               {f"threads/{name}.txt": text})
        placed, asked = None, False
        if ok:
            import csv
            import io
            cal = list(csv.DictReader(io.StringIO(produced["calendar.csv"])))
            placed = (cal[0]["date"], cal[0]["start"], cal[0]["end"]) if cal else None
            reminders = json.loads(produced.get("asks.json", "[]"))
            asked = bool(reminders)
        got = "on" if placed else ("ask" if asked else "off")
        e = exp["expect"]
        if e == "on":
            verdict = ("right" if placed[:2] == (exp["date"], exp["start"]) else "wrong_time") if got == "on" else "missed"
        elif e == "ask":
            verdict = "right" if got == "ask" else ("WRONG_ON_CALENDAR" if got == "on" else "missed")
        elif e == "follow_up":
            # open-ended (Adrian, 2026-10-03: most are "handled by a call"): right only in the follow-up section
            rk = ((json.loads(produced.get("asks.json", "[]")) or [{}])[0].get("kind")) if ok else None
            verdict = ("right" if rk == "follow_up" else "wrong_section") if got == "ask" else \
                      ("WRONG_ON_CALENDAR" if got == "on" else "missed")
        elif e == "invite":
            verdict = "WRONG_ON_CALENDAR" if got == "on" else "right"       # off or ask both acceptable for now
        else:
            verdict = "WRONG_ON_CALENDAR" if got == "on" else ("needless_ask" if got == "ask" else "right")
        score[verdict] += 1
        # a reminder: which kind, and does its tentative hold name the right day / time? A hold on the wrong day is
        # harm too, even though it is not on the calendar
        rem = reminders[0] if ok and reminders else None
        hold = (rem or {}).get("tentative") or {}
        hold_wrong = [k for k, want in (("date", exp.get("date")), ("start", exp.get("start")))
                      if want and hold.get(k) and hold[k] != want]
        if hold_wrong:
            score["WRONG_HOLD"] = score.get("WRONG_HOLD", 0) + 1
        if rem:
            score[f"remind_{rem.get('kind', '?')}"] = score.get(f"remind_{rem.get('kind', '?')}", 0) + 1
        row = {"thread": name, "expect": e, "got": got, "verdict": verdict, "placed": placed, "tier": answer["tier"],
               "reminder": rem and {"kind": rem.get("kind"), "why": rem.get("why"), "tentative": hold,
                                    "wrong": hold_wrong},
               "slot": [answer["day"], answer["time"], answer["length"]], "corrections": corrections,
               "seconds": round(_time.time() - t0, 1), "why_expected": exp["why"]}
        rows.append(row)
        print(json.dumps(row), flush=True)
    result = {"form_model": model.model, "agree": [m.model for m in agree], "score": score, "taps": taps, "rows": rows}
    print("SCORE", json.dumps(score), flush=True)
    if a.out:
        Path(a.out).write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
