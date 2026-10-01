"""Form mode: the model fills one form; a fixed pipeline of engines does the rest.

The other way to do a job is an agent that explores for up to 40 steps and writes process.py itself. Here the
process is declared once (a pipeline script built on the engines) and the model's only part is selection over
a closed set: for each item, which kind it is (an enum) and the text it is about, copied as written. That is
the shape that made a 26M function-calling model beat a 35B one on a closed operation set.

The harness checks the form before anything is delivered:
  - its shape (required fields, enum values)
  - that copied text really is in the file it came from (nothing invented)
  - by running the pipeline on it: an engine that refuses names what is wrong, and that message goes back
    to the model as the correction for the next round

The pipeline is process.py and the form is entry.json, so the delivery is verified, locked in and replayed
exactly like an agent's.
"""
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from .verifier import ENTRY_FILE, PROCESS_FILE, install_engines, process_argv

FORM_PROMPT = """You fill in a form about the files below. Read them and call `submit` once with the form.
Copy text exactly as it is written in the files: do not convert, reformat, complete or invent anything.
A pipeline then does every conversion and checks your form. If it refuses, you get its message: fix
what it names and call `submit` again."""


def _copy_key(text):
    """What counts as the same copied text: case and whitespace aside. '9:30 am' copies '9:30am'; a value
    taken from another file or invented still does not match."""
    return re.sub(r"\s+", "", text).lower()


def _collapse(text):
    return re.sub(r"\s+", " ", text).strip()


def shape_problems(value, schema, where="form"):
    """The form against its JSON schema: object/array/string/integer, required, enum. Enough for forms."""
    out = []
    kind = schema.get("type")
    if kind == "object":
        if not isinstance(value, dict):
            return [f"{where} must be an object"]
        for key in schema.get("required", []):
            if key not in value:
                out.append(f"{where} is missing {key!r}")
        for key, sub in schema.get("properties", {}).items():
            if key in value:
                out += shape_problems(value[key], sub, f"{where}.{key}")
        extra = [k for k in value if k not in schema.get("properties", {})]
        if extra:
            out.append(f"{where} has fields the form does not have: {extra}")
    elif kind == "array":
        if not isinstance(value, list):
            return [f"{where} must be a list"]
        for i, item in enumerate(value):
            out += shape_problems(item, schema.get("items", {}), f"{where}[{i}]")
    elif kind == "string":
        if not isinstance(value, str):
            out.append(f"{where} must be text")
        elif "enum" in schema and value not in schema["enum"]:
            out.append(f"{where} is {value!r}; it must be one of {schema['enum']}")
    elif kind == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        out.append(f"{where} must be a whole number")
    return out


def verbatim_problems(items, files, file_key, fields):
    """Every copied field must appear in the file its item names (whitespace and case aside); blank is allowed."""
    out = []
    for i, item in enumerate(items):
        source = files.get(item.get(file_key, ""))
        if source is None:
            continue                                   # the shape check reports an unknown file
        haystack = _copy_key(source)
        for field in fields:
            text = item.get(field, "")
            if isinstance(text, str) and text.strip() and _copy_key(text) not in haystack:
                out.append(f"item {i} ({item.get(file_key)}): {field} {text!r} is not in that file; copy it exactly")
    return out


