# coding=UTF-8
"""
Lynxx desktop sidecar: JSON-RPC 2.0 over stdio (one JSON object per line)
Started by the Electron main process: python -m lynxx.server
Handles short queries (tags, preview, count, extensions, tag list updates). Downloads run as separate CLI processes
(LYNXX_IPC=1 python -m lynxx ...), so module-global core state never mixes between jobs.
"""
#########################################
#
#

from __future__ import annotations

import datetime
import io
import json
import os
import pathlib
import platform
import re
import sys
import threading
import time
import traceback
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from multiprocessing.dummy import current_process
from typing import Any

# protocol channel must stay clean: everything else printed to stdout goes to stderr
_PROTO_OUT = sys.stdout
sys.stdout = sys.stderr

from . import tags_learned  # noqa: E402
from .defines import DATE_MIN_DEFAULT, PREVIEW_PAGE_SIZE_DEFAULT, ModuleCaps, ThreadInterruptException  # noqa: E402, F401
from .extensions import ENV_CATALOG_AUTOLOAD, SOURCE_USER, Extensions, data_dir  # noqa: E402
from .gender import decide, infer_from_tags, rules_for_module  # noqa: E402
from .help import APP_ADDRESS  # noqa: E402
from .library import LibraryDB  # noqa: E402
from .library import books as library_books  # noqa: E402
from .library.importer import import_document, upgrade_legacy  # noqa: E402
from .library.epub_reader import epub_image_pages  # noqa: E402
from .export import service as export_service  # noqa: E402
from .export.pdf import NeedsTranscode  # noqa: E402
from .formats import SUPPORTED_EXTENSIONS, FormatError  # noqa: E402
from .logger import Logger  # noqa: E402
from .modspec import re_abbr, re_kind_token  # noqa: E402
from .module import ProcModule  # noqa: E402
from .policy import Operation, list_sources, source_status  # noqa: E402
from .rex import re_api_credentials  # noqa: E402
from .safety import blocked_tag  # noqa: E402
from .search_kinds import id_search_supported, is_module_meta, kind_of, kind_tokens  # noqa: E402
from .tags import TAG_NAMES_BY_TYPE, TagTypes  # noqa: E402
from .tagsdb import TAG_ALIASES, TagsDB, is_wtag, load_tag_aliases, wildcard_pattern  # noqa: E402
from .task import KNOWN_META_NAMES, re_meta_name  # noqa: E402
from .utils import as_date  # noqa: E402
from .vcs import APP_NAME, APP_VERSION  # noqa: E402

__all__ = ('main',)

PROTOCOL_VERSION = 1
ENV_TAGS_DIR = 'LYNXX_TAGS_DIR'
REPO_TAGS_DIR = pathlib.Path(__file__).resolve().parent.parent / '2tags'

_out_lock = threading.Lock()
_core_lock = threading.RLock()
'''core keeps current module in global state: module-dependent calls are serialized.
Tag list methods (autocomplete, browse, status, update) do not take it: TagsDB has its own lock and is keyed by module'''
_tls = threading.local()


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


_ARG_VALUE_END = r'(?=\s+--?[A-Za-z_]|\s+--(?:\s|$)|$)'
'''end of an option's value in a command line printed as text: the next option, the '--' before the tags or the end of the line
(a header or a JSON value may hold spaces)'''
re_secret_args = re.compile(r'(?<![\w-])(-api_key|-proxy|-gender_model_token)(\s+|=)(\S+)')
re_secret_maps = re.compile(rf'(?<![\w-])(-cookies|-headers)(\s+|=)(\S.*?){_ARG_VALUE_END}', re.MULTILINE)
re_secret_pairs = re.compile(rf'(?<![\w-])(-cookie|-header)(\s+|=)([^\s=]+)=(.*?){_ARG_VALUE_END}', re.MULTILINE)
'''-cookie / -header name=value: the name stays readable'''


def _redact(text: str) -> str:
    """Hides credentials in anything that may reach the UI (logs, error messages): api key, proxy, model token, cookies and headers
    (-cookies / -headers JSON, and the name=value pairs of -cookie / -header, whose names are kept)"""
    text = re_api_credentials.sub(r'\1=<REDACTED>', str(text))
    text = re_secret_pairs.sub(r'\1\2\3=<REDACTED>', text)
    text = re_secret_maps.sub(r'\1\2<REDACTED>', text)
    return re_secret_args.sub(r'\1\2<REDACTED>', text)


def _log_sink(text: str) -> None:
    buf: list[str] | None = getattr(_tls, 'log', None)
    text = _redact(text.rstrip('\n'))
    if buf is not None:
        buf.append(text)
    else:
        notify('log', {'text': text})


# ----- tag lists -----

def tags_dirs() -> list[pathlib.Path]:
    """Updated lists (env / data dir) first, bundled lists as fallback"""
    dirs = []
    if env := os.environ.get(ENV_TAGS_DIR):
        dirs.append(pathlib.Path(env).expanduser())
    dirs.append(data_dir() / 'tags')
    dirs.append(REPO_TAGS_DIR)
    return dirs


def _init_tags_db() -> None:
    TagsDB.locate_default_lists()


AUTOCOMPLETE_SCAN_MAX = 50000
'''autocomplete ranks at most this many prefix matches (alphabetically first ones)'''


TAG_TYPES_BY_NAME: dict[str, TagTypes] = {**{str(v): k for k, v in TAG_NAMES_BY_TYPE.items()}, 'meta': TagTypes.TAG_TYPE_META}
'''UI type names ('metadata', 'species'...) -> tag types. The original enum names differ ('TAG_TYPE_META'): metadata filter did nothing'''


def _tag_row(name: str, info) -> dict:
    return {'tag': name, 'posts': info.posts_count, 'type': str(TAG_NAMES_BY_TYPE.get(info.tag_type, 'unknown'))}


RESOLVE_TAGS_MAX = 64
FUZZY_LIMIT_MAX = 50
RELATED_LIMIT_MAX = 200


def _plain_tag(raw: Any) -> str:
    """User input -> tag name: trimmed, lowercase, spaces as underscores, no negation"""
    return '_'.join(str(raw).strip().lower().split()).lstrip('-')


def _is_metatag(tag: str, module: str | None = None) -> bool:
    """A core known metatag name, or (module given) a kind token / metatag name of the module's manifest ('search')"""
    m = re_meta_name.match(tag)
    return bool(m and m.group(1) in KNOWN_META_NAMES) or (module is not None and is_module_meta(tag, module))


KIND_TYPES = ('artist', 'character', 'copyright')
'''tag types offered as search kinds ("Tim theo") when the module's list has tags of that type (tags.kinds)'''


def _types_param(p: dict) -> list[TagTypes]:
    """'types': list of type names ('artist'...) -> tag types; unknown names are dropped (a list of only unknown names: no type)"""
    raw = p.get('types') or []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise RpcError(-32602, 'invalid \'types\'')
    return [TAG_TYPES_BY_NAME[str(t).lower()] for t in raw if str(t).lower() in TAG_TYPES_BY_NAME]


# ----- helpers -----

def _module_id(abbr: str) -> int:
    Extensions.ensure_loaded()
    from .downloaders import DOWNLOADERS_BY_PROC_MODULE
    pid = ProcModule.PROC_MODULES_BY_NAME.get(abbr or '')
    if pid is None or pid not in DOWNLOADERS_BY_PROC_MODULE:
        raise RpcError(-32001, f'module \'{abbr}\' is not installed')
    return pid


