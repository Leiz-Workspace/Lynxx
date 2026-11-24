# coding=UTF-8
"""
Lynxx: tests for search kinds ("Tim theo", Lynxx 2.0.11, SDK 1.2): the manifest 'search' and its 2.0.11 gate, kinds in the
query parser, listing kinds (local filters, site order, page size, robots of the extension), the sort syntax of a module as a
custom sort, kind tokens / metatag names never moved to local filters, the sidecar methods (autocomplete and fuzzy by type,
tags.kinds, validate errors, resolve, related, preview flag, files.identify), SafeFetcher pacing its robots.txt request.
Offline: a fake site behind an extension installed in a temporary data dir, in-memory tag lists.
Run: python -m unittest lynxx.tests.test_search_kinds
"""
#########################################
#
#

import contextlib
import importlib.util
import io
import itertools
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import types
from multiprocessing.dummy import current_process
from unittest import TestCase, mock
from urllib import parse as url_parse

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx import modspec, server  # noqa: E402
from lynxx.cmdargs import prepare_arglist  # noqa: E402
from lynxx.defines import ThreadInterruptException  # noqa: E402
from lynxx.download_db import DownloaderDb  # noqa: E402
from lynxx.downloaders import DOWNLOADERS_BY_PROC_MODULE, _make_downloader  # noqa: E402
from lynxx.extensions import Extensions  # noqa: E402
from lynxx.extgen.fetch import SafeFetcher  # noqa: E402
from lynxx.modspec import MANIFEST_KEYS_SINCE, ExtensionManifest, ManifestError  # noqa: E402
from lynxx.module import ProcModule  # noqa: E402
from lynxx.network import HTMLContent  # noqa: E402
from lynxx.sdk import SDK_VERSION, KindMatch, sdk_satisfies, split_kind  # noqa: E402
from lynxx.search_kinds import (  # noqa: E402
    _REGISTRY,
    ModuleSearch,
    find_kind,
    id_search_supported,
    is_module_meta,
    kind_of,
    kind_tokens,
    module_search,
)
from lynxx.tags import TagTypes  # noqa: E402
from lynxx.tags_parser import TagsError, convert_taglist, parse_error, parse_tags, reset_last_tags  # noqa: E402
from lynxx.tagsdb import TagsDB  # noqa: E402
from lynxx.task import _is_meta, extract_neg_and_groups  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
G, C, A, CP = TagTypes.TAG_TYPE_GENERAL, TagTypes.TAG_TYPE_CHARACTER, TagTypes.TAG_TYPE_ARTIST, TagTypes.TAG_TYPE_COPYRIGHT
SITE = 'https://kinds.example/'
ABBR = 'kq'

KQ_MANIFEST = {
    'id': 'kindsite', 'name': 'Kinds', 'abbr': ABBR, 'version': '1.1.0', 'entry': 'downloader.py:DownloaderKq', 'minSdkVersion': '1.2',
    'site': SITE, 'tagList': 'kq_tags.json',
    'syntax': {'maxTags': 2, 'localPositiveOverflow': True, 'metaCount': ':(?:[<>]=?|!)?[a-z\\d\\-_.,]+?', 'sort': 'order:[a-z_]+',
               'fav': 'favorited_by:[^:]+?', 'pool': 'pool:[^:]+?'},
    'search': {
        'kinds': [
            {'id': 'artist', 'token': 'artist:', 'value': '[a-z0-9_-]+', 'listing': True, 'pageSize': 24, 'tagType': 'artist'},
            {'id': 'category', 'token': 'category:', 'value': '[a-z0-9_]+', 'tagType': 'copyright'},
            {'id': 'uploader', 'token': 'user:', 'value': '[^\\s:]+', 'input': 'text'},
            {'id': 'pool', 'use': 'pool', 'input': 'id', 'order': 'site'},
            {'id': 'favorites', 'use': 'fav', 'input': 'text'},
        ],
        'metaNames': ['vote'],
        'idSearch': False,
    },
}
KQ_DOWNLOADER = '''
from lynxx.sdk import DownloaderJsonApi, JsonPost, ModuleCaps, split_kind


class DownloaderKq(DownloaderJsonApi):
    CAPABILITIES = frozenset((ModuleCaps.DATE_FILTER, ModuleCaps.PREVIEW))
    SITE = 'https://kinds.example/'
    ABBR = 'kq'
    ITEMS_PER_PAGE = 50
    MAX_PAGES = 100

    def _get_min_request_interval(self) -> float:
        return 0.0

    def _search_url(self, tags: str, page: int, limit: int) -> str:
        kind, rest = split_kind(tags)
        if kind is not None and kind.id == 'artist':  # its own listing, like a KVS /models/<x>/ page
            url = f'{self.SITE}models/{kind.value}/?from={page + 1:d}&limit={limit:d}'
            self.require_robots_allowed(url)
            return f'{url}&rest={rest}'
        return f'{self.SITE}posts.json?tags={tags}&limit={limit:d}&page={page + 1:d}'

    def _cursor_url(self, tags: str, cursor, ascending: bool, limit: int) -> str:
        extra = f'+order:{"id" if ascending else "id_desc"}' + ('' if cursor is None else f'+id:{">" if ascending else "<"}{cursor}')
        return self._search_url(f'{tags}{extra}', 0, limit)

    def _posts(self, data):
        return data if isinstance(data, list) else None

    def _post(self, p: dict):
        return JsonPost(id=str(p['id']), file_url=p['file_url'], tags=p['tags'].split(), created_at=p['created_at'],
                        preview_url=p['file_url'])
'''
KQ_ROBOTS = 'User-agent: *\nDisallow: *rape\nDisallow: /tags/*_*\nAllow: /\n'


class KqSite:
    """60 posts of artist 'foo' (every other one 'solo', every third one 'bar'), 30 of 'baz', newest first"""

    def __init__(self) -> None:
        self.posts = [{'id': 5000 - i, 'tags': ['foo', *(['solo'] if i % 2 == 0 else []), *(['bar'] if i % 3 == 0 else [])]}
                      for i in range(60)]
        self.posts += [{'id': 4000 - i, 'tags': ['baz', 'solo']} for i in range(30)]
        self.urls: list[str] = []

    def fetch(self, url: str, **_) -> HTMLContent:
        self.urls.append(url)
        u = url_parse.urlparse(url)
        q = url_parse.parse_qs(u.query)
        limit = int(q['limit'][0])
        if u.path.startswith('/models/'):
            name = u.path.split('/')[2]
            res = [p for p in self.posts if name in p['tags']]
            start = (int(q['from'][0]) - 1) * limit
        else:
            words = [t for t in q.get('tags', [''])[0].split(' ') if t]
            res = [p for p in self.posts if all(w in p['tags'] for w in words if ':' not in w and not w.startswith('-'))]
            if 'order:id' in words:
                res.reverse()
            for w in words:
                if w.startswith(('id:<', 'id:>')):
                    res = [p for p in res if (p['id'] < int(w[4:]) if w[3] == '<' else p['id'] > int(w[4:]))]
            start = (int(q['page'][0]) - 1) * limit
        return HTMLContent(None, json.dumps([
            {'id': p['id'], 'file_url': f'https://cdn.kinds.example/{p["id"]}.jpg', 'tags': ' '.join(p['tags']),
             'created_at': '2026-11-01T00:00:00+00:00'} for p in res[start:start + limit]]).encode())


def _write_kq(root: pathlib.Path, manifest: dict | None = None, robots: bool = True) -> pathlib.Path:
    src = root / 'kindsite'
    src.mkdir(parents=True)
    (src / 'manifest.json').write_text(json.dumps(manifest or KQ_MANIFEST), encoding='utf-8')
    (src / 'downloader.py').write_text(KQ_DOWNLOADER, encoding='utf-8')
    if robots:
        (src / 'robots.txt').write_text(KQ_ROBOTS, encoding='utf-8')
    return src


class _KqEnv(TestCase):
    """The 'kindsite' extension (module kq) installed once in a temporary data dir, its fake site behind fetch_html"""
    tmp: tempfile.TemporaryDirectory
    env: mock._patch
    cls_kq: type

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': cls.tmp.name, 'LYNXX_TAGS_DIR': os.path.join(cls.tmp.name, 'tags')})
        cls.env.start()
        Extensions.ensure_loaded()
        Extensions.install_from_dir(_write_kq(pathlib.Path(cls.tmp.name) / 'src'))
        cls.pid = ProcModule.PROC_MODULES_BY_NAME[ABBR]
        cls.cls_kq = DOWNLOADERS_BY_PROC_MODULE[cls.pid]

    @classmethod
    def tearDownClass(cls) -> None:
        with contextlib.suppress(Exception):
            Extensions.uninstall('kindsite')
        cls.env.stop()
        cls.tmp.cleanup()
        ProcModule.set(ProcModule.RX)

    def setUp(self) -> None:
        self.site = KqSite()
        self.patches = [mock.patch.object(self.cls_kq, 'fetch_html', lambda _, url, **kw: self.site.fetch(url)),
                        mock.patch.object(server, 'notify'), mock.patch('lynxx.network.time.sleep')]
        for p in self.patches:
            p.start()
        TagsDB.clear()
        reset_last_tags()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        TagsDB.clear()
        reset_last_tags()
        ProcModule.set(ProcModule.RX)

    @staticmethod
    def parsed(abbr: str, tags: str, *extra: str, preprocess=False):
        """A downloader of module abbr with the query parsed as a run parses it (no request unless preprocess)"""
        pid = ProcModule.PROC_MODULES_BY_NAME[abbr]
        ProcModule.set(pid)
        reset_last_tags()
        args = prepare_arglist(['-module', abbr, *extra, '--', *tags.split()])
        dwn = _make_downloader(pid)
        me = current_process()
        me.killed = False
        dwn.reset_root_thread(me)
        dwn.no_conflict_delay = True
        dwn._parse_args(args, preprocess)
        return dwn

    def refused(self, abbr: str, tags: str, *extra: str) -> str:
        with self.assertRaises(ThreadInterruptException) as ctx:
            self.parsed(abbr, tags, *extra)
        return str(ctx.exception)

    @staticmethod
    def preview(tags: str, **params) -> dict:
        return server.m_search_preview({'module': ABBR, 'tags': tags, 'args': params.pop('args', []), **params})


# ----- manifest 'search' -----

