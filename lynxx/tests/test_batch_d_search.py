# coding=UTF-8
"""
Lynxx: tests for audit item D7, sidecar part (docs/reports/audit-2026-11-16.md): tags.resolve, tags.fuzzy, search.related,
search.preview page size and multi-task merge. Offline (fake danbooru / rule34 api pages, in-memory tag lists)
Run: python -m unittest lynxx.tests.test_batch_d_search
"""
#########################################
#
#

import datetime
import fnmatch
import json
import os
import random
import statistics
import string
import sys
import tempfile
import threading
import time
import warnings
from unittest import TestCase, mock
from urllib import parse as url_parse

from bs4 import XMLParsedAsHTMLWarning

from lynxx.logger import Logger

Logger.init(True, True)
warnings.filterwarnings('ignore', category=XMLParsedAsHTMLWarning)  # rule34 api pages are parsed as html, like the downloader does

from lynxx import server, tagsdb  # noqa: E402
from lynxx.defines import ITEMS_PER_PAGE_DB  # noqa: E402
from lynxx.download_db import DownloaderDb  # noqa: E402
from lynxx.download_gelbooru import DownloaderGelbooru  # noqa: E402
from lynxx.extensions import Extensions  # noqa: E402
from lynxx.module import ProcModule  # noqa: E402
from lynxx.network import HTMLContent  # noqa: E402
from lynxx.tags import TagInfo, TagTypes  # noqa: E402
from lynxx.tagsdb import TagsDB, _osa_distance  # noqa: E402

G, C, A, M = TagTypes.TAG_TYPE_GENERAL, TagTypes.TAG_TYPE_CHARACTER, TagTypes.TAG_TYPE_ARTIST, TagTypes.TAG_TYPE_META