def _with_downloader(abbr: str, tags: str, extra_args: list[str], fn: Callable[[Any, Any], Any]) -> Any:
    """Creates a downloader for module and runs fn(downloader, parsed_args) with log capture"""
    from .cmdargs import prepare_arglist
    from .downloaders import _make_downloader
    pid = _module_id(abbr)
    with _core_lock:
        _tls.log = []
        try:
            ProcModule.set(pid)
            args = prepare_arglist(['-module', abbr, *extra_args, '--', *tags.split()])
            with _make_downloader(pid) as dwn:
                me = current_process()
                me.killed = False
                dwn.reset_root_thread(me)
                dwn.no_conflict_delay = True
                return fn(dwn, args)
        except SystemExit as err:  # argparse error
            raise RpcError(-32602, 'invalid arguments', {'log': _tls.log, 'code': err.code})
        except ThreadInterruptException:
            raise RpcError(-32002, 'operation failed', {'log': _tls.log})
        finally:
            log = _tls.log
            _tls.log = None
            if log:
                notify('log', {'text': '\n'.join(log), 'scope': abbr})


LIBRARY_DB_NAME = 'index/library.sqlite'
LIBRARY_ROOT_NAME = 'library'


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


PLUGIN_ZIP_MAX = 20 * 1024 * 1024
PLUGIN_FILE_MAX = 10 * 1024 * 1024
PLUGIN_TOTAL_MAX = 60 * 1024 * 1024
re_plugin_member = re.compile(r'^(?:[^/]{1,120}/)?(?:plugin\.json|icon\.png|src/[A-Za-z0-9_.-]{1,80}\.js)$')


def m_plugins_unpack(p: dict) -> dict:
    """Extract a vBook-format extension archive: only plugin.json, icon.png and src/*.js (optionally under one folder)"""
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
        raise RpcError(-32001, 'archive không có plugin.json / src/*.js của extension vBook')
    return {'ok': True, 'files': written}


def m_library_import(p: dict) -> dict:
    source = _str_param(p, 'source')
    title = _str_param(p, 'title', False, 500)
    return _with_library(lambda db, root: import_document(source, root, db, title))


def m_library_artifacts(p: dict) -> list[dict]:
    chapter_id = p.get('chapterId')
    if not isinstance(chapter_id, str) or not chapter_id:
        raise RpcError(-32602, 'chapterId is required')
    db = library_db()
    try:
        return db.chapter_artifacts(chapter_id)
    finally:
        db.close()


def m_reader_epub_pages(p: dict) -> list[dict[str, str | int]]:
    source = p.get('path')
    if not isinstance(source, str) or not source:
        raise RpcError(-32602, 'path is required')
    candidate = pathlib.Path(source).expanduser().resolve()
    managed_root = (data_dir() / LIBRARY_ROOT_NAME).resolve()
    try:
        candidate.relative_to(managed_root)
    except ValueError as err:
        raise RpcError(-32602, 'reader path is outside the managed library') from err
    return epub_image_pages(candidate)


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


# ----- methods -----

def m_app_info(_: dict) -> dict:
    return {
        'name': APP_NAME, 'version': APP_VERSION, 'protocol': PROTOCOL_VERSION, 'upstream': APP_ADDRESS,
        'python': platform.python_version(), 'dataDir': data_dir().as_posix(),
        'tagsDirs': [d.as_posix() for d in tags_dirs()],
    }


def m_ext_list(_: dict) -> list[dict]:
    return Extensions.list_all()


def m_ext_install(p: dict) -> dict:
    if p.get('id'):
        m = Extensions.install_from_catalog(p['id'])
    elif p.get('remoteId'):  # entry of the signed remote catalog: verified again here, archive pinned by the entry's sha256
        m = Extensions.install_from_remote_catalog(p.get('catalogUrl') or None, p['remoteId'], p.get('sha256', ''))
    elif p.get('url'):
        m = Extensions.install_from_url(p['url'], p.get('sha256', ''))
    elif p.get('archivePath'):
        m = Extensions.install_from_archive_file(pathlib.Path(p['archivePath']), p.get('sha256', ''))
    elif p.get('path'):
        m = Extensions.install_from_dir(pathlib.Path(p['path']))
    else:
        raise RpcError(-32602, 'id, remoteId, url, archivePath or path required')
    TagsDB.locate_default_lists(reload=(m.abbr,))  # a tag list shipped in the extension (the generator's drafts) is used at once
    _refresh_learned(m.abbr)
    return {**m.to_public_dict(), 'restartRequired': Extensions.restart_required(m)}


def m_ext_uninstall(p: dict) -> dict:
    abbr = next((m.abbr for m in Extensions._scan(Extensions.user_dir(), SOURCE_USER) if m.id == p.get('id')), '')
    Extensions.uninstall(p['id'])
    TagsDB.locate_default_lists()  # its shipped tag list is gone with its folder
    _refresh_learned(abbr)
    return {'ok': True}


def _refresh_learned(module: str, recheck: bool = False) -> None:
    """The list the learning adds to came, changed or went with the extension, or a merge update rewrote it (TagSource.merge): the
    learned list is written again for it (the learned tags it now has leave the additions). recheck: see tags_learned.refresh"""
    try:
        if module:
            tags_learned.refresh(module, recheck)
    except Exception as err:
        Logger.log(f'Warning: learned tag list of {module} not refreshed: {err}', False, False)


def m_ext_remote_catalog(p: dict) -> list[dict]:
    return Extensions.fetch_remote_catalog(p.get('url'))


def m_tags_autocomplete(p: dict) -> list[dict]:
    """Thread-safe on its own (TagsDB lock): never waits for _core_lock, so it stays responsive while a preview / count runs.
    types (optional): only tags of these types ('artist'...), picked among the first AUTOCOMPLETE_SCAN_MAX prefix matches.
    token (optional, a search kind's token 'artist:'): list rows named '<token><prefix>' (an extension's job may give
    'category:x' rows); a list without any row of that token gives rows named '<prefix>' instead (a kind whose value is a plain
    tag); 'insert' always carries the token. A list that has rows of the token never offers a plain row with the token in
    front: 'artist:<a plain tag typed artist>' is no page of a site that lists its artists itself (2.0.12)"""
    module, prefix = p.get('module', 'rx'), str(p.get('prefix', '')).lower()
    neg = prefix.startswith('-')
    base = prefix.lstrip('-')
    if not module or len(base) < 1:
        return []
    limit = int(p.get('limit', 12))
    types = _types_param(p)
    if p.get('types') and not types:
        return []  # only types the core does not know: no tag has them
    token = str(p.get('token') or '')
    if token and not re_kind_token.fullmatch(token):
        raise RpcError(-32602, 'invalid \'token\'')
    if token and base.startswith(token):
        base = base[len(token):]
        if not base:
            return []
    sign = '-' if neg else ''
    if token:
        if matches := TagsDB.top_matches(module, f'{token}{base}', limit, scan_max=AUTOCOMPLETE_SCAN_MAX, tag_types=types):
            return [{**_tag_row(k, info), 'insert': f'{sign}{k}'} for k, info in matches]
        if TagsDB.top_matches(module, token, 1, scan_max=1):
            return []  # the kind's values are the list's own '<token>...' rows, none starts with the prefix
    matches = TagsDB.top_matches(module, base, limit, scan_max=AUTOCOMPLETE_SCAN_MAX, tag_types=types)
    return [{**_tag_row(k, info), 'insert': f'{sign}{token}{k}'} for k, info in matches]


