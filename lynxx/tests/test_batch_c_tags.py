# coding=UTF-8
"""
Lynxx: regression tests for audit item C8 (docs/reports/audit-2026-11-16.md): tag list locking, compact storage,
binary cache, LRU unloading, cached popularity sorting. Offline
Run: python -m unittest lynxx.tests.test_batch_c_tags
"""
#########################################
#
#

import json
import os
import pathlib
import random
import tempfile
import threading
from unittest import TestCase, mock

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx import server, tags_updater, tagsdb  # noqa: E402
from lynxx.tags import TagInfo, TagTypes, tag_type_from_name  # noqa: E402
from lynxx.tagsdb import TAGS_MAX_LOADED, TagsDB  # noqa: E402

WAIT = 10.0
'''seconds: a call that takes longer is considered blocked'''


def _reference_load(path: pathlib.Path) -> dict[str, TagInfo]:
    """The original loader (dict of TagInfo, line by line), names in python sort order"""
    result: dict[str, TagInfo] = {}
    with open(path, 'rt', encoding='utf-8') as f:
        for line in f:
            try:
                k, v = TagsDB._parse_line(line)
                result[k] = TagInfo(tag_type_from_name(v, v.find('('), v.rfind(')')), int(v[:v.find(' ')]))
            except Exception:
                continue
    return dict(sorted(result.items()))


class _TagsTestBase(TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name)
        self.tags_dir = self.root / 'tags'
        self.tags_dir.mkdir()
        self.data_dir = self.root / 'data'
        self.env = mock.patch.dict(os.environ, {'LYNXX_TAGS_DIR': self.tags_dir.as_posix(), 'LYNXX_DATA_DIR': self.data_dir.as_posix()})
        self.env.start()
        TagsDB.clear()

    def tearDown(self) -> None:
        TagsDB.clear()
        self.env.stop()
        self.tmp.cleanup()

    def write_list(self, module: str, tags: dict[str, tuple[int, str]]) -> pathlib.Path:
        path = self.tags_dir / f'{module}_tags.json'
        tags_updater.write_tag_list(path, tags)
        return path

    @property
    def cache_files(self) -> list[pathlib.Path]:
        cdir = self.data_dir / 'cache' / 'tags'
        return sorted(cdir.glob('*.bin')) if cdir.is_dir() else []


