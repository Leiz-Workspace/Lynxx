# coding=UTF-8
"""
Lynxx: tag lists updater (autocompletion / A-Z browser data)
Writes '<abbr>_tags.json' files in the same format as bundled lists: {"tag": "N posts (type)", ...}, one tag per line, sorted.
Sources (TagSource): the five reviewed modules (CURATED), every extension with its own tag update ('tagUpdate' in its manifest: a
function of the extension run only when the user asks, see TagJob / EXTENSION_JOB) and every extension made from a known engine,
read from the '$generator.engine' and 'site' of its manifest (lynxx.extgen writes them), see ENGINE_TAG_SOURCES / tag_source.
An extension's own update that can read only the most used tags declares "merge": {"minPosts": N} (TagSource.merge, Paheal from
1.0.3): its read goes into the list in use (fresh counts for the tags read, new tags added, every other tag kept) instead of
replacing it, see update_tag_list.
A row an extension's own update gives no type takes the type the list the extension ships has for that tag (Lynxx 2.0.12, see
TagJob / _take_shipped_types): the types its maintainer set in the shipped list survive the update.
Politeness, every source (docs/operations.md §3): the delay between requests, robots.txt Crawl-delay, Retry-After, a request
budget per list, no cookies, a stop on bot challenges, the user's own API key only (Lynxx ships no shared key). robots.txt
(RFC 9309, lynxx/robots.py) is applied strictly to extension sources; the five reviewed modules use the sites' documented
APIs and are exceptions to its Disallow lines (decision of 2026-11-18: Danbooru's robots.txt disallows '/*.json').
Used by: lynxx-core tags / python -m lynxx.tags_updater (the app's system schedule, cron), tools/update_tags.py,
the desktop app (Tags A-Z -> Danh sách tag, sidecar tags.update / tags.sources)
"""
#########################################
#
#

from __future__ import annotations

import contextlib
import csv
import datetime
import gzip
import hashlib
import html
import importlib.util
import io
import ipaddress
import json
import math
import os
import pathlib
import re
import socket
import sys
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from http.cookiejar import DefaultCookiePolicy
from typing import NamedTuple
from urllib import parse as url_parse

import requests

from .defines import RATE_LIMIT_RX, UTF8
from .denylist import denied, denied_message
from .network import (
    LYNXX_USER_AGENT,
    RATE_LIMIT_MARGIN,
    is_cloudflare_challenge,
    key_rate_interval,
    parse_retry_after,
    shared_rate_wait,
)
from .robots import ROBOTS_MAX_BYTES, RobotsRules
from .tags import TAG_TYPES_BY_NAME, TagTypes

__all__ = ('CURATED', 'ENGINE_TAG_SOURCES', 'EXTENSION_JOB', 'TagJob', 'TagSource', 'TagSourceError', 'UpdateResult', 'tag_source',
           'tag_sources', 'update_tag_list', 'write_tag_list')

USER_AGENT = f'{LYNXX_USER_AGENT} (tag list updater)'
ROBOTS_AGENT = 'Lynxx'
'''product token looked up in robots.txt (the extension generator uses the same)'''
PAGE_LIMIT = 1000
SANITY_MIN_RATIO = 0.5
'''refuse to replace an existing list with one that has less than this ratio of tags (partial/failed fetch); a merge (TagSource.merge)
is refused when its read lists less than this ratio of the tags the list in use has at its threshold (a cut read, another page,
names in another form)'''
REQUEST_BUDGET = 5000
'''requests per list (robots.txt and retries count): 5M tags in 1000 tag pages. A list that needs more is not written'''
ENGINE_MIN_INTERVAL = 1.0
'''extension sources: at least this many seconds between requests (the generator's downloaders use the same floor)'''
CRAWL_DELAY_MAX = 60.0
TRIES = 5
'''network errors and gateway errors (GATEWAY_ERRORS): tries of one request'''
GATEWAY_ERRORS = (502, 504)
'''answers of a proxy / gateway in front of the site that failed this once (an extension site answered one 502 in the middle of its
tag list on 2026-11-21 and the same page on the next try, docs/dev_log.md 7bf): tried again like a network error, not a reason to
stop the list'''
GATEWAY_WAIT = 5.0
'''seconds before the next try after a gateway error, times the gateway errors of that request so far (5, 10, 15, 20 s)'''
RATE_LIMIT_WAITS = (5.0, 15.0, 30.0, 60.0, 120.0, 180.0)
'''seconds waited after each HTTP 429 / 503 of one request without Retry-After (6 min 50 s in all), then the list stops'''
PARTIAL_DIR = '.partial'
PARTIAL_MAX_AGE = 3 * 86400
'''a list stopped half way (429, network, closed app) continues from where it stopped when updated again within this time'''
PARTIAL_SAVE_PAGES = 20
TAIL_QUIET_PAGES = 3
TAIL_QUIET_SHARE = 0.01
TAIL_MIN_PAGES = 5
TAIL_MAX_PAGES = 800
'''an update of a source whose listing keeps its recently changed tags last (TagSource.tail_updates) reads back from the end
until TAIL_QUIET_PAGES pages in a row have at most TAIL_QUIET_SHARE of their tags changed (TAIL_MIN_PAGES at least,
TAIL_MAX_PAGES at most, about half of rule34.xxx), see _iter_gelbooru_tail'''
FULL_EVERY = 30 * 86400
'''a full read at least this often: the tail does not see tags whose rows the database moved elsewhere'''
META_DIR = '.meta'
'''<out>/.meta/<abbr>_tags.json: how the list was read (pages, last full read), for the next update'''
TIMEOUT = (15, 120)
MAX_REDIRECTS = 5
MAX_PAGE_BYTES = 32 * 1024 * 1024
MAX_EXPORT_BYTES = 512 * 1024 * 1024
'''one answer holding a whole list: e621 database export, shimmie find_tags'''
PHILOMENA_PER_PAGE = 50
PHILOMENA_MAX_RESULTS = 10000
'''philomena's search answers this many results at most (elasticsearch window): its list is the most used tags'''
SZURUBOORU_PER_PAGE = 100

GELBOORU_TYPES = {0: 'general', 1: 'artist', 3: 'copyright', 4: 'character', 5: 'metadata', 6: 'general'}
DANBOORU_TYPES = {0: 'general', 1: 'artist', 3: 'copyright', 4: 'character', 5: 'metadata'}
E621_TYPES = {0: 'general', 1: 'artist', 3: 'copyright', 4: 'character', 5: 'species', 6: 'invalid1', 7: 'metadata', 8: 'lore'}
MOEBOORU_TYPES = {0: 'general', 1: 'artist', 3: 'copyright', 4: 'character', 5: 'artist', 6: 'metadata'}
'''moebooru: 5 circle (artist group), 6 faults'''
PHILOMENA_TYPES = {'character': 'character', 'oc': 'character', 'species': 'species', 'origin': 'artist', 'content-official': 'copyright',
                   'rating': 'metadata', 'spoiler': 'metadata', 'error': 'metadata'}

re_xml_tag = re.compile(r'<tag\s([^>]*?)/?>')
re_attr = re.compile(r'(\w+)=(["\'])(.*?)\2', re.DOTALL)
re_site = re.compile(r'^https://[a-z\d.-]+(?::\d+)?/$')
re_rate_limit = re.compile(r'limit of\s+(\d+)\s+requests?\s+(?:every|per)\s+(\d+)\s+second', re.IGNORECASE)
'''the 429 text of rule34.xxx: "You currently have a limit of 60 requests every 60 second(s)."'''
re_secret_param = re.compile(r'([?&](?:api_key|login|user_id|password|pass_hash|token|key)=)[^&#]*', re.IGNORECASE)
re_export_row = re.compile(r'<(?:tr\b|div\b[^>]*\bclass=["\'][^"\']*\brow\b)', re.IGNORECASE)
'''a row of e621's db_exports page: its export link and that file's checksum'''
re_html_tag = re.compile(r'<(a|input)\b([^>]*)>', re.IGNORECASE)
re_e621_tags_export = re.compile(r'tags(?:-\d{4}-\d{2}-\d{2})?\.csv\.gz')
re_sha256 = re.compile(r'[\da-fA-F]{64}')

ProgressCb = Callable[[str, int], None]
'''(message, tags fetched so far)'''
PROGRESS_INTERVAL = 2.0
'''seconds between two progress lines while tags come in (the app shows the count read so far)'''

E621_EXPORTS_PAGE = 'db_exports'
'''e621's page listing its daily database exports (db_export/ redirects there since 2026-11)'''
ENGINE_TAG_SOURCES: dict[str, str] = {
    'gelbooru_dapi': 'DAPI tag index, pages by number (pid)',
    'danbooru_json': 'tags.json, id cursor',
    'moebooru_json': 'tag.json, pages by name',
    'e621_json': f'daily database export (tags.csv.gz listed on {E621_EXPORTS_PAGE}, its SHA-256 checked)',
    'philomena_json': f'api/v1/json/search/tags, the {PHILOMENA_MAX_RESULTS:d} most used tags',
    'szurubooru_json': 'api/tags/, offset pages',
    'shimmie_api': 'api/danbooru/find_tags, the whole list in one answer',
}
'''engines (lynxx.extgen.engines) with a tag listing -> how the list is read'''
EXTENSION_JOB = 'extension_job'
'''engine of a source that is the extension's own tag update (manifest 'tagUpdate'): run only when the user asks for it'''
JOB_TAG_MAX_LEN = 200
JOB_TAG_TYPES = frozenset(str(k) for k in TAG_TYPES_BY_NAME)
'''tag types an extension's update may give (others are written as 'general')'''
GENERIC_TYPES = frozenset(('general', 'unknown'))
'''types that say nothing about a tag: what a job that cannot tell types gives'''
SPECIFIC_TYPES = frozenset(str(k) for k, v in TAG_TYPES_BY_NAME.items() if v not in (TagTypes.TAG_TYPE_GENERAL, TagTypes.TAG_TYPE_UNKNOWN))
'''types that say what a tag is (artist, character, copyright, metadata...): the ones a row without a type takes from the list the
extension ships (_take_shipped_types). 'tag', another name of general, is not one'''
NO_TYPE = ''
'''type of a row an extension's update gave no type ((name, posts), or None / '' as its type), see _job_tag: written as 'general'
unless a list says more (the list in use for a merge, the list the extension ships: _take_shipped_types)'''