class ManifestSearchTests(TestCase):
    BASE = {'id': 'testsite', 'name': 'Test', 'abbr': 'zz', 'version': '1.0.0', 'entry': 'downloader.py:DownloaderZz'}

    def load(self, search, syntax: dict | None = None) -> ExtensionManifest:
        return ExtensionManifest.from_dict({**self.BASE, 'syntax': syntax or {}, 'search': search}, None, 'user')

    def test_kinds_are_read(self) -> None:
        m = self.load({'kinds': [
            {'id': 'artist', 'token': 'artist:', 'value': '[a-z0-9_-]+', 'listing': True, 'pageSize': 24, 'tagType': 'artist',
             'example': 'x' * 100},
            {'id': 'character', 'tagType': 'character'},
            {'id': 'uploader', 'token': 'user=', 'input': 'text'},
            {'id': 'keyword', 'token': 'search:', 'input': 'text', 'label': 'Tìm chữ'},
            {'id': 'collection', 'token': 'set:', 'input': 'id'},
            {'id': 'favorites', 'use': 'fav', 'token': 'fav:'},
            {'id': 'pool', 'use': 'pool', 'order': 'site'},
        ], 'metaNames': ['vote', 'upvoted_by', 'vote'], 'idSearch': False},
            {'idSep': '=', 'fav': 'favorited_by=[^:=]+?', 'pool': 'pool=\\d+?'})
        kinds = {k.id: k for k in m.search.kinds}
        self.assertEqual(['artist', 'character', 'uploader', 'keyword', 'collection', 'favorites', 'pool'], list(kinds))
        self.assertEqual(('artist:', '[a-z0-9_-]+', True, 24, 'artist', 'x' * 80),
                         (kinds['artist'].token, kinds['artist'].value, kinds['artist'].listing, kinds['artist'].pageSize,
                          kinds['artist'].tagType, kinds['artist'].example))
        self.assertEqual(('', 'tag'), (kinds['character'].token, kinds['character'].input))
        self.assertEqual('\\S+', kinds['uploader'].value, 'default value: any non space')
        self.assertEqual('\\d+', kinds['collection'].value, 'input id: digits')
        self.assertEqual('Tìm chữ', kinds['keyword'].label)
        # 'use' kinds take the token of the module's syntax (its idSep) and the rest of that regex as the value
        self.assertEqual(('favorited_by=', '[^:=]+?'), (kinds['favorites'].token, kinds['favorites'].value))
        self.assertEqual(('pool=', '\\d+?', 'site'), (kinds['pool'].token, kinds['pool'].value, kinds['pool'].order))
        self.assertTrue(any('replaced' in n for n in m.search.notes))
        self.assertEqual(['vote', 'upvoted_by'], m.search.metaNames)
        self.assertFalse(m.search.idSearch)
        pub = m.to_public_dict()['search']
        self.assertEqual({'kinds', 'metaNames', 'idSearch'}, set(pub))
        self.assertEqual('artist:', pub['kinds'][0]['token'])
        self.assertNotIn('notes', pub)

    def test_example_is_the_value(self) -> None:
        # the app shows '<token><example>': an example written with its token would read 'user:user:x'
        m = self.load({'kinds': [{'id': 'uploader', 'token': 'user:', 'input': 'text', 'example': 'user:some_user'},
                                 {'id': 'favorites', 'use': 'fav', 'example': 'Favorited_By:some_user'},
                                 {'id': 'pool', 'use': 'pool', 'example': '12'}]},
                      {'fav': 'favorited_by:[^:]+?', 'pool': 'pool:\\d+?'})
        self.assertEqual(['some_user', 'some_user', '12'], [k.example for k in m.search.kinds])
        self.assertEqual(2, sum('holds the token' in n for n in m.search.notes))

    def test_without_search(self) -> None:
        m = ExtensionManifest.from_dict(dict(self.BASE), None, 'user')
        self.assertEqual({'kinds': [], 'metaNames': [], 'idSearch': True}, m.to_public_dict()['search'])

    def test_forward_compatible_parts_are_skipped(self) -> None:
        m = self.load({'kinds': [{'id': 'popular', 'token': 'popular:', 'choices': ['day']},
                                 {'id': 'artist', 'token': 'artist:', 'paged': False},
                                 {'id': 'model', 'token': 'model:', 'input': 'choice'},
                                 {'id': 'category', 'token': 'category:', 'tagType': 'model'}],
                       'modifiers': ['age:']})
        self.assertEqual(['artist', 'category'], [k.id for k in m.search.kinds])
        self.assertEqual('', m.search.kinds[1].tagType, 'unknown tag type dropped, kind kept')
        notes = ' | '.join(m.search.notes)
        for part in ("unknown kind id 'popular'", "unknown field 'paged'", "unknown input 'choice'", "unknown tagType 'model'",
                     "unknown search field 'modifiers'"):
            self.assertIn(part, notes)

    def test_bad_declarations_are_refused(self) -> None:
        bad = [
            ([], 'must be an object'),
            ({'kinds': {}}, 'must be a list'),
            ({'kinds': [{'id': 'artist', 'token': 'Artist:'}]}, 'invalid token'),
            ({'kinds': [{'id': 'artist', 'token': 'artist'}]}, 'invalid token'),
            ({'kinds': [{'id': 'artist', 'token': 'a' * 25 + ':'}]}, 'invalid token'),
            ({'kinds': [{'id': 'artist', 'token': 'artist:', 'value': '('}]}, 'not a valid regex'),
            ({'kinds': [{'id': 'artist', 'token': 'artist:', 'value': '(a+)+'}]}, 'unsafe value regex'),
            ({'kinds': [{'id': 'artist', 'token': 'artist:', 'value': '(a)\\1'}]}, 'unsafe value regex'),
            ({'kinds': [{'id': 'artist', 'token': 'artist:', 'value': 'a' * 201}]}, 'longer than'),
            ({'kinds': [{'id': 'artist', 'token': 'artist:', 'listing': 'yes'}]}, 'listing must be a boolean'),
            ({'kinds': [{'id': 'artist', 'token': 'artist:', 'pageSize': -1}]}, 'pageSize'),
            ({'kinds': [{'id': 'artist', 'token': 1}]}, 'must be a string'),
            ({'kinds': [{'token': 'artist:'}]}, 'missing id'),
            ({'kinds': [{'id': 'artist', 'token': 'artist:'}, {'id': 'artist', 'token': 'a:'}]}, 'more than one kind with id'),
            ({'kinds': [{'id': 'artist', 'token': 'a:'}, {'id': 'model', 'token': 'a:'}]}, 'more than one kind with token'),
            ({'kinds': [{'id': 'artist'}]}, 'without token'),
            ({'kinds': [{'id': 'artist', 'tagType': 'artist', 'listing': True}]}, 'without token'),
            ({'kinds': [{'id': 'pool', 'use': 'pool', 'listing': True}]}, 'cannot be a listing'),
            ({'kinds': [{'id': 'artist', 'token': 'a:'}] * 17}, 'at most 16'),
            ({'metaNames': ['Vote']}, 'metaNames'),
            ({'metaNames': 'vote'}, 'metaNames'),
            ({'idSearch': 'no'}, 'idSearch'),
        ]
        for search, msg in bad:
            with self.subTest(search=search), self.assertRaisesRegex(ManifestError, msg):
                self.load(search)

    def test_a_use_kind_without_that_syntax_is_skipped(self) -> None:
        m = self.load({'kinds': [{'id': 'pool', 'use': 'pool'}]}, {'pool': ''})
        self.assertEqual([], m.search.kinds)
        self.assertIn('has no pool syntax', ' '.join(m.search.notes))

    def test_gate_2_0_11(self) -> None:
        self.assertEqual({'search': '2.0.11'}, MANIFEST_KEYS_SINCE)
        # what a core before 2.0.11 says: the known fields of the manifest without 'search' (the new core stands in for it)
        with mock.patch.dict(ExtensionManifest.__dataclass_fields__):
            del ExtensionManifest.__dataclass_fields__['search']
            with self.assertRaisesRegex(ManifestError, r'unknown field\(s\): search'):
                self.load({'kinds': []})

    def test_gate_with_the_2_0_10_code(self) -> None:
        # the real 2.0.10 parser (develop at the 2.0.10 release), when git has it: a shallow clone does not
        src = subprocess.run(('git', 'show', 'fe0c695:lynxx/modspec.py'), cwd=ROOT, capture_output=True, timeout=60)
        if src.returncode != 0:
            self.skipTest('the 2.0.10 commit is not in this checkout')
        old = types.ModuleType('lynxx._modspec_2_0_10')
        old.__package__ = 'lynxx'
        sys.modules[old.__name__] = old  # dataclasses look the module up
        try:
            exec(compile(src.stdout.decode('utf-8'), 'modspec_2_0_10.py', 'exec'), old.__dict__)  # noqa: S102
            with self.assertRaisesRegex(old.ManifestError, r'unknown field\(s\): search'):
                old.ExtensionManifest.from_dict({**self.BASE, 'search': {'kinds': []}}, None, 'user')
        finally:
            sys.modules.pop(old.__name__, None)

    def test_catalog_build_warns(self) -> None:
        spec = importlib.util.spec_from_file_location('build_catalog_kinds', ROOT / 'tools' / 'build_catalog.py')
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        self.assertEqual(['search (Lynxx 2.0.11)'], tool.newer_core_keys({**self.BASE, 'search': {}}))
        self.assertEqual(['search (Lynxx 2.0.11)', 'tagUpdate.merge (Lynxx 2.0.10)'],
                         tool.newer_core_keys({**self.BASE, 'search': {}, 'tagUpdate': {'entry': 'tags.py:f', 'merge': {'minPosts': 5}}}))
        self.assertEqual([], tool.newer_core_keys(dict(self.BASE)))
        # SDK 1.2 without 'search' (a new draft of the generator, SDK 1.2 names only): 2.0.7 to 2.0.10 have SDK 1.1 and refuse it
        self.assertEqual({'1.2': '2.0.11', '1.3': '2.0.14'}, modspec.SDK_VERSIONS_SINCE)
        self.assertEqual(['minSdkVersion 1.2 (Lynxx 2.0.11)'], tool.newer_core_keys({**self.BASE, 'minSdkVersion': '1.2'}))
        self.assertEqual(['minSdkVersion 1.2 (Lynxx 2.0.11)', 'search (Lynxx 2.0.11)'],
                         tool.newer_core_keys({**self.BASE, 'minSdkVersion': '1.2', 'search': {}}))
        for old in ('1.0', '1.1', 1.2, None):
            self.assertEqual([], tool.newer_core_keys({**self.BASE, 'minSdkVersion': old}), old)
        with tempfile.TemporaryDirectory() as tmp:  # what a catalog build says on stderr
            ext = pathlib.Path(tmp) / 'src' / 'testsite'
            ext.mkdir(parents=True)
            (ext / 'manifest.json').write_text(json.dumps({**self.BASE, 'minSdkVersion': '1.2'}), encoding='utf-8')
            (ext / 'downloader.py').write_text('', encoding='utf-8')
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                tool.build(pathlib.Path(tmp) / 'src', pathlib.Path(tmp) / 'out', 'https://catalog.example/', tags_dir=None)
            self.assertIn('testsite 1.0.0: minSdkVersion 1.2 (Lynxx 2.0.11): older cores refuse to install it', err.getvalue())

    def test_builtin_modules_declare_their_kinds(self) -> None:
        Extensions.ensure_loaded()
        db, rx = Extensions.get_by_abbr('db'), Extensions.get_by_abbr('rx')
        self.assertEqual(('1.1.0', '2.1.0'), (db.version, rx.version))
        self.assertEqual({'uploader': 'user:', 'favorites': 'favorited_by:', 'pool': 'pool:'}, {k.id: k.token for k in db.search.kinds})
        self.assertEqual({'uploader': 'user:', 'favorites': 'favorited_by:', 'pool': 'pool:'}, {k.id: k.token for k in rx.search.kinds})
        self.assertEqual(['favgroup', 'ordfavgroup'], db.search.metaNames)
        self.assertEqual('\\d+?', next(k for k in rx.search.kinds if k.id == 'favorites').value)
        self.assertEqual([], db.search.notes + rx.search.notes)
        rows = {r['abbr']: r for r in Extensions.list_all()}
        self.assertEqual(['uploader', 'favorites', 'pool'], [k['id'] for k in rows['db']['search']['kinds']])
        self.assertTrue(rows['rx']['search']['idSearch'])

    def test_json_schema_knows_search(self) -> None:
        schema = json.loads((ROOT / 'docs' / 'schemas' / 'extension-manifest.schema.json').read_text(encoding='utf-8'))
        search = schema['properties']['search']
        self.assertEqual({'kinds', 'metaNames', 'idSearch'}, set(search['properties']))
        self.assertEqual(set(modspec.SEARCH_KIND_KEYS), set(search['properties']['kinds']['items']['properties']))
        self.assertEqual(list(modspec.SEARCH_KIND_IDS), search['properties']['kinds']['items']['properties']['id']['enum'])

    def test_sdk_1_2(self) -> None:
        self.assertEqual('1.3', SDK_VERSION, 'SDK 1.3 (2.0.14, -max_vres) keeps every 1.2 name')
        self.assertTrue(sdk_satisfies('1.1') and sdk_satisfies('1.2'))
        self.assertFalse(sdk_satisfies('1.4'))


# ----- parser, task, downloader -----

