"""Everyday tasks: jobs a non-coder would hand off, judged so that they never have to check the result.

Each task is asked the way a person would ask it, over realistic, messy (synthetic, never real) data.
The acceptance check is written once per task TYPE and derives the truth from the data. Its output
is private except `PUBLIC:` lines, so the agent learns what is wrong without being handed the answer.
Every delivery also passes Axiom-1's universal guards (nothing lost, well-formed CSV/JSON).

Each task carries a correct solution (gold) and wrong-but-plausible ones (controls). `validate` proves
the check FAILS on the untouched data, PASSES on gold and REJECTS every control, end to end through
Axiom-1's verifier, before any agent is pointed at it.

    python examples/everyday_tasks.py validate
    python examples/everyday_tasks.py run --agents 2     # needs NEBIUS_API_KEY and Docker
"""
import argparse
import asyncio
import csv
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from axiom1 import Axiom  # noqa: E402
from axiom1.sandbox import DockerSandbox, LocalSandbox  # noqa: E402

IMAGE = "python:3.13-slim"
CHECK = ["python", "-I", ".axiom_check/check.py"]


# ---------------------------------------------------------------------------------------------------
# 1. receipts -> a spreadsheet for the accountant
# ---------------------------------------------------------------------------------------------------
VENDORS = {"Corner Cafe": "food", "Green Grocer": "food", "Metro Transit": "transport", "City Cabs": "transport",
           "Hardware World": "home", "Lamp & Linen": "home", "Page Turners Books": "other", "Post Office": "other"}
FORMATS = [
    "{vendor}\n{date_slash}\nTOTAL ${amount}\nThank you!",
    "Receipt - {vendor}\nDate: {date_iso}\nItems: 3\nAmount due: {amount} USD",
    "*** {vendor_upper} ***\n{date_text}\nSubtotal {sub}\nTax {tax}\nTotal {amount}",
    "{vendor}\nPaid on {date_text} by card ending 4421\n{amount}",
]


def receipts_data(rng):
    files, truth = {}, []
    for i in range(12):
        vendor = rng.choice(sorted(VENDORS))
        day = rng.randint(1, 31)
        amount = round(rng.uniform(3, 180), 2)
        sub = round(amount / 1.08, 2)
        fields = {"vendor": vendor, "vendor_upper": vendor.upper(), "amount": f"{amount:,.2f}",
                  "sub": f"{sub:,.2f}", "tax": f"{amount - sub:,.2f}",
                  "date_slash": f"03/{day:02d}/2026", "date_iso": f"2026-03-{day:02d}",
                  "date_text": f"March {day}, 2026"}
        files[f"receipts/receipt_{i + 1:02d}.txt"] = rng.choice(FORMATS).format(**fields) + "\n"
        truth.append({"date": f"2026-03-{day:02d}", "vendor": vendor, "amount": amount, "category": VENDORS[vendor]})
    files["receipts/README.txt"] = "Scans of my March receipts, typed up.\n"
    return files, truth


RECEIPTS_CHECK = r'''
import csv, json, sys
from pathlib import Path
truth = json.loads(Path(".axiom_check/truth.json").read_text())
out = Path("march_expenses.csv")
if not out.exists():
    print("PUBLIC: march_expenses.csv was not found in the workspace"); sys.exit(1)
rows = list(csv.DictReader(out.open(encoding="utf-8-sig")))
cols = {c.strip().lower() for c in (rows[0].keys() if rows else [])}
need = {"date", "shop", "amount", "category"}
if not need <= cols:
    print(f"PUBLIC: the spreadsheet needs the columns date, shop, amount, category; it has {sorted(cols)}"); sys.exit(1)
norm = lambda r: {k.strip().lower(): (v or "").strip() for k, v in r.items()}
rows = [norm(r) for r in rows]
def money(s):
    try: return round(float(s.replace("$", "").replace(",", "").replace("USD", "").strip()), 2)
    except ValueError: return None
got = sorted((r["date"], money(r["amount"])) for r in rows)
want = sorted((t["date"], t["amount"]) for t in truth)
problems = []
if len(rows) != len(truth):
    problems.append(f"PUBLIC: there are {len(truth)} receipts but the spreadsheet has {len(rows)} rows")
missing = len([w for w in want if w not in got])
if missing:
    problems.append(f"PUBLIC: {missing} receipt(s) are missing or have the wrong date or amount (dates as YYYY-MM-DD)")
cats = {(t["date"], t["amount"]): t["category"] for t in truth}
wrong = sum(1 for r in rows if cats.get((r["date"], money(r["amount"]))) not in (None, r["category"].lower()))
if wrong:
    problems.append(f"PUBLIC: {wrong} row(s) have the wrong category (food, transport, home or other)")
if problems:
    print("\n".join(problems)); print("expected", want); sys.exit(1)
print("PUBLIC: every receipt is in the spreadsheet with the right date, amount and category")
'''


def receipts_gold(files, truth):
    """A real solution, from the receipt files only (and common knowledge of what kind of shop each is)."""
    months = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July",
                                          "August", "September", "October", "November", "December"], 1)}
    rows = []
    for rel, text in sorted(files.items()):
        if not rel.startswith("receipts/receipt_"):
            continue
        low = text.lower()
        vendor = next(v for v in VENDORS if v.lower() in low)
        if m := re.search(r"(\d{4})-(\d{2})-(\d{2})", text):
            date = m.group(0)
        elif m := re.search(r"(\d{2})/(\d{2})/(\d{4})", text):
            date = f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
        else:
            m = re.search(r"([A-Z][a-z]+) (\d{1,2}), (\d{4})", text)
            date = f"{m.group(3)}-{months[m.group(1)]:02d}-{int(m.group(2)):02d}"
        amounts = re.findall(r"(?i)(?:total|amount due)[^\d\n]*([\d,]+\.\d{2})", text) or \
            re.findall(r"^\$?([\d,]+\.\d{2})\s*$", text, re.M)
        rows.append([date, vendor, amounts[-1].replace(",", ""), VENDORS[vendor]])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "shop", "amount", "category"])
    w.writerows(rows)
    return {"march_expenses.csv": buf.getvalue()}


def receipts_controls(files, truth):
    gold = receipts_gold(files, truth)["march_expenses.csv"].splitlines()
    one_short = "\n".join(gold[:-1]) + "\n"
    big = max(truth, key=lambda t: t["amount"])
    misparsed = "\n".join(l.replace(f"{big['amount']:.2f}", f"{big['amount'] / 100:.2f}") for l in gold) + "\n"
    all_other = "\n".join([gold[0]] + [l.rsplit(",", 1)[0] + ",other" for l in gold[1:]]) + "\n"
    return {"one receipt missing": {"march_expenses.csv": one_short},
            "an amount misread": {"march_expenses.csv": misparsed},
            "every category 'other'": {"march_expenses.csv": all_other},
            "perfect sheet but a receipt deleted": {"march_expenses.csv": "\n".join(gold) + "\n",
                                                     "__delete__": ["receipts/receipt_01.txt"]}}


# ---------------------------------------------------------------------------------------------------
# 2. tidy a downloads folder
# ---------------------------------------------------------------------------------------------------
KINDS = {"Documents": [".pdf", ".docx", ".txt", ".xlsx"], "Photos": [".jpg", ".png", ".heic"],
         "Installers": [".exe", ".msi", ".dmg"], "Other": [".zip", ".mp3", ".ics"]}


def downloads_data(rng):
    names = ["tax_return_2025", "IMG_4471", "zoom_installer", "wedding_playlist", "lease_agreement", "beach",
             "invoice_8812", "setup_v2", "screenshot 2026-03-02", "backup", "dentist_appointment", "notes"]
    files, truth = {}, {}
    for i, base in enumerate(names):
        kind = list(KINDS)[i % 4]
        ext = rng.choice(KINDS[kind])
        name = f"{base}{ext}"
        files[f"downloads/{name}"] = f"pretend contents of {name} #{rng.randint(0, 10 ** 9)}\n"
        truth[name] = kind
    return files, truth


DOWNLOADS_CHECK = r'''
import json, sys
from pathlib import Path
truth = json.loads(Path(".axiom_check/truth.json").read_text())
root = Path("downloads")
loose = [p.name for p in root.iterdir() if p.is_file()] if root.exists() else []
problems = []
if loose:
    problems.append(f"PUBLIC: {len(loose)} file(s) are still loose in downloads/ instead of in a subfolder")
for name, kind in truth.items():
    if not (root / kind / name).is_file():
        problems.append(f"PUBLIC: {name} is not in downloads/{kind}/")
if problems:
    print("\n".join(problems[:15])); sys.exit(1)
print("PUBLIC: every file is in the right subfolder and nothing is loose")
'''


def downloads_gold(files, truth):
    """A real solution, from the file names only: sort by extension."""
    kind_of = {ext: kind for kind, exts in KINDS.items() for ext in exts}
    moves = []
    for rel in sorted(files):
        name = rel.split("/", 1)[1]
        moves.append((rel, f"downloads/{kind_of[Path(name).suffix.lower()]}/{name}"))
    return {"__move__": moves}


