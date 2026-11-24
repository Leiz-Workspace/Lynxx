# coding=UTF-8
"""
Lynxx: security & data-loss regression tests (audit 2026-11-16, batch A). Offline: a local http server on 127.0.0.1 only.
Run: python -m unittest lynxx.tests.test_security
"""
#########################################
#
#

import contextlib
import functools
import hashlib
import http.server
import io
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import zipfile
from multiprocessing.dummy import current_process
from unittest import TestCase, mock

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx.cmdargs import prepare_arglist  # noqa: E402
from lynxx.defines import DownloadModes  # noqa: E402
from lynxx.downloaders import _make_downloader  # noqa: E402
from lynxx.extensions import ExtensionError, Extensions  # noqa: E402
from lynxx.file_searcher import find_duplicated_files  # noqa: E402
from lynxx.lynxx_cmd import ENV_SECRET_ARGS, _pop_secret_args  # noqa: E402
from lynxx.modspec import ExtensionManifest, ManifestError  # noqa: E402
from lynxx.module import ProcModule  # noqa: E402
from lynxx.network import PART_SUFFIX, DownloadInterruptException  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
BASE_MANIFEST = {'id': 'secsite', 'name': 'S', 'abbr': 'sq', 'version': '1.0.0', 'entry': 'downloader.py:DownloaderSq'}
DOWNLOADER_OK = 'from lynxx.download_db import DownloaderDb\n\n\nclass DownloaderSq(DownloaderDb):\n    pass\n'


def _package(root: pathlib.Path, manifest: dict, code: str = DOWNLOADER_OK) -> pathlib.Path:
    src = root / manifest['id']
    src.mkdir(parents=True, exist_ok=True)
    (src / 'manifest.json').write_text(json.dumps(manifest))
    (src / 'downloader.py').write_text(code)
    return src


