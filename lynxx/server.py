# coding=UTF-8
"""
Lynxx desktop sidecar: JSON-RPC 2.0 over stdio (one JSON object per line), started by the Electron main process
(python -m lynxx.server, or 'lynxx-core server' in the packaged app).
It owns the library (SQLite + files), converts imported files, reads chapters for the reader and builds exports.
Extension scripts never run here: they run in Chromium sandboxes of the main process (app/plugin), which hands the
downloaded files to the methods below.
"""

from __future__ import annotations

import datetime
import io
import json
import pathlib
import platform
import re
import sys
import threading
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# protocol channel must stay clean: everything else printed to stdout goes to stderr
_PROTO_OUT = sys.stdout
sys.stdout = sys.stderr

from .export import service as export_service  # noqa: E402
from .export.pdf import NeedsTranscode  # noqa: E402
from .formats import SUPPORTED_EXTENSIONS, FormatError  # noqa: E402
from .library import LibraryDB  # noqa: E402
from .library import books as library_books  # noqa: E402
from .library.importer import import_document, upgrade_legacy  # noqa: E402
from .paths import data_dir  # noqa: E402
from .policy import Operation, list_sources, source_status  # noqa: E402
from .vcs import APP_NAME, APP_VERSION  # noqa: E402

__all__ = ('main',)

PROTOCOL_VERSION = 2
APP_ADDRESS = 'https://github.com/Leiz-Workspace/Lynxx'

_out_lock = threading.Lock()


class RpcError(Exception):
    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


def _send(obj: dict) -> None:
    line = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
    with _out_lock:
        _PROTO_OUT.write(line + '\n')
        _PROTO_OUT.flush()


def notify(method: str, params: dict) -> None:
    _send({'jsonrpc': '2.0', 'method': method, 'params': params})


re_secret_query = re.compile(r'([?&](?:api_key|apikey|token|access_token|key|auth|sig|signature|password)=)[^&\s"\']+', re.IGNORECASE)
re_secret_header = re.compile(r'((?:authorization|cookie|x-api-key)\s*[:=]\s*)[^\n"\']+', re.IGNORECASE)


def _redact(text: str) -> str:
    """Error messages and traces reach the UI: credentials in a URL query or a header line are hidden"""
    return re_secret_header.sub(r'\1<REDACTED>', re_secret_query.sub(r'\1<REDACTED>', str(text)))


LIBRARY_DB_NAME = 'index/library.sqlite'


LIBRARY_ROOT_NAME = 'library'


PLUGIN_ZIP_MAX = 20 * 1024 * 1024


PLUGIN_FILE_MAX = 10 * 1024 * 1024


PLUGIN_TOTAL_MAX = 60 * 1024 * 1024
re_plugin_member = re.compile(r'^(?:[^/]{1,120}/)?(?:plugin\.json|icon\.png|src/[A-Za-z0-9_.-]{1,80}\.js)$')
'''the files of an extension package: anything else in the archive is skipped'''


def m_source_list(_: dict) -> list[dict[str, object]]:
    return list_sources()


def m_source_policy(p: dict) -> dict[str, object]:
    source_id, url, operation = p.get('sourceId'), p.get('url'), p.get('operation')
    if not all(isinstance(value, str) and value for value in (source_id, url, operation)):
        raise RpcError(-32602, 'sourceId, url and operation are required')
    try:
        op = Operation(operation)
    except ValueError as err:
        raise RpcError(-32602, f'unsupported operation: {operation}') from err
    decision = source_status(source_id, url, op)
    return {'allowed': decision.allowed, 'decision': decision.decision, 'reason': decision.reason, 'policyVersion': decision.policy_version}


def library_db() -> LibraryDB:
    return LibraryDB(data_dir() / LIBRARY_DB_NAME)


def library_root() -> pathlib.Path:
    root = (data_dir() / LIBRARY_ROOT_NAME).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _str_param(p: dict, key: str, required: bool = True, limit: int = 4096) -> str:
    value = p.get(key, '')
    if not isinstance(value, str) or (required and not value) or len(value) > limit:
        raise RpcError(-32602, f'{key} is required' if required else f'{key} must be a string')
    return value


