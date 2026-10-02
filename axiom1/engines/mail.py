"""A thread of email, read by its headers: who wrote each message and when. Who wrote what is structure, not a
judgement, so the harness reads it here and shows it to the model as fact."""
import re

from ._base import EngineError

SPEC = {
    "name": "mail",
    "version": 1,
    "summary": "split an email thread into messages by their headers; say which are from me and when they were sent",
    "functions": {
        "split_thread": {
            "args": ["text"], "returns": "[{from, to, date, subject, body}] in the order written", "io": False,
            "examples": [
                {"args": {"text": "From: Sam <sam@x.com>\nDate: Mon, 4 May 2026 09:12\nSubject: Coffee?\n\nFree Thursday?\n"
                                  "\nFrom: Me <me@x.com>\nDate: Mon, 4 May 2026 10:03\nSubject: Re: Coffee?\n\nSure.\n"},
                 "returns": [{"from": "Sam <sam@x.com>", "to": "", "date": "Mon, 4 May 2026 09:12",
                              "subject": "Coffee?", "body": "Free Thursday?"},
                             {"from": "Me <me@x.com>", "to": "", "date": "Mon, 4 May 2026 10:03",
                              "subject": "Re: Coffee?", "body": "Sure."}]},
                {"args": {"text": "just some text"}, "refuses": "no message headers"},
            ]},
        "sent_on": {
            "args": ["date_header"], "returns": "YYYY-MM-DD the message was sent", "io": False,
            "examples": [
                {"args": {"date_header": "Mon, 4 May 2026 09:12"}, "returns": "2026-05-04"},
                {"args": {"date_header": "4 May 2026"}, "returns": "2026-05-04"},
                {"args": {"date_header": "yesterday"}, "refuses": "not a date header"},
                {"args": {"date_header": "Tue, 4 May 2026 09:12"}, "refuses": "is a Monday"},
            ]},
        "mine": {
            "args": ["messages", "me"], "returns": "the messages whose From is my address", "io": False,
            "examples": [
                {"args": {"messages": [{"from": "Sam <sam@x.com>"}, {"from": "Me <ME@x.com>"}], "me": "me@x.com"},
                 "returns": [{"from": "Me <ME@x.com>"}]},
                {"args": {"messages": [], "me": "not an address"}, "refuses": "address"},
            ]},
    },
}

_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"], 1)}
_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def split_thread(text):
    """Messages in the order they appear. A message starts at a 'From:' header line (at the start of the text or
    after a blank line); headers run to the first blank line, the body to the next message."""
    if not isinstance(text, str):
        raise EngineError("text must be a string")
    lines = text.replace("\r\n", "\n").split("\n")
    starts = [i for i, ln in enumerate(lines) if ln.startswith("From:") and (i == 0 or not lines[i - 1].strip())]
    if not starts:
        raise EngineError("no message headers (a message starts with a 'From:' line)")
    messages = []
    for k, start in enumerate(starts):
        block = lines[start:starts[k + 1] if k + 1 < len(starts) else len(lines)]
        head, body, in_body = {}, [], False
        for ln in block:
            if in_body:
                body.append(ln)
            elif not ln.strip():
                in_body = True
            else:
                m = re.match(r"(From|To|Date|Subject):\s*(.*)$", ln)
                if m:
                    head[m.group(1).lower()] = m.group(2).strip()
        messages.append({"from": head.get("from", ""), "to": head.get("to", ""), "date": head.get("date", ""),
                         "subject": head.get("subject", ""), "body": "\n".join(body).strip()})
    return messages


def sent_on(date_header):
    """The day a message was sent, from its Date header ('Mon, 4 May 2026 09:12')."""
    if not isinstance(date_header, str):
        raise EngineError("date_header must be a string")
    m = re.search(r"(?:\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+)?\b(\d{1,2})\s+([a-z]{3})[a-z]*\s+(\d{4})\b",
                  date_header, re.IGNORECASE)
    if not m or m.group(3).lower() not in _MONTHS:
        raise EngineError(f"{date_header!r} is not a date header like 'Mon, 4 May 2026 09:12'")
    from datetime import date
    try:
        d = date(int(m.group(4)), _MONTHS[m.group(3).lower()], int(m.group(2)))
    except ValueError as e:
        raise EngineError(f"{date_header!r}: {e}") from None
    if m.group(1) and _DAYS.index(m.group(1).lower()) != d.weekday():
        raise EngineError(f"{date_header!r}: {d.isoformat()} is a {d.strftime('%A')}")
    return d.isoformat()


def mine(messages, me):
    """The messages I sent: their From header contains my address (any case)."""
    if not isinstance(me, str) or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", me.strip()):
        raise EngineError(f"me must be an email address, got {me!r}")
    return [m for m in messages if me.strip().lower() in str(m.get("from", "")).lower()]


GUIDE = '''Use mail to read a thread before deciding anything about it.
- `mail.split_thread(text)` -> the messages, each with from, to, date, subject, body, in the order written.
- `mail.sent_on(message["date"])` -> the ISO day it was sent: the day relative words ("next Thursday") are read
  from, with time.resolve_date.
- `mail.mine(messages, "me@example.com")` -> my own messages: what I said yes or no with.
Who wrote which message comes from these headers, never from guessing at the text.'''