class TagSourceError(RuntimeError):
    """A request Lynxx refuses to make or to repeat. kind: 'robots' (robots.txt disallows it), 'budget' (requests or time used up),
    'challenge' (bot challenge), 'policy' (address, host, size...)"""

    def __init__(self, message: str, kind: str = 'policy') -> None:
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class TagSource:
    module: str
    engine: str
    site: str
    '''https://host/ the tag endpoints are under'''
    name: str = ''
    key: str = 'none'
    '''none | optional (used when the user has one) | required (the site refuses anonymous requests)'''
    key_format: str = ''
    robots: bool = True
    '''False: a reviewed exception to robots.txt Disallow lines (its Crawl-delay is still honored)'''
    curated: bool = False
    rate_limit: tuple[int, int] | None = None
    '''(requests, seconds) the site publishes for one API key; the user's key may have more (lynxx.network.key_rate_limit)'''
    tail_updates: bool = False
    '''the listing keeps its recently changed and new tags on the last pages (rule34.xxx: the tag index is in storage order, a row
    whose post count changes moves to the end): an update may read only the tail (TAIL_PAGES) and merge it, see update_tag_list'''
    job: str = ''
    '''the extension's own tag update, 'tags.py:read_tags' (manifest 'tagUpdate.entry'), engine EXTENSION_JOB'''
    folder: str = ''
    '''folder of the extension the job file is in'''
    tag_list: str = ''
    '''the list the extension ships next to its manifest (manifest 'tagList', '<abbr>_tags.json'), '' when it names none: a merge
    goes into it when no list is in use (the command line where the extension is not installed), and the rows its own update
    gives no type take its types (_take_shipped_types)'''
    merge_min_posts: int = 0
    '''the extension's own update reads only the tags with at least this many posts, every one of them (manifest
    tagUpdate.merge.minPosts): update_tag_list merges the read into the list in use'''

    @property
    def on_demand(self) -> bool:
        """Run only when the user asks (Tags A-Z button, the command line naming the module): never by the app's system schedule
        ('auto'), its update when it opens, or right after the install. The extension ships its list (manifest 'tagList')"""
        return self.engine == EXTENSION_JOB

    @property
    def merge(self) -> bool:
        """The update reads only the tags with merge_min_posts or more posts: merged into the list in use, never replacing it"""
        return self.engine == EXTENSION_JOB and self.merge_min_posts > 0

    @property
    def description(self) -> str:
        if self.engine == EXTENSION_JOB:
            return (f'the extension\'s own update ({self.job}), run only when asked'
                    + (f': the tags with {self.merge_min_posts:d}+ posts, merged into the list in use' if self.merge else ''))
        return ENGINE_TAG_SOURCES.get(self.engine, '')

    def as_dict(self) -> dict:
        return {'module': self.module, 'name': self.name or self.module, 'engine': self.engine, 'site': self.site, 'key': self.key,
                'keyFormat': self.key_format, 'robots': self.robots, 'curated': self.curated, 'description': self.description,
                'tailUpdates': self.tail_updates, 'onDemand': self.on_demand, 'job': self.job, 'merge': self.merge,
                'mergeMinPosts': self.merge_min_posts}


CURATED: dict[str, TagSource] = {
    # api.rule34.xxx: 'a limit of 60 requests every 60 second(s)' per key, shared with the downloads of that key
    'rx': TagSource('rx', 'gelbooru_dapi', 'https://api.rule34.xxx/', 'Rule34', 'required', 'KEY,USER_ID', robots=False, curated=True,
                    rate_limit=RATE_LIMIT_RX, tail_updates=True),
    'db': TagSource('db', 'danbooru_json', 'https://danbooru.donmai.us/', 'Danbooru', 'optional', 'API_KEY,LOGIN', robots=False,
                    curated=True),
    'xb': TagSource('xb', 'gelbooru_dapi', 'https://xbooru.com/', 'Xbooru', robots=False, curated=True),
    'bb': TagSource('bb', 'gelbooru_dapi', 'https://bbooru.com/', 'Bbooru', robots=False, curated=True),
    'en': TagSource('en', 'e621_json', 'https://e621.net/', 'e621', robots=False, curated=True),
}
'''reviewed modules: the sites' documented APIs, exceptions to robots.txt Disallow lines (decision of 2026-11-18)'''


def _host(url: str) -> str:
    return (url_parse.urlsplit(url).hostname or '').lower().rstrip('.')


def _base_domain(host: str) -> str:
    return '.'.join(host.split('.')[-2:]) if host.count('.') >= 1 and not host.replace('.', '').isdigit() else host


def redact(text: str) -> str:
    return re_secret_param.sub(r'\1<REDACTED>', text)


# ----- sources -----

def _manifests(installed_only: bool) -> list:
    """Manifests only, no extension code is run: builtin + installed (user) extensions, then the local catalog if asked"""
    from .extensions import SOURCE_BUILTIN, SOURCE_CATALOG, SOURCE_USER, Extensions
    found = [*Extensions._scan(Extensions.builtin_dir(), SOURCE_BUILTIN), *Extensions._scan(Extensions.user_dir(), SOURCE_USER)]
    if not installed_only:
        found += Extensions._scan(Extensions.catalog_dir(), SOURCE_CATALOG)
    return found


def source_for_manifest(m, jobs: bool = True) -> TagSource | None:
    """Tag source of an extension: a reviewed one for the same site, else its own update ('tagUpdate', when jobs), else from its
    engine ('$generator.engine') and 'site'. jobs=False: the extension generator, whose drafts' code runs only in its sandbox"""
    site = str(m.site or '')
    cur = CURATED.get(m.abbr)
    if cur and _base_domain(_host(cur.site)) == _base_domain(_host(site)):
        return cur
    tu = getattr(m, 'tagUpdate', None) or {}
    job = str(tu.get('entry', ''))
    if job and jobs and m.path is not None and re_site.fullmatch(site) and not denied(site):
        auth = m.auth if isinstance(m.auth, dict) else {}
        key = 'required' if auth.get('apiKey') is True else 'none'
        merge = tu.get('merge')
        tag_list = str(m.path / m.tagList) if m.tagList == f'{m.abbr}_tags.json' else ''  # the name TagsDB.extension_lists reads
        return TagSource(m.abbr, EXTENSION_JOB, site, m.name, key, str(auth.get('apiKeyFormat') or '') if key != 'none' else '',
                         rate_limit=manifest_rate_limit(auth), job=job, folder=str(m.path), tag_list=tag_list,
                         merge_min_posts=int(merge.get('minPosts') or 0) if isinstance(merge, dict) else 0)
    engine = str((m.generator or {}).get('engine', ''))
    if engine not in ENGINE_TAG_SOURCES or not re_site.fullmatch(site) or denied(site):
        return None
    auth = m.auth if isinstance(m.auth, dict) else {}
    if engine == 'gelbooru_dapi':
        key, fmt = ('required' if auth.get('apiKey') else 'optional'), 'API_KEY,USER_ID'
    elif engine == 'danbooru_json':
        key, fmt = 'optional', 'API_KEY,LOGIN'
    else:
        key, fmt = 'none', ''
    return TagSource(m.abbr, engine, site, m.name, key, str(auth.get('apiKeyFormat') or fmt) if key != 'none' else '',
                     rate_limit=manifest_rate_limit(auth))


def manifest_rate_limit(auth: object) -> tuple[int, int] | None:
    """auth.rateLimit of a manifest, {"requests": 60, "seconds": 60}: the limit the site publishes for one API key"""
    rl = auth.get('rateLimit') if isinstance(auth, dict) else None
    if not isinstance(rl, dict):
        return None
    n, s = rl.get('requests'), rl.get('seconds')
    ok = all(isinstance(v, int) and not isinstance(v, bool) and 0 < v <= 100000 for v in (n, s))
    return (n, s) if ok else None


def tag_source(module: str, manifests: list | None = None) -> TagSource | None:
    """Source of one module: its manifest (builtin, installed or local catalog) or, with no manifest, a reviewed module"""
    manifests = _manifests(installed_only=False) if manifests is None else manifests
    m = next((x for x in manifests if x.abbr == module), None)
    return source_for_manifest(m) if m is not None else CURATED.get(module)


def tag_sources(manifests: list | None = None) -> list[TagSource]:
    """Installed modules (builtin + user extensions) that have a tag source"""
    result: dict[str, TagSource] = {}
    for m in _manifests(installed_only=True) if manifests is None else manifests:
        if m.abbr not in result and (src := source_for_manifest(m)) is not None:
            result[m.abbr] = src
    return list(result.values())


# ----- polite client -----

class _Answer(NamedTuple):
    url: str
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def text(self) -> str:
        return self.body.decode(UTF8, 'replace')

    def json(self):
        try:
            return json.loads(self.body)
        except ValueError:
            return None


def _session() -> requests.Session:
    s = requests.Session()
    s.headers['User-Agent'] = USER_AGENT
    s.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))  # no cookies, ever
    return s


