# coding=UTF-8
"""
Lynxx: tools of the local download index (lynxx/dlindex.py)
  python -m lynxx.dlindex_tools clean [--dry-run] [--missing-folders] [--json]
  python -m lynxx.dlindex_tools import FOLDER [--module ABBR] [--no-recursive] [--verify-md5] [--dry-run] [--json]
  python -m lynxx.dlindex_tools retag PATH... [--module ABBR] [--types auto|general,copyright,artist,character,metadata]
                                    [--source both|index|json]
  python -m lynxx.dlindex_tools trash list [--json]
  python -m lynxx.dlindex_tools trash purge (--days N | --all) [--json]
- clean: drops the rows whose file is no longer on disk. Rows only: files are never touched. Rows whose folder is gone too (an
  unplugged drive looks the same) are kept unless --missing-folders
- import: records media files already on disk (a library downloaded earlier). Module + post id (or md5) come from the file name
  (the rules of 'files.identify', lynxx/file_identify.py), the post metadata from the website: danbooru 100 posts per request
  (id / md5 lists), other modules one request per post, at most one request per IMPORT_MIN_INTERVAL seconds (or the module's own
  interval if longer). Files already in the index (same path) are skipped; names that are not recognized are counted and listed,
  never guessed. Plain numbers ('12345.jpg') and md5 names need --module
- retag: the re-tag tool (score and tags appended to Lynxx file names) with tags from the index, the info files of the folder
  ('-dump_tags') as fallback
- trash: the files the desktop app's "Đã tải" page deleted (moved to a trash folder, see DownloadIndex.trash). list: what is in
  it; purge: deletes for good the files trashed more than N days ago (--days 0: all of them) or all of them, then their rows. The
  app empties its trash by itself (settings: library.trashDays)
Desktop app: 'import' runs as a job (frozen core: 'lynxx-core index import ...'); with LYNXX_IPC=1 it prints '@@LYNXX_PROGRESS'
lines and reads 'cancel' on stdin like download jobs. 'clean' runs in the sidecar ('index.clean'). Exit codes: 0 ok, 1 error,
2 invalid arguments, 3 finished with failed requests, 130 cancelled
"""
#########################################
#
#

from __future__ import annotations

import argparse
import contextlib
import hashlib
import inspect
import json
import os
import pathlib
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from multiprocessing.dummy import current_process

from .defines import KNOWN_EXTENSIONS, ThreadInterruptException
from .dlindex import SEARCH_PAGE_SIZE_MAX, CleanResult, DownloadIndex, IndexRecord, norm_md5, record_for
from .file_identify import ID_LIST_CHUNK, ID_LIST_MODULES, identify_file_name
from .logger import Logger, trace
from .module import ProcModule

__all__ = ('ImportResult', 'LibraryImport', 'clean_index', 'main', 'purge_trash', 'scan_library')

ENV_SECRET_MODULE_ARGS = 'LYNXX_SECRET_MODULE_ARGS'
'''JSON object {module abbr: [arguments]} (ex. {"db": ["-api_key", "KEY,LOGIN"]}): per module secrets of an import job, removed from
the environment once read. Arguments shared by every module come in LYNXX_SECRET_ARGS (see lynxx_cmd)'''
IMPORT_MIN_INTERVAL = 0.5
'''seconds between two metadata requests of an import, at least'''
IMPORT_WRITE_BATCH = 200
IMPORT_FILES_MAX = 1_000_000
IMPORT_DEPTH_MAX = 32
IMPORT_FAIL_STREAK = 5
'''a module whose requests fail this many times in a row is given up: its remaining files count as failed'''
IMPORT_SINGLE_PAGE = 20
'''page size asked for one id / md5 (one post expected)'''
IMPORT_NAMES_KEEP = 10_000
'''names kept per list in memory (the counts stay exact)'''
IMPORT_NAMES_REPORT = 200
'''names per list in the DONE line of a job'''
IMPORT_NAMES_LOG = 1000
'''names per list written to the log'''
WRITE_ATTEMPTS = 3
STATE_SCAN = 'IMPORT_SCAN'
STATE_FETCH = 'IMPORT_FETCH'
STATE_DONE = 'DONE'
EXIT_OK, EXIT_ERROR, EXIT_ARGS, EXIT_PARTIAL, EXIT_CANCELLED = 0, 1, 2, 3, 130
MEDIA_EXTENSIONS = frozenset(KNOWN_EXTENSIONS) | {'jpeg'}


