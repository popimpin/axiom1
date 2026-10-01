"""Time and date engine: parse, calculate, and compare dates, times, and durations deterministically."""
import re
from datetime import date

from ._base import EngineError

SPEC = {
    "name": "time",
    "version": 1,
    "summary": "parse and calculate dates, times, and durations deterministically without guessing",
    "functions": {
        "parse_time": {
            "args": ["text"],
            "returns": "HH:MM 24-hour time string",
            "io": False,
            "examples": [
                {"args": {"text": "1:00 pm"}, "returns": "13:00"},
                {"args": {"text": "9:30am"}, "returns": "09:30"},
                {"args": {"text": "noon"}, "returns": "12:00"},
                {"args": {"text": "midnight"}, "returns": "00:00"},
                {"args": {"text": "11:00"}, "returns": "11:00"},
                {"args": {"text": "11"}, "refuses": "ambiguous"},
                {"args": {"text": "13pm"}, "refuses": "invalid"},
                {"args": {"text": "25:00"}, "refuses": "invalid hour"},
                {"args": {"text": "9:75"}, "refuses": "invalid minute"},
            ],
        },
        "parse_date": {
            "args": ["text", "year"],
            "returns": "YYYY-MM-DD date string",
            "io": False,
            "examples": [
                {"args": {"text": "Friday May 8", "year": 2026}, "returns": "2026-05-08"},
                {"args": {"text": "May 8th", "year": 2026}, "returns": "2026-05-08"},
                {"args": {"text": "8 May", "year": 2026}, "returns": "2026-05-08"},
                {"args": {"text": "2026-05-08", "year": 2026}, "returns": "2026-05-08"},
                {"args": {"text": "05/08/2026", "year": 2026}, "refuses": "ambiguous"},
                {"args": {"text": "Feb 30", "year": 2026}, "refuses": "invalid date"},
                {"args": {"text": "Thursday May 8", "year": 2026}, "refuses": "different day"},
            ],
        },
        "parse_duration": {
            "args": ["text"],
            "returns": "duration in minutes as an integer",
            "io": False,
            "examples": [
                {"args": {"text": "45 minutes"}, "returns": 45},
                {"args": {"text": "about 45 minutes"}, "returns": 45},
                {"args": {"text": "1.5 hours"}, "returns": 90},
                {"args": {"text": "an hour"}, "returns": 60},
                {"args": {"text": "1h30"}, "returns": 90},
                {"args": {"text": "half an hour"}, "returns": 30},
                {"args": {"text": "0 minutes"}, "refuses": "greater than 0"},
                {"args": {"text": "a while"}, "refuses": "vague"},
            ],
        },
        "add_minutes": {
            "args": ["start", "minutes"],
            "returns": "HH:MM 24-hour time string",
            "io": False,
            "examples": [
                {"args": {"start": "09:00", "minutes": 45}, "returns": "09:45"},
                {"args": {"start": "1:00 pm", "minutes": 30}, "returns": "13:30"},
                {"args": {"start": "23:30", "minutes": 45}, "refuses": "midnight"},
            ],
        },
        "days_between": {
            "args": ["a", "b"],
            "returns": "integer number of days (b - a)",
            "io": False,
            "examples": [
                {"args": {"a": "2026-05-08", "b": "2026-05-10"}, "returns": 2},
                {"args": {"a": "2026-05-10", "b": "2026-05-08"}, "returns": -2},
                {"args": {"a": "not-a-date", "b": "2026-05-10"}, "refuses": "invalid"},
            ],
        },
        "is_overdue": {
            "args": ["due", "as_of"],
            "returns": "true if due date is before as_of date",
            "io": False,
            "examples": [
                {"args": {"due": "2026-05-01", "as_of": "2026-05-08"}, "returns": True},
                {"args": {"due": "2026-05-08", "as_of": "2026-05-08"}, "returns": False},
                {"args": {"due": "2026-05-10", "as_of": "2026-05-08"}, "returns": False},
                {"args": {"due": "invalid", "as_of": "2026-05-08"}, "refuses": "invalid"},
            ],
        },
    },
}

