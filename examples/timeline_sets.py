"""The data behind web/timeline: one set per person (a whole mailbox) plus the 50 scored threads.

A whole mailbox is thousands of threads, and nearly all of them are simply filed. So a mailbox's lanes are its
short list - what is on the calendar, the reminders, the follow-ups - and the filing is the headline number
("2,393 of 2,537 threads just need filing"). The 50 scored threads show every lane.

    python examples/timeline_sets.py          # writes web/timeline/data/sets.js
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

BOXES = ROOT / "docs" / "measurements" / "mailboxes"
OUT = ROOT / "web" / "timeline" / "data" / "sets.js"
# who each mailbox belongs to. Roles only where the corpus record is clear; otherwise the department.
PEOPLE = [
    ("lay-k", "Kenneth Lay", "Chairman & CEO", "his mail is sent by his assistant"),
    ("haedicke-m", "Mark Haedicke", "General Counsel, Enron North America", None),
    ("steffes-j", "James Steffes", "Government & regulatory affairs", None),
    ("giron-d", "Darron Giron", "Trading", None),
    ("heard-m", "Marie Heard", "Legal", None),
]


def short_list(tl):
    """Only the threads that carry something beyond filing."""
    folders = []
    for f in tl["folders"]:
        ts = [t for t in f["threads"] if t["outcome"]["kind"] != "filed"]
        if ts:
            folders.append({**f, "threads": ts})
    return folders


def main():
    sets = []
    for box, name, role, note in PEOPLE:
        summ, tlf = BOXES / f"{box}.summary.json", BOXES / f"{box}.timeline.json"
        if not (summ.exists() and tlf.exists()):
            print(f"skip {box}: not run yet")
            continue
        s = json.loads(summ.read_text(encoding="utf-8"))
        tl = json.loads(tlf.read_text(encoding="utf-8"))
        # project folders over the WHOLE mailbox (structure, no model); the short list carries each thread's project
        import projects as P
        everything = [t for f in tl["folders"] for t in f["threads"]]
        assign, sizes = P.find_projects({t["thread"]: t["subject"] for t in everything})
        for t in everything:
            t["project"] = assign.get(t["thread"])
        open_by = {}
        for t in everything:
            if t["project"] and t["outcome"]["kind"] != "filed":
                open_by[t["project"]] = open_by.get(t["project"], 0) + 1
        projects = [{"name": n, "threads": c, "open": open_by.get(n, 0)} for n, c in sizes.most_common(24)]
        sets.append({"id": box, "name": name, "role": role, "note": note, "whole_mailbox": True, "projects": projects,
                     "summary": {"threads": s["threads"], "messages": s["messages"], "outcomes": s["outcomes"],
                                 "call_gaps": s["call_gaps"], "announcements": s.get("announcements", 0),
                                 "zero_model_calls": s["zero_model_calls"]},
                     "folders": short_list(tl)})
    # the 50 scored threads, from the newest scored receipt
    import inbox_timeline as TL
    import mailex_eval as X
    receipt = sorted((ROOT / "docs" / "measurements").glob("*_mailex_bee_v*.json"),
                     key=lambda p: int(p.stem.rsplit("_v", 1)[1]))[-1]
    key = json.load(open(X.KEY, encoding="utf-8"))["threads"]
    owners = json.load(open(X.OWNERS, encoding="utf-8"))
    threads = {}
    for n in key:
        d = json.load(open(X.DATED / f"{n}.json", encoding="utf-8"))
        threads[n] = X.thread_text(d, owners[d["owner"]])
    rows = {r["thread"]: r for r in json.load(open(receipt, encoding="utf-8"))["rows"]}
    tl = TL.build(threads, rows)
    s = TL.summary(tl)
    sets.append({"id": "mailex-50", "name": "50 scored threads", "role": f"MailEx, answer key ({receipt.stem})",
                 "note": "0 wrong on the calendar", "whole_mailbox": False,
                 "summary": {"threads": s["threads"], "outcomes": s["outcomes"], "call_gaps": s["call_gaps"]},
                 "folders": tl["folders"]})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("window.AXIOM_SETS = " + json.dumps(sets, ensure_ascii=False) + ";\n", encoding="utf-8")
    for x in sets:
        print(f"{x['id']:11} lanes={sum(len(f['threads']) for f in x['folders']):4}  {x['summary']['outcomes']}")
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
