# coding=UTF-8
"""
Lynxx: tests of picked posts ("Tải đã chọn" for sites without id search, Lynxx 2.0.14), -max_vres (SDK 1.3) and log hygiene.
- the pick options (-pick_page, -pick_size, -pick_ids, -max_vres) are declared: page 0 is kept, bad ids are refused, picks without
  their page are a fatal argument error, a misspelled pick option never becomes a tag
- a pick job reads page P of the query exactly as the Preview read it (same requests: tasks, page size, page index, dates in the
  search address), then P+1 and P+2 only while picked posts are missing, logs the ones never found (W2), never counts, never streams
- the picked posts skip the query's filters (dates, required tags, negative groups, gender, -dlimit) but keep media type,
  existing files, -skip_known and the safety blacklist
- -max_vres sets Downloader.max_video_height, silently ignored by modules without 'video_quality_choice'; SDK 1.3
- credentials never reach a log: -cookie / -header values (W1 of a repeated name, -verbose command line, sidecar log redaction)
Offline: danbooru json pages of a fake site behind fetch_html (module db), temporary data dir.
Run: python -m unittest lynxx.tests.test_pick_and_quality
"""
#########################################
#
#

import contextlib
import io
import json
import os
import pathlib
import tempfile
import urllib.request
from multiprocessing.dummy import current_process
from types import SimpleNamespace
from unittest import TestCase, mock
from urllib import parse as url_parse

import requests

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx import download, modspec, network, server  # noqa: E402
from lynxx.cmdargs import prepare_arglist  # noqa: E402
from lynxx.defines import PICK_IDS_MAX, DownloadModes, ItemInfo, ModuleCaps, ThreadInterruptException  # noqa: E402
from lynxx.download_db import DownloaderDb  # noqa: E402
from lynxx.downloaders import DOWNLOADERS_BY_PROC_MODULE, _make_downloader  # noqa: E402
from lynxx.extensions import SOURCE_CATALOG, Extensions  # noqa: E402
from lynxx.module import ProcModule  # noqa: E402
from lynxx.network import CF_CHALLENGE_HINT, HTMLContent  # noqa: E402
from lynxx.sdk import SDK_VERSION, sdk_satisfies  # noqa: E402
from lynxx.utils import redact_args  # noqa: E402
from lynxx.validators import valid_id_list  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
SIZE = 7
'''page size of the Preview in these tests (danbooru takes up to 200)'''


class FakeDanbooru:
    """posts.json of a danbooru site: 120 posts (ids 1119 down to 1000, newest first). Every 10th post is a video, posts 1100..1104
    carry 'x y' (a negative group target), 1090 a tag of the safety blacklist. A tag with ':' (date:, order:) is ignored"""

    def __init__(self) -> None:
        self.posts = [{'id': 1119 - i, 'tags': ['a', 'b' if i % 2 else 'c', *(['x', 'y'] if 1100 <= 1119 - i <= 1104 else []),
                                                *(['diaper'] if 1119 - i == 1090 else [])]} for i in range(120)]
        self.urls: list[str] = []

    def fetch(self, url: str, **_) -> HTMLContent:
        self.urls.append(url)
        q = url_parse.parse_qs(url_parse.urlparse(url).query)
        words = [w for w in q.get('tags', [''])[0].split(' ') if w and ':' not in w]
        res = [p for p in self.posts if all((w[1:] not in p['tags']) if w.startswith('-') else (w in p['tags']) for w in words)]
        limit, page = int(q['limit'][0]), int(q['page'][0])
        return HTMLContent(None, json.dumps([{
            'id': p['id'], 'created_at': '2026-11-16T04:24:19.090-05:00', 'tag_string': ' '.join(p['tags']),
            'file_ext': 'mp4' if p['id'] % 10 == 0 else 'jpg', 'md5': f'{p["id"]:032d}',
            'file_url': f'https://cdn.example/{p["id"]}.{"mp4" if p["id"] % 10 == 0 else "jpg"}',
            'preview_file_url': f'https://cdn.example/p{p["id"]}.jpg', 'rating': 'g', 'score': 1, 'image_width': 10, 'image_height': 10}
            for p in res[(page - 1) * limit:page * limit]]).encode())

    def ids(self, *tags: str) -> list[str]:
        """ids of the posts having all these tags, newest first"""
        return [str(p['id']) for p in self.posts if all(t in p['tags'] for t in tags)]

    def params(self, i: int) -> dict[str, str]:
        return {k: v[0] for k, v in url_parse.parse_qs(url_parse.urlparse(self.urls[i]).query).items()}


