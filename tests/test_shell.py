"""The agent's shell: useful inside its worktree, and nowhere else.

The worktree is writable, so an agent can run tests and leave scratch files. Its `.git` FILE is the
one thing inside that must not change: it tells the host's git where the repository lives, and the
host runs `commit` for the agent. Rewritten, it would aim that commit at another repository.
"""
import subprocess
import tempfile
import unittest
from pathlib import Path

from axiom1.agent import Workspace
from axiom1.sandbox import DockerSandbox
from test_core import BUGGY, REAL_TEST, Repo

IMAGE = "python:3.13-slim"
DOCKER = DockerSandbox.available() and subprocess.run(
    ["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0


class NoShellWithoutASandbox(unittest.TestCase):
    def test_no_sandbox_means_no_run_tool(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            ws = Workspace(Repo(Path(tmp) / "repo").root)
            self.assertNotIn("run", [t["name"] for t in ws.tools()])
            with self.assertRaises(ValueError):
                ws.run("echo hi")

    def test_the_git_file_is_mounted_read_only_on_the_command_line(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            repo = Repo(Path(tmp) / "repo")
            wt = Path(tmp) / "wt"
            repo.git("worktree", "add", "-q", "--detach", str(wt), repo.base)
            cmd = " ".join(DockerSandbox(IMAGE).command(str(wt), ["sh"], "x", writable=True, protect=(".git",)))
            self.assertIn("dst=/work/.git,readonly", cmd)
            self.assertIn(",dst=/work ", cmd + " ")   # the worktree itself is writable
            self.assertIn("--network none", cmd)


@unittest.skipUnless(DOCKER, f"docker daemon or {IMAGE} not available")
class ShellInDocker(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.repo = Repo(Path(self.tmp.name) / "repo")
        self.wt = Path(self.tmp.name) / "wt"
        self.repo.git("worktree", "add", "-q", "--detach", str(self.wt), self.repo.base)
        self.ws = Workspace(self.wt, DockerSandbox(IMAGE))

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_agent_can_run_its_tests(self):
        self.ws.write_file("tests/test_calc.py", REAL_TEST)
        r = self.ws.run("python -m unittest discover -s tests")
        self.assertNotEqual(r["exit"], 0)                     # add() is still broken
        self.assertIn("FAILED", r["output"])

    def test_the_agent_can_write_in_its_worktree(self):
        r = self.ws.run("echo scratch > notes.txt && cat notes.txt")
        self.assertEqual(r["exit"], 0, r["output"])
        self.assertEqual((self.wt / "notes.txt").read_text().strip(), "scratch")

    def test_the_git_file_cannot_be_rewritten(self):
        before = (self.wt / ".git").read_text()
        r = self.ws.run("echo 'gitdir: /tmp/elsewhere' > .git")
        self.assertNotEqual(r["exit"], 0)
        self.assertEqual((self.wt / ".git").read_text(), before)
        # and the host-side commit still lands in the real repository
        sha = self.ws.commit("still the right repo")["sha"]
        self.assertEqual(self.repo.git("cat-file", "-t", sha), "commit")

    def test_no_network_and_nothing_outside(self):
        secret = Path(self.tmp.name) / "host_secret.txt"
        secret.write_text("S3CRET")
        r = self.ws.run("python -c \"import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)\"")
        self.assertNotEqual(r["exit"], 0)
        r = self.ws.run(f"cat {secret.as_posix()} ; ls /work/..")
        self.assertNotIn("S3CRET", r["output"])
        self.assertNotIn("repo", r["output"])                 # the main repo is not mounted


class Control(unittest.TestCase):
    """Without the read-only overlay, the same shell CAN rewrite `.git`: the protection is load-bearing."""

    @unittest.skipUnless(DOCKER, f"docker daemon or {IMAGE} not available")
    def test_unprotected_mount_lets_git_be_rewritten(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            repo = Repo(Path(tmp) / "repo")
            wt = Path(tmp) / "wt"
            repo.git("worktree", "add", "-q", "--detach", str(wt), repo.base)
            r = DockerSandbox(IMAGE).run(str(wt), ["sh", "-c", "echo 'gitdir: /tmp/elsewhere' > .git"],
                                         writable=True, protect=())
            self.assertEqual(r.returncode, 0, r.output)
            self.assertIn("/tmp/elsewhere", (wt / ".git").read_text())


if __name__ == "__main__":
    unittest.main()
