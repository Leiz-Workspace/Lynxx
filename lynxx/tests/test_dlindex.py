# coding=UTF-8
"""
Lynxx: local download index (lynxx/dlindex.py, docs/plans/search-engine.md phase 3), offline
- schema (WAL), upsert by file path, search by tag / module / post date with paging, stats
- the writer: batches on a background thread, add() never waits for the database, errors are reported, not raised
- several processes writing at the same time (CLI jobs running in parallel)
- download pipeline: finished files are recorded from the scan's post info; '-skip_known' (off by default) skips posts known by
  module + id or by md5 (any module) whose file is still on disk
- sidecar methods index.search / index.stats, desktop app wiring (allowlist, -skip_known)
- schema 3 (a schema 1 index is migrated, rows kept) and the trash of the "Đã tải" page: trash / restore / purge_trash / forget,
  trashed rows left out of the library (find_known, search, stats, rows_for_posts, posts_after), trash folders on the file's drive
  (EXDEV goes on to the next one), the sidecar methods and their desktop wiring (main only, confirmations, trashDays)
- moves that cannot finish (a locked index, an error, a process killed between the renames and the commit: recover_moves), a
  purge while a restore runs, a trash folder on an unplugged drive
Run: python -m unittest lynxx.tests.test_dlindex
"""
#########################################
#
#

import errno
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from multiprocessing.dummy import current_process
from unittest import TestCase, mock

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx import dlindex  # noqa: E402
from lynxx.cmdargs import prepare_arglist  # noqa: E402
from lynxx.defines import ItemInfo  # noqa: E402
from lynxx.downloaders import _make_downloader  # noqa: E402
from lynxx.extensions import Extensions  # noqa: E402
from lynxx.module import ProcModule  # noqa: E402
from lynxx.network import FileDownloadResult  # noqa: E402
from lynxx.tests.test_batch_c import FakeBooru, _make_posts  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _rec(module: str, post_id: int, path: str | pathlib.Path, *, md5: str = '', tags: str = '', score: int | None = None,
         rating: str = '', date: str = '', at: float = 0.0, size: int = 10) -> dlindex.IndexRecord:
    return dlindex.IndexRecord(module=module, post_id=str(post_id), path=str(path), md5=md5, tags=tags, score=score, rating=rating,
                               post_date=date, size=size, downloaded_at=at or time.time())


def _md5(n: int) -> str:
    return f'{n:032x}'


class _TempDir(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.db_path = self.tmp / 'index' / dlindex.INDEX_FILE_NAME

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _file(self, name: str, size: int = 3) -> pathlib.Path:
        p = self.tmp / 'files' / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b'x' * size)
        return p

    def _write(self, records: list[dlindex.IndexRecord]) -> None:
        with dlindex.DownloadIndex(self.db_path, create=True) as index:
            index.add_many(records)


class IndexDbTests(_TempDir):
    def test_readers_never_create_the_file(self) -> None:
        with dlindex.DownloadIndex(self.db_path) as index:
            self.assertEqual(0, index.search('a')['total'])
            self.assertEqual(({}, {}), index.find_known('rx', ['1'], [_md5(1)]))
            st = index.stats()
        self.assertFalse(st['exists'])
        self.assertEqual(0, st['files'])
        self.assertFalse(self.db_path.exists())

    def test_schema_wal_and_upsert_by_path(self) -> None:
        f = self._file('1.jpg')
        self._write([_rec('rx', 1, f, md5=_md5(1).upper(), tags='a b', score=5, rating='e', date='2020-01-02')])
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual('wal', conn.execute('PRAGMA journal_mode').fetchone()[0])
            self.assertEqual(dlindex.SCHEMA_VERSION, conn.execute('PRAGMA user_version').fetchone()[0])
            self.assertEqual([(1, _md5(1))], conn.execute('SELECT post_id, md5 FROM files').fetchall())
        finally:
            conn.close()
        # the same file downloaded again: one row, tags replaced
        self._write([_rec('rx', 1, f, md5=_md5(1), tags='b c')])
        with dlindex.DownloadIndex(self.db_path) as index:
            self.assertEqual(1, index.stats()['files'])
            self.assertEqual(0, index.search('a')['total'])
            self.assertEqual(1, index.search('c')['total'])
            item = index.search('b')['items'][0]
        self.assertEqual(('rx', '1', 'b c', '1.jpg', True), (item['module'], item['id'], item['tags'], item['name'], item['exists']))

    def test_wal_switch_tries_again_while_locked(self) -> None:
        class Conn:  # SQLite reports the lock at once (no busy wait) while another process switches the same new file
            def __init__(self) -> None:
                self.calls: list[str] = []

            def execute(self, sql: str) -> mock.Mock:
                self.calls.append(sql)
                if sql == 'PRAGMA journal_mode = WAL' and self.calls.count(sql) < 3:
                    raise sqlite3.OperationalError('database is locked')
                return mock.Mock(fetchone=mock.Mock(return_value=('delete',)))

        conn = Conn()
        with mock.patch('lynxx.dlindex.time.sleep') as sleep:
            dlindex.DownloadIndex._enable_wal(conn)
        self.assertEqual(3, conn.calls.count('PRAGMA journal_mode = WAL'))
        self.assertEqual(2, sleep.call_count)
        # other errors and a lock that outlasts BUSY_TIMEOUT are raised
        conn = Conn()
        conn.execute = mock.Mock(side_effect=sqlite3.OperationalError('disk I/O error'))
        with self.assertRaises(sqlite3.OperationalError):
            dlindex.DownloadIndex._enable_wal(conn)
        conn.execute = mock.Mock(side_effect=sqlite3.OperationalError('database is locked'))
        with mock.patch('lynxx.dlindex.time.sleep'), mock.patch('lynxx.dlindex.time.monotonic', side_effect=[0.0, 1.0, 99.0]), \
                self.assertRaises(sqlite3.OperationalError):
            dlindex.DownloadIndex._enable_wal(conn)

    def test_search(self) -> None:
        recs = [
            _rec('rx', 1, self._file('1.jpg'), md5=_md5(1), tags='a b solo', score=10, rating='e', date='2020-01-01', at=100),
            _rec('rx', 2, self.tmp / 'gone' / '2.jpg', md5=_md5(2), tags='a c', score=3, rating='s', date='2020-02-01', at=200),
            _rec('db', 3, self._file('3.png'), md5=_md5(3), tags='a b sky', score=None, rating='g', date='2020-03-01', at=300),
            _rec('db', 4, self._file('4.png'), tags='b skyline', score=50, rating='q', date='', at=400),
        ]
        self._write(recs)
        with dlindex.DownloadIndex(self.db_path) as index:
            def ids(text: str = '', **kw) -> list[str]:
                return [x['id'] for x in index.search(text, **kw)['items']]

            self.assertEqual(['3', '2', '1'], ids('a'))  # newest download first
            self.assertEqual(['3', '1'], ids('a b'))
            self.assertEqual(['2'], ids('a -b'))
            self.assertEqual(['4', '3'], ids('sky*'))
            self.assertEqual(['4'], ids('sky* -sky'))
            self.assertEqual(['1'], ids('rating:e'))
            self.assertEqual(['3'], ids(f'md5:{_md5(3)}'))
            self.assertEqual(['2'], ids('id:2'))
            self.assertEqual(['4', '3'], ids(module='db'))
            self.assertEqual(['3', '2'], ids(date_from='2020-02-01', date_to='2020-03-01'))
            self.assertEqual(['2', '1'], ids(date_to='15-02-2020'))  # core date format too; posts without a date are left out
            self.assertEqual(['4', '1', '2', '3'], ids(order='score'))  # unknown score last
            self.assertEqual(['1', '2', '3', '4'], ids(order='downloaded_asc'))
            self.assertEqual(['3', '2', '1', '4'], ids('', order='date'))
            page = index.search('', page=1, page_size=3)
            self.assertEqual((4, 1, 3, ['1']), (page['total'], page['page'], page['pageSize'], [x['id'] for x in page['items']]))
            self.assertEqual([], index.search('', page=5)['items'])
            gone = index.search('id:2')['items'][0]
            self.assertFalse(gone['exists'])
            self.assertEqual(0, index.search('nope*')['total'])

    def test_parse_search_terms(self) -> None:
        self.assertEqual([(False, 'tag', 'a'), (True, 'tag', 'b_c'), (False, 'prefix', 'sky'), (False, 'rating', 'e'),
                          (True, 'md5', _md5(9)), (False, 'id', '12'), (False, 'tag', 'id:x')],  # a bad meta value is a plain tag
                         dlindex.parse_search_terms(f'A -b_c sky* rating:explicit -md5:{_md5(9).upper()} id:12 * - a*b id:x'))
        self.assertEqual([(False, 'tag', 'a'), (False, 'tag', 'b')], dlindex.parse_search_terms(['a', 'b ']))
        self.assertEqual(dlindex.SEARCH_TERMS_MAX, len(dlindex.parse_search_terms(' '.join(f't{i}' for i in range(100)))))

    def test_find_known_needs_the_file_on_disk(self) -> None:
        self._write([
            _rec('rx', 1, self._file('1.jpg'), md5=_md5(1)),
            _rec('rx', 2, self.tmp / 'missing.jpg', md5=_md5(2)),
            _rec('rx', 3, self._file('3.jpg', size=0), md5=_md5(3)),  # an empty file is not a download
            _rec('db', 4, self._file('4.jpg'), md5=_md5(4)),
        ])
        with dlindex.DownloadIndex(self.db_path) as index:
            by_id, by_md5 = index.find_known('rx', ['1', '2', '3', '4', 'x'], [_md5(n) for n in range(1, 6)] + ['bad'])
        self.assertEqual(['1'], sorted(by_id))
        self.assertEqual(sorted([_md5(1), _md5(4)]), sorted(by_md5))

    def test_stats(self) -> None:
        self._write([
            _rec('rx', 1, self._file('a'), md5=_md5(1), tags='a b', size=100, at=10),
            _rec('rx', 1, self._file('b'), md5=_md5(1), tags='a', size=100, at=20),  # same post in another folder
            _rec('db', 2, self._file('c'), tags='c', size=50, at=30),
        ])
        with dlindex.DownloadIndex(self.db_path) as index:
            st = index.stats()
        self.assertTrue(st['exists'])
        self.assertEqual((3, 2, 2, 3, 250, 10, 30), (st['files'], st['posts'], st['withMd5'], st['tags'], st['bytes'], st['first'], st['last']))
        self.assertEqual([('rx', 2, 200), ('db', 1, 50)], [(m['module'], m['files'], m['bytes']) for m in st['modules']])
        self.assertGreater(st['dbBytes'], 0)

    def test_record_for(self) -> None:
        info = ItemInfo()
        info.id, info.md5, info.tags, info.score, info.date, info.ext = '77', _md5(77).upper(), 'B a  b', '+12', '06-01-2020', 'png'
        rec = dlindex.record_for('rx', info, 'rx_77', self.tmp / 'rx_77.jpg', 1234, raw='<post id="77" rating="explicit" />')
        self.assertEqual(('rx', '77', _md5(77), 'b a', 12, 'e', '2020-01-06', 'jpg', 1234),
                         (rec.module, rec.post_id, rec.md5, rec.tags, rec.score, rec.rating, rec.post_date, rec.ext, rec.size))
        self.assertTrue(os.path.isabs(rec.path))
        empty = dlindex.record_for('db', ItemInfo(), '5', 'x/5', 1, raw='{"rating":"q"}')
        self.assertEqual(('5', '', None, 'q', '', ''), (empty.post_id, empty.md5, empty.score, empty.rating, empty.post_date, empty.ext))
        self.assertEqual('g', dlindex.rating_of('', 'x rating:general'))
        self.assertEqual('2026-11-16', dlindex.norm_date('2026-11-16T04:24:19.090-05:00'))


