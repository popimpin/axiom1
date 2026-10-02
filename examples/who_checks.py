"""Who writes the check? With no hidden answer key, which layer of the harness stops a wrong answer?

In the experiments the operator's hidden check judges every delivery. A real user has no such key. So this plants
every plausible single mistake (the ones models actually made) into otherwise-correct per-email answers, across many
inboxes, and records which layer stops it, in the order production has them:

  menu      the mistake cannot be expressed on the form (a value outside its menu)
  rule      a structure rule fixes or refuses it (no reply from me -> not agreed; a reply points at the meeting with
            its own subject; an agreed meeting needs its date, time and length)
  engine    the pipeline's engines refuse it (the ledger will not move a meeting that does not exist)
  harmless  it gets through and the calendar is still right
  HIDDEN    it gets through, the calendar is wrong, and only the hidden check sees it: the risk a real user carries

No model is called.   python examples/who_checks.py [--inboxes 30]
"""
import argparse
import copy
import json
import random
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.argv = sys.argv[:1] + [a for a in sys.argv[1:]]

import everyday_tasks as e  # noqa: E402
from axiom1 import forms  # noqa: E402

TODAY = "2026-10-01"


def gold(files):
    """The right per-email answers, read from the generator's templates."""
    answers, agreed = [], {}
    for name in sorted(p for p in files if p.startswith("inbox/")):
        text = files[name]
        topic = e._topic(text)
        reply = e._my_reply(text)
        a = {"file": name, "kind": "not_agreed", "refers_to": "", "date": "", "time": "", "duration": ""}
        if "Could we do" in text:
            m = re.search(r"on (.+?) at (.+?)\? It should take (about \d+ minutes)", text)
            a.update(date=m.group(1), time=m.group(2), duration=m.group(3))
            a["said_yes"] = "no" if reply in e.NO_REPLIES or reply.startswith("Sorry, I can't") else "yes"
            if a["said_yes"] == "yes":
                a["kind"] = "agreed"
                agreed[topic] = name
            else:
                a.update(date="", time="", duration="")
        elif text.startswith("Subject: Re:"):
            m = re.search(r"to (.+?) at (.+?) instead", text)
            a.update(kind="moved_and_agreed", refers_to=agreed[topic], date=m.group(1), time=m.group(2), said_yes="yes")
        elif text.lower().startswith("subject: cancel"):
            a.update(kind="cancelled_by_them", refers_to=agreed[topic])
        answers.append(a)
    return answers


def mistakes(answers, files):
    """Every plausible single mistake, each as (type, email, mutated answers). Types are the ones seen live."""
    agreed = [a["file"] for a in answers if a["kind"] == "agreed"]
    out = []

    def mut(label, i, **change):
        m = copy.deepcopy(answers)
        m[i].update(change)
        out.append((label, answers[i]["file"], m))
    for i, a in enumerate(answers):
        text = files[a["file"]]
        if a["kind"] == "agreed":
            mut("yes read as no (router)", i, said_yes="no")
            mut("agreed filed as not_agreed", i, kind="not_agreed", date="", time="", duration="")
            mut("agreed: length left out", i, duration="")
        if a.get("said_yes") == "no":
            m = re.search(r"on (.+?) at (.+?)\? It should take (about \d+ minutes)", text)
            mut("no read as yes (router)", i, said_yes="yes", kind="agreed", date=m.group(1), time=m.group(2),
                duration=m.group(3))
            mut("decline filed as agreed", i, kind="agreed", date=m.group(1), time=m.group(2), duration=m.group(3))
        if a["kind"] == "moved_and_agreed":
            for other in agreed:
                if other != a["refers_to"]:
                    mut("move linked to the wrong meeting", i, refers_to=other)
            mut("move filed as a new meeting", i, kind="agreed", refers_to="",
                duration=re.search(r"about \d+ minutes", files[a["refers_to"]]).group(0))
            mut("move's yes read as no (router)", i, said_yes="no")
        if a["kind"] == "cancelled_by_them":
            for other in agreed:
                if other != a["refers_to"]:
                    mut("cancel linked to the wrong meeting", i, refers_to=other)
            mut("cancel filed as not_agreed", i, kind="not_agreed", refers_to="")
        if a["file"].endswith("newsletter.txt"):
            m = re.search(r"on (.+?) at (.+?)!", text)
            mut("newsletter filed as agreed", i, kind="agreed", date=m.group(1), time=m.group(2),
                duration="about 60 minutes")
    return out


