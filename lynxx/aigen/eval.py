# coding=UTF-8
"""
Eval harness of the extension generator (lynxx.aigen): runs generation sessions over a list of target sites and measures
success rate, cost (from the API usage of each session), model turns, tool steps, site requests, time and failures.

  python -m lynxx.aigen.eval run [--suite FILE] (--fixtures DIR | --live [--record DIR]) [--mode template|fake|ai]
                                  [--only name,name] [--out DIR] [--model M] [--max-usd X] [--max-turns N] [--confirm-cost]
  python -m lynxx.aigen.eval estimate [--suite FILE] [--model M] [--max-usd X] [--turns N] [--out-tokens N]
                                       [--result-tokens N] [--reports DIR ...]

Suite (default lynxx/aigen/eval_sites.json): {"targets": [{"name", "url", "media"?, "query"?, "maxUsd"?, "maxTurns"?,
"expect": {"status": "ready" | [..], "engine"?, "ruleIds"?: [...]}}]}.
Website traffic:
- --fixtures DIR: offline replay of recorded responses (DIR/<target name>/index.json + bodies); a request that was not
  recorded gets a 404 and is listed in the report ('missing');
- --live: the real sites through the guarded fetcher (robots.txt, 1 request / s, budget); --record DIR saves every response
  for later offline runs.
Model:
- template: the no-AI mode (free, no key);
- fake: the template plan behind a simulated model that reports token usage from the conversation size (cost / budget
  accounting without any API call);
- ai: Claude through the anthropic SDK, credentials from the environment (ANTHROPIC_API_KEY or an `ant auth login`
  profile). It costs money: refused without --confirm-cost; the run stops when the total reaches the sum of the per target caps.
Drafts are written under the report folder and never installed; every target also checks that nothing was installed and
that the API key (when set in the environment) appears in no draft or report file.
The report (report.json + report.md) goes to --out (default <data dir>/extgen/eval/<timestamp>/). Exit code: 0 every target
met its expectations, 1 some did not, 2 usage error / refused.
"""
#########################################
#
#

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import re
import statistics
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from urllib import parse as url_parse

from requests.structures import CaseInsensitiveDict

__all__ = ('DEFAULT_SUITE', 'FakeModelClient', 'Recorder', 'ReplaySession', 'Target', 'estimate_cost', 'load_suite', 'run_suite')

DEFAULT_SUITE = pathlib.Path(__file__).with_name('eval_sites.json')
re_target_name = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,63}$')
KEPT_HEADERS = ('content-type', 'location', 'cf-mitigated', 'retry-after')
RECORD_BODY_MAX = 2 * 1024 * 1024 + 65536
FIXTURE_INDEX = 'index.json'
FIXTURE_FORMAT = 'lynxx-eval-fixtures/1'
CHARS_PER_TOKEN = 3.5
'''rough size of a token for English prompts and code (the estimate says so); real counts come from the API usage'''


# ----- suite

@dataclass
class Target:
    name: str
    url: str
    expect: dict
    media: list[str] = field(default_factory=lambda: ['image', 'video'])
    query: str = ''
    max_usd: float = 0.0
    max_turns: int = 0
    modes: list[str] = field(default_factory=lambda: ['template', 'fake', 'ai'])
    '''modes the target is meant for (ex. a custom HTML site: only 'ai', the template plan cannot make it)'''
    note: str = ''


def load_suite(path: pathlib.Path) -> list[Target]:
    data = json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    rows = data.get('targets') if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise ValueError(f'{path}: a "targets" list is required')
    out: list[Target] = []
    for r in rows:
        if not isinstance(r, dict) or not isinstance(r.get('url'), str) or not isinstance(r.get('expect'), dict):
            raise ValueError(f'{path}: every target needs "name", "url" and "expect"')
        name = str(r.get('name', ''))
        if not re_target_name.fullmatch(name) or any(t.name == name for t in out):
            raise ValueError(f'{path}: invalid or duplicate target name \'{name}\'')
        if 'status' not in r['expect']:
            raise ValueError(f'{path}: target {name}: expect.status is required')
        modes = list(r.get('modes') or ['template', 'fake', 'ai'])
        if not set(modes) <= {'template', 'fake', 'ai'}:
            raise ValueError(f'{path}: target {name}: unknown mode in {modes}')
        out.append(Target(name=name, url=r['url'], expect=dict(r['expect']), media=list(r.get('media') or ['image', 'video']),
                          query=str(r.get('query') or ''), max_usd=float(r.get('maxUsd') or 0), max_turns=int(r.get('maxTurns') or 0),
                          modes=modes, note=str(r.get('note') or '')))
    return out


