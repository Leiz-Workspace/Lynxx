# coding=UTF-8
"""
Lynxx: local index of downloaded files, <data dir>/index/downloads.sqlite
- one row per file that finished downloading: module, post id, md5, path, tags, score, rating, post date, download time
- metadata only: files are never opened or hashed, the md5 is the one reported by the website
- written by a background thread in batches (download threads only enqueue, see IndexWriter), WAL journal: several CLI
  processes may write at the same time (writers wait for each other, BUSY_TIMEOUT), the desktop sidecar reads meanwhile
- read by '-skip_known' (posts whose md5 or module + id is known and whose file is still on disk are not downloaded again)
  and by the sidecar methods 'index.search' / 'index.stats'
- LYNXX_DLINDEX=0 disables recording (reading still works)
- maintenance (lynxx/dlindex_tools.py): clean_missing() drops rows whose file is gone (rows only, never files), the library
  import adds files already on disk, rename_paths() follows files renamed by the re-tag / sort tools
- trash (the "Đã tải" page of the desktop app): trash() renames files into a trash folder on the same drive (never copied, see
  trash_folders), restore() puts them back, purge_trash() deletes them for good, forget() drops rows whose file is gone. A trashed row
  keeps its metadata: path is where the file is now (in the trash), orig_path where it was, trashed_at when. Readers that mean "files
  of the library" (find_known, search, stats, rows_for_posts, posts_after) leave trashed rows out: a post whose file is in the trash
  or was purged can be downloaded again. A move writes where the file goes (moving_to) before the rename: recover_moves() finishes
  or drops the moves a stopped process left half done
"""
#########################################
#
#

from __future__ import annotations

import contextlib
import datetime
import errno
import json
import os
import pathlib
import queue
import re
import sqlite3
import stat
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field

from .defines import FMT_DATE, ItemInfo
from .logger import trace

__all__ = (
    'ENV_DLINDEX', 'CleanResult', 'DownloadIndex', 'IndexRecord', 'IndexWriter', 'file_present', 'in_trash_folder', 'index_path',
    'indexing_enabled', 'norm_date', 'norm_md5', 'parse_search_terms', 'record_for', 'rename_indexed_files',
)

ENV_DLINDEX = 'LYNXX_DLINDEX'
'''0: finished downloads are not recorded (the index stays readable, -skip_known keeps working)'''
INDEX_DIR_NAME = 'index'
INDEX_FILE_NAME = 'downloads.sqlite'
SCHEMA_VERSION = 3
BUSY_TIMEOUT = 10.0
'''seconds a connection waits while another process holds the write lock'''
WRITE_ATTEMPTS = 3
'''a batch is retried this many times when the database stays locked longer than BUSY_TIMEOUT, then dropped'''
RETRY_PAUSE = 0.05
'''seconds between two tries when SQLite reports a lock without waiting (see DownloadIndex._enable_wal)'''
BATCH_MAX = 200
FLUSH_INTERVAL = 1.5
'''seconds a record may wait in the writer queue before its batch is written'''
CLOSE_TIMEOUT = 45.0
SQL_CHUNK = 400
'''bound parameters per IN (...) list (old SQLite builds allow 999 per statement)'''
TAG_CACHE_MAX = 200_000
SEARCH_PAGE_SIZE_MAX = 200
SEARCH_TERMS_MAX = 24
TAG_MAX_CHAR = '\U0010ffff'
'''upper bound of a prefix range: name >= prefix AND name < prefix + this'''
CLEAN_BATCH = 500
'''rows checked on disk per step of clean_missing(): each step deletes in its own short transaction'''
TRASH_DIR_NAME = 'trash'
'''<data dir>/trash: the trash of files on the same drive as the data dir'''
TRASH_HIDDEN_NAME = '.lynxx-trash'
'''trash folder on another drive: at the root of the download folder, else next to the file (the library import skips dot folders)'''
TRASH_BATCH = 200
'''files moved per write transaction of trash() / restore()'''
TRASH_NAME_TRIES = 1000
TRASH_UNREACHABLE = 'the trash folder is not reachable (drive unplugged?)'
DAY_SECONDS = 86400

re_md5 = re.compile(r'[0-9a-f]{32}')
re_int = re.compile(r'[-+]?\d+')
re_rating_attr = re.compile(r'\brating["\']?\s*[=:]\s*["\']?([A-Za-z]+)')
RATINGS = {
    'g': 'g', 'general': 'g', 's': 's', 'safe': 's', 'sensitive': 's', 'q': 'q', 'questionable': 'q', 'e': 'e', 'explicit': 'e',
}
'''website ratings -> one letter (danbooru 's' is 'sensitive', gelbooru 's' is 'safe': kept as reported)'''

SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY,
        module TEXT NOT NULL,
        post_id INTEGER NOT NULL,
        md5 TEXT,
        path TEXT NOT NULL UNIQUE,
        ext TEXT NOT NULL DEFAULT '',
        size INTEGER NOT NULL DEFAULT 0,
        tags TEXT NOT NULL DEFAULT '',
        score INTEGER,
        rating TEXT NOT NULL DEFAULT '',
        post_date TEXT NOT NULL DEFAULT '',
        downloaded_at REAL NOT NULL
    )''',
    'CREATE INDEX IF NOT EXISTS files_module_post ON files(module, post_id)',
    'CREATE INDEX IF NOT EXISTS files_md5 ON files(md5)',
    'CREATE INDEX IF NOT EXISTS files_post_date ON files(post_date)',
    'CREATE INDEX IF NOT EXISTS files_downloaded_at ON files(downloaded_at)',
    'CREATE TABLE IF NOT EXISTS tag_names (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE)',
    '''CREATE TABLE IF NOT EXISTS file_tags (
        tag_id INTEGER NOT NULL,
        file_id INTEGER NOT NULL,
        PRIMARY KEY (tag_id, file_id)
    ) WITHOUT ROWID''',
    'CREATE INDEX IF NOT EXISTS file_tags_file ON file_tags(file_id)',
)
'''schema 1. post_id has INTEGER affinity: numeric ids are stored (and compared) as numbers'''
MIGRATIONS = {
    2: (
        'ALTER TABLE files ADD COLUMN trashed_at REAL',
        'ALTER TABLE files ADD COLUMN orig_path TEXT',
        'CREATE INDEX IF NOT EXISTS files_trashed_at ON files(trashed_at) WHERE trashed_at IS NOT NULL',
    ),
    3: (
        'ALTER TABLE files ADD COLUMN moving_to TEXT',
        'CREATE INDEX IF NOT EXISTS files_moving_to ON files(moving_to) WHERE moving_to IS NOT NULL',
    ),
}
'''schema N-1 -> N. A new index runs them after SCHEMA too, so both ways give the same tables. Rows already there are untouched
(trashed_at NULL: not in the trash; moving_to NULL: no move under way)'''

UPSERT_FILE = (
    'INSERT INTO files (module, post_id, md5, path, ext, size, tags, score, rating, post_date, downloaded_at) '
    'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) '
    'ON CONFLICT(path) DO UPDATE SET module = excluded.module, post_id = excluded.post_id, md5 = excluded.md5, ext = excluded.ext, '
    'size = excluded.size, tags = excluded.tags, score = excluded.score, rating = excluded.rating, post_date = excluded.post_date, '
    'downloaded_at = excluded.downloaded_at, trashed_at = NULL, orig_path = NULL'
)
'''one row per file path: downloading a post again into the same file refreshes its row (a file written there is not in the trash)'''

ROW_COLUMNS = ('f.module, f.post_id, f.md5, f.path, f.ext, f.size, f.tags, f.score, f.rating, f.post_date, f.downloaded_at, '
               'f.trashed_at, f.orig_path')
SEARCH_ORDERS = {
    'downloaded': 'f.downloaded_at DESC, f.id DESC',
    'downloaded_asc': 'f.downloaded_at ASC, f.id ASC',
    'date': 'f.post_date DESC, f.downloaded_at DESC',
    'date_asc': "f.post_date = '' ASC, f.post_date ASC, f.downloaded_at ASC",
    'score': 'f.score IS NULL ASC, f.score DESC, f.downloaded_at DESC',
    'id': 'f.module ASC, f.post_id DESC',
    'trashed': 'f.trashed_at DESC, f.id DESC',
}
TAG_FILES_EQ = 'SELECT file_id FROM file_tags WHERE tag_id = (SELECT id FROM tag_names WHERE name = ?)'
TAG_FILES_PREFIX = 'SELECT file_id FROM file_tags WHERE tag_id IN (SELECT id FROM tag_names WHERE name >= ? AND name < ?)'


def index_path() -> pathlib.Path:
    from .extensions import data_dir  # local import: keeps this module free of the extensions loader at import time
    return data_dir() / INDEX_DIR_NAME / INDEX_FILE_NAME


def indexing_enabled() -> bool:
    return os.environ.get(ENV_DLINDEX, '1').strip() != '0'


# ----- normalization -----

def norm_md5(value: str) -> str:
    v = str(value or '').strip().lower()
    return v if re_md5.fullmatch(v) else ''


def norm_date(value: str) -> str:
    """Core 'DD-MM-YYYY' or ISO 'YYYY-MM-DD[...]' -> 'YYYY-MM-DD' ('' if not a date)"""
    v = str(value or '').strip()[:10]
    if not v:
        return ''
    with contextlib.suppress(ValueError):
        return datetime.datetime.strptime(v, FMT_DATE).date().isoformat()
    with contextlib.suppress(ValueError):
        return datetime.date.fromisoformat(v).isoformat()
    return ''


def norm_score(value: str | int | None) -> int | None:
    if isinstance(value, int):
        return value
    m = re_int.search(str(value or ''))
    return int(m.group()) if m else None


def norm_rating(value: str) -> str:
    return RATINGS.get(str(value or '').strip().lower(), '')


def rating_of(raw: str, tags: str) -> str:
    """Rating from the raw item string (rating="e", "rating":"e") or a 'rating:x' tag, '' if unknown"""
    if raw and (m := re_rating_attr.search(raw)) and (r := norm_rating(m.group(1))):
        return r
    for t in str(tags or '').split():
        if t.startswith('rating:') and (r := norm_rating(t[7:])):
            return r
    return ''


def norm_tags(tags: str) -> list[str]:
    return list(dict.fromkeys(t for t in str(tags or '').lower().split() if t))


def _db_post_id(post_id: str | int) -> str | int:
    s = str(post_id).strip()
    return int(s) if s.isdigit() else s


def _chunks(seq: Sequence, n: int) -> Iterator[Sequence]:
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _qs(n: int) -> str:
    return ','.join('?' * n)


def _is_busy(err: sqlite3.OperationalError) -> bool:
    return 'locked' in str(err) or 'busy' in str(err)


def file_present(path: str) -> bool:
    """The indexed file is still on disk (regular, not empty). A stat only, the file is not opened"""
    try:
        st = os.stat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode) and st.st_size > 0


def _device(path: str) -> int | None:
    """Device of path, or of its nearest existing parent (a trash folder not created yet). None if nothing of it exists"""
    p = os.path.abspath(path)
    while True:
        with contextlib.suppress(OSError):
            return os.stat(p).st_dev
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent


def _within(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.normcase(path), os.path.normcase(root)]) == os.path.normcase(root)
    except ValueError:  # Windows: another drive
        return False


def in_trash_folder(path: str) -> bool:
    """path is a file directly inside a trash folder (see DownloadIndex.trash_folders): purge_trash() deletes no other file"""
    return os.path.basename(os.path.dirname(path)) in (TRASH_DIR_NAME, TRASH_HIDDEN_NAME)


def _os_error(err: OSError) -> str:
    return f'{type(err).__name__}: {err.strerror or err}'


def _rename_first(src: str, targets: Sequence[str]) -> tuple[str, str]:
    """Renames src to the first of targets that takes it: (target, ''), or ('', why). Never over an existing file"""
    why = ''
    for target in targets:
        try:
            if os.path.lexists(target):
                raise FileExistsError(errno.EEXIST, 'the name was taken meanwhile', target)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            os.replace(src, target)
            return target, ''
        except OSError as err:  # EXDEV: another drive after all, the next folder is nearer to the file
            if not why or err.errno != errno.EXDEV:
                why = _os_error(err)
    return '', why or 'no trash folder on the drive of the file'


@dataclass(slots=True)
class IndexRecord:
    """A finished file. post_date: 'YYYY-MM-DD' or ''"""
    module: str
    post_id: str
    path: str
    md5: str = ''
    tags: str = ''
    score: int | None = None
    rating: str = ''
    post_date: str = ''
    ext: str = ''
    size: int = 0
    downloaded_at: float = field(default_factory=time.time)


@dataclass(slots=True)
class CleanResult:
    """clean_missing() counts. missing: rows whose file is gone; missing_dirs: those of them whose folder is gone too (an unplugged
    drive looks the same: kept unless asked); removed: rows deleted (0 for a dry run)"""
    total: int = 0
    checked: int = 0
    missing: int = 0
    missing_dirs: int = 0
    removed: int = 0
    dry_run: bool = False
    include_missing_dirs: bool = False
    cancelled: bool = False
    folders: list[str] = field(default_factory=list)
    '''missing folders (at most CLEAN_FOLDERS_MAX), for the report'''

    def as_dict(self) -> dict:
        return {'total': self.total, 'checked': self.checked, 'missing': self.missing, 'missingDirs': self.missing_dirs,
                'removed': self.removed, 'dryRun': self.dry_run, 'missingFolders': self.include_missing_dirs, 'cancelled': self.cancelled,
                'folders': list(self.folders)}


CLEAN_FOLDERS_MAX = 50


def record_for(module: str, info: ItemInfo, post_id: str, dest: str | os.PathLike, size: int, raw: str = '') -> IndexRecord:
    """IndexRecord of a downloaded file from the post info collected during the scan (no extra request, the file is not read)"""
    path = os.path.abspath(os.fspath(dest))
    ext = os.path.splitext(path)[1][1:].lower() or str(info.ext or '').lower()
    return IndexRecord(module=module, post_id=str(info.id or post_id), path=path, md5=norm_md5(info.md5),
                       tags=' '.join(norm_tags(info.tags)), score=norm_score(info.score), rating=rating_of(raw, info.tags),
                       post_date=norm_date(info.date), ext=ext, size=int(size))


def parse_search_terms(text: str | Sequence[str]) -> list[tuple[bool, str, str]]:
    """'tag -tag pre* rating:e md5:<hash> id:<n>' -> [(negated, kind, value)], kind: tag | prefix | rating | md5 | id.
    Unusable terms are dropped, at most SEARCH_TERMS_MAX are kept"""
    words = text.split() if isinstance(text, str) else [w for x in text for w in str(x).split()]
    terms: list[tuple[bool, str, str]] = []
    for word in words:
        w = word.strip().lower()
        neg = w.startswith('-')
        w = w.lstrip('-')
        if not w or w == '*':
            continue
        name, _, value = w.partition(':')
        if name == 'rating' and (r := norm_rating(value)):
            terms.append((neg, 'rating', r))
        elif name == 'md5' and (m := norm_md5(value)):
            terms.append((neg, 'md5', m))
        elif name == 'id' and value.isdigit():
            terms.append((neg, 'id', value))
        elif w.endswith('*') and '*' not in w[:-1]:
            terms.append((neg, 'prefix', w[:-1]))
        elif '*' not in w:
            terms.append((neg, 'tag', w))
        if len(terms) >= SEARCH_TERMS_MAX:
            break
    return terms


class DownloadIndex:
    """A connection to the index (one thread at a time). Readers never create the file: an index that does not exist yet is
    empty. Writers (create=True) create it and its schema. trash_dir: the trash on the drive of the data dir (default: next to
    the index folder, <data dir>/trash)"""

    def __init__(self, path: str | os.PathLike | None = None, *, create: bool = False, trash_dir: str | os.PathLike | None = None) -> None:
        self.path = pathlib.Path(path) if path else index_path()
        self.trash_dir = pathlib.Path(trash_dir) if trash_dir else self.path.parent.parent / TRASH_DIR_NAME
        self._create = create
        self._conn: sqlite3.Connection | None = None
        self._tag_ids: dict[str, int] = {}

    def __enter__(self) -> DownloadIndex:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    def _connection(self) -> sqlite3.Connection | None:
        if self._conn is None:
            if not self._create and not self.path.is_file():
                return None
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT, isolation_level=None, check_same_thread=False)
            try:
                conn.execute(f'PRAGMA busy_timeout = {int(BUSY_TIMEOUT * 1000):d}')
                self._enable_wal(conn)
                conn.execute('PRAGMA synchronous = NORMAL')
                self._ensure_schema(conn)
            except BaseException:
                conn.close()
                raise
            self._conn = conn
        return self._conn

    @staticmethod
    def _enable_wal(conn: sqlite3.Connection) -> None:
        """While other processes open the same new file, SQLite answers "database is locked" at once instead of waiting
        BUSY_TIMEOUT (waiting could deadlock), so the switch is tried again until then. WAL is stored in the file: later opens
        find it set and switch nothing"""
        deadline = time.monotonic() + BUSY_TIMEOUT
        while True:
            try:
                if str(conn.execute('PRAGMA journal_mode').fetchone()[0]).lower() != 'wal':
                    conn.execute('PRAGMA journal_mode = WAL')
                return
            except sqlite3.OperationalError as err:
                if not _is_busy(err) or time.monotonic() >= deadline:
                    raise
            time.sleep(RETRY_PAUSE)

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        if conn.execute('PRAGMA user_version').fetchone()[0] >= SCHEMA_VERSION:
            return
        conn.execute('BEGIN IMMEDIATE')  # another process may be creating or migrating it right now
        try:
            version = conn.execute('PRAGMA user_version').fetchone()[0]
            if version < SCHEMA_VERSION:
                if version < 1:
                    for statement in SCHEMA:
                        conn.execute(statement)
                for step in range(max(version, 1) + 1, SCHEMA_VERSION + 1):
                    for statement in MIGRATIONS[step]:
                        conn.execute(statement)
                conn.execute(f'PRAGMA user_version = {SCHEMA_VERSION:d}')
            conn.execute('COMMIT')
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute('ROLLBACK')
            raise

    # ----- write -----

    def _tag_ids_for(self, conn: sqlite3.Connection, names: set[str]) -> dict[str, int]:
        if len(self._tag_ids) + len(names) > TAG_CACHE_MAX:
            self._tag_ids.clear()
        missing = [n for n in names if n not in self._tag_ids]
        if missing:
            conn.executemany('INSERT OR IGNORE INTO tag_names (name) VALUES (?)', ((n,) for n in missing))
            for chunk in _chunks(missing, SQL_CHUNK):
                for tag_id, name in conn.execute(f'SELECT id, name FROM tag_names WHERE name IN ({_qs(len(chunk))})', chunk):
                    self._tag_ids[name] = tag_id
        return {n: self._tag_ids[n] for n in names}

    def add_many(self, records: Sequence[IndexRecord]) -> int:
        """Writes records in one transaction (upsert by file path), returns their number"""
        if not records:
            return 0
        conn = self._connection()
        if conn is None:
            raise FileNotFoundError(f'download index does not exist: {self.path}')
        conn.execute('BEGIN IMMEDIATE')
        try:
            tag_ids = self._tag_ids_for(conn, {t for r in records for t in r.tags.split()})
            for r in records:
                conn.execute(UPSERT_FILE, (r.module, _db_post_id(r.post_id), norm_md5(r.md5) or None, r.path, r.ext, int(r.size),
                                           r.tags, r.score, r.rating, r.post_date, float(r.downloaded_at)))
                file_id = conn.execute('SELECT id FROM files WHERE path = ?', (r.path,)).fetchone()[0]
                conn.execute('DELETE FROM file_tags WHERE file_id = ?', (file_id,))
                conn.executemany('INSERT OR IGNORE INTO file_tags (tag_id, file_id) VALUES (?, ?)',
                                 ((tag_ids[t], file_id) for t in set(r.tags.split())))
            conn.execute('COMMIT')
        except BaseException:
            self._tag_ids.clear()  # ids cached by a rolled back transaction do not exist
            with contextlib.suppress(sqlite3.Error):
                conn.execute('ROLLBACK')
            raise
        return len(records)

    # ----- read -----

    def find_known(self, module: str, post_ids: Iterable[str] = (), md5s: Iterable[str] = ()) -> tuple[dict[str, str], dict[str, str]]:
        """({post id: path} of this module's posts, {md5: path} of any module) whose indexed file is still on disk. Files in the
        trash do not count (deleted by the user: the post may be downloaded again)"""
        by_id: dict[str, str] = {}
        by_md5: dict[str, str] = {}
        conn = self._connection()
        if conn is None:
            return by_id, by_md5
        ids = list(dict.fromkeys(str(x).strip() for x in post_ids if str(x).strip()))
        for chunk in _chunks(ids, SQL_CHUNK):
            sql = f'SELECT post_id, path FROM files WHERE module = ? AND post_id IN ({_qs(len(chunk))}) AND trashed_at IS NULL'
            for post_id, path in conn.execute(sql, (module, *(_db_post_id(x) for x in chunk))):
                if str(post_id) not in by_id and file_present(path):
                    by_id[str(post_id)] = path
        hashes = list(dict.fromkeys(h for h in (norm_md5(x) for x in md5s) if h))
        for chunk in _chunks(hashes, SQL_CHUNK):
            for md5, path in conn.execute(f'SELECT md5, path FROM files WHERE md5 IN ({_qs(len(chunk))}) AND trashed_at IS NULL', chunk):
                if md5 not in by_md5 and file_present(path):
                    by_md5[md5] = path
        return by_id, by_md5

    @staticmethod
    def _search_filter(terms: list[tuple[bool, str, str]], module: str, date_from: str, date_to: str,
                       trash: bool = False) -> tuple[str, list]:
        clauses: list[str] = ['f.trashed_at IS NOT NULL' if trash else 'f.trashed_at IS NULL']
        params: list = []
        if module:
            clauses.append('f.module = ?')
            params.append(module)
        if d := norm_date(date_from):
            clauses.append('f.post_date >= ?')
            params.append(d)
        if d := norm_date(date_to):
            clauses.append("f.post_date <> '' AND f.post_date <= ?")
            params.append(d)
        for neg, kind, value in terms:
            if kind == 'rating':
                clause, args = 'f.rating = ?', [value]
            elif kind == 'md5':
                clause, args = 'f.md5 = ?', [value]
            elif kind == 'id':
                clause, args = 'f.post_id = ?', [_db_post_id(value)]
            elif kind == 'prefix':
                clause, args = f'f.id IN ({TAG_FILES_PREFIX})', [value, f'{value}{TAG_MAX_CHAR}']
            else:
                clause, args = f'f.id IN ({TAG_FILES_EQ})', [value]
            clauses.append(f'NOT ({clause})' if neg else clause)
            params.extend(args)
        return ' AND '.join(clauses) or '1', params

    @staticmethod
    def _row(r: tuple) -> dict:
        module, post_id, md5, path, ext, size, tags, score, rating, post_date, downloaded_at, trashed_at, orig_path = r
        return {'module': module, 'id': str(post_id), 'md5': md5 or '', 'path': path, 'name': os.path.basename(orig_path or path),
                'ext': ext, 'size': size, 'tags': tags, 'score': score, 'rating': rating, 'date': post_date, 'downloadedAt': downloaded_at,
                'exists': file_present(path), 'trashedAt': trashed_at, 'origPath': orig_path or ''}

    def search(self, tags: str | Sequence[str] = '', *, module: str = '', date_from: str = '', date_to: str = '', page: int = 0,
               page_size: int = 50, order: str = 'downloaded', trash: bool = False) -> dict:
        """Files matching all terms (see parse_search_terms), module, post date range (inclusive). Pages start at 0.
        trash: the files in the trash instead of the library (path: in the trash, origPath: where the file was)"""
        page_size = max(1, min(SEARCH_PAGE_SIZE_MAX, int(page_size or 50)))
        page = max(0, int(page or 0))
        terms = parse_search_terms(tags)
        res = {'total': 0, 'page': page, 'pageSize': page_size, 'items': [], 'terms': [f'{"-" if n else ""}{k}:{v}' for n, k, v in terms]}
        conn = self._connection()
        if conn is None:
            return res
        where, params = self._search_filter(terms, module, date_from, date_to, trash)
        res['total'] = conn.execute(f'SELECT COUNT(*) FROM files f WHERE {where}', params).fetchone()[0]
        order_by = SEARCH_ORDERS.get(order, SEARCH_ORDERS['downloaded'])
        sql = f'SELECT {ROW_COLUMNS} FROM files f WHERE {where} ORDER BY {order_by} LIMIT ? OFFSET ?'
        res['items'] = [self._row(r) for r in conn.execute(sql, (*params, page_size, page * page_size))]
        return res

    def stats(self) -> dict:
        """Totals for the settings page: the library (files in the trash left out) and the trash {files, bytes, oldest: trashed_at}.
        Files are not checked on disk (a stat per file would be slow on large indexes)"""
        db_bytes = 0
        for suffix in ('', '-wal', '-shm'):
            with contextlib.suppress(OSError):
                db_bytes += os.path.getsize(f'{self.path}{suffix}')
        out = {'path': str(self.path), 'exists': False, 'schema': 0, 'files': 0, 'posts': 0, 'withMd5': 0, 'tags': 0, 'bytes': 0,
               'dbBytes': db_bytes, 'first': None, 'last': None, 'modules': [], 'recording': indexing_enabled(),
               'trash': {'files': 0, 'bytes': 0, 'oldest': None}}
        conn = self._connection()
        if conn is None:
            return out
        files, with_md5, size, first, last = conn.execute(
            'SELECT COUNT(*), COUNT(md5), COALESCE(SUM(size), 0), MIN(downloaded_at), MAX(downloaded_at) FROM files '
            'WHERE trashed_at IS NULL').fetchone()
        trashed, trash_bytes, oldest = conn.execute(
            'SELECT COUNT(*), COALESCE(SUM(size), 0), MIN(trashed_at) FROM files WHERE trashed_at IS NOT NULL').fetchone()
        out.update({
            'exists': True, 'schema': conn.execute('PRAGMA user_version').fetchone()[0], 'files': files, 'withMd5': with_md5,
            'bytes': size, 'first': first, 'last': last,
            'posts': conn.execute('SELECT COUNT(*) FROM (SELECT 1 FROM files WHERE trashed_at IS NULL GROUP BY module, post_id)').fetchone()[0],
            'tags': conn.execute('SELECT COUNT(*) FROM tag_names').fetchone()[0],
            'modules': [{'module': m, 'files': n, 'bytes': b, 'last': t} for m, n, b, t in conn.execute(
                'SELECT module, COUNT(*), COALESCE(SUM(size), 0), MAX(downloaded_at) FROM files WHERE trashed_at IS NULL '
                'GROUP BY module ORDER BY COUNT(*) DESC')],
            'trash': {'files': trashed, 'bytes': trash_bytes, 'oldest': oldest},
        })
        return out

    # ----- maintenance (lynxx/dlindex_tools.py) -----

    def known_paths(self, paths: Iterable[str]) -> set[str]:
        """The given absolute paths that already have a row (whether the file is still on disk or not)"""
        conn = self._connection()
        out: set[str] = set()
        if conn is None:
            return out
        for chunk in _chunks(list(dict.fromkeys(paths)), SQL_CHUNK):
            out.update(p for (p,) in conn.execute(f'SELECT path FROM files WHERE path IN ({_qs(len(chunk))})', chunk))
        return out

    def rows_for_paths(self, paths: Iterable[str]) -> dict[str, dict]:
        """{path: row (see search items)} of the given absolute paths"""
        conn = self._connection()
        out: dict[str, dict] = {}
        if conn is None:
            return out
        for chunk in _chunks(list(dict.fromkeys(paths)), SQL_CHUNK):
            for r in conn.execute(f'SELECT {ROW_COLUMNS} FROM files f WHERE f.path IN ({_qs(len(chunk))})', chunk):
                out[r[3]] = self._row(r)
        return out

    def posts_after(self, module: str, after: int = 0, limit: int = 5000) -> list[tuple[int, str, str]]:
        """(row id, post id, tags) of this module's files with a row id above 'after', oldest first: tag lists learned from the
        downloads read the index in steps (lynxx/tags_learned.py). Files in the trash are left out"""
        conn = self._connection()
        if conn is None:
            return []
        rows = conn.execute('SELECT id, post_id, tags FROM files WHERE module = ? AND id > ? AND trashed_at IS NULL ORDER BY id LIMIT ?',
                            (module, int(after), int(limit)))
        return [(int(r[0]), str(r[1]), str(r[2] or '')) for r in rows]

    def rows_for_posts(self, module: str, post_ids: Iterable[str]) -> dict[str, dict]:
        """{post id: row} of this module's posts (the most recent download of each), any path. Files in the trash are left out"""
        conn = self._connection()
        out: dict[str, dict] = {}
        if conn is None:
            return out
        ids = list(dict.fromkeys(str(x).strip() for x in post_ids if str(x).strip()))
        for chunk in _chunks(ids, SQL_CHUNK):
            sql = (f'SELECT {ROW_COLUMNS} FROM files f WHERE f.module = ? AND f.post_id IN ({_qs(len(chunk))}) AND f.trashed_at IS NULL '
                   'ORDER BY f.downloaded_at ASC')
            for r in conn.execute(sql, (module, *(_db_post_id(x) for x in chunk))):
                out[str(r[1])] = self._row(r)
        return out

    def rename_paths(self, pairs: Sequence[tuple[str, str]]) -> int:
        """Files moved / renamed on disk (re-tag, sort into subfolders): their rows follow (old path -> new path, absolute).
        A row already at the new path (a stale one) is replaced. Returns the number of rows moved"""
        pairs = [(str(a), str(b)) for a, b in pairs if str(a) != str(b)]
        conn = self._connection()
        if conn is None or not pairs:
            return 0
        moved = 0
        conn.execute('BEGIN IMMEDIATE')
        try:
            for old, new in pairs:
                if conn.execute('SELECT 1 FROM files WHERE path = ?', (old,)).fetchone() is None:
                    continue
                if (stale := conn.execute('SELECT id FROM files WHERE path = ?', (new,)).fetchone()) is not None:
                    conn.execute('DELETE FROM file_tags WHERE file_id = ?', stale)
                    conn.execute('DELETE FROM files WHERE id = ?', stale)
                conn.execute('UPDATE files SET path = ? WHERE path = ?', (new, old))
                moved += 1
            conn.execute('COMMIT')
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute('ROLLBACK')
            raise
        return moved

    def clean_missing(self, *, include_missing_dirs: bool = False, dry_run: bool = False, batch: int = CLEAN_BATCH,
                      progress: Callable[[CleanResult], None] | None = None, cancel: threading.Event | None = None,
                      pause: float = 0.0) -> CleanResult:
        """Drops the rows whose file is no longer on disk (a stat per row, files are never opened, moved or deleted). Rows are read
        in id order, `batch` at a time; each batch's deletions are one short transaction, so download jobs keep recording meanwhile.
        A row rewritten meanwhile (the file downloaded again: new downloaded_at) is kept. Rows whose folder is gone as well are kept
        unless include_missing_dirs (an unplugged drive or unmounted share looks the same). The tag names stay (writers cache their
        ids). progress(result) after each batch; cancel stops between batches"""
        res = CleanResult(dry_run=dry_run, include_missing_dirs=include_missing_dirs)
        conn = self._connection()
        if conn is None:
            return res
        if not dry_run:
            self.recover_moves()  # a row whose file reached the trash before a crash is not missing
        batch = max(1, int(batch))
        res.total = conn.execute('SELECT COUNT(*) FROM files').fetchone()[0]
        dirs: dict[str, bool] = {}
        last_id = 0
        while True:
            if cancel is not None and cancel.is_set():
                res.cancelled = True
                break
            rows = conn.execute('SELECT id, path, downloaded_at FROM files WHERE id > ? ORDER BY id LIMIT ?', (last_id, batch)).fetchall()
            if not rows:
                break
            last_id = rows[-1][0]
            gone: list[tuple[int, str, float]] = []
            for file_id, path, at in rows:
                if file_present(path):
                    continue
                res.missing += 1
                folder = os.path.dirname(path)
                if (folder_ok := dirs.get(folder)) is None:
                    folder_ok = dirs[folder] = os.path.isdir(folder)
                    if not folder_ok and len(res.folders) < CLEAN_FOLDERS_MAX:
                        res.folders.append(folder)
                if not folder_ok:
                    res.missing_dirs += 1
                    if not include_missing_dirs:
                        continue
                gone.append((file_id, path, at))
            res.checked += len(rows)
            if gone and not dry_run:
                res.removed += self._delete_rows(conn, gone)
            if progress is not None:
                progress(res)
            if pause > 0:
                time.sleep(pause)
        return res

    @staticmethod
    def _delete_rows(conn: sqlite3.Connection, rows: Sequence[tuple[int, str, float]]) -> int:
        removed = 0
        conn.execute('BEGIN IMMEDIATE')
        try:
            for file_id, path, at in rows:
                # same path and download time: a row the writer of a running job rewrote meanwhile is not touched
                if conn.execute('DELETE FROM files WHERE id = ? AND path = ? AND downloaded_at = ?', (file_id, path, at)).rowcount:
                    conn.execute('DELETE FROM file_tags WHERE file_id = ?', (file_id,))
                    removed += 1
            conn.execute('COMMIT')
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute('ROLLBACK')
            raise
        return removed

    # ----- trash (the "Đã tải" page of the desktop app, see the module docstring) -----

    def trash_folders(self, path: str, roots: Sequence[str | os.PathLike] = ()) -> list[str]:
        """Trash folders for the file at path, best first: <data dir>/trash when it is on the file's drive, '.lynxx-trash' at the
        root of the download folder that holds the file (roots: the configured download folders), '.lynxx-trash' next to the file.
        Files are only renamed into one of them, never copied: a folder on another drive is left out, and trash() goes on to the
        next one when a rename still fails with EXDEV (a mount point inside the download folder)"""
        try:
            dev = os.stat(path).st_dev
        except OSError:
            return []
        out: list[str] = []
        if _device(str(self.trash_dir)) == dev:
            out.append(str(self.trash_dir))
        folder = os.path.dirname(os.path.abspath(path))
        for root in roots:
            root = os.path.abspath(os.fspath(root))
            if _within(folder, root) and _device(root) == dev:
                out.append(os.path.join(root, TRASH_HIDDEN_NAME))
        out.append(os.path.join(folder, TRASH_HIDDEN_NAME))
        return list(dict.fromkeys(out))

    @staticmethod
    def _free_path(conn: sqlite3.Connection, folder: str, name: str, taken: set[str] | None = None) -> str:
        """folder/name, else folder/<stem>_1<ext>, _2, ... when a file, a row of the index or another file of the batch (taken) has
        that path already"""
        stem, ext = os.path.splitext(name)
        for n in range(TRASH_NAME_TRIES):
            target = os.path.join(folder, f'{stem}_{n:d}{ext}' if n else name)
            if (target not in (taken or ()) and not os.path.lexists(target)
                    and conn.execute('SELECT 1 FROM files WHERE path = ?', (target,)).fetchone() is None):
                return target
        raise FileExistsError(errno.EEXIST, 'no free name', os.path.join(folder, name))

    def _plan_moves(self, conn: sqlite3.Connection, chunk: Sequence[str], restoring: bool, roots: Sequence[str | os.PathLike],
                    res: dict) -> list[tuple[int, str, list[str]]]:
        """First step of trash() / restore(), one transaction: the rows of chunk that may move and where to, best first (trash: a
        free '<row id>_<name>' in each trash folder of the file; restore: its old path, else <stem>_1<ext>, ...), written to their
        moving_to before any file is renamed: a process stopped between the renames and the commit leaves where the files went
        (recover_moves). Paths refused or failed go to res. Returns [(row id, path, targets)]"""
        plans: list[tuple[int, str, list[str]]] = []
        taken: set[str] = set()
        conn.execute('BEGIN IMMEDIATE')
        try:
            rows = {p: (i, orig) for i, p, orig in conn.execute(
                f'SELECT id, path, orig_path FROM files WHERE path IN ({_qs(len(chunk))}) '
                f'AND trashed_at IS {"NOT NULL" if restoring else "NULL"}', chunk)}
            for path in chunk:
                row_id, orig = rows.get(path, (None, ''))
                if row_id is None or (restoring and not orig) or (not restoring and not file_present(path)):
                    res['refused'].append(path)
                    continue
                if restoring and not file_present(path):
                    res['failed'].append({'path': path, 'error': 'the file is no longer in the trash'})
                    continue
                targets: list[str] = []
                why = 'no trash folder on the drive of the file'
                folders = [os.path.dirname(orig)] if restoring else self.trash_folders(path, roots)
                name = os.path.basename(orig) if restoring else f'{row_id:d}_{os.path.basename(path)}'
                for folder in folders:
                    try:
                        targets.append(self._free_path(conn, folder, name, taken))
                    except OSError as err:
                        why = _os_error(err)
                if not targets:
                    res['failed'].append({'path': path, 'error': why})
                    continue
                taken.update(targets)
                conn.execute('UPDATE files SET moving_to = ? WHERE id = ?', (json.dumps(targets), row_id))
                plans.append((row_id, path, targets))
            conn.execute('COMMIT')
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute('ROLLBACK')
            raise
        return plans

    @staticmethod
    def _move_planned(conn: sqlite3.Connection, plans: list[tuple[int, str, list[str]]], restoring: bool,
                      when: float) -> tuple[list[tuple[int, str, str]], list[dict]]:
        """Second step of trash() / restore(), one transaction that holds the write lock from the first rename to the commit (a purge,
        forget or other move that finds a file gone meanwhile waits for the commit, then finds its row changed and leaves it alone;
        recover_moves waits too): each file whose row did not change since the first step
        is renamed to the first of its targets that takes it (EXDEV: another drive after all, the next folder is nearer to the
        file; never copied) and its row follows. A failure renames back every file of the batch before the rollback; a process
        stopped here leaves moving_to (recover_moves). Returns (moves kept: (row id, old path, new path), failed: [{path, error}])"""
        moved: list[tuple[int, str, str]] = []
        failed: list[dict] = []
        if not plans:
            return moved, failed
        try:
            conn.execute('BEGIN IMMEDIATE')
            for row_id, old, targets in plans:
                row = conn.execute('SELECT path, trashed_at IS NOT NULL FROM files WHERE id = ?', (row_id,)).fetchone()
                new, why = '', ''
                if row is None or row[0] != old or bool(row[1]) != restoring:
                    why = 'changed meanwhile'
                elif not file_present(old):
                    why = 'the file is no longer in the trash' if restoring else 'the file is no longer there'
                else:
                    new, why = _rename_first(old, targets)
                if not new:
                    failed.append({'path': old, 'error': why})
                    conn.execute('UPDATE files SET moving_to = NULL WHERE id = ?', (row_id,))
                    continue
                moved.append((row_id, old, new))
                if restoring:
                    conn.execute('UPDATE files SET path = ?, orig_path = NULL, trashed_at = NULL, moving_to = NULL WHERE id = ?',
                                 (new, row_id))
                else:
                    conn.execute('UPDATE files SET path = ?, orig_path = ?, trashed_at = ?, moving_to = NULL WHERE id = ?',
                                 (new, old, when, row_id))
            conn.execute('COMMIT')
        except BaseException:
            for _, old, new in moved:  # back while the write lock is still held (BEGIN failed: nothing was moved)
                if not os.path.lexists(old):
                    with contextlib.suppress(OSError):
                        os.replace(new, old)
            with contextlib.suppress(sqlite3.Error):
                conn.execute('ROLLBACK')
            with contextlib.suppress(sqlite3.Error):  # the files back where they were need no recovery
                back = [(row_id,) for row_id, old, _ in plans if os.path.lexists(old)]
                conn.executemany('UPDATE files SET moving_to = NULL WHERE id = ?', back)
            raise
        return moved, failed

    def recover_moves(self) -> int:
        """Moves of trash() / restore() a stopped process left half done (crash, power loss, the app quitting while files moved):
        a row whose file is gone from its path and sits at one of its moving_to targets follows the file (into the trash, or back
        to the library); any other mark is dropped (the file never moved). Under the write lock: a move under way holds it from its
        first rename to its commit. Cheap when there is nothing to do (one indexed query). Returns the rows fixed"""
        conn = self._connection()
        if conn is None or conn.execute('SELECT 1 FROM files WHERE moving_to IS NOT NULL LIMIT 1').fetchone() is None:
            return 0
        fixed = 0
        conn.execute('BEGIN IMMEDIATE')
        try:
            for row_id, path, trashed_at, moving_to in conn.execute(
                    'SELECT id, path, trashed_at, moving_to FROM files WHERE moving_to IS NOT NULL').fetchall():
                try:
                    targets = [str(t) for t in json.loads(moving_to)]
                except (TypeError, ValueError):
                    targets = []
                landed = '' if os.path.lexists(path) else next((t for t in targets if file_present(t)), '')
                if not landed or conn.execute('SELECT 1 FROM files WHERE path = ?', (landed,)).fetchone() is not None:
                    conn.execute('UPDATE files SET moving_to = NULL WHERE id = ?', (row_id,))
                elif trashed_at is None:  # trash(): the file reached the trash
                    conn.execute('UPDATE files SET path = ?, orig_path = ?, trashed_at = ?, moving_to = NULL WHERE id = ?',
                                 (landed, path, time.time(), row_id))
                    fixed += 1
                else:  # restore(): the file is back in the library
                    conn.execute('UPDATE files SET path = ?, orig_path = NULL, trashed_at = NULL, moving_to = NULL WHERE id = ?',
                                 (landed, row_id))
                    fixed += 1
            conn.execute('COMMIT')
        except BaseException:
            with contextlib.suppress(sqlite3.Error):
                conn.execute('ROLLBACK')
            raise
        if fixed:
            trace(f'Download index: {fixed:d} file move(s) left unfinished were completed')
        return fixed

    def trash(self, paths: Iterable[str], *, roots: Sequence[str | os.PathLike] = (), now: float | None = None) -> dict:
        """Moves files of the library into the trash (see trash_folders; named '<row id>_<name>') and marks their rows: path = where
        the file is now, orig_path = where it was, trashed_at. Only rows of the library whose file is on disk: a path the index does
        not know, or already in the trash, is refused and never touched. TRASH_BATCH files at a time, each batch in two steps
        (_plan_moves, _move_planned). {trashed: [{path, trashPath}], failed: [{path, error}], refused: [path]}"""
        wanted = list(dict.fromkeys(str(p) for p in paths))
        res: dict = {'trashed': [], 'failed': [], 'refused': []}
        conn = self._connection()
        if conn is None:
            res['refused'] = wanted
            return res
        self.recover_moves()
        when = time.time() if now is None else float(now)
        for chunk in _chunks(wanted, TRASH_BATCH):
            kept, failed = self._move_planned(conn, self._plan_moves(conn, chunk, False, roots, res), False, when)
            res['trashed'] += [{'path': old, 'trashPath': new} for _, old, new in kept]
            res['failed'] += failed
        return res

    def restore(self, paths: Iterable[str]) -> dict:
        """Moves files of the trash (their current paths) back where they were and clears their trash mark. A folder that is gone
        is created again; a name taken meanwhile (a file, or the row of a new download of the post) gets _1, _2, ... Only rows in
        the trash: any other path is refused. A rename that fails (another drive: files are never copied) leaves the file in the
        trash. {restored: [{path, restoredPath}], failed: [{path, error}], refused: [path]}"""
        wanted = list(dict.fromkeys(str(p) for p in paths))
        res: dict = {'restored': [], 'failed': [], 'refused': []}
        conn = self._connection()
        if conn is None:
            res['refused'] = wanted
            return res
        self.recover_moves()
        for chunk in _chunks(wanted, TRASH_BATCH):
            kept, failed = self._move_planned(conn, self._plan_moves(conn, chunk, True, (), res), True, 0.0)
            res['restored'] += [{'path': old, 'restoredPath': new} for _, old, new in kept]
            res['failed'] += failed
        return res

    def purge_trash(self, *, older_than_days: float | None = None, paths: Iterable[str] | None = None, all: bool = False,  # noqa: A002
                    now: float | None = None, batch: int = CLEAN_BATCH) -> dict:
        """Deletes files of the trash for good, then their rows and tags, a batch at a time (one short transaction each): rows
        trashed more than older_than_days ago, the given paths (rows in the trash only), or all of them; one of the three. A file
        already gone only loses its row; a file that cannot be deleted (in use) keeps it, and so does a file whose trash folder is
        gone (an unplugged drive or an unmounted share looks the same: failed, purged when it is back). Only files directly inside a
        trash folder are deleted: a row of the trash that points anywhere else loses its row, its file stays (kept). A row being
        restored (moving_to) is left alone. Empty '.lynxx-trash' folders are removed.
        {purged: rows removed, files: files deleted, bytes, failed: [{path, error}], kept}"""
        if (older_than_days is not None) + (paths is not None) + bool(all) != 1:
            raise ValueError('purge_trash: older_than_days, paths or all, one of them')
        res: dict = {'purged': 0, 'files': 0, 'bytes': 0, 'failed': [], 'kept': 0}
        conn = self._connection()
        if conn is None:
            return res
        self.recover_moves()
        batch = max(1, int(batch))

        def steps() -> Iterator[list[tuple[int, str, float]]]:
            if paths is not None:
                for chunk in _chunks(list(dict.fromkeys(str(p) for p in paths)), min(batch, SQL_CHUNK)):
                    yield conn.execute(f'SELECT id, path, downloaded_at FROM files WHERE path IN ({_qs(len(chunk))}) '
                                       'AND trashed_at IS NOT NULL AND moving_to IS NULL', chunk).fetchall()
                return
            where, args = 'trashed_at IS NOT NULL AND moving_to IS NULL', []
            if older_than_days is not None:
                where += ' AND trashed_at < ?'
                args.append((time.time() if now is None else float(now)) - float(older_than_days) * DAY_SECONDS)
            last = 0
            while rows := conn.execute(f'SELECT id, path, downloaded_at FROM files WHERE {where} AND id > ? ORDER BY id LIMIT ?',
                                       (*args, last, batch)).fetchall():
                last = rows[-1][0]
                yield rows

        folders: set[str] = set()
        for rows in steps():
            gone: list[tuple[int, str, float]] = []
            for row in rows:
                path = row[1]
                if not in_trash_folder(path):
                    res['kept'] += 1
                else:
                    try:
                        size = os.lstat(path).st_size
                        os.remove(path)
                        res['files'] += 1
                        res['bytes'] += size
                        folders.add(os.path.dirname(path))
                    except FileNotFoundError:
                        if not os.path.isdir(os.path.dirname(path)):
                            res['failed'].append({'path': path, 'error': TRASH_UNREACHABLE})
                            continue
                    except OSError as err:
                        res['failed'].append({'path': path, 'error': _os_error(err)})
                        continue
                gone.append(row)
            if gone:
                res['purged'] += self._delete_rows(conn, gone)
        for folder in folders:
            if os.path.basename(folder) == TRASH_HIDDEN_NAME:
                with contextlib.suppress(OSError):
                    os.rmdir(folder)  # only when empty
        return res

    def forget(self, paths: Iterable[str]) -> dict:
        """Drops the rows (and tags) of files that are no longer on disk, in the library or the trash. Rows only: a path whose file
        is still there, or that the index does not know, is refused, and so is a row of the trash whose trash folder is gone (an
        unplugged drive: its file would stay there with no row). {removed, refused: [path]}"""
        wanted = list(dict.fromkeys(str(p) for p in paths))
        res: dict = {'removed': 0, 'refused': []}
        conn = self._connection()
        if conn is None:
            res['refused'] = wanted
            return res
        self.recover_moves()
        for chunk in _chunks(wanted, SQL_CHUNK):
            rows = {r[1]: r for r in conn.execute(f'SELECT id, path, downloaded_at, trashed_at FROM files WHERE path IN ({_qs(len(chunk))}) '
                                                  'AND moving_to IS NULL', chunk)}
            gone = []
            for path in chunk:
                row = rows.get(path)
                if row is None or file_present(path) or (row[3] is not None and not os.path.isdir(os.path.dirname(path))):
                    res['refused'].append(path)
                else:
                    gone.append(row[:3])
            if gone:
                res['removed'] += self._delete_rows(conn, gone)
        return res


def rename_indexed_files(pairs: Sequence[tuple[str | os.PathLike, str | os.PathLike]]) -> int:
    """Best effort for file tools (re-tag, un-tag, sort): moved files keep their index rows. Never raises, an index that does not
    exist is not created. Returns the number of rows moved"""
    if not pairs:
        return 0
    try:
        with DownloadIndex() as index:
            return index.rename_paths([(os.path.abspath(os.fspath(a)), os.path.abspath(os.fspath(b))) for a, b in pairs])
    except Exception as err:
        trace(f'Warning (W2): download index: unable to follow {len(pairs):d} renamed file(s): {type(err).__name__}: {err}', True)
        return 0


class IndexWriter:
    """Download threads only enqueue records (add() never touches the database); a daemon thread writes them in batches, one
    transaction per batch. Failures never reach the download: they are counted and reported once"""
    _STOP = object()

    def __init__(self, path: str | os.PathLike | None = None, *, batch_max: int = BATCH_MAX, interval: float = FLUSH_INTERVAL) -> None:
        self._path = path
        self._batch_max = max(1, batch_max)
        self._interval = max(0.0, interval)
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._closed = False
        self.written = 0
        self.failed = 0
        self.error = ''

    def add(self, record: IndexRecord) -> None:
        with self._lock:
            if self._closed:
                return
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name='dlindex', daemon=True)
                self._thread.start()
        self._queue.put(record)

    def close(self, timeout: float = CLOSE_TIMEOUT) -> bool:
        """Writes what is queued and stops the thread. False if it did not finish in time (it keeps going as a daemon thread)"""
        with self._lock:
            if self._closed:
                thread = None
            else:
                self._closed = True
                thread = self._thread
                if thread is not None:
                    self._queue.put(self._STOP)
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _run(self) -> None:
        index = DownloadIndex(self._path, create=True)
        batch: list[IndexRecord] = []
        deadline = 0.0
        try:
            while True:
                try:
                    item = self._queue.get(timeout=max(0.0, deadline - time.monotonic()) if batch else None)
                except queue.Empty:
                    item = None
                if item is self._STOP:
                    self._flush(index, batch)
                    break
                if item is not None:
                    if not batch:
                        deadline = time.monotonic() + self._interval
                    batch.append(item)
                if batch and (item is None or len(batch) >= self._batch_max or time.monotonic() >= deadline):
                    self._flush(index, batch)
        finally:
            index.close()

    def _flush(self, index: DownloadIndex, batch: list[IndexRecord]) -> None:
        if not batch:
            return
        for attempt in range(1, WRITE_ATTEMPTS + 1):
            try:
                self.written += index.add_many(batch)
                break
            except sqlite3.OperationalError as err:
                if not _is_busy(err) or attempt == WRITE_ATTEMPTS:
                    self._fail(len(batch), err)
                    break
                time.sleep(RETRY_PAUSE * attempt)
            except Exception as err:
                self._fail(len(batch), err)
                break
        batch.clear()

    def _fail(self, count: int, err: Exception) -> None:
        self.failed += count
        if not self.error:
            self.error = f'{type(err).__name__}: {err}'
            # the report must not stop the thread: outside the app the logger has no window and raises
            with contextlib.suppress(Exception):
                trace(f'Warning (W2): download index: unable to record {count:d} file(s): {self.error}', True)

#
#
#########################################
