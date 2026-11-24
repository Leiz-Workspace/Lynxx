# coding=UTF-8
"""
Lynxx: tag list sources for any extension (lynxx/tags_updater.py, lynxx/robots.py), offline.
A fake site answers every engine's tag listing through a fake transport; the updater's real policy code runs (robots.txt, pacing,
Retry-After, request budget, bot challenges, hosts). End to end: a Gelbooru-type extension made by the generator (lynxx.extgen)
gets its list from the updater and autocompletion shows it.
Every new extension has a list at once: the generator puts the site's list into the draft and the archive (lynxx/extgen/taglist.py),
the installed extension's list is used (TagsDB.extension_lists), and a site without a tag listing gets its list learned from the
posts previewed and downloaded (lynxx/tags_learned.py); a list from a source wins over a learned one.
A running update is listed by tags.sources until it ends; e621's tag export is found on its db_exports page (a trimmed copy in
lynxx/tests/fixtures/) and checked against the SHA-256 listed there.
Run: python -m unittest lynxx.tests.test_tag_sources
"""
#########################################
#
#

import contextlib
import csv
import dataclasses
import datetime
import functools
import gzip
import hashlib
import html
import importlib.util
import io
import itertools
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zipfile
from unittest import TestCase, mock
from urllib import parse as url_parse

from requests.structures import CaseInsensitiveDict

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx import network, server, tags_learned, tags_updater, tagsdb  # noqa: E402
from lynxx.aigen.session import GenRequest, GenSession  # noqa: E402
from lynxx.defines import RATE_LIMIT_RX  # noqa: E402
from lynxx.dlindex import DownloadIndex, IndexRecord  # noqa: E402
from lynxx.downloaders import _make_downloader  # noqa: E402
from lynxx.extgen.fetch import SafeFetcher  # noqa: E402
from lynxx.extgen.sandbox import pack  # noqa: E402
from lynxx.extgen.scaffold import create_draft, survey  # noqa: E402
from lynxx.extgen.taglist import bundle_tag_list  # noqa: E402
from lynxx.extgen.templates import make_manifest  # noqa: E402
from lynxx.modspec import ExtensionManifest, ManifestError  # noqa: E402
from lynxx.module import ProcModule  # noqa: E402
from lynxx.robots import RobotsRules  # noqa: E402
from lynxx.tags import TagTypes  # noqa: E402
from lynxx.tags_updater import CURATED, PoliteClient, TagSource, TagSourceError, tag_source, tag_sources, update_tag_list  # noqa: E402
from lynxx.tagsdb import TagsDB  # noqa: E402

HOST = 'fakebooru.test'
SITE = f'https://{HOST}/'
WAIT = 10.0
TAGS = [  # id, name, posts, type code (gelbooru / danbooru / moebooru / e621: 1 artist, 3 copyright, 4 character)
    (1, 'scenery', 900, 0), (2, 'sky', 700, 0), (3, 'tom_&_jerry', 40, 3), (4, "it's", 12, 0), (5, 'some_artist', 30, 1),
    (6, 'hatsune_miku', 300, 4), (7, 'empty_tag', 0, 0),
]
EXPECTED = {name: posts for _, name, posts, _ in TAGS if posts > 0}
PHILOMENA_CATEGORY = {0: None, 1: 'origin', 3: 'content-official', 4: 'character'}
SZURU_CATEGORY = {0: 'default', 1: 'artist', 3: 'copyright', 4: 'character'}


class FakeResponse:
    def __init__(self, url: str, status: int, body: bytes, headers: dict) -> None:
        self.url, self.status_code, self._body = url, status, body
        self.headers = CaseInsensitiveDict(headers)
        self.is_redirect = self.is_permanent_redirect = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def iter_content(self, n: int):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]


class FakeSession:
    def __init__(self, route) -> None:
        self.route = route
        self.calls: list[tuple[str, dict]] = []

    def request(self, method, url, headers=None, **_):
        self.calls.append((url, dict(headers or {})))
        return FakeResponse(url, *self.route(url, dict(headers or {})))


def _xml(body: str) -> tuple[int, bytes, dict]:
    return 200, body.encode(), {'content-type': 'text/xml'}


def _json(data) -> tuple[int, bytes, dict]:
    return 200, json.dumps(data).encode(), {'content-type': 'application/json'}


class FakeTagSite:
    """One site, every engine's tag listing (and the gelbooru post index the generator's engine detection reads)"""

    def __init__(self, robots: str = '', need_key: bool = False, cap: int = 0, inclusive: bool = False) -> None:
        self.robots = robots
        self.need_key = need_key
        self.cap = cap
        '''answers at most this many tags per page, whatever 'limit' asks (some sites cap it)'''
        self.inclusive = inclusive
        '''the id cursor includes its own id (the last tag of a page comes again on the next one)'''
        self.answers: list[tuple[int, bytes, dict]] = []
        '''answered first, before the routes (429, challenges...)'''

    def __call__(self, url: str, headers: dict) -> tuple[int, bytes, dict]:
        p = url_parse.urlsplit(url)
        qs = {k: v[0] for k, v in url_parse.parse_qs(p.query).items()}
        if p.path == '/robots.txt':
            return (200, self.robots.encode(), {'content-type': 'text/plain'}) if self.robots else (404, b'none', {'content-type': 'text/html'})
        if self.answers:
            return self.answers.pop(0)
        live = sorted((t for t in TAGS if t[2] > 0), key=lambda t: t[0])

        def page(rows: list, after: int, limit: int) -> list:
            return [t for t in rows if (t[0] >= after if self.inclusive and after else t[0] > after)][:min(limit, self.cap or limit)]
        if p.path == '/index.php' and qs.get('page') == 'dapi':
            if self.need_key and 'api_key' not in qs:
                return _xml('"Missing authentication. Go to api.gelbooru.com"')
            if qs.get('s') == 'post':
                return _xml('<?xml version="1.0" encoding="UTF-8"?><posts count="1" offset="0"><post id="1" tags=" scenery "/></posts>')
            # pages by number (pid), in the site's own order: like rule34.xxx, not by id (an after_id cursor would skip tags)
            limit = min(int(qs.get('limit', 100)), self.cap or 1000)
            pid = int(qs.get('pid', 0))
            rows = sorted(TAGS, key=lambda t: t[1])[pid * limit:(pid + 1) * limit]
            if not rows:
                return _xml('<?xml version="1.0" encoding="UTF-8"?><tag type="array"/>')  # rule34.xxx's empty page
            return _xml('<?xml version="1.0" encoding="UTF-8"?><tags type="array">' + ''.join(
                f'<tag type="{c}" count="{n}" name="{html.escape(name, quote=True)}" ambiguous="false" id="{i}"/>' for i, name, n, c in rows)
                + '</tags>')
        if p.path == '/tags.json':
            rows = page(live, int(qs['page'][1:]), int(qs['limit']))
            return _json([{'id': i, 'name': name, 'post_count': n, 'category': c} for i, name, n, c in rows])
        if p.path == '/tag.json':
            limit, page = int(qs['limit']), int(qs['page'])
            rows = sorted(TAGS, key=lambda t: t[1])[(page - 1) * limit:page * limit]
            return _json([{'id': i, 'name': name, 'count': n, 'type': c, 'ambiguous': False} for i, name, n, c in rows])
        if p.path.startswith('/db_export/'):
            yesterday = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
            if p.path != f'/db_export/tags-{yesterday}.csv.gz':
                return 404, b'not found', {'content-type': 'text/html'}
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(['id', 'name', 'category', 'post_count'])
            w.writerows([i, name, c, n] for i, name, n, c in TAGS)
            return 200, gzip.compress(buf.getvalue().encode()), {'content-type': 'application/gzip'}
        if p.path == '/api/v1/json/search/tags':
            per_page, page = int(qs['per_page']), int(qs['page'])
            least = int(qs['q'].split(':', 1)[1])
            rows = [{'name': name.replace('_', ' '), 'images': n, 'category': PHILOMENA_CATEGORY[c], 'aliased_tag': None}
                    for i, name, n, c in sorted(TAGS, key=lambda t: -t[2]) if n >= least]
            rows.insert(1, {'name': 'skies', 'images': 700, 'category': None, 'aliased_tag': 'sky'})  # an alias: not a tag of its own
            return _json({'tags': rows[(page - 1) * per_page:page * per_page], 'total': len(rows)})
        if p.path == '/api/tags/':
            if headers.get('Accept') != 'application/json':
                return 406, b'<html>not acceptable</html>', {'content-type': 'text/html'}
            offset, limit = int(qs['offset']), int(qs['limit'])
            rows = [{'names': [name, f'{name}_alias'], 'category': SZURU_CATEGORY[c], 'usages': n} for i, name, n, c in TAGS]
            return _json({'query': '', 'offset': offset, 'limit': limit, 'total': len(rows), 'results': rows[offset:offset + limit]})
        if p.path == '/api/danbooru/find_tags':
            return _xml('<tags>\n' + ''.join(f"<tag type='0' counts='{n}' name='{html.escape(name, quote=True)}' id='{i}'/>\n"
                                             for i, name, n, c in TAGS) + '</tags>')
        return 404, b'<html>not found</html>', {'content-type': 'text/html'}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s

    def clock(self) -> float:
        return self.now


def _read_list(path: pathlib.Path) -> dict[str, str]:
    return json.loads(path.read_text(encoding='utf-8'))


class RobotsTests(TestCase):
    def test_danbooru_like_rules_longest_match(self) -> None:
        r = RobotsRules.parse('User-agent: *\nDisallow: /\nAllow: /$\nDisallow: /*.json\nAllow: /tags\nAllow: /posts\n', 'Lynxx')
        self.assertFalse(r.allows('https://x/tags.json?limit=1000'))  # '/*.json' (7) beats '/tags' (5)
        self.assertTrue(r.allows('https://x/tags?search=a'))
        self.assertTrue(r.allows('https://x/'))
        self.assertFalse(r.allows('https://x/other'))
        self.assertTrue(r.allows('https://x/robots.txt'))

    def test_agent_group_wildcards_tie_and_delay(self) -> None:
        text = ('# comment\nDisallow: /before-any-group\n'
                'User-agent: *\nDisallow: /\n\n'
                'User-agent: SomeBot\nUser-agent: lynxx/2.0\nDisallow: /private\nAllow: /private/ok$\nCrawl-delay: 3\n\n'
                'User-agent: Lynxx\nDisallow: /*.xml$\nAllow: /a\nDisallow: /a\n')
        r = RobotsRules.parse(text, 'Lynxx')
        self.assertEqual(3.0, r.crawl_delay)
        self.assertTrue(r.allows('/index.php?page=dapi'))  # the agent's groups are merged, the '*' group is not used
        self.assertFalse(r.allows('/private/x'))
        self.assertTrue(r.allows('/private/ok'))
        self.assertFalse(r.allows('/private/ok/more'))  # '$' anchors
        self.assertFalse(r.allows('/feed.xml'))
        self.assertTrue(r.allows('/feed.xml?x=1'))
        self.assertTrue(r.allows('/a'))  # same length: Allow wins
        self.assertTrue(r.allows('/before-any-group'))  # a rule before any user-agent line belongs to no group
        self.assertFalse(RobotsRules.parse(text, 'Other').allows('/index.php'))  # no group of its own: the '*' group
        self.assertTrue(RobotsRules.allow_all('none (404)').allows('/anything'))
        self.assertFalse(RobotsRules.disallow_all('unreachable (503)').allows('/anything'))