# ----- website traffic: replay / record

class _Response:
    """the part of requests.Response that SafeFetcher uses"""

    def __init__(self, url: str, status: int, headers: dict, body: bytes) -> None:
        self.url, self.status_code, self._body = url, status, body
        self.headers = CaseInsensitiveDict(headers)
        self.is_redirect = status in (301, 302, 303, 307, 308) and 'location' in self.headers
        self.is_permanent_redirect = status in (301, 308) and 'location' in self.headers

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def iter_content(self, n: int):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]


def _key(method: str, url: str, accept: str) -> str:
    return f'{method.upper()} {url} accept={accept}'


class Recorder:
    """Responses of one target, saved as DIR/<name>/index.json + bodies"""

    def __init__(self, folder: pathlib.Path, site: str) -> None:
        self.folder = pathlib.Path(folder)
        self.site = site
        self.rows: list[dict] = []
        self._seen: set[str] = set()

    def add(self, method: str, url: str, accept: str, status: int, headers: dict, body: bytes) -> None:
        k = _key(method, url, accept)
        if k in self._seen:
            return
        self._seen.add(k)
        self.folder.mkdir(parents=True, exist_ok=True)
        name = f'{len(self.rows) + 1:04d}.bin'
        (self.folder / name).write_bytes(body)
        self.rows.append({'method': method.upper(), 'url': url, 'accept': accept, 'status': status,
                          'headers': {h: headers[h] for h in KEPT_HEADERS if h in headers}, 'file': name})
        self.save()

    def save(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        doc = {'format': FIXTURE_FORMAT, 'site': self.site, 'recordedAt': _now(), 'responses': self.rows}
        (self.folder / FIXTURE_INDEX).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + '\n', encoding='utf-8')


class RecordingSession:
    """Wraps the fetcher's real requests session: every response is read, kept for the fetcher and recorded"""

    def __init__(self, inner: Any, recorder: Recorder) -> None:
        self.inner = inner
        self.recorder = recorder

    def request(self, method: str, url: str, headers: dict | None = None, **kw: Any) -> _Response:
        accept = str((headers or {}).get('Accept', ''))
        with self.inner.request(method, url, headers=headers, **kw) as r:
            body = b''
            if method.upper() != 'HEAD':
                for chunk in r.iter_content(65536):
                    body += chunk
                    if len(body) > RECORD_BODY_MAX:
                        break
            hdrs = {k.lower(): v for k, v in r.headers.items()}
            status = r.status_code
        self.recorder.add(method, url, accept, status, hdrs, body)
        return _Response(url, status, hdrs, body)


class ReplaySession:
    """Serves recorded responses (offline). Unknown requests get a 404 and are listed in missing"""

    def __init__(self, folder: pathlib.Path) -> None:
        self.folder = pathlib.Path(folder)
        self.by_key: dict[str, dict] = {}
        self.by_url: dict[str, dict] = {}
        self.missing: list[str] = []
        self.calls: list[tuple[str, str]] = []
        index = self.folder / FIXTURE_INDEX
        if index.is_file():
            doc = json.loads(index.read_text(encoding='utf-8'))
            for row in doc.get('responses') or []:
                self.by_key.setdefault(_key(row['method'], row['url'], row.get('accept', '')), row)
                self.by_url.setdefault(f'{row["method"]} {row["url"]}', row)

    def request(self, method: str, url: str, headers: dict | None = None, **_: Any) -> _Response:
        self.calls.append((method.upper(), url))
        accept = str((headers or {}).get('Accept', ''))
        row = self.by_key.get(_key(method, url, accept)) or self.by_url.get(f'{method.upper()} {url}')
        if row is None:
            if url not in self.missing:
                self.missing.append(url)
            return _Response(url, 404, {'content-type': 'text/plain', 'x-lynxx-replay': 'missing'}, b'not recorded')
        body = (self.folder / row['file']).read_bytes() if (self.folder / row['file']).is_file() else b''
        return _Response(url, int(row['status']), row.get('headers') or {}, body)


# ----- simulated model

