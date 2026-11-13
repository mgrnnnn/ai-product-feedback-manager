import contextlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from uuid import uuid4


def now():
    return datetime.now(timezone.utc).isoformat()


def uid():
    return uuid4().hex


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


SCHEMA = '''
CREATE TABLE IF NOT EXISTS schema_version(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES(1);
CREATE TABLE IF NOT EXISTS users(
 id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL,
 products TEXT NOT NULL, password TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
 token TEXT PRIMARY KEY, user_id TEXT REFERENCES users(id), csrf TEXT NOT NULL,
 expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources(
 id TEXT PRIMARY KEY, version INTEGER NOT NULL, product TEXT NOT NULL,
 channel TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 acl TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
 id TEXT PRIMARY KEY, source_id TEXT REFERENCES sources(id), version INTEGER NOT NULL,
 pipeline TEXT NOT NULL, payload TEXT, status TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, available_at TEXT NOT NULL,
 locked_at TEXT, error TEXT, correlation_id TEXT NOT NULL,
 UNIQUE(source_id,version,pipeline));
CREATE TABLE IF NOT EXISTS evidence(
 id TEXT PRIMARY KEY, source_id TEXT REFERENCES sources(id), version INTEGER NOT NULL,
 extract_hash TEXT NOT NULL, text_redacted TEXT NOT NULL, model_version TEXT NOT NULL,
 quality_flags TEXT NOT NULL, extraction TEXT NOT NULL, active INTEGER NOT NULL,
 created_at TEXT NOT NULL, UNIQUE(source_id,version));
CREATE TABLE IF NOT EXISTS topics(
 id TEXT PRIMARY KEY, product TEXT NOT NULL, type TEXT NOT NULL, title TEXT NOT NULL,
 user_need TEXT NOT NULL, scenario TEXT, draft_task TEXT NOT NULL,
 uncertainties TEXT NOT NULL, acl TEXT NOT NULL, first_seen TEXT, last_seen TEXT,
 status TEXT NOT NULL DEFAULT 'review', owner_id TEXT, revision INTEGER NOT NULL DEFAULT 1,
 approved_revision INTEGER, approved_by TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS topic_evidence(
 topic_id TEXT REFERENCES topics(id), evidence_id TEXT REFERENCES evidence(id),
 confidence REAL NOT NULL, decision TEXT NOT NULL, matched_at TEXT NOT NULL,
 PRIMARY KEY(topic_id,evidence_id));
CREATE TABLE IF NOT EXISTS reviews(
 id TEXT PRIMARY KEY, topic_id TEXT REFERENCES topics(id), revision INTEGER NOT NULL,
 actor_id TEXT NOT NULL, action TEXT NOT NULL, timestamp TEXT NOT NULL, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS backlog(
 topic_id TEXT PRIMARY KEY REFERENCES topics(id), tracker_id TEXT NOT NULL,
 external_issue_id TEXT, sync_status TEXT NOT NULL, last_synced_at TEXT,
 idempotency_key TEXT NOT NULL UNIQUE, approved_revision INTEGER NOT NULL,
 approved_by TEXT NOT NULL, error TEXT);
CREATE TABLE IF NOT EXISTS local_issues(
 id TEXT PRIMARY KEY, idempotency_key TEXT UNIQUE NOT NULL, title TEXT NOT NULL,
 description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open');
CREATE TABLE IF NOT EXISTS audit(
 id TEXT PRIMARY KEY, actor_id TEXT NOT NULL, action TEXT NOT NULL,
 object_id TEXT NOT NULL, timestamp TEXT NOT NULL, correlation_id TEXT NOT NULL,
 details TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS checkpoints(name TEXT PRIMARY KEY, watermark TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS digests(
 actor_id TEXT NOT NULL, topic_id TEXT NOT NULL, sent_at TEXT NOT NULL,
 PRIMARY KEY(actor_id,topic_id));
CREATE INDEX IF NOT EXISTS job_queue ON jobs(status,available_at);
CREATE INDEX IF NOT EXISTS evidence_source ON evidence(source_id,active);
CREATE INDEX IF NOT EXISTS topics_product ON topics(product,status);
'''


class Database:
    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            conn.execute('PRAGMA journal_mode=WAL')
        os.chmod(path, 0o600)

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        conn.execute('PRAGMA secure_delete=ON')
        return conn

    @contextlib.contextmanager
    def transaction(self):
        conn = self.connect()
        try:
            conn.execute('BEGIN IMMEDIATE')
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def audit(conn, actor, action, object_id, correlation=None, details=None):
    conn.execute('INSERT INTO audit VALUES(?,?,?,?,?,?,?)',
                 (uid(), actor, action, object_id, now(), correlation or uid(), dumps(details or {})))
