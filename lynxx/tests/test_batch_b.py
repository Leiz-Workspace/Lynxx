# coding=UTF-8
"""
Lynxx: regression tests for audit batch B (docs/reports/audit-2026-11-16.md), offline
Run: python -m unittest lynxx.tests.test_batch_b
"""
#########################################
#
#

import datetime
import json
import os
import pathlib
import random
import tempfile
from types import SimpleNamespace
from unittest import TestCase, mock

from bs4 import BeautifulSoup
from requests import Response

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx import server, tags_updater  # noqa: E402
from lynxx.defines import Comment, DownloaderStates, ThreadInterruptException  # noqa: E402
from lynxx.download_base import DownloaderBase  # noqa: E402
from lynxx.downloaders import _make_downloader  # noqa: E402
from lynxx.extensions import Extensions  # noqa: E402
from lynxx.gender import GenderLabel, infer_from_tags, rules_for_module  # noqa: E402
from lynxx.lynxx_cmd import EXIT_CANCELLED, EXIT_ERROR, EXIT_OK, EXIT_PARTIAL, run_exit_code  # noqa: E402
from lynxx.module import ProcModule  # noqa: E402
from lynxx.network import HTMLContent, thread_exit  # noqa: E402
from lynxx.safety import blocked_tag, register_extra_tags  # noqa: E402
from lynxx.tags import TagInfo, TagTypes  # noqa: E402
from lynxx.tagsdb import TagsDB  # noqa: E402

FMT = '%d-%m-%Y'
BASE = datetime.date(2020, 1, 1)


def _d(days: int) -> str:
    return (BASE + datetime.timedelta(days=days)).strftime(FMT)


class _FakePages:
    """Search pages newest first, each page a list of dates (descending). Uses the real search code of DownloaderBase"""
    _get_page_boundary_by_date = DownloaderBase._get_page_boundary_by_date
    _num_pages = DownloaderBase._num_pages
    date_today = datetime.date.today()

    def __init__(self, pages: list[list[str]], date_min: str, date_max: str) -> None:
        self.pages = pages
        self.minpage, self.maxpage = 0, len(pages) - 1
        self.date_min, self.date_max = date_min, date_max
        self.requests = 0

    def catch_cancel_or_ctrl_c(self) -> None:
        pass

    def _page_edge_date(self, pnum: int, first: bool) -> str | None:
        self.requests += 1
        p = self.pages[pnum]
        return (p[0] if first else p[-1]) if p else None


class _FakeItems:
    """Items newest first for the item level date filters"""
    _first_index_by_date = DownloaderBase._first_index_by_date
    _filter_last_items = DownloaderBase._filter_last_items
    _filter_first_items = DownloaderBase._filter_first_items
    date_today = datetime.date.today()

    def __init__(self, dates: list[str | None], date_min: str, date_max: str, per_page: int = 1000) -> None:
        self.items_raw_per_task = [f'item{i}' for i in range(len(dates))]
        self.items_raw_all: list[str] = []
        self.dates = {f'item{i}': d for i, d in enumerate(dates)}
        self.current_state = DownloaderStates.FILTERING_ITEMS1
        self.date_min, self.date_max = date_min, date_max
        self.per_page = per_page

    def catch_cancel_or_ctrl_c(self) -> None:
        pass

    def _get_items_per_page(self) -> int:
        return self.per_page

    def _single_item_date(self, item_raw: str) -> str | None:
        return self.dates[item_raw]


def _random_pages(rng: random.Random) -> list[list[str]]:
    n_pages = rng.randint(3, 40)
    per_page = rng.randint(1, 5)
    total = n_pages * per_page - rng.randint(0, per_page - 1)
    days = sorted((rng.randint(0, 2000) for _ in range(total)), reverse=True)  # all dates before 'today' (max date is checked)
    return [[_d(x) for x in days[i:i + per_page]] for i in range(0, total, per_page)]