class KindParsingTests(_KqEnv):
    def parse(self, tags: str) -> tuple[bool, tuple]:
        ProcModule.set(self.pid)
        reset_last_tags()
        ok, parsed = parse_tags(tags)
        return ok, tuple(parsed)

    def test_kinds_registered(self) -> None:
        ms = module_search(self.pid)
        self.assertEqual({'artist:', 'category:', 'user:', 'pool:', 'favorited_by:'}, set(ms.by_token))
        self.assertEqual(frozenset({'vote'}), ms.meta_names)
        self.assertFalse(id_search_supported(ABBR))
        self.assertTrue(id_search_supported('db'))
        self.assertTrue(id_search_supported('nosuchmodule'))
        self.assertEqual('artist', kind_of('-artist:x', ABBR).id)
        self.assertIsNone(kind_of('artistx:y', ABBR))
        self.assertIsNone(kind_of('artist:x', 'db'))

    def test_an_unknown_module_has_no_kinds(self) -> None:
        # an abbr that is not registered (not installed: a catalog module of files.identify, one that failed to load) answers for
        # itself, never with the current module's kinds, metatag names or id search
        ProcModule.set(self.pid)
        self.assertFalse(id_search_supported())
        self.assertTrue(id_search_supported('zz'))
        self.assertIsNone(kind_of('artist:foo', 'zz'))
        self.assertFalse(is_module_meta('vote:3:bob', 'zz'))
        self.assertFalse(server._is_metatag('vote:3:bob', 'zz'))
        self.assertTrue(server._is_metatag('vote:3:bob', ABBR))
        self.assertEqual((), kind_tokens('zz'))
        self.assertEqual({'artist:', 'category:', 'user:', 'pool:', 'favorited_by:'}, set(kind_tokens(ABBR)))
        # files.identify: a link of a catalog module that is not installed ('zz' here) while kq is the current module
        with mock.patch.object(server, 'build_file_queries', return_value=[{'module': 'zz', 'tags': 'id:5', 'count': 1, 'dropped': 0}]):
            self.assertEqual([True], [q['idSearch'] for q in server.m_files_identify({'module': ABBR, 'names': ['x']})['queries']])

    def test_meta_names_with_digits(self) -> None:
        # modspec accepts 'sha512_hash' (^[a-z][a-z0-9_]{0,31}$): the core must know it as a metatag, never a tag to check locally
        spec = modspec.SearchSpec.from_dict({**KQ_MANIFEST['search'], 'metaNames': ['vote', 'sha512_hash']},
                                            ExtensionManifest.from_dict(KQ_MANIFEST, None, 'user').syntax)
        with mock.patch.dict(_REGISTRY, {self.pid: ModuleSearch(spec)}):
            ProcModule.set(self.pid)
            for tag in ('sha512_hash:abc', '-sha512_hash:abc', 'md5:abc'):  # declared / a core name with a digit
                self.assertTrue(_is_meta(tag), tag)
            self.assertTrue(is_module_meta('sha512_hash:abc', ABBR))
            self.assertFalse(_is_meta('sha512:abc'), 'a name nobody declared stays a tag')
            required: list[str] = []
            tags, _ = extract_neg_and_groups('aaa bbb sha512_hash:abc', False, required)
            self.assertIn('sha512_hash:abc', tags)
            self.assertNotIn('sha512_hash:abc', required)

    def test_values_by_the_kind_regex(self) -> None:
        self.assertEqual((True, ('solo', 'user:Some_Name.Name')), self.parse('solo user:Some_Name.Name'))  # capitals and dots: its regex
        self.assertEqual((True, ('-user:Bob', 'solo')), self.parse('-user:Bob solo'))
        self.assertEqual((True, ('artist:foo', 'solo', '-bar')), self.parse('artist:foo solo -bar'))
        ok, _ = self.parse('artist:Foo solo')
        self.assertFalse(ok)
        self.assertIn("not a valid value for 'artist:'", parse_error())
        self.assertEqual((True, ('artist:Foo.Bar', 'solo')), self.parse('%artist:Foo.Bar% solo'), 'escaped: not validated')

    def test_the_parse_cache_is_per_module(self) -> None:
        # 'user:Some_Name' is valid where a kind's regex allows capitals, not with the core tables of xb: same string, other module
        self.assertTrue(self.parse('user:Some_Name')[0])
        ProcModule.set(ProcModule.XB)
        self.assertFalse(parse_tags('user:Some_Name')[0])
        ProcModule.set(self.pid)
        self.assertTrue(parse_tags('user:Some_Name')[0])

    def test_listing_rules(self) -> None:
        for tags, why in (('artist:a artist:b', 'one site listing per search'), ('%artist:a% artist:b', 'one site listing'),
                          ('-artist:a solo', 'cannot be negated'), ('artist:a (x~y)', "'or' groups cannot be used"),
                          ('artist:a pool:12', 'favorites / pools cannot be used'), ('artist:a favorited_by:bob', 'favorites'),
                          # listings inside an 'or' group: each member is a tag
                          ('(artist:a~artist:b)', "artist:a is a site listing: 'or' groups cannot be used"),
                          ('(artist:a~bar) solo', "artist:a is a site listing: 'or' groups"),
                          ('(bar~artist:a)', "artist:a is a site listing: 'or' groups"),
                          ('(-artist:a~bar)', "artist:a is a site listing: 'or' groups"),
                          ('(%artist:a%~bar)', "artist:a is a site listing: 'or' groups"),
                          ('(pool:12~pool:13)', "pool:12 lists posts in the site's order: 'or' groups cannot be used"),
                          ('(user:a:b~bar)', "'user:a:b': not a valid value for 'user:'"),
                          # '-' outside an escape: no escape (a literal tag), the listing rules would be skipped
                          ('-%artist:ab.c% solo', "'-%artist:ab.c%': write the '-' inside the escape: '%-artist:ab.c%'"),
                          ('-%user:a:b% solo', "write the '-' inside the escape")):
            with self.subTest(tags=tags):
                ok, _ = self.parse(tags)
                self.assertFalse(ok)
                self.assertIn(why, parse_error())
        self.assertTrue(self.parse('user:a (x~y)')[0], 'a kind that is not a listing: or groups as usual')
        self.assertEqual((True, ('( user:a ~ user:b )',)), self.parse('(user:a~user:b)'), 'and inside one')
        self.assertEqual((True, ('-user:a:b', 'solo')), self.parse('%-user:a:b% solo'), "'-' inside the escape")
        res = server.m_tags_validate({'module': ABBR, 'tags': '(artist:a~artist:b) -mindate'})
        self.assertEqual((False, "artist:a is a site listing: 'or' groups cannot be used with it"), (res['valid'], res['error']))
        res = server.m_tags_validate({'module': ABBR, 'tags': 'artist:a artist:b'})
        self.assertEqual((False, 'one site listing per search: artist:a artist:b'), (res['valid'], res['error']))
        self.assertEqual('', server.m_tags_validate({'module': ABBR, 'tags': 'artist:a'})['error'])
        with self.assertRaisesRegex(TagsError, "'or' groups"):
            convert_taglist(['artist:a', '(x~y)'])
        with self.assertRaises(AssertionError):  # still what callers of the original assert catch
            convert_taglist(['artist:a', 'artist:b'])

    def test_split_kind(self) -> None:
        ProcModule.set(self.pid)
        self.assertEqual((KindMatch('artist', 'artist:', 'foo+bar'), 'order:rating'), split_kind('artist:foo%2bbar+order:rating'))
        self.assertEqual((KindMatch('artist', 'artist:', 'foo'), 'user:x order:rating'), split_kind('user:x artist:foo order:rating'))
        self.assertEqual((KindMatch('uploader', 'user:', 'Some_Name'), 'solo'), split_kind('user:Some_Name+solo'))
        self.assertEqual((None, '(+user:a+~+user:b+)+solo'), split_kind('(+user:a+~+user:b+)+solo'), 'not inside an or group')
        self.assertEqual((None, 'solo+-user:a'), split_kind('solo+-user:a'))
        self.assertEqual((None, ''), split_kind(''))
        self.assertEqual(KindMatch('category', 'category:', 'x'), split_kind('category:x', ABBR)[0])
        self.assertIsNone(find_kind(['solo'], ABBR))
        self.assertEqual(KindMatch('artist', 'artist:', 'a b'), find_kind(['user:x', 'artist:a%20b'], ABBR))

    def test_current_kind(self) -> None:
        self.assertEqual(KindMatch('uploader', 'user:', 'Some_Name'), self.parsed(ABBR, 'user:Some_Name solo').current_kind)
        self.assertEqual(KindMatch('artist', 'artist:', 'foo'), self.parsed(ABBR, 'solo user:y artist:foo').current_kind)
        self.assertIsNone(self.parsed(ABBR, 'solo').current_kind)
        self.assertIsNone(self.parsed('db', 'solo').current_kind)

    def test_listing_moves_plain_tags_local(self) -> None:
        dwn = self.parsed(ABBR, 'artist:foo solo -bar -ba?z* order:rating user:x')
        self.assertEqual(['artist:foo+order:rating+user:x'], dwn.tags_str_arr, 'token not counted: maxTags 2 holds two metas')
        self.assertEqual(['solo'], dwn.required_tags)
        self.assertEqual(1, len(dwn.neg_and_groups))
        group = dwn.neg_and_groups[0]
        for tag, hit in (('bar', True), ('bazz', True), ('solo', False)):
            self.assertEqual(hit, all(any(p.fullmatch(t) for t in ('x', tag)) for p in group), tag)
        self.assertFalse(dwn.default_sort, 'a listing is in the site order')
        self.assertEqual('artist:foo', dwn.site_order_kind)
        self.assertEqual(24, dwn._get_items_per_page())
        self.assertEqual(24 * 100, dwn._get_max_search_depth())
        self.assertEqual(50, self.parsed(ABBR, 'solo').__class__.ITEMS_PER_PAGE)
        self.assertEqual(50, self.parsed(ABBR, 'solo')._get_items_per_page())
        self.assertIn('max tags exceeded', self.refused(ABBR, 'artist:foo user:x vote:3:y order:rating'))

    def test_listing_refuses_dates(self) -> None:
        msg = self.refused(ABBR, 'artist:foo solo', '-mindate', '01-01-2020')
        self.assertIn("'artist:foo' lists posts in the site's order: date limits cannot be used with it", msg)

    def test_listing_in_an_or_group_never_reaches_the_site(self) -> None:
        # before: tasks 'artist:foo' / 'artist:baz' with no kind, newest first, 50 per page, dates accepted
        for tags in ('(artist:foo~artist:baz)', '(artist:foo~bar) solo'):
            for extra in ((), ('-mindate', '01-01-2026')):
                with self.subTest(tags=tags, extra=extra), self.assertRaisesRegex(TagsError, "'or' groups cannot be used"):
                    self.parsed(ABBR, tags, *extra)
        self.assertEqual([], self.site.urls)

    def test_module_sort_syntax_is_a_custom_sort(self) -> None:
        # C1: syntax.sort 'order:...' (yande.re, Sakugabooru, generated moebooru extensions): not newest first
        dwn = self.parsed(ABBR, 'solo order:score')
        self.assertFalse(dwn.default_sort)
        self.assertTrue(dwn._supports_id_cursor(), 'the module has a cursor, the sort turns it off')
        for tags in ('solo order:id_desc', 'solo'):
            self.assertTrue(self.parsed(ABBR, tags).default_sort, tags)
        # 'order:id' is oldest first on the 'order:' engines (Danbooru, e621, Moebooru): with it the date search picked page 0
        # (the oldest posts) and downloaded nothing
        self.assertFalse(self.parsed(ABBR, 'solo order:id').default_sort)
        for sort in ('order:score', 'order:id'):
            self.assertIn('cannot use both sort tag and date filter', self.refused(ABBR, f'solo {sort}', '-mindate', '01-01-2020'))
        self.assertIn('non-default sorting with multi-task', self.refused(ABBR, 'order:score (solo~bar)'))
        # downloaders with their own check keep it: danbooru knows only 'order:'
        self.assertFalse(DownloaderDb._is_custom_sort_tag(self.parsed('db', 'solo'), 'sort:score'))
        self.assertTrue(self.parsed('db', 'solo order:id_desc').default_sort)
        self.assertFalse(self.parsed('db', 'solo order:id').default_sort, 'oldest first')
        self.assertFalse(self.parsed('db', 'solo order:score').default_sort)
        self.assertFalse(self.parsed('rx', 'solo sort:score').default_sort)
        self.assertTrue(self.parsed('rx', 'solo sort:id').default_sort)

    def test_yandere_order_tags(self) -> None:
        Extensions.ensure_loaded(include_catalog=True)
        if 'yr' not in ProcModule.PROC_MODULES_BY_NAME:
            self.skipTest('no yandere extension in the catalog')
        self.assertFalse(self.parsed('yr', 'touhou order:score').default_sort)
        self.assertTrue(self.parsed('yr', 'touhou order:id_desc').default_sort)
        self.assertFalse(self.parsed('yr', 'touhou order:id').default_sort, 'oldest first')
        self.assertIn('sort tag and date filter', self.refused('yr', 'touhou order:score', '-mindate', '01-01-2020'))

    def test_pool_kind_in_site_order(self) -> None:
        dwn = self.parsed(ABBR, 'pool:12 solo')
        self.assertEqual(('pool:12', False), (dwn.site_order_kind, dwn.default_sort), "pool kind with order 'site'")
        self.assertEqual(KindMatch('pool', 'pool:', '12'), dwn.current_kind)
        self.assertTrue(self.parsed('db', 'pool:12 solo').default_sort, 'db pool kind: order id (today)')

    def test_kind_tokens_and_meta_names_stay_on_the_site(self) -> None:
        # C2: maxTags 2 + localPositiveOverflow; 'category:' (a kind) and 'vote:' (a metatag name) are never the rarest tag nor
        # moved local, whatever the order, with a real list and without one
        ProcModule.set(self.pid)
        TagsDB.load_from_dict(ABBR, {'tag_a': (G, 1000), 'tag_b': (G, 10)})
        TagsDB.DBFiles[ABBR] = pathlib.Path(self.tmp.name) / 'kq_tags.json'
        for meta in ('category:foo', 'vote:3:bob', 'user:Bob'):
            for empty in (False, True):
                with mock.patch.object(TagsDB, 'is_empty', return_value=empty):
                    for query in (f'tag_a tag_b {meta}', f'{meta} tag_a tag_b'):
                        with self.subTest(query=query, empty=empty):
                            required: list[str] = []
                            tags, _ = extract_neg_and_groups(query, False, required)
                            self.assertIn(meta, tags)
                            self.assertNotIn(meta, required)
                            self.assertEqual(1, len(required))
                            if not empty:
                                self.assertEqual((['tag_a'], 'tag_b'), (required, next(t for t in tags if t.startswith('tag'))))
        required = []
        tags, neg = extract_neg_and_groups('tag_a -vote:3:bob', False, required)
        self.assertEqual((['tag_a', '-vote:3:bob'], [], []), (tags, required, neg), 'a negative metatag is never a local filter')