def downloads_controls(files, truth):
    moves = downloads_gold(files, truth)["__move__"]
    photo = next(n for n, k in truth.items() if k == "Photos")
    wrong = [(s, d.replace("/Photos/", "/Documents/") if photo in d else d) for s, d in moves]
    return {"one file left loose": {"__move__": moves[1:]},
            "a photo filed under Documents": {"__move__": wrong},
            "tidy, but a file deleted": {"__move__": moves[1:], "__delete__": [moves[0][0]]}}


# ---------------------------------------------------------------------------------------------------
# 3. merge two contact lists
# ---------------------------------------------------------------------------------------------------
PEOPLE = ["Ana Lopez", "Ben Okafor", "Chloe Martin", "Dev Patel", "Erin Walsh", "Farid Haddad", "Grace Kim",
          "Hugo Silva", "Ines Rossi", "Jamal Carter"]


def contacts_data(rng):
    truth, phone_rows, email_rows = [], [], []
    for i, name in enumerate(PEOPLE):
        first, last = name.split()
        email = f"{first.lower()}.{last.lower()}@example.com"
        digits = f"555{rng.randint(1000000, 9999999)}"
        # the truth holds a phone only where a source file has one: people who are only in the email
        # export have none anywhere, and inventing one is fabrication (an earlier version demanded it,
        # which made the task unsolvable and failed two agents that were right)
        truth.append({"name": name, "email": email, "phone": digits if i % 3 != 2 else ""})
        if i % 3 != 2:      # some people only in the phone export
            phone_rows.append([name.upper() if i % 4 == 0 else name, f"({digits[:3]}) {digits[3:6]}-{digits[6:]}",
                               email.upper() if i % 2 else email])
        if i % 3 != 1:      # some only in the email export
            email_rows.append([f"{last}, {first}", email if i % 2 else email.capitalize(), ""])
    def to_csv(header, rows):
        buf = io.StringIO()
        csv.writer(buf).writerows([header] + rows)
        return buf.getvalue()
    files = {"contacts/phone_export.csv": to_csv(["Name", "Mobile", "E-mail"], phone_rows),
             "contacts/email_export.csv": to_csv(["Contact", "Email Address", "Phone"], email_rows)}
    return files, truth


CONTACTS_CHECK = r'''
import csv, json, re, sys
from pathlib import Path
truth = json.loads(Path(".axiom_check/truth.json").read_text())
out = Path("contacts.csv")
if not out.exists():
    print("PUBLIC: contacts.csv was not found in the workspace"); sys.exit(1)
rows = [{k.strip().lower(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(out.open(encoding="utf-8-sig"))]
if not rows or not {"name", "email", "phone"} <= set(rows[0]):
    print("PUBLIC: contacts.csv needs the columns name, email, phone"); sys.exit(1)
emails = [r["email"].lower() for r in rows]
problems = []
dupes = len(emails) - len(set(emails))
if dupes:
    problems.append(f"PUBLIC: {dupes} person(s) appear more than once (the same email in a different case counts)")
want = {t["email"]: t for t in truth}
missing = [e for e in want if e not in set(emails)]
if missing:
    problems.append(f"PUBLIC: {len(missing)} person(s) from the two lists are missing")
digits = lambda s: re.sub(r"\D", "", s)
known = [r for r in rows if r["email"].lower() in want]
no_phone = sum(1 for r in known if want[r["email"].lower()]["phone"]
               and digits(r["phone"])[-10:] != want[r["email"].lower()]["phone"])
if no_phone:
    problems.append(f"PUBLIC: {no_phone} person(s) are missing their phone number or have the wrong one")
invented = sum(1 for r in known if not want[r["email"].lower()]["phone"] and digits(r["phone"]))
if invented:
    problems.append(f"PUBLIC: {invented} person(s) have a phone number that appears in neither list")
if problems:
    print("\n".join(problems)); sys.exit(1)
print("PUBLIC: one row per person, nobody missing, every known phone number kept")
'''


def contacts_gold(files, truth):
    """A real solution, from the two exports only: it never reads the truth."""
    people = {}
    for r in csv.DictReader(io.StringIO(files["contacts/phone_export.csv"])):
        e = r["E-mail"].strip().lower()
        people.setdefault(e, {"name": r["Name"].title(), "phone": ""})["phone"] = re.sub(r"\D", "", r["Mobile"])
    for r in csv.DictReader(io.StringIO(files["contacts/email_export.csv"])):
        e = r["Email Address"].strip().lower()
        last, first = [x.strip() for x in r["Contact"].split(",", 1)]
        p = people.setdefault(e, {"name": f"{first} {last}", "phone": ""})
        p["phone"] = p["phone"] or re.sub(r"\D", "", r["Phone"])
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "email", "phone"])
    for e, p in sorted(people.items()):
        w.writerow([p["name"], e, p["phone"]])
    return {"contacts.csv": buf.getvalue()}


def contacts_controls(files, truth):
    gold = contacts_gold(files, truth)["contacts.csv"].splitlines()
    first_email = gold[1].split(",")[1]
    dup = gold + [gold[1].replace(first_email, first_email.upper())]
    no_phones = [gold[0]] + [l.rsplit(",", 1)[0] + "," for l in gold[1:]]
    invented = [gold[0]] + [(l + "5550000000") if l.endswith(",") else l for l in gold[1:]]
    return {"same person twice (email case differs)": {"contacts.csv": "\n".join(dup) + "\n"},
            "a person dropped": {"contacts.csv": "\n".join(gold[:-1]) + "\n"},
            "phone numbers lost": {"contacts.csv": "\n".join(no_phones) + "\n"},
            "phone numbers invented": {"contacts.csv": "\n".join(invented) + "\n"}}


# ---------------------------------------------------------------------------------------------------
# 4. an answer with its source
# ---------------------------------------------------------------------------------------------------
def policy_data(rng):
    days_opened, days_unopened = rng.choice([(14, 30), (10, 45), (21, 60)])
    files = {
        "policies/returns.md": f"# Returns\n\nUnopened items can be returned within {days_unopened} days of delivery "
                               f"for a full refund.\n\nOpened items can be returned within {days_opened} days of "
                               f"delivery, minus a 10% restocking fee.\n\nRefunds go back to the original payment "
                               f"method within 5 business days.\n",
        "policies/shipping.md": "# Shipping\n\nOrders ship within 2 business days. Returns are shipped at the "
                                "customer's cost unless the item arrived damaged.\n",
        "policies/warranty.md": f"# Warranty\n\nElectronics carry a one-year warranty. Claims must be made within "
                                f"{days_unopened} days of noticing the fault.\n",
    }
    truth = {"days": days_opened, "file": "policies/returns.md",
             "sentence": f"Opened items can be returned within {days_opened} days of delivery"}
    return files, truth


POLICY_CHECK = r'''
import json, re, sys
from pathlib import Path
truth = json.loads(Path(".axiom_check/truth.json").read_text())
out = Path("answer.md")
if not out.exists():
    print("PUBLIC: answer.md was not found in the workspace"); sys.exit(1)
text = out.read_text(encoding="utf-8")
problems = []
numbers = {int(n) for n in re.findall(r"\b(\d{1,3})\s*days?\b", text)}
if truth["days"] not in numbers:
    problems.append("PUBLIC: the answer does not state the correct number of days for OPENED items")
quotes = [q for t in re.findall(r'"([^"]{15,})"|“([^”]{15,})”|^>\s*(.{15,})$', text, re.M) for q in t if q]
files = set(re.findall(r"[\w./-]+\.md", text)) - {"answer.md"}
if not quotes:
    problems.append("PUBLIC: the answer needs the supporting sentence quoted word for word")
if not files:
    problems.append("PUBLIC: the answer needs to name the file the quote comes from")
ok_quote = any(q.strip().rstrip(".") in Path(f).read_text(encoding="utf-8") for q in quotes for f in files if Path(f).is_file())
if quotes and files and not ok_quote:
    problems.append("PUBLIC: the quoted sentence does not appear word for word in the file named")
if quotes and files and ok_quote and not any(truth["sentence"] in q for q in quotes):
    problems.append("PUBLIC: the quote does not support the answer about opened items")
if problems:
    print("\n".join(problems)); sys.exit(1)
print("PUBLIC: correct number of days, quoted word for word from the named file")
'''


def policy_gold(files, truth):
    """A real solution, from the policy files only: find the sentence about opened items."""
    for rel, text in sorted(files.items()):
        for line in text.splitlines():
            if line.startswith("Opened items"):
                days = re.search(r"within (\d+) days", line).group(1)
                return {"answer.md": f"Opened items can be returned within {days} days.\n\nSource: {rel}\n\n"
                                     f"\"{line}\"\n"}
    raise ValueError("no sentence about opened items in the policies")


def policy_controls(files, truth):
    other = [l for l in files["policies/returns.md"].splitlines() if l.startswith("Unopened")][0]
    unopened_days = int(re.search(r"within (\d+) days", other).group(1))
    return {"right number, no source": {"answer.md": f"{truth['days']} days for opened items.\n"},
            "the unopened-items number": {"answer.md": f"Items can be returned within {unopened_days} days.\n\n"
                                                         f"Source: {truth['file']}\n\n\"{other}\"\n"},
            "a quote that is not in the file": {"answer.md": f"{truth['days']} days.\n\nSource: policies/shipping.md\n\n"
                                                              f"\"{truth['sentence']}\"\n"}}