# ----- clean -----

def clean_index(*, include_missing_dirs: bool = False, dry_run: bool = False, index_path: str | os.PathLike | None = None,
                progress: Callable[[CleanResult], None] | None = None, cancel: threading.Event | None = None) -> CleanResult:
    """DownloadIndex.clean_missing on the index (default: <data dir>/index/downloads.sqlite). A missing index: nothing to do"""
    with DownloadIndex(index_path) as index:
        return index.clean_missing(include_missing_dirs=include_missing_dirs, dry_run=dry_run, progress=progress, cancel=cancel)


# ----- trash -----

def purge_trash(*, days: float | None = None, everything: bool = False, index_path: str | os.PathLike | None = None) -> dict:
    """DownloadIndex.purge_trash: files trashed more than `days` ago, or every file of the trash. A missing index: nothing to do"""
    with DownloadIndex(index_path) as index:
        if everything:
            return index.purge_trash(all=True)
        return index.purge_trash(older_than_days=days)


def trash_items(index_path: str | os.PathLike | None = None) -> Iterator[dict]:
    """Rows of the trash, most recently trashed first (see DownloadIndex.search)"""
    with DownloadIndex(index_path) as index:
        page = 0
        while items := index.search('', trash=True, order='trashed', page=page, page_size=SEARCH_PAGE_SIZE_MAX)['items']:
            yield from items
            page += 1


# ----- import -----

@dataclass(slots=True)
class LibraryFile:
    path: str
    rel: str
    size: int
    mtime: float
    module: str = ''
    kind: str = ''
    value: str = ''

    @property
    def name(self) -> str:
        return os.path.basename(self.path)


def scan_library(folder: str | os.PathLike, *, recursive: bool = True, cancel: threading.Event | None = None) -> Iterator[LibraryFile]:
    """Media files (known extensions, not empty) under folder, sorted by path. Symlinked folders are not followed, '.part' files and
    hidden folders are skipped (among them '.lynxx-trash', the app's trash: its files are not imported again). Only names and
    sizes are read"""
    root = os.path.abspath(os.fspath(folder))
    stack = [(root, 0)]
    while stack:
        if cancel is not None and cancel.is_set():
            return
        current, depth = stack.pop()
        try:
            with os.scandir(current) as listing:
                entries = sorted(listing, key=lambda e: e.name)
        except OSError:
            continue
        subdirs = []
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if recursive and depth < IMPORT_DEPTH_MAX and not e.name.startswith('.'):
                        subdirs.append(e.path)
                    continue
                ext = e.name.rsplit('.', 1)[-1].lower() if '.' in e.name else ''
                if ext not in MEDIA_EXTENSIONS or not e.is_file():
                    continue
                st = e.stat()
            except OSError:
                continue
            if st.st_size > 0:
                yield LibraryFile(path=e.path, rel=os.path.relpath(e.path, root), size=st.st_size, mtime=st.st_mtime)
        stack.extend((d, depth + 1) for d in reversed(subdirs))


def file_md5(path: str) -> str:
    h = hashlib.md5(usedforsecurity=False)
    with open(path, 'rb') as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class ImportResult:
    folder: str
    dry_run: bool = False
    scanned: int = 0
    '''media files found'''
    known: int = 0
    '''already in the index (same path): skipped'''
    unknown: int = 0
    '''names not recognized, without a module, or of a module that is not installed'''
    identified: int = 0
    '''files to look up'''
    imported: int = 0
    not_found: int = 0
    '''the website did not return the post (deleted, hidden, wrong module)'''
    failed: int = 0
    '''requests that failed (the files can be imported by running again)'''
    mismatch: int = 0
    '''--verify-md5: the file is not the post's original file'''
    requests: int = 0
    cancelled: bool = False
    truncated: bool = False
    '''more than IMPORT_FILES_MAX media files: the rest was not looked at'''
    error: str = ''
    unknown_names: list[str] = field(default_factory=list)
    not_found_names: list[str] = field(default_factory=list)
    failed_names: list[str] = field(default_factory=list)
    mismatch_names: list[str] = field(default_factory=list)
    modules: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def processed(self) -> int:
        return self.imported + self.not_found + self.failed + self.mismatch

    def exit_code(self) -> int:
        if self.cancelled:
            return EXIT_CANCELLED
        if self.error:
            return EXIT_ERROR
        if self.failed:
            return EXIT_PARTIAL if self.imported or self.not_found else EXIT_ERROR
        return EXIT_OK

    def as_dict(self, names_max: int = IMPORT_NAMES_REPORT) -> dict:
        return {
            'folder': self.folder, 'dryRun': self.dry_run, 'scanned': self.scanned, 'known': self.known, 'unknown': self.unknown,
            'totalAll': self.identified, 'processed': self.processed, 'success': self.imported, 'notFound': self.not_found,
            'failed': self.failed, 'mismatch': self.mismatch, 'requests': self.requests, 'cancelled': self.cancelled,
            'truncated': self.truncated, 'error': self.error, 'modules': self.modules,
            'unknownNames': self.unknown_names[:names_max], 'notFoundItems': self.not_found_names[:names_max],
            'failedItems': self.failed_names[:names_max], 'mismatchItems': self.mismatch_names[:names_max],
        }