class ListingPipelineTests(_KqEnv):
    def test_preview_of_a_listing(self) -> None:
        res = self.preview('artist:foo solo', pageSize=100)
        u = url_parse.urlparse(self.site.urls[-1])
        self.assertEqual('/models/foo/', u.path)
        self.assertEqual({'from': ['1'], 'limit': ['24']}, {k: v for k, v in url_parse.parse_qs(u.query).items() if k != 'rest'})
        self.assertEqual((24, ['artist:foo'], ['solo']), (res['pageSize'], res['tasks'], res['requiredTags']))
        self.assertEqual(24, len(res['items']))
        for it in res['items']:
            self.assertEqual('solo' not in it['tags'], it['excluded'])
        self.assertFalse(res['localFiltersUnchecked'])
        res = self.preview('artist:foo -bar', page=1, pageSize=24)
        self.assertIn('from=2', self.site.urls[-1])
        self.assertTrue(any(it['excludedBy'] == ['neg'] for it in res['items']))

    def test_preview_flag_when_the_listing_has_no_tags(self) -> None:
        # C6: tags only by fetching each post: local filters were not checked, the app says so
        with mock.patch.object(self.cls_kq, '_can_extract_item_info_without_fetch', return_value=False):
            self.assertTrue(self.preview('artist:foo solo')['localFiltersUnchecked'])
            self.assertFalse(self.preview('artist:foo')['localFiltersUnchecked'])

    def test_count_pages_by_the_kind_page_size(self) -> None:
        res = server.m_search_count({'module': ABBR, 'tags': 'artist:foo', 'args': []})
        self.assertEqual(60, res['count'])
        self.assertTrue(all('limit=24' in u for u in self.site.urls), 'binary search with 24 per page')
        res = server.m_search_count({'module': ABBR, 'tags': 'artist:foo solo', 'args': []})
        self.assertEqual((60, True, ['solo']), (res['count'], res['approximate'], res['requiredTags']))

    def test_download_of_a_listing(self) -> None:
        with tempfile.TemporaryDirectory() as dest:
            dwn = self._run('artist:foo solo', dest)
        self.assertEqual('', dwn.launch_error)
        self.assertFalse(any('order:id' in u or 'id%3A' in u or 'id:<' in u for u in self.site.urls), 'no id cursor')
        pages = {url_parse.parse_qs(url_parse.urlparse(u).query)['from'][0] for u in self.site.urls if '/models/' in u}
        self.assertLessEqual({'1', '2', '3'}, pages, 'pages of 24 (the count looks for the last page first)')
        self.assertEqual(30, dwn.processed_count, 'the 30 solo posts of 60')

    @mock.patch('lynxx.download_json.DownloaderJsonApi._prefer_id_cursor', return_value=True)
    def test_download_with_a_sort_has_no_cursor(self, _) -> None:
        with tempfile.TemporaryDirectory() as dest:
            dwn = self._run('solo order:score', dest)
        self.assertEqual('', dwn.launch_error)
        self.assertFalse(any('order:id' in u or 'id:<' in u or 'id:>' in u for u in self.site.urls))
        with tempfile.TemporaryDirectory() as dest:
            self.site.urls.clear()
            dwn = self._run('solo', dest)
        self.assertEqual('', dwn.launch_error)
        self.assertTrue(any('order:id+id:>' in u for u in self.site.urls), 'default order: the id cursor (oldest first)')
        self.assertEqual(60, dwn.processed_count)

    def _run(self, tags: str, dest: str):
        ProcModule.set(self.pid)
        reset_last_tags()
        args = prepare_arglist(['-module', ABBR, '-dmode', 'skip', '-path', dest, '--', *tags.split()])
        dwn = _make_downloader(self.pid)
        me = current_process()
        me.killed = False
        dwn.reset_root_thread(me)
        dwn.no_conflict_delay = True
        dwn.launch_download(args)
        return dwn

    def test_robots_of_the_extension(self) -> None:
        # 'Disallow: *rape' (a KVS rule) matches /models/grape/: refused before any request
        with self.assertRaises(server.RpcError):
            self.preview('artist:grape')
        self.assertEqual([], self.site.urls)
        dwn = self.parsed(ABBR, 'artist:grape')
        with self.assertRaisesRegex(ThreadInterruptException, 'robots.txt does not allow https://kinds.example/models/grape/'):
            dwn._form_tags_search_address('artist:grape')
        dwn = self.parsed(ABBR, 'solo')
        self.assertTrue(dwn.robots_allows(f'{SITE}models/foo/'))
        self.assertFalse(dwn.robots_allows(f'{SITE}tags/a_b/'))
        robots = Extensions.get_by_abbr(ABBR).path / 'robots.txt'
        saved = robots.read_bytes()
        robots.unlink()
        try:
            self.assertFalse(dwn.robots_allows(f'{SITE}models/foo/'), 'no saved robots.txt: nothing is allowed')
            with self.assertRaisesRegex(ThreadInterruptException, 'has no saved robots.txt'):
                dwn.require_robots_allowed(f'{SITE}models/foo/')
            with self.assertRaises(server.RpcError):
                self.preview('artist:foo')
            self.assertEqual([], self.site.urls)
        finally:
            robots.write_bytes(saved)

    def test_related_tags_leave_out_the_kind_value(self) -> None:
        res = server.m_search_related({'module': ABBR, 'tags': 'artist:foo'})
        tags = [r['tag'] for r in res['items']]
        self.assertNotIn('foo', tags, 'the posts of /models/foo/ carry foo as a plain tag')
        self.assertIn('solo', tags)

    def test_listing_or_group_is_refused_by_preview(self) -> None:
        for tags in ('artist:foo (solo~bar)', '(artist:foo~artist:baz)', '(artist:foo~bar) solo'):
            with self.subTest(tags), self.assertRaisesRegex(AssertionError, "'or' groups cannot be used"):
                self.preview(tags)
        self.assertEqual([], self.site.urls)


# ----- sidecar: type kinds, autocomplete, resolve, files -----