class PageBoundaryTests(TestCase):
    """B6: the original bisection stopped one page off when pages count was not a power of 2"""

    def test_random_against_brute_force(self) -> None:
        rng = random.Random(1234)
        for _ in range(3000):
            pages = _random_pages(rng)
            lo_day, hi_day = sorted((rng.randint(1, 1999), rng.randint(1, 1999)))
            date_min, date_max = _d(lo_day), _d(hi_day)
            fp = _FakePages(pages, date_min, date_max)
            minpage = fp._get_page_boundary_by_date(True)
            fp.minpage = minpage
            maxpage = fp._get_page_boundary_by_date(False)
            dmin, dmax = (datetime.datetime.strptime(x, FMT).date() for x in (date_min, date_max))
            in_range = [pi for pi, p in enumerate(pages) for x in p if dmin <= datetime.datetime.strptime(x, FMT).date() <= dmax]
            if in_range:
                # every page holding a matching item is scanned
                self.assertLessEqual(minpage, min(in_range), (pages, date_min, date_max))
                self.assertGreaterEqual(maxpage, max(in_range), (pages, date_min, date_max))
                # and no page that is entirely out of range on the outer side (pages count >= 3 here)
                self.assertEqual(min(in_range), minpage)
                if len(pages) - minpage > 2:
                    self.assertEqual(max(in_range), maxpage)
                else:  # 1-2 pages left: not searched (as originally), item filters cover 2 pages worth of items
                    self.assertLessEqual(maxpage - max(in_range), 1)
            self.assertLessEqual(fp.requests, 2 * (len(pages).bit_length() + 1))


class ItemDateFilterTests(TestCase):
    """B6: item level filters (single item was never checked, bisection stopped one step off)"""

    def _expected(self, dates: list[str | None], date_min: str, date_max: str) -> list[int]:
        dmin, dmax = (datetime.datetime.strptime(x, FMT).date() for x in (date_min, date_max))
        return [i for i, x in enumerate(dates) if x is None or dmin <= datetime.datetime.strptime(x, FMT).date() <= dmax]

    def test_random_against_brute_force(self) -> None:
        rng = random.Random(42)
        for _ in range(3000):
            n = rng.randint(1, 60)
            dates = [_d(x) for x in sorted((rng.randint(0, 400) for _ in range(n)), reverse=True)]
            lo_day, hi_day = sorted((rng.randint(1, 399), rng.randint(1, 399)))
            fi = _FakeItems(dates, _d(lo_day), _d(hi_day), per_page=rng.randint(30, 100))
            fi._filter_last_items()
            fi._filter_first_items()
            self.assertEqual([f'item{i}' for i in self._expected(dates, _d(lo_day), _d(hi_day))], fi.items_raw_per_task)

    def test_single_item(self) -> None:
        fi = _FakeItems([_d(10)], _d(20), _d(30))
        fi._filter_last_items()
        self.assertEqual([], fi.items_raw_per_task)
        fi = _FakeItems([_d(40)], _d(20), _d(30))
        fi._filter_first_items()
        self.assertEqual([], fi.items_raw_per_task)
        fi = _FakeItems([_d(25)], _d(20), _d(30))
        fi._filter_last_items()
        fi._filter_first_items()
        self.assertEqual(['item0'], fi.items_raw_per_task)


