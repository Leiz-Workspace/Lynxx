# coding=UTF-8
"""
Lynxx: tests for features added on top of the original code (offline, no network)
Run: python -m unittest lynxx.tests.test_lynxx
"""
#########################################
#
#

import hashlib
import io
import json
import os
import pathlib
import tempfile
import zipfile
from email.utils import format_datetime
from multiprocessing.dummy import current_process
from unittest import TestCase, mock

from requests import Response

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx.cmdargs import prepare_arglist  # noqa: E402
from lynxx.downloaders import DOWNLOADERS_BY_PROC_MODULE, _make_downloader  # noqa: E402
from lynxx.extensions import ExtensionError, Extensions  # noqa: E402
from lynxx.modspec import ExtensionManifest, ManifestError  # noqa: E402
from lynxx.module import EXTERNAL_MODULE_ID_BASE, ProcModule  # noqa: E402
from lynxx.network import is_cloudflare_challenge, parse_retry_after  # noqa: E402
from lynxx.tags import TagTypes  # noqa: E402
from lynxx.tags_parser import RE_PLAINS, parse_tags, reset_last_tags  # noqa: E402
from lynxx.tags_updater import write_tag_list  # noqa: E402
from lynxx.tagsdb import TagsDB  # noqa: E402
from lynxx.task import extract_neg_and_groups  # noqa: E402
from lynxx.validators import valid_api_key, valid_proxy  # noqa: E402


def _response(status: int, headers: dict[str, str]) -> Response:
    r = Response()
    r.status_code = status
    r.headers.update(headers)
    return r


class TagsDBTests(TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        path = pathlib.Path(self.tmp.name)
        write_tag_list(path / 'zz_tags.json', {
            'big_breasts': (3752066, 'general'), 'bigger_version_available': (80, 'metadata'), 'big_the_cat': (1648, 'character'),
            'big_ass': (1924460, 'general'), 'bird': (70000, 'general'), 'zebra': (10, 'general'),
        })
        TagsDB.clear()
        TagsDB.DBFiles['zz'] = path / 'zz_tags.json'

    def tearDown(self) -> None:
        TagsDB.clear()
        self.tmp.cleanup()

    def test_autocomplete_ranks_by_posts_count(self) -> None:
        # regression: TagInfo sorts by tag type first, metadata tags used to outrank popular general tags
        res = TagsDB.autocomplete_tag('zz', 'big')
        self.assertEqual(['big_breasts', 'big_ass', 'big_the_cat', 'bigger_version_available'], [f'big{s}' for s, _ in res])

    def test_browse_prefix_and_filters(self) -> None:
        total, rows = TagsDB.browse('zz', 'bi', limit=2)
        self.assertEqual(5, total)
        self.assertEqual(['big_ass', 'big_breasts'], [n for n, _ in rows])
        total, rows = TagsDB.browse('zz', 'b', min_posts=10000, tag_types=[TagTypes.TAG_TYPE_GENERAL])
        self.assertEqual({'big_ass', 'big_breasts', 'bird'}, {n for n, _ in rows})


class ValidatorsTests(TestCase):
    def test_proxy(self) -> None:
        for good in ('socks5h://127.0.0.1:1080', 'socks5://proxy.local:9050', 'http://user:pw@10.0.0.1:3128', 'https://[::1]:8443'):
            self.assertEqual(good, valid_proxy(good))
        for bad in ('ftp://1.2.3.4:21', 'socks5://bad_host!:80', 'http://1.2.3.4:0'):
            with self.assertRaises(Exception):
                valid_proxy(bad)

    def test_api_key(self) -> None:
        self.assertEqual('a' * 128 + ',12345', valid_api_key('a' * 128 + ',12345'))  # rx
        self.assertEqual('AbCdEf1234567890xyz,some_login', valid_api_key('AbCdEf1234567890xyz,some_login'))  # danbooru
        with self.assertRaises(Exception):
            valid_api_key('short,1')


class NetworkHelpersTests(TestCase):
    def test_retry_after(self) -> None:
        self.assertEqual(0.0, parse_retry_after(None))
        self.assertEqual(5.0, parse_retry_after('5'))
        self.assertEqual(120.0, parse_retry_after('100000'))
        import datetime
        when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=30)
        self.assertTrue(20 <= parse_retry_after(format_datetime(when, usegmt=True)) <= 31)

    def test_cloudflare_challenge(self) -> None:
        self.assertTrue(is_cloudflare_challenge(_response(403, {'cf-mitigated': 'challenge'})))
        self.assertFalse(is_cloudflare_challenge(_response(403, {})))
        self.assertFalse(is_cloudflare_challenge(_response(200, {'cf-mitigated': 'challenge'})))

    def test_download_without_proxy(self) -> None:
        # regression: '-proxynodown' had no effect since sessions were unified (original rev 413)
        with _make_downloader(ProcModule.RX) as dwn:
            dwn.proxies = {'http': 'socks5://127.0.0.1:1', 'https': 'socks5://127.0.0.1:1'}
            dwn.ignore_proxy_dwn = True
            self.assertTrue(dwn.make_session().proxies)
            self.assertFalse(dwn.make_session(for_download=True).proxies)
            dwn.ignore_proxy_dwn = False
            self.assertTrue(dwn.make_session(for_download=True).proxies)