class SidecarKindTests(_KqEnv):
    def setUp(self) -> None:
        super().setUp()
        TagsDB.load_from_dict(ABBR, {'foo_artist': (A, 100), 'foo_char': (C, 500), 'foo_general': (G, 1000),
                                     'category:foo_game': (CP, 50), 'fop_artist': (A, 5)})

    def ac(self, prefix: str, **kw) -> list[tuple[str, str]]:
        return [(r['tag'], r['insert']) for r in server.m_tags_autocomplete({'module': ABBR, 'prefix': prefix, **kw})]

    def test_autocomplete_by_type(self) -> None:
        self.assertEqual(['foo_general', 'foo_char', 'foo_artist'], [t for t, _ in self.ac('foo')])
        self.assertEqual([('foo_artist', 'foo_artist')], self.ac('foo', types=['artist']))
        self.assertEqual([('foo_artist', 'foo_artist'), ('fop_artist', 'fop_artist')], self.ac('fo', types=['artist']))
        self.assertEqual([('foo_char', 'foo_char'), ('foo_artist', 'foo_artist')], self.ac('foo', types=['artist', 'character']))
        self.assertEqual([], self.ac('foo', types=['nosuchtype']))
        self.assertEqual([('foo_artist', '-foo_artist')], self.ac('-foo', types=['artist']))
        self.assertEqual(0, len(TagsDB._load(ABBR)._views), 'no cached view: browse results are not evicted')

    def test_autocomplete_with_a_token(self) -> None:
        # rows named '<token><prefix>' first (an extension's job may give them), else plain rows with the token inserted
        self.assertEqual([('category:foo_game', 'category:foo_game')], self.ac('foo', token='category:', types=['copyright']))
        self.assertEqual([('category:foo_game', 'category:foo_game')], self.ac('category:foo', token='category:'))
        self.assertEqual([('foo_artist', 'artist:foo_artist')], self.ac('foo', token='artist:', types=['artist']))
        self.assertEqual([('foo_artist', '-artist:foo_artist')], self.ac('-foo', token='artist:', types=['artist']))
        self.assertEqual([], self.ac('artist:', token='artist:'))
        with self.assertRaises(server.RpcError):
            self.ac('foo', token='Artist')

    def test_a_token_with_rows_of_its_own_never_prefixes_a_plain_row(self) -> None:
        # a video site's list: 'artist:<name>' rows of its artist listing, and plain tags typed artist by name (2.0.12 drafts).
        # 'artist:gcraw' would ask the site for a listing page that does not exist: only the token's own rows are offered
        TagsDB.load_from_dict(ABBR, {'artist:sessho': (A, 1), 'gcraw': (A, 40), 'category:disney': (CP, 9), 'disney_princess': (CP, 30)})
        self.assertEqual([('artist:sessho', 'artist:sessho')], self.ac('se', token='artist:', types=['artist']))
        self.assertEqual([], self.ac('gc', token='artist:', types=['artist']))
        self.assertEqual([], self.ac('-gc', token='artist:', types=['artist']))
        self.assertEqual([('category:disney', 'category:disney')], self.ac('dis', token='category:', types=['copyright']))
        self.assertEqual([], self.ac('disney_p', token='category:', types=['copyright']))
        # a token the list has no row of keeps the plain fallback (a kind whose value is a plain tag)
        self.assertEqual([('gcraw', 'user:gcraw')], self.ac('gc', token='user:', types=['artist']))
        self.assertEqual(['gcraw'], [t for t, _ in self.ac('gc')], 'without a token the plain tag is offered as before')

    def test_fuzzy_by_type(self) -> None:
        rows = server.m_tags_fuzzy({'module': ABBR, 'text': 'foo_artst', 'types': ['artist']})
        self.assertEqual(['foo_artist', 'fop_artist'], [r['tag'] for r in rows])
        rows = server.m_tags_fuzzy({'module': ABBR, 'text': 'foo_artst', 'types': ['character']})
        self.assertEqual([], rows)
        rows = server.m_tags_fuzzy({'module': ABBR, 'text': 'foo_chr', 'types': ['artist']})
        self.assertEqual([], rows)
        self.assertEqual(['foo_char'], [r['tag'] for r in server.m_tags_fuzzy({'module': ABBR, 'text': 'foo_chr'})])

    def test_tags_kinds(self) -> None:
        # 'category:foo_game' (copyright) is a row of the declared kind 'category:': no "Series" for it (its value is no plain tag)
        res = server.m_tags_kinds({'module': ABBR})
        self.assertEqual({'module': ABBR, 'hasList': True, 'types': {'artist': 2, 'character': 1}}, res)
        TagsDB.load_from_dict(ABBR, {'category:a': (CP, 50), 'category:b': (CP, 5), 'some_saga': (CP, 9), 'foo_artist': (A, 1)})
        self.assertEqual({'copyright': 1, 'artist': 1}, server.m_tags_kinds({'module': ABBR})['types'])
        TagsDB.load_from_dict(ABBR, {'category:a': (CP, 50)})
        self.assertEqual({'module': ABBR, 'hasList': True, 'types': {}}, server.m_tags_kinds({'module': ABBR}), 'a list all the same')
        TagsDB.load_from_dict(ABBR, {'foo_artist': (A, 100), 'foo_char': (C, 500), 'foo_general': (G, 1000),
                                     'category:foo_game': (CP, 50), 'fop_artist': (A, 5)})
        self.assertIs(res['types'], res['types'])
        self.assertEqual({'module': 'zq', 'hasList': False, 'types': {}}, server.m_tags_kinds({'module': 'zq'}))
        TagsDB.load_from_dict('zr', {'a': (G, 1)})
        self.assertEqual({'module': 'zr', 'hasList': True, 'types': {}}, server.m_tags_kinds({'module': 'zr'}))
        with self.assertRaises(server.RpcError):
            server.m_tags_kinds({'module': '../x'})
        self.assertIn('tags.kinds', server.METHODS)

    def test_tags_types(self) -> None:
        # every type the list has, '<token><value>' rows included (the A-Z browser lists them)
        res = server.m_tags_types({'module': ABBR})
        self.assertEqual({'module': ABBR, 'hasList': True, 'types': {'general': 1, 'character': 1, 'artist': 2, 'copyright': 1}}, res)
        TagsDB.load_from_dict('zs', {'a': (TagTypes.TAG_TYPE_SPECIES, 1), 'b': (TagTypes.TAG_TYPE_LORE, 2), 'c': (TagTypes.TAG_TYPE_META, 3),
                                     'd': (TagTypes.TAG_TYPE_UNKNOWN, 4), 'e': (TagTypes.TAG_TYPE_UNKNOWN, 5), 'f': (G, 6)})
        self.assertEqual({'species': 1, 'lore': 1, 'metadata': 1, 'unknown': 2, 'general': 1}, server.m_tags_types({'module': 'zs'})['types'])
        self.assertEqual(['d', 'e'], [r['tag'] for r in server.m_tags_browse({'module': 'zs', 'prefix': '', 'types': ['unknown']})['items']])
        # no list
        self.assertEqual({'module': 'zq', 'hasList': False, 'types': {}}, server.m_tags_types({'module': 'zq'}))
        for bad in ('../x', '', 'Rx', None):
            with self.assertRaises(server.RpcError):
                server.m_tags_types({'module': bad})
        self.assertIn('tags.types', server.METHODS)

    def test_tags_types_of_a_list_file_without_types(self) -> None:
        # a list read from a site that gives no tag types ("N posts"): every row is 'unknown', browsable by that filter only;
        # 'tag' rows (rs lists) are general
        tags_dir = pathlib.Path(self.tmp.name) / 'tags'
        tags_dir.mkdir(exist_ok=True)
        (tags_dir / 'zt_tags.json').write_text('{\n"alpha": "5 posts",\n"beta_artist": "7 posts",\n"gamma": "1 posts"\n}\n', encoding='utf-8')
        (tags_dir / 'zu_tags.json').write_text('{\n"alpha": "5 posts (tag)",\n"beta": "2 posts (artist)"\n}\n', encoding='utf-8')
        try:
            TagsDB.locate_default_lists(reload=('zt', 'zu'))
            self.assertEqual({'module': 'zt', 'hasList': True, 'types': {'unknown': 3}}, server.m_tags_types({'module': 'zt'}))
            self.assertEqual({'module': 'zt', 'hasList': True, 'types': {}}, server.m_tags_kinds({'module': 'zt'}))
            self.assertEqual(0, server.m_tags_browse({'module': 'zt', 'prefix': 'b', 'types': ['artist']})['total'])
            rows = server.m_tags_browse({'module': 'zt', 'prefix': 'b', 'types': ['unknown']})['items']
            self.assertEqual([{'tag': 'beta_artist', 'posts': 7, 'type': 'unknown'}], rows)
            self.assertEqual({'general': 1, 'artist': 1}, server.m_tags_types({'module': 'zu'})['types'])
        finally:
            for name in ('zt_tags.json', 'zu_tags.json'):
                (tags_dir / name).unlink(missing_ok=True)

    def test_resolve_per_module_metas(self) -> None:
        res = server.m_tags_resolve({'module': ABBR, 'tags': ['vote:3:bob', 'category:foo_game', 'artist:foo', 'foo_char']})
        self.assertEqual(['meta', 'meta', 'meta', 'character'], [r['type'] for r in res['items']])
        res = server.m_tags_resolve({'module': 'db', 'tags': ['vote:3:bob', 'favgroup:1']})
        self.assertEqual(['unknown', 'meta'], [r['type'] for r in res['items']])

    def test_ext_list_and_files_identify(self) -> None:
        row = next(r for r in Extensions.list_all() if r['abbr'] == ABBR)
        self.assertEqual(['artist', 'category', 'uploader', 'pool', 'favorites'], [k['id'] for k in row['search']['kinds']])
        self.assertFalse(row['search']['idSearch'])
        self.assertEqual(['vote'], row['search']['metaNames'])
        res = server.m_files_identify({'module': ABBR, 'names': ['kq_123.jpg', 'db_5.jpg']})
        self.assertEqual({ABBR: False, 'db': True}, {q['module']: q['idSearch'] for q in res['queries']})


# ----- extension generator: robots.txt is paced too -----

class _Resp:
    def __init__(self, url: str, status: int, body: bytes) -> None:
        self.url, self.status_code, self._body, self.headers = url, status, body, {'content-type': 'text/plain'}
        self.is_redirect = self.is_permanent_redirect = False

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def iter_content(self, n: int):
        yield self._body


class SafeFetcherPacingTests(TestCase):
    def test_robots_request_is_paced(self) -> None:
        # C4: the first page after robots.txt waited nothing (st.last stayed 0): now min_interval / Crawl-delay
        for robots, wait in (('User-agent: *\nAllow: /\n', 2.0), ('User-agent: *\nCrawl-delay: 5\n', 5.0)):
            clock, sleeps, calls = [1000.0], [], []

            def sleep(s: float, clock=clock, sleeps=sleeps) -> None:
                sleeps.append(round(s, 3))
                clock[0] += s

            def request(method, url, **_):
                calls.append((url, clock[0]))
                return _Resp(url, 200, robots.encode() if url.endswith('/robots.txt') else b'ok')

            f = SafeFetcher(allowed_hosts={'site.example'}, min_interval=2.0, allow_local=True)
            f._session = types.SimpleNamespace(request=request)
            with mock.patch('lynxx.extgen.fetch.time.monotonic', side_effect=lambda clock=clock: clock[0]), \
                    mock.patch('lynxx.extgen.fetch.time.sleep', side_effect=sleep):
                f.fetch('https://site.example/posts')
                f.fetch('https://site.example/posts?page=2')
            self.assertEqual(['https://site.example/robots.txt', 'https://site.example/posts', 'https://site.example/posts?page=2'],
                             [u for u, _ in calls])
            gaps = [round(b[1] - a[1], 3) for a, b in itertools.pairwise(calls)]
            self.assertEqual([wait, wait], gaps, robots)
            self.assertEqual([wait, wait], sleeps)


class ExtensionGateInstallTests(TestCase):
    def test_a_refused_update_keeps_the_installed_version(self) -> None:
        # a core before 2.0.11 ('search' unknown) refuses the new version and keeps the one installed (transactional install)
        from lynxx.downloaders import DOWNLOADERS_BY_PROC_MODULE as registry
        loaded, downloaders = dict(Extensions.loaded), dict(registry)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': tmp}):
            Extensions.ensure_loaded()
            manifest = {**KQ_MANIFEST, 'id': 'kindsite2', 'abbr': 'kz', 'version': '1.0.0'}
            manifest.pop('search')
            manifest.pop('minSdkVersion')
            old = _write_kq(pathlib.Path(tmp) / 'old', manifest)
            (old / 'downloader.py').write_text(KQ_DOWNLOADER.replace("'kq'", "'kz'").replace(', split_kind', '')
                                               .replace('kind, rest = split_kind(tags)', 'kind, rest = None, tags'), encoding='utf-8')
            shutil.move(old, pathlib.Path(tmp) / 'old' / 'kindsite2')
            try:
                Extensions.install_from_dir(pathlib.Path(tmp) / 'old' / 'kindsite2')
                new = _write_kq(pathlib.Path(tmp) / 'new', {**KQ_MANIFEST, 'id': 'kindsite2', 'abbr': 'kz'})
                shutil.move(new, pathlib.Path(tmp) / 'new' / 'kindsite2')
                with mock.patch.dict(ExtensionManifest.__dataclass_fields__):
                    del ExtensionManifest.__dataclass_fields__['search']
                    with self.assertRaisesRegex(ManifestError, r'unknown field\(s\): search \(bản extension này viết cho Lynxx mới hơn: '
                                                               r'cập nhật Lynxx rồi cài lại\)$'), contextlib.redirect_stdout(io.StringIO()):
                        Extensions.install_from_dir(pathlib.Path(tmp) / 'new' / 'kindsite2')
                    # the parser itself (the generator checks its drafts with it): no hint, a draft's unknown key is its own mistake
                    with self.assertRaisesRegex(ManifestError, r'^unknown field\(s\): search$'):
                        ExtensionManifest.from_dict({**KQ_MANIFEST, 'id': 'kindsite2', 'abbr': 'kz'}, None, 'user')
                installed = json.loads((Extensions.user_dir() / 'kindsite2' / 'manifest.json').read_text(encoding='utf-8'))
                self.assertEqual(('1.0.0', False), (installed['version'], 'search' in installed))
            finally:
                Extensions.loaded.clear()
                Extensions.loaded.update(loaded)
                registry.clear()
                registry.update(downloaders)


# ----- repository extensions fixed with 2.0.11: favorites, pools, 'or' groups, id searches -----

