# coding=UTF-8
"""
Lynxx extension generator: test run of an extension draft inside an isolated process (child side, plan B.6).
Started by lynxx.extgen.sandbox as `python -I -X utf8 sandbox_boot.py` with an empty environment and a temporary cwd.
- the draft's code is imported only after an audit hook (PEP 578) blocks sockets / DNS, processes, native libraries and writes
  outside the temporary folder;
- every website request of the core goes through the parent (stdout '@@FETCH {json}' -> stdin json reply), which applies
  robots.txt, host and rate rules (live run) or serves recorded fixtures (replay);
- file downloads are not performed: links are only recorded.
This is a defence layer, not an absolute security boundary: drafts are also statically checked and reviewed by a person.
"""

import base64
import json
import os
import sys

CONFIG = json.loads(sys.stdin.readline())
TMP = os.path.realpath(CONFIG['tmp'])
sys.path.insert(0, CONFIG['repo'])
os.environ.update({'LYNXX_DATA_DIR': os.path.join(TMP, 'data'), 'LYNXX_CATALOG_AUTOLOAD': '0', 'LYNXX_STREAMING': '1'})

try:
    import resource
    for res, value in ((resource.RLIMIT_AS, 2 << 30), (resource.RLIMIT_CPU, 120), (resource.RLIMIT_FSIZE, 16 << 20)):
        resource.setrlimit(res, (value, value))
except (ImportError, ValueError, OSError):
    pass  # windows: the parent's wall clock timeout still applies

import datetime  # noqa: E402
import re  # noqa: E402
import traceback  # noqa: E402
from urllib import parse as url_parse  # noqa: E402

from requests import Response  # noqa: E402
from requests.structures import CaseInsensitiveDict  # noqa: E402

from lynxx.logger import Logger  # noqa: E402

Logger.init(True, True)

import lynxx.sdk  # noqa: E402,F401  the API drafts import (and every base class), loaded before the hook: trusted code
from lynxx.cmdargs import prepare_arglist  # noqa: E402
from lynxx.defines import FMT_DATE, KNOWN_EXTENSIONS, ModuleCaps  # noqa: E402
from lynxx.denylist import default_list  # noqa: E402
from lynxx.download import Downloader  # noqa: E402
from lynxx.downloaders import _make_downloader  # noqa: E402
from lynxx.extensions import Extensions  # noqa: E402
from lynxx.lynxx_cmd import run_exit_code  # noqa: E402
from lynxx.modspec import ExtensionManifest  # noqa: E402
from lynxx.module import ProcModule  # noqa: E402
from lynxx.network import ThreadedHtmlWorker  # noqa: E402
from lynxx.search_kinds import module_search  # noqa: E402

Extensions.ensure_loaded()  # builtin modules, before the hook (trusted code)
default_list()  # the domain denylist file is read once here (Extensions._register checks the draft's site)

_out = sys.__stdout__
_in = sys.__stdin__
_seq = [0]
REQUESTS: list[dict] = []
DOWNLOADS: list[dict] = []


def _send(prefix: str, payload: dict) -> None:
    _out.write(f'{prefix} {json.dumps(payload, separators=(",", ":"))}\n')
    _out.flush()


def _proxy_request(self, url: str, tries: int, method: str, **kwargs):
    _seq[0] += 1
    try:  # the only header passed on: some APIs answer json only with it (szurubooru)
        accept = str(self._get_module_specific_default_headers().get('Accept', ''))[:100]
    except Exception:
        accept = ''
    _send('@@FETCH', {'id': _seq[0], 'url': url, 'method': method, 'accept': accept})
    reply = json.loads(_in.readline() or '{}')
    REQUESTS.append({'url': url, 'status': reply.get('status'), 'error': reply.get('error', '')})
    if reply.get('error'):
        if reply.get('rule') == 'R5':
            self.cf_challenge_hit = True
        return None
    r = Response()
    r.status_code = int(reply.get('status', 0))
    r.headers = CaseInsensitiveDict(reply.get('headers') or {})
    r._content = base64.b64decode(reply.get('body', ''))
    r.url = url
    if 200 <= r.status_code < 300:
        return r
    if 400 <= r.status_code < 500 and 'json' in r.headers.get('content-type', '') and r.content:
        return r  # api error message (the core does the same)
    return None


def _record_download(self, link: str, item_id: str, dest) -> None:
    DOWNLOADS.append({'link': link, 'id': item_id, 'name': getattr(dest, 'name', str(dest))})
    with self.item_lock:
        self.success_count += 1


ThreadedHtmlWorker.wrap_request = _proxy_request
ThreadedHtmlWorker._respect_rate_limit = lambda self: None  # the parent paces live requests
Downloader._download = _record_download

_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
BLOCKED_EVENTS = {
    'socket.connect', 'socket.bind', 'socket.getaddrinfo', 'socket.gethostbyname', 'socket.gethostbyaddr', 'socket.sendto',
    'subprocess.Popen', 'os.system', 'os.exec', 'os.posix_spawn', 'os.spawn', 'os.fork', 'os.forkpty', 'os.kill', 'os.killpg',
    'ctypes.dlopen', 'ctypes.dlsym', 'ctypes.cdata', 'webbrowser.open', 'pty.spawn', 'os.startfile', 'winreg.OpenKey',
    'sys.setprofile', 'sys.settrace',
}