class TagsLockingTests(_TagsTestBase):
    """Tag RPCs used _core_lock, which search.preview / search.count hold for a whole network request"""

    def setUp(self) -> None:
        super().setUp()
        self.write_list('zz', {'apple': (50, 'general'), 'apricot': (70, 'general'), 'absurdres': (100, 'metadata'), '1girl': (5, 'general')})
        TagsDB.locate_default_lists()

    def _hold_core_lock(self) -> threading.Event:
        held, release = threading.Event(), threading.Event()

        def holder() -> None:
            with server._core_lock:
                held.set()
                release.wait(60)

        thread = threading.Thread(target=holder, daemon=True)
        thread.start()
        self.assertTrue(held.wait(WAIT))
        self.addCleanup(thread.join, WAIT)
        self.addCleanup(release.set)
        return release

    def test_tag_methods_do_not_wait_for_core_lock(self) -> None:
        self._hold_core_lock()
        results: dict = {}

        def work() -> None:
            results['ac'] = server.m_tags_autocomplete({'module': 'zz', 'prefix': '-ap', 'limit': 5})
            results['posts'] = server.m_tags_browse({'module': 'zz', 'prefix': 'a', 'sort': 'posts'})
            results['az'] = server.m_tags_browse({'module': 'zz', 'prefix': 'a', 'sort': 'az', 'types': ['metadata']})
            results['other'] = server.m_tags_browse({'module': 'zz', 'prefix': '#'})
            results['status'] = server.m_tags_status({})

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        worker.join(WAIT)
        self.assertFalse(worker.is_alive(), 'tag methods waited for _core_lock')
        self.assertEqual([{'tag': 'apricot', 'posts': 70, 'type': 'general', 'insert': '-apricot'},
                          {'tag': 'apple', 'posts': 50, 'type': 'general', 'insert': '-apple'}], results['ac'])
        self.assertEqual(['absurdres', 'apricot', 'apple'], [x['tag'] for x in results['posts']['items']])
        self.assertEqual({'total': 1, 'items': [{'tag': 'absurdres', 'posts': 100, 'type': 'metadata'}]}, results['az'])
        self.assertEqual(['1girl'], [x['tag'] for x in results['other']['items']])
        self.assertEqual([('zz', True)], [(r['module'], r['loaded']) for r in results['status'] if r['module'] == 'zz'])

    def test_update_reload_does_not_wait_for_core_lock(self) -> None:
        self.assertEqual(4, TagsDB.count('zz'))
        done = threading.Event()
        notes: list[tuple[str, dict]] = []

        def fake_update(module: str, out_dir: pathlib.Path, **_kwargs) -> tags_updater.UpdateResult:
            res = tags_updater.UpdateResult(module, out_dir / f'{module}_tags.json')
            tags_updater.write_tag_list(res.path, {'apple': (51, 'general'), 'banana': (9, 'general')})
            res.written, res.count = True, 2
            return res

        def fake_notify(method: str, params: dict) -> None:
            notes.append((method, params))
            if method == 'tags.updateDone':
                done.set()

        with (mock.patch.object(tags_updater, 'update_tag_list', fake_update), mock.patch.object(server, 'notify', fake_notify),
              mock.patch.object(tags_updater, 'tag_source', lambda m, *_a: tags_updater.TagSource(m, 'danbooru_json', 'https://x.test/'))):
            self._hold_core_lock()
            server.m_tags_update({'module': 'zz', 'outDir': self.tags_dir.as_posix()})
            self.assertTrue(done.wait(WAIT), 'tags.update reload waited for _core_lock')
        self.assertTrue(notes[-1][1]['ok'], notes)
        self.assertFalse(TagsDB.is_loaded('zz'))
        self.assertEqual(TagInfo(TagTypes.TAG_TYPE_GENERAL, 51), TagsDB.get_tag_info('zz', 'apple'))
        self.assertIsNone(TagsDB.get_tag_info('zz', 'apricot'))

    def test_concurrent_loads_parse_once(self) -> None:
        calls = []
        parse = tagsdb._parse_tag_list
        gate = threading.Event()

        def slow_parse(path: pathlib.Path):
            calls.append(path)
            gate.wait(WAIT)
            return parse(path)

        with mock.patch.object(tagsdb, '_parse_tag_list', slow_parse):
            threads = [threading.Thread(target=TagsDB.count, args=('zz',), daemon=True) for _ in range(4)]
            for t in threads:
                t.start()
            gate.set()
            for t in threads:
                t.join(WAIT)
        self.assertEqual(1, len(calls))
        self.assertEqual(4, TagsDB.count('zz'))

    def test_reload_during_reads(self) -> None:
        errors: list[BaseException] = []
        stop = threading.Event()

        def reader() -> None:
            try:
                while not stop.is_set():
                    rows = server.m_tags_autocomplete({'module': 'zz', 'prefix': 'ap'})
                    if [r['tag'] for r in rows] != ['apricot', 'apple']:
                        raise AssertionError(rows)
            except BaseException as err:
                errors.append(err)

        readers = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
        for t in readers:
            t.start()
        for _ in range(30):
            TagsDB.locate_default_lists(reload=('zz',))
        stop.set()
        for t in readers:
            t.join(WAIT)
        self.assertEqual([], errors)