class _Env(TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = pathlib.Path(self.tmp.name) / 'dl'
        self.dest.mkdir()
        self.env = mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': self.tmp.name, 'LYNXX_TAGS_DIR': os.path.join(self.tmp.name, 'tags'),
                                                'LYNXX_DLINDEX': '0'})
        self.env.start()
        Extensions.ensure_loaded()
        self.site = FakeDanbooru()
        self.patches = [mock.patch.object(DownloaderDb, 'fetch_html', lambda _, url, **kw: self.site.fetch(url)),
                        mock.patch.object(server, 'notify'), mock.patch.object(server, '_learn_from_preview'),
                        mock.patch('lynxx.network.time.sleep')]
        for p in self.patches:
            p.start()
        self.pid = ProcModule.PROC_MODULES_BY_NAME['db']

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        self.env.stop()
        self.tmp.cleanup()
        ProcModule.set(ProcModule.RX)

    def downloader(self):
        ProcModule.set(self.pid)
        dwn = _make_downloader(self.pid)
        me = current_process()
        me.killed = False
        dwn.reset_root_thread(me)
        dwn.no_conflict_delay = True
        return dwn

    def parsed(self, tags: str, *extra: str):
        """A downloader with the query parsed as a job parses it (no request)"""
        dwn = self.downloader()
        dwn._parse_args(prepare_arglist(['-module', 'db', '-path', str(self.dest), *extra, '--', *tags.split()]))
        return dwn

    def job(self, tags: str, *extra: str) -> tuple[object, list[str], list[str]]:
        """Runs a download job, files not downloaded: (downloader, ids sent to download in order, log lines)"""
        args = prepare_arglist(['-module', 'db', '-path', str(self.dest), *extra, '--', *tags.split()])
        dwn = self.downloader()
        sent: list[str] = []
        log: list[str] = []
        dwn._process_item = lambda raw: sent.append(dwn._extract_id(dwn._local_addr_from_string(str(raw))))
        with mock.patch.object(download, 'trace', lambda msg, *a, **kw: log.append(str(msg))):
            dwn.launch_download(args)
        self.assertEqual('', dwn.launch_error, '\n'.join(log))
        return dwn, sent, log

    def preview(self, tags: str, page: int, args: list[str] | None = None) -> dict:
        return server.m_search_preview({'module': 'db', 'tags': tags, 'args': args or [], 'page': page, 'pageSize': SIZE})


class PickArgumentsTests(_Env):
    def test_options_are_declared(self) -> None:
        args = prepare_arglist(['-module', 'db', '-pick_page', '0', '-pick_size', '7', '-pick_ids', '12,13,12', '-max_vres', '720',
                                '--', 'a', '-b'])
        self.assertEqual((0, 7, ['12', '13'], 720), (args.pick_page, args.pick_size, args.pick_ids, args.max_video_height))
        self.assertEqual(['a', '-b'], args.tags, 'no pick option or value turned into a tag')
        args = prepare_arglist(['-module', 'db', '--', 'a'])
        self.assertEqual((None, None, None, None), (args.pick_page, args.pick_size, args.pick_ids, args.max_video_height))
        for typo in (['-pick_pages', '1'], ['-pick_id', '5'], ['-max_vres_h', '480']):
            with self.subTest(typo=typo), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                prepare_arglist(['-module', 'db', '-pick_page', '0', '-pick_ids', '5', *typo, 'a'])
        self.assertEqual(['a', '-pick_axe'], prepare_arglist(['-module', 'db', '--', 'a', '-pick_axe']).tags, 'after \'--\' a tag')

    def test_page_zero_is_kept(self) -> None:
        dwn = self.parsed('a', '-pick_page', '0', '-pick_ids', '1105')
        self.assertEqual((0, 0, ('1105',)), (dwn.pick_page, dwn.pick_size, dwn.pick_ids))
        self.assertTrue(dwn._pick_mode())
        self.assertFalse(self.parsed('a')._pick_mode())

    def test_bad_values_are_refused(self) -> None:
        for bad in (['-pick_ids', ''], ['-pick_ids', ','], ['-pick_ids', '1;2'], ['-pick_ids', '1 2'], ['-pick_ids', 'x' * 65],
                    ['-pick_ids', ','.join(map(str, range(PICK_IDS_MAX + 1)))], ['-pick_page', '-1'], ['-pick_page', 'x'],
                    ['-pick_size', '0'], ['-max_vres', '-480']):
            with self.subTest(bad=bad[1][:20]), self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                prepare_arglist(['-module', 'db', *bad, '--', 'a'])
        self.assertEqual(['5', 'ab_c-1'], valid_id_list(' 5 ,ab_c-1,5,'))
        self.assertEqual(PICK_IDS_MAX, len(valid_id_list(','.join(map(str, [*range(PICK_IDS_MAX), 0, 1])))), 'counted without duplicates')

    def test_picks_need_their_page(self) -> None:
        for extra, said in ((['-pick_ids', '5'], 'needs \'-pick_page\''), (['-pick_page', '1'], 'need \'-pick_ids\''),
                            (['-pick_size', '7'], 'need \'-pick_ids\'')):
            with self.subTest(extra=extra), self.assertRaises(ThreadInterruptException) as ctx:
                self.parsed('a', *extra)
            self.assertIn(said, str(ctx.exception))
        dwn = self.downloader()
        dwn.get_max_id = True
        with self.assertRaises(ThreadInterruptException) as ctx:
            dwn._parse_args(prepare_arglist(['-module', 'db', '-get_maxid', '-pick_page', '0', '-pick_ids', '5']), False)
        self.assertIn('-get_maxid', str(ctx.exception))
        self.assertEqual([], self.site.urls, 'refused before any request')

    def test_parent_posts_off(self) -> None:
        dwn = self.parsed('a', '-include_parchi', '-pick_page', '0', '-pick_ids', '5')
        self.assertFalse(dwn.include_parchi)