_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def parse_time(text):
    """Parse a time string into 24-hour "HH:MM" format.

    Accepts 12-hour times with am/pm (e.g. '1pm', '1:00 pm', '9.30am') and 24-hour
    times with colon (e.g. '11:00', '13:00'), plus 'noon' and 'midnight'.
    Refuses bare numbers without colon or am/pm (e.g. '11') and invalid times.
    """
    if not isinstance(text, str) or not text.strip():
        raise EngineError("time text must be a non-empty string")
    s = text.strip().lower()

    if s == "noon":
        return "12:00"
    if s == "midnight":
        return "00:00"

    # Bare number check: e.g. "11", "9"
    if re.fullmatch(r"\d{1,2}", s):
        raise EngineError(f"ambiguous time {text!r}: bare hour without am/pm or colon is not accepted; "
                          "use '11:00', '11am', or '11pm'")

    # 12-hour clock with am/pm: "1pm", "1:00 pm", "9:30am", "9.30am", "12am", etc.
    m12 = re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?\s*([ap]\.?m\.?)", s)
    if m12:
        hour = int(m12.group(1))
        minute = int(m12.group(2)) if m12.group(2) is not None else 0
        meridian = m12.group(3)
        if hour < 1 or hour > 12:
            raise EngineError(f"invalid 12-hour clock hour {hour} in {text!r}; hour must be 1..12 when am/pm is used")
        if minute < 0 or minute > 59:
            raise EngineError(f"invalid minute {minute} in {text!r}; minute must be 0..59")
        is_pm = "p" in meridian
        if hour == 12:
            h24 = 12 if is_pm else 0
        else:
            h24 = hour + 12 if is_pm else hour
        return f"{h24:02d}:{minute:02d}"

    # 24-hour clock with colon: "11:00", "13:00", "09:30", "0:00"
    m24 = re.fullmatch(r"(\d{1,2}):(\d{2})", s)
    if m24:
        hour = int(m24.group(1))
        minute = int(m24.group(2))
        if hour < 0 or hour > 23:
            raise EngineError(f"invalid hour {hour} in {text!r}; hour must be 0..23 in 24-hour clock")
        if minute < 0 or minute > 59:
            raise EngineError(f"invalid minute {minute} in {text!r}; minute must be 0..59")
        return f"{hour:02d}:{minute:02d}"

    raise EngineError(f"unrecognized or invalid time format {text!r}; "
                      "expected e.g. '1:00 pm', '13:00', 'noon', 'midnight'")