import re  # noqa: E402  (used by policy_controls)

TASKS = [
    {"id": "receipts-to-spreadsheet", "data": receipts_data, "check": RECEIPTS_CHECK,
     "gold": receipts_gold, "controls": receipts_controls,
     "ask": "Can you put my March receipts (in the receipts folder) into a spreadsheet for my accountant? I need the "
            "date, the shop, the amount and a category for each one: food, transport, home or other. Please save it "
            "as march_expenses.csv."},
    {"id": "tidy-downloads", "data": downloads_data, "check": DOWNLOADS_CHECK,
     "gold": downloads_gold, "controls": downloads_controls,
     "ask": "My downloads folder is a mess. Please sort everything in it into four subfolders inside downloads: "
            "Documents, Photos, Installers and Other. Don't delete anything."},
    {"id": "merge-contacts", "data": contacts_data, "check": CONTACTS_CHECK,
     "gold": contacts_gold, "controls": contacts_controls,
     "ask": "I have two contact lists in the contacts folder, one exported from my phone and one from my email. "
            "Please merge them into a single contacts.csv with name, email and phone: one row per person, nobody "
            "missing, and keep every phone number you can find."},
    {"id": "policy-answer-with-source", "data": policy_data, "check": POLICY_CHECK,
     "gold": policy_gold, "controls": policy_controls,
     "ask": "A customer is asking how long they have to return an item they already opened. Can you find the answer "
            "in our policies folder and write it in answer.md? Quote the exact sentence it comes from and say which "
            "file it is in, so I can send it to them with confidence."},
]


# ---------------------------------------------------------------------------------------------------
# 5. reconcile a budget against the bank statement
# ---------------------------------------------------------------------------------------------------
PAYEES = ["Corner Cafe", "Metro Transit", "Hardware World", "Green Grocer", "City Power & Light", "Netstream",
          "Page Turners Books", "Fuel Stop", "Pharmacy Plus", "Gym Central"]


def bank_data(rng):
    days = sorted(rng.sample(range(1, 29), 14))
    tx = [{"day": d, "payee": rng.choice(PAYEES), "amount": round(rng.uniform(5, 250), 2)} for d in days]
    missing = rng.sample(range(len(tx)), 3)
    mismatch = rng.choice([i for i in range(len(tx)) if i not in missing])
    extra_day = rng.choice([d for d in range(1, 29) if d not in days])
    statement = [["Date", "Description", "Amount"]]
    for t in tx:
        statement.append([f"2026-03-{t['day']:02d}", f"POS {rng.randint(1000, 9999)} {t['payee'].upper()}",
                          f"-{t['amount']:.2f}"])
    ledger, truth = [["Date", "Payee", "Amount"]], []
    for i, t in enumerate(tx):
        if i in missing:
            truth.append({"date": f"2026-03-{t['day']:02d}", "issue": "missing_from_budget", "amount": t["amount"]})
            continue
        amount = t["amount"]
        if i == mismatch:
            amount = round(amount * 1.1 + 1, 2)
            truth.append({"date": f"2026-03-{t['day']:02d}", "issue": "amount_mismatch", "amount": t["amount"]})
        ledger.append([f"03/{t['day']:02d}/2026", t["payee"], f"{amount:.2f}"])
    cash = round(rng.uniform(5, 60), 2)
    ledger.append([f"03/{extra_day:02d}/2026", "Farmers Market (cash)", f"{cash:.2f}"])
    truth.append({"date": f"2026-03-{extra_day:02d}", "issue": "not_on_statement", "amount": cash})
    ledger = [ledger[0]] + sorted(ledger[1:], key=lambda r: r[0])

    def to_csv(rows):
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        return buf.getvalue()
    return {"statement.csv": to_csv(statement), "ledger.csv": to_csv(ledger)}, truth


BANK_CHECK = r'''
import csv, json, re, sys
from pathlib import Path
truth = json.loads(Path(".axiom_check/truth.json").read_text())
out = Path("reconciliation.csv")
if not out.exists():
    print("PUBLIC: reconciliation.csv was not found in the workspace"); sys.exit(1)
rows = [{k.strip().lower(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(out.open(encoding="utf-8-sig"))]
if not rows or not {"date", "description", "amount", "issue"} <= set(rows[0]):
    print("PUBLIC: reconciliation.csv needs the columns date, description, amount, issue"); sys.exit(1)
def iso(s):
    if m := re.match(r"(\d{4})-(\d{2})-(\d{2})$", s): return s
    if m := re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})$", s): return f"{m.group(3)}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"
    return s
got = {(iso(r["date"]), r["issue"].lower()): r for r in rows}
want = {(t["date"], t["issue"]): t for t in truth}
problems = []
names = {"missing_from_budget": "on the statement but missing from the budget",
         "not_on_statement": "in the budget but not on the statement", "amount_mismatch": "with amounts that differ"}
for issue, words in names.items():
    lost = [k for k in want if k[1] == issue and k not in got]
    if lost:
        problems.append(f"PUBLIC: {len(lost)} difference(s) {words} are not listed (issue = {issue})")
extra = [k for k in got if k not in want]
if extra:
    problems.append(f"PUBLIC: {len(extra)} listed row(s) are not real differences, or have the wrong issue")
def money(s):
    try: return round(abs(float(s.replace("$", "").replace(",", ""))), 2)
    except ValueError: return None
bad = [k for k, t in want.items() if k in got and t["issue"] != "amount_mismatch" and money(got[k]["amount"]) != t["amount"]]
if bad:
    problems.append(f"PUBLIC: {len(bad)} listed difference(s) have the wrong amount")
if problems:
    print("\n".join(problems)); print("expected", sorted(want)); sys.exit(1)
print("PUBLIC: every difference between the budget and the statement is listed, and nothing else")
'''


def bank_gold(files, truth):
    stmt = [(r["Date"], r["Description"], abs(float(r["Amount"]))) for r in csv.DictReader(io.StringIO(files["statement.csv"]))]
    ledg = []
    for r in csv.DictReader(io.StringIO(files["ledger.csv"])):
        m, d, y = r["Date"].split("/")
        ledg.append((f"{y}-{m}-{d}", r["Payee"], float(r["Amount"])))
    out, used = [], set()
    for date, desc, amt in stmt:
        same = [i for i, l in enumerate(ledg) if l[0] == date and i not in used]
        exact = [i for i in same if abs(ledg[i][2] - amt) < 0.005]
        if exact:
            used.add(exact[0])
        elif same:
            used.add(same[0])
            out.append([date, desc, f"{amt:.2f}", "amount_mismatch"])
        else:
            out.append([date, desc, f"{amt:.2f}", "missing_from_budget"])
    for i, (date, payee, amt) in enumerate(ledg):
        if i not in used:
            out.append([date, payee, f"{amt:.2f}", "not_on_statement"])
    buf = io.StringIO()
    csv.writer(buf).writerows([["date", "description", "amount", "issue"]] + sorted(out))
    return {"reconciliation.csv": buf.getvalue()}


def bank_controls(files, truth):
    gold = bank_gold(files, truth)["reconciliation.csv"].splitlines()
    one_way = [gold[0]] + [l for l in gold[1:] if l.endswith("missing_from_budget")]
    split = [gold[0]]
    for l in gold[1:]:
        if l.endswith("amount_mismatch"):
            split += [l.replace("amount_mismatch", "missing_from_budget"), l.replace("amount_mismatch", "not_on_statement")]
        else:
            split.append(l)
    everything = [gold[0]] + [f"{r['Date']},{r['Description']},{r['Amount'].lstrip('-')},missing_from_budget"
                              for r in csv.DictReader(io.StringIO(files["statement.csv"]))]
    return {"only one direction checked": {"reconciliation.csv": "\n".join(one_way) + "\n"},
            "a mismatch reported as missing + extra": {"reconciliation.csv": "\n".join(split) + "\n"},
            "every transaction listed": {"reconciliation.csv": "\n".join(everything) + "\n"},
            "right list, but the statement deleted": {"reconciliation.csv": "\n".join(gold) + "\n",
                                                       "__delete__": ["statement.csv"]}}


# ---------------------------------------------------------------------------------------------------
# 6. fill a claim form from a customer's letter
# ---------------------------------------------------------------------------------------------------
FIRST = ["Maria", "Tom", "Aisha", "Lukas", "Priya", "Owen"]
LAST = ["Fernandes", "Brooks", "Nwosu", "Becker", "Raman", "Doyle"]
STREETS = ["14 Elm Street", "220 Harbor Road", "7 Mill Lane", "91 Oak Avenue"]
CITIES = [("Springfield", "62704"), ("Riverton", "82501"), ("Fairview", "37062"), ("Lakeside", "92040")]
ORD = {1: "1st", 2: "2nd", 3: "3rd", 21: "21st", 22: "22nd", 23: "23rd", 31: "31st"}