class PickJobTests(_Env):
    def test_same_request_as_the_preview(self) -> None:
        for tags, extra in (('a', []), ('a c', ['-mindate', '01-01-2020', '-maxdate', '01-01-2027']), ('(b~c) a', [])):
            with self.subTest(tags=tags):
                self.site.urls.clear()
                res = self.preview(tags, 2, extra)
                preview_urls = list(self.site.urls)
                self.assertEqual(SIZE, res['pageSize'])
                if extra:
                    self.assertIn('date:2020-01-01..2027-01-01', preview_urls[0], 'the dates are part of the search address')
                ids = [it['id'] for it in res['items']][:3]
                self.site.urls.clear()
                _, sent, _ = self.job(tags, *extra, '-pick_page', '2', '-pick_size', str(SIZE), '-pick_ids', ','.join(ids))
                self.assertEqual(preview_urls, self.site.urls, 'exactly the requests of the Preview: no count, no other page')
                self.assertEqual(sorted(ids, key=int), sent)

    def test_found_on_the_next_page_or_never(self) -> None:
        all_a = self.site.ids('a')
        on_p, on_next = all_a[2 * SIZE:2 * SIZE + 2], all_a[3 * SIZE + 1]
        picks = [*on_p, on_next, '999']
        dwn, sent, log = self.job('a', '-pick_page', '2', '-pick_size', str(SIZE), '-pick_ids', ','.join(picks))
        self.assertEqual([('7', '3'), ('7', '4'), ('7', '5')], [(self.site.params(i)['limit'], self.site.params(i)['page'])
                                                              for i in range(len(self.site.urls))], 'P, P+1, P+2, no further')
        self.assertEqual(sorted([*on_p, on_next], key=int), sent)
        warn = [line for line in log if line.startswith('Warning (W2): 1 picked post(s) not found')]
        self.assertEqual(1, len(warn), log)
        self.assertIn('page(s) 3, 4, 5', warn[0])
        self.assertTrue(warn[0].endswith(': 999'))
        self.assertFalse(dwn.streaming_active)
        self.site.urls.clear()
        self.job('a', '-pick_page', '2', '-pick_size', str(SIZE), '-pick_ids', ','.join(on_p))
        self.assertEqual(1, len(self.site.urls), 'every pick on page P: one request')

    def test_an_unreadable_extra_page_keeps_the_picks(self) -> None:
        all_a = self.site.ids('a')
        on_p = all_a[2 * SIZE:2 * SIZE + 3]
        fetch = self.site.fetch

        def page_4_fails(url: str, **kw):
            page = url_parse.parse_qs(url_parse.urlparse(url).query)['page'][0]
            if page == '4':
                self.site.urls.append(url)
                return None if self.answer == 'none' else HTMLContent(None, b'{"success": false, "message": "busy"}')
            return fetch(url, **kw)
        for self.answer in ('none', 'error page'):
            with self.subTest(answer=self.answer), mock.patch.object(self.site, 'fetch', page_4_fails):
                self.site.urls.clear()
                _, sent, log = self.job('a', '-pick_page', '2', '-pick_size', str(SIZE), '-pick_ids', ','.join([*on_p, '999']))
                self.assertEqual(['3', '4'], [self.site.params(i)['page'] for i in range(len(self.site.urls))], 'no page 5')
                self.assertEqual(sorted(on_p, key=int), sent, 'the picks of page P are downloaded')
                self.assertTrue(any(line.startswith('Warning (W2): could not read page 4 (') for line in log), log)
                warn = [line for line in log if 'picked post(s) not found' in line]
                self.assertEqual(1, len(warn), log)
                self.assertIn('page(s) 3 ', warn[0], 'page 4 was not read')

    def test_an_unreadable_extra_page_still_stops_on_cancel(self) -> None:
        fetch = self.site.fetch

        def cancelled_on_page_2(url: str, **kw):
            if url_parse.parse_qs(url_parse.urlparse(url).query)['page'][0] == '2':
                current_process().killed = True
                return None
            return fetch(url, **kw)
        dwn = self.parsed('a', '-pick_page', '0', '-pick_size', str(SIZE), '-pick_ids', '999')
        with mock.patch.object(self.site, 'fetch', cancelled_on_page_2), self.assertRaises(ThreadInterruptException):
            dwn._fetch_picked_items()

    def test_next_page_only_for_full_tasks(self) -> None:
        # task 'b a' fills page 1 (60 posts), task 'x a' does not (5 posts): page 2 is read for 'b a' only
        _, sent, _ = self.job('(b~x) a', '-pick_page', '0', '-pick_size', str(SIZE), '-pick_ids', '1104,999')
        self.assertEqual(['1104'], sent)
        asked = [(self.site.params(i)['tags'].split(' ')[0], self.site.params(i)['page']) for i in range(len(self.site.urls))]
        self.assertEqual([('b', '1'), ('x', '1'), ('b', '2'), ('b', '3')], asked)

    def test_a_short_page_ends_the_search(self) -> None:
        last = (len(self.site.ids('a')) - 1) // SIZE
        _, sent, log = self.job('a', '-pick_page', str(last), '-pick_size', str(SIZE), '-pick_ids', '999')
        self.assertEqual(1, len(self.site.urls), 'the last page is not full: no next page')
        self.assertEqual([], sent)
        self.assertTrue(any('not found on page(s) ' + str(last + 1) + ' ' in line for line in log), log)

    def test_multi_task_query_merged(self) -> None:
        res = self.preview('(b~c) a', 1)
        page = [it['id'] for it in res['items']]
        only_b = next(i for i in page if i in self.site.ids('b'))
        only_c = next(i for i in page if i in self.site.ids('c'))
        self.site.urls.clear()
        dwn, sent, _ = self.job('(b~c) a', '-pick_page', '1', '-pick_size', str(SIZE), '-pick_ids', f'{only_b},{only_c}')
        self.assertEqual(2, dwn.orig_tasks_count)
        self.assertEqual(2, len(self.site.urls), 'page P of each task, read once (task 2 is not fetched again)')
        self.assertEqual(sorted([only_b, only_c], key=int), sent)

    def test_query_filters_do_not_apply_to_picks(self) -> None:
        # the posts the Preview showed as excluded (negative group 'x y', required tag over the tags limit, gender): picked anyway
        extra = ['-mindate', '01-01-2020', '-maxdate', '01-01-2027', '-gender', 'only_male', '-tags_limit', '1']
        res = self.preview('a c -(x,y)', 2, extra)
        by = {it['id']: set(it['excludedBy']) for it in res['items']}
        self.assertTrue(any('neg' in v for v in by.values()) and any('required' in v for v in by.values()), by)
        self.assertTrue(all('gender' in v for v in by.values()), by)
        picks = list(by)[:5]
        preview_url = self.site.urls[-1]
        self.site.urls.clear()
        dwn, sent, _ = self.job('a c -(x,y)', *extra, '-dlimit', '1', '-pick_page', '2', '-pick_size', str(SIZE),
                                  '-pick_ids', ','.join(picks))
        self.assertEqual([preview_url], self.site.urls, 'the dates still shaped the request')
        self.assertIn('date:', self.site.params(0)['tags'])
        self.assertEqual(sorted(picks, key=int), sent, 'every pick, -dlimit 1 ignored')
        self.assertEqual(([], [], frozenset(), 0), (dwn.required_tags, dwn.neg_and_groups, dwn.gender_select, dwn.download_limit))
        self.assertFalse(dwn._has_date_limits())

    def test_media_type_existing_known_and_safety_still_apply(self) -> None:
        page = self.site.ids('a')[4 * SIZE:5 * SIZE]  # 1091 .. 1085: 1090 is a video and carries 'diaper'
        self.assertIn('1090', page)
        (self.dest / '1089.jpg').write_bytes(b'x')
        known = {'1088': 'x'}
        with mock.patch.object(DownloaderDb, '_known_lookup', lambda _, ids, md5s: ({i: known[i] for i in ids if i in known}, {})):
            _, sent, _ = self.job('a', '-pick_page', '4', '-pick_size', str(SIZE), '-pick_ids', ','.join(page), '-skip_vid', '-skip_known')
        self.assertEqual(sorted(set(page) - {'1090', '1089', '1088'}, key=int), sent, 'video, existing file and known post skipped')
        _, sent, _ = self.job('a', '-pick_page', '4', '-pick_size', str(SIZE), '-pick_ids', '1090,1087', '-safety_blacklist')
        self.assertEqual(['1087'], sent, 'safety blacklist')

    def test_never_counts_streams_or_searches_pages(self) -> None:
        with mock.patch.object(DownloaderDb, '_resolve_task_pages', side_effect=AssertionError('page search')), \
                mock.patch.object(DownloaderDb, '_process_tags_streaming', side_effect=AssertionError('streaming')), \
                mock.patch.object(DownloaderDb, '_get_query_size_or_none', side_effect=AssertionError('count')):
            dwn, sent, _ = self.job('a', '-pick_page', '0', '-pick_size', str(SIZE), '-pick_ids', self.site.ids('a')[0])
        self.assertEqual([self.site.ids('a')[0]], sent)
        self.assertFalse(dwn._can_stream_task())

    def test_favorites_are_not_converted(self) -> None:
        # the Preview reads a favorites / pool query as it is: no 'id:' group made from the favorites pages first
        for extra, called in (([], True), (['-pick_page', '0', '-pick_ids', '5'], False)):
            with self.subTest(called=called), mock.patch.object(DownloaderDb, '_try_preprocess_favorites') as fav, \
                    mock.patch.object(DownloaderDb, '_try_preprocess_pool') as pool:
                self.parsed('favorited_by:bob a', *extra)
            self.assertEqual((called, called), (fav.called, pool.called))

    def test_failed_page_stops_the_job(self) -> None:
        with mock.patch.object(DownloaderDb, 'fetch_html', lambda *a, **kw: None):
            args = prepare_arglist(['-module', 'db', '-path', str(self.dest), '-pick_page', '0', '-pick_ids', '5', '--', 'a'])
            dwn = self.downloader()
            dwn.launch_download(args)
        self.assertIn('unable to retreive page 1', dwn.launch_error)


