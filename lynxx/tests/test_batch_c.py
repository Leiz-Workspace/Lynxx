# coding=UTF-8
"""
Lynxx: regression tests for audit batch C (download pipeline, docs/reports/audit-2026-11-16.md), offline
- the streaming pipeline must schedule exactly the items of the classic pipeline (same order with 1 thread), on fake
  danbooru (json, id cursor) and rule34 (xml, offset pages, date bisection) websites
- file downloads: streaming to .part, keep-alive, resume with Range, permanent errors, Retry-After, cancel
Run: python -m unittest lynxx.tests.test_batch_c
"""
#########################################
#
#

import datetime
import http.server
import json
import os
import pathlib
import random
import socketserver
import tempfile
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from multiprocessing.dummy import current_process
from unittest import TestCase, mock
from urllib import parse as url_parse

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx.cmdargs import prepare_arglist  # noqa: E402
from lynxx.defines import DownloadModes  # noqa: E402
from lynxx.download_gelbooru import DownloaderGelbooru  # noqa: E402
from lynxx.downloaders import _make_downloader  # noqa: E402
from lynxx.extensions import Extensions  # noqa: E402
from lynxx.module import ProcModule  # noqa: E402
from lynxx.network import PART_SUFFIX, DownloadInterruptException, FileDownloadResult, HTMLContent  # noqa: E402

TAG_POOL = ('a', 'b', 'c', 'solo', '1girl', '1boy', 'scenery', 'sky')
BASE_DAY = datetime.datetime(2020, 1, 1, 12, 0, 0, tzinfo=datetime.UTC)


def _make_posts(n: int, seed: int) -> list[dict]:
    rng = random.Random(seed)
    posts = []
    pid = 1000
    day = 0
    for _ in range(n):
        pid += rng.randint(1, 7)
        day += rng.choice((0, 0, 1, 1, 2, 5))
        tags = sorted(set(rng.sample(TAG_POOL, rng.randint(1, 4))))
        posts.append({'id': pid, 'date': BASE_DAY + datetime.timedelta(days=day), 'tags': tags, 'score': rng.randint(0, 50),
                      'ext': rng.choice(('jpg', 'jpg', 'png', 'mp4'))})
    posts.reverse()  # newest first, like a booru
    return posts


def _match(post: dict, query: list[str]) -> bool:
    for q in query:
        if not q or ':' in q.lstrip('-'):
            continue
        if q.startswith('-'):
            if q[1:] in post['tags']:
                return False
        elif q not in post['tags']:
            return False
    return True