def form_data(rng):
    name = f"{rng.choice(FIRST)} {rng.choice(LAST)}"
    street = rng.choice(STREETS)
    city, postal = rng.choice(CITIES)
    policy = f"HP-{rng.randint(100000, 999999)}"
    day = rng.randint(1, 28)
    phone = f"555{rng.randint(1000000, 9999999)}"
    date_text = rng.choice([f"the {ORD.get(day, f'{day}th')} of March", f"March {ORD.get(day, f'{day}th')}",
                            f"Tuesday, March {day}"])
    phone_text = rng.choice([f"({phone[:3]}) {phone[3:6]}-{phone[6:]}", f"{phone[:3]}.{phone[3:6]}.{phone[6:]}",
                             f"{phone[:3]} {phone[3:6]} {phone[6:]}"])
    letter = (f"Dear Claims Team,\n\nMy name is {name} and I hold policy number {policy}. On {date_text} 2026 a pipe "
              f"burst under my kitchen sink, and the water damaged the floor and two cabinets before I could shut "
              f"it off.\n\nYou can reach me on {phone_text}, or write to me at {street}, {city} {postal}.\n\n"
              f"Kind regards,\n{name}\n")
    template = {"full_name": "", "street": "", "city": "", "postal_code": "", "phone": "", "email": "",
                "policy_number": "", "incident_date": "YYYY-MM-DD", "description": ""}
    truth = {"full_name": name, "street": street, "city": city, "postal_code": postal, "phone": phone,
             "email": "", "policy_number": policy, "incident_date": f"2026-03-{day:02d}"}
    return {"letters/claim_letter.txt": letter, "forms/form_template.json": json.dumps(template, indent=2)}, truth


FORM_CHECK = r'''
import json, re, sys
from pathlib import Path
truth = json.loads(Path(".axiom_check/truth.json").read_text())
out = Path("claim_form.json")
if not out.exists():
    print("PUBLIC: claim_form.json was not found in the workspace"); sys.exit(1)
try:
    form = json.loads(out.read_text(encoding="utf-8"))
except ValueError:
    print("PUBLIC: claim_form.json is not valid JSON"); sys.exit(1)
def norm(key, value):
    if key == "phone":
        return re.sub(r"\D", "", str(value))[-10:]       # any formatting, same ten digits
    return " ".join(str(value).split()).lower()
problems = []
for key, value in truth.items():
    got = form.get(key)
    if got is None:
        problems.append(f"PUBLIC: the field {key} is missing from the form")
    elif value == "" and str(got).strip():
        problems.append(f"PUBLIC: {key} is filled in, but the letter does not say it (leave it empty, do not guess)")
    elif value and norm(key, got) != norm(key, value):
        problems.append(f"PUBLIC: {key} does not match the letter" + (" (use YYYY-MM-DD)" if key == "incident_date" else ""))
desc = str(form.get("description", "")).lower()
if not any(w in desc for w in ("water", "pipe", "leak")):
    problems.append("PUBLIC: the description should say what happened")
if problems:
    print("\n".join(problems)); sys.exit(1)
print("PUBLIC: every field matches the letter, and nothing was guessed")
'''


def form_gold(files, truth):
    """A real solution, from the letter only."""
    letter = files["letters/claim_letter.txt"]
    name = re.search(r"My name is (.+?) and", letter).group(1)
    policy = re.search(r"policy number ([A-Z]+-\d+)", letter).group(1)
    m = re.search(r"(?:March (\d{1,2})|the (\d{1,2})\w\w of March)", letter)
    day = int(m.group(1) or m.group(2))
    phone = re.sub(r"\D", "", re.search(r"reach me on ([\d(). -]+?),", letter).group(1))
    street, rest = re.search(r"write to me at (.+?)\.\n", letter).group(1).split(", ")
    city, postal = rest.rsplit(" ", 1)
    form = {"full_name": name, "street": street, "city": city, "postal_code": postal, "phone": phone, "email": "",
            "policy_number": policy, "incident_date": f"2026-03-{day:02d}",
            "description": "A pipe burst under the kitchen sink; water damaged the floor and two cabinets."}
    return {"claim_form.json": json.dumps(form, indent=2)}


def form_controls(files, truth):
    gold = json.loads(form_gold(files, truth)["claim_form.json"])
    invented = dict(gold, email=gold["full_name"].lower().replace(" ", ".") + "@gmail.com")
    us_date = dict(gold, incident_date=f"03/{gold['incident_date'][-2:]}/2026")
    no_policy = dict(gold, policy_number="")
    swapped = dict(gold, city=gold["postal_code"], postal_code=gold["city"])
    return {"an email invented": {"claim_form.json": json.dumps(invented)},
            "the date in the wrong format": {"claim_form.json": json.dumps(us_date)},
            "the policy number left out": {"claim_form.json": json.dumps(no_policy)},
            "city and postal code swapped": {"claim_form.json": json.dumps(swapped)}}


# ---------------------------------------------------------------------------------------------------
# 7. a calendar from the inbox
# ---------------------------------------------------------------------------------------------------
MEETINGS = ["Budget review", "Dentist", "Coffee with Sam", "Team retro", "Car service", "Call with the landlord",
            "Kids' school play"]
WEEKDAY = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _time_text(rng, h, m):
    return rng.choice([f"{h:02d}:{m:02d}", f"{(h - 1) % 12 + 1}:{m:02d} {'am' if h < 12 else 'pm'}",
                       f"{(h - 1) % 12 + 1}{'' if m == 0 else f':{m:02d}'}{'am' if h < 12 else 'pm'}"])


def calendar_data(rng):
    import datetime as dt
    titles = rng.sample(MEETINGS, 6)
    slots, used = [], set()
    while len(slots) < 6:
        day = rng.randint(4, 24)
        h = rng.choice([9, 10, 11, 13, 14, 15, 16])
        if (day, h) not in used and (day, h + 1) not in used and (day, h - 1) not in used:
            used.add((day, h))
            slots.append((day, h, rng.choice([0, 30]), rng.choice([30, 45, 60])))
    files, truth = {}, []
    def when(day, h, m):
        d = dt.date(2026, 5, day)
        return f"{WEEKDAY[d.weekday()]} May {day}", _time_text(rng, h, m)
    for i, (title, (day, h, m, dur)) in enumerate(zip(titles, slots)):
        date_text, time_text = when(day, h, m)
        ask = f"Subject: {title}\n\nHi! Could we do {title.lower()} on {date_text} at {time_text}? It should take about {dur} minutes.\n"
        if i == 4:
            files[f"inbox/{i + 1:02d}_{title.split()[0].lower()}.txt"] = ask + "\n> Me: Sorry, I can't make that, I'm away that week.\n"
            continue
        files[f"inbox/{i + 1:02d}_{title.split()[0].lower()}.txt"] = ask + "\n> Me: Sounds good, see you then.\n"
        start = (day, h, m)
        if i == 1:      # moved later
            nday = day + 1 if (day + 1, h) not in used and day < 28 else day
            nh = h + 2 if h + 2 <= 17 else h - 2
            ndate_text, ntime = when(nday, nh, m)
            files[f"inbox/{i + 7:02d}_re_{title.split()[0].lower()}.txt"] = (
                f"Subject: Re: {title}\n\nSomething came up, sorry! Can we move {title.lower()} to {ndate_text} at "
                f"{ntime} instead? Same length.\n\n> Me: No problem, moved.\n")
            start = (nday, nh, m)
        if i == 2:      # cancelled
            files[f"inbox/{i + 7:02d}_cancel_{title.split()[0].lower()}.txt"] = (
                f"Subject: Cancelled: {title}\n\nI'm afraid we'll have to cancel {title.lower()}. I'll be in touch.\n")
            continue
        d, hh, mm = start
        end_minutes = hh * 60 + mm + dur
        truth.append({"date": f"2026-05-{d:02d}", "start": f"{hh:02d}:{mm:02d}",
                      "end": f"{end_minutes // 60:02d}:{end_minutes % 60:02d}", "title": title})
    wday, wh = rng.randint(4, 24), rng.choice([12, 18])
    wdate, wtime = when(wday, wh, 0)
    files["inbox/99_newsletter.txt"] = (f"Subject: Webinar: Spring savings\n\nJoin our free webinar on {wdate} at "
                                        f"{wtime}! Spaces are limited.\n\nUnsubscribe | View in browser\n")
    return files, truth


CALENDAR_CHECK = r'''
import csv, json, re, sys
from pathlib import Path
truth = json.loads(Path(".axiom_check/truth.json").read_text())
out = Path("calendar.csv")
if not out.exists():
    print("PUBLIC: calendar.csv was not found in the workspace"); sys.exit(1)
rows = [{k.strip().lower(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(out.open(encoding="utf-8-sig"))]
if not rows or not {"date", "start", "end", "title"} <= set(rows[0]):
    print("PUBLIC: calendar.csv needs the columns date, start, end, title"); sys.exit(1)
def hm(s):
    m = re.match(r"(\d{1,2}):(\d{2})", s)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else s
got = {(r["date"], hm(r["start"]), hm(r["end"])) for r in rows}
want = {(t["date"], t["start"], t["end"]) for t in truth}
problems = []
if want - got:
    problems.append(f"PUBLIC: {len(want - got)} agreed meeting(s) are missing or at the wrong date or time "
                    "(dates YYYY-MM-DD, times 24-hour HH:MM; use the latest time if a meeting was moved)")
if got - want:
    problems.append(f"PUBLIC: {len(got - want)} event(s) should not be in the calendar (declined, cancelled, "
                    "moved, or never agreed)")
if problems:
    print("\n".join(problems)); print("expected", sorted(want)); sys.exit(1)
print("PUBLIC: every agreed meeting is in the calendar at its final time, and nothing else")
'''