class FakeModelClient:
    """The template plan (lynxx.aigen.template_agent) behind a simulated model: every response reports token usage from the
    size of the request (cache write of the new part, cache read of the part sent before), like a model with prompt caching.
    No API call. Used by the 'fake' mode and the tests"""

    def __init__(self, *, query: str = '', media: list[str] | None = None, output_tokens: int = 400) -> None:
        from .template_agent import TemplateClient
        self.plan = TemplateClient(query=query, media=media)
        self.output_tokens = output_tokens
        self._sent = 0
        self.requests = 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **params: Any) -> Any:
        stream = self.plan._stream(**params)
        size = int(len(json.dumps({k: params.get(k) for k in ('system', 'tools', 'messages')}, default=str)) / CHARS_PER_TOKEN)
        message = stream.get_final_message()
        message.usage = SimpleNamespace(input_tokens=60, output_tokens=self.output_tokens, cache_read_input_tokens=self._sent,
                                        cache_creation_input_tokens=max(0, size - self._sent))
        self._sent = size
        self.requests += 1
        return stream


# ----- run

def _now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def _site_hosts(url: str) -> set[str]:
    host = (url_parse.urlsplit(url).hostname or '').lower()
    return {host, '.'.join(host.split('.')[-2:])} if host else set()


def _installed_snapshot() -> list[str]:
    from lynxx.extensions import Extensions
    d = Extensions.user_dir()
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def _secret_leaks(folder: pathlib.Path, secret: str) -> list[str]:
    if not secret or not folder.is_dir():
        return []
    needle = secret.encode()
    return [str(p.relative_to(folder)) for p in folder.rglob('*') if p.is_file() and needle in p.read_bytes()]


def _expect_checks(t: Target, report: dict) -> list[dict]:
    exp = t.expect
    want = exp['status'] if isinstance(exp['status'], list) else [exp['status']]
    checks = [{'id': 'status', 'ok': report['status'] in want, 'detail': f'{report["status"]} (expected {" | ".join(want)})'}]
    if exp.get('engine'):
        checks.append({'id': 'engine', 'ok': report.get('engine') == exp['engine'],
                       'detail': f'{report.get("engine") or "-"} (expected {exp["engine"]})'})
    if exp.get('ruleIds'):
        missing = [r for r in exp['ruleIds'] if r not in (report.get('ruleIds') or [])]
        checks.append({'id': 'ruleIds', 'ok': not missing, 'detail': f'{report.get("ruleIds")} (expected {exp["ruleIds"]})'})
    return checks