class FakeBooru:
    """Search results of a booru: tag / date filters, default (id desc) or score order, offset and id cursor pages"""

    def __init__(self, posts: list[dict], page_size: int) -> None:
        self.posts = posts
        self.page_size = page_size
        self.page_requests = 0
        self.cursor_requests = 0
        self.ignore_id_sort = False
        self.lock = threading.Lock()

    def search(self, tags: list[str]) -> list[dict]:
        res = [p for p in self.posts if _match(p, tags)]
        for t in tags:
            if t.startswith('date:'):
                lo, hi = (datetime.date.fromisoformat(x) for x in t[5:].split('..'))
                res = [p for p in res if lo <= p['date'].date() <= hi]
        for t in tags:
            if t.startswith('id:>'):
                res = [p for p in res if p['id'] > int(t[4:])]
            elif t.startswith('id:<'):
                res = [p for p in res if p['id'] < int(t[4:])]
        if 'order:score' in tags or 'sort:score' in tags:
            res.sort(key=lambda p: (-p['score'], -p['id']))
        elif 'sort:id:asc' in tags:
            with self.lock:
                self.cursor_requests += 1
            if not self.ignore_id_sort:
                res.sort(key=lambda p: p['id'])
        return res

    def _count_request(self) -> None:
        with self.lock:
            self.page_requests += 1

    # ---- danbooru json
    def db_fetch(self, url: str, **_) -> HTMLContent | None:
        u = url_parse.urlparse(url)
        q = url_parse.parse_qs(u.query)
        tags = q.get('tags', [''])[0].split(' ')
        res = self.search(tags)
        if u.path.endswith('counts/posts.json'):
            return HTMLContent(None, json.dumps({'counts': {'posts': len(res)}}).encode())
        self._count_request()
        limit = int(q.get('limit', [str(self.page_size)])[0])
        page = q.get('page', ['1'])[0]
        if page.startswith('a'):
            above = sorted((p for p in res if p['id'] > int(page[1:])), key=lambda p: p['id'])[:limit]
            chunk = sorted(above, key=lambda p: -p['id'])
        elif page.startswith('b'):
            chunk = [p for p in res if p['id'] < int(page[1:])][:limit]
        else:
            chunk = res[(int(page) - 1) * limit:int(page) * limit]
        out = [{'id': p['id'], 'created_at': p['date'].isoformat(), 'tag_string': ' '.join(p['tags']), 'file_ext': p['ext'],
                'md5': f'{p["id"]:032x}', 'file_url': f'https://cdn.example/{p["id"]}.{p["ext"]}', 'score': p['score'],
                'image_width': 10, 'image_height': 10, 'rating': 'g', 'has_children': False, 'parent_id': None, 'source': ''}
               for p in chunk]
        return HTMLContent(None, json.dumps(out).encode())

    # ---- rule34 dapi xml
    def rx_fetch(self, url: str, **_) -> HTMLContent | None:
        q = url_parse.parse_qs(url_parse.urlparse(url).query)
        tags = q.get('tags', [''])[0].split(' ')
        res = self.search(tags)
        limit = int(q.get('limit', [str(self.page_size)])[0])
        pid = int(q.get('pid', ['0'])[0])
        self._count_request()
        chunk = res[pid * limit:(pid + 1) * limit]
        posts = ''.join(
            f'<post height="10" score="{p["score"]}" file_url="https://cdn.example/images/{p["id"]}.{p["ext"]}" parent_id="" '
            f'sample_url="https://cdn.example/samples/{p["id"]}.jpg" preview_url="https://cdn.example/thumbs/{p["id"]}.jpg" '
            f'rating="e" tags=" {" ".join(p["tags"] + (["video"] if p["ext"] == "mp4" else []))} " id="{p["id"]}" width="10" '
            f'change="1" md5="{p["id"]:032x}" creator_id="1" has_children="false" '
            f'created_at="{p["date"].strftime("%a %b %d %H:%M:%S %z %Y")}" status="active" source="" has_notes="false" '
            f'has_comments="false" preview_width="10" preview_height="10"/>' for p in chunk)
        return HTMLContent(None, f'<?xml version="1.0" encoding="UTF-8"?><posts count="{len(res)}" offset="{pid * limit}">{posts}</posts>'
                           .encode())


def _run(module: int, server: FakeBooru, argv: list[str], streaming: bool, depth: int = 0) -> tuple[list[str], int]:
    """Runs a download with fake website and fake file downloads, returns (scheduled item ids in processing order, page requests)"""
    done: list[str] = []
    lock = threading.Lock()

    def fake_download(link: str, item_id: str, dest: pathlib.Path, mode, orig_date) -> FileDownloadResult:
        with lock:
            done.append(item_id)
        r = FileDownloadResult()
        r.file_size = r.expected_size = 1
        return r

    server.page_requests = 0
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {'LYNXX_STREAMING': '1' if streaming else '0'}):
        abbr = ProcModule.PROC_MODULES_NAMES_BY_ID[module]
        args = prepare_arglist(['-module', abbr, '-path', tmp, *argv])
        current_process().killed = False
        with _make_downloader(module) as dwn:
            dwn.fetch_html = server.db_fetch if module == ProcModule.DB else server.rx_fetch
            dwn.download_file = fake_download
            with mock.patch.object(type(dwn), '_get_items_per_page', return_value=server.page_size), \
                    mock.patch.object(type(dwn), '_get_max_search_depth', return_value=depth or dwn._get_max_search_depth()), \
                    mock.patch('lynxx.network.time.sleep'), mock.patch('lynxx.download.time.sleep'):
                dwn.launch_download(args)
            if dwn.launch_error:
                raise AssertionError(dwn.launch_error)
            assert dwn.streaming_active == streaming, 'pipeline selection'
    return done, server.page_requests


def _date_arg(days: int) -> str:
    return (BASE_DAY + datetime.timedelta(days=days)).strftime('%d-%m-%Y')


