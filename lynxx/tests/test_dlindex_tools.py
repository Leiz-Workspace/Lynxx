# coding=UTF-8
"""
Lynxx: download index tools (lynxx/dlindex_tools.py, docs/plans/search-engine.md phase 3, 'để sau' items 2, 3, 5, 6), offline
- clean: rows of missing files dropped in batches (files untouched), rows of missing folders kept unless asked, dry run, cancel,
  a row rewritten meanwhile is kept, sidecar index.clean / index.cleanCancel (background thread, notifications, one at a time)
- library import: names -> module + id / md5 (files.identify rules), metadata from a fake website (danbooru id lists, one request
  per post elsewhere, pacing), records, already indexed files skipped, unknown names listed, not found / failed requests, md5 check,
  cancel, the IPC job protocol (dry run in a subprocess)
- re-tag: tags from the index (info files as fallback), renamed / sorted files keep their rows, the CLI entry
- trash: clean on rows of the trash, the import never reads a trash folder, 'trash list' / 'trash purge'
- desktop app wiring: no direct rpc for clean, folder from the native dialog only, multi-site jobs (forced -skip_known / -prefix)
Run: python -m unittest lynxx.tests.test_dlindex_tools
"""
#########################################
#
#

import contextlib
import hashlib
import io
import itertools
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import time
from unittest import TestCase, mock
from urllib import parse as url_parse

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx import dlindex, dlindex_tools, file_identify  # noqa: E402
from lynxx.defines import ItemInfo  # noqa: E402
from lynxx.downloaders import _make_downloader  # noqa: E402
from lynxx.extensions import Extensions  # noqa: E402
from lynxx.module import ProcModule  # noqa: E402
from lynxx.network import HTMLContent  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _rec(module: str, post_id: int, path, *, md5: str = '', tags: str = '', score=None, at: float = 0.0) -> dlindex.IndexRecord:
    return dlindex.IndexRecord(module=module, post_id=str(post_id), path=str(path), md5=md5, tags=tags, score=score,
                               downloaded_at=at or time.time(), size=3)


