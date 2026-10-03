"""Score wordings of the follow-up gate: asked only when a reminder is about to be made, "is anything left for ME?"

Adrian, 2026-10-03, on the first reminders: "some of these have no need of a reminder or follow up action at all".
A reminder is only worth making when something is still waiting on me. Scored on every thread a reminder could
come from:

  must remind   generated threads keyed "ask" + real threads keyed "ask"/"on": a "no" here loses a meeting
  no reminder   real threads keyed "off" that v8 reminded about (Adrian's list): a "yes" is a needless reminder

    python examples/followup_wording.py [--out FILE]
"""
import argparse
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

import mailex_eval as X  # noqa: E402
import real_inbox as R  # noqa: E402

WORDINGS = {
    "waiting_on_me": "Is anyone in this thread still waiting on me - to settle a day or time, to reply, or to confirm?",
    "left_to_do": "Is there still something I need to do about this meeting - settle a day or time, reply, or confirm it?",
    "still_arranging": "Is a meeting that I am going to still being arranged in this thread?",
}
REAL_REMIND = ["corman-s_inbox_archives142", "shackleton-s_inbox677", "steffes-j_inbox58", "steffes-j_inbox99",
               "ybarbo-p_inbox214", "rapp-b_inbox_323", "wolfe-j_inbox436"]
REAL_QUIET = ["heard-m_inbox_118", "reitmeyer-j_inbox_52", "tycholiz-b_inbox368", "watson-k_inbox471",
              "blair-l_inbox_66"]


def cases(seeds=10):
    out = []
    for seed in range(1, seeds + 1):
        files, truth = R.inbox(random.Random(seed))
        for name, exp in sorted(truth["threads"].items()):
            if exp["expect"] == "ask":
                out.append({"id": f"{seed}/{name}", "want": "yes", "kind": exp["scenario"],
                            "shown": R.render(R.M.split_thread(files[name]))})
    key = json.load(open(X.KEY, encoding="utf-8"))["threads"]
    owners = json.load(open(X.OWNERS, encoding="utf-8"))
    for names, want in ((REAL_REMIND, "yes"), (REAL_QUIET, "no")):
        for name in names:
            d = json.load(open(X.DATED / f"{name}.json", encoding="utf-8"))
            out.append({"id": name, "want": want, "kind": key[name]["why"][:60],
                        "shown": R.render(R.M.split_thread(X.thread_text(d, owners[d["owner"]])))})
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--out")
    a = p.parse_args()
    from axiom1.agent import ChatModel
    from axiom1.router import Router
    model = ChatModel("ornith:9b")
    cs = cases()
    result = {"model": model.model, "cases": len(cs), "wordings": {}}
    for wid, q in WORDINGS.items():
        rows = [{**{k: c[k] for k in ("id", "want", "kind")},
                 "got": Router._ask(model, f"Here is an email thread. 'Me' is me.\n\n{c['shown']}\n\n{q}", ["yes", "no"])}
                for c in cs]
        must = [r for r in rows if r["want"] == "yes"]
        quiet = [r for r in rows if r["want"] == "no"]
        s = {"kept": f"{sum(r['got'] == 'yes' for r in must)}/{len(must)}",
             "quieted": f"{sum(r['got'] == 'no' for r in quiet)}/{len(quiet)}",
             "lost": [f"{r['id']} ({r['kind']})" for r in must if r["got"] != "yes"],
             "still_reminds": [r["id"] for r in quiet if r["got"] != "no"]}
        result["wordings"][wid] = {"question": q, "score": s, "rows": rows}
        print(wid, json.dumps(s), flush=True)
    if a.out:
        Path(a.out).write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
