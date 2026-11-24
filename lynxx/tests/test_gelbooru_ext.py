# coding=UTF-8
"""
Lynxx: the Gelbooru extension (extensions/gelbooru, gb 1.0.2) against answers of gelbooru.com recorded with the owner's API key on
2026-11-21 (lynxx/tests/fixtures/gelbooru/, SOURCES.json holds their addresses and sha256; the key is in none of them).
The site's DAPI answers posts and tags as child elements, not the attributes of rule34.xxx: the downloader turns each post into the
attribute item string DownloaderGelbooru reads (ids, file urls, tags, dates; the sidecar's Preview and count), posts with
attributes are read as before; a task of up to 20,000 posts is read by page numbers with any search, a bigger one by id cursor
when the core can (one task, default order, no date limits) and refused otherwise; the tag list update reads the json tag index
sorted by post count down to its floor (20 posts) through the core's TagJob (robots.txt, pacing, no cookies), and says when the
site wants the user's key or answers something else.
Offline. Run: python -m unittest lynxx.tests.test_gelbooru_ext
"""
#########################################
#
#

import functools
import hashlib
import html
import importlib.util
import json
import os
import pathlib
import re
import subprocess
import tempfile
import types
from unittest import TestCase, mock

from requests.structures import CaseInsensitiveDict

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx import server, tags_updater  # noqa: E402
from lynxx.download_gelbooru import DownloaderGelbooru  # noqa: E402
from lynxx.downloaders import DOWNLOADERS_BY_PROC_MODULE  # noqa: E402
from lynxx.extensions import SOURCE_CATALOG, Extensions  # noqa: E402
from lynxx.modspec import ExtensionManifest  # noqa: E402
from lynxx.module import ProcModule  # noqa: E402
from lynxx.network import HTMLContent  # noqa: E402
from lynxx.tags_updater import PoliteClient, read_tag_list, update_tag_list  # noqa: E402
from lynxx.tagsdb import TagsDB  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
GB = ROOT / 'extensions' / 'gelbooru'
FIXTURES = ROOT / 'lynxx' / 'tests' / 'fixtures' / 'gelbooru'
SITE = 'https://gelbooru.com/'
KEY = 'testkey0123456789,12'
'''made up: gelbooru.com answers its DAPI only with the user's key'''
AUTH = '&api_key=testkey0123456789&user_id=12'
POSTS = 'dapi_post_index_limit=2.xml'
TAGS_XML = 'dapi_tag_index_limit=1000.xml'
UNORDERED = ('dapi_tag_index_json_limit=3_pid=0.json', 'dapi_tag_index_json_limit=3_pid=1.json')
BY_COUNT = {pid: f'dapi_tag_index_json_count_desc_pid={pid:d}.json' for pid in (0, 1000, 3000)}
KEY_NEEDED = 'the site needs your own API key (KEY,USER_ID)'


def _fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _tag_page_url(pid: int) -> str:
    return f'{SITE}index.php?page=dapi&s=tag&q=index&json=1&orderby=count&order=desc&limit=100&pid={pid:d}{AUTH}'


def _attrs(h: str) -> dict[str, str]:
    """name="value" pairs of an item string (attribute values never hold '"')"""
    return dict(re.findall(r' (\w+)="([^"]*)"', h))


class _Response:
    def __init__(self, status: int, body: bytes, headers: dict) -> None:
        self.status_code, self._body, self.headers = status, body, CaseInsensitiveDict(headers)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def iter_content(self, n: int):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]


class _Session:
    """The site behind the core's PoliteClient: answers[url] = (status, body), robots.txt 404 (no rules), every other url 404"""

    def __init__(self, answers: dict[str, tuple[int, bytes]]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, dict]] = []

    def request(self, method, url, headers=None, **_):
        self.calls.append((url, dict(headers or {})))
        status, body = self.answers.get(url, (404, b'<html>not found</html>'))
        return _Response(status, body, {'content-type': 'application/json' if body[:1] in (b'{', b'[') else 'text/html'})


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, s: float) -> None:
        self.now += s

    def clock(self) -> float:
        return self.now


