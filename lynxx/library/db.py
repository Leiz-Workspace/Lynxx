"""SQLite storage for Lynxx library metadata and reader progress."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

_SCHEMA = """
PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;
PRAGMA busy_timeout = 10000;
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sources (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, base_url TEXT NOT NULL,
  adapter_id TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
  policy_state TEXT NOT NULL DEFAULT 'discovered',
  policy_checked_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS collections (
  id TEXT PRIMARY KEY, source_id TEXT NOT NULL DEFAULT '', remote_id TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL, metadata_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(source_id, remote_id)
);
CREATE TABLE IF NOT EXISTS chapters (
  id TEXT PRIMARY KEY, collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
  remote_id TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, chapter_number REAL,
  content_kind TEXT NOT NULL, state TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  UNIQUE(collection_id, remote_id)
);
CREATE TABLE IF NOT EXISTS pages (
  id TEXT PRIMARY KEY, chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
  page_number INTEGER NOT NULL, source_url TEXT NOT NULL DEFAULT '', local_path TEXT NOT NULL DEFAULT '',
  mime_type TEXT NOT NULL DEFAULT '', sha256 TEXT NOT NULL DEFAULT '', byte_size INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL, UNIQUE(chapter_id, page_number)
);
CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY, chapter_id TEXT NOT NULL REFERENCES chapters(id) ON DELETE CASCADE,
  kind TEXT NOT NULL, path TEXT NOT NULL UNIQUE, mime_type TEXT NOT NULL,
  size INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reader_progress (
  chapter_id TEXT PRIMARY KEY REFERENCES chapters(id) ON DELETE CASCADE,
  page INTEGER NOT NULL DEFAULT 0, scroll_offset REAL NOT NULL DEFAULT 0,
  mode TEXT NOT NULL DEFAULT 'vertical', zoom REAL NOT NULL DEFAULT 1,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_decisions (
  id INTEGER PRIMARY KEY AUTOINCREMENT, source_id TEXT NOT NULL, url_scope TEXT NOT NULL,
  operation TEXT NOT NULL, decision TEXT NOT NULL, reason TEXT NOT NULL,
  policy_version TEXT NOT NULL, checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS pages_chapter_order ON pages(chapter_id, page_number);
CREATE INDEX IF NOT EXISTS chapters_collection_order ON chapters(collection_id, chapter_number);
"""


class LibraryDB:
    """Small, transaction-oriented database facade used by sidecar services."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA busy_timeout = 10000")
        self.migrate()

    def migrate(self) -> None:
        with self.connection:
            self.connection.executescript(_SCHEMA)
            row = self.connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO schema_meta(key, value) VALUES(?, ?)",
                    ("schema_version", str(SCHEMA_VERSION)),
                )
            elif int(row["value"]) > SCHEMA_VERSION:
                raise RuntimeError("Lynxx library database is newer than this app")

    def create_collection(self, collection_id: str, title: str, source_id: str = "", remote_id: str = "", metadata_json: str = "{}", created_at: str = "", updated_at: str = "") -> None:
        self.connection.execute(
            "INSERT INTO collections(id,source_id,remote_id,title,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (collection_id, source_id, remote_id, title, metadata_json, created_at, updated_at),
        )

    def create_chapter(self, chapter_id: str, collection_id: str, title: str, content_kind: str, state: str, remote_id: str = "", chapter_number: float | None = None, created_at: str = "", updated_at: str = "") -> None:
        self.connection.execute(
            "INSERT INTO chapters(id,collection_id,remote_id,title,chapter_number,content_kind,state,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (chapter_id, collection_id, remote_id, title, chapter_number, content_kind, state, created_at, updated_at),
        )

    def create_artifact(self, artifact_id: str, chapter_id: str, kind: str, path: str, mime_type: str, size: int, sha256: str, state: str, created_at: str) -> None:
        self.connection.execute(
            "INSERT INTO artifacts(id,chapter_id,kind,path,mime_type,size,sha256,state,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (artifact_id, chapter_id, kind, path, mime_type, size, sha256, state, created_at),
        )

    def chapter_artifacts(self, chapter_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM artifacts WHERE chapter_id = ? ORDER BY created_at, id", (chapter_id,)
        )]

    def create_page(self, page_id: str, chapter_id: str, page_number: int, local_path: str, mime_type: str, sha256: str, byte_size: int, state: str) -> None:
        self.connection.execute(
            "INSERT INTO pages(id,chapter_id,page_number,local_path,mime_type,sha256,byte_size,state) VALUES(?,?,?,?,?,?,?,?)",
            (page_id, chapter_id, page_number, local_path, mime_type, sha256, byte_size, state),
        )

        self.connection.execute(
            "INSERT INTO policy_decisions(source_id,url_scope,operation,decision,reason,policy_version,checked_at) VALUES(?,?,?,?,?,?,?)",
            (source_id, url_scope, operation, decision, reason, version, checked_at),
        )

    def save_reader_progress(self, chapter_id: str, page: int, scroll_offset: float, mode: str, zoom: float, updated_at: str) -> None:
        if page < 0 or scroll_offset < 0 or zoom <= 0 or mode not in {"vertical", "horizontal"}:
            raise ValueError("invalid reader progress")
        self.connection.execute(
            """INSERT INTO reader_progress(chapter_id,page,scroll_offset,mode,zoom,updated_at) VALUES(?,?,?,?,?,?)
            ON CONFLICT(chapter_id) DO UPDATE SET page=excluded.page,scroll_offset=excluded.scroll_offset,
            mode=excluded.mode,zoom=excluded.zoom,updated_at=excluded.updated_at""",
            (chapter_id, page, scroll_offset, mode, zoom, updated_at),
        )

    def reader_progress(self, chapter_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM reader_progress WHERE chapter_id = ?", (chapter_id,)).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        self.connection.close()