def _with_library(fn: Callable[[LibraryDB, pathlib.Path], Any]) -> Any:
    db = library_db()
    try:
        return fn(db, library_root())
    except FormatError as err:
        raise RpcError(-32001, f'Không đọc được file: {err}') from err
    finally:
        db.close()


def m_formats_supported(_: dict) -> dict:
    return {'extensions': list(SUPPORTED_EXTENSIONS)}


def m_library_collections(_: dict) -> list[dict]:
    return _with_library(lambda db, root: library_books.list_collections(db, root))


def m_library_collection(p: dict) -> dict:
    cid = _str_param(p, 'collectionId', limit=100)
    return _with_library(lambda db, root: library_books.collection_detail(db, root, cid))


def m_library_collection_delete(p: dict) -> dict:
    cid = _str_param(p, 'collectionId', limit=100)
    return {'ok': _with_library(lambda db, root: library_books.delete_collection(db, root, cid))}


def m_library_collection_remote(p: dict) -> list[dict]:
    cid = _str_param(p, 'collectionId', limit=100)
    return _with_library(lambda db, root: library_books.remote_chapters(db, cid))


def m_library_book_upsert(p: dict) -> dict:
    detail = p.get('detail') if isinstance(p.get('detail'), dict) else {}
    chapters = p.get('chapters') if isinstance(p.get('chapters'), list) else []
    return _with_library(lambda db, root: library_books.upsert_book(
        db, root, plugin_id=_str_param(p, 'pluginId', limit=60), plugin_name=_str_param(p, 'pluginName', False, 200),
        source=_str_param(p, 'source', False, 600), url=_str_param(p, 'url', limit=4000), kind=_str_param(p, 'kind', False, 20),
        detail=detail, chapters=[c for c in chapters if isinstance(c, dict)]))


def m_library_book_cover(p: dict) -> dict:
    return _with_library(lambda db, root: library_books.set_cover(db, root, _str_param(p, 'collectionId', limit=100), _str_param(p, 'path'),
                                                                 _str_param(p, 'sourceUrl', False)))


def m_library_chapter_commit_images(p: dict) -> dict:
    pages = p.get('pages') if isinstance(p.get('pages'), list) else []
    return _with_library(lambda db, root: library_books.commit_images(db, root, _str_param(p, 'chapterId', limit=100), [x for x in pages if isinstance(x, dict)]))


def m_library_chapter_commit_text(p: dict) -> dict:
    images = p.get('images') if isinstance(p.get('images'), list) else []
    html = p.get('html')
    if not isinstance(html, str) or len(html) > 20 * 1024 * 1024:
        raise RpcError(-32602, 'html must be a string')
    return _with_library(lambda db, root: library_books.commit_text(db, root, _str_param(p, 'chapterId', limit=100), html,
                                                                   _str_param(p, 'title', False, 400), [x for x in images if isinstance(x, dict)]))


def m_library_chapter_state(p: dict) -> dict:
    _with_library(lambda db, root: library_books.set_chapter_state(db, _str_param(p, 'chapterId', limit=100), _str_param(p, 'state', limit=20),
                                                                   _str_param(p, 'error', False, 1000)))
    return {'ok': True}


def m_reader_chapter(p: dict) -> dict:
    chapter_id = _str_param(p, 'chapterId', limit=100)

    def read(db: LibraryDB, root: pathlib.Path) -> dict:
        upgraded = upgrade_legacy(db, root, chapter_id)
        target = str(upgraded['chapterId']) if upgraded else chapter_id
        return {**library_books.read_chapter(db, root, target), 'upgradedFrom': chapter_id if upgraded else ''}
    return _with_library(read)


def m_export_suggest(p: dict) -> dict:
    return {'name': _with_library(lambda db, root: export_service.suggested_name(db, root, _str_param(p, 'collectionId', limit=100), _str_param(p, 'format', limit=10)))}


