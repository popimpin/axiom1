"""Where a check's command runs. Verification executes code an agent wrote, so this matters.

  local   runs on the server's own machine. For development only: the agent's code gets the
          server's network, files and credentials. Evidence marks every verdict `isolated: false`.
  docker  runs in a throwaway container: no network, capped memory, CPU and processes, an
          unprivileged user, and only a temporary copy of the tree mounted.

A sandbox runs one command in one tree and reports the exit code and the tail of the output.
"""
import os
import secrets
import shutil
import subprocess
from dataclasses import dataclass

RUN_TIMEOUT_S = 300


@dataclass
class Run:
    returncode: int
    output: str


class LocalSandbox:
    kind, isolated = "local", False

    def __init__(self, timeout=RUN_TIMEOUT_S):
        self.timeout = timeout

    def describe(self):
        return {"kind": self.kind, "isolated": self.isolated}

    def run(self, tree, argv):
        # stdin=DEVNULL: under MCP the server's stdin is the protocol pipe
        try:
            p = subprocess.run(argv, cwd=tree, capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=self.timeout)
            return Run(p.returncode, (p.stdout + p.stderr)[-4000:])
        except subprocess.TimeoutExpired:
            return Run(-1, f"timed out after {self.timeout}s")


class DockerSandbox:
    kind, isolated = "docker", True

    def __init__(self, image, memory="1g", cpus="1", pids=256, timeout=RUN_TIMEOUT_S, docker="docker"):
        self.image, self.memory, self.cpus, self.pids = image, memory, cpus, pids
        self.timeout, self.docker = timeout, docker

    def describe(self):
        return {"kind": self.kind, "isolated": self.isolated, "image": self.image, "network": "none",
                "memory": self.memory, "cpus": self.cpus, "pids": self.pids, "user": "nobody"}

    def command(self, tree, argv, name, writable=False, protect=()):
        """`writable` mounts the tree read-write (an agent's own worktree, for its shell). `protect`
        lists paths inside the tree mounted read-only on top, e.g. a worktree's `.git` file, which
        otherwise could be rewritten to point the host's git at some other repository."""
        tree = os.path.abspath(tree)
        mounts = ["--mount", f"type=bind,src={tree},dst=/work" + ("" if writable else ",readonly")]
        for rel in protect:
            src = os.path.join(tree, rel)
            if os.path.exists(src):
                mounts += ["--mount", f"type=bind,src={src},dst=/work/{rel},readonly"]
        return [self.docker, "run", "--rm", "--name", name,
                "--network", "none",
                "--memory", self.memory, "--memory-swap", self.memory,
                "--cpus", str(self.cpus), "--pids-limit", str(self.pids),
                "--user", "65534:65534",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--read-only", "--tmpfs", "/tmp:rw,size=64m",
                "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "HOME=/tmp",
                *mounts,
                "--workdir", "/work",
                self.image, *argv]

    def run(self, tree, argv, writable=False, protect=()):
        name = f"axiom1-{secrets.token_hex(6)}"
        try:
            p = subprocess.run(self.command(tree, argv, name, writable, protect), capture_output=True,
                               text=True, stdin=subprocess.DEVNULL, timeout=self.timeout)
            return Run(p.returncode, (p.stdout + p.stderr)[-4000:])
        except subprocess.TimeoutExpired:
            subprocess.run([self.docker, "kill", name], capture_output=True, stdin=subprocess.DEVNULL)
            return Run(-1, f"timed out after {self.timeout}s")

    @staticmethod
    def available(docker="docker"):
        if not shutil.which(docker):
            return False
        return subprocess.run([docker, "info"], capture_output=True, stdin=subprocess.DEVNULL,
                              timeout=30).returncode == 0


def from_check(kind, image=None):
    if kind in (None, "", "local"):
        return LocalSandbox()
    if kind == "docker":
        if not image:
            raise ValueError("a docker check needs an image")
        return DockerSandbox(image)
    raise ValueError(f"unknown sandbox {kind!r}")
