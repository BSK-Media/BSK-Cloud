"""Baza SQLite — metadane kont, plików, wiadomości i kopii zapasowych."""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'client')),
    display_name TEXT NOT NULL DEFAULT '',
    company TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    session_version INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_login TEXT
);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES users(id),
    rel_path TEXT NOT NULL,
    original_name TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    uploaded_by INTEGER NOT NULL REFERENCES users(id),
    uploaded_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_files_client ON files(client_id, uploaded_at);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    client_id INTEGER NOT NULL REFERENCES users(id),
    author_id INTEGER NOT NULL REFERENCES users(id),
    body TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_client ON messages(client_id, created_at);
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE COLLATE NOCASE,
    employee TEXT NOT NULL DEFAULT '',
    folder_name TEXT NOT NULL UNIQUE,
    token_hash TEXT NOT NULL UNIQUE,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    last_seen TEXT,
    last_ip TEXT
);
CREATE TABLE IF NOT EXISTS backup_files (
    id INTEGER PRIMARY KEY,
    device_id INTEGER NOT NULL REFERENCES devices(id),
    rel_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    mtime REAL NOT NULL DEFAULT 0,
    uploaded_at TEXT NOT NULL,
    versions INTEGER NOT NULL DEFAULT 1,
    UNIQUE (device_id, rel_path)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    user_id INTEGER,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS folders (
    id INTEGER PRIMARY KEY,
    owner_id INTEGER NOT NULL REFERENCES users(id),
    parent_id INTEGER REFERENCES folders(id),
    name TEXT NOT NULL,
    created_by INTEGER,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_folders_owner ON folders(owner_id, parent_id);
CREATE TABLE IF NOT EXISTS file_versions (
    id INTEGER PRIMARY KEY,
    file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    uploaded_by INTEGER,
    uploaded_at TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS seen (
    user_id INTEGER NOT NULL REFERENCES users(id),
    client_id INTEGER NOT NULL REFERENCES users(id),
    seen_at TEXT NOT NULL,
    PRIMARY KEY (user_id, client_id)
);
CREATE TABLE IF NOT EXISTS share_links (
    id INTEGER PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'shared')),
    file_id INTEGER REFERENCES files(id) ON DELETE CASCADE,
    rel_path TEXT NOT NULL DEFAULT '',
    name TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL DEFAULT '',
    expires_at TEXT,
    max_downloads INTEGER NOT NULL DEFAULT 0,
    downloads INTEGER NOT NULL DEFAULT 0,
    revoked INTEGER NOT NULL DEFAULT 0,
    created_by INTEGER NOT NULL REFERENCES users(id),
    created_at TEXT NOT NULL,
    last_download TEXT
);
CREATE TABLE IF NOT EXISTS install_links (
    id INTEGER PRIMARY KEY,
    code TEXT NOT NULL UNIQUE,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    token TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    downloads INTEGER NOT NULL DEFAULT 0
);
"""

# Kolumny dodane po pierwszej wersji — dokładane do istniejącej bazy przy starcie.
MIGRATIONS = [
    ("users", "quota_mb", "INTEGER NOT NULL DEFAULT 0"),
    ("devices", "quota_mb", "INTEGER NOT NULL DEFAULT 0"),
    ("devices", "folders", "TEXT NOT NULL DEFAULT ''"),
    ("users", "notify_email", "INTEGER NOT NULL DEFAULT 1"),
    ("files", "folder_id", "INTEGER"),
    ("files", "deleted_at", "TEXT"),
    ("files", "deleted_by", "INTEGER"),
    ("files", "trash_path", "TEXT"),
    ("files", "version", "INTEGER NOT NULL DEFAULT 1"),
    ("files", "office_version", "INTEGER NOT NULL DEFAULT 1"),
    ("files", "modified_at", "TEXT"),
    ("files", "modified_by", "INTEGER"),
]


def now_iso() -> str:
    # z mikrosekundami — kolejność zdarzeń w tej samej sekundzie (np. „przeczytane” vs nowa wiadomość)
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def iso_in(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        c = self.conn()
        c.executescript(SCHEMA)
        for table, column, ddl in MIGRATIONS:
            cols = {r["name"] for r in c.execute(f"PRAGMA table_info({table})")}
            if column not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=30, isolation_level=None)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            self._local.conn = c
        return c

    def one(self, sql: str, *args):
        return self.conn().execute(sql, args).fetchone()

    def all(self, sql: str, *args):
        return self.conn().execute(sql, args).fetchall()

    def run(self, sql: str, *args) -> int:
        cur = self.conn().execute(sql, args)
        return cur.lastrowid

    def log(self, user_id: int | None, action: str, detail: str = "") -> None:
        self.run(
            "INSERT INTO events (ts, user_id, action, detail) VALUES (?, ?, ?, ?)",
            now_iso(), user_id, action, detail[:1000],
        )