def m_tags_kinds(p: dict) -> dict:
    """{module} -> {module, hasList, types: {artist: n, character: n, copyright: n}}: tags of each KIND_TYPES type in the module's
    list in use (counted once per loaded list), only the types it has. The 2.0.11 app offered a type kind ("Nghe si"...) for each;
    the app no longer asks (2.0.12: no picker), command line clients may. Rows named '<token><value>' of the module's kinds
    ('category:x' typed copyright) are not counted: a type kind's value is a
    plain tag, the declared kind offers those. Same locking as tags.autocomplete. The kinds a manifest declares come with ext.list"""
    module = str(p.get('module', ''))
    if not re_abbr.fullmatch(module):
        raise RpcError(-32602, f'invalid module \'{module}\'')
    counts = TagsDB.type_counts(module, kind_tokens(module))
    types = {name: counts[TAG_TYPES_BY_NAME[name]] for name in KIND_TYPES if counts.get(TAG_TYPES_BY_NAME[name], 0) > 0}
    return {'module': module, 'hasList': bool(counts or TagsDB.type_counts(module)), 'types': types}


def m_tags_types(p: dict) -> dict:
    """{module} -> {module, hasList, types: {general: n, artist: n, ..., unknown: n}}: rows of every tag type in the module's list in
    use (counted once per loaded list), only the types it has, by their tags.browse names ('tag' rows are general, rows without a type
    are 'unknown'). Every row counts, '<token><value>' ones included: the A-Z browser lists them. The app enables its type filters
    (tags.browse 'types') by these. Same locking as tags.autocomplete"""
    module = str(p.get('module', ''))
    if not re_abbr.fullmatch(module):
        raise RpcError(-32602, f'invalid module \'{module}\'')
    types: dict[str, int] = {}
    for tag_type, n in TagsDB.type_counts(module).items():
        name = str(TAG_NAMES_BY_TYPE.get(tag_type, 'unknown'))
        types[name] = types.get(name, 0) + n
    return {'module': module, 'hasList': bool(types), 'types': types}


def m_tags_browse(p: dict) -> dict:
    """Same locking as tags.autocomplete. Popularity sorted / filtered results are cached by TagsDB, paging reuses them"""
    module = p.get('module', 'rx')
    prefix = str(p.get('prefix', '')).lower()
    types = [TAG_TYPES_BY_NAME[str(t).lower()] for t in p.get('types', []) if str(t).lower() in TAG_TYPES_BY_NAME]
    total, rows = TagsDB.browse(module, prefix, offset=int(p.get('offset', 0)), limit=int(p.get('limit', 100)), min_posts=int(p.get('minPosts', 0)),
                                tag_types=types, by_posts=p.get('sort') == 'posts', non_alpha=prefix == '#')  # '#': not starting with a latin letter
    return {'total': total, 'items': [_tag_row(*r) for r in rows]}


def m_tags_validate(p: dict) -> dict:
    from .tags_parser import parse_error, parse_tags, reset_last_tags
    abbr = p.get('module', 'rx')
    pid = _module_id(abbr)
    with _core_lock:
        ProcModule.set(pid)
        reset_last_tags()
        ok, tags = parse_tags(str(p.get('tags', '')))
        return {'valid': bool(ok), 'tags': list(tags), 'error': '' if ok else parse_error()}


def m_tags_resolve(p: dict) -> dict:
    """Alias, existence, posts count and type of each tag (one item per input tag, same order, at most RESOLVE_TAGS_MAX).
    all_tag_aliases.json is a cross-site normalization map (the original file tagging: 'oral_sex' -> 'oral', 'male' -> '1+boys'), not the
    site's own aliases: a tag present in the module list is never rewritten, the alias is used only when it leads to a listed tag.
    Wildcard patterns: type 'wildcard', known when some listed tag matches. Same locking as tags.autocomplete"""
    module = p.get('module', 'rx')
    raw = p.get('tags') or []
    tags = [_plain_tag(t) for t in (raw.split() if isinstance(raw, str) else list(raw))[:RESOLVE_TAGS_MAX]]
    load_tag_aliases()
    has_list = TagsDB.count(module) > 0
    items = []
    for tag in tags:
        row = {'tag': tag, 'canonical': tag, 'alias': False, 'posts': 0, 'type': 'unknown', 'known': False}
        items.append(row)
        if not tag:
            continue
        if _is_metatag(tag, module):
            row.update(type='meta', known=True)
            continue
        info = TagsDB.find_tag(module, tag)
        if info is None and (target := TAG_ALIASES.get(tag)) and target != tag and (target_info := TagsDB.find_tag(module, target)):
            info = target_info
            row.update(canonical=target, alias=True)
        if info is not None:
            row.update(known=True, posts=info.posts_count, type=str(TAG_NAMES_BY_TYPE.get(info.tag_type, 'unknown')))
        elif is_wtag(tag):
            row.update(type='wildcard', known=has_list and TagsDB.has_wildcard_match(module, tag))
    return {'items': items, 'hasList': has_list}


def m_tags_fuzzy(p: dict) -> list[dict]:
    """Typo tolerant suggestions: up to 2 edits (1 for texts shorter than 6 chars), missing / extra '_' free ('catears' -> 'cat_ears',
    distance 0), ranked by distance then posts. Same locking as tags.autocomplete; the index is built on the first call per list"""
    module = p.get('module', 'rx')
    limit = max(1, min(FUZZY_LIMIT_MAX, int(p.get('limit') or 8)))
    types = _types_param(p)
    if p.get('types') and not types:
        return []
    return [{**_tag_row(name, info), 'distance': dist}
            for name, info, dist in TagsDB.fuzzy_matches(module, _plain_tag(p.get('text', '')), limit, types)]


def m_tags_status(_: dict) -> list[dict]:
    """learned: the list is learned from the posts seen here (lynxx/tags_learned.py), its counts are not the site's.
    bundled: the list ships inside an installed extension that has no tag source (version: that extension's), it is not updated and
    the learning adds the tags it lacks.
    shipped: the list in use is still the one shipped inside an installed extension that has a tag source (its own job, an engine,
    a reviewed module), not updated here yet (version: that extension's). For both, mtime is the time of the file (the build or
    the install), not the date of the data.
    added: learned tags served on top of the list, for every list the learning adds to (TagsDB.LearningBases: a bundled list, or
    the list of a module whose own update merges, shipped or merged).
    merged: the list is the one an update that merges (TagSource.merge) wrote there last: mtime is that update's time (the counts
    of the tags it read are of that time, the other tags keep theirs). Not for a list no update wrote (shipped, 2tags/)"""
    from .tags_updater import is_merged_list
    rows = []
    own = TagsDB._extension_list_manifests()
    for module, path in sorted(TagsDB.files().items()):
        st = path.stat() if path.is_file() else None
        bundled = TagsDB.bundled(module)
        on_base = bundled is not None and bundled.path == path
        on_top = TagsDB.learning_base(module) == path
        ext = own.get(module)
        shipped = not on_base and ext is not None and ext[0] == path
        rows.append({'module': module, 'path': path.as_posix(), 'size': st.st_size if st else 0, 'mtime': st.st_mtime if st else 0,
                     'loaded': TagsDB.is_loaded(module), 'learned': tags_learned.is_learned(module, path), 'bundled': on_base,
                     'shipped': shipped, 'merged': is_merged_list(path),
                     'version': bundled.version if on_base else ext[1].version if shipped else '',
                     'added': tags_learned.added_count(module) if on_top and TagsDB.get_additions(module) else 0})
    return rows


