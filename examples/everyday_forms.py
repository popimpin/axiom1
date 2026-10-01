"""Form mode for the everyday jobs: per job, a FORM (what the model fills: closed choices and text copied as
written) and a PIPELINE (process.py on the engines, written once; the model never writes it).

Each job: {"form": files -> JSON schema, "pipeline": process.py text, "verbatim": callable or tuple,
"task_file": True when the form copies text from the task itself (e.g. an "as of" date)}.
"""
import csv
import io
import json
import re
from pathlib import PurePosixPath

COPIED = "copied exactly as written; '' if it does not say"
TASK_FILE = "task.txt"


def _collapse(text):
    return re.sub(r"\s+", " ", text).strip().lower()


def _in(text, source):
    return not text.strip() or _collapse(text) in _collapse(source)


def _headers(files, path):
    return next(csv.reader(io.StringIO(files[path].lstrip("﻿"))), [])


def _copied_problems(pairs):
    """pairs: (label, text, source_name, source_text) -> problems for copied text that is not in its source."""
    return [f"{label} {text!r} is not in {name}; copy it exactly" for label, text, name, source in pairs
            if isinstance(text, str) and not _in(text, source)]


# shared by every pipeline: read a written date whose year is in the text, or fall back to a reference year
DATE_HELPER = '''
def written_date(text, year):
    """A date as written in a document. A year in the text wins over the reference year."""
    import re as _re
    found = _re.search(r"\\b(19|20)\\d{2}\\b", text)
    return time.parse_date(text, int(found.group(0)) if found else year)
'''

# ---- receipts ----------------------------------------------------------------------------------------------

def receipts_form(files):
    names = sorted(p for p in files if p.startswith("receipts/"))
    return {"type": "object", "required": ["numeric_date_order", "receipts"], "properties": {
        "numeric_date_order": {"type": "string", "enum": ["MDY", "DMY"],
                               "description": "how dates like 03/06/2026 are written in these receipts: MDY = month "
                                              "first, DMY = day first (look for one with a number over 12)"},
        "receipts": {"type": "array", "description": "one entry for EVERY file in receipts/", "items": {
            "type": "object", "required": ["file", "kind", "shop", "date", "total", "category"], "properties": {
                "file": {"type": "string", "enum": names},
                "kind": {"type": "string", "enum": ["receipt", "not_a_receipt"]},
                "shop": {"type": "string", "description": f"the shop's name, {COPIED}"},
                "date": {"type": "string", "description": f"the date of purchase, {COPIED}"},
                "total": {"type": "string", "description": f"the amount actually paid (the total, not a subtotal), {COPIED}"},
                "category": {"type": "string", "enum": ["food", "transport", "home", "other"]}}}}}}


RECEIPTS_PIPELINE = '''import json, re
from pathlib import Path
from axiom_engines import money, table, time
''' + DATE_HELPER + '''
entry = json.loads(Path("entry.json").read_text(encoding="utf-8"))
year = int(entry["today"][:4])
listed = sorted(r["file"] for r in entry["receipts"])
present = sorted(p.as_posix() for p in Path("receipts").iterdir() if p.is_file())
if listed != present:
    raise SystemExit(f"EngineError: the form must have exactly one entry per file in receipts/; "
                     f"missing {sorted(set(present) - set(listed))}, extra {sorted(set(listed) - set(present))}")
rows = []
for r in sorted(entry["receipts"], key=lambda r: r["file"]):
    if r["kind"] != "receipt":
        continue
    d = r["date"].strip()
    date = time.parse_numeric_date(d, entry["numeric_date_order"]) if re.fullmatch(r"[\\d/.-]+", d) else written_date(d, year)
    rows.append({"date": date, "shop": r["shop"].strip(), "category": r["category"],
                 "amount": money.format_amount(money.parse_amount(r["total"]))})
table.write_csv("march_expenses.csv", ["date", "shop", "amount", "category"], rows)
'''

# ---- tidy downloads ----------------------------------------------------------------------------------------

def downloads_form(files):
    exts = sorted({PurePosixPath(p).suffix.lower() for p in files if p.startswith("downloads/")})
    return {"type": "object", "required": ["rules"], "properties": {"rules": {
        "type": "array", "description": "one rule for EVERY file extension in downloads/", "items": {
            "type": "object", "required": ["extension", "folder"], "properties": {
                "extension": {"type": "string", "enum": exts},
                "folder": {"type": "string", "enum": ["Documents", "Photos", "Installers", "Other"]}}}}}}