class PoliteClient:
    """Every request of one list update: pacing (delay, Crawl-delay), robots.txt, Retry-After, the request budget.
    allow_local: tests only (fake sites), skips the public address check; never set by the app or the command line"""

    def __init__(self, source: TagSource, *, delay=0.5, budget=REQUEST_BUDGET, allow_local=False, session=None,
                 sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None,
                 min_interval: float | None = None, deadline: float | None = None) -> None:
        """min_interval: floor of the delay (default: none for reviewed modules, ENGINE_MIN_INTERVAL for extension sites).
        deadline: clock() value after which no request is made (TagSourceError 'budget')"""
        self.source = source
        self.site = source.site
        host = _host(source.site)
        self.hosts = {host, _base_domain(host)}
        floor = (0.0 if source.curated else ENGINE_MIN_INTERVAL) if min_interval is None else float(min_interval)
        key_interval = key_rate_interval(source.module, source.rate_limit)
        self.interval = max(0.0, float(delay), floor, key_interval)
        self._shared = source.site if key_interval > 0 else ''
        '''a key limited per key: every process using it (download jobs too) keeps its pace together (shared_rate_wait)'''
        self.waited = 0.0
        '''seconds waited because the site asked to (429 / 503)'''
        self.notify: Callable[[str], None] | None = None
        '''told of each wait and retry (the update's progress): a slow list says why it is slow'''
        self.deadline = deadline
        self.budget = max(1, int(budget))
        self.used = 0
        self.allow_local = allow_local
        self.session = session or _session()
        self.robots: RobotsRules | None = None
        self._public: set[str] = set()
        self._sleep, self._clock = sleep or time.sleep, clock or time.monotonic
        self._last = -math.inf

    def _check_url(self, url: str) -> None:
        p = url_parse.urlsplit(url)
        host = (p.hostname or '').lower().rstrip('.')
        if p.scheme != 'https':
            raise TagSourceError(f'only https is used: {redact(url)}')
        if not host or not any(host == h or host.endswith(f'.{h}') for h in self.hosts):
            raise TagSourceError(f'{host or url} is not the site of this tag list ({self.site})')
        if p.username or p.password:
            raise TagSourceError('credentials in urls are not allowed')
        if d := denied(host):
            raise TagSourceError(denied_message(d))
        if self.allow_local or host in self._public:
            return
        try:
            infos = socket.getaddrinfo(host, p.port or 443, proto=socket.IPPROTO_TCP)
        except OSError as err:
            raise requests.ConnectionError(f'cannot resolve {host}: {err}')
        bad = [i[4][0] for i in infos if not (ipaddress.ip_address(i[4][0]).is_global and not ipaddress.ip_address(i[4][0]).is_multicast)]
        if bad or not infos:
            raise TagSourceError(f'{host} resolves to a non public address ({", ".join(bad) or "none"})')
        self._public.add(host)

    def _spend(self) -> None:
        if self.used >= self.budget:
            raise TagSourceError(f'request budget of this list ({self.budget:d}) used up before the list was complete: not written',
                                 'budget')
        self.used += 1

    def _pace(self) -> None:
        wait = self._last + self.interval - self._clock()
        if self.deadline is not None and self._clock() + max(0.0, wait) > self.deadline:
            raise TagSourceError('time budget of this list used up before the list was complete: not written', 'budget')
        if self._shared and shared_rate_wait(self._shared, self.interval):
            self._last = self._clock()
            return
        if wait > 0:
            self._sleep(wait)
        self._last = self._clock()

    def _request(self, url: str, accept: str, max_bytes: int, truncate: bool = False) -> _Answer:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            self._check_url(current)
            self._spend()
            headers = {'Accept': accept} if accept else {}
            with self.session.request('GET', current, headers=headers, timeout=TIMEOUT, stream=True, allow_redirects=False) as r:
                if r.status_code in (301, 302, 303, 307, 308) and r.headers.get('location'):
                    current = url_parse.urljoin(current, r.headers['location'])
                    continue
                if is_cloudflare_challenge(r):
                    raise TagSourceError(f'{_host(current)} answered with a bot challenge: Lynxx does not bypass it, try again later',
                                         'challenge')
                body = bytearray()
                for chunk in r.iter_content(65536):
                    body += chunk
                    if len(body) > max_bytes:
                        if truncate:
                            del body[max_bytes:]
                            break
                        raise TagSourceError(f'answer of {_host(current)} is larger than {max_bytes // 1048576:d} MB: not read')
                return _Answer(current, r.status_code, {k.lower(): v for k, v in r.headers.items()}, bytes(body))
        raise TagSourceError(f'too many redirects: {redact(url)}')

    def _robots_rules(self) -> RobotsRules:
        if self.robots is None:
            p = url_parse.urlsplit(self.site)
            try:
                self._pace()
                r = self._request(f'{p.scheme}://{p.netloc}/robots.txt', 'text/plain', ROBOTS_MAX_BYTES, truncate=True)
                if 200 <= r.status < 300:
                    rules = RobotsRules.parse(r.text, ROBOTS_AGENT)
                elif 400 <= r.status < 500:
                    rules = RobotsRules.allow_all(f'none ({r.status:d})')
                else:
                    rules = RobotsRules.disallow_all(f'unreachable ({r.status:d})')
            except requests.RequestException as err:
                rules = RobotsRules.disallow_all(f'unreachable ({type(err).__name__})')
            self.robots = rules
            if rules.crawl_delay:
                self.interval = max(self.interval, min(rules.crawl_delay, CRAWL_DELAY_MAX))
        return self.robots

    def get(self, url: str, *, accept: str = '', max_bytes: int = MAX_PAGE_BYTES, ok: tuple[int, ...] = (200,)) -> _Answer:
        """GET after robots.txt and pacing; 429 / 503 wait for Retry-After, network errors and 502 / 504 are retried (TRIES in all)"""
        rules = self._robots_rules()
        if self.source.robots and not rules.allows(url):
            path = url_parse.urlsplit(url).path
            raise TagSourceError(f'robots.txt of {_host(url)} does not allow {path} ({rules.status}): the tag list is not updated',
                                 'robots')
        errors = waits = gateway = 0
        while True:
            self._pace()
            try:
                r = self._request(url, accept, max_bytes)
            except TagSourceError:
                raise
            except requests.RequestException as err:
                errors += 1
                if errors >= TRIES:
                    raise
                self._tell(f'{_host(url)}: {err.__class__.__name__}, trying again in {2.0 * errors:.0f} s')
                self._sleep(2.0 * errors)
                continue
            if r.status in (429, 503):
                self._slow_down(r)
                if waits >= len(RATE_LIMIT_WAITS):
                    raise RuntimeError(f'{_host(url)} still asks to wait (HTTP {r.status:d}) after {waits:d} waits '
                                       f'({self.waited:.0f} s): try again later')
                wait = parse_retry_after(r.headers.get('retry-after')) or RATE_LIMIT_WAITS[waits]
                waits += 1
                self.waited += wait
                self._tell(f'{_host(url)} asks to wait (HTTP {r.status:d}): waiting {wait:.0f} s, then one request every '
                           f'{self.interval:.2f} s')
                self._sleep(wait)
                continue
            if r.status in GATEWAY_ERRORS and r.status not in ok:
                errors += 1  # one budget of TRIES with the network errors
                gateway += 1
                if errors >= TRIES:
                    raise RuntimeError(f'{_host(url)} answered HTTP {r.status:d} for {redact(url_parse.urlsplit(url).path)} '
                                       f'({errors:d} failed tries, {gateway:d} of them HTTP 502 / 504): try again later')
                wait = GATEWAY_WAIT * gateway
                self._tell(f'{_host(url)} answered HTTP {r.status:d}: trying again in {wait:.0f} s')
                self._sleep(wait)
                continue
            if r.status not in ok:
                raise RuntimeError(f'{_host(url)} answered HTTP {r.status:d} for {redact(url_parse.urlsplit(url).path)}')
            return r

    def _tell(self, msg: str) -> None:
        if self.notify:
            with contextlib.suppress(Exception):
                self.notify(msg)

    def _slow_down(self, r: _Answer) -> None:
        """After a 429 / 503 the rest of the list goes slower: at the pace the site states ('limit of N requests every M seconds'),
        else 1.5 times slower (1 s at least), never slower than CRAWL_DELAY_MAX"""
        if (m := re_rate_limit.search(r.text[:4096])) and int(m.group(1)) > 0:
            pace = int(m.group(2)) / int(m.group(1)) * RATE_LIMIT_MARGIN
        else:
            pace = max(self.interval * 1.5, 1.0)
        self.interval = min(max(self.interval, pace), CRAWL_DELAY_MAX)


# ----- readers: (name, posts count, type name) -----

class TagJob:
    """What the tag update of an extension gets (manifest 'tagUpdate': 'tags.py:read_tags', called as read_tags(job)): the requests
    of the core for its own site only, with the pacing, robots.txt, Retry-After, request budget and no cookies of every list.
    read_tags yields (name, posts, type) for each tag of the site; the core checks each one, keeps those with min_posts or more and
    writes the list. Exported by lynxx.sdk (SDK 1.1).
    type: one of the core's type names ('general', 'artist', 'character', 'copyright', 'metadata'...); a name the core does not know
    is written as 'general'. A type the job gives, 'general' included, is written as it is.
    A row without a type - (name, posts), or None, '' or 'unknown' as its type - takes the type the list the extension ships
    (manifest 'tagList', the file next to the installed manifest) gives that very name when it is a specific one (not 'general' /
    'unknown' / none): a job that cannot tell types keeps the types its maintainer set in the shipped list. Without one it is
    written as before, 'general' ('unknown' for a row typed 'unknown'). Lynxx 2.0.12+: older cores write 'general' ('unknown')
    whatever the shipped list says, so a job on a site with no tag types may yield (name, posts) for every core.
    A merge update (tagUpdate.merge) asks the list in use first, as before: a row without a type, or typed 'general', keeps the
    type that list has for its tag; the shipped list is asked only for the rows the list in use says nothing specific of.
    min_posts: for a merge update at least merge.minPosts; the job must yield every tag with that many posts or more"""

    def __init__(self, client: PoliteClient, api_key: str = '', min_posts: int = 1) -> None:
        self._client = client
        self.site = client.site
        '''https://host/ of the extension (manifest 'site')'''
        self.api_key = api_key
        '''the user's own key for this module, '' without one'''
        self.min_posts = min_posts

    def get_text(self, url: str, accept: str = '') -> str:
        """GET a page of the site (https, the site's host or a subdomain), its text. Raises when the site refuses or fails"""
        return self._client.get(url, accept=accept or 'text/html').text

    def get_json(self, url: str):
        """GET an API answer of the site, its json (None when the body is not json)"""
        return self._client.get(url, accept='application/json').json()

    def log(self, text: str) -> None:
        """A line of the update's progress (shown in the app; the update puts the module in front)"""
        self._client._tell(redact(str(text))[:300])


