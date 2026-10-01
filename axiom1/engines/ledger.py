"""Ledger engine: maintain and mutate calendar entries in order with add, move, and cancel events."""
import re
from datetime import date

from ._base import EngineError
from .time import add_minutes

SPEC = {
    "name": "ledger",
    "version": 1,
    "summary": "maintain and mutate calendar entries in order with add, move, and cancel events",
    "functions": {
        "apply_events": {
            "args": ["events"],
            "returns": "list of active entries [{key, title, date, start, end}] sorted by date then start",
            "io": False,
            "examples": [
                {
                    "args": {
                        "events": [
                            {
                                "kind": "add",
                                "key": "retro",
                                "title": "Retro",
                                "date": "2026-05-07",
                                "start": "09:30",
                                "minutes": 45,
                            },
                            {
                                "kind": "move",
                                "key": "retro",
                                "date": "2026-05-08",
                                "start": "11:30",
                            },
                            {
                                "kind": "add",
                                "key": "call",
                                "title": "Call",
                                "date": "2026-05-08",
                                "start": "14:00",
                                "minutes": 30,
                            },
                            {"kind": "cancel", "key": "call"},
                        ]
                    },
                    "returns": [
                        {
                            "key": "retro",
                            "title": "Retro",
                            "date": "2026-05-08",
                            "start": "11:30",
                            "end": "12:15",
                        }
                    ],
                },
                {
                    "args": {
                        "events": [
                            {
                                "kind": "add",
                                "key": "m1",
                                "title": "Meeting 1",
                                "date": "2026-05-08",
                                "start": "10:00",
                                "minutes": 30,
                            },
                            {
                                "kind": "add",
                                "key": "m1",
                                "title": "Meeting 1 Dup",
                                "date": "2026-05-08",
                                "start": "11:00",
                                "minutes": 30,
                            },
                        ]
                    },
                    "refuses": "already exists",
                },
                {
                    "args": {
                        "events": [
                            {"kind": "cancel", "key": "nonexistent_meeting"}
                        ]
                    },
                    "refuses": "nonexistent_meeting",
                },
            ],
        },
    },
}


def _validate_date(d):
    if not isinstance(d, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.strip()):
        raise EngineError(f"date {d!r} must be an ISO date string (YYYY-MM-DD)")
    try:
        date.fromisoformat(d.strip())
    except ValueError as e:
        raise EngineError(f"invalid date {d!r}: {e}") from None
    return d.strip()


def _validate_start(s):
    if not isinstance(s, str) or not re.fullmatch(r"\d{2}:\d{2}", s.strip()):
        raise EngineError(f"start time {s!r} must be in 24-hour HH:MM format")
    h, m = map(int, s.strip().split(":"))
    if h < 0 or h > 23 or m < 0 or m > 59:
        raise EngineError(f"start time {s!r} is out of 24-hour range (00:00 - 23:59)")
    return s.strip()


def _validate_minutes(mins):
    if not isinstance(mins, int) or isinstance(mins, bool) or mins <= 0:
        raise EngineError(f"minutes must be an integer > 0, got {mins!r}")
    return mins


def apply_events(events):
    """Apply a list of calendar events in order and return the active entries sorted by date then start.

    Events must be dicts with 'kind':
      - 'add': key, title, date (ISO), start (HH:MM), minutes (int > 0). End is computed automatically.
      - 'move': key, and any of date / start / minutes (unspecified fields keep their current values).
      - 'cancel': key.
    Refuses:
      - add with an existing key.
      - move or cancel of an unknown or cancelled key (the error message names the key).
      - minutes <= 0 or crossing midnight.
      - unknown event kinds or missing/extra fields.
    """
    if not isinstance(events, (list, tuple)):
        raise EngineError("events must be a list of event dicts")

    active = {}

    for i, event in enumerate(events, 1):
        if not isinstance(event, dict):
            raise EngineError(f"event {i} must be a dict")
        if "kind" not in event:
            raise EngineError(f"event {i} is missing 'kind'")

        kind = event["kind"]

        if kind == "add":
            expected = {"kind", "key", "title", "date", "start", "minutes"}
            extra = set(event.keys()) - expected
            missing = expected - set(event.keys())
            if extra or missing:
                err_parts = []
                if extra:
                    err_parts.append(f"unknown fields {sorted(extra)}")
                if missing:
                    err_parts.append(f"missing fields {sorted(missing)}")
                raise EngineError(f"add event {i}: {'; '.join(err_parts)}")

            key = event["key"]
            if not isinstance(key, str) or not key.strip():
                raise EngineError(f"event {i} key must be a non-empty string")
            if key in active:
                raise EngineError(f"key {key!r} already exists in ledger")

            title = event["title"]
            if not isinstance(title, str):
                raise EngineError(f"event {i} title must be a string")

            d = _validate_date(event["date"])
            st = _validate_start(event["start"])
            mins = _validate_minutes(event["minutes"])
            end = add_minutes(st, mins)

            active[key] = {
                "key": key,
                "title": title,
                "date": d,
                "start": st,
                "end": end,
                "minutes": mins,
            }

        elif kind == "move":
            allowed = {"kind", "key", "date", "start", "minutes"}
            extra = set(event.keys()) - allowed
            if extra:
                raise EngineError(f"move event {i} has unknown fields: {sorted(extra)}")
            if "key" not in event:
                raise EngineError(f"move event {i} is missing 'key'")

            key = event["key"]
            if key not in active:
                raise EngineError(f"cannot move key {key!r}: meeting key does not exist or was cancelled")

            if not any(f in event for f in ("date", "start", "minutes")):
                raise EngineError(f"move event {i} for key {key!r} must specify at least one of date, start, minutes")

            curr = active[key]
            d = _validate_date(event["date"]) if "date" in event else curr["date"]
            st = _validate_start(event["start"]) if "start" in event else curr["start"]
            mins = _validate_minutes(event["minutes"]) if "minutes" in event else curr["minutes"]
            end = add_minutes(st, mins)

            active[key] = {
                "key": key,
                "title": curr["title"],
                "date": d,
                "start": st,
                "end": end,
                "minutes": mins,
            }

        elif kind == "cancel":
            expected = {"kind", "key"}
            extra = set(event.keys()) - expected
            missing = expected - set(event.keys())
            if extra or missing:
                err_parts = []
                if extra:
                    err_parts.append(f"unknown fields {sorted(extra)}")
                if missing:
                    err_parts.append(f"missing fields {sorted(missing)}")
                raise EngineError(f"cancel event {i}: {'; '.join(err_parts)}")

            key = event["key"]
            if key not in active:
                raise EngineError(f"cannot cancel key {key!r}: meeting key does not exist or was cancelled")

            del active[key]

        else:
            raise EngineError(f"unknown event kind {kind!r} in event {i}; accepted kinds are 'add', 'move', 'cancel'")

    entries = [
        {
            "key": e["key"],
            "title": e["title"],
            "date": e["date"],
            "start": e["start"],
            "end": e["end"],
        }
        for e in active.values()
    ]
    entries.sort(key=lambda x: (x["date"], x["start"], x["key"]))
    return entries