def m_tags_learn(p: dict) -> dict:
    """Learns a module's tag list from its downloaded files (download index) when it has no list from a source, or on top of the
    list it adds to; the app calls it after each download job. No request is made. -> {module, learned, posts, tags (status),
    newPosts, written (the list served for the module changed: the app asks its tags again), mayLearn (False: nothing is learned,
    the module has a list from a source, see sourceList), sourceList, indexed (the download index holds posts of the module),
    bundled (the learning adds to a list shipped inside an extension that has no tag source), added (learned tags the list it adds
    to lacks: a bundled list, or the list of a module whose own update merges)}"""
    module = str(p.get('module', ''))
    if not re_abbr.fullmatch(module):
        raise RpcError(-32602, f'invalid module \'{module}\'')
    may = tags_learned.may_learn(module)
    res = tags_learned.learn_from_index(module) if may else tags_learned.Learned(0, False)
    base = tags_learned.base_list(module)
    return {'module': module, **tags_learned.status(module), 'newPosts': res.posts, 'written': res.written, 'mayLearn': may,
            'sourceList': tags_learned.has_source_list(module), 'indexed': tags_learned.indexed(module),
            'bundled': base is not None and TagsDB.bundled(module) is not None, 'added': tags_learned.added_count(module) if base else 0}


def _learn_from_preview(module: str, items: list[dict]) -> None:
    """A module without a tag list learns it from the posts shown in Preview (in the background, no request)"""
    posts = [(it.get('id'), it.get('tags') or ()) for it in items if it.get('tags')]
    if posts and tags_learned.may_learn(module):
        threading.Thread(target=_learn_quietly, args=(module, posts), daemon=True, name=f'learn-{module}').start()


def _learn_quietly(module: str, posts: list) -> None:
    try:
        if tags_learned.learn(module, posts).written:  # not when the posts only had tags a bundled list has: nothing changed
            notify('tags.learned', {'module': module, **tags_learned.status(module)})  # the app reloads its list of lists
    except Exception as err:
        Logger.log(f'Warning: tag list of {module} not learned: {err}', False, False)


_tags_updates_lock = threading.Lock()
_tags_updates: dict[str, dict] = {}
'''running tag list updates, module -> {count, message, startedAt}: Tags A-Z shows them as running whoever started them (its button,
the app's auto update, an extension just installed), also after the window was reloaded'''


def _running_tag_updates() -> dict[str, dict]:
    with _tags_updates_lock:
        return {m: dict(st) for m, st in _tags_updates.items()}


def m_tags_sources(_: dict) -> list[dict]:
    """Installed modules with a tag list source (the five reviewed ones, extensions made from a known engine, extensions with their
    own update): the Tags A-Z page shows an update button for each, the system schedule ('lynxx-core tags --modules auto') updates
    those with key 'none' / 'optional' and onDemand false. merge / mergeMinPosts: the extension's own update reads only the tags
    with mergeMinPosts or more posts and merges them into the list in use (TagSource.as_dict).
    running: an update of the module runs in this core (count: tags read so far, message: its last progress line, startedAt: epoch s)"""
    from .tags_updater import tag_sources
    running = _running_tag_updates()
    rows = []
    for s in tag_sources():
        st = running.get(s.module) or {}
        rows.append({**s.as_dict(), 'running': bool(st), 'count': st.get('count', 0), 'message': st.get('message', ''),
                     'startedAt': st.get('startedAt', 0)})
    return rows


def m_tags_update(p: dict) -> dict:
    from .tags_updater import tag_source, update_tag_list
    module = str(p.get('module', ''))
    source = tag_source(module) if module else None
    if source is None:
        raise RpcError(-32602, f'tag list update is not supported for \'{module}\'')
    out_dir = pathlib.Path(p['outDir']) if p.get('outDir') else tags_dirs()[0] if os.environ.get(ENV_TAGS_DIR) else data_dir() / 'tags'
    job = f'tags-{module}'
    reference = TagsDB.get_file(module)
    with _tags_updates_lock:
        if module in _tags_updates:
            raise RpcError(-32004, f'tag list update for \'{module}\' is already running')
        _tags_updates[module] = {'count': 0, 'message': '', 'startedAt': time.time()}

    def progress(msg: str, count: int) -> None:
        msg = _redact(msg)
        with _tags_updates_lock:
            if (st := _tags_updates.get(module)) is not None:
                st.update(count=int(count), message=msg)
        notify('tags.updateProgress', {'job': job, 'module': module, 'count': count, 'message': msg})

    def run() -> None:
        done: dict = {'job': job, 'module': module, 'ok': False, 'written': False, 'message': 'failed: stopped'}
        try:
            res = update_tag_list(module, out_dir, api_key=p.get('apiKey', ''), delay=float(p.get('delay', 0.5)), reference=reference,
                                  source=source, full=p.get('full') is True, progress=progress)
            if res.written:
                TagsDB.locate_default_lists(reload=(module,))  # TagsDB locks itself: readers keep the old list until this swap
                # a merge rewrote the list the learning adds to: the tags it brought leave the additions, also those a learn took
                # for lacking while the old list was still loaded (recheck)
                _refresh_learned(module, recheck=True)
            done = {'job': job, 'ok': True, **res.as_dict()}
        except Exception as err:
            done = {'job': job, 'module': module, 'ok': False, 'written': False, 'message': _redact(f'failed: {err}')}
        finally:
            with _tags_updates_lock:
                _tags_updates.pop(module, None)
            # told once the running state is gone: the app reads tags.sources again when an update ends
            notify('tags.updateDone', done)

    threading.Thread(target=run, daemon=True, name=job).start()
    return {'job': job, 'outDir': out_dir.as_posix()}


def _date_filter_kind(dwn, args) -> str:
    """How the date limits (-mindate / -maxdate) reach a search, after argument parsing:
    '' none set; 'native': the site filters (db turns them into a 'date:' metatag), so counts and pages are exact;
    'local': not part of the site query: a results page hides the posts outside the limits, the count ignores them (the download
    finds the right pages by date first, download.py); 'unsupported': the module cannot filter by date, the limits were dropped"""
    if dwn._has_date_limits():
        return 'native' if dwn._supports_native_date_filter() else 'local'
    asked = bool(getattr(args, 'mindate', None) or getattr(args, 'maxdate', None))
    return 'unsupported' if asked and not dwn._has_capability(ModuleCaps.DATE_FILTER) else ''


def m_search_count(p: dict) -> dict:
    def fn(dwn, args) -> dict:
        dwn.launch_check_tags(args)
        if dwn.launch_error or dwn.interrupted:
            raise RpcError(-32002, dwn.launch_error or 'cancelled', {'log': list(_tls.log or []), 'cfChallenge': dwn.cf_challenge_hit})
        counts = list(dwn.task_counts)
        dates = _date_filter_kind(dwn, args)
        # 'local' dates: the site counted every date, the real count is lower (an upper bound, like required tags)
        return {'count': int(dwn.total_count), 'taskCounts': counts,
                'approximate': len(counts) > 1 or bool(dwn.required_tags) or dates == 'local',
                'requiredTags': list(dwn.required_tags), 'dateFilter': dates, 'cfChallenge': dwn.cf_challenge_hit}
    return _with_downloader(p.get('module', 'rx'), p.get('tags', ''), list(p.get('args', [])), fn)


def _takes_page_size(cls: type) -> bool:
    """Whether a downloader's search address takes a page size (see download.takes_page_size)"""
    from .download import takes_page_size
    return takes_page_size(cls)


