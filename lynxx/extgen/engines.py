# coding=UTF-8
"""
Lynxx extension generator: site engine detection (plan A.5). Every probe is one request through SafeFetcher, cheapest first;
the first certain match wins. Evidence (url, status, excerpt) goes into the report / specs.md.
"""
#########################################
#
#

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from .fetch import FetchPolicyError, FetchResult, SafeFetcher

__all__ = ('ENGINES', 'EngineGuess', 'detect_engine')

ENGINES: dict[str, dict] = {
    'gelbooru_dapi': {'template': True, 'description': 'Gelbooru 0.2 DAPI (XML): index.php?page=dapi&s=post&q=index'},
    'danbooru_json': {'template': True, 'description': 'Danbooru JSON API: posts.json + counts/posts.json'},
    'e621_json': {'template': True, 'description': 'e621/e926 JSON API: posts.json -> {"posts": [...]}', 'reference': 'e621'},
    'moebooru_json': {'template': True, 'description': 'Moebooru JSON API: post.json', 'reference': 'danbooru'},
    'philomena_json': {'template': True, 'description': 'Philomena API: api/v1/json/search/images', 'reference': 'e621'},
    'szurubooru_json': {'template': True, 'description': 'Szurubooru API: api/posts/', 'reference': 'e621'},
    'shimmie_api': {'template': True, 'description': 'Shimmie2 with the danbooru compatible API: api/danbooru/find_posts (XML)',
                    'reference': 'paheal'},
    'shimmie': {'template': False, 'description': 'Shimmie2 without its danbooru API: HTML listing /post/list/<tags>/<page>',
                'reference': 'template_html'},
    'custom': {'template': False, 'description': 'unknown engine: custom HTML / JSON parsing', 'reference': 'template_html'},
}


@dataclass
class EngineGuess:
    engine: str
    confidence: str  # high | medium | low
    evidence: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    auth_required: bool = False

    def to_dict(self) -> dict:
        return {**asdict(self), 'template': ENGINES[self.engine]['template'], 'description': ENGINES[self.engine]['description']}


def _evidence(res: FetchResult) -> dict:
    return {'url': res.url, 'status': res.status, 'type': res.content_type, 'excerpt': res.text(300)}


def _json(res: FetchResult):
    if res.status != 200:
        return None
    try:
        return json.loads(res.body)
    except ValueError:
        return None


def detect_engine(fetcher: SafeFetcher, site: str) -> EngineGuess:
    """site: 'https://host/' (normalize_site). Raises FetchPolicyError only for hard rules (challenge, robots of the root...)"""
    evidence: list[dict] = []
    notes: list[str] = []

    def probe(path: str, accept: str = ''):
        try:
            res = fetcher.fetch(f'{site}{path}', accept=accept)
        except FetchPolicyError as err:
            if err.rule in ('R2',):  # this probe is not allowed by robots.txt: skip it, keep probing
                notes.append(f'skipped {path}: {err.message}')
                return None
            if err.rule == 'R10' and err.redirect and path:
                # an api probe the site sends to plain http or another host (never contacted): no such api here, keep probing.
                # The home page ('') redirected away still stops the survey: the site itself is not usable over https
                notes.append(f'redirected {path}: {err.message}')
                return None
            raise
        evidence.append(_evidence(res))
        return res

    # 1. gelbooru 0.2 DAPI
    if res := probe('index.php?page=dapi&s=post&q=index&limit=1'):
        t = res.text(4000)
        if re.search(r'<posts\s[^>]*count=["\']\d+', t):
            return EngineGuess('gelbooru_dapi', 'high', evidence, notes)
        if 'Missing authentication' in t:
            notes.append('the DAPI needs an API key of the user (manifest auth.apiKey)')
            return EngineGuess('gelbooru_dapi', 'high', evidence, notes, auth_required=True)
    # 2. danbooru / e621 (both posts.json)
    if res := probe('posts.json?limit=1', 'application/json'):
        data = _json(res)
        if isinstance(data, list) and data and isinstance(data[0], dict) and 'tag_string' in data[0]:
            return EngineGuess('danbooru_json', 'high', evidence, notes)
        if isinstance(data, dict) and isinstance(data.get('posts'), list):
            posts = data['posts']
            if not posts or isinstance(posts[0].get('file'), dict):
                return EngineGuess('e621_json', 'high' if posts else 'medium', evidence, notes)
    # 3. moebooru
    if res := probe('post.json?limit=1', 'application/json'):
        data = _json(res)
        if isinstance(data, list) and data and isinstance(data[0], dict) and ('jpeg_url' in data[0] or 'file_url' in data[0]):
            return EngineGuess('moebooru_json', 'high', evidence, notes)
    # 4. philomena
    if res := probe('api/v1/json/search/images?q=*&per_page=1', 'application/json'):
        data = _json(res)
        if isinstance(data, dict) and 'images' in data and 'total' in data:
            return EngineGuess('philomena_json', 'high', evidence, notes)
    # 5. szurubooru
    if res := probe('api/posts/?limit=1', 'application/json'):
        data = _json(res)
        if isinstance(data, dict) and 'results' in data and 'total' in data:
            return EngineGuess('szurubooru_json', 'high', evidence, notes)
    # 6. shimmie with its danbooru compatible api
    if res := probe('api/danbooru/find_posts?limit=1'):
        if re.search(r'<posts\s[^>]*count=["\']\d+', res.text(4000)):  # shimmie quotes attributes with '
            return EngineGuess('shimmie_api', 'high', evidence, notes)
    # 7. home page: shimmie / anything else
    if res := probe(''):
        t = res.text(200000)
        if re.search(r'shimmie', t, re.IGNORECASE):
            return EngineGuess('shimmie', 'medium', evidence, notes)
    return EngineGuess('custom', 'low', evidence, [*notes, 'no known API answered: the downloader must parse the site itself'])

#
#
#########################################
