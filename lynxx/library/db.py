"""SQLite storage for Lynxx library metadata and reader progress."""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 2

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

# v2: collection-centric library (extension downloads, converted documents, exports that can be refreshed)
_MIGRATION_V2 = (
    "ALTER TABLE collections ADD COLUMN kind TEXT NOT NULL DEFAULT 'document'",
    "ALTER TABLE collections ADD COLUMN source_url TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE collections ADD COLUMN cover_path TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE chapters ADD COLUMN source_url TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE chapters ADD COLUMN position INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE chapters ADD COLUMN content_path TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE chapters ADD COLUMN page_count INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE chapters ADD COLUMN locked INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE chapters ADD COLUMN error TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE reader_progress ADD COLUMN direction TEXT NOT NULL DEFAULT 'ltr'",
    """CREATE TABLE IF NOT EXISTS exports (
      id TEXT PRIMARY KEY, collection_id TEXT NOT NULL REFERENCES collections(id) ON DELETE CASCADE,
      format TEXT NOT NULL, path TEXT NOT NULL, options_json TEXT NOT NULL DEFAULT '{}',
      chapter_count INTEGER NOT NULL DEFAULT 0, chapters_json TEXT NOT NULL DEFAULT '[]',
      size INTEGER NOT NULL DEFAULT 0, sha256 TEXT NOT NULL DEFAULT '', auto_update INTEGER NOT NULL DEFAULT 0,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(collection_id, path)
    )""",
    """CREATE TABLE IF NOT EXISTS collection_progress (
      collection_id TEXT PRIMARY KEY REFERENCES collections(id) ON DELETE CASCADE,
      chapter_id TEXT NOT NULL, updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS chapters_collection_position ON chapters(collection_id, position)",
)

READER_MODES = {"vertical", "horizontal", "paged"}
READER_DIRECTIONS = {"ltr", "rtl"}

# the sidecar answers requests on several threads, each opening its own connection; switching a new file to WAL (an exclusive
# lock, refused at once instead of waited for) and upgrading the schema are done by one thread at a time
_OPEN_LOCK = threading.Lock()


class LibraryDB:
    """Small, transaction-oriented database facade used by sidecar services."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        try:
            with _OPEN_LOCK:
                self.connection.execute("PRAGMA foreign_keys = ON")
                self.connection.execute("PRAGMA busy_timeout = 10000")
                self.connection.execute("PRAGMA journal_mode = WAL")
                self.migrate()
        except BaseException:
            self.connection.close()
            raise

    def __enter__(self) -> "LibraryDB":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _schema_version(self) -> int:
        row = self.connection.execute("SELECT value FROM schema_meta WHERE key = 'schema_version'").fetchone()
        version = int(row["value"]) if row else 0
        if version > SCHEMA_VERSION:
            raise RuntimeError("Lynxx library database is newer than this app")
        return version

    def migrate(self) -> None:
        self.connection.executescript(_SCHEMA)
        if self._schema_version() == SCHEMA_VERSION:
            return
        # another process (the CLI) may upgrade the same file: the upgrade runs under the write lock and looks again
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if self._schema_version() < 2:
                columns = {
                    table: {r["name"] for r in self.connection.execute(f"PRAGMA table_info({table})")}
                    for table in ("collections", "chapters", "reader_progress")
                }
                for statement in _MIGRATION_V2:
                    if statement.startswith("ALTER TABLE"):
                        table, column = statement.split()[2], statement.split()[5]
                        if column in columns[table]:
                            continue
                    self.connection.execute(statement)
            self.connection.execute(
                "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    # ------------------------------------------------------------------ rows

    def create_collection(self, collection_id: str, title: str, source_id: str = "", remote_id: str = "", metadata_json: str = "{}",
                          created_at: str = "", updated_at: str = "", kind: str = "document", source_url: str = "", cover_path: str = "") -> None:
        self.connection.execute(
            "INSERT INTO collections(id,source_id,remote_id,title,metadata_json,created_at,updated_at,kind,source_url,cover_path) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (collection_id, source_id, remote_id, title, metadata_json, created_at, updated_at, kind, source_url, cover_path),
        )

    def create_chapter(self, chapter_id: str, collection_id: str, title: str, content_kind: str, state: str, remote_id: str = "",
                       chapter_number: float | None = None, created_at: str = "", updated_at: str = "", source_url: str = "",
                       position: int = 0, content_path: str = "", page_count: int = 0, locked: bool = False) -> None:
        self.connection.execute(
            "INSERT INTO chapters(id,collection_id,remote_id,title,chapter_number,content_kind,state,created_at,updated_at,source_url,position,content_path,page_count,locked)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (chapter_id, collection_id, remote_id, title, chapter_number, content_kind, state, created_at, updated_at, source_url,
             position, content_path, page_count, 1 if locked else 0),
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

    def create_page(self, page_id: str, chapter_id: str, page_number: int, local_path: str, mime_type: str, sha256: str,
                    byte_size: int, state: str, source_url: str = "") -> None:
        self.connection.execute(
            "INSERT INTO pages(id,chapter_id,page_number,local_path,mime_type,sha256,byte_size,state,source_url) VALUES(?,?,?,?,?,?,?,?,?)",
            (page_id, chapter_id, page_number, local_path, mime_type, sha256, byte_size, state, source_url),
        )

    def record_policy_decision(self, source_id: str, url_scope: str, operation: str, decision: str, reason: str, version: str, checked_at: str) -> None:
        self.connection.execute(
            "INSERT INTO policy_decisions(source_id,url_scope,operation,decision,reason,policy_version,checked_at) VALUES(?,?,?,?,?,?,?)",
            (source_id, url_scope, operation, decision, reason, version, checked_at),
        )

    # ------------------------------------------------------------------ queries

    def collection(self, collection_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM collections WHERE id = ?", (collection_id,)).fetchone()
        return dict(row) if row else None

    def chapter(self, chapter_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
        return dict(row) if row else None

    def chapters(self, collection_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM chapters WHERE collection_id = ? ORDER BY position, created_at, id", (collection_id,)
        )]

    def pages(self, chapter_id: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT * FROM pages WHERE chapter_id = ? ORDER BY page_number", (chapter_id,)
        )]

    # ------------------------------------------------------------------ progress

    def save_reader_progress(self, chapter_id: str, page: int, scroll_offset: float, mode: str, zoom: float, updated_at: str, direction: str = "ltr") -> None:
        if page < 0 or scroll_offset < 0 or zoom <= 0 or mode not in READER_MODES or direction not in READER_DIRECTIONS:
            raise ValueError("invalid reader progress")
        self.connection.execute(
            """INSERT INTO reader_progress(chapter_id,page,scroll_offset,mode,zoom,updated_at,direction) VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(chapter_id) DO UPDATE SET page=excluded.page,scroll_offset=excluded.scroll_offset,
            mode=excluded.mode,zoom=excluded.zoom,updated_at=excluded.updated_at,direction=excluded.direction""",
            (chapter_id, page, scroll_offset, mode, zoom, updated_at, direction),
        )
        row = self.connection.execute("SELECT collection_id FROM chapters WHERE id = ?", (chapter_id,)).fetchone()
        if row:
            self.connection.execute(
                """INSERT INTO collection_progress(collection_id,chapter_id,updated_at) VALUES(?,?,?)
                ON CONFLICT(collection_id) DO UPDATE SET chapter_id=excluded.chapter_id,updated_at=excluded.updated_at""",
                (row["collection_id"], chapter_id, updated_at),
            )

    def reader_progress(self, chapter_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM reader_progress WHERE chapter_id = ?", (chapter_id,)).fetchone()
        return dict(row) if row else None

    def collection_progress(self, collection_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM collection_progress WHERE collection_id = ?", (collection_id,)).fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        self.connection.close()