def _results_page_size(dwn, requested: Any = None) -> tuple[int, int | None]:
    """(page size, maxlim for _form_tags_search_address): Downloader._listing_page_size, which pick jobs use too (default
    min(module page size, PREVIEW_PAGE_SIZE_DEFAULT), at most the module page size)"""
    from .download import Downloader
    return Downloader._listing_page_size(dwn, requested)


def _fetch_results_page(dwn, page: int, page_size: int, maxlim: int | None) -> tuple[list[str], bool]:
    """Raw items of page `page` of every task, merged (Downloader._fetch_listing_page: a pick job of the app reads the same page the
    same way). A page that cannot be fetched is an RpcError whose message says when it is a bot challenge"""
    def fetch(address: str) -> list[str]:
        items = dwn._fetch_listing_items(address)
        if items is None:
            # the error data does not cross the app's IPC (only the message does): the message itself says when it is a challenge
            why = ' (the site answered with a bot challenge: Lynxx does not bypass it)' if dwn.cf_challenge_hit else ''
            raise RpcError(-32003, f'unable to fetch search page{why}', {'cfChallenge': dwn.cf_challenge_hit})
        return items
    return dwn._fetch_listing_page(page, page_size, maxlim, fetch)


class _ResultsFilter:
    """Local filters of a results page, as the download applies them: safety blacklist (-safety_blacklist), required tags,
    negative groups, date limits the site query does not carry (see _date_filter_kind), gender. Counts what it hides"""

    def __init__(self, dwn) -> None:
        self.dwn = dwn
        self.required = {t.lower() for t in dwn.required_tags}
        self.gender_on = bool(dwn.gender_select)
        self.safety_on = bool(dwn.safety_blacklist)
        self.rules = rules_for_module(dwn._get_module_abbr()) if self.gender_on else None
        self.with_tags = bool(dwn._can_extract_item_info_without_fetch())
        self.dates = _date_bounds(dwn) if _date_filter_kind(dwn, None) == 'local' else None
        self.dated = 0
        '''posts of the page whose listing gave a readable date (none: the dates could not be checked, see m_search_preview)'''
        self.gender_stats: dict[str, int] = {}
        self.blocked = 0
        self.hidden_no_tags = 0
        self.model_pending: list[tuple[dict, str, object]] = []
        self.model_info: dict = {}

    def item(self, h: str) -> dict | None:
        """Preview item, None if it must not be shown at all"""
        dwn = self.dwn
        item = {'id': dwn._extract_id(dwn._local_addr_from_string(h)), 'preview': dwn.extract_preview_url(h), 'sample': _attr(h, 'sample_url'),
                'isVideo': bool(dwn._is_video(h)), 'mediaKind': dwn._media_kind(h)}
        if item['mediaKind'] != 'image':  # the in-app player streams the original file
            item['file'] = _attr(h, 'file_url')
        if not self.with_tags:
            if self.safety_on:
                # tags are unknown without fetching each post: the safety blacklist cannot be checked -> fail closed,
                # the post (and its thumbnail url) is not returned at all
                self.hidden_no_tags += 1
                return None
            return item
        info = dwn._extract_item_info(h)
        tags = info.tags.split(' ') if info.tags else []
        item.update({'tags': tags, 'score': info.score, 'width': info.width, 'height': info.height, 'ext': info.ext,
                     'date': info.date, 'rating': _attr(h, 'rating'), 'md5': getattr(info, 'md5', '') or ''})
        if self.safety_on and blocked_tag(tags):
            self.blocked += 1  # safety: never returned, not even a thumbnail url
            return None
        tl = {t.lower() for t in tags}
        excluded_by = []
        if self.required and not self.required.issubset(tl):
            excluded_by.append('required')
        if any(all(any(pt.fullmatch(t) for t in tl) for pt in pl) for pl in dwn.neg_and_groups):
            excluded_by.append('neg')
        if self.dates is not None and not self._in_dates(info.date):
            excluded_by.append('date')
        if self.rules is not None:
            res = infer_from_tags(tags, self.rules)
            keep, eff = decide(res, dwn.gender_select, dwn.gender_min_conf, dwn.gender_strict)
            item['gender'] = {**res.as_dict(), 'effective': str(eff)}
            self.gender_stats[str(eff)] = self.gender_stats.get(str(eff), 0) + 1
            if not keep:
                excluded_by.append('gender')
            if dwn.gender_mode == 'model':
                from .gender_model import needs_model
                if needs_model(res, dwn.gender_min_conf):
                    self.model_pending.append((item, h, res))
        item['excluded'] = bool(excluded_by)
        item['excludedBy'] = excluded_by
        return item

    def _in_dates(self, date: str) -> bool:
        """Post date (FMT_DATE) within the limits; a post without a readable date is kept, as the download keeps it"""
        if self.dates is None:
            return True
        try:
            day = as_date(date)
        except (TypeError, ValueError):
            return True
        self.dated += 1
        return self.dates[0] <= day <= self.dates[1]


def _date_bounds(dwn) -> tuple[datetime.date, datetime.date]:
    """First and last day a download keeps (download_base: _filter_last_items, _filter_first_items): a min date on or before
    DATE_MIN_DEFAULT and a max date on or after the downloader's day (date_today, the default) limit nothing. A site ahead of the
    user's time zone dates its newest posts tomorrow: they stay, as in the download"""
    lo, hi = as_date(dwn.date_min), as_date(dwn.date_max)
    return lo if lo > as_date(DATE_MIN_DEFAULT) else datetime.date.min, hi if hi < dwn.date_today else datetime.date.max


PREVIEW_MODEL_MAX = 40
'''posts of one preview page the gender model looks at (images are fetched at the module's request pace)'''


def _apply_gender_model(filt: _ResultsFilter, items: list[dict]) -> None:
    """-gender_mode model: the image model labels the posts of the page whose tags were not conclusive; with -safety_blacklist
    its safety gate removes posts entirely (like the blacklist: never returned). Posts past PREVIEW_MODEL_MAX keep their tag label"""
    if not filt.model_pending:
        return
    from .gender_model import ModelUnavailable, combine, verdicts_for
    dwn = filt.dwn
    todo = filt.model_pending[:PREVIEW_MODEL_MAX]
    for it, _, _ in filt.model_pending[PREVIEW_MODEL_MAX:]:
        it['gender']['modelSkipped'] = True
    try:
        verdicts = verdicts_for(dwn, [(it['id'], h) for it, h, _ in todo])
    except ModelUnavailable as err:
        filt.model_info = {'error': str(err)}
        return
    stats = filt.gender_stats
    dropped: set[int] = set()
    for it, _, res in todo:
        v = verdicts.get(it['id'])
        if v is None:
            continue
        old = it['gender']['effective']
        stats[old] = stats.get(old, 1) - 1
        if v.safety_tag and filt.safety_on:
            dropped.add(id(it))
            filt.blocked += 1
            continue
        comb = combine(res, v.result, dwn.gender_min_conf)
        keep, eff = decide(comb, dwn.gender_select, dwn.gender_min_conf, dwn.gender_strict)
        stats[str(eff)] = stats.get(str(eff), 0) + 1
        it['gender'] = {**comb.as_dict(), 'effective': str(eff)}
        by = [x for x in it['excludedBy'] if x != 'gender'] + ([] if keep else ['gender'])
        it['excludedBy'], it['excluded'] = by, bool(by)
    for k in [k for k, n in stats.items() if n <= 0]:
        del stats[k]
    if dropped:
        items[:] = [it for it in items if id(it) not in dropped]
    filt.model_info = {'checked': len(todo), 'labeled': sum(1 for v in verdicts.values() if str(v.result.label) != 'unknown'),
                       'skipped': max(0, len(filt.model_pending) - len(todo))}