def run_pipeline(pipeline, entry, files, python=sys.executable, timeout=60):
    """Run the pipeline on a copy of the files with this entry, as the verifier will. Returns (ok, message,
    produced) where message is the engine's refusal (or the error) and produced maps new files to their text.
    An original file the pipeline moved away is listed under produced["__removed__"]."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text, encoding="utf-8")
        (root / PROCESS_FILE).write_text(pipeline, encoding="utf-8")
        (root / ENTRY_FILE).write_text(json.dumps(entry, indent=1), encoding="utf-8")
        install_engines(root)
        try:
            p = subprocess.run(process_argv(python), cwd=root, capture_output=True, text=True, timeout=timeout,
                               stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            return False, f"the pipeline took longer than {timeout}s", {}
        if p.returncode != 0:
            lines = [ln for ln in (p.stderr or p.stdout).strip().splitlines() if ln.strip()]
            last = lines[-1] if lines else f"exit {p.returncode}"
            return False, re.sub(r"^\S*EngineError: ", "", last), {}
        known = set(files) | {PROCESS_FILE, ENTRY_FILE}
        produced = {f.relative_to(root).as_posix(): f.read_text(encoding="utf-8")
                    for f in root.rglob("*") if f.is_file() and "axiom_engines" not in f.parts
                    and f.relative_to(root).as_posix() not in known}
        removed = sorted(rel for rel in files if not (root / rel).exists())
        if removed:
            produced["__removed__"] = removed
        return True, "", produced


def _form_from(reply):
    for call in reply.get("tool_calls") or []:
        if call.get("function", {}).get("name") == "submit":
            try:
                return json.loads(call["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                return None
    text = (reply.get("content") or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            return None
    return None


def fill(model, task, files, schema, pipeline, verbatim=None, fixed=None, max_rounds=3):
    """Fill a form for these files. `verbatim` = (items_key, file_key, [fields]) to check copied text, or a
    function (form, files) -> problems for a form of another shape; `fixed`
    = entry values the harness supplies, not the model (e.g. today's date). Returns {"ok", "entry", "rounds",
    "corrections", "produced"}: the entry is the form plus `fixed`, ready to be entry.json."""
    if hasattr(model, "thinking"):
        model.thinking = False                         # selection, not deliberation
    tool = {"type": "function", "function": {"name": "submit", "description": "Submit the filled-in form.",
                                             "parameters": schema}}
    shown = "\n\n".join(f"--- {path}\n{text}" for path, text in sorted(files.items()))
    messages = [{"role": "system", "content": FORM_PROMPT},
                {"role": "user", "content": f"Task: {task}\n\nThe files:\n\n{shown}"}]
    corrections = []
    for round_no in range(1, max_rounds + 1):
        reply = model(messages, [tool])
        form = _form_from(reply)
        messages.append({"role": "assistant", "content": json.dumps(form) if form is not None
                         else (reply.get("content") or "")})
        if form is None:
            problem = "No form was submitted. Call `submit` with the form."
        else:
            problems = shape_problems(form, schema)
            if not problems and verbatim:
                if callable(verbatim):                 # a job with its own shape: verbatim(form, files) -> problems
                    problems = verbatim(form, files)
                else:
                    items_key, file_key, fields = verbatim
                    problems = verbatim_problems(form.get(items_key, []), files, file_key, fields)
            problem = "; ".join(problems[:6])
            if not problem:
                entry = {**form, **(fixed or {})}
                ok, problem, produced = run_pipeline(pipeline, entry, files)
                if ok:
                    return {"ok": True, "entry": entry, "rounds": round_no, "corrections": corrections,
                            "produced": produced}
                problem = f"The pipeline refused your form: {problem}"
        corrections.append(problem)
        messages.append({"role": "user", "content": problem + "\nFix exactly that and call `submit` again."})
    return {"ok": False, "entry": None, "rounds": max_rounds, "corrections": corrections, "produced": {}}


ITEM_PROMPT = """You fill in a short form about ONE file, shown below. Call `submit` once with the form.
Copy text exactly as it is written in this file: do not convert, reformat, complete or invent anything, and
never use text from any other file. If the form is refused, you get the reason: fix it and submit again."""


def fill_each(model, task, items, item_form, copied=(), item_problems=None, context=None, relevant=None,
              explain=None, max_rounds=3):
    """One small form per item, the way a function-calling model works best: it sees one item, makes one
    selection from a closed menu, and copies only from that item. Answers so far shape the next form (e.g. a
    reply can only point at an earlier email). Returns {"ok", "answers", "rounds", "corrections"}.

    items: [(name, text)] in order. item_form(name, answers) -> JSON schema for this item.
    copied: fields whose text must appear in this item. item_problems(name, text, answer, answers) -> problems
    (e.g. an engine that cannot read a copied time). context(name, answers) -> text shown above the item.
    relevant(answer) -> answer with the fields that do not apply to its choice cleared, before anything is
    judged: a small model fills every field it is shown, and refusing an "add" over a field only a "move" uses
    is the harness being pedantic, not the model being wrong.
    copied may also be a dict field -> same(value, text) -> bool, for fields where an engine decides what counts as
    the same value ('9:00 am' for a file that says '9am'); a value found nowhere in the item still fails.
    explain(name, form, problems) -> problems lets a job say WHY in its own words (the generic message for a
    value outside a menu only lists the menu)."""
    if hasattr(model, "thinking"):
        model.thinking = False
    answers, corrections, rounds = [], [], 0
    for name, text in items:
        schema = item_form(name, answers)
        tool = {"type": "function", "function": {"name": "submit", "description": "Submit the form for this file.",
                                                 "parameters": schema}}
        above = (context(name, answers) + "\n\n") if context else ""
        messages = [{"role": "system", "content": ITEM_PROMPT},
                    {"role": "user", "content": f"Task: {task}\n\n{above}The file:\n--- {name}\n{text}"}]
        for _ in range(max_rounds):
            rounds += 1
            reply = model(messages, [tool])
            form = _form_from(reply)
            messages.append({"role": "assistant", "content": json.dumps(form) if form is not None
                             else (reply.get("content") or "")})
            if form is not None and relevant:
                form = relevant(form)
            if form is None:
                problems = ["No form was submitted. Call `submit` with the form."]
            else:
                problems = shape_problems(form, schema)
                if not problems:
                    same = copied if isinstance(copied, dict) else {f: None for f in copied}
                    problems = [f"{f} {form.get(f)!r} is not in this file; copy it exactly" for f, judge in same.items()
                                if isinstance(form.get(f), str) and form.get(f).strip()
                                and _copy_key(form[f]) not in _copy_key(text)
                                and not (judge and judge(form[f], text))]
                if not problems and item_problems:
                    problems = item_problems(name, text, form, answers)
            if problems and explain and form is not None:
                problems = explain(name, form, problems)
            if not problems:
                answers.append({"file": name, **form})
                break
            corrections.append(f"{name}: " + "; ".join(problems[:4]))
            messages.append({"role": "user", "content": "; ".join(problems[:4]) + "\nFix exactly that and call "
                             "`submit` again."})
        else:
            return {"ok": False, "answers": answers, "rounds": rounds, "corrections": corrections}
    return {"ok": True, "answers": answers, "rounds": rounds, "corrections": corrections}
