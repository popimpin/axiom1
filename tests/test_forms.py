"""Form mode: the model fills one form, a fixed pipeline of engines does the rest, and the harness checks the form
(shape, copied text, the pipeline itself) before anything is delivered."""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from axiom1 import forms  # noqa: E402

FILES = {"inbox/01_a.txt": "Subject: Dentist\n\nCould we do dentist on Friday May 15 at 1:00 pm? About 60 minutes.\n"
                           "> Me: Sounds good.\n",
         "inbox/02_b.txt": "Subject: Coffee\n\nCoffee on Friday May 8 at 10am?\n> Me: Sorry, I can't.\n"}
SCHEMA = {"type": "object", "required": ["items"], "properties": {"items": {"type": "array", "items": {
    "type": "object", "required": ["file", "kind", "when"], "properties": {
        "file": {"type": "string", "enum": sorted(FILES)},
        "kind": {"type": "string", "enum": ["add", "ignore"]},
        "when": {"type": "string"}}}}}}
# writes one line per added item, through the engines: a wrong time is refused by time.parse_time
PIPELINE = '''import json
from pathlib import Path
from axiom_engines import table, time
entry = json.loads(Path("entry.json").read_text())
rows = [{"start": time.parse_time(i["when"])} for i in entry["items"] if i["kind"] == "add"]
table.write_csv("out.csv", ["start"], rows)
'''
GOOD = {"items": [{"file": "inbox/01_a.txt", "kind": "add", "when": "1:00 pm"},
                  {"file": "inbox/02_b.txt", "kind": "ignore", "when": ""}]}


def scripted(*forms_in_order):
    """A model that submits these forms in turn, recording what it was shown."""
    seen = []

    def model(messages, tools):
        seen.append({"messages": list(messages), "tools": tools})
        form = forms_in_order[min(len(seen), len(forms_in_order)) - 1]
        return {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c", "type": "function", "function": {"name": "submit", "arguments": json.dumps(form)}}]}
    model.seen = seen
    return model


class Checks(unittest.TestCase):
    def test_shape(self):
        self.assertEqual(forms.shape_problems(GOOD, SCHEMA), [])
        bad = {"items": [{"file": "inbox/01_a.txt", "kind": "maybe", "when": "x"}], "extra": 1}
        found = forms.shape_problems(bad, SCHEMA)
        self.assertTrue(any("must be one of ['add', 'ignore']" in p for p in found), found)
        self.assertTrue(any("does not have: ['extra']" in p for p in found), found)
        self.assertTrue(forms.shape_problems({"items": [{"file": "inbox/01_a.txt"}]}, SCHEMA))

    def test_copied_text_must_be_in_its_own_file(self):
        self.assertEqual(forms.verbatim_problems(GOOD["items"], FILES, "file", ["when"]), [])
        converted = [{"file": "inbox/01_a.txt", "kind": "add", "when": "13:00"}]
        self.assertIn("not in that file", forms.verbatim_problems(converted, FILES, "file", ["when"])[0])
        borrowed = [{"file": "inbox/02_b.txt", "kind": "add", "when": "1:00 pm"}]   # true, but of another email
        self.assertTrue(forms.verbatim_problems(borrowed, FILES, "file", ["when"]))

    def test_the_pipeline_runs_on_the_engines_and_reports_a_refusal(self):
        ok, msg, produced = forms.run_pipeline(PIPELINE, GOOD, FILES)
        self.assertTrue(ok, msg)
        self.assertEqual(produced, {"out.csv": "start\n13:00\n"})
        ok, msg, _ = forms.run_pipeline(PIPELINE, {"items": [{"file": "inbox/01_a.txt", "kind": "add",
                                                              "when": "1"}]}, FILES)
        self.assertFalse(ok)
        self.assertIn("ambiguous", msg)
        self.assertNotIn("Traceback", msg)


class Fill(unittest.TestCase):
    def test_a_right_form_is_delivered_in_one_call_with_thinking_off(self):
        model = scripted(GOOD)
        model.thinking = None
        res = forms.fill(model, "make out.csv", FILES, SCHEMA, PIPELINE, verbatim=("items", "file", ["when"]))
        self.assertTrue(res["ok"])
        self.assertEqual((res["rounds"], len(model.seen)), (1, 1))
        self.assertIs(model.thinking, False)
        self.assertEqual(res["produced"]["out.csv"], "start\n13:00\n")
        self.assertEqual([t["function"]["name"] for t in model.seen[0]["tools"]], ["submit"])
        self.assertIn("Sounds good", model.seen[0]["messages"][1]["content"])     # the files are shown

    def test_a_refusal_goes_back_as_the_correction(self):
        wrong = {"items": [{"file": "inbox/01_a.txt", "kind": "add", "when": "13:00"},
                           {"file": "inbox/02_b.txt", "kind": "ignore", "when": ""}]}
        model = scripted(wrong, GOOD)
        res = forms.fill(model, "make out.csv", FILES, SCHEMA, PIPELINE, verbatim=("items", "file", ["when"]))
        self.assertTrue(res["ok"])
        self.assertEqual(res["rounds"], 2)
        self.assertIn("copy it exactly", model.seen[1]["messages"][-1]["content"])

    def test_it_gives_up_after_the_rounds_without_delivering(self):
        missing = {"items": [{"file": "inbox/01_a.txt", "kind": "add", "when": "1"}]}
        res = forms.fill(scripted(missing), "t", FILES, SCHEMA, PIPELINE, max_rounds=2)
        self.assertFalse(res["ok"])
        self.assertIsNone(res["entry"])
        self.assertEqual(len(res["corrections"]), 2)

    def test_fixed_values_come_from_the_harness_not_the_model(self):
        res = forms.fill(scripted(GOOD), "t", FILES, SCHEMA, PIPELINE, fixed={"as_of": "2026-10-01"})
        self.assertEqual(res["entry"]["as_of"], "2026-10-01")


if __name__ == "__main__":
    unittest.main()