def _mark_downloaded(module: str, items: list[dict]) -> None:
    """items already in the download index with their file on disk (same post of this module, or same md5 from any module):
    'downloaded': 'id' | 'md5'. Index only, no request. A missing index or a database error marks nothing"""
    try:
        from .dlindex import DownloadIndex
        with DownloadIndex() as index:
            by_id, by_md5 = index.find_known(module, [it['id'] for it in items], [it.get('md5', '') for it in items])
    except Exception:
        return
    for it in items:
        if it['id'] in by_id:
            it['downloaded'] = 'id'
        elif it.get('md5') and it['md5'].lower() in by_md5:
            it['downloaded'] = 'md5'


def m_search_preview(p: dict) -> dict:
    """Page `page` of search results with thumbnails, local filters (required tags / negative groups / dates / gender) applied.
    pageSize: see _results_page_size. Multi-task queries: page `page` of every task, merged (see _fetch_results_page).
    dateFilter: see _date_filter_kind, plus 'unchecked' (local limits, but no post of the listing carries a date: nothing hidden).
    Pages are the site's pages: with 'local' limits a page may hold no post in range (finding the first page by date costs a
    search per step, the download does it)"""
    page = max(0, int(p.get('page', 0)))

    def fn(dwn, args) -> dict:
        dwn._parse_args(args, False)
        page_size, maxlim = _results_page_size(dwn, p.get('pageSize'))
        items_raw, has_more = _fetch_results_page(dwn, page, page_size, maxlim)
        filt = _ResultsFilter(dwn)
        result = [item for item in map(filt.item, items_raw) if item is not None]
        _apply_gender_model(filt, result)
        _mark_downloaded(dwn._get_module_abbr(), result)
        dates = _date_filter_kind(dwn, args)
        # no post of the page had a readable date in the listing (rule34hentai lists tags, not dates): nothing could be checked
        if dates == 'local' and (not filt.with_tags or (result and not filt.dated)):
            dates = 'unchecked'
        # the listing gives no post tags (the module fetches each post for them): required tags / negative groups were not checked
        unchecked = not filt.with_tags and bool(dwn.required_tags or dwn.neg_and_groups)
        return {'page': page, 'pageSize': page_size, 'hasMore': has_more, 'items': result, 'tasks': list(dwn.tags_str_arr),
                'requiredTags': list(dwn.required_tags), 'site': dwn._get_sitename(), 'genderStats': filt.gender_stats,
                'blockedCount': filt.blocked, 'hiddenNoTags': filt.hidden_no_tags, 'genderEnabled': filt.gender_on,
                'genderModel': filt.model_info, 'dateFilter': dates, 'localFiltersUnchecked': unchecked}
    module = p.get('module', 'rx')
    res = _with_downloader(module, p.get('tags', ''), list(p.get('args', [])), fn)
    _learn_from_preview(module, res.get('items', []))
    return res


def _query_tag_matchers(query: str, module: str | None = None) -> tuple[set[str], list[re.Pattern]]:
    """Tags of a query, negated ones and members of '(a~b)' / '-(a,b)' groups included: (plain names, wildcard patterns).
    A tag of a search kind of the module ('artist:some_artist') also gives its value: posts of that listing carry it as a plain tag"""
    names: set[str] = set()
    for word in query.lower().split():
        word = word.lstrip('-')
        if len(word) > 2 and word[0] == word[-1] == '%':  # escaped tag
            word = word[1:-1]
            names.add(word)
        elif word.startswith('(') and word.endswith(')') and ('~' in word or ',' in word):
            names.update(t.lstrip('-') for t in re.split(r'[~,]', word[1:-1]))
            continue
        else:
            names.add(word)
        if module is not None and (kind := kind_of(word, module)) is not None:
            names.add(word[len(kind.token):])
    names.discard('')
    return names, [wildcard_pattern(n) for n in names if is_wtag(n)]


def m_search_related(p: dict) -> dict:
    """Tags co-occurring in the first results page of the query (posts kept by the local filters), the query's own tags and metatags
    excluded. count: sampled posts having the tag; order: count desc, then rarer overall (TagsDB posts asc), then name.
    Uses the posts' tags of the listing: modules without tags in listings give nothing (no extra requests)"""
    module = p.get('module', 'rx')
    query = str(p.get('tags', ''))
    limit = max(1, min(RELATED_LIMIT_MAX, int(p.get('limit') or 30)))

    def fn(dwn, args) -> tuple[dict[str, int], int]:
        dwn._parse_args(args, False)
        if not dwn._can_extract_item_info_without_fetch():
            return {}, 0
        page_size, maxlim = _results_page_size(dwn)
        items_raw, _ = _fetch_results_page(dwn, 0, page_size, maxlim)
        filt = _ResultsFilter(dwn)
        names, patterns = _query_tag_matchers(query, module)
        counts: dict[str, int] = {}
        sample = 0
        for h in items_raw:
            item = filt.item(h)
            if item is None or item.get('excluded'):
                continue
            sample += 1
            for tag in {t.lower() for t in item.get('tags', ()) if t}:
                if tag not in names and not _is_metatag(tag, module) and not any(pt.fullmatch(tag) for pt in patterns):
                    counts[tag] = counts.get(tag, 0) + 1
        return counts, sample

    counts, sample = _with_downloader(module, query, list(p.get('args', [])), fn)
    # tag list lookups after the core lock is released (a cold list load must not block other searches)
    rows = []
    for tag, count in counts.items():
        info = TagsDB.find_tag(module, tag)
        rows.append({'tag': tag, 'count': count, 'posts': info.posts_count if info else 0,
                     'type': str(TAG_NAMES_BY_TYPE.get(info.tag_type, 'unknown')) if info else 'unknown'})
    rows.sort(key=lambda r: (-r['count'], r['posts'], r['tag']))
    return {'items': rows[:limit], 'sample': sample}


def _attr(h: str, name: str) -> str:
    key = f' {name}="'
    i = h.find(key)
    return h[i + len(key):h.find('"', i + len(key))] if i >= 0 else ''


def resolve_user_path(raw: str) -> pathlib.Path:
    """'~/x' and relative paths are resolved against the home folder (the desktop app does the same for jobs)"""
    path = pathlib.Path(str(raw).strip() or '.').expanduser()
    return path if path.is_absolute() else pathlib.Path.home() / path


# ----- search by file name / post link (rules: lynxx/file_identify.py, shared with the library import) -----

from .file_identify import build_file_queries, identify_file_name  # noqa: E402
from .file_identify import site_hosts as _site_hosts  # noqa: E402

FILES_IDENTIFY_MAX = 500


def m_files_identify(p: dict) -> dict:
    """{names: [...], module} -> {items: [{name, module, kind, value, note}], queries: [{module, tags, count, dropped}]}"""
    module = str(p.get('module') or 'rx')
    names = p.get('names') or []
    if isinstance(names, str):
        names = names.splitlines()
    names = [str(x) for x in names if str(x).strip()][:FILES_IDENTIFY_MAX]
    hosts = _site_hosts()
    items = [identify_file_name(n, module, hosts) for n in names]
    # idSearch False (manifest 'search'): the module cannot run these 'id:' / 'md5:' searches, the app does not offer them
    queries = [{**q, 'idSearch': id_search_supported(q['module'])} for q in build_file_queries(items)]
    return {'items': items, 'queries': queries, 'max': FILES_IDENTIFY_MAX}


