"""Run the harness over one person's whole mailbox, the way it would run for them: no answer key, no stand-in person.

Every thread is filed; the threads that name a day or time (or hand off to a call) and that I am part of go through
the same decide_thread() as the evals; what comes out is a calendar, reminders, follow-ups, and a timeline.
A decision the models cannot make is not guessed: with no person to ask, it becomes a "confirm" item.

Resumable: each thread's result is appended to <out>/<box>.jsonl as it finishes, and a rerun skips what is there.

    python examples/mailbox_run.py --box steffes-j --model qwen3:1.7b --agree ornith:9b
"""
import argparse
import csv
import io
import json
import sys
import time as _time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

import inbox_timeline as TL  # noqa: E402
import mailbox as MB  # noqa: E402
import real_inbox as R  # noqa: E402
from axiom1 import forms  # noqa: E402

ROUTED = ("is_meeting", "said_yes", "called_off", "which_time", "agreed_day", "agreed_time")


def run_thread(name, text, router, model):
    """One thread: (answer, outcome row). The row has the shape inbox_timeline.outcome() reads."""
    def person():
        pass
    person.truth = {}
    teach = lambda *a: None                      # nobody to ask: no truth is taught
    t0 = _time.time()
    answer, _ = R.decide_thread(name, text, router, model, person, teach)
    row = {"thread": name, "tier": answer.get("tier"), "got": "off", "placed": None, "reminder": None}
    if answer.get("agreed") or answer.get("handoff"):
        ok, msg, produced = forms.run_pipeline(R.PIPELINE, {"threads": [answer], "open_end": True}, {name: text})
        if ok:
            cal = list(csv.DictReader(io.StringIO(produced.get("calendar.csv", ""))))
            rem = json.loads(produced.get("asks.json", "[]"))
            if cal:
                row.update(got="on", placed=[cal[0]["date"], cal[0]["start"], cal[0]["end"]])
            elif rem:
                row.update(got="ask", reminder={k: rem[0].get(k) for k in ("kind", "why", "tentative", "missing")})
        else:
            row["pipeline_error"] = msg[:300]
    row["seconds"] = round(_time.time() - t0, 2)
    return row


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--box", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--agree", nargs="+", required=True)
    p.add_argument("--out", default=str(ROOT / "docs" / "measurements" / "mailboxes"))
    p.add_argument("--limit", type=int, help="first N threads only (a smoke run)")
    a = p.parse_args()
    from axiom1.agent import ChatModel
    from axiom1.router import Router, Table
    model, agree = ChatModel(a.model), [ChatModel(x) for x in a.agree]
    router = Router(Table(), {d: agree for d in ROUTED}, consensus=True, ask_user=None)

    msgs = MB.load(a.box)
    owner = MB.owner_of(msgs)
    ts = MB.threads(msgs)
    names = sorted(ts, key=lambda k: ts[k][0]["date"] or "")[: a.limit]
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    log = out / f"{a.box}.jsonl"
    done = {json.loads(l)["thread"] for l in log.read_text(encoding="utf-8").splitlines()} if log.exists() else set()
    texts = {}
    with log.open("a", encoding="utf-8") as fh:
        for i, k in enumerate(names, 1):
            name = f"t{i:05d}"
            texts[name] = MB.thread_text(ts[k], owner)
            if name in done:
                continue
            row = run_thread(name, texts[name], router, model)
            row["topic"] = k
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            if i % 50 == 0:
                print(f"{a.box}: {i}/{len(names)}", flush=True)

    rows = {r["thread"]: r for r in map(json.loads, log.read_text(encoding="utf-8").splitlines())}
    tl = TL.build(texts, rows)
    s = TL.summary(tl)
    s.update(box=a.box, owner=owner, messages=len(msgs),
             model_seconds=round(sum(r["seconds"] for r in rows.values()), 1),
             zero_model_calls=sum(1 for r in rows.values() if "model" not in (r["tier"] or "") and "you" not in (r["tier"] or "")),
             pipeline_errors=sum(1 for r in rows.values() if r.get("pipeline_error")))
    (out / f"{a.box}.timeline.json").write_text(json.dumps(tl) + "\n", encoding="utf-8")
    (out / f"{a.box}.summary.json").write_text(json.dumps(s, indent=1) + "\n", encoding="utf-8")
    print("SUMMARY", json.dumps(s), flush=True)


if __name__ == "__main__":
    main()
