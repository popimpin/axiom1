"""The calendar connector: read a person's calendar and reconcile the harness's output against it. No model.

Why (Adrian, 2026-10-03): "we can see the calendar ... a small api call wires in gmail or outlook". Email alone has to
say "invited, no reply in email - probably answered on a call or by a calendar click". The calendar knows which.

Source: iCalendar (.ics, RFC 5545). Google Calendar and Outlook both export it, and both APIs return the same fields
(start, end, title, attendees, my response), so one reader serves both; a live API adapter only has to produce the
same Event records. (Enron's "calendar" folders are Outlook-migration stubs with no times or responses, so the
real-mail runs cannot use one; this module is shown on the generated inbox, whose calendar is generated too.)

Reconciliation, per thread outcome:
  follow-up / confirm  a matching event I ACCEPTED   -> on the calendar, receipt "accepted in your calendar"
                       a matching event I DECLINED   -> filed, receipt "declined in your calendar"
                       TENTATIVE or no response      -> stays open, receipt "tentative" / "no response yet"
                       no matching event             -> unchanged
  calendar entry       a matching event, same start  -> verified, receipt "matches your calendar"
                       a matching event, other time  -> CONFLICT, both times shown
                       no matching event             -> unverified, said so
A match is structural: the event's title shares the thread's topic words, and its day is one the thread names (or
the held day); attendees overlapping the thread's people strengthen it. Ambiguity (two events fit) is not resolved
by guessing: the thread stays as it was and says so.
"""
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

GENERIC = set("re fw fwd the a an and or of to in on at for with meeting call sync catch up quick chat about".split())


@dataclass
class Event:
    uid: str
    title: str
    start: str               # ISO "YYYY-MM-DDTHH:MM" (local time)
    end: str = ""
    attendees: list = field(default_factory=list)
    response: str = ""       # mine: accepted | declined | tentative | needs-action | "" (I organised it)


def _unfold(text):
    return re.sub(r"\r?\n[ \t]", "", text)


def _ics_time(v):
    m = re.match(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2}))?", v)
    if not m:
        return ""
    y, mo, d, h, mi = m.groups()
    return f"{y}-{mo}-{d}T{h or '00'}:{mi or '00'}"


def read_ics(text, me):
    """Events from an iCalendar file. `me` is my address: my ATTENDEE line's PARTSTAT is my response."""
    events = []
    for block in re.findall(r"BEGIN:VEVENT(.*?)END:VEVENT", _unfold(text), re.S):
        props = {}
        attendees, response = [], ""
        for line in block.strip().splitlines():
            if ":" not in line:
                continue
            name, value = line.split(":", 1)
            key = name.split(";")[0].upper()
            if key == "ATTENDEE":
                addr = value.lower().replace("mailto:", "").strip()
                attendees.append(addr)
                if addr == me.lower():
                    m = re.search(r"PARTSTAT=([A-Z-]+)", name, re.I)
                    response = (m.group(1).lower() if m else "needs-action")
            else:
                props[key] = value.strip()
        events.append(Event(uid=props.get("UID", ""), title=props.get("SUMMARY", ""),
                            start=_ics_time(props.get("DTSTART", "")), end=_ics_time(props.get("DTEND", "")),
                            attendees=attendees, response=response))
    return events


def topic_words(text):
    s = re.sub(r"^\s*(?:(?:re|fwd?|fw)\s*:\s*)+", "", text or "", flags=re.I).lower()
    words = {w for w in re.findall(r"[a-z][a-z0-9'-]+", s) if w not in GENERIC and len(w) > 2}
    # a title with no usable words ("1:1") is matched on the title itself (seen: every "1:1" was unmatchable)
    return words or ({s.strip()} if s.strip() else set())


def match(thread, events):
    """Events that fit this thread: a shared topic word, and a day the thread names or holds. Returns a list."""
    words = topic_words(thread.get("subject", ""))
    days = set(thread.get("days", []))
    if not days:
        # no day, no match (seen 2026-10-04: a day-less ad "...our event packages" matched an accepted "Networking
        # event" on the shared word alone and was put on the calendar)
        return []
    people = {p.lower() for p in thread.get("people", [])}
    fits = []
    for e in events:
        if not (words & topic_words(e.title)):
            continue
        if e.start[:10] not in days:
            continue
        score = len(words & topic_words(e.title)) + (2 if people & set(e.attendees) else 0)
        fits.append((score, e))
    if not fits:
        return []
    best = max(s for s, _ in fits)
    return [e for s, e in fits if s == best]


def reconcile(thread, events):
    """thread: {subject, kind (calendar|follow_up|confirm|reminder|filed), start (for calendar), days, people}.
    Returns {kind, receipt, event?}: the outcome after the calendar has been read, with what decided it."""
    kind = thread["kind"]
    if kind == "filed":
        return {"kind": kind, "receipt": None}
    fits = match(thread, events)
    if len(fits) > 1:
        return {"kind": kind, "receipt": f"{len(fits)} calendar events fit equally; not resolved by guessing"}
    if not fits:
        return {"kind": kind, "receipt": "no matching event in your calendar" if kind == "calendar" else None}
    e = fits[0]
    if kind == "calendar":
        if e.start == thread.get("start"):
            return {"kind": "calendar", "receipt": f"matches your calendar: {e.title} {e.start}", "event": e.uid}
        return {"kind": "calendar", "receipt": f"CONFLICT: the email says {thread.get('start')}, your calendar says "
                f"{e.start} ({e.title})", "event": e.uid, "conflict": True}
    if e.response == "accepted" or (e.response == "" and not e.attendees):
        return {"kind": "calendar", "receipt": f"accepted in your calendar: {e.title} {e.start}", "event": e.uid,
                "start": e.start}
    if e.response == "declined":
        return {"kind": "filed", "receipt": f"declined in your calendar: {e.title} {e.start}", "event": e.uid}
    return {"kind": kind, "receipt": f"{e.response or 'no response yet'} in your calendar: {e.title} {e.start}",
            "event": e.uid}


def reconcile_all(threads, events):
    """Reconcile a whole inbox at once, so one event explains at most one thread (seen: a generated inbox with two
    "Car service" threads gave the firm meeting's event to the open invitation too; real calendars repeat titles -
    "1:1", "Lunch" - all the time). Firm calendar entries claim their events first; then each remaining thread may use
    only unclaimed events, and an event two remaining threads both fit is not given to either.
    threads: {name: thread dict as for reconcile}. Returns {name: result}."""
    out, claimed = {}, set()
    for name, t in threads.items():
        if t["kind"] == "calendar":
            out[name] = reconcile(t, events)
            if out[name].get("event"):
                claimed.add(out[name]["event"])
    free = [e for e in events if e.uid not in claimed]
    wanted = {}
    for name, t in threads.items():
        if name not in out:
            for e in match(t, free):
                wanted.setdefault(e.uid, []).append(name)
    shared = {uid for uid, names in wanted.items() if len(names) > 1}
    for name, t in threads.items():
        if name in out:
            continue
        mine = [e for e in free if e.uid not in shared]
        r = reconcile(t, mine)
        if r["receipt"] is None and any(name in wanted.get(uid, []) for uid in shared):
            r = {"kind": t["kind"], "receipt": "a calendar event fits this and another thread equally; not resolved by guessing"}
        out[name] = r
    return out
