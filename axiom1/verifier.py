"""Fail-before / pass-after verification.

A claim says "commit AFTER fixes what was wrong at commit BEFORE". The server
checks that itself, in two runs of a command a human registered:

  1. BEFORE's code with AFTER's tests laid over it  -> must FAIL
  2. AFTER's code and tests                          -> must PASS

Run 1 is the negative control. A test that passes on the unfixed code proves
nothing, so the claim is refuted. The overlay matters: without it, a test file
that only exists in AFTER is simply missing in run 1, the command errors, and a
test that asserts nothing would look like it "failed before".

Run 2 needs a control of its own, because the fix runs in full: it can leave the
bug in place and rig HOW the tests run instead (shadow the test runner, disable
assertions when imported). So after run 2 passes:

  3. AFTER's code and tests plus one canary test that must fail  -> must FAIL

once per assertion style (assertEqual, assertTrue, bare assert), each with a
random name and random values. A harness that lets a canary pass would have let
anything pass, and the claim is refuted as tampering.

This catches tampering that is blanket, or aimed at an assertion style. A rig
aimed at one specific test by name still gets through; that is what held-out
tests the agent never sees are for.
"""
import io
import secrets
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from .sandbox import LocalSandbox


# stdin=DEVNULL everywhere: under MCP the server's own stdin IS the protocol pipe, and a child
# that inherits it stalls the session
def _git(repo, *args, text=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          stdin=subprocess.DEVNULL, text=text, check=True)


def resolve(repo, ref):
    """Pin a ref to a commit sha, so moving a branch later cannot change the verdict."""
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()


def _export(repo, sha, dest, paths=()):
    data = _git(repo, "archive", "--format=tar", sha, *paths, text=False).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")


def _existing(repo, sha, paths):
    """The subset of `paths` present at `sha` (git archive errors on a missing pathspec)."""
    out = _git(repo, "ls-tree", "-r", "--name-only", sha, "--", *paths).stdout.splitlines()
    return [p for p in paths
            if any(o == p.rstrip("/") or o.startswith(p.rstrip("/") + "/") for o in out)]


CANARY_STYLES = {
    "assertEqual": "self.assertEqual({a!r}, {b!r})",
    "assertTrue": "self.assertTrue({a!r} == {b!r})",
    "assert": "assert {a!r} == {b!r}",
}


def _canary(style, passing=False):
    """A unittest-style test (pytest collects these too) that must fail, named and valued at random.
    `passing=True` gives its twin, identical in shape, that must pass (used for calibration)."""
    tag, a, b = secrets.token_hex(6), secrets.token_hex(4), secrets.token_hex(4)
    b = a if passing else b
    body = CANARY_STYLES[style].format(a=a, b=b)
    return (f"test_{tag}.py",
            f"import unittest\n\n\nclass T{tag}(unittest.TestCase):\n    def test_{tag}(self):\n        {body}\n")


def _canary_dir(tree, test_path, is_dir):
    """Where a test file under `test_path` belongs: inside it if a directory, beside it if a file.
    `is_dir` comes from the AFTER tree: in the BEFORE tree a new test directory may not exist yet."""
    p = Path(tree) / test_path.rstrip("/")
    return p if is_dir else p.parent


def _canary_collected(sandbox, base_dir, argv, test_path, is_dir):
    """Calibration: does this command run a file shaped like a canary at all?

    A PASSING twin of the canary goes into the test directory of the untouched BEFORE tree, with that
    directory emptied first, and the command must exit 0. Checking with a failing canary instead
    would be wrong: "no tests ran" also exits non-zero, so a command restricted to one file would
    look like it had run the canary."""
    with tempfile.TemporaryDirectory() as ctrl:
        shutil.copytree(base_dir, ctrl, dirs_exist_ok=True)
        target = _canary_dir(ctrl, test_path, is_dir)
        if is_dir and target.exists():
            shutil.rmtree(target)  # our own temp copy: only the twin may be collected
        target.mkdir(parents=True, exist_ok=True)
        name, text = _canary("assertEqual", passing=True)
        (target / name).write_text(text, encoding="utf-8")
        return sandbox.run(ctrl, argv).returncode == 0