DOWNLOADS_PIPELINE = '''import json
from pathlib import Path
from axiom_engines import sorter

entry = json.loads(Path("entry.json").read_text(encoding="utf-8"))
loose = sorted(p.as_posix() for p in Path("downloads").iterdir() if p.is_file())
rules = [{"match": "*" + r["extension"], "folder": "downloads/" + r["folder"]} for r in entry["rules"]]
covered = {r["extension"].lower() for r in entry["rules"]}
missing = sorted({Path(p).suffix.lower() for p in loose} - covered)
if missing:
    raise SystemExit(f"EngineError: no rule for the extension(s) {missing}; every file needs a folder")
sorter.apply_moves(sorter.plan_moves(loose, rules))
'''

# ---- merge contacts ----------------------------------------------------------------------------------------

def contacts_form(files):
    sources = sorted(p for p in files if p.startswith("contacts/") and p.endswith(".csv"))
    headers = sorted({h for p in sources for h in _headers(files, p)})
    col = {"type": "string", "enum": headers}
    return {"type": "object", "required": ["sources"], "properties": {"sources": {
        "type": "array", "description": "one entry for EVERY contacts CSV", "items": {
            "type": "object", "required": ["file", "name_column", "email_column", "phone_column", "name_order"],
            "properties": {
                "file": {"type": "string", "enum": sources},
                "name_column": col, "email_column": col,
                "phone_column": {"type": "string", "enum": headers + [""], "description": "'' if the file has none"},
                "name_order": {"type": "string", "enum": ["First Last", "Last, First"],
                               "description": "how names are written in THIS file"}}}}}}


CONTACTS_PIPELINE = '''import json
from pathlib import Path
from axiom_engines import merge, table

entry = json.loads(Path("entry.json").read_text(encoding="utf-8"))
present = sorted(p.as_posix() for p in Path("contacts").glob("*.csv"))
if sorted(s["file"] for s in entry["sources"]) != present:
    raise SystemExit(f"EngineError: the form must have exactly one entry per contacts CSV: {present}")

def person_name(text, order):
    text = " ".join(text.split())
    if order == "Last, First":
        if "," not in text:
            raise SystemExit(f"EngineError: name {text!r} has no comma, but its file was marked 'Last, First'")
        last, first = [part.strip() for part in text.split(",", 1)]
        text = f"{first} {last}"
    return " ".join(w[:1].upper() + w[1:].lower() for w in text.split())

records = []
for s in entry["sources"]:
    data = table.read_csv(s["file"])
    for c in (s["name_column"], s["email_column"], s["phone_column"]):
        if c and c not in data["columns"]:
            raise SystemExit(f"EngineError: {s['file']} has no column {c!r}; it has {data['columns']}")
    for row in data["rows"]:
        email, phone = row[s["email_column"]].strip(), row[s["phone_column"]].strip() if s["phone_column"] else ""
        records.append({"name": person_name(row[s["name_column"]], s["name_order"]),
                        "email": merge.normalize_email(email) if email else "",
                        "phone": merge.normalize_phone(phone) if phone else ""})
result = merge.merge_records(records, ["email", "phone"])
if result["conflicts"]:
    c = result["conflicts"][0]
    raise SystemExit(f"EngineError: the same person has different {c['field']} values {c['values']}")
table.write_csv("contacts.csv", ["name", "email", "phone"],
                [{k: m[k] for k in ("name", "email", "phone")} for m in result["merged"]])
'''

# ---- policy answer with source -----------------------------------------------------------------------------

def policy_form(files):
    return {"type": "object", "required": ["file", "quote"], "properties": {
        "file": {"type": "string", "enum": sorted(p for p in files if p.startswith("policies/"))},
        "quote": {"type": "string", "description": "the ONE sentence that answers the question, copied exactly"}}}


def policy_verbatim(form, files):
    return _copied_problems([("quote", form.get("quote", ""), form.get("file", ""), files.get(form.get("file", ""), ""))])


