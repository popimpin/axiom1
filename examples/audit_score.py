"""Score a person's sampled-audit marks (from web/timeline/audit.html "Download marks").

    python examples/audit_score.py axiom_audit_marks_20261005.json   # writes docs/measurements/final/audit.json
    python examples/audit_score.py marks.json --set notices            # another set: data/audit_<set>.js -> audit_<set>.json

Per stratum (what Axiom-1 decided): how often the person, deciding BLIND, picked the same thing, with a 95% Wilson
interval. Overall accuracy is weighted by each stratum's share of all 9,124 threads, because the sample over-draws the
rare outcomes (every calendar entry and reminder) on purpose. Also: details right/wrong for agreed calendar and
reminder items, and the full confusion (Axiom-1 said X, person said Y).
"""
import json
import math
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "web" / "timeline" / "data" / "audit_sample.js"
OUT = ROOT / "docs" / "measurements" / "final" / "audit.json"


def category(kind):
    return "reminder" if kind.startswith("reminder") else {"calendar": "calendar", "follow_up": "follow_up"}.get(kind, "nothing")


def wilson(k, n, z=1.96):
    if not n:
        return None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(c - h, 3), round(c + h, 3)]


def main():
    global OUT
    sample_path = SAMPLE
    if "--set" in sys.argv:
        name = sys.argv[sys.argv.index("--set") + 1]
        sample_path = SAMPLE.with_name(f"audit_{name}.js")
        OUT = OUT.with_name(f"audit_{name}.json")
    sample = json.loads(sample_path.read_text(encoding="utf-8").split("=", 1)[1].rstrip().rstrip(";"))
    marks = json.load(open(sys.argv[1], encoding="utf-8"))
    if marks.get("seed") != sample["seed"]:
        sys.exit(f"marks are for seed {marks.get('seed')}, sample is seed {sample['seed']}")
    marks = marks["marks"]
    per, confusion, details = {}, Counter(), Counter()
    for it in sample["items"]:
        m = marks.get(it["id"])
        if not m or not m.get("pick"):
            continue
        said = category(it["axiom"]["kind"])
        s = per.setdefault(it["stratum"], {"n": 0, "agree": 0})
        s["n"] += 1
        s["agree"] += said == m["pick"]
        confusion[(said, m["pick"])] += 1
        if said == m["pick"] and m.get("details"):
            details[(said, m["details"])] += 1
    pop = sample["population"]
    for k, s in per.items():
        s["rate"] = round(s["agree"] / s["n"], 3)
        s["ci95"] = wilson(s["agree"], s["n"])
        s["population"] = pop.get(k, 0)
    covered = sum(pop[k] for k in per)
    weighted = sum(per[k]["rate"] * pop[k] for k in per) / covered if covered else None
    # The weighted number alone flatters: most threads only need filing, so a system that files EVERYTHING scores
    # the filed share (92% here) and does nothing. Lead with the two numbers a do-nothing system cannot fake.
    short = [k for k in ("calendar", "reminder", "follow_up") if k in per]
    short_pop = sum(pop[k] for k in short)
    filed = [k for k in ("filed_model", "filed_structure") if k in per]
    res = {"seed": sample["seed"], "marked": sum(s["n"] for s in per.values()), "sample": sample["n"],
           "HEADLINE_short_list_precision":
               round(sum(per[k]["rate"] * pop[k] for k in short) / short_pop, 3) if short_pop else None,
           "HEADLINE_estimated_missed_items": round(sum((1 - per[k]["rate"]) * pop[k] for k in filed)),
           "of_filed_threads": sum(pop[k] for k in filed),
           "weighted_agreement_all_threads": round(weighted, 3) if weighted is not None else None,
           "baseline_file_everything": round(sum(pop.get(k, 0) for k in ("filed_model", "filed_structure")) / sum(pop.values()), 3),
           "population_covered": covered, "per_stratum": per,
           "confusion_axiom_vs_person": {f"{a} -> {b}": n for (a, b), n in sorted(confusion.items())},
           "details_on_agreed_items": {f"{a}: {b}": n for (a, b), n in sorted(details.items())},
           "method": "blind: the person picks what the thread needs before seeing Axiom-1's decision"}
    OUT.write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