class DanbooruTagLimitTests(TestCase):
    def setUp(self) -> None:
        Extensions.ensure_loaded()
        ProcModule.set(ProcModule.DB)
        TagsDB.clear()

    def tearDown(self) -> None:
        ProcModule.set(ProcModule.RX)

    def test_free_metatags_do_not_count(self) -> None:
        required: list[str] = []
        tags, neg = extract_neg_and_groups('cat_ears solo rating:g id:>1000 score:>5', False, required)
        self.assertEqual(['cat_ears', 'solo', 'rating:g', 'id:>1000', 'score:>5'], tags)
        self.assertEqual([], required)
        self.assertEqual([], neg)

    def test_extra_positive_tags_are_checked_locally(self) -> None:
        required: list[str] = []
        tags, _ = extract_neg_and_groups('a b c d rating:g', False, required)
        self.assertEqual(['a', 'b', 'rating:g'], tags)
        self.assertEqual(['c', 'd'], required)

    def test_negative_tags_moved_to_local_group(self) -> None:
        required: list[str] = []
        tags, neg = extract_neg_and_groups('a b -c', False, required)
        self.assertEqual(['a', 'b'], tags)
        self.assertEqual(1, len(neg))
        self.assertTrue(any(p.fullmatch('c') for p in neg[0]))

    def test_parse_tags_db_syntax(self) -> None:
        reset_last_tags()
        ok, _ = parse_tags('cat_ears date:2024-01-01..2024-02-01 rating:g,s order:score')
        self.assertTrue(ok)


class DanbooruParsingTests(TestCase):
    POSTS = [
        {'id': 10, 'created_at': '2026-11-16T04:24:19.090-05:00', 'tag_string': 'cat_ears solo', 'file_ext': 'png', 'md5': 'ab',
         'file_url': 'https://cdn.donmai.us/original/ab.png', 'large_file_url': 'https://cdn.donmai.us/sample/ab.jpg',
         'preview_file_url': 'https://cdn.donmai.us/180x180/ab.jpg', 'image_width': 100, 'image_height': 50, 'score': 3,
         'has_children': True, 'parent_id': None, 'rating': 'g', 'source': 'https://x.com/"quoted"'},
        {'id': 11, 'created_at': '2026-11-15T01:00:00.000-05:00', 'tag_string': 'banned_artist', 'file_ext': 'jpg'},
        {'id': 12, 'created_at': '2026-11-14T01:00:00.000-05:00', 'tag_string': 'ugoira', 'file_ext': 'zip', 'md5': 'cd',
         'file_url': 'https://cdn.donmai.us/original/cd.zip', 'large_file_url': 'https://cdn.donmai.us/sample/cd.webm'},
    ]

    def test_posts_parsing(self) -> None:
        with _make_downloader(ProcModule.DB) as dwn:
            items = dwn._get_all_post_tags(json.dumps(self.POSTS).encode())
            self.assertEqual(3, len(items))
            info = dwn._extract_item_info(items[0])
            self.assertEqual(('10', 'png', '16-11-2026', 'true'), (info.id, info.ext, info.date, info.has_children))
            self.assertIn('cat_ears', info.tags)
            self.assertEqual('https://cdn.donmai.us/180x180/ab.jpg', dwn.extract_preview_url(items[0]))
            self.assertEqual(('https://cdn.donmai.us/sample/ab.jpg', 'jpg'), dwn._extract_sample_url(items[0]))
            self.assertIn('db_file_unavailable', items[1])
            self.assertEqual(('https://cdn.donmai.us/sample/cd.webm', 'webm'), dwn._extract_file_url(items[2]))
            self.assertTrue(dwn._is_video(items[2]))
            self.assertEqual([], dwn._get_all_post_tags(b'{"success":false,"message":"You cannot search for more than 2 tags at a time."}'))

    def test_custom_tags_and_dates(self) -> None:
        with _make_downloader(ProcModule.DB) as dwn:
            dwn.date_min, dwn.date_max = '01-01-2024', '01-02-2024'
            self.assertEqual('fav:someone+date:2024-01-01..2024-02-01', dwn._consume_custom_module_tags('favorited_by:someone'))

    def test_capabilities_resolve_conflicts(self) -> None:
        with _make_downloader(ProcModule.DB) as dwn:
            me = current_process()
            me.killed = False
            dwn.reset_root_thread(me)
            dwn.no_conflict_delay = True
            dwn._parse_args(prepare_arglist(['-module', 'db', '-include_parchi', '-dump_comments', 'cat_ears']), False)
            self.assertTrue(dwn.include_parchi)
            self.assertFalse(dwn.dump_comments)


