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
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["date", "shop", "amount", "category"])
    for t in truth:
        w.writerow([t["date"], t["vendor"], f"{t['amount']:.2f}", t["category"]])
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
    return {"__move__": [(f"downloads/{n}", f"downloads/{k}/{n}") for n, k in truth.items()]}


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
        truth.append({"name": name, "email": email, "phone": digits})
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
no_phone = sum(1 for r in rows if r["email"].lower() in want and digits(r["phone"])[-10:] != want[r["email"].lower()]["phone"])
if no_phone:
    problems.append(f"PUBLIC: {no_phone} person(s) are missing their phone number or have the wrong one")
if problems:
    print("\n".join(problems)); sys.exit(1)
print("PUBLIC: one row per person, nobody missing, every known phone number kept")
'''


def contacts_gold(files, truth):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "email", "phone"])
    for t in truth:
        w.writerow([t["name"], t["email"], t["phone"]])
    return {"contacts.csv": buf.getvalue()}


def contacts_controls(files, truth):
    gold = contacts_gold(files, truth)["contacts.csv"].splitlines()
    dup = gold + [gold[1].replace(truth[0]["email"], truth[0]["email"].upper())]
    no_phones = [gold[0]] + [l.rsplit(",", 1)[0] + "," for l in gold[1:]]
    return {"same person twice (email case differs)": {"contacts.csv": "\n".join(dup) + "\n"},
            "a person dropped": {"contacts.csv": "\n".join(gold[:-1]) + "\n"},
            "phone numbers lost": {"contacts.csv": "\n".join(no_phones) + "\n"}}


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
    return {"answer.md": f"Opened items can be returned within {truth['days']} days.\n\nSource: {truth['file']}\n\n"
                         f"\"{truth['sentence']}, minus a 10% restocking fee.\"\n"}


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


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("--seed", type=int, default=7)
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
    result = run(a.agents, a.max_steps, a.model, a.thinking, a.seed, a.only)
    if a.out:
        Path(a.out).write_text(json.dumps(result, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