def _note(names: list[str], text: str) -> None:
    if len(names) < IMPORT_NAMES_KEEP:
        names.append(text)


_page_size_support: dict[type, bool] = {}


def _takes_page_size(cls: type) -> bool:
    if (ok := _page_size_support.get(cls)) is None:
        try:
            ok = 'maxlim' in inspect.signature(cls._form_tags_search_address).parameters
        except (TypeError, ValueError):
            ok = False
        _page_size_support[cls] = ok
    return ok


class LibraryImport:
    """One import run (see the module docstring). Thread-safe to read progress() from another thread; cancel: an Event set from
    outside (the job's 'cancel' line), checked between requests"""

    def __init__(self, folder: str | os.PathLike, *, default_module: str = '', recursive: bool = True, dry_run: bool = False,
                 verify_md5: bool = False, index_path: str | os.PathLike | None = None, common_args: Sequence[str] = (),
                 module_args: dict[str, list[str]] | None = None, min_interval: float = IMPORT_MIN_INTERVAL,
                 cancel: threading.Event | None = None, downloader_factory: Callable[[int], object] | None = None,
                 log: Callable[[str], None] | None = None) -> None:
        self.folder = os.path.abspath(os.fspath(folder))
        self.default_module = default_module
        self.recursive = recursive
        self.verify_md5 = verify_md5
        self.index_path = index_path
        self.common_args = list(common_args)
        self.module_args = dict(module_args or {})
        self.min_interval = max(0.0, float(min_interval))
        self.cancel = cancel or threading.Event()
        self.factory = downloader_factory
        self.log = log or (lambda text: trace(text, True))
        self.result = ImportResult(folder=self.folder, dry_run=dry_run)
        self.state = STATE_SCAN
        self.module = ''
        self._pending: list[IndexRecord] = []
        self._writer: DownloadIndex | None = None
        self._last_request = 0.0

    # ----- progress -----

    def progress(self) -> dict:
        r = self.result
        return {'state': 0, 'stateName': self.state, 'scanned': r.scanned, 'known': r.known, 'unknown': r.unknown,
                'totalAll': r.identified, 'processed': r.processed, 'success': r.imported, 'failed': r.failed, 'notFound': r.not_found,
                'mismatch': r.mismatch, 'requests': r.requests, 'module': self.module, 'dryRun': r.dry_run}

    def _cancelled(self) -> bool:
        if self.cancel.is_set():
            self.result.cancelled = True
        return self.result.cancelled

    # ----- run -----

    def run(self) -> ImportResult:
        res = self.result
        me = current_process()
        me.killed = False
        try:
            if not os.path.isdir(self.folder):
                res.error = f'folder not found: {self.folder}'
                return res
            from .extensions import Extensions
            Extensions.ensure_loaded()  # module prefixes of file names: builtin and installed extensions
            files = self._scan()
            if self._cancelled():
                return res
            todo = self._identify(files)
            res.identified = len(todo)
            by_module: dict[str, list[LibraryFile]] = {}
            for f in todo:
                by_module.setdefault(f.module, []).append(f)
            for module, mfiles in by_module.items():
                res.modules[module] = {'files': len(mfiles), 'imported': 0, 'notFound': 0, 'failed': 0, 'mismatch': 0, 'requests': 0}
            if res.dry_run:
                return res
            self.state = STATE_FETCH
            with DownloadIndex(self.index_path, create=True) as writer:
                self._writer = writer
                try:
                    for module, mfiles in by_module.items():
                        if self._cancelled():
                            break
                        self._import_module(module, mfiles)
                finally:
                    self._flush()
                    self._writer = None
        except Exception as err:
            res.error = f'{type(err).__name__}: {err}'
        finally:
            self.state = STATE_DONE
            self.module = ''
            me.killed = False
        return res

    def _scan(self) -> list[LibraryFile]:
        res = self.result
        files: list[LibraryFile] = []
        for f in scan_library(self.folder, recursive=self.recursive, cancel=self.cancel):
            if len(files) >= IMPORT_FILES_MAX:
                res.truncated = True
                break
            files.append(f)
            res.scanned += 1
        with DownloadIndex(self.index_path) as index:
            known = index.known_paths(f.path for f in files)
        res.known = sum(1 for f in files if f.path in known)
        return [f for f in files if f.path not in known]

    def _installed(self, abbr: str) -> bool:
        from .downloaders import DOWNLOADERS_BY_PROC_MODULE
        from .extensions import Extensions
        pid = ProcModule.PROC_MODULES_BY_NAME.get(abbr)
        if pid is None:
            return False
        if pid not in DOWNLOADERS_BY_PROC_MODULE and self.factory is None and Extensions.catalog_autoload():
            Extensions.ensure_loaded(include_catalog=True)
        return self.factory is not None or pid in DOWNLOADERS_BY_PROC_MODULE

    def _identify(self, files: list[LibraryFile]) -> list[LibraryFile]:
        res = self.result
        todo: list[LibraryFile] = []
        installed: dict[str, bool] = {}
        for f in files:
            it = identify_file_name(f.name, self.default_module, {})
            module = it['module']
            if not it['kind']:
                note = it['note'] or 'not recognized'
            elif not module:
                note = f'no module prefix in the name ({it["kind"]} {it["value"]}): choose the module of such names'
            elif not installed.setdefault(module, self._installed(module)):
                note = f'module \'{module}\' is not installed'
            else:
                f.module, f.kind, f.value = module, it['kind'], it['value']
                todo.append(f)
                continue
            res.unknown += 1
            _note(res.unknown_names, f'{f.rel}: {note}')
        return todo

    # ----- one module -----

    def _open(self, abbr: str, first_query: str):
        from .cmdargs import prepare_arglist
        from .downloaders import _make_downloader
        pid = ProcModule.PROC_MODULES_BY_NAME[abbr]
        ProcModule.set(pid)
        dwn = self.factory(pid) if self.factory is not None else _make_downloader(pid)
        try:
            args = prepare_arglist([*self.common_args, *self.module_args.get(abbr, []), '-module', abbr, '--', first_query])
        except SystemExit as err:
            with contextlib.suppress(Exception):
                dwn.__exit__(None, None, None)
            raise ValueError(f'invalid arguments for module \'{abbr}\' (code {err.code})') from None
        dwn.reset_root_thread(current_process())
        dwn.no_conflict_delay = True
        dwn._parse_args(args, False)
        return dwn

    def _units(self, abbr: str, sep: str, files: list[LibraryFile]) -> list[tuple[str, str, list[str]]]:
        """(query, kind, values): danbooru id / md5 lists, else one query per post"""
        values: dict[str, list[str]] = {'id': [], 'md5': []}
        for f in files:
            if f.value not in values[f.kind]:
                values[f.kind].append(f.value)
        values['id'].sort(key=lambda v: (len(v), v))
        units: list[tuple[str, str, list[str]]] = []
        for kind in ('id', 'md5'):
            if abbr in ID_LIST_MODULES:
                for i in range(0, len(values[kind]), ID_LIST_CHUNK):
                    chunk = values[kind][i:i + ID_LIST_CHUNK]
                    units.append((f'{kind}{sep}{",".join(chunk)}', kind, chunk))
            else:
                units.extend((f'{kind}{sep}{v}', kind, [v]) for v in values[kind])
        return units

    def _import_module(self, abbr: str, files: list[LibraryFile]) -> None:
        res = self.result
        self.module = abbr
        by_value: dict[tuple[str, str], list[LibraryFile]] = {}
        for f in files:
            by_value.setdefault((f.kind, f.value), []).append(f)
        try:
            dwn = self._open(abbr, f'{files[0].kind}:{files[0].value}')
        except Exception as err:
            self.log(f'Import: module \'{abbr}\' cannot be used: {err}')
            self._fail(abbr, files, f'module error: {err}')
            return
        with dwn:
            units = self._units(abbr, dwn._get_idval_equal_seaparator() or ':', files)
            self.log(f'Import: {len(files):d} file(s) of \'{abbr}\', {len(units):d} request(s)')
            streak = 0
            for n, (query, kind, values) in enumerate(units):
                if self._cancelled():
                    break
                unit_files = [f for v in values for f in by_value[(kind, v)]]
                try:
                    items = self._fetch(dwn, query, len(values))
                    if items is not None:
                        self._match(dwn, abbr, kind, values, items, by_value)
                except ThreadInterruptException:
                    if self.cancel.is_set() or dwn.is_killed():
                        res.cancelled = True
                        break
                    items = None
                except Exception as err:
                    self.log(f'Import: {abbr} \'{query[:80]}\': {type(err).__name__}: {err}')
                    items = None
                if items is None:
                    self._fail(abbr, unit_files, 'request failed')
                    streak += 1
                    if dwn.cf_challenge_hit or streak >= IMPORT_FAIL_STREAK:
                        why = 'bot challenge' if dwn.cf_challenge_hit else f'{streak:d} failed requests in a row'
                        rest = [f for _, k, vs in units[n + 1:] for v in vs for f in by_value[(k, v)]]
                        self.log(f'Import: module \'{abbr}\' given up ({why}), {len(rest):d} file(s) left')
                        self._fail(abbr, rest, why)
                        break
                    continue
                streak = 0
                if len(self._pending) >= IMPORT_WRITE_BATCH:
                    self._flush()
        self._flush()

    def _wait(self, seconds: float) -> bool:
        """Sleeps unless cancelled meanwhile (True)"""
        return self.cancel.wait(seconds) if seconds > 0 else self.cancel.is_set()

    def _fetch(self, dwn, query: str, expected: int) -> list[str] | None:
        interval = max(self.min_interval, float(dwn._get_min_request_interval() or 0))
        if self._wait(self._last_request + interval - time.monotonic()):
            raise ThreadInterruptException
        self._last_request = time.monotonic()
        self.result.requests += 1
        self.result.modules[self.module]['requests'] += 1
        tags = dwn._consume_custom_module_tags(query)
        if _takes_page_size(type(dwn)):
            page = max(1, min(int(dwn._get_items_per_page()), max(expected, IMPORT_SINGLE_PAGE)))
            dwn.url = dwn._form_tags_search_address(tags, page)
        else:
            dwn.url = dwn._form_tags_search_address(tags)
        raw = dwn.fetch_html(dwn._form_page_num_address(0))
        self._last_request = time.monotonic()
        if raw is None:
            return None
        return [str(h) for h in dwn._get_all_post_tags(raw.raw if dwn._parses_raw_pages() else raw.html)]

    def _match(self, dwn, abbr: str, kind: str, values: list[str], items: list[str],
               by_value: dict[tuple[str, str], list[LibraryFile]]) -> None:
        res = self.result
        stats = res.modules[abbr]
        wanted = set(values)
        found: set[str] = set()
        broken = ''
        for h in items:
            try:
                post_id = str(dwn._extract_id(dwn._local_addr_from_string(h)))
                info = dwn._extract_item_info(h)  # modules without infos in listings fetch the post page here
            except ThreadInterruptException:
                raise
            except Exception as err:
                broken = f'{type(err).__name__}: {err}'
                continue
            md5 = norm_md5(info.md5)
            if kind == 'id':
                key = str(int(post_id)) if post_id.isdigit() else post_id
            else:  # a site that does not report md5 in listings: the only result of a single md5 search is that post
                key = md5 or (values[0] if len(values) == 1 and len(items) == 1 else '')
            if key not in wanted or key in found:
                continue
            found.add(key)
            for f in by_value[(kind, key)]:
                if self.verify_md5 and md5:
                    try:
                        same = file_md5(f.path) == md5
                    except OSError:
                        same = False
                    if not same:
                        res.mismatch += 1
                        stats['mismatch'] += 1
                        _note(res.mismatch_names, f'{f.rel} ({abbr} {post_id})')
                        continue
                rec = record_for(abbr, info, post_id, f.path, f.size, h)
                rec.downloaded_at = f.mtime  # the file's own time: the library page sorts old files as old
                self._pending.append(rec)
                res.imported += 1
                stats['imported'] += 1
        missing = [v for v in values if v not in found]
        if broken:  # a post of the answer could not be read: its files may be among these
            self.log(f'Import: {abbr}: unreadable post in the answer: {broken}')
            self._fail(abbr, [f for v in missing for f in by_value[(kind, v)]], f'unreadable answer: {broken}')
            return
        for v in missing:
            for f in by_value[(kind, v)]:
                res.not_found += 1
                stats['notFound'] += 1
                _note(res.not_found_names, f'{f.rel} ({abbr} {kind} {v})')

    def _fail(self, abbr: str, files: list[LibraryFile], why: str) -> None:
        res = self.result
        for f in files:
            res.failed += 1
            res.modules[abbr]['failed'] += 1
            _note(res.failed_names, f'{f.rel} ({abbr} {f.kind} {f.value}): {why}')

    def _flush(self) -> None:
        if not self._pending or self._writer is None:
            return
        batch, self._pending = self._pending, []
        for attempt in range(1, WRITE_ATTEMPTS + 1):
            try:
                self._writer.add_many(batch)
                return
            except sqlite3.OperationalError as err:
                if ('locked' not in str(err) and 'busy' not in str(err)) or attempt == WRITE_ATTEMPTS:
                    raise