class _Env(TestCase):
    """Scratch data dir, no tag list files, notifications muted, tag lists dropped afterwards"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': self.tmp.name, 'LYNXX_TAGS_DIR': os.path.join(self.tmp.name, 'tags')})
        self.env.start()
        self.mute = mock.patch.object(server, 'notify')
        self.mute.start()
        TagsDB.clear()
        Extensions.ensure_loaded()

    def tearDown(self) -> None:
        TagsDB.clear()
        self.mute.stop()
        self.env.stop()
        self.tmp.cleanup()
        ProcModule.set(ProcModule.RX)


# ----- tags.resolve -----

class ResolveTests(_Env):
    def setUp(self) -> None:
        super().setUp()
        TagsDB.load_from_dict('zz', {
            'cat_ears': TagInfo(G, 500), 'oral': TagInfo(G, 90), 'latex': TagInfo(G, 70), 'latex_gloves': TagInfo(G, 7),
            'saber_(fate)': TagInfo(C, 40), '?': TagInfo(G, 11), 'solo': TagInfo(G, 1000), 'honkai:_star_rail': TagInfo(G, 30),
            'some_artist': TagInfo(A, 3),
        })
        # all_tag_aliases.json is a file tagging map, not the site's aliases: only used when the tag itself is not listed
        self.aliases = mock.patch.dict(tagsdb.TAG_ALIASES, {'catear': 'cat_ears', 'oral_sex': 'oral', 'latex_gloves': 'latex',
                                                            'male': '1+boys'}, clear=True)
        self.aliases.start()

    def tearDown(self) -> None:
        self.aliases.stop()
        super().tearDown()

    def resolve(self, tags, module='zz') -> dict:
        return server.m_tags_resolve({'module': module, 'tags': tags})

    def test_known_alias_unknown_meta(self) -> None:
        res = self.resolve([' Cat_Ears ', 'catear', 'oral_sex', 'latex_gloves', 'male', 'nope', 'rating:explicit', 'id:>5',
                            'honkai:_star_rail', 'saber_(fate)', '-solo', '?', 'cat_*', 'zz_*', ''])
        self.assertTrue(res['hasList'])
        rows = {r['tag']: r for r in res['items']}
        self.assertEqual(15, len(res['items']), 'one item per input tag')
        self.assertEqual(['cat_ears', 'catear', 'oral_sex'], [r['tag'] for r in res['items'][:3]], 'input order kept')
        self.assertEqual({'tag': 'cat_ears', 'canonical': 'cat_ears', 'alias': False, 'posts': 500, 'type': 'general', 'known': True},
                         res['items'][0])
        self.assertEqual({'tag': 'catear', 'canonical': 'cat_ears', 'alias': True, 'posts': 500, 'type': 'general', 'known': True},
                         rows['catear'])
        self.assertEqual(('oral', True, 90), (rows['oral_sex']['canonical'], rows['oral_sex']['alias'], rows['oral_sex']['posts']))
        self.assertEqual(('latex_gloves', False, 7), (rows['latex_gloves']['canonical'], rows['latex_gloves']['alias'],
                                                      rows['latex_gloves']['posts']), 'a listed tag is never rewritten')
        self.assertEqual(('male', False, False, 'unknown'), (rows['male']['canonical'], rows['male']['alias'], rows['male']['known'],
                                                             rows['male']['type']), 'alias to an unlisted tag is not applied')
        self.assertEqual((False, 0, 'unknown'), (rows['nope']['known'], rows['nope']['posts'], rows['nope']['type']))
        for meta in ('rating:explicit', 'id:>5'):
            self.assertEqual((True, 'meta', 0), (rows[meta]['known'], rows[meta]['type'], rows[meta]['posts']))
        self.assertEqual((True, 'general', 30), (rows['honkai:_star_rail']['known'], rows['honkai:_star_rail']['type'],
                                                 rows['honkai:_star_rail']['posts']), 'not a known meta name: plain tag')
        self.assertEqual(('character', 40), (rows['saber_(fate)']['type'], rows['saber_(fate)']['posts']))
        self.assertEqual((True, 1000), (rows['solo']['known'], rows['solo']['posts']), 'negation stripped')
        self.assertEqual((True, 'general'), (rows['?']['known'], rows['?']['type']), "listed '?' is a tag, not a wildcard")
        self.assertEqual((True, 'wildcard'), (rows['cat_*']['known'], rows['cat_*']['type']))
        self.assertEqual((False, 'wildcard'), (rows['zz_*']['known'], rows['zz_*']['type']))
        self.assertFalse(rows['']['known'])

    def test_limits_and_missing_list(self) -> None:
        res = self.resolve([f't{i}' for i in range(100)])
        self.assertEqual(server.RESOLVE_TAGS_MAX, len(res['items']))
        res = self.resolve('solo  cat_ears')  # a string is split
        self.assertEqual(['solo', 'cat_ears'], [r['tag'] for r in res['items']])
        res = self.resolve(['solo', 'score:>10'], module='nolist')
        self.assertFalse(res['hasList'])
        self.assertEqual([False, True], [r['known'] for r in res['items']])

    def test_never_takes_core_lock(self) -> None:
        done = threading.Event()
        with server._core_lock:
            t = threading.Thread(target=lambda: (self.resolve(['solo']), server.m_tags_fuzzy({'module': 'zz', 'text': 'sollo'}), done.set()))
            t.start()
            t.join(10.0)
        self.assertTrue(done.is_set())


# ----- tags.fuzzy -----

def _brute_force(names: dict[str, int], text: str, max_edits: int) -> list[tuple[int, str]]:
    key = text.replace('_', '')
    out = []
    for name in names:
        if (d := _osa_distance(key, name.replace('_', ''))) <= max_edits:
            out.append((d, name))
    return sorted(out, key=lambda r: (r[0], -names[r[1]], r[1]))


class FuzzyTests(_Env):
    def fuzzy(self, text: str, limit=8, module='zz') -> list[dict]:
        return server.m_tags_fuzzy({'module': module, 'text': text, 'limit': limit})

    def test_ranking_and_underscores(self) -> None:
        TagsDB.load_from_dict('zz', {
            'scenery': TagInfo(G, 900), 'scene': TagInfo(G, 5), 'cat_ears': TagInfo(G, 400), 'bat_ears': TagInfo(G, 40),
            'cat_ear_headphones': TagInfo(G, 30), 'catears': TagInfo(G, 2), 'long_hair': TagInfo(G, 5000), 'long_hai': TagInfo(G, 1),
            'black_and_white': TagInfo(G, 50), 'smile': TagInfo(G, 3000), 'mile': TagInfo(G, 9),
        })
        rows = self.fuzzy('sceneyr')
        self.assertEqual({'tag': 'scenery', 'posts': 900, 'type': 'general', 'distance': 1}, rows[0], 'adjacent swap is one edit')
        self.assertEqual([('cat_ears', 0), ('catears', 0), ('bat_ears', 1)], [(r['tag'], r['distance']) for r in self.fuzzy('CatEars')][:3],
                         'missing / extra underscores are free, equal distance: most popular first')
        self.assertEqual('cat_ears', self.fuzzy('cat_ear')[0]['tag'])
        self.assertEqual('scenery', self.fuzzy(' -SCENEYR ')[0]['tag'], 'trimmed, lowercase, negation ignored')
        self.assertEqual(('black_and_white', 0), (self.fuzzy('blackandwhite')[0]['tag'], self.fuzzy('blackandwhite')[0]['distance']))
        self.assertEqual(['long_hair', 'long_hai'], [r['tag'] for r in self.fuzzy('long_hiar')][:2], 'distance, then posts')
        self.assertEqual(2, len(self.fuzzy('sceneyr', limit=2)))
        self.assertEqual([], self.fuzzy('zzzzzzzz'))

    def test_distance_limits(self) -> None:
        TagsDB.load_from_dict('zz', {'ab': TagInfo(G, 1), 'abc': TagInfo(G, 1), 'abcd': TagInfo(G, 1), 'abxyz': TagInfo(G, 1),
                                     'smile': TagInfo(G, 1), 'smxle': TagInfo(G, 1), 'smxye': TagInfo(G, 1),
                                     'scenery': TagInfo(G, 1), 'scxnxry': TagInfo(G, 1), 'sxxnxry': TagInfo(G, 1)})
        self.assertEqual([], self.fuzzy('ab'), 'shorter than 3 chars: nothing')
        self.assertEqual([], self.fuzzy('a_b__'), 'underscores do not count')
        self.assertEqual({'smile': 0, 'smxle': 1}, {r['tag']: r['distance'] for r in self.fuzzy('smile')}, 'up to 5 chars: 1 edit')
        self.assertEqual({'abc': 0, 'ab': 1, 'abcd': 1}, {r['tag']: r['distance'] for r in self.fuzzy('abc')})
        self.assertEqual({'scenery': 0, 'scxnxry': 2}, {r['tag']: r['distance'] for r in self.fuzzy('scenery')}, '6+ chars: 2 edits')
        self.assertEqual([], self.fuzzy('sceneyr', module='nolist'), 'no tag list: nothing')

    def test_matches_brute_force(self) -> None:
        rng = random.Random(7)
        alphabet = 'abcde_'
        names: dict[str, int] = {}
        while len(names) < 3000:
            names[''.join(rng.choice(alphabet) for _ in range(rng.randint(1, 9))).strip('_') or 'a'] = rng.randint(0, 1000)
        for extra in ('äbc', 'ábcd', 'ab一cd', 'a二bcde', 'café', 'naïve_one', 'ab\x00cd', 'lone_\ud800'):
            names[extra] = rng.randint(0, 1000)
        TagsDB.load_from_dict('zz', {k: TagInfo(G, v) for k, v in names.items()})
        queries = [''.join(rng.choice(alphabet) for _ in range(rng.randint(3, 9))) for _ in range(150)]
        queries += ['abc', 'äbcd', 'ab二cd', 'naive_one', 'abxcd', 'lone_\ud801']
        for q in queries:
            key = q.replace('_', '')
            if len(key) < tagsdb.FUZZY_MIN_LENGTH:
                continue
            max_edits = 2 if len(key) >= tagsdb.FUZZY_TWO_EDITS_LENGTH else 1
            expected = _brute_force(names, q, max_edits)
            got = [(d, n) for n, _, d in TagsDB.fuzzy_matches('zz', q, 10 ** 6)]
            self.assertEqual(expected, got, q)

    def test_index_lifetime(self) -> None:
        TagsDB.load_from_dict('zz', {'scenery': TagInfo(G, 1)})
        lst = TagsDB._load('zz')
        self.assertIsNone(lst._fuzzy, 'built lazily')
        self.fuzzy('sceneyr')
        self.assertIsNotNone(lst._fuzzy)
        TagsDB.load_from_dict('zz', {'scenario': TagInfo(G, 1)})
        self.assertEqual(['scenario'], [r['tag'] for r in self.fuzzy('scenaroi')], 'a replaced list gets its own index')
        TagsDB.unload('zz')
        self.assertFalse(TagsDB.is_loaded('zz'))

    def test_speed_500k(self) -> None:
        rng = random.Random(3)
        words = [''.join(rng.choice(string.ascii_lowercase) for _ in range(rng.randint(2, 9))) for _ in range(4000)]
        names: dict[str, TagInfo] = {}
        while len(names) < 500_000:
            names['_'.join(rng.sample(words, rng.choice((1, 2, 2, 3))))] = TagInfo(G, rng.randint(0, 100000))
        TagsDB.load_from_dict('zz', names)
        t0 = time.perf_counter()
        TagsDB.fuzzy_matches('zz', 'warmup', 1)
        build = time.perf_counter() - t0
        sample = rng.sample(list(names), 40)
        times = []
        for name in sample:
            typo = list(name)
            i = rng.randrange(len(typo))
            typo[i] = 'q' if typo[i] != 'q' else 'z'
            t0 = time.perf_counter()
            rows = TagsDB.fuzzy_matches('zz', ''.join(typo), 8)
            times.append(time.perf_counter() - t0)
            if len(name.replace('_', '')) >= tagsdb.FUZZY_MIN_LENGTH:
                self.assertIn(name, [r[0] for r in rows] + [n for n, *_ in TagsDB.fuzzy_matches('zz', ''.join(typo), 10 ** 6)])
        median, worst = statistics.median(times) * 1000, max(times) * 1000
        print(f'\nfuzzy, 500k tags: index build {build:.2f} s, query median {median:.1f} ms, max {worst:.1f} ms')
        self.assertLess(median, 50.0)
        self.assertLess(build, 10.0)


# ----- fake websites -----

def _posts(n: int, seed: int, pool=('a', 'b', 'c', 'solo', 'sky', 'cloud', 'tree', 'x', 'y')) -> list[dict]:
    rng = random.Random(seed)
    out = []
    pid = 100
    for _ in range(n):
        pid += rng.randint(1, 5)
        out.append({'id': pid, 'tags': sorted(set(rng.sample(pool, rng.randint(1, 5))))})
    out.reverse()  # newest first
    return out


def _match(post: dict, tags: list[str]) -> bool:
    for t in tags:
        if not t or ':' in t:
            continue
        found = any(fnmatch.fnmatchcase(pt, t.lstrip('-')) for pt in post['tags'])
        if found == t.startswith('-'):
            return False
    return True


class FakeSite:
    def __init__(self, posts: list[dict], page_size: int) -> None:
        self.posts, self.page_size = posts, page_size
        self.urls: list[str] = []
        self.rx_created = 'Sat Jan 04 12:00:00 +0000 2020'
        '''created_at of the rule34 posts (a post's own 'created' wins)'''

    def db_fetch(self, url: str, **_) -> HTMLContent:
        self.urls.append(url)
        q = url_parse.parse_qs(url_parse.urlparse(url).query)
        res = [p for p in self.posts if _match(p, q.get('tags', [''])[0].split(' '))]
        limit, page = int(q.get('limit', [str(self.page_size)])[0]), int(q.get('page', ['1'])[0])
        chunk = res[(page - 1) * limit:page * limit]
        return HTMLContent(None, json.dumps([{
            'id': p['id'], 'created_at': '2026-11-16T04:24:19.090-05:00', 'tag_string': ' '.join(p['tags']), 'file_ext': 'jpg',
            'md5': f'{p["id"]:032d}', 'file_url': f'https://cdn.example/{p["id"]}.jpg', 'preview_file_url': f'https://cdn.example/p{p["id"]}.jpg',
            'rating': 'g', 'score': 1, 'image_width': 10, 'image_height': 10} for p in chunk]).encode())

    def rx_fetch(self, url: str, **_) -> HTMLContent:
        self.urls.append(url)
        q = url_parse.parse_qs(url_parse.urlparse(url).query)
        res = [p for p in self.posts if _match(p, q.get('tags', [''])[0].split(' '))]
        limit, pid = int(q.get('limit', [str(self.page_size)])[0]), int(q.get('pid', ['0'])[0])
        posts = ''.join(
            f'<post height="10" score="1" file_url="https://cdn.example/images/{p["id"]}.jpg" parent_id="" '
            f'sample_url="https://cdn.example/samples/{p["id"]}.jpg" preview_url="https://cdn.example/thumbs/{p["id"]}.jpg" '
            f'rating="e" tags=" {" ".join(p["tags"])} " id="{p["id"]}" width="10" change="1" md5="{p["id"]:032x}" creator_id="1" '
            f'has_children="false" created_at="{p.get("created", self.rx_created)}" status="active" source="" has_notes="false" '
            f'has_comments="false" preview_width="10" preview_height="10"/>' for p in res[pid * limit:(pid + 1) * limit])
        return HTMLContent(None, f'<?xml version="1.0" encoding="UTF-8"?><posts count="{len(res)}" offset="{pid * limit}">{posts}</posts>'
                           .encode())

    def query_params(self, i=-1) -> dict[str, str]:
        return {k: v[0] for k, v in url_parse.parse_qs(url_parse.urlparse(self.urls[i]).query).items()}


def _expected_ids(posts: list[dict], *queries: list[str]) -> list[str]:
    return [str(p['id']) for p in posts if any(_match(p, q) for q in queries)]


# ----- search.preview -----

class _SiteEnv(_Env):
    def setUp(self) -> None:
        super().setUp()
        self.site = FakeSite(_posts(120, 1), 50)
        self.patches = [mock.patch.object(DownloaderDb, 'fetch_html', lambda _, url, **kw: self.site.db_fetch(url)),
                        mock.patch.object(DownloaderGelbooru, 'fetch_html', lambda _, url, **kw: self.site.rx_fetch(url)),
                        mock.patch('lynxx.network.time.sleep')]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        super().tearDown()



class PreviewTests(_SiteEnv):
    def preview(self, module: str, tags: str, **params) -> dict:
        return server.m_search_preview({'module': module, 'tags': tags, 'args': [], **params})

    def test_page_size(self) -> None:
        all_a = _expected_ids(self.site.posts, ['a'])
        res = self.preview('db', 'a', pageSize=7, page=2)
        self.assertEqual(('7', '3'), (self.site.query_params()['limit'], self.site.query_params()['page']))
        self.assertEqual(all_a[14:21], [it['id'] for it in res['items']])
        self.assertEqual((7, True, 2), (res['pageSize'], res['hasMore'], res['page']))
        last = (len(all_a) - 1) // 7
        res = self.preview('db', 'a', pageSize=7, page=last)
        self.assertEqual(all_a[last * 7:], [it['id'] for it in res['items']])
        self.assertEqual(len(all_a) % 7 == 0, res['hasMore'])
        self.assertFalse(self.preview('db', 'a', pageSize=7, page=last + 1)['hasMore'])
        module_size = ITEMS_PER_PAGE_DB
        self.assertEqual(min(module_size, server.PREVIEW_PAGE_SIZE_DEFAULT), self.preview('db', 'a')['pageSize'], 'default')
        self.assertEqual(module_size, self.preview('db', 'a', pageSize=10 ** 6)['pageSize'], 'at most the module page size')
        self.assertEqual(str(module_size), self.site.query_params()['limit'])
        self.assertEqual(server.PREVIEW_PAGE_SIZE_DEFAULT, self.preview('db', 'a', pageSize='x')['pageSize'], 'invalid -> default')

    def test_gelbooru_page_index(self) -> None:
        res = self.preview('rx', 'b', pageSize=5, page=3)
        self.assertEqual(('5', '3'), (self.site.query_params()['limit'], self.site.query_params()['pid']), 'dapi pid is a page index')
        self.assertEqual(_expected_ids(self.site.posts, ['b'])[15:20], [it['id'] for it in res['items']])
        self.assertEqual(200, self.preview('rx', 'b')['pageSize'], 'rx lists 1000 posts per page: preview default is smaller')

    def test_module_page_size_kept(self) -> None:
        class Site:
            favorites_search_user = pool_search_str = ''

            def _get_items_per_page(self) -> int:
                return 50

            def _form_tags_search_address(self, tags: str, maxlim: int | None = None) -> str:
                return ''

        class NoPageSize(Site):
            def _form_tags_search_address(self, tags: str, *_) -> str:  # like rule34.us, rule34hentai
                return ''

        self.assertTrue(server._takes_page_size(DownloaderDb))
        self.assertTrue(server._takes_page_size(DownloaderGelbooru))
        self.assertEqual((5, 5), server._results_page_size(Site(), 5))
        self.assertEqual((50, 50), server._results_page_size(Site(), 500))
        self.assertEqual((50, None), server._results_page_size(NoPageSize(), 5), 'no page size in the search address: module page size')
        fav = Site()
        fav.favorites_search_user = 'bob'
        self.assertEqual((50, None), server._results_page_size(fav, 5), 'favorites pages: module page size')
        pool = Site()
        pool.pool_search_str = '12'
        self.assertEqual((50, None), server._results_page_size(pool, 5), 'pool pages: module page size')

    def test_multi_task_merge(self) -> None:
        res = self.preview('db', '(a~b) c', pageSize=6)
        self.assertEqual(2, len(res['tasks']))
        self.assertEqual(2, len(self.site.urls), 'one request per task')
        a_c, b_c = _expected_ids(self.site.posts, ['a', 'c']), _expected_ids(self.site.posts, ['b', 'c'])
        expected = sorted(set(a_c[:6]) | set(b_c[:6]), key=int, reverse=True)
        ids = [it['id'] for it in res['items']]
        self.assertEqual(expected, ids, 'merged, deduplicated, newest first')
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(any(i in a_c[:6] and i in b_c[:6] for i in ids), 'test data has posts found by both tasks')
        self.assertTrue(res['hasMore'])
        self.site.urls.clear()
        res = self.preview('db', '(a~b) c', pageSize=6, page=10 ** 3)
        self.assertEqual(([], False), (res['items'], res['hasMore']))

    def test_single_task_keeps_site_order(self) -> None:
        random.Random(5).shuffle(self.site.posts)  # e.g. 'order:score': never re-sorted by id
        res = self.preview('db', 'a', pageSize=10)
        self.assertEqual(_expected_ids(self.site.posts, ['a'])[:10], [it['id'] for it in res['items']])
        self.assertEqual(1, len(res['tasks']))

    def test_local_filters_kept(self) -> None:
        res = self.preview('db', '(a~b) -(x,y)', pageSize=40)
        by_id = {it['id']: it for it in res['items']}
        posts = {str(p['id']): p for p in self.site.posts}
        self.assertTrue(by_id)
        for i, it in by_id.items():
            neg = 'x' in posts[i]['tags'] and 'y' in posts[i]['tags']
            self.assertEqual(['neg'] if neg else [], it['excludedBy'])
            self.assertEqual(neg, it['excluded'])
        self.assertTrue(any(it['excluded'] for it in by_id.values()))
        for key in ('requiredTags', 'site', 'genderStats', 'blockedCount', 'hiddenNoTags', 'genderEnabled', 'dateFilter'):
            self.assertIn(key, res)
        self.assertEqual('', res['dateFilter'])

    def test_dates_hide_posts_without_native_filter(self) -> None:
        # rule34 has no date search: the dates are not in the site query, posts outside them are excluded on the page
        # (fake rule34 posts are all from 04-01-2020)
        res = server.m_search_preview({'module': 'rx', 'tags': 'a', 'args': ['-mindate', '01-01-2021'], 'pageSize': 20})
        self.assertNotIn('date', self.site.query_params()['tags'])
        self.assertEqual('local', res['dateFilter'])
        self.assertTrue(res['items'])
        for it in res['items']:
            self.assertEqual((True, ['date']), (it['excluded'], it['excludedBy']))
        res = server.m_search_preview({'module': 'rx', 'tags': 'a', 'args': ['-maxdate', '31-12-2019'], 'pageSize': 20})
        self.assertTrue(all(it['excludedBy'] == ['date'] for it in res['items']))
        res = server.m_search_preview({'module': 'rx', 'tags': 'a', 'args': ['-mindate', '01-01-2020', '-maxdate', '04-01-2020'], 'pageSize': 20})
        self.assertEqual(('local', []), (res['dateFilter'], [it['id'] for it in res['items'] if it['excluded']]), 'limits are inclusive')
        # a listing without post dates: nothing can be checked, nothing is hidden, the app says so
        with mock.patch.object(DownloaderGelbooru, '_can_extract_item_info_without_fetch', return_value=False):
            res = server.m_search_preview({'module': 'rx', 'tags': 'a', 'args': ['-mindate', '01-01-2021'], 'pageSize': 20})
        self.assertEqual(('unchecked', False), (res['dateFilter'], any(it.get('excluded') for it in res['items'])))

    def test_dates_of_a_listing_without_dates_are_unchecked(self) -> None:
        # rule34hentai lists tags but no post dates: the page cannot be checked, the app must say so (not 'local' with 0 hidden)
        real = DownloaderGelbooru._extract_item_info

        def undated(dwn, h: str):
            info = real(dwn, h)
            info.date = ''
            return info
        with mock.patch.object(DownloaderGelbooru, '_extract_item_info', undated):
            res = server.m_search_preview({'module': 'rx', 'tags': 'a', 'args': ['-mindate', '01-01-2021'], 'pageSize': 20})
        self.assertTrue(res['items'])
        self.assertEqual(('unchecked', False), (res['dateFilter'], any(it.get('excluded') for it in res['items'])))
        first = {it['id'] for it in res['items'][:3]}

        def some_dated(dwn, h: str):
            info = real(dwn, h)
            if dwn._extract_id(dwn._local_addr_from_string(h)) not in first:
                info.date = ''
            return info
        with mock.patch.object(DownloaderGelbooru, '_extract_item_info', some_dated):
            res = server.m_search_preview({'module': 'rx', 'tags': 'a', 'args': ['-mindate', '01-01-2021'], 'pageSize': 20})
        hidden = [it['id'] for it in res['items'] if it['excludedBy'] == ['date']]
        self.assertEqual('local', res['dateFilter'], 'some posts were checked: the page was filtered')
        self.assertEqual(sorted(first), sorted(hidden))

    def test_dates_follow_the_day_of_each_request(self) -> None:
        # the app's core runs for days: a 'Đến ngày' left empty must not turn into the day the core started (an upper limit that
        # hides the newest posts), and a 'Đến ngày' on that day is a real limit. The download runs in its own process and agrees
        today = datetime.date.today()
        started = today - datetime.timedelta(days=1)
        stale = started.strftime('%d-%m-%Y')
        patches = [mock.patch.object(mod, name, value) for mod in list(sys.modules.values())
                   if getattr(mod, '__name__', '').startswith('lynxx')
                   for name, value in (('DATE_MAX_DEFAULT', stale), ('LAUCH_DATE', started)) if name in vars(mod)]
        self.assertTrue(patches)
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        site_day = lambda d: d.strftime('%a %b %d 12:00:00 +0000 %Y')  # noqa: E731
        preview = lambda *args: server.m_search_preview({'module': 'rx', 'tags': 'a', 'args': list(args), 'pageSize': 20})  # noqa: E731
        for day in (today, today + datetime.timedelta(days=1)):  # a site ahead of the user's time zone: its newest posts are 'tomorrow'
            self.site.rx_created = site_day(day)
            res = preview('-mindate', '01-01-2021')
            self.assertEqual('local', res['dateFilter'])
            self.assertTrue(res['items'])
            self.assertFalse(any(it['excluded'] for it in res['items']), day)
            res = preview('-maxdate', today.strftime('%d-%m-%Y'))  # today: no limit, as in the download (_filter_first_items)
            self.assertFalse(any(it['excluded'] for it in res['items']), day)
        self.site.rx_created = site_day(today)
        res = preview('-maxdate', stale)
        self.assertEqual('local', res['dateFilter'], 'the day the core started is a real limit')
        self.assertTrue(res['items'])
        self.assertTrue(all(it['excludedBy'] == ['date'] for it in res['items']))
        dwn_day = []
        server._with_downloader('rx', 'a', [], lambda dwn, args: dwn_day.append((dwn.date_today, dwn.date_max, dwn._has_date_limits())))
        self.assertEqual([(today, today.strftime('%d-%m-%Y'), False)], dwn_day)
        # danbooru's 'date:' metatag ends today, not on the day the core started
        server.m_search_preview({'module': 'db', 'tags': 'a', 'args': ['-mindate', '01-01-2021'], 'pageSize': 20})
        self.assertIn(f'date:2021-01-01..{today.isoformat()}', self.site.query_params()['tags'].split(' '))
        server.m_search_preview({'module': 'db', 'tags': 'a', 'args': [], 'pageSize': 20})
        self.assertFalse(any(t.startswith('date:') for t in self.site.query_params()['tags'].split(' ')))

    def test_dates_native_on_danbooru(self) -> None:
        # danbooru turns the dates into a 'date:' metatag: the site filters, the page is not filtered again
        res = server.m_search_preview({'module': 'db', 'tags': 'a', 'args': ['-mindate', '01-01-2021', '-maxdate', '31-12-2021'], 'pageSize': 20})
        self.assertIn('date:2021-01-01..2021-12-31', self.site.query_params()['tags'].split(' '))
        self.assertEqual('native', res['dateFilter'])
        self.assertTrue(res['items'])
        self.assertFalse(any(it['excluded'] for it in res['items']))

    def test_dates_in_count(self) -> None:
        from lynxx.download_rx import DownloaderRx
        count = lambda module, args: server.m_search_count({'module': module, 'tags': 'a', 'args': args})  # noqa: E731
        with mock.patch.object(DownloaderGelbooru, '_get_items_query_size_or_html', lambda self, url, tries=None: 7), \
                mock.patch.object(DownloaderDb, '_get_items_query_size_or_html', lambda self, url, tries=None: 5):
            r = count('rx', ['-mindate', '01-01-2021'])
            self.assertEqual((7, True, 'local'), (r['count'], r['approximate'], r['dateFilter']), 'the site counted every date')
            r = count('rx', [])
            self.assertEqual((False, ''), (r['approximate'], r['dateFilter']))
            r = count('db', ['-mindate', '01-01-2021'])
            self.assertEqual((5, False, 'native'), (r['count'], r['approximate'], r['dateFilter']))
            with mock.patch.object(DownloaderRx, 'CAPABILITIES', DownloaderRx.CAPABILITIES - {'date_filter'}):
                r = count('rx', ['-mindate', '01-01-2021'])
            self.assertEqual((False, 'unsupported'), (r['approximate'], r['dateFilter']))


# ----- search.related -----

class RelatedTests(_SiteEnv):
    def related(self, tags: str, module='db', **params) -> dict:
        return server.m_search_related({'module': module, 'tags': tags, 'args': [], **params})

    def test_counts_and_order(self) -> None:
        TagsDB.load_from_dict('db', {'sky': TagInfo(G, 50), 'cloud': TagInfo(G, 5), 'tree': TagInfo(G, 500), 'solo': TagInfo(G, 9000)})
        res = self.related('a')
        page = [p for p in self.site.posts if 'a' in p['tags']][:server.PREVIEW_PAGE_SIZE_DEFAULT]
        self.assertEqual(len(page), res['sample'])
        counts = {}
        for p in page:
            for t in p['tags']:
                if t != 'a':
                    counts[t] = counts.get(t, 0) + 1
        self.assertEqual(counts, {r['tag']: r['count'] for r in res['items']})
        keys = [(-r['count'], r['posts'], r['tag']) for r in res['items']]
        self.assertEqual(sorted(keys), keys, 'count desc, then rarer overall')
        sky = next(r for r in res['items'] if r['tag'] == 'sky')
        self.assertEqual((50, 'general'), (sky['posts'], sky['type']))
        self.assertEqual(3, len(self.related('a', limit=3)['items']))

    def test_exclusions(self) -> None:
        res = self.related('(a~b) -x sk*')
        tags = {r['tag'] for r in res['items']}
        self.assertFalse(tags & {'a', 'b', 'x', 'sky'}, 'query tags, negated tags and wildcard matches are excluded')
        expected = _expected_ids(self.site.posts, ['a', '-x', 'sky'])[:200] + _expected_ids(self.site.posts, ['b', '-x', 'sky'])[:200]
        self.assertEqual(len(set(expected)), res['sample'], 'tasks merged, deduplicated')
        res = self.related('a -(x,y)')
        kept = [p for p in self.site.posts if 'a' in p['tags']][:200]
        self.assertEqual(sum(1 for p in kept if not ('x' in p['tags'] and 'y' in p['tags'])), res['sample'],
                         'posts removed by local filters are not sampled')

    def test_listing_without_tags(self) -> None:
        with mock.patch.object(DownloaderDb, '_can_extract_item_info_without_fetch', return_value=False):
            self.assertEqual({'items': [], 'sample': 0}, self.related('a'))
        self.assertEqual([], self.site.urls, 'no request')

    def test_rpc_table(self) -> None:
        for name in ('tags.resolve', 'tags.fuzzy', 'search.related', 'search.preview'):
            self.assertIn(name, server.METHODS)

#
#
#########################################
