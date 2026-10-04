"""Project folders: the threads of one mailbox grouped by the project they belong to, not just by exact subject.

Seen in Heard's mailbox: about 85 of 890 threads are one project, master netting agreements, under 85 different
subject lines ("master netting: dynegy", "bnp paribas master netting agreement", "master netting group meeting"...).
A folder per subject keeps each thread whole; a project folder puts them side by side.

Structure only, no model: a project is a phrase that recurs across many DIFFERENT subject lines in one mailbox
(a pair of words, or one distinctive word), after the words that say nothing about a project are set aside
("meeting", "agreement", "update", "re"...). Each thread joins the most widely shared project phrase its subject
contains; a thread that shares none stays in its own folder.

    python examples/projects.py --box heard-m          # print the projects found in one mailbox
"""
import argparse
import collections
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIN_THREADS = 4          # a phrase in fewer subjects than this is a topic, not a project
GENERIC = set("""
a an the and or of to in on at by for from with without into about as is are be re fw fwd please read this that these
your our my his her their its it we you i me us new revised revision revisions final draft drafts form forms update
updates updated status list lists question questions issue issues review comments comment memo meeting
meetings call calls conference agreement agreements agmt agmts contract contracts document documents information info set through favor regarding
request requests follow followup reminder today tomorrow week next monday tuesday wednesday thursday friday saturday
sunday january february march april may june july august september october november december
enron ena ect hou corp inc co llc ltd company group team mr ms dr re: fw: fwd: no subject delivered undeliverable
out office autoreply auto reply attached attachment copy copies notice note thanks thank hi hello summary agenda
""".split())


def words(subject):
    """The subject's words that could name a project, in order (prefixes, punctuation, numbers and generic words out)."""
    s = re.sub(r"^\s*(?:(?:re|fwd?|fw)\s*:\s*)+", "", subject or "", flags=re.I).lower()
    toks = [t.strip("-'&") for t in re.findall(r"[a-z][a-z&'-]+", s)]       # "credit report--" -> "report"
    return [t for t in toks if t not in GENERIC and len(t) > 2]


def candidates(ws):
    """Pairs of neighbouring words. Single words were tried first and named projects badly: "master" lumped the
    ISDA Master Agreement in with master netting, and "letter", "response", "legal", "october" became projects."""
    return {" ".join(p) for p in zip(ws, ws[1:])}


def find_projects(subjects):
    """subjects: {thread: subject}. Returns ({thread: project or None}, {project: count})."""
    cand = {t: candidates(words(s)) for t, s in subjects.items()}
    count = collections.Counter(c for cs in cand.values() for c in cs)
    keep = {c: n for c, n in count.items() if n >= MIN_THREADS}
    # a single word that only ever appears inside one pair adds nothing: the pair is the project
    pairs = [c for c in keep if " " in c]
    for c in list(keep):
        if " " not in c and any(c in p.split() and keep[p] >= keep[c] for p in pairs):
            del keep[c]
    assign = {}
    for t, cs in cand.items():
        mine = [c for c in cs if c in keep]
        # the most widely shared phrase wins; on a tie the pair (more specific) wins, then the alphabetically first
        assign[t] = max(mine, key=lambda c: (keep[c], " " in c, [-ord(x) for x in c])) if mine else None
    sizes = collections.Counter(p for p in assign.values() if p)
    # a project that ended up with fewer threads than the bar (its threads went to bigger ones) dissolves
    small = {p for p, n in sizes.items() if n < MIN_THREADS}
    assign = {t: (None if p in small else p) for t, p in assign.items()}
    return assign, collections.Counter(p for p in assign.values() if p)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--box", required=True)
    p.add_argument("--src", default="mailboxes")
    a = p.parse_args()
    tl = json.loads((ROOT / "docs" / "measurements" / a.src / f"{a.box}.timeline.json").read_text(encoding="utf-8"))
    subjects = {t["thread"]: t["subject"] for f in tl["folders"] for t in f["threads"]}
    assign, sizes = find_projects(subjects)
    inproj = sum(1 for p in assign.values() if p)
    print(f"{a.box}: {len(subjects)} threads, {len(sizes)} projects holding {inproj} threads ({100 * inproj / len(subjects):.0f}%)")
    for name, n in sizes.most_common(15):
        ex = [subjects[t] for t, p in assign.items() if p == name][:3]
        print(f"  {n:4}  {name:28} e.g. {' | '.join(s[:38] for s in ex)}")


if __name__ == "__main__":
    main()