class DownloaderRobustnessTests(TestCase):
    def setUp(self) -> None:
        Extensions.ensure_loaded()

    def test_idstring_prefix(self) -> None:
        """B7: one key format for item infos (prefixed only with -prefix)"""
        with _make_downloader(ProcModule.RX) as dwn:
            self.assertEqual('123', dwn._item_idstring('123'))
            dwn.add_filename_prefix = True
            self.assertEqual('rx_123', dwn._item_idstring('123'))

    def test_failing_item_does_not_kill_run(self) -> None:
        """B8: one item raising used to set killed for the whole run"""
        with _make_downloader(ProcModule.RX) as dwn:
            root = SimpleNamespace(killed=False)
            dwn.reset_root_thread(root)

            def boom(raw: str) -> None:
                try:
                    raise ValueError('broken item')
                except Exception:
                    dwn._on_thread_exception('worker')
                    raise
            with mock.patch.object(dwn, '_process_item', side_effect=boom):
                dwn._process_item_safe('<post id="77" file_url="x"/>')
            self.assertFalse(root.killed)
            self.assertEqual(1, dwn.fail_count)
            self.assertEqual(['77'], dwn.failed_items)
            self.assertEqual('', dwn.launch_error)

    def test_cancel_still_propagates(self) -> None:
        with _make_downloader(ProcModule.RX) as dwn:
            root = SimpleNamespace(killed=True)
            dwn.reset_root_thread(root)
            with mock.patch.object(dwn, '_process_item', side_effect=ThreadInterruptException()), \
                    self.assertRaises(ThreadInterruptException):
                dwn._process_item_safe('<post id="1"/>')

    def test_non_item_thread_failure_is_reported(self) -> None:
        with _make_downloader(ProcModule.RX) as dwn:
            root = SimpleNamespace(killed=False)
            dwn.reset_root_thread(root)
            try:
                raise RuntimeError('page parser crashed')
            except RuntimeError:
                dwn._on_thread_exception('page-worker')
            self.assertTrue(root.killed)
            self.assertIn('page parser crashed', dwn.launch_error)

    def test_empty_page_retries_are_capped(self) -> None:
        """B8: an empty page was retried forever"""
        with _make_downloader(ProcModule.RX) as dwn:
            dwn.reset_root_thread(SimpleNamespace(killed=False))
            dwn.url = 'https://api.rule34.xxx/index.php?page=dapi&s=post&q=index&tags=x'
            dwn.total_count_old = 3000
            empty = HTMLContent(BeautifulSoup('<posts count="3000"></posts>', 'html.parser'), b'<posts count="3000"></posts>')
            with mock.patch.object(dwn, 'fetch_html', return_value=empty) as fh, mock.patch.object(dwn, 'sleep_interruptible'):
                dwn._get_page_items(2, 3, 2)
            self.assertEqual(3, fh.call_count)
            self.assertEqual([], dwn.items_raw_per_page[2])
            self.assertEqual([3], dwn.failed_pages)

    def test_client_error_not_retried(self) -> None:
        """B10: 4xx used to be retried 10 times; json api errors are returned to the module"""
        with _make_downloader(ProcModule.DB) as dwn:
            dwn.reset_root_thread(SimpleNamespace(killed=False))
            calls = []

            def request(method, url, **_):
                calls.append(url)
                r = Response()
                r.status_code = 422
                r.url = url
                r.headers['content-type'] = 'application/json; charset=utf-8'
                r._content = b'{"success":false,"message":"You cannot search for more than 2 tags at a time."}'
                return r
            with mock.patch.object(dwn, '_thread_session', return_value=SimpleNamespace(request=request)), \
                    mock.patch.object(dwn, '_respect_rate_limit'):
                res = dwn.fetch_html('https://danbooru.donmai.us/posts.json?tags=a+b+c', tries=10)
            self.assertEqual(1, len(calls))
            self.assertIn('more than 2 tags', dwn._api_error(res.raw))

            def request404(method, url, **_):
                calls.append(url)
                r = Response()
                r.status_code = 403
                r.url = url
                r._content = b'forbidden'
                return r
            calls.clear()
            with mock.patch.object(dwn, '_thread_session', return_value=SimpleNamespace(request=request404)), \
                    mock.patch.object(dwn, '_respect_rate_limit'):
                self.assertIsNone(dwn.fetch_html('https://danbooru.donmai.us/posts.json?tags=a', tries=10))
            self.assertEqual(1, len(calls))

    def test_danbooru_unknown_count_bisection(self) -> None:
        """B8: counts=null (database timeout) was always FATAL, now the last page is searched"""
        total = 123 * 200 + 57
        with _make_downloader(ProcModule.DB) as dwn:
            dwn.reset_root_thread(SimpleNamespace(killed=False))
            dwn.url = dwn._form_tags_search_address('heavy_query')

            def fetch(url: str, **_):
                if 'counts/posts.json' in url:
                    raw = b'{"counts":{"posts":null}}'
                else:
                    page = int(url.rsplit('&page=', 1)[1]) - 1
                    n = max(0, min(200, total - page * 200))
                    raw = json.dumps([{'id': i} for i in range(n)]).encode()
                return HTMLContent(BeautifulSoup('', 'html.parser'), raw)
            with mock.patch.object(dwn, 'fetch_html', side_effect=fetch) as fh:
                self.assertEqual(total, dwn._get_items_query_size_or_html(dwn.url))
            self.assertLess(fh.call_count, 16)

    def test_gelbooru_count_error_has_reason(self) -> None:
        """B8: unexpected count response returned None (NotImplementedError later)"""
        with _make_downloader(ProcModule.RX) as dwn:
            dwn.reset_root_thread(SimpleNamespace(killed=False))
            raw = b'<response success="false" reason="Search error: API limited due to abuse"/>'
            bad = HTMLContent(BeautifulSoup(raw, 'html.parser'), raw)
            with mock.patch.object(dwn, 'fetch_html', return_value=bad), self.assertRaises(ThreadInterruptException) as ctx, \
                    mock.patch('lynxx.network.time.sleep'):
                dwn._get_items_query_size_or_html('https://api.rule34.xxx/index.php?page=dapi&s=post&q=index&tags=x')
            self.assertIn('API limited due to abuse', str(ctx.exception))

    def test_launch_error_and_exit_codes(self) -> None:
        """B9: the CLI always exited with 0"""
        with _make_downloader(ProcModule.RX) as dwn:
            def fatal() -> None:
                thread_exit('FATAL: something broke', -1)
            with mock.patch('lynxx.network.time.sleep'):
                dwn._launch(prepare_args(['-module', 'rx', '--', 'a']), fatal, False)
            self.assertEqual('FATAL: something broke', dwn.launch_error)
            self.assertEqual((EXIT_ERROR, 'FATAL: something broke'), run_exit_code(dwn))

        def ns(**kw):
            base = {'interrupted': False, 'launch_error': '', 'cf_challenge_hit': False, 'success_count': 0, 'fail_count': 0}
            return SimpleNamespace(**{**base, **kw})
        self.assertEqual(EXIT_OK, run_exit_code(ns(success_count=5))[0])
        self.assertEqual(EXIT_OK, run_exit_code(ns())[0])  # nothing found is not an error
        self.assertEqual(EXIT_PARTIAL, run_exit_code(ns(success_count=5, fail_count=1))[0])
        self.assertEqual(EXIT_ERROR, run_exit_code(ns(fail_count=3))[0])
        self.assertEqual(EXIT_ERROR, run_exit_code(ns(cf_challenge_hit=True))[0])
        self.assertEqual(EXIT_CANCELLED, run_exit_code(ns(interrupted=True, launch_error='x'))[0])