class StreamingEquivalenceTests(TestCase):
    """C1/C7: streaming (and danbooru id cursor) must produce the classic result"""

    @classmethod
    def setUpClass(cls) -> None:
        Extensions.ensure_loaded()
        posts = _make_posts(1300, 7)
        cls.db = FakeBooru(posts, 40)
        cls.rx = FakeBooru(posts, 50)
        last_day = (posts[0]['date'] - BASE_DAY).days
        cls.mid = last_day // 3

    def tearDown(self) -> None:
        ProcModule.set(ProcModule.RX)

    def _compare(self, module: int, server: FakeBooru, argv: list[str], threads: int, depth: int = 0) -> tuple[int, int]:
        full = ['-threads', str(threads), *argv]
        classic, classic_pages = _run(module, server, full, False)
        streamed, streamed_pages = _run(module, server, full, True, depth)
        msg = f'{ProcModule.PROC_MODULES_NAMES_BY_ID[module]} {full}'
        if threads == 1:
            self.assertEqual(classic, streamed, msg)
        else:
            self.assertEqual(sorted(classic), sorted(streamed), msg)
            self.assertEqual(len(set(streamed)), len(streamed), msg)
        return classic_pages, streamed_pages

    def test_matrix(self) -> None:
        cases = [
            ['--', 'a'],
            ['--', 'a', '-b'],
            ['-dlimit', '7', '--', 'a'],
            ['-dlimit', '7', '-reverse', '--', 'a'],
            ['-reverse', '--', 'solo'],
            ['-skip_vid', '-dlimit', '30', '--', 'sky'],
            ['-mindate', _date_arg(self.mid), '-maxdate', _date_arg(self.mid * 2), '--', 'a'],
            ['-mindate', _date_arg(self.mid), '-maxdate', _date_arg(self.mid * 2), '-dlimit', '5', '--', 'a'],
            ['-mindate', _date_arg(self.mid), '-dlimit', '5', '-reverse', '--', 'b'],
        ]
        for module, server, sort_tag in ((ProcModule.DB, self.db, 'order:score'), (ProcModule.RX, self.rx, 'sort:score')):
            for argv in [*cases, ['-dlimit', '9', '--', 'a', sort_tag], ['-dlimit', '9', '-reverse', '--', 'a', sort_tag]]:
                for threads in (1, 4):
                    with self.subTest(module=module, argv=argv, threads=threads):
                        self._compare(module, server, argv, threads)

    def test_dlimit_stops_scanning_early(self) -> None:
        for module, server in ((ProcModule.DB, self.db), (ProcModule.RX, self.rx)):
            classic_pages, streamed_pages = self._compare(module, server, ['-dlimit', '3', '--', 'a'], 1)
            self.assertLessEqual(streamed_pages, 3, module)
            self.assertGreater(classic_pages, 8, module)

    def test_gelbooru_cursor_and_fallback(self) -> None:
        # small results: page numbers (parallel); past the search depth: id cursor ('id:>N sort:id:asc' / 'id:<N'),
        # the result equals the classic pipeline run without the depth limit
        self.rx.cursor_requests = 0
        self._compare(ProcModule.RX, self.rx, ['--', 'a'], 1)
        self.assertEqual(0, self.rx.cursor_requests, 'page numbers below the search depth')
        for argv in (['--', 'a'], ['-reverse', '--', 'a'], ['-dlimit', '6', '--', 'b'], ['-dlimit', '6', '-reverse', '--', 'b']):
            with self.subTest(argv=argv):
                self.rx.cursor_requests = 0
                self._compare(ProcModule.RX, self.rx, argv, 4, depth=100)
                if '-reverse' not in argv:
                    self.assertGreater(self.rx.cursor_requests, 0)
        # a website ignoring 'sort:id:asc': detected on the first page, page numbers are used, result unchanged
        self.rx.ignore_id_sort = True
        try:
            self.rx.cursor_requests = 0
            self._compare(ProcModule.RX, self.rx, ['-dlimit', '4', '--', 'b'], 4, depth=100_000)
            self.assertEqual(0, self.rx.cursor_requests)
            with mock.patch.object(DownloaderGelbooru, '_prefer_id_cursor', return_value=True):
                self.rx.cursor_requests = 0
                self._compare(ProcModule.RX, self.rx, ['--', 'a'], 1)
                self.assertEqual(1, self.rx.cursor_requests, 'only the first cursor page is tried')
        finally:
            self.rx.ignore_id_sort = False

    def test_multi_task_query_uses_classic_pipeline(self) -> None:
        # danbooru splits OR groups into tasks: the classic pipeline must run (cross task ordering / dedupe)
        done, _ = _run(ProcModule.DB, self.db, ['-threads', '1', '--', '(a~b)', 'solo'], False)
        self.assertTrue(done)
        with self.assertRaises(AssertionError):
            _run(ProcModule.DB, self.db, ['-threads', '1', '--', '(a~b)', 'solo'], True)


