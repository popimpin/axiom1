"""The data behind web/timeline: one set per person (a whole mailbox) plus the 50 scored threads.

A whole mailbox is thousands of threads, and nearly all of them are simply filed. So a mailbox's lanes are its
short list - what is on the calendar, the reminders, the follow-ups - and the filing is the headline number
("2,393 of 2,537 threads just need filing"). The 50 scored threads show every lane.

    python examples/timeline_sets.py          # writes web/timeline/data/sets.js
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

# the page shows the Nemotron runs (Nano + Super must agree) - the entry runs on Nemotron; Bee stays in MEASUREMENTS
BOXES = Path(os.environ["AXIOM_BOXES_DIR"]) if os.environ.get("AXIOM_BOXES_DIR") else ROOT / "docs" / "measurements" / "mailboxes_nebius"
MODELS = "Nemotron 3 Nano + Super on Nebius"
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
        ts = [t for t in f["threads"] if t["outcome"]["kind"] != "filed" or t.get("oversight")]
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
        rows = [json.loads(l) for l in (BOXES / f"{box}.jsonl").read_text(encoding="utf-8").splitlines()]
        s.setdefault("announcements", sum(1 for r in rows if (r["tier"] or "").startswith("structure: announcement")))
        s["confirm_items"] = sum((r["tier"] or "").count("needs you") for r in rows)
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
        # needs human oversight (code, commands, database statements, encoded blobs, AI-addressed instructions):
        # read from the original messages, structure only; such a thread joins the short list whatever else it is
        import mailbox as MB
        from axiom1 import oversight as O
        raw = MB.threads(MB.load(box))
        topic_of = {r["thread"]: r.get("topic") for r in rows}
        for t in everything:
            ms = raw.get(topic_of.get(t["thread"]), [])
            t["oversight"] = O.reasons([MB.new_text(m) for m in ms])
        s["oversight"] = sum(1 for t in everything if t["oversight"])
        # an invitation from outside the organisation, from someone the owner never wrote to, is flagged (not dropped)
        from axiom1 import provenance as PV
        all_msgs = MB.load(box)
        owner = MB.owner_of(all_msgs)
        known = PV.written_to([m for m in all_msgs if any("sent" in f for f in m.get("folders", [m["folder"]]))])
        tier_of = {r["thread"]: r.get("tier") or "" for r in rows}
        for t in everything:
            ms = raw.get(topic_of.get(t["thread"]), [])
            t["outside"] = PV.outside_flag(ms[0]["from"], owner, known) if ms and tier_of.get(t["thread"], "").startswith("invited") else None
        s["outside"] = sum(1 for t in everything if t["outside"])
        # the review folder's text, for the sandboxed inspector only (web/timeline/inspect.html): the original messages
        # as plain text, never rendered as HTML, attachments listed by name and never opened
        review = {}
        for t in everything:
            if t["oversight"]:
                ms = raw.get(topic_of.get(t["thread"]), [])
                review[t["thread"]] = {"subject": t["subject"], "reasons": t["oversight"],
                                       "attachments": O.attachments([m["body"] for m in ms]),
                                       "messages": [{"from": m["from"], "date": m["date"], "body": MB.new_text(m)} for m in ms]}
        rv = OUT.parent / f"review_{box}.js"
        rv.write_text(f"(window.AXIOM_REVIEW = window.AXIOM_REVIEW || {{}})[{json.dumps(box)}] = "
                      + json.dumps(review, ensure_ascii=False) + ";\n", encoding="utf-8")
        sets.append({"id": box, "name": name, "role": role, "note": note, "whole_mailbox": True, "projects": projects,
                     "summary": {"threads": s["threads"], "messages": s["messages"], "outcomes": s["outcomes"],
                                 "call_gaps": s["call_gaps"], "announcements": s.get("announcements", 0), "oversight": s.get("oversight", 0),
                                 "zero_model_calls": s["zero_model_calls"], "confirm_items": s["confirm_items"],
                                 "models": MODELS},
                     "folders": short_list(tl)})
    # the 50 scored threads, from the newest scored receipt
    import inbox_timeline as TL
    import mailex_eval as X
    receipt = Path(os.environ["AXIOM_MAILEX_RECEIPT"]) if os.environ.get("AXIOM_MAILEX_RECEIPT") else sorted((ROOT / "docs" / "measurements").glob("*_mailex_nebius_v*.json"),
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
                 "summary": {"threads": s["threads"], "outcomes": s["outcomes"], "call_gaps": s["call_gaps"], "models": MODELS},
                 "folders": tl["folders"]})
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("window.AXIOM_SETS = " + json.dumps(sets, ensure_ascii=False) + ";\n", encoding="utf-8")
    for x in sets:
        print(f"{x['id']:11} lanes={sum(len(f['threads']) for f in x['folders']):4}  {x['summary']['outcomes']}")
    print(f"wrote {OUT} ({OUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