def run_target(t: Target, *, mode: str, drafts_root: pathlib.Path, model: str, max_usd: float, max_turns: int,
               fixtures: pathlib.Path | None = None, record: pathlib.Path | None = None,
               client_factory: Callable[[Target], Any] | None = None, secret: str = '') -> dict:
    from lynxx.extgen.fetch import FetchPolicyError, SafeFetcher, normalize_site
    from lynxx.extgen.scaffold import fetcher_for

    from .session import GenRequest, GenSession
    replay: ReplaySession | None = None
    recorder: Recorder | None = None
    try:
        site = normalize_site(t.url)
    except FetchPolicyError:
        site = ''  # refused by the session itself (denylist, http...): a fetcher that reaches nothing
    if fixtures is not None or not site:
        replay = ReplaySession(pathlib.Path(fixtures or drafts_root) / t.name)

        class ReplayFetcher(SafeFetcher):
            def _pace(self, url: str, st: Any) -> None:  # recorded responses: no site to be polite to
                st.last = time.monotonic()
        fetcher = ReplayFetcher(allowed_hosts=_site_hosts(site), budget=60, min_interval=0.0, allow_local=True)
        fetcher._session = replay  # recorded responses only: no connection is ever made (allow_local skips DNS)
    else:
        fetcher = fetcher_for(site, budget=60)
        if record is not None:
            recorder = Recorder(pathlib.Path(record) / t.name, site)
            fetcher._session = RecordingSession(fetcher._session, recorder)
    req = GenRequest(url=t.url, media=t.media, model=model, max_usd=t.max_usd or max_usd, max_turns=t.max_turns or max_turns,
                     mode='template' if mode == 'template' else 'ai', query=t.query,
                     tag_list=False)  # the eval measures the generator: no tag list traffic (not in the fixtures either)
    client = None
    if mode == 'fake':
        client = FakeModelClient(query=t.query, media=t.media)
    elif mode == 'ai':
        client = client_factory(t) if client_factory else None
    events: list[tuple[str, dict]] = []
    before = _installed_snapshot()
    t0 = time.monotonic()
    report = GenSession(req, drafts_root=drafts_root / t.name, client=client, fetcher=fetcher,
                        emit=lambda kind, data: events.append((kind, data))).run()
    seconds = round(time.monotonic() - t0, 2)
    tools = Counter(d.get('tool', '') for k, d in events if k == 'phase' and d.get('state') == 'running' and d.get('tool') != 'check')
    checks = _expect_checks(t, report)
    after = _installed_snapshot()
    checks.append({'id': 'installedNothing', 'ok': before == after, 'detail': 'no extension installed' if before == after
                   else f'installed extensions changed: {sorted(set(after) - set(before))}'})
    leaks = _secret_leaks(drafts_root / t.name, secret)
    checks.append({'id': 'noSecret', 'ok': not leaks, 'detail': 'API key in no file' if not leaks else f'API key found in {leaks[:5]}'})
    failures = [f'{c["id"]}: {c["detail"]}' for c in checks if not c['ok']]
    if failures and report.get('summary'):
        failures.append(f'summary: {str(report["summary"])[:400]}')
    failed_checks = [c.get('id') for c in report.get('checks') or [] if not c.get('ok') and c.get('severity') == 'error']
    return {
        'name': t.name, 'url': t.url, 'mode': mode, 'model': report.get('model') or '', 'ok': not [c for c in checks if not c['ok']],
        'status': report['status'], 'expect': t.expect, 'engine': report.get('engine', ''), 'ruleIds': report.get('ruleIds', []),
        'checks': checks, 'failures': failures, 'draftChecksFailed': failed_checks,
        'usd': report.get('usd', 0.0), 'maxUsd': req.max_usd, 'overBudget': report.get('usd', 0.0) > req.max_usd,
        'usage': report.get('usage', {}), 'turns': report.get('turns', 0), 'steps': sum(tools.values()), 'tools': dict(tools),
        'siteRequests': report.get('requests', 0), 'testRuns': report.get('testRuns', 0), 'seconds': seconds,
        'missing': list(replay.missing) if replay else [], 'recorded': len(recorder.rows) if recorder else 0,
        'session': report.get('session', ''), 'summary': str(report.get('summary') or '')[:1000],
    }


def _aggregate(rows: list[dict]) -> dict:
    n = len(rows)
    passed = [r for r in rows if r['ok']]
    usd = [r['usd'] for r in rows]
    total = round(sum(usd), 4)
    return {
        'targets': n, 'passed': len(passed), 'successRate': round(len(passed) / n, 3) if n else 0.0,
        'totalUsd': total, 'meanUsd': round(total / n, 4) if n else 0.0,
        'usdPerPass': round(total / len(passed), 4) if passed else None,
        'meanTurns': round(statistics.fmean(r['turns'] for r in rows), 2) if n else 0.0,
        'meanSteps': round(statistics.fmean(r['steps'] for r in rows), 2) if n else 0.0,
        'meanSeconds': round(statistics.fmean(r['seconds'] for r in rows), 2) if n else 0.0,
        'tokens': {k: sum(int(r['usage'].get(k, 0) or 0) for r in rows) for k in ('input', 'output', 'cacheRead', 'cacheWrite')},
        'byStatus': dict(Counter(r['status'] for r in rows)),
        'failures': {r['name']: r['failures'] for r in rows if r['failures']},
        'missingRequests': sum(len(r['missing']) for r in rows),
    }