def parse_date(text, year):
    """Parse a date string in the context of a reference year into "YYYY-MM-DD".

    Accepts named month formats (e.g. 'Friday May 8', 'May 8th', '8 May'), ISO dates
    (e.g. '2026-05-08'), and unambiguous slash dates where one value is > 12.
    Refuses slash dates where both numbers <= 12 as ambiguous, mismatched weekdays,
    and invalid calendar dates (e.g. 'Feb 30').
    """
    if not isinstance(year, int) or isinstance(year, bool) or year < 1:
        raise EngineError(f"year must be a positive integer, got {year!r}")
    if not isinstance(text, str) or not text.strip():
        raise EngineError("date text must be a non-empty string")

    s = text.strip()

    # Detect and extract weekday if present
    stated_weekday = None
    weekday_match = re.search(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                              r"mon|tue|wed|thu|fri|sat|sun)\b", s, flags=re.IGNORECASE)
    if weekday_match:
        stated_weekday = weekday_match.group(1).lower()
        # Remove weekday from string
        s = s[:weekday_match.start()] + " " + s[weekday_match.end():]

    # Clean ordinals: "8th" -> "8", "1st" -> "1", commas to spaces
    s = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", s, flags=re.IGNORECASE)
    s = s.replace(",", " ")
    s = re.sub(r"\s+", " ", s).strip()

    m_val = None
    d_val = None

    # Check ISO format: YYYY-MM-DD
    m_iso = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m_iso:
        iso_year = int(m_iso.group(1))
        if iso_year != year:
            raise EngineError(f"date year {iso_year} does not match reference year {year}")
        m_val = int(m_iso.group(2))
        d_val = int(m_iso.group(3))

    # Check slash dates: e.g. 05/08/2026 or 25/12/2026 or 12/25
    if m_val is None:
        m_slash = re.fullmatch(r"(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?", s)
        if m_slash:
            n1 = int(m_slash.group(1))
            n2 = int(m_slash.group(2))
            slash_yr = m_slash.group(3)
            if slash_yr is not None:
                sy = int(slash_yr)
                if sy < 100:
                    sy += 2000
                if sy != year:
                    raise EngineError(f"date year {sy} does not match reference year {year}")
            if n1 <= 12 and n2 <= 12:
                raise EngineError(f"ambiguous date format {text!r}: both {n1} and {n2} are <= 12 "
                                  "(cannot determine month vs day); use YYYY-MM-DD or month name")
            if n1 > 12 and n2 <= 12:
                d_val, m_val = n1, n2
            elif n2 > 12 and n1 <= 12:
                m_val, d_val = n1, n2
            else:
                raise EngineError(f"invalid date numbers in {text!r}")

    # Check named month formats: "May 8", "8 May", "May 8 2026", "8 May 2026"
    if m_val is None:
        tokens = s.lower().split()
        month_found = None
        day_found = None
        text_year_found = None

        for tok in tokens:
            if tok in _MONTHS and month_found is None:
                month_found = _MONTHS[tok]
            elif tok.isdigit():
                val = int(tok)
                if val > 31:
                    text_year_found = val
                elif day_found is None:
                    day_found = val
                elif text_year_found is None:
                    text_year_found = val
                else:
                    raise EngineError(f"extra numeric token {tok!r} in date {text!r}")
            else:
                raise EngineError(f"unrecognized token {tok!r} in date {text!r}")

        if month_found is not None and day_found is not None:
            if text_year_found is not None and text_year_found != year:
                raise EngineError(f"date year {text_year_found} does not match reference year {year}")
            m_val = month_found
            d_val = day_found

    if m_val is None or d_val is None:
        raise EngineError(f"could not parse date from {text!r}; "
                          "expected e.g. 'May 8', '8 May', 'Friday May 8', 'YYYY-MM-DD'")

    try:
        parsed_dt = date(year, m_val, d_val)
    except ValueError as e:
        raise EngineError(f"invalid date in {text!r}: {e}") from None

    if stated_weekday is not None:
        actual_weekday_idx = parsed_dt.weekday()
        expected_idx = _WEEKDAYS[stated_weekday]
        if actual_weekday_idx != expected_idx:
            day_name = parsed_dt.strftime("%A")
            raise EngineError(f"{text!r} specifies {stated_weekday.capitalize()}, but {parsed_dt.isoformat()} "
                              f"is a {day_name} (it names a different day)")

    return parsed_dt.isoformat()


def parse_duration(text):
    """Parse a duration string into integer minutes.

    Accepts formats such as '45 minutes', 'about 45 minutes', 'an hour', '1 hour',
    '1.5 hours', '1h30', '90 min', 'half an hour'. Refuses zero, negative, or vague
    durations.
    """
    if not isinstance(text, str) or not text.strip():
        raise EngineError("duration text must be a non-empty string")
    s = text.strip().lower()

    if s.startswith("-") or "negative" in s:
        raise EngineError(f"duration cannot be negative in {text!r}")

    # Strip conversational qualifiers
    s = re.sub(r"^(about|around|approx\.?|approximately)\s+", "", s).strip()

    # Common word forms
    if s in ("an hour", "1 hour", "one hour", "1 hr", "1h"):
        return 60
    if s in ("half an hour", "half hour", "a half hour", "0.5 hour", "0.5 hours"):
        return 30
    if s in ("quarter of an hour", "quarter an hour", "a quarter hour"):
        return 15

    # Mixed hours and minutes: e.g. "1h30", "1h 30m", "1 hour 30 minutes", "1 hour and 30 mins"
    m_mixed = re.fullmatch(r"(\d+)\s*(?:hours?|hrs?|h)\s*(?:and\s*)?(\d+)\s*(?:minutes?|mins?|m)?", s)
    if m_mixed:
        h = int(m_mixed.group(1))
        m = int(m_mixed.group(2))
        total = h * 60 + m
        if total <= 0:
            raise EngineError(f"duration must be greater than 0 minutes, got {total}")
        return total

    # Decimal or integer hours: e.g. "1.5 hours", "2 hours", "1.5h"
    m_hrs = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?|h)", s)
    if m_hrs:
        h_float = float(m_hrs.group(1))
        mins = round(h_float * 60)
        if mins <= 0:
            raise EngineError(f"duration must be greater than 0 minutes, got {mins}")
        return mins

    # Minutes only: e.g. "45 minutes", "90 min", "45 mins", "45m"
    m_mins = re.fullmatch(r"(\d+)\s*(?:minutes?|mins?|m)", s)
    if m_mins:
        mins = int(m_mins.group(1))
        if mins <= 0:
            raise EngineError(f"duration must be greater than 0 minutes, got {mins}")
        return mins

    raise EngineError(f"unrecognized or vague duration {text!r}; "
                      "expected e.g. '45 minutes', '1.5 hours', '1h30', 'half an hour'")


