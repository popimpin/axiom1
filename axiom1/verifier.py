"""Fail-before / pass-after verification.

A claim says "commit AFTER fixes what was wrong at commit BEFORE". The server
checks that itself, in two runs of a command a human registered:

  1. BEFORE's code with AFTER's tests laid over it  -> must FAIL
  2. AFTER's code and tests                          -> must PASS

Run 1 is the negative control. A test that passes on the unfixed code proves
nothing, so the claim is refuted. The overlay matters: without it, a test file
that only exists in AFTER is simply missing in run 1, the command errors, and a
test that asserts nothing would look like it "failed before".
"""
import io
import subprocess
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

RUN_TIMEOUT_S = 300


@dataclass
class Run:
    returncode: int
    output: str


def _git(repo, *args, text=True):
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                          text=text, check=True)


def resolve(repo, ref):
    """Pin a ref to a commit sha, so moving a branch later cannot change the verdict."""
    return _git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").stdout.strip()


def _export(repo, sha, dest, paths=()):
    data = _git(repo, "archive", "--format=tar", sha, *paths, text=False).stdout
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        tar.extractall(dest, filter="data")


def _run(argv, cwd):
    try:
        p = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                           timeout=RUN_TIMEOUT_S)
        return Run(p.returncode, (p.stdout + p.stderr)[-4000:])
    except subprocess.TimeoutExpired:
        return Run(-1, f"timed out after {RUN_TIMEOUT_S}s")


def _existing(repo, sha, paths):
    """The subset of `paths` present at `sha` (git archive errors on a missing pathspec)."""
    out = _git(repo, "ls-tree", "--name-only", sha, "--", *paths).stdout.split()
    return [p for p in paths if p.rstrip("/") in out]


def fail_before_pass_after(repo, before_sha, after_sha, argv, test_paths):
    """Returns (label, reason, evidence). label is 'witnessed' or 'refuted'."""
    overlay = _existing(repo, after_sha, test_paths)
    if not overlay:
        return "refuted", "the fix commit contains none of the registered test paths", {}
    with tempfile.TemporaryDirectory() as before_dir, tempfile.TemporaryDirectory() as after_dir:
        _export(repo, before_sha, before_dir)
        _export(repo, after_sha, before_dir, overlay)
        _export(repo, after_sha, after_dir)
        before = _run(argv, before_dir)
        after = _run(argv, after_dir)
    evidence = {
        "before": {"sha": before_sha, "exit": before.returncode, "tail": before.output[-800:]},
        "after": {"sha": after_sha, "exit": after.returncode, "tail": after.output[-800:]},
    }
    if before.returncode == 0:
        return "refuted", "the test passes without the fix, so it proves nothing", evidence
    if after.returncode != 0:
        return "refuted", "the test still fails with the fix", evidence
    return "witnessed", "failed before the fix, passes after it", evidence


def is_repo(path):
    return Path(path).exists() and subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--git-dir"], capture_output=True).returncode == 0