class _DataDirCase(TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': str(self.root / 'data')})
        self.env.start()
        Extensions.ensure_loaded()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()


class ExtensionPathTests(_DataDirCase):
    def test_uninstall_never_leaves_user_dir(self) -> None:  # A1
        victim = self.root / 'victim'
        victim.mkdir()
        (victim / 'manifest.json').write_text('{}')
        for bad in (str(victim), '../victim', '../../victim', 'Bad Id', '', None, 123):
            with self.subTest(bad=bad), self.assertRaises(ExtensionError):
                Extensions.uninstall(bad)  # type: ignore[arg-type]
        self.assertTrue((victim / 'manifest.json').is_file())
        with self.assertRaises(ExtensionError):
            Extensions.install_from_catalog('../builtin_ext/rule34')

    def test_builtin_cannot_be_removed(self) -> None:
        with self.assertRaises(ExtensionError):
            Extensions.uninstall('danbooru')


class ManifestTypeTests(TestCase):  # A9
    def test_wrong_types_raise_manifest_error(self) -> None:
        for patch in ({'id': 123}, {'media': None}, {'syntax': 'x'}, {'apiVersion': True}, {'syntax': {'maxTags': 'lots'}},
                      {'syntax': {'maxTags': -1}}, {'default': 'yes'}, {'safetyTags': 'loli'}, {'auth': []}, {'abbr': 'a٣'},
                      {'syntax': {'freeMetaPrefixes': [1]}}):
            with self.subTest(patch=patch), self.assertRaises(ManifestError):
                ExtensionManifest.from_dict({**BASE_MANIFEST, **patch}, None, 'user')


class ExtensionInstallSafetyTests(_DataDirCase):
    def test_scan_skips_hidden_mismatched_and_malformed(self) -> None:
        user = Extensions.user_dir()
        _package(user, {**BASE_MANIFEST, 'id': '.hidden'})
        (user / 'wrongname').mkdir(parents=True)
        (user / 'wrongname' / 'manifest.json').write_text(json.dumps({**BASE_MANIFEST, 'id': 'other'}))
        (user / 'broken').mkdir(parents=True)
        (user / 'broken' / 'manifest.json').write_text(json.dumps({**BASE_MANIFEST, 'id': 'broken', 'media': None}))
        self.assertEqual([], Extensions._scan(user, 'user'))  # no crash, nothing loaded

    def test_install_is_transactional(self) -> None:
        src_root = self.root / 'src'
        # abbreviation used by a catalog extension -> refused, nothing written
        with self.assertRaises(ExtensionError):
            Extensions.install_from_dir(_package(src_root, {**BASE_MANIFEST, 'id': 'clash', 'abbr': 'en'}))
        self.assertFalse((Extensions.user_dir() / 'clash').exists())
        # v1 ok, v2 with a syntax error -> refused, v1 kept on disk
        Extensions.install_from_dir(_package(src_root / 'v1', BASE_MANIFEST))
        with self.assertRaises(SyntaxError):
            Extensions.install_from_dir(_package(src_root / 'v2', {**BASE_MANIFEST, 'version': '2.0.0'}, 'def broken(:\n'))
        installed = json.loads((Extensions.user_dir() / 'secsite' / 'manifest.json').read_text())
        self.assertEqual('1.0.0', installed['version'])
        self.assertFalse([p.name for p in Extensions.user_dir().iterdir() if p.name.startswith('.')])
        # v3 valid -> installed, restart required because v1 code is still imported
        m = Extensions.install_from_dir(_package(src_root / 'v3', {**BASE_MANIFEST, 'version': '3.0.0'}))
        self.assertTrue(Extensions.restart_required(m))
        Extensions.uninstall('secsite')

    def test_symlinks_rejected(self) -> None:
        src = _package(self.root / 'src', {**BASE_MANIFEST, 'id': 'linky', 'abbr': 'lk'})
        (src / 'home').symlink_to(pathlib.Path.home())
        with self.assertRaises(ExtensionError):
            Extensions.install_from_dir(src)
        self.assertFalse((Extensions.user_dir() / 'linky').exists())

    def test_archive_limits(self) -> None:
        big = io.BytesIO()
        with zipfile.ZipFile(big, 'w', zipfile.ZIP_DEFLATED) as zf:
            zf.writestr('x/manifest.json', json.dumps({**BASE_MANIFEST, 'id': 'x'}))
            zf.writestr('x/blob.bin', b'\0' * (51 * 1024 * 1024))  # compresses to ~50 KB
        data = big.getvalue()
        with self.assertRaises(ExtensionError):
            Extensions.install_from_archive(data, hashlib.sha256(data).hexdigest())
        link = io.BytesIO()
        with zipfile.ZipFile(link, 'w') as zf:
            info = zipfile.ZipInfo('y/evil')
            info.external_attr = 0o120777 << 16
            zf.writestr(info, '/etc/passwd')
        data = link.getvalue()
        with self.assertRaises(ExtensionError):
            Extensions.install_from_archive(data, hashlib.sha256(data).hexdigest())
        with self.assertRaises(ExtensionError):
            Extensions.install_from_archive(data, 'not-a-sha')

    def test_https_only(self) -> None:
        with mock.patch('requests.get') as get:
            with self.assertRaises(ExtensionError):
                Extensions.install_from_url('http://example.invalid/x.zip', '0' * 64)
            with self.assertRaises(ExtensionError):
                Extensions.fetch_remote_catalog('http://example.invalid/index.json')
            get.assert_not_called()


@contextlib.contextmanager
def _http_server(directory: pathlib.Path):
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

    handler = functools.partial(QuietHandler, directory=str(directory))
    srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f'http://127.0.0.1:{srv.server_address[1]}/'
    finally:
        srv.shutdown()
        srv.server_close()


class OwnApiKeyTests(TestCase):
    """Lynxx ships no shared API key (the original code had the author's rx key): rx runs only with the user's own key,
    from -api_key (the app) or LYNXX_API_KEY_RX (CLI, cron)"""

    @staticmethod
    def _parse(argv: list[str]):
        dwn = _make_downloader(ProcModule.RX)
        me = current_process()
        me.killed = False
        dwn.reset_root_thread(me)
        dwn.no_conflict_delay = True
        dwn._parse_args(prepare_arglist(['-module', 'rx', *argv]), False)
        return dwn

    def test_no_shared_key_in_the_code(self) -> None:
        from lynxx import defines, download_rx, tags_updater
        for mod in (defines, download_rx, tags_updater):
            self.assertFalse(any(n in vars(mod) for n in ('API_KEY_DEFAULT_RX', 'API_USER_ID_DEFAULT_RX', 'API_KEY', 'USER_ID')), mod.__name__)

    def test_rx_without_a_key_stops_with_a_clear_error(self) -> None:
        from lynxx.defines import ThreadInterruptException
        with mock.patch.dict(os.environ, {'LYNXX_API_KEY_RX': ''}), self.assertRaises(ThreadInterruptException) as cm:
            self._parse(['x'])._solve_argument_conflicts()
        self.assertIn("module 'RX' needs your own API key", str(cm.exception))

    def test_the_users_key_is_used(self) -> None:
        MINE, FROM_ENV = 'a1' * 64, 'b2' * 64  # rx keys: 128 alphanumeric characters
        with mock.patch.dict(os.environ, {'LYNXX_API_KEY_RX': ''}):
            dwn = self._parse(['-api_key', f'{MINE},77', 'x'])
            self.assertEqual(f'&api_key={MINE}&user_id=77', dwn._get_api_key())
        with mock.patch.dict(os.environ, {'LYNXX_API_KEY_RX': f'{FROM_ENV},5'}):
            self.assertEqual(f'&api_key={FROM_ENV}&user_id=5', self._parse(['x'])._get_api_key())
            self.assertEqual(f'&api_key={MINE}&user_id=77', self._parse(['-api_key', f'{MINE},77', 'x'])._get_api_key())  # the option wins

    def test_tag_update_needs_the_users_key(self) -> None:
        from lynxx.tags_updater import _rx_auth
        for bad in ('', 'only-a-key'):
            with self.subTest(key=bad), self.assertRaises(ValueError):
                _rx_auth(bad)
        self.assertEqual('&api_key=K&user_id=9', _rx_auth('K, 9'))

    def test_tags_command(self) -> None:
        # 'lynxx-core tags' / python -m lynxx.tags_updater: the app's scheduled update. Keys: --api-key, else LYNXX_API_KEY_<MODULE>
        from lynxx import tags_updater
        seen: dict[str, str] = {}
        references: dict[str, pathlib.Path | None] = {}

        def fake_update(module, out_dir, *, api_key='', reference=None, **_kw):
            seen[module] = api_key
            references[module] = reference
            if module == 'rx':
                tags_updater._rx_auth(api_key)
            res = tags_updater.UpdateResult(module, out_dir / f'{module}_tags.json')
            res.message = 'written 3 tags (was 0)'
            return res

        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tags_updater, 'update_tag_list', fake_update), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            log = pathlib.Path(tmp) / 'logs.txt'
            with mock.patch.dict(os.environ, {'LYNXX_API_KEY_RX': '', 'LYNXX_API_KEY_DB': 'DBKEY,me'}):
                code = tags_updater.main(['--modules', 'db,rx', '--out', tmp, '--log', str(log)])
            self.assertEqual(1, code)  # rx without a key fails, db still updated
            self.assertEqual({'db': 'DBKEY,me', 'rx': ''}, seen)
            # the sanity check compares with the list the app uses now (the bundled one here), not with an empty folder
            self.assertEqual('rx_tags.json', references['rx'].name if references['rx'] else None)
            text = log.read_text(encoding='utf-8')
            self.assertIn('db: written 3 tags', text)
            self.assertIn('rx: FAILED: rx needs your own API key', text)
            with mock.patch.dict(os.environ, {'LYNXX_API_KEY_RX': 'K,1'}):
                self.assertEqual(0, tags_updater.main(['--modules', 'rx', '--out', tmp]))
            self.assertEqual('K,1', seen['rx'])


