# coding=UTF-8
"""
Lynxx AI extension generator: one generation session.
Drives the model (Claude API, streaming, the client-side tools of tools.ToolBox) under turn / cost / test budgets, then checks
the draft independently of what the model says. The result is a draft folder with report.json: it is never installed here.
"""
#########################################
#
#

from __future__ import annotations

import datetime
import json
import pathlib
import re
import secrets
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .prompts import SYSTEM_PROMPT
from .tools import TOOLS, ToolBox, refusal_text
from ..extgen.fetch import FetchPolicyError, SafeFetcher, normalize_site
from ..extgen.sandbox import check_draft
from ..extgen.scaffold import fetcher_for

__all__ = ('DEFAULT_MODEL', 'MODELS', 'GenRequest', 'GenSession', 'list_sessions')

DEFAULT_MODEL = 'claude-opus-5-5'
# USD per million tokens: input, output, cache read, cache write (5 min TTL = 1.25 x input)
MODELS = {
    'claude-opus-5-5': (4.0, 20.0, 0.20, 5.0),
    'claude-sonnet-5-5': (2.0, 10.0, 0.10, 2.5),
}
FALLBACK_BETA = 'server-side-fallback-2026-07-01'
MAX_TOKENS = 64000
MAX_JSON_RETRIES = 2
MAX_NUDGES = 2
SESSION_RE = r'\d{8}-\d{6}-[0-9a-f]{6}'
TOOL_LABELS = {
    'survey_site': 'Đọc robots.txt và nhận diện engine của site',
    'http_fetch': 'Xem một trang / API của site',
    'create_draft': 'Tạo thư mục bản nháp',
    'read_reference': 'Đọc mã tham khảo của Lynxx',
    'read_file': 'Đọc tệp bản nháp',
    'write_file': 'Viết tệp bản nháp',
    'static_check': 'Kiểm tra tĩnh downloader.py',
    'test_run': 'Chạy thử trong sandbox',
    'finish': 'Kết thúc phiên',
}


@dataclass
class GenRequest:
    url: str
    media: list[str] = field(default_factory=lambda: ['image', 'video'])
    model: str = DEFAULT_MODEL
    max_usd: float = 2.0
    max_turns: int = 30
    mode: str = 'ai'  # 'ai': Claude writes / fixes the code; 'template': no model, only gelbooru / danbooru sites
    query: str = ''  # template mode: tags of the test run (empty: picked from the site's tags)
    repair: str = ''  # session id of a draft that did not pass: copied into this session, the model fixes it (ai mode only)
    tag_list: bool = True  # a ready draft gets the site's tag list (lynxx/extgen/taglist.py); the eval harness turns it off

    @classmethod
    def from_dict(cls, d: dict) -> GenRequest:
        media = [m for m in d.get('media') or ['image', 'video'] if m in ('image', 'video', 'audio')] or ['image', 'video']
        model = str(d.get('model') or DEFAULT_MODEL)
        if model not in MODELS:
            raise ValueError(f'unsupported model {model}')
        mode = str(d.get('mode') or 'ai')
        if mode not in ('ai', 'template'):
            raise ValueError(f'unsupported mode {mode}')
        repair = str(d.get('repair') or '')
        if repair and (not re.fullmatch(SESSION_RE, repair) or mode != 'ai'):
            raise ValueError('repair needs the ai mode and a draft session id')
        return cls(url=str(d.get('url') or ''), media=media, model=model, max_usd=max(0.1, min(float(d.get('maxUsd') or 2.0), 20.0)),
                   max_turns=max(5, min(int(d.get('maxTurns') or 30), 60)), mode=mode, query=str(d.get('query') or '')[:200],
                   repair=repair, tag_list=d.get('tagList', True) is not False)


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    requests: int = 0

    def add(self, u: Any) -> None:
        if u is None:
            return
        self.requests += 1
        self.input += int(getattr(u, 'input_tokens', 0) or 0)
        self.output += int(getattr(u, 'output_tokens', 0) or 0)
        self.cache_read += int(getattr(u, 'cache_read_input_tokens', 0) or 0)
        self.cache_write += int(getattr(u, 'cache_creation_input_tokens', 0) or 0)

    def usd(self, model: str) -> float:
        p = MODELS.get(model, MODELS[DEFAULT_MODEL])
        return round((self.input * p[0] + self.output * p[1] + self.cache_read * p[2] + self.cache_write * p[3]) / 1e6, 4)

    def to_dict(self) -> dict:
        return {'input': self.input, 'output': self.output, 'cacheRead': self.cache_read, 'cacheWrite': self.cache_write,
                'requests': self.requests}


