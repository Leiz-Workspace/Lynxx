# coding=UTF-8
"""
Lynxx: tests of the e621 extension (extensions/e621) against the posts.json answers of the site, offline.
- e621's posts.json answered {"posts": [...]} until November 2026 and answers the list of posts itself since then (recorded
  2026-11-22, lynxx/tests/fixtures/e621/posts_2026-11-22.json, descriptions emptied): 'files' / 'stats' / 'has' and a flat
  'tags' list instead of 'file' / 'sample' / 'score' / tags by category. 1.0.2 read only the old shape ("Preview thất bại: list
  indices must be integers or slices, not str"); 1.0.3 reads both (_posts)
- a file the site hides from guests (no original url) is marked always blacklisted, as before
- Preview and Đếm through the sidecar on the recorded answer; a search page refused by a bot challenge says so in the error message
- 1.0.4: the video files the answer lists (files.video: original, variants, samples; before: sample.alternates) are kept, the
  downloaded one (video_url) is chosen by -max_vres (the best not above it, else the smallest) and at the same quality by -webm /
  -mp4, else the original; -lowres gets the sample of an image (the core's sample regex matches file_url)
Run: python -m unittest lynxx.tests.test_e621_ext
"""
#########################################
#
#

import json
import os
import pathlib
import sys
import tempfile
from types import SimpleNamespace
from unittest import TestCase, mock

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx import server  # noqa: E402
from lynxx.downloaders import DOWNLOADERS_BY_PROC_MODULE  # noqa: E402
from lynxx.extensions import SOURCE_CATALOG, Extensions  # noqa: E402
from lynxx.module import ProcModule  # noqa: E402
from lynxx.network import HTMLContent  # noqa: E402
from lynxx.tagsdb import TagsDB  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
EN = ROOT / 'extensions' / 'e621'
FIXTURE = ROOT / 'lynxx' / 'tests' / 'fixtures' / 'e621' / 'posts_2026-11-22.json'
ATTR = '<post_id="{id}"'
'''start of an item string of the downloader'''
VIDEO = 'https://static1.e621.net/data/{}98/01/9801493460200c9113243dd2933562df{}'
'''files of the recorded video post 6874802: original webm 1080, variants.mp4 1080 (_alt.mp4), samples 480p / 720p mp4'''
V_ORIGINAL, V_ALT = VIDEO.format('', '.webm'), VIDEO.format('sample/', '_alt.mp4')
V_480, V_720 = VIDEO.format('sample/', '_480p.mp4'), VIDEO.format('sample/', '_720p.mp4')


def _field(item: str, name: str) -> str:
    start = item.index(f' {name}="') + len(name) + 3
    return item[start:item.index('"', start)]


def _legacy_post(post_id: int, url: str | None = 'https://static1.e621.net/data/aa/bb/aabb.png') -> dict:
    """A post of the answer before November 2026"""
    return {'id': post_id, 'file': {'url': url, 'md5': 'aabb', 'width': 10, 'height': 20, 'ext': 'png'},
            'sample': {'url': 'https://static1.e621.net/data/sample/aa/bb/aabb.jpg', 'alternates': {}},
            'tags': {'general': ['solo'], 'species': ['fox'], 'artist': ['someone']}, 'sources': ['https://example.org/x'],
            'score': {'total': 7}, 'relationships': {'has_children': True, 'parent_id': 5}, 'created_at': '2024-01-02T03:04:05.000-05:00',
            'comment_count': 3, 'rating': 's'}