class RepoExtensionFixTests(TestCase):
    """extensions/<id> as the repository has them: yande.re / Sakugabooru favorites, Gelbooru favorites and pools through the DAPI,
    Gelbooru / TBIB 'or' groups, the API-only sites without favorites / pools, rule34.xyz / rule34.world / Zerochan without id
    searches. Every address is built offline: fetch_html is faked and records what would be requested"""
    PATCHED = {'yandere': 'yr', 'sakugabooru': 'sk', 'gelbooru': 'gb', 'tbib': 'tb'}
    '''1.0.1: fixes that keep working on Lynxx 2.0.7 to 2.0.10 (no new manifest key, SDK 1.1 names only); Gelbooru 1.0.2 (posts and
    tags as gelbooru.com answers them, child elements) keeps that rule'''
    NO_ID_SEARCH = {'rule34xyz': 'rz', 'rule34world': 'rw', 'zerochan': 'zc'}
    '''1.1.0: manifest 'search' {"idSearch": false} (Lynxx 2.0.11+); rule34.xyz / rule34.world 1.1.1: the post's smaller files for
    -lowres / -max_vres (no new manifest key, SDK 1.1 names only)'''
    API_ONLY = {'safebooru': 'sf', 'tbib': 'tb', 'hypnohub': 'hh'}
    '''favorites / pools are html pages there and the terms allow the API only: syntax.fav / syntax.pool ""'''
    GB_KEY = ('-api_key', 'testkey0123456789,12')
    '''made up: the gelbooru module does not run without a key'''
    GB_API = 'https://gelbooru.com/index.php?page=dapi&s=post&q=index&api_key=testkey0123456789&user_id=12'

    @classmethod
    def setUpClass(cls) -> None:
        from lynxx.extensions import SOURCE_CATALOG
        Extensions.ensure_loaded(include_catalog=True)
        Extensions._load_many(Extensions._scan(ROOT / 'extensions', SOURCE_CATALOG))  # an earlier test may have pointed the catalog elsewhere

    def setUp(self) -> None:
        self.urls: list[str] = []
        self.body = b'[]'
        self.patches = [mock.patch.object(server, 'notify'), mock.patch('lynxx.network.time.sleep')]
        for abbr in {*self.PATCHED.values(), *self.NO_ID_SEARCH.values(), *self.API_ONLY.values()}:
            cls = DOWNLOADERS_BY_PROC_MODULE[ProcModule.PROC_MODULES_BY_NAME[abbr]]
            self.patches.append(mock.patch.object(cls, 'fetch_html', lambda _, url, **kw: self._fetch(url)))
        for p in self.patches:
            p.start()
        reset_last_tags()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        reset_last_tags()
        ProcModule.set(ProcModule.RX)

    def _fetch(self, url: str) -> HTMLContent:
        self.urls.append(url)
        return HTMLContent(None, self.body)

    def manifest(self, ext_id: str) -> ExtensionManifest:
        m = Extensions.loaded.get(ext_id)
        self.assertIsNotNone(m, ext_id)
        self.assertEqual((ROOT / 'extensions' / ext_id).resolve(), m.path.resolve(), f'{ext_id}: the repository version is loaded')
        return m

    @staticmethod
    def addresses(abbr: str, tags: str, *extra: str):
        """(downloader, search address of each task) as a run builds them, favorites / pools preprocessed"""
        dwn = _KqEnv.parsed(abbr, tags, *extra, preprocess=True)
        return dwn, [dwn._form_tags_search_address(dwn._consume_custom_module_tags(t)) for t in dwn.tags_str_arr]

    def test_versions_and_catalog_gate(self) -> None:
        spec = importlib.util.spec_from_file_location('build_catalog_repo_ext', ROOT / 'tools' / 'build_catalog.py')
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        rows = [(e, '1.0.2' if e == 'gelbooru' else '1.0.1', []) for e in self.PATCHED]
        rows += [(e, '1.1.0' if e == 'zerochan' else '1.1.1', ['search (Lynxx 2.0.11)']) for e in self.NO_ID_SEARCH]
        for ext_id, version, gate in rows:
            with self.subTest(ext_id):
                m = self.manifest(ext_id)
                raw = json.loads((ROOT / 'extensions' / ext_id / 'manifest.json').read_text(encoding='utf-8'))
                self.assertEqual((version, '1.1'), (m.version, m.minSdkVersion), 'no SDK 1.2 names')
                self.assertEqual(gate, tool.newer_core_keys(raw), 'published only once Lynxx 2.0.11 is out' if gate else 'old cores')

    def test_moebooru_favorites_are_the_site_vote_search(self) -> None:
        # the core routes favorites as favorited_by:<name> only; 1.0.0's fav regex 'vote:3:...' could never match it
        for ext_id, abbr, site in (('yandere', 'yr', 'https://yande.re/'), ('sakugabooru', 'sk', 'https://www.sakugabooru.com/')):
            with self.subTest(ext_id):
                self.assertEqual('favorited_by:[^:]+?', self.manifest(ext_id).syntax.fav)
                dwn, urls = self.addresses(abbr, 'scenery favorited_by:bob -favorited_by:eve')
                self.assertEqual((['scenery+favorited_by:bob+-favorited_by:eve'], ''), (dwn.tags_str_arr, dwn.favorites_search_user),
                                 'the task keeps the tag, nothing to convert')
                self.assertEqual([f'{site}post.json?tags=scenery+vote:3:bob+-vote:3:eve&limit=100&page=1'], urls)
                self.assertEqual(f'{site}post.xml?tags=scenery+vote:3:bob+-vote:3:eve&limit=1', dwn._count_url(dwn._task_tags))
                dwn.cursor_base_tags = dwn._consume_custom_module_tags(dwn.tags_str_arr[0])
                self.assertIn('?tags=scenery+vote:3:bob+-vote:3:eve+order:id_desc+id:<500&', dwn._form_cursor_page_address(500, False))
                self.assertFalse(self.addresses(abbr, 'favorited_by:bob order:vote')[0].default_sort, 'order:vote: vote time')
                self.assertEqual([f'{site}post.json?tags=vote:3:bob&limit=100&page=1'], self.addresses(abbr, 'vote:3:bob')[1],
                                 'the site\'s own spelling still works')
                with self.assertRaises(TagsError):
                    self.addresses(abbr, 'favorited_by:bob favorited_by:eve')
                self.assertEqual([], self.urls)
                # the sidecar: Preview and count
                res = server.m_search_preview({'module': abbr, 'tags': 'favorited_by:bob', 'args': []})
                self.assertEqual((['favorited_by:bob'], [f'{site}post.json?tags=vote:3:bob&limit=100&page=1']), (res['tasks'], self.urls))
                self.urls.clear()
                self.body = b'<?xml version="1.0" encoding="UTF-8"?><posts count="7" offset="0"></posts>'
                self.assertEqual(7, server.m_search_count({'module': abbr, 'tags': 'favorited_by:bob', 'args': []})['count'])
                self.assertEqual([f'{site}post.xml?tags=vote:3:bob&limit=1'], self.urls)
                self.urls.clear()
                self.body = b'[]'

    def test_sakugabooru_tag_types(self) -> None:
        # the site's types (its /tag page): General 0, Artist 1, Copyright 3, Terminology 4, Meta 5 (not yande.re's circle)
        spec = importlib.util.spec_from_file_location('sk_tags_repo_ext', ROOT / 'extensions' / 'sakugabooru' / 'tags.py')
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        page = [{'name': 'presumed', 'count': 37867, 'type': 5}, {'name': 'some_term', 'count': 10, 'type': 4},
                {'name': 'some_animator', 'count': 50, 'type': 1}, {'name': 'some_show', 'count': 900, 'type': 3},
                {'name': 'effects', 'count': 9000, 'type': 0}]
        asked: list[str] = []
        job = types.SimpleNamespace(site='https://www.sakugabooru.com/', get_json=lambda url: asked.append(url) or (page if len(asked) == 1 else []))
        self.assertEqual({'presumed': (37867, 'metadata'), 'some_term': (10, 'general'), 'some_animator': (50, 'artist'),
                          'some_show': (900, 'copyright'), 'effects': (9000, 'general')},
                         {name: (posts, kind) for name, posts, kind in mod.read_tags(job)})
        self.assertEqual(['https://www.sakugabooru.com/tag.json?limit=1000&page=1&order=name'], asked)

    def test_gelbooru_favorites_and_pools_through_the_dapi(self) -> None:
        m = self.manifest('gelbooru')
        self.assertEqual(('favorited_by:\\d+?', 'pool:\\d+?'), (m.syntax.fav, m.syntax.pool))
        dwn, urls = self.addresses('gb', 'favorited_by:123 rating:general', *self.GB_KEY)
        self.assertEqual((['favorited_by:123+rating:general'], '', ''), (dwn.tags_str_arr, dwn.favorites_search_user, dwn.pool_search_str),
                         'no favorites user: DownloaderGelbooru builds no html favorites address')
        self.assertEqual([f'{self.GB_API}&tags=fav:123+rating:general&limit=100'], urls)
        self.assertTrue(dwn._supports_id_cursor())
        dwn.cursor_base_tags = dwn._consume_custom_module_tags(dwn.tags_str_arr[0])
        self.assertEqual(f'{self.GB_API}&tags=fav:123+rating:general+id:<500&limit=100&pid=0', dwn._form_cursor_page_address(500, False))
        dwn, urls = self.addresses('gb', 'pool:2 -rating:explicit', *self.GB_KEY)
        self.assertEqual(('', [f'{self.GB_API}&tags=pool:2+-rating:explicit&limit=100']), (dwn.pool_search_str, urls))
        for refused in ('favorited_by:bob', 'pool:some_name', 'favorited_by:1 pool:2'):  # user and pool ids, one of them per query
            with self.subTest(refused), self.assertRaises(TagsError):
                self.addresses('gb', refused, *self.GB_KEY)
        self.assertEqual([], self.urls)
        self.body = b'<?xml version="1.0" encoding="UTF-8"?><posts count="0" offset="0"></posts>'
        res = server.m_search_preview({'module': 'gb', 'tags': 'favorited_by:123', 'args': list(self.GB_KEY), 'pageSize': 40})
        self.assertEqual((['favorited_by:123'], [f'{self.GB_API}&tags=fav:123&limit=40&pid=0']), (res['tasks'], self.urls))

    def test_or_groups_run_as_one_search_each(self) -> None:
        # gelbooru.com documents 'tag1 {tag2 ~ tag3}' (not with metatags), TBIB's '~tag1 ~tag2' "currently does not work"
        tb_api = 'https://tbib.org/index.php?page=dapi&s=post&q=index'
        for abbr, extra, api in (('gb', self.GB_KEY, self.GB_API), ('tb', (), tb_api)):
            with self.subTest(abbr):
                dwn, urls = self.addresses(abbr, 'a_tag (b_tag~c_tag)', *extra)
                self.assertEqual(['b_tag+a_tag', 'c_tag+a_tag'], dwn.tags_str_arr)
                self.assertEqual([f'{api}&tags=b_tag+a_tag&limit=100', f'{api}&tags=c_tag+a_tag&limit=100'], urls)
        # sites whose cheat sheet documents '( a ~ b )' keep one search
        self.assertEqual(['a_tag+(+b_tag+~+c_tag+)'], self.addresses('sf', 'a_tag (b_tag~c_tag)')[0].tags_str_arr)
        self.assertEqual([], self.urls)

    def test_api_only_sites_offer_no_favorites_or_pools(self) -> None:
        for ext_id, abbr in self.API_ONLY.items():
            m = self.manifest(ext_id)
            self.assertEqual(('', '', ()), (m.syntax.fav, m.syntax.pool, module_search(ProcModule.PROC_MODULES_BY_NAME[abbr]).kinds), ext_id)
            for refused in ('favorited_by:1', 'pool:1', 'scenery favorited_by:1'):
                with self.subTest(abbr=abbr, tags=refused), self.assertRaises(TagsError):
                    self.addresses(abbr, refused)
        self.assertEqual([], self.urls)

    def test_modules_without_id_search(self) -> None:
        rows = {r['id']: r for r in Extensions.list_all()}
        for ext_id, abbr in self.NO_ID_SEARCH.items():
            with self.subTest(ext_id):
                m = self.manifest(ext_id)
                self.assertEqual((False, [], [], []), (m.search.idSearch, m.search.kinds, m.search.metaNames, m.search.notes))
                self.assertFalse(id_search_supported(abbr))
                self.assertEqual({'kinds': [], 'metaNames': [], 'idSearch': False}, rows[ext_id]['search'], 'what the app reads')
        for ext_id, abbr in self.PATCHED.items():
            self.assertTrue(id_search_supported(abbr) and rows[ext_id]['search']['idSearch'], ext_id)

    def test_old_cores(self) -> None:
        # the real 2.0.7 manifest reader and SDK names (the first release with extensions), when git has the tag: a shallow clone does not
        import ast
        sources = {}
        for name in ('modspec', 'sdk'):
            src = subprocess.run(('git', 'show', f'v2.0.7:lynxx/{name}.py'), cwd=ROOT, capture_output=True, timeout=60)
            if src.returncode != 0:
                self.skipTest('the v2.0.7 tag is not in this checkout')
            sources[name] = src.stdout.decode('utf-8')
        sdk_names = next(set(ast.literal_eval(node.value)) for node in ast.parse(sources['sdk']).body
                         if isinstance(node, ast.Assign) and any(getattr(t, 'id', '') == '__all__' for t in node.targets))
        old = types.ModuleType('lynxx._modspec_2_0_7')
        old.__package__ = 'lynxx'
        sys.modules[old.__name__] = old  # dataclasses look the module up
        try:
            exec(compile(sources['modspec'], 'modspec_2_0_7.py', 'exec'), old.__dict__)  # noqa: S102
            for ext_id in {*self.PATCHED, *self.API_ONLY}:
                with self.subTest(ext_id):
                    raw = json.loads((ROOT / 'extensions' / ext_id / 'manifest.json').read_text(encoding='utf-8'))
                    m = old.ExtensionManifest.from_dict(raw, None, 'catalog')
                    self.assertEqual((raw['syntax']['fav'], raw['syntax']['pool']), (m.syntax.fav, m.syntax.pool), '"" is accepted')
                    tree = ast.parse((ROOT / 'extensions' / ext_id / 'downloader.py').read_text(encoding='utf-8'))
                    imported = {(node.module, a.name) for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
                                and node.module.startswith('lynxx') for a in node.names}
                    self.assertEqual(set(), {f'{mod}.{n}' for mod, n in imported if mod != 'lynxx.sdk' or n not in sdk_names}, 'SDK 1.1')
            for ext_id in self.NO_ID_SEARCH:
                raw = json.loads((ROOT / 'extensions' / ext_id / 'manifest.json').read_text(encoding='utf-8'))
                with self.subTest(ext_id), self.assertRaisesRegex(old.ManifestError, r'unknown field\(s\): search'):
                    old.ExtensionManifest.from_dict(raw, None, 'catalog')
        finally:
            sys.modules.pop(old.__name__, None)


