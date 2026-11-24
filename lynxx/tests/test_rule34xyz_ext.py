# coding=UTF-8
"""
Lynxx: tests of the rule34.xyz / rule34.world extensions (extensions/rule34xyz, extensions/rule34world, the same code) 1.1.1,
offline: the file a post's variants give for the app's image choice (-lowres) and video quality (-max_vres).
- The post json lists its variants ('files': variant id -> storage ids); the site's postFileResolverService names them (read on
  2026-11-22, lynxx/tests/fixtures/rule34xyz/SOURCES.json): 10 pic.jpg, 14 picsmall.jpg, 100 mov.mp4, 111 360.mp4, 112 mov480.mp4,
  113 mov720.mp4, 114 1080.mp4. A file is taken only when the post lists its variant (R6), and only when the post has the site's
  Download action (10 / 100): otherwise the post has no file, as in 1.1.0.
- Recorded video posts (trimmed: 3 tags, no sources): rule34.xyz 4713370 (1440 x 1000, lists 112 and 113), rule34.world 1439789
  (1280 x 720, lists 112). Image posts are SYNTHETIC: the recorded post with type 0, made in memory.
Run: python -m unittest lynxx.tests.test_rule34xyz_ext
"""
#########################################
#
#

import json
import os
import pathlib
import tempfile
from multiprocessing.dummy import current_process
from unittest import TestCase, mock

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx.cmdargs import prepare_arglist  # noqa: E402
from lynxx.defines import ModuleCaps  # noqa: E402
from lynxx.downloaders import DOWNLOADERS_BY_PROC_MODULE, _make_downloader  # noqa: E402
from lynxx.extensions import SOURCE_CATALOG, Extensions  # noqa: E402
from lynxx.module import ProcModule  # noqa: E402
from lynxx.network import HTMLContent  # noqa: E402
from lynxx.tags_parser import reset_last_tags  # noqa: E402
from lynxx.tagsdb import TagsDB  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / 'lynxx' / 'tests' / 'fixtures'
SITES = {
    'rz': ('rule34xyz', 'https://rule34.xyz/', 'https://rule34xyz.b-cdn.net/', 4713370),
    'rw': ('rule34world', 'https://rule34.world/', 'https://rule34storage.b-cdn.net/', 1439789),
}
'''abbr -> (extension id, site, storage 2, recorded video post)'''


