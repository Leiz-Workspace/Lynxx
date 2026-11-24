# coding=UTF-8
"""
Lynxx extension generator: parent side of draft test runs (see sandbox_boot.py) and the full draft check.
- record(): live test run, website requests go through SafeFetcher and are saved as fixtures (tests/fixtures/), the result
  becomes tests/expected.json;
- check_draft(): manifest + static policy + offline replay of the fixtures compared with expected.json (no network);
- pack(): zip + sha256 for a manual install (Extensions -> install from zip).
record() also saves the site's robots.txt next to the manifest (what SDK 1.2's robots_allows reads): the search address of each
kind of the manifest's 'search' (Lynxx 2.0.11) is built for a sample value and checked against it (_search_checks).
"""
#########################################
#
#

from __future__ import annotations

import base64
import hashlib
import json
import os
import pathlib
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile

from lynxx.denylist import denied_in_extension, denied_message
from lynxx.modspec import ExtensionManifest
from lynxx.robots import ROBOTS_MAX_BYTES, RobotsRules

from .fetch import ROBOTS_AGENT, FetchPolicyError, SafeFetcher, host_of, redact_url
from .policy import check_manifest, check_source
from .taglist import draft_tag_list

__all__ = ('check_draft', 'pack', 'record', 'run_sandbox')

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
BOOT = pathlib.Path(__file__).with_name('sandbox_boot.py')
FIXTURE_MAX = 1024 * 1024
FIXTURES_TOTAL_MAX = 6 * 1024 * 1024
ROBOTS_FILE = 'robots.txt'
'''the site's robots.txt as the live test run read it, next to manifest.json (lynxx.download_base.ROBOTS_FILE_NAME, SDK 1.2)'''
PACK_FILES = ('manifest.json', 'downloader.py', 'specs.md', 'components.md', ROBOTS_FILE)
TIMEOUT = 180


def _fixtures_dir(draft: pathlib.Path) -> pathlib.Path:
    return draft / 'tests' / 'fixtures'