def _job_function(source: TagSource) -> Callable[[TagJob], Iterable]:
    """The function named by 'tagUpdate.entry', from a .py file at the top of the extension folder"""
    file_name, func_name = source.job.split(':', 1)
    folder = pathlib.Path(source.folder).resolve()
    path = (folder / file_name).resolve()
    if path.parent != folder or path.suffix != '.py' or not path.is_file():
        raise TagSourceError(f'{source.module}: tag update file \'{file_name}\' not found in the extension folder')
    spec = importlib.util.spec_from_file_location(f'lynxx_ext_tags_{source.module}', path)
    if spec is None or spec.loader is None:
        raise TagSourceError(f'{source.module}: unable to load \'{file_name}\'')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    func = getattr(module, func_name, None)
    if not callable(func):
        raise TagSourceError(f'{source.module}: \'{func_name}\' is not a function of \'{file_name}\'')
    return func


def _job_tag(item: object) -> tuple[str, int, str] | None:
    """One (name, posts[, type]) of an extension's update, checked: names as one word (spaces as '_'), types the core does not
    know as general. A row without a type ((name, posts), or None / '' as its type) gets NO_TYPE; 'unknown' stays 'unknown'.
    update_tag_list asks the lists for the type of both (TagJob)"""
    if not isinstance(item, (tuple, list)) or len(item) not in (2, 3):
        return None
    name, posts = item[0], item[1]
    kind = item[2] if len(item) == 3 else None
    if not isinstance(name, str) or isinstance(posts, bool) or not isinstance(posts, int) or posts < 0:
        return None
    name = '_'.join(name.split())
    if not name or len(name) > JOB_TAG_MAX_LEN or not name.isprintable():
        return None
    if kind is None or (isinstance(kind, str) and not kind.strip()):
        return name, posts, NO_TYPE
    kind = kind.strip().lower() if isinstance(kind, str) else ''
    return name, posts, kind if kind in JOB_TAG_TYPES else 'general'


def _iter_extension_job(source: TagSource, client: PoliteClient, api_key: str, min_posts: int) -> Iterator[tuple[str, int, str]]:
    """The rows of the extension's own update, checked (_job_tag): the type is NO_TYPE for a row the job gave none"""
    func = _job_function(source)
    good = bad = 0
    for item in func(TagJob(client, api_key, min_posts)) or ():
        if (tag := _job_tag(item)) is None:
            bad += 1
            continue
        good += 1
        if tag[1] >= min_posts:
            yield tag
    if bad:
        client._tell(f'{bad:d} of {good + bad:d} tags from {source.job} skipped: not (name, posts, type)')
    if not good:
        raise RuntimeError(f'{source.job} gave no tag (expected (name, posts, type) for each tag of the site)')


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _rx_auth(api_key: str) -> str:
    # the user's own key only: Lynxx ships no shared key
    if not (api_key and ',' in api_key):
        raise ValueError('rx needs your own API key, KEY,USER_ID (rule34.xxx: My Account > Options > API Access Credentials)')
    key, user = api_key.split(',', 1)
    return f'&api_key={key.strip()}&user_id={user.strip()}'


def _dapi_auth(source: TagSource, api_key: str) -> str:
    if source.module == 'rx' and source.curated:
        return _rx_auth(api_key)
    if api_key and ',' in api_key:
        key, user = api_key.split(',', 1)
        return f'&api_key={key.strip()}&user_id={user.strip()}'
    if source.key == 'required':
        raise ValueError(f'{source.module} needs your own API key, {source.key_format or "API_KEY,USER_ID"} (the site refuses requests'
                         f' without one)')
    return ''


class Resume:
    """Where a listing stands: 'cursor' to start from (the next page number, or the last id), updated after every page the caller
    consumed; on_page(cursor) is called then (update_tag_list keeps the progress in <out>/.partial/)"""

    def __init__(self, cursor: int = 0, on_page: Callable[[int], None] | None = None) -> None:
        self.cursor = cursor
        self.on_page = on_page


def _id_cursor_pages(fetch: Callable[[int], list[dict]], max_pages: int, what: str, resume: Resume | None = None) -> Iterator[list[dict]]:
    """Pages of a listing by id cursor (tags with an id above the cursor), each tag once. Ends at an empty page or at a page
    shorter than the site's page size. A short first page is the end or a site that answers fewer tags than asked (some cap
    'limit'): one more request tells. A cursor that includes its own id (only the last tag again) ends cleanly, a cursor the site
    ignores (the same tags again) is an error: the list would be incomplete"""
    cursor, pages, page_size = (resume.cursor if resume else 0), 0, PAGE_LIMIT
    while True:
        tags = fetch(cursor)
        if not tags:
            return
        fresh = [t for t in tags if _int(t.get('id')) > cursor]
        if not fresh:
            if pages and len(tags) == 1:
                return
            raise RuntimeError(f'website ignores the {what} cursor (stuck at {cursor:d}), the list would be incomplete')
        yield fresh
        cursor = max(_int(t.get('id')) for t in fresh)
        pages += 1
        if resume:
            resume.cursor = cursor
            if resume.on_page:
                resume.on_page(cursor)
        if max_pages and pages >= max_pages:
            return
        if len(tags) < page_size:
            if pages > 1:
                return
            page_size = len(tags)


def _pid_pages(fetch: Callable[[int], list[dict]], max_pages: int, resume: Resume | None = None) -> Iterator[list[dict]]:
    """Pages of a listing by page number (pid from 0), in the site's own order, until an empty page. A short page is not the end:
    rule34.xxx page 1583 holds 999 tags and one with an empty name, and the most used tags are on the last pages.
    resume.cursor: the next pid"""
    pid = resume.cursor if resume else 0
    pages = 0
    previous: tuple | None = None
    while True:
        tags = fetch(pid)
        if not tags:
            return
        # a site that ignores pid answers the same page forever: stop, or the list (and memory) would grow without end.
        # The same short page again: a site that answers its last page for any pid past the end
        ids = tuple((t.get('id'), t.get('name')) for t in tags)
        if ids == previous:
            if len(tags) < PAGE_LIMIT:
                return
            raise RuntimeError(f'page {pid:d} is the same as page {pid - 1:d}: the site ignores pid, stopping')
        previous = ids
        yield tags
        pid += 1
        pages += 1
        if resume:
            resume.cursor = pid
            if resume.on_page:
                resume.on_page(pid)
        if max_pages and pages >= max_pages:
            return


def _gelbooru_tag_page(client: PoliteClient, auth: str, pid: int) -> list[dict]:
    """Rows of one page of a Gelbooru 0.2 tag index (attribute dicts, xml-escaped values)"""
    text = client.get(f'{client.site}index.php?page=dapi&s=tag&q=index&limit={PAGE_LIMIT:d}&pid={pid:d}{auth}').text
    if 'Missing authentication' in text:
        raise RuntimeError('website requires API key (use --api-key KEY,USER_ID)')
    rows = ({k: v for k, _, v in re_attr.findall(m.group(1))} for m in re_xml_tag.finditer(text))
    # an empty page is '<tag type="array"/>' on rule34.xxx (no id, no name); a tag with an empty name has an id: it counts
    return [r for r in rows if r.get('id') or r.get('name')]


def _gelbooru_tags(page: list[dict], min_posts: int) -> Iterator[tuple[str, int, str]]:
    for t in page:
        count = _int(t.get('count'))
        if count >= min_posts and t.get('name'):
            # xml attribute values are escaped: '&amp;' -> '&', '&#039;' -> "'"
            yield html.unescape(t['name']), count, GELBOORU_TYPES.get(_int(t.get('type')), 'general')


def _iter_gelbooru(client: PoliteClient, auth: str, min_posts: int, max_pages: int, resume: Resume | None = None) -> Iterator[tuple[str, int, str]]:
    # pid paging: the tag index of Gelbooru 0.2 sites (rule34.xxx at least) is not in id order and ignores order / orderby, so an
    # after_id cursor (the highest id of a page) skips every tag of a lower id that comes later: most of the popular ones
    for page in _pid_pages(lambda pid: _gelbooru_tag_page(client, auth, pid), max_pages, resume):
        yield from _gelbooru_tags(page, min_posts)


def _iter_gelbooru_tail(client: PoliteClient, auth: str, min_posts: int, base: dict[str, tuple[int, str]], state: dict,
                        on_page: Callable[[dict], None] | None = None) -> Iterator[tuple[str, int, str]]:
    """The tail of a tag index in storage order (TagSource.tail_updates): the rows whose post count changed since 'base' was read
    sit together at the end (rule34.xxx 2026-11-19, 4 hours after a full read: 8,867 of 8,870 changed tags on the last 10 pages,
    ~960 of 1,000 rows each, then pages without a change).
    Reads back from the last read's end until TAIL_QUIET_PAGES pages in a row barely changed (TAIL_MIN_PAGES at least,
    TAIL_MAX_PAGES at most), then forward from that end to the first empty page: the rows appended since, those that moved
    during this read included. state (kept in the progress file, so a stopped tail continues): phase 'back' / 'fwd', pid
    (next page), quiet (quiet pages in a row), back (pages read back), end (the last read's end, then this one's)"""
    previous: tuple | None = None
    while True:
        back = state['phase'] == 'back'
        if back and (state['pid'] < 0 or state['back'] >= TAIL_MAX_PAGES
                     or (state['quiet'] >= TAIL_QUIET_PAGES and state['back'] >= TAIL_MIN_PAGES)):
            state.update(phase='fwd', pid=state['end'])
            previous = None
            continue
        pid = state['pid']
        page = _gelbooru_tag_page(client, auth, pid)
        ids = tuple((t.get('id'), t.get('name')) for t in page)
        if page and ids == previous:
            raise RuntimeError(f'page {pid:d} is the same as the page before: the site ignores pid, stopping')
        previous = ids
        if not back and not page:
            state['end'] = pid
            return
        changed = 0
        for name, count, ttype in _gelbooru_tags(page, min_posts):
            if name in base and base[name][0] != count:
                changed += 1
            yield name, count, ttype
        if back:
            state['quiet'] = state['quiet'] + 1 if changed <= len(page) * TAIL_QUIET_SHARE else 0
            state['back'] += 1
            state['pid'] = pid - 1
        else:
            state['pid'] = pid + 1
        if on_page:
            on_page(state)