class _Tmp(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self._env = mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': str(self.tmp / 'data')})
        self._env.start()
        self.db_path = self.tmp / 'data' / dlindex.INDEX_DIR_NAME / dlindex.INDEX_FILE_NAME

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def _file(self, rel: str, data: bytes = b'abc') -> pathlib.Path:
        p = self.tmp / 'lib' / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
        return p

    def _write(self, records) -> None:
        with dlindex.DownloadIndex(self.db_path, create=True) as index:
            index.add_many(records)

    def _paths(self) -> list[str]:
        with dlindex.DownloadIndex(self.db_path) as index:
            return sorted(x['path'] for x in index.search('', page_size=200)['items'])


# ---------------- clean ----------------

class CleanTests(_Tmp):
    def _fill(self) -> dict[str, pathlib.Path]:
        f = {
            'keep1': self._file('a/1.jpg'), 'keep2': self._file('a/2.jpg'),
            'gone1': self.tmp / 'lib' / 'a' / '3.jpg', 'gone2': self.tmp / 'lib' / 'a' / '4.jpg',
            'nodir': self.tmp / 'lib' / 'unplugged' / '5.jpg',
        }
        self._write([_rec('rx', i, p, tags=f'common t{i}') for i, p in enumerate(f.values(), 1)])
        return f

    def test_rows_of_missing_files_are_dropped_files_untouched(self) -> None:
        f = self._fill()
        res = dlindex_tools.clean_index(index_path=self.db_path)
        self.assertEqual((5, 5, 3, 1, 2, False), (res.total, res.checked, res.missing, res.missing_dirs, res.removed, res.cancelled))
        self.assertEqual([str(self.tmp / 'lib' / 'unplugged')], res.folders)
        self.assertEqual(sorted(str(f[k]) for k in ('keep1', 'keep2', 'nodir')), self._paths())
        self.assertTrue(f['keep1'].is_file() and f['keep2'].is_file())
        with dlindex.DownloadIndex(self.db_path) as index:
            self.assertEqual(0, index.search('t3')['total'])
            self.assertEqual(3, index.search('common')['total'])
            conn = index._connection()
            self.assertEqual(0, conn.execute('SELECT COUNT(*) FROM file_tags WHERE file_id NOT IN (SELECT id FROM files)').fetchone()[0])
        # the unplugged folder only when asked
        res = dlindex_tools.clean_index(index_path=self.db_path, include_missing_dirs=True)
        self.assertEqual((1, 1), (res.missing, res.removed))
        self.assertEqual(sorted(str(f[k]) for k in ('keep1', 'keep2')), self._paths())

    def test_dry_run_removes_nothing(self) -> None:
        self._fill()
        before = self._paths()
        res = dlindex_tools.clean_index(index_path=self.db_path, dry_run=True)
        self.assertEqual((3, 1, 0, True), (res.missing, res.missing_dirs, res.removed, res.dry_run))
        self.assertEqual(before, self._paths())

    def test_batches_progress_and_cancel(self) -> None:
        self._fill()
        seen: list[int] = []
        cancel = threading.Event()

        def progress(r) -> None:
            seen.append(r.checked)
            cancel.set()  # stop after the first batch

        with dlindex.DownloadIndex(self.db_path) as index:
            res = index.clean_missing(batch=2, progress=progress, cancel=cancel)
        self.assertEqual(([2], 2, True), (seen, res.checked, res.cancelled))
        with dlindex.DownloadIndex(self.db_path) as index:
            seen.clear()
            res = index.clean_missing(batch=2, progress=lambda r: seen.append(r.checked))
        self.assertEqual([2, 4, 5], seen)

    def test_a_row_rewritten_meanwhile_is_kept(self) -> None:
        f = self._fill()
        real = dlindex.file_present

        def present(path: str) -> bool:
            if path == str(f['gone1']):  # a running job downloads this post again right after the stat
                self._write([_rec('rx', 3, path, tags='again')])
            return real(path)

        with mock.patch.object(dlindex, 'file_present', present):
            res = dlindex_tools.clean_index(index_path=self.db_path)
        self.assertEqual(1, res.removed)
        self.assertIn(str(f['gone1']), self._paths())

    def test_no_index(self) -> None:
        res = dlindex_tools.clean_index(index_path=self.db_path)
        self.assertEqual((0, 0), (res.total, res.removed))
        self.assertFalse(self.db_path.exists())

    def test_rows_of_the_trash(self) -> None:
        # a row of the trash is kept while its file is in the trash folder, dropped once that file is gone
        keep, vanish = self._file('a/1.jpg'), self._file('a/2.jpg')
        self._write([_rec('rx', 1, keep, tags='one'), _rec('rx', 2, vanish, tags='two')])
        with dlindex.DownloadIndex(self.db_path) as index:
            moved = {x['path']: x['trashPath'] for x in index.trash([str(keep), str(vanish)])['trashed']}
        self.assertEqual(str(self.tmp / 'data' / dlindex.TRASH_DIR_NAME), os.path.dirname(moved[str(keep)]))
        os.remove(moved[str(vanish)])  # deleted from the trash folder by hand
        res = dlindex_tools.clean_index(index_path=self.db_path)
        self.assertEqual((2, 1, 0, 1), (res.checked, res.missing, res.missing_dirs, res.removed))
        with dlindex.DownloadIndex(self.db_path) as index:
            self.assertEqual([moved[str(keep)]], [x['path'] for x in index.search('', trash=True)['items']])

    def test_cli(self) -> None:
        self._fill()
        out = subprocess.run((sys.executable, '-m', 'lynxx.dlindex_tools', 'clean', '--json'), cwd=ROOT, capture_output=True, text=True,
                             env={**os.environ, 'PYTHONPATH': str(ROOT)}, timeout=120)
        self.assertEqual(0, out.returncode, out.stderr)
        self.assertEqual(2, json.loads(out.stdout.strip().splitlines()[-1])['removed'])


class CleanRpcTests(_Tmp):
    def test_background_clean_notifications_and_one_at_a_time(self) -> None:
        from lynxx import server
        self._write([_rec('rx', 1, self.tmp / 'gone.jpg')])
        notes: list[tuple[str, dict]] = []
        done = threading.Event()
        gate = threading.Event()
        real = dlindex_tools.clean_index

        def slow_clean(**kw):
            gate.wait(10)
            return real(**kw)

        def note(method: str, params: dict) -> None:
            notes.append((method, params))
            if method == 'index.cleanDone':
                done.set()

        with mock.patch.object(server, 'notify', note), mock.patch.object(dlindex_tools, 'clean_index', slow_clean):
            first = server.METHODS['index.clean']({'missingFolders': True})
            with self.assertRaises(server.RpcError) as ctx:
                server.METHODS['index.clean']({})
            self.assertEqual(-32004, ctx.exception.code)
            gate.set()
            self.assertTrue(done.wait(10))
        _, params = notes[-1]
        self.assertEqual((first['job'], True, 1, True), (params['job'], params['ok'], params['removed'], params['missingFolders']))
        self.assertFalse(server.METHODS['index.cleanCancel']({})['cancelled'])  # nothing running any more

    def test_next_clean_can_start_on_done(self) -> None:
        # a dry run, then the real clean as soon as the dry run's cleanDone arrives (the app's settings and self-test do that):
        # the finished job must be gone before cleanDone is sent, or the second clean is refused with -32004
        from lynxx import server
        done = threading.Event()
        running_at_done: list[dict] = []

        def note(method: str, params: dict) -> None:
            if method == 'index.cleanDone':
                running_at_done.append(dict(server._index_clean))
                done.set()

        with mock.patch.object(server, 'notify', note), mock.patch.object(dlindex_tools, 'clean_index', lambda **_: dlindex.CleanResult()):
            server.METHODS['index.clean']({'dryRun': True})
            self.assertTrue(done.wait(10))
        self.assertEqual([{}], running_at_done)

    def test_cancel(self) -> None:
        from lynxx import server
        done = threading.Event()
        result: dict = {}

        def note(method: str, params: dict) -> None:
            if method == 'index.cleanDone':
                result.update(params)
                done.set()

        def blocking_clean(cancel=None, **_):
            cancel.wait(10)
            return dlindex.CleanResult(cancelled=cancel.is_set())

        with mock.patch.object(server, 'notify', note), mock.patch.object(dlindex_tools, 'clean_index', blocking_clean):
            server.METHODS['index.clean']({})
            self.assertTrue(server.METHODS['index.cleanCancel']({})['cancelled'])
            self.assertTrue(done.wait(10))
        self.assertTrue(result['cancelled'])


# ---------------- library import ----------------

def _post_md5(post_id: int) -> str:
    return hashlib.md5(f'post {post_id}'.encode()).hexdigest()


class FakeSite:
    """Danbooru json (id / md5 lists) and rule34 dapi xml (one id or md5 per search) over a fixed set of posts"""

    def __init__(self, posts: dict[int, dict]) -> None:
        self.posts = posts
        self.queries: list[str] = []
        self.fail = False
        self.on_request = None

    def _select(self, tags: str) -> list[dict]:
        self.queries.append(tags)
        if self.on_request:
            self.on_request(tags)
        out = []
        for t in tags.split():
            name, _, value = t.partition(':')
            for v in value.split(','):
                for pid, p in self.posts.items():
                    if (name == 'id' and v == str(pid)) or (name == 'md5' and v == p['md5']):
                        out.append({'id': pid, **p})
        return sorted({p['id']: p for p in out}.values(), key=lambda p: -p['id'])

    def db_fetch(self, url: str, **_) -> HTMLContent | None:
        if self.fail:
            return None
        q = url_parse.parse_qs(url_parse.urlparse(url).query)
        posts = self._select(q.get('tags', [''])[0])
        return HTMLContent(None, json.dumps([
            {'id': p['id'], 'created_at': '2024-05-06T07:08:09.000-04:00', 'tag_string': p['tags'], 'file_ext': 'jpg', 'md5': p['md5'],
             'file_url': f'https://cdn.example/{p["md5"]}.jpg', 'score': p['score'], 'image_width': 10, 'image_height': 10,
             'rating': 'g', 'has_children': False, 'parent_id': None, 'source': ''} for p in posts]).encode())

    def rx_fetch(self, url: str, **_) -> HTMLContent | None:
        if self.fail:
            return None
        q = url_parse.parse_qs(url_parse.urlparse(url).query)
        posts = self._select(q.get('tags', [''])[0])
        body = ''.join(
            f'<post height="10" score="{p["score"]}" file_url="https://cdn.example/images/{p["id"]}.jpg" parent_id="" '
            f'sample_url="https://cdn.example/samples/{p["id"]}.jpg" preview_url="https://cdn.example/thumbs/{p["id"]}.jpg" '
            f'rating="q" tags=" {p["tags"]} " id="{p["id"]}" width="10" change="1" md5="{p["md5"]}" creator_id="1" has_children="false" '
            f'created_at="Mon May 06 07:08:09 +0000 2024" status="active" source="" has_notes="false" has_comments="false" '
            f'preview_width="10" preview_height="10"/>' for p in posts)
        return HTMLContent(None, f'<?xml version="1.0" encoding="UTF-8"?><posts count="{len(posts)}" offset="0">{body}</posts>'.encode())


class ImportTests(_Tmp):
    @classmethod
    def setUpClass(cls) -> None:
        Extensions.ensure_loaded()

    def setUp(self) -> None:
        super().setUp()
        posts = {i: {'md5': _post_md5(i), 'tags': f'tag_{i} common', 'score': i % 50} for i in (1001, 1002, 1003, 1004, 2001, 2002, 3001)}
        self.db = FakeSite({k: v for k, v in posts.items() if k < 2000})
        self.rx = FakeSite({k: v for k, v in posts.items() if k >= 2000})
        self.lib = self.tmp / 'lib'
        self.files = {
            'db1': self._file('db_1001.jpg'), 'db2': self._file('db_1002_score(5)_tag.png'), 'db3': self._file('sub/deeper/db_1003.jpeg'),
            'gone': self._file('db_9999.jpg'), 'rx1': self._file('rx_2001.jpg'), 'plain': self._file('3001.jpg'),
            'md5': self._file(f'{_post_md5(2002)}.jpg'), 'unknown': self._file('IMG_0001.jpg'), 'known': self._file('db_1004.jpg'),
        }
        self._file('notes.txt')
        self._file('.hidden/db_1001.jpg')
        self._file('db_1001.jpg.part')
        (self.lib / 'empty.jpg').write_bytes(b'')
        self._write([_rec('db', 1004, self.files['known'], tags='already')])

    def tearDown(self) -> None:
        super().tearDown()
        ProcModule.set(ProcModule.RX)

    def _factory(self, pid: int):
        dwn = _make_downloader(pid)
        dwn.fetch_html = self.db.db_fetch if pid == ProcModule.DB else self.rx.rx_fetch
        return dwn

    def _run(self, **kw) -> dlindex_tools.ImportResult:
        kw.setdefault('min_interval', 0.0)
        kw.setdefault('index_path', self.db_path)
        return dlindex_tools.LibraryImport(self.lib, downloader_factory=self._factory, log=lambda _: None, **kw).run()

    def test_scan(self) -> None:
        rels = [f.rel for f in dlindex_tools.scan_library(self.lib)]
        self.assertEqual(sorted(rels), rels)
        self.assertEqual(9, len(rels))  # no .part / .txt / hidden folder / empty file
        self.assertNotIn('notes.txt', rels)
        self.assertEqual(8, len(list(dlindex_tools.scan_library(self.lib, recursive=False))))

    def test_import(self) -> None:
        res = self._run(default_module='rx')
        self.assertEqual('', res.error)
        self.assertEqual((9, 1, 1, 7, 6, 1, 0, False), (res.scanned, res.known, res.unknown, res.identified, res.imported, res.not_found,
                                                       res.failed, res.cancelled))
        self.assertEqual(['IMG_0001.jpg: not recognized'], res.unknown_names)
        self.assertEqual(['db_9999.jpg (db id 9999)'], res.not_found_names)
        # danbooru: one request for the 4 ids; rule34: one request per post (2 ids + 1 md5)
        self.assertEqual(['id:1001,1002,1003,9999'], self.db.queries)
        self.assertEqual(sorted(['id:2001', 'id:3001', f'md5:{_post_md5(2002)}']), sorted(self.rx.queries))
        self.assertEqual((1, 3), (res.modules['db']['requests'], res.modules['rx']['requests']))
        with dlindex.DownloadIndex(self.db_path) as index:
            rows = {pathlib.Path(x['path']).name: x for x in index.search('', page_size=200)['items']}
        self.assertEqual({'db_1001.jpg', 'db_1002_score(5)_tag.png', 'db_1003.jpeg', 'db_1004.jpg', 'rx_2001.jpg', '3001.jpg',
                          f'{_post_md5(2002)}.jpg'}, set(rows))
        r = rows['db_1003.jpeg']
        self.assertEqual(('db', '1003', _post_md5(1003), 'tag_1003 common', 3, 'g', '2024-05-06', 'jpeg'),
                         (r['module'], r['id'], r['md5'], r['tags'], r['score'], r['rating'], r['date'], r['ext']))
        self.assertAlmostEqual(self.files['db3'].stat().st_mtime, r['downloadedAt'], places=3)  # the file's own time
        self.assertEqual(('rx', '2002', 'q'), (rows[f'{_post_md5(2002)}.jpg']['module'], rows[f'{_post_md5(2002)}.jpg']['id'],
                                               rows[f'{_post_md5(2002)}.jpg']['rating']))
        self.assertEqual(('rx', '3001'), (rows['3001.jpg']['module'], rows['3001.jpg']['id']))
        self.assertEqual(dlindex_tools.EXIT_OK, res.exit_code())
        # -skip_known now knows them: same md5 from another module included
        with dlindex.DownloadIndex(self.db_path) as index:
            by_id, by_md5 = index.find_known('rx', ['2001'], [_post_md5(1001)])
        self.assertEqual((['2001'], [_post_md5(1001)]), (list(by_id), list(by_md5)))
        # a second run: the imported files are in the index (by path); only the post that was not found is asked again
        self.db.queries.clear()
        self.rx.queries.clear()
        again = self._run(default_module='rx')
        self.assertEqual((7, 0, 1, 1), (again.known, again.imported, again.not_found, again.requests))
        self.assertEqual((['id:9999'], []), (self.db.queries, self.rx.queries))

    def test_names_without_a_module_are_listed_not_guessed(self) -> None:
        res = self._run()
        self.assertEqual(3, res.unknown)
        self.assertTrue(any(x.startswith('3001.jpg: no module prefix') for x in res.unknown_names))
        self.assertTrue(any(x.startswith(f'{_post_md5(2002)}.jpg: no module prefix') for x in res.unknown_names))
        self.assertEqual(['id:2001'], self.rx.queries)

    def test_dry_run(self) -> None:
        res = self._run(default_module='rx', dry_run=True)
        self.assertEqual((7, 0, 0), (res.identified, res.imported, res.requests))
        self.assertEqual({'db': 4, 'rx': 3}, {m: s['files'] for m, s in res.modules.items()})
        self.assertEqual([], self.db.queries + self.rx.queries)
        self.assertEqual(['db_1004.jpg'], [pathlib.Path(p).name for p in self._paths()])

    def test_pacing(self) -> None:
        stamps: list[float] = []
        self.rx.on_request = lambda _: stamps.append(time.monotonic())
        self._run(default_module='rx', min_interval=0.05)
        self.assertEqual(3, len(stamps))
        self.assertTrue(all(b - a >= 0.045 for a, b in itertools.pairwise(stamps)), stamps)

    def test_cancel(self) -> None:
        cancel = threading.Event()
        self.rx.on_request = lambda _: cancel.set()  # rx comes first: '3001.jpg' is the first file found
        res = self._run(default_module='rx', cancel=cancel)
        self.assertTrue(res.cancelled)
        self.assertEqual(dlindex_tools.EXIT_CANCELLED, res.exit_code())
        self.assertEqual((1, []), (len(self.rx.queries), self.db.queries))  # no other request, the next module never started
        self.assertEqual(1, res.imported)  # what the answered request brought is recorded
        self.assertIn('rx_2001.jpg', [pathlib.Path(p).name for p in self._paths()])

    def test_failed_requests_give_up_the_module(self) -> None:
        self.rx.fail = True
        with mock.patch.object(dlindex_tools, 'IMPORT_FAIL_STREAK', 2):
            res = self._run(default_module='rx')
        self.assertEqual((3, 3), (res.imported, res.failed))  # db files imported, every rx file failed
        self.assertEqual(2, res.modules['rx']['requests'])
        self.assertEqual(dlindex_tools.EXIT_PARTIAL, res.exit_code())
        self.assertTrue(any('2 failed requests in a row' in x for x in res.failed_names))
        # running again imports what failed
        self.rx.fail = False
        again = self._run(default_module='rx')
        self.assertEqual((4, 3, 0), (again.known, again.imported, again.failed))

    def test_verify_md5(self) -> None:
        self.files['db1'].write_bytes(b'post 1001')  # the posts' "original files" in this fake: b'post <id>'
        self.db.posts[1001]['md5'] = hashlib.md5(b'post 1001').hexdigest()
        res = self._run(default_module='rx', verify_md5=True)
        self.assertEqual(1, res.modules['db']['imported'])
        self.assertEqual(2, res.modules['db']['mismatch'])
        self.assertTrue(any(x.startswith('db_1002_score(5)_tag.png') for x in res.mismatch_names))

    def test_missing_folder(self) -> None:
        res = dlindex_tools.LibraryImport(self.tmp / 'nope', index_path=self.db_path).run()
        self.assertIn('folder not found', res.error)
        self.assertEqual(dlindex_tools.EXIT_ERROR, res.exit_code())

    def test_ipc_job_protocol(self) -> None:
        env = {**os.environ, 'PYTHONPATH': str(ROOT), 'LYNXX_IPC': '1', 'LYNXX_CATALOG_AUTOLOAD': '0',
               'LYNXX_SECRET_ARGS': '[]', dlindex_tools.ENV_SECRET_MODULE_ARGS: json.dumps({'db': ['-api_key', 'K,U']})}
        out = subprocess.run((sys.executable, '-m', 'lynxx.dlindex_tools', 'import', str(self.lib), '--module', 'rx', '--dry-run'),
                             cwd=ROOT, env=env, input='', capture_output=True, text=True, timeout=120)
        self.assertEqual(0, out.returncode, out.stderr)
        lines = [json.loads(x[len('@@LYNXX_PROGRESS '):]) for x in out.stdout.splitlines() if x.startswith('@@LYNXX_PROGRESS ')]
        self.assertTrue(lines)
        last = lines[-1]
        self.assertEqual(('DONE', True, 0, 7, 1, 1, True), (last['stateName'], last['ok'], last['exitCode'], last['totalAll'], last['known'],
                                                           last['unknown'], last['dryRun']))
        self.assertEqual(['IMG_0001.jpg: not recognized'], last['unknownNames'])
        self.assertNotIn('K,U', out.stdout + out.stderr)

    def test_ipc_final_line_is_last(self) -> None:
        # the progress thread read a state, then lost the CPU until the import ended (a slow machine): that older state must not
        # be printed after the final DONE line, or the app keeps its counts
        import contextlib
        import io
        real = dlindex_tools.LibraryImport.progress

        def late_progress(imp) -> dict:
            state = real(imp)
            if threading.current_thread().name == 'ipc-progress':
                time.sleep(0.5)
            return state

        out = io.StringIO()
        with mock.patch.dict(os.environ, {'LYNXX_IPC': '1', 'LYNXX_SECRET_ARGS': '[]'}), \
                mock.patch.object(dlindex_tools.LibraryImport, 'progress', late_progress), \
                mock.patch.object(sys, 'stdin', io.StringIO('')), contextlib.redirect_stdout(out):
            code = dlindex_tools.main(['import', str(self.lib), '--module', 'rx', '--dry-run'])
            for t in threading.enumerate():
                if t.name == 'ipc-progress':
                    t.join(5)
        self.assertEqual(dlindex_tools.EXIT_OK, code)
        lines = [json.loads(x[len('@@LYNXX_PROGRESS '):]) for x in out.getvalue().splitlines() if x.startswith('@@LYNXX_PROGRESS ')]
        self.assertEqual(('DONE', 7), (lines[-1]['stateName'], lines[-1]['totalAll']))


# ---------------- re-tag / renamed files ----------------

class RetagTests(_Tmp):
    @classmethod
    def setUpClass(cls) -> None:
        Extensions.ensure_loaded()

    def test_tags_from_the_index_then_info_files(self) -> None:
        from lynxx.file_tagger import item_infos_from_index, retag_files_any
        from lynxx.rex import re_tags_exclude_rx, re_tags_to_process_rx
        a = self._file('rx_1.jpg')
        b = self._file('rx_2.jpg')
        c = self._file('rx_3_old_name.jpg')
        self._write([_rec('rx', 1, a, tags='solo cat_ears', score=12),
                     _rec('rx', 3, self.tmp / 'elsewhere' / 'rx_3.jpg', tags='dog', score=1)])  # c: same post, other path
        index_infos = item_infos_from_index([a, b, c], self.db_path)
        self.assertEqual({'rx_1', 'rx_3_old_name'}, set(index_infos))
        json_info = ItemInfo()
        json_info.id, json_info.tags, json_info.score = '2', 'sky', '3'
        n = retag_files_any([a, b, c], {'2': json_info, **index_infos}, re_tags_to_process_rx, re_tags_exclude_rx)
        self.assertEqual(3, n)
        names = sorted(p.name for p in (self.tmp / 'lib').iterdir())
        self.assertEqual(3, len(names))
        self.assertTrue(any(x.startswith('rx_1_(+12)') for x in names), names)
        self.assertTrue(any(x.startswith('rx_2_(+3)') for x in names), names)
        # the index follows the renamed file
        new_a = next(p for p in (self.tmp / 'lib').iterdir() if p.name.startswith('rx_1_'))
        with dlindex.DownloadIndex(self.db_path) as index:
            self.assertEqual([str(new_a)], [x['path'] for x in index.search('solo')['items']])
            self.assertTrue(index.search('solo')['items'][0]['exists'])

    def test_names_that_are_not_post_ids_get_nothing(self) -> None:
        from lynxx.file_tagger import item_infos_from_index
        md5_name = self._file(f'{_post_md5(4)}.jpg')  # imported by md5: '<digits><hex>' must not be taken for an id
        other = self._file('rx_8.jpg')  # indexed path, but the row is another post
        self._write([_rec('db', 4, md5_name, tags='x'), _rec('rx', 9, other, tags='y')])
        self.assertEqual({}, item_infos_from_index([md5_name, other], self.db_path))

    def test_rename_paths_replaces_a_stale_row(self) -> None:
        old = self._file('rx_1.jpg')
        new = self.tmp / 'lib' / 'rx_1_x.jpg'
        self._write([_rec('rx', 1, old, tags='fresh'), _rec('rx', 1, new, tags='stale')])
        old.rename(new)
        self.assertEqual(1, dlindex.rename_indexed_files([(old, new)]))
        with dlindex.DownloadIndex(self.db_path) as index:
            self.assertEqual(([str(new)], 0), ([x['path'] for x in index.search('fresh')['items']], index.search('stale')['total']))

    def test_rename_without_an_index_creates_nothing(self) -> None:
        self.assertEqual(0, dlindex.rename_indexed_files([(self.tmp / 'a', self.tmp / 'b')]))
        self.assertFalse(self.db_path.exists())

    def test_untag_and_sort_keep_rows(self) -> None:
        from lynxx.file_sorter import FileTypeFilter, sort_files_by_type
        from lynxx.file_tagger import untag_files
        f = self._file('rx_5_score(1)_tag.jpg')
        self._write([_rec('rx', 5, f, tags='five')])
        self.assertEqual(1, untag_files([f]))
        untagged = self.tmp / 'lib' / 'rx_5.jpg'
        self.assertEqual(1, sort_files_by_type([untagged], FileTypeFilter.BY_EXTENSION))
        with dlindex.DownloadIndex(self.db_path) as index:
            item = index.search('five')['items'][0]
        self.assertEqual((str(self.tmp / 'lib' / 'jpg' / 'rx_5.jpg'), True), (item['path'], item['exists']))

    def test_cli(self) -> None:
        a = self._file('rx_7.jpg')
        self._write([_rec('rx', 7, a, tags='solo', score=4)])
        with mock.patch('builtins.print'), mock.patch.object(Logger, 'init'):
            code = dlindex_tools.main(['retag', str(self.tmp / 'lib'), '--source', 'index'])
        self.assertEqual(dlindex_tools.EXIT_OK, code)
        self.assertTrue(any(p.name.startswith('rx_7_(+4)') for p in (self.tmp / 'lib').iterdir()))
        with mock.patch('builtins.print'), mock.patch.object(Logger, 'init'):
            self.assertEqual(dlindex_tools.EXIT_ARGS, dlindex_tools.main(['retag', str(self.tmp / 'lib'), '--types', 'nope']))


# ---------------- trash ----------------

class TrashToolTests(_Tmp):
    def test_import_skips_the_trash_folder(self) -> None:
        a = self._file('db_1.jpg')
        b = self._file('sub/db_2.jpg')
        self._write([_rec('db', 2, b, tags='two')])
        real = dlindex._device
        trash_dir = str(self.tmp / 'data' / dlindex.TRASH_DIR_NAME)
        # the data dir on another drive: the file goes to '.lynxx-trash' at the root of the download folder
        with dlindex.DownloadIndex(self.db_path) as index, mock.patch.object(dlindex, '_device', lambda p: -1 if p == trash_dir else real(p)):
            moved = index.trash([str(b)], roots=[str(self.tmp / 'lib')])['trashed'][0]['trashPath']
        self.assertEqual(self.tmp / 'lib' / dlindex.TRASH_HIDDEN_NAME / '1_db_2.jpg', pathlib.Path(moved))
        self._file('sub/.lynxx-trash/7_db_3.jpg')  # one next to a file
        self.assertEqual([a.name], [f.rel for f in dlindex_tools.scan_library(self.tmp / 'lib')])

    def _run(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), mock.patch.object(Logger, 'init'):
            code = dlindex_tools.main(list(args))
        return code, out.getvalue()

    def test_cli(self) -> None:
        a, b = self._file('rx_1.jpg'), self._file('rx_2.jpg')
        self._write([_rec('rx', 1, a, tags='one'), _rec('rx', 2, b, tags='two')])
        with dlindex.DownloadIndex(self.db_path) as index:
            index.trash([str(a)], now=time.time() - 40 * dlindex.DAY_SECONDS)
            index.trash([str(b)])
        code, out = self._run('trash', 'list', '--json')
        listed = json.loads(out)
        self.assertEqual((dlindex_tools.EXIT_OK, 2), (code, listed['total']))
        self.assertEqual([str(b), str(a)], [x['origPath'] for x in listed['items']])  # most recently deleted first
        code, out = self._run('trash', 'purge', '--days', '30', '--json')
        self.assertEqual((dlindex_tools.EXIT_OK, 1, 1), (code, json.loads(out)['purged'], json.loads(out)['files']))
        code, out = self._run('trash', 'list')
        self.assertIn(str(b), out)
        self.assertNotIn(str(a), out)
        self.assertIn('Trash: 1 file(s)', out)
        with mock.patch('sys.stderr', io.StringIO()):
            self.assertEqual(dlindex_tools.EXIT_ARGS, self._run('trash', 'purge', '--days', '-1')[0])
            for args in (['trash', 'purge'], ['trash', 'purge', '--days', '1', '--all'], ['trash']):
                with self.subTest(args=args), self.assertRaises(SystemExit):
                    self._run(*args)
        code, out = self._run('trash', 'purge', '--all')
        self.assertEqual(dlindex_tools.EXIT_OK, code)
        self.assertIn('1 file(s) deleted', out)
        self.assertFalse(any((self.tmp / 'data' / dlindex.TRASH_DIR_NAME).iterdir()))
        self.assertTrue(self.db_path.exists())

    def test_cli_without_an_index(self) -> None:
        self.assertEqual((dlindex_tools.EXIT_OK, 0), (self._run('trash', 'purge', '--days', '0', '--json')[0],
                                                      json.loads(self._run('trash', 'purge', '--days', '0', '--json')[1])['purged']))
        self.assertIn('Trash: 0 file(s)', self._run('trash', 'list')[1])
        self.assertFalse(self.db_path.exists())


# ---------------- frontends ----------------

class WiringTests(TestCase):
    def test_identify_rules_are_shared(self) -> None:
        from lynxx import server
        self.assertIs(server.identify_file_name, file_identify.identify_file_name)
        self.assertIs(server.build_file_queries, file_identify.build_file_queries)

    def test_frozen_core_command(self) -> None:
        src = (ROOT / 'lynxx' / 'frozen_main.py').read_text(encoding='utf-8')
        self.assertIn("if command == 'index':", src)
        self.assertIn('from lynxx.dlindex_tools import main as index_main', src)

    def test_desktop_wiring(self) -> None:
        main_js = (ROOT / 'app' / 'main.js').read_text(encoding='utf-8')
        allowed = re.search(r"handle\('rpc'.*?const allowed = \[(.*?)\];", main_js, re.S).group(1)
        self.assertNotIn('index.clean', allowed)  # only through index:clean (native confirmation)
        for channel in ('index:clean', 'index:cleanCancel', 'index:pickFolder', 'index:import', 'jobs:startMulti'):
            self.assertIn(f"handle('{channel}'", main_js)
        self.assertIn("index: 'lynxx.dlindex_tools'", main_js)
        self.assertIn('skipKnown: true, prefix: true', main_js)
        self.assertIn("if (!approvedImportFolders.has(folder)) throw new Error('choose the folder with the folder dialog first');", main_js)
        self.assertIn('autoKey: undefined, group: undefined', main_js)
        preload = (ROOT / 'app' / 'preload.js').read_text(encoding='utf-8')
        for name in ("invoke('index:clean'", "invoke('index:pickFolder'", "invoke('index:import'", "invoke('jobs:startMulti'"):
            self.assertIn(name, preload)
        html = (ROOT / 'app' / 'renderer' / 'index.html').read_text(encoding='utf-8')
        for el in ('id="index-clean"', 'id="index-clean-check"', 'id="index-import"', 'id="lib-import"', 'id="f-multi-pills"'):
            self.assertIn(el, html)
        mock_js = (ROOT / 'app' / 'renderer' / 'mock.js').read_text(encoding='utf-8')
        for name in ('startMulti:', 'pickImportFolder:', 'importLibrary:', 'cleanCancel:'):
            self.assertIn(name, mock_js)

#
#
#########################################