POLICY_PIPELINE = '''import json, re
from pathlib import Path
from axiom_engines import quote

entry = json.loads(Path("entry.json").read_text(encoding="utf-8"))
text = Path(entry["file"]).read_text(encoding="utf-8")
found = quote.verify_quote(text, entry["quote"])
days = sorted(set(re.findall(r"\\b(\\d+)\\s+days?\\b", entry["quote"])))
if len(days) != 1:
    raise SystemExit(f"EngineError: the quote must state exactly one number of days; it states {days or 'none'}")
Path("answer.md").write_text(f"**{days[0]} days.**\\n\\n> {entry['quote'].strip()}\\n\\n"
                             f"Source: {entry['file']} (line {found['line']})\\n", encoding="utf-8")
'''

# ---- reconcile bank statement ------------------------------------------------------------------------------

def reconcile_form(files):
    csvs = sorted(p for p in files if p.endswith(".csv"))
    headers = sorted({h for p in csvs for h in _headers(files, p)})
    side = {"type": "object", "required": ["file", "date_column", "description_column", "amount_column", "date_order"],
            "properties": {"file": {"type": "string", "enum": csvs},
                           "date_column": {"type": "string", "enum": headers},
                           "description_column": {"type": "string", "enum": headers},
                           "amount_column": {"type": "string", "enum": headers},
                           "date_order": {"type": "string", "enum": ["MDY", "DMY"],
                                          "description": "for dates like 03/06/2026 in THIS file (ISO dates read either way)"}}}
    return {"type": "object", "required": ["budget", "statement"], "properties": {"budget": side, "statement": side}}


RECONCILE_PIPELINE = '''import json
from pathlib import Path
from axiom_engines import matcher, money, table, time

entry = json.loads(Path("entry.json").read_text(encoding="utf-8"))

def load(side):
    data = table.read_csv(side["file"])
    for c in (side["date_column"], side["description_column"], side["amount_column"]):
        if c not in data["columns"]:
            raise SystemExit(f"EngineError: {side['file']} has no column {c!r}; it has {data['columns']}")
    return [{"date": time.parse_numeric_date(r[side["date_column"]], side["date_order"]),
             "description": r[side["description_column"]].strip(),
             "amount": money.format_amount(abs(money.parse_amount(r[side["amount_column"]])))} for r in data["rows"]]

budget, statement = load(entry["budget"]), load(entry["statement"])
if entry["budget"]["file"] == entry["statement"]["file"]:
    raise SystemExit("EngineError: the budget and the statement must be different files")
paired = matcher.match_amount_date(budget, statement, "amount", "date", 0)
if paired["ambiguous"]:
    raise SystemExit(f"EngineError: rows that could pair more than one way: {paired['ambiguous'][0]}")
left, right, rows = list(paired["left_only"]), list(paired["right_only"]), []
for b in list(left):
    name = b["description"].split(" (")[0].lower()
    same = [s for s in right if s["date"] == b["date"] and name in s["description"].lower()]
    if len(same) == 1:
        rows.append({**same[0], "issue": "amount_mismatch"})
        left.remove(b)
        right.remove(same[0])
rows += [{**s, "issue": "missing_from_budget"} for s in right] + [{**b, "issue": "not_on_statement"} for b in left]
table.write_csv("reconciliation.csv", ["date", "description", "amount", "issue"], sorted(rows, key=lambda r: r["date"]))
'''

# ---- fill claim form ---------------------------------------------------------------------------------------

def _template(files):
    return json.loads(files["forms/form_template.json"])


def claim_form(files):
    fields = {k: {"type": "string", "description": f"{k.replace('_', ' ')}, {COPIED}"} for k in _template(files)}
    fields["description"] = {"type": "string", "description": "the sentence of the letter that says what happened, "
                                                              "copied exactly"}
    letters = sorted(p for p in files if p.startswith("letters/"))
    return {"type": "object", "required": ["letter", *fields], "properties": {
        "letter": {"type": "string", "enum": letters}, **fields}}


def claim_verbatim(form, files):
    source = files.get(form.get("letter", ""), "")
    return _copied_problems([(k, v, form.get("letter"), source) for k, v in form.items() if k != "letter"])


CLAIM_PIPELINE = '''import json
from pathlib import Path
from axiom_engines import merge, table, time
''' + DATE_HELPER + '''
entry = json.loads(Path("entry.json").read_text(encoding="utf-8"))
template = json.loads(Path("forms/form_template.json").read_text(encoding="utf-8"))
year = int(entry["today"][:4])
out = {}
for key in template:
    value = entry[key].strip()
    if value and key == "incident_date":
        value = written_date(value, year)
    elif value and key == "phone":
        value = merge.normalize_phone(value)
    elif value and key == "email":
        value = merge.normalize_email(value)
    out[key] = value
table.write_json("claim_form.json", out)
'''

