"""One person's whole mailbox from the Enron corpus, as threads the harness can file and decide on.

The point (Adrian, 2026-10-03): run it for several employees, so the judges see it works for every employee it is
aimed at, not for 50 threads chosen from 50 strangers.

Read from external/enron (the full corpus, 517,401 messages across ~150 mailboxes). No model in here:
  1. every message in the mailbox, every folder, one copy each (by message id)
  2. the owner is whoever the sent folder is sent from (Steffes writes as d..steffes@enron.com): read, never assumed
  3. quoted history ("-----Original Message-----") is cut from replies, because the messages it quotes are in the
     thread already; a forward keeps what it forwards, because that is the content (an invitation passed on)
  4. threads = the same topic (subject without Re:/Fw:) in this mailbox, oldest first
  5. each thread is written in the harness's format, the owner as "Me", dates in Houston time

    python examples/mailbox.py --box steffes-j            # cache the mailbox, print its shape
"""
import argparse
import collections
import json
import re
import sys
from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "examples"))

CORPUS = ROOT / "external" / "enron"
CACHE = ROOT / "external" / "mailboxes"
HOUSTON = ZoneInfo("America/Chicago")
QUOTE = re.compile(r"^\s*-{3,}\s*Original Message\s*-{3,}|^\s*-{5,}\s*Forwarded by|^\s*>?\s*From:\s.*\n\s*>?\s*Sent:",
                   re.I | re.M)
PREFIX = re.compile(r"^\s*(?:(?:re|fwd?|fw)\s*:\s*)+", re.I)


def one_copy(msgs):
    """The same email filed in two folders (all_documents and sent) carries two different message ids: seen on
    Haedicke, 1,345 of 1,462 two-message "threads" were one email twice. One copy per sender + time + body.
    A date before 1997 is a broken header (1980-01-01 appears), so it is unknown, not ancient."""
    # every folder a copy was filed in is kept: the owner is read from the sent folder, and keeping only the
    # all_documents copy flipped Haedicke's owner to another alias (343 -> 49 threads "mine")
    seen, out = {}, []
    for m in msgs:
        if m["date"] and m["date"] < "1997":
            m = {**m, "date": None}
        k = (m["from"], m["date"], re.sub(r"\s+", " ", m["body"]).strip()[:400])
        if k in seen:
            seen[k]["folders"].append(m["folder"])
            continue
        m = {**m, "folders": [m["folder"]]}
        seen[k] = m
        out.append(m)
    return out


def load(box):
    """Every message in one mailbox, one copy each, cached as JSON (reading the corpus takes a while)."""
    CACHE.mkdir(parents=True, exist_ok=True)
    cached = CACHE / f"{box}.json"
    if cached.exists():
        return one_copy(json.loads(cached.read_text(encoding="utf-8")))
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    rows = []
    for f in sorted(CORPUS.glob("*.parquet")):
        t = pq.read_table(f)
        rows += t.filter(pc.starts_with(t.column("file_name"), box + "/")).to_pylist()
    seen, out = set(), []
    for r in rows:
        if r["message_id"] in seen:
            continue
        seen.add(r["message_id"])
        out.append({"id": r["message_id"], "folder": r["file_name"].split("/")[1], "from": r["from"] or "",
                    "to": [x for x in (r["to"] or []) if x], "cc": [x for x in (r["cc"] or []) if x],
                    "subject": r["subject"] or "", "body": r["body"] or "",
                    "date": r["date"].astimezone(timezone.utc).isoformat() if r["date"] else None})
    out.sort(key=lambda m: m["date"] or "")
    cached.write_text(json.dumps(out), encoding="utf-8")
    return one_copy(out)


def owner_of(msgs):
    """The address the sent folder is sent from."""
    sent = collections.Counter(m["from"] for m in msgs if any("sent" in f for f in m.get("folders", [m["folder"]])))
    return sent.most_common(1)[0][0] if sent else None


def new_text(m):
    """What this message adds: the reply above the quoted history; a forward keeps what it forwards."""
    body = m["body"].replace("\r\n", "\n")
    if re.match(r"^\s*(?:fwd?|fw)\s*:", m["subject"], re.I):
        return body.strip()
    hit = QUOTE.search(body)
    return (body[:hit.start()] if hit else body).strip()


def topic(subject):
    s = re.sub(r"\s+", " ", PREFIX.sub("", subject or "")).strip().lower()
    return s or None


def threads(msgs):
    """{topic: [messages, oldest first]}. A message without a subject stands alone (no topic to join on)."""
    out = collections.defaultdict(list)
    for m in msgs:
        out[topic(m["subject"]) or f"(no subject) {m['id']}"].append(m)
    return dict(out)


def thread_text(ms, owner):
    """A thread in the harness's format: the owner as Me, dates in Houston time."""
    import mailex_eval as X
    is_owner = X.owner_matcher(owner)
    parts = []
    for m in ms:
        frm = "Me <me@example.com>" if (m["from"] == owner or is_owner(m["from"])) else (m["from"] or "someone")
        head = [f"From: {frm}"]
        if m["date"]:
            from datetime import datetime
            local = datetime.fromisoformat(m["date"]).astimezone(HOUSTON)
            head.append(f"Date: {local.strftime('%a')}, {local.day} {local.strftime('%b %Y %H:%M')}")
        head.append(f"Subject: {m['subject'] or '(no subject)'}")
        body = re.sub(r"(?m)^From:", "> From:", new_text(m))      # a quoted header must not start a message
        parts.append("\n".join(head) + "\n\n" + body + "\n")
    return "\n".join(parts)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--box", required=True)
    a = p.parse_args()
    msgs = load(a.box)
    owner = owner_of(msgs)
    ts = threads(msgs)
    sizes = collections.Counter(min(len(v), 6) for v in ts.values())
    print(json.dumps({"box": a.box, "owner": owner, "messages": len(msgs), "threads": len(ts),
                      "span": [msgs[0]["date"], msgs[-1]["date"]],
                      "thread_sizes": {("6+" if k == 6 else str(k)): v for k, v in sorted(sizes.items())}}))


if __name__ == "__main__":
    main()