def run_suite(targets: list[Target], *, mode: str, out_dir: pathlib.Path, model: str = '', max_usd: float = 2.0, max_turns: int = 30,
              fixtures: pathlib.Path | None = None, record: pathlib.Path | None = None,
              client_factory: Callable[[Target], Any] | None = None, max_total_usd: float = 0.0,
              progress: Callable[[str], None] | None = None) -> dict:
    """Runs every target and writes report.json + report.md into out_dir. mode: template | fake | ai"""
    from .session import DEFAULT_MODEL, MODELS
    model = model or DEFAULT_MODEL
    if mode not in ('template', 'fake', 'ai'):
        raise ValueError(f'unknown mode {mode}')
    if model not in MODELS:
        raise ValueError(f'unsupported model {model}')
    if mode == 'ai' and client_factory is None:
        raise ValueError('the ai mode needs a client factory')
    out_dir = pathlib.Path(out_dir)
    drafts = out_dir / 'drafts'
    drafts.mkdir(parents=True, exist_ok=True)
    secret = os.environ.get('ANTHROPIC_API_KEY', '') if mode == 'ai' else ''
    rows: list[dict] = []
    started = _now()
    for t in targets:
        spent = sum(r['usd'] for r in rows)
        if max_total_usd and spent >= max_total_usd:
            rows.append({'name': t.name, 'url': t.url, 'mode': mode, 'model': '', 'ok': False, 'status': 'skipped', 'expect': t.expect,
                         'engine': '', 'ruleIds': [], 'checks': [], 'failures': [f'skipped: total cost cap {max_total_usd:.2f} USD reached'],
                         'draftChecksFailed': [], 'usd': 0.0, 'maxUsd': 0.0, 'overBudget': False, 'usage': {}, 'turns': 0, 'steps': 0,
                         'tools': {}, 'siteRequests': 0, 'testRuns': 0, 'seconds': 0.0, 'missing': [], 'recorded': 0, 'session': '',
                         'summary': ''})
            continue
        if progress:
            progress(f'[{len(rows) + 1}/{len(targets)}] {t.name} ({t.url})')
        row = run_target(t, mode=mode, drafts_root=drafts, model=model, max_usd=max_usd, max_turns=max_turns, fixtures=fixtures,
                         record=record, client_factory=client_factory, secret=secret)
        rows.append(row)
        if progress:
            progress(f'    {"OK " if row["ok"] else "FAIL"} {row["status"]}, {row["usd"]:.4f} USD, {row["turns"]} turns, '
                     f'{row["steps"]} steps, {row["seconds"]} s{"; " + row["failures"][0] if row["failures"] else ""}')
    report = {
        'tool': 'lynxx.aigen.eval', 'startedAt': started, 'finishedAt': _now(), 'mode': mode,
        'model': model if mode == 'ai' else ('simulated' if mode == 'fake' else ''),
        'traffic': 'replay' if fixtures is not None else ('live+record' if record is not None else 'live'),
        'fixtures': str(fixtures or ''), 'summary': _aggregate(rows), 'targets': rows,
    }
    (out_dir / 'report.json').write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str) + '\n', encoding='utf-8')
    (out_dir / 'report.md').write_text(markdown_report(report), encoding='utf-8')
    return report


def markdown_report(report: dict) -> str:
    s = report['summary']
    model = f', model `{report["model"]}`' if report['model'] else ''
    per_pass = f', {s["usdPerPass"]:.4f} USD / site đạt' if s['usdPerPass'] is not None else ''
    lines = [
        '# Đánh giá trình tạo extension (lynxx.aigen.eval)', '',
        f'- Chế độ: `{report["mode"]}`{model}; dữ liệu site: `{report["traffic"]}`',
        f'- Thời gian: {report["startedAt"]} → {report["finishedAt"]}',
        f'- Đạt: **{s["passed"]} / {s["targets"]}** ({s["successRate"] * 100:.0f} %)',
        f'- Chi phí: tổng {s["totalUsd"]:.4f} USD, trung bình {s["meanUsd"]:.4f} USD / site{per_pass}',
        f'- Trung bình: {s["meanTurns"]} lượt model, {s["meanSteps"]} bước tool, {s["meanSeconds"]} s',
        (f'- Token: vào {s["tokens"]["input"]}, ra {s["tokens"]["output"]}, đọc cache {s["tokens"]["cacheRead"]}, '
         f'ghi cache {s["tokens"]["cacheWrite"]}'),
        f'- Request không có trong fixture: {s["missingRequests"]}', '',
        '| Site | Kết quả | Trạng thái (mong đợi) | Engine | USD | Lượt | Bước | Request site | Giây |',
        '|---|---|---|---|---|---|---|---|---|',
    ]
    for r in report['targets']:
        want = r['expect'].get('status')
        want = ' | '.join(want) if isinstance(want, list) else want
        lines.append(f'| {r["name"]} | {"đạt" if r["ok"] else "**không đạt**"} | {r["status"]} ({want}) | {r["engine"] or "-"} | '
                     f'{r["usd"]:.4f} | {r["turns"]} | {r["steps"]} | {r["siteRequests"]} | {r["seconds"]} |')
    if s['failures']:
        lines += ['', '## Lỗi', '']
        for name, items in s['failures'].items():
            lines.append(f'- **{name}**')
            lines += [f'  - {x.replace(chr(10), " ")[:500]}' for x in items]
    return '\n'.join(lines) + '\n'


