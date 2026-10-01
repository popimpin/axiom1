"""Two things a kernel-less mesh needs: every attempt leaves a record, and the harness decides when the
model thinks.

The commonest failure in the first lessons run was an agent stopping without ever claiming: no claim,
no verdict, no lesson. Now a release, a lease expiry, or a runner that ends empty-handed each leave an
`abandoned` lesson. Thinking is a dial set by the harness: on for new work, off once the task arrives
with a verified skill, back on after a refutation.
"""
import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

from axiom1 import Axiom, AxiomError
from axiom1.agent import run_agent
from test_agent import ScriptedModel
from test_core import FIXED, REAL_TEST, TAUTOLOGY, Repo

TITLE = "fix add"


class Abandoned(unittest.TestCase):
    def setUp(self):
        self.now = [1000.0]
        self.ax = Axiom(clock=lambda: self.now[0])
        for a in ("operator", "first", "second"):
            self.ax.join(a)

    def tearDown(self):
        self.ax.db.close()

    def test_release_leaves_a_lesson_with_the_agents_account_labelled_declared(self):
        t = self.ax.post_task("operator", TITLE)
        self.ax.take_task("first")
        self.now[0] += 42
        self.ax.release_task("first", t["id"], "tried editing calc.py but could not find the test path")
        [lesson] = self.ax.take_task("second")["lessons"]
        self.assertEqual(lesson["kind"], "abandoned")
        self.assertIn("held this task for 42s", lesson["lesson"])
        self.assertIn("(declared, unverified): tried editing calc.py", lesson["lesson"])
        rec = self.ax.track_record("first")
        self.assertEqual((rec["released"], rec["leases_expired"], rec["refuted"]), (1, 0, 0))

    def test_only_the_holder_can_release(self):
        t = self.ax.post_task("operator", TITLE)
        self.ax.take_task("first")
        with self.assertRaises(AxiomError):
            self.ax.release_task("second", t["id"], "not mine")

    def test_an_expired_lease_leaves_a_lesson_too(self):
        self.ax.post_task("operator", TITLE)
        self.ax.take_task("first", lease_s=60)
        self.now[0] += 61
        [lesson] = self.ax.take_task("second")["lessons"]
        self.assertEqual(lesson["kind"], "abandoned")
        self.assertIn("lease expired", lesson["reason"])


class RunnerReleases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Repo(Path(self.tmp.name) / "repo")
        self.db = str(Path(self.tmp.name) / "axiom1.db")
        ax = Axiom(self.db)
        ax.register_check("unit", self.repo.root, [sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                          ["tests/"])
        ax.join("operator")
        ax.post_task("operator", TITLE)
        ax.db.close()

    def tearDown(self):
        self.tmp.cleanup()

    def _wt(self, name):
        wt = Path(self.tmp.name) / name
        self.repo.git("worktree", "add", "-q", "--detach", str(wt), self.repo.base)
        return wt

    def test_a_run_that_gives_up_releases_and_leaves_a_lesson(self):
        steps = iter([("take_task", {}), ("write_file", {"path": "notes.txt", "content": "stuck"})])

        def quitter(messages, tools):
            try:
                name, args = next(steps)
            except StopIteration:
                return {"role": "assistant", "content": "I give up."}
            return {"role": "assistant", "content": "", "tool_calls": [
                {"id": name, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}

        asyncio.run(run_agent("first", self._wt("wt1"), self.db, quitter, log=lambda *_: None))
        ax = Axiom(self.db)
        [lesson] = [r for r in ax.lessons() if r["kind"] == "abandoned"]
        self.assertIn("wrote notes.txt", lesson["body"])
        self.assertIn("declared, unverified", lesson["body"])
        self.assertEqual(ax.track_record("first")["released"], 1)
        ax.db.close()

    def test_a_witnessed_run_does_not_release(self):
        asyncio.run(run_agent("first", self._wt("wt1"), self.db, ScriptedModel(REAL_TEST, FIXED), log=lambda *_: None))
        ax = Axiom(self.db)
        self.assertEqual([r for r in ax.lessons() if r["kind"] == "abandoned"], [])
        ax.db.close()


class ThinkingDial(RunnerReleases):
    class Recording(ScriptedModel):
        def __init__(self, *a):
            super().__init__(*a)
            self.thinking, self.seen = None, []

        def __call__(self, messages, tools):
            self.seen.append(self.thinking)
            return super().__call__(messages, tools)

    def test_new_work_thinks(self):
        m = self.Recording(REAL_TEST, FIXED)
        asyncio.run(run_agent("first", self._wt("wt1"), self.db, m, log=lambda *_: None))
        self.assertTrue(all(m.seen))

    def test_a_verified_skill_turns_thinking_off_and_a_refutation_turns_it_back_on(self):
        asyncio.run(run_agent("first", self._wt("wt1"), self.db, ScriptedModel(REAL_TEST, FIXED), log=lambda *_: None))
        ax = Axiom(self.db)
        ax.post_task("operator", TITLE)              # the job comes round again; a skill now exists
        ax.db.close()
        m = self.Recording(TAUTOLOGY)                # this time the agent ships a test that proves nothing
        asyncio.run(run_agent("second", self._wt("wt2"), self.db, m, log=lambda *_: None))
        # calls: briefing, take_task (thinking on until the task arrives), then off ... until refuted
        self.assertEqual(m.seen[:2], [True, True])
        self.assertIn(False, m.seen)
        self.assertEqual(m.seen[-1], True)          # the call after the refutation thinks again

    def test_fixed_settings_are_respected(self):
        for setting, expect in (("on", True), ("off", False)):
            ax = Axiom(self.db)
            ax.post_task("operator", TITLE)
            ax.db.close()
            m = self.Recording(REAL_TEST, FIXED)
            asyncio.run(run_agent(f"a-{setting}", self._wt(f"wt-{setting}"), self.db, m, log=lambda *_: None,
                                  thinking=setting))
            self.assertEqual(set(m.seen), {expect}, setting)


if __name__ == "__main__":
    unittest.main()