READ_ROOTS: tuple[str, ...] = ()
READ_FILES: set[str] = set()


def _set_read_roots(draft: str) -> None:
    """reading is limited to python / lynxx code, the draft and the temporary folder: never the user's files (settings,
    keys, downloads...). Computed right before the hook is installed"""
    global READ_ROOTS
    import mimetypes
    roots = {TMP, CONFIG['repo'], draft, sys.prefix, sys.base_prefix, sys.exec_prefix, '/usr/share/zoneinfo', '/usr/lib/locale',
             getattr(sys, '_MEIPASS', '')}
    roots.update(p for p in sys.path if p and os.path.isdir(p))
    READ_ROOTS = tuple(os.path.realpath(r).rstrip(os.sep) + os.sep for r in roots if r)
    READ_FILES.update(os.path.realpath(f) for f in ('/dev/null', '/dev/urandom', *mimetypes.knownfiles))
    if getattr(sys, 'frozen', False):  # frozen core: python modules not imported yet are read from the executable's archive
        READ_FILES.add(os.path.realpath(sys.executable))


def _readable(path) -> bool:
    try:
        p = os.path.realpath(os.fsdecode(path))
    except Exception:
        return False
    return p in READ_FILES or (p + os.sep).startswith(READ_ROOTS)


def _under_tmp(path) -> bool:
    try:
        return os.path.realpath(os.fsdecode(path)).startswith(TMP + os.sep)
    except Exception:
        return False


def _hook(event: str, args: tuple) -> None:
    if event in BLOCKED_EVENTS:
        raise PermissionError(f'sandbox: {event} is not allowed')
    if event == 'open' and args and not isinstance(args[0], int):
        mode = args[1] if len(args) > 1 and isinstance(args[1], str) else ''
        flags = args[2] if len(args) > 2 and isinstance(args[2], int) else 0
        if (any(c in mode for c in 'wax+') or flags & _WRITE_FLAGS) and not _under_tmp(args[0]):
            raise PermissionError(f'sandbox: writing {args[0]!r} is not allowed')
        if not _readable(args[0]):
            raise PermissionError(f'sandbox: reading {args[0]!r} is not allowed')
    elif event in ('os.listdir', 'os.scandir') and args and isinstance(args[0], (str, bytes, os.PathLike)):
        if not _readable(args[0]):
            raise PermissionError(f'sandbox: listing {args[0]!r} is not allowed')
    elif event in ('os.remove', 'os.rename', 'os.rmdir', 'shutil.rmtree', 'os.mkdir', 'os.chmod') and args:
        if not _under_tmp(args[0]):
            raise PermissionError(f'sandbox: {event} on {args[0]!r} is not allowed')


def _check(checks: list, cid: str, ok: bool, detail: str = '', severity: str = 'error') -> bool:
    checks.append({'id': cid, 'ok': bool(ok), 'severity': severity, 'detail': detail})
    return ok


def _site_base(host: str) -> str:
    return '.'.join(host.split('.')[-2:])


def _kind_routes(m: ExtensionManifest, proc_id: int) -> list[dict]:
    """The search address(es) of each kind with a token (manifest 'search'), built for a sample value as a run builds them: nothing
    is fetched. The parent checks them against the site's recorded robots.txt (a kind is declared after the robots / terms check)"""
    ms = module_search(proc_id)
    out: list[dict] = []
    for kind in m.search.kinds:
        if not kind.token:
            continue
        row: dict = {'id': kind.id, 'token': kind.token, 'query': '', 'urls': [], 'error': ''}
        out.append(row)
        rex = ms.patterns.get(kind.token) if ms is not None else None
        example = kind.example[len(kind.token):] if kind.example.startswith(kind.token) else kind.example
        tries = (example, '12345' if kind.input == 'id' else 'sample_value', 'sample', '12345', 'a')
        sample = next((v for v in tries if v and ' ' not in v and rex is not None and rex.fullmatch(kind.token + v)), '')
        if not sample:
            row['error'] = 'no sample value matches the kind\'s value: give the kind an example'
            continue
        row['query'] = kind.token + sample
        try:
            ProcModule.set(proc_id)
            kdwn = _make_downloader(proc_id)
            kdwn._parse_args(prepare_arglist(['-module', m.abbr, '--', row['query']]), False)
            row['urls'] = [kdwn._form_tags_search_address(kdwn._consume_custom_module_tags(t)) for t in kdwn.tags_str_arr or ['']]
        except BaseException as err:  # refused by the downloader (thread_exit) or the parser: never requested
            row['error'] = f'{type(err).__name__}: {err}'[:400]
    return out