class E621Tests(TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': cls.tmp.name})
        cls.env.start()
        os.environ.pop('LYNXX_TAGS_DIR', None)  # restored by the patch
        Extensions.ensure_loaded()
        Extensions._load_many(Extensions._scan(EN.parent, SOURCE_CATALOG))
        m = Extensions.loaded.get('e621')
        assert m is not None and m.path.resolve() == EN.resolve(), f'e621 not loaded from {EN}'
        cls.cls = DOWNLOADERS_BY_PROC_MODULE[ProcModule.PROC_MODULES_BY_NAME['en']]
        cls.mod = sys.modules[cls.cls.__module__]

    @classmethod
    def tearDownClass(cls) -> None:
        TagsDB.clear()
        cls.env.stop()
        cls.tmp.cleanup()

    def items(self, answer, max_height: int = 0, webm: bool = False, mp4: bool = False, lowres: bool = False) -> list[str]:
        opts = SimpleNamespace(max_video_height=max_height, prefer_webm=webm, prefer_mp4=mp4, low_res=lowres)
        return self.cls._get_all_post_tags(opts, json.dumps(answer).encode())

    def downloaded(self, item: str, lowres: bool = False) -> str:
        """url the download of this item gets"""
        cls = self.cls  # no downloader object (sessions, index): the address methods with what they read
        dwn = SimpleNamespace(low_res=lowres, _extract_file_url=cls._extract_file_url, _extract_sample_url=cls._extract_sample_url)
        return (cls._get_video_address(dwn, item) if cls._is_video(dwn, item) else cls._get_image_address(dwn, item))[0]

    def test_manifest_1_0_4(self) -> None:
        self.assertEqual('1.0.4', json.loads((EN / 'manifest.json').read_text(encoding='utf-8'))['version'])
        self.assertLessEqual({'video_format_choice', 'video_quality_choice'}, set(self.cls.CAPABILITIES))
        self.assertTrue(all(isinstance(c, str) for c in self.cls.CAPABILITIES))

    def test_the_answer_since_november_2026(self) -> None:
        recorded = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertIsInstance(recorded, list, 'the list of posts itself, no {"posts": ...}')
        items = self.items(recorded)
        self.assertEqual(['6875008', '6874802', '6874926'], [_field(f' {it[1:]}', 'post_id') for it in items])
        image, video, hidden = items
        self.assertEqual('https://static1.e621.net/data/1b/cf/1bcfb0ea9aa309cce83645ac29934e55.png', _field(image, 'file_url'))
        self.assertEqual('https://static1.e621.net/data/sample/1b/cf/1bcfb0ea9aa309cce83645ac29934e55.jpg', _field(image, 'sample_url'))
        self.assertEqual(('2500', '2500', '1bcfb0ea9aa309cce83645ac29934e55'), (_field(image, 'height'), _field(image, 'width'),
                                                                               _field(image, 'md5')))
        self.assertEqual(recorded[0]['tags'], _field(image, 'tags').split(' '), 'the flat tag list, as the site gives it')
        self.assertEqual(('0', 'false', '', '0'), (_field(image, 'score'), _field(image, 'has_children'), _field(image, 'parent_id'),
                                                   _field(image, 'comment_count')))
        self.assertEqual('2026-11-22T02:12:24.441-05:00', _field(image, 'created_at'))
        self.assertTrue(_field(video, 'file_url').endswith('9801493460200c9113243dd2933562df.webm'))
        self.assertEqual(['webm', 'png'], [self.cls._extract_file_url(it)[1] for it in (video, image)])
        # hidden from guests: no original url, always blacklisted (never a url rebuilt from the md5)
        self.assertIn(self.mod.TAG_BLACKLISTED_ALWAYS, _field(hidden, 'tags').split(' '))
        self.assertTrue(_field(hidden, 'file_url').endswith('help/blacklist#default'))

    def test_the_answer_before_november_2026(self) -> None:
        items = self.items({'posts': [_legacy_post(11), _legacy_post(12, url=None)]})
        self.assertEqual(2, len(items))
        first = items[0]
        self.assertEqual(('https://static1.e621.net/data/aa/bb/aabb.png', '7', 'true', '5', '3'),
                         (_field(first, 'file_url'), _field(first, 'score'), _field(first, 'has_children'), _field(first, 'parent_id'),
                          _field(first, 'comment_count')))
        self.assertEqual(['solo', 'fox', 'someone'], _field(first, 'tags').split(' '))
        self.assertIn(self.mod.TAG_BLACKLISTED_ALWAYS, _field(items[1], 'tags').split(' '))

    def test_video_by_quality_and_format(self) -> None:
        recorded = json.loads(FIXTURE.read_text(encoding='utf-8'))
        for max_height, webm, mp4, expected in ((0, False, False, V_ORIGINAL), (1080, False, False, V_ORIGINAL),
                                                (2160, False, False, V_ORIGINAL), (0, False, True, V_ALT), (0, True, False, V_ORIGINAL),
                                                (1080, False, True, V_ALT), (720, False, False, V_720), (720, True, False, V_720),
                                                (719, False, False, V_480), (480, False, False, V_480), (360, False, False, V_480),
                                                (1, False, True, V_480)):
            with self.subTest(max_height=max_height, webm=webm, mp4=mp4):
                image, video, hidden = self.items(recorded, max_height, webm, mp4)
                self.assertEqual(expected, _field(video, 'video_url'))
                self.assertEqual(expected, self.downloaded(video))
                # the post stays the site's: original file, size and md5
                self.assertEqual((V_ORIGINAL, '1080', '9801493460200c9113243dd2933562df'),
                                 (_field(video, 'file_url'), _field(video, 'height'), _field(video, 'md5')))
                self.assertNotIn(' video_url="', image)
                self.assertNotIn(' video_url="', hidden)
        # the file gets the extension of the url downloaded; the post keeps the original's (Preview, info file)
        video = self.items(recorded, 480)[1]
        self.assertEqual(('mp4', 'webm'), (self.cls._get_video_address(None, video)[1], self.cls._extract_file_url(video)[1]))

    def test_video_files_listed_by_the_site_only(self) -> None:
        recorded = json.loads(FIXTURE.read_text(encoding='utf-8'))
        files = self.mod._video_files(recorded[1]['files']['video'], {'url': V_ORIGINAL, 'width': 1080, 'height': 1080})
        self.assertEqual([(1080, V_ORIGINAL), (1080, V_ALT), (480, V_480), (720, V_720)], files)
        # no video files listed (or junk): the original only, as 1.0.3 had it
        for junk in (None, {}, {'has': False}, {'samples': {'480p': {'url': 'http://x/a.mp4', 'height': 480}}},
                     {'samples': {'480p': {'url': 'https://x/a.jpg', 'height': 480}}}, {'samples': {'x': {'url': 'https://x/a.mp4'}}}):
            with self.subTest(junk=junk):
                self.assertEqual([(1080, V_ORIGINAL)], self.mod._video_files(junk, {'url': V_ORIGINAL, 'width': 1920, 'height': 1080}))
        # the 'p' number is the shorter side (a portrait video's width), else the name of the sample
        portrait = {'samples': {'720p': {'url': 'https://x/v_720p.mp4', 'width': 720, 'height': 1280}, '480p': {'url': 'https://x/v_480p.mp4'}}}
        self.assertEqual([(1080, V_ORIGINAL), (720, 'https://x/v_720p.mp4'), (480, 'https://x/v_480p.mp4')],
                         self.mod._video_files(portrait, {'url': V_ORIGINAL, 'width': 1080, 'height': 1920}))

    def test_video_choice_in_the_answer_before_november_2026(self) -> None:
        old = 'https://static1.e621.net/data/cc/dd/ccdd{}'
        post = {**_legacy_post(21, url=old.format('.webm')),
                'file': {'url': old.format('.webm'), 'md5': 'ccdd', 'width': 1920, 'height': 1080, 'ext': 'webm'}}
        alternates = {  # the shape before 2025: webm and mp4 urls of one quality
            'original': {'type': 'video', 'width': 1920, 'height': 1080, 'urls': [old.format('.webm'), old.format('.mp4')]},
            '720p': {'type': 'video', 'width': 1280, 'height': 720, 'urls': [old.format('_720p.webm'), old.format('_720p.mp4')]},
            '480p': {'type': 'video', 'width': 854, 'height': 480, 'urls': [None, old.format('_480p.mp4')]}}
        for max_height, webm, mp4, expected in ((0, False, False, '.webm'), (0, False, True, '.mp4'), (720, False, False, '_720p.webm'),
                                                (720, False, True, '_720p.mp4'), (480, True, False, '_480p.mp4'), (240, False, False, '_480p.mp4')):
            with self.subTest(max_height=max_height, webm=webm, mp4=mp4):
                item, = self.items({'posts': [{**post, 'sample': {**post['sample'], 'alternates': alternates}}]}, max_height, webm, mp4)
                self.assertEqual(old.format(expected), self.downloaded(item))
        # 2025 to November 2026: sample.alternates as files.video is now
        recorded = json.loads(FIXTURE.read_text(encoding='utf-8'))[1]
        newer = {**post, 'file': {'url': V_ORIGINAL, 'md5': 'x', 'width': 1080, 'height': 1080, 'ext': 'webm'},
                 'sample': {'url': None, 'alternates': recorded['files']['video']}}
        self.assertEqual([V_720, V_ALT], [self.downloaded(self.items({'posts': [newer]}, h, mp4=True)[0]) for h in (720, 0)])
        # hidden from guests: no original url, no file chosen
        hidden, = self.items({'posts': [{**_legacy_post(22, url=None), 'sample': {'url': None, 'alternates': alternates}}]}, 480)
        self.assertNotIn(' video_url="', hidden)

    def test_lowres_gets_the_sample_of_an_image(self) -> None:
        image, video, _ = self.items(json.loads(FIXTURE.read_text(encoding='utf-8')), 480, lowres=True)
        self.assertEqual('https://static1.e621.net/data/sample/1b/cf/1bcfb0ea9aa309cce83645ac29934e55.jpg', self.downloaded(image, True))
        self.assertEqual('https://static1.e621.net/data/1b/cf/1bcfb0ea9aa309cce83645ac29934e55.png', self.downloaded(image))
        # -lowres is for images only: a video keeps the file of -max_vres
        self.assertEqual(V_480, self.downloaded(video, True))
        self.assertEqual(V_ORIGINAL, self.downloaded(self.items(json.loads(FIXTURE.read_text(encoding='utf-8')), lowres=True)[1], True))

    def test_posts_of_any_answer(self) -> None:
        posts = self.mod._posts
        self.assertEqual([], posts({}))
        self.assertEqual([], posts({'posts': None}))
        self.assertEqual([], posts('nothing'))
        self.assertEqual([11], [p['id'] for p in posts({'post': _legacy_post(11)})])
        self.assertEqual([11], [p['id'] for p in posts([_legacy_post(11), 'junk'])], 'old posts inside a list are kept as they are')
        new = posts(json.loads(FIXTURE.read_text(encoding='utf-8')))
        self.assertEqual({'general'}, set(new[0]['tags']))
        self.assertIsNone(new[2]['file']['url'])

    def test_preview_and_count_through_the_sidecar(self) -> None:
        raw = FIXTURE.read_bytes()
        urls: list[str] = []

        def fetch(_, url: str, **_kw) -> HTMLContent:
            urls.append(url)
            return HTMLContent(None, raw)
        with mock.patch.object(self.cls, 'fetch_html', fetch), mock.patch.object(server, 'notify'), \
                mock.patch.object(server, '_learn_from_preview'):
            res = server.m_search_preview({'module': 'en', 'tags': 'solo', 'args': [], 'pageSize': 40})
            self.assertTrue(urls and urls[0].startswith('https://e621.net/posts.json?tags=solo'), urls)
            items = res['items']
            self.assertEqual(['6875008', '6874802'], [it['id'] for it in items if not it['excluded']][:2])
            self.assertEqual(('22-11-2026', 'png', '2500', False), (items[0]['date'], items[0]['ext'], items[0]['width'], items[0]['isVideo']))
            self.assertTrue(items[1]['isVideo'])
            urls.clear()
            count = server.m_search_count({'module': 'en', 'tags': 'solo', 'args': []})
            self.assertEqual(3, count['count'], 'one short page: its posts are the count')

    def test_a_challenge_says_so(self) -> None:
        # the error data (cfChallenge) does not reach the app over IPC: the message says it, the app shows what to do
        def challenged(dwn, _url: str, **_kw):
            dwn.cf_challenge_hit = True
            return None

        def failed(_dwn, _url: str, **_kw):
            return None
        with mock.patch.object(server, 'notify'):
            for fetch, said in ((challenged, True), (failed, False)):
                with self.subTest(said=said), mock.patch.object(self.cls, 'fetch_html', fetch), \
                        self.assertRaises(server.RpcError) as ctx:
                    server.m_search_preview({'module': 'en', 'tags': 'solo', 'args': [], 'pageSize': 40})
                self.assertTrue(str(ctx.exception).startswith('unable to fetch search page'))
                self.assertEqual(said, 'bot challenge' in str(ctx.exception), str(ctx.exception))

#
#
#########################################
