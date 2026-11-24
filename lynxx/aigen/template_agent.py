# coding=UTF-8
"""
Lynxx extension generator, 'template' mode: stands in for the Claude client with a fixed plan, no model and no API key.
Works for sites whose engine has a tested template (gelbooru DAPI, danbooru JSON): survey -> draft -> static check ->
pick a common tag -> test run -> finish. Other engines end with 'failed' and a hint to use the AI mode.
Runs through the same session loop and tools as the AI mode, so every rule and check applies the same way.
"""
#########################################
#
#

from __future__ import annotations

import json
import re
from collections import Counter
from types import SimpleNamespace
from typing import Any

from .tools import UNTRUSTED, refusal_text
from ..extgen.templates import TEMPLATE_ENGINES

__all__ = ('TemplateClient',)

SAMPLE_URLS = {  # recent posts, to pick a tag that surely has results
    'gelbooru_dapi': ('index.php?page=dapi&s=post&q=index&limit=40', ''),
    'danbooru_json': ('posts.json?limit=40', 'application/json'),
    'e621_json': ('posts.json?limit=40', 'application/json'),
    'moebooru_json': ('post.json?limit=40', 'application/json'),
    'philomena_json': ('api/v1/json/search/images?q=*&per_page=40', 'application/json'),
    'szurubooru_json': ('api/posts/?limit=40', 'application/json'),
    'shimmie_api': ('api/danbooru/find_posts?limit=40', ''),
}
ENGINE_NAMES = 'Gelbooru 0.2 (DAPI), Danbooru, e621, Moebooru, Philomena, Szurubooru và Shimmie (API danbooru)'
RE_TAG_OK = re.compile(r'[a-z\d][a-z\d_()\-]{1,40}')


TAG_FIELDS = {  # regexes, not json.loads: the sample reaches the agent truncated (http_fetch returns 20000 characters)
    'danbooru_json': (r'"tag_string_general"\s*:\s*"((?:[^"\\\\]|\\\\.)*)"|"tag_string"\s*:\s*"((?:[^"\\\\]|\\\\.)*)"', 'words'),
    'moebooru_json': (r'"tags"\s*:\s*"((?:[^"\\\\]|\\\\.)*)"', 'words'),
    'e621_json': (r'"general"\s*:\s*\[([^\]]*)\]', 'list'),
    'philomena_json': (r'"tags"\s*:\s*\[([^\]]*)\]', 'list'),
    'szurubooru_json': (r'"names"\s*:\s*\[([^\]]*)\]', 'first'),
}


def _unescape(s: str) -> str:
    try:
        return json.loads(f'"{s}"')
    except ValueError:
        return s


def pick_tag(body: str, engine: str) -> str:
    """The most common general tag of a sample of recent posts (a tag that surely has results)"""
    counts: Counter = Counter()
    if engine in ('gelbooru_dapi', 'shimmie_api'):  # xml attributes
        for _, tags in re.findall(r'\btags=(["\'])(.*?)\1', body):  # shimmie quotes attributes with '
            counts.update(t for t in tags.split() if t)
        return _most_common(counts)
    pattern, kind = TAG_FIELDS.get(engine, TAG_FIELDS['danbooru_json'])
    for m in re.finditer(pattern, body):
        value = next((g for g in m.groups() if g is not None), '')
        if kind == 'words':
            counts.update(_unescape(value).split())
        else:
            items = [_unescape(x).replace(' ', '_') for x in re.findall(r'"((?:[^"\\\\]|\\\\.)*)"', value)]
            counts.update(items[:1] if kind == 'first' else items)
    return _most_common(counts)


def _most_common(counts: Counter) -> str:
    for tag, n in counts.most_common(50):
        if n >= 2 and RE_TAG_OK.fullmatch(tag) and ':' not in tag:
            return tag
    return ''


class _Stream:
    def __init__(self, message: Any) -> None:
        self._message = message

    def __enter__(self) -> _Stream:
        return self

    def __exit__(self, *_: Any) -> None:
        return None

    def __iter__(self):
        return iter(())

    def get_final_message(self) -> Any:
        return self._message