def calendar_gold(files, truth):
    """A real solution, from the inbox only."""
    def parse_time(t):
        m = re.match(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t.strip())
        h, mm, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ap == "pm" and h != 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        return h, mm
    events, cancelled = {}, set()
    for rel in sorted(files):
        text = files[rel]
        subject = text.split("\n", 1)[0].removeprefix("Subject: ")
        if subject.startswith("Cancelled: "):
            cancelled.add(subject.removeprefix("Cancelled: "))
            continue
        if "Me:" not in text or "can't" in text.split("Me:")[1]:
            continue
        title = subject.removeprefix("Re: ")
        m = re.search(r"on \w+ May (\d{1,2}) at ([\d:]+\s*(?:am|pm)?)", text) or \
            re.search(r"to \w+ May (\d{1,2}) at ([\d:]+\s*(?:am|pm)?)", text)
        day, (h, mm) = int(m.group(1)), parse_time(m.group(2))
        dur = int(re.search(r"about (\d+) minutes", text).group(1)) if "about" in text else events[title][3]
        events[title] = (day, h, mm, dur)
    rows = [["date", "start", "end", "title"]]
    for title, (day, h, mm, dur) in sorted(events.items()):
        if title in cancelled:
            continue
        end = h * 60 + mm + dur
        rows.append([f"2026-05-{day:02d}", f"{h:02d}:{mm:02d}", f"{end // 60:02d}:{end % 60:02d}", title])
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return {"calendar.csv": buf.getvalue()}


def calendar_controls(files, truth):
    gold = calendar_gold(files, truth)["calendar.csv"].splitlines()
    cancelled_title = next(files[r].split("\n", 1)[0].removeprefix("Subject: Cancelled: ") for r in files if "cancel_" in r)
    kept = gold + [f"2026-05-01,10:00,10:30,{cancelled_title}"]
    hdr, body = gold[0], gold[1:]
    webinar = gold + ["2026-05-20,12:00,13:00,Webinar: Spring savings"]
    return {"a cancelled meeting kept": {"calendar.csv": "\n".join(kept) + "\n"},
            "a meeting left out": {"calendar.csv": "\n".join([hdr] + body[1:]) + "\n"},
            "the newsletter webinar added": {"calendar.csv": "\n".join(webinar) + "\n"},
            "12-hour times": {"calendar.csv": "\n".join([hdr] + [re.sub(r",1([3-7]):", lambda m: f",{int(m.group(1)) - 2}:", l)
                                                               for l in body]) + "\n"}}


# ---------------------------------------------------------------------------------------------------
# 8. overdue invoices and what each client owes
# ---------------------------------------------------------------------------------------------------
CLIENTS = ["Bluebird Bakery", "Northwind Studio", "Hillside Dental", "Oak & Iron Fitness"]


def invoice_data(rng):
    import datetime as dt
    today = dt.date(2026, 4, 15)
    files, overdue = {}, []
    for n in range(10):
        client = CLIENTS[n % 4]
        issued = dt.date(2026, 1, 5) + dt.timedelta(days=rng.randint(0, 95))
        net = rng.choice([None, 14, 30])
        due = issued + dt.timedelta(days=net or 30) if net else issued + dt.timedelta(days=rng.randint(10, 40))
        amount = round(rng.uniform(80, 2400), 2)
        paid = rng.random() < 0.3
        lines = [f"INVOICE #{1040 + n}", f"Billed to: {client}", f"Issued: {issued.strftime('%B %d, %Y')}"]
        lines.append(f"Terms: Net {net}" if net else f"Due date: {due.strftime('%d %b %Y')}")
        lines.append(f"Amount due: ${amount:,.2f}")
        if paid:
            lines.append(f"PAID {(issued + dt.timedelta(days=rng.randint(3, 20))).isoformat()} - thank you")
        files[f"invoices/invoice_{1040 + n}.txt"] = "\n".join(lines) + "\n"
        if not paid and due < today:
            overdue.append({"invoice": str(1040 + n), "client": client, "due_date": due.isoformat(), "amount": amount})
    totals = {}
    for o in overdue:
        totals[o["client"]] = round(totals.get(o["client"], 0) + o["amount"], 2)
    return files, {"overdue": overdue, "totals": totals}


INVOICE_CHECK = r'''
import csv, json, sys
from pathlib import Path
truth = json.loads(Path(".axiom_check/truth.json").read_text())
problems = []
def read(name, cols):
    p = Path(name)
    if not p.exists():
        problems.append(f"PUBLIC: {name} was not found in the workspace"); return None
    rows = [{k.strip().lower(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(p.open(encoding="utf-8-sig"))]
    if rows and not set(cols) <= set(rows[0]):
        problems.append(f"PUBLIC: {name} needs the columns {', '.join(cols)}"); return None
    return rows
money = lambda s: round(float(s.replace("$", "").replace(",", "")), 2)
od = read("overdue.csv", ["invoice", "client", "due_date", "amount"])
tot = read("owed_by_client.csv", ["client", "total"])
if od is not None:
    got = {r["invoice"].lstrip("#") for r in od}
    want = {o["invoice"] for o in truth["overdue"]}
    if want - got: problems.append(f"PUBLIC: {len(want - got)} overdue invoice(s) are missing")
    if got - want: problems.append(f"PUBLIC: {len(got - want)} listed invoice(s) are not overdue (paid already, or not yet due)")
if tot is not None:
    got_t = {r["client"].lower(): r["total"] for r in tot}
    wrong = [c for c, v in truth["totals"].items() if c.lower() not in got_t or money(got_t[c.lower()]) != v]
    extra = [c for c in got_t if c not in {k.lower() for k in truth["totals"]} and money(got_t[c]) != 0]
    if wrong: problems.append(f"PUBLIC: {len(wrong)} client total(s) are missing or wrong")
    if extra: problems.append(f"PUBLIC: {len(extra)} client(s) are listed as owing money but have nothing overdue")
if problems:
    print("\n".join(problems)); print("expected", truth); sys.exit(1)
print("PUBLIC: the overdue invoices and every client's total are right")
'''


def invoice_gold(files, truth):
    """A real solution, from the invoice files only."""
    import datetime as dt
    today = dt.date(2026, 4, 15)
    rows, totals = [], {}
    for rel in sorted(files):
        text = files[rel]
        if "PAID" in text:
            continue
        no = re.search(r"#(\d+)", text).group(1)
        client = re.search(r"Billed to: (.+)", text).group(1).strip()
        issued = dt.datetime.strptime(re.search(r"Issued: (.+)", text).group(1).strip(), "%B %d, %Y").date()
        if m := re.search(r"Net (\d+)", text):
            due = issued + dt.timedelta(days=int(m.group(1)))
        else:
            due = dt.datetime.strptime(re.search(r"Due date: (.+)", text).group(1).strip(), "%d %b %Y").date()
        amount = float(re.search(r"\$([\d,]+\.\d{2})", text).group(1).replace(",", ""))
        if due < today:
            rows.append([no, client, due.isoformat(), f"{amount:.2f}"])
            totals[client] = round(totals.get(client, 0) + amount, 2)
    a, b = io.StringIO(), io.StringIO()
    csv.writer(a).writerows([["invoice", "client", "due_date", "amount"]] + rows)
    csv.writer(b).writerows([["client", "total"]] + [[c, f"{t:.2f}"] for c, t in sorted(totals.items())])
    return {"overdue.csv": a.getvalue(), "owed_by_client.csv": b.getvalue()}


def invoice_controls(files, truth):
    gold = invoice_gold(files, truth)
    paid = next(r for r in files if "PAID" in files[r])
    paid_no = re.search(r"#(\d+)", files[paid]).group(1)
    with_paid = gold["overdue.csv"].rstrip("\n") + f"\n{paid_no},Someone,2026-01-01,10.00\n"
    no_totals = {"overdue.csv": gold["overdue.csv"]}
    inflated = gold["owed_by_client.csv"].splitlines()
    inflated = [inflated[0]] + [f"{l.split(',')[0]},{float(l.split(',')[1]) + 100:.2f}" for l in inflated[1:]]
    return {"a paid invoice counted as overdue": {"overdue.csv": with_paid, "owed_by_client.csv": gold["owed_by_client.csv"]},
            "the per-client totals missing": no_totals,
            "totals that include invoices not yet due": {"overdue.csv": gold["overdue.csv"],
                                                         "owed_by_client.csv": "\n".join(inflated) + "\n"}}


