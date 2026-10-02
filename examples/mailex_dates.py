"""Recover the send dates MailEx stripped, from the original Enron corpus, and write each thread oldest-first.

MailEx (CC BY-SA 4.0) threads are Enron messages with earlier messages quoted inside, newest first, without Date
headers. A relative day ("tonight", "next Tuesday") cannot be read without the day it was sent, so:
  - the newest message: its Enron file (user/folder/index.) by name, content-checked
  - each quoted message: the Enron message whose opening text it is (first 160 normalised characters)
A message whose date cannot be recovered keeps date None: the harness will ask about it rather than guess.

    python examples/mailex_dates.py external/mailex/data/raw_threads external/enron external/mailex_dated
"""
import json
import re
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq


def norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", (text or "").lower())).strip()


def topic(subject):
    return norm(re.sub(r"^\s*((re|fw|fwd)\s*:\s*)+", "", subject or "", flags=re.I))


def split_raw(raw):
    """MailEx raw thread -> messages, newest first: blocks of FROM/TO/SUBJECT then body, split on ----- lines."""
    blocks = re.split(r"\n-{5,}\s*\n", raw.replace("\r\n", "\n"))
    out = []
    for b in blocks:
        head, body, in_body = {}, [], False
        for line in b.strip("\n").split("\n"):
            m = re.match(r"(FROM|TO|SUBJECT)\s*:\s*(.*)$", line, re.I) if not in_body else None
            if m:
                head[m.group(1).lower()] = m.group(2).strip()
            else:
                in_body = True
                body.append(line)
        if head or "".join(body).strip():
            out.append({"from": head.get("from", ""), "to": head.get("to", ""), "subject": head.get("subject", ""),
                        "body": "\n".join(body).strip()})
    return out


def main(raw_dir, enron_dir, out_dir):
    t = pa.concat_tables([pq.read_table(f, columns=["file_name", "date", "body", "from", "subject"])
                          for f in sorted(Path(enron_dir).glob("*.parquet"))])
    files = t.column("file_name").to_pylist()
    dates = t.column("date").to_pylist()
    bodies = t.column("body").to_pylist()
    by_file = {fn: i for i, fn in enumerate(files)}
    subjects = t.column("subject").to_pylist()
    by_subject = {}
    for i, sj in enumerate(subjects):
        by_subject.setdefault(topic(sj), []).append(i)
    by_open = {}
    for i, b in enumerate(bodies):
        key = norm(b)[:160]
        if len(key) >= 40:
            by_open.setdefault(key, []).append(i)
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stats = {"threads": 0, "top_by_file": 0, "messages": 0, "dated": 0}
    for path in sorted(Path(raw_dir).iterdir()):
        name = path.name
        msgs = split_raw(path.read_text(encoding="utf-8", errors="replace"))
        if not msgs:
            continue
        stats["threads"] += 1
        m = re.match(r"^([a-z]+(?:-[a-z]+)?)_(.+?)_?(\d+)$", name)
        top = None
        if m:
            user, folder, idx = m.group(1), m.group(2).rstrip("_"), m.group(3)
            for f in (folder, folder.replace("_", "/"), folder.replace("_", "/", 1)):
                if f"{user}/{f}/{idx}." in by_file:
                    top = by_file[f"{user}/{f}/{idx}."]
                    break
        for k, msg in enumerate(msgs):
            msg["date"], msg["date_from"] = None, None
            if k == 0 and top is not None:
                msg["date"], msg["date_from"] = dates[top].isoformat(), "file"
                stats["top_by_file"] += 1
                continue
            key = norm(msg["body"])[:160]
            if len(key) >= 40 and key in by_open:
                when = sorted(dates[i] for i in by_open[key] if dates[i] is not None)
                if when:
                    msg["date"], msg["date_from"] = when[0].isoformat(), "opening text"
                    continue
            # fallback: an original with the same subject whose body contains this message's text (greetings,
            # signatures and line wraps differ between the quoted copy and the original)
            probe = norm(msg["body"])[:80]
            if len(probe) >= 30:
                when = sorted(dates[i] for i in by_subject.get(topic(msg["subject"]), [])[:400]
                              if dates[i] is not None and probe in norm(bodies[i]))
                if when:
                    msg["date"], msg["date_from"] = when[0].isoformat(), "subject + text"
        # refuse, don't guess: walking from the newest (the trusted file date) back, a date later than the message after
        # it is a wrong match (a later forward of the same text); it is dropped, so the harness asks instead
        newer = None
        for msg in msgs:                                   # msgs is newest first here
            if msg["date"] and newer and msg["date"] > newer:
                msg["date"], msg["date_from"] = None, "dropped: later than the message after it"
                stats["dropped"] = stats.get("dropped", 0) + 1
            elif msg["date"]:
                newer = msg["date"]
        stats["messages"] += len(msgs)
        stats["dated"] += sum(1 for x in msgs if x["date"])
        (out / f"{name}.json").write_text(json.dumps({"thread": name, "owner": name.split("_")[0],
                                                     "messages": list(reversed(msgs))}, indent=1), encoding="utf-8")
    print(json.dumps(stats))


if __name__ == "__main__":
    main(*sys.argv[1:4])