# ----- command line -----

def _pop_module_args() -> dict[str, list[str]]:
    raw = os.environ.pop(ENV_SECRET_MODULE_ARGS, '')
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        assert isinstance(data, dict)
        assert all(isinstance(k, str) and isinstance(v, list) and all(isinstance(x, str) for x in v) for k, v in data.items())
        return data
    except Exception:
        print(f'ERROR: {ENV_SECRET_MODULE_ARGS} must be a JSON object of string lists', file=sys.stderr)
        return {}


def _print_names(title: str, names: list[str], total: int) -> None:
    if not total:
        return
    trace(f'{title} ({total:d}):', True)
    for name in names[:IMPORT_NAMES_LOG]:
        trace(f'  {name}', True)
    if total > IMPORT_NAMES_LOG:
        trace(f'  ... and {total - IMPORT_NAMES_LOG:d} more', True)


def _summary(res: ImportResult) -> str:
    parts = [f'{res.scanned:d} media file(s)', f'{res.known:d} already indexed', f'{res.unknown:d} not recognized']
    if res.dry_run:
        per_module = ', '.join(f'{m}: {st["files"]:d}' for m, st in res.modules.items()) or 'none'
        parts.append(f'{res.identified:d} to look up ({per_module})')
    else:
        parts += [f'{res.imported:d} imported', f'{res.not_found:d} not found on the website', f'{res.failed:d} failed',
                  f'{res.requests:d} request(s)']
        if res.mismatch:
            parts.append(f'{res.mismatch:d} md5 mismatch')
    return f'Import of {res.folder}: {", ".join(parts)}{" (cancelled)" if res.cancelled else ""}{f". Error: {res.error}" if res.error else ""}'