class ManifestTests(TestCase):
    BASE = {'id': 'testsite', 'name': 'Test', 'abbr': 'zz', 'version': '1.0.0', 'entry': 'downloader.py:DownloaderZz'}

    def test_valid(self) -> None:
        m = ExtensionManifest.from_dict(dict(self.BASE), None, 'user')
        self.assertTrue(m.is_file_entry)

    def test_invalid(self) -> None:
        for patch in ({'abbr': 'ZZ!'}, {'version': '1'}, {'unknown': 1}, {'syntax': {'sort': '('}}, {'media': ['text']},
                      {'entry': 'downloader.py'}, {'apiVersion': 2}):
            with self.assertRaises(ManifestError, msg=str(patch)):
                ExtensionManifest.from_dict({**self.BASE, **patch}, None, 'user')


DOWNLOADER_PY = '''
from lynxx.download_db import DownloaderDb


class DownloaderZz(DownloaderDb):
    def _get_module_abbr(self) -> str:
        return 'zz'

    def _get_module_abbr_p(self) -> str:
        return 'zz_'
'''


class ExtensionInstallTests(TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': self.tmp.name})
        self.env.start()
        Extensions.ensure_loaded()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def _make_package(self, root: pathlib.Path, ext_id: str, abbr: str) -> pathlib.Path:
        src = root / ext_id
        src.mkdir(parents=True)
        (src / 'manifest.json').write_text(json.dumps({**ManifestTests.BASE, 'id': ext_id, 'abbr': abbr,
                                                       'syntax': {'maxTags': 2, 'localPositiveOverflow': True}}))
        (src / 'downloader.py').write_text(DOWNLOADER_PY.replace("'zz'", f"'{abbr}'").replace("'zz_'", f"'{abbr}_'"))
        return src

    def test_install_dir_registers_module(self) -> None:
        src = self._make_package(pathlib.Path(self.tmp.name) / 'src', 'testsite', 'zz')
        m = Extensions.install_from_dir(src)
        pid = ProcModule.PROC_MODULES_BY_NAME['zz']
        self.assertGreaterEqual(pid, EXTERNAL_MODULE_ID_BASE)
        self.assertIn(pid, DOWNLOADERS_BY_PROC_MODULE)
        self.assertIn(pid, RE_PLAINS)
        self.assertTrue((Extensions.user_dir() / 'testsite' / 'manifest.json').is_file())
        self.assertEqual('user', m.source)
        with _make_downloader(pid) as dwn:
            self.assertEqual('zz', dwn._get_module_abbr())
        Extensions.uninstall('testsite')
        self.assertFalse((Extensions.user_dir() / 'testsite').exists())

    def test_builtin_cannot_be_replaced_or_removed(self) -> None:
        src = self._make_package(pathlib.Path(self.tmp.name) / 'src', 'danbooru', 'qq')
        with self.assertRaises(ExtensionError):
            Extensions.install_from_dir(src)
        with self.assertRaises(ExtensionError):
            Extensions.uninstall('rule34')

    def test_archive_checksum_and_zip_slip(self) -> None:
        src = self._make_package(pathlib.Path(self.tmp.name) / 'src', 'archsite', 'zy')
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w') as zf:
            for f in src.iterdir():
                zf.write(f, f'archsite/{f.name}')
        data = buf.getvalue()
        with self.assertRaises(ExtensionError):
            Extensions.install_from_archive(data, '0' * 64)
        m = Extensions.install_from_archive(data, hashlib.sha256(data).hexdigest())
        self.assertEqual('archsite', m.id)
        evil = io.BytesIO()
        with zipfile.ZipFile(evil, 'w') as zf:
            zf.writestr('../../evil.txt', 'x')
        with self.assertRaises(ExtensionError):
            Extensions.install_from_archive(evil.getvalue(), hashlib.sha256(evil.getvalue()).hexdigest())

    def test_catalog_lists_optional_sites(self) -> None:
        rows = {r['abbr']: r for r in Extensions.list_all()}
        self.assertTrue(rows['rx']['installed'] and rows['db']['installed'])
        self.assertTrue(rows['rx']['default'] and rows['db']['default'])
        for abbr in ('rn', 'rs', 'rp', 'en', 'xb', 'bb'):
            self.assertIn(abbr, rows)
            self.assertFalse(rows[abbr]['default'])

#
#
#########################################