def _export_output(p: dict) -> pathlib.Path:
    out = pathlib.Path(_str_param(p, 'output')).expanduser()
    if not out.is_absolute() or out.suffix.lower() not in {'.epub', '.cbz', '.pdf', '.html'}:
        raise RpcError(-32602, 'output must be an absolute .epub / .cbz / .pdf path')
    return out


def m_export_build(p: dict) -> dict:
    replacements = p.get('replacements') if isinstance(p.get('replacements'), dict) else None
    options = p.get('options') if isinstance(p.get('options'), dict) else {}
    try:
        return _with_library(lambda db, root: export_service.export_collection(db, root, _str_param(p, 'collectionId', limit=100), _str_param(p, 'format', limit=10),
                                                                              options, _export_output(p), replacements))
    except export_service.NeedsPrint as err:
        raise RpcError(-32010, str(err), {'needsPrint': True}) from err
    except NeedsTranscode as err:
        raise RpcError(-32011, str(err), {'transcode': err.paths[:5000]}) from err


def m_export_bundle(p: dict) -> dict:
    options = p.get('options') if isinstance(p.get('options'), dict) else {}
    return _with_library(lambda db, root: export_service.html_bundle(db, root, _str_param(p, 'collectionId', limit=100), options, _export_output(p)))


def m_export_record(p: dict) -> dict:
    options = p.get('options') if isinstance(p.get('options'), dict) else {}
    ids = [str(x) for x in p.get('chapterIds') or [] if isinstance(x, str)]
    return _with_library(lambda db, root: export_service.record_export(db, _str_param(p, 'collectionId', limit=100), _str_param(p, 'format', limit=10),
                                                                      _export_output(p), export_service.clean_options(options), ids))


def m_export_auto_targets(p: dict) -> list[dict]:
    return _with_library(lambda db, root: export_service.auto_update_exports(db, _str_param(p, 'collectionId', limit=100)))


def m_export_forget(p: dict) -> dict:
    return {'ok': _with_library(lambda db, root: export_service.forget_export(db, _str_param(p, 'exportId', limit=100)))}


def m_plugins_unpack(p: dict) -> dict:
    """Extract an extension archive: only plugin.json, icon.png and src/*.js (optionally under one folder)"""
    import zipfile
    archive = pathlib.Path(_str_param(p, 'archive')).expanduser().resolve(strict=True)
    dest = pathlib.Path(_str_param(p, 'dest')).expanduser().resolve()
    if archive.stat().st_size > PLUGIN_ZIP_MAX:
        raise RpcError(-32001, 'file extension quá lớn')
    if dest.exists():
        raise RpcError(-32602, 'dest must not exist')
    written = 0
    total = 0
    try:
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist()[:5000]:
                name = info.filename.replace('\\', '/')
                if info.is_dir() or name.startswith('__MACOSX/') or '/.' in f'/{name}':
                    continue
                if not re_plugin_member.match(name) or '..' in name.split('/'):
                    continue
                if ((info.external_attr >> 16) & 0o170000) == 0o120000 or info.flag_bits & 0x1:
                    continue
                if info.file_size > PLUGIN_FILE_MAX:
                    raise RpcError(-32001, f'file quá lớn trong extension: {name}')
                total += info.file_size
                if total > PLUGIN_TOTAL_MAX:
                    raise RpcError(-32001, 'extension giải nén quá lớn')
                target = (dest / name).resolve()
                if dest not in target.parents:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src:
                    data = src.read(PLUGIN_FILE_MAX + 1)
                if len(data) > PLUGIN_FILE_MAX:
                    raise RpcError(-32001, f'file quá lớn trong extension: {name}')
                target.write_bytes(data)
                written += 1
    except zipfile.BadZipFile as err:
        raise RpcError(-32001, 'file không phải .zip hợp lệ') from err
    if not written:
        raise RpcError(-32001, 'archive không có plugin.json / src/*.js của extension')
    return {'ok': True, 'files': written}


def m_library_import(p: dict) -> dict:
    source = _str_param(p, 'source')
    title = _str_param(p, 'title', False, 500)
    return _with_library(lambda db, root: import_document(source, root, db, title))


