"""Merge engine: normalize emails and phones, and merge record sets with transitive grouping."""
import re

from ._base import EngineError

SPEC = {
    "name": "merge",
    "version": 1,
    "summary": "normalize emails and phones, and merge contact record sets with transitive grouping",
    "functions": {
        "normalize_email": {
            "args": ["text"],
            "returns": "lowercase trimmed email string",
            "io": False,
            "examples": [
                {"args": {"text": " Alice@Example.Com "}, "returns": "alice@example.com"},
                {"args": {"text": "not-an-email"}, "refuses": "exactly one '@'"},
                {"args": {"text": "alice@nodot"}, "refuses": "dot"},
            ],
        },
        "normalize_phone": {
            "args": ["text"],
            "returns": "digit string keeping leading plus",
            "io": False,
            "examples": [
                {"args": {"text": "+1 (555) 123-4567"}, "returns": "+15551234567"},
                {"args": {"text": "555-1234"}, "returns": "5551234"},
                {"args": {"text": "123"}, "refuses": "fewer than 7 digits"},
                {"args": {"text": "555-CALL"}, "refuses": "letters"},
            ],
        },
        "merge_records": {
            "args": ["records", "match_on"],
            "returns": "{merged, conflicts}",
            "io": False,
            "examples": [
                {
                    "args": {
                        "records": [
                            {"name": "Alice", "email": "alice@example.com", "phone": "555-1234"},
                            {"name": "Alice", "email": "alice@example.com", "phone": "", "role": "Dev"},
                        ],
                        "match_on": ["email", "phone"],
                    },
                    "returns": {
                        "merged": [
                            {
                                "name": "Alice",
                                "email": "alice@example.com",
                                "phone": "5551234",
                                "role": "Dev",
                                "sources": 2,
                            }
                        ],
                        "conflicts": [],
                    },
                },
                {
                    "args": {
                        "records": [
                            {"name": "Alice", "email": "alice@example.com"},
                            {"name": "Bob", "email": "alice@example.com"},
                        ],
                        "match_on": ["email"],
                    },
                    "returns": {
                        "merged": [],
                        "conflicts": [
                            {
                                "records": [
                                    {"name": "Alice", "email": "alice@example.com"},
                                    {"name": "Bob", "email": "alice@example.com"},
                                ],
                                "field": "name",
                                "values": ["Alice", "Bob"],
                            }
                        ],
                    },
                },
                {
                    "args": {"records": [], "match_on": []},
                    "refuses": "non-empty",
                },
            ],
        },
    },
}


def normalize_email(text):
    """Normalize an email address: trimmed, lowercase.

    Refuses anything without exactly one '@' or without a dot in the domain.
    """
    if not isinstance(text, str):
        raise EngineError("email must be a string")
    s = text.strip().lower()
    if s.count("@") != 1:
        raise EngineError(f"email {text!r} must contain exactly one '@'")
    local, domain = s.split("@")
    if not local:
        raise EngineError(f"email {text!r} local part cannot be empty")
    if not domain or "." not in domain or domain.startswith(".") or domain.endswith("."):
        raise EngineError(f"email {text!r} domain must contain a dot")
    return s


def normalize_phone(text):
    """Normalize a phone number: digits only, preserving a leading '+'.

    Refuses fewer than 7 digits, invalid characters, or letters.
    """
    if not isinstance(text, str):
        raise EngineError("phone must be a string")
    if re.search(r"[A-Za-z]", text):
        raise EngineError(f"phone number {text!r} contains letters")

    s = text.strip()
    has_plus = s.startswith("+")
    digits = "".join(c for c in s if c.isdigit())
    if len(digits) < 7:
        raise EngineError(f"phone number {text!r} has fewer than 7 digits")
    if re.search(r"[^\d+\s\-().]", s):
        raise EngineError(f"invalid characters in phone number {text!r}")

    return ("+" + digits) if has_plus else digits


def merge_records(records, match_on):
    """Merge records transitively based on matching fields.

    Two records are the same person if ANY match_on field is equal after normalization
    (blank values never match).
    For each field in a merged group:
      - All non-blank values agree -> merged field takes that value.
      - Only blanks -> merged field takes "".
      - Values disagree -> group is routed to 'conflicts' as {records, field, values} and excluded from 'merged'.
    Tracks 'sources' count per merged record, ordered deterministically by first appearance.
    """
    if not isinstance(records, (list, tuple)):
        raise EngineError("records must be a list of dicts")
    if not isinstance(match_on, (list, tuple)) or not match_on or not all(isinstance(f, str) and f for f in match_on):
        raise EngineError("match_on must be a non-empty list of field names")

    for i, r in enumerate(records):
        if not isinstance(r, dict):
            raise EngineError(f"record {i} must be a dict")

    n = len(records)
    parent = list(range(n))

    def _find(i):
        path = []
        while parent[i] != i:
            path.append(i)
            i = parent[i]
        for node in path:
            parent[node] = i
        return i

    def _union(i, j):
        root_i = _find(i)
        root_j = _find(j)
        if root_i != root_j:
            parent[root_i] = root_j

    key_owners = {}

    for i, r in enumerate(records):
        for f in match_on:
            val = r.get(f)
            if val is None:
                continue
            s_val = str(val).strip()
            if not s_val:
                continue
            if f == "email":
                norm_val = normalize_email(s_val)
            elif f == "phone":
                norm_val = normalize_phone(s_val)
            else:
                norm_val = s_val.lower()

            k = (f, norm_val)
            if k in key_owners:
                _union(i, key_owners[k])
            else:
                key_owners[k] = i

    groups = {}
    for i in range(n):
        root = _find(i)
        groups.setdefault(root, []).append(i)

    # Sort groups deterministically by earliest record index in each group
    sorted_groups = sorted(groups.values(), key=lambda idxs: min(idxs))

    merged = []
    conflicts = []

    for idxs in sorted_groups:
        group_records = [records[i] for i in idxs]

        # Gather all field names seen across records in order of appearance
        all_fields = []
        for r in group_records:
            for k in r.keys():
                if k not in all_fields:
                    all_fields.append(k)

        group_has_conflict = False
        merged_row = {}

        for f in all_fields:
            non_blanks = []
            for r in group_records:
                v = r.get(f)
                if v is not None and not (isinstance(v, str) and not v.strip()):
                    if f == "email":
                        non_blanks.append(normalize_email(str(v)))
                    elif f == "phone":
                        non_blanks.append(normalize_phone(str(v)))
                    else:
                        non_blanks.append(v)

            unique_vals = list(dict.fromkeys(non_blanks))

            if len(unique_vals) == 0:
                merged_row[f] = ""
            elif len(unique_vals) == 1:
                merged_row[f] = unique_vals[0]
            else:
                conflicts.append({
                    "records": group_records,
                    "field": f,
                    "values": unique_vals,
                })
                group_has_conflict = True
                break

        if not group_has_conflict:
            merged_row["sources"] = len(group_records)
            merged.append(merged_row)

    return {"merged": merged, "conflicts": conflicts}