def prepare_args(argv: list[str]):
    from lynxx.cmdargs import prepare_arglist
    return prepare_arglist(argv)


class SearchCountTests(TestCase):
    """B10: search.count swallowed errors and returned 0; OR groups split into tasks were refused"""

    def setUp(self) -> None:
        Extensions.ensure_loaded()
        TagsDB.clear()

    def tearDown(self) -> None:
        ProcModule.set(ProcModule.RX)

    def test_error_is_reported(self) -> None:
        from lynxx.download_gelbooru import DownloaderGelbooru

        def fail(self, url, tries=None):
            thread_exit('ERROR: GetItemsQueSize: unable to retreive html', code=-444)
        with mock.patch.object(DownloaderGelbooru, '_get_items_query_size_or_html', fail), mock.patch('lynxx.network.time.sleep'), \
                self.assertRaises(server.RpcError) as ctx:
            server.m_search_count({'module': 'rx', 'tags': 'cat_ears'})
        self.assertIn('unable to retreive html', ctx.exception.message)

    def test_tasks_are_summed(self) -> None:
        from lynxx.download_db import DownloaderDb
        with mock.patch.object(DownloaderDb, '_get_items_query_size_or_html', lambda self, url, tries=None: 7):
            r = server.m_search_count({'module': 'db', 'tags': '(cat_ears~dog_ears) solo'})
        self.assertEqual([7, 7], r['taskCounts'])
        self.assertEqual(14, r['count'])
        self.assertTrue(r['approximate'])