# ----- cost estimate

def _usd(tokens: dict, model: str) -> float:
    from .session import MODELS
    p = MODELS[model]
    return (tokens['input'] * p[0] + tokens['output'] * p[1] + tokens['cacheRead'] * p[2] + tokens['cacheWrite'] * p[3]) / 1e6


def estimate_cost(n_targets: int, *, model: str = '', max_usd: float = 2.0, turns: int = 14, out_tokens: int = 1500,
                  result_tokens: int = 2500, prefix_tokens: int = 0, reports: list[pathlib.Path] | None = None) -> dict:
    """Cost of an ai run before running it:
    - worst case: every session stops at its cost cap (the session never starts a turn past it);
    - model: one session = 'turns' requests with prompt caching: turn 1 writes the cached prefix (system prompt + tools),
      turn k reads prefix + history and writes the last turn's output + tool results ('out_tokens' + 'result_tokens'),
      each turn outputs 'out_tokens' (thinking included). Token sizes of text are estimated at 3.5 characters per token;
    - measured: the mean cost of past ai sessions found in 'reports' (eval report.json files or draft report.json files)"""
    from .prompts import SYSTEM_PROMPT
    from .session import DEFAULT_MODEL
    from .tools import TOOLS
    model = model or DEFAULT_MODEL
    prefix = prefix_tokens or int((len(SYSTEM_PROMPT) + len(json.dumps(TOOLS))) / CHARS_PER_TOKEN)
    tokens = {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}
    history = 0
    for k in range(turns):
        tokens['input'] += 60  # the uncached tail (new user text)
        if k == 0:
            tokens['cacheWrite'] += prefix + 100
        else:
            tokens['cacheRead'] += prefix + 100 + history
            tokens['cacheWrite'] += out_tokens + result_tokens
            history += out_tokens + result_tokens
        tokens['output'] += out_tokens
    per_session = min(_usd(tokens, model), max_usd)
    measured: list[float] = []
    for path in reports or []:
        for f in ([path] if pathlib.Path(path).is_file() else sorted(pathlib.Path(path).rglob('report.json'))):
            try:
                data = json.loads(pathlib.Path(f).read_text(encoding='utf-8'))
            except (OSError, ValueError):
                continue
            rows = data.get('targets') if isinstance(data.get('targets'), list) else [data]
            measured += [float(r.get('usd') or 0) for r in rows
                         if isinstance(r, dict) and r.get('mode') == 'ai' and (r.get('model') or '') == model and float(r.get('usd') or 0) > 0]
    return {
        'model': model, 'targets': n_targets, 'maxUsdPerSession': max_usd,
        'worstCaseUsd': round(n_targets * max_usd, 2),
        'modelEstimate': {'perSessionUsd': round(per_session, 4), 'totalUsd': round(per_session * n_targets, 2), 'tokensPerSession': tokens,
                          'assumptions': {'turns': turns, 'outputTokensPerTurn': out_tokens, 'toolResultTokensPerTurn': result_tokens,
                                          'cachedPrefixTokens': prefix, 'charsPerToken': CHARS_PER_TOKEN}},
        'measured': {'sessions': len(measured), 'meanUsd': round(statistics.fmean(measured), 4) if measured else None,
                     'totalUsd': round(statistics.fmean(measured) * n_targets, 2) if measured else None},
    }


# ----- command line

def _anthropic_factory() -> Callable[[Target], Any]:
    try:
        import anthropic
    except ImportError:
        raise SystemExit('the ai mode needs the anthropic package: pip install "lynxx[ai]"')
    client = anthropic.Anthropic(max_retries=3)  # credentials from the environment, never printed

    def factory(_t: Target) -> Any:
        return client
    return factory