def _now() -> str:
    return datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')


def _block_type(b: Any) -> str:
    return getattr(b, 'type', '') if not isinstance(b, dict) else b.get('type', '')


class GenSession:
    """One site -> one draft. emit(type, data) receives progress events; cancel stops between steps and while streaming"""

    def __init__(self, req: GenRequest, *, drafts_root: pathlib.Path, client: Any = None,
                 emit: Callable[[str, dict], None] | None = None, fetcher: SafeFetcher | None = None,
                 cancel: threading.Event | None = None) -> None:
        self.req = req
        self.id = f'{time.strftime("%Y%m%d-%H%M%S")}-{secrets.token_hex(3)}'
        self.dir = pathlib.Path(drafts_root) / self.id
        self.client = client
        self._emit = emit or (lambda _t, _d: None)
        self.fetcher = fetcher
        self.cancel = cancel or threading.Event()
        self.usage = Usage()
        self.turns = 0
        self.log: list[dict] = []
        self.box: ToolBox | None = None
        self.started = _now()
        self.repair_note = ''

    # ----- events

    def emit(self, kind: str, **data: Any) -> None:
        if kind in ('log', 'phase'):
            self.log.append({'t': round(time.time(), 1), 'kind': kind, **{k: v for k, v in data.items() if k != 'detail'}})
            del self.log[:-300]
        self._emit(kind, data)

    def _usd(self) -> float:
        return self.usage.usd(self.req.model)

    # ----- run

    def run(self) -> dict:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.emit('start', session=self.id, url=self.req.url, mode=self.req.mode, model=self.req.model if self.req.mode == 'ai' else '')
        status, summary, rule_ids, error = 'error', '', [], ''
        try:
            site = normalize_site(self.req.url)
            fetcher = self.fetcher or fetcher_for(site, budget=60)
            generator = {'tool': 'lynxx.aigen', 'session': self.id, 'mode': self.req.mode}
            if self.req.mode == 'ai':
                generator['model'] = self.req.model
            self.box = ToolBox(site_url=site, drafts_root=self.dir, media=self.req.media, fetcher=fetcher, generator=generator)
            self.repair_note = self._start_repair() if self.req.repair else ''
            status, summary, rule_ids = self._loop()
        except FetchPolicyError as err:
            status, summary, rule_ids = 'refused', refusal_text(err.rule, err.message), [err.rule]
        except Exception as err:  # API / network / unexpected: reported, never raised to the worker
            status, error = 'error', f'{type(err).__name__}: {err}'
            summary = _api_error_text(err)
        if self.cancel.is_set() and status == 'error':  # an error caused by stopping mid-request
            status = 'cancelled'
        report = self._finalize(status, summary, rule_ids, error)
        self.emit('done', **{k: report[k] for k in ('session', 'status', 'summary', 'ruleIds', 'draftDir', 'extId', 'usd')},
                  report=report)
        return report

    def _loop(self) -> tuple[str, str, list[str]]:
        box = self.box
        client = self.client
        if self.req.mode == 'template':
            from .template_agent import TemplateClient
            client = TemplateClient(query=self.req.query, media=self.req.media)
        if client is None:
            raise RuntimeError('no Claude client')
        user = (f'Website: {box.site_url}\nMedia the user wants: {", ".join(self.req.media)}\n'
                f'Today is {datetime.date.today().isoformat()}. Create the extension draft for this website.')
        if self.req.repair:
            user = (f'Website: {box.site_url}\nToday is {datetime.date.today().isoformat()}.\n'
                    f'Repair session: the draft below was made earlier and did not pass. Its files are already in place '
                    f'(read_file; create_draft is not needed). Find the cause, fix downloader.py (and manifest.json if needed), '
                    f'run test_run until it passes, then finish.\n\n{self.repair_note}')
        messages: list[dict] = [{'role': 'user', 'content': user}]
        json_retries = nudges = 0
        while True:
            if self.cancel.is_set():
                return 'cancelled', 'Đã huỷ.', []
            if self.turns >= self.req.max_turns:
                return 'failed', f'Hết giới hạn {self.req.max_turns} lượt gọi model.', ['budget']
            if self._usd() >= self.req.max_usd:
                return 'failed', f'Hết ngân sách {self.req.max_usd:.2f} USD (đã dùng {self._usd():.2f} USD).', ['budget']
            self.turns += 1
            try:
                response = self._request(client, messages)
            except ValueError:  # tool input JSON the SDK could not parse at all: no tool_use id to answer, re-issue the turn
                json_retries += 1
                if json_retries > MAX_JSON_RETRIES:
                    raise
                self.emit('log', text='Model trả về JSON không hợp lệ, gửi lại lượt này.')
                continue
            json_retries = 0
            if response is None:  # cancelled while streaming
                return 'cancelled', 'Đã huỷ.', []
            self.usage.add(getattr(response, 'usage', None))
            self.emit('usage', usd=self._usd(), maxUsd=self.req.max_usd, turns=self.turns, **self.usage.to_dict())
            content = list(getattr(response, 'content', []) or [])
            for b in content:
                if _block_type(b) == 'text' and getattr(b, 'text', '').strip():
                    self.emit('log', text=getattr(b, 'text').strip()[:2000], source='model')
                elif _block_type(b) == 'fallback':
                    self.emit('log', text='Model chính từ chối, yêu cầu được chạy lại trên model dự phòng.')
            stop = getattr(response, 'stop_reason', '')
            if stop == 'refusal':
                details = getattr(response, 'stop_details', None)
                category = getattr(details, 'category', '') if details is not None else ''
                return 'refused', f'Model từ chối yêu cầu{f" ({category})" if category else ""}.', ['model-refusal']
            if stop == 'pause_turn':
                messages.append({'role': 'assistant', 'content': content})
                continue
            tool_uses = [b for b in content if _block_type(b) == 'tool_use']
            messages.append({'role': 'assistant', 'content': content})
            if not tool_uses:
                nudges += 1
                if nudges > MAX_NUDGES:
                    return 'failed', 'Model dừng mà không gọi finish.', []
                messages.append({'role': 'user', 'content': 'Continue with the tools. End the session only by calling finish.'})
                continue
            results = []
            for b in tool_uses:
                if stop == 'max_tokens':
                    results.append({'type': 'tool_result', 'tool_use_id': b.id, 'is_error': True,
                                    'content': 'Your output was cut at max_tokens before this call was complete: it was not run. '
                                               'Make one smaller call at a time.'})
                    continue
                results.append(self._run_tool(b))
                if box.finished or self.cancel.is_set():
                    break
            answered = {r['tool_use_id'] for r in results}
            for b in tool_uses:  # every tool_use needs a result, even the ones skipped after finish / cancel
                if b.id not in answered:
                    results.append({'type': 'tool_result', 'tool_use_id': b.id, 'is_error': True, 'content': 'not run'})
            messages.append({'role': 'user', 'content': results})
            if box.finished:
                f = box.finished
                return f['status'], f['summary'], f['rule_ids']

    def _start_repair(self) -> str:
        """copies the draft of a previous session into this one (the old session stays as it was) and describes why it failed.
        Text from the old report (website data included) is marked as untrusted"""
        old_dir = self.dir.parent / self.req.repair
        try:
            old = json.loads((old_dir / 'report.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            raise ValueError(f'draft session {self.req.repair} has no report')
        ext_id = str(old.get('extId') or '')
        src = old_dir / ext_id
        if not re.fullmatch(r'[a-z][a-z0-9_-]{1,31}', ext_id) or not (src / 'manifest.json').is_file():
            raise ValueError(f'draft session {self.req.repair} has no draft folder')
        dst = self.dir / ext_id
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns('fixtures.new', '__pycache__'))
        self.box.draft = dst
        failed = [c for c in old.get('checks') or [] if isinstance(c, dict) and not c.get('ok') and c.get('severity') == 'error']
        lines = [f'- {c.get("id")}: {str(c.get("detail") or "")[:600]}' for c in failed[:12]] or ['- (no failed check recorded)']
        self.emit('log', text=f'Sửa bản nháp {ext_id} của phiên {self.req.repair} (bản cũ giữ nguyên).')
        return (f'Draft id: {ext_id}. Previous status: {old.get("status")}. Previous summary (untrusted, may quote the website): '
                f'{str(old.get("summary") or "")[:1500]}\nFailed checks of the last test run:\n' + '\n'.join(lines))

    def _request(self, client: Any, messages: list[dict]) -> Any:
        params: dict[str, Any] = {
            'model': self.req.model,
            'max_tokens': MAX_TOKENS,
            'system': [{'type': 'text', 'text': SYSTEM_PROMPT, 'cache_control': {'type': 'ephemeral'}}],
            'tools': TOOLS,
            'messages': messages,
            'output_config': {'effort': 'high'},
            'cache_control': {'type': 'ephemeral'},  # automatic caching of the growing conversation
            'betas': [FALLBACK_BETA],
            'fallbacks': 'default',
        }
        with client.beta.messages.stream(**params) as stream:
            for event in stream:
                if self.cancel.is_set():
                    return None
                if getattr(event, 'type', '') == 'content_block_start':
                    block = getattr(event, 'content_block', None)
                    if _block_type(block) == 'tool_use':
                        name = getattr(block, 'name', '')
                        self.emit('phase', tool=name, label=TOOL_LABELS.get(name, name), state='writing')
            return stream.get_final_message()

    def _run_tool(self, block: Any) -> dict:
        name = getattr(block, 'name', '')
        args = getattr(block, 'input', None)
        label = TOOL_LABELS.get(name, name)
        detail = ''
        if isinstance(args, dict):
            detail = str(args.get('path') or args.get('url') or args.get('query') or args.get('name') or args.get('engine') or '')
        self.emit('phase', tool=name, label=label, state='running', detail=detail[:200])
        text, is_error = self.box.run(name, args if isinstance(args, dict) else {})
        self.emit('log', text=f'{label}{f" ({detail[:120]})" if detail else ""}: {_short_result(name, text, is_error)}', tool=name,
                  error=is_error)
        res = {'type': 'tool_result', 'tool_use_id': block.id, 'content': text}
        if is_error:
            res['is_error'] = True
        return res

    # ----- result

    def _bundle_tag_list(self, draft: pathlib.Path) -> dict:
        """The site's tag list into the ready draft (lynxx/extgen/taglist.py), through the session's own transport (no cookies)"""
        from ..extgen.taglist import bundle_tag_list
        self.emit('phase', tool='tags', label='Tải danh sách tag của site', state='running')
        fetcher = self.box.fetcher
        try:
            res = bundle_tag_list(draft, session=fetcher._session, allow_local=fetcher.allow_local, min_interval=fetcher.min_interval)
        except Exception as err:  # never fails a ready draft: the app reads the list after the install
            res = {'status': 'failed', 'file': '', 'count': 0, 'requests': 0, 'message': str(err)}
        self.emit('log', text=f'Danh sách tag: {_tag_list_note(res)}', tool='tags', error=False)
        return res

    def _finalize(self, status: str, summary: str, rule_ids: list[str], error: str) -> dict:
        box = self.box
        draft = box.draft if box else None
        checks: list[dict] = []
        manifest: dict = {}
        if draft is not None and (draft / 'manifest.json').is_file():
            try:
                manifest = json.loads((draft / 'manifest.json').read_text(encoding='utf-8'))
            except ValueError:
                manifest = {}
        if status == 'ready':
            # the model's word is not enough: offline replay of the recorded fixtures, independent of the session
            if draft is None or not (draft / 'downloader.py').is_file():
                status, summary = 'failed', f'{summary}\n\nKhông có downloader.py trong bản nháp.'.strip()
            else:
                self.emit('phase', tool='check', label='Kiểm tra độc lập bản nháp (chạy lại offline)', state='running')
                res = check_draft(draft)
                checks = res.get('checks', [])
                last_ok = bool(box.last_test and box.last_test.get('ok'))
                if not res.get('ok') or not last_ok:
                    failed = ', '.join(res.get('failed') or []) or 'test_run cuối chưa đạt'
                    status, summary = 'failed', f'{summary}\n\nKiểm tra độc lập không đạt: {failed}.'.strip()
        elif box and box.last_test:
            checks = box.last_test.get('checks', [])
        tag_list: dict = {}
        if status == 'ready' and draft is not None and self.req.tag_list:
            tag_list = self._bundle_tag_list(draft)
        files = sorted(str(p.relative_to(draft)) for p in draft.rglob('*') if p.is_file()) if draft is not None and draft.is_dir() else []
        report = {
            'session': self.id, 'url': self.req.url, 'site': box.site_url if box else '', 'mode': self.req.mode,
            'model': self.req.model if self.req.mode == 'ai' else '', 'status': status, 'summary': summary,
            'ruleIds': rule_ids, 'error': error,
            'extId': manifest.get('id', ''), 'abbr': manifest.get('abbr', ''), 'name': manifest.get('name', ''),
            'version': manifest.get('version', ''), 'engine': (manifest.get('$generator') or {}).get('engine', ''),
            'draftDir': str(draft) if draft is not None else '', 'files': files, 'checks': checks,
            'testRuns': box.test_runs if box else 0, 'requests': box.fetcher.used if box else 0,
            'usage': self.usage.to_dict(), 'usd': self._usd(), 'maxUsd': self.req.max_usd, 'turns': self.turns,
            'startedAt': self.started, 'finishedAt': _now(), 'log': self.log[-200:], 'repairOf': self.req.repair, 'tagList': tag_list,
        }
        try:
            (self.dir / 'report.json').write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str) + '\n', encoding='utf-8')
        except OSError:
            pass
        return report