class PreviewPageTests(_Env):
    def test_listing_page_is_shared(self) -> None:
        dwn = self.parsed('a')
        self.assertEqual((SIZE, SIZE), dwn._listing_page_size(SIZE))
        self.assertEqual((server.PREVIEW_PAGE_SIZE_DEFAULT, server.PREVIEW_PAGE_SIZE_DEFAULT), dwn._listing_page_size(None))
        self.assertEqual(dwn._listing_page_size(5), server._results_page_size(dwn, 5))
        self.assertTrue(download.takes_page_size(DownloaderDb) and server._takes_page_size(DownloaderDb))
        items, more = server._fetch_results_page(dwn, 1, SIZE, SIZE)
        self.assertEqual(self.site.ids('a')[SIZE:2 * SIZE], [dwn._extract_id(dwn._local_addr_from_string(h)) for h in items])
        self.assertTrue(more)

    def test_preview_error_says_challenge(self) -> None:
        def challenged(dwn, _url: str, **_kw):
            dwn.cf_challenge_hit = True
            return None
        with mock.patch.object(DownloaderDb, 'fetch_html', challenged), self.assertRaises(server.RpcError) as ctx:
            self.preview('a', 0)
        self.assertIn('bot challenge', str(ctx.exception))
        self.assertTrue(ctx.exception.data['cfChallenge'])