class _DataDir:
    """A temporary data dir for the class (no installed extension, no list), the repository's extension loaded as a catalog one"""
    tmp: tempfile.TemporaryDirectory
    env: mock._patch

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': cls.tmp.name})
        cls.env.start()
        os.environ.pop('LYNXX_TAGS_DIR', None)  # restored by the patch
        Extensions.ensure_loaded()
        Extensions._load_many(Extensions._scan(GB.parent, SOURCE_CATALOG))
        m = Extensions.loaded.get('gelbooru')
        assert m is not None and m.path.resolve() == GB.resolve(), f'gelbooru not loaded from {GB}'
        cls.manifest = m

    @classmethod
    def tearDownClass(cls) -> None:
        TagsDB.clear()
        cls.env.stop()
        cls.tmp.cleanup()


class GelbooruFixturesTests(TestCase):

    def test_fixtures_are_the_recorded_answers(self) -> None:
        sources = json.loads((FIXTURES / 'SOURCES.json').read_text(encoding='utf-8'))
        files = sources['files']
        self.assertEqual({POSTS, TAGS_XML, *UNORDERED, *BY_COUNT.values()}, set(files))
        for name, meta in files.items():
            data = _fixture(name)
            self.assertEqual((meta['bytes'], meta['sha256']), (len(data), hashlib.sha256(data).hexdigest()), name)
            self.assertTrue(meta['url'].startswith(f'{SITE}index.php?page=dapi&s='), name)
            self.assertTrue(meta['url'].endswith('&api_key=<KEY>&user_id=<UID>'), name)  # the owner's key is never written
            self.assertNotIn(b'api_key', data, name)
            self.assertNotIn(b'user_id', data, name)
        # byte for byte on every system: git never turns their line ends into CRLF
        rel = [(FIXTURES / name).relative_to(ROOT).as_posix() for name in files]
        attrs = subprocess.run(('git', 'check-attr', 'text', '--', *rel), cwd=ROOT, capture_output=True, timeout=60)
        if attrs.returncode == 0:  # else not a git checkout (a source archive)
            self.assertEqual({f'{r}: text: unset' for r in rel}, set(attrs.stdout.decode('utf-8').splitlines()))
        # the bug report: the xml tag index has child elements, the attribute reading of 1.0.1 found no tag in it
        xml = _fixture(TAGS_XML).decode('utf-8')
        self.assertIn('<tags type="array" limit="100" offset="0" count="1487082"><tag><id>827834</id><name>', xml)
        self.assertEqual(100, xml.count('<tag>'))
        self.assertEqual([], re.findall(r'<tag\s([^>]*?)/?>', xml))
        self.assertIn('<posts limit="2" offset="0" count="14630217"><post><id>15305114</id>', _fixture(POSTS).decode('utf-8'))

    def test_manifest_1_0_2_for_older_cores(self) -> None:
        raw = json.loads((GB / 'manifest.json').read_text(encoding='utf-8'))
        m = ExtensionManifest.load(GB / 'manifest.json', 'catalog')
        self.assertEqual(('gb', '1.0.2', '1.1', 'gb_tags.json'), (m.abbr, m.version, m.minSdkVersion, m.tagList))
        self.assertEqual({'entry': 'tags.py:read_tags'}, m.tagUpdate)
        self.assertEqual({'apiKey': True, 'apiKeyFormat': 'KEY,USER_ID'}, raw['auth'])
        self.assertEqual({'id', 'name', 'abbr', 'version', 'apiVersion', 'kind', 'default', 'site', 'media', 'tagList', 'entry',
                          'minSdkVersion', 'description', 'syntax', '$generator', 'auth', 'tagUpdate'}, set(raw), 'no new manifest key')


def _page_items(dwn, body: bytes) -> list[str]:
    """The item strings of a search page, read the way the core reads them (download.py _fetch_page_by_address)"""
    page = HTMLContent(None, body)
    return [str(i) for i in dwn._argswitch2(dwn._get_all_post_tags, page.raw, page.html, dwn._parses_raw_pages())]