# ----- Realbooru (extensions/realbooru 1.1.0): pools and favorites read from their own pages -----

import ast  # noqa: E402
import hashlib  # noqa: E402
import re  # noqa: E402
from collections.abc import Iterable  # noqa: E402

from lynxx.robots import RobotsRules  # noqa: E402

RB = ROOT / 'extensions' / 'realbooru'
RB_SITE = 'https://realbooru.com/'
RB_FIXTURES = pathlib.Path(__file__).resolve().parent / 'fixtures' / 'realbooru'
RB_POOL = f'{RB_SITE}index.php?page=pool&s=show&id=678'
RB_FAVS = f'{RB_SITE}index.php?page=favorites&s=view&id=1'
RB_POOL_FILE = 'pool_s_show_id=678.html'
RB_FAVS_FILE = 'favorites_s_view_id=1.html'
RB_POOL_TAGS = ('solo blonde', 'solo animated gif', 'duo webm sound', 'solo bed', 'solo bed', 'duo video', 'solo webm', 'solo webm',
                'trio 1:1_aspect_ratio')
'''tags the tests give the 9 thumbnails of the recorded pool page, in its order (posts 888876 and 877357 are on it twice)'''
RB_POOL_IDS = ['890613', '888878', '888877', '888876', '888875', '877357', '822529']


def _rb_pool_page(page: str | None = None) -> str:
    """the recorded pool page, its thumbnails' titles as a pool page writes them: '<tags> rating:… score:… user:…'"""
    page = (RB_FIXTURES / RB_POOL_FILE).read_text(encoding='utf-8') if page is None else page
    for n, tags in enumerate(RB_POOL_TAGS):
        title = f'{tags} rating:Explicit score:{n:d} user:Some_User'
        page = page.replace('{TITLE}', title, 1).replace('{ALT}', title, 1)
    return page


def _rb_favorites_fixture(tags: str) -> str:
    """the recorded favorites page (one favorite, no paginator links) with these tags"""
    page = (RB_FIXTURES / RB_FAVS_FILE).read_text(encoding='utf-8')
    return page.replace('{TITLE}', tags, 1).replace('{TAGS_URL}', url_parse.quote(tags), 1)


def _rb_favorites_page(user: int, ids: Iterable[int], pids: Iterable[int] = (), onclick=False) -> str:
    """a favorites page written by a test in the recorded page's markup: thumbnails tagged 'solo', then a paginator linking the
    offsets pids (as href, or as the onclick of Gelbooru 0.2 favorites: the recorded page had one page only)"""
    thumbs = ''.join(
        f'<span class="thumb"><a href="index.php?page=post&amp;s=view&amp;id={i:d}" id="p{i:d}" onclick="document.location='
        f'\'index.php?page=post&amp;s=view&amp;id={i:d}\'; return false;"><img src="https://realbooru.com/thumbnails//aa/bb/'
        f'thumbnail_{i:032x}.jpg?{i:d}" title="solo" border="0" alt="image_thumb"/></a></span>' for i in ids)
    link = '?page=favorites&amp;s=view&amp;id={u:d}&amp;pid={p:d}'
    links = ''.join((f'<a href="javascript:;" onclick="document.location=\'{link.format(u=user, p=p)}\'; return false;">{n + 2:d}</a> '
                     if onclick else f'<a href="{link.format(u=user, p=p)}">{n + 2:d}</a> ') for n, p in enumerate(pids))
    return (f'<html><body><div id="header"><a href="https://realbooru.com/index.php?page=favorites&s=view&id=">Favorites</a></div>'
            f'{thumbs}<div id=\'paginator\'><b>1</b> {links}<br /><br /></body></html>')