class ListingPageSizeTests(_Env):
    def test_json_api_pages_keep_the_module_size(self) -> None:
        # yande.re, sakugabooru, zerochan: the page address always asks for the module's page size, whatever the Preview's size
        Extensions._load_many(Extensions._scan(ROOT / 'extensions', SOURCE_CATALOG))
        from lynxx.download_json import DownloaderJsonApi
        for abbr, limit_param in (('yr', 'limit'), ('sk', 'limit'), ('zc', 'l')):
            with self.subTest(abbr=abbr):
                pid = ProcModule.PROC_MODULES_BY_NAME[abbr]
                cls = DOWNLOADERS_BY_PROC_MODULE[pid]
                self.assertTrue(issubclass(cls, DownloaderJsonApi))
                self.assertFalse(download.takes_page_size(cls) or server._takes_page_size(cls))
                ProcModule.set(pid)
                dwn = _make_downloader(pid)
                dwn.reset_root_thread(current_process())
                dwn._parse_args(prepare_arglist(['-module', abbr, '--', 'a']), False)
                module_size = dwn._items_per_page()
                self.assertEqual((module_size, None), dwn._listing_page_size(50))
                self.assertEqual((module_size, None), server._results_page_size(dwn, 50))
                urls: list[str] = []
                items, more = dwn._fetch_listing_page(1, module_size, None, lambda url: urls.append(url) or [])
                self.assertEqual(([], False), (items, more))
                self.assertEqual(str(module_size), url_parse.parse_qs(url_parse.urlparse(urls[0]).query)[limit_param][0])
        self.assertTrue(download.takes_page_size(DownloaderDb), 'danbooru: the page address carries the page size')


class VideoQualityTests(_Env):
    def test_max_vres(self) -> None:
        self.assertEqual('video_quality_choice', ModuleCaps.VIDEO_QUALITY_CHOICE)
        self.assertEqual(720, self.parsed('a', '-max_vres', '720').max_video_height)
        dwn = self.parsed('a', '-lowres')
        self.assertEqual((0, True), (dwn.max_video_height, dwn.low_res), '-lowres (images) does not choose a video height')

    def test_ignored_silently_without_the_capability(self) -> None:
        dwn = self.downloader()
        self.assertNotIn(ModuleCaps.VIDEO_QUALITY_CHOICE, DownloaderDb.CAPABILITIES)
        log: list[str] = []
        with mock.patch.object(download, 'trace', lambda msg, *a, **kw: log.append(str(msg))), \
                mock.patch.object(download.time, 'sleep', side_effect=AssertionError('paused')):
            dwn.no_conflict_delay = False
            dwn._parse_args(prepare_arglist(['-module', 'db', '-max_vres', '480', '--', 'a']), False)
        self.assertEqual(480, dwn.max_video_height)
        self.assertFalse(dwn._solve_argument_conflicts(), 'no warning: multi-site jobs send it to every site')
        self.assertFalse([line for line in log if 'max_vres' in line or 'W1' in line], log)

    def test_sdk_1_3(self) -> None:
        self.assertEqual('1.3', SDK_VERSION)
        self.assertEqual('2.0.14', modspec.SDK_VERSIONS_SINCE['1.3'])
        self.assertTrue(sdk_satisfies('1.2') and sdk_satisfies('1.3'))
        self.assertFalse(sdk_satisfies('1.4'))