class GelbooruPostsTests(_DataDir, TestCase):

    def setUp(self) -> None:
        self.cls = DOWNLOADERS_BY_PROC_MODULE[ProcModule.PROC_MODULES_BY_NAME['gb']]
        self.dwn = self.cls()

    def test_element_posts_become_the_items_the_core_reads(self) -> None:
        body = _fixture(POSTS)
        items = _page_items(self.dwn, body)
        self.assertEqual(['15305114', '15305113'], [self.dwn._extract_id(h) for h in items])
        h = items[0]
        self.assertTrue(h.startswith('<post id="15305114" created_at="Sat Nov 21 00:31:00 -0600 2026" score="0" '), h[:120])
        img = 'https://img4.gelbooru.com/{}/bf/ad/{}bfadbac153594f6fa94aff48b5760103.jpg'
        self.assertEqual((img.format('images', ''), 'jpg'), self.dwn._extract_file_url(h))
        self.assertEqual((img.format('images', ''), 'jpg'), self.dwn._get_image_address(h))
        self.assertEqual(img.format('thumbnails', 'thumbnail_'), self.dwn.extract_preview_url(h))
        self.assertEqual(img.format('samples', 'sample_'), server._attr(h, 'sample_url'))
        self.assertEqual(('general', 'false'), (server._attr(h, 'rating'), server._attr(h, 'has_children')))
        self.assertFalse(self.dwn._is_video(h))
        self.assertFalse(self.dwn._has_comments(h))
        self.assertEqual('21-11-2026', self.dwn._extract_post_date(h))
        info = self.dwn._extract_item_info(h)
        self.assertEqual(('15305114', '1588', '2048', 'jpg', '0', 'bfadbac153594f6fa94aff48b5760103', '21-11-2026'),
                         (info.id, info.width, info.height, info.ext, info.score, info.md5, info.date))
        self.assertEqual('https://x.com/EcclesiaFeriae/status/2123525385648242814', info.source)
        self.assertEqual('', info.parent_id, '<parent_id>0</parent_id>: no parent (not a post 0 to fetch with -include_parchi)')
        self.assertEqual('false', info.has_children)
        tags = re.search(r'<tags>([^<]*)</tags>', body.decode('utf-8')).group(1).split()
        self.assertEqual(28, len(tags))
        self.assertEqual(tags, info.tags.split(' '))
        self.assertEqual(['2boys', 'arthur_morgan'], tags[:2])
        # every field of the post, in the site's order after the id; nothing but the item string is needed (no request)
        self.assertEqual(['id', 'created_at', 'score', 'width', 'height', 'md5', 'directory', 'image', 'rating', 'source', 'change',
                          'owner', 'creator_id', 'parent_id', 'sample', 'preview_height', 'preview_width', 'tags', 'title', 'has_notes',
                          'has_comments', 'file_url', 'preview_url', 'sample_url', 'sample_height', 'sample_width', 'status',
                          'post_locked', 'has_children'], list(_attrs(h)))
        self.assertEqual(('4096', '4096', '1970s_(style)'), (_attrs(items[1])['width'], _attrs(items[1])['height'],
                                                             _attrs(items[1])['tags'].split()[0]))

    def test_element_fields_with_markup_characters(self) -> None:
        post = ('<post><id>77</id><created_at>Mon Jan 06 21:51:58 -0600 2025</created_at><score>12</score><width>640</width>'
                '<height>360</height><md5>0123456789abcdef0123456789abcdef</md5><rating>explicit</rating>'
                '<source>https://example.com/?a=1&amp;b=&quot;2&quot;\nnext</source><parent_id>1234</parent_id>'
                '<tags> &gt;_&lt;  tom_&amp;_jerry  it&#039;s </tags><title>say "hi" id="9"</title><has_comments>true</has_comments>'
                '<has_children>true</has_children><file_url>https://img4.gelbooru.com/images/aa/bb/x.webm</file_url>'
                '<preview_url>https://img4.gelbooru.com/thumbnails/aa/bb/thumbnail_x.jpg</preview_url><sample_url/></post>')
        body = f'<?xml version="1.0" encoding="UTF-8"?>\n<posts limit="100" offset="0" count="1">{post}</posts>'.encode()
        items = _page_items(self.dwn, body)
        self.assertEqual(1, len(items))
        h = items[0]
        self.assertEqual('77', self.dwn._extract_id(h), 'a title holding id="9" is no id: values never hold \'"\'')
        a = _attrs(h)
        self.assertEqual(('>_< tom_&_jerry it\'s', "say 'hi' id='9'", "https://example.com/?a=1&b='2' next", ''),
                         (a['tags'], a['title'], a['source'], a['sample_url']))
        self.assertTrue(self.dwn._is_video(h))
        self.assertTrue(self.dwn._has_comments(h))
        self.assertEqual(('https://img4.gelbooru.com/images/aa/bb/x.webm', 'webm'), self.dwn._get_video_address(h))
        info = self.dwn._extract_item_info(h)
        self.assertEqual(('1234', 'true', '06-01-2025', '12', 'webm'), (info.parent_id, info.has_children, info.date, info.score, info.ext))
        self.assertEqual(['>_<', 'tom_&_jerry', "it's"], info.tags.split(' '))
        # a post without a numeric id is left out, its page still read
        bad = post.replace('<id>77</id>', '<id></id>')
        self.assertEqual(['77'], [self.dwn._extract_id(x) for x in _page_items(self.dwn, body.replace(b'</posts>', f'{bad}</posts>'.encode()))])

    def test_attribute_posts_and_empty_pages_read_as_before(self) -> None:
        # rule34.xxx's form (other answers of the DAPI): what DownloaderGelbooru reads from the core's html tree, unchanged
        body = (b'<?xml version="1.0" encoding="UTF-8"?><posts count="2" offset="0">'
                b'<post height="720" score="5" file_url="https://img.example/images/1/abc.mp4" parent_id="" sample_url="https://img.example/'
                b'samples/1/sample_abc.jpg" sample_width="850" sample_height="478" preview_url="https://img.example/thumbnails/1/'
                b'thumbnail_abc.jpg" rating="e" tags=" a_tag b_tag " id="10" width="1280" change="1700000000" md5="abc" creator_id="1" '
                b'has_children="true" created_at="Mon Jan 06 21:51:58 +0000 2020" status="active" source="" has_notes="false" '
                b'has_comments="false" preview_width="150" preview_height="84"/>'
                b'<post height="10" score="0" file_url="https://img.example/images/1/def.png" parent_id="10" rating="s" tags=" c_tag " '
                b'id="9" width="10" created_at="Sun Jan 05 01:00:00 +0000 2020" has_children="false" has_comments="true"/></posts>')
        items = _page_items(self.dwn, body)
        page = HTMLContent(None, body)
        self.assertEqual([str(p) for p in DownloaderGelbooru._get_all_post_tags(self.dwn, page.html)], items)
        self.assertEqual(['10', '9'], [self.dwn._extract_id(h) for h in items])
        self.assertEqual([True, False], [self.dwn._is_video(h) for h in items])
        self.assertEqual(['06-01-2020', '05-01-2020'], [self.dwn._extract_item_info(h).date for h in items])
        self.assertEqual(['a_tag b_tag', 'c_tag'], [self.dwn._extract_item_info(h).tags for h in items])
        for empty in (b'<?xml version="1.0" encoding="UTF-8"?><posts count="0" offset="0"></posts>',
                      b'<?xml version="1.0" encoding="UTF-8"?><posts limit="100" offset="0" count="0"/>'):
            self.assertEqual([], _page_items(self.dwn, empty))

    def _depth_run(self, tags: str, count: int, *extra: str) -> tuple[dict, list[str]]:
        """A task of `tags` whose search counts `count` posts, run offline by the core (its launch, arguments and task split): the
        posts the streaming pipeline reads ('read'), or the page range _resolve_task_pages gives the classic one ('pages', None when
        refused). Every search page is the recorded one with that count; the site keeps the order asked for ('sort:id:asc': oldest
        first) and has nothing past the recorded posts by id cursor. Returns what the core got (and its log) and the addresses asked"""
        asked: list[str] = []
        logs: list[str] = []
        page = _fixture(POSTS).replace(b'count="14630217"', f'count="{count:d}"'.encode())
        posts = re.findall(rb'<post>.*?</post>', page, re.DOTALL)
        oldest_first = page.replace(b''.join(posts), b''.join(reversed(posts)))
        nothing = b'<?xml version="1.0" encoding="UTF-8"?><posts limit="100" offset="0" count="0"/>'

        def fetch(_, url: str, **_kw) -> HTMLContent:
            asked.append(url.replace(AUTH, ''))
            return HTMLContent(None, nothing if re.search(r'id:[<>]', url) else oldest_first if 'sort:id:asc' in url else page)

        def run(dwn, args) -> dict:
            got: dict = {}

            def task() -> None:
                got['tasks'] = list(dwn.tags_str_arr)
                if dwn._can_stream_task():
                    dwn._process_tags_streaming(dwn.tags_str_arr[0])
                elif dwn._resolve_task_pages(dwn._consume_custom_module_tags(dwn.tags_str_arr[0])):
                    got['pages'] = (dwn.minpage, dwn.maxpage)
            dwn._stream_run = lambda batches: got.setdefault('read', [x for items, *_ in batches for x in items])
            dwn._launch(args, task)
            got['error'], got['log'] = dwn.launch_error, '\n'.join(logs)
            return got
        with mock.patch.object(self.cls, 'fetch_html', fetch), mock.patch.object(server, 'notify'), \
                mock.patch.object(Logger, 'log', lambda message, *_: logs.append(message)):
            return server._with_downloader('gb', tags, ['-api_key', KEY, *extra], run), asked

    def test_page_numbers_read_20000_posts_then_the_id_cursor(self) -> None:
        # live 2026-11-21: 'limit=1&pid=20001' answered this text (39 bytes, no posts); 'id:<N' and 'id:>N sort:id:asc' answered posts.
        # so did 'limit=100&pid=201' (offset 20,100): an offset limit, pages of 100 end at pid 199 (offset 19,900)
        too_deep = b'Too deep! Pull it back some. Holy fuck.'
        self.assertEqual([], _page_items(self.dwn, too_deep))
        self.assertEqual(20001, self.dwn._get_max_search_depth(), 'the core refuses page numbers from this many posts on')
        self.assertEqual((False, True), (self.dwn._prefer_id_cursor(20000), self.dwn._prefer_id_cursor(20001)))
        search = f'{SITE}index.php?page=dapi&s=post&q=index&tags=cat_ears'
        sort_score, mindate, or_group, parchi = (('cat_ears sort:score', '-dlimit', '100'), ('cat_ears', '-mindate', '01-11-2026'),
                                                 ('cat_ears (dog_ears~fox_ears)',), ('cat_ears', '-include_parchi'))
        # 20,000 posts: pages 0..199, by every search (streamed: default order, sort:, date limits; classic: 'or', -include_parchi)
        for tags, *extra in (('cat_ears',), sort_score, mindate):
            with self.subTest(tags=tags, extra=extra):
                got, asked = self._depth_run(tags, 20000, *extra)
                self.assertEqual('', got['error'])
                self.assertEqual(400, len(got['read']), 'the 2 recorded posts of each page')
                self.assertEqual(set(range(200)), {int(re.search(r'&limit=100&pid=(\d+)$', u).group(1)) for u in asked})
                self.assertFalse([u for u in asked if 'id:' in u], 'no id cursor')
        for tags, *extra in (or_group, parchi):
            with self.subTest(tags=tags, extra=extra):
                got, _ = self._depth_run(tags, 20000, *extra)
                self.assertEqual(('', (0, 199)), (got['error'], got.get('pages')))
        # 20,001 posts: a single task in the default order without date limits goes on by id cursor (oldest first)
        got, asked = self._depth_run('cat_ears', 20001)
        self.assertEqual(('', ['15305113', '15305114']), (got['error'], [self.dwn._extract_id(h) for h in got['read']]))
        self.assertEqual([f'{search}&limit=100&pid=0', f'{search}+sort:id:asc&limit=100&pid=0',
                          f'{search}+id:>15305114+sort:id:asc&limit=100&pid=0'], asked)
        # the others have page numbers only: refused past 20,000 posts a task (specs.md), nothing read past the count
        for tags, *extra in (sort_score, mindate, or_group, parchi):
            with self.subTest(tags=tags, extra=extra):
                got, asked = self._depth_run(tags, 20001, *extra)
                self.assertEqual(('', None, None), (got['error'], got.get('read'), got.get('pages')))
                self.assertIn('too many results, can only fetch html for 201 pages! Try adding an ID filter.', got['log'])
                self.assertFalse([u for u in asked if not u.endswith('&limit=100&pid=0')], 'the count only')

    def test_preview_and_count_through_the_sidecar(self) -> None:
        urls: list[str] = []

        def fetch(_, url: str, **_kw) -> HTMLContent:
            urls.append(url)
            return HTMLContent(None, _fixture(POSTS))
        api = f'{SITE}index.php?page=dapi&s=post&q=index{AUTH}&tags=brother_and_sister+rating:general'
        with mock.patch.object(self.cls, 'fetch_html', fetch), mock.patch.object(server, 'notify'), \
                mock.patch.object(server, '_learn_from_preview') as learn:
            res = server.m_search_preview({'module': 'gb', 'tags': 'brother_and_sister rating:general', 'args': ['-api_key', KEY],
                                           'pageSize': 40})
            self.assertEqual([f'{api}&limit=40&pid=0'], urls)
            items = res['items']
            self.assertEqual(['15305114', '15305113'], [it['id'] for it in items])
            self.assertEqual(['https://img4.gelbooru.com/thumbnails/bf/ad/thumbnail_bfadbac153594f6fa94aff48b5760103.jpg',
                              'https://img4.gelbooru.com/thumbnails/2c/2f/thumbnail_2c2fe547b0bc1966abaf1535e663ec8d.jpg'],
                             [it['preview'] for it in items])
            self.assertEqual('https://img4.gelbooru.com/samples/bf/ad/sample_bfadbac153594f6fa94aff48b5760103.jpg', items[0]['sample'])
            self.assertEqual([('21-11-2026', 'general', 'jpg', '1588', '2048', 'bfadbac153594f6fa94aff48b5760103', False, 'image'),
                              ('21-11-2026', 'general', 'jpg', '4096', '4096', '2c2fe547b0bc1966abaf1535e663ec8d', False, 'image')],
                             [(it['date'], it['rating'], it['ext'], it['width'], it['height'], it['md5'], it['isVideo'], it['mediaKind'])
                              for it in items])
            self.assertEqual(28, len(items[0]['tags']))
            self.assertIn('red_dead_redemption_2', items[0]['tags'])
            self.assertEqual([False, False], [it['excluded'] for it in items])
            self.assertEqual(('gb', ['15305114', '15305113']), (learn.call_args.args[0], [it['id'] for it in learn.call_args.args[1]]))
            urls.clear()
            count = server.m_search_count({'module': 'gb', 'tags': 'brother_and_sister rating:general', 'args': ['-api_key', KEY]})
            self.assertEqual(14630217, count['count'])  # '<posts limit="2" offset="0" count="14630217">'
            self.assertEqual([f'{api}&limit=100&pid=0'], urls)