def _iter_danbooru(client: PoliteClient, api_key: str, min_posts: int, max_pages: int, resume: Resume | None = None) -> Iterator[tuple[str, int, str]]:
    auth = ''
    if api_key and ',' in api_key:
        key, login = api_key.split(',', 1)
        auth = f'&login={login.strip()}&api_key={key.strip()}'

    def fetch(cursor: int) -> list[dict]:
        # 'a<id>' cursor: tags with id greater than cursor, no page depth limit
        tags = client.get(f'{client.site}tags.json?limit={PAGE_LIMIT:d}&page=a{cursor:d}'
                          f'&search[hide_empty]=true&only=id,name,post_count,category{auth}', accept='application/json').json()
        return [t for t in tags if isinstance(t, dict)] if isinstance(tags, list) else []

    for page in _id_cursor_pages(fetch, max_pages, 'id', resume):
        for t in page:
            count = _int(t.get('post_count'))
            if count >= min_posts and t.get('name'):
                yield str(t['name']), count, DANBOORU_TYPES.get(_int(t.get('category')), 'general')


def _e621_export_link(page: str, base: str) -> tuple[str, str]:
    """(address, SHA-256 or '') of the tag export the db_exports page of e621 lists, ('', '') when it lists none. The link is
    tags.csv.gz (a dated tags-YYYY-MM-DD.csv.gz is taken too, the newest when there are several, the undated one first); its
    checksum is the one in the same row, used only when that row holds exactly one export link and one checksum"""
    found: list[tuple[str, str, str]] = []
    for row in re_export_row.split(page):
        links, sums = [], []
        for m in re_html_tag.finditer(row):
            attrs = {k.lower(): html.unescape(v).strip() for k, _, v in re_attr.findall(m.group(2))}
            if m.group(1).lower() == 'a' and attrs.get('href'):
                url = url_parse.urljoin(base, attrs['href'])
                name = url_parse.urlsplit(url).path.rsplit('/', 1)[-1]
                if name.endswith('.csv.gz'):
                    links.append((name, url))
            elif m.group(1).lower() == 'input' and re_sha256.fullmatch(attrs.get('value', '')):
                sums.append(attrs['value'].lower())
        for name, url in links:
            if re_e621_tags_export.fullmatch(name):
                found.append((name, url, sums[0] if len(links) == 1 and len(sums) == 1 else ''))
    if not found:
        return '', ''
    _name, url, digest = max(found, key=lambda x: (x[0] == 'tags.csv.gz', x[0]))
    return url, digest


def _e621_rows(name: str, body: bytes, min_posts: int) -> Iterator[tuple[str, int, str]]:
    if body[:2] != b'\x1f\x8b':
        raise RuntimeError(f'{name} is not a gzip file: not read')
    with gzip.open(io.BytesIO(body), 'rt', encoding=UTF8, newline='') as f:
        for row in csv.DictReader(f):
            count = _int(row.get('post_count'))
            if count >= min_posts and row.get('name'):
                yield row['name'], count, E621_TYPES.get(_int(row.get('category')), 'general')


def _iter_e621_export(client: PoliteClient, min_posts: int) -> Iterator[tuple[str, int, str]]:
    """e621 publishes daily database exports, much lighter for the website than paging through the api. The page db_exports lists
    them (gzipped CSV files on its static host, each with its SHA-256): the tag export is read from there and checked. The dated
    files of before (db_export/tags-DATE.csv.gz, the last 4 days) are tried only when the page lists no tag export"""
    host = _host(client.site)
    page = client.get(f'{client.site}{E621_EXPORTS_PAGE}', accept='text/html', ok=(200, 404))
    url, digest = _e621_export_link(page.text, page.url) if page.status == 200 else ('', '')
    if url:
        name = url_parse.urlsplit(url).path.rsplit('/', 1)[-1]
        client._tell(f'downloading {name} from {_host(url)} (the tag export listed on {host}/{E621_EXPORTS_PAGE})')
        r = client.get(url, max_bytes=MAX_EXPORT_BYTES)
        if digest:
            if (got := hashlib.sha256(r.body).hexdigest()) != digest:
                raise RuntimeError(f'{name} does not match the SHA-256 listed on {host}/{E621_EXPORTS_PAGE} ({got[:12]}... instead of '
                                   f'{digest[:12]}...): not read, try again later')
        else:
            client._tell(f'{host}/{E621_EXPORTS_PAGE} lists no SHA-256 for {name}: not checked')
        yield from _e621_rows(name, r.body, min_posts)
        return
    for days_back in range(4):
        day = (datetime.date.today() - datetime.timedelta(days=days_back)).isoformat()
        r = client.get(f'{client.site}db_export/tags-{day}.csv.gz', max_bytes=MAX_EXPORT_BYTES, ok=(200, 404))
        if r.status == 404 or r.body[:2] != b'\x1f\x8b':  # gone, or a page instead of the file
            continue
        yield from _e621_rows(f'tags-{day}.csv.gz', r.body, min_posts)
        return
    raise RuntimeError(f'no recent tag export found on {host} ({E621_EXPORTS_PAGE} lists no tags.csv.gz)')


def _iter_moebooru(client: PoliteClient, min_posts: int, max_pages: int) -> Iterator[tuple[str, int, str]]:
    seen: set[str] = set()
    page = 1
    while True:
        tags = client.get(f'{client.site}tag.json?limit={PAGE_LIMIT:d}&page={page:d}&order=name', accept='application/json').json()
        if not isinstance(tags, list) or not tags:
            return
        new = 0
        for t in tags:
            name = str(t.get('name') or '') if isinstance(t, dict) else ''
            if not name or name in seen:
                continue
            seen.add(name)
            new += 1
            if (count := _int(t.get('count'))) >= min_posts:
                yield name, count, MOEBOORU_TYPES.get(_int(t.get('type')), 'general')
        if not new:
            raise RuntimeError(f'website ignores the page parameter (page {page:d} repeats earlier tags), the list would be incomplete')
        if len(tags) < PAGE_LIMIT or (max_pages and page >= max_pages):
            return
        page += 1


def _iter_philomena(client: PoliteClient, min_posts: int, max_pages: int) -> Iterator[tuple[str, int, str]]:
    """most used first; names with spaces get '_' (the philomena downloaders send '_' as ' ')"""
    page, seen = 1, 0
    query = url_parse.urlencode({'q': f'images.gte:{max(1, min_posts):d}', 'per_page': PHILOMENA_PER_PAGE})
    while True:
        data = client.get(f'{client.site}api/v1/json/search/tags?{query}&page={page:d}', accept='application/json').json()
        tags = data.get('tags') if isinstance(data, dict) else None
        if not isinstance(tags, list) or not tags:
            return
        for t in tags:
            if not isinstance(t, dict) or t.get('aliased_tag'):
                continue
            name = str(t.get('name') or '').strip().replace(' ', '_')
            if name and (count := _int(t.get('images'))) >= min_posts:
                yield name, count, PHILOMENA_TYPES.get(str(t.get('category') or ''), 'general')
        seen += len(tags)
        total = data.get('total') if isinstance(data.get('total'), int) else PHILOMENA_MAX_RESULTS
        if len(tags) < PHILOMENA_PER_PAGE or seen >= min(total, PHILOMENA_MAX_RESULTS) or (max_pages and page >= max_pages):
            return
        page += 1


def _szuru_type(category: str) -> str:
    c = category.lower()
    return next((t for k, t in (('artist', 'artist'), ('character', 'character'), ('copyright', 'copyright'), ('series', 'copyright'),
                                ('meta', 'metadata'), ('species', 'species')) if k in c), 'general')


def _iter_szurubooru(client: PoliteClient, min_posts: int, max_pages: int) -> Iterator[tuple[str, int, str]]:
    offset, pages = 0, 0
    while True:
        data = client.get(f'{client.site}api/tags/?offset={offset:d}&limit={SZURUBOORU_PER_PAGE:d}&fields=names,category,usages',
                          accept='application/json').json()
        results = data.get('results') if isinstance(data, dict) else None
        if not isinstance(results, list) or not results:
            return
        for t in results:
            names = t.get('names') if isinstance(t, dict) else None
            name = str(names[0]).strip().replace(' ', '_') if isinstance(names, list) and names else ''
            if name and (count := _int(t.get('usages'))) >= min_posts:
                yield name, count, _szuru_type(str(t.get('category') or ''))
        offset += len(results)
        pages += 1
        total = data.get('total')
        if (isinstance(total, int) and offset >= total) or len(results) < SZURUBOORU_PER_PAGE or (max_pages and pages >= max_pages):
            return


