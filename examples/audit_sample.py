"""Draw the sampled audit of the five whole mailboxes: a person marks what each thread needs, blind.

The 9,124 mailbox threads have no answer key - every number about them so far is the system's own. This draws a
stratified random sample from the final run (docs/measurements/final/mailboxes) for a person to judge:

    every calendar entry, every reminder, and per mailbox 8 follow-ups, 4 threads the model filed and 4 threads
    filed on structure alone (no model call)

The page (web/timeline/audit.html) shows the email FIRST and asks what it needs - calendar, reminder, follow-up or
nothing - before it shows what Axiom-1 decided, so the mark is the person's own call, not agreement with ours.
Items are shuffled across strata so the order gives nothing away. Seed is fixed and recorded.

    python examples/audit_sample.py            # writes web/timeline/data/audit_sample.js
    python examples/audit_score.py marks.json  # scores the downloaded marks
"""
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))
import mailbox as MB  # noqa: E402

FINAL = ROOT / "docs" / "measurements" / "final" / "mailboxes"
OUT = ROOT / "web" / "timeline" / "data" / "audit_sample.js"
SEED = 20261005
PER_BOX = {"follow_up": 8, "filed_model": 4, "filed_structure": 4}


def stratum(outcome, tier):
    k = outcome["kind"]
    if k == "calendar":
        return "calendar"
    if k.startswith("reminder"):
        return "reminder"
    if k == "follow_up":
        return "follow_up"
    return "filed_structure" if (tier or "").startswith("structure") or "no model" in outcome.get("decided_by", "") \
        else "filed_model"


def main():
    rng = random.Random(SEED)
    items, population = [], {}
    for tl_path in sorted(FINAL.glob("*.timeline.json")):
        box = tl_path.name.split(".")[0]
        rows = {json.loads(l)["thread"]: json.loads(l) for l in open(FINAL / f"{box}.jsonl", encoding="utf-8")}
        tl = json.load(open(tl_path, encoding="utf-8"))
        raw = MB.threads(MB.load(box))
        by_stratum = {}
        for fo in tl["folders"]:
            for th in fo["threads"]:
                r = rows.get(th["thread"], {})
                st = stratum(th["outcome"], r.get("tier"))
                by_stratum.setdefault(st, []).append((th, r))
        for st, lst in by_stratum.items():
            population[st] = population.get(st, 0) + len(lst)
            n = PER_BOX.get(st, len(lst))          # calendar and reminders: all of them
            for th, r in rng.sample(lst, min(n, len(lst))):
                ms = raw.get(r.get("topic"), [])
                if not ms:
                    continue
                items.append({
                    "id": f"{box}/{th['thread']}", "box": box, "stratum": st, "subject": th["subject"],
                    "messages": [{"from": m["from"], "date": m["date"], "body": MB.new_text(m)[:6000]} for m in ms[:12]],
                    "more_messages": max(0, len(ms) - 12),
                    "axiom": {"kind": th["outcome"]["kind"], "why": th["outcome"].get("why", ""),
                              "decided_by": th["outcome"].get("decided_by", ""),
                              "date": th["outcome"].get("date", ""), "start": th["outcome"].get("start", ""),
                              "tentative": th["outcome"].get("tentative", {})},
                })
    rng.shuffle(items)
    data = {"seed": SEED, "per_box": PER_BOX, "population": population, "n": len(items), "items": items}
    OUT.write_text("window.AXIOM_AUDIT = " + json.dumps(data, ensure_ascii=False) + ";\n", encoding="utf-8")
    from collections import Counter
    print(f"{len(items)} items -> {OUT.relative_to(ROOT)}  by stratum {dict(Counter(i['stratum'] for i in items))}")
    print(f"population {population}")


if __name__ == "__main__":
    main()