def main() -> dict:
    checks: list[dict] = []
    report: dict = {'checks': checks, 'requests': REQUESTS, 'downloads': DOWNLOADS}
    query = [str(t) for t in CONFIG.get('query') or []]
    limit = int(CONFIG.get('limit') or 3)
    draft = CONFIG['draft']
    _set_read_roots(draft)
    sys.addaudithook(_hook)  # from here on the draft's code runs

    try:
        m = ExtensionManifest.load(__import__('pathlib').Path(draft) / 'manifest.json', 'user')
        Extensions._register(m)
        proc_id = ProcModule.PROC_MODULES_BY_NAME[m.abbr]
        dwn = _make_downloader(proc_id)
    except Exception:
        _check(checks, 'load', False, traceback.format_exc(limit=4)[-1500:])
        return report
    _check(checks, 'load', True, f'{type(dwn).__name__} loaded')
    site_host = (url_parse.urlparse(m.site).hostname or '').lower()
    report['abbr'] = m.abbr
    caps = sorted(getattr(type(dwn), 'CAPABILITIES', frozenset()))
    report['capabilities'] = caps
    try:
        interval = float(dwn._get_min_request_interval())
    except Exception:
        interval = -1.0
    _check(checks, 'rate', interval >= 0.5, f'minimal request interval {interval} s (>= 0.5 s)')
    if any(k.token for k in m.search.kinds):
        report['kindRoutes'] = _kind_routes(m, proc_id)

    # count (search.count of the app)
    try:
        cdwn = _make_downloader(proc_id)
        cdwn.launch_check_tags(prepare_arglist(['-module', m.abbr, '--', *query]))
        count = int(cdwn.total_count)
        report['count'] = count
        _check(checks, 'count', not cdwn.launch_error and count > 0,
               cdwn.launch_error or f'{count} post(s) for \'{" ".join(query)}\'')
    except Exception:
        _check(checks, 'count', False, traceback.format_exc(limit=3)[-1200:])

    # a small download run (files are not fetched, links are recorded)
    try:
        ddwn = _make_downloader(proc_id)
        args = prepare_arglist(['-module', m.abbr, '-path', os.path.join(TMP, 'out'), '-threads', '1', '-dlimit', str(limit),
                                '--', *query])
        ddwn.launch_download(args)
        code, error = run_exit_code(ddwn)
        found = len(ddwn.items_raw_all)
        report['found'] = found
        _check(checks, 'run', code in (0, 3) and not ddwn.launch_error, error or f'exit code {code}')
        _check(checks, 'items', found > 0, f'{found} post(s) scheduled')
        expected = min(limit, found)
        _check(checks, 'download', len(DOWNLOADS) >= max(1, expected) and not ddwn.failed_items,
               f'{len(DOWNLOADS)} / {expected} file link(s); failed: {ddwn.failed_items[:5]}')
        bad_links, foreign = [], set()
        for d in DOWNLOADS:
            p = url_parse.urlparse(d['link'])
            ext = d['name'].rsplit('.', 1)[-1].lower() if '.' in d['name'] else ''
            if p.scheme != 'https' or not p.hostname or ext not in KNOWN_EXTENSIONS:
                bad_links.append(d['link'][:200])
            elif _site_base(p.hostname.lower()) != _site_base(site_host):
                foreign.add(p.hostname.lower())
        _check(checks, 'links', not bad_links and bool(DOWNLOADS), f'not https / unknown file type: {bad_links[:3]}' if bad_links else
               f'{len(DOWNLOADS)} https link(s) with known media extensions')
        if foreign:
            _check(checks, 'hosts', True, f'files are served by other hosts (cdn?): {", ".join(sorted(foreign))}', 'warning')
        infos = list(ddwn.item_info_dict_all.values())
        with_tags = sum(1 for i in infos if (i.tags or '').strip())
        _check(checks, 'tags', bool(infos) and with_tags >= 0.8 * len(infos),
               f'{with_tags} / {len(infos)} post(s) have tags (needed by local filters and the safety list)')
        if ModuleCaps.DATE_FILTER in caps:
            dated = 0
            for i in infos:
                try:
                    datetime.datetime.strptime(i.date, FMT_DATE)
                    dated += 1
                except Exception:
                    pass
            _check(checks, 'dates', bool(infos) and dated == len(infos), f'{dated} / {len(infos)} post date(s) parse as dd-mm-yyyy')
        report['ids'] = [re.sub(r'^[a-z\d]+_', '', d['id']) for d in DOWNLOADS]
        previews = []
        for raw in ddwn.items_raw_all[:3]:
            try:
                previews.append(ddwn.extract_preview_url(raw))
            except Exception:
                previews.append('')
        report['previews'] = previews
        if ModuleCaps.PREVIEW in caps:
            _check(checks, 'preview', all(x.startswith('https://') for x in previews) and bool(previews),
                   f'preview urls: {previews[:2]}', 'warning')
    except Exception:
        _check(checks, 'run', False, traceback.format_exc(limit=4)[-1500:])
    return report


if __name__ == '__main__':
    try:
        result = main()
    except BaseException:
        result = {'checks': [{'id': 'sandbox', 'ok': False, 'severity': 'error', 'detail': traceback.format_exc(limit=3)[-1500:]}]}
    _send('@@REPORT', result)