TASKS += [
    {"id": "reconcile-bank-statement", "data": bank_data, "check": BANK_CHECK,
     "gold": bank_gold, "controls": bank_controls,
     "ask": "Can you reconcile my budget spreadsheet (ledger.csv) against my bank statement (statement.csv) for "
            "March? Make a reconciliation.csv listing every difference, with the columns date, description, amount "
            "and issue, where issue is missing_from_budget (on the statement but not in my budget), not_on_statement "
            "(in my budget but not on the statement) or amount_mismatch."},
    {"id": "fill-claim-form", "data": form_data, "check": FORM_CHECK,
     "gold": form_gold, "controls": form_controls,
     "ask": "Please fill in the insurance claim form (forms/form_template.json) using the customer's letter in the "
            "letters folder, and save it as claim_form.json. Leave anything the letter doesn't say empty. Don't "
            "guess."},
    {"id": "calendar-from-inbox", "data": calendar_data, "check": CALENDAR_CHECK,
     "gold": calendar_gold, "controls": calendar_controls,
     "ask": "Go through the emails in my inbox folder and put every meeting I've agreed to into calendar.csv, with "
            "the columns date, start, end and title. If something was moved, use the new time, and leave out "
            "anything that was cancelled or that I said no to."},
    {"id": "overdue-invoices", "data": invoice_data, "check": INVOICE_CHECK,
     "gold": invoice_gold, "controls": invoice_controls,
     "ask": "Which of my invoices (in the invoices folder) are overdue as of April 15, 2026, and how much does each "
            "client owe me in total? Put the overdue invoices in overdue.csv (invoice, client, due_date, amount) and "
            "the totals in owed_by_client.csv (client, total)."},
]


# ---------------------------------------------------------------------------------------------------
def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def build(task, tmp, seed):
    rng = random.Random(seed)
    files, truth = task["data"](rng)
    repo = Path(tmp) / "work"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "my files")
    check = Path(tmp) / "check"
    check.mkdir()
    (check / "check.py").write_text(task["check"], encoding="utf-8")
    (check / "truth.json").write_text(json.dumps(truth), encoding="utf-8")
    return repo, check, files, truth


def apply(repo, base, name, delivery):
    git(repo, "checkout", "-q", "-b", name, base)
    for src, dst in delivery.get("__move__", []):
        (repo / dst).parent.mkdir(parents=True, exist_ok=True)
        git(repo, "mv", src, dst)
    for rel in delivery.get("__delete__", []):
        git(repo, "rm", "-q", rel)
    for rel, text in delivery.items():
        if not rel.startswith("__"):
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(text, encoding="utf-8")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "--allow-empty", "-m", name)
    sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")
    return sha


def validate(seed=7):
    ok = True
    for task in TASKS:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            repo, check, files, truth = build(task, tmp, seed)
            base = git(repo, "rev-parse", "HEAD")
            ax = Axiom()
            ax.register_check("t", repo, [sys.executable, "-I", ".axiom_check/check.py"], [], holdout=str(check),
                              claim_kind="deliver")
            ax.join("v")
            verdict = lambda name, d: ax.verify(ax.claim("v", name, "t", base, apply(repo, base, name, d))["id"])
            nothing = verdict("nothing", {"unrelated.txt": "x\n"})
            gold = verdict("gold", task["gold"](files, truth))
            results = {"untouched": nothing["label"] == "refuted", "gold": gold["label"] == "witnessed"}
            for i, (name, d) in enumerate(task["controls"](files, truth).items()):
                v = verdict(f"control{i}", d)
                results[f"control: {name}"] = v["label"] == "refuted"
            ax.db.close()
        good = all(results.values())
        ok &= good
        print(f"{'OK  ' if good else 'BAD '} {task['id']}")
        for k, v in results.items():
            print(f"       {'pass' if v else 'FAIL'}  {k}" + ("" if v or k != "gold" else f"  ({gold['reason']})"))
    return ok


def run(n_agents, max_steps, model_name, thinking, seed, only):
    from axiom1.agent import ChatModel, run_agent
    model = ChatModel(model_name)
    rows = []
    for task in TASKS:
        if only and task["id"] not in only:
            continue
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            repo, check, files, truth = build(task, tmp, seed)
            db = str(Path(tmp) / "axiom1.db")
            ax = Axiom(db)
            ax.register_check("job", repo, CHECK, [], sandbox="docker", image=IMAGE, holdout=str(check),
                              claim_kind="deliver")
            ax.join("person", ["human"])
            ax.db.close()
            for i in range(n_agents):
                ax = Axiom(db)
                if not ax.db.execute("SELECT 1 FROM tasks WHERE status='open'").fetchone():
                    ax.post_task("person", task["ask"])
                ax.db.close()
                wt = Path(tmp) / f"wt{i}"
                git(repo, "worktree", "add", "-q", "--detach", str(wt), "main")
                before, t0 = dict(model.usage), time.time()
                asyncio.run(run_agent(f"agent{i}", wt, db, model, max_steps=max_steps, log=lambda *_: None,
                                      shell=DockerSandbox(IMAGE), thinking=thinking))
                used = {k: model.usage[k] - before[k] for k in model.usage}
                ax = Axiom(db)
                claims = [(r["label"], r["reason"]) for r in ax.db.execute(
                    "SELECT label, reason FROM claims WHERE agent=? ORDER BY made_at", (f"agent{i}",))]
                ax.db.close()
                row = {"task": task["id"], "position": i, "done": any(l == "witnessed" for l, _ in claims),
                       "claims": [l for l, _ in claims], "last_reason": (claims[-1][1] or "")[:200] if claims else "",
                       "seconds": round(time.time() - t0, 1), "model_calls": used["calls"],
                       "prompt_tokens": used["prompt_tokens"], "completion_tokens": used["completion_tokens"]}
                rows.append(row)
                print(json.dumps(row), flush=True)
    return {"model": model.model, "thinking": thinking, "rows": rows}


def trailing_note(reason):
    """The server appends a process outcome to a witnessed claim's reason as " (note)", and the note can hold
    parentheses of its own ("instance(s)"), so match back from the final ')' instead of taking the last '('."""
    if not reason.endswith(")"):
        return ""
    depth = 0
    for i in range(len(reason) - 1, -1, -1):
        depth += {")": 1, "(": -1}.get(reason[i], 0)
        if depth == 0:
            return reason[i + 1:-1]
    return ""


# ---- form mode: the model fills a form, a fixed pipeline of engines does the rest -------------------------------

def calendar_form(files):
    """One entry per email: what kind it is (closed set) and the text it is about, copied as written."""
    copied = "copied exactly as written in that email; '' if the email does not say"
    return {"type": "object", "required": ["emails"], "properties": {"emails": {
        "type": "array", "description": "one entry for EVERY email file, in any order",
        "items": {"type": "object", "required": ["file", "kind", "meeting", "date", "time", "duration"], "properties": {
            "file": {"type": "string", "enum": sorted(p for p in files if p.startswith("inbox/"))},
            "kind": {"type": "string", "enum": ["add", "move", "cancel", "ignore"],
                     "description": "add = a meeting I agreed to; move = a meeting moved to a new time and I agreed; "
                                    "cancel = a meeting called off; ignore = declined by me, a newsletter, or anything "
                                    "that is not a meeting I am going to"},
            "meeting": {"type": "string", "description": "the meeting's name as written in the subject of its FIRST "
                        "email, without 'Re:' or 'Cancelled:' (the same name for every email about that meeting)"},
            "date": {"type": "string", "description": f"the day ONLY, e.g. 'Friday May 8' (the time goes in `time`), {copied}"},
            "time": {"type": "string", "description": f"the start time, e.g. '1:00 pm', {copied}"},
            "duration": {"type": "string", "description": f"how long, e.g. 'about 45 minutes', {copied}. A move that keeps "
                         "the length ('Same length') gets '': the pipeline keeps the old length"},
        }}}}}


# The calendar process, written once on the engines. The model never writes this; it fills entry.json.
CALENDAR_PIPELINE = '''import json
from pathlib import Path
from axiom_engines import ledger, table, time

entry = json.loads(Path("entry.json").read_text(encoding="utf-8"))
emails = sorted(entry["emails"], key=lambda e: e["file"])           # the inbox is numbered in the order it arrived
inbox = sorted(str(p.as_posix()) for p in Path("inbox").glob("*.txt"))
missing = sorted(set(inbox) - {e["file"] for e in emails})
if missing or len(emails) != len({e["file"] for e in emails}):
    raise SystemExit(f"EngineError: the form must have exactly one entry per email; missing {missing}")
year = time.year_from_weekdays([e["date"] for e in emails if e["kind"] in ("add", "move") and e["date"]],
                               int(entry["today"][:4]))
events = []
for e in emails:
    key = e["meeting"].strip().lower()
    if e["kind"] == "add":
        events.append({"kind": "add", "key": key, "title": e["meeting"].strip(),
                       "date": time.parse_date(e["date"], year), "start": time.parse_time(e["time"]),
                       "minutes": time.parse_duration(e["duration"])})
    elif e["kind"] == "move":
        move = {"kind": "move", "key": key}
        if e["date"]:
            move["date"] = time.parse_date(e["date"], year)
        if e["time"]:
            move["start"] = time.parse_time(e["time"])
        if e["duration"]:
            move["minutes"] = time.parse_duration(e["duration"])
        events.append(move)
    elif e["kind"] == "cancel":
        events.append({"kind": "cancel", "key": key})
rows = [{k: r[k] for k in ("date", "start", "end", "title")} for r in ledger.apply_events(events)]
table.write_csv("calendar.csv", ["date", "start", "end", "title"], rows)
'''

