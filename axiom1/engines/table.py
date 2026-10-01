"""Tables in and out: exact columns, nothing silently dropped or invented."""
import csv
import io
import json
import math
from decimal import Decimal
from pathlib import Path

from ._base import EngineError, relative_path

SPEC = {
    "name": "table",
    "version": 1,
    "summary": "write CSV/JSON with exact columns; read CSV into rows",
    "functions": {
        "csv_text": {
            "args": ["columns", "rows"], "returns": "the CSV text", "io": False,
            "examples": [
                {"args": {"columns": ["date", "title"], "rows": [{"date": "2026-05-08", "title": "Team retro"}]},
                 "returns": "date,title\n2026-05-08,Team retro\n"},
                {"args": {"columns": ["name", "note"], "rows": [{"name": "Lee", "note": None}]},
                 "returns": "name,note\nLee,\n"},
                {"args": {"columns": ["total"], "rows": [{"total": Decimal("12.50")}]},
                 "returns": "total\n12.50\n"},
                {"args": {"columns": ["date", "title"], "rows": [{"date": "2026-05-08"}]},
                 "refuses": "missing"},
                {"args": {"columns": ["date"], "rows": [{"date": "2026-05-08", "title": "x"}]},
                 "refuses": "not in the columns"},
                {"args": {"columns": ["total"], "rows": [{"total": 0.1 + 0.2}]},
                 "refuses": "float"},
                {"args": {"columns": ["a", "a"], "rows": []}, "refuses": "duplicate"},
            ]},
        "json_text": {
            "args": ["data"], "returns": "the JSON text, stable key order", "io": False,
            "examples": [
                {"args": {"data": {"b": 1, "a": "x"}}, "returns": '{\n  "a": "x",\n  "b": 1\n}\n'},
                {"args": {"data": {"total": float("nan")}}, "refuses": "not a number"},
            ]},
        "write_csv": {"args": ["path", "columns", "rows"], "returns": "{path, rows}", "io": True, "examples": []},
        "write_json": {"args": ["path", "data"], "returns": "{path}", "io": True, "examples": []},
        "read_csv": {"args": ["path"], "returns": "{columns, rows}", "io": True, "examples": []},
    },
}


def _cell(column, value):
    if value is None:
        return ""
    if isinstance(value, bool):
        raise EngineError(f"column {column!r}: a true/false value is ambiguous in a table; pass the text to write")
    if isinstance(value, float):
        raise EngineError(f"column {column!r}: {value!r} is a float, which can print as 0.30000000000000004; "
                          "pass a string or a Decimal (the money engine returns Decimals)")
    if isinstance(value, (str, int, Decimal)):
        return str(value)
    raise EngineError(f"column {column!r}: cannot write a {type(value).__name__} into a cell")


def csv_text(columns, rows):
    """The CSV for these rows, with exactly these columns in this order. Every row must have every column
    (None for a deliberately blank cell) and nothing else."""
    if not columns or not all(isinstance(c, str) and c for c in columns):
        raise EngineError("columns must be a non-empty list of names")
    if len(set(columns)) != len(columns):
        raise EngineError(f"duplicate column names in {columns}")
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(columns)
    for n, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise EngineError(f"row {n} must be a dict of column -> value")
        missing = [c for c in columns if c not in row]
        if missing:
            raise EngineError(f"row {n} is missing {missing}; use None for a cell that should be blank")
        extra = [k for k in row if k not in columns]
        if extra:
            raise EngineError(f"row {n} has {extra}, which are not in the columns {columns}")
        writer.writerow([_cell(c, row[c]) for c in columns])
    return out.getvalue()


def _plain(value):
    if isinstance(value, float) and not math.isfinite(value):
        raise EngineError(f"{value!r} is not a number JSON can hold")
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


def json_text(data):
    """JSON with sorted keys and two-space indent, so the same data always gives the same file."""
    try:
        return json.dumps(_plain(data), indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False) + "\n"
    except TypeError as e:
        raise EngineError(f"cannot write as JSON: {e}") from None


def write_csv(path, columns, rows):
    """Write csv_text(columns, rows) to a path inside the job's folder."""
    text = csv_text(columns, rows)
    p = Path(relative_path(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="")
    return {"path": path, "rows": len(rows)}


def write_json(path, data):
    """Write json_text(data) to a path inside the job's folder."""
    text = json_text(data)
    p = Path(relative_path(path))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="")
    return {"path": path}


def read_csv(path):
    """Read a CSV with a header row. Refuses a row with a different number of cells than the header,
    rather than padding or cutting it."""
    p = Path(relative_path(path))
    if not p.is_file():
        raise EngineError(f"{path!r} does not exist")
    text = p.read_text(encoding="utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    try:
        columns = next(reader)
    except StopIteration:
        raise EngineError(f"{path!r} is empty") from None
    rows = []
    for n, cells in enumerate(reader, 2):
        if not cells:
            continue
        if len(cells) != len(columns):
            raise EngineError(f"{path!r} line {n} has {len(cells)} cells; the header has {len(columns)}")
        rows.append(dict(zip(columns, cells)))
    return {"columns": columns, "rows": rows}