class PartFileTests(TestCase):  # A7
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        (self.root / 'srv').mkdir()
        (self.root / 'srv' / 'f.jpg').write_bytes(os.urandom(150_000))
        (self.root / 'out').mkdir()
        self.dwn = _make_downloader(ProcModule.DB)
        me = current_process()
        me.killed = False
        self.dwn.reset_root_thread(me)
        self.dwn.retries = 2

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_complete_download_renames_part(self) -> None:
        dest = self.root / 'out' / 'db_1.jpg'
        with _http_server(self.root / 'srv') as base:
            res = self.dwn.download_file(f'{base}f.jpg', 'db_1', dest, DownloadModes.FULL, False)
        self.assertEqual(150_000, res.file_size)
        self.assertEqual((self.root / 'srv' / 'f.jpg').read_bytes(), dest.read_bytes())
        self.assertFalse(dest.with_name(dest.name + PART_SUFFIX).exists())

    def test_interrupted_download_leaves_nothing_under_final_name(self) -> None:
        dest = self.root / 'out' / 'db_2.jpg'
        from lynxx.defines import ThreadInterruptException
        with _http_server(self.root / 'srv') as base, mock.patch('lynxx.network.os.replace', side_effect=ThreadInterruptException):
            with self.assertRaises(DownloadInterruptException):
                self.dwn.download_file(f'{base}f.jpg', 'db_2', dest, DownloadModes.FULL, False)
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_name(dest.name + PART_SUFFIX).exists())

    def test_existing_file_kept_and_touch_does_not_truncate(self) -> None:
        dest = self.root / 'out' / 'db_3.jpg'
        dest.write_bytes(b'keep me')
        res = self.dwn.download_file('http://127.0.0.1:9/never', 'db_3', dest, DownloadModes.FULL, False)
        self.assertEqual(7, res.file_size)
        self.dwn.download_file('http://127.0.0.1:9/never', 'db_3', dest, DownloadModes.TOUCH, False)
        self.assertEqual(b'keep me', dest.read_bytes())

    def test_existing_filter_ignores_part_and_empty_files(self) -> None:
        out = self.root / 'out'
        (out / 'db_10.jpg').write_bytes(b'x')
        (out / 'db_11.jpg.part').write_bytes(b'x')
        (out / 'db_12.jpg').write_bytes(b'')
        (out / 'img').mkdir()
        (out / 'img' / 'db_13.png').write_bytes(b'x')
        d = self.dwn
        d.dest_base, d.subfolder_img = out, 'img'
        d.items_raw_per_task[:] = [f'<post_id="{i}" file_url="x">' for i in (10, 11, 12, 13, 14)]
        d.total_count = 5
        d._filter_existing_items()
        self.assertEqual(['11', '12', '14'], [d._extract_id(h) for h in d.items_raw_per_task])