class GenderSafetyTests(TestCase):
    """B12"""

    def test_futa_only_with_penis_is_only_female(self) -> None:
        res = infer_from_tags(['futa_only', 'penis', 'solo', 'breasts'], rules_for_module('rx'))
        self.assertEqual(GenderLabel.ONLY_FEMALE, res.label)
        self.assertIn('futa', res.flags)

    def test_safety_tokens_and_allowlist(self) -> None:
        self.assertEqual('teenage_girl', blocked_tag(['1girl', 'teenage_girl']))
        self.assertEqual('teenaged', blocked_tag(['teenaged']))
        self.assertEqual('', blocked_tag(['child_bearing_hips', 'young_adult', '1girl']))
        self.assertEqual('teen_titans', blocked_tag(['teen_titans']))  # series with teenage characters stay blocked
        register_extra_tags(['young_adult_test_only'])
        self.assertEqual('young_adult_test_only', blocked_tag(['young_adult_test_only']))

    def test_browse_metadata_type(self) -> None:
        """A-Z 'metadata' filter mapped to a non existing enum name and was ignored"""
        TagsDB.load_from_dict('zz', {'absurdres': TagInfo(TagTypes.TAG_TYPE_META, 100), 'apple': TagInfo(TagTypes.TAG_TYPE_GENERAL, 50)})
        try:
            r = server.m_tags_browse({'module': 'zz', 'prefix': 'a', 'types': ['metadata']})
            self.assertEqual(['absurdres'], [x['tag'] for x in r['items']])
            self.assertEqual('metadata', r['items'][0]['type'])
        finally:
            TagsDB.unload('zz')