def _tag_list_note(res: dict) -> str:
    status = res.get('status')
    if status == 'bundled':
        return f'đã kèm {res.get("count", 0):,} tag vào bản nháp ({res.get("file")})'.replace(',', '.')
    if status == 'learned':
        return 'site không có danh sách tag dùng được (hoặc robots.txt cấm): app học dần từ post bạn xem và tải'
    return 'app sẽ tải danh sách ngay sau khi cài'


def _short_result(name: str, text: str, is_error: bool) -> str:
    if is_error:
        return f'lỗi: {text[:300]}'
    try:
        data = json.loads(text)
    except ValueError:
        return text.split('\n', 1)[0][:200]
    if name == 'test_run':
        return 'đạt' if data.get('ok') else f'chưa đạt ({", ".join(data.get("failed") or [])})'
    if name == 'static_check':
        return 'đạt' if data.get('ok') else f'{len(data.get("violations", []))} vi phạm'
    if name == 'survey_site':
        eng = data.get('engine', {})
        return f'engine {eng.get("engine")} ({eng.get("confidence")}), robots.txt: {data.get("robots", {}).get("status", "?")}'
    if name == 'create_draft':
        return f'{data.get("draft")}: {", ".join(data.get("files", []))}'
    return 'xong'


def _api_error_text(err: Exception) -> str:
    name = type(err).__name__
    status = getattr(err, 'status_code', None)
    if name == 'AuthenticationError' or status == 401:
        return 'Khoá Claude API không hợp lệ hoặc đã bị thu hồi.'
    if name == 'PermissionDeniedError' or status == 403:
        return 'Khoá Claude API không có quyền dùng model này.'
    if name == 'RateLimitError' or status == 429:
        return 'Claude API đang giới hạn tốc độ (429): thử lại sau.'
    if status in (500, 529) or name in ('InternalServerError', 'OverloadedError'):
        return 'Claude API đang quá tải: thử lại sau.'
    if name in ('APIConnectionError', 'APITimeoutError'):
        return 'Không kết nối được tới Claude API.'
    return f'Lỗi: {name}: {str(err)[:300]}'


def list_sessions(drafts_root: pathlib.Path) -> list[dict]:
    """Reports of past sessions, newest first (for the app's drafts list)"""
    import re
    out = []
    root = pathlib.Path(drafts_root)
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir(), reverse=True):
        if not (d.is_dir() and re.fullmatch(SESSION_RE, d.name)):
            continue
        try:
            rep = json.loads((d / 'report.json').read_text(encoding='utf-8'))
        except (OSError, ValueError):
            rep = {'session': d.name, 'status': 'unknown'}
        rep.pop('log', None)
        out.append(rep)
    return out

#
#
#########################################