def m_reader_progress_get(p: dict) -> dict | None:
    chapter_id = p.get('chapterId')
    if not isinstance(chapter_id, str) or not chapter_id:
        raise RpcError(-32602, 'chapterId is required')
    db = library_db()
    try:
        return db.reader_progress(chapter_id)
    finally:
        db.close()


def m_reader_progress_set(p: dict) -> dict:
    chapter_id = p.get('chapterId')
    if not isinstance(chapter_id, str) or not chapter_id:
        raise RpcError(-32602, 'chapterId is required')
    db = library_db()
    try:
        db.save_reader_progress(chapter_id, int(p.get('page', 0)), float(p.get('scrollOffset', 0)),
                                str(p.get('mode', 'vertical')), float(p.get('zoom', 1)), str(p.get('updatedAt', datetime.datetime.now(datetime.timezone.utc).isoformat())),
                                str(p.get('direction', 'ltr')))
        return {'ok': True}
    finally:
        db.close()


def _handle(req: dict) -> None:
    rid = req.get('id')
    method = req.get('method', '')
    try:
        fn = METHODS.get(method)
        if fn is None:
            raise RpcError(-32601, f'method not found: {method}')
        result = fn(req.get('params') or {})
        if rid is not None:
            _send({'jsonrpc': '2.0', 'id': rid, 'result': result})
    except RpcError as err:
        if rid is not None:
            data = err.data
            if isinstance(data, dict) and isinstance(data.get('log'), list):
                data = {**data, 'log': [_redact(x) for x in data['log']]}
            _send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': err.code, 'message': _redact(err.message), 'data': data}})
    except Exception as err:
        if rid is not None:
            _send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32000, 'message': _redact(str(err) or type(err).__name__),
                                                            'data': {'trace': _redact(traceback.format_exc(limit=6))}}})


def m_app_info(_: dict) -> dict:
    return {
        'name': APP_NAME, 'version': APP_VERSION, 'protocol': PROTOCOL_VERSION, 'upstream': APP_ADDRESS,
        'python': platform.python_version(), 'dataDir': data_dir().as_posix(),
    }


METHODS: dict[str, Callable[[dict], Any]] = {
    'app.info': m_app_info,
    'formats.supported': m_formats_supported,
    'source.list': m_source_list,
    'source.policy': m_source_policy,
    'library.import': m_library_import,
    'library.collections': m_library_collections,
    'library.collection': m_library_collection,
    'library.collection.delete': m_library_collection_delete,
    'library.collection.remote': m_library_collection_remote,
    'library.book.upsert': m_library_book_upsert,
    'library.book.cover': m_library_book_cover,
    'library.chapter.commitImages': m_library_chapter_commit_images,
    'library.chapter.commitText': m_library_chapter_commit_text,
    'library.chapter.state': m_library_chapter_state,
    'reader.chapter': m_reader_chapter,
    'reader.progress.get': m_reader_progress_get,
    'reader.progress.set': m_reader_progress_set,
    'export.suggest': m_export_suggest,
    'export.build': m_export_build,
    'export.bundle': m_export_bundle,
    'export.record': m_export_record,
    'export.autoTargets': m_export_auto_targets,
    'export.forget': m_export_forget,
    'plugins.unpack': m_plugins_unpack,
}


def main() -> int:
    notify('ready', m_app_info({}))
    stdin = io.TextIOWrapper(sys.stdin.buffer, encoding='utf-8', errors='replace')
    with ThreadPoolExecutor(max_workers=6, thread_name_prefix='rpc') as pool:
        for line in stdin:
            line = line.strip()
            if not line:
                continue
            try:
                req = json.loads(line)
            except json.JSONDecodeError:
                _send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}})
                continue
            if not isinstance(req, dict):  # batches / scalars are not supported
                _send({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'invalid request'}})
                continue
            if req.get('method') == 'app.shutdown':
                break
            pool.submit(_handle, req)
    return 0


if __name__ == '__main__':
    sys.exit(main())
