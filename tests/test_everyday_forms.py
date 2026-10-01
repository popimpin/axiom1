"""Form mode for every everyday job: a right form, run through the job's pipeline, passes the job's own hidden
check; a plausible wrong one is stopped (by the form check, an engine, or the hidden check). Seed 100, no model."""
import copy
import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'examples'))
import everyday_tasks as e  # noqa: E402
from axiom1 import forms  # noqa: E402

R = "receipts/receipt_"
GOLD = {
    "receipts-to-spreadsheet": {"numeric_date_order": "MDY", "receipts": [
        {"file": "receipts/README.txt", "kind": "not_a_receipt", "shop": "", "date": "", "total": "", "category": "other"},
        {"file": R + "01.txt", "kind": "receipt", "shop": "Green Grocer", "date": "2026-03-15", "total": "83.52 USD", "category": "food"},
        {"file": R + "02.txt", "kind": "receipt", "shop": "Page Turners Books", "date": "03/24/2026", "total": "$64.90", "category": "other"},
        {"file": R + "03.txt", "kind": "receipt", "shop": "CORNER CAFE", "date": "March 3, 2026", "total": "133.40", "category": "food"},
        {"file": R + "04.txt", "kind": "receipt", "shop": "City Cabs", "date": "2026-03-22", "total": "168.13 USD", "category": "transport"},
        {"file": R + "05.txt", "kind": "receipt", "shop": "Metro Transit", "date": "2026-03-08", "total": "57.76 USD", "category": "transport"},
        {"file": R + "06.txt", "kind": "receipt", "shop": "GREEN GROCER", "date": "March 5, 2026", "total": "36.38", "category": "food"},
        {"file": R + "07.txt", "kind": "receipt", "shop": "Metro Transit", "date": "2026-03-21", "total": "75.52 USD", "category": "transport"},
        {"file": R + "08.txt", "kind": "receipt", "shop": "PAGE TURNERS BOOKS", "date": "March 31, 2026", "total": "84.62", "category": "other"},
        {"file": R + "09.txt", "kind": "receipt", "shop": "Page Turners Books", "date": "03/06/2026", "total": "$154.60", "category": "other"},
        {"file": R + "10.txt", "kind": "receipt", "shop": "Green Grocer", "date": "2026-03-01", "total": "109.69 USD", "category": "food"},
        {"file": R + "11.txt", "kind": "receipt", "shop": "Green Grocer", "date": "03/07/2026", "total": "$32.56", "category": "food"},
        {"file": R + "12.txt", "kind": "receipt", "shop": "Hardware World", "date": "March 15, 2026", "total": "144.49", "category": "home"}]},
    "tidy-downloads": {"rules": [{"extension": x, "folder": f} for x, f in [
        (".png", "Photos"), (".jpg", "Photos"), (".heic", "Photos"), (".dmg", "Installers"), (".exe", "Installers"),
        (".txt", "Documents"), (".docx", "Documents"), (".xlsx", "Documents"), (".mp3", "Other"), (".zip", "Other")]]},
    "merge-contacts": {"sources": [
        {"file": "contacts/email_export.csv", "name_column": "Contact", "email_column": "Email Address",
         "phone_column": "Phone", "name_order": "Last, First"},
        {"file": "contacts/phone_export.csv", "name_column": "Name", "email_column": "E-mail",
         "phone_column": "Mobile", "name_order": "First Last"}]},
    "policy-answer-with-source": {"file": "policies/returns.md",
                                  "quote": "Opened items can be returned within 14 days of delivery, minus a 10% restocking fee."},
    "reconcile-bank-statement": {
        "budget": {"file": "ledger.csv", "date_column": "Date", "description_column": "Payee", "amount_column": "Amount", "date_order": "MDY"},
        "statement": {"file": "statement.csv", "date_column": "Date", "description_column": "Description", "amount_column": "Amount", "date_order": "MDY"}},
    "fill-claim-form": {"letter": "letters/claim_letter.txt", "full_name": "Tom Becker", "street": "91 Oak Avenue",
                        "city": "Riverton", "postal_code": "82501", "phone": "555 686 7610", "email": "",
                        "policy_number": "HP-839784", "incident_date": "March 13th 2026",
                        "description": "a pipe burst under my kitchen sink, and the water damaged the floor and two cabinets before I could shut it off"},
}


def invoices_gold(files):
    import re
    inv = []
    for p in sorted(f for f in files if f.startswith("invoices/")):
        t = files[p]
        g = lambda pat: (re.search(pat, t).group(1) if re.search(pat, t) else "")  # noqa: E731
        inv.append({"file": p, "invoice": g(r"#(\d+)"), "client": g(r"Billed to: (.+)"), "issued": g(r"Issued: (.+)"),
                    "terms": g(r"Terms: (.+)"), "due": g(r"Due date: (.+)"), "amount": g(r"Amount due: (.+)"),
                    "status": "paid" if "PAID" in t else "open"})
    return {"as_of": "April 15, 2026", "invoices": inv}