def _run_import(ns: argparse.Namespace) -> int:
    from .lynxx_cmd import ENV_IPC, IPC_PROGRESS_PREFIX, _pop_secret_args
    ipc = os.environ.get(ENV_IPC) == '1'
    imp = LibraryImport(ns.folder, default_module=ns.module or '', recursive=not ns.no_recursive, dry_run=ns.dry_run,
                        verify_md5=ns.verify_md5, common_args=_pop_secret_args(), module_args=_pop_module_args())

    def emit(state: dict) -> None:
        with Logger.print_lock:
            print(f'{IPC_PROGRESS_PREFIX}{json.dumps(state, separators=(",", ":"))}', flush=True)

    done = threading.Event()
    reporter: threading.Thread | None = None
    if ipc:
        def report() -> None:
            last = ''
            while not done.is_set():
                state = imp.progress()
                line = json.dumps(state, sort_keys=True)
                if line != last:
                    emit(state)
                    last = line
                done.wait(0.4)

        def watch_stdin() -> None:
            for line in sys.stdin:
                if line.strip() == 'cancel':
                    imp.cancel.set()
                    current_process().killed = True  # a request in progress stops too
                    break

        reporter = threading.Thread(target=report, daemon=True, name='ipc-progress')
        reporter.start()
        threading.Thread(target=watch_stdin, daemon=True, name='ipc-stdin').start()
    trace(f'Import: scanning {imp.folder}{"" if imp.recursive else " (no subfolders)"}', True)
    res = imp.run()
    done.set()
    if reporter:
        # a state the reporter read before the end must not be printed after the final DONE line: the app merges the lines in
        # order, an older one would overwrite the final counts
        reporter.join(10)
    _print_names('Not recognized', res.unknown_names, res.unknown)
    _print_names('Not found on the website', res.not_found_names, res.not_found)
    _print_names('Failed', res.failed_names, res.failed)
    _print_names('md5 mismatch (not imported)', res.mismatch_names, res.mismatch)
    trace(_summary(res), True)
    code = res.exit_code()
    if ipc:
        emit({**imp.progress(), **res.as_dict(), 'stateName': STATE_DONE, 'ok': code in (EXIT_OK, EXIT_PARTIAL), 'exitCode': code})
    if ns.json:
        print(json.dumps({**res.as_dict(IMPORT_NAMES_KEEP), 'exitCode': code}, ensure_ascii=False))
    return code