# ---- overdue invoices --------------------------------------------------------------------------------------

def invoices_form(files):
    names = sorted(p for p in files if p.startswith("invoices/"))
    return {"type": "object", "required": ["as_of", "invoices"], "properties": {
        "as_of": {"type": "string", "description": "the date the task asks about, copied exactly from the task"},
        "invoices": {"type": "array", "description": "one entry for EVERY invoice file", "items": {
            "type": "object", "required": ["file", "invoice", "client", "issued", "terms", "due", "amount", "status"],
            "properties": {
                "file": {"type": "string", "enum": names},
                "invoice": {"type": "string", "description": f"the invoice number, {COPIED}"},
                "client": {"type": "string", "description": f"who it is billed to, {COPIED}"},
                "issued": {"type": "string", "description": f"the issue date, {COPIED}"},
                "terms": {"type": "string", "description": f"payment terms like 'Net 14', {COPIED}"},
                "due": {"type": "string", "description": f"the due date if one is written, {COPIED}"},
                "amount": {"type": "string", "description": f"the amount due, {COPIED}"},
                "status": {"type": "string", "enum": ["open", "paid"],
                           "description": "paid only if the invoice says it was paid"}}}}}}


def invoices_verbatim(form, files):
    pairs = [("as_of", form.get("as_of", ""), "the task", files.get(TASK_FILE, ""))]
    for i, inv in enumerate(form.get("invoices", [])):
        src = files.get(inv.get("file", ""), "")
        pairs += [(f"invoice {i} {k}", inv.get(k, ""), inv.get("file"), src)
                  for k in ("invoice", "client", "issued", "terms", "due", "amount")]
    return _copied_problems(pairs)


INVOICES_PIPELINE = '''import json, re
from pathlib import Path
from axiom_engines import money, table, time
''' + DATE_HELPER + '''
entry = json.loads(Path("entry.json").read_text(encoding="utf-8"))
year = int(entry["today"][:4])
present = sorted(p.as_posix() for p in Path("invoices").iterdir() if p.is_file())
if sorted(i["file"] for i in entry["invoices"]) != present:
    raise SystemExit(f"EngineError: the form must have exactly one entry per invoice file: {present}")
as_of = written_date(entry["as_of"], year)
overdue = []
for inv in sorted(entry["invoices"], key=lambda i: i["file"]):
    if inv["status"] == "paid":
        continue
    if inv["due"].strip():
        due = written_date(inv["due"], year)
    else:
        net = re.findall(r"\\d+", inv["terms"])
        if len(net) != 1:
            raise SystemExit(f"EngineError: {inv['file']}: no due date and terms {inv['terms']!r} give no number of days")
        due = time.add_days(written_date(inv["issued"], year), int(net[0]))
    if time.is_overdue(due, as_of):
        overdue.append({"invoice": re.sub(r"\\D", "", inv["invoice"]), "client": inv["client"].strip(), "due_date": due,
                        "amount": money.format_amount(money.parse_amount(inv["amount"]))})
table.write_csv("overdue.csv", ["invoice", "client", "due_date", "amount"], overdue)
totals = money.total_by(overdue, "client", "amount")
table.write_csv("owed_by_client.csv", ["client", "total"],
                [{"client": c, "total": money.format_amount(t)} for c, t in sorted(totals.items())])
'''

FORMS = {
    "receipts-to-spreadsheet": {"form": receipts_form, "pipeline": RECEIPTS_PIPELINE,
                                "verbatim": ("receipts", "file", ["shop", "date", "total"])},
    "tidy-downloads": {"form": downloads_form, "pipeline": DOWNLOADS_PIPELINE, "verbatim": None},
    "merge-contacts": {"form": contacts_form, "pipeline": CONTACTS_PIPELINE, "verbatim": None},
    "policy-answer-with-source": {"form": policy_form, "pipeline": POLICY_PIPELINE, "verbatim": policy_verbatim},
    "reconcile-bank-statement": {"form": reconcile_form, "pipeline": RECONCILE_PIPELINE, "verbatim": None},
    "fill-claim-form": {"form": claim_form, "pipeline": CLAIM_PIPELINE, "verbatim": claim_verbatim},
    "overdue-invoices": {"form": invoices_form, "pipeline": INVOICES_PIPELINE, "verbatim": invoices_verbatim,
                         "task_file": True},
}