def _tamper_check(sandbox, after_dir, argv, test_path, is_dir):
    """Run the fixed tree once per canary style. Returns the first style whose canary PASSED, or None."""
    for style in CANARY_STYLES:
        with tempfile.TemporaryDirectory() as ctrl:
            shutil.copytree(after_dir, ctrl, dirs_exist_ok=True)
            name, text = _canary(style)
            (_canary_dir(ctrl, test_path, is_dir) / name).write_text(text, encoding="utf-8")
            run = sandbox.run(ctrl, argv)
        if run.returncode == 0:
            return style, run
    return None


def _holdout_run(sandbox, after_dir, argv, test_path, is_dir, holdout):
    """The fixed tree plus the operator's held-out tests, which the agent has never seen."""
    with tempfile.TemporaryDirectory() as ctrl:
        shutil.copytree(after_dir, ctrl, dirs_exist_ok=True)
        dest = _canary_dir(ctrl, test_path, is_dir)
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copytree(holdout, dest, dirs_exist_ok=True)
        return sandbox.run(ctrl, argv)


def fail_before_pass_after(repo, before_sha, after_sha, argv, test_paths, sandbox=None, holdout=None):
    """Returns (label, reason, evidence, private). label is 'witnessed' or 'refuted'.

    `evidence` goes back to the agent. `private` is for the operator only: held-out test output
    names the inputs and expected values, and an agent that saw them could special-case those too."""
    sandbox = sandbox or LocalSandbox()
    label, reason, evidence, private = _verify(repo, before_sha, after_sha, argv, test_paths, sandbox, holdout)
    evidence["sandbox"] = sandbox.describe()
    return label, reason, evidence, private


def _verify(repo, before_sha, after_sha, argv, test_paths, sandbox, holdout):
    overlay = _existing(repo, after_sha, test_paths)
    if not overlay:
        return "refuted", "the fix commit contains none of the registered test paths", {}, {}
    with tempfile.TemporaryDirectory() as base_dir, tempfile.TemporaryDirectory() as before_dir, \
            tempfile.TemporaryDirectory() as after_dir:
        _export(repo, before_sha, base_dir)
        _export(repo, before_sha, before_dir)
        _export(repo, after_sha, before_dir, overlay)
        _export(repo, after_sha, after_dir)
        before = sandbox.run(before_dir, argv)
        after = sandbox.run(after_dir, argv)
        evidence = {
            "before": {"sha": before_sha, "exit": before.returncode, "tail": before.output[-800:]},
            "after": {"sha": after_sha, "exit": after.returncode, "tail": after.output[-800:]},
        }
        private = {}
        if before.returncode == 0:
            return "refuted", "the test passes without the fix, so it proves nothing", evidence, private
        if after.returncode != 0:
            return "refuted", "the test still fails with the fix", evidence, private
        is_dir = (Path(after_dir) / overlay[0].rstrip("/")).is_dir()

        if holdout:
            held = _holdout_run(sandbox, after_dir, argv, overlay[0], is_dir, holdout)
            evidence["holdout"] = {"ran": True, "passed": held.returncode == 0}
            if held.returncode != 0:
                private["holdout_tail"] = held.output[-2000:]
                return ("refuted", "the fix fails the operator's held-out tests: it does not fix the "
                                   "behaviour in general, only what its own test checks",
                        evidence, private)

        if not _canary_collected(sandbox, base_dir, argv, overlay[0], is_dir):
            evidence["canaries"] = {"collected": False,
                                    "note": "this check's command does not run a canary placed under its "
                                            "test path, so tampering could not be checked"}
            return ("witnessed", "failed before the fix, passes after it"
                    + (", passes the held-out tests" if holdout else "") + " (no tamper check possible)",
                    evidence, private)
        tampered = _tamper_check(sandbox, after_dir, argv, overlay[0], is_dir)
    evidence["canaries"] = {"collected": True, "styles": list(CANARY_STYLES), "all_failed": tampered is None}
    if tampered:
        style, run = tampered
        evidence["canaries"].update(passed=style, tail=run.output[-800:])
        reason = (f"tampering: with the fix applied, a test that must fail ({style}) passed, "
                  "so the fix changed how tests run rather than what they test")
        return "refuted", reason, evidence, private
    return ("witnessed", "failed before the fix, passes after it"
            + (", passes the held-out tests" if holdout else "") + ", and a must-fail canary still fails",
            evidence, private)