class GelbooruTagJobTests(_DataDir, TestCase):

    def setUp(self) -> None:
        self.source = tags_updater.source_for_manifest(ExtensionManifest.load(GB / 'manifest.json', 'catalog'))
        self.job = self._load_job()

    @staticmethod
    def _load_job():
        spec = importlib.util.spec_from_file_location('lynxx_test_gelbooru_tags', GB / 'tags.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _by_count(self) -> dict[str, tuple[int, bytes]]:
        """the recorded pages sorted by post count as pages 0, 1 and 2 (ranks 0, 100,000 and 300,000)"""
        return {_tag_page_url(n): (200, _fixture(BY_COUNT[pid])) for n, pid in enumerate((0, 1000, 3000))}

    def _through_core(self, answers: dict[str, tuple[int, bytes]], min_posts: int = 1, key: str = KEY):
        """The job run by the core (TagJob, PoliteClient) with this min_posts; the clock is faked"""
        session, clock, notes = _Session(answers), _Clock(), []
        client = PoliteClient(self.source, delay=0.5, allow_local=True, session=session, sleep=clock.sleep, clock=clock.clock)
        client.notify = notes.append
        return list(tags_updater._iter_extension_job(self.source, client, key, min_posts)), session, client, notes

    def _fake_job(self, pages, api_key: str = KEY, min_posts: int = 1):
        """A TagJob stand in: pages(pid) -> text (pid read from the address)"""
        asked: list[tuple[str, str]] = []
        logs: list[str] = []

        def get_text(url: str, accept: str = '') -> str:
            asked.append((url, accept))
            return pages(int(re.search(r'&pid=(\d+)', url).group(1)))
        return types.SimpleNamespace(site=SITE, api_key=api_key, min_posts=min_posts, get_text=get_text, log=logs.append), asked, logs

    def test_the_list_by_post_count_through_the_core(self) -> None:
        got, session, client, notes = self._through_core(self._by_count())
        self.assertEqual([f'{SITE}robots.txt', _tag_page_url(0), _tag_page_url(1), _tag_page_url(2)], [u for u, _ in session.calls])
        self.assertEqual(['application/json'] * 3, [h.get('Accept') for _, h in session.calls[1:]])
        self.assertFalse([h for _, h in session.calls if 'Cookie' in h])
        self.assertEqual((1.0, 4), (client.interval, client.used))  # the core's floor for extension sites; robots.txt and 3 pages
        rows = [json.loads(_fixture(BY_COUNT[pid]))['tag'] for pid in (0, 1000)]
        self.assertEqual(['', '1girl'], [r['name'] for r in rows[0][:2]])  # rank 0: 14,115,642 posts without a name, skipped
        # 99 named tags of page 0 (82 of them deprecated, left out), the 100 of page 1 (94 and 93 posts, 4 deprecated); page 2 has
        # 15 posts each
        self.assertEqual(113, len(got))
        kept = [r for r in rows[0][1:] + rows[1] if r['type'] != 6]
        self.assertEqual([(html.unescape(r['name']), r['count']) for r in kept], [(n, c) for n, c, _ in got])
        self.assertEqual(sorted((c for _, c, _ in got), reverse=True), [c for _, c, _ in got])
        d = {name: (posts, kind) for name, posts, kind in got}
        self.assertEqual((9998846, 'general'), d['1girl'])
        self.assertEqual((9536589, 'metadata'), d['highres'])
        self.assertNotIn('1firl', d)  # 6 deprecated (8,540,770 posts): a misspelling, left out
        self.assertEqual((93, 'general'), d["jonathan_joestar's_pose"])  # 'jonathan_joestar&#039;s_pose' unescaped
        types_of = {row['type']: d[html.unescape(row['name'])][1] for row in rows[1] if row['type'] != 6}
        self.assertEqual({0: 'general', 1: 'artist', 3: 'copyright', 4: 'character'}, types_of)
        self.assertEqual(['tags with 20 or more posts, 100 a page sorted by post count',
                          '113 tags, down to 93 posts (86 deprecated left out)'], notes)

    def test_update_writes_the_list_with_types(self) -> None:
        # update_tag_list as the app runs it (tags.update, min_posts 1, delay 0.5), into <data>/tags
        clock, session = _Clock(), _Session(self._by_count())
        out = pathlib.Path(self.tmp.name) / 'tags'
        with mock.patch.object(tags_updater, 'PoliteClient', functools.partial(PoliteClient, sleep=clock.sleep, clock=clock.clock)):
            res = update_tag_list('gb', out, api_key=KEY, delay=0.5, source=self.source, allow_local=True, session=session)
        self.assertTrue(res.written, res.message)
        self.assertEqual((113, 4), (res.count, res.requests))
        written = read_tag_list(out / 'gb_tags.json')
        self.assertEqual(113, len(written))
        self.assertEqual({'general', 'artist', 'copyright', 'character', 'metadata'}, {t for _, t in written.values()})
        self.assertEqual((7754836, 'general'), written['solo'])
        self.assertNotIn('', written)

    def test_floor_is_min_posts_or_twenty(self) -> None:
        self.assertEqual(20, self.job.MIN_POSTS)
        got, session, _, notes = self._through_core(self._by_count(), min_posts=5000000)
        self.assertEqual([_tag_page_url(0)], [u for u, _ in session.calls[1:]], 'page 0 ends below 5,000,000 posts: no page 1')
        self.assertEqual((6, ('1girl', 9998846, 'general'), ('looking_at_viewer', 5495167, 'general')), (len(got), got[0], got[-1]))
        self.assertEqual('6 tags, down to 5495167 posts (22 deprecated left out)', notes[-1])
        job, asked, _ = self._fake_job(lambda pid: _fixture(BY_COUNT[3000]).decode('utf-8'), min_posts=0)
        self.assertEqual([], list(self.job.read_tags(job)), 'min_posts 0: still 20 (15 posts a tag there)')
        self.assertEqual(1, len(asked))

    def test_a_repeated_page_stops(self) -> None:
        job, asked, logs = self._fake_job(lambda pid: _fixture(BY_COUNT[0]).decode('utf-8'))  # a site that ignores pid
        got = list(self.job.read_tags(job))
        self.assertEqual(17, len(got))  # the 99 named tags of page 0 less 82 deprecated
        self.assertEqual([(_tag_page_url(0), 'application/json'), (_tag_page_url(1), 'application/json')], asked)
        self.assertIn('page 1 is the same as page 0: the site ignores pid, stopping', logs)

    def test_one_tag_is_an_object_and_an_empty_page_ends(self) -> None:
        one = {'@attributes': {'limit': 100, 'offset': 0, 'count': 1},
               'tag': {'id': 7, 'name': 'tom_&amp;_jerry', 'count': 50, 'type': 4, 'ambiguous': 0}}
        for empty in ({'@attributes': {'limit': 100, 'offset': 100, 'count': 1}}, [], {'@attributes': {}, 'tag': []}):
            with self.subTest(empty=empty):
                job, asked, _ = self._fake_job(lambda pid: json.dumps(one if pid == 0 else empty))
                self.assertEqual([('tom_&_jerry', 50, 'character')], list(self.job.read_tags(job)))
                self.assertEqual(2, len(asked))

    def test_the_site_wants_the_key(self) -> None:
        job, asked, _ = self._fake_job(lambda pid: '', api_key='')
        with self.assertRaises(RuntimeError) as ctx:
            list(self.job.read_tags(job))
        self.assertEqual(KEY_NEEDED, str(ctx.exception))
        self.assertEqual([], asked, 'no request without a key')
        for status, body in ((401, b'"Missing authentication. Go to api.gelbooru.com"'), (200, b'Missing authentication'),
                             (200, b'{"success": false, "message": "Invalid API key"}')):
            with self.subTest(status=status, body=body), self.assertRaises(RuntimeError) as ctx:
                self._through_core({_tag_page_url(0): (status, body)})
            self.assertTrue(str(ctx.exception).startswith(KEY_NEEDED), str(ctx.exception))
        self.assertIn("'Invalid API key'", str(ctx.exception))

    def test_unexpected_answers_are_quoted(self) -> None:
        page = '<!DOCTYPE html><html><head><title>Gelbooru</title></head><body>' + 'x' * 300 + '</body></html>'
        with self.assertRaises(RuntimeError) as ctx:
            self._through_core({_tag_page_url(0): (200, page.encode())})
        self.assertEqual(f'the tag index did not answer a json tag list: {page[:200]!r}', str(ctx.exception))
        # a later page: the most used tags read so far are kept, the log says what came
        job, asked, logs = self._fake_job(lambda pid: _fixture(BY_COUNT[0]).decode('utf-8') if pid == 0 else page)
        self.assertEqual(17, len(list(self.job.read_tags(job))))
        self.assertEqual(2, len(asked))
        self.assertIn(f'page 1 is not a json tag list, stopping: {page[:200]!r}', logs)
        # the index without an order (recorded): not sorted by post count, the floor would stop at a random tag
        job, _, _ = self._fake_job(lambda pid: _fixture(UNORDERED[pid]).decode('utf-8'))
        with self.assertRaises(RuntimeError) as ctx:
            list(self.job.read_tags(job))
        self.assertIn('not sorted by post count', str(ctx.exception))

#
#
#########################################