# ---- calendar, one email at a time: nothing to keep straight across emails, nothing to retype ---------------

def _subject(text):
    first = text.splitlines()[0] if text else ""
    return first.split(":", 1)[1].strip() if first.lower().startswith("subject:") else first.strip()


# The kind is the one judgement per email. Its labels say what they mean: a 1.7B model read "cancel" as "I said
# no" until the label itself made the difference (measured 2026-10-01).
CALENDAR_KINDS = {"agreed_new_meeting": "add", "moved_an_agreed_meeting": "move",
         "cancelled_an_agreed_meeting": "cancel", "declined_or_not_a_meeting": "ignore"}


def calendar_item_form(name, answers):
    agreed = [a["file"] for a in answers if CALENDAR_KINDS[a["kind"]] == "add"]
    return {"type": "object", "required": ["kind", "refers_to", "date", "time", "duration"], "properties": {
        "kind": {"type": "string", "enum": list(CALENDAR_KINDS),
                 "description": "agreed_new_meeting = a new meeting I said yes to; moved_an_agreed_meeting = one of "
                                "the meetings agreed so far gets a new time and I said yes; cancelled_an_agreed_meeting "
                                "= the other person called off one of the meetings agreed so far; "
                                "declined_or_not_a_meeting = I said no, or it is a newsletter or anything else"},
        "refers_to": {"type": "string", "enum": agreed + [""],
                      "description": "for a moved or cancelled meeting: the file of that EARLIER agreed meeting, from "
                                     "the list above; '' otherwise"},
        "date": {"type": "string", "description": "the day ONLY, e.g. 'Friday May 8', as written; '' if not given"},
        "time": {"type": "string", "description": "the start time, e.g. '1pm', as written; '' if not given"},
        "duration": {"type": "string", "description": "how long, e.g. 'about 45 minutes', as written; '' if not "
                                                      "given"}}}


def calendar_context(files):
    def context(name, answers):
        agreed = [a["file"] for a in answers if CALENDAR_KINDS[a["kind"]] == "add"]
        if not agreed:
            return "Meetings agreed so far: none yet."
        return ("Meetings agreed so far (a moved or cancelled meeting points at one of these files):\n"
                + "\n".join(f"- {f}: {_subject(files[f])}" for f in agreed))
    return context


def calendar_relevant(a):
    """refers_to only means something for a move or a cancel; date, time and duration only for an add or a move.
    'Same length' on a move says the length does not change: that is no duration, not a vague one."""
    a = dict(a)
    kind = CALENDAR_KINDS.get(a.get("kind"))
    if kind in ("add", "ignore"):
        a["refers_to"] = ""
    if kind in ("cancel", "ignore"):
        a.update(date="", time="", duration="")
    if kind == "move" and re.match(r"\s*same\b", a.get("duration") or "", re.I):
        a["duration"] = ""
    return a


def calendar_explain(name, form, problems):
    if form.get("refers_to") == name or any("refers_to" in p and "must be one of" in p for p in problems):
        return [f"refers_to must be an EARLIER agreed meeting from the list above, never this email itself. If I said "
                f"no to this invitation, the kind is declined_or_not_a_meeting and refers_to is ''."]
    return problems


def _same_by(read, pattern):
    """A copied value counts as copied if the engine reads it the same as something written in the file."""
    def same(value, text):
        try:
            want = read(value)
        except Exception:
            return False
        for m in re.finditer(pattern, text, re.I):
            try:
                if read(m.group(0)) == want:
                    return True
            except Exception:
                continue
        return False
    return same


def calendar_same(today):
    from axiom1.engines import time as t
    years = [int(today[:4]) + d for d in (0, -1, 1)]

    def date_key(text):
        for y in years:
            try:
                return t.parse_date(text, y)[5:]          # month-day; the year is fixed later, from the weekdays
            except Exception:
                continue
        raise ValueError(text)
    return {"time": _same_by(t.parse_time, r"\b\d{1,2}(?:[:.]\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)|\b\d{1,2}:\d{2}\b|\bnoon\b|\bmidnight\b"),
            "date": _same_by(date_key, r"(?:\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*,?\s+)?\b(?:jan|feb|mar|apr|may|jun|"
                                       r"jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}(?:st|nd|rd|th)?\b"),
            "duration": _same_by(t.parse_duration, r"\b(?:about\s+)?(?:an?\s+hour(?:\s+and\s+a\s+half)?|half\s+an\s+hour|"
                                                   r"\d+(?:\.\d+)?\s*(?:minutes?|mins?|hours?|hrs?)\b)")}


def calendar_item_checks(today):
    """Each answer checked as it is made, by the engines, so a correction is about this one email."""
    from axiom1.engines import EngineError, time as t
    year = int(today[:4])

    def problems(name, text, a, answers):
        out = []
        kind = CALENDAR_KINDS[a["kind"]]
        if kind in ("move", "cancel") and not a["refers_to"]:
            out.append("a moved or cancelled meeting must say which earlier agreed meeting it is (refers_to)")
        if kind == "add" and not (a["date"] and a["time"] and a["duration"]):
            out.append("a new meeting needs its date, time and duration")
        if kind == "move" and not (a["date"] or a["time"]):
            out.append("a moved meeting needs the new date or time")
        for field, read in (("time", t.parse_time), ("duration", t.parse_duration)):
            if kind in ("add", "move") and a[field]:
                try:
                    read(a[field])
                except EngineError as e:
                    out.append(f"{field} {a[field]!r}: {e}")
        if kind in ("add", "move") and a["date"]:
            errors = []
            for y in (year - 1, year, year + 1):
                try:
                    t.parse_date(a["date"], y)
                    break
                except EngineError as e:
                    errors.append(str(e))
            else:
                if not any("names a different day" in e for e in errors):
                    out.append(f"date {a['date']!r}: {errors[0]}")
        return out
    return problems


CALENDAR_EACH_PIPELINE = '''import json
from pathlib import Path
from axiom_engines import ledger, table, time

entry = json.loads(Path("entry.json").read_text(encoding="utf-8"))
KINDS = {"agreed_new_meeting": "add", "moved_an_agreed_meeting": "move",
         "cancelled_an_agreed_meeting": "cancel", "declined_or_not_a_meeting": "ignore"}
emails = [{**e, "kind": KINDS[e["kind"]]} for e in sorted(entry["emails"], key=lambda e: e["file"])]
inbox = sorted(p.as_posix() for p in Path("inbox").glob("*.txt"))
if sorted(e["file"] for e in emails) != inbox:
    raise SystemExit(f"EngineError: the answers must cover exactly one entry per email: {inbox}")

def subject(path):
    first = Path(path).read_text(encoding="utf-8").splitlines()[0]
    return first.split(":", 1)[1].strip() if first.lower().startswith("subject:") else first.strip()

year = time.year_from_weekdays([e["date"] for e in emails if e["kind"] in ("add", "move") and e["date"]],
                               int(entry["today"][:4]))
events = []
for e in emails:
    if e["kind"] == "add":
        events.append({"kind": "add", "key": e["file"], "title": subject(e["file"]),
                       "date": time.parse_date(e["date"], year), "start": time.parse_time(e["time"]),
                       "minutes": time.parse_duration(e["duration"])})
    elif e["kind"] == "move":
        move = {"kind": "move", "key": e["refers_to"]}
        if e["date"]:
            move["date"] = time.parse_date(e["date"], year)
        if e["time"]:
            move["start"] = time.parse_time(e["time"])
        if e["duration"]:
            move["minutes"] = time.parse_duration(e["duration"])
        events.append(move)
    elif e["kind"] == "cancel":
        events.append({"kind": "cancel", "key": e["refers_to"]})
rows = [{k: r[k] for k in ("date", "start", "end", "title")} for r in ledger.apply_events(events)]
table.write_csv("calendar.csv", ["date", "start", "end", "title"], rows)
'''

FORM_JOBS = {"calendar-from-inbox": {"form": calendar_form, "pipeline": CALENDAR_PIPELINE,
                                     "verbatim": ("emails", "file", ["meeting", "date", "time", "duration"])}}
from everyday_forms import FORMS, TASK_FILE  # noqa: E402  (the other seven jobs)
FORM_JOBS.update(FORMS)


# jobs that can also be presented one item at a time (fill_each): the same deliverable, a smaller decision per call
EACH_JOBS = {"calendar-from-inbox": {
    "pipeline": CALENDAR_EACH_PIPELINE, "items": lambda files: [(p, files[p]) for p in sorted(files) if p.startswith("inbox/")],
    "item_form": lambda files: calendar_item_form, "copied": calendar_same,
    "context": calendar_context, "checks": calendar_item_checks, "relevant": calendar_relevant,
    "explain": calendar_explain}}