class TagListTests(TestCase):
    """B13"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_gelbooru_unescape_and_cursor_guard(self) -> None:
        xml = '<tags><tag type="0" count="5" name="tom_&amp;_jerry" id="10"/><tag type="0" count="3" name="it&#039;s" id="11"/></tags>'
        client = SimpleNamespace(site='https://x/', get=lambda *_a, **_k: SimpleNamespace(text=xml))  # the same page every time
        with mock.patch.object(tags_updater, 'PAGE_LIMIT', 2):
            gen = tags_updater._iter_gelbooru(client, '', 1, 0)
            self.assertEqual(('tom_&_jerry', 5, 'general'), next(gen))
            self.assertEqual(("it's", 3, 'general'), next(gen))
            with self.assertRaisesRegex(RuntimeError, 'ignores pid'):  # same page again: the site ignores pid (never an endless list)
                list(gen)

    def test_gelbooru_pages_end_at_an_empty_page_only(self) -> None:
        # rule34.xxx page 1583: 999 tags and one with an empty name. Taken for the last page, the list stopped 33 pages early,
        # before every popular tag (they are on the last pages)
        def tag(i: int, name: str) -> str:
            return f'<tag type="0" count="{i}" name="{name}" id="{i}"/>'
        pages = {0: [tag(1, 'a'), tag(2, 'b'), tag(3, 'c')], 1: [tag(4, 'd'), tag(5, ''), tag(6, 'f')], 2: [tag(7, 'g'), tag(8, 'h')]}
        for past_end in ('<tag type="array"/>', None):  # rule34's empty page, or a site that answers its last page again
            with self.subTest(past_end=past_end):
                def get(url: str, *_a, **_k) -> SimpleNamespace:
                    pid = int(url.split('&pid=', 1)[1].split('&', 1)[0])
                    rows = pages.get(pid) or ([past_end] if past_end else pages[2])
                    return SimpleNamespace(text='<tags>' + ''.join(rows) + '</tags>')
                client = SimpleNamespace(site='https://x/', get=get)
                with mock.patch.object(tags_updater, 'PAGE_LIMIT', 3):
                    got = [name for name, _n, _t in tags_updater._iter_gelbooru(client, '', 1, 0)]
                self.assertEqual(['a', 'b', 'c', 'd', 'f', 'g', 'h'], got)

    def test_sanity_against_active_list_and_empty(self) -> None:
        active = self.dir / 'bundled' / 'zz_tags.json'
        active.parent.mkdir()
        tags_updater.write_tag_list(active, {f't{i}': (i + 1, 'general') for i in range(100)})
        out = self.dir / 'data'
        src = tags_updater.TagSource('zz', 'gelbooru_dapi', 'https://x.test/')
        with mock.patch.object(tags_updater, '_iter_source', return_value=iter([('a', 1, 'general')])):
            res = tags_updater.update_tag_list('zz', out, reference=active, source=src)
        self.assertFalse(res.written)
        self.assertEqual(100, res.previous)
        with mock.patch.object(tags_updater, '_iter_source', return_value=iter([])):
            res = tags_updater.update_tag_list('zz', out, force=True, source=src)
        self.assertFalse(res.written)
        self.assertFalse((out / 'zz_tags.json').exists())
        self.assertEqual([], [p.name for p in out.iterdir() if p.name.endswith('.tmp')])

    def test_locate_keeps_loaded_lists(self) -> None:
        tags_updater.write_tag_list(self.dir / 'zz_tags.json', {'apple': (5, 'general')})
        tags_updater.write_tag_list(self.dir / 'yy_tags.json', {'pear': (5, 'general')})
        with mock.patch.dict(os.environ, {'LYNXX_TAGS_DIR': self.dir.as_posix()}):
            TagsDB.locate_default_lists()
            TagsDB._load('zz')
            TagsDB._load('yy')
            TagsDB.locate_default_lists(reload=('yy',))
            self.assertTrue(TagsDB.is_loaded('zz'))
            self.assertFalse(TagsDB.is_loaded('yy'))
        TagsDB.clear()

    def test_tagsdb_loader_escapes(self) -> None:
        tags_updater.write_tag_list(self.dir / 'zz_tags.json', {'say_"hi"': (5, 'general'), 'back\\slash': (2, 'artist'), 'plain': (1, 'general')})
        with mock.patch.dict(os.environ, {'LYNXX_TAGS_DIR': self.dir.as_posix()}):
            TagsDB.locate_default_lists()
            TagsDB._load('zz')
            self.assertEqual({'say_"hi"', 'back\\slash', 'plain'}, set(TagsDB.as_dict('zz')))
        TagsDB.clear()


class Rule34UsCommentsTests(TestCase):
    """B14: 2+ comment pages crashed (HTMLContent used as soup), items of earlier tasks raised KeyError"""

    def test_comment_pages(self) -> None:
        Extensions.ensure_loaded(include_catalog=True)
        pid = ProcModule.PROC_MODULES_BY_NAME['rs']
        page1 = ('<div class="commentBox"><a href="index.php?r=account/profile&amp;id=1">alice</a>  first</div>'
                 '<a href="?r=posts/view&amp;id=5&amp;page=1">2</a><a href="?r=posts/view&amp;id=5&amp;page=1">next</a>')
        page2 = '<div class="commentBox"><a href="index.php?r=account/profile&amp;id=2">bob</a>  second</div>'
        try:
            with _make_downloader(pid) as dwn:
                dwn.reset_root_thread(SimpleNamespace(killed=False))
                info = SimpleNamespace(comments=[])
                dwn.item_info_dict_all['5'] = info  # earlier task: not in item_info_dict_per_task
                html2 = HTMLContent(BeautifulSoup(page2, 'html.parser'), page2.encode())
                with mock.patch.object(dwn, 'fetch_html', return_value=html2) as fh:
                    dwn._extract_comments(BeautifulSoup(page1, 'html.parser'), '5')
                self.assertEqual(1, fh.call_count)
                self.assertEqual([Comment('alice', 'first'), Comment('bob', 'second')], info.comments)
                dwn._extract_comments(BeautifulSoup('<div></div>', 'html.parser'), '999')  # unknown item: no KeyError
        finally:
            ProcModule.set(ProcModule.RX)


class PathTests(TestCase):
    """B11"""

    def test_resolve_user_path(self) -> None:
        home = pathlib.Path.home()
        self.assertEqual(home / 'x', server.resolve_user_path('~/x'))
        self.assertEqual(home / 'Downloads/L', server.resolve_user_path('Downloads/L'))
        self.assertEqual(pathlib.Path('/tmp/a'), server.resolve_user_path('/tmp/a'))

    def test_last_modified_parsing(self) -> None:
        import email.utils
        ts = email.utils.parsedate_to_datetime('Wed, 21 Oct 2015 07:28:00 GMT').timestamp()
        self.assertEqual(1445412480.0, ts)  # UTC, not local time (strptime %Z ignored the zone)

#
#
#########################################