class LogHygieneTests(_Env):
    SECRETS = ('SECRET-CF', 'SECRET-JSON', 'SECRET-AUTH', 'K3Y', 'p4ss')

    def assertNoSecret(self, text: str) -> None:
        for s in self.SECRETS:
            self.assertNotIn(s, text)

    def test_repeated_names_log_no_value(self) -> None:
        log: list[str] = []
        with mock.patch.object(network, 'trace', lambda msg, *a, **kw: log.append(str(msg))):
            self.parsed('a', '-cookies', '{"cf_clearance": "SECRET-JSON"}', '-cookie', 'cf_clearance=SECRET-CF',
                        '-header', 'Authorization=SECRET-AUTH', '-header', 'Authorization=SECRET-AUTH2')
        warn = [line for line in log if 'W1' in line]
        self.assertEqual(2, len(warn), log)
        self.assertEqual(['Authorization', 'cf_clearance'], sorted(w.split("'")[1] for w in warn), 'the names are said')
        self.assertNoSecret('\n'.join(log))

    def test_verbose_command_line(self) -> None:
        cmd = ['-module', 'db', '-verbose', '-cookie', 'cf_clearance=SECRET-CF', '-header=Authorization=Bearer SECRET-AUTH',
               '-cookies', '{"a": "SECRET-JSON"}', '-api_key', 'K3Y,1', '-proxy', 'http://u:p4ss@127.0.0.1:8080', '--', '-cookie']
        self.assertEqual(['-module', 'db', '-verbose', '-cookie', 'cf_clearance=<REDACTED>', '-header=Authorization=<REDACTED>',
                          '-cookies', '<REDACTED>', '-api_key', '<REDACTED>', '-proxy', '<REDACTED>', '--', '-cookie'], redact_args(cmd))
        dwn = self.downloader()
        dwn.save_cmdline(cmd)
        dwn.verbose = True
        log: list[str] = []
        with mock.patch.object(download, 'trace', lambda msg, *a, **kw: log.append(str(msg))), \
                mock.patch.object(download.sys, 'argv', ['lynxx', *cmd]):
            dwn._at_launch()
        self.assertIn('cf_clearance=<REDACTED>', log[0])
        self.assertNoSecret(log[0])
        self.assertEqual(cmd, dwn.cmdline, 'the saved command line itself is not changed')

    def test_verbose_base_args(self) -> None:
        # '-opt=value' forms with '/' in the value: redacted before the script path is shortened, nothing else is cut
        argv = ['/x/y/lynxx_cmd.py', '-module', 'db', '-proxy=http://user:p4ss@127.0.0.1:8080',
                '-header=Authorization=Bearer abc/SECRET-AUTH', '-cookie=cf_clearance=ab/SECRET-CF', '-api_key', 'K3Y,1', '--', 'a/b']
        dwn = self.downloader()
        dwn.save_cmdline(argv[1:])
        dwn.verbose = True
        log: list[str] = []
        with mock.patch.object(download, 'trace', lambda msg, *a, **kw: log.append(str(msg))), \
                mock.patch.object(download.sys, 'argv', argv):
            dwn._at_launch()
        self.assertNoSecret(log[0])
        base = next(line for line in log[0].splitlines() if line.startswith('Base args: '))
        self.assertEqual('Base args: lynxx_cmd.py -module db -proxy=<REDACTED> -header=Authorization=<REDACTED> '
                         '-cookie=cf_clearance=<REDACTED> -api_key <REDACTED> -- a/b', base)

    def test_sidecar_redaction(self) -> None:
        text = server._redact('My args: -module rn -cookie cf_clearance=SECRET-CF -header=Authorization=Bearer SECRET-AUTH '
                              '-headers {"User-Agent": "Mozilla/5.0 (X11) SECRET-JSON"} -api_key K3Y,1 -proxy http://u:p4ss@h:1 '
                              '-- tag\nnext line -cookie shm_user=SECRET-CF')
        self.assertNoSecret(text)
        for kept in ('-cookie cf_clearance=<REDACTED>', '-header=Authorization=<REDACTED>', '-headers <REDACTED>', '-- tag',
                     'next line -cookie shm_user=<REDACTED>'):
            self.assertIn(kept, text)

    def test_challenge_hint(self) -> None:
        self.assertIn('Xác minh trong Lynxx', CF_CHALLENGE_HINT)
        self.assertIn('Cài đặt → Kết nối', CF_CHALLENGE_HINT)
        self.assertIn('does not bypass', CF_CHALLENGE_HINT)
        self.assertLess(CF_CHALLENGE_HINT.index('Xác minh trong Lynxx'), CF_CHALLENGE_HINT.index('by hand'), 'the button first')