def m_files_nonempty(p: dict) -> dict:
    path = resolve_user_path(p.get('path', ''))
    return {'exists': path.is_dir(), 'nonEmpty': path.is_dir() and next(path.iterdir(), None) is not None}


# ----- local download index (lynxx/dlindex.py) -----

INDEX_QUERY_MAX_LEN = 2000
re_index_module = re.compile(r'[a-z][a-z\d]{1,3}')
'''module abbreviation (see modspec.re_abbr)'''


def _int_param(p: dict, key: str, default: int) -> int:
    try:
        return int(p.get(key, default) or default)
    except (TypeError, ValueError):
        raise RpcError(-32602, f'invalid \'{key}\'') from None


def _recover_moves(index) -> None:
    """Trash moves a stopped core left half done are finished before the library or the trash is shown (recover_moves: one
    indexed query when there is nothing to do). A locked or broken index still shows what it has"""
    import sqlite3
    try:
        index.recover_moves()
    except sqlite3.Error:
        pass


def m_index_search(p: dict) -> dict:
    """{tags, module, dateFrom, dateTo, page, pageSize, order, trash} -> {total, page, pageSize, items: [...], terms}.
    tags: 'tag -tag pre* rating:e md5:<hash> id:<n>' (all must match); dates: post date 'YYYY-MM-DD' (inclusive); pages from 0.
    trash: true lists the files in the trash (items: trashedAt, origPath) instead of the library.
    Reads only the index: no request to any website. Each call has its own connection (the rpc pool is multi-threaded)"""
    from .dlindex import DownloadIndex
    tags = p.get('tags') or ''
    tags = ' '.join(str(t) for t in tags) if isinstance(tags, list) else str(tags)
    module = str(p.get('module') or '').strip().lower()
    if module and not re_index_module.fullmatch(module):
        raise RpcError(-32602, 'invalid module')
    with DownloadIndex() as index:
        _recover_moves(index)
        return index.search(tags[:INDEX_QUERY_MAX_LEN], module=module, date_from=str(p.get('dateFrom') or ''),
                            date_to=str(p.get('dateTo') or ''), page=_int_param(p, 'page', 0), page_size=_int_param(p, 'pageSize', 50),
                            order=str(p.get('order') or 'downloaded'), trash=p.get('trash') is True)


def _model_server_params(p: dict) -> tuple[str, str]:
    url, token = p.get('url') or '', p.get('token') or ''
    if not isinstance(url, str) or not isinstance(token, str):
        raise RpcError(-32602, 'url and token must be strings')
    return url, token


def m_gender_model_status(p: dict) -> dict:
    """{url, token?}: can the gender model server at url be used (the desktop app runs the model there, never in the app),
    on what hardware (check: GPU with enough VRAM, or the CPU). Without url: the model in this process (CLI / developers)"""
    from .gender_model import api_status, model_status
    url, token = _model_server_params(p)
    if not url:
        return {'local': True, 'inProcess': model_status()}
    return api_status(url, token)


def m_gender_model_load(p: dict) -> dict:
    """{url, token?}: the app turns the model on: load it on the server now (the first job does not wait), then the status"""
    from .gender_model import api_status
    url, token = _model_server_params(p)
    if not url:
        raise RpcError(-32602, 'url required')
    return api_status(url, token, 'load')


def m_gender_model_unload(p: dict) -> dict:
    """{url, token?}: the app turns the model off: free the memory it holds on the server"""
    from .gender_model import api_status
    url, token = _model_server_params(p)
    if not url:
        raise RpcError(-32602, 'url required')
    return api_status(url, token, 'unload')


def m_index_stats(_: dict) -> dict:
    """Totals of the download index (files, posts, tags, sizes, per module), exists: False until the first file is recorded"""
    from .dlindex import DownloadIndex
    with DownloadIndex() as index:
        _recover_moves(index)
        return index.stats()


METHODS: dict[str, Callable[[dict], Any]] = {
    'app.info': m_app_info,
    'ext.list': m_ext_list,
    'ext.install': m_ext_install,
    'ext.uninstall': m_ext_uninstall,
    'ext.remoteCatalog': m_ext_remote_catalog,
    'tags.autocomplete': m_tags_autocomplete,
    'tags.browse': m_tags_browse,
    'tags.validate': m_tags_validate,
    'tags.resolve': m_tags_resolve,
    'tags.fuzzy': m_tags_fuzzy,
    'tags.kinds': m_tags_kinds,
    'tags.types': m_tags_types,
    'tags.status': m_tags_status,
    'tags.sources': m_tags_sources,
    'tags.learn': m_tags_learn,
    'tags.update': m_tags_update,
    'search.count': m_search_count,
    'search.preview': m_search_preview,
    'search.related': m_search_related,
    'files.nonEmpty': m_files_nonempty,
    'files.identify': m_files_identify,
    'index.search': m_index_search,
    'gender.modelStatus': m_gender_model_status,
    'gender.modelLoad': m_gender_model_load,
    'gender.modelUnload': m_gender_model_unload,
    'index.stats': m_index_stats,
    'source.list': m_source_list,
    'source.policy': m_source_policy,
    'library.import': m_library_import,
    'library.artifacts': m_library_artifacts,
    'reader.progress.get': m_reader_progress_get,
    'reader.progress.set': m_reader_progress_set,
    'reader.epub.pages': m_reader_epub_pages,
    'reader.chapter': m_reader_chapter,
    'formats.supported': m_formats_supported,
    'library.collections': m_library_collections,
    'library.collection': m_library_collection,
    'library.collection.delete': m_library_collection_delete,
    'library.collection.remote': m_library_collection_remote,
    'library.book.upsert': m_library_book_upsert,
    'library.book.cover': m_library_book_cover,
    'library.chapter.commitImages': m_library_chapter_commit_images,
    'library.chapter.commitText': m_library_chapter_commit_text,
    'library.chapter.state': m_library_chapter_state,
    'export.suggest': m_export_suggest,
    'export.build': m_export_build,
    'export.bundle': m_export_bundle,
    'export.record': m_export_record,
    'export.autoTargets': m_export_auto_targets,
    'export.forget': m_export_forget,
    'plugins.unpack': m_plugins_unpack,
}


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


# ----- download index maintenance: clean (lynxx/dlindex_tools.py; the library import runs as a job, not here) -----

INDEX_CLEAN_NOTIFY_INTERVAL = 0.3
'''seconds between two index.cleanProgress notifications, at least'''
_index_clean_lock = threading.Lock()
_index_clean: dict[str, Any] = {}
'''the clean in progress: {'job': id, 'cancel': Event}'''