# ---------------- file downloads (C2-C4) ----------------

class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    files: dict[str, bytes] = {}
    log: list[tuple[str, str, str]] = []
    behavior: dict[str, str] = {}
    connections: set[int] = set()

    def log_message(self, *args) -> None:
        pass

    def do_HEAD(self) -> None:
        self.log.append(('HEAD', self.path, ''))
        self.send_response(405)
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_GET(self) -> None:
        self.connections.add(id(self.connection))
        rng = self.headers.get('Range', '')
        self.log.append(('GET', self.path, rng))
        name = self.path.lstrip('/')
        mode = self.behavior.get(name, '')
        if mode == 'cf':
            self.send_response(403)
            self.send_header('cf-mitigated', 'challenge')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if mode == '404':
            self.send_response(404)
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        if mode.startswith('429') and sum(1 for x in self.log if x[1] == self.path) == 1:
            self.send_response(429)
            self.send_header('Retry-After', '1')
            self.send_header('Content-Length', '0')
            self.end_headers()
            return
        data = self.files[name]
        start = 0
        if rng and 'norange' not in mode:
            start = int(rng[6:].split('-')[0])
            end_s = rng[6:].split('-')[1]
            end = int(end_s) if end_s else len(data) - 1
            body = data[start:end + 1]
            self.send_response(206)
            self.send_header('Content-Range', f'bytes {start}-{start + len(body) - 1}/{len(data)}')
        else:
            body = data
            self.send_response(200)
            if 'norange' not in mode:
                self.send_header('Accept-Ranges', 'bytes')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('ETag', '"v1"')
        self.send_header('Last-Modified', 'Wed, 21 Oct 2015 07:28:00 GMT')
        self.end_headers()
        if mode in ('drop', 'norange-drop') and start == 0 and sum(1 for x in self.log if x[1] == self.path) == 1:
            self.wfile.write(body[:len(body) // 3])  # connection dropped mid file
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(body)


@contextmanager
def _server() -> Iterator[str]:
    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address) -> None:
            pass  # clients closing connections on purpose (cancel / keep-alive end)
    srv = Server(('127.0.0.1', 0), _Handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f'http://127.0.0.1:{srv.server_address[1]}/'
    finally:
        srv.shutdown()
        srv.server_close()


class FileDownloadTests(TestCase):
    def setUp(self) -> None:
        Extensions.ensure_loaded()
        self.tmp = tempfile.TemporaryDirectory()
        self.out = pathlib.Path(self.tmp.name)
        _Handler.files = {name: os.urandom(size) for name, size in (('a.jpg', 300_000), ('b.mp4', 5_000_000), ('c.jpg', 1000),
                                                                    ('d.jpg', 200_000), ('e.jpg', 120_000))}
        _Handler.log = []
        _Handler.behavior = {}
        _Handler.connections = set()
        self.dwn = _make_downloader(ProcModule.RX)  # rx: chunked (ranged) downloads for videos only
        current_process().killed = False
        self.dwn.reset_root_thread(current_process())
        self.dwn.retries = 4

    def tearDown(self) -> None:
        self.dwn.close_sessions()
        self.tmp.cleanup()

    def _get(self, base: str, name: str) -> FileDownloadResult:
        return self.dwn.download_file(f'{base}{name}', name, self.out / name, DownloadModes.FULL, True)

    def test_no_head_keep_alive_and_streaming(self) -> None:
        with _server() as base:
            for name in ('a.jpg', 'c.jpg', 'd.jpg'):
                res = self._get(base, name)
                self.assertEqual(len(_Handler.files[name]), res.file_size)
                self.assertEqual(_Handler.files[name], (self.out / name).read_bytes())
        self.assertFalse([x for x in _Handler.log if x[0] == 'HEAD'])
        self.assertEqual(3, len(_Handler.log))
        self.assertEqual(1, len(_Handler.connections), 'one keep-alive connection for 3 files')
        self.assertEqual(datetime.datetime(2015, 10, 21, 7, 28, tzinfo=datetime.UTC).timestamp(), (self.out / 'a.jpg').stat().st_mtime)

    def test_chunked_video_uses_ranges(self) -> None:
        with _server() as base:
            res = self._get(base, 'b.mp4')
        self.assertEqual(5_000_000, res.file_size)
        self.assertEqual(_Handler.files['b.mp4'], (self.out / 'b.mp4').read_bytes())
        self.assertEqual(3, len(_Handler.log))  # 2 MB windows
        self.assertTrue(all(x[2].startswith('bytes=') for x in _Handler.log))

    def test_resume_after_dropped_connection(self) -> None:
        _Handler.behavior = {'a.jpg': 'drop'}
        with _server() as base, mock.patch.object(self.dwn, 'sleep_interruptible'):
            res = self._get(base, 'a.jpg')
        self.assertEqual(_Handler.files['a.jpg'], (self.out / 'a.jpg').read_bytes())
        self.assertEqual(300_000, res.file_size)
        self.assertEqual(2, len(_Handler.log))
        resumed_at = int(_Handler.log[1][2][6:-1]) if _Handler.log[1][2].startswith('bytes=') else 0
        self.assertTrue(0 < resumed_at <= 100_000, f'resumed, not restarted: {_Handler.log[1][2]!r}')

    def test_restart_when_server_ignores_ranges(self) -> None:
        _Handler.behavior = {'a.jpg': 'norange-drop'}
        with _server() as base, mock.patch.object(self.dwn, 'sleep_interruptible'):
            res = self._get(base, 'a.jpg')
        self.assertEqual(_Handler.files['a.jpg'], (self.out / 'a.jpg').read_bytes())
        self.assertEqual(300_000, res.file_size)
        self.assertEqual('', _Handler.log[1][2], 'no Range when the server did not announce support')

    def test_404_not_retried(self) -> None:
        _Handler.behavior = {'c.jpg': '404'}
        with _server() as base:
            res = self._get(base, 'c.jpg')
        self.assertEqual(0, res.file_size)
        self.assertEqual(1, len(_Handler.log))
        self.assertFalse((self.out / 'c.jpg').exists())
        self.assertFalse((self.out / f'c.jpg{PART_SUFFIX}').exists())

    def test_429_retry_after_is_shared(self) -> None:
        _Handler.behavior = {'c.jpg': '429'}
        waits: list[float] = []
        with _server() as base, mock.patch.object(self.dwn, 'sleep_interruptible', side_effect=waits.append):
            res = self._get(base, 'c.jpg')
        self.assertEqual(1000, res.file_size)
        self.assertTrue(waits and 0.5 < waits[0] <= 1.0, waits)  # Retry-After: 1, waited through the shared backoff
        self.assertGreater(self.dwn._backoff_until['dl'], 0)

    def test_bot_challenge_streak_stops_requests(self) -> None:
        names = ['a.jpg', 'c.jpg', 'd.jpg', 'e.jpg', 'b.mp4']
        _Handler.behavior = dict.fromkeys(names, 'cf')
        with _server() as base:
            results = [self._get(base, n) for n in names]
        self.assertEqual([0] * 5, [r.file_size for r in results])
        self.assertEqual(3, len(_Handler.log), 'files after 3 challenges in a row are not requested')
        self.assertTrue(self.dwn.cf_challenge_hit)

    def test_download_pacing(self) -> None:
        with _server() as base, mock.patch.object(self.dwn, '_get_min_download_interval', return_value=0.1):
            t0 = time.monotonic()
            for name in ('a.jpg', 'c.jpg', 'd.jpg', 'e.jpg'):
                self._get(base, name)
            self.assertGreaterEqual(time.monotonic() - t0, 0.29, '4 files at >= 0.1 s intervals')

    def test_cancel_mid_file(self) -> None:
        calls = {'n': 0}
        real_is_killed = self.dwn.is_killed

        def killed_after_first_chunk() -> bool:
            calls['n'] += 1
            return calls['n'] > 3 or real_is_killed()
        with _server() as base, mock.patch.object(self.dwn, 'is_killed', side_effect=killed_after_first_chunk), \
                self.assertRaises(DownloadInterruptException):
            self._get(base, 'b.mp4')
        self.assertFalse((self.out / 'b.mp4').exists())
        self.assertFalse((self.out / f'b.mp4{PART_SUFFIX}').exists())


class ExecutorTests(TestCase):
    def setUp(self) -> None:
        Extensions.ensure_loaded()

    def test_window_and_cancel(self) -> None:
        with _make_downloader(ProcModule.RX) as dwn:
            root = current_process()
            root.killed = False
            dwn.reset_root_thread(root)
            dwn.maxthreads_items = 4
            started: list[int] = []
            lock = threading.Lock()

            def work(i: int) -> None:
                with lock:
                    started.append(i)
                    if len(started) == 10:
                        root.killed = True
                time.sleep(0.05)
            from lynxx.defines import ThreadInterruptException
            t0 = time.monotonic()
            with self.assertRaises(ThreadInterruptException):
                dwn._run_parallel('dl', work, ((i,) for i in range(100_000)))
            dwn._shutdown_executors()
            root.killed = False
            self.assertLess(time.monotonic() - t0, 2.0)
            self.assertLess(len(started), 40, 'queued work dropped on cancel, at most window + in flight ran')

    def test_ordered_results(self) -> None:
        with _make_downloader(ProcModule.RX) as dwn:
            current_process().killed = False
            dwn.reset_root_thread(current_process())
            dwn.maxthreads_items = 4
            out: list[int] = []
            dwn._run_parallel('info', lambda i: (time.sleep(0.001 * (i % 3)), i)[1], ((i,) for i in range(200)), ordered=True,
                              on_result=lambda f: out.append(f.result()))
            dwn._shutdown_executors()
            self.assertEqual(list(range(200)), out)


class LazyHtmlTests(TestCase):
    def test_json_page_is_not_parsed(self) -> None:
        page = HTMLContent(None, b'[{"id": 1}]')
        self.assertIsNone(page._html)
        self.assertEqual(b'[{"id": 1}]', page.raw)
        self.assertEqual('posts', HTMLContent(None, b'<posts count="3"></posts>').html.find('posts').name)


class CursorAddressTests(TestCase):
    def test_db_cursor_addresses(self) -> None:
        Extensions.ensure_loaded()
        with _make_downloader(ProcModule.DB) as dwn:
            dwn.url = 'https://danbooru.donmai.us/posts.json?tags=a&limit=200'
            self.assertTrue(dwn.url.endswith('&limit=200'))
            self.assertTrue(dwn._form_cursor_page_address(None, True).endswith('&page=a0'))
            self.assertTrue(dwn._form_cursor_page_address(None, False).endswith('&page=1'))
            self.assertTrue(dwn._form_cursor_page_address(123, True).endswith('&page=a123'))
            self.assertTrue(dwn._form_cursor_page_address(123, False).endswith('&page=b123'))
            self.assertTrue(dwn._supports_id_cursor())
        with _make_downloader(ProcModule.RX) as dwn:
            dwn.tags_str_arr = ['a+b']
            self.assertTrue(dwn._supports_id_cursor())
            dwn.cursor_base_tags = 'a+b'
            self.assertIn('tags=a+b+id:>77+sort:id:asc', dwn._form_cursor_page_address(77, True))
            self.assertIn('tags=a+b+id:<77', dwn._form_cursor_page_address(77, False))
            dwn.tags_str_arr = ['a+sort:score']
            self.assertFalse(dwn._supports_id_cursor())
            dwn.tags_str_arr = ['a']
            dwn.date_min = '01-01-2024'
            self.assertFalse(dwn._supports_id_cursor(), 'date filters need page numbers')

#
#
#########################################
