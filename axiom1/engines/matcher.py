"""Matcher engine: pair rows by exact keys or amount and date window without guessing."""
from datetime import date

from ._base import EngineError
from .money import parse_amount

SPEC = {
    "name": "matcher",
    "version": 1,
    "summary": "pair rows by exact keys or amount and date window without guessing; ambiguous candidates go to ambiguous bucket",
    "functions": {
        "match_on": {
            "args": ["left", "right", "keys"],
            "returns": "{pairs, left_only, right_only, ambiguous}",
            "io": False,
            "examples": [
                {
                    "args": {
                        "left": [{"id": "A", "val": 1}, {"id": "B", "val": 2}],
                        "right": [{"id": "A", "val": 1}, {"id": "C", "val": 3}],
                        "keys": ["id"],
                    },
                    "returns": {
                        "pairs": [[{"id": "A", "val": 1}, {"id": "A", "val": 1}]],
                        "left_only": [{"id": "B", "val": 2}],
                        "right_only": [{"id": "C", "val": 3}],
                        "ambiguous": [],
                    },
                },
                {
                    "args": {
                        "left": [{"id": "A", "val": 1}, {"id": "A", "val": 2}],
                        "right": [{"id": "A", "val": 3}],
                        "keys": ["id"],
                    },
                    "returns": {
                        "pairs": [],
                        "left_only": [],
                        "right_only": [],
                        "ambiguous": [
                            {
                                "left": [{"id": "A", "val": 1}, {"id": "A", "val": 2}],
                                "right": [{"id": "A", "val": 3}],
                            }
                        ],
                    },
                },
                {
                    "args": {
                        "left": [{"val": 1}],
                        "right": [],
                        "keys": ["id"],
                    },
                    "refuses": "missing",
                },
            ],
        },
        "match_amount_date": {
            "args": ["left", "right", "amount_field", "date_field", "max_days"],
            "returns": "{pairs, left_only, right_only, ambiguous}",
            "io": False,
            "examples": [
                {
                    "args": {
                        "left": [{"amt": "$100.00", "dt": "2026-05-01"}],
                        "right": [{"amt": "100.00", "dt": "2026-05-02"}],
                        "amount_field": "amt",
                        "date_field": "dt",
                        "max_days": 2,
                    },
                    "returns": {
                        "pairs": [[{"amt": "$100.00", "dt": "2026-05-01"}, {"amt": "100.00", "dt": "2026-05-02"}]],
                        "left_only": [],
                        "right_only": [],
                        "ambiguous": [],
                    },
                },
                {
                    "args": {
                        "left": [],
                        "right": [],
                        "amount_field": "amt",
                        "date_field": "dt",
                        "max_days": -1,
                    },
                    "refuses": "non-negative",
                },
            ],
        },
    },
}


def match_on(left, right, keys):
    """Match rows between left and right on exact equality of listed keys.

    One-to-one matches become pairs. Rows that have multiple candidates on either side
    are placed in the 'ambiguous' bucket for the model to decide.
    Refuses rows missing any of the keys.
    """
    if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)):
        raise EngineError("left and right must be lists of dicts")
    if not isinstance(keys, (list, tuple)) or not keys or not all(isinstance(k, str) and k for k in keys):
        raise EngineError("keys must be a non-empty list of string key names")

    for i, r in enumerate(left):
        if not isinstance(r, dict):
            raise EngineError(f"left row {i} must be a dict")
        for k in keys:
            if k not in r:
                raise EngineError(f"left row {i} is missing key {k!r}")

    for i, r in enumerate(right):
        if not isinstance(r, dict):
            raise EngineError(f"right row {i} must be a dict")
        for k in keys:
            if k not in r:
                raise EngineError(f"right row {i} is missing key {k!r}")

    def _sig(row):
        return tuple(row[k] for k in keys)

    left_by_sig = {}
    for r in left:
        left_by_sig.setdefault(_sig(r), []).append(r)

    right_by_sig = {}
    for r in right:
        right_by_sig.setdefault(_sig(r), []).append(r)

    pairs = []
    left_only = []
    right_only = []
    ambiguous = []

    for sig, l_rows in left_by_sig.items():
        if sig not in right_by_sig:
            left_only.extend(l_rows)
        else:
            r_rows = right_by_sig[sig]
            if len(l_rows) == 1 and len(r_rows) == 1:
                pairs.append([l_rows[0], r_rows[0]])
            else:
                ambiguous.append({"left": l_rows, "right": r_rows})

    for sig, r_rows in right_by_sig.items():
        if sig not in left_by_sig:
            right_only.extend(r_rows)

    return {
        "pairs": pairs,
        "left_only": left_only,
        "right_only": right_only,
        "ambiguous": ambiguous,
    }