def m_index_clean(p: dict) -> dict:
    """{missingFolders?: bool, dryRun?: bool} -> {job}. Drops the index rows whose file is no longer on disk (rows only, files are
    never touched) in a background thread: batches of rows, one short write transaction each, so the rpc pool and download jobs
    keep going. Rows whose folder is gone too are kept unless missingFolders. Notifications: index.cleanProgress {job, total,
    checked, missing, missingDirs, removed} and index.cleanDone {job, ok, cancelled, ..., message}. One clean at a time (-32004)"""
    import time  # local: this section keeps the module imports untouched

    from .dlindex_tools import clean_index
    missing_folders = p.get('missingFolders') is True
    dry_run = p.get('dryRun') is True
    cancel = threading.Event()
    with _index_clean_lock:
        if _index_clean:
            raise RpcError(-32004, 'index clean is already running')
        job = f'index-clean-{int(time.time() * 1000):d}'
        _index_clean.update(job=job, cancel=cancel)
    last = [0.0]

    def progress(r) -> None:
        if time.monotonic() - last[0] >= INDEX_CLEAN_NOTIFY_INTERVAL:
            last[0] = time.monotonic()
            notify('index.cleanProgress', {'job': job, **r.as_dict()})

    def run() -> None:
        try:
            res = clean_index(include_missing_dirs=missing_folders, dry_run=dry_run, progress=progress, cancel=cancel)
            done = {'job': job, 'ok': True, **res.as_dict(), 'message': ''}
        except Exception as err:
            done = {'job': job, 'ok': False, 'cancelled': False, 'message': _redact(f'{type(err).__name__}: {err}')}
        finally:
            with _index_clean_lock:
                _index_clean.clear()
        # only now: a client may start the next clean as soon as it reads cleanDone (a dry run, then the real clean), and
        # that one would be refused with -32004 while this job is still registered
        notify('index.cleanDone', done)

    threading.Thread(target=run, daemon=True, name=job).start()
    return {'job': job, 'missingFolders': missing_folders, 'dryRun': dry_run}


def m_index_clean_cancel(_: dict) -> dict:
    """Stops the clean in progress after its current batch (rows already removed stay removed)"""
    with _index_clean_lock:
        cancel = _index_clean.get('cancel')
        job = _index_clean.get('job', '')
    if cancel is not None:
        cancel.set()
    return {'cancelled': cancel is not None, 'job': job}


METHODS.update({'index.clean': m_index_clean, 'index.cleanCancel': m_index_clean_cancel})


# ----- trash of the library (lynxx/dlindex.py). The app calls these from main only (library:* handlers), never the renderer -----

INDEX_PATHS_MAX = 1000
INDEX_ROOTS_MAX = 8
INDEX_PATH_MAX_LEN = 4096
TRASH_DAYS_MAX = 3650


def _paths_param(p: dict, key: str, max_count: int, required: bool = True) -> list[str]:
    """A list of absolute paths (the index compares them as stored: they come from index.search items)"""
    raw = p.get(key)
    if raw is None and not required:
        return []
    if not isinstance(raw, list) or (required and not raw) or len(raw) > max_count:
        raise RpcError(-32602, f'invalid \'{key}\'')
    for x in raw:
        if not isinstance(x, str) or not x or len(x) > INDEX_PATH_MAX_LEN or '\x00' in x or not os.path.isabs(x):
            raise RpcError(-32602, f'invalid \'{key}\': absolute paths only')
    return raw


def m_index_trash(p: dict) -> dict:
    """{paths, roots?} -> {trashed: [{path, trashPath}], failed: [{path, error}], refused: [path]}. Moves files of the library into
    the trash (renamed on the same drive, never copied; roots: the download folders, see DownloadIndex.trash_folders). Only files
    the index has as rows of the library: any other path is refused, never touched"""
    from .dlindex import DownloadIndex
    paths = _paths_param(p, 'paths', INDEX_PATHS_MAX)
    roots = _paths_param(p, 'roots', INDEX_ROOTS_MAX, required=False)
    with DownloadIndex() as index:
        return index.trash(paths, roots=roots)


def m_index_restore(p: dict) -> dict:
    """{paths: paths in the trash} -> {restored: [{path, restoredPath}], failed, refused}: files back where they were"""
    from .dlindex import DownloadIndex
    paths = _paths_param(p, 'paths', INDEX_PATHS_MAX)
    with DownloadIndex() as index:
        return index.restore(paths)


def m_index_purge_trash(p: dict) -> dict:
    """{days} | {paths} | {all: true} -> {purged, files, bytes, failed, kept}: deletes files of the trash for good (trashed more
    than `days` days ago, the given paths of the trash, or all of them), then their rows. Cannot be undone: the app asks first"""
    from .dlindex import DownloadIndex
    ways = [k for k in ('days', 'paths', 'all') if p.get(k) is not None and p.get(k) is not False]
    if len(ways) != 1:
        raise RpcError(-32602, 'one of \'days\', \'paths\' or \'all\'')
    with DownloadIndex() as index:
        if ways[0] == 'days':
            days = p['days']
            if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= TRASH_DAYS_MAX:
                raise RpcError(-32602, f'invalid \'days\': 0..{TRASH_DAYS_MAX:d}')
            return index.purge_trash(older_than_days=days)
        if ways[0] == 'paths':
            return index.purge_trash(paths=_paths_param(p, 'paths', INDEX_PATHS_MAX))
        if p['all'] is not True:
            raise RpcError(-32602, 'invalid \'all\'')
        return index.purge_trash(all=True)


def m_index_forget(p: dict) -> dict:
    """{paths} -> {removed, refused}: drops the rows of files that are no longer on disk (rows only)"""
    from .dlindex import DownloadIndex
    paths = _paths_param(p, 'paths', INDEX_PATHS_MAX)
    with DownloadIndex() as index:
        return index.forget(paths)


METHODS.update({'index.trash': m_index_trash, 'index.restore': m_index_restore, 'index.purgeTrash': m_index_purge_trash,
                'index.forget': m_index_forget})


# ----- network -----

PROXY_FOR_URL_MAX = 2048


def _public_https_url(value: object) -> str:
    """value if it is an https address of a public host (no credentials, not this computer or a local network), else RpcError"""
    import ipaddress
    from urllib import parse as url_parse

    from .denylist import ascii_host
    from .gender_model import _is_local_network
    bad = RpcError(-32602, '\'url\' must be an https address of a public host')
    if not isinstance(value, str) or not value or len(value) > PROXY_FOR_URL_MAX or any(c.isspace() for c in value):
        raise bad
    try:
        parts = url_parse.urlsplit(value)
        host = ascii_host(parts.hostname or '')
        _ = parts.port  # ValueError: not a port
    except ValueError:
        raise bad
    if parts.scheme.lower() != 'https' or parts.username is not None or parts.password is not None:
        raise bad
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    if (ip is not None and (not ip.is_global or ip.is_multicast)) or (ip is None and '.' not in host) or _is_local_network(host):
        raise bad
    return value


def m_net_proxy_for(p: dict) -> dict:
    """{url} -> {proxy}: the proxy the core's requests sessions use for this https address when Lynxx has no proxy of its own
    (-proxy not given, or -noproxy): requests trusts the environment, as urllib reads it (http(s)_proxy / all_proxy / no_proxy
    variables; on Windows the registry's ProxyServer when ProxyEnable is set; on macOS the system proxy settings; never a PAC file
    or WPAD). '' = a direct connection. The app's verify window leaves through the same way: a cf_clearance cookie is bound to the IP
    it was issued to. The value may carry a login (user:password@): it is never logged here"""
    import requests.utils
    url = _public_https_url(p.get('url'))
    proxies = requests.utils.get_environ_proxies(url)
    return {'proxy': str(proxies.get('https') or proxies.get('all') or '')}


METHODS.update({'net.proxyFor': m_net_proxy_for})


def main() -> int:
    os.environ[ENV_CATALOG_AUTOLOAD] = '0'  # only builtin and installed extensions are usable from the app
    Logger.init(False)
    Logger.append_to_window_proc = _log_sink
    Extensions.ensure_loaded()
    _init_tags_db()
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

#
#
#########################################