class IndexWriterTests(_TempDir):
    def _rows(self) -> int:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute('SELECT COUNT(*) FROM files').fetchone()[0]
        finally:
            conn.close()

    def test_batches_and_close_flushes(self) -> None:
        batches: list[tuple[int, str]] = []
        real = dlindex.DownloadIndex.add_many

        def spy(index, records):
            batches.append((len(records), threading.current_thread().name))
            return real(index, records)

        writer = dlindex.IndexWriter(self.db_path, batch_max=3, interval=60)
        with mock.patch.object(dlindex.DownloadIndex, 'add_many', spy):
            for i in range(7):
                writer.add(_rec('rx', i, self.tmp / f'{i}.jpg', tags='a'))
            self.assertTrue(writer.close(10))
        self.assertEqual((7, 0, ''), (writer.written, writer.failed, writer.error))
        self.assertEqual([3, 3, 1], [n for n, _ in batches])
        self.assertEqual({'dlindex'}, {name for _, name in batches})
        self.assertEqual(7, self._rows())
        writer.add(_rec('rx', 99, self.tmp / '99.jpg'))  # closed: ignored
        self.assertTrue(writer.close())
        self.assertEqual(7, self._rows())

    def test_add_never_waits_for_a_locked_database(self) -> None:
        self._write([_rec('rx', 0, self.tmp / '0.jpg')])
        blocker = sqlite3.connect(self.db_path, isolation_level=None)
        blocker.execute('BEGIN IMMEDIATE')  # another process writing
        writer = dlindex.IndexWriter(self.db_path, batch_max=1, interval=0)
        try:
            t0 = time.monotonic()
            for i in range(1, 6):
                writer.add(_rec('rx', i, self.tmp / f'{i}.jpg'))
            self.assertLess(time.monotonic() - t0, 0.5)
            time.sleep(0.3)
            self.assertEqual(0, writer.written)
        finally:
            blocker.execute('COMMIT')
            blocker.close()
        self.assertTrue(writer.close(20))
        self.assertEqual((5, 0), (writer.written, writer.failed))
        self.assertEqual(6, self._rows())

    def test_errors_are_counted_not_raised(self) -> None:
        not_a_dir = self._file('plain-file')
        writer = dlindex.IndexWriter(not_a_dir / 'index' / 'downloads.sqlite', interval=0)
        with mock.patch('lynxx.dlindex.trace') as trace:
            writer.add(_rec('rx', 1, self.tmp / '1.jpg'))
            writer.add(_rec('rx', 2, self.tmp / '2.jpg'))
            self.assertTrue(writer.close(10))
        self.assertEqual((0, 2), (writer.written, writer.failed))
        self.assertTrue(writer.error)
        self.assertEqual(1, trace.call_count)  # reported once

    def test_a_failing_report_does_not_stop_the_writer(self) -> None:
        # outside the app the logger has no window and raises: the thread still counts every record and finishes
        not_a_dir = self._file('plain-file')
        writer = dlindex.IndexWriter(not_a_dir / 'index' / 'downloads.sqlite', interval=0)
        with mock.patch('lynxx.dlindex.trace', side_effect=AssertionError('Logger window is uninitialized!')):
            for i in range(3):
                writer.add(_rec('rx', i, self.tmp / f'{i}.jpg'))
            self.assertTrue(writer.close(10))
        self.assertEqual((0, 3), (writer.written, writer.failed))


