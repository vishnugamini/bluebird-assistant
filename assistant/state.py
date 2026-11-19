from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import threading
import time


PRIVATE_PREFIX = re.compile(r"^\s*(?:/private\b\s*:?|(?:private|confidential|local.only)\s*:)\s*", re.I)
PRIVATE_MARKER = re.compile(r"(?:" + PRIVATE_PREFIX.pattern + r"|"
                            r"\b(?:this|keep this|mark this)\s+(?:is\s+|as\s+)?(?:private|confidential|local.only)\b)", re.I)


class State:
    """One transaction boundary shared by jobs, approvals, memory, and snapshots."""

    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / "assistant.db"
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS sessions(
                    id TEXT PRIMARY KEY, private INTEGER NOT NULL DEFAULT 0,
                    platform TEXT NOT NULL, sender TEXT NOT NULL,
                    user_message TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS memory(
                    scope TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
                    PRIMARY KEY(scope,key));
                CREATE TABLE IF NOT EXISTS approvals(
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, digest TEXT NOT NULL,
                    session TEXT NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL,
                    gmail_draft TEXT NOT NULL, message_id TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS deliveries(
                    id TEXT PRIMARY KEY, status TEXT NOT NULL, updated REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL,
                    started REAL NOT NULL, finished REAL, detail TEXT);
                CREATE TABLE IF NOT EXISTS flags(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS calls(
                    id TEXT PRIMARY KEY, timestamp REAL NOT NULL, route TEXT NOT NULL,
                    model TEXT NOT NULL, reason TEXT NOT NULL, ok INTEGER NOT NULL,
                    latency REAL NOT NULL, usage TEXT NOT NULL);
            """)
            db.execute("UPDATE memory SET value = 'Bluebird' "
                       "WHERE scope = 'shared' AND key = 'project' AND value = 'Assignment demo'")
            db.execute("INSERT OR IGNORE INTO memory VALUES ('shared','project','Bluebird')")

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            try:
                with db:
                    yield db
            finally:
                db.close()

    def bind(self, session, platform, sender, message):
        if not session:
            raise ValueError("Hermes session identity is required.")
        private = bool(PRIVATE_MARKER.search(message))
        with self.connect() as db:
            db.execute("""INSERT INTO sessions VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                private=MAX(sessions.private,excluded.private), platform=excluded.platform,
                sender=excluded.sender,user_message=excluded.user_message,updated=excluded.updated""",
                       (session, int(private), platform, sender, message[:32000], time.time()))
        return self.session(session)

    def session(self, session):
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (session,)).fetchone()
        if row is None:
            raise PermissionError("Session is unregistered. Start a new Hermes conversation.")
        return dict(row)

    def memories(self, session):
        context = self.session(session)
        scope = f"private:{session}" if context["private"] else "shared"
        with self.connect() as db:
            rows = db.execute("SELECT key,value FROM memory WHERE scope=?", (scope,)).fetchall()
        return dict(rows)

    def remember(self, session, key, value):
        context = self.session(session)
        # A model cannot create its own authorization to write memory.
        message = context["user_message"].lower()
        if not any(word in message for word in ("remember", "save my", "my preference")):
            raise PermissionError("Ask explicitly to remember a synthetic preference first.")
        if not key or len(key) > 100 or not value or len(value) > 2000:
            raise ValueError("Memory requires a short key and a value of at most 2,000 characters.")
        scope = f"private:{session}" if context["private"] else "shared"
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO memory VALUES (?,?,?)", (scope, key, value))

    def flag(self, key):
        with self.connect() as db:
            row = db.execute("SELECT value FROM flags WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_flag(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO flags VALUES (?,?)", (key, str(value)))

    def record_call(self, request_id, route, model, reason, ok, latency, usage):
        with self.connect() as db:
            db.execute("INSERT INTO calls VALUES (?,?,?,?,?,?,?,?)",
                       (request_id, time.time(), route, model, reason, int(ok), latency, json.dumps(usage)))

    def recover_inflight(self):
        with self.connect() as db:
            sends = db.execute("UPDATE approvals SET status='uncertain' WHERE status='sending'").rowcount
            db.execute("UPDATE approvals SET status='invalidated' WHERE status='checking'")
            deliveries = db.execute("UPDATE deliveries SET status='uncertain' WHERE status='sending'").rowcount
            jobs = db.execute("UPDATE jobs SET status='interrupted', finished=? WHERE status='running'",
                              (time.time(),)).rowcount
        return {"uncertainSends": sends, "uncertainDeliveries": deliveries, "interruptedJobs": jobs}