def _run_clean(ns: argparse.Namespace) -> int:
    last = [0.0]

    def progress(r: CleanResult) -> None:
        if time.monotonic() - last[0] >= 1.0:
            last[0] = time.monotonic()
            print(f'checked {r.checked:d} / {r.total:d}, missing {r.missing:d}, removed {r.removed:d}', file=sys.stderr, flush=True)

    res = clean_index(include_missing_dirs=ns.missing_folders, dry_run=ns.dry_run, progress=progress)
    if ns.json:
        print(json.dumps(res.as_dict(), ensure_ascii=False))
    else:
        removable = res.missing - (0 if res.include_missing_dirs else res.missing_dirs)
        done = f'would remove {removable:d}' if res.dry_run else f'removed {res.removed:d}'
        kept = f', {res.missing_dirs:d} row(s) kept: their folder is gone too (--missing-folders removes them)' \
            if res.missing_dirs and not res.include_missing_dirs else ''
        print(f'Index clean: {res.checked:d} row(s) checked, {res.missing:d} file(s) missing, {done} row(s){kept}. '
              'Files on disk are never touched.')
        for folder in res.folders:
            print(f'  missing folder: {folder}')
    return EXIT_OK


def _when(ts: float | None) -> str:
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(ts)) if ts else '-'