def judge_hidden(task, files, truth, produced):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in {**files, **produced}.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        (root / ".axiom_check").mkdir()
        (root / ".axiom_check/truth.json").write_text(json.dumps(truth), encoding="utf-8")
        (root / ".axiom_check/check.py").write_text(task["check"], encoding="utf-8")
        return subprocess.run([sys.executable, "-I", ".axiom_check/check.py"], cwd=root,
                              capture_output=True, text=True).returncode == 0


def layer(task, files, truth, answers):
    """Run mutated answers through the production layers, in order, as fill_each and the pipeline would."""
    form_for = e.calendar_item_form_for(files, yes_no_routed=True)
    checks = e.calendar_item_checks(TODAY, files)
    same = e.calendar_same(TODAY)
    done = []
    for a in answers:
        name = a["file"]
        text = files[name]
        decided = {"said_yes": a["said_yes"]} if "said_yes" in a and e._my_reply(text) else {}
        form = {k: v for k, v in a.items() if k not in ("file", "said_yes")}
        form = e.calendar_relevant({**form, **decided}, text)
        schema = form_for(name, done)
        if forms.shape_problems({k: v for k, v in form.items() if k not in decided}, schema):
            return "menu"
        if [f for f, j in same.items() if form.get(f) and forms._copy_key(form[f]) not in forms._copy_key(text)
                and not j(form[f], text)]:
            return "menu"
        if checks(name, text, form, done):
            return "rule"
        done.append({"file": name, **form})
    ok, _, produced = forms.run_pipeline(e.CALENDAR_EACH_PIPELINE, {"emails": done, "today": TODAY}, files)
    if not ok:
        return "engine"
    return "harmless" if judge_hidden(task, files, truth, produced) else "HIDDEN"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--inboxes", type=int, default=30)
    p.add_argument("--seed", type=int, default=900)
    p.add_argument("--out", default=None)
    a = p.parse_args()
    task = next(t for t in e.TASKS if t["id"] == "calendar-from-inbox")
    table, rows = {}, []
    for seed in range(a.seed, a.seed + a.inboxes):
        files, truth = e.calendar_varied_data(random.Random(seed))
        right = gold(files)
        assert layer(task, files, truth, right) == "harmless", f"gold answers fail on seed {seed}"
        for kind, email, wrong in mistakes(right, files):
            where = layer(task, files, truth, wrong)
            table.setdefault(kind, {}).setdefault(where, 0)
            table[kind][where] += 1
            rows.append({"seed": seed, "mistake": kind, "email": email, "stopped_by": where})
    cols = ["menu", "rule", "engine", "harmless", "HIDDEN"]
    print(f"{'mistake':36} " + " ".join(f"{c:>8}" for c in cols) + "   n")
    for kind, counts in table.items():
        n = sum(counts.values())
        print(f"{kind:36} " + " ".join(f"{counts.get(c, 0):>8}" for c in cols) + f"   {n}")
    total = {c: sum(t.get(c, 0) for t in table.values()) for c in cols}
    print(f"{'ALL':36} " + " ".join(f"{total[c]:>8}" for c in cols) + f"   {sum(total.values())}")
    if a.out:
        Path(a.out).write_text(json.dumps({"inboxes": a.inboxes, "first_seed": a.seed, "table": table,
                                           "rows": rows}, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