class DuplicatesTests(TestCase):  # A8
    def test_last_byte_is_compared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            payload = os.urandom(4096)
            (base / 'a.jpg').write_bytes(payload + b'A')
            (base / 'b.jpg').write_bytes(payload + b'B')
            (base / 'c.png').write_bytes(b'1')
            (base / 'd.png').write_bytes(b'2')
            (base / 'e.gif').write_bytes(payload + b'A')
            found: dict[str, list[pathlib.Path]] = {}
            find_duplicated_files(found, base, 0, 'first')
            dupes = {pathlib.Path(k).name: sorted(p.name for p in v) for k, v in found.items()}
            self.assertEqual({'a.jpg': ['e.gif']}, dupes)


class SecretsTests(TestCase):  # A4
    def test_argparse_never_echoes_secrets(self) -> None:
        for args in (['-api_key', 'TOPSECRETVALUE1234'], ['-cookies', '{TOPSECRETVALUE1234'], ['-proxy', 'ftp://TOPSECRETVALUE1234:1']):
            err = io.StringIO()
            with self.subTest(args=args[0]), contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                prepare_arglist(['-module', 'rx', *args, 'x'])
            self.assertNotIn('TOPSECRETVALUE1234', err.getvalue())

    def test_secret_args_from_environment(self) -> None:
        with mock.patch.dict(os.environ, {ENV_SECRET_ARGS: json.dumps(['-api_key', 'a' * 20 + ',login'])}):
            self.assertEqual(['-api_key', 'a' * 20 + ',login'], _pop_secret_args())
            self.assertNotIn(ENV_SECRET_ARGS, os.environ)

    def test_sidecar_redaction(self) -> None:
        from lynxx import server
        text = server._redact('GET https://x/posts.json?tags=a&login=bob&api_key=abc123 and -api_key KEY,USER')
        self.assertNotIn('abc123', text)
        self.assertNotIn('bob', text)
        self.assertNotIn('KEY,USER', text)