def _run_trash(ns: argparse.Namespace) -> int:
    if ns.trash_command == 'list':
        items = list(trash_items())
        if ns.json:
            print(json.dumps({'total': len(items), 'items': items}, ensure_ascii=False))
            return EXIT_OK
        for it in items:
            print(f'{_when(it["trashedAt"])}  {it["module"]} {it["id"]}  {it["origPath"]}{"" if it["exists"] else "  (file gone)"}')
            print(f'    in the trash: {it["path"]}')
        size = sum(int(it['size'] or 0) for it in items)
        print(f'Trash: {len(items):d} file(s), {size / 1048576:.1f} MB')
        return EXIT_OK
    if ns.days is not None and ns.days < 0:
        print('ERROR: --days must be 0 or more', file=sys.stderr)
        return EXIT_ARGS
    res = purge_trash(days=ns.days, everything=ns.all)
    if ns.json:
        print(json.dumps(res, ensure_ascii=False))
    else:
        which = 'every file' if ns.all else f'files trashed more than {ns.days:d} day(s) ago'
        print(f'Trash purge ({which}): {res["files"]:d} file(s) deleted ({res["bytes"] / 1048576:.1f} MB), {res["purged"]:d} row(s) '
              f'removed, {len(res["failed"]):d} failed')
        for f in res['failed']:
            print(f'  not deleted: {f["path"]}: {f["error"]}')
    return EXIT_PARTIAL if res['failed'] else EXIT_OK


RETAG_TYPES = ('general', 'copyright', 'artist', 'character', 'metadata')