class RealbooruRouteTests(TestCase):
    """Realbooru (rb): 'pool:<id>' and 'favorited_by:<user id>' are listing kinds (manifest 'search', SDK 1.2) read from the site's
    own pages, in the site's order: a pool's page holds every post of the pool, favorites pages step by 'pid' offsets read from
    the paginator. The query's tags are checked locally, other site metatags are refused, the addresses are checked against the
    robots.txt saved next to the manifest, then the post page step decides each post. Pages recorded on 2026-11-21
    (lynxx/tests/fixtures/realbooru, SOURCES.json: their tags replaced by placeholders the tests fill); the pages a test writes
    (_rb_favorites_page) are not. Requests go through a fake wrap_request: fetch_html's cache is the real one"""
    tmp: tempfile.TemporaryDirectory
    env: mock._patch

    @classmethod
    def setUpClass(cls) -> None:
        from lynxx.extensions import SOURCE_CATALOG
        cls.tmp = tempfile.TemporaryDirectory()
        cls.env = mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': cls.tmp.name, 'LYNXX_TAGS_DIR': os.path.join(cls.tmp.name, 'tags')})
        cls.env.start()
        Extensions.ensure_loaded(include_catalog=True)
        Extensions._load_many(Extensions._scan(ROOT / 'extensions', SOURCE_CATALOG))  # an earlier test may have pointed the catalog elsewhere
        cls.pid = ProcModule.PROC_MODULES_BY_NAME['rb']
        cls.cls_rb = DOWNLOADERS_BY_PROC_MODULE[cls.pid]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.env.stop()
        cls.tmp.cleanup()
        ProcModule.set(ProcModule.RX)

    def setUp(self) -> None:
        self.pages: dict[str, str] = {RB_POOL: _rb_pool_page()}
        self.urls: list[str] = []
        self.patches = [mock.patch.object(self.cls_rb, 'wrap_request', lambda _, url, *a, **kw: self._request(url)),
                        mock.patch.object(server, 'notify'), mock.patch('lynxx.network.time.sleep')]
        for p in self.patches:
            p.start()
        TagsDB.clear()
        reset_last_tags()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        TagsDB.clear()
        reset_last_tags()
        ProcModule.set(ProcModule.RX)

    def _request(self, url: str) -> types.SimpleNamespace | None:
        self.urls.append(url)
        body = self.pages.get(url)
        return None if body is None else types.SimpleNamespace(content=body.encode('utf-8'))

    @staticmethod
    def parsed(tags: str, *extra: str):
        return _KqEnv.parsed('rb', tags, *extra)

    @staticmethod
    def preview(tags: str, **params) -> dict:
        return server.m_search_preview({'module': 'rb', 'tags': tags, 'args': [], **params})

    @staticmethod
    def count(tags: str) -> int:
        return server.m_search_count({'module': 'rb', 'tags': tags, 'args': []})['count']

    def failure(self, call, *args) -> str:
        """the message of a run the sidecar stops (RpcError: a count gives the run's error)"""
        with self.assertRaises(server.RpcError) as ctx:
            call(*args)
        return ctx.exception.message

    def _run(self, tags: str, dest: str):
        ProcModule.set(self.pid)
        reset_last_tags()
        args = prepare_arglist(['-module', 'rb', '-dmode', 'skip', '-path', dest, '--', *tags.split()])
        dwn = _make_downloader(self.pid)
        me = current_process()
        me.killed = False
        dwn.reset_root_thread(me)
        dwn.no_conflict_delay = True
        dwn.launch_download(args)
        return dwn

    def test_fixtures_are_the_recorded_pages(self) -> None:
        files = json.loads((RB_FIXTURES / 'SOURCES.json').read_text(encoding='utf-8'))['files']
        self.assertEqual({RB_POOL_FILE: RB_POOL, RB_FAVS_FILE: RB_FAVS}, {name: meta['url'] for name, meta in files.items()})
        for name, meta in files.items():
            data = (RB_FIXTURES / name).read_bytes()
            self.assertEqual((meta['bytes'], meta['sha256']), (len(data), hashlib.sha256(data).hexdigest()), name)
        pool = (RB_FIXTURES / RB_POOL_FILE).read_text(encoding='utf-8')
        self.assertEqual((9, 9, 9, 1), (pool.count('<span class="thumb"'), pool.count('title="{TITLE}"'), pool.count('alt="{ALT}"'),
                                        pool.count('<h4>Pool: ')))
        self.assertEqual(len(RB_POOL_TAGS), pool.count('{TITLE}'))
        favs = (RB_FIXTURES / RB_FAVS_FILE).read_text(encoding='utf-8')
        self.assertEqual((1, 1, 1), (favs.count('<span class="thumb">'), favs.count('{TITLE}'), favs.count('{TAGS_URL}')))
        # byte for byte on every system: git never turns their line ends into CRLF
        rel = [(RB_FIXTURES / name).relative_to(ROOT).as_posix() for name in files]
        attrs = subprocess.run(('git', 'check-attr', 'text', '--', *rel), cwd=ROOT, capture_output=True, timeout=60)
        if attrs.returncode == 0:  # else not a git checkout (a source archive)
            self.assertEqual({f'{r}: text: unset' for r in rel}, set(attrs.stdout.decode('utf-8').splitlines()))

    def test_manifest_robots_and_catalog_gate(self) -> None:
        m = Extensions.loaded.get('realbooru')
        self.assertEqual(RB.resolve(), m.path.resolve(), 'the repository version is loaded')
        self.assertEqual(('1.1.0', '1.2', '', ''), (m.version, m.minSdkVersion, m.syntax.fav, m.syntax.pool),
                         'not the core favorites / pool pipeline: listing kinds')
        self.assertTrue(sdk_satisfies(m.minSdkVersion))
        self.assertEqual({'pool': ('pool:', '[0-9]+', 'id', True), 'favorites': ('favorited_by:', '[0-9]+', 'id', True)},
                         {k.id: (k.token, k.value, k.input, k.listing) for k in m.search.kinds})
        self.assertEqual(([], True, []), (m.search.metaNames, m.search.idSearch, m.search.notes))
        spec = importlib.util.spec_from_file_location('build_catalog_rb', ROOT / 'tools' / 'build_catalog.py')
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        raw = json.loads((RB / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(['minSdkVersion 1.2 (Lynxx 2.0.11)', 'search (Lynxx 2.0.11)'], tool.newer_core_keys(raw),
                         'published once Lynxx 2.0.11 is out')
        # only SDK names (1.2: split_kind, current_kind, require_robots_allowed)
        from lynxx import sdk
        tree = ast.parse((RB / 'downloader.py').read_text(encoding='utf-8'))
        imported = {(node.module, a.name) for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
                    and node.module.startswith('lynxx') for a in node.names}
        self.assertEqual({'lynxx.sdk'}, {mod for mod, _ in imported})
        self.assertLessEqual({'split_kind'}, {n for _, n in imported})
        self.assertLessEqual({n for _, n in imported}, set(sdk.__all__))
        # the site's robots.txt (2026-11-21), saved next to the manifest: the routes are allowed
        rules = RobotsRules.parse((RB / 'robots.txt').read_text(encoding='utf-8'), 'Lynxx')
        for url in (RB_POOL, RB_FAVS, f'{RB_FAVS}&pid=50', f'{RB_SITE}index.php?page=post&s=list&tags=all&pid=42'):
            self.assertTrue(rules.allows(url), url)
        self.assertFalse(rules.allows(f'{RB_SITE}public/remove.php'))

    def test_queries(self) -> None:
        dwn = self.parsed('pool:678 solo -webm')
        self.assertEqual((['pool:678'], ['solo'], 1), (dwn.tags_str_arr, dwn.required_tags, len(dwn.neg_and_groups)),
                         'the tags are checked locally')
        self.assertEqual((KindMatch('pool', 'pool:', '678'), 'pool:678', False), (dwn.current_kind, dwn.site_order_kind, dwn.default_sort))
        self.assertEqual(('', ''), (dwn.favorites_search_user, dwn.pool_search_str))
        self.assertEqual(RB_POOL, dwn._form_tags_search_address(dwn.tags_str_arr[0]))
        dwn = self.parsed('favorited_by:1 -solo')
        self.assertEqual((['favorited_by:1'], [], 1, False), (dwn.tags_str_arr, dwn.required_tags, len(dwn.neg_and_groups), dwn.default_sort))
        self.assertEqual(RB_FAVS, dwn._form_tags_search_address(dwn.tags_str_arr[0]))
        dwn = self.parsed('solo -webm')
        self.assertEqual((None, True), (dwn.current_kind, dwn.default_sort))
        self.assertEqual(f'{RB_SITE}index.php?page=post&s=list&tags=solo+-webm', dwn._form_tags_search_address('solo+-webm'))
        self.assertEqual(f'{RB_SITE}index.php?page=post&s=list&tags=solo+-webm&pid=84', (setattr(dwn, 'url', dwn._form_tags_search_address(
            'solo+-webm')), dwn._form_page_num_address(2))[1], 'the post listing as before')
        for tags, why in (('pool:abc', "not a valid value for 'pool:'"), ('pool:1 favorited_by:2', 'one site listing per search'),
                          ('-pool:1 solo', 'cannot be negated'), ('favorited_by:1 (a~b)', "'or' groups cannot be used"),
                          # before: tasks pool:678 / pool:679 without their kind, the pool page paged by &pid=, ids order
                          ('(pool:678~pool:679)', "pool:678 is a site listing: 'or' groups cannot be used"),
                          ('(pool:1~solo)', "pool:1 is a site listing: 'or' groups"), ('(solo~favorited_by:1)', 'favorited_by:1 is a site')):
            with self.subTest(tags), self.assertRaisesRegex(TagsError, why):
                self.parsed(tags)
        with self.assertRaisesRegex(ThreadInterruptException, "'pool:678' lists posts in the site's order: date limits cannot be used"):
            self.parsed('pool:678 solo', '-mindate', '01-01-2020')
        self.assertEqual([], self.urls)

    def test_other_site_tags_are_refused_before_any_request(self) -> None:
        for tags, other in (('pool:678 rating:explicit', 'rating:explicit'), ('favorited_by:1 solo sort:score', 'sort:score'),
                            ('pool:678 -rating:safe', '-rating:safe'), ('pool:678 id:>1000', 'id:>1000')):
            dwn = self.parsed(tags)
            with self.subTest(tags), self.assertRaisesRegex(ThreadInterruptException, f"'{re.escape(other)}' cannot be used with it"):
                dwn._form_tags_search_address(dwn.tags_str_arr[0])
        self.assertIn("'pool:678' reads the pool page of the site", self.failure(self.count, 'pool:678 rating:explicit'))
        with self.assertRaises(server.RpcError):
            self.preview('pool:678 rating:explicit')
        self.assertEqual([], self.urls)

    def test_robots_txt_is_checked_before_any_request(self) -> None:
        rules = RobotsRules.parse('User-agent: *\nDisallow: /index.php?page=pool\n', 'Lynxx')
        with mock.patch('lynxx.download_base.saved_robots_rules', return_value=rules):
            self.assertIn(f'robots.txt does not allow {RB_POOL}', self.failure(self.count, 'pool:678'))
            with self.assertRaises(server.RpcError):
                self.preview('pool:678')
            self.assertEqual(RB_FAVS, self.parsed('favorited_by:1')._form_tags_search_address('favorited_by:1'))
        self.assertEqual([], self.urls)

    def test_a_pool(self) -> None:
        res = self.preview('pool:678 solo')
        self.assertEqual([RB_POOL], self.urls, 'one page: the whole pool')
        self.assertEqual(RB_POOL_IDS, [it['id'] for it in res['items']], "the pool's order, each post once")
        self.assertEqual((5000, False, ['pool:678'], ['solo'], False),
                         (res['pageSize'], res['hasMore'], res['tasks'], res['requiredTags'], res['localFiltersUnchecked']))
        by_id = {it['id']: it for it in res['items']}
        self.assertEqual(['solo', 'blonde'], by_id['890613']['tags'], 'without the rating / score / uploader the page adds')
        self.assertEqual(['trio', '1:1_aspect_ratio'], by_id['822529']['tags'])
        self.assertEqual({'888877', '888875', '877357'}, {i for i, it in by_id.items() if it['isVideo']}, 'by their tags')
        self.assertEqual({'890613', '888878', '888876', '877357'}, {i for i, it in by_id.items() if not it['excluded']})
        self.assertEqual(('a3e3cdba50592c33460343343559c702', f'{RB_SITE}thumbnails//a3/e3/thumbnail_a3e3cdba50592c33460343343559c702.jpg'),
                         (by_id['890613']['md5'], by_id['890613']['preview']))
        self.urls.clear()
        self.assertEqual(7, self.count('pool:678'))
        self.assertEqual([RB_POOL], self.urls)
        self.urls.clear()
        with tempfile.TemporaryDirectory() as dest:
            dwn = self._run('pool:678 solo -webm', dest)
        self.assertEqual(('', [RB_POOL]), (dwn.launch_error, self.urls), 'counted and scanned from one request')
        self.assertEqual(3, dwn.processed_count)
        self.assertEqual(['890613', '888878', '888876'], [dwn._extract_id(x) for x in dwn.items_raw_all], "in the pool's order")

    def test_a_pool_the_site_does_not_show(self) -> None:
        # no pool block (a redirect to the pool list, a login page...): no such pool, or a private one
        self.pages[f'{RB_SITE}index.php?page=pool&s=show&id=5'] = ('<html><body><div id="content"><div id="pool-index"><table>'
                                                                  '</table></div></div></body></html>')
        self.assertIn('the site shows no pool 5: there is no pool with this id, or it is private', self.failure(self.count, 'pool:5'))
        # the pool block without posts: nothing to download
        empty = re.sub(r'<span class="thumb".*?</span>', '', (RB_FIXTURES / RB_POOL_FILE).read_text(encoding='utf-8'), flags=re.S)
        self.pages[f'{RB_SITE}index.php?page=pool&s=show&id=6'] = empty
        self.assertEqual(0, self.count('pool:6'))
        self.assertEqual([], self.preview('pool:6')['items'])

    def test_favorites(self) -> None:
        self.pages[RB_FAVS] = _rb_favorites_fixture('solo animated sound long_video')
        res = self.preview('favorited_by:1')
        self.assertEqual([RB_FAVS], self.urls)
        self.assertEqual([('646949', ['solo', 'animated', 'sound', 'long_video'], True, '88a210386608f9ec7139a607681b8341')],
                         [(it['id'], it['tags'], it['isVideo'], it['md5']) for it in res['items']], "md5 of 'thumbnail_<md5>.jpg?<id>'")
        self.assertEqual((50, False), (res['pageSize'], res['hasMore']))
        self.assertEqual(1, self.count('favorited_by:1'))
        # none (or no such user): the same page without thumbnails
        self.pages[f'{RB_SITE}index.php?page=favorites&s=view&id=2'] = re.sub(r'<span class="thumb">.*?</span>', '', self.pages[RB_FAVS])
        self.assertEqual(0, self.count('favorited_by:2'))

    def test_favorites_pages_by_the_paginator(self) -> None:
        url = f'{RB_SITE}index.php?page=favorites&s=view&id=7'
        ids = [5000 - (n * 37) % 107 for n in range(107)]  # the order favorites were added, not by id
        self.pages.update({url: _rb_favorites_page(7, ids[:50], (50, 100)), f'{url}&pid=50': _rb_favorites_page(7, ids[50:100], (0, 100)),
                           f'{url}&pid=100': _rb_favorites_page(7, ids[100:], (0, 50))})
        self.assertEqual(107, self.count('favorited_by:7'))
        self.assertEqual([url, f'{url}&pid=100'], self.urls, 'first and last pages')
        self.urls.clear()
        with tempfile.TemporaryDirectory() as dest:
            dwn = self._run('favorited_by:7', dest)
        self.assertEqual(('', [url, f'{url}&pid=100', f'{url}&pid=50']), (dwn.launch_error, self.urls), 'each page once')
        self.assertEqual((107, [str(i) for i in ids]), (dwn.processed_count, [dwn._extract_id(x) for x in dwn.items_raw_all]),
                         'in the site\'s order')
        # pages of 40 (onclick links): the paginator gives the size, before the count and for Preview's next pages
        url = f'{RB_SITE}index.php?page=favorites&s=view&id=8'
        self.pages.update({url: _rb_favorites_page(8, range(100, 60, -1), (40, 80), onclick=True),
                           f'{url}&pid=40': _rb_favorites_page(8, range(60, 20, -1), (0, 80), onclick=True),
                           f'{url}&pid=80': _rb_favorites_page(8, range(20, 15, -1), (0, 40), onclick=True)})
        self.assertEqual(85, self.count('favorited_by:8'))
        self.urls.clear()
        res = self.preview('favorited_by:8', page=1)
        self.assertEqual(([url, f'{url}&pid=40'], [str(i) for i in range(60, 20, -1)]), (self.urls, [it['id'] for it in res['items']]))
        # a paginator in page numbers, not offsets: refused, the pages would be wrong
        url = f'{RB_SITE}index.php?page=favorites&s=view&id=9'
        self.pages[url] = _rb_favorites_page(9, range(200, 150, -1), (1, 2))
        self.assertIn('unexpected favorites pages: the first page shows 50 posts but its paginator steps by 1',
                      self.failure(self.count, 'favorited_by:9'))

    def test_the_post_page_decides_the_media_type(self) -> None:
        # pool / favorites pages tell videos by their tags only: a post tagged as an image may be a video
        dwn = self.parsed('pool:678')
        item = next(h for h in dwn._get_all_post_tags(HTMLContent(None, _rb_pool_page().encode()).html) if 'post_id="890613"' in h)
        self.assertFalse(dwn._is_video(item))
        post = f'{RB_SITE}index.php?page=post&s=view&id=890613'
        file_url = 'https://realbooru.com//images/a3/e3/a3e3cdba50592c33460343343559c702.webm'
        self.pages[post] = f'<html><body><a href="{file_url}">Original</a><video><source src="{file_url}"></video></body></html>'
        got: list[tuple[str, str]] = []
        with (mock.patch.object(dwn, '_process_video', lambda link, _: got.append(('video', link))),
              mock.patch.object(dwn, '_process_image', lambda link, _: got.append(('image', link)))):
            dwn.skip_videos = True
            dwn._process_item(item)
            self.assertEqual([], got, 'videos skipped')
            dwn.skip_videos = False
            dwn._process_item(item)
        self.assertEqual(([('video', file_url)], [post, post]), (got, self.urls))

#
#
#########################################