class TagsLruTests(_TagsTestBase):
    def test_lru_unload(self) -> None:
        self.assertEqual(2, TAGS_MAX_LOADED)
        for module in ('aa', 'bb', 'cc'):
            self.write_list(module, {f'{module}_tag': (1, 'general')})
        TagsDB.locate_default_lists()
        self.assertEqual(1, TagsDB.count('aa'))
        self.assertEqual(1, TagsDB.count('bb'))
        self.assertEqual(['aa', 'bb'], TagsDB.loaded_modules())
        self.assertIsNotNone(TagsDB.get_tag_info('aa', 'aa_tag'))  # aa becomes the most recently used one
        self.assertEqual(['bb', 'aa'], TagsDB.loaded_modules())
        self.assertEqual(1, TagsDB.count('cc'))
        self.assertEqual(['aa', 'cc'], TagsDB.loaded_modules())
        self.assertFalse(TagsDB.is_loaded('bb'))
        self.assertEqual({'bb_tag': TagInfo(TagTypes.TAG_TYPE_GENERAL, 1)}, TagsDB.as_dict('bb'))  # loaded again on demand
        self.assertEqual(['cc', 'bb'], TagsDB.loaded_modules())

    def test_missing_list_is_not_kept(self) -> None:
        TagsDB.locate_default_lists()
        self.assertEqual(0, TagsDB.count('nope'))
        self.assertIsNone(TagsDB.get_tag_info('nope', 'x'))
        self.assertEqual((0, []), TagsDB.browse('nope', 'a', by_posts=True))
        self.assertEqual([], TagsDB.loaded_modules())