def _iter_shimmie(client: PoliteClient, min_posts: int) -> Iterator[tuple[str, int, str]]:
    text = client.get(f'{client.site}api/danbooru/find_tags', accept='application/xml, text/xml', max_bytes=MAX_EXPORT_BYTES).text
    if '<tags' not in text:
        raise RuntimeError(f'{_host(client.site)} has no tag listing (api/danbooru/find_tags)')
    for m in re_xml_tag.finditer(text):
        t = {k: html.unescape(v) for k, _, v in re_attr.findall(m.group(1))}
        count = _int(t.get('count') or t.get('counts'))  # shimmie writes 'counts'
        if t.get('name') and count >= min_posts:
            yield t['name'], count, 'general'


RESUMABLE_ENGINES = {'gelbooru_dapi': 'pid', 'danbooru_json': 'id'}
'''listings a list stopped half way continues: the kind of cursor kept (next page number, last id)'''


def _iter_source(source: TagSource, client: PoliteClient, api_key: str, min_posts: int, max_pages: int,
                 resume: Resume | None = None) -> Iterator[tuple[str, int, str]]:
    engine = source.engine
    if engine == 'gelbooru_dapi':
        return _iter_gelbooru(client, _dapi_auth(source, api_key), min_posts, max_pages, resume)
    if engine == 'danbooru_json':
        return _iter_danbooru(client, api_key, min_posts, max_pages, resume)
    if engine == 'e621_json':
        return _iter_e621_export(client, min_posts)
    if engine == 'moebooru_json':
        return _iter_moebooru(client, min_posts, max_pages)
    if engine == 'philomena_json':
        return _iter_philomena(client, min_posts, max_pages)
    if engine == 'szurubooru_json':
        return _iter_szurubooru(client, min_posts, max_pages)
    if engine == 'shimmie_api':
        return _iter_shimmie(client, min_posts)
    if engine == EXTENSION_JOB:
        return _iter_extension_job(source, client, api_key, min_posts)
    raise ValueError(f'engine \'{engine}\' has no tag list source')


# ----- list files -----

class UpdateResult:
    __slots__ = ('count', 'message', 'module', 'path', 'previous', 'requests', 'waited', 'written')

    def __init__(self, module: str, path: pathlib.Path) -> None:
        self.module, self.path = module, path
        self.count = self.previous = self.requests = 0
        self.waited = 0.0
        '''seconds the site made the update wait (429 / 503)'''
        self.written = False
        self.message = ''

    def as_dict(self) -> dict:
        return {k: (getattr(self, k).as_posix() if k == 'path' else getattr(self, k)) for k in self.__slots__}


def _count_lines(path: pathlib.Path) -> int:
    if not path.is_file():
        return 0
    with open(path, 'rb') as f:
        return max(0, sum(1 for _ in f) - 2)