class EngineSourceTests(TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.out = pathlib.Path(self.tmp.name)
        self.patches = [mock.patch.object(tags_updater, 'ENGINE_MIN_INTERVAL', 0.0), mock.patch.object(tags_updater, 'PAGE_LIMIT', 3),
                        mock.patch.object(tags_updater, 'PHILOMENA_PER_PAGE', 3), mock.patch.object(tags_updater, 'SZURUBOORU_PER_PAGE', 3)]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def _update(self, engine: str, site: FakeTagSite | None = None, **kw):
        site = site or FakeTagSite()
        session = FakeSession(site)
        src = kw.pop('source', None) or TagSource('zq', engine, SITE, 'Fake')
        res = update_tag_list('zq', self.out / engine, source=src, delay=0, allow_local=True, session=session, **kw)
        return res, session

    def test_every_engine_reads_the_whole_list(self) -> None:
        types = {'gelbooru_dapi': True, 'danbooru_json': True, 'moebooru_json': True, 'e621_json': True, 'philomena_json': True,
                 'szurubooru_json': True, 'shimmie_api': False}
        for engine, typed in types.items():
            with self.subTest(engine=engine):
                res, session = self._update(engine)
                self.assertTrue(res.written, res.message)
                got = _read_list(res.path)
                self.assertEqual(EXPECTED, {k: int(v.split()[0]) for k, v in got.items()})
                if typed:
                    self.assertEqual('40 posts (copyright)', got['tom_&_jerry'])
                    self.assertEqual('30 posts (artist)', got['some_artist'])
                    self.assertEqual('300 posts (character)', got['hatsune_miku'])
                self.assertEqual(f'{SITE}robots.txt', session.calls[0][0])  # robots.txt first, always
                self.assertEqual(len(session.calls), res.requests)
                self.assertTrue(all(url_parse.urlsplit(u).hostname == HOST for u, _ in session.calls))

    def test_capped_page_size_and_inclusive_cursor(self) -> None:
        for engine in ('gelbooru_dapi', 'danbooru_json'):
            for cap, inclusive in ((2, False), (0, True), (2, True)):
                with self.subTest(engine=engine, cap=cap, inclusive=inclusive):
                    res, _ = self._update(engine, FakeTagSite(cap=cap, inclusive=inclusive))
                    self.assertTrue(res.written, res.message)
                    self.assertEqual(set(EXPECTED), set(_read_list(res.path)))
        with mock.patch.object(tags_updater, 'PAGE_LIMIT', 1000):  # a small site: one more request tells the short page is the end
            res, _ = self._update('gelbooru_dapi')
        self.assertEqual(3, res.requests)  # robots.txt, the whole list, the empty page after it

    def test_robots_txt_is_strict_for_extensions_and_reviewed_modules_are_exceptions(self) -> None:
        site = FakeTagSite(robots='User-agent: *\nDisallow: /\nAllow: /$\nDisallow: /*.json\nAllow: /tags\n')
        with self.assertRaises(TagSourceError) as cm:
            self._update('danbooru_json', site)
        self.assertIn('robots.txt', str(cm.exception))
        session = FakeSession(site)
        with self.assertRaises(TagSourceError):
            update_tag_list('zq', self.out, source=TagSource('zq', 'danbooru_json', SITE), delay=0, allow_local=True, session=session)
        self.assertEqual([f'{SITE}robots.txt'], [u for u, _ in session.calls])  # nothing else was asked
        reviewed = TagSource('zq', 'danbooru_json', SITE, robots=False, curated=True)  # like db: the site's documented api
        res, _ = self._update('danbooru_json', site, source=reviewed)
        self.assertTrue(res.written, res.message)
        self.assertFalse(CURATED['db'].robots)
        self.assertTrue(all(s.curated and not s.robots for s in CURATED.values()))

    def test_crawl_delay_and_retry_after(self) -> None:
        site = FakeTagSite(robots='User-agent: *\nCrawl-delay: 7\n')
        site.answers.append((429, b'slow down', {'retry-after': '3'}))
        t = FakeClock()
        times: list[float] = []

        def timed(url: str, headers: dict):
            times.append(t.now)
            return site(url, headers)

        client = PoliteClient(TagSource('zq', 'danbooru_json', SITE), delay=0.5, allow_local=True, session=FakeSession(timed),
                              sleep=t.sleep, clock=t.clock)
        tags = list(tags_updater._iter_danbooru(client, '', 1, 0))
        self.assertEqual(set(EXPECTED), {x[0] for x in tags})
        self.assertEqual(10.5, client.interval)  # the Crawl-delay, 1.5 times slower after the 429 (no rate stated)
        self.assertIn(3.0, t.sleeps)  # Retry-After
        self.assertEqual(5, len(times))  # robots.txt, the 429, pages 1 to 3
        self.assertTrue(all(b - a >= 7.0 - 1e-9 for a, b in itertools.pairwise(times)), times)  # paced by the Crawl-delay

    def test_rate_limit_text_sets_the_pace(self) -> None:
        # api.rule34.xxx answers 429 'You currently have a limit of 60 requests every 60 second(s).': the rest goes at that pace
        site = FakeTagSite()
        site.answers.append((429, b'You currently have a limit of 60 requests every 60 second(s).', {'content-type': 'text/html'}))
        t = FakeClock()
        client = PoliteClient(TagSource('zq', 'gelbooru_dapi', SITE), delay=0.5, allow_local=True, session=FakeSession(site),
                              sleep=t.sleep, clock=t.clock)
        told: list[str] = []
        client.notify = told.append
        tags = list(tags_updater._iter_gelbooru(client, '', 1, 0))
        self.assertEqual(set(EXPECTED), {x[0] for x in tags})
        self.assertAlmostEqual(60 / 60 * tags_updater.RATE_LIMIT_MARGIN, client.interval)
        self.assertEqual(tags_updater.RATE_LIMIT_WAITS[0], client.waited)
        self.assertEqual([f'{HOST} asks to wait (HTTP 429): waiting 5 s, then one request every 1.05 s'], told)  # the user sees why
        # rx: its published limit is the pace from the first request
        rx = PoliteClient(CURATED['rx'], delay=0.5)
        self.assertGreaterEqual(rx.interval, 1.05)

    def test_waits_grow_then_the_list_stops(self) -> None:
        site = FakeTagSite()
        site.answers.extend([(429, b'slow down', {'content-type': 'text/plain'})] * (len(tags_updater.RATE_LIMIT_WAITS) + 1))
        t = FakeClock()
        client = PoliteClient(TagSource('zq', 'gelbooru_dapi', SITE), delay=0, allow_local=True, session=FakeSession(site),
                              sleep=t.sleep, clock=t.clock)
        with self.assertRaisesRegex(RuntimeError, 'still asks to wait'):
            list(tags_updater._iter_gelbooru(client, '', 1, 0))
        waits = [s for s in t.sleeps if s in tags_updater.RATE_LIMIT_WAITS]
        self.assertEqual(list(tags_updater.RATE_LIMIT_WAITS), waits)
        self.assertEqual(sum(tags_updater.RATE_LIMIT_WAITS), client.waited)
        self.assertGreaterEqual(client.interval, 1.0)  # slowed down

    def test_a_gateway_error_is_tried_again(self) -> None:
        # one 502 / 504 of a proxy in front of the site does not end the list: tried again after 5 s, 10 s...
        site = FakeTagSite()
        site.answers.extend([(502, b'<html>Bad Gateway</html>', {'content-type': 'text/html'}),
                             (504, b'<html>Gateway Timeout</html>', {'content-type': 'text/html'})])
        t = FakeClock()
        client = PoliteClient(TagSource('zq', 'gelbooru_dapi', SITE), delay=0, allow_local=True, session=FakeSession(site),
                              sleep=t.sleep, clock=t.clock)
        told: list[str] = []
        client.notify = told.append
        tags = list(tags_updater._iter_gelbooru(client, '', 1, 0))
        self.assertEqual(set(EXPECTED), {x[0] for x in tags})
        self.assertEqual([tags_updater.GATEWAY_WAIT, 2 * tags_updater.GATEWAY_WAIT],
                         [s for s in t.sleeps if s >= tags_updater.GATEWAY_WAIT])
        self.assertEqual([f'{HOST} answered HTTP 502: trying again in 5 s', f'{HOST} answered HTTP 504: trying again in 10 s'], told)
        self.assertEqual(0, client.waited)  # not a wait the site asked for
        # every try a gateway error: the list stops after TRIES of them, with what the site answered
        site = FakeTagSite()
        site.answers.extend([(502, b'<html>Bad Gateway</html>', {'content-type': 'text/html'})] * tags_updater.TRIES)
        client = PoliteClient(TagSource('zq', 'gelbooru_dapi', SITE), delay=0, allow_local=True, session=FakeSession(site),
                              sleep=FakeClock().sleep, clock=t.clock)
        with self.assertRaisesRegex(RuntimeError, rf'answered HTTP 502 for /index.php \({tags_updater.TRIES:d} failed tries, '
                                                  rf'{tags_updater.TRIES:d} of them HTTP 502 / 504\)'):
            list(tags_updater._iter_gelbooru(client, '', 1, 0))
        # network errors and gateway errors share the tries; the wait grows with the gateway errors only
        import requests

        class Flaky(FakeSession):
            def __init__(self, route, fail: int) -> None:
                super().__init__(route)
                self.fail = fail

            def request(self, method, url, headers=None, **kw):
                if not url.endswith('/robots.txt') and self.fail > 0:
                    self.fail -= 1
                    raise requests.ConnectionError('reset')
                return super().request(method, url, headers, **kw)

        site = FakeTagSite()
        site.answers.extend([(502, b'x', {'content-type': 'text/plain'})] * 2)
        clock = FakeClock()
        client = PoliteClient(TagSource('zq', 'gelbooru_dapi', SITE), delay=0, allow_local=True, session=Flaky(site, 3),
                              sleep=clock.sleep, clock=clock.clock)
        with self.assertRaisesRegex(RuntimeError, r'\(5 failed tries, 2 of them HTTP 502 / 504\)'):
            client.get(f'{SITE}index.php')
        self.assertEqual([2.0, 4.0, 6.0, tags_updater.GATEWAY_WAIT], [s for s in clock.sleeps if s >= 2.0])
        # a status the caller accepts is returned as it is
        site = FakeTagSite()
        site.answers.append((502, b'x', {'content-type': 'text/plain'}))
        client = PoliteClient(TagSource('zq', 'gelbooru_dapi', SITE), delay=0, allow_local=True, session=FakeSession(site),
                              sleep=FakeClock().sleep, clock=t.clock)
        self.assertEqual(502, client.get(f'{SITE}x', ok=(200, 502)).status)

    def test_a_stopped_update_continues_where_it_stopped(self) -> None:
        for engine in ('gelbooru_dapi', 'danbooru_json'):
            with self.subTest(engine=engine):
                out = self.out / f'resume-{engine}'
                seen: list[int] = []

                def failing(url: str, headers: dict, site=FakeTagSite(cap=2), calls=[0]):
                    qs = {k: v[0] for k, v in url_parse.parse_qs(url_parse.urlsplit(url).query).items()}
                    if 'pid' in qs or 'page' in qs:
                        calls[0] += 1
                        if calls[0] == 3:  # the third page fails: two pages (4 tags) are kept
                            return 500, b'oops', {'content-type': 'text/plain'}
                    return site(url, headers)
                src = TagSource('zq', engine, SITE, 'Fake')
                with self.assertRaises(RuntimeError):
                    update_tag_list('zq', out, source=src, delay=0, allow_local=True, session=FakeSession(failing))
                partial = out / tags_updater.PARTIAL_DIR / 'zq_tags.json'
                self.assertTrue(partial.is_file())
                self.assertFalse((out / 'zq_tags.json').exists())  # nothing half done is used as the list
                kept = json.loads(partial.read_text(encoding='utf-8'))
                self.assertEqual(tags_updater.RESUMABLE_ENGINES[engine], kept['cursorKind'])
                if engine == 'gelbooru_dapi':  # a partial of the old after_id reader (no cursor kind) is not continued from
                    old = dict(kept, cursorKind=None)
                    partial.with_name('old.json').write_text(json.dumps(old), encoding='utf-8')
                    self.assertIsNone(tags_updater._load_partial(partial.with_name('old.json'), src))
                    partial.with_name('old.json').unlink()

                def recording(url: str, headers: dict, site=FakeTagSite(cap=2)):
                    qs = {k: v[0] for k, v in url_parse.parse_qs(url_parse.urlsplit(url).query).items()}
                    if 'pid' in qs:
                        seen.append(int(qs['pid']))
                    elif 'page' in qs and str(qs['page']).startswith('a'):
                        seen.append(int(qs['page'][1:]))
                    return site(url, headers)
                notes: list[str] = []
                res = update_tag_list('zq', out, source=src, delay=0, allow_local=True, session=FakeSession(recording),
                                      progress=lambda msg, _n: notes.append(msg))
                self.assertTrue(res.written, res.message)
                self.assertEqual(set(EXPECTED), set(_read_list(res.path)))
                self.assertGreater(seen[0], 0)  # started from the saved cursor (page 2 / an id), not from the first page
                self.assertTrue(any('continuing a stopped update' in n for n in notes), notes)
                self.assertFalse(partial.exists())  # complete: nothing left to continue

    def test_progress_comes_with_time_not_with_a_count_of_tags(self) -> None:
        # a slow read (Bbooru: 1000 tags a page, a page a second or more) told only every 5000 tags looked stopped for minutes
        def counts(interval: float) -> list[int]:
            notes: list[tuple[str, int]] = []
            with mock.patch.object(tags_updater, 'PROGRESS_INTERVAL', interval):
                res, _ = self._update('danbooru_json', progress=lambda msg, n: notes.append((msg, n)))
            self.assertTrue(res.written, res.message)
            self.assertEqual(f'[zq] {len(EXPECTED):d} tags read, writing the list', notes[-1][0])
            return [n for msg, n in notes if msg.endswith(' tags...')]

        self.assertEqual(list(range(1, len(EXPECTED) + 1)), counts(0.0))  # each tag as it comes when the interval is over
        self.assertEqual([1], counts(3600.0))  # the first tag at once, then once per interval
        self.assertEqual(2.0, tags_updater.PROGRESS_INTERVAL)

    def test_budget_error_is_raised_and_nothing_is_written(self) -> None:
        with self.assertRaises(TagSourceError) as cm:
            self._update('gelbooru_dapi', budget=2)
        self.assertIn('budget', str(cm.exception))
        self.assertFalse((self.out / 'gelbooru_dapi' / 'zq_tags.json').exists())

    def test_challenge_redirect_https_and_keys(self) -> None:
        site = FakeTagSite()
        site.answers.append((503, b'<html>challenge</html>', {'cf-mitigated': 'challenge'}))
        with self.assertRaises(TagSourceError) as cm:
            self._update('danbooru_json', site)
        self.assertIn('challenge', str(cm.exception))
        site.answers.append((302, b'', {'location': 'https://elsewhere.test/tags.json'}))
        with self.assertRaises(TagSourceError) as cm:
            self._update('danbooru_json', site)
        self.assertIn('elsewhere.test', str(cm.exception))
        with self.assertRaises(TagSourceError):
            self._update('danbooru_json', source=TagSource('zq', 'danbooru_json', f'http://{HOST}/'))
        # a site that refuses anonymous requests: the user's own key or nothing, never a request without it
        need = TagSource('zq', 'gelbooru_dapi', SITE, key='required', key_format='API_KEY,USER_ID')
        session = FakeSession(FakeTagSite(need_key=True))
        with self.assertRaises(ValueError) as cm:
            update_tag_list('zq', self.out, source=need, delay=0, allow_local=True, session=session)
        self.assertIn('your own API key', str(cm.exception))
        self.assertEqual([], session.calls)
        res = update_tag_list('zq', self.out, source=need, delay=0, allow_local=True, session=session, api_key='SECRET1, 42')
        self.assertTrue(res.written, res.message)
        self.assertTrue(any('api_key=SECRET1&user_id=42' in u for u, _ in session.calls))
        self.assertEqual('https://x/tags.json?login=<REDACTED>&api_key=<REDACTED>',
                         tags_updater.redact('https://x/tags.json?login=me&api_key=SECRET1'))


class SourceResolutionTests(TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data = pathlib.Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': self.data.as_posix()})
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _install_manifest(self, ext_id: str, abbr: str, engine: str, site: str = SITE, api_key=False) -> dict:
        m = make_manifest(ext_id=ext_id, abbr=abbr, name=ext_id.title(), site=site, engine=engine, media=['image'], api_key=api_key)
        folder = self.data / 'extensions' / ext_id
        folder.mkdir(parents=True)
        (folder / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
        return m

    def test_generator_sets_tag_list_for_engines_with_a_source(self) -> None:
        for engine in tags_updater.ENGINE_TAG_SOURCES:
            self.assertEqual('zq_tags.json', make_manifest(ext_id='zqsite', abbr='zq', name='Zq', site=SITE, engine=engine,
                                                            media=['image'])['tagList'], engine)
        for engine in ('shimmie', 'custom'):
            self.assertNotIn('tagList', make_manifest(ext_id='zqsite', abbr='zq', name='Zq', site=SITE, engine=engine, media=['image']))

    def test_sources_of_installed_extensions(self) -> None:
        self._install_manifest('fakebooru', 'fq', 'gelbooru_dapi')
        self._install_manifest('keybooru', 'kq', 'gelbooru_dapi', site='https://keybooru.test/', api_key=True)
        self._install_manifest('moesite', 'mq', 'moebooru_json', site='https://moe.test/')
        self._install_manifest('htmlsite', 'hq', 'custom', site='https://html.test/')
        srcs = {s.module: s for s in tag_sources()}
        self.assertEqual({'db', 'rx', 'fq', 'kq', 'mq'}, set(srcs))
        self.assertEqual(('gelbooru_dapi', SITE, True, False, 'optional'),
                         (srcs['fq'].engine, srcs['fq'].site, srcs['fq'].robots, srcs['fq'].curated, srcs['fq'].key))
        self.assertEqual('required', srcs['kq'].key)
        self.assertEqual('none', srcs['mq'].key)
        self.assertIs(CURATED['db'], srcs['db'])
        self.assertIsNone(tag_source('hq'))
        self.assertIsNone(tag_source('nope'))
        self.assertEqual(['db', 'fq', 'mq'], tags_updater._modules_arg('auto'))  # the system schedule: no module that needs a key
        self.assertEqual(['xb', 'db', 'fq', 'mq'], tags_updater._modules_arg('xb,auto,db'))

    def test_a_reviewed_abbreviation_on_another_site_is_not_reviewed(self) -> None:
        raw = make_manifest(ext_id='otherxb', abbr='xb', name='Other', site='https://other.test/', engine='gelbooru_dapi', media=['image'])
        m = ExtensionManifest.from_dict(raw, None, 'user')
        src = tags_updater.source_for_manifest(m)
        self.assertEqual(('https://other.test/', True, False), (src.site, src.robots, src.curated))
        raw = {**raw, 'site': 'http://other.test/'}
        self.assertIsNone(tags_updater.source_for_manifest(ExtensionManifest.from_dict(raw, None, 'user')))  # https only

    def test_scheduled_command_auto(self) -> None:
        self._install_manifest('fakebooru', 'fq', 'gelbooru_dapi')
        seen: list[str] = []

        def fake_update(module, out_dir, **kw):
            seen.append(module)
            self.assertEqual(module, kw['source'].module)
            res = tags_updater.UpdateResult(module, out_dir / f'{module}_tags.json')
            res.message = 'written 3 tags (was 0)'
            return res

        with mock.patch.object(tags_updater, 'update_tag_list', fake_update), mock.patch('sys.stdout', io.StringIO()):
            self.assertEqual(0, tags_updater.main(['--modules', 'auto', '--out', (self.data / 'tags').as_posix()]))
        self.assertEqual(['db', 'fq'], seen)


class GeneratedExtensionEndToEndTests(TestCase):
    """the generator makes a Gelbooru-type extension, it is installed, the app's update button (tags.update) reads its list,
    autocompletion (tags.autocomplete) shows its tags"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': (self.root / 'data').as_posix()})
        self.env.start()
        os.environ.pop('LYNXX_TAGS_DIR', None)  # restored by the patch
        TagsDB.clear()

    def tearDown(self) -> None:
        TagsDB.clear()
        self.env.stop()
        self.tmp.cleanup()

    def test_generated_gelbooru_extension_gets_its_tag_list(self) -> None:
        site = FakeTagSite(robots='User-agent: *\nDisallow: /admin\n')
        fetcher = SafeFetcher({HOST}, min_interval=0.0, allow_local=True)
        fetcher._session = FakeSession(lambda url, headers=None: site(url, headers or {}))
        sv = survey(SITE, fetcher)
        self.assertEqual('gelbooru_dapi', sv['engine']['engine'])
        draft = create_draft(self.root / 'drafts', sv, ext_id='fakebooru', abbr='fq', name='Fakebooru')
        manifest = json.loads((draft / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual('fq_tags.json', manifest['tagList'])
        self.assertIn('fq_tags.json', (draft / 'specs.md').read_text(encoding='utf-8'))
        shutil.copytree(draft, self.root / 'data' / 'extensions' / 'fakebooru')  # installed (the updater reads manifests only)

        rows = {r['module']: r for r in server.m_tags_sources({})}
        self.assertEqual({'module': 'fq', 'name': 'Fakebooru', 'engine': 'gelbooru_dapi', 'site': SITE, 'robots': True, 'curated': False},
                         {k: rows['fq'][k] for k in ('module', 'name', 'engine', 'site', 'robots', 'curated')})

        session = FakeSession(site)
        done = threading.Event()
        notes: list[tuple[str, dict]] = []

        def notify(method: str, params: dict) -> None:
            notes.append((method, params))
            if method == 'tags.updateDone':
                done.set()

        public = [(2, 1, 6, '', ('93.184.216.34', 443))]  # the fake host 'resolves' to a public address
        with (mock.patch.object(tags_updater, '_session', lambda: session), mock.patch.object(tags_updater.socket, 'getaddrinfo', return_value=public),
              mock.patch.object(tags_updater, 'ENGINE_MIN_INTERVAL', 0.0), mock.patch.object(server, 'notify', notify)):
            self.assertEqual([], server.m_tags_autocomplete({'module': 'fq', 'prefix': 'sc'}))
            server.m_tags_update({'module': 'fq', 'delay': 0})
            self.assertTrue(done.wait(WAIT), notes)
        self.assertTrue(notes[-1][1]['ok'] and notes[-1][1]['written'], notes[-1])
        self.assertEqual((self.root / 'data' / 'tags' / 'fq_tags.json').as_posix(), notes[-1][1]['path'])
        self.assertEqual(f'{SITE}robots.txt', session.calls[0][0])
        self.assertEqual(['scenery'], [r['tag'] for r in server.m_tags_autocomplete({'module': 'fq', 'prefix': 'sc'})])
        self.assertEqual(['tom_&_jerry'], [r['tag'] for r in server.m_tags_autocomplete({'module': 'fq', 'prefix': 'tom'})])



def _draft(root: pathlib.Path, engine: str = 'gelbooru_dapi', api_key: bool = False, abbr: str = 'fq') -> pathlib.Path:
    draft = root / 'drafts' / 'fakebooru'
    draft.mkdir(parents=True)
    m = make_manifest(ext_id='fakebooru', abbr=abbr, name='Fakebooru', site=SITE, engine=engine, media=['image'], api_key=api_key)
    (draft / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
    (draft / 'downloader.py').write_text('# test\n', encoding='utf-8')
    return draft


class _DataDirTest(TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.data = self.root / 'data'
        self.env = mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': self.data.as_posix()})
        self.env.start()
        os.environ.pop('LYNXX_TAGS_DIR', None)  # restored by the patch
        TagsDB.clear()
        TagsDB.locate_default_lists()

    def tearDown(self) -> None:
        TagsDB.clear()
        self.env.stop()
        self.tmp.cleanup()


class StorageOrderSite:
    """A Gelbooru tag index in storage order, like rule34.xxx: a tag whose post count changes, and every new tag, goes to the end"""

    def __init__(self, n: int = 20) -> None:
        self.rows = [[i, f'tag{i:02d}', 100 + i] for i in range(1, n + 1)]
        self.next_id = n + 1
        self.pids: list[int] = []
        self.fail_at: int | None = None  # answer 500 for this pid, once

    def bump(self, name: str, count: int) -> None:
        row = next(r for r in self.rows if r[1] == name)
        self.rows.remove(row)
        self.rows.append([row[0], name, count])

    def add(self, name: str, count: int) -> None:
        self.rows.append([self.next_id, name, count])
        self.next_id += 1

    def __call__(self, url: str, headers: dict) -> tuple[int, bytes, dict]:
        p = url_parse.urlsplit(url)
        if p.path == '/robots.txt':
            return 404, b'none', {'content-type': 'text/html'}
        qs = {k: v[0] for k, v in url_parse.parse_qs(p.query).items()}
        pid, limit = int(qs['pid']), tags_updater.PAGE_LIMIT
        if pid == self.fail_at:
            self.fail_at = None
            return 500, b'oops', {'content-type': 'text/plain'}
        self.pids.append(pid)
        rows = self.rows[pid * limit:(pid + 1) * limit]
        if not rows:
            return _xml('<?xml version="1.0" encoding="UTF-8"?><tag type="array"/>')
        return _xml('<?xml version="1.0" encoding="UTF-8"?><tags type="array">' + ''.join(
            f'<tag type="0" count="{n}" name="{name}" ambiguous="false" id="{i}"/>' for i, name, n in rows) + '</tags>')


class TailUpdateTests(TestCase):
    """Rule34: a weekly update reads only the last pages (new tags and tags whose count changed are there) and merges them"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.out = pathlib.Path(self.tmp.name)
        self.patches = [mock.patch.object(tags_updater, 'PAGE_LIMIT', 3), mock.patch.object(tags_updater, 'TAIL_QUIET_PAGES', 2),
                        mock.patch.object(tags_updater, 'TAIL_MIN_PAGES', 1), mock.patch.object(tags_updater, 'ENGINE_MIN_INTERVAL', 0.0)]
        for x in self.patches:
            x.start()
        self.src = TagSource('zq', 'gelbooru_dapi', SITE, 'Fake', tail_updates=True)
        self.site = StorageOrderSite()

    def tearDown(self) -> None:
        for x in self.patches:
            x.stop()
        self.tmp.cleanup()

    def _update(self, **kw):
        self.site.pids.clear()
        notes: list[str] = []
        src = kw.pop('source', self.src)
        res = update_tag_list('zq', self.out, source=src, delay=0, allow_local=True, session=FakeSession(self.site),
                              progress=lambda msg, _n: notes.append(msg), **kw)
        return res, notes

    def _meta(self) -> dict:
        return json.loads((self.out / tags_updater.META_DIR / 'zq_tags.json').read_text(encoding='utf-8'))

    def test_first_update_reads_everything_then_the_tail_only(self) -> None:
        res, _ = self._update()
        self.assertTrue(res.written, res.message)
        self.assertEqual(list(range(8)), self.site.pids)  # 20 tags, 3 a page: 7 pages and the empty one
        self.assertIn('full read of 7 pages', res.message)
        self.assertEqual(7, self._meta()['pages'])
        full_at = self._meta()['fullAt']
        self.site.bump('tag03', 999)  # its count changed: its row moves to the end
        self.site.add('newtag', 5)
        res, notes = self._update()
        self.assertTrue(res.written, res.message)
        # back from the last read's end while pages change (6: tag03 moved there), 2 quiet pages (5, 4), then on to the empty page
        self.assertEqual([6, 5, 4, 7], self.site.pids)
        got = _read_list(res.path)
        self.assertEqual(21, len(got))  # every tag of the full read is kept
        self.assertEqual('999 posts (general)', got['tag03'])
        self.assertEqual('5 posts (general)', got['newtag'])
        self.assertEqual('101 posts (general)', got['tag01'])  # not in the tail: kept as it was
        self.assertIn('the end of the list only: pages 4-6 of 7, 1 new, 1 updated', res.message)
        self.assertTrue(any('reading the end of the list only' in n for n in notes), notes)
        self.assertEqual(7, self._meta()['pages'])
        self.assertEqual(full_at, self._meta()['fullAt'])  # a tail is not a full read

    def test_the_tail_reads_back_as_far_as_the_counts_changed(self) -> None:
        self._update()
        changed = [f'tag{i:02d}' for i in range(5, 14)]  # 9 rows: 3 pages at the end
        for i, name in enumerate(changed):
            self.site.bump(name, 500 + i)
        res, _ = self._update()
        self.assertEqual([6, 5, 4, 3, 2, 1, 7], self.site.pids)  # 4 pages with changed rows (3 holds tag05), 2 quiet, the end
        got = _read_list(res.path)
        self.assertEqual({f'{500 + i} posts (general)' for i in range(9)}, {got[n] for n in changed})
        with mock.patch.object(tags_updater, 'TAIL_MAX_PAGES', 2):  # never longer than TAIL_MAX_PAGES back
            for name in changed:
                self.site.bump(name, 1)
            self._update()
        self.assertEqual([6, 5, 7], self.site.pids)

    def test_full_read_when_asked_too_old_or_unknown(self) -> None:
        self._update()
        res, _ = self._update(full=True)
        self.assertEqual(0, self.site.pids[0])
        self.assertIn('full read', res.message)
        meta_path = self.out / tags_updater.META_DIR / 'zq_tags.json'
        meta = self._meta()
        meta['fullAt'] -= tags_updater.FULL_EVERY + 60
        meta_path.write_text(json.dumps(meta), encoding='utf-8')
        self._update()
        self.assertEqual(0, self.site.pids[0])  # the last full read is too old
        meta_path.unlink()
        self._update(source=TagSource('zq', 'gelbooru_dapi', SITE, 'Fake'))
        self.assertEqual(0, self.site.pids[0])  # a source without tail updates always reads everything
        self.assertFalse(meta_path.exists())  # and keeps no read state
        self._update()
        self.assertEqual(0, self.site.pids[0])  # how the list was read is unknown
        (self.out / 'zq_tags.json').unlink()
        self._update()
        self.assertEqual(0, self.site.pids[0])  # nothing to merge into

    def test_a_stopped_full_read_continues_before_any_tail(self) -> None:
        self._update()
        self.site.fail_at = 3
        with self.assertRaises(RuntimeError):
            self._update(full=True)
        res, notes = self._update()  # auto: the stopped full read goes on, it is not replaced by a tail
        self.assertTrue(res.written, res.message)
        self.assertEqual(3, self.site.pids[0])
        self.assertIn('full read', res.message)
        self.assertTrue(any('continuing a stopped update' in n for n in notes), notes)

    def test_a_stopped_tail_continues_as_a_tail(self) -> None:
        self._update()
        self.site.add('late', 7)
        self.site.bump('tag20', 1)
        self.site.fail_at = 5
        with self.assertRaises(RuntimeError):
            self._update()
        kept = json.loads((self.out / tags_updater.PARTIAL_DIR / 'zq_tags.json').read_text(encoding='utf-8'))
        self.assertEqual('tail', kept['mode'])
        self.assertLess(len(kept['tags']), 10)  # only the tail's tags, not the whole list
        res, _ = self._update()
        self.assertTrue(res.written, res.message)
        self.assertEqual(5, self.site.pids[0])  # where it stopped, not the end of the list
        got = _read_list(res.path)
        self.assertEqual(21, len(got))
        self.assertEqual('7 posts (general)', got['late'])
        self.assertEqual('1 posts (general)', got['tag20'])

    def test_read_tag_list_reads_what_write_tag_list_wrote(self) -> None:
        tags = {'plain': (5, 'general'), 'say_"hi"': (3, 'artist'), 'back\\slash': (2, 'character'), 'ünïcode': (1, 'metadata')}
        path = self.out / 'zz_tags.json'
        tags_updater.write_tag_list(path, tags)
        self.assertEqual(tags, tags_updater.read_tag_list(path))


class KeyRateLimitTests(_DataDirTest):
    """A limit per API key (rule34.xxx: 60 requests every 60 s) that the site's admins may raise for the user's key: the app writes the
    new limit to <data dir>/ratelimit/limits.json, every core process paces the key with it, together (shared_rate_wait)"""

    def _write(self, limits: dict) -> None:
        f = self.data / 'ratelimit' / network.RATE_LIMITS_FILE
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(limits), encoding='utf-8')
        os.utime(f, ns=(time.time_ns(), time.time_ns() + 1))  # a new stamp even within the same clock tick

    def test_published_file_and_env(self) -> None:
        os.environ.pop('LYNXX_RATE_LIMIT_RX', None)
        self.assertEqual((60, 60), network.key_rate_limit('rx', RATE_LIMIT_RX))  # nothing set: what the site publishes
        self.assertAlmostEqual(1.05, network.key_rate_interval('rx', RATE_LIMIT_RX))
        self.assertIsNone(network.key_rate_limit('zq', None))
        self.assertEqual(0.0, network.key_rate_interval('zq', None))
        self._write({'rx': 120, 'xb': -1, 'bb': 'x', 'db': True})
        self.assertEqual((120, 60), network.key_rate_limit('rx', RATE_LIMIT_RX))  # the app's setting
        self.assertAlmostEqual(0.525, network.key_rate_interval('rx', RATE_LIMIT_RX))
        for bad in ('xb', 'bb', 'db'):
            self.assertIsNone(network.key_rate_limit(bad, None), bad)
        self._write({'rx': 240})
        self.assertEqual((240, 60), network.key_rate_limit('rx', RATE_LIMIT_RX))  # read again once the file changed
        with mock.patch.dict(os.environ, {'LYNXX_RATE_LIMIT_RX': '90'}):
            self.assertEqual((90, 60), network.key_rate_limit('rx', RATE_LIMIT_RX))  # CLI / cron
        with mock.patch.dict(os.environ, {'LYNXX_RATE_LIMIT_RX': 'lots'}):
            self.assertEqual((240, 60), network.key_rate_limit('rx', RATE_LIMIT_RX))

    def test_downloads_and_tag_updates_pace_the_key_together(self) -> None:
        os.environ.pop('LYNXX_RATE_LIMIT_RX', None)
        dwn = _make_downloader(ProcModule.RX)
        self.assertAlmostEqual(1.05, dwn._get_min_request_interval())
        dwn.favorites_search_user = '1'  # html pages of the main site: not the API
        self.assertEqual(0.0, dwn._get_min_request_interval())
        dwn.favorites_search_user = ''
        self._write({'rx': 120})
        self.assertAlmostEqual(0.525, dwn._get_min_request_interval())
        client = PoliteClient(CURATED['rx'], delay=0)
        self.assertAlmostEqual(0.525, client.interval)
        self.assertEqual(dwn._get_sitename(), client._shared)  # the same lock file as the downloads of that key
        calls: list[tuple[str, float]] = []
        with mock.patch.object(tags_updater, 'shared_rate_wait', lambda site, interval: calls.append((site, interval)) or True):
            client._pace()
        self.assertEqual([(CURATED['rx'].site, 0.525)], calls)
        self.assertFalse(PoliteClient(TagSource('zq', 'danbooru_json', SITE), delay=0, allow_local=True)._shared)  # no known limit

    def test_shared_wait_spaces_two_clients(self) -> None:
        with mock.patch.dict(os.environ, {'LYNXX_RATE_LIMIT_ZQ': '600'}):  # 0.105 s
            src = TagSource('zq', 'danbooru_json', SITE, rate_limit=(1, 1))
            a, b = PoliteClient(src, delay=0, allow_local=True), PoliteClient(src, delay=0, allow_local=True)
            started = time.monotonic()
            for c in (a, b, a, b):
                c._pace()
            self.assertGreaterEqual(time.monotonic() - started, 3 * 0.105 * 0.9)  # 4 requests of two clients: 3 gaps

    def test_extensions_declare_their_limit_in_the_manifest(self) -> None:
        self.assertEqual((30, 60), tags_updater.manifest_rate_limit({'rateLimit': {'requests': 30, 'seconds': 60}}))
        for bad in ({}, {'rateLimit': {'requests': 0, 'seconds': 60}}, {'rateLimit': {'requests': True, 'seconds': 60}},
                    {'rateLimit': [60, 60]}, None):
            self.assertIsNone(tags_updater.manifest_rate_limit(bad), bad)
        rx = json.loads((pathlib.Path(tags_updater.__file__).parent / 'builtin_ext' / 'rule34' / 'manifest.json').read_text('utf-8'))
        self.assertEqual(RATE_LIMIT_RX, tags_updater.manifest_rate_limit(rx['auth']))  # what the app shows = what the core uses


class DraftTagListTests(_DataDirTest):
    """the generator puts the site's list into the draft; what does not fit is left to the app (read after the install)"""

    def _bundle(self, draft: pathlib.Path, site: FakeTagSite | None = None, **kw) -> tuple[dict, FakeSession]:
        session = FakeSession(site or FakeTagSite())
        return bundle_tag_list(draft, session=session, allow_local=True, min_interval=0.0, **kw), session

    def test_the_list_goes_into_the_draft_and_the_archive(self) -> None:
        draft = _draft(self.root)
        res, session = self._bundle(draft)
        self.assertEqual(('bundled', 'fq_tags.json', len(EXPECTED)), (res['status'], res['file'], res['count']), res)
        self.assertEqual(EXPECTED, {k: int(v.split()[0]) for k, v in _read_list(draft / 'fq_tags.json').items()})
        self.assertEqual([], [p.name for p in draft.iterdir() if p.name.startswith('.taglist-')])
        self.assertEqual(f'{SITE}robots.txt', session.calls[0][0])
        z = pack(draft, self.root / 'fakebooru.zip')
        with zipfile.ZipFile(z['path']) as zf:
            self.assertIn('fakebooru/fq_tags.json', zf.namelist())

    def test_what_does_not_fit_is_left_to_the_app_or_learned(self) -> None:
        cases = {
            'requests': ({'budget': 2}, FakeTagSite(), 'later'),
            'size': ({'max_bytes': 10}, FakeTagSite(), 'later'),
            'robots': ({}, FakeTagSite(robots='User-agent: *\nDisallow: /index.php\n'), 'learned'),
            'no answer': ({}, FakeTagSite(robots='User-agent: *\nAllow: /\n'), 'failed'),
        }
        for name, (kw, site, status) in cases.items():
            with self.subTest(name):
                draft = _draft(self.root / name)
                if name == 'no answer':
                    site.answers.append((500, b'oops', {'content-type': 'text/html'}))
                res, _ = self._bundle(draft, site, **kw)
                self.assertEqual(status, res['status'], res)
                self.assertFalse((draft / 'fq_tags.json').exists())
        res, session = self._bundle(_draft(self.root / 'key', api_key=True))  # the site needs the user's key: no request here
        self.assertEqual('later', res['status'])
        self.assertEqual([], session.calls)
        res, session = self._bundle(_draft(self.root / 'html', engine='custom'))
        self.assertEqual('learned', res['status'])
        self.assertEqual([], session.calls)

    def test_a_ready_generator_session_ships_the_list(self) -> None:
        from lynxx.tests.test_extgen import fake_fetcher, fake_gelbooru
        tags = FakeTagSite()

        def route(url: str):
            return tags(url, {}) if 's=tag' in url else fake_gelbooru(url)

        report = GenSession(GenRequest(url=SITE, mode='template'), drafts_root=self.root / 'gen', fetcher=fake_fetcher(route)).run()
        self.assertEqual('ready', report['status'], report['summary'])
        self.assertEqual('bundled', report['tagList']['status'], report['tagList'])
        self.assertIn(f'{report["abbr"]}_tags.json', report['files'])
        off = GenSession(GenRequest(url=SITE, mode='template', tag_list=False), drafts_root=self.root / 'off', fetcher=fake_fetcher(route)).run()
        self.assertEqual(('ready', {}), (off['status'], off['tagList']))

    def test_installed_extension_list_is_used_and_a_newer_list_wins(self) -> None:
        draft = _draft(self.root)
        self._bundle(draft)
        installed = self.data / 'extensions' / 'fakebooru'
        shutil.copytree(draft, installed)
        TagsDB.locate_default_lists()
        self.assertEqual(installed / 'fq_tags.json', TagsDB.get_file('fq'))
        self.assertEqual(['scenery'], [r['tag'] for r in server.m_tags_autocomplete({'module': 'fq', 'prefix': 'sc'})])
        newer = self.data / 'tags' / 'fq_tags.json'
        newer.parent.mkdir(parents=True)
        tags_updater.write_tag_list(newer, {'scenery': (999, 'general'), 'scarf': (5, 'general')})
        later = time.time() + 60
        os.utime(newer, (later, later))
        TagsDB.locate_default_lists(reload=('fq',))
        self.assertEqual(newer, TagsDB.get_file('fq'))
        self.assertEqual(['scenery', 'scarf'], [r['tag'] for r in server.m_tags_autocomplete({'module': 'fq', 'prefix': 'sc'})])

    def test_an_extension_ships_only_its_own_list(self) -> None:
        folder = self.data / 'extensions' / 'fakebooru'
        folder.mkdir(parents=True)
        m = {**make_manifest(ext_id='fakebooru', abbr='fq', name='Fakebooru', site=SITE, engine='gelbooru_dapi', media=['image']),
             'tagList': 'rx_tags.json'}
        (folder / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
        tags_updater.write_tag_list(folder / 'rx_tags.json', {'fake': (1, 'general')})
        self.assertNotIn('rx', TagsDB.extension_lists())
        self.assertNotIn('fq', TagsDB.extension_lists())


class LearnedTagListTests(_DataDirTest):
    """a site without a tag listing: the list is learned from the posts previewed and downloaded, no request"""

    def setUp(self) -> None:
        super().setUp()
        os.environ['LYNXX_TAGS_LEARN'] = '1'  # off for the other tests (lynxx/tests/__init__.py); restored by the patch

    def test_preview_posts_teach_a_module_without_a_list(self) -> None:
        self.assertTrue(tags_learned.may_learn('zq'))
        notes: list[tuple[str, dict]] = []
        with mock.patch.object(server, 'notify', lambda m, p: notes.append((m, p))):
            server._learn_quietly('zq', [('1', ['Sky', 'cloud', 'blue sky']), ('2', ['sky']), ('1', ['sky', 'again'])])
        self.assertEqual('tags.learned', notes[-1][0])
        self.assertEqual({'module': 'zq', 'learned': True, 'posts': 2, 'tags': 3}, notes[-1][1])
        self.assertEqual({'sky': '2 posts (unknown)', 'cloud': '1 posts (unknown)', 'blue_sky': '1 posts (unknown)'},
                         _read_list(tags_learned.list_path('zq')))
        self.assertEqual(['sky'], [r['tag'] for r in server.m_tags_autocomplete({'module': 'zq', 'prefix': 'sk'})])
        row = next(r for r in server.m_tags_status({}) if r['module'] == 'zq')
        self.assertTrue(row['learned'])
        self.assertEqual((0, False), tags_learned.learn('zq', [('2', ['sky'])]))  # seen before: counted once

    def test_downloads_teach_it_from_the_index(self) -> None:
        tags_learned.learn('zq', [('1', ['sky'])])  # seen in Preview first
        with DownloadIndex(create=True) as index:
            index.add_many([IndexRecord(module='zq', post_id='1', path='/x/zq_1.jpg', tags='sky'),
                            IndexRecord(module='zq', post_id='2', path='/x/zq_2.jpg', tags='sky tree'),
                            IndexRecord(module='other', post_id='3', path='/x/o_3.jpg', tags='ignored')])
        res = server.m_tags_learn({'module': 'zq'})
        self.assertEqual((1, True, 2), (res['newPosts'], res['learned'], res['posts']))
        self.assertEqual(0, server.m_tags_learn({'module': 'zq'})['newPosts'])  # the index cursor moved on
        with DownloadIndex(create=True) as index:
            index.add_many([IndexRecord(module='zq', post_id='4', path='/x/zq_4.jpg', tags='tree')])
        self.assertEqual(1, server.m_tags_learn({'module': 'zq'})['newPosts'])
        self.assertEqual({'sky': '2 posts (unknown)', 'tree': '2 posts (unknown)'}, _read_list(tags_learned.list_path('zq')))
        with self.assertRaises(server.RpcError):
            server.m_tags_learn({'module': '../x'})

    def test_a_list_from_a_source_stops_the_learning(self) -> None:
        tags_learned.learn('zq', [('1', ['sky'])])
        real = tags_learned.list_path('zq')
        tags_updater.write_tag_list(real, {'sky': (5000, 'general'), 'skyline': (40, 'general')})  # the updater got the real list
        TagsDB.locate_default_lists(reload=('zq',))
        self.assertFalse(tags_learned.may_learn('zq'))
        self.assertEqual((0, False), tags_learned.learn('zq', [('2', ['sky', 'new'])]))
        self.assertEqual({'sky': '5000 posts (general)', 'skyline': '40 posts (general)'}, _read_list(real))
        self.assertFalse(tags_learned.status('zq')['learned'])
        self.assertFalse(tags_learned.may_learn('rp'))  # a list bundled in 2tags/
        TagsDB.load_from_dict('zm', {'sky': (TagTypes.TAG_TYPE_GENERAL, 3)})
        self.assertFalse(tags_learned.may_learn('zm'))  # a list registered in memory
        with mock.patch.dict(os.environ, {'LYNXX_TAGS_LEARN': '0'}):
            self.assertFalse(tags_learned.may_learn('zn'))
            self.assertEqual((0, False), tags_learned.learn('zn', [('1', ['sky'])]))
        self.assertFalse(tags_learned.may_learn('../x'))
        with mock.patch.object(server.threading, 'Thread') as thread:  # Preview: nothing to learn, no thread
            server._learn_from_preview('rp', [{'id': '1', 'tags': ['a']}])
            thread.assert_not_called()

    def test_a_list_from_a_source_wins_over_a_newer_learned_list(self) -> None:
        # a list learned from Preview, then an extension with a tag source ships the site's list (older file). An extension
        # without a source (Rule34Hentai 1.0.2) gets the learning on top of its list instead: BundledListLearningTests
        tags_learned.learn('fq', [('1', ['sky'])])
        learned = tags_learned.list_path('fq')
        self.assertEqual(learned, TagsDB.get_file('fq'))
        folder = self.data / 'extensions' / 'fakebooru'
        folder.mkdir(parents=True)
        m = {**make_manifest(ext_id='fakebooru', abbr='fq', name='Fakebooru', site=SITE, engine='gelbooru_dapi', media=['image']),
             'tagList': 'fq_tags.json'}
        (folder / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
        tags_updater.write_tag_list(folder / 'fq_tags.json', {'sky': (5000, 'general'), 'skyline': (40, 'general')})
        old = time.time() - 30 * 86400
        os.utime(folder / 'fq_tags.json', (old, old))
        TagsDB.locate_default_lists(reload=('fq',))
        self.assertEqual(folder / 'fq_tags.json', TagsDB.get_file('fq'))
        self.assertEqual(['sky', 'skyline'], [r['tag'] for r in server.m_tags_autocomplete({'module': 'fq', 'prefix': 'sk'})])
        self.assertFalse(tags_learned.may_learn('fq'))
        self.assertTrue(tags_learned.has_source_list('fq'))
        self.assertEqual((0, False), tags_learned.learn('fq', [('2', ['sky', 'new'])]))
        shutil.rmtree(folder)  # uninstalled: the learned list is the module's list again
        TagsDB.locate_default_lists(reload=('fq',))
        self.assertEqual(learned, TagsDB.get_file('fq'))
        self.assertTrue(tags_learned.may_learn('fq'))

    def test_learn_says_why_nothing_was_learned(self) -> None:
        # "Học từ file đã tải" (Tags A-Z): nothing downloaded yet, nothing new, or no learning needed (a list from a source)
        keys = ('mayLearn', 'sourceList', 'indexed', 'newPosts')
        res = server.m_tags_learn({'module': 'zq'})
        self.assertEqual((True, False, False, 0), tuple(res[k] for k in keys))
        with DownloadIndex(create=True) as index:
            index.add_many([IndexRecord(module='zq', post_id='1', path='/x/zq_1.jpg', tags='sky')])
        res = server.m_tags_learn({'module': 'zq'})
        self.assertEqual((True, False, True, 1), tuple(res[k] for k in keys))
        res = server.m_tags_learn({'module': 'zq'})
        self.assertEqual((True, False, True, 0), tuple(res[k] for k in keys))
        self.assertEqual({'learned': True, 'posts': 1, 'tags': 1}, {k: res[k] for k in ('learned', 'posts', 'tags')})
        TagsDB.load_from_dict('zm', {'sky': (TagTypes.TAG_TYPE_GENERAL, 3)})
        res = server.m_tags_learn({'module': 'zm'})
        self.assertEqual((False, True, False, 0), tuple(res[k] for k in keys))
        self.assertFalse(server.m_tags_learn({'module': 'rp'})['mayLearn'])  # its list is bundled in 2tags/
        self.assertTrue(server.m_tags_learn({'module': 'rp'})['sourceList'])


class _ShippedListTest(_DataDirTest):
    """An extension 'fakebooru' (fq) installed the way ext.install does it, with the list it ships; the learning is on"""
    BUNDLED = {'sky': (5000, 'general'), 'skyline': (40, 'general'), 'tree': (300, 'general')}

    def setUp(self) -> None:
        super().setUp()
        os.environ['LYNXX_TAGS_LEARN'] = '1'  # off for the other tests (lynxx/tests/__init__.py); restored by the patch
        self.folder = self.data / 'extensions' / 'fakebooru'

    def _source(self, tags: dict, version: str = '1.0.2', engine: str = 'custom', **extra) -> pathlib.Path:
        src = self.root / 'src' / version / 'fakebooru'
        src.mkdir(parents=True)
        m = {**make_manifest(ext_id='fakebooru', abbr=extra.pop('abbr', 'fq'), name='Fakebooru', site=extra.pop('site', SITE),
                             engine=engine, media=['image']), 'version': version, **extra}
        m['tagList'] = f'{m["abbr"]}_tags.json'
        (src / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
        tags_updater.write_tag_list(src / m['tagList'], tags)
        if 'tagUpdate' in m:
            (src / 'tags.py').write_text(MERGE_JOB_FILE, encoding='utf-8')
        return src

    def _install(self, tags: dict | None = None, version: str = '1.0.2', engine: str = 'custom', src: pathlib.Path | None = None,
                 **extra) -> dict:
        """ext.install of the core (the copy stands in for the transactional install, which imports the extension's code)"""
        src = src or self._source(self.BUNDLED if tags is None else tags, version, engine, **extra)

        def copy(path: pathlib.Path) -> ExtensionManifest:
            shutil.rmtree(self.folder, ignore_errors=True)
            shutil.copytree(path, self.folder)
            return ExtensionManifest.load(self.folder / 'manifest.json', 'user')

        with mock.patch.object(server.Extensions, 'install_from_dir', copy):
            return server.m_ext_install({'path': str(src)})

    def _learn(self, posts: list) -> list[tuple[str, dict]]:
        notes: list[tuple[str, dict]] = []
        with mock.patch.object(server, 'notify', lambda m, p: notes.append((m, p))):
            server._learn_quietly('fq', posts)
        return notes

    @staticmethod
    def _row(module: str = 'fq') -> dict:
        return next(r for r in server.m_tags_status({}) if r['module'] == module)

    @staticmethod
    def _all(module: str = 'fq') -> dict[str, int]:
        total, rows = TagsDB.browse(module, limit=1000)
        names = [name for name, _ in rows]
        assert len(names) == len(set(names)) == total, names  # never a duplicate
        return {name: info.posts_count for name, info in rows}


class BundledListLearningTests(_ShippedListTest):
    """a list shipped inside an extension that has no tag source (Rule34Hentai, Rule34.us 1.0.2; Paheal 1.0.2) is never updated: the
    tags of the posts previewed and downloaded that it lacks are learned on top of it, in a small file of their own (rule34.us:
    18.9 MB, 544k tags, never rewritten or parsed again for it)"""

    def test_tags_the_bundled_list_lacks_are_learned_on_top_of_it(self) -> None:
        self._install()
        bundled = self.folder / 'fq_tags.json'
        before = bundled.stat()
        self.assertEqual((bundled, '1.0.2'), (TagsDB.bundled('fq').path, TagsDB.bundled('fq').version))
        self.assertTrue(tags_learned.may_learn('fq'))
        self.assertFalse(tags_learned.has_source_list('fq'))
        self.assertEqual({'bundled': True, 'version': '1.0.2', 'added': 0, 'learned': False},
                         {k: self._row()[k] for k in ('bundled', 'version', 'added', 'learned')})
        with mock.patch.object(server.threading, 'Thread') as thread:  # Preview starts the learning
            server._learn_from_preview('fq', [{'id': '1', 'tags': ['a']}])
            thread.assert_called_once()
        notes = self._learn([('1', ['Sky', 'new tag', 'tree']), ('2', ['new_tag', 'skyfall']), ('1', ['again'])])
        self.assertEqual(('tags.learned', {'module': 'fq', 'learned': False, 'posts': 2, 'tags': 4}), notes[-1])
        # its own file holds only what the bundled list lacks, out of the lists' names; the bundled list is untouched
        added = tags_learned.additions_path('fq')
        self.assertEqual(self.data / 'tags' / 'learned' / 'fq_added.json', added)
        self.assertEqual({'new_tag': '2 posts (unknown)', 'skyfall': '1 posts (unknown)'}, _read_list(added))
        self.assertFalse(tags_learned.list_path('fq').exists())
        self.assertEqual((before.st_size, before.st_mtime_ns), (bundled.stat().st_size, bundled.stat().st_mtime_ns))
        self.assertEqual((bundled, added), (TagsDB.get_file('fq'), TagsDB.get_additions('fq')))
        # autocompletion and Tags A-Z: the bundled list plus the learned tags, the bundled counts stay
        self.assertEqual({'sky': 5000, 'skyline': 40, 'tree': 300, 'new_tag': 2, 'skyfall': 1}, self._all())
        rows = server.m_tags_autocomplete({'module': 'fq', 'prefix': 'sky'})
        self.assertEqual([('sky', 5000, 'general'), ('skyline', 40, 'general'), ('skyfall', 1, 'unknown')],
                         [(r['tag'], r['posts'], r['type']) for r in rows])
        self.assertEqual(['skyfall'], [r['tag'] for r in server.m_tags_fuzzy({'module': 'fq', 'text': 'skyfal'})][:1])
        res = server.m_tags_resolve({'module': 'fq', 'tags': ['new_tag', 'tree', 'nope']})
        self.assertEqual([True, True, False], [r['known'] for r in res['items']])
        self.assertEqual({'bundled': True, 'version': '1.0.2', 'added': 2, 'learned': False, 'path': bundled.as_posix()},
                         {k: self._row()[k] for k in ('bundled', 'version', 'added', 'learned', 'path')})
        self.assertEqual('tags.learned', self._learn([('3', ['tree', 'new_tag'])])[-1][0])  # the count of an addition went up
        self.assertEqual({'new_tag': '3 posts (unknown)', 'skyfall': '1 posts (unknown)'}, _read_list(added))
        self.assertEqual(3, self._all()['new_tag'])
        self.assertEqual([], self._learn([('3', ['tree', 'other'])]))  # counted before: nothing written, nothing told

    def test_a_preview_looks_up_only_its_own_tags_and_the_bundled_list_is_not_parsed_again(self) -> None:
        TagsDB.CacheMinSize = 0  # every list has its binary cache (lists over 1 MB, rule34.us 18.9 MB)
        self.addCleanup(setattr, TagsDB, 'CacheMinSize', tagsdb.TAGS_CACHE_MIN_SIZE)
        self._install()
        self._learn([('1', ['sky', 'old_a'])])
        self.assertEqual(1, self._all()['old_a'])
        looked: list[list[str]] = []
        parsed: list[pathlib.Path] = []
        lacking, parse = TagsDB.lacking, tagsdb._parse_tag_list
        with mock.patch.object(TagsDB, 'lacking', lambda m, names: looked.append(list(names)) or lacking(m, names)), \
                mock.patch.object(tagsdb, '_parse_tag_list', lambda path: parsed.append(pathlib.Path(path)) or parse(path)):
            self._learn([('2', ['tree', 'new_b']), ('3', ['new_b'])])
            self.assertEqual({'sky': 5000, 'skyline': 40, 'tree': 300, 'old_a': 1, 'new_b': 2}, self._all())
        self.assertEqual([['new_b', 'tree']], looked)  # the tags of the posts counted now, not the whole learned set
        self.assertEqual([tags_learned.additions_path('fq')], parsed)  # the additions only (small): the bundled list comes from its cache
        self.assertEqual(tags_learned.additions_path('fq'), TagsDB.get_additions('fq'))

    def test_an_update_of_the_extension_keeps_the_additions_its_new_list_lacks(self) -> None:
        self._install()
        self._learn([('1', ['sky', 'new_a', 'new_b'])])
        added = tags_learned.additions_path('fq')
        self.assertEqual({'new_a': '1 posts (unknown)', 'new_b': '1 posts (unknown)'}, _read_list(added))
        newer = {**self.BUNDLED, 'new_a': (77, 'character')}
        # the new list alone, before the learned file is written again: no duplicate, the bundled count wins
        shutil.rmtree(self.folder)
        shutil.copytree(self._source(newer, '1.0.3'), self.folder)
        TagsDB.locate_default_lists(reload=('fq',))
        self.assertEqual({'sky': 5000, 'skyline': 40, 'tree': 300, 'new_a': 77, 'new_b': 1}, self._all())
        self.assertEqual('character', server.m_tags_resolve({'module': 'fq', 'tags': ['new_a']})['items'][0]['type'])
        # ext.install of the update: the learned file drops what the new list has
        res = self._install(newer, '1.0.4')
        self.assertEqual('1.0.4', res['version'])
        self.assertEqual({'new_b': '1 posts (unknown)'}, _read_list(added))
        self.assertEqual({'sky': 5000, 'skyline': 40, 'tree': 300, 'new_a': 77, 'new_b': 1}, self._all())
        self.assertEqual({'version': '1.0.4', 'added': 1}, {k: self._row()[k] for k in ('version', 'added')})
        # a newer list that has every learned tag: the learned file is emptied, not left with stale additions
        self._install({**newer, 'new_b': (9, 'general')}, '1.0.5')
        self.assertEqual({}, _read_list(added))
        self.assertEqual({'sky': 5000, 'skyline': 40, 'tree': 300, 'new_a': 77, 'new_b': 9}, self._all())
        self.assertEqual(0, self._row()['added'])

    def test_uninstalling_the_extension_leaves_the_whole_learned_list(self) -> None:
        self._install()
        self._learn([('1', ['sky', 'new_a']), ('2', ['sky'])])
        self.assertEqual({'new_a': '1 posts (unknown)'}, _read_list(tags_learned.additions_path('fq')))
        self.assertEqual({'ok': True}, server.m_ext_uninstall({'id': 'fakebooru'}))
        self.assertFalse(self.folder.exists())
        self.assertEqual(tags_learned.list_path('fq'), TagsDB.get_file('fq'))
        self.assertFalse(tags_learned.additions_path('fq').exists())
        self.assertIsNone(TagsDB.bundled('fq'))
        self.assertEqual({'sky': '2 posts (unknown)', 'new_a': '1 posts (unknown)'}, _read_list(tags_learned.list_path('fq')))
        self.assertEqual({'sky': 2, 'new_a': 1}, self._all())
        self.assertEqual({'learned': True, 'bundled': False, 'added': 0},
                         {k: self._row()[k] for k in ('learned', 'bundled', 'added')})
        self.assertTrue(tags_learned.may_learn('fq'))

    def test_a_list_learned_before_the_extension_shipped_one_is_served_on_top(self) -> None:
        # Rule34Hentai before 1.0.2: a list learned from Preview; the update ships the site's list (older file)
        src = self._source(self.BUNDLED)
        old = time.time() - 30 * 86400
        os.utime(src / 'fq_tags.json', (old, old))
        tags_learned.learn('fq', [('1', ['sky', 'mine'])])
        self.assertTrue(tags_learned.is_learned('fq'))
        self._install(src=src)
        self.assertEqual(self.folder / 'fq_tags.json', TagsDB.get_file('fq'))
        # the learned list moved onto the bundled one: only what it lacks, in the additions file; the whole list is gone from the
        # lists' names (written again from the learning's state when the extension is uninstalled)
        self.assertEqual({'mine': '1 posts (unknown)'}, _read_list(tags_learned.additions_path('fq')))
        self.assertFalse(tags_learned.list_path('fq').exists())
        self.assertEqual({'sky': 5000, 'skyline': 40, 'tree': 300, 'mine': 1}, self._all())

    def test_modules_with_a_source_never_learn_on_top_of_their_list(self) -> None:
        job = {'tagUpdate': {'entry': 'tags.py:read_tags'}, 'minSdkVersion': '1.1'}
        cases = {
            'engine': ({'engine': 'gelbooru_dapi'}, 'fq'),
            'job': ({**job}, 'fq'),
            'reviewed': ({'abbr': 'xb', 'site': 'https://xbooru.com/'}, 'xb'),
        }
        for name, (kw, module) in cases.items():
            with self.subTest(name):
                self._install(**kw)
                self.assertEqual(self.folder / f'{module}_tags.json', TagsDB.get_file(module))
                self.assertIsNone(TagsDB.bundled(module))
                self.assertFalse(tags_learned.may_learn(module))
                self.assertTrue(tags_learned.has_source_list(module))
                self.assertEqual((0, False), tags_learned.learn(module, [('1', ['sky', 'new'])]))
                self.assertFalse(tags_learned.list_path(module).exists())
                self.assertFalse(tags_learned.additions_path(module).exists())
                # the list in use is still the one the extension ships: its version, not the file's time (Tags A-Z)
                self.assertEqual((False, True, '1.0.2', 0), tuple(self._row(module)[k] for k in ('bundled', 'shipped', 'version', 'added')))
                res = server.m_tags_learn({'module': module})
                self.assertEqual((False, True, False), (res['mayLearn'], res['sourceList'], res['bundled']))
                shutil.rmtree(self.root / 'src')

    def test_a_shipped_list_with_a_source_reads_as_shipped_until_it_is_updated_here(self) -> None:
        """tags.status 'shipped': the list of an extension that has its own update is the shipped one until the user updates it;
        the update writes <data dir>/tags/<abbr>_tags.json, newer, and from then on the row gives that file's time"""
        self._install(tagUpdate={'entry': 'tags.py:read_tags'}, minSdkVersion='1.1')
        self.assertEqual({'bundled': False, 'shipped': True, 'version': '1.0.2', 'path': (self.folder / 'fq_tags.json').as_posix()},
                         {k: self._row()[k] for k in ('bundled', 'shipped', 'version', 'path')})
        updated = self.data / 'tags' / 'fq_tags.json'
        updated.parent.mkdir(parents=True, exist_ok=True)
        tags_updater.write_tag_list(updated, {'sky': (6000, 'general')})  # what update_tag_list writes
        later = (self.folder / 'fq_tags.json').stat().st_mtime + 60
        os.utime(updated, (later, later))
        TagsDB.locate_default_lists()
        self.assertEqual({'bundled': False, 'shipped': False, 'version': '', 'path': updated.as_posix()},
                         {k: self._row()[k] for k in ('bundled', 'shipped', 'version', 'path')})
        for row in server.m_tags_status({}):  # the lists in no extension (the repository's 2tags/) are never 'shipped'
            if row['module'] != 'fq':
                self.assertEqual((False, ''), (row['shipped'], row['version']), row['module'])

    def test_learning_off_learns_nothing_on_top(self) -> None:
        self._install()
        with mock.patch.dict(os.environ, {'LYNXX_TAGS_LEARN': '0'}):
            self.assertFalse(tags_learned.may_learn('fq'))
            self.assertEqual((0, False), tags_learned.learn('fq', [('1', ['new'])]))
            res = server.m_tags_learn({'module': 'fq'})
            self.assertEqual((False, False), (res['mayLearn'], res['sourceList']))  # the app says the learning is off
            self.assertFalse(tags_learned.refresh('fq'))
        self.assertFalse(tags_learned.list_path('fq').exists())
        self.assertFalse(tags_learned.additions_path('fq').exists())

    def test_learn_from_downloads_on_top(self) -> None:
        # "Học từ file đã tải" (Tags A-Z) on a bundled list
        self._install()
        keys = ('mayLearn', 'sourceList', 'indexed', 'newPosts', 'bundled', 'added', 'written')
        self.assertEqual((True, False, False, 0, True, 0, False), tuple(server.m_tags_learn({'module': 'fq'})[k] for k in keys))
        with DownloadIndex(create=True) as index:
            index.add_many([IndexRecord(module='fq', post_id='1', path='/x/fq_1.jpg', tags='sky downloaded_tag'),
                            IndexRecord(module='fq', post_id='2', path='/x/fq_2.jpg', tags='tree')])
        # after a download job main forwards it as tags.learned when written (app/main.js learnTagList): 'learned' stays False here
        res = server.m_tags_learn({'module': 'fq'})
        self.assertEqual((True, False, True, 2, True, 1, True, False), tuple(res[k] for k in (*keys, 'learned')))
        self.assertEqual({'downloaded_tag': '1 posts (unknown)'}, _read_list(tags_learned.additions_path('fq')))
        self.assertEqual(1, self._all()['downloaded_tag'])
        self.assertEqual((True, False, True, 0, True, 1, False), tuple(server.m_tags_learn({'module': 'fq'})[k] for k in keys))
        with DownloadIndex(create=True) as index:  # downloaded posts whose tags the bundled list has: nothing to tell the app
            index.add_many([IndexRecord(module='fq', post_id='3', path='/x/fq_3.jpg', tags='sky tree')])
        res = server.m_tags_learn({'module': 'fq'})
        self.assertEqual((True, False, True, 1, True, 1, False), tuple(res[k] for k in keys))

    def test_a_preview_whose_tags_the_bundled_list_has_rewrites_nothing(self) -> None:
        # rule34.us: most Preview pages bring only tags its 544k list has; the additions file, the loaded list and the app's chip
        # hints stay as they are
        self._install()
        self._learn([('1', ['sky', 'old_a'])])
        added = tags_learned.additions_path('fq')
        stamp = (added.stat().st_size, added.stat().st_mtime_ns)
        self.assertEqual(1, self._all()['old_a'])
        self.assertTrue(TagsDB.is_loaded('fq'))
        writes: list[pathlib.Path] = []
        write = tags_learned.write_tag_list
        with mock.patch.object(tags_learned, 'write_tag_list', lambda path, tags: writes.append(path) or write(path, tags)), \
                mock.patch.object(TagsDB, 'unload', wraps=TagsDB.unload) as unload:
            self.assertEqual([], self._learn([('2', ['sky', 'tree']), ('3', ['Skyline'])]))
            self.assertEqual((2, False), tags_learned.learn('fq', [('4', ['tree']), ('5', ['sky'])]))
            unload.assert_not_called()
        self.assertEqual([], writes)
        self.assertEqual(stamp, (added.stat().st_size, added.stat().st_mtime_ns))
        self.assertTrue(TagsDB.is_loaded('fq'))
        self.assertEqual(5, tags_learned.status('fq')['posts'])  # still counted (an uninstall writes the whole learned list)
        notes = self._learn([('6', ['sky', 'old_a'])])  # an addition's count went up: written, the app is told
        self.assertEqual(['tags.learned'], [m for m, _ in notes])
        self.assertEqual({'old_a': '2 posts (unknown)'}, _read_list(added))
        self.assertEqual({'ok': True}, server.m_ext_uninstall({'id': 'fakebooru'}))
        self.assertEqual({'sky': 4, 'tree': 2, 'skyline': 1, 'old_a': 2}, self._all())

    def test_a_lost_stamp_never_hides_the_bundled_list(self) -> None:
        # the learning's stamp (its sqlite state) can be lost: a quit while it wrote, a profile copied without its file times, the
        # state removed. Neither the additions nor a whole learned list from before the bundled one then takes the module's list
        later = time.time() + 60
        tags_learned.learn('fq', [('0', ['sky', 'old'])])  # learned before the extension shipped its list
        whole = tags_learned.list_path('fq')
        kept = whole.read_bytes()
        self._install()
        self._learn([('1', ['sky', 'new_a'])])
        added = tags_learned.additions_path('fq')
        self.assertEqual({'old': '1 posts (unknown)', 'new_a': '1 posts (unknown)'}, _read_list(added))
        whole.write_bytes(kept)  # left behind (a quit before it was removed), its stamp lost
        for f in (whole, added):
            os.utime(f, (later, later))
        shutil.rmtree(self.data / 'tags' / 'learned' / 'fq.sqlite', ignore_errors=True)
        (self.data / 'tags' / 'learned' / 'fq.sqlite').unlink(missing_ok=True)
        TagsDB.clear()
        TagsDB.locate_default_lists()  # a restart
        bundled = self.folder / 'fq_tags.json'
        self.assertEqual((bundled, added), (TagsDB.get_file('fq'), TagsDB.get_additions('fq')))
        self.assertEqual({'sky': 5000, 'skyline': 40, 'tree': 300, 'old': 1, 'new_a': 1}, self._all())
        self.assertEqual(['sky'], [r['tag'] for r in server.m_tags_autocomplete({'module': 'fq', 'prefix': 'sky'})][:1])
        self.assertEqual({'bundled': True, 'path': bundled.as_posix()}, {k: self._row()[k] for k in ('bundled', 'path')})
        self.assertTrue(tags_learned.may_learn('fq'))  # the learning goes on on top of it
        self.assertFalse(tags_learned.has_source_list('fq'))
        self.assertEqual('tags.learned', self._learn([('2', ['tree', 'new_b'])])[-1][0])
        self.assertEqual({'new_b': '1 posts (unknown)'}, _read_list(added))  # learned again from here (its state was lost)
        self.assertEqual(bundled, TagsDB.get_file('fq'))
        self.assertEqual(kept, whole.read_bytes())  # not the learning's any more: never removed or overwritten

    def test_a_write_cut_short_is_done_by_the_next_learn(self) -> None:
        # the counts are committed before the additions file is written: a quit in between (learning runs on a daemon thread) is
        # caught up by the next Preview, even one that brings nothing new
        self._install()
        self._learn([('1', ['sky', 'new_a'])])
        write = tags_learned.write_tag_list

        def quit_while_writing(path, tags):
            raise SystemExit('quit')

        with mock.patch.object(tags_learned, 'write_tag_list', quit_while_writing), self.assertRaises(SystemExit):
            tags_learned.learn('fq', [('2', ['new_a', 'new_b'])])
        self.assertEqual({'new_a': '1 posts (unknown)'}, _read_list(tags_learned.additions_path('fq')))
        TagsDB.clear()
        TagsDB.locate_default_lists()  # a restart
        self.assertEqual(TagsDB.get_file('fq'), self.folder / 'fq_tags.json')
        with mock.patch.object(tags_learned, 'write_tag_list', wraps=write) as written:
            self.assertEqual('tags.learned', self._learn([('3', ['sky'])])[-1][0])
            self.assertEqual(1, written.call_count)
            self.assertEqual([], self._learn([('4', ['tree'])]))  # done: nothing written again
            self.assertEqual(1, written.call_count)
        self.assertEqual({'new_a': '2 posts (unknown)', 'new_b': '1 posts (unknown)'}, _read_list(tags_learned.additions_path('fq')))
        self.assertEqual({'sky': 5000, 'skyline': 40, 'tree': 300, 'new_a': 2, 'new_b': 1}, self._all())

    def test_with_additions_merges_bytes_in_order(self) -> None:
        base = tagsdb._TagList.from_mapping({'b': (TagTypes.TAG_TYPE_GENERAL, 5), 'd': (TagTypes.TAG_TYPE_ARTIST, 7),
                                             'é': (TagTypes.TAG_TYPE_GENERAL, 2)})
        extra = tagsdb._TagList.from_mapping({'a': (TagTypes.TAG_TYPE_UNKNOWN, 1), 'b': (TagTypes.TAG_TYPE_UNKNOWN, 1),
                                              'c': (TagTypes.TAG_TYPE_UNKNOWN, 3), 'zz': (TagTypes.TAG_TYPE_UNKNOWN, 1),
                                              '日本': (TagTypes.TAG_TYPE_UNKNOWN, 4)})
        merged = tagsdb._with_additions(base, extra)
        self.assertEqual([('a', 1), ('b', 5), ('c', 3), ('d', 7), ('zz', 1), ('é', 2), ('日本', 4)],
                         [(merged.name(i), merged.info(i).posts_count) for i in range(len(merged))])
        self.assertEqual(TagTypes.TAG_TYPE_ARTIST, merged.info(merged.find('d')).tag_type)
        self.assertEqual({'a', 'c', 'zz', '日本'}, merged.added)
        self.assertEqual(range(1, 2), merged.prefix_range('b'))
        self.assertEqual([0, 1, 2, 3, 4, 5, 6], [merged.find(n) for n in ('a', 'b', 'c', 'd', 'zz', 'é', '日本')])
        self.assertIs(base, tagsdb._with_additions(base, tagsdb._TagList.from_mapping({'b': (TagTypes.TAG_TYPE_UNKNOWN, 1)})))
        self.assertIs(base, tagsdb._with_additions(base, tagsdb._EMPTY_LIST))


class RunningUpdateTests(_DataDirTest):
    """Tags A-Z shows a running update whoever started it (its button, the app's auto update, a new extension), also after a reload
    of the window: tags.sources says which modules run, with the count of tags read so far"""

    def test_a_running_update_is_listed_until_it_ends(self) -> None:
        shutil.copytree(_draft(self.root), self.data / 'extensions' / 'fakebooru')  # installed: fq has a tag source
        started, release, done = threading.Event(), threading.Event(), threading.Event()
        self.addCleanup(release.set)
        notes: list[tuple[str, dict]] = []
        running_at_done: list[dict] = []

        def slow_update(module: str, out_dir: pathlib.Path, *, progress=None, **_kw):
            progress(f'[{module}] 1234 tags...', 1234)
            started.set()
            release.wait(WAIT)
            res = tags_updater.UpdateResult(module, out_dir / f'{module}_tags.json')
            res.message = 'not written: no tags fetched'
            return res

        def notify(method: str, params: dict) -> None:
            notes.append((method, params))
            if method == 'tags.updateDone':
                running_at_done.append(server._running_tag_updates())
                done.set()

        def row() -> dict:
            return next(r for r in server.m_tags_sources({}) if r['module'] == 'fq')

        self.assertEqual((False, 0, ''), (row()['running'], row()['count'], row()['message']))
        out = self.root / 'out'
        with mock.patch.object(tags_updater, 'update_tag_list', slow_update), mock.patch.object(server, 'notify', notify):
            self.assertEqual('tags-fq', server.m_tags_update({'module': 'fq', 'outDir': str(out)})['job'])
            self.assertTrue(started.wait(WAIT))
            r = row()
            self.assertEqual((True, 1234, '[fq] 1234 tags...'), (r['running'], r['count'], r['message']))
            self.assertLessEqual(abs(time.time() - r['startedAt']), WAIT)
            with self.assertRaises(server.RpcError) as cm:  # one update of a module at a time
                server.m_tags_update({'module': 'fq', 'outDir': str(out)})
            self.assertEqual(-32004, cm.exception.code)
            release.set()
            self.assertTrue(done.wait(WAIT), notes)
        self.assertEqual([{}], running_at_done)  # gone before the app is told: its next tags.sources has it stopped
        self.assertEqual((False, 0, ''), (row()['running'], row()['count'], row()['message']))
        self.assertEqual(['tags.updateProgress', 'tags.updateDone'], [m for m, _ in notes])
        self.assertEqual({'job': 'tags-fq', 'module': 'fq', 'count': 1234, 'message': '[fq] 1234 tags...'}, notes[0][1])
        self.assertEqual(('fq', False), (notes[1][1]['module'], notes[1][1]['written']))


FIXTURES = pathlib.Path(__file__).resolve().parent / 'fixtures'


class E621ExportTests(_DataDirTest):
    """e621 lists its daily database exports on db_exports (db_export/ redirects there): the tag export is found there, its SHA-256
    checked, then read. A fake e621 serves a trimmed copy of the real page (lynxx/tests/fixtures/e621_db_exports.html)"""
    LISTED = '702bfb79c59542891a0b6ec3a345ac90864e9edd978cc6f1136c3841fca728b7'
    '''the checksum of tags.csv.gz in the saved page (not the one of the small export here)'''

    def setUp(self) -> None:
        super().setUp()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(['id', 'name', 'category', 'post_count'])
        w.writerows([i, name, c, n] for i, name, n, c in TAGS)
        self.export = gzip.compress(buf.getvalue().encode())
        self.page = (FIXTURES / 'e621_db_exports.html').read_text(encoding='utf-8')
        self.out = self.root / 'out'

    def _update(self, page: str) -> tuple[tags_updater.UpdateResult, FakeSession]:
        def route(url: str, _headers: dict) -> tuple[int, bytes, dict]:
            where = url_parse.urlsplit(url)
            where = (where.hostname, where.path)
            if where == ('e621.net', '/robots.txt'):
                return 200, b'User-agent: *\nDisallow: /posts.json\n', {'content-type': 'text/plain'}
            if where == ('e621.net', '/db_exports'):
                return 200, page.encode(), {'content-type': 'text/html; charset=utf-8'}
            if where == ('e621.net', '/db_export/'):
                return 301, b'', {'location': 'https://e621.net/db_exports'}
            if where == ('static1.e621.net', '/data/db_export/tags.csv.gz'):
                return 200, self.export, {'content-type': 'application/octet-stream'}
            return 404, b'<html>not found</html>', {'content-type': 'text/html'}

        session = FakeSession(route)
        return update_tag_list('en', self.out, delay=0, allow_local=True, session=session), session

    def test_the_tag_export_listed_on_db_exports_is_checked_and_read(self) -> None:
        self.assertIs(CURATED['en'], tag_source('en'))
        self.assertIn('db_exports', CURATED['en'].description)
        res, session = self._update(self.page.replace(self.LISTED, hashlib.sha256(self.export).hexdigest()))
        self.assertTrue(res.written, res.message)
        got = _read_list(res.path)
        self.assertEqual(EXPECTED, {k: int(v.split()[0]) for k, v in got.items()})
        self.assertEqual('40 posts (copyright)', got['tom_&_jerry'])
        self.assertEqual('300 posts (character)', got['hatsune_miku'])
        self.assertEqual(['https://e621.net/robots.txt', 'https://e621.net/db_exports',
                          'https://static1.e621.net/data/db_export/tags.csv.gz'], [u for u, _ in session.calls])

    def test_a_wrong_checksum_is_refused(self) -> None:
        with self.assertRaises(RuntimeError) as cm:
            self._update(self.page)  # the page's checksum is the one of the real 33.7 MB export
        self.assertIn('SHA-256', str(cm.exception))
        self.assertFalse((self.out / 'en_tags.json').exists())

    def test_the_link_and_its_checksum_are_read_from_the_same_row(self) -> None:
        link = tags_updater._e621_export_link
        base = 'https://e621.net/db_exports'
        self.assertEqual(('https://static1.e621.net/data/db_export/tags.csv.gz', self.LISTED), link(self.page, base))
        # a dated name, a relative link; a row without its checksum, or with two links, gives none
        dated = self.page.replace('db_export/tags.csv.gz', 'db_export/tags-2026-11-19.csv.gz')
        self.assertEqual(('https://static1.e621.net/data/db_export/tags-2026-11-19.csv.gz', self.LISTED), link(dated, base))
        rel = self.page.replace('https://static1.e621.net/data/db_export/tags.csv.gz', '/data/tags.csv.gz')
        self.assertEqual(('https://e621.net/data/tags.csv.gz', self.LISTED), link(rel, base))
        self.assertEqual(('https://static1.e621.net/data/db_export/tags.csv.gz', ''), link(self.page.replace(self.LISTED, 'x'), base))
        flat = self.page.replace('<div class="row">', '<div class="cells">')
        self.assertEqual(('https://static1.e621.net/data/db_export/tags.csv.gz', ''), link(flat, base))
        self.assertEqual(('', ''), link(self.page.replace('tags.csv.gz', 'tags.json'), base))

    def test_without_a_link_the_dated_exports_of_before_are_tried(self) -> None:
        session = FakeSession(lambda url, _h: (404, b'<html>not found</html>', {'content-type': 'text/html'}))
        with self.assertRaises(RuntimeError) as cm:  # no page, and the dated files are gone (the error the owner saw, now clearer)
            update_tag_list('en', self.out, delay=0, allow_local=True, session=session)
        self.assertEqual('no recent tag export found on e621.net (db_exports lists no tags.csv.gz)', str(cm.exception))
        today = datetime.date.today()
        self.assertEqual(['https://e621.net/robots.txt', 'https://e621.net/db_exports',
                          *(f'https://e621.net/db_export/tags-{today - datetime.timedelta(days=d)}.csv.gz' for d in range(4))],
                         [u for u, _ in session.calls])


JOB_FILE = '''# coding=UTF-8
import re
from html import unescape


def read_tags(job):
    \"\"\"the whole list in one answer (Shimmie's find_tags), plus what the checks of the core must clean up\"\"\"
    text = job.get_text(f'{job.site}api/danbooru/find_tags', accept='text/xml')
    job.log(f'{len(text)} bytes')
    for count, name in re.findall(r"counts='(\\d+)' name='([^']*)'", text):
        yield unescape(name), int(count), 'general'
    yield 'two  words', 5, 'Character'
    yield '', 3
    yield 'bad_count', '7'
'''
ROWS_JOB_FILE = '''# coding=UTF-8
import json
import pathlib


def read_tags(job):
    """a site that has no tag types: the rows the test wrote next to the job (rows.json), yielded as they are"""
    for row in json.loads((pathlib.Path(__file__).parent / 'rows.json').read_text(encoding='utf-8')):
        yield tuple(row)
'''
TOOK = ' taken from the list the extension ships'
'''the clause of the update's message for the rows without a type that took the type of the shipped list'''


def _write_lines(path: pathlib.Path, lines: dict[str, str]) -> pathlib.Path:
    """A list file in the bundled format: name -> the line's value ('30 posts (artist)', '30 posts'), sorted by name"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{\n' + ',\n'.join(f'{json.dumps(n, ensure_ascii=False)}: {json.dumps(v)}' for n, v in sorted(lines.items()))
                    + '\n}\n', encoding='utf-8')
    return path


@contextlib.contextmanager
def _list_reads():
    """The list files the updater reads meanwhile, one path for each read"""
    with mock.patch.object(tags_updater, '_read_list_lines', wraps=tags_updater._read_list_lines) as reads:
        paths: list[pathlib.Path] = []
        yield paths
        paths += [pathlib.Path(c.args[0]) for c in reads.call_args_list]


class ExtensionJobTests(_DataDirTest):
    """an extension ships its list and its own update (manifest tagUpdate): run only when asked, its requests through the core.
    A row its update gives no type takes the type of the shipped list (Lynxx 2.0.12)"""

    def _install(self, ext_id: str = 'jobsite', abbr: str = 'jq', job: str = JOB_FILE, engine: str = 'custom',
                 where: pathlib.Path | None = None) -> pathlib.Path:
        m = make_manifest(ext_id=ext_id, abbr=abbr, name=ext_id.title(), site=SITE, engine=engine, media=['image'])
        m.update({'tagList': f'{abbr}_tags.json', 'tagUpdate': {'entry': 'tags.py:read_tags'}, 'minSdkVersion': '1.1'})
        folder = (where or self.data / 'extensions') / ext_id
        folder.mkdir(parents=True)
        (folder / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
        (folder / 'downloader.py').write_text('# test\n', encoding='utf-8')
        (folder / 'tags.py').write_text(job, encoding='utf-8')
        return folder

    def _update(self, module: str, site=None, **kw):
        session = FakeSession(site or FakeTagSite())
        notes: list[str] = []
        res = update_tag_list(module, self.data / 'tags', session=session, allow_local=True, min_interval=0.0,
                              progress=lambda msg, _n: notes.append(msg), **kw)
        return res, session, notes

    def _rows_job(self, rows: list, shipped: dict[str, str] | None = None) -> pathlib.Path:
        """The extension 'jq' whose update yields these rows as they are (ROWS_JOB_FILE), installed when it is not yet; shipped:
        the list next to its manifest, name -> the line's value (None: the list is left as it is). -> that list's path"""
        folder = self.data / 'extensions' / 'jobsite'
        if not folder.is_dir():
            self._install(job=ROWS_JOB_FILE)
        (folder / 'rows.json').write_text(json.dumps(rows), encoding='utf-8')
        return _write_lines(folder / 'jq_tags.json', shipped) if shipped is not None else folder / 'jq_tags.json'

    def _written(self) -> dict[str, str]:
        return _read_list(self.data / 'tags' / 'jq_tags.json')

    def test_the_extensions_own_update_is_its_source_and_runs_only_when_asked(self) -> None:
        self._install()
        _install_engine = make_manifest(ext_id='fakebooru', abbr='fq', name='Fakebooru', site=SITE, engine='gelbooru_dapi', media=['image'])
        (self.data / 'extensions' / 'fakebooru').mkdir(parents=True)
        (self.data / 'extensions' / 'fakebooru' / 'manifest.json').write_text(json.dumps(_install_engine), encoding='utf-8')
        src = tag_source('jq')
        self.assertEqual((tags_updater.EXTENSION_JOB, 'tags.py:read_tags', True, True), (src.engine, src.job, src.on_demand, src.robots))
        self.assertTrue(src.as_dict()['onDemand'])
        self.assertFalse(tag_source('fq').on_demand)
        self.assertIn('jq', [s.module for s in tag_sources()])
        # the app's system schedule ('auto') leaves it out; naming the module runs it
        auto = tags_updater._modules_arg('auto')
        self.assertIn('fq', auto)
        self.assertNotIn('jq', auto)
        self.assertEqual([*auto, 'jq'], tags_updater._modules_arg('auto,jq'))
        res, session, notes = self._update('jq')
        self.assertTrue(res.written, res.message)
        self.assertEqual([f'{SITE}robots.txt', f'{SITE}api/danbooru/find_tags'], [c[0] for c in session.calls])
        # job.log and the skipped rows: one line each in the progress, the module in front once
        self.assertEqual(1, sum(1 for n in notes if n.startswith('[jq] ') and n.endswith(' bytes')), notes)
        self.assertEqual(1, sum(1 for n in notes if n.startswith('[jq] 2 of ') and 'skipped' in n), notes)
        self.assertFalse([n for n in notes if '[jq] [jq]' in n], notes)
        got = _read_list(self.data / 'tags' / 'jq_tags.json')
        self.assertEqual({**EXPECTED, 'two_words': 5}, {k: int(v.split()[0]) for k, v in got.items()})
        self.assertEqual('5 posts (character)', got['two_words'])

    def test_the_job_reaches_only_its_site_and_follows_robots_txt(self) -> None:
        self._install(job="def read_tags(job):\n    job.get_text('https://other.test/tags')\n    yield 'x', 1\n")
        with self.assertRaises(TagSourceError) as ctx:
            self._update('jq')
        self.assertIn('not the site of this tag list', str(ctx.exception))
        shutil.rmtree(self.data / 'extensions' / 'jobsite')
        self._install()
        with self.assertRaises(TagSourceError) as ctx:
            self._update('jq', site=FakeTagSite(robots='User-agent: *\nDisallow: /api/\n'))
        self.assertEqual('robots', ctx.exception.kind)
        self.assertFalse((self.data / 'tags' / 'jq_tags.json').exists())

    def test_a_job_that_gives_nothing_writes_nothing(self) -> None:
        self._install(job="def read_tags(job):\n    yield ('only', 'bad')\n")
        with self.assertRaises(RuntimeError) as ctx:
            self._update('jq')
        self.assertIn('gave no tag', str(ctx.exception))
        self.assertFalse((self.data / 'tags' / 'jq_tags.json').exists())

    def test_the_manifest_entry_is_checked(self) -> None:
        base = make_manifest(ext_id='jobsite', abbr='jq', name='Jobsite', site=SITE, engine='custom', media=['image'])
        ExtensionManifest.from_dict({**base, 'tagUpdate': {'entry': 'tags.py:read_tags'}}, None, 'user')
        for bad in ({'entry': 'sub/tags.py:read_tags'}, {'entry': '../tags.py:read_tags'}, {'entry': 'tags:read_tags'},
                    {'entry': 'tags.py:read_tags', 'every': 7}, {'entry': 5}, 'tags.py:read_tags'):
            with self.assertRaises(Exception, msg=str(bad)):
                ExtensionManifest.from_dict({**base, 'tagUpdate': bad}, None, 'user')
        # merge (Lynxx 2.0.10): the update reads every tag with minPosts or more posts, and only those
        entry = 'tags.py:read_tags'
        for n in (100, 2, 1_000_000):
            m = ExtensionManifest.from_dict({**base, 'tagUpdate': {'entry': entry, 'merge': {'minPosts': n}}}, None, 'user')
            self.assertEqual({'entry': entry, 'merge': {'minPosts': n}}, m.tagUpdate)
        for merge in (True, {}, {'minPosts': 1}, {'minPosts': 0}, {'minPosts': '100'}, {'minPosts': True}, {'minPosts': 10 ** 7},
                      {'minPosts': 100, 'x': 1}, {'minPosts': 100.0}, [100], 100):
            with self.subTest(merge=merge), self.assertRaisesRegex(ManifestError, r'^invalid tagUpdate\.merge \(expected'):
                ExtensionManifest.from_dict({**base, 'tagUpdate': {'entry': entry, 'merge': merge}}, None, 'user')
        with self.assertRaisesRegex(ManifestError, r'^invalid tagUpdate\.entry'):  # never a merge without its job
            ExtensionManifest.from_dict({**base, 'tagUpdate': {'merge': {'minPosts': 100}}}, None, 'user')
        with self.assertRaisesRegex(ManifestError, r'^unknown tagUpdate field\(s\): every$'):
            ExtensionManifest.from_dict({**base, 'tagUpdate': {'entry': entry, 'merge': {'minPosts': 100}, 'every': 7}}, None, 'user')

    def test_the_forms_of_a_row_without_a_type(self) -> None:
        # (name, posts), or None / '' as the type: no type (tags_updater.NO_TYPE); 'unknown' stays, and is asked for like them
        for row in (('a', 1), ['a', 1], ('a', 1, None), ('a', 1, ''), ('a', 1, '  ')):
            self.assertEqual(('a', 1, tags_updater.NO_TYPE), tags_updater._job_tag(row), row)
        for kind in ('unknown', ' Unknown ', 'UNKNOWN'):
            self.assertEqual(('a', 1, 'unknown'), tags_updater._job_tag(('a', 1, kind)), kind)
        # a type the job gives: a name of the core as it is ('general' too), any other value 'general' as before
        for kind, written in (('general', 'general'), (' Artist', 'artist'), ('metadata', 'metadata'), ('tag', 'tag'),
                              ('studio', 'general'), (4, 'general'), (('artist',), 'general'), (False, 'general')):
            self.assertEqual(('a', 1, written), tags_updater._job_tag(('a', 1, kind)), kind)
        self.assertEqual(frozenset(('artist', 'character', 'copyright', 'metadata', 'species', 'lore', 'invalid1')),
                         tags_updater.SPECIFIC_TYPES)

    def test_rows_without_a_type_take_the_types_of_the_shipped_list(self) -> None:
        # a site with no tag types: its maintainer typed the shipped list by name (tools/type_tag_list.py). Before 2.0.12 the first
        # "Cập nhật" wrote every tag back as 'general'
        rows = [('some_artist', 30), ('hatsune_miku', 300, None), ('tom_&_jerry', 40, ''), ('meta  tag', 5, ' Unknown '),
                ('plain', 12), ('bare', 11), ('tag_word', 7), ('said_unknown', 6, 'unknown'), ('new_tag', 3), ('new_unknown', 4, 'unknown'),
                ('Mixed_Case', 2), ('other  form', 9)]
        shipped = self._rows_job(rows, {
            'some_artist': '25 posts (artist)', 'hatsune_miku': '290 posts (character)', 'tom_&_jerry': '38 posts (copyright)',
            'meta_tag': '5 posts (metadata)', 'plain': '12 posts (general)', 'bare': '11 posts', 'tag_word': '7 posts (tag)',
            'said_unknown': '6 posts (unknown)', 'mixed_case': '2 posts (artist)', 'other form': '9 posts (artist)',
            'gone_artist': '1 posts (artist)', 'odd_line': 'about 40 posts'})
        before = shipped.read_bytes()
        self.assertEqual(shipped, pathlib.Path(tag_source('jq').tag_list))
        res, session, _ = self._update('jq', reference=shipped)  # the list in use, as the app's button gives it
        self.assertTrue(res.written, res.message)
        self.assertEqual({
            # the four forms of a row without a type, the name as the core writes it ('_' for spaces)
            'some_artist': '30 posts (artist)', 'hatsune_miku': '300 posts (character)', 'tom_&_jerry': '40 posts (copyright)',
            'meta_tag': '5 posts (metadata)',
            # the shipped list says nothing specific of them ('general', no type, 'tag', 'unknown'): as before
            'plain': '12 posts (general)', 'bare': '11 posts (general)', 'tag_word': '7 posts (general)',
            'said_unknown': '6 posts (unknown)',
            # not in the shipped list: as before, 'general' ('unknown' for a row typed 'unknown')
            'new_tag': '3 posts (general)', 'new_unknown': '4 posts (unknown)',
            # names are compared as they are written to the list: another case or a space in the shipped line is another name
            'Mixed_Case': '2 posts (general)', 'other_form': '9 posts (general)',
        }, self._written())
        self.assertEqual(f'written 12 tags (was 12, 0 requests, 4 types{TOOK})', res.message)
        self.assertEqual(([], before), (session.calls, shipped.read_bytes()))  # the shipped list is only read
        self.assertNotIn('gone_artist', self._written())  # a replace: the list is the job's rows, the shipped list gives types only

    def test_a_type_the_job_gives_is_written_as_it_is(self) -> None:
        # the shipped list says 'artist' of each: only the row without a type takes it
        names = ('untyped', 'said_general', 'said_character', 'said_tag', 'odd_type', 'number_type')
        rows = [('untyped', 30), ('said_general', 31, 'general'), ('said_character', 32, 'Character'), ('said_tag', 33, 'tag'),
                ('odd_type', 34, 'studio'), ('number_type', 35, 1)]
        self._rows_job(rows, dict.fromkeys(names, '20 posts (artist)'))
        res, _, _ = self._update('jq')
        self.assertTrue(res.written, res.message)
        self.assertEqual({'untyped': '30 posts (artist)', 'said_general': '31 posts (general)', 'said_character': '32 posts (character)',
                          'said_tag': '33 posts (tag)', 'odd_type': '34 posts (general)', 'number_type': '35 posts (general)'},
                         self._written())
        self.assertEqual(f'written 6 tags (was 0, 0 requests, 1 type{TOOK})', res.message)
        # the same tag twice: the row with more posts is the tag's, with its type or without one
        self._rows_job([('untyped', 30, 'general'), ('untyped', 40), ('said_general', 50), ('said_general', 31, 'general'),
                        ('said_character', 32), ('said_character', 60, 'general')])
        res, _, _ = self._update('jq')
        self.assertEqual({'untyped': '40 posts (artist)', 'said_general': '50 posts (artist)', 'said_character': '60 posts (general)'},
                         self._written())
        self.assertEqual(f'written 3 tags (was 6, 0 requests, 2 types{TOOK})', res.message)

    def test_without_a_shipped_list_a_row_without_a_type_is_general_as_before(self) -> None:
        rows = [('some_artist', 30), ('hatsune_miku', 300, None), ('sky', 700, 'unknown')]
        typed = {'some_artist': '30 posts (artist)', 'hatsune_miku': '300 posts (character)', 'sky': '700 posts (metadata)'}
        as_before = {'some_artist': '30 posts (general)', 'hatsune_miku': '300 posts (general)', 'sky': '700 posts (unknown)'}
        shipped = self._rows_job(rows, typed)
        out = self.data / 'tags' / 'jq_tags.json'
        res, _, _ = self._update('jq')
        self.assertEqual((typed, f'written 3 tags (was 0, 0 requests, 3 types{TOOK})'), (self._written(), res.message))

        def check(why: str) -> None:
            out.unlink()
            res, _, _ = self._update('jq')
            self.assertEqual((as_before, 'written 3 tags (was 0, 0 requests)'), (self._written(), res.message), why)
        shipped.write_bytes(b'{\n"some_artist": "30 posts (artist)",\n"sky\xff": "700 posts (metadata)"\n}\n')
        check('a list that cannot be read (not UTF-8)')
        shipped.unlink()
        shipped.mkdir()
        check('not a file')
        shipped.rmdir()
        check('the list the manifest names is not there')
        _write_lines(shipped, dict.fromkeys(typed, '1 posts'))
        check('a shipped list without types')
        # a manifest that names no list: the file next to it is not the extension's list
        _write_lines(shipped, typed)
        manifest = shipped.parent / 'manifest.json'
        m = json.loads(manifest.read_text(encoding='utf-8'))
        manifest.write_text(json.dumps({k: v for k, v in m.items() if k != 'tagList'}), encoding='utf-8')
        self.assertEqual('', tag_source('jq').tag_list)
        check('no tagList in the manifest')

    def test_the_shipped_list_is_read_once_and_only_for_rows_without_a_type(self) -> None:
        typed = {'some_artist': '30 posts (artist)', 'hatsune_miku': '300 posts (character)', 'sky': '700 posts (artist)'}
        shipped = self._rows_job([('some_artist', 30), ('hatsune_miku', 300, None), ('sky', 700, 'unknown')], typed)
        with _list_reads() as reads:
            res, _, _ = self._update('jq')
        self.assertEqual(([shipped], f'written 3 tags (was 0, 0 requests, 3 types{TOOK})'), (reads, res.message))
        # every row typed by the job: the list is not opened
        self._rows_job([('some_artist', 30, 'general'), ('hatsune_miku', 300, 'character'), ('sky', 700, 'general')])
        with _list_reads() as reads:
            res, _, _ = self._update('jq')
        self.assertEqual(([], 'written 3 tags (was 3, 0 requests)'), (reads, res.message))
        self.assertEqual('30 posts (general)', self._written()['some_artist'])
        # nor for a list that is not written (the sanity check: under half of the list in use)
        self._rows_job([('some_artist', 30)], {**typed, **{f't{i:d}': '1 posts' for i in range(10)}})
        with _list_reads() as reads:
            res, _, _ = self._update('jq', reference=shipped)
        self.assertEqual(([], False, 'not written: fetched 1 tags vs 13 existing (sanity check failed, use --force)'),
                         (reads, res.written, res.message))
        # never for a list read by an engine's reader: what the site's listing says is written, whatever a list next to the
        # manifest says (the fake site lists 'scenery' and 'sky' as general)
        m = make_manifest(ext_id='fakebooru', abbr='fq', name='Fakebooru', site=SITE, engine='gelbooru_dapi', media=['image'])
        folder = self.data / 'extensions' / 'fakebooru'
        folder.mkdir()
        (folder / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
        _write_lines(folder / m['tagList'], {'scenery': '900 posts (artist)', 'sky': '700 posts (character)'})
        with _list_reads() as reads:
            res, _, _ = self._update('fq')
        self.assertTrue(res.written, res.message)
        got = _read_list(self.data / 'tags' / 'fq_tags.json')
        self.assertEqual(([], '900 posts (general)', '700 posts (general)', '30 posts (artist)'),
                         (reads, got['scenery'], got['sky'], got['some_artist']))
        self.assertNotIn(TOOK, res.message)

    def test_the_generator_never_runs_a_drafts_job_but_checks_and_packs_it(self) -> None:
        from lynxx.extgen.sandbox import _policy_checks
        marker = self.root / 'ran'
        draft = self._install(ext_id='fakebooru', abbr='fq', where=self.root / 'drafts',
                              job=f"import requests\n\n\ndef read_tags(job):\n    open({str(marker)!r}, 'w').close()\n    yield 'x', 1\n")
        res = bundle_tag_list(draft, session=FakeSession(FakeTagSite()), allow_local=True, min_interval=0.0)
        self.assertEqual('later', res['status'], res)
        self.assertIn('only when you ask', res['message'])
        self.assertFalse(marker.exists())
        (draft / 'fq_tags.json').write_text('{\n"scenery": "900 posts (general)",\n"sky": "700 posts (general)"\n}\n', encoding='utf-8')
        res = bundle_tag_list(draft, session=FakeSession(FakeTagSite()), allow_local=True, min_interval=0.0)
        self.assertEqual(('bundled', 2), (res['status'], res['count']), res)
        m = ExtensionManifest.load(draft / 'manifest.json', 'user')
        bad = [c for c in _policy_checks(draft, m) if not c['ok']]
        self.assertTrue(any(c['detail'].startswith('tags.py line 1') for c in bad), bad)
        with zipfile.ZipFile(pack(draft, self.root / 'fakebooru.zip')['path']) as zf:
            self.assertTrue({'fakebooru/tags.py', 'fakebooru/fq_tags.json'} <= set(zf.namelist()))
        self.assertFalse(marker.exists())

    def test_the_generator_needs_the_list_a_merge_job_goes_into(self) -> None:
        # a job that reads only the most used tags (tagUpdate.merge) never makes a whole list: the app could never give the draft
        # its first list with it (update_tag_list refuses a merge with no list to go into), so the draft must ship one
        draft = self._install(ext_id='fakebooru', abbr='fq', where=self.root / 'drafts', job=MERGE_JOB_FILE)
        m = json.loads((draft / 'manifest.json').read_text(encoding='utf-8'))
        (draft / 'manifest.json').write_text(json.dumps({**m, 'tagUpdate': MERGE}), encoding='utf-8')
        site = PopularSite({'sky': 6000})
        res = bundle_tag_list(draft, session=FakeSession(site), allow_local=True, min_interval=0.0)
        self.assertEqual(('failed', '', 0), (res['status'], res['file'], res['requests']), res)
        self.assertEqual("tags.py:read_tags reads only the tags with 100+ posts and merges them into the list the extension ships "
                         "(tagUpdate.merge): put fq_tags.json next to manifest.json, or remove 'merge' if the job reads the whole "
                         "list", res['message'])
        self.assertEqual([], site.asked)
        (draft / 'fq_tags.json').write_text('{\n"sky": "5000 posts"\n}\n', encoding='utf-8')
        res = bundle_tag_list(draft, session=FakeSession(site), allow_local=True, min_interval=0.0)
        self.assertEqual(('bundled', 1), (res['status'], res['count']), res)


MERGE_JOB_FILE = '''# coding=UTF-8


def read_tags(job):
    """every tag with job.min_posts or more posts, as a site with a list by popularity answers it"""
    page = job.get_json(f'{job.site}popular.json?min={job.min_posts:d}') or {}
    for name, posts, kind in page.get('tags', ()):
        yield name, posts, kind
    if page.get('cut'):
        raise RuntimeError('the page is cut short')
'''
MERGE = {'entry': 'tags.py:read_tags', 'merge': {'minPosts': 100}}


class PopularSite:
    """A site that lists its tags by popularity (MERGE_JOB_FILE): it answers its tags whatever 'min' asks (the core drops those
    under it) and records each 'min' asked"""

    def __init__(self, tags: dict) -> None:
        self.tags = tags
        '''name -> posts, or (posts, type); 'general' when no type is given'''
        self.asked: list[int] = []
        self.status = 200
        self.cut = False

    def __call__(self, url: str, _headers: dict) -> tuple[int, bytes, dict]:
        p = url_parse.urlsplit(url)
        if p.path == '/robots.txt':
            return 200, b'User-agent: *\nAllow: /\n', {'content-type': 'text/plain'}
        if p.path != '/popular.json':
            return 404, b'<html>not found</html>', {'content-type': 'text/html'}
        self.asked.append(int(url_parse.parse_qs(p.query)['min'][0]))
        if self.status != 200:
            return self.status, b'', {'content-type': 'text/html'}
        rows = [[name, *(v if isinstance(v, tuple) else (v, 'general'))] for name, v in self.tags.items()]
        return _json({'tags': rows, 'cut': self.cut})


class MergeUpdateTests(_DataDirTest):
    """an extension whose own update reads only the most used tags (manifest tagUpdate.merge, Paheal from 1.0.3): the core merges
    the read into the list in use (the tags read take their new counts, new tags are added, every other tag is kept) instead of
    replacing it; the list the extension ships is never written"""
    SHIPPED = ('"hero": "900 posts (character)"', '"old": "150 posts"', '"rare": "7 posts"', '"sky": "5000 posts"',
               '"skyline": "40 posts"', '"tree": "300 posts"')
    '''lines without a type, as 2tags/rp_tags.json has them, and one with a type'''

    def setUp(self) -> None:
        super().setUp()
        self.folder = self._extension()
        self.site = PopularSite({'sky': 6000, 'tree': 310, 'brand_new': 150, 'hero': 950, 'low': 50})

    def _extension(self, tag_list: bool = True) -> pathlib.Path:
        m = make_manifest(ext_id='mergesite', abbr='mq', name='Mergesite', site=SITE, engine='custom', media=['image'])
        m.update({'tagUpdate': MERGE, 'minSdkVersion': '1.1', **({'tagList': 'mq_tags.json'} if tag_list else {})})
        folder = self.data / 'extensions' / 'mergesite'
        folder.mkdir(parents=True)
        (folder / 'manifest.json').write_text(json.dumps(m), encoding='utf-8')
        (folder / 'downloader.py').write_text('# test\n', encoding='utf-8')
        (folder / 'tags.py').write_text(MERGE_JOB_FILE, encoding='utf-8')
        if tag_list:
            self._ship(folder, self.SHIPPED)
        TagsDB.locate_default_lists(reload=('mq',))
        return folder

    @staticmethod
    def _ship(folder: pathlib.Path, lines, age: float = 60.0) -> pathlib.Path:
        """The extension's list; its file time 'age' seconds back: an update writes a newer file (the newest list wins, and of two
        lists with the same time the first one found, the extension's)"""
        path = folder / 'mq_tags.json'
        path.write_text('{\n' + ',\n'.join(lines) + '\n}\n', encoding='utf-8')
        when = time.time() - age
        os.utime(path, (when, when))
        TagsDB.locate_default_lists(reload=('mq',))
        return path

    def _update(self, **kw) -> tuple[tags_updater.UpdateResult, FakeSession, list[str]]:
        """update_tag_list as the app's button runs it: the list in use is the reference"""
        notes: list[str] = []
        kw.setdefault('reference', TagsDB.get_file('mq'))
        session = FakeSession(self.site)
        res = update_tag_list('mq', self.data / 'tags', session=session, allow_local=True, delay=0, min_interval=0.0,
                              progress=lambda msg, _n: notes.append(msg), **kw)
        return res, session, notes

    def _written(self) -> dict[str, str]:
        return _read_list(self.data / 'tags' / 'mq_tags.json')

    def test_a_merge_source_says_so_and_runs_only_when_asked(self) -> None:
        src = tag_source('mq')
        self.assertEqual((tags_updater.EXTENSION_JOB, True, True, 100), (src.engine, src.on_demand, src.merge, src.merge_min_posts))
        self.assertEqual(self.folder / 'mq_tags.json', pathlib.Path(src.tag_list))
        self.assertEqual((True, 100), (src.as_dict()['merge'], src.as_dict()['mergeMinPosts']))
        self.assertTrue(src.description.endswith(': the tags with 100+ posts, merged into the list in use'), src.description)
        self.assertNotIn('mq', tags_updater._modules_arg('auto'))
        row = next(r for r in server.m_tags_sources({}) if r['module'] == 'mq')
        self.assertEqual((True, 100, True), (row['merge'], row['mergeMinPosts'], row['onDemand']))
        ExtensionJobTests._install(self)  # a job that reads the whole list: it replaces the list, as before
        plain = tag_source('jq')
        self.assertEqual((False, 0, False, 0),
                         (plain.merge, plain.merge_min_posts, plain.as_dict()['merge'], plain.as_dict()['mergeMinPosts']))
        self.assertEqual("the extension's own update (tags.py:read_tags), run only when asked", plain.description)

    def test_the_read_goes_into_the_list_in_use(self) -> None:
        shipped = self.folder / 'mq_tags.json'
        before = shipped.read_bytes()
        self.assertEqual(shipped, TagsDB.get_file('mq'))
        res, session, notes = self._update()
        self.assertTrue(res.written, res.message)
        self.assertEqual([100], self.site.asked)  # the app asks with min_posts 1: the core raises it to merge.minPosts
        self.assertEqual([f'{SITE}robots.txt', f'{SITE}popular.json?min=100'], [u for u, _ in session.calls])
        # 'low' (50) is under 100: the job claims nothing there, the core drops it. A line without a type keeps none; a type read as
        # 'general' (a job that cannot tell) never replaces the type the list has
        self.assertEqual({'sky': '6000 posts', 'skyline': '40 posts', 'tree': '310 posts', 'old': '150 posts', 'rare': '7 posts',
                          'hero': '950 posts (character)', 'brand_new': '150 posts (general)'}, self._written())
        self.assertEqual((7, 6), (res.count, res.previous))
        self.assertIn('written 7 tags (was 6, 2 requests, the tags with 100+ posts merged into the list in use: 4 read, 1 new, '
                      '3 updated, 1 no longer listed kept)', res.message)
        self.assertTrue(any('4 tags read, merging them into the list in use (6 tags)' in n for n in notes), notes)
        self.assertTrue(any('7 tags in the merged list, writing it' in n for n in notes), notes)
        self.assertEqual(before, shipped.read_bytes())  # the extension's list is never written
        TagsDB.locate_default_lists(reload=('mq',))
        self.assertEqual(self.data / 'tags' / 'mq_tags.json', TagsDB.get_file('mq'))
        hero, sky = TagsDB.find_tag('mq', 'hero'), TagsDB.find_tag('mq', 'sky')
        self.assertEqual((950, TagTypes.TAG_TYPE_CHARACTER, 6000, TagTypes.TAG_TYPE_UNKNOWN),
                         (hero.posts_count, hero.tag_type, sky.posts_count, sky.tag_type))
        self.assertEqual(7, TagsDB.find_tag('mq', 'rare').posts_count)

    def test_rows_without_a_type_ask_the_list_in_use_then_the_shipped_list(self) -> None:
        shipped = self._ship(self.folder, (
            '"explicit": "170 posts (artist)"', '"hero": "900 posts (character)"', '"painter": "400 posts (artist)"',
            '"series": "500 posts (copyright)"', '"sky": "5000 posts"', '"studio": "200 posts (artist)"',
            '"tree": "300 posts (general)"', '"word": "250 posts (tag)"'))
        # the list in use is another one than the shipped list (the command line's --out, a list of before the types)
        in_use = _write_lines(self.root / 'in_use' / 'mq_tags.json', {
            'explicit': '165 posts', 'hero': '890 posts (artist)', 'painter': '390 posts', 'rare': '7 posts',
            'series': '490 posts (general)', 'sky': '4900 posts', 'tree': '290 posts (general)', 'word': '240 posts (unknown)'})
        self.site.tags = {'hero': (950, None), 'painter': (410, 'unknown'), 'series': (510, None), 'sky': (6000, None),
                          'tree': (310, None), 'studio': (210, None), 'word': (260, None), 'brand_new': (150, None),
                          'brand_unknown': (160, 'unknown'), 'explicit': (170, 'general')}
        before = shipped.read_bytes()
        with _list_reads() as reads:
            res, _, _ = self._update(reference=in_use)
        self.assertTrue(res.written, res.message)
        self.assertEqual({
            'hero': '950 posts (artist)',  # the list in use first, as before: its type stays whatever the shipped list says
            'painter': '410 posts (artist)', 'series': '510 posts (copyright)',  # no type / 'general' there: the shipped list's
            'studio': '210 posts (artist)',  # a new tag the shipped list types
            # neither list says more: as before (a line without a type keeps none; 'tag' in the shipped list is general)
            'sky': '6000 posts', 'tree': '310 posts (general)', 'word': '260 posts (unknown)',
            'brand_new': '150 posts (general)', 'brand_unknown': '160 posts (unknown)',  # new tags no list knows
            'explicit': '170 posts',  # typed 'general' by the job: the line of the list in use as before, the shipped list not asked
            'rare': '7 posts'}, self._written())
        self.assertEqual('written 11 tags (was 8, 2 requests, the tags with 100+ posts merged into the list in use: 10 read, 3 new, '
                         f'7 updated, 0 no longer listed kept, 3 types{TOOK})', res.message)
        self.assertEqual((sorted((in_use, shipped)), before), (sorted(reads), shipped.read_bytes()))  # each list read once
        # the shipped list is the list in use (the first update after an install): every type comes from it as before, the file
        # is read once and the message says nothing more
        (self.data / 'tags' / 'mq_tags.json').unlink()
        with _list_reads() as reads:
            res, _, _ = self._update(reference=shipped)
        self.assertTrue(res.written, res.message)
        self.assertEqual({'hero': '950 posts (character)', 'painter': '410 posts (artist)', 'series': '510 posts (copyright)',
                          'studio': '210 posts (artist)', 'sky': '6000 posts', 'tree': '310 posts (general)', 'word': '260 posts (tag)',
                          'brand_new': '150 posts (general)', 'brand_unknown': '160 posts (unknown)', 'explicit': '170 posts (artist)'},
                         self._written())
        self.assertEqual('written 10 tags (was 8, 2 requests, the tags with 100+ posts merged into the list in use: 10 read, 2 new, '
                         '7 updated, 0 no longer listed kept)', res.message)  # 'explicit': the line it had
        self.assertEqual([shipped], reads)

    def test_min_posts_raises_the_floor_only(self) -> None:
        res, _, _ = self._update(min_posts=200)
        self.assertTrue(res.written, res.message)
        self.assertEqual([200], self.site.asked)
        self.assertIn('the tags with 200+ posts merged into the list in use: 3 read, 0 new, 3 updated, 0 no longer listed kept',
                      res.message)
        got = self._written()
        self.assertEqual(('7 posts', '40 posts', '150 posts'), (got['rare'], got['skyline'], got['old']))  # never a filter of the list
        self.assertNotIn('brand_new', got)

    def test_the_next_merge_starts_from_the_last_one(self) -> None:
        self._update()
        TagsDB.locate_default_lists(reload=('mq',))
        merged = self.data / 'tags' / 'mq_tags.json'
        self.assertEqual(merged, TagsDB.get_file('mq'))
        self.site.tags = {'sky': 6100, 'tree': 320, 'hero': 950}  # hero: the count of the last merge
        res, _, _ = self._update()
        self.assertTrue(res.written, res.message)
        self.assertEqual((7, 7), (res.count, res.previous))
        self.assertIn('3 read, 0 new, 2 updated, 2 no longer listed kept', res.message)
        got = self._written()
        self.assertEqual(('150 posts (general)', '320 posts', '150 posts'), (got['brand_new'], got['tree'], got['old']))

    def test_a_newer_shipped_list_is_the_base_again(self) -> None:
        self._update()
        TagsDB.locate_default_lists(reload=('mq',))
        merged = self.data / 'tags' / 'mq_tags.json'
        self.assertEqual(merged, TagsDB.get_file('mq'))
        # an update of the extension ships another list: the base again, whatever its file time. Installed from a zip, the install's
        # (later than the merge); from the catalog bundled with an app built before the merge, the build's (earlier)
        for age in (-60.0, 3600.0):
            newer = self._ship(self.folder, ('"fresh": "120 posts"', '"hero": "900 posts (character)"', '"sky": "5500 posts"',
                                             '"tree": "305 posts"'), age=age)
            self.assertEqual(newer, TagsDB.get_file('mq'), age)
        self.site.tags = {'sky': 6200, 'tree': 330, 'hero': 970}
        res, _, _ = self._update()
        self.assertTrue(res.written, res.message)
        self.assertEqual({'fresh': '120 posts', 'hero': '970 posts (character)', 'sky': '6200 posts', 'tree': '330 posts'},
                         self._written())  # 'brand_new' was only in the list merged before

    def test_the_merged_list_stays_while_the_extension_ships_the_list_it_went_into(self) -> None:
        # the same list installed again (a reinstall from a zip, a version that ships it unchanged) gets the install's file time,
        # later than the merge: the merged list stays the list in use, and so after the next merge (it keeps the list it came from)
        merged = self.data / 'tags' / 'mq_tags.json'
        self.assertTrue(self._update()[0].written)
        for i in (1, 2):
            self._ship(self.folder, self.SHIPPED, age=-60.0 * i)
            self.assertEqual(merged, TagsDB.get_file('mq'))
            self.site.tags = {'sky': 6000 + i, 'tree': 310 + i, 'hero': 950 + i}
            res, _, _ = self._update()
            self.assertTrue(res.written, res.message)
            self.assertIn('merged into the list in use: 3 read, 0 new, 3 updated, 2 no longer listed kept', res.message)
            got = self._written()
            self.assertEqual(('150 posts (general)', f'{6000 + i:d} posts'), (got['brand_new'], got['sky']))  # brand_new: of the merge
        TagsDB.locate_default_lists()
        self.assertEqual(merged, TagsDB.get_file('mq'))

    def test_a_read_that_misses_the_popular_tags_writes_nothing(self) -> None:
        out = self.data / 'tags' / 'mq_tags.json'
        ten = {f't{i}': 100 * (i + 1) for i in range(10)}
        self._ship(self.folder, [f'"{n}": "{c} posts"' for n, c in sorted(ten.items())])
        four = {'t0': 101, 't1': 201, 't2': 301, 't3': 401, 'other': 500}
        self.site.tags = four
        res, _, _ = self._update()
        self.assertEqual((False, ('not written: the read lists 4 of the 10 tags with 100+ posts of the list in use (5 tags read; sanity '
                                  'check failed, use --force)')), (res.written, res.message))
        self.assertFalse(out.exists())
        self.site.tags = {n.upper(): c + 1 for n, c in ten.items()}  # names in another form than the list's: never added
        res, _, _ = self._update()
        self.assertIn('not written: the read lists 0 of the 10 tags with 100+ posts', res.message)
        self.assertFalse(out.exists())
        self.site.tags = four
        res, _, _ = self._update(force=True)
        self.assertTrue(res.written, res.message)
        got = self._written()
        self.assertEqual(11, len(got))  # every tag of the list in use kept
        self.assertEqual(('101 posts', '1000 posts', '500 posts (general)'), (got['t0'], got['t9'], got['other']))
        out.unlink()
        self.site.tags = {'t0': 101, 't1': 201, 't2': 301, 't3': 401, 't4': 501}  # half of them: written
        self.assertTrue(self._update()[0].written)
        out.unlink()
        self._ship(self.folder, ['"a": "5 posts"', '"b": "99 posts"'])  # no tag at 100+ posts: nothing to compare
        self.site.tags = {'c': 150}
        res, _, _ = self._update()
        self.assertTrue(res.written, res.message)
        self.assertEqual({'a': '5 posts', 'b': '99 posts', 'c': '150 posts (general)'}, self._written())

    def test_failed_or_empty_reads_write_nothing(self) -> None:
        tags_dir = self.data / 'tags'
        before = TagsDB.get_file('mq')
        self.site.tags = {'low': 50, 'lower': 7}  # nothing at 100+ posts
        for force in (False, True):
            res, _, _ = self._update(force=force)
            self.assertEqual((False, 'not written: no tags fetched'), (res.written, res.message))
        self.site.tags = {'sky': 6000}
        self.site.status = 500
        with self.assertRaisesRegex(RuntimeError, 'HTTP 500'):
            self._update()
        self.site.status = 200
        self.site.cut = True  # the job raises after it gave tags
        with self.assertRaisesRegex(RuntimeError, 'cut short'):
            self._update()
        for path in (tags_dir / 'mq_tags.json', tags_dir / tags_updater.PARTIAL_DIR, tags_dir / tags_updater.META_DIR):
            self.assertFalse(path.exists(), path)
        TagsDB.locate_default_lists()
        self.assertEqual(before, TagsDB.get_file('mq'))

    def test_unreadable_lines_of_the_list_in_use_are_never_dropped(self) -> None:
        self._ship(self.folder, ('"sky": "5000 posts"', '"tree": "300 posts"', '"odd": "about 40 posts"'))
        res, _, _ = self._update()
        self.assertEqual((False, ('not written: 1 line of the list in use could not be read: the merged list would lose it (use '
                                  '--force)')), (res.written, res.message))
        self.assertFalse((self.data / 'tags' / 'mq_tags.json').exists())
        self.assertTrue(self._update(force=True)[0].written)
        self.assertNotIn('odd', self._written())

    def test_a_learned_list_is_never_a_base(self) -> None:
        os.environ['LYNXX_TAGS_LEARN'] = '1'  # restored by the patch
        shutil.rmtree(self.folder)
        self._extension(tag_list=False)  # a merge job, no list shipped: the module's list is learned from the posts seen
        tags_learned.learn('mq', [('1', ['sky', 'mine']), ('2', ['sky'])])
        learned = tags_learned.list_path('mq')
        self.assertEqual((learned, True), (TagsDB.get_file('mq'), tags_learned.is_learned('mq')))
        res, _, _ = self._update()
        self.assertTrue(res.written, res.message)
        self.assertIn(', the tags with 100+ posts only: no list in use to merge into', res.message)
        self.assertEqual(0, res.previous)  # nor what the read is measured against: its counts are not the site's
        self.assertEqual({'sky': '6000 posts (general)', 'tree': '310 posts (general)', 'brand_new': '150 posts (general)',
                          'hero': '950 posts (general)'}, self._written())
        TagsDB.locate_default_lists(reload=('mq',))
        tags_learned.refresh('mq')  # (server.m_tags_update does it after an update)
        merged = self.data / 'tags' / 'mq_tags.json'
        self.assertEqual((merged, merged), (TagsDB.get_file('mq'), TagsDB.learning_base('mq')))
        self.assertEqual({'mine': '1 posts (unknown)'}, _read_list(tags_learned.additions_path('mq')))
        self.assertEqual((1, 6000), (TagsDB.find_tag('mq', 'mine').posts_count, TagsDB.find_tag('mq', 'sky').posts_count))

    def test_a_merge_never_writes_the_popular_tags_alone_over_a_shipped_list(self) -> None:
        # 'lynxx-core tags --modules mq' on a machine where the extension is not installed: no list in use, the extension of the
        # catalog bundled with the core is the source, and the list it ships is the base
        catalog = self.root / 'catalog'
        catalog.mkdir()
        shutil.move(self.folder, catalog / 'mergesite')
        with mock.patch.dict(os.environ, {'LYNXX_CATALOG_DIR': catalog.as_posix()}):
            TagsDB.locate_default_lists()
            self.assertIsNone(TagsDB.get_file('mq'))
            res, _, _ = self._update(source=tag_source('mq'), reference=None)
            self.assertTrue(res.written, res.message)
            self.assertIn('the tags with 100+ posts merged into the list the extension ships: 4 read, 1 new', res.message)
            self.assertEqual(7, len(self._written()))
            (self.data / 'tags' / 'mq_tags.json').unlink()
            (catalog / 'mergesite' / 'mq_tags.json').unlink()  # the list it names is nowhere: the read alone would replace it
            res, _, _ = self._update(source=tag_source('mq'), reference=None)
            self.assertEqual((False, ('not written: no list to merge the tags with 100+ posts into (the extension names mq_tags.json, '
                                      'not found): they would be the whole list (4 tags read, use --force)')), (res.written, res.message))
            self.assertFalse((self.data / 'tags' / 'mq_tags.json').exists())
            res, _, _ = self._update(source=tag_source('mq'), reference=None, force=True)
            self.assertTrue(res.written, res.message)
            self.assertIn('the tags with 100+ posts only: no list in use to merge into', res.message)

    def test_a_list_the_update_did_not_write_never_hides_the_shipped_list(self) -> None:
        # a file at <data dir>/tags/mq_tags.json the update did not write, newer than the shipped list: a learned list whose stamp
        # was lost (a quit while it was written, a profile copied without its file times, the learning's state removed)
        stray = self.data / 'tags' / 'mq_tags.json'
        stray.parent.mkdir(parents=True)
        tags_updater.write_tag_list(stray, {'sky': (3, 'unknown'), 'mine': (1, 'unknown')})
        TagsDB.clear()
        TagsDB.locate_default_lists()
        shipped = self.folder / 'mq_tags.json'
        self.assertEqual(shipped, TagsDB.get_file('mq'))
        res, _, _ = self._update()  # its base: the shipped list, never the stray file
        self.assertTrue(res.written, res.message)
        got = self._written()
        self.assertEqual(('7 posts', '6000 posts'), (got['rare'], got['sky']))
        self.assertNotIn('mine', got)
        TagsDB.locate_default_lists()
        self.assertEqual(stray, TagsDB.get_file('mq'))  # what the update wrote, the merge of the shipped list: it replaces it
        later = time.time() + 60  # its time changed (a copy without file times): not known as the update's any more
        os.utime(stray, (later, later))
        TagsDB.locate_default_lists()
        self.assertEqual(shipped, TagsDB.get_file('mq'))

    def test_the_command_line_merges_into_the_list_in_use(self) -> None:
        out = self.root / 'lists'
        update = functools.partial(tags_updater.update_tag_list, session=FakeSession(self.site), allow_local=True, min_interval=0.0)
        stdout = io.StringIO()
        with mock.patch.object(tags_updater, 'update_tag_list', update), contextlib.redirect_stdout(stdout):
            self.assertEqual(0, tags_updater.main(['--modules', 'mq', '--out', str(out), '--delay', '0']))
        text = stdout.getvalue()
        self.assertIn("mq: updating (Mergesite: the extension's own update (tags.py:read_tags), run only when asked: the tags with "
                      "100+ posts, merged into the list in use)...", text)
        self.assertIn('merged into the list in use: 4 read, 1 new, 3 updated, 1 no longer listed kept', text)
        self.assertEqual(('7 posts', '6000 posts'), tuple(_read_list(out / 'mq_tags.json')[n] for n in ('rare', 'sky')))
        self.assertFalse((self.data / 'tags' / 'mq_tags.json').exists())


class MergeSourceLearningTests(_ShippedListTest):
    """the list of a module whose own update merges a partial read into it (TagSource.merge, Paheal from 1.0.3) is learned on top
    of like a list with no source: the shipped list before the first update, the merged list after it; each update takes over the
    learned tags it brings (the site's count instead of the one seen here)"""

    def setUp(self) -> None:
        super().setUp()
        self.site = PopularSite({'sky': 6000, 'tree': 310, 'new': 150})

    def _install_merge(self, version: str = '1.0.2') -> dict:
        src = self._source(self.BUNDLED, version, tagUpdate=MERGE, minSdkVersion='1.1')
        old = time.time() - 60  # an update writes a newer file (the newest list wins; equal times keep the shipped one)
        os.utime(src / 'fq_tags.json', (old, old))
        return self._install(src=src)

    def _update(self) -> dict:
        """The Tags A-Z button (tags.update) -> its tags.updateDone"""
        done, notes = threading.Event(), []

        def notify(method: str, params: dict) -> None:
            notes.append((method, params))
            if method == 'tags.updateDone':
                done.set()

        update = functools.partial(tags_updater.update_tag_list, session=FakeSession(self.site), allow_local=True, min_interval=0.0)
        with mock.patch.object(tags_updater, 'update_tag_list', update), mock.patch.object(server, 'notify', notify):
            server.m_tags_update({'module': 'fq', 'delay': 0})
            self.assertTrue(done.wait(WAIT), notes)
        return notes[-1][1]

    def test_a_merge_source_learns_on_top_of_its_list(self) -> None:
        self._install_merge()
        shipped = self.folder / 'fq_tags.json'
        self.assertTrue(tag_source('fq').merge)
        self.assertEqual((None, shipped, shipped), (TagsDB.bundled('fq'), TagsDB.get_file('fq'), TagsDB.learning_base('fq')))
        self.assertTrue(tags_learned.may_learn('fq'))
        self.assertFalse(tags_learned.has_source_list('fq'))
        self.assertEqual('tags.learned', self._learn([('1', ['sky', 'new'])])[-1][0])
        self.assertEqual({'new': '1 posts (unknown)'}, _read_list(tags_learned.additions_path('fq')))
        self.assertEqual({'sky': 5000, 'skyline': 40, 'tree': 300, 'new': 1}, self._all())
        self.assertEqual({'bundled': False, 'shipped': True, 'merged': False, 'version': '1.0.2', 'added': 1},
                         {k: self._row()[k] for k in ('bundled', 'shipped', 'merged', 'version', 'added')})
        res = server.m_tags_learn({'module': 'fq'})
        self.assertEqual((True, False, False, 1), (res['mayLearn'], res['sourceList'], res['bundled'], res['added']))

    def test_the_update_folds_the_learned_tags_it_brings(self) -> None:
        self._install_merge()
        self._learn([('1', ['sky', 'new']), ('2', ['rare_new'])])
        added = tags_learned.additions_path('fq')
        self.assertEqual({'new', 'rare_new'}, set(_read_list(added)))
        done = self._update()
        self.assertTrue(done['written'], done)
        self.assertIn('merged into the list in use', done['message'])
        merged = self.data / 'tags' / 'fq_tags.json'
        self.assertEqual((merged, merged), (TagsDB.get_file('fq'), TagsDB.learning_base('fq')))
        self.assertEqual({'rare_new': '1 posts (unknown)'}, _read_list(added))  # 'new' came with the merge: the site's count
        self.assertEqual(150, TagsDB.find_tag('fq', 'new').posts_count)
        self.assertEqual({'sky': 6000, 'skyline': 40, 'tree': 310, 'new': 150, 'rare_new': 1}, self._all())
        self.assertEqual({'bundled': False, 'shipped': False, 'merged': True, 'version': '', 'added': 1},
                         {k: self._row()[k] for k in ('bundled', 'shipped', 'merged', 'version', 'added')})
        self.assertTrue(next(r for r in server.m_tags_sources({}) if r['module'] == 'fq')['merge'])

    def test_the_status_says_no_merge_wrote_a_list_of_another_folder(self) -> None:
        # the app run from the source: the repository's catalog has no list next to the manifest (.gitignore), the list in use is
        # one of another folder (2tags/ of the repository; here LYNXX_TAGS_DIR). No update wrote it: Tags A-Z gives its file time
        # without calling it the time of the last update (tags.status merged)
        src = self._source(self.BUNDLED, '1.0.3', tagUpdate=MERGE, minSdkVersion='1.1')
        (src / 'fq_tags.json').unlink()
        self._install(src=src)
        other = self.root / 'lists'
        other.mkdir()
        tags_updater.write_tag_list(other / 'fq_tags.json', self.BUNDLED)
        os.environ['LYNXX_TAGS_DIR'] = other.as_posix()  # restored by the patch
        TagsDB.locate_default_lists()
        self.assertEqual((other / 'fq_tags.json').as_posix(), self._row()['path'])
        self.assertEqual({'bundled': False, 'shipped': False, 'merged': False, 'version': ''},
                         {k: self._row()[k] for k in ('bundled', 'shipped', 'merged', 'version')})

    def test_a_learn_while_the_update_writes_never_keeps_what_it_brought(self) -> None:
        # a Preview learns on a thread of its own: from the second merge on the list keeps its path, and a learn that lands between
        # the write and the reload of the list takes the new file's stamp while it looks the tags up in the list still loaded
        self._install_merge()
        self._learn([('1', ['sky', 'new', 'rare'])])
        self.site.tags = {'sky': 6000, 'tree': 310}  # 'new' is not one of the most used tags yet
        self.assertTrue(self._update()['written'])
        added = tags_learned.additions_path('fq')
        self.assertEqual({'new', 'rare'}, set(_read_list(added)))
        self.assertTrue(TagsDB.count('fq') and TagsDB.is_loaded('fq'))  # loaded (a search, the autocomplete)
        self.site.tags = {'sky': 6100, 'tree': 320, 'new': 150}  # now it is
        save = tags_updater._save_merged

        def learn_meanwhile(*args, **kw) -> None:
            save(*args, **kw)
            self.assertTrue(TagsDB.is_loaded('fq'))  # the list of the last merge: the update thread reloads it next
            tags_learned.learn('fq', [('2', ['other'])])

        with mock.patch.object(tags_updater, '_save_merged', learn_meanwhile):
            self.assertTrue(self._update()['written'])
        self.assertEqual({'other', 'rare'}, set(_read_list(added)))  # 'new' came with the merge
        self.assertEqual((2, 2, 150), (tags_learned.added_count('fq'), self._row()['added'], TagsDB.find_tag('fq', 'new').posts_count))

    def test_an_install_keeps_the_merged_list_unless_it_ships_another(self) -> None:
        # which list is in use never hangs on how the extension came: a zip gives the list it ships the install's file time, the
        # catalog bundled with the app the build's
        self._install_merge('1.0.3')
        self._learn([('1', ['sky', 'new', 'rare_new'])])
        self.assertTrue(self._update()['written'])
        merged = self.data / 'tags' / 'fq_tags.json'
        state = ('path', 'shipped', 'version', 'added')
        after_merge = {'path': merged.as_posix(), 'shipped': False, 'version': '', 'added': 1}
        self.assertEqual(after_merge, {k: self._row()[k] for k in state})
        # Paheal removed, then installed again from a zip: the same list, a file time later than the merge
        server.m_ext_uninstall({'id': 'fakebooru'})
        same = self.root / 'src' / '1.0.3' / 'fakebooru'
        later = time.time() + 60
        os.utime(same / 'fq_tags.json', (later, later))
        self._install(src=same)
        self.assertEqual(after_merge, {k: self._row()[k] for k in state})
        self.assertEqual({'sky': 6000, 'skyline': 40, 'tree': 310, 'new': 150, 'rare_new': 1}, self._all())
        # an update that ships another list, from a catalog built before the merge (an earlier file time): its list is the base
        newer = self._source({**self.BUNDLED, 'fresh': (120, 'general')}, '1.0.4', tagUpdate=MERGE, minSdkVersion='1.1')
        earlier = time.time() - 3600
        os.utime(newer / 'fq_tags.json', (earlier, earlier))
        self._install(src=newer)
        shipped = self.folder / 'fq_tags.json'
        self.assertEqual({'path': shipped.as_posix(), 'shipped': True, 'version': '1.0.4', 'added': 2},
                         {k: self._row()[k] for k in state})  # 'new' is a learned tag again, until the next update
        self.assertEqual((shipped, 1, 120), (TagsDB.learning_base('fq'), self._all()['new'], self._all()['fresh']))
        self.assertTrue(self._update()['written'])
        self.assertEqual({'sky': 6000, 'skyline': 40, 'tree': 310, 'new': 150, 'rare_new': 1, 'fresh': 120}, self._all())
        self.assertEqual(after_merge, {k: self._row()[k] for k in state})

    def test_learning_goes_on_after_the_update(self) -> None:
        self._install_merge()
        self.assertTrue(self._update()['written'])
        merged = self.data / 'tags' / 'fq_tags.json'
        before = merged.read_bytes()
        self.assertEqual('tags.learned', self._learn([('3', ['tree', 'another'])])[-1][0])
        self.assertEqual({'another': '1 posts (unknown)'}, _read_list(tags_learned.additions_path('fq')))
        self.assertEqual(1, self._all()['another'])
        self.assertEqual((merged, before), (TagsDB.get_file('fq'), merged.read_bytes()))  # never written by the learning
        self.assertEqual(1, self._row()['added'])

    def test_from_no_source_to_a_merge_source_keeps_the_additions(self) -> None:
        self._install()  # 1.0.2: no source, a bundled list
        self.assertIsNotNone(TagsDB.bundled('fq'))
        self._learn([('1', ['sky', 'new'])])
        self._install_merge('1.0.3')  # Paheal 1.0.2 -> 1.0.3
        self.assertIsNone(TagsDB.bundled('fq'))
        self.assertEqual(self.folder / 'fq_tags.json', TagsDB.learning_base('fq'))
        self.assertEqual({'new': '1 posts (unknown)'}, _read_list(tags_learned.additions_path('fq')))
        self.assertEqual(1, self._all()['new'])
        self.assertEqual({'bundled': False, 'shipped': True, 'version': '1.0.3', 'added': 1},
                         {k: self._row()[k] for k in ('bundled', 'shipped', 'version', 'added')})

    def test_the_learning_never_removes_a_list_the_update_wrote(self) -> None:
        # the whole learned list of before is removed once the learning adds to a base. For a merge source that path is also where
        # its update writes: an update that lands while the learning writes its additions (Preview runs on a thread of its own) is
        # never removed with it
        tags_learned.learn('fq', [('0', ['sky', 'old'])])  # learned before the extension shipped its list
        whole = tags_learned.list_path('fq')
        write = tags_learned.write_tag_list

        def merge_lands(path: pathlib.Path, tags: dict) -> None:
            write(path, tags)
            if path != whole:
                tags_updater.write_tag_list(whole, {'sky': (6000, 'general')})  # what update_tag_list writes there meanwhile

        with mock.patch.object(tags_learned, 'write_tag_list', merge_lands):
            self._install_merge()  # ext.install: the learned list moves onto the shipped one (tags_learned.refresh)
        self.assertEqual({'old': '1 posts (unknown)'}, _read_list(tags_learned.additions_path('fq')))
        self.assertEqual({'sky': '6000 posts (general)'}, _read_list(whole))

    def test_a_lost_stamp_never_hides_the_shipped_list(self) -> None:
        # as for a list with no source (BundledListLearningTests): a whole learned list left behind without its stamp is not taken
        # for the module's list, nor for the base of the next update
        tags_learned.learn('fq', [('0', ['sky', 'old'])])  # learned before the extension shipped its list
        whole = tags_learned.list_path('fq')
        kept = whole.read_bytes()
        self._install_merge()
        self._learn([('1', ['sky', 'new_a'])])
        added = tags_learned.additions_path('fq')
        self.assertEqual({'old', 'new_a'}, set(_read_list(added)))
        whole.write_bytes(kept)  # left behind (a quit before it was removed), its stamp lost
        later = time.time() + 60
        for f in (whole, added):
            os.utime(f, (later, later))
        (self.data / 'tags' / 'learned' / 'fq.sqlite').unlink()
        TagsDB.clear()
        TagsDB.locate_default_lists()  # a restart
        shipped = self.folder / 'fq_tags.json'
        self.assertEqual((shipped, shipped, added), (TagsDB.get_file('fq'), TagsDB.learning_base('fq'), TagsDB.get_additions('fq')))
        self.assertEqual(5000, self._all()['sky'])
        done = self._update()
        self.assertTrue(done['written'], done)
        got = _read_list(self.data / 'tags' / 'fq_tags.json')
        self.assertEqual(('40 posts (general)', '6000 posts (general)'), (got['skyline'], got['sky']))  # merged into the shipped list
        self.assertNotIn('old', got)


ROOT = pathlib.Path(__file__).resolve().parents[2]
PAHEAL = ROOT / 'extensions' / 'paheal'
PAHEAL_SITE = 'https://rule34.paheal.net/'
PAHEAL_FIXTURES = FIXTURES / 'paheal'
PAHEAL_SECTION = 'tags_popularity_mincount=20000.section.html'
'''the list's <section> of tags/popularity?mincount=20000, cut unchanged out of the recorded page (fixtures/paheal/SOURCES.json)'''
re_paheal_row = re.compile(r"<a href='/post/list/([^']*)/1' class='tag' title='View all posts tagged ([^']*)'>([^<]*)</a> \((\d+)\)")


def _paheal_url(mincount: int) -> str:
    return f'{PAHEAL_SITE}tags/popularity?mincount={mincount:d}'


def _paheal_page(section: str) -> str:
    """The page around the list as the site writes it: the menu and the other sections have links too, they are not tags"""
    return ("<!doctype html><html lang='en'><head><title>Tag List</title></head><body><nav><a href='/post/list'>Posts</a> "
            f"<a href='/tags/popularity?mincount=1'>Show All</a></nav>{section}<section id='unknownmain'><div class='blockbody'>"
            "<a href='/wiki/Terms_of_use'>Terms of use</a> (2007)</div></section></body></html>")


def _paheal_list(*rows: tuple[str, int], link: str = "class='tag'") -> str:
    """A list written by a test (never a fixture), in the site's markup: (name as on the page, post count)"""
    spans = ''.join(f"<span style='margin-right: 1em; white-space: nowrap;'><a href='/post/list/x/1' {link} "
                    f"title='View all posts tagged {name}'>{name}</a> ({count:d})</span> " for name, count in rows)
    return _paheal_page(f"<section id='unknownmain'><div class='blockbody'>Results grouped by log<sub>10</sub>(n)<br /><br />5<br />"
                        f"{spans}</div></section>")


class PahealTagJobTests(_DataDirTest):
    """Rule34 Paheal (rp) updates its list with its own update (extensions/paheal/tags.py, from 1.0.3): one page of the site, every
    tag with at least 100 posts sorted by count, through the core (robots.txt, its Crawl-delay of 3 s, no cookies), merged into the
    list in use (manifest tagUpdate.merge): the site cannot answer a page big enough to replace it. Pages of the site recorded on
    2026-11-20 (lynxx/tests/fixtures/paheal/, SOURCES.json holds their sha256); the pages a test writes (_paheal_list) are not.
    Rule34Hentai (rn) and Rule34.us (rs) keep no source: their shipped list, with the tags learned on top of it"""

    def setUp(self) -> None:
        super().setUp()
        self.manifest = ExtensionManifest.load(PAHEAL / 'manifest.json', 'catalog')
        self.source = tags_updater.source_for_manifest(self.manifest)
        self.section = (PAHEAL_FIXTURES / PAHEAL_SECTION).read_text(encoding='utf-8')

    def _session(self, pages: dict[str, str]) -> FakeSession:
        robots = (PAHEAL_FIXTURES / 'robots.txt').read_bytes()

        def route(url: str, _headers: dict) -> tuple[int, bytes, dict]:
            if url == f'{PAHEAL_SITE}robots.txt':
                return 200, robots, {'content-type': 'text/plain'}
            if url in pages:
                return 200, pages[url].encode(), {'content-type': 'text/html; charset=utf-8'}
            return 404, b'<html>not found</html>', {'content-type': 'text/html'}
        return FakeSession(route)

    def _read(self, pages: dict[str, str], min_posts: int = 1) -> tuple[list, FakeSession, PoliteClient, list[str]]:
        """The job run by the core (TagJob, PoliteClient) with this min_posts; the clock is faked"""
        session, clock, notes = self._session(pages), FakeClock(), []
        client = PoliteClient(self.source, delay=0.5, allow_local=True, session=session, sleep=clock.sleep, clock=clock.clock)
        client.notify = notes.append
        return list(tags_updater._iter_extension_job(self.source, client, '', min_posts)), session, client, notes

    def _update(self, pages: dict[str, str], **kw) -> tuple[tags_updater.UpdateResult, FakeClock, FakeSession]:
        """update_tag_list as the app runs it (tags.update), into <root>/tags; its waits on a fake clock.
        The list the extension ships is the test's (<root>/extension/rp_tags.json, there when a test writes it), never one a
        maintainer keeps next to the manifest (extensions/paheal/rp_tags.json, not in the repository): its types go to the rows
        the list in use does not type"""
        clock, session = FakeClock(), self._session(pages)
        source = dataclasses.replace(self.source, tag_list=str(self.root / 'extension' / 'rp_tags.json'))
        with mock.patch.object(tags_updater, 'PoliteClient', functools.partial(PoliteClient, sleep=clock.sleep, clock=clock.clock)):
            res = update_tag_list('rp', self.root / 'tags', source=source, delay=0.5, allow_local=True, session=session, **kw)
        return res, clock, session

    def _shipped(self) -> pathlib.Path:
        shipped = ROOT / '2tags' / 'rp_tags.json'
        if not shipped.is_file():
            self.skipTest('no 2tags/rp_tags.json (a source archive without the lists)')
        return shipped

    def test_fixtures_are_the_recorded_pages(self) -> None:
        files = json.loads((PAHEAL_FIXTURES / 'SOURCES.json').read_text(encoding='utf-8'))['files']
        self.assertEqual({'robots.txt', PAHEAL_SECTION}, set(files))
        for name, meta in files.items():
            data = (PAHEAL_FIXTURES / name).read_bytes()
            self.assertEqual((meta['bytes'], meta['sha256']), (len(data), hashlib.sha256(data).hexdigest()), name)
        # byte for byte on every system: git never turns their line ends into CRLF (a Windows checkout, the CI's windows-latest)
        rel = [(PAHEAL_FIXTURES / name).relative_to(ROOT).as_posix() for name in files]
        attrs = subprocess.run(('git', 'check-attr', 'text', '--', *rel), cwd=ROOT, capture_output=True, timeout=60)
        if attrs.returncode == 0:  # else not a git checkout (a source archive): its files are as they were packed
            self.assertEqual({f'{r}: text: unset' for r in rel}, set(attrs.stdout.decode('utf-8').splitlines()),
                             '.gitattributes must mark lynxx/tests/fixtures/** -text')
        cut = files[PAHEAL_SECTION]['excerpt']
        self.assertEqual(cut['end'] - cut['start'], files[PAHEAL_SECTION]['bytes'])
        self.assertTrue(self.section.startswith("<section id='unknownmain'>") and self.section.endswith('</section>'))
        self.assertNotIn('auth_token', self.section)

    def test_its_own_update_is_a_merge_source_rn_and_rs_have_none(self) -> None:
        self.assertEqual(('rp', '1.0.4', '1.1', 'rp_tags.json'),
                         (self.manifest.abbr, self.manifest.version, self.manifest.minSdkVersion, self.manifest.tagList))
        self.assertEqual({'entry': 'tags.py:read_tags', 'merge': {'minPosts': 100}}, self.manifest.tagUpdate)
        spec = importlib.util.spec_from_file_location('lynxx_test_paheal_tags', PAHEAL / 'tags.py')
        job = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(job)
        self.assertEqual(job.MIN_POSTS, self.manifest.tagUpdate['merge']['minPosts'])  # one page holds every tag down to it
        src = self.source
        self.assertEqual((tags_updater.EXTENSION_JOB, 'tags.py:read_tags', PAHEAL_SITE, True, True, 'none', True, 100),
                         (src.engine, src.job, src.site, src.on_demand, src.robots, src.key, src.merge, src.merge_min_posts))
        self.assertEqual(PAHEAL, pathlib.Path(src.folder))
        self.assertTrue(src.description.endswith(': the tags with 100+ posts, merged into the list in use'))
        # installed, as the app has it: a source the app lists, that its system schedule ('auto') never runs; named, it runs
        self.assertNotIn('rp', [s.module for s in tags_updater.tag_sources()])
        shutil.copytree(PAHEAL, self.data / 'extensions' / 'paheal', ignore=shutil.ignore_patterns('*_tags.json', '__pycache__'))
        installed = {s.module: s for s in tags_updater.tag_sources()}
        rp = installed['rp']
        self.assertEqual((tags_updater.EXTENSION_JOB, True, True), (rp.engine, rp.on_demand, rp.merge))
        self.assertNotIn('rp', tags_updater._modules_arg('auto'))
        self.assertEqual(['rp'], tags_updater._modules_arg('rp'))
        # no listing they may read (survey of 2026-11-20, docs/dev_log.md 7bc): Rule34Hentai answers a Cloudflare bot challenge on
        # every page but robots.txt (R5); Rule34.us has no tag listing with post counts, only an autocomplete of 10 tags a prefix.
        # Both keep the shipped list, with the tags learned on top
        for ext_id in ('rule34hentai', 'rule34us'):
            self.assertIsNone(tags_updater.source_for_manifest(ExtensionManifest.load(ROOT / 'extensions' / ext_id / 'manifest.json',
                                                                                      'catalog')), ext_id)

    def test_robots_txt_allows_the_listing_and_sets_the_pace(self) -> None:
        rules = RobotsRules.parse((PAHEAL_FIXTURES / 'robots.txt').read_text(encoding='utf-8'), tags_updater.ROBOTS_AGENT)
        self.assertEqual(3.0, rules.crawl_delay)
        for mincount in (100, 1000, 20000):
            self.assertTrue(rules.allows(_paheal_url(mincount)))
        self.assertFalse(rules.allows(f'{PAHEAL_SITE}tag_history/1'))  # strict: a path the site disallows stays disallowed
        self.assertFalse(rules.allows(f'{PAHEAL_SITE}comment/list'))

    def test_the_list_through_the_core(self) -> None:
        got, session, client, notes = self._read({_paheal_url(20000): _paheal_page(self.section)}, min_posts=20000)
        self.assertEqual([f'{PAHEAL_SITE}robots.txt', _paheal_url(20000)], [u for u, _ in session.calls])
        self.assertEqual((3.0, 2), (client.interval, client.used))  # robots.txt Crawl-delay; robots.txt and one page
        self.assertEqual('text/html', session.calls[1][1].get('Accept'))
        self.assertFalse([h for _, h in session.calls if 'Cookie' in h])
        rows = re_paheal_row.findall(self.section)
        self.assertEqual(98, len(rows))
        self.assertEqual(len(rows), self.section.count("class='tag'"))  # every tag link of the page has its count
        for href, title, text, _ in rows:  # the link text is the tag of the link's title and of its address
            self.assertEqual(title, text)
            self.assertEqual(url_parse.unquote(href), html.unescape(text))
        self.assertEqual(98, len(got))
        self.assertEqual(('porkyman', 493734, 'unknown'), got[0])  # 'Porkyman' on the page; the site has no tag types
        self.assertEqual(('raven', 20040, 'unknown'), got[-1])
        d = {name: posts for name, posts, _ in got}
        self.assertEqual(len(got), len(d))
        self.assertEqual((28562, 24078, 116819), (d["five_nights_at_freddy's"], d["jojo's_bizarre_adventure"], d['fate_(series)']))
        self.assertEqual({'unknown'}, {t for _, _, t in got})
        self.assertTrue(all(n == n.lower() and '&' not in n and ' ' not in n for n in d))
        self.assertEqual(sorted(d.values(), reverse=True), [p for _, p, _ in got])
        self.assertEqual(['tags with 20000 or more posts, one page sorted by post count', '98 tags, down to 20040 posts'], notes)

    def test_most_used_tags_only(self) -> None:
        # one page with every tag down to 100 posts: the app asks with min_posts 1, the core raises it to merge.minPosts
        for min_posts in (0, 1, 2, 100):
            got, session, _, _ = self._read({_paheal_url(100): _paheal_page(self.section)}, min_posts=min_posts)
            self.assertEqual(_paheal_url(100), session.calls[-1][0])
            self.assertEqual(98, len(got))
        got, session, _, _ = self._read({_paheal_url(250): _paheal_page(self.section)}, min_posts=250)
        self.assertEqual(_paheal_url(250), session.calls[-1][0])
        # a site that would ignore mincount: tags below min_posts are left out
        got, _, _, _ = self._read({_paheal_url(100000): _paheal_page(self.section)}, min_posts=100000)
        expected = [(text.lower(), int(n)) for _, _, text, n in re_paheal_row.findall(self.section) if int(n) >= 100000]
        self.assertEqual(16, len(expected))
        self.assertEqual(expected, [(name, posts) for name, posts, _ in got])
        # the core asks the job with merge.minPosts, whatever the job's own floor: a job that would go lower is never asked lower
        real = tags_updater._job_function

        def lower_floor(source: TagSource):
            func = real(source)
            func.__globals__['MIN_POSTS'] = 2
            return func
        with mock.patch.object(tags_updater, '_job_function', lower_floor):
            _, _, session = self._update({_paheal_url(100): _paheal_page(self.section)}, min_posts=1, reference=self.root / 'none.json')
        self.assertEqual(_paheal_url(100), session.calls[-1][0])

    def test_names_unescaped_once_lowercase_first_of_the_same_name(self) -> None:
        page = _paheal_list(('Foo', 300), ('foo', 200), ('AT&amp;T', 150), ('Pok&eacute;mon_X', 120), ('&amp;amp;kept', 110),
                            ('Liara_T&#039;Soni', 100), ('two words', 100))
        got, _, _, notes = self._read({_paheal_url(100): page})
        self.assertEqual([('foo', 300, 'unknown'), ('at&t', 150, 'unknown'), ('pokémon_x', 120, 'unknown'),
                          ('&amp;kept', 110, 'unknown'), ("liara_t'soni", 100, 'unknown'), ('two_words', 100, 'unknown')], got)
        self.assertTrue(any('1 names that differ only by case kept once' in n for n in notes), notes)
        # a tag link with another class is read like the others (never skipped)
        got, _, _, _ = self._read({_paheal_url(100): _paheal_list(('new_tag', 120), link="class='tag new' rel='tag'")})
        self.assertEqual([('new_tag', 120, 'unknown')], got)

    def test_a_changed_page_writes_no_list(self) -> None:
        out = self.root / 'tags'
        cut = _paheal_page(self.section)
        cut = cut[:cut.index('</section>')]
        no_count = _paheal_list(('sky', 300)).replace('</div></section>', "<a href='/tags/map'>more</a></div></section>", 1)
        for page, error in ((cut, 'cut short'), (no_count, '1 of 2 links of the tag list have no post count'),
                            (_paheal_page(''), 'not the tag list by popularity'), (_paheal_list(), 'has no tag'),
                            (_paheal_list(('a', 100), ('b', 200)), 'not sorted'),
                            ((PAHEAL_FIXTURES / 'robots.txt').read_text(encoding='utf-8'), 'not the tag list by popularity')):
            with self.subTest(error=error), self.assertRaisesRegex(RuntimeError, error):
                self._update({_paheal_url(100): page})
            self.assertFalse((out / 'rp_tags.json').exists())

    def test_the_update_merges_into_a_shipped_list(self) -> None:
        rows = [(text.lower(), int(n)) for _, _, text, n in re_paheal_row.findall(self.section)]
        base = self.root / 'base' / 'rp_tags.json'  # the list in use: older counts, a tag under the page's floor, one it left
        base.parent.mkdir()
        lines = [*(f'{json.dumps(html.unescape(name), ensure_ascii=False)}: "{n - 1000:d} posts"' for name, n in rows),
                 '"rare_tag": "7 posts"', '"gone_tag": "30000 posts"']
        base.write_text('{\n' + ',\n'.join(sorted(lines)) + '\n}\n', encoding='utf-8')
        res, clock, _ = self._update({_paheal_url(20000): _paheal_page(self.section)}, min_posts=20000, reference=base)
        self.assertTrue(res.written, res.message)
        self.assertEqual((2, [3.0]), (res.requests, clock.sleeps))  # robots.txt, then the page 3 s later (its Crawl-delay)
        self.assertIn('the tags with 20000+ posts merged into the list in use: 98 read, 0 new, 98 updated, 1 no longer listed kept',
                      res.message)
        written = _read_list(self.root / 'tags' / 'rp_tags.json')
        self.assertEqual((100, 100), (res.count, len(written)))
        self.assertEqual(('493734 posts', '7 posts', '30000 posts'), (written['porkyman'], written['rare_tag'], written['gone_tag']))
        self.assertEqual('28562 posts', written["five_nights_at_freddy's"])  # no type: the shipped list has none, the site neither

    def test_a_typed_shipped_list_types_what_the_list_in_use_does_not(self) -> None:
        # the site has no tag types: every row of the job is 'unknown', a row without a type. The list in use has no types (the
        # list shipped before 1.0.4), the extension now ships one typed by name
        rows = [(html.unescape(text).lower(), int(n)) for _, _, text, n in re_paheal_row.findall(self.section)]
        base = _write_lines(self.root / 'base' / 'rp_tags.json', {**{name: f'{n - 1000:d} posts' for name, n in rows},
                                                                  'rare_tag': '7 posts'})
        shipped = _write_lines(self.root / 'extension' / 'rp_tags.json', {
            'porkyman': '490000 posts (copyright)', 'raven': '20001 posts (character)', 'rare_tag': '7 posts (artist)'})
        page = {_paheal_url(20000): _paheal_page(self.section)}
        res, _, _ = self._update(page, min_posts=20000, reference=base)
        self.assertTrue(res.written, res.message)
        self.assertIn(f'merged into the list in use: 98 read, 0 new, 98 updated, 0 no longer listed kept, 2 types{TOOK})',
                      res.message)
        written = _read_list(self.root / 'tags' / 'rp_tags.json')
        self.assertEqual(('493734 posts (copyright)', '20040 posts (character)', '28562 posts', '7 posts'),
                         (written['porkyman'], written['raven'], written["five_nights_at_freddy's"], written['rare_tag']))
        self.assertEqual(97, sum(1 for v in written.values() if v.endswith(' posts')))  # the other 96 and 'rare_tag': no type
        # the shipped list as the list in use: as before 2.0.12, its types stay and a new tag is 'unknown'
        (self.root / 'tags' / 'rp_tags.json').unlink()
        res, _, _ = self._update(page, min_posts=20000, reference=shipped)
        self.assertTrue(res.written, res.message)
        self.assertTrue(res.message.endswith('merged into the list in use: 98 read, 96 new, 2 updated, 0 no longer listed kept)'),
                        res.message)
        written = _read_list(self.root / 'tags' / 'rp_tags.json')
        self.assertEqual(('493734 posts (copyright)', '20040 posts (character)', '28562 posts (unknown)', '7 posts (artist)'),
                         (written['porkyman'], written['raven'], written["five_nights_at_freddy's"], written['rare_tag']))

    def test_the_real_shipped_list_merges_without_loss(self) -> None:
        shipped = self._shipped()
        before = tags_updater.read_tag_list(shipped, '')
        page = {name: posts for name, posts, _ in self._read({_paheal_url(20000): _paheal_page(self.section)}, min_posts=20000)[0]}
        res, _, _ = self._update({_paheal_url(20000): _paheal_page(self.section)}, min_posts=20000, reference=shipped)
        self.assertTrue(res.written, res.message)
        self.assertEqual((len(before), len(before)), (res.count, res.previous))  # the 98 most used tags are all in the shipped list
        updated = sum(1 for name, posts in page.items() if before[name][0] != posts)
        self.assertIn(f'98 read, 0 new, {updated:d} updated, 0 no longer listed kept', res.message)
        written = tags_updater.read_tag_list(self.root / 'tags' / 'rp_tags.json', '')
        self.assertEqual(set(before), set(written))
        self.assertEqual({**before, **{name: (posts, before[name][1]) for name, posts in page.items()}}, written)
        self.assertEqual((493734, ''), written['porkyman'])
        self.assertEqual(before['!karryn!'], written['!karryn!'])  # under the page's floor: the shipped count stays

    def test_names_match_the_shipped_list(self) -> None:
        names = json.loads(self._shipped().read_text(encoding='utf-8'))
        got, _, _, _ = self._read({_paheal_url(20000): _paheal_page(self.section)}, min_posts=20000)
        self.assertEqual([], [n for n, _, _ in got if n not in names])  # the same names: lowercase, html-unescaped once

    def test_static_rules_of_tag_jobs(self) -> None:
        from lynxx.extgen.policy import check_source
        violations = check_source((PAHEAL / 'tags.py').read_text(encoding='utf-8'), allowed_hosts={'rule34.paheal.net', 'paheal.net'},
                                  downloader=False)
        self.assertEqual([], [v.to_dict() for v in violations])

    def test_the_ai_generator_reference_leaves_its_tag_update_out(self) -> None:
        # the AI generator reads paheal as the reference of shimmie_api but writes no tags.py: a copied tagUpdate would fail its draft
        from lynxx.aigen.tools import reference_text
        text = reference_text('paheal')
        self.assertNotIn('tagUpdate', text)
        manifest = json.loads(text.split('### extensions/paheal/manifest.json\n', 1)[1])
        self.assertEqual(('rp', '1.0.4', 'rp_tags.json'), (manifest['abbr'], manifest['version'], manifest['tagList']))
        self.assertIn('class DownloaderRp', text)

#
#
#########################################