class Rule34XyzTests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': cls.tmp.name})
        cls.env.start()
        os.environ.pop('LYNXX_TAGS_DIR', None)  # restored by the patch
        Extensions.ensure_loaded()
        Extensions._load_many(Extensions._scan(ROOT / 'extensions', SOURCE_CATALOG))
        for ext_id, *_ in SITES.values():
            m = Extensions.loaded.get(ext_id)
            assert m is not None and m.path.resolve() == (ROOT / 'extensions' / ext_id).resolve(), f'{ext_id} not loaded from the repo'

    @classmethod
    def tearDownClass(cls) -> None:
        TagsDB.clear()
        reset_last_tags()
        ProcModule.set(ProcModule.RX)
        cls.env.stop()
        cls.tmp.cleanup()

    @staticmethod
    def post(abbr: str, **changes) -> dict:
        ext_id, _, _, post_id = SITES[abbr]
        return {**json.loads((FIXTURES / ext_id / f'post_{post_id}.json').read_text(encoding='utf-8')), **changes}

    def downloader(self, abbr: str, max_height: int | None = None, low_res: bool = False):
        """a downloader of the module, the query parsed as a run parses it (no request); max_height None: a core without the
        attribute (before 2.0.14)"""
        pid = ProcModule.PROC_MODULES_BY_NAME[abbr]
        ProcModule.set(pid)
        reset_last_tags()
        dwn = _make_downloader(pid)
        me = current_process()
        me.killed = False
        dwn.reset_root_thread(me)
        dwn.no_conflict_delay = True
        dwn._parse_args(prepare_arglist(['-module', abbr, '--', 'solo']), False)
        vars(dwn).pop('max_video_height', None)
        if max_height is not None:
            dwn.max_video_height = max_height
        dwn.low_res = low_res
        return dwn

    def file(self, abbr: str, data: dict, max_height: int | None = None, low_res: bool = False) -> str:
        """the file name of the url downloaded ('' when none)"""
        url = self.downloader(abbr, max_height, low_res)._file_url(data)
        if url:
            storage = SITES[abbr][2]
            post_id = int(data['id'])
            prefix = f'{storage}posts/{post_id // 1000:d}/{post_id:d}/{post_id:d}.'
            self.assertTrue(url.startswith(prefix), url)
            return url[len(prefix):]
        return url

    def test_manifests_and_capability(self) -> None:
        for abbr, (ext_id, *_) in SITES.items():
            with self.subTest(ext_id):
                raw = json.loads((ROOT / 'extensions' / ext_id / 'manifest.json').read_text(encoding='utf-8'))
                self.assertEqual(('1.1.1', '1.1'), (raw['version'], raw['minSdkVersion']), 'no SDK 1.2 name: old cores keep loading it')
                caps = DOWNLOADERS_BY_PROC_MODULE[ProcModule.PROC_MODULES_BY_NAME[abbr]].CAPABILITIES
                self.assertIn('video_quality_choice', caps)
                self.assertNotIn('video_format_choice', caps, 'h264 MP4 only (the hevc / av1 variants are not used)')

    def test_video_by_quality(self) -> None:
        for abbr, max_height, name in (('rz', None, 'mov.mp4'), ('rz', 0, 'mov.mp4'), ('rz', 2160, 'mov.mp4'), ('rz', 1080, 'mov.mp4'),
                                       ('rz', 1000, 'mov.mp4'), ('rz', 999, 'mov720.mp4'), ('rz', 720, 'mov720.mp4'),
                                       ('rz', 719, 'mov480.mp4'), ('rz', 480, 'mov480.mp4'), ('rz', 360, 'mov480.mp4'),
                                       ('rw', None, 'mov.mp4'), ('rw', 0, 'mov.mp4'), ('rw', 1080, 'mov.mp4'), ('rw', 720, 'mov.mp4'),
                                       ('rw', 719, 'mov480.mp4'), ('rw', 480, 'mov480.mp4'), ('rw', 360, 'mov480.mp4')):
            with self.subTest(abbr=abbr, max_height=max_height):
                self.assertEqual(name, self.file(abbr, self.post(abbr), max_height))
                self.assertEqual(name, self.file(abbr, self.post(abbr), max_height, low_res=True), '-lowres is for images only')

    def test_only_variants_the_post_lists(self) -> None:
        post = self.post('rz')
        files = post['files']
        self.assertEqual('360.mp4', self.file('rz', {**post, 'files': {**files, '111': [2]}}, 360))
        self.assertEqual('1080.mp4', self.file('rz', {**post, 'width': 3840, 'height': 2160, 'files': {**files, '114': [2]}}, 1080))
        self.assertEqual('mov.mp4', self.file('rz', {**post, 'width': 1920, 'height': 1080, 'files': {**files, '114': [2]}}, 1080),
                         'a reduced file is never taken when the original fits')
        no_reduced = {k: v for k, v in files.items() if k not in ('112', '113')}
        self.assertEqual('mov.mp4', self.file('rz', {**post, 'files': no_reduced}, 480), 'none listed: the original')
        self.assertEqual('mov480.mp4', self.file('rz', {**post, 'files': {**files, '113': [0]}}, 720),
                         'a storage the extension does not know: that variant is not listed for it')
        # no Download action (variant 100), not posted, members only: no file at all, whatever the choice
        no_download = {k: v for k, v in files.items() if k != '100'}
        for data in ({**post, 'files': no_download}, {**post, 'status': 1},
                     {**post, 'tags': [*post['tags'], {'id': -10, 'value': 'sign in', 'type': 1}]}):
            for max_height in (0, 480):
                self.assertEqual('', self.file('rz', data, max_height))
        # portrait: the 'p' number is the shorter side
        self.assertEqual('mov.mp4', self.file('rz', {**post, 'width': 720, 'height': 1280}, 720))
        self.assertEqual('mov480.mp4', self.file('rz', {**post, 'width': 720, 'height': 1280}, 480))

    def test_image_lowres(self) -> None:
        for abbr in SITES:
            image = self.post(abbr, type=0)  # SYNTHETIC: the recorded post as an image post
            with self.subTest(abbr):
                self.assertEqual('pic.jpg', self.file(abbr, image))
                self.assertEqual('picsmall.jpg', self.file(abbr, image, low_res=True))
                self.assertEqual('pic.jpg', self.file(abbr, image, 480), 'the video quality does not change an image')
                no_small = {k: v for k, v in image['files'].items() if k != '14'}
                self.assertEqual('pic.jpg', self.file(abbr, {**image, 'files': no_small}, low_res=True), 'not listed: the original')
                self.assertEqual('', self.file(abbr, {**image, 'files': {'14': [2]}}, low_res=True), 'no Download action (10): no file')

    def test_item_info_and_download_take_the_same_file(self) -> None:
        for abbr, (ext_id, site, storage, post_id) in SITES.items():
            raw = (FIXTURES / ext_id / f'post_{post_id}.json').read_bytes()
            urls: list[str] = []

            def fetch(_, url: str, **_kw) -> HTMLContent | None:
                urls.append(url)
                return HTMLContent(None, raw) if url == f'{site}api/v2/post/{post_id}' else None
            cls = DOWNLOADERS_BY_PROC_MODULE[ProcModule.PROC_MODULES_BY_NAME[abbr]]
            with self.subTest(abbr), mock.patch.object(cls, 'fetch_html', fetch):
                dwn = self.downloader(abbr, 480)
                item = f'<post_id="{post_id}" video="1" preview_url="">'
                info = dwn._extract_item_info(item)
                self.assertEqual(('mp4', '1000' if abbr == 'rz' else '720'), (info.ext, info.height), 'the post keeps its own size')
                sent: list[tuple[str, str]] = []
                with mock.patch.object(cls, '_process_video', lambda _, url, item_id: sent.append((url, item_id))):
                    dwn._process_item(item)
                self.assertEqual([(f'{storage}posts/{post_id // 1000}/{post_id}/{post_id}.mov480.mp4', str(post_id))], sent)
                self.assertEqual({f'{site}api/v2/post/{post_id}'}, set(urls), 'the post json only (no other request)')

    def test_the_option_on_this_core(self) -> None:
        if not hasattr(ModuleCaps, 'VIDEO_QUALITY_CHOICE'):
            self.skipTest('this core has no -max_vres (before 2.0.14)')
        pid = ProcModule.PROC_MODULES_BY_NAME['rz']
        ProcModule.set(pid)
        reset_last_tags()
        dwn = _make_downloader(pid)
        me = current_process()
        me.killed = False
        dwn.reset_root_thread(me)
        dwn.no_conflict_delay = True
        dwn._parse_args(prepare_arglist(['-module', 'rz', '-max_vres', '720', '--', 'solo']), False)
        self.assertEqual(720, dwn.max_video_height, 'kept: the module declares video_quality_choice')
        self.assertTrue(dwn._file_url(self.post('rz')).endswith('/4713370.mov720.mp4'))

#
#
#########################################
