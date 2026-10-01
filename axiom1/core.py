"""Axiom-1: one shared state for many agents.

One rule, applied to both halves of the system:

  * a message is a claim until its recipient acks the exact bytes (sha256);
  * a "done" is a claim until the server itself checks it.

Agents can say things. Only the server can witness them. There is no argument
anywhere in the agent-facing API that sets a label: `remember` and `claim`
always store `declared`, and `witnessed` / `refuted` are written only by
`verify`, which runs a check a human registered.
"""
import hashlib
import json
import sqlite3
import time
import functools
import secrets
import threading
import uuid
from pathlib import Path

from . import sandbox as sandboxes
from . import verifier

DECLARED, WITNESSED, REFUTED = "declared", "witnessed", "refuted"
DEFAULT_LEASE_S = 600

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents   (id TEXT PRIMARY KEY, caps TEXT NOT NULL, joined_at REAL NOT NULL,
                                     token_sha256 TEXT);
CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL,
                                     body TEXT NOT NULL, sha256 TEXT NOT NULL, sent_at REAL NOT NULL,
                                     delivered_at REAL);
CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, agent TEXT NOT NULL,
                                     content TEXT NOT NULL, label TEXT NOT NULL, claim_id TEXT, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS checks   (id TEXT PRIMARY KEY, repo TEXT NOT NULL, argv TEXT NOT NULL,
                                     test_paths TEXT NOT NULL, registered_by TEXT NOT NULL, at REAL NOT NULL,
                                     sandbox TEXT NOT NULL DEFAULT 'local', image TEXT, holdout TEXT,
                                     base_ref TEXT);