def match_amount_date(left, right, amount_field, date_field, max_days):
    """Pair rows with equal amounts whose ISO dates are within max_days.

    Equal amounts are compared using money.parse_amount (never float).
    Unique one-to-one matches become pairs. If any row has multiple candidates within
    the date window, the entire connected group is placed in 'ambiguous'.
    Refuses rows missing named fields, invalid ISO dates, or negative max_days.
    """
    if not isinstance(left, (list, tuple)) or not isinstance(right, (list, tuple)):
        raise EngineError("left and right must be lists of dicts")
    if not isinstance(amount_field, str) or not amount_field:
        raise EngineError("amount_field must be a non-empty string")
    if not isinstance(date_field, str) or not date_field:
        raise EngineError("date_field must be a non-empty string")
    if not isinstance(max_days, int) or isinstance(max_days, bool) or max_days < 0:
        raise EngineError(f"max_days must be a non-negative integer, got {max_days!r}")

    def _parse_row(r, idx, side):
        if not isinstance(r, dict):
            raise EngineError(f"{side} row {idx} must be a dict")
        if amount_field not in r:
            raise EngineError(f"{side} row {idx} is missing amount field {amount_field!r}")
        if date_field not in r:
            raise EngineError(f"{side} row {idx} is missing date field {date_field!r}")

        raw_amt = r[amount_field]
        if isinstance(raw_amt, float):
            raise EngineError(f"{side} row {idx} amount is a float ({raw_amt!r}); pass Decimal or string")
        parsed_amt = parse_amount(raw_amt)

        raw_dt = r[date_field]
        if not isinstance(raw_dt, str):
            raise EngineError(f"{side} row {idx} date must be an ISO date string (YYYY-MM-DD), got {raw_dt!r}")
        try:
            parsed_dt = date.fromisoformat(raw_dt.strip())
        except ValueError as e:
            raise EngineError(f"{side} row {idx} invalid ISO date {raw_dt!r}: {e}") from None

        return parsed_amt, parsed_dt

    left_by_amt = {}
    for i, r in enumerate(left):
        amt, dt = _parse_row(r, i, "left")
        left_by_amt.setdefault(amt, []).append((i, r, dt))

    right_by_amt = {}
    for j, r in enumerate(right):
        amt, dt = _parse_row(r, j, "right")
        right_by_amt.setdefault(amt, []).append((j, r, dt))

    pairs = []
    left_only = []
    right_only = []
    ambiguous = []

    # Amounts only on left
    for amt, l_entries in left_by_amt.items():
        if amt not in right_by_amt:
            left_only.extend([r for _, r, _ in l_entries])

    # Amounts only on right
    for amt, r_entries in right_by_amt.items():
        if amt not in left_by_amt:
            right_only.extend([r for _, r, _ in r_entries])

    # Amounts present on both sides
    for amt, l_entries in left_by_amt.items():
        if amt not in right_by_amt:
            continue
        r_entries = right_by_amt[amt]

        cand_L = {i: [] for i, _, _ in l_entries}
        cand_R = {j: [] for j, _, _ in r_entries}
        lookup_L = {i: r for i, r, _ in l_entries}
        lookup_R = {j: r for j, r, _ in r_entries}

        for i, r_l, dt_l in l_entries:
            for j, r_r, dt_r in r_entries:
                if abs((dt_l - dt_r).days) <= max_days:
                    cand_L[i].append(j)
                    cand_R[j].append(i)

        for i, r_l, _ in l_entries:
            if not cand_L[i]:
                left_only.append(r_l)

        for j, r_r, _ in r_entries:
            if not cand_R[j]:
                right_only.append(r_r)

        visited_L = set()
        visited_R = set()

        for i, _, _ in l_entries:
            if not cand_L[i] or i in visited_L:
                continue

            comp_L = []
            comp_R = []
            queue_L = [i]
            queue_R = []
            visited_L.add(i)

            while queue_L or queue_R:
                while queue_L:
                    curr_i = queue_L.pop(0)
                    comp_L.append(curr_i)
                    for curr_j in cand_L[curr_i]:
                        if curr_j not in visited_R:
                            visited_R.add(curr_j)
                            queue_R.append(curr_j)
                while queue_R:
                    curr_j = queue_R.pop(0)
                    comp_R.append(curr_j)
                    for curr_i in cand_R[curr_j]:
                        if curr_i not in visited_L:
                            visited_L.add(curr_i)
                            queue_L.append(curr_i)

            if len(comp_L) == 1 and len(comp_R) == 1:
                pairs.append([lookup_L[comp_L[0]], lookup_R[comp_R[0]]])
            else:
                ambiguous.append({
                    "left": [lookup_L[idx] for idx in sorted(comp_L)],
                    "right": [lookup_R[idx] for idx in sorted(comp_R)],
                })

    return {
        "pairs": pairs,
        "left_only": left_only,
        "right_only": right_only,
        "ambiguous": ambiguous,
    }