class TagsCacheTests(_TagsTestBase):
    def setUp(self) -> None:
        super().setUp()
        self.min_size = mock.patch.object(TagsDB, 'CacheMinSize', 0)
        self.min_size.start()

    def tearDown(self) -> None:
        self.min_size.stop()
        super().tearDown()

    def _load_from_cache_only(self, module: str) -> dict[str, TagInfo]:
        TagsDB.unload(module)
        with mock.patch.object(tagsdb, '_parse_tag_list', side_effect=AssertionError('parsed instead of using the cache')):
            return TagsDB.as_dict(module)

    def test_cache_written_and_used(self) -> None:
        path = self.write_list('zz', {'apple': (5, 'general'), 'zebra': (2 ** 32 + 7, 'artist'), 'odd': (0, 'species')})
        TagsDB.locate_default_lists()
        parsed = TagsDB.as_dict('zz')
        self.assertEqual(_reference_load(path), parsed)
        self.assertEqual(1, len(self.cache_files))
        self.assertTrue(self.cache_files[0].name.startswith('zz_tags-'))
        self.assertEqual(parsed, self._load_from_cache_only('zz'))

    def test_cache_invalidated_when_source_changes(self) -> None:
        path = self.write_list('zz', {'apple': (5, 'general')})
        TagsDB.locate_default_lists()
        self.assertEqual(5, TagsDB.get_tag_info('zz', 'apple').posts_count)
        st = path.stat()
        path.write_text('{\n"apple": "6 posts (general)"\n}\n', encoding='utf-8')  # same size
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))
        TagsDB.locate_default_lists(reload=('zz',))
        self.assertEqual(6, TagsDB.get_tag_info('zz', 'apple').posts_count)
        self.assertEqual(1, len(self.cache_files))  # one cache per source list, replaced
        self.assertEqual({'apple': TagInfo(TagTypes.TAG_TYPE_GENERAL, 6)}, self._load_from_cache_only('zz'))

    def test_escaped_keys_survive_cache(self) -> None:
        names = ['say_"hi"', 'back\\slash', 'tab\there', 'line sep', 'ünï', '日本語', '\U0001f600_emoji', 'plain', '', 'ctl\x1f']
        lines = [f'{json.dumps(n, ensure_ascii=False)}: "{i + 1} posts (general)",' for i, n in enumerate(names)]
        lines.append('"lone_\\ud800": "3 posts (artist)",')  # only representable escaped
        lines.append('"no_type": "4 posts",')
        path = self.tags_dir / 'zz_tags.json'
        path.write_text('{\n' + '\n'.join(sorted(lines)) + '\n"zz_end": "1 posts (general)"\n}\n', encoding='utf-8')
        TagsDB.locate_default_lists()
        expected = _reference_load(path)
        self.assertEqual(len(names) + 3, len(expected))
        self.assertEqual(expected, TagsDB.as_dict('zz'))
        self.assertEqual(expected, self._load_from_cache_only('zz'))
        for name, info in expected.items():
            self.assertEqual(info, TagsDB.get_tag_info('zz', name), name)
        self.assertEqual(['say_"hi"'], [r['tag'] for r in server.m_tags_autocomplete({'module': 'zz', 'prefix': 'say_"'})])
        self.assertEqual(['lone_\ud800'], [r['tag'] for r in server.m_tags_autocomplete({'module': 'zz', 'prefix': 'lone_'})])

    def test_corrupt_cache_ignored(self) -> None:
        self.write_list('zz', {'apple': (5, 'general'), 'pear': (7, 'general')})
        TagsDB.locate_default_lists()
        expected = TagsDB.as_dict('zz')
        cache = self.cache_files[0]
        raw = bytearray(cache.read_bytes())
        raw[-2] ^= 0x55  # inside the names blob: crc mismatch
        cache.write_bytes(bytes(raw))
        TagsDB.unload('zz')
        self.assertEqual(expected, TagsDB.as_dict('zz'))
        cache.write_bytes(bytes(raw[:len(raw) // 2]))  # truncated
        TagsDB.unload('zz')
        self.assertEqual(expected, TagsDB.as_dict('zz'))
        self.assertEqual(expected, self._load_from_cache_only('zz'))  # rewritten by the last parse

    def test_cache_of_removed_list_is_pruned(self) -> None:
        old = self.write_list('yy', {'x': (1, 'general')})
        self.write_list('zz', {'apple': (5, 'general')})
        TagsDB.locate_default_lists()
        TagsDB.count('yy')
        self.assertEqual(1, len(self.cache_files))
        old.unlink()
        TagsDB.count('zz')  # writing a cache prunes caches of lists that no longer exist
        self.assertEqual(['zz_tags'], [p.name.split('-')[0] for p in self.cache_files])

    def test_small_lists_are_not_cached(self) -> None:
        self.min_size.stop()
        try:
            self.write_list('zz', {'apple': (5, 'general')})
            TagsDB.locate_default_lists()
            self.assertEqual(1, TagsDB.count('zz'))
            self.assertEqual([], self.cache_files)
        finally:
            self.min_size.start()


class TagsParserTests(_TagsTestBase):
    def test_odd_lines_match_reference_loader(self) -> None:
        text = ('﻿{\r\n"zeta": "3 posts (general)",\r\n"alpha": "5 posts (artist)",\r\n"alpha": "6 posts (character)",\r\n'
                '"be\\u0074a": "7 posts (general)",\r\n"beta": "8 posts (metadata)",\r\n  "spaced": "9 posts (lore)" ,\r\n'
                '"neg": "-4 posts (general)",\r\n"huge": "99999999999 posts (general)",\r\n"notype": "2 posts",\r\n'
                '"badtype": "2 posts (gen\\u0065ral)",\r\n"junk"\r\n"x": 5,\r\n}\r\n')
        path = self.tags_dir / 'zz_tags.json'
        path.write_bytes(text.encode('utf-8'))
        TagsDB.locate_default_lists()
        expected = _reference_load(path)
        self.assertEqual(TagInfo(TagTypes.TAG_TYPE_META, 8), expected['beta'])  # the last duplicate wins
        self.assertEqual(TagInfo(TagTypes.TAG_TYPE_GENERAL, 2), expected['badtype'])
        self.assertEqual(expected, TagsDB.as_dict('zz'))
        self.assertEqual(1, TagsDB.browse('zz', 'n', min_posts=-10)[0] - TagsDB.browse('zz', 'n', min_posts=0)[0])


class TagsBrowseTests(_TagsTestBase):
    """browse sort=posts sorted every matching row on every call (paging included)"""

    def setUp(self) -> None:
        super().setUp()
        rng = random.Random(8)
        alphabet = 'abcz_19!(éア'
        types = ['general', 'artist', 'character', 'copyright', 'metadata', 'species']
        self.tags = {''.join(rng.choice(alphabet) for _ in range(rng.randint(1, 6))): (rng.choice([1, 2, 3, 10, 50, 1000, rng.randint(1, 10 ** 6)]),
                                                                                        rng.choice(types))
                     for _ in range(4000)}
        self.write_list('zz', self.tags)
        TagsDB.locate_default_lists()

    def _brute(self, prefix: str, types: list[str], min_posts: int, by_posts: bool) -> list[str]:
        names = sorted(n for n, (c, t) in self.tags.items()
                       if (not ('a' <= n[:1] <= 'z') if prefix == '#' else n.startswith(prefix)) and c >= min_posts and (not types or t in types))
        return sorted(names, key=lambda n: self.tags[n][0], reverse=True) if by_posts else names

    def test_browse_equals_brute_force(self) -> None:
        for prefix in ('', 'a', 'b', 'z', 'ab', '#', 'é', 'nothing'):
            for types in ([], ['general'], ['artist', 'metadata']):
                for min_posts in (0, 3, 1000):
                    for sort in ('posts', 'az'):
                        expected = self._brute(prefix, types, min_posts, sort == 'posts')
                        for offset in (0, 150, 300, max(0, len(expected) - 10)):
                            p = {'module': 'zz', 'prefix': prefix, 'types': types, 'minPosts': min_posts, 'sort': sort, 'offset': offset, 'limit': 150}
                            r = server.m_tags_browse(p)
                            self.assertEqual(len(expected), r['total'], p)
                            self.assertEqual(expected[offset:offset + 150], [x['tag'] for x in r['items']], p)
                            self.assertEqual([self.tags[n][0] for n in expected[offset:offset + 150]], [x['posts'] for x in r['items']], p)

    def test_sorted_result_reused_and_dropped_on_reload(self) -> None:
        p = {'module': 'zz', 'prefix': 'a', 'sort': 'posts', 'limit': 5}
        first = server.m_tags_browse(p)
        with mock.patch('lynxx.tagsdb.sorted', side_effect=AssertionError('sorted again'), create=True):
            self.assertEqual(first, server.m_tags_browse(p))
            self.assertEqual(server.m_tags_browse({**p, 'offset': 5})['total'], first['total'])
        self.write_list('zz', {'a_new': (1, 'general')})
        TagsDB.locate_default_lists(reload=('zz',))
        self.assertEqual({'total': 1, 'items': [{'tag': 'a_new', 'posts': 1, 'type': 'general'}]}, server.m_tags_browse(p))

    def test_autocomplete_equals_brute_force(self) -> None:
        for prefix in ('a', 'ab', 'z_', '1', 'ア', 'nothing'):
            expected = self._brute(prefix, [], 0, True)
            rows = server.m_tags_autocomplete({'module': 'zz', 'prefix': prefix, 'limit': 12})
            self.assertEqual(expected[:12], [r['tag'] for r in rows], prefix)
            self.assertEqual([(n, self.tags[n][1]) for n in expected[:12]], [(r['tag'], r['type']) for r in rows])
            self.assertEqual([n[len(prefix):] for n in expected[:7]] if len(prefix) >= 2 else [], [s for s, _ in TagsDB.autocomplete_tag('zz', prefix)])
        with mock.patch.object(server, 'AUTOCOMPLETE_SCAN_MAX', 10):
            alphabetical = self._brute('a', [], 0, False)[:10]
            expected = sorted(alphabetical, key=lambda n: self.tags[n][0], reverse=True)[:12]
            self.assertEqual(expected, [r['tag'] for r in server.m_tags_autocomplete({'module': 'zz', 'prefix': 'a'})])

#
#
#########################################
