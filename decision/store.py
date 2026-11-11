"""SQLite persistence. A state transition and its audit event share one transaction."""
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_versions (
    name TEXT NOT NULL, version TEXT NOT NULL, definition TEXT NOT NULL,
    PRIMARY KEY(name, version)
);
CREATE TABLE IF NOT EXISTS active_policies (
    name TEXT PRIMARY KEY, version TEXT NOT NULL,
    FOREIGN KEY(name, version) REFERENCES policy_versions(name, version)
);
CREATE TABLE IF NOT EXISTS requests (
    id TEXT PRIMARY KEY, workflow TEXT NOT NULL, version TEXT NOT NULL,
    idem_key TEXT NOT NULL, payload TEXT NOT NULL, stage TEXT NOT NULL,
    status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0, due_at REAL,
    UNIQUE(workflow, idem_key),
    FOREIGN KEY(workflow, version) REFERENCES policy_versions(name, version)
);
CREATE INDEX IF NOT EXISTS ready_requests ON requests(status, due_at, id);
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(id),
    at REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS request_events ON events(request_id, seq);
"""


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self, write=True):
        self.db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield self.db
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise
