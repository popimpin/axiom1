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
import uuid

from . import verifier

DECLARED, WITNESSED, REFUTED = "declared", "witnessed", "refuted"
DEFAULT_LEASE_S = 600

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents   (id TEXT PRIMARY KEY, caps TEXT NOT NULL, joined_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS messages (id TEXT PRIMARY KEY, sender TEXT NOT NULL, recipient TEXT NOT NULL,
                                     body TEXT NOT NULL, sha256 TEXT NOT NULL, sent_at REAL NOT NULL,
                                     delivered_at REAL);
CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, agent TEXT NOT NULL,
                                     content TEXT NOT NULL, label TEXT NOT NULL, claim_id TEXT, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS checks   (id TEXT PRIMARY KEY, repo TEXT NOT NULL, argv TEXT NOT NULL,
                                     test_paths TEXT NOT NULL, registered_by TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tasks    (id TEXT PRIMARY KEY, title TEXT NOT NULL, caps TEXT NOT NULL,
                                     posted_by TEXT NOT NULL, status TEXT NOT NULL, holder TEXT,
                                     lease_until REAL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS claims   (id TEXT PRIMARY KEY, agent TEXT NOT NULL, statement TEXT NOT NULL,
                                     check_id TEXT NOT NULL, before_sha TEXT NOT NULL, after_sha TEXT NOT NULL,
                                     task_id TEXT, label TEXT NOT NULL, reason TEXT, evidence TEXT,
                                     made_at REAL NOT NULL, verified_at REAL);
CREATE TABLE IF NOT EXISTS events   (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, kind TEXT NOT NULL,
                                     agent TEXT, subject TEXT, detail TEXT);
"""


class AxiomError(Exception):
    """A request the rules refuse. The message says which rule."""


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Axiom:
    def __init__(self, db_path=":memory:", clock=time.time):
        # several agents' server processes can share one file: WAL + a busy timeout let them
        self.db = sqlite3.connect(db_path, check_same_thread=False, timeout=30)
        self.db.row_factory = sqlite3.Row
        if db_path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.clock = clock

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
    def join(self, agent_id, caps=()):
        """Join the collective. `caps` are what this agent can do, e.g. ["shell", "gpu"]."""
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO agents VALUES (?,?,?)",
                            (agent_id, json.dumps(sorted(set(caps))), self.clock()))
            self._event("join", agent_id, caps=sorted(set(caps)))
        return self.briefing(agent_id)

    # ---- messages: delivered only when the recipient acks the exact bytes ----
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

    def inbox(self, agent_id):
        self._agent(agent_id)
        rows = self.db.execute("SELECT id, sender, body, sha256 FROM messages "
                               "WHERE recipient=? AND delivered_at IS NULL ORDER BY sent_at",
                               (agent_id,)).fetchall()
        return [dict(r) for r in rows]

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

    def message_status(self, msg_id):
        row = self.db.execute("SELECT delivered_at FROM messages WHERE id=?", (msg_id,)).fetchone()
        if row is None:
            raise AxiomError(f"no message {msg_id!r}")
        return "delivered" if row["delivered_at"] else "sent"

    # ---- memory: agents can only declare ----------------------------------
    def remember(self, agent_id, key, content):
        self._agent(agent_id)
        with self.db:
            self.db.execute("INSERT INTO memories (key, agent, content, label, at) VALUES (?,?,?,?,?)",
                            (key, agent_id, content, DECLARED, self.clock()))
            self._event("remember", agent_id, key)
        return {"key": key, "label": DECLARED}

    def recall(self, key):
        rows = self.db.execute("SELECT key, agent, content, label, claim_id, at FROM memories "
                               "WHERE key=? ORDER BY id DESC", (key,)).fetchall()
        return [dict(r) for r in rows]

    # ---- operator API: NOT exposed to agents ------------------------------
    def register_check(self, check_id, repo, argv, test_paths, registered_by="operator"):
        """A human registers what "verified" means for a repo. Agents cannot add or edit checks."""
        if not verifier.is_repo(repo):
            raise AxiomError(f"{repo!r} is not a git repository")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO checks VALUES (?,?,?,?,?,?)",
                            (check_id, str(repo), json.dumps(list(argv)), json.dumps(list(test_paths)),
                             registered_by, self.clock()))
            self._event("register_check", registered_by, check_id)

    def list_checks(self):
        """What agents may claim against. The command and repo path stay server-side."""
        return [{"id": r["id"], "test_paths": json.loads(r["test_paths"])}
                for r in self.db.execute("SELECT id, test_paths FROM checks ORDER BY id")]

    # ---- tasks: posted to the collective, taken by capability, held by lease ----
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
                return {"id": t["id"], "title": t["title"], "lease_s": lease_s}
        return None

    # ---- claims: declared until the server checks them --------------------
    def claim(self, agent_id, statement, check_id, before_ref, after_ref, task_id=None):
        self._agent(agent_id)
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
        claim_id = uuid.uuid4().hex[:12]
        with self.db:
            self.db.execute("INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,NULL,NULL,?,NULL)",
                            (claim_id, agent_id, statement, check_id, before_sha, after_sha,
                             task_id, DECLARED, self.clock()))
            self._event("claim", agent_id, claim_id, statement=statement)
        return {"id": claim_id, "label": DECLARED, "before": before_sha, "after": after_sha}

    def verify(self, claim_id):
        c = self.db.execute("SELECT * FROM claims WHERE id=?", (claim_id,)).fetchone()
        if c is None:
            raise AxiomError(f"no claim {claim_id!r}")
        if c["label"] != DECLARED:
            return {"id": claim_id, "label": c["label"], "reason": c["reason"]}
        check = self.db.execute("SELECT * FROM checks WHERE id=?", (c["check_id"],)).fetchone()
        label, reason, evidence = verifier.fail_before_pass_after(
            check["repo"], c["before_sha"], c["after_sha"],
            json.loads(check["argv"]), json.loads(check["test_paths"]))
        with self.db:
            # another process may have verified this claim while ours ran; the first verdict stands
            won = self.db.execute("UPDATE claims SET label=?, reason=?, evidence=?, verified_at=? "
                                  "WHERE id=? AND label=?",
                                  (label, reason, json.dumps(evidence), self.clock(), claim_id, DECLARED))
            if won.rowcount == 0:
                c = self.db.execute("SELECT label, reason FROM claims WHERE id=?", (claim_id,)).fetchone()
                return {"id": claim_id, "label": c["label"], "reason": c["reason"]}
            if label == WITNESSED:
                self.db.execute("INSERT INTO memories (key, agent, content, label, claim_id, at) "
                                "VALUES (?,?,?,?,?,?)",
                                (f"claim:{claim_id}", c["agent"], c["statement"], WITNESSED,
                                 claim_id, self.clock()))
            if c["task_id"]:
                status = "done" if label == WITNESSED else "open"
                self.db.execute("UPDATE tasks SET status=?, holder=CASE WHEN ?='done' THEN holder END, "
                                "lease_until=NULL WHERE id=?", (status, status, c["task_id"]))
            self._event(label, c["agent"], claim_id, reason=reason)
        return {"id": claim_id, "label": label, "reason": reason, "evidence": evidence}

    # ---- track record: honesty and reliability are separate ----------------
    def track_record(self, agent_id):
        counts = dict(self.db.execute("SELECT label, COUNT(*) FROM claims WHERE agent=? GROUP BY label",
                                      (agent_id,)).fetchall())
        expired = self.db.execute("SELECT COUNT(*) FROM events WHERE kind='lease_expired' AND agent=?",
                                  (agent_id,)).fetchone()[0]
        return {"witnessed": counts.get(WITNESSED, 0), "refuted": counts.get(REFUTED, 0),
                "pending": counts.get(DECLARED, 0), "leases_expired": expired}

    # ---- briefing: what a joining agent needs, not the transcript ----------
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
        return {"you": agent_id, "facts": facts, "open_claims": open_claims, "refuted": refuted,
                "tasks_you_can_take": tasks, "agents": agents, "unread": len(self.inbox(agent_id))}

    def events(self, since_id=0):
        return [dict(r) for r in self.db.execute("SELECT * FROM events WHERE id>? ORDER BY id", (since_id,))]