WRITER_SCRIPT = r'''
import sys, time
from lynxx import dlindex
path, module, count = sys.argv[1], sys.argv[2], int(sys.argv[3])
w = dlindex.IndexWriter(path, batch_max=7, interval=0)
for i in range(count):
    w.add(dlindex.IndexRecord(module=module, post_id=str(i), path=f'{path}.{module}.{i}', md5=f'{i:032x}',
                              tags=f'shared t{i % 13} {module}', downloaded_at=time.time()))
    if i % 25 == 0:
        time.sleep(0.01)
ok = w.close(60)
print(w.written, w.failed, w.error, ok)
sys.exit(0 if ok and w.written == count and not w.failed else 1)
'''


OPEN_SCRIPT = r'''
import sys, time
from lynxx import dlindex
start = float(sys.argv[2])
while time.time() < start:
    time.sleep(0.001)
with dlindex.DownloadIndex(sys.argv[1], create=True) as index:
    print(index.stats()['files'])
'''


class MultiProcessTests(_TempDir):
    def test_concurrent_first_open(self) -> None:
        # several processes open a new index at the same moment: every one switches it to WAL or finds it switched
        env = {**os.environ, 'PYTHONPATH': str(ROOT), 'LYNXX_DATA_DIR': str(self.tmp)}
        start = time.time() + 1.5
        procs = [subprocess.Popen((sys.executable, '-c', OPEN_SCRIPT, str(self.db_path), str(start)), cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(8)]
        for p in procs:
            out, err = p.communicate(timeout=120)
            self.assertEqual((0, '0'), (p.returncode, out.strip()), err[-2000:])
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual('wal', conn.execute('PRAGMA journal_mode').fetchone()[0])
        finally:
            conn.close()

    def test_concurrent_writers(self) -> None:
        count, modules = 150, ('p1', 'p2', 'p3', 'p4')
        env = {**os.environ, 'PYTHONPATH': str(ROOT), 'LYNXX_DATA_DIR': str(self.tmp)}
        procs = [subprocess.Popen((sys.executable, '-c', WRITER_SCRIPT, str(self.db_path), m, str(count)), cwd=ROOT, env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for m in modules]
        for p in procs:
            out, err = p.communicate(timeout=180)
            self.assertEqual(0, p.returncode, f'{out}\n{err[-2000:]}')
        with dlindex.DownloadIndex(self.db_path) as index:
            st = index.stats()
            self.assertEqual(count * len(modules), st['files'])
            self.assertEqual(dict.fromkeys(modules, count), {m['module']: m['files'] for m in st['modules']})
            self.assertEqual(count * len(modules), index.search('shared')['total'])
            self.assertEqual(count, index.search('p3')['total'])
            self.assertEqual(len(range(5, count, 13)) * len(modules), index.search('t5')['total'])
            self.assertEqual(13 + 1 + len(modules), st['tags'])


# ---------------- download pipeline ----------------

class PipelineTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        Extensions.ensure_loaded()
        posts = _make_posts(160, 11)
        cls.db = FakeBooru(posts, 40)
        cls.rx = FakeBooru(posts, 50)
        cls.posts = {p['id']: p for p in posts}

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': str(self.tmp / 'data'), dlindex.ENV_DLINDEX: '1'})
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()
        ProcModule.set(ProcModule.RX)

    def _run(self, module: int, dest: str, argv: list[str], streaming: bool = True) -> tuple[list[str], object]:
        """Fake website and fake (but real on disk) file downloads, returns (downloaded item ids, downloader)"""
        done: list[str] = []
        lock = threading.Lock()

        def fake_download(link: str, item_id: str, path: pathlib.Path, mode, orig_date) -> FileDownloadResult:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'abc')
            with lock:
                done.append(item_id)
            r = FileDownloadResult()
            r.file_size = r.expected_size = 3
            return r

        abbr = ProcModule.PROC_MODULES_NAMES_BY_ID[module]
        server = self.db if module == ProcModule.DB else self.rx
        ProcModule.set(module)
        args = prepare_arglist(['-module', abbr, '-path', str(self.tmp / dest), *argv])
        current_process().killed = False
        with mock.patch.dict(os.environ, {'LYNXX_STREAMING': '1' if streaming else '0'}), _make_downloader(module) as dwn:
            dwn.fetch_html = server.db_fetch if module == ProcModule.DB else server.rx_fetch
            dwn.download_file = fake_download
            with mock.patch.object(type(dwn), '_get_items_per_page', return_value=server.page_size), \
                    mock.patch('lynxx.network.time.sleep'), mock.patch('lynxx.download.time.sleep'):
                dwn.launch_download(args)
            if dwn.launch_error:
                raise AssertionError(dwn.launch_error)
            self.assertEqual(streaming, dwn.streaming_active)
        return done, dwn

    def _index(self) -> dlindex.DownloadIndex:
        return dlindex.DownloadIndex(self.tmp / 'data' / dlindex.INDEX_DIR_NAME / dlindex.INDEX_FILE_NAME)

    def test_finished_files_are_recorded_off_the_download_threads(self) -> None:
        threads: list[str] = []
        real = dlindex.DownloadIndex.add_many

        def spy(index, records):
            threads.append(threading.current_thread().name)
            return real(index, records)

        with mock.patch.object(dlindex.DownloadIndex, 'add_many', spy):
            done, _ = self._run(ProcModule.DB, 'one', ['-threads', '4', '--', 'a'])
        self.assertTrue(done)
        self.assertEqual({'dlindex'}, set(threads))
        with self._index() as index:
            res = index.search('', module='db', page_size=200)
        self.assertEqual(len(done), res['total'])
        for item in res['items']:
            post = self.posts[int(item['id'])]
            self.assertEqual(_md5(post['id']), item['md5'])
            self.assertEqual(' '.join(post['tags']), item['tags'])
            self.assertEqual((post['score'], 'g', post['date'].date().isoformat()), (item['score'], item['rating'], item['date']))
            self.assertTrue(item['exists'])
            # macOS: the temp folder is reached through a link (/var -> /private/var), the index keeps the real path
            self.assertEqual(os.path.realpath(self.tmp / 'one'), os.path.realpath(os.path.dirname(item['path'])))

    def test_skip_known_is_off_by_default(self) -> None:
        first, _ = self._run(ProcModule.DB, 'one', ['--', 'sky'])
        again, dwn = self._run(ProcModule.DB, 'two', ['--', 'sky'])
        self.assertEqual(sorted(first), sorted(again))
        self.assertEqual(0, dwn.known_skipped_count)

    def test_skip_known_by_module_and_id(self) -> None:
        for streaming in (True, False):
            with self.subTest(streaming=streaming), mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': str(self.tmp / f'data{streaming}')}):
                first, _ = self._run(ProcModule.DB, f'one{streaming}', ['--', 'a'], streaming)
                self.assertGreater(len(first), 4)
                # two of the files are gone: those are downloaded again, the others are skipped
                for name in sorted(os.listdir(self.tmp / f'one{streaming}'))[:2]:
                    os.unlink(self.tmp / f'one{streaming}' / name)
                again, dwn = self._run(ProcModule.DB, f'two{streaming}', ['-skip_known', '--', 'a'], streaming)
                self.assertEqual(2, len(again))
                self.assertEqual(len(first) - 2, dwn.known_skipped_count)

    def test_skip_known_by_md5_across_modules(self) -> None:
        first, _ = self._run(ProcModule.DB, 'db', ['--', 'sky'])
        done, dwn = self._run(ProcModule.RX, 'rx', ['-skip_known', '--', 'sky'])
        self.assertEqual([], done)
        self.assertEqual(len(first), dwn.known_skipped_count)
        done, _ = self._run(ProcModule.RX, 'rx2', ['--', 'sky'])
        self.assertEqual(sorted(first), sorted(done))

    def test_prefixed_file_names_record_plain_ids(self) -> None:
        done, _ = self._run(ProcModule.DB, 'one', ['-prefix', '--', 'solo'])
        self.assertTrue(all(x.startswith('db_') for x in done))
        with self._index() as index:
            ids = sorted(x['id'] for x in index.search('solo', page_size=200)['items'])
        self.assertEqual(sorted(x[3:] for x in done), ids)
        again, dwn = self._run(ProcModule.DB, 'two', ['-prefix', '-skip_known', '--', 'solo'])
        self.assertEqual(([], len(done)), (again, dwn.known_skipped_count))

    def test_skip_known_without_an_index(self) -> None:
        done, dwn = self._run(ProcModule.DB, 'one', ['-skip_known', '--', 'sky'])
        self.assertTrue(done)
        self.assertEqual(0, dwn.known_skipped_count)

    def test_recording_can_be_disabled(self) -> None:
        with mock.patch.dict(os.environ, {dlindex.ENV_DLINDEX: '0'}):
            done, _ = self._run(ProcModule.DB, 'one', ['--', 'sky'])
        self.assertTrue(done)
        self.assertFalse((self.tmp / 'data' / dlindex.INDEX_DIR_NAME).exists())


# ---------------- schema 2, the trash ----------------

class TrashTests(_TempDir):
    def setUp(self) -> None:
        super().setUp()
        self.trash_dir = self.tmp / dlindex.TRASH_DIR_NAME
        self.root = self.tmp / 'files'

    def _index(self) -> dlindex.DownloadIndex:
        return dlindex.DownloadIndex(self.db_path)

    def _fill(self, n: int = 3) -> list[pathlib.Path]:
        files = [self._file(f'rx_{i}.jpg') for i in range(1, n + 1)]
        self._write([_rec('rx', i, f, md5=_md5(i), tags=f'common t{i}', size=100, at=100 + i) for i, f in enumerate(files, 1)])
        return files

    def _row(self, path: str | pathlib.Path) -> dict:
        with self._index() as index:
            return index.rows_for_paths([str(path)])[str(path)]

    @staticmethod
    def _table(path: pathlib.Path) -> list[tuple]:
        conn = sqlite3.connect(path)
        try:
            return [tuple(r[1:]) for r in conn.execute('PRAGMA table_info(files)')]
        finally:
            conn.close()

    def test_a_schema_1_index_is_migrated(self) -> None:
        f = self._file('rx_1.jpg')
        self.db_path.parent.mkdir(parents=True)
        conn = sqlite3.connect(self.db_path)
        try:
            for statement in dlindex.SCHEMA:
                conn.execute(statement)
            conn.execute("INSERT INTO files (module, post_id, md5, path, tags, downloaded_at) VALUES ('rx', 1, ?, ?, 'a', 5)", (_md5(1), str(f)))
            conn.execute("INSERT INTO tag_names (name) VALUES ('a')")
            conn.execute('INSERT INTO file_tags (tag_id, file_id) VALUES (1, 1)')
            conn.execute('PRAGMA user_version = 1')
            conn.commit()
        finally:
            conn.close()
        with self._index() as index:  # a reader migrates it in place
            items = index.search('a')['items']
        self.assertEqual([(str(f), '1', None, '', True)], [(x['path'], x['id'], x['trashedAt'], x['origPath'], x['exists']) for x in items])
        conn = sqlite3.connect(self.db_path)
        try:
            self.assertEqual(dlindex.SCHEMA_VERSION, conn.execute('PRAGMA user_version').fetchone()[0])
            self.assertLessEqual({'files_trashed_at', 'files_moving_to'}, {r[1] for r in conn.execute('PRAGMA index_list(files)')})
            self.assertEqual([(1, None, None, None)], conn.execute('SELECT id, trashed_at, orig_path, moving_to FROM files').fetchall())
        finally:
            conn.close()
        migrated = self._table(self.db_path)
        self.assertEqual(['trashed_at', 'orig_path', 'moving_to'], [c[0] for c in migrated[-3:]])
        new = self.tmp / 'new' / dlindex.INDEX_FILE_NAME  # a new index: the same table
        with dlindex.DownloadIndex(new, create=True) as index:
            index.add_many([_rec('rx', 1, f)])
        self.assertEqual(migrated, self._table(new))

    def test_trash_moves_the_file_and_marks_the_row(self) -> None:
        a, b, _ = self._fill()
        before = self._row(a)
        with self._index() as index:
            res = index.trash([str(a), str(b)], now=1000.0)
        self.assertEqual(([], []), (res['failed'], res['refused']))
        moved = {x['path']: x['trashPath'] for x in res['trashed']}
        self.assertEqual({str(a), str(b)}, set(moved))
        trash_a = pathlib.Path(moved[str(a)])
        self.assertFalse(a.exists())
        # the data dir is on the same drive: its trash folder, named '<row id>_<name>'
        self.assertEqual((self.trash_dir, f'1_{a.name}', b'xxx'), (trash_a.parent, trash_a.name, trash_a.read_bytes()))
        row = self._row(trash_a)
        self.assertEqual((str(trash_a), str(a), 1000.0, a.name, True), (row['path'], row['origPath'], row['trashedAt'], row['name'], row['exists']))
        kept = ('module', 'id', 'md5', 'tags', 'size', 'downloadedAt')
        self.assertEqual({k: before[k] for k in kept}, {k: row[k] for k in kept})

    def test_trashed_rows_leave_the_library(self) -> None:
        a, _, _ = self._fill()
        with self._index() as index:
            index.trash([str(a)], now=500.0)
            self.assertEqual(['3', '2'], [x['id'] for x in index.search('common')['items']])
            self.assertEqual(0, index.search('t1')['total'])
            trashed = index.search('common', trash=True, order='trashed')
            self.assertEqual((1, '1', str(a)), (trashed['total'], trashed['items'][0]['id'], trashed['items'][0]['origPath']))
            # a post whose file is in the trash is not known any more: it may be downloaded again
            by_id, by_md5 = index.find_known('rx', ['1', '2'], [_md5(1), _md5(2)])
            self.assertEqual((['2'], [_md5(2)]), (sorted(by_id), sorted(by_md5)))
            self.assertEqual(['2'], sorted(index.rows_for_posts('rx', ['1', '2'])))
            self.assertEqual([2, 3], [r[0] for r in index.posts_after('rx')])
            st = index.stats()
        self.assertEqual((2, 2, 2, 200, 102, 103), (st['files'], st['posts'], st['withMd5'], st['bytes'], st['first'], st['last']))
        self.assertEqual({'files': 1, 'bytes': 100, 'oldest': 500.0}, st['trash'])
        self.assertEqual([('rx', 2, 200)], [(m['module'], m['files'], m['bytes']) for m in st['modules']])
        with dlindex.DownloadIndex(self.tmp / 'none' / dlindex.INDEX_FILE_NAME) as index:
            self.assertEqual({'files': 0, 'bytes': 0, 'oldest': None}, index.stats()['trash'])

    def test_paths_the_index_does_not_know_are_refused(self) -> None:
        a, b = self._fill(2)
        stranger = self._file('stranger.jpg')
        gone = self.root / 'gone.jpg'
        self._write([_rec('rx', 9, gone)])
        with self._index() as index:
            res = index.trash([str(stranger), str(gone), str(self.root / 'nope.jpg'), str(a)])
            self.assertEqual([str(a)], [x['path'] for x in res['trashed']])
            self.assertEqual([str(stranger), str(gone), str(self.root / 'nope.jpg')], res['refused'])
            trash_a = res['trashed'][0]['trashPath']
            # already in the trash: by its old path (no row there any more) or its path in the trash
            self.assertEqual([str(a), trash_a], index.trash([str(a), trash_a])['refused'])
            # restore: only rows of the trash
            self.assertEqual([str(b), str(stranger)], index.restore([str(b), str(stranger)])['refused'])
        self.assertTrue(stranger.exists() and b.exists())

    def test_trash_folders(self) -> None:
        f = self._file('sub/x.jpg')
        hidden = dlindex.TRASH_HIDDEN_NAME
        real = dlindex._device
        with self._index() as index:
            self.assertEqual([str(self.trash_dir), str(self.root / hidden), str(f.parent / hidden)], index.trash_folders(str(f), [str(self.root)]))
            # the data dir on another drive: the root of the download folder that holds the file, else next to the file
            with mock.patch.object(dlindex, '_device', lambda p: -1 if p == str(self.trash_dir) else real(p)):
                self.assertEqual([str(self.root / hidden), str(f.parent / hidden)], index.trash_folders(str(f), [str(self.root)]))
                self.assertEqual([str(f.parent / hidden)], index.trash_folders(str(f), [str(self.tmp / 'elsewhere')]))
                self.assertEqual([str(f.parent / hidden)], index.trash_folders(str(f), [str(f.parent)]))
            self.assertEqual([], index.trash_folders(str(self.tmp / 'missing.jpg')))

    def test_a_cross_device_rename_goes_on_to_the_next_folder(self) -> None:
        a, b = self._fill(2)
        real = os.replace

        def replace(src, dst) -> None:  # the data dir is on another drive after all (a mount point)
            if str(dst).startswith(str(self.trash_dir)):
                raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))
            real(src, dst)

        def nowhere(src, dst) -> None:
            raise OSError(errno.EXDEV, os.strerror(errno.EXDEV))

        with self._index() as index:
            with mock.patch('lynxx.dlindex.os.replace', replace):
                res = index.trash([str(a)], roots=[str(self.root)])
            self.assertEqual([str(self.root / dlindex.TRASH_HIDDEN_NAME / f'1_{a.name}')], [x['trashPath'] for x in res['trashed']])
            with mock.patch('lynxx.dlindex.os.replace', nowhere):  # no folder takes it: never copied, the file and its row stay
                res = index.trash([str(b)], roots=[str(self.root)])
        self.assertEqual(([], [str(b)]), (res['trashed'], [x['path'] for x in res['failed']]))
        self.assertIn('OSError', res['failed'][0]['error'])
        self.assertTrue(b.exists())
        self.assertIsNone(self._row(b)['trashedAt'])

    def test_a_download_while_trashed_gets_its_own_row(self) -> None:
        a, = self._fill(1)
        with self._index() as index:
            trash_a = index.trash([str(a)])['trashed'][0]['trashPath']
        a.write_bytes(b'new file')  # the post is downloaded again into the same file name
        self._write([_rec('rx', 1, a, md5=_md5(1), tags='again')])
        with self._index() as index:
            st = index.stats()
            self.assertEqual((1, 1), (st['files'], st['trash']['files']))
            self.assertEqual({'1': str(a)}, index.find_known('rx', ['1'])[0])
            res = index.restore([trash_a])
            # the name is taken: the file of the trash comes back next to it
            restored = self.root / 'rx_1_1.jpg'
            self.assertEqual([{'path': trash_a, 'restoredPath': str(restored)}], res['restored'])
            self.assertEqual((2, 0), (index.stats()['files'], index.stats()['trash']['files']))
        self.assertEqual((b'new file', b'xxx'), (a.read_bytes(), restored.read_bytes()))
        self.assertFalse(os.path.exists(trash_a))

    def test_restore(self) -> None:
        f = self._file('sub/rx_5.jpg')
        self._write([_rec('rx', 5, f, tags='five')])
        with self._index() as index:
            trash_f = index.trash([str(f)])['trashed'][0]['trashPath']
            f.parent.rmdir()  # the folder is gone: created again
            self.assertEqual([str(f)], [x['restoredPath'] for x in index.restore([trash_f])['restored']])
            row = index.search('five')['items'][0]
            self.assertEqual((str(f), None, ''), (row['path'], row['trashedAt'], row['origPath']))
            # a file that is not in the index took the name meanwhile
            trash_f = index.trash([str(f)])['trashed'][0]['trashPath']
            f.write_bytes(b'other')
            self.assertEqual([str(f.parent / 'rx_5_1.jpg')], [x['restoredPath'] for x in index.restore([trash_f])['restored']])
            self.assertEqual(b'other', f.read_bytes())
            # the file left the trash by another way: it stays a row of the trash
            trash_f = index.trash([str(f.parent / 'rx_5_1.jpg')])['trashed'][0]['trashPath']
            os.remove(trash_f)
            res = index.restore([trash_f])
            self.assertEqual(([], [trash_f]), (res['restored'], [x['path'] for x in res['failed']]))
            self.assertEqual(1, index.stats()['trash']['files'])

    def test_purge_by_age_by_paths_and_all(self) -> None:
        a, b, c, d = self._fill(4)
        now = time.time()
        with self._index() as index:
            old = index.trash([str(a)], now=now - 10 * dlindex.DAY_SECONDS)['trashed'][0]['trashPath']
            mid = index.trash([str(b)], now=now - 3 * dlindex.DAY_SECONDS)['trashed'][0]['trashPath']
            index.trash([str(c), str(d)], now=now)
            res = index.purge_trash(older_than_days=7, now=now)
            self.assertEqual((1, 1, 3, []), (res['purged'], res['files'], res['bytes'], res['failed']))
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(mid))
            self.assertEqual(1, index.purge_trash(paths=[mid, str(c)])['purged'])  # c: its old path, not a path of the trash
            self.assertEqual(2, index.purge_trash(all=True)['purged'])
            self.assertEqual(0, index.stats()['trash']['files'])
            conn = index._connection()
            self.assertEqual((0, 0), (conn.execute('SELECT COUNT(*) FROM files').fetchone()[0],
                                      conn.execute('SELECT COUNT(*) FROM file_tags').fetchone()[0]))
            for bad in ({}, {'all': True, 'older_than_days': 1}, {'paths': [], 'all': True}):
                with self.subTest(bad=bad), self.assertRaises(ValueError):
                    index.purge_trash(**bad)
        self.assertEqual([], list(self.trash_dir.iterdir()))

    def test_purge_keeps_what_it_cannot_delete(self) -> None:
        a, b, c = self._fill(3)
        real_remove = os.remove
        with self._index() as index:
            ta, tb = [x['trashPath'] for x in index.trash([str(a), str(b)])['trashed']]
            real_remove(tb)  # deleted from the trash folder by hand: only the row goes
            # a row of the trash that points outside any trash folder: its row goes, the file is never deleted
            index._connection().execute('UPDATE files SET trashed_at = 1 WHERE path = ?', (str(c),))

            def remove(path) -> None:
                if path == ta:
                    raise PermissionError(errno.EACCES, 'in use')
                real_remove(path)

            with mock.patch('lynxx.dlindex.os.remove', remove):
                res = index.purge_trash(all=True)
            self.assertEqual((2, 0, 1), (res['purged'], res['files'], res['kept']))
            self.assertEqual([ta], [x['path'] for x in res['failed']])
            self.assertEqual([ta], [x['path'] for x in index.search('', trash=True)['items']])
        self.assertTrue(os.path.exists(ta) and c.exists())

    def test_purge_removes_an_empty_trash_folder(self) -> None:
        a, = self._fill(1)
        real = dlindex._device
        with self._index() as index, mock.patch.object(dlindex, '_device', lambda p: -1 if p == str(self.trash_dir) else real(p)):
            moved = index.trash([str(a)], roots=[str(self.root)])['trashed'][0]['trashPath']
            self.assertEqual(self.root / dlindex.TRASH_HIDDEN_NAME, pathlib.Path(moved).parent)
            self.assertEqual(1, index.purge_trash(all=True)['files'])
        self.assertFalse((self.root / dlindex.TRASH_HIDDEN_NAME).exists())

    def test_forget(self) -> None:
        a, b = self._fill(2)
        gone = self.root / 'gone.jpg'
        unknown = self.root / 'unknown.jpg'
        self._write([_rec('rx', 9, gone, tags='gone common')])
        with self._index() as index:
            res = index.forget([str(gone), str(a), str(unknown)])
            self.assertEqual((1, [str(a), str(unknown)]), (res['removed'], res['refused']))
            self.assertEqual(0, index.search('gone')['total'])
            tb = index.trash([str(b)])['trashed'][0]['trashPath']
            os.remove(tb)  # a row of the trash whose file vanished
            self.assertEqual(1, index.forget([tb])['removed'])
            self.assertEqual(0, index.stats()['trash']['files'])
        self.assertTrue(a.exists())

    def _moving(self) -> list[tuple]:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute('SELECT id, moving_to FROM files WHERE moving_to IS NOT NULL').fetchall()
        finally:
            conn.close()

    def test_a_locked_index_moves_nothing(self) -> None:
        # another writer holds the lock longer than BUSY_TIMEOUT: the error reaches the caller, and no file has left its place
        a, b = self._fill(2)
        other = sqlite3.connect(self.db_path, isolation_level=None)
        try:
            other.execute('BEGIN IMMEDIATE')
            with mock.patch.object(dlindex, 'BUSY_TIMEOUT', 0.2), self._index() as index:
                with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                    index.trash([str(a), str(b)])
        finally:
            other.execute('ROLLBACK')
        # the lock is taken between the two steps: the rows are marked, the renames cannot start
        real_plan = dlindex.DownloadIndex._plan_moves

        def plan_then_lock(index, *args):
            plans = real_plan(index, *args)
            other.execute('BEGIN IMMEDIATE')
            return plans
        try:
            with mock.patch.object(dlindex, 'BUSY_TIMEOUT', 0.2), mock.patch.object(dlindex.DownloadIndex, '_plan_moves', plan_then_lock), \
                    self._index() as index:
                with self.assertRaisesRegex(sqlite3.OperationalError, 'locked'):
                    index.trash([str(a), str(b)])
        finally:
            other.execute('ROLLBACK')
            other.close()
        self.assertTrue(a.exists() and b.exists())
        self.assertFalse(self.trash_dir.exists() and any(self.trash_dir.iterdir()))
        self.assertEqual(2, len(self._moving()), 'the marks could not be dropped either: the next call drops them')
        with self._index() as index:
            self.assertEqual(0, index.recover_moves())
            self.assertEqual([], self._moving())
            self.assertEqual((None, None), (self._row(a)['trashedAt'], self._row(b)['trashedAt']))
            self.assertEqual(2, len(index.trash([str(a), str(b)])['trashed']))

    def test_an_error_while_moving_puts_the_files_back(self) -> None:
        a, b, c = self._fill(3)
        real = dlindex._rename_first
        calls = []

        def rename(src, targets):
            calls.append(src)
            if len(calls) == 3:
                raise RuntimeError('disk gone')
            return real(src, targets)
        with self._index() as index, mock.patch.object(dlindex, '_rename_first', rename):
            with self.assertRaisesRegex(RuntimeError, 'disk gone'):
                index.trash([str(a), str(b), str(c)])
        self.assertTrue(a.exists() and b.exists() and c.exists())
        self.assertEqual([], list(self.trash_dir.iterdir()))
        self.assertEqual([], self._moving())
        self.assertTrue(all(self._row(f)['trashedAt'] is None for f in (a, b, c)))

    def _killed_during(self, method: str, paths: list[str], renames: int) -> None:
        """index.<method>(paths) in another process killed (no cleanup) right after its renames-th rename"""
        code = ('import json, os, sys\n'
                'from lynxx import dlindex\n'
                'real, done = os.replace, []\n'
                'def replace(src, dst):\n'
                '    real(src, dst)\n'
                '    done.append(dst)\n'
                f'    if len(done) == {renames:d}:\n'
                '        os._exit(3)\n'
                'os.replace = replace\n'
                'index = dlindex.DownloadIndex(sys.argv[1])\n'
                f'getattr(index, {method!r})(json.loads(sys.argv[2]))\n')
        import json
        r = subprocess.run((sys.executable, '-c', code, str(self.db_path), json.dumps(paths)), cwd=ROOT, capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(3, r.returncode, r.stderr)

    def test_a_killed_trash_is_finished_later(self) -> None:
        # the app quits (or crashes) while files move: they reached the trash, their rows did not follow
        files = self._fill(5)
        self._killed_during('trash', [str(f) for f in files], 3)
        moved = sorted(self.trash_dir.iterdir())
        self.assertEqual([f'{i}_rx_{i}.jpg' for i in (1, 2, 3)], [m.name for m in moved])
        self.assertEqual(5, len(self._moving()))
        with self._index() as index:
            # until recovered: the rows say the files are in the library (3 of them gone), the trash looks empty
            self.assertEqual(3, sum(not x['exists'] for x in index.search('common')['items']))
            self.assertEqual(0, index.search('common', trash=True)['total'])
            self.assertEqual(3, index.recover_moves())
            self.assertEqual([], self._moving())
            trashed = index.search('common', trash=True)['items']
            self.assertEqual(sorted(str(m) for m in moved), sorted(x['path'] for x in trashed))
            self.assertEqual(sorted(str(f) for f in files[:3]), sorted(x['origPath'] for x in trashed))
            self.assertTrue(all(x['exists'] for x in trashed))
            self.assertEqual([str(f) for f in files[3:]], sorted(x['path'] for x in index.search('common')['items']))
            restored = index.restore([str(m) for m in moved])['restored']  # the trash works with them as usual
        self.assertEqual(3, len(restored))
        self.assertTrue(all(f.exists() for f in files))

    def test_a_killed_restore_is_finished_later(self) -> None:
        files = self._fill(3)
        with self._index() as index:
            moved = [x['trashPath'] for x in index.trash([str(f) for f in files])['trashed']]
        self._killed_during('restore', moved, 2)
        self.assertEqual([True, True, False], [f.exists() for f in files])
        with mock.patch.object(dlindex.DownloadIndex, 'recover_moves', return_value=0), self._index() as index:
            # without recovery a purge would drop the rows of the files already back: they are left alone while marked
            self.assertEqual((0, 0), (index.purge_trash(all=True)['purged'], index.forget(moved[:2])['removed']))
        from lynxx import server
        with mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': str(self.tmp)}):
            self.assertEqual(2, server.METHODS['index.stats']({})['files'], 'the library page finishes the moves first')
        self.assertEqual([], self._moving())
        with self._index() as index:
            self.assertEqual(sorted(str(f) for f in files[:2]), sorted(x['path'] for x in index.search('common')['items']))
            self.assertEqual([moved[2]], [x['path'] for x in index.search('common', trash=True)['items']])

    def test_marks_without_a_move_are_dropped(self) -> None:
        # stopped after the rows were marked, before any rename: nothing moved, nothing to finish
        a, b = self._fill(2)
        with self._index() as index:
            conn = index._connection()
            res = {'refused': [], 'failed': []}
            self.assertEqual(2, len(index._plan_moves(conn, [str(a), str(b)], False, (), res)))
            self.assertEqual(2, len(self._moving()))
            self.assertEqual(0, index.recover_moves())
        self.assertEqual([], self._moving())
        self.assertTrue(a.exists() and b.exists())
        self.assertIsNone(self._row(a)['trashedAt'])

    def test_a_purge_while_a_restore_runs(self) -> None:
        # the automatic purge (another rpc thread) meets a file a restore has just renamed out of the trash: the purge must not
        # drop its row, or the restore puts the file back into the trash with no row (never shown, never purged)
        a, = self._fill(1)
        now = time.time()
        with self._index() as index:
            trash_a = index.trash([str(a)], now=now - 40 * dlindex.DAY_SECONDS)['trashed'][0]['trashPath']
        real = os.replace
        purges: list[dict] = []
        threads: list[threading.Thread] = []

        def purge() -> None:
            with self._index() as other:
                purges.append(other.purge_trash(older_than_days=30))

        def replace(src, dst) -> None:
            real(src, dst)
            if src == trash_a and not threads:
                threads.append(threading.Thread(target=purge))
                threads[0].start()
                time.sleep(0.3)  # the purge finds the file gone meanwhile
        with self._index() as index, mock.patch('lynxx.dlindex.os.replace', replace):
            res = index.restore([trash_a])
        threads[0].join(15)
        self.assertEqual(([str(a)], []), ([x['restoredPath'] for x in res['restored']], res['failed']))
        self.assertEqual(0, purges[0]['purged'])
        self.assertTrue(a.exists())
        self.assertFalse(os.path.exists(trash_a))
        self.assertEqual((str(a), None), (self._row(a)['path'], self._row(a)['trashedAt']))

    def test_a_trash_folder_on_an_unplugged_drive(self) -> None:
        # the download folder is on another drive than the data dir: its own '.lynxx-trash'. Unplugged when the trash is purged:
        # the rows stay (the files would stay on the drive with no row: never shown, never purged again)
        drive = self.tmp / 'files'
        a, = self._fill(1)
        real = dlindex._device
        now = time.time()
        with self._index() as index, mock.patch.object(dlindex, '_device', lambda p: -1 if p == str(self.trash_dir) else real(p)):
            trash_a = index.trash([str(a)], roots=[str(drive)], now=now - 40 * dlindex.DAY_SECONDS)['trashed'][0]['trashPath']
        self.assertEqual(drive / dlindex.TRASH_HIDDEN_NAME, pathlib.Path(trash_a).parent)
        away = self.tmp / 'unplugged'
        drive.rename(away)
        try:
            with self._index() as index:
                for res in (index.purge_trash(older_than_days=30), index.purge_trash(all=True), index.purge_trash(paths=[trash_a])):
                    self.assertEqual((0, 0, [(trash_a, dlindex.TRASH_UNREACHABLE)]),
                                     (res['purged'], res['files'], [(x['path'], x['error']) for x in res['failed']]))
                self.assertEqual([trash_a], index.forget([trash_a])['refused'])
                self.assertEqual(1, index.stats()['trash']['files'])
        finally:
            away.rename(drive)
        with self._index() as index:
            res = index.purge_trash(older_than_days=30)  # plugged in again: purged
            self.assertEqual((1, 1, []), (res['purged'], res['files'], res['failed']))
        self.assertFalse(os.path.exists(trash_a))

    def test_no_index(self) -> None:
        f = self._file('x.jpg')
        with self._index() as index:
            self.assertEqual([str(f)], index.trash([str(f)])['refused'])
            self.assertEqual([str(f)], index.restore([str(f)])['refused'])
            self.assertEqual([str(f)], index.forget([str(f)])['refused'])
            self.assertEqual(0, index.purge_trash(all=True)['purged'])
        self.assertFalse(self.db_path.exists())
        self.assertTrue(f.exists())


# ---------------- cli, sidecar, desktop app ----------------

class FrontendTests(TestCase):
    def test_cli_option(self) -> None:
        Extensions.ensure_loaded()
        self.assertFalse(prepare_arglist(['-module', 'rx', '--', 'a']).skip_known)
        self.assertTrue(prepare_arglist(['-module', 'rx', '-skip_known', '--', 'a']).skip_known)
        parsed = prepare_arglist(['-module', 'rx', '--', '-skip_known'])  # after '--' it is a negative tag
        self.assertEqual((False, ['-skip_known']), (parsed.skip_known, parsed.tags))

    def test_sidecar_methods(self) -> None:
        from lynxx import server
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': tmp}):
            self.assertFalse(server.METHODS['index.stats']({})['exists'])
            self.assertEqual(0, server.METHODS['index.search']({'tags': 'a'})['total'])
            with dlindex.DownloadIndex(create=True) as index:
                index.add_many([_rec('rx', i, pathlib.Path(tmp) / f'{i}.jpg', tags=f'a t{i % 2}', date=f'2020-01-0{i}') for i in range(1, 6)])
            st = server.METHODS['index.stats']({})
            self.assertEqual((True, 5), (st['exists'], st['files']))
            res = server.METHODS['index.search']({'tags': ['a', '-t1'], 'module': 'rx', 'pageSize': 1, 'page': 1, 'order': 'date'})
            self.assertEqual((2, 1, ['2']), (res['total'], res['pageSize'], [x['id'] for x in res['items']]))
            res = server.METHODS['index.search']({'dateFrom': '2020-01-02', 'dateTo': '2020-01-03', 'pageSize': 9999})
            self.assertEqual((2, dlindex.SEARCH_PAGE_SIZE_MAX), (res['total'], res['pageSize']))
            for bad in ({'module': 'RX!'}, {'pageSize': 'x'}, {'page': [1]}):
                with self.subTest(bad=bad), self.assertRaises(server.RpcError):
                    server.METHODS['index.search'](bad)

    def test_trash_sidecar_methods(self) -> None:
        from lynxx import server
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': tmp}):
            lib = pathlib.Path(tmp) / 'lib'
            lib.mkdir()
            files = []
            for i in (1, 2):
                (lib / f'rx_{i}.jpg').write_bytes(b'abc')
                files.append(str(lib / f'rx_{i}.jpg'))
            self.assertEqual(files, server.METHODS['index.trash']({'paths': files})['refused'])  # no index yet: nothing is touched
            with dlindex.DownloadIndex(create=True) as index:
                index.add_many([_rec('rx', i, f, tags='a') for i, f in enumerate(files, 1)])
            res = server.METHODS['index.trash']({'paths': files, 'roots': [str(lib)]})
            moved = [x['trashPath'] for x in res['trashed']]
            self.assertEqual(2, len(moved))
            self.assertTrue(all(pathlib.Path(p).parent == pathlib.Path(tmp) / dlindex.TRASH_DIR_NAME for p in moved))
            self.assertEqual((2, 0), (server.METHODS['index.search']({'trash': True})['total'], server.METHODS['index.search']({})['total']))
            self.assertEqual(2, server.METHODS['index.stats']({})['trash']['files'])
            self.assertEqual([files[0]], [x['restoredPath'] for x in server.METHODS['index.restore']({'paths': moved[:1]})['restored']])
            self.assertEqual(0, server.METHODS['index.purgeTrash']({'days': 1})['purged'])
            self.assertEqual(1, server.METHODS['index.purgeTrash']({'paths': moved[1:]})['purged'])
            self.assertEqual(0, server.METHODS['index.purgeTrash']({'all': True})['purged'])
            os.remove(files[0])
            self.assertEqual(1, server.METHODS['index.forget']({'paths': files[:1]})['removed'])
            many = [files[0]] * (server.INDEX_PATHS_MAX + 1)
            bad = [('index.trash', {}), ('index.trash', {'paths': files[0]}), ('index.trash', {'paths': ['relative.jpg']}),
                   ('index.trash', {'paths': many}), ('index.trash', {'paths': files, 'roots': files[0]}), ('index.restore', {'paths': [1]}),
                   ('index.forget', {'paths': []}), ('index.purgeTrash', {}), ('index.purgeTrash', {'days': 1, 'all': True}),
                   ('index.purgeTrash', {'days': -1}), ('index.purgeTrash', {'days': True}), ('index.purgeTrash', {'all': 'yes'}),
                   ('index.purgeTrash', {'paths': ['relative.jpg']}), ('index.purgeTrash', {'all': False})]
            for method, params in bad:
                with self.subTest(method=method, params=str(params)[:80]), self.assertRaises(server.RpcError):
                    server.METHODS[method](params)

    def test_trash_desktop_wiring(self) -> None:
        main_js = (ROOT / 'app' / 'main.js').read_text(encoding='utf-8')
        allowed = re.search(r"handle\('rpc'.*?const allowed = \[(.*?)\];", main_js, re.S).group(1)
        for method in ('index.trash', 'index.restore', 'index.purgeTrash', 'index.forget'):
            self.assertNotIn(f"'{method}'", allowed)  # main only: the library:* handlers
        for channel in ('library:trash', 'library:restore', 'library:forget', 'library:purge', 'library:emptyTrash'):
            self.assertIn(f"handle('{channel}'", main_js)
        for channel in ('library:forget', 'library:purge', 'library:emptyTrash'):  # cannot be undone: a native confirmation first
            body = re.search(rf"handle\('{channel}'.*?\n  \}}\);", main_js, re.S).group(0)
            self.assertIn('await confirmLibrary(', body)
        for channel in ('library:trash', 'library:restore'):  # reversible: no dialog, paths checked
            line = re.search(rf"handle\('{channel}'.*", main_js).group(0)
            self.assertNotIn('confirm', line)
            self.assertIn('libraryPaths(paths)', line)
        self.assertIn('library: { trashDays: 30 },', main_js)  # settings.library.trashDays default
        # a time the app did not write (corrupt file, hand edit) turns the automatic purge off: never the default on a guess
        self.assertIn('if (error) settings.library.trashDays = 0;', main_js)
        sanitize = re.search(r'\nfunction sanitizeSettings\(\) \{.*?\n\}\n', main_js, re.S).group(0)
        self.assertIn('settings.library.trashDays = 0;', sanitize)
        self.assertNotIn('settings.library.trashDays = DEFAULT_SETTINGS', main_js)
        self.assertIn('const validTrashDays = (d) => Number.isInteger(d) && d >= 0 && d <= TRASH_DAYS_MAX;', main_js)
        self.assertIn('const TRASH_DAYS_MAX = 365;', main_js)
        # 60 s after the core is up, then on the 5-minute tick (purgeOldTrash itself waits 12 h between two timer runs)
        startup = re.search(r'sidecar\.ready\.then\(\(\) => \{\n\s+setTimeout\(\(\) => \{.*?\}, AUTO_CHECK_MS\);', main_js, re.S).group(0)
        self.assertEqual(2, startup.count("purgeOldTrash('timer')"))
        self.assertIn('}, 60000);', startup)
        self.assertIn('const TRASH_PURGE_EVERY_MS = 12 * 3600e3;', main_js)
        self.assertIn("purgeOldTrash('setting')", main_js)
        preload = (ROOT / 'app' / 'preload.js').read_text(encoding='utf-8')
        for channel in ('library:trash', 'library:restore', 'library:forget', 'library:purge', 'library:emptyTrash'):
            self.assertIn(f"invoke('{channel}'", preload)
        html = (ROOT / 'app' / 'renderer' / 'index.html').read_text(encoding='utf-8')
        app_js = (ROOT / 'app' / 'renderer' / 'app.js').read_text(encoding='utf-8')
        for el in ('lib-view', 'lib-trash-count', 'lib-sel-page', 'lib-sel-none', 'lib-delete', 'lib-restore', 'lib-purge', 'lib-empty',
                   's-trash-days', 'index-trash-info', 'index-empty-trash'):
            self.assertIn(f'id="{el}"', html)
            self.assertIn(f"'#{el}", app_js)
        self.assertIn('<option value="trashed" hidden>', html)
        self.assertIn("trash: LIB.view === 'trash'", app_js)
        for call in ('L.library.trash(', 'L.library.restore(', 'L.library.forget(', 'L.library.purge(', 'L.library.emptyTrash('):
            self.assertIn(call, app_js)
        mock_js = (ROOT / 'app' / 'renderer' / 'mock.js').read_text(encoding='utf-8')
        library = re.search(r'\n    library: \{\n(.*?)\n    \},', mock_js, re.S).group(1)
        for name in ('trash:', 'restore:', 'forget:', 'purge:', 'emptyTrash:'):
            self.assertIn(name, library)
        self.assertIn('library: { trashDays: 30 },', mock_js)
        self.assertIn('p.trash === true', mock_js)

    def test_trash_badge_contrast(self) -> None:
        # 'còn X ngày' / 'sắp xoá' on a --warn background: WCAG AA (4.5:1) for its 10.5px text in every theme
        css = (ROOT / 'app' / 'renderer' / 'styles.css').read_text(encoding='utf-8')
        self.assertIn('.lib-grid .card .b.left { background: var(--warn); color: var(--warn-ink', css)

        def lum(hex_color: str) -> float:
            rgb = [int(hex_color[i:i + 2], 16) / 255 for i in (1, 3, 5)] if len(hex_color) == 7 else [int(c * 2, 16) / 255 for c in hex_color[1:]]
            lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
            return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]
        themes = re.findall(r'\[data-theme="(\w+)"\] \{(.*?)\n\}', css, re.S)
        self.assertEqual(['ember', 'graphite', 'paper', 'oled'], [t for t, _ in themes])
        for theme, body in themes:
            warn = re.search(r'--warn: (#[0-9a-fA-F]+);', body).group(1)
            ink = re.search(r'--warn-ink: (#[0-9a-fA-F]+);', body).group(1)
            hi, lo = sorted((lum(warn), lum(ink)), reverse=True)
            self.assertGreaterEqual((hi + 0.05) / (lo + 0.05), 4.5, theme)

    def test_desktop_wiring(self) -> None:
        main_js = (ROOT / 'app' / 'main.js').read_text(encoding='utf-8')
        allowed = re.search(r"handle\('rpc'.*?const allowed = \[(.*?)\];", main_js, re.S)
        self.assertIsNotNone(allowed)
        self.assertIn("'index.search'", allowed.group(1))
        self.assertIn("'index.stats'", allowed.group(1))
        self.assertIn("if (d.skipKnown === true) a.push('-skip_known');", main_js)
        self.assertRegex(main_js, r'skipKnown: false, //')
        html = (ROOT / 'app' / 'renderer' / 'index.html').read_text(encoding='utf-8')
        app_js = (ROOT / 'app' / 'renderer' / 'app.js').read_text(encoding='utf-8')
        self.assertIn('id="f-skip-known"', html)
        self.assertIn('id="index-stats"', html)
        self.assertIn("skipKnown: $('#f-skip-known').checked", app_js)
        self.assertIn("L.rpc('index.stats')", app_js)

#
#
#########################################