def form_series(n_instances, model_name, first_seed, only, as_of=None, each=False):
    """Form mode over fresh instances, verified by the same server check as the agents' runs. each=True presents
    the job one item at a time (only jobs in EACH_JOBS)."""
    import datetime as dt
    from axiom1 import forms
    from axiom1.agent import ChatModel
    as_of = as_of or dt.date.today().isoformat()
    model = ChatModel(model_name)
    rows = []
    for task in TASKS:
        if task["id"] not in (EACH_JOBS if each else FORM_JOBS) or (only and task["id"] not in only):
            continue
        job = EACH_JOBS[task["id"]] if each else FORM_JOBS[task["id"]]
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = str(Path(tmp) / "axiom1.db")
            for i in range(n_instances):
                inst = Path(tmp) / f"instance{i}"
                repo, check, files, truth = build(task, inst, first_seed + i)
                ax = Axiom(db)
                ax.register_check(task["id"], repo, CHECK, [], sandbox="docker", image=IMAGE, holdout=str(check),
                                  claim_kind="deliver")
                ax.join("former", ["human"])
                base = git(repo, "rev-parse", "HEAD")
                used0, t0 = dict(model.usage), time.time()
                shown = {**files, TASK_FILE: task["ask"]} if job.get("task_file") else files
                if each:
                    res = forms.fill_each(model, task["ask"], job["items"](files), job["item_form"](files),
                                          copied=job["copied"](as_of), item_problems=job["checks"](as_of),
                                          context=job["context"](files), relevant=job.get("relevant"),
                                          explain=job.get("explain"))
                    res.update(entry=None, produced={})
                    if res["ok"]:
                        entry = {"emails": res["answers"], "today": as_of}
                        ok, problem, produced = forms.run_pipeline(job["pipeline"], entry, files)
                        if ok:
                            res.update(entry=entry, produced=produced)
                        else:
                            res["ok"] = False
                            res["corrections"].append(f"the pipeline refused the answers: {problem}")
                else:
                    res = forms.fill(model, task["ask"], shown, job["form"](files), job["pipeline"],
                                     verbatim=job["verbatim"], fixed={"today": as_of})
                used = {k: model.usage[k] - used0[k] for k in model.usage}
                label, reason = "not delivered", "; ".join(res["corrections"][-1:])
                if res["ok"]:
                    git(repo, "checkout", "-q", "-b", f"form{i}", base)
                    produced = dict(res["produced"])
                    for rel in produced.pop("__removed__", []):
                        if rel != TASK_FILE:
                            git(repo, "rm", "-q", rel)                 # moved by the pipeline, not lost
                    deliver = {"process.py": job["pipeline"], "entry.json": json.dumps(res["entry"], indent=1),
                               **{k: v for k, v in produced.items() if k != TASK_FILE}}
                    for rel, text in deliver.items():
                        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
                        (repo / rel).write_text(text, encoding="utf-8", newline="")
                    git(repo, "add", "-A")
                    git(repo, "-c", "user.name=former", "-c", "user.email=f@axiom1.invalid", "commit", "-q",
                        "-m", "filled the form; the pipeline wrote the delivery")
                    claim = ax.claim("former", "filled the form; the pipeline wrote the delivery", task["id"],
                                     base, git(repo, "rev-parse", "HEAD"))
                    v = ax.verify(claim["id"])
                    label, reason = v["label"], v["reason"]
                ax.db.close()
                row = {"task": task["id"], "instance": i, "seed": first_seed + i, "model": model.model,
                       "done": label == "witnessed", "label": label, "rounds": res["rounds"],
                       "corrections": res["corrections"], "reason": reason[:300], "model_calls": used["calls"],
                       "cut_off": used["cut_off"], "prompt_tokens": used["prompt_tokens"],
                       "completion_tokens": used["completion_tokens"], "seconds": round(time.time() - t0, 1)}
                rows.append(row)
                print(json.dumps(row), flush=True)
    return {"model": model.model, "mode": "form, one item at a time" if each else "form", "as_of": as_of, "rows": rows}


def series(n_instances, max_steps, model_name, thinking, first_seed, only, learn_model_name=None):
    """Getting good at a job: one job type, fresh data every instance (a new month of receipts, a new inbox).

    Instance 1 is worked out by the model; a process that reproduces a witnessed result is locked in.
    Later instances replay it (the model fills only the entry, if any). When new data strays from what the
    process handles, the model repairs it, and the repair is locked only if it still passes every earlier
    instance. Every instance's files are kept, because they ARE the regression set.

    learn_model_name: a (larger) model that works the job while it has no locked process. Once a process is
    locked, the main model does everything after it, replays and repairs."""
    from axiom1.agent import ChatModel, run_agent
    model = ChatModel(model_name)
    learner = ChatModel(learn_model_name) if learn_model_name else model
    rows = []
    for task in TASKS:
        if only and task["id"] not in only:
            continue
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db = str(Path(tmp) / "axiom1.db")
            for i in range(n_instances):
                inst = Path(tmp) / f"instance{i}"
                repo, check, files, truth = build(task, inst, first_seed + i)
                ax = Axiom(db)
                ax.register_check(task["id"], repo, CHECK, [], sandbox="docker", image=IMAGE, holdout=str(check),
                                  claim_kind="deliver")
                ax.join("person", ["human"])
                ax.post_task("person", task["ask"], check_id=task["id"])
                before_version = (ax.process_for(task["id"]) or {}).get("version")
                ax.db.close()
                wt = inst / "wt"
                git(repo, "worktree", "add", "-q", "--detach", str(wt), "main")
                worker = learner if before_version is None else model
                used0, t0 = dict(worker.usage), time.time()
                messages = asyncio.run(run_agent(f"agent{i}", wt, db, worker, max_steps=max_steps, log=lambda *_: None,
                                                 shell=DockerSandbox(IMAGE), thinking=thinking))
                used = {k: worker.usage[k] - used0[k] for k in worker.usage}
                ax = Axiom(db)
                claim_rows = ax.db.execute("SELECT label, reason, statement FROM claims WHERE agent=? ORDER BY made_at",
                                           (f"agent{i}",)).fetchall()
                claims = [(r["label"], r["reason"] or "") for r in claim_rows]
                # the replay's own claim names the locked process; the model's transcript does not
                replay_claimed = bool(claim_rows) and claim_rows[0]["statement"].startswith("ran the locked process")
                after_version = (ax.process_for(task["id"]) or {}).get("version")
                ax.db.close()
                replayed = replay_claimed
                done = any(l == "witnessed" for l, _ in claims)
                if replayed and done and len(claims) == 1:
                    path = "replayed"
                elif replayed:
                    path = "replay failed -> model"
                else:
                    path = "worked out by the model"
                outcome = next((trailing_note(r) for l, r in reversed(claims) if l == "witnessed" and trailing_note(r)), "")
                row = {"task": task["id"], "instance": i, "seed": first_seed + i, "model": worker.model,
                       "done": done, "path": path,
                       "process_before": before_version, "process_after": after_version, "outcome": outcome,
                       "claims": [l for l, _ in claims], "model_calls": used["calls"], "cut_off": used["cut_off"],
                       "prompt_tokens": used["prompt_tokens"], "completion_tokens": used["completion_tokens"],
                       "seconds": round(time.time() - t0, 1)}
                rows.append(row)
                print(json.dumps(row), flush=True)
    return {"model": model.model, "learn_model": learner.model, "thinking": thinking,
            "instances_per_type": n_instances, "rows": rows}


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--seed", type=int, default=7)
    se = sub.add_parser("series", help="fresh instances per job type: learn once, replay, repair")
    se.add_argument("--instances", type=int, default=5)
    se.add_argument("--max-steps", type=int, default=40)
    se.add_argument("--model", default=None)
    se.add_argument("--learn-model", default=None, help="works the job until a process is locked")
    se.add_argument("--thinking", default="auto", choices=["auto", "on", "off"])
    se.add_argument("--seed", type=int, default=100)
    se.add_argument("--only", nargs="*")
    se.add_argument("--out", default=None)
    fm = sub.add_parser("form", help="form mode: the model fills a form, a fixed pipeline of engines does the rest")
    fm.add_argument("--instances", type=int, default=5)
    fm.add_argument("--model", default=None)
    fm.add_argument("--seed", type=int, default=100)
    fm.add_argument("--only", nargs="*")
    fm.add_argument("--as-of", default=None, help="today's date for the pipeline (default: the real one)")
    fm.add_argument("--each", action="store_true", help="one item at a time (jobs that support it)")
    fm.add_argument("--out", default=None)
    r = sub.add_parser("run")
    r.add_argument("--agents", type=int, default=2)
    r.add_argument("--max-steps", type=int, default=40)
    r.add_argument("--model", default=None)
    r.add_argument("--thinking", default="auto", choices=["auto", "on", "off"])
    r.add_argument("--seed", type=int, default=7)
    r.add_argument("--only", nargs="*")
    r.add_argument("--out", default=None)
    a = p.parse_args()
    if a.cmd == "validate":
        sys.exit(0 if validate(a.seed) else 1)
    if a.cmd == "form":
        result = form_series(a.instances, a.model, a.seed, a.only, a.as_of, a.each)
        if a.out:
            Path(a.out).write_text(json.dumps(result, indent=1), encoding="utf-8")
        return
    if a.cmd == "series":
        result = series(a.instances, a.max_steps, a.model, a.thinking, a.seed, a.only, a.learn_model)
        if a.out:
            Path(a.out).write_text(json.dumps(result, indent=1), encoding="utf-8")
        return
    result = run(a.agents, a.max_steps, a.model, a.thinking, a.seed, a.only)
    if a.out:
        Path(a.out).write_text(json.dumps(result, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