class OriginalFileTests(_Env):
    POST = ('<post id="1105" md5="0123456789abcdef0123456789abcdef" file_url="https://cdn.example/original/01/23/0123.png" '
            'sample_url="https://cdn.example/sample/01/23/sample-0123.jpg" rating="g" tags="a c">')

    def indexed(self, dwn, link: str) -> list:
        """index records written for a download of link (the file is not requested)"""
        records: list = []

        class Writer:
            failed = 0

            def add(self, record) -> None:
                records.append(record)

            def close(self) -> bool:
                return True

        def download_file(_link, _item_id, dest, _mode, _orig_date):
            dest.write_bytes(b'x')
            res = network.FileDownloadResult()
            res.file_size = res.expected_size = 1
            return res
        dwn._index_on, dwn.download_mode, dwn.download_file = True, DownloadModes.FULL, download_file
        info = ItemInfo()
        info.id, info.md5, info.ext = '1105', '0123456789abcdef0123456789abcdef', 'png'
        dwn.item_info_dict_all['1105'] = info
        dwn._item_tls.raw = self.POST
        with mock.patch.object(download, 'IndexWriter', Writer), mock.patch.object(download, 'trace'):
            dwn._download(link, '1105', self.dest / f'1105.{link.rsplit(".", 1)[1]}')
        return records

    def test_a_sample_is_recorded_without_the_md5(self) -> None:
        dwn = self.parsed('a', '-lowres')
        self.assertEqual(('https://cdn.example/sample/01/23/sample-0123.jpg', 'jpg'), dwn._get_image_address(self.POST))
        rec = self.indexed(dwn, 'https://cdn.example/sample/01/23/sample-0123.jpg')
        self.assertEqual(1, len(rec))
        self.assertEqual(('', 'jpg'), (rec[0].md5, rec[0].ext), 'not the original: no md5 (-skip_known, md5: searches)')
        self.assertEqual('0123456789abcdef0123456789abcdef', dwn.item_info_dict_all['1105'].md5, 'the info dump keeps the site\'s md5')
        for link in ('https://cdn.example/original/01/23/0123.png', 'https://mirror.example/data/0123.png?x=1'):
            with self.subTest(link=link):
                dwn = self.parsed('a')
                rec = self.indexed(dwn, link)
                self.assertEqual('0123456789abcdef0123456789abcdef', rec[0].md5, 'the original (another host / folder): its md5')
        dwn = self.parsed('a')
        dwn._extract_file_url = lambda _h: ('', '')
        self.assertEqual('0123456789abcdef0123456789abcdef', self.indexed(dwn, 'https://x.example/other.jpg')[0].md5,
                         'the original\'s address unknown: the md5 as before')

    def test_gelbooru_lowres_gets_the_sample(self) -> None:
        from lynxx.download_gelbooru import DownloaderGelbooru as Gb
        post = ('<post height="1" score="1" file_url="https://img.example/images/1/abc.png" parent_id="" '
                'sample_url="https://img.example/samples/1/sample_abc.jpg?5" id="1" md5="abc"/>')
        dwn = SimpleNamespace(low_res=True, _extract_file_url=Gb._extract_file_url, _extract_sample_url=Gb._extract_sample_url)
        self.assertEqual(('https://img.example/samples/1/sample_abc.jpg?5', 'jpg'), Gb._get_image_address(dwn, post))
        for no_sample in (post.replace('sample_url="https://img.example/samples/1/sample_abc.jpg?5"', 'sample_url=""'),
                          post.replace(' sample_url="https://img.example/samples/1/sample_abc.jpg?5"', '')):
            with self.subTest(no_sample=no_sample[-60:]), mock.patch('lynxx.download_gelbooru.trace') as tr:
                self.assertEqual(('https://img.example/images/1/abc.png', 'png'), Gb._get_image_address(dwn, no_sample))
                self.assertFalse(tr.called, 'no sample is normal: no warning')
        dwn.low_res = False
        self.assertEqual(('https://img.example/images/1/abc.png', 'png'), Gb._get_image_address(dwn, post))
        video = post.replace('abc.png', 'abc.mp4')
        dwn.low_res = True
        self.assertEqual(('https://img.example/images/1/abc.mp4', 'mp4'), Gb._get_video_address(dwn, video), 'a video: its file')