def judge(task, files, truth, produced):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in {**files, **produced}.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        (root / ".axiom_check").mkdir()
        (root / ".axiom_check/truth.json").write_text(json.dumps(truth), encoding="utf-8")
        (root / ".axiom_check/check.py").write_text(task["check"], encoding="utf-8")
        p = subprocess.run([sys.executable, "-I", ".axiom_check/check.py"], cwd=root, capture_output=True, text=True)
        return p.returncode == 0, (p.stdout + p.stderr).strip()[-300:]


CAL = {"emails": [
 {"file": "inbox/01_dentist.txt", "kind": "add", "meeting": "Dentist", "date": "Friday May 15", "time": "1:00 pm", "duration": "about 60 minutes"},
 {"file": "inbox/02_team.txt", "kind": "add", "meeting": "Team retro", "date": "Thursday May 7", "time": "9:30am", "duration": "about 45 minutes"},
 {"file": "inbox/03_call.txt", "kind": "add", "meeting": "Call with the landlord", "date": "Tuesday May 5", "time": "3:00 pm", "duration": "about 45 minutes"},
 {"file": "inbox/04_kids'.txt", "kind": "add", "meeting": "Kids' school play", "date": "Monday May 11", "time": "11:00", "duration": "about 30 minutes"},
 {"file": "inbox/05_coffee.txt", "kind": "ignore", "meeting": "Coffee with Sam", "date": "", "time": "", "duration": ""},
 {"file": "inbox/06_car.txt", "kind": "add", "meeting": "Car service", "date": "Sunday May 24", "time": "1pm", "duration": "about 45 minutes"},
 {"file": "inbox/08_re_team.txt", "kind": "move", "meeting": "Team retro", "date": "Friday May 8", "time": "11:30 am", "duration": ""},
 {"file": "inbox/09_cancel_call.txt", "kind": "cancel", "meeting": "Call with the landlord", "date": "", "time": "", "duration": ""},
 {"file": "inbox/99_newsletter.txt", "kind": "ignore", "meeting": "Webinar: Spring savings", "date": "", "time": "", "duration": ""}]}


MISTAKES = {   # one plausible wrong form per job, and why it is wrong
    "calendar-from-inbox": ("the move linked to a meeting that does not exist", lambda g: g["emails"][6].update(meeting="Retro")),
    "receipts-to-spreadsheet": ("Hardware World filed as food", lambda g: g["receipts"][12].update(category="food")),
    "tidy-downloads": ("no rule for .zip", lambda g: g["rules"].pop()),
    "merge-contacts": ("'Lopez, Ana' read as First Last", lambda g: g["sources"][0].update(name_order="First Last")),
    "policy-answer-with-source": ("30 days misquoted", lambda g: g.update(quote=g["quote"].replace("14", "30"))),
    "reconcile-bank-statement": ("budget dates read day first", lambda g: g["budget"].update(date_order="DMY")),
    "fill-claim-form": ("an invented email", lambda g: g.update(email="tom.becker@example.com")),
    "overdue-invoices": ("a paid invoice marked open", lambda g: g["invoices"][7].update(status="open")),
}


def gold_for(tid, files):
    return CAL if tid == "calendar-from-inbox" else invoices_gold(files) if tid == "overdue-invoices" else GOLD[tid]


def outcome(task, form):
    """'delivered' if the form gets through every check and the hidden check passes, else what stopped it."""
    tid = task["id"]
    job = e.FORM_JOBS[tid]
    files, truth = task["data"](random.Random(100))
    shown = {**files, e.TASK_FILE: task["ask"]} if job.get("task_file") else files
    if forms.shape_problems(form, job["form"](files)):
        return "form check"
    v = job["verbatim"]
    if v and (v(form, shown) if callable(v) else forms.verbatim_problems(form[v[0]], shown, v[1], v[2])):
        return "form check"
    ok, _, produced = forms.run_pipeline(job["pipeline"], {**form, "today": "2026-10-01"}, shown)
    if not ok:
        return "engine"
    removed = set(produced.pop("__removed__", []))
    kept = {k: val for k, val in files.items() if k not in removed}
    passed, _ = judge(task, kept, truth, {k: val for k, val in produced.items() if k != e.TASK_FILE})
    return "delivered" if passed else "hidden check"


class EveryJobHasAWorkingForm(unittest.TestCase):
    def test_a_right_form_passes_the_jobs_own_check(self):
        for task in e.TASKS:
            with self.subTest(job=task["id"]):
                files, _ = task["data"](random.Random(100))
                self.assertEqual(outcome(task, gold_for(task["id"], files)), "delivered")

    def test_a_plausible_wrong_form_is_stopped(self):
        for task in e.TASKS:
            with self.subTest(job=task["id"]):
                files, _ = task["data"](random.Random(100))
                why, mutate = MISTAKES[task["id"]]
                wrong = copy.deepcopy(gold_for(task["id"], files))
                mutate(wrong)
                self.assertNotEqual(outcome(task, wrong), "delivered", why)

    def test_every_job_has_a_form(self):
        self.assertEqual(sorted(e.FORM_JOBS), sorted(t["id"] for t in e.TASKS))


if __name__ == "__main__":
    unittest.main()