def _replace_tests(tree, repo, sha, test_paths):
    """Swap `tree`'s tests for the ones at `sha`: the paths are emptied first, so tests deleted
    since then come back and tests added since then are gone."""
    for p in test_paths:
        target = Path(tree) / p.rstrip("/")
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    present = _existing(repo, sha, test_paths)
    if present:
        _export(repo, sha, tree, present)


def no_regression(repo, before_sha, after_sha, argv, test_paths, sandbox=None, holdout=None):
    """"I changed this and nothing broke". Returns (label, reason, evidence, private).

      1. BEFORE's tree                          -> must PASS (the baseline)
      2. AFTER's code with BEFORE's tests       -> must PASS (deleting a broken test cannot hide it)
      3. AFTER's tree                           -> must PASS
      4. held-out tests: any that passed on BEFORE must pass on AFTER
      5. canaries on AFTER                      -> must FAIL (the harness was not rigged)"""
    sandbox = sandbox or LocalSandbox()
    evidence, private = {"sandbox": sandbox.describe(), "kind": "no_regression"}, {}
    with tempfile.TemporaryDirectory() as before_dir, tempfile.TemporaryDirectory() as old_tests_dir, \
            tempfile.TemporaryDirectory() as after_dir:
        _export(repo, before_sha, before_dir)
        _export(repo, after_sha, after_dir)
        _export(repo, after_sha, old_tests_dir)
        _replace_tests(old_tests_dir, repo, before_sha, test_paths)

        base = sandbox.run(before_dir, argv)
        evidence["before"] = {"sha": before_sha, "exit": base.returncode, "tail": base.output[-800:]}
        if base.returncode != 0:
            return ("refuted", "the tests already fail before the change, so there is no baseline "
                               "to show nothing broke", evidence, private)
        old = sandbox.run(old_tests_dir, argv)
        evidence["old_tests_on_new_code"] = {"exit": old.returncode, "tail": old.output[-800:]}
        if old.returncode != 0:
            return ("refuted", "regression: tests that passed before fail on the changed code",
                    evidence, private)
        new = sandbox.run(after_dir, argv)
        evidence["after"] = {"sha": after_sha, "exit": new.returncode, "tail": new.output[-800:]}
        if new.returncode != 0:
            return "refuted", "the changed tree fails its own tests", evidence, private

        overlay = _existing(repo, after_sha, test_paths) or test_paths[:1]
        is_dir = (Path(after_dir) / overlay[0].rstrip("/")).is_dir() or overlay[0].endswith("/")
        if holdout:
            held_before = _holdout_run(sandbox, before_dir, argv, overlay[0], is_dir, holdout)
            held_after = _holdout_run(sandbox, after_dir, argv, overlay[0], is_dir, holdout)
            evidence["holdout"] = {"ran": True, "passed_before": held_before.returncode == 0,
                                   "passed": held_after.returncode == 0}
            if held_before.returncode == 0 and held_after.returncode != 0:
                private["holdout_tail"] = held_after.output[-2000:]
                return ("refuted", "regression: the operator's held-out tests passed before the change "
                                   "and fail after it", evidence, private)

        if not _canary_collected(sandbox, before_dir, argv, overlay[0], is_dir):
            evidence["canaries"] = {"collected": False}
            return ("witnessed", "no regression: the old tests pass on the new code (no tamper check "
                                 "possible)", evidence, private)
        tampered = _tamper_check(sandbox, after_dir, argv, overlay[0], is_dir)
    evidence["canaries"] = {"collected": True, "styles": list(CANARY_STYLES), "all_failed": tampered is None}
    if tampered:
        style, _ = tampered
        return ("refuted", f"tampering: with the change applied, a test that must fail ({style}) passed",
                evidence, private)
    return ("witnessed", "no regression: the tests passed before, the old tests pass on the new code, the "
                         "new tests pass" + (", held-out tests still pass" if holdout else "")
            + ", and a must-fail canary still fails", evidence, private)


def is_repo(path):
    return Path(path).exists() and subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-dir"], capture_output=True,
        stdin=subprocess.DEVNULL).returncode == 0