class CookieDomainTests(_Env):
    @staticmethod
    def cookie(session, url: str) -> str:
        return session.prepare_request(requests.Request('GET', url)).headers.get('Cookie', '')

    def test_cookies_reach_the_site_only(self) -> None:
        dwn = self.parsed('a', '-cookie', 'cf_clearance=SECRET-CF', '-cookies', '{"shm_user": "bob"}')
        for for_download in (False, True):
            s = dwn.make_session(for_download=for_download)
            for url in ('https://danbooru.donmai.us/posts.json?tags=a', 'https://cdn.danbooru.donmai.us/x.jpg'):
                with self.subTest(url=url):
                    self.assertIn('cf_clearance=SECRET-CF', self.cookie(s, url))
                    self.assertIn('shm_user=bob', self.cookie(s, url))
            for url in ('https://cdn.other-host.net/v.mp4', 'https://donmai.us.evil.example/', 'https://cdn.donmai.us/x.jpg'):
                with self.subTest(url=url):
                    self.assertEqual('', self.cookie(s, url), 'another host: no cookie')

    def test_site_host_without_www_or_api(self) -> None:
        dwn = self.parsed('a', '-cookie', 'cf_clearance=SECRET-CF')
        for site, domain in (('https://www.zerochan.net/', '.zerochan.net'), ('https://api.rule34.xxx/', '.rule34.xxx'),
                             ('https://rule34.xyz/', '.rule34.xyz'), ('https://api.com/', '.api.com'), ('', ''),
                             ('http://127.0.0.1:8080/', ''), ('http://localhost:1/', ''), ('https://[::1]/', '')):
            with self.subTest(site=site), mock.patch.object(DownloaderDb, '_get_sitename', return_value=site):
                self.assertEqual(domain, dwn._cookie_domain())
        with mock.patch.object(DownloaderDb, '_get_sitename', return_value='https://api.rule34.xxx/'):
            s = dwn.make_session()
        for url in ('https://api.rule34.xxx/index.php', 'https://rule34.xxx/index.php?page=favorites', 'https://wimg.rule34.xxx/1.jpg'):
            self.assertIn('cf_clearance=SECRET-CF', self.cookie(s, url), url)
        self.assertEqual('', self.cookie(s, 'https://rule34xyz.b-cdn.net/x.mp4'))

    def test_no_site_keeps_the_cookies_for_every_host(self) -> None:
        dwn = self.parsed('a', '-cookie', 'k=v')
        with mock.patch.object(DownloaderDb, '_get_sitename', return_value='http://127.0.0.1:8123/'):
            s = dwn.make_session()
        self.assertEqual('k=v', self.cookie(s, 'http://127.0.0.1:8123/posts.json'))
        self.assertEqual('k=v', self.cookie(s, 'https://anywhere.example/'), 'as before: no site host to bind them to')

    def test_cookies_the_site_set_keep_their_domain(self) -> None:
        dwn = self.parsed('a', '-cookie', 'cf_clearance=SECRET-CF')
        jar = requests.cookies.RequestsCookieJar()
        jar.set('sid', '1', domain='.donmai.us', path='/')
        jar.update(dwn.add_cookies)  # wrap_request: the response's cookies, then the worker's own
        dwn.add_cookies = jar
        s = dwn.make_session()
        self.assertEqual('sid=1', self.cookie(s, 'https://cdn.donmai.us/x.jpg'), 'the site\'s own cookie: its own domain')
        self.assertIn('cf_clearance=SECRET-CF', self.cookie(s, 'https://danbooru.donmai.us/'))
        self.assertEqual('', self.cookie(s, 'https://cdn.other-host.net/'))


class ProxyForTests(TestCase):
    PROXY_VARS = ('http_proxy', 'https_proxy', 'all_proxy', 'no_proxy', 'ftp_proxy', 'socks_proxy')

    @contextlib.contextmanager
    def env(self, **values: str):
        """these proxy variables only: the system settings of this computer (Windows registry, macOS) are not read"""
        clean = {k: v for k, v in os.environ.items() if k.lower() not in self.PROXY_VARS}
        with mock.patch.dict(os.environ, {**clean, **values}, clear=True), \
                mock.patch.object(requests.utils, 'getproxies', urllib.request.getproxies_environment), \
                mock.patch.object(requests.utils, 'proxy_bypass', urllib.request.proxy_bypass_environment):
            yield

    def test_registered(self) -> None:
        self.assertIs(server.m_net_proxy_for, server.METHODS['net.proxyFor'])

    def test_the_proxy_requests_would_use(self) -> None:
        url = 'https://rule34.xyz/'
        cases = (({}, ''), ({'https_proxy': 'http://127.0.0.1:3128'}, 'http://127.0.0.1:3128'),
                 ({'HTTPS_PROXY': 'http://10.0.0.2:8080'}, 'http://10.0.0.2:8080'),
                 ({'http_proxy': 'http://127.0.0.1:1'}, ''),
                 ({'all_proxy': 'socks5://127.0.0.1:1080'}, 'socks5://127.0.0.1:1080'),
                 ({'https_proxy': 'http://127.0.0.1:3128', 'no_proxy': 'rule34.xyz'}, ''),
                 ({'https_proxy': 'http://127.0.0.1:3128', 'no_proxy': 'other.example'}, 'http://127.0.0.1:3128'))
        for env, proxy in cases:
            with self.subTest(env=env), self.env(**env):
                self.assertEqual({'proxy': proxy}, server.m_net_proxy_for({'url': url}))
                merged = requests.Session().merge_environment_settings(url, {}, None, None, None)['proxies']
                self.assertEqual(proxy, requests.utils.select_proxy(url, merged) or '', 'what a core session would use')

    def test_only_https_urls_of_public_hosts(self) -> None:
        for bad in (None, 5, '', 'rule34.xyz', 'http://rule34.xyz/', 'ftp://rule34.xyz/', 'https://user:pw@rule34.xyz/',
                    'https://localhost/', 'https://127.0.0.1/', 'https://192.168.1.2/', 'https://10.1.2.3:8443/', 'https://[::1]/',
                    'https://[fe80::1]/', 'https://router.lan/', 'https://printer.local/', 'https://intranet/',
                    'https://rule34.xyz:99999/', 'https://rule 34.xyz/', 'https://' + 'a' * 2050 + '.com/'):
            with self.subTest(bad=str(bad)[:40]), self.assertRaises(server.RpcError) as ctx:
                server.m_net_proxy_for({'url': bad})
            self.assertEqual(-32602, ctx.exception.code)
        with self.env():
            for good in ('https://www.zerochan.net/Hatsune+Miku', 'https://8.8.8.8/', 'https://rule34.xyz:443/x?y=1'):
                self.assertEqual({'proxy': ''}, server.m_net_proxy_for({'url': good}), good)


#
#
#########################################