def _load_index(draft: pathlib.Path) -> dict:
    p = _fixtures_dir(draft) / 'index.json'
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def run_sandbox(draft: pathlib.Path, *, query: list[str], limit: int = 3, fetcher: SafeFetcher | None = None,
                record_to: pathlib.Path | None = None, timeout: float = TIMEOUT) -> dict:
    """fetcher set: live run (optionally recording fixtures into record_to). No fetcher: replay of the draft's fixtures"""
    draft = pathlib.Path(draft).resolve()
    index = {} if fetcher else _load_index(draft)
    new_index: dict[str, dict] = {}
    total = [0]
    served: list[dict] = []
    tmp = pathlib.Path(tempfile.mkdtemp(prefix='lynxx-sandbox-'))
    env = {'PATH': os.defpath, 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'}
    if sys.platform == 'win32':
        env['SYSTEMROOT'] = os.environ.get('SYSTEMROOT', r'C:\Windows')
    # frozen core (desktop app): the same executable runs the boot module ('lynxx-core sandbox', see frozen_main.py)
    cmd = [sys.executable, 'sandbox'] if getattr(sys, 'frozen', False) else [sys.executable, '-I', '-X', 'utf8', str(BOOT)]
    proc = subprocess.Popen(cmd, cwd=tmp, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding='utf-8', bufsize=1)
    config = {'tmp': str(tmp), 'repo': str(REPO_ROOT), 'draft': str(draft), 'query': query, 'limit': limit}
    report: dict | None = None
    log_tail: list[str] = []
    deadline = time.monotonic() + timeout

    def reply(payload: dict) -> None:
        proc.stdin.write(json.dumps(payload) + '\n')
        proc.stdin.flush()

    # watchdog: also stops a draft that hangs where select() is not available on pipes (windows); stderr is drained so a
    # chatty child cannot block on a full pipe
    timed_out = threading.Event()

    def _expire() -> None:
        timed_out.set()
        proc.kill()

    watchdog = threading.Timer(timeout + 5, _expire)
    watchdog.daemon = True
    watchdog.start()
    err_tail: list[str] = []

    def _drain() -> None:
        for chunk in iter(lambda: proc.stderr.read(4096), ''):
            err_tail.append(chunk)
            del err_tail[:-8]

    drain = threading.Thread(target=_drain, daemon=True)
    drain.start()
    try:
        reply(config)
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError
            if sys.platform != 'win32':
                ready, _, _ = select.select([proc.stdout], [], [], 1.0)
                if not ready:
                    if proc.poll() is not None:
                        break
                    continue
            line = proc.stdout.readline()
            if not line:
                break
            if line.startswith('@@FETCH '):
                req = json.loads(line[8:])
                url = req.get('url', '')
                if fetcher is None:
                    entry = index.get(redact_url(url))
                    if entry and (_fixtures_dir(draft) / entry['file']).is_file():
                        body = (_fixtures_dir(draft) / entry['file']).read_bytes()
                        served.append({'url': redact_url(url), 'status': entry['status'], 'fixture': entry['file']})
                        reply({'id': req['id'], 'status': entry['status'], 'headers': {'content-type': entry.get('type', '')},
                               'body': base64.b64encode(body).decode()})
                    else:
                        served.append({'url': redact_url(url), 'status': 0, 'fixture': None})
                        reply({'id': req['id'], 'error': 'not recorded: the downloader requested a url that is not in the fixtures'})
                    continue
                try:
                    res = fetcher.fetch(url, method='HEAD' if req.get('method') == 'HEAD' else 'GET',
                                        accept=str(req.get('accept') or '')[:100])
                except FetchPolicyError as err:
                    served.append({'url': redact_url(url), 'status': 0, 'error': err.message, 'rule': err.rule})
                    reply({'id': req['id'], 'error': err.message, 'rule': err.rule})
                    continue
                except Exception as err:
                    served.append({'url': redact_url(url), 'status': 0, 'error': str(err)})
                    reply({'id': req['id'], 'error': f'{type(err).__name__}: {err}'})
                    continue
                served.append({'url': redact_url(res.url), 'status': res.status, 'bytes': len(res.body)})
                if (record_to is not None and redact_url(url) not in new_index and len(res.body) <= FIXTURE_MAX
                        and total[0] + len(res.body) <= FIXTURES_TOTAL_MAX):
                    record_to.mkdir(parents=True, exist_ok=True)
                    ext = 'json' if 'json' in res.content_type else 'xml' if 'xml' in res.content_type else 'html'
                    name = f'{len(new_index) + 1:03d}.{ext}'
                    (record_to / name).write_bytes(res.body)
                    total[0] += len(res.body)
                    new_index[redact_url(url)] = {'file': name, 'status': res.status, 'type': res.headers.get('content-type', '')}
                reply({'id': req['id'], 'status': res.status, 'headers': {'content-type': res.headers.get('content-type', '')},
                       'body': base64.b64encode(res.body).decode()})
            elif line.startswith('@@REPORT '):
                report = json.loads(line[9:])
            else:
                log_tail.append(line.rstrip()[:300])
                del log_tail[:-60]
        proc.wait(timeout=10)
        if timed_out.is_set():
            raise TimeoutError
    except TimeoutError:
        proc.kill()
        report = {'checks': [{'id': 'timeout', 'ok': False, 'severity': 'error', 'detail': f'test run exceeded {timeout:.0f} s'}]}
    except Exception as err:
        proc.kill()
        report = {'checks': [{'id': 'sandbox', 'ok': False, 'severity': 'error', 'detail': f'{type(err).__name__}: {err}'}]}
    finally:
        watchdog.cancel()
        if proc.poll() is None:
            proc.kill()
        drain.join(5)
        stderr = ''.join(err_tail)[-2000:]
        shutil.rmtree(tmp, ignore_errors=True)
    if report is None:
        report = {'checks': [{'id': 'sandbox', 'ok': False, 'severity': 'error',
                              'detail': f'the test process ended without a report (exit {proc.returncode}): {stderr[-800:]}'}]}
    report['served'] = served
    report['log'] = log_tail[-30:]
    report['stderr'] = stderr[-800:]
    if record_to is not None:
        report['fixtures'] = new_index
    return report


def _summary(checks: list[dict]) -> dict:
    errors = [c for c in checks if not c['ok'] and c.get('severity', 'error') == 'error']
    return {'ok': not errors, 'failed': [c['id'] for c in errors]}


def _manifest_checks(draft: pathlib.Path) -> tuple[list[dict], ExtensionManifest | None]:
    checks: list[dict] = []
    if d := denied_in_extension(draft):  # rules R6 / R7 / R8: never generated, never installed
        checks.append({'id': f'domain:{d.rule}', 'ok': False, 'severity': 'error', 'detail': denied_message(d)})
    try:
        m = ExtensionManifest.load(draft / 'manifest.json', 'user')
        checks.append({'id': 'manifest', 'ok': True, 'severity': 'error', 'detail': f'{m.id} ({m.abbr}) {m.version}'})
        if m.id != draft.name:
            checks.append({'id': 'folder', 'ok': False, 'severity': 'error', 'detail': f'folder name must be the id \'{m.id}\''})
        raw = json.loads((draft / 'manifest.json').read_text(encoding='utf-8'))
        checks += [{'id': f'manifest:{v.rule}', 'ok': v.severity != 'error', 'severity': v.severity, 'detail': v.message}
                   for v in check_manifest(raw)]
        return checks, m
    except Exception as err:
        checks.append({'id': 'manifest', 'ok': False, 'severity': 'error', 'detail': str(err)})
        return checks, None


def _policy_checks(draft: pathlib.Path, m: ExtensionManifest | None) -> list[dict]:
    src_path = draft / 'downloader.py'
    if not src_path.is_file():
        return [{'id': 'policy', 'ok': False, 'severity': 'error', 'detail': 'downloader.py is missing'}]
    hosts = {host_of(m.site)} if m and m.site else set()
    hosts |= {'.'.join(h.split('.')[-2:]) for h in hosts}
    violations = check_source(src_path.read_text(encoding='utf-8'), allowed_hosts=hosts)
    out = [{'id': f'policy:{v.rule}', 'ok': v.severity != 'error', 'severity': v.severity, 'detail': f'line {v.line}: {v.message}'}
           for v in violations]
    if job := _tag_update_file(draft, m):  # the extension's own tag update: the same rules, it runs in the app when asked
        if not job.is_file():
            out.append({'id': 'policy:tagUpdate', 'ok': False, 'severity': 'error', 'detail': f'{job.name} (tagUpdate) is missing'})
        else:
            out += [{'id': f'policy:{v.rule}', 'ok': v.severity != 'error', 'severity': v.severity,
                     'detail': f'{job.name} line {v.line}: {v.message}'}
                    for v in check_source(job.read_text(encoding='utf-8'), allowed_hosts=hosts, downloader=False)]
    return out or [{'id': 'policy', 'ok': True, 'severity': 'error', 'detail': 'no violation'}]


def _tag_update_file(draft: pathlib.Path, m: ExtensionManifest | None) -> pathlib.Path | None:
    """The .py file of the manifest's 'tagUpdate' (checked by the manifest: a file at the top of the folder)"""
    entry = str((m.tagUpdate if m else {}).get('entry', ''))
    return draft / entry.split(':', 1)[0] if entry else None


def _recorded_robots(draft: pathlib.Path) -> str | None:
    try:
        with open(draft / ROBOTS_FILE, 'rb') as f:
            return f.read(ROBOTS_MAX_BYTES).decode('utf-8', errors='replace')
    except OSError:
        return None


def _search_checks(m: ExtensionManifest | None, report: dict, robots_text: str | None) -> list[dict]:
    """Kinds of the manifest's 'search' are declared after the robots.txt / terms check: the search address of each kind with a
    token, built in the sandbox for a sample value (report['kindRoutes'], nothing fetched), must be allowed by the robots.txt the
    live run read (record) or saved (check). A kind the downloader refuses is never requested (warning)"""
    kinds = [k for k in m.search.kinds if k.token] if m is not None else []
    if not kinds:
        return []
    if robots_text is None:
        return [{'id': 'search:robots', 'ok': False, 'severity': 'error',
                 'detail': f'the manifest declares search kinds but the draft has no recorded {ROBOTS_FILE}: run record (it saves the '
                           f'site\'s robots.txt), then check each kind\'s route against it and against the terms'}]
    rules = RobotsRules.parse(robots_text, ROBOTS_AGENT)
    site_host = host_of(m.site)
    routes = {r.get('id'): r for r in report.get('kindRoutes') or [] if isinstance(r, dict)}
    out: list[dict] = []
    for k in kinds:
        r = routes.get(k.id)
        cid = f'search:{k.id}'
        if r is None or not r.get('query'):
            why = (r or {}).get('error') or 'no search address was built (the test run stopped before)'
            out.append({'id': cid, 'ok': False, 'severity': 'error', 'detail': f'\'{k.token}\': {why}'})
        elif r.get('error'):
            out.append({'id': cid, 'ok': True, 'severity': 'warning',
                        'detail': f'\'{r.get("query") or k.token}\' is refused, nothing requested: {r["error"]}'})
        else:
            urls = [str(u) for u in r.get('urls') or []]
            other = sorted({host_of(u) for u in urls if host_of(u) != site_host})
            bad = [u for u in urls if host_of(u) == site_host and not rules.allows(u)]
            if other:
                out.append({'id': f'{cid}:hosts', 'ok': True, 'severity': 'warning',
                            'detail': f'\'{r["query"]}\': robots.txt of {", ".join(other)} is not recorded, not checked'})
            out.append({'id': cid, 'ok': bool(urls) and not bad, 'severity': 'error',
                        'detail': (f'robots.txt does not allow the route of \'{r["query"]}\': {redact_url(bad[0])}' if bad else
                                   f'\'{r["query"]}\' -> {redact_url(urls[0])}: allowed by robots.txt' if urls else
                                   f'\'{r["query"]}\': no search address')})
    return out


def record(draft: pathlib.Path, *, query: list[str], limit: int = 3, fetcher: SafeFetcher) -> dict:
    """Live test run: records fixtures and expected.json. Static checks first: code failing them is never run"""
    draft = pathlib.Path(draft).resolve()
    checks, m = _manifest_checks(draft)
    checks += _policy_checks(draft, m)
    if not _summary(checks)['ok']:
        return {'checks': checks, **_summary(checks), 'mode': 'live'}
    fixtures = _fixtures_dir(draft)
    staging = fixtures.with_name('fixtures.new')
    shutil.rmtree(staging, ignore_errors=True)
    report = run_sandbox(draft, query=query, limit=limit, fetcher=fetcher, record_to=staging)
    checks += report.get('checks', [])
    robots_text = fetcher.robots_txt(m.site) if m is not None else None
    checks += _search_checks(m, report, robots_text)
    summary = _summary(checks)
    if summary['ok'] and report.get('fixtures'):
        shutil.rmtree(fixtures, ignore_errors=True)
        staging.rename(fixtures)
        if robots_text is not None:
            (draft / ROBOTS_FILE).write_text(robots_text, encoding='utf-8', newline='\n')
        (fixtures / 'index.json').write_text(json.dumps(report['fixtures'], indent=1, sort_keys=True), encoding='utf-8')
        expected = {'query': query, 'limit': limit, 'ids': report.get('ids', []), 'count': report.get('count', 0),
                    'found': report.get('found', 0)}
        (draft / 'tests' / 'expected.json').write_text(json.dumps(expected, indent=1), encoding='utf-8')
    else:
        shutil.rmtree(staging, ignore_errors=True)
    return {**report, 'checks': checks, **summary, 'mode': 'live'}


def check_draft(draft: pathlib.Path) -> dict:
    """Offline: manifest + policy + replay of recorded fixtures compared with expected.json"""
    draft = pathlib.Path(draft).resolve()
    checks, m = _manifest_checks(draft)
    checks += _policy_checks(draft, m)
    try:
        expected = json.loads((draft / 'tests' / 'expected.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        expected = None
        checks.append({'id': 'fixtures', 'ok': False, 'severity': 'error', 'detail': 'no recorded test run (tests/expected.json)'})
    if not _summary(checks)['ok'] or expected is None:
        return {'checks': checks, **_summary(checks), 'mode': 'replay'}
    report = run_sandbox(draft, query=expected['query'], limit=expected.get('limit', 3))
    checks += report.get('checks', [])
    checks += _search_checks(m, report, _recorded_robots(draft))
    missing = [s['url'] for s in report.get('served', []) if not s.get('fixture')]
    checks.append({'id': 'replay', 'ok': not missing, 'severity': 'error',
                   'detail': f'requests not in fixtures: {missing[:3]}' if missing else f'{len(report.get("served", []))} request(s) replayed'})
    if expected.get('ids'):
        same = report.get('ids', []) == expected['ids']
        checks.append({'id': 'expected', 'ok': same, 'severity': 'error',
                       'detail': 'same posts as the recorded run' if same else f'ids {report.get("ids")} != {expected["ids"]}'})
    return {**report, 'checks': checks, **_summary(checks), 'mode': 'replay'}


def pack(draft: pathlib.Path, dest: pathlib.Path) -> dict:
    """Deterministic zip (sorted names, fixed timestamps, no caches / reports) + sha256. The site's tag list (the manifest's
    tagList, lynxx/extgen/taglist.py) and the extension's own tag update (tagUpdate) go in too"""
    draft = pathlib.Path(draft).resolve()
    m = ExtensionManifest.load(draft / 'manifest.json', 'user')
    files = [draft / f for f in PACK_FILES if (draft / f).is_file()]
    if (tag_list := draft_tag_list(draft)) is not None:
        files.append(tag_list)
    if (job := _tag_update_file(draft, m)) is not None and job.is_file():
        files.append(job)
    tests = draft / 'tests'
    if tests.is_dir():
        files += sorted(p for p in tests.rglob('*') if p.is_file() and '__pycache__' not in p.parts)
    dest = pathlib.Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, 'w', zipfile.ZIP_DEFLATED) as z:
        for p in sorted(files, key=lambda x: x.relative_to(draft).as_posix()):
            info = zipfile.ZipInfo(f'{m.id}/{p.relative_to(draft).as_posix()}', date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, p.read_bytes())
    digest = hashlib.sha256(dest.read_bytes()).hexdigest()
    return {'path': str(dest), 'sha256': digest, 'id': m.id, 'abbr': m.abbr, 'files': len(files)}

#
#
#########################################