def main(argv: list[str] | None = None) -> int:
    from lynxx.logger import Logger
    Logger.init(True, True)
    from .session import DEFAULT_MODEL, MODELS
    ap = argparse.ArgumentParser(prog='python -m lynxx.aigen.eval', allow_abbrev=False,
                                 description='Eval harness of the Lynxx extension generator')
    sub = ap.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run', help='run the suite and write a report')
    r.add_argument('--suite', default=str(DEFAULT_SUITE))
    r.add_argument('--mode', choices=('template', 'fake', 'ai'), default='template')
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument('--fixtures', default='', help='offline: recorded responses (DIR/<target>/index.json)')
    src.add_argument('--live', action='store_true', help='real websites (polite, robots.txt)')
    r.add_argument('--record', default='', help='with --live: save every response into DIR for offline runs')
    r.add_argument('--only', default='', help='comma separated target names')
    r.add_argument('--out', default='')
    r.add_argument('--model', default=DEFAULT_MODEL, choices=sorted(MODELS))
    r.add_argument('--max-usd', type=float, default=2.0, help='cost cap of one session (ai mode)')
    r.add_argument('--max-turns', type=int, default=30)
    r.add_argument('--confirm-cost', action='store_true', help='required for --mode ai: the run calls the paid Claude API')
    e = sub.add_parser('estimate', help='cost estimate of an ai run, no API call')
    e.add_argument('--suite', default=str(DEFAULT_SUITE))
    e.add_argument('--only', default='')
    e.add_argument('--model', default=DEFAULT_MODEL, choices=sorted(MODELS))
    e.add_argument('--max-usd', type=float, default=2.0)
    e.add_argument('--turns', type=int, default=14)
    e.add_argument('--out-tokens', type=int, default=1500)
    e.add_argument('--result-tokens', type=int, default=2500)
    e.add_argument('--reports', action='append', default=[], help='past report.json files / folders (eval or drafts) to average')
    a = ap.parse_args(argv)
    try:
        targets = load_suite(pathlib.Path(a.suite))
    except (OSError, ValueError) as err:
        print(f'error: {err}', file=sys.stderr)
        return 2
    if a.only:
        names = {x.strip() for x in a.only.split(',') if x.strip()}
        targets = [t for t in targets if t.name in names]
    mode = getattr(a, 'mode', 'ai')
    targets = [t for t in targets if mode in t.modes]
    if not targets:
        print('error: no target of the suite matches --only / the mode', file=sys.stderr)
        return 2
    if a.cmd == 'estimate':
        from lynxx.extensions import data_dir
        reports = [pathlib.Path(x) for x in a.reports] or [p for p in (data_dir() / 'extgen' / 'eval', data_dir() / 'extgen' / 'drafts')
                                                          if p.is_dir()]
        res = estimate_cost(len(targets), model=a.model, max_usd=a.max_usd, turns=a.turns, out_tokens=a.out_tokens,
                            result_tokens=a.result_tokens, reports=reports)
        print(json.dumps(res, indent=1))
        return 0
    if a.record and not a.live:
        print('error: --record needs --live', file=sys.stderr)
        return 2
    factory = None
    max_total = 0.0
    if a.mode == 'ai':
        est = estimate_cost(len(targets), model=a.model, max_usd=a.max_usd)
        max_total = sum(t.max_usd or a.max_usd for t in targets)
        print(f'ai mode: {len(targets)} target(s), model {a.model}, at most {a.max_usd:.2f} USD per session, run stops at '
              f'{max_total:.2f} USD; model estimate {est["modelEstimate"]["totalUsd"]:.2f} USD', file=sys.stderr)
        if not a.confirm_cost:
            print('refused: this calls the paid Claude API. Add --confirm-cost to run it.', file=sys.stderr)
            return 2
        factory = _anthropic_factory()
    from lynxx.extensions import data_dir
    out = pathlib.Path(a.out) if a.out else data_dir() / 'extgen' / 'eval' / time.strftime('%Y%m%d-%H%M%S')
    report = run_suite(targets, mode=a.mode, out_dir=out, model=a.model, max_usd=a.max_usd, max_turns=a.max_turns,
                       fixtures=pathlib.Path(a.fixtures) if a.fixtures else None, record=pathlib.Path(a.record) if a.record else None,
                       client_factory=factory, max_total_usd=max_total, progress=lambda line: print(line, file=sys.stderr))
    s = report['summary']
    print(json.dumps({'report': str(out / 'report.json'), 'markdown': str(out / 'report.md'), **{k: s[k] for k in (
        'targets', 'passed', 'successRate', 'totalUsd', 'meanUsd', 'meanTurns', 'meanSteps', 'missingRequests')}}, indent=1))
    return 0 if s['passed'] == s['targets'] else 1


if __name__ == '__main__':
    sys.exit(main())

#
#
#########################################
