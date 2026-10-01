"""One witnessed pass locks in a job's process; the next instance replays it, the model fills only the entry.

A deliver job's work is `process.py`: a program from the original files to the deliverable, with the
job's variable parts in `entry.json`. The server re-runs it on a clean copy of the original files and
judges ITS output. Only a process that reproduces a witnessed delivery is locked in, as the next
version for that check. A later task judged by the same check gets the locked process: the runner
replays it (the model fills only the entry, if there is one) and the model is called in to repair it
only when the input strays from what the process handles.
"""
import asyncio
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from axiom1 import Axiom
from axiom1.agent import run_agent
from axiom1.sandbox import DockerSandbox
from test_deliver import CHECK, GOOD, DataRepo

IMAGE = "python:3.13-slim"
DOCKER = DockerSandbox.available() and subprocess.run(
    ["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0

# reads receipts written as "Shop\n12.50\n"; writes the summary file named in entry.json
PROCESS = '''import csv, json
from pathlib import Path
entry = json.loads(Path("entry.json").read_text()) if Path("entry.json").exists() else {}
rows = []
for p in sorted(Path("receipts").glob("receipt_*.txt")) or sorted(Path("receipts").glob("*.txt")):
    shop, amount = p.read_text().splitlines()[:2]
    rows.append([shop, f"{float(amount):.2f}"])
with open(entry.get("output", "summary.csv"), "w", newline="") as f:
    csv.writer(f).writerows([["vendor", "amount"]] + rows)
'''
# a repaired version that also reads "Total: $7.25"
PROCESS_V2 = PROCESS.replace('rows.append([shop, f"{float(amount):.2f}"])',
                             'rows.append([shop, f"{float(amount.split(\'$\')[-1]):.2f}"])')
ENTRY = json.dumps({"output": "summary.csv"})


class Locking(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = DataRepo(Path(self.tmp.name) / "data")
        self.check = Path(self.tmp.name) / "check"
        self.check.mkdir()
        (self.check / "check.py").write_text(CHECK, encoding="utf-8")
        (self.check / "truth.json").write_text(json.dumps({"receipts": 3, "total": 59.75}), encoding="utf-8")
        self.ax = Axiom()
        self.ax.register_check("receipts", self.repo.root, [sys.executable, "-I", ".axiom_check/check.py"], [],
                               holdout=str(self.check), claim_kind="deliver")
        self.ax.join("a")

    def tearDown(self):
        self.ax.db.close()
        self.tmp.cleanup()

    def _verify(self, files):
        after = self.repo.branch(f"b{len(self.ax.events())}", files)
        return self.ax.verify(self.ax.claim("a", "summary", "receipts", self.repo.base, after)["id"])

    def test_a_process_that_reproduces_the_delivery_is_locked_in(self):
        v = self._verify({"process.py": PROCESS, "entry.json": ENTRY, "summary.csv": GOOD})
        self.assertEqual(v["label"], "witnessed", v["reason"])
        self.assertTrue(v["evidence"]["process"]["reproduced"])
        self.assertEqual(self.ax.process_for("receipts")["version"], 1)

    def test_hand_typed_output_with_a_process_that_does_not_produce_it_locks_nothing(self):
        broken = PROCESS.replace('f"{float(amount):.2f}"', '"0.00"')
        v = self._verify({"process.py": broken, "summary.csv": GOOD})
        self.assertEqual(v["label"], "witnessed")             # the files ARE right...
        self.assertFalse(v["evidence"]["process"]["reproduced"])
        self.assertIsNone(self.ax.process_for("receipts"))     # ...but there is no process to trust

    def test_no_process_no_lock(self):
        v = self._verify({"summary.csv": GOOD})
        self.assertEqual(v["label"], "witnessed")
        self.assertIsNone(self.ax.process_for("receipts"))

    def test_a_refuted_delivery_never_locks(self):
        v = self._verify({"process.py": PROCESS.replace("[:2]", "[:1] * 2"), "summary.csv": "vendor,amount\n"})
        self.assertEqual(v["label"], "refuted")
        self.assertIsNone(self.ax.process_for("receipts"))

    def test_versions_accumulate(self):
        self._verify({"process.py": PROCESS, "summary.csv": GOOD})
        self._verify({"process.py": PROCESS_V2, "summary.csv": GOOD})
        self.assertEqual(self.ax.process_for("receipts")["version"], 2)
        self.assertIn("split('$')", self.ax.process_for("receipts")["script"])

    def test_a_task_names_its_check_and_carries_the_process(self):
        self._verify({"process.py": PROCESS, "entry.json": ENTRY, "summary.csv": GOOD})
        self.ax.post_task("a", "summarise receipts", check_id="receipts")
        taken = self.ax.take_task("a")
        self.assertEqual(taken["process"]["version"], 1)
        self.assertEqual(json.loads(taken["process"]["entry_example"]), {"output": "summary.csv"})
        self.ax.post_task("a", "something else")
        self.assertNotIn("process", self.ax.take_task("a"))


class RegressionProtected(Locking):
    """Improving a process at scale must not break what it already does: a changed version is locked in only
    if it passes the new instance AND every earlier one, each with its own original files, entry and check."""

    # handles only "Total: $7.25" lines: right for the new month, wrong for every month before it
    ONLY_DOLLARS = PROCESS.replace('f"{float(amount):.2f}"', 'f"{float(amount.split(\'$\')[1]):.2f}"')

    def _new_month(self):
        """A second instance of the same job: its own repo, its own check, the same check id."""
        repo = DataRepo(Path(self.tmp.name) / "april")
        # April's receipts all come in the new format (same shops and amounts, so the same check passes)
        repo.write("receipts/a.txt", "Corner Cafe\nTotal: $12.50\n")
        repo.write("receipts/b.txt", "Hardware World\nTotal: $40.00\n")
        repo.write("receipts/c.txt", "Corner Cafe\nTotal: $7.25\n")
        repo.base = repo.commit("april")
        self.ax.register_check("receipts", repo.root, [sys.executable, "-I", ".axiom_check/check.py"], [],
                               holdout=str(self.check), claim_kind="deliver")
        self.repo = repo

    def test_a_fix_that_breaks_an_earlier_instance_is_not_locked_in(self):
        self._verify({"process.py": PROCESS, "summary.csv": GOOD})                        # v1, March
        self._new_month()
        v = self._verify({"process.py": self.ONLY_DOLLARS, "summary.csv": GOOD})
        self.assertEqual(v["label"], "witnessed")                  # April's spreadsheet IS right...
        self.assertEqual(v["evidence"]["process"]["regression"]["failed"], 1)
        self.assertIn("breaks 1 of 1 earlier", v["reason"])
        self.assertEqual(self.ax.process_for("receipts")["version"], 1)   # ...but March's method stays

    def test_a_fix_that_handles_old_and_new_is_locked_in(self):
        self._verify({"process.py": PROCESS, "summary.csv": GOOD})
        self._new_month()
        v = self._verify({"process.py": PROCESS_V2, "summary.csv": GOOD})
        self.assertEqual(v["evidence"]["process"]["regression"], {"instances": 1, "failed": 0, "first_failure": []})
        self.assertEqual(self.ax.process_for("receipts")["version"], 2)

    def test_running_the_same_process_again_adds_an_instance_and_a_proven_fit_not_a_version(self):
        self.ax.post_task("a", "March receipts", check_id="receipts")
        t1 = self.ax.take_task("a")
        after = self.repo.branch("m", {"process.py": PROCESS, "summary.csv": GOOD})
        self.ax.verify(self.ax.claim("a", "march", "receipts", self.repo.base, after, task_id=t1["id"])["id"])
        self._new_month()
        self.ax.post_task("a", "April receipts", check_id="receipts")
        t2 = self.ax.take_task("a")
        after = self.repo.branch("a2", {"process.py": PROCESS_V2, "summary.csv": GOOD})
        self.ax.verify(self.ax.claim("a", "april", "receipts", self.repo.base, after, task_id=t2["id"])["id"])
        self.assertEqual(self.ax.process_for("receipts")["version"], 2)
        # and May, with the April version unchanged
        repo = DataRepo(Path(self.tmp.name) / "may")
        self.ax.register_check("receipts", repo.root, [sys.executable, "-I", ".axiom_check/check.py"], [],
                               holdout=str(self.check), claim_kind="deliver")
        self.repo = repo
        self.ax.post_task("a", "May receipts", check_id="receipts")
        t3 = self.ax.take_task("a")
        after = repo.branch("may", {"process.py": PROCESS_V2, "summary.csv": GOOD})
        v = self.ax.verify(self.ax.claim("a", "may", "receipts", repo.base, after, task_id=t3["id"])["id"])
        self.assertIn("replayed process v2", v["reason"])
        p = self.ax.process_for("receipts")
        self.assertEqual(p["version"], 2)
        self.assertEqual(p["proven"], ["April receipts", "May receipts"])


# the same job, with the table written by the engine instead of by hand
ENGINE_PROCESS = '''from pathlib import Path
from axiom_engines import table
rows = []
for p in sorted(Path("receipts").glob("*.txt")):
    shop, amount = p.read_text().splitlines()[:2]
    rows.append({"vendor": shop, "amount": f"{float(amount):.2f}"})
table.write_csv("summary.csv", ["vendor", "amount"], rows)
'''
# an engine an agent might commit to rig its own run: it writes the right file whatever it is given
RIGGED_TABLE = '''def write_csv(path, columns, rows):
    open(path, "w").write(%r)
''' % GOOD


class EnginesInProcesses(Locking):
    """A process imports the harness's engines, and only ever the harness's own copy."""

    def _no_install(self):
        from unittest import mock
        return mock.patch("axiom1.verifier.install_engines", lambda tree: None)

    def test_a_process_built_on_engines_is_locked_in(self):
        v = self._verify({"process.py": ENGINE_PROCESS, "summary.csv": GOOD})
        self.assertEqual(v["label"], "witnessed", v["reason"])
        self.assertTrue(v["evidence"]["process"]["reproduced"], v["evidence"]["process"])
        self.assertEqual(self.ax.process_for("receipts")["version"], 1)

    def test_control_without_the_engines_installed_it_cannot_run(self):
        with self._no_install():
            v = self._verify({"process.py": ENGINE_PROCESS, "summary.csv": GOOD})
        self.assertFalse(v["evidence"]["process"]["reproduced"])
        self.assertIn("axiom_engines", v["evidence"]["process"]["run_tail"])

    def test_engines_an_agent_commits_are_replaced_by_the_real_ones(self):
        # the rigged engine ignores its input; the process feeds it nothing. Only the real engine,
        # writing exactly the rows it is given, shows the process produces nothing.
        empty = ENGINE_PROCESS.replace("rows.append(", "0 and rows.append(")
        files = {"process.py": empty, "summary.csv": GOOD, "axiom_engines/__init__.py": "",
                 "axiom_engines/table.py": RIGGED_TABLE}
        v = self._verify(files)
        self.assertFalse(v["evidence"]["process"]["reproduced"])
        self.assertIsNone(self.ax.process_for("receipts"))

    def test_planted_engines_never_reach_the_replay_at_all(self):
        # Why the rig fails: the replay starts from the ORIGINAL files plus only process.py and entry.json.
        # With the harness's install switched off, the agent's committed engine is simply not there.
        empty = ENGINE_PROCESS.replace("rows.append(", "0 and rows.append(")
        files = {"process.py": empty, "summary.csv": GOOD, "axiom_engines/__init__.py": "",
                 "axiom_engines/table.py": RIGGED_TABLE}
        with self._no_install():
            v = self._verify(files)
        self.assertFalse(v["evidence"]["process"]["reproduced"])
        self.assertIn("No module named 'axiom_engines'", v["evidence"]["process"]["run_tail"])


@unittest.skipUnless(DOCKER, f"docker daemon or {IMAGE} not available")
class Replay(unittest.TestCase):
    """The runner end to end: replay with no model, replay with an entry, and repair when input strays."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.check = Path(self.tmp.name) / "check"
        self.check.mkdir()
        (self.check / "check.py").write_text(CHECK, encoding="utf-8")
        (self.check / "truth.json").write_text(json.dumps({"receipts": 3, "total": 59.75}), encoding="utf-8")
        self.db = str(Path(self.tmp.name) / "a.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _instance(self, name, receipts=None):
        """A new instance of the job: fresh data, the same check id (the job's type)."""
        repo = DataRepo(Path(self.tmp.name) / name)
        if receipts:
            for rel, text in receipts.items():
                repo.write(rel, text)
            repo.base = repo.commit("this month's receipts")
        ax = Axiom(self.db)
        ax.register_check("receipts", repo.root, [sys.executable, "-I", ".axiom_check/check.py"], [],
                          holdout=str(self.check), claim_kind="deliver")
        ax.join("operator")
        ax.post_task("operator", "Summarise my receipts into summary.csv", check_id="receipts")
        ax.db.close()
        wt = Path(self.tmp.name) / f"wt-{name}"
        repo.git("worktree", "add", "-q", "--detach", str(wt), "main")
        return repo, wt

    def _lock_v1(self, with_entry):
        repo, _ = self._instance("first")
        ax = Axiom(self.db)
        ax.join("teacher")
        files = {"process.py": PROCESS, "summary.csv": GOOD, **({"entry.json": ENTRY} if with_entry else {})}
        task = ax.take_task("teacher")
        v = ax.verify(ax.claim("teacher", "summary", "receipts", repo.base, repo.branch("teach", files),
                               task_id=task["id"])["id"])
        self.assertEqual(v["label"], "witnessed", v["reason"])
        self.assertEqual(ax.process_for("receipts")["version"], 1)
        ax.db.close()

    def test_a_locked_process_replays_with_no_model_at_all(self):
        self._lock_v1(with_entry=False)
        _, wt = self._instance("second")

        def no_model(messages, tools):
            raise AssertionError("the model was called for a job the locked process handles")
        messages = asyncio.run(run_agent("worker", wt, self.db, no_model, log=lambda *_: None,
                                         shell=DockerSandbox(IMAGE)))
        self.assertIn("witnessed", messages[-1]["content"])

    def test_the_model_fills_only_the_entry(self):
        self._lock_v1(with_entry=True)
        _, wt = self._instance("second")
        calls = []

        def entry_only(messages, tools):
            calls.append(tools)
            return {"role": "assistant", "content": '{"output": "summary.csv"}'}
        messages = asyncio.run(run_agent("worker", wt, self.db, entry_only, log=lambda *_: None,
                                         shell=DockerSandbox(IMAGE)))
        self.assertIn("witnessed", messages[-1]["content"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0], [])                            # no tools offered: it fills a form, nothing else

    def test_input_that_strays_from_the_process_brings_in_the_model_to_repair_it(self):
        self._lock_v1(with_entry=False)
        _, wt = self._instance("strayed", {"receipts/c.txt": "Corner Cafe\nTotal: $7.25\n"})
        seen = {"opening": None}
        steps = iter([("write_file", {"path": "process.py", "content": PROCESS_V2}),
                      ("run", {"command": "python -I process.py"}), ("commit", {"message": "handle Total: $"})])

        def repairer(messages, tools):
            seen["opening"] = seen["opening"] or messages[1]["content"]
            try:
                name, args = next(steps)
            except StopIteration:
                if not any(m.get("tool_calls", [{}])[0].get("function", {}).get("name") == "claim"
                           for m in messages if m["role"] == "assistant" and m.get("tool_calls")):
                    sha = json.loads([m for m in messages if m["role"] == "tool"][-1]["content"])["sha"]
                    task = json.loads(messages[1]["content"].split(": ", 1)[1].split("\n\n")[0])
                    base = messages[0]["content"].split("before_ref = ")[1].split(",")[0]
                    name, args = "claim", {"statement": "repaired", "check_id": "receipts", "before_ref": base,
                                           "after_ref": sha, "task_id": task["id"]}
                elif not any(m.get("tool_calls", [{}])[0].get("function", {}).get("name") == "verify"
                             for m in messages if m["role"] == "assistant" and m.get("tool_calls")):
                    claim_id = json.loads([m for m in messages if m["role"] == "tool"][-1]["content"])["id"]
                    name, args = "verify", {"claim_id": claim_id}
                else:
                    return {"role": "assistant", "content": "done"}
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": name + str(len(messages)), "type": "function",
                 "function": {"name": name, "arguments": json.dumps(args)}}]}

        asyncio.run(run_agent("worker", wt, self.db, repairer, log=lambda *_: None, shell=DockerSandbox(IMAGE)))
        self.assertIn("did not pass", seen["opening"])
        ax = Axiom(self.db)
        self.assertEqual(ax.process_for("receipts")["version"], 2)        # the repair is locked in
        ax.db.close()


if __name__ == "__main__":
    unittest.main()
