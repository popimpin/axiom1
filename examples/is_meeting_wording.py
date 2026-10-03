"""Score wordings of the is_meeting question on every thread that reaches it, generated and real, at once.

One probe per wording misled twice on 2026-10-02: "meeting or call" dropped car services and a dentist, and
"put something on my calendar" dropped every forwarded invitation (read literally: nobody is putting anything).
So each wording is scored on all of them:

  generated  every thread that reaches the question should be "yes" (all are things I am asked to)
  real yes   MailEx threads whose answer key says on/ask: a "no" drops a real meeting (missed)
  real no    MailEx threads keyed off: a "yes" is only a risk, the agreement decisions still follow

    python examples/is_meeting_wording.py [--seeds 10] [--out FILE]
"""
import argparse
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

import mailex_eval as X  # noqa: E402
import real_inbox as R  # noqa: E402

WORDINGS = {
    "calendar": "Is someone in this thread trying to put something on my calendar - a meeting, a call or an appointment?",
    "meeting_or_call": "Is this thread arranging a meeting or call that I will attend? (No if it already happened, it is "
                       "someone else's, or nobody is actually setting one up.)",
    "asked_to_go": "Is there something in this thread - a meeting, a call, an appointment or an event - that I am being "
                   "asked to go to?",
    "might_go": "Is this thread about a meeting, call, appointment or event that I might go to?",
}


def reaches(text):
    """The threads decide_thread asks is_meeting about: a day or time somewhere, and I wrote or was written to."""
    msgs = R.M.split_thread(text)
    has_when = any(re.search(p, m["body"], re.I) for m in msgs for p in (R.DATE_RE, R.TIME_RE))
    return msgs if has_when and R.M.mine(msgs, R.ME) else None


def cases(seeds):
    out = []
    for seed in range(1, seeds + 1):
        files, truth = R.inbox(random.Random(seed))
        for name, text in sorted(files.items()):
            msgs = reaches(text)
            if msgs:
                out.append({"set": "generated", "id": f"{seed}/{name}", "want": "yes",
                            "kind": truth["threads"][name]["scenario"], "shown": R.render(msgs)})
    key = json.load(open(X.KEY, encoding="utf-8"))["threads"]
    owners = json.load(open(X.OWNERS, encoding="utf-8"))
    for name, exp in sorted(key.items()):
        d = json.load(open(X.DATED / f"{name}.json", encoding="utf-8"))
        msgs = reaches(X.thread_text(d, owners[d["owner"]]))
        if msgs:
            out.append({"set": "real", "id": name, "want": "yes" if exp["expect"] in ("on", "ask") else "no",
                        "kind": exp["why"][:60], "shown": R.render(msgs)})
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--seeds", type=int, default=10)
    p.add_argument("--out")
    a = p.parse_args()
    from axiom1.agent import ChatModel
    from axiom1.router import Router
    model = ChatModel("ornith:9b")
    cs = cases(a.seeds)
    result = {"model": model.model, "cases": len(cs), "wordings": {}}
    for wid, q in WORDINGS.items():
        rows = []
        for c in cs:
            got = Router._ask(model, f"Here is an email thread. 'Me' is me.\n\n{c['shown']}\n\n{q}", ["yes", "no"])
            rows.append({"set": c["set"], "id": c["id"], "kind": c["kind"], "want": c["want"], "got": got})
        gen = [r for r in rows if r["set"] == "generated"]
        ry = [r for r in rows if r["set"] == "real" and r["want"] == "yes"]
        rn = [r for r in rows if r["set"] == "real" and r["want"] == "no"]
        s = {"generated_yes": f"{sum(r['got'] == 'yes' for r in gen)}/{len(gen)}",
             "real_meetings_kept": f"{sum(r['got'] == 'yes' for r in ry)}/{len(ry)}",
             "real_off_said_no": f"{sum(r['got'] == 'no' for r in rn)}/{len(rn)}",
             "dropped": [f"{r['id']} ({r['kind']})" for r in gen + ry if r["got"] != "yes"]}
        result["wordings"][wid] = {"question": q, "score": s, "rows": rows}
        print(wid, json.dumps(s), flush=True)
    if a.out:
        Path(a.out).write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