def add_minutes(start, minutes):
    """Add integer minutes to a start time, returning 'HH:MM'.

    Refuses if the end time crosses midnight (a calendar entry cannot wrap to the next day).
    """
    if not isinstance(minutes, int) or isinstance(minutes, bool):
        raise EngineError(f"minutes must be an integer, got {minutes!r}")

    parsed_start = parse_time(start)
    sh, sm = map(int, parsed_start.split(":"))
    start_mins = sh * 60 + sm
    end_mins = start_mins + minutes

    if end_mins < 0 or end_mins >= 24 * 60:
        raise EngineError(f"adding {minutes} minutes to {start!r} crosses midnight ({end_mins // 60:02d}:{end_mins % 60:02d}); "
                          "an entry on the next day is a different calendar entry and must not wrap silently")

    eh = end_mins // 60
    em = end_mins % 60
    return f"{eh:02d}:{em:02d}"


def days_between(a, b):
    """Return the integer number of days between two ISO dates (b - a). Both dates must be 'YYYY-MM-DD'."""
    if not isinstance(a, str) or not isinstance(b, str):
        raise EngineError("dates must be strings in YYYY-MM-DD format")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", a.strip()):
        raise EngineError(f"invalid ISO date {a!r}; expected YYYY-MM-DD")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", b.strip()):
        raise EngineError(f"invalid ISO date {b!r}; expected YYYY-MM-DD")

    try:
        da = date.fromisoformat(a.strip())
        db = date.fromisoformat(b.strip())
    except ValueError as e:
        raise EngineError(f"invalid ISO date: {e}") from None

    return (db - da).days


def is_overdue(due, as_of):
    """Return True if due date is strictly before as_of date. Both dates must be 'YYYY-MM-DD'."""
    if not isinstance(due, str) or not isinstance(as_of, str):
        raise EngineError("dates must be strings in YYYY-MM-DD format")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due.strip()):
        raise EngineError(f"invalid ISO date {due!r}; expected YYYY-MM-DD")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", as_of.strip()):
        raise EngineError(f"invalid ISO date {as_of!r}; expected YYYY-MM-DD")

    try:
        d_due = date.fromisoformat(due.strip())
        d_as_of = date.fromisoformat(as_of.strip())
    except ValueError as e:
        raise EngineError(f"invalid ISO date: {e}") from None

    return d_due < d_as_of


GUIDE = '''Use time for every date, time and duration you read from text. Never parse them yourself.
- `time.parse_date("Friday May 8", 2026)` -> "2026-05-08". Pass the year (the task's or the files'). If a
  weekday is given and does not match, it refuses: re-read the text.
- `time.parse_time("1pm")` -> "13:00". Bare "11" is refused (am or pm?): look for more context in the text.
- `time.parse_duration("about 45 minutes")` -> 45 (int minutes).
- `time.add_minutes("11:30", 45)` -> "12:15" (refuses crossing midnight).
- `time.days_between(a, b)` and `time.is_overdue(due, as_of)` work on ISO dates (e.g. due dates vs today's date).
Feeds ledger (date/start/minutes) and table.'''