class SidecarRobustnessTests(TestCase):
    def test_non_object_json_lines_do_not_kill_sidecar(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, 'LYNXX_DATA_DIR': tmp, 'PYTHONPATH': str(ROOT)}
            payload = '[]\n42\n\xff\n{"jsonrpc":"2.0","id":7,"method":"app.info"}\n{"jsonrpc":"2.0","method":"app.shutdown"}\n'
            r = subprocess.run([sys.executable, '-m', 'lynxx.server'], input=payload.encode('utf-8', 'surrogateescape'),
                               capture_output=True, cwd=ROOT, env=env, timeout=120)
            msgs = [json.loads(line) for line in r.stdout.decode().splitlines() if line.strip()]
            self.assertTrue(any(m.get('id') == 7 and 'result' in m for m in msgs), r.stderr.decode()[-500:])
            self.assertEqual(2, sum(1 for m in msgs if m.get('error', {}).get('code') == -32600))

    def test_preview_fails_closed_without_tags(self) -> None:  # A5
        from lynxx import server
        from lynxx.download_db import DownloaderDb
        from lynxx.network import HTMLContent
        posts = json.dumps([{'id': 1, 'tag_string': '1girl solo', 'file_ext': 'jpg', 'file_url': 'https://cdn/x.jpg',
                             'preview_file_url': 'https://cdn/p.jpg', 'created_at': '2026-11-16T00:00:00.000-05:00'}]).encode()
        with mock.patch.object(DownloaderDb, 'fetch_html', return_value=HTMLContent(None, posts)), \
                mock.patch.object(DownloaderDb, '_can_extract_item_info_without_fetch', return_value=False), \
                mock.patch.object(server, 'notify'):
            res = server.m_search_preview({'module': 'db', 'tags': 'x', 'args': ['-gender', 'only_female,unknown', '-safety_blacklist']})
        self.assertEqual([], res['items'])
        self.assertEqual(1, res['hiddenNoTags'])
        self.assertNotIn('p.jpg', json.dumps(res))
        # without -safety_blacklist (the default, even with the gender filter) nothing is hidden for safety
        with mock.patch.object(DownloaderDb, 'fetch_html', return_value=HTMLContent(None, posts)), \
                mock.patch.object(DownloaderDb, '_can_extract_item_info_without_fetch', return_value=False), \
                mock.patch.object(server, 'notify'):
            res2 = server.m_search_preview({'module': 'db', 'tags': 'x', 'args': ['-gender', 'only_female,unknown']})
        self.assertEqual((['1'], 0), ([it['id'] for it in res2['items']], res2['hiddenNoTags']))

#
#
#########################################