def _run_retag(ns: argparse.Namespace) -> int:
    from .downloaders import _make_downloader
    from .extensions import Extensions
    from .file_parser import gather_item_infos_in_dir
    from .file_tagger import item_infos_from_index, retag_files_any, retag_files_tagtype
    from .tags import TagCategories
    from .tagsdb import TagsDB
    types = [t.strip().lower() for t in ns.types.split(',') if t.strip()]
    if types == ['auto']:
        categories = [TagCategories.AUTO]
    elif types and all(t in RETAG_TYPES for t in types):
        categories = [TagCategories[t.upper()] for t in types]
    else:
        print(f'ERROR: --types: auto, or a list of {", ".join(RETAG_TYPES)}', file=sys.stderr)
        return EXIT_ARGS
    files: list[pathlib.Path] = []
    for raw in ns.paths:
        p = pathlib.Path(raw).expanduser().absolute()
        if p.is_dir():
            files.extend(sorted(x for x in p.iterdir() if x.is_file() and x.suffix[1:].lower() in MEDIA_EXTENSIONS))
        elif p.is_file():
            files.append(p)
        else:
            print(f'ERROR: not found: {p}', file=sys.stderr)
            return EXIT_ARGS
    if not files:
        print('Nothing to re-tag: no media files')
        return EXIT_OK
    Extensions.ensure_loaded()
    pid = ProcModule.PROC_MODULES_BY_NAME.get(ns.module)
    if pid is None:
        print(f'ERROR: unknown module \'{ns.module}\'', file=sys.stderr)
        return EXIT_ARGS
    if categories != [TagCategories.AUTO]:
        TagsDB.locate_default_lists()
        if TagsDB.is_empty():
            print('ERROR: re-tag by tag type needs the tag lists (2tags/ or <data dir>/tags)', file=sys.stderr)
            return EXIT_ERROR
    with _make_downloader(pid) as dwn:
        re_process, re_exclude = dwn.get_re_tags_to_process(), dwn.get_re_tags_to_exclude()
    by_folder: dict[pathlib.Path, list[pathlib.Path]] = {}
    for f in files:
        by_folder.setdefault(f.parent, []).append(f)  # the tools rename into the folder of the first file
    total = done = 0
    for folder, group in by_folder.items():
        infos = {}
        if ns.source in ('both', 'json'):
            infos.update(gather_item_infos_in_dir(folder))
        index_infos = item_infos_from_index(group) if ns.source in ('both', 'index') else {}
        infos.update(index_infos)
        if categories == [TagCategories.AUTO]:
            n = retag_files_any(group, infos, re_process, re_exclude)
        else:
            n = retag_files_tagtype(group, infos, *categories)
        total += len(group)
        done += max(0, n)
        print(f'{folder}: re-tagged {max(0, n):d} / {len(group):d} file(s)'
              f' (infos: {len(index_infos):d} from the index, {len(infos) - len(index_infos):d} from info files)'
              f'{"; no post info found" if n == -1 else ""}')
    return EXIT_OK if done == total else EXIT_PARTIAL if done else EXIT_ERROR


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog='python -m lynxx.dlindex_tools', description='Lynxx download index tools')
    sub = ap.add_subparsers(dest='command', required=True)
    c = sub.add_parser('clean', help='drop rows whose file is no longer on disk (files are never touched)')
    c.add_argument('--dry-run', action='store_true', help='only count')
    c.add_argument('--missing-folders', action='store_true', help='also drop rows whose folder is gone (unplugged drive: plug it in first)')
    c.add_argument('--json', action='store_true')
    i = sub.add_parser('import', help='record media files already on disk (metadata from the websites)')
    i.add_argument('folder')
    i.add_argument('--module', default='', help='module of names without a module prefix (plain post ids, md5 names)')
    i.add_argument('--no-recursive', action='store_true', help='only the folder itself')
    i.add_argument('--verify-md5', action='store_true', help='read each file and import it only if it is the post\'s original file')
    i.add_argument('--dry-run', action='store_true', help='only recognize names, no request')
    i.add_argument('--json', action='store_true')
    r = sub.add_parser('retag', help='append score and tags to Lynxx file names, tags from the index (info files as fallback)')
    r.add_argument('paths', nargs='+', help='media files or folders (files directly inside)')
    r.add_argument('--module', default='rx', help='module whose tag filters are used (--types auto)')
    r.add_argument('--types', default='auto', help=f'auto, or tags of these types only: {",".join(RETAG_TYPES)} (needs tag lists)')
    r.add_argument('--source', choices=('both', 'index', 'json'), default='both')
    t = sub.add_parser('trash', help='files the desktop app deleted (kept in a trash folder until purged)')
    tsub = t.add_subparsers(dest='trash_command', required=True)
    tl = tsub.add_parser('list', help='files in the trash, most recently deleted first')
    tl.add_argument('--json', action='store_true')
    tp = tsub.add_parser('purge', help='delete files of the trash for good (and their rows)')
    which = tp.add_mutually_exclusive_group(required=True)
    which.add_argument('--days', type=int, help='files deleted more than N days ago (0: all of them)')
    which.add_argument('--all', action='store_true', help='every file of the trash')
    tp.add_argument('--json', action='store_true')
    return ap


def main(argv: Sequence[str] | None = None) -> int:
    Logger.init(True)
    ns = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    if ns.command == 'clean':
        return _run_clean(ns)
    if ns.command == 'import':
        return _run_import(ns)
    if ns.command == 'trash':
        return _run_trash(ns)
    return _run_retag(ns)


if __name__ == '__main__':
    sys.exit(main())

#
#
#########################################