CREATE TABLE IF NOT EXISTS tasks    (id TEXT PRIMARY KEY, title TEXT NOT NULL, caps TEXT NOT NULL,
                                     posted_by TEXT NOT NULL, status TEXT NOT NULL, holder TEXT,
                                     lease_until REAL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS claims   (id TEXT PRIMARY KEY, agent TEXT NOT NULL, statement TEXT NOT NULL,
                                     check_id TEXT NOT NULL, before_sha TEXT NOT NULL, after_sha TEXT NOT NULL,
                                     task_id TEXT, label TEXT NOT NULL, reason TEXT, evidence TEXT,
                                     made_at REAL NOT NULL, verified_at REAL, kind TEXT NOT NULL DEFAULT 'fix',
                                     anchored INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS events   (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL,
                                     agent TEXT, subject TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS lessons  (id TEXT PRIMARY KEY, kind TEXT NOT NULL, claim_id TEXT NOT NULL,
                                     check_id TEXT NOT NULL, task_title TEXT, agent TEXT NOT NULL,
                                     label TEXT NOT NULL, reason TEXT NOT NULL, changed TEXT NOT NULL,
                                     body TEXT NOT NULL, at REAL NOT NULL);
"""

# columns added after a table first shipped; applied to older database files on open
MIGRATIONS = [
    ("agents", "token_sha256", "TEXT"),
    ("checks", "sandbox", "TEXT NOT NULL DEFAULT 'local'"),
    ("checks", "image", "TEXT"),
    ("checks", "holdout", "TEXT"),
    ("claims", "kind", "TEXT NOT NULL DEFAULT 'fix'"),
    ("checks", "base_ref", "TEXT"),
    ("claims", "anchored", "INTEGER NOT NULL DEFAULT 1"),
]

# What a claim can assert, how the server checks it, and how a witnessed one is written down. The
# fact records what was PROVEN; the agent's own words follow it and prove nothing by themselves.
CLAIM_KINDS = {
    "fix": ("fixed", verifier.fail_before_pass_after),
    "no_regression": ("no regression", verifier.no_regression),
}


def _locked(method):
    """One agent's call at a time touches the database. The hub runs tool calls on worker threads."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self.lock:
            return method(self, *args, **kwargs)
    return wrapper


class AxiomError(Exception):
    """A request the rules refuse. The message says which rule."""


def _display_command(argv):
    """`C:/.../python.exe -m pytest` -> `python -m pytest`: agents learn the framework, not our disk."""
    import os
    out = [os.path.splitext(os.path.basename(x))[0] if os.path.isabs(x) else x for x in argv]
    return " ".join(out)


def _okf(frontmatter, body):
    """One OKF document: YAML frontmatter, then the markdown body. Values are JSON-quoted strings,
    which YAML reads as plain strings, so agent text cannot break out of the frontmatter."""
    lines = ["---"] + [f"{k}: {json.dumps(v if v is not None else '')}" for k, v in frontmatter.items()]
    return "\n".join(lines + ["---", "", body, ""])


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Axiom:
    """`share_lessons=False` keeps recording lessons but stops handing them to agents (for A/B runs)."""

    def __init__(self, db_path=":memory:", clock=time.time, share_lessons=True):
        self.share_lessons = share_lessons
        # several agents' server processes can share one file: WAL + a busy timeout let them
        self.db = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        if db_path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        for table, column, decl in MIGRATIONS:
            have = {r["name"] for r in self.db.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
        self.clock = clock
        self.lock = threading.RLock()

    # ---- plumbing -------------------------------------------------------
    def _event(self, kind, agent=None, subject=None, **detail):
        self.db.execute("INSERT INTO events (at, kind, agent, subject, detail) VALUES (?,?,?,?,?)",
                        (self.clock(), kind, agent, subject, json.dumps(detail)))

    def _agent(self, agent_id):
        row = self.db.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
        if row is None:
            raise AxiomError(f"unknown agent {agent_id!r}: join first")
        return row

    # ---- membership -----------------------------------------------------
    @_locked
    def join(self, agent_id, caps=()):
        """Join the collective. `caps` are what this agent can do, e.g. ["shell", "gpu"]."""
        with self.db:
            self.db.execute("INSERT INTO agents (id, caps, joined_at) VALUES (?,?,?) "
                            "ON CONFLICT(id) DO UPDATE SET caps=excluded.caps",
                            (agent_id, json.dumps(sorted(set(caps))), self.clock()))
            self._event("join", agent_id, caps=sorted(set(caps)))
        return self.briefing(agent_id)

    # ---- identity for the hub: a token per agent, stored only as a hash ----
    @_locked
    def issue_token(self, agent_id, caps=(), issued_by="operator"):
        """Operator-only. Admits an agent and returns its token, the only time it is ever shown.
        Issuing again replaces the old token, which stops working at once."""
        token = "axm_" + secrets.token_urlsafe(32)
        self.join(agent_id, caps)
        with self.db:
            self.db.execute("UPDATE agents SET token_sha256=? WHERE id=?", (sha256(token), agent_id))
            self._event("issue_token", issued_by, agent_id)
        return token

    @_locked
    def revoke_token(self, agent_id, revoked_by="operator"):
        with self.db:
            self.db.execute("UPDATE agents SET token_sha256=NULL WHERE id=?", (agent_id,))
            self._event("revoke_token", revoked_by, agent_id)

    @_locked
    def agent_for_token(self, token):
        if not token:
            return None
        row = self.db.execute("SELECT id FROM agents WHERE token_sha256=?", (sha256(token),)).fetchone()
        return row["id"] if row else None

    # ---- messages: delivered only when the recipient acks the exact bytes ----
    @_locked
    def send(self, sender, recipient, body):
        self._agent(sender)
        self._agent(recipient)
        msg_id = uuid.uuid4().hex[:12]
        digest = sha256(body)
        with self.db:
            self.db.execute("INSERT INTO messages VALUES (?,?,?,?,?,?,NULL)",
                            (msg_id, sender, recipient, body, digest, self.clock()))
            self._event("send", sender, msg_id, to=recipient)
        return {"id": msg_id, "sha256": digest, "status": "sent"}

    @_locked
    def inbox(self, agent_id):
        self._agent(agent_id)
        rows = self.db.execute("SELECT id, sender, body, sha256 FROM messages "
                               "WHERE recipient=? AND delivered_at IS NULL ORDER BY sent_at",
                               (agent_id,)).fetchall()
        return [dict(r) for r in rows]

    @_locked
    def ack(self, agent_id, msg_id, echoed_sha256):
        row = self.db.execute("SELECT * FROM messages WHERE id=?", (msg_id,)).fetchone()
        if row is None:
            raise AxiomError(f"no message {msg_id!r}")
        if row["recipient"] != agent_id:
            raise AxiomError("only the recipient can ack a message")
        if echoed_sha256 != row["sha256"]:
            raise AxiomError("ack hash does not match the bytes that were sent")
        with self.db:
            self.db.execute("UPDATE messages SET delivered_at=? WHERE id=?", (self.clock(), msg_id))
            self._event("ack", agent_id, msg_id)
        return {"id": msg_id, "status": "delivered"}

    @_locked
    def message_status(self, msg_id):
        row = self.db.execute("SELECT delivered_at FROM messages WHERE id=?", (msg_id,)).fetchone()
        if row is None:
            raise AxiomError(f"no message {msg_id!r}")
        return "delivered" if row["delivered_at"] else "sent"

    # ---- memory: agents can only declare ----------------------------------
    @_locked
    def remember(self, agent_id, key, content):
        self._agent(agent_id)
        with self.db:
            self.db.execute("INSERT INTO memories (key, agent, content, label, at) VALUES (?,?,?,?,?)",
                            (key, agent_id, content, DECLARED, self.clock()))
            self._event("remember", agent_id, key)
        return {"key": key, "label": DECLARED}

    @_locked
    def recall(self, key):
        rows = self.db.execute("SELECT key, agent, content, label, claim_id, at FROM memories "
                               "WHERE key=? ORDER BY id DESC", (key,)).fetchall()
        return [dict(r) for r in rows]

    # ---- operator API: NOT exposed to agents ------------------------------
    @_locked
    def register_check(self, check_id, repo, argv, test_paths, registered_by="operator",
                       sandbox="local", image=None, holdout=None, base=None):
        """A human registers what "verified" means for a repo. Agents cannot add or edit checks.
        `sandbox` is where the command runs: "local" (development only) or "docker" (needs `image`).
        `holdout` is a directory of tests the agents never see, run against every fix. It must live
        OUTSIDE the repo: agents work in worktrees of it and can read everything in its history.
        `base` is the branch holding the collective's accepted code (default: the repo's current
        branch). Only claims that start from a commit on it count toward a track record."""
        if not verifier.is_repo(repo):
            raise AxiomError(f"{repo!r} is not a git repository")
        try:
            base = base or verifier.current_branch(repo)
            verifier.resolve(repo, base)
        except Exception:
            raise AxiomError(f"base branch {base!r} does not resolve in {repo!r}") from None
        try:
            sandboxes.from_check(sandbox, image)
        except ValueError as e:
            raise AxiomError(str(e)) from None
        if holdout is not None:
            held, root = Path(holdout).resolve(), Path(repo).resolve()
            if not held.is_dir():
                raise AxiomError(f"held-out tests {holdout!r} is not a directory")
            if held == root or root in held.parents:
                raise AxiomError("held-out tests must live outside the repo, where agents cannot read them")
            holdout = str(held)
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO checks VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (check_id, str(repo), json.dumps(list(argv)), json.dumps(list(test_paths)),
                             registered_by, self.clock(), sandbox, image, holdout, base))
            self._event("register_check", registered_by, check_id)

    @_locked
    def list_checks(self):
        """What agents may claim against: the command that judges them and where tests must live.
        The repo path stays server-side, and the command is shown without absolute paths."""
        return [{"id": r["id"], "command": _display_command(json.loads(r["argv"])),
                 "test_paths": json.loads(r["test_paths"]),
                 "sandbox": sandboxes.from_check(r["sandbox"], r["image"]).describe(),
                 "holdout": r["holdout"] is not None,
                 "base": r["base_ref"],
                 "note": "a test path ending in / is a directory; put new test files inside it"
                         + ("; this check also runs held-out tests you cannot see, so fix the behaviour "
                            "in general, not just the case your test checks" if r["holdout"] else "")
                         + f"; only claims whose before_ref is on {r['base_ref']!r} count toward your record"}
                for r in self.db.execute("SELECT id, argv, test_paths, sandbox, image, holdout, base_ref "
                                         "FROM checks ORDER BY id")]

    # ---- tasks: posted to the collective, taken by capability, held by lease ----
    @_locked
    def post_task(self, agent_id, title, caps=()):
        self._agent(agent_id)
        task_id = uuid.uuid4().hex[:12]
        with self.db:
            self.db.execute("INSERT INTO tasks VALUES (?,?,?,?,'open',NULL,NULL,?)",
                            (task_id, title, json.dumps(sorted(set(caps))), agent_id, self.clock()))
            self._event("post_task", agent_id, task_id, title=title)
        return {"id": task_id, "status": "open"}

    def _expire_leases(self):
        now = self.clock()
        expired = self.db.execute("SELECT id, holder FROM tasks WHERE status='leased' AND lease_until<?",
                                  (now,)).fetchall()
        with self.db:
            for t in expired:
                self.db.execute("UPDATE tasks SET status='open', holder=NULL, lease_until=NULL WHERE id=?",
                                (t["id"],))
                self._event("lease_expired", t["holder"], t["id"])
                self._write_abandoned(t["id"], t["holder"], "the lease expired without a verified claim", None)

    def _write_abandoned(self, task_id, agent_id, why, note):
        """An attempt that ended without a verified claim still teaches something. The server records
        only what it observed (who, how long, which claims); the agent's own account, if it gave one,
        is quoted and labelled declared: unverified, like everything an agent says."""
        task = self.db.execute("SELECT title FROM tasks WHERE id=?", (task_id,)).fetchone()
        took = self.db.execute("SELECT at FROM events WHERE kind='take_task' AND agent=? AND subject=? "
                               "ORDER BY id DESC LIMIT 1", (agent_id, task_id)).fetchone()
        held = f"{self.clock() - took['at']:.0f}s" if took else "an unknown time"
        claims = [r["label"] for r in self.db.execute(
            "SELECT label FROM claims WHERE agent=? AND task_id=? ORDER BY made_at", (agent_id, task_id))]
        body = (f"{agent_id} held this task for {held} and stopped: {why}. "
                f"Its claims on it: {', '.join(claims) or 'none'}.")
        if note:
            body += f" Its own account (declared, unverified): {' '.join(str(note).split())[:600]}"
        self.db.execute("INSERT INTO lessons VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex[:12], "abandoned", "", "", task["title"] if task else None,
                         agent_id, "abandoned", why, "[]", body, self.clock()))

    @_locked
    def release_task(self, agent_id, task_id, note=""):
        """Give a task back without a verified claim. Honest, so not counted as an expired lease; the
        attempt still leaves a lesson for whoever takes the task next."""
        t = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if t is None or t["status"] != "leased" or t["holder"] != agent_id:
            raise AxiomError("only the current lease holder can release a task")
        with self.db:
            self.db.execute("UPDATE tasks SET status='open', holder=NULL, lease_until=NULL WHERE id=?",
                            (task_id,))
            self._event("release_task", agent_id, task_id)
            self._write_abandoned(task_id, agent_id, "released without a verified claim", note)
        return {"id": task_id, "status": "open"}

    @_locked
    def take_task(self, agent_id, lease_s=DEFAULT_LEASE_S):
        """Lease the oldest open task this agent is capable of, or None."""
        caps = set(json.loads(self._agent(agent_id)["caps"]))
        self._expire_leases()
        for t in self.db.execute("SELECT * FROM tasks WHERE status='open' ORDER BY at").fetchall():
            if set(json.loads(t["caps"])) <= caps:
                with self.db:
                    self.db.execute("UPDATE tasks SET status='leased', holder=?, lease_until=? WHERE id=?",
                                    (agent_id, self.clock() + lease_s, t["id"]))
                    self._event("take_task", agent_id, t["id"])
                return {"id": t["id"], "title": t["title"], "lease_s": lease_s,
                        "lessons": self._lessons_for(t["title"])}
        return None

    # ---- lessons: what the collective learned, written by the server from its own verdicts ----
    def _lessons_for(self, task_title, limit=5):
        """Earlier attempts at a task with this title, newest first: what failed and why, what worked.
        Empty when sharing is off. Agents cannot write these; they come only from verdicts."""
        if not self.share_lessons:
            return []
        rows = self.db.execute("SELECT kind, label, reason, changed, body, agent FROM lessons "
                               "WHERE task_title=? ORDER BY at DESC LIMIT ?", (task_title, limit)).fetchall()
        return [{"kind": r["kind"], "label": r["label"], "by": r["agent"], "reason": r["reason"],
                 "changed": json.loads(r["changed"]), "lesson": r["body"]} for r in rows]

    def _write_lesson(self, c, check, label, reason, evidence, anchored):
        """A refutation becomes a lesson, an anchored witness becomes a skill. Unanchored wins teach
        nothing (they fixed code nobody had), so they are not written."""
        if label == WITNESSED and not anchored:
            return
        try:
            changed = verifier.changed_files(check["repo"], c["before_sha"], c["after_sha"])
        except Exception:
            changed = []
        tests = [f for f in changed if any(f == p.rstrip("/") or f.startswith(p.rstrip("/") + "/")
                                           for p in json.loads(check["test_paths"]))]
        task = self.db.execute("SELECT title FROM tasks WHERE id=?", (c["task_id"],)).fetchone() \
            if c["task_id"] else None
        if label == WITNESSED:
            kind = "skill"
            body = (f"Worked ({CLAIM_KINDS[c['kind']][0]}): {c['statement']}. Changed {', '.join(changed) or 'nothing'}"
                    f"; tests {', '.join(tests) or 'none'}. Verified: {reason}.")
        else:
            kind = "lesson"
            body = f"Tried: {c['statement']}. Changed {', '.join(changed) or 'nothing'}. Refuted: {reason}."
            if not tests:
                body += " The commit contained no test under the check's test path."
            tail = (evidence.get("after") or {}).get("tail", "")
            if "still fails" in reason and tail:   # the agent's OWN test output; never held-out output
                body += " Last output of the agent's own tests: " + " ".join(tail.split())[-300:]
        self.db.execute("INSERT INTO lessons VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (uuid.uuid4().hex[:12], kind, c["id"], c["check_id"], task["title"] if task else None,
                         c["agent"], label, reason, json.dumps(changed), body, self.clock()))

    @_locked
    def lessons(self, limit=50):
        return [dict(r) for r in self.db.execute("SELECT * FROM lessons ORDER BY at DESC LIMIT ?", (limit,))]

    @_locked
    def export_okf(self, directory):
        """Write the collective's record as an OKF bundle: markdown + YAML frontmatter + [[links]],
        one file per lesson or skill and per witnessed fact, plus an index. Git-native and portable."""
        root = Path(directory)
        for sub in ("lessons", "skills", "facts"):
            (root / sub).mkdir(parents=True, exist_ok=True)
        index = {"lessons": [], "skills": [], "facts": []}
        for r in self.db.execute("SELECT * FROM lessons ORDER BY at"):
            sub = "skills" if r["kind"] == "skill" else "lessons"
            name = f"{r['kind']}-{r['id']}"
            fm = {"name": name, "type": r["kind"], "label": r["label"], "check": r["check_id"],
                  "task": r["task_title"], "agent": r["agent"], "claim": r["claim_id"],
                  "written_by": "axiom1-server", "description": r["reason"]}
            (root / sub / f"{name}.md").write_text(_okf(fm, r["body"] + f"\n\nFrom claim [[claim-{r['claim_id']}]]."),
                                                   encoding="utf-8")
            index[sub].append((name, r["task_title"] or "(no task)", r["reason"]))
        for r in self.db.execute("SELECT key, agent, content, claim_id FROM memories WHERE label=? ORDER BY id",
                                 (WITNESSED,)):
            name = f"claim-{r['claim_id']}"
            fm = {"name": name, "type": "fact", "label": WITNESSED, "agent": r["agent"],
                  "written_by": "axiom1-server", "description": r["content"]}
            (root / "facts" / f"{name}.md").write_text(_okf(fm, r["content"]), encoding="utf-8")
            index["facts"].append((name, "", r["content"]))
        lines = ["# Axiom-1 knowledge bundle", "",
                 "Everything here was written by the server from its own verdicts. Nothing an agent merely "
                 "said appears in it.", ""]
        for sub in ("facts", "skills", "lessons"):
            lines += [f"## {sub.title()} ({len(index[sub])})", ""]
            lines += [f"- [[{n}]] {t + ': ' if t else ''}{d}" for n, t, d in index[sub]] + [""]
        (root / "index.md").write_text("\n".join(lines), encoding="utf-8")
        return {"directory": str(root), **{k: len(v) for k, v in index.items()}}

    # ---- claims: declared until the server checks them --------------------
    @_locked
    def claim(self, agent_id, statement, check_id, before_ref, after_ref, task_id=None, kind="fix"):
        """`kind` is what the claim asserts: "fix" (a test that failed before passes after) or
        "no_regression" (a change that broke nothing: the old tests still pass on the new code)."""
        self._agent(agent_id)
        if kind not in CLAIM_KINDS:
            raise AxiomError(f"unknown claim kind {kind!r}; use one of {sorted(CLAIM_KINDS)}")
        check = self.db.execute("SELECT * FROM checks WHERE id=?", (check_id,)).fetchone()
        if check is None:
            raise AxiomError(f"no registered check {check_id!r}")
        if task_id is not None:
            self._expire_leases()
            t = self.db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if t is None or t["status"] != "leased" or t["holder"] != agent_id:
                raise AxiomError("only the current lease holder can claim a task")
        try:
            before_sha = verifier.resolve(check["repo"], before_ref)
            after_sha = verifier.resolve(check["repo"], after_ref)
        except Exception as e:
            raise AxiomError(f"could not resolve refs: {e}") from None
        if before_sha == after_sha:
            raise AxiomError("before and after are the same commit")
        # anchored: the claim starts from code the collective already had. A fix to a bug the agent
        # planted in its own unmerged commit is true, and worth nothing.
        anchored = verifier.is_ancestor(check["repo"], before_sha, check["base_ref"])
        claim_id = uuid.uuid4().hex[:12]
        with self.db:
            self.db.execute("INSERT INTO claims (id, agent, statement, check_id, before_sha, after_sha, "
                            "task_id, label, made_at, kind, anchored) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            (claim_id, agent_id, statement, check_id, before_sha, after_sha,
                             task_id, DECLARED, self.clock(), kind, int(anchored)))
            self._event("claim", agent_id, claim_id, statement=statement, claim_kind=kind, anchored=anchored)
        return {"id": claim_id, "label": DECLARED, "kind": kind, "before": before_sha, "after": after_sha,
                "counts": anchored}

    def verify(self, claim_id):
        with self.lock:
            c = self.db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
            if c is None:
                raise AxiomError(f"no claim {claim_id!r}")
            if c["label"] != DECLARED:
                return {"id": claim_id, "label": c["label"], "reason": c["reason"],
                        "counts": bool(c["anchored"])}
            check = self.db.execute("SELECT * FROM checks WHERE id=?", (c["check_id"],)).fetchone()
        # the slow part runs unlocked, so one agent's verification does not stall the others
        _, check_fn = CLAIM_KINDS[c["kind"]]
        label, reason, evidence, private = check_fn(
            check["repo"], c["before_sha"], c["after_sha"],
            json.loads(check["argv"]), json.loads(check["test_paths"]),
            sandboxes.from_check(check["sandbox"], check["image"]), check["holdout"])
        with self.lock:
            return self._record_verdict(c, claim_id, label, reason, evidence, private, check)

    def _record_verdict(self, c, claim_id, label, reason, evidence, private, check):
        with self.db:
            # another process may have verified this claim while ours ran; the first verdict stands
            won = self.db.execute("UPDATE claims SET label=?, reason=?, evidence=?, verified_at=? "
                                  "WHERE id=? AND label=?",
                                  (label, reason, json.dumps(evidence), self.clock(), claim_id, DECLARED))
            if won.rowcount == 0:
                c = self.db.execute("SELECT label, reason, anchored FROM claims WHERE id=?",
                                    (claim_id,)).fetchone()
                return {"id": claim_id, "label": c["label"], "reason": c["reason"],
                        "counts": bool(c["anchored"])}
            anchored = bool(c["anchored"])
            if label == WITNESSED and not anchored:
                reason += (" (does not count: before_ref is not on the base branch, so this fixes "
                           "code the collective never had)")
                self.db.execute("UPDATE claims SET reason=? WHERE id=?", (reason, claim_id))
            if label == WITNESSED and anchored:
                self.db.execute("INSERT INTO memories (key, agent, content, label, claim_id, at) "
                                "VALUES (?,?,?,?,?,?)",
                                (f"claim:{claim_id}", c["agent"],
                                 f"[{CLAIM_KINDS[c['kind']][0]}] {c['statement']}", WITNESSED,
                                 claim_id, self.clock()))
            if c["task_id"]:
                status = "done" if label == WITNESSED and anchored else "open"
                self.db.execute("UPDATE tasks SET status=?, holder=CASE WHEN ?='done' THEN holder END, "
                                "lease_until=NULL WHERE id=?", (status, status, c["task_id"]))
            self._event(label, c["agent"], claim_id, reason=reason)
            self._write_lesson(c, check, label, reason, evidence, anchored)
            if "holdout_tail" in private:  # operator-only: the event log is not on the agent surface
                self._event("holdout_failed", c["agent"], claim_id, tail=private["holdout_tail"])
        return {"id": claim_id, "label": label, "reason": reason, "evidence": evidence, "counts": anchored}

    # ---- track record: honesty and reliability are separate ----------------
    @_locked
    def track_record(self, agent_id):
        """Witnessed counts only anchored claims (farming your own bugs earns nothing). Refuted counts
        every claim: a false statement is false wherever it started."""
        rows = self.db.execute("SELECT label, anchored, COUNT(*) AS n FROM claims WHERE agent=? "
                               "GROUP BY label, anchored", (agent_id,)).fetchall()
        n = lambda label, anchored=None: sum(r["n"] for r in rows if r["label"] == label  # noqa: E731
                                             and (anchored is None or r["anchored"] == anchored))
        events = lambda kind: self.db.execute("SELECT COUNT(*) FROM events WHERE kind=? AND agent=?",  # noqa: E731
                                              (kind, agent_id)).fetchone()[0]
        return {"witnessed": n(WITNESSED, 1), "refuted": n(REFUTED), "pending": n(DECLARED),
                "unanchored": n(WITNESSED, 0), "leases_expired": events("lease_expired"),
                "released": events("release_task")}

    # ---- briefing: what a joining agent needs, not the transcript ----------
    @_locked
    def briefing(self, agent_id, limit=20):
        self._agent(agent_id)
        self._expire_leases()
        q = self.db.execute
        facts = [dict(r) for r in q("SELECT key, content, agent FROM memories WHERE label=? "
                                    "ORDER BY id DESC LIMIT ?", (WITNESSED, limit))]
        open_claims = [dict(r) for r in q("SELECT id, agent, statement FROM claims WHERE label=? "
                                          "ORDER BY made_at DESC LIMIT ?", (DECLARED, limit))]
        refuted = [dict(r) for r in q("SELECT id, agent, statement, reason FROM claims WHERE label=? "
                                      "ORDER BY verified_at DESC LIMIT ?", (REFUTED, limit))]
        caps = set(json.loads(self._agent(agent_id)["caps"]))
        tasks = [{"id": t["id"], "title": t["title"]}
                 for t in q("SELECT * FROM tasks WHERE status='open' ORDER BY at")
                 if set(json.loads(t["caps"])) <= caps][:limit]
        agents = {r["id"]: self.track_record(r["id"]) for r in q("SELECT id FROM agents")}
        recent = ([{"kind": r["kind"], "task": r["task_title"], "lesson": r["body"]}
                   for r in q("SELECT kind, task_title, body FROM lessons ORDER BY at DESC LIMIT 5")]
                  if self.share_lessons else [])
        return {"you": agent_id, "facts": facts, "open_claims": open_claims, "refuted": refuted,
                "tasks_you_can_take": tasks, "agents": agents, "recent_lessons": recent,
                "unread": len(self.inbox(agent_id))}

    @_locked
    def events(self, since_id=0):
        return [dict(r) for r in self.db.execute("SELECT * FROM events WHERE id>? ORDER BY id", (since_id,))]