def write_tag_list(path: pathlib.Path, tags: dict[str, tuple[int, str]]) -> None:
    """Atomic write in bundled list format (one entry per line, sorted by python string order for binary search). A tag whose type
    is '' is written without one ('N posts', read as unknown), as the lists of 2tags/ that have no types"""
    tmp = path.with_name(f'.{path.name}.{os.getpid():d}.{uuid.uuid4().hex[:8]}.tmp')  # concurrent writers never share a tmp file
    try:
        with open(tmp, 'wt', encoding=UTF8, newline='\n') as f:
            f.write('{\n')
            names = sorted(tags)
            for i, name in enumerate(names):
                count, ttype = tags[name]
                kind = f' ({ttype})' if ttype else ''
                f.write(f'{json.dumps(name, ensure_ascii=False)}: "{count:d} posts{kind}"{"," if i < len(names) - 1 else ""}\n')
            f.write('}\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


re_list_value = re.compile(r'^([0-9]+) posts(?: \(([^()]*)\))?$')


def read_tag_list(path: pathlib.Path, untyped: str = 'general') -> dict[str, tuple[int, str]]:
    """{name: (posts, type)} of a list file in the bundled format (write_tag_list). Lines it cannot read are skipped.
    untyped: the type of a line that names none ('' keeps it without one when written back)"""
    return _read_list_lines(path, untyped)[0]


def _read_list_lines(path: pathlib.Path, untyped: str = 'general') -> tuple[dict[str, tuple[int, str]], int]:
    """read_tag_list, and how many lines it could not read"""
    from .tagsdb import TagsDB
    tags: dict[str, tuple[int, str]] = {}
    skipped = 0
    with open(path, encoding=UTF8) as f:
        for line in f:
            if line.strip() in ('{', '}', ''):
                continue
            try:
                name, value = TagsDB._parse_line(line)
            except (ValueError, StopIteration):
                skipped += 1
                continue
            if m := re_list_value.match(value):
                tags[name] = (int(m.group(1)), m.group(2) or untyped)
            else:
                skipped += 1
    return tags, skipped


def _merge_counts(base: dict[str, tuple[int, str]], fetched: dict[str, tuple[int, str]]) -> tuple[int, int]:
    """Puts the tags of a partial read into base, in place: their counts and types win, base keeps every tag the read did not
    list. -> (added, changed)"""
    added = sum(1 for n in fetched if n not in base)
    changed = sum(1 for n, v in fetched.items() if n in base and base[n] != v)
    base.update(fetched)
    return added, changed


def _keep_known_types(base: dict[str, tuple[int, str]], fetched: dict[str, tuple[int, str]]) -> dict[str, tuple[int, str]]:
    """A read that says only 'general' or 'unknown' of a tag (a job that cannot tell types: update_tag_list makes 'general' of a
    row without a type) keeps the type the list in use has for it: a specific one, or none"""
    return {n: (c, base[n][1]) if t in GENERIC_TYPES and n in base else (c, t) for n, (c, t) in fetched.items()}


def _shipped_types(src: TagSource) -> dict[str, str]:
    """name -> type of the tags the list the extension ships (src.tag_list) gives a specific type (SPECIFIC_TYPES). {} when the
    extension names no list, or the file is missing or cannot be read"""
    if not src.tag_list:
        return {}
    try:
        listed = read_tag_list(pathlib.Path(src.tag_list), NO_TYPE)
    except (OSError, ValueError):
        return {}
    return {n: t for n, (_, t) in listed.items() if t in SPECIFIC_TYPES}


def _is_shipped_list(src: TagSource, path: pathlib.Path | None) -> bool:
    """path is the list the extension ships (src.tag_list), the very file"""
    if path is None or not src.tag_list:
        return False
    try:
        return os.path.samefile(path, src.tag_list)
    except OSError:
        return False


def _take_shipped_types(src: TagSource, tags: dict[str, tuple[int, str]], names: Iterable[str]) -> int:
    """Rows of an extension's own update that came without a type (names, see TagJob): each takes, in place, the type the list the
    extension ships gives its tag when that is a specific one (_shipped_types; names are compared as they are written); the
    others stay as they are. The shipped list is read once, and only when there is such a row. -> how many took a type"""
    names = list(names)
    if not names:
        return 0
    shipped = _shipped_types(src)
    took = 0
    for n in names:
        if (kind := shipped.get(n)) is not None:
            tags[n] = (tags[n][0], kind)
            took += 1
    return took


def _merge_base(module: str, src: TagSource, reference: pathlib.Path | None, out: pathlib.Path) -> pathlib.Path | None:
    """The list a partial read (TagSource.merge) goes into: the list in use (reference), else the list at out, else the list the
    extension ships (src.tag_list: the command line where it is not installed). None without one, or when it is the list learned
    from the posts seen here (lynxx/tags_learned.py: its counts are not the site's)"""
    from .tagsdb import _is_learned_list
    for p in (reference, out, pathlib.Path(src.tag_list) if src.tag_list else None):
        if p is not None and p.is_file() and not _is_learned_list(module, p):
            return p
    return None


def _list_stamp(path: pathlib.Path) -> list[int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return [st.st_size, st.st_mtime_ns]


_digests: dict[str, tuple[list[int], str]] = {}
'''path -> (size and time of the file, its sha256): list_digest'''


def list_digest(path: pathlib.Path) -> str:
    """sha256 of a list file, '' when it cannot be read; kept while the file's size and time stay the same (TagsDB compares the
    list a merging extension ships each time it locates the lists)"""
    stamp = _list_stamp(path)
    if stamp is None:
        return ''
    key = os.fspath(path)
    if (hit := _digests.get(key)) is not None and hit[0] == stamp:
        return hit[1]
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            while chunk := f.read(1 << 20):
                h.update(chunk)
    except OSError:
        return ''
    _digests[key] = (stamp, h.hexdigest())
    return h.hexdigest()


def _save_merged(meta_path: pathlib.Path, src: TagSource, written: pathlib.Path, cover: int, onto: str) -> None:
    """<out>/.meta/<abbr>_tags.json of a merge: the size and time of the list it wrote (is_merged_list), the list its merges
    started from (merged_onto)"""
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = meta_path.with_name(f'{meta_path.name}.{uuid.uuid4().hex[:8]}.tmp')
    tmp.write_text(json.dumps({'site': src.site, 'engine': src.engine, 'merged': _list_stamp(written), 'onto': onto,
                               'minPosts': cover, 'updatedAt': time.time()}), encoding=UTF8)
    os.replace(tmp, meta_path)


def merged_onto(path: pathlib.Path) -> str | None:
    """The list at path is the one a merge (TagSource.merge) wrote there last (its size and time are in <its folder>/.meta/): the
    sha256 of the list the first of its merges went into (list_digest; each merge into a merged list keeps it), '' when it went
    into none. None: not a merged list (no stamp, or the file changed since).
    TagsDB lets a file of <data dir>/tags replace the list a merging extension ships only when it is the merge of that very list:
    a learned list that lost its stamp never is, and an extension update that ships another list makes that list the base again,
    whatever the file times (a zip install gives its files the install's time, the catalog bundled with the app the build's)"""
    try:
        d = json.loads((path.parent / META_DIR / path.name).read_text(encoding=UTF8))
    except (OSError, ValueError):
        return None
    stamp = _list_stamp(path)
    if not isinstance(d, dict) or stamp is None or d.get('merged') != stamp:
        return None
    return str(d.get('onto') or '')


def is_merged_list(path: pathlib.Path) -> bool:
    """The list at path is the one a merge (TagSource.merge) wrote there last (merged_onto)"""
    return merged_onto(path) is not None


def _load_partial(path: pathlib.Path, src: TagSource) -> tuple[str, int, dict[str, tuple[int, str]], dict] | None:
    """(mode, cursor, tags, state) of a list stopped half way, None when there is none (or it belongs to another site / engine, or
    is too old). mode: 'full' or 'tail' (only the tags of that read, merged at the end; state: _iter_gelbooru_tail's); files
    written before tails existed are 'full'"""
    try:
        d = json.loads(path.read_text(encoding=UTF8))
    except (OSError, ValueError):
        return None
    if (not isinstance(d, dict) or d.get('site') != src.site or d.get('engine') != src.engine
            or d.get('cursorKind') != RESUMABLE_ENGINES.get(src.engine) or time.time() - float(d.get('saved') or 0) > PARTIAL_MAX_AGE):
        return None
    tags = {str(k): (int(v[0]), str(v[1])) for k, v in (d.get('tags') or {}).items() if isinstance(v, list) and len(v) == 2}
    state = d.get('state') if isinstance(d.get('state'), dict) else {}
    return ('tail' if d.get('mode') == 'tail' else 'full'), int(d.get('cursor') or 0), tags, state


def _save_partial(path: pathlib.Path, src: TagSource, cursor: int, tags: dict[str, tuple[int, str]], mode: str = 'full',
                  state: dict | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'{path.name}.{uuid.uuid4().hex[:8]}.tmp')
    tmp.write_text(json.dumps({'site': src.site, 'engine': src.engine, 'cursorKind': RESUMABLE_ENGINES.get(src.engine), 'cursor': cursor,
                               'mode': mode, 'state': state or {}, 'saved': time.time(),
                               'tags': {k: [c, ty] for k, (c, ty) in tags.items()}}, ensure_ascii=False), encoding=UTF8)
    os.replace(tmp, path)


def _load_meta(path: pathlib.Path, src: TagSource) -> dict | None:
    """How the list of <out> was read last ({pages, fullAt, updatedAt}), None when unknown or for another site / engine"""
    try:
        d = json.loads(path.read_text(encoding=UTF8))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict) or d.get('site') != src.site or d.get('engine') != src.engine:
        return None
    try:
        return {'pages': int(d['pages']), 'fullAt': float(d['fullAt']), 'updatedAt': float(d.get('updatedAt') or 0)}
    except (KeyError, TypeError, ValueError):
        return None


def _save_meta(path: pathlib.Path, src: TagSource, pages: int, full_at: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'{path.name}.{uuid.uuid4().hex[:8]}.tmp')
    tmp.write_text(json.dumps({'site': src.site, 'engine': src.engine, 'pages': pages, 'fullAt': full_at, 'updatedAt': time.time()}),
                   encoding=UTF8)
    os.replace(tmp, path)


def update_tag_list(module: str, out_dir: pathlib.Path, *, min_posts=1, delay=0.5, max_pages=0, api_key='', force=False,
                    progress: ProgressCb | None = None, reference: pathlib.Path | None = None, source: TagSource | None = None,
                    budget=REQUEST_BUDGET, allow_local=False, session=None, min_interval: float | None = None,
                    deadline: float | None = None, full: bool = False) -> UpdateResult:
    """reference: the list currently in use (may live in another folder). The sanity check compares against the bigger one,
    otherwise a failed first update into an empty folder would shadow the bundled list with an almost empty newer file.
    source: default tag_source(module); allow_local / session: tests (fake sites), session: also the generator's own transport;
    min_interval / deadline: see PoliteClient (the generator bounds the time a draft waits for its list).
    A source with tail_updates reads only its last TAIL_PAGES pages and merges them into the list of <out> when that list came
    from a full read less than FULL_EVERY ago (<out>/.meta/); full=True, or no such read, reads everything.
    A source with merge reads only the tags with cover = max(min_posts, merge_min_posts) or more posts and merges them into the list
    in use (reference, else the list of <out>, else the one the extension ships), never into a learned list: the tags read take
    their counts (and their types, unless the read says only 'general' or 'unknown'), new ones are added, every other tag is kept
    (a line without a type keeps none); written to <out> with its stamp in <out>/.meta/ (merged_onto), never over the base.
    An extension's own update (EXTENSION_JOB), Lynxx 2.0.12: a row the job gave no type (TagJob: NO_TYPE, or 'unknown') takes the
    type the list the extension ships (src.tag_list) has for that name when it is a specific one, else it is written as before
    ('general', 'unknown'). A merge asks the list in use first, as before (_keep_known_types): the shipped list is asked for the
    rows that list says nothing specific of (not there, 'general' / 'unknown', a line without a type), never when it is the
    list in use itself. The message says how many rows took a type. Never for the engine readers or the reviewed modules"""
    src = source or tag_source(module)
    if src is None:
        raise ValueError(f'module \'{module}\' has no tag list source (reviewed: {", ".join(CURATED)}; extensions with their own '
                         f'update (manifest tagUpdate); extensions made from the engines {", ".join(ENGINE_TAG_SOURCES)})')
    out_dir.mkdir(parents=True, exist_ok=True)
    res = UpdateResult(module, out_dir / f'{module}_tags.json')
    cover = max(int(min_posts or 0), src.merge_min_posts) if src.merge else min_posts
    '''a merge's job reads every tag with this many posts or more, and claims nothing below'''
    res.previous = max(_count_lines(res.path), _count_lines(reference) if reference else 0)
    client = PoliteClient(src, delay=delay, budget=budget, allow_local=allow_local, session=session, min_interval=min_interval,
                          deadline=deadline)
    tags: dict[str, tuple[int, str]] = {}
    onto = ''
    '''a merge: the list its line of merges started from (merged_onto)'''
    if progress:
        client.notify = lambda msg: progress(f'[{module}] {msg}', len(tags))
    # page / id cursor listings keep their progress in <out>/.partial/: a list stopped half way (rate limit, network, closed app)
    # continues from its cursor next time instead of starting over (the app only reads *_tags.json of <out> itself)
    partial_path = out_dir / PARTIAL_DIR / f'{module}_tags.json'
    meta_path = out_dir / META_DIR / f'{module}_tags.json'
    resumable = src.engine in RESUMABLE_ENGINES and not max_pages
    meta = _load_meta(meta_path, src) if resumable and src.tail_updates and src.engine == 'gelbooru_dapi' else None
    saved = _load_partial(partial_path, src) if resumable else None
    # the end of the list only: when the list of <out> came from a full read that is recent enough, and no full read waits to
    # continue
    tail = bool(meta and not full and res.path.is_file() and meta['pages'] > 0 and time.time() - meta['fullAt'] < FULL_EVERY
                and not (saved and saved[0] == 'full'))
    mode = 'tail' if tail else 'full'
    base: dict[str, tuple[int, str]] = read_tag_list(res.path) if tail else {}
    resume: Resume | None = None
    state: dict | None = None
    pages = [0]
    if resumable and mode == 'full':
        start = 0
        if saved and saved[0] == 'full':
            _mode, start, tags, _state = saved
            if progress:
                where = f'page {start:d}' if RESUMABLE_ENGINES[src.engine] == 'pid' else f'id {start:d}'
                progress(f'[{module}] continuing a stopped update: {len(tags):d} tags kept, from {where}', len(tags))

        def on_page(cursor: int) -> None:
            pages[0] += 1
            if pages[0] % PARTIAL_SAVE_PAGES == 0:
                _save_partial(partial_path, src, cursor, tags)
        resume = Resume(start, on_page)
    elif mode == 'tail':
        state = {'phase': 'back', 'pid': meta['pages'] - 1, 'quiet': 0, 'back': 0, 'end': meta['pages']}
        if saved and saved[0] == 'tail' and saved[3].get('phase') in ('back', 'fwd'):
            _mode, _cursor, tags, state = saved
            if progress:
                progress(f'[{module}] continuing a stopped update: {len(tags):d} tags kept, from page {state["pid"]:d}', len(tags))
        elif progress:
            progress(f'[{module}] reading the end of the list only, back from page {state["pid"]:d} (a full read every '
                     f'{FULL_EVERY // 86400:d} days, the last one {(time.time() - meta["fullAt"]) / 86400:.0f} days ago)', 0)

        def on_tail_page(st: dict) -> None:
            pages[0] += 1
            if pages[0] % PARTIAL_SAVE_PAGES == 0:
                _save_partial(partial_path, src, st['pid'], tags, 'tail', st)
    try:
        source_iter = (_iter_gelbooru_tail(client, _dapi_auth(src, api_key), min_posts, base, state, on_tail_page) if state is not None
                       else _iter_source(src, client, api_key, cover, max_pages, resume))
        told = -math.inf
        for name, count, ttype in source_iter:
            if name not in tags or tags[name][0] < count:
                tags[name] = (count, ttype)
            # every PROGRESS_INTERVAL seconds while tags come in (a page or two): a long read never looks stopped
            if progress and (now := time.monotonic()) - told >= PROGRESS_INTERVAL:
                told = now
                progress(f'[{module}] {len(tags):d} tags...', len(tags))
    except BaseException:
        if (resume is not None or state is not None) and tags:
            with contextlib.suppress(OSError):
                if state is not None:
                    _save_partial(partial_path, src, state['pid'], tags, 'tail', state)
                else:
                    _save_partial(partial_path, src, resume.cursor, tags)
            if progress:
                progress(f'[{module}] stopped: {len(tags):d} tags kept, the next update continues from there', len(tags))
        raise
    finally:
        res.requests = client.used
        res.waited = client.waited
    if resume is not None or state is not None:
        partial_path.unlink(missing_ok=True)  # the listing is complete: nothing to continue
    # an extension's own update: the rows it gave no type are 'general' ('unknown' when it said so) from here on, as before 2.0.12;
    # the list the extension ships is asked for them below (_take_shipped_types), a merge asks the list in use before it
    untyped: set[str] = set()
    typed = 0
    '''rows without a type that took the type of the shipped list'''
    if src.engine == EXTENSION_JOB:
        untyped = {n for n, (_, t) in tags.items() if t == NO_TYPE or t == 'unknown'}
        tags.update({n: (c, 'general') for n, (c, t) in tags.items() if t == NO_TYPE})
    detail = ''
    end_page = state['end'] if state is not None else resume.cursor if resume is not None else 0
    if state is not None:
        # the tail's counts replace the list's; tags it did not see keep theirs until the next full read
        fetched = tags
        tags = base
        added, changed = _merge_counts(tags, fetched)
        detail = (f', the end of the list only: pages {meta["pages"] - state["back"]:d}-{end_page - 1:d} of {end_page:d}, '
                  f'{added:d} new, {changed:d} updated')
    elif src.merge:
        # the job read only the tags with cover+ posts: they go into the list in use, every other tag keeps its count
        if not tags:
            res.message = 'not written: no tags fetched'
            return res
        from .tagsdb import _is_learned_list
        fetched, base_path = tags, _merge_base(module, src, reference, res.path)
        if base_path is None and src.tag_list and not force:
            res.count = len(fetched)
            res.message = (f'not written: no list to merge the tags with {cover:d}+ posts into (the extension names '
                           f'{pathlib.Path(src.tag_list).name}, not found): they would be the whole list ({len(fetched):d} tags read, '
                           f'use --force)')
            return res
        into = 'the list in use' if base_path in (reference, res.path) else 'the list the extension ships'
        # the list this line of merges started from (before the write: the base may be the list of <out>, merged before)
        if base_path is not None:
            onto = merged_onto(base_path)
            onto = list_digest(base_path) if onto is None else onto
        tags, unread = _read_list_lines(base_path, '') if base_path is not None else ({}, 0)  # lines without a type keep none
        if unread and not force:
            res.count = len(fetched)
            res.message = (f'not written: {unread:d} line{"s" if unread != 1 else ""} of {into} could not be read: the merged list '
                           f'would lose {"them" if unread != 1 else "it"} (use --force)')
            return res
        popular = [n for n, (c, _) in tags.items() if c >= cover]
        seen = sum(1 for n in popular if n in fetched)
        if not force and seen < int(len(popular) * SANITY_MIN_RATIO):
            res.count = len(fetched)
            res.message = (f'not written: the read lists {seen:d} of the {len(popular):d} tags with {cover:d}+ posts of {into} '
                           f'({len(fetched):d} tags read; sanity check failed, use --force)')
            return res
        # nor is a learned list what the merged one is measured against: its tags come back on top of it (tags_learned)
        res.previous = max((_count_lines(p) for p in (res.path, reference) if p is not None and not _is_learned_list(module, p)),
                           default=0)
        if progress:
            progress(f'[{module}] {len(fetched):d} tags read, merging them into {into} ({len(tags):d} tags)', len(fetched))
        fetched = _keep_known_types(tags, fetched)
        if not _is_shipped_list(src, base_path):  # the shipped list as the list in use has said all it knows
            typed = _take_shipped_types(src, fetched, (n for n in untyped if fetched[n][1] not in SPECIFIC_TYPES))
        added, changed = _merge_counts(tags, fetched)
        detail = (f', the tags with {cover:d}+ posts merged into {into}: {len(fetched):d} read, {added:d} new, {changed:d} updated, '
                  f'{len(popular) - seen:d} no longer listed kept' if base_path is not None
                  else f', the tags with {cover:d}+ posts only: no list in use to merge into')
    elif resume is not None and src.tail_updates:
        detail = f', full read of {end_page:d} pages'
    res.count = len(tags)
    if res.count == 0:
        res.message = 'not written: no tags fetched'
        return res
    partial = bool(max_pages)
    if not force and (partial or res.count < res.previous * SANITY_MIN_RATIO):
        res.message = (f'not written: fetched {res.count:d} tags vs {res.previous:d} existing'
                       f'{" (partial fetch, use --force)" if partial else " (sanity check failed, use --force)"}')
        return res
    if progress:
        progress(f'[{module}] {res.count:d} tags in the merged list, writing it' if src.merge
                 else f'[{module}] {res.count:d} tags read, writing the list', res.count)
    if untyped and not src.merge:
        typed = _take_shipped_types(src, tags, untyped)
    write_tag_list(res.path, tags)
    res.written = True
    if src.tail_updates and src.engine == 'gelbooru_dapi' and (resume is not None or state is not None):
        _save_meta(meta_path, src, end_page, meta['fullAt'] if state is not None else time.time())
    if src.merge:
        _save_merged(meta_path, src, res.path, cover, onto)
    waited = f', waited {res.waited:.0f} s for the site\'s rate limit' if res.waited else ''
    took = f', {typed:d} type{"s" if typed != 1 else ""} taken from the list the extension ships' if typed else ''
    res.message = f'written {res.count:d} tags (was {res.previous:d}, {res.requests:d} requests{detail}{took}{waited})'
    return res


def _modules_arg(value: str) -> list[str]:
    """'auto': every installed module with a tag source that needs no key of the user (the app's system schedule), except the
    extensions' own updates: those run only when asked (TagSource.on_demand), here when the module is named"""
    mods: list[str] = []
    for m in (x.strip() for x in value.split(',')):
        if m == 'auto':
            mods += [s.module for s in tag_sources() if s.key != 'required' and not s.on_demand]
        elif m:
            mods.append(m)
    return list(dict.fromkeys(mods))


def _lists_in_use() -> dict[str, pathlib.Path]:
    """module -> the list the app uses now (an earlier update, an extension's list, the bundled one): the reference of the CLI's
    sanity check, so a broken first update into an empty folder is not written over a good bundled list"""
    try:
        from .tagsdb import TagsDB
        TagsDB.locate_default_lists()
        return TagsDB.files()
    except Exception:
        return {}


def main(argv: list[str] | None = None, default_out: str | os.PathLike | None = None) -> int:
    """CLI: python -m lynxx.tags_updater, lynxx-core tags (the app's scheduled update), tools/update_tags.py.
    Keys: --api-key for every module, else LYNXX_API_KEY_<MODULE> (KEY,USER_ID); rx fails with a clear message without one.
    Exit code: 0 = every requested list updated (or kept by the sanity check), 1 = at least one module failed"""
    import argparse

    from .extensions import data_dir
    ap = argparse.ArgumentParser(prog='lynxx-core tags',
                                 description='Update Lynxx tag lists used for autocompletion and the A-Z tag browser')
    ap.add_argument('--modules', default='rx,db', help=f'comma separated module abbreviations (reviewed: {",".join(CURATED)}; '
                    f'extensions with their own update or made from a known engine), or \'auto\': every installed module that '
                    f'needs no API key, except the extensions\' own updates (run only when their module is named)')
    ap.add_argument('--out', default=str(default_out or data_dir() / 'tags'), help='output folder (default: <data dir>/tags)')
    ap.add_argument('--min-posts', type=int, default=1, help='skip tags with fewer posts (default 1); an extension whose update merges '
                    '(tagUpdate.merge) reads from max(this, its minPosts) and keeps the other tags of its list')
    ap.add_argument('--delay', type=float, default=0.5, help='delay between requests, seconds (be polite, default 0.5; a robots.txt '
                    f'Crawl-delay wins, extension sites get {ENGINE_MIN_INTERVAL:g} at least)')
    ap.add_argument('--max-requests', type=int, default=REQUEST_BUDGET, help=f'request budget per list (default {REQUEST_BUDGET:d})')
    ap.add_argument('--max-pages', type=int, default=0, help='debug: stop after N pages (partial lists are not written without --force)')
    ap.add_argument('--api-key', default='', help='KEY,USER_ID (gelbooru based) or API_KEY,LOGIN (danbooru based), for every module')
    ap.add_argument('--force', action='store_true', help='write even if the sanity check fails')
    ap.add_argument('--full', action='store_true', help=f'read the whole list even where the last pages would do (Rule34: a full read '
                    f'happens anyway every {FULL_EVERY // 86400:d} days)')
    ap.add_argument('--log', default='', help='append the output to this file (scheduled runs: no shell redirection needed)')
    args = ap.parse_args(argv)
    log = open(args.log, 'a', encoding=UTF8) if args.log else None  # noqa: SIM115 (closed below)

    def say(line: str, err: bool = False) -> None:
        line = redact(line)
        print(line, file=sys.stderr if err else sys.stdout, flush=True)
        if log:
            log.write(line + '\n')
            log.flush()

    failed = 0
    try:
        modules = _modules_arg(args.modules)
        in_use = _lists_in_use()
        if not modules:
            say(f'[{time.strftime("%F %T")}] no installed module with a tag source that needs no API key: nothing to update')
        for module in modules:
            started = time.monotonic()
            try:
                src = tag_source(module)
                if src is not None:  # none: update_tag_list says why
                    say(f'[{time.strftime("%F %T")}] {module}: updating ({src.name or module}: {src.description})...')
                key = args.api_key or os.environ.get(f'LYNXX_API_KEY_{module.upper()}', '').strip()
                res = update_tag_list(module, pathlib.Path(args.out), min_posts=args.min_posts, delay=args.delay, max_pages=args.max_pages,
                                      api_key=key, force=args.force, full=args.full, progress=lambda msg, _n: say(f'  {msg}'), source=src,
                                      budget=args.max_requests, reference=in_use.get(module))
                say(f'[{time.strftime("%F %T")}] {module}: {res.message} in {time.monotonic() - started:.0f}s -> {res.path}')
            except Exception as err:
                failed += 1
                say(f'[{time.strftime("%F %T")}] {module}: FAILED: {err}', True)
    finally:
        if log:
            log.close()
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main())

#
#
#########################################