class TemplateClient:
    def __init__(self, *, query: str = '', media: list[str] | None = None) -> None:
        self.query = query.strip()
        self.media = media or ['image', 'video']
        self.engine = ''
        self.site = ''
        self.pending = ''
        self.existing: list[dict] = []
        self.n = 0
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    # ----- fake API

    def _stream(self, **params: Any) -> _Stream:
        return _Stream(self._next(params['messages']))

    def _call(self, name: str, args: dict, text: str = '') -> Any:
        self.n += 1
        self.pending = name
        content = []
        if text:
            content.append(SimpleNamespace(type='text', text=text))
        content.append(SimpleNamespace(type='tool_use', id=f'toolu_template_{self.n:02d}', name=name, input=args))
        return SimpleNamespace(content=content, stop_reason='tool_use', usage=None)

    def _finish(self, status: str, summary: str, rule_ids: list[str] | None = None) -> Any:
        return self._call('finish', {'status': status, 'summary': summary, 'rule_ids': rule_ids or []})

    def _existing_note(self) -> str:
        if not self.existing:
            return ''
        names = ', '.join(f'{e.get("name") or e.get("id")} ({"đã cài" if e.get("installed") else "chưa cài"})' for e in self.existing)
        return f' Lưu ý: Lynxx đã có extension cho site này: {names}; cân nhắc dùng extension đó thay vì cài thêm.'

    # ----- plan

    def _next(self, messages: list[dict]) -> Any:
        last = messages[-1]['content']
        if isinstance(last, str):
            if self.site == '':
                m = re.search(r'Website: (\S+)', last)
                self.site = m.group(1) if m else ''
                return self._call('survey_site', {}, f'Chế độ không dùng AI: dùng template có sẵn nếu site chạy {ENGINE_NAMES}.')
            return self._finish('failed', 'Kế hoạch template bị gián đoạn.')
        res = last[0] if last else {}
        text, err = str(res.get('content', '')), bool(res.get('is_error'))
        step = self.pending
        if step == 'survey_site':
            if err:
                m = re.match(r'refused by rule (R\d+): (.*)', text, re.S)
                return self._finish('refused', refusal_text(m.group(1), m.group(2)), [m.group(1)]) if m else self._finish('failed', f'Không khảo sát được site: {text}')
            data = json.loads(text)
            self.existing = [e for e in data.get('existing') or [] if isinstance(e, dict)]
            eng = data.get('engine', {})
            self.engine = eng.get('engine', '')
            if self.engine not in TEMPLATE_ENGINES:
                return self._finish('failed', f'Site dùng engine "{self.engine}" (độ tin cậy {eng.get("confidence")}): chế độ không AI '
                                              f'chỉ hỗ trợ {ENGINE_NAMES}. Hãy dùng chế độ AI cho site này.')
            names = data.get('names', {})
            if not names.get('abbr'):
                return self._finish('failed', 'Không tìm được tên viết tắt (abbr) còn trống cho site này.')
            return self._call('create_draft', {'engine': self.engine, 'id': names.get('id', ''), 'abbr': names['abbr'],
                                               'name': names.get('name', ''), 'media': self.media, 'description': ''})
        if step == 'create_draft':
            return self._finish('failed', f'Không tạo được bản nháp: {text}') if err else self._call('static_check', {})
        if step == 'static_check':
            if err or not json.loads(text).get('ok'):
                return self._finish('failed', f'Template không qua kiểm tra tĩnh: {text[:500]}')
            if self.query:
                return self._call('test_run', {'query': self.query, 'limit': 3})
            path, accept = SAMPLE_URLS[self.engine]
            return self._call('http_fetch', {'url': self.site + path, 'accept': accept})
        if step == 'http_fetch':
            body = text.split(UNTRUSTED, 1)[-1] if not err else ''
            self.query = pick_tag(body, self.engine)
            if not self.query:
                return self._finish('failed', 'Không tìm được tag phổ biến để chạy thử: hãy nhập tag thử (ô "Tag chạy thử") rồi tạo lại.')
            return self._call('test_run', {'query': self.query, 'limit': 3})
        if step == 'test_run':
            data = {} if err else json.loads(text)
            if data.get('ok'):
                return self._finish('ready', f'Site chạy engine {self.engine}: bản nháp dùng template có sẵn của Lynxx. Chạy thử với '
                                             f'tag "{self.query}": {data.get("count")} bài, lấy được {len(data.get("ids") or [])} link tệp. '
                                             f'Hãy xem lại manifest.json trước khi cài.{self._existing_note()}')
            failed = ', '.join(data.get('failed') or []) or text[:300]
            return self._finish('failed', f'Chạy thử với tag "{self.query}" chưa đạt: {failed}.')
        return self._finish('failed', 'Kế hoạch template bị gián đoạn.')

#
#
#########################################
