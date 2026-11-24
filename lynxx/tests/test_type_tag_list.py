# coding=UTF-8
"""
Lynxx: tests of tools/type_tag_list.py (types the untyped rows of a tag list from typed lists by exact name), offline.
- only rows without a type (or 'unknown') get one, the first reference that types a name with an assigned type wins, --types,
  --min-ref-posts, --max-count-ratio, --conflicts, escaped names, CRLF / no final newline, every other line byte for byte, the core parser loads the output
- --retype: rows of a given type get the newer type of a same-site reference, everything else as without it
- --evaluate: precision / recall counts per type, the >= 20 posts cut, wrong predictions
- the CLI (in process and as a script)
- the committed 2tags/rx_tags.json and xb_tags.json type artists (retyped 2026-11-21), their characters stay
Run: python -m unittest lynxx.tests.test_type_tag_list
"""
#########################################
#
#

import contextlib
import importlib.util
import io
import pathlib
import subprocess
import sys
import tempfile
from unittest import TestCase

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx import tags_updater  # noqa: E402
from lynxx.tags import TagInfo, TagTypes  # noqa: E402
from lynxx.tagsdb import _parse_lines_exact, _parse_tag_list  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]
TOOL = ROOT / 'tools' / 'type_tag_list.py'


def _load_tool():
    spec = importlib.util.spec_from_file_location('type_tag_list_tool', TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ttl = _load_tool()

LIST = (
    '{\n'
    '"!karryn!": "12 posts",\n'
    '"\\"l\\"": "10 posts",\n'
    '"alice": "5 posts (unknown)",\n'
    '"bob": "7 posts (general)",\n'
    '"carol": "3 posts",\n'
    '"dave": "4 posts",\n'
    '"eve": "2 posts (weird)",\n'
    '"meta_tag": "9 posts",\n'
    '"zed": "1 posts"\n'
    '}\n'
)
'''untyped list: not in the refs, escaped name, unknown, typed already, general in the first ref, few posts in the first ref,
an unrecognized type, metadata, 'tag' in the second ref'''
REF1 = (
    '{\n'
    '"\\"l\\"": "100 posts (character)",\n'
    '"alice": "50 posts (copyright)",\n'
    '"bob": "60 posts (artist)",\n'
    '"carol": "70 posts (general)",\n'
    '"dave": "1 posts (character)",\n'
    '"eve": "5 posts (artist)",\n'
    '"meta_tag": "5 posts (metadata)",\n'
    '"zed": "5 posts"\n'
    '}\n'
)
REF2 = (
    '{\n'
    '"carol": "8 posts (artist)",\n'
    '"dave": "9 posts (copyright)",\n'
    '"zed": "3 posts (tag)",\n'
    '"alice": "1 posts (artist)"\n'
    '}\n'
)


class _Tmp(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = pathlib.Path(self._tmp.name)
        self.list = self._write('list.json', LIST)
        self.ref1 = self._write('ref1.json', REF1)
        self.ref2 = self._write('ref2.json', REF2)
        self.out = self.tmp / 'out.json'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, name: str, text: str | bytes) -> pathlib.Path:
        path = self.tmp / name
        path.write_bytes(text if isinstance(text, bytes) else text.encode())
        return path

    def _type(self, *, refs=None, types=ttl.DEFAULT_TYPES, min_ref_posts: int = 0, src: pathlib.Path | None = None):
        src = src or self.list
        loaded = ttl.load_references(refs or [self.ref1, self.ref2], ttl.untyped_names(src), min_ref_posts, types)
        return ttl.type_list(src, self.out, loaded.types, types)

    def _typed(self) -> dict[str, tuple[int, str]]:
        return tags_updater.read_tag_list(self.out, untyped='')


class TypingTests(_Tmp):
    def test_default_types_first_ref_wins(self) -> None:
        res = self._type()
        self.assertEqual({
            '!karryn!': (12, ''),  # no reference knows it
            '"l"': (10, 'character'),  # escaped name
            'alice': (5, 'copyright'),  # 'unknown' is untyped; the first reference wins over the second one's artist
            'bob': (7, 'general'),  # typed already: kept although the reference says artist
            'carol': (3, 'artist'),  # 'general' (not assigned) in the first reference: the second one answers
            'dave': (4, 'character'),
            'eve': (2, 'weird'),  # an unrecognized type is kept as it is
            'meta_tag': (9, ''),  # metadata is not assigned by default
            'zed': (1, ''),  # untyped in the first reference, 'tag' (general) in the second
        }, self._typed())
        self.assertEqual((9, 2, 0), (res.rows, res.typed_before, res.other))
        self.assertEqual({'character': 2, 'copyright': 1, 'artist': 1}, dict(res.typed))
        self.assertEqual(3, res.left)

    def test_types_and_min_ref_posts(self) -> None:
        self._type(types=('general', 'metadata'))
        typed = self._typed()
        self.assertEqual(('general', 'metadata', 'general', 'unknown'),  # alice: copyright / artist only, kept '(unknown)'
                         (typed['carol'][1], typed['meta_tag'][1], typed['zed'][1], typed['alice'][1]))
        self._type(min_ref_posts=2)
        typed = self._typed()
        self.assertEqual('copyright', typed['dave'][1])  # 1 post in the first reference: ignored, the second one answers
        self.assertEqual('copyright', typed['alice'][1])
        self._type(refs=[self.ref2, self.ref1])
        self.assertEqual(('artist', 'copyright'), (self._typed()['alice'][1], self._typed()['dave'][1]))

    def test_untouched_lines_are_byte_identical(self) -> None:
        self._type()
        src, out = LIST.encode().splitlines(keepends=True), self.out.read_bytes().splitlines(keepends=True)
        self.assertEqual(len(src), len(out))
        changed = {
            b'"\\"l\\"": "10 posts",\n': b'"\\"l\\"": "10 posts (character)",\n',
            b'"alice": "5 posts (unknown)",\n': b'"alice": "5 posts (copyright)",\n',
            b'"carol": "3 posts",\n': b'"carol": "3 posts (artist)",\n',
            b'"dave": "4 posts",\n': b'"dave": "4 posts (character)",\n',
        }
        for a, b in zip(src, out, strict=True):
            self.assertEqual(changed.get(a, a), b)
        # nothing to type: the copy is the same file
        self._type(refs=[self._write('none.json', '{\n"other": "3 posts (artist)"\n}\n')])
        self.assertEqual(LIST.encode(), self.out.read_bytes())
        self.assertEqual(['list.json', 'none.json', 'out.json', 'ref1.json', 'ref2.json'], sorted(p.name for p in self.tmp.iterdir()))

    def test_core_parser_loads_the_output(self) -> None:
        self._type()
        lst = _parse_tag_list(self.out)
        self.assertEqual(9, len(lst))
        expect = {'"l"': TagInfo(TagTypes.TAG_TYPE_CHARACTER, 10), 'carol': TagInfo(TagTypes.TAG_TYPE_ARTIST, 3),
                  'alice': TagInfo(TagTypes.TAG_TYPE_COPYRIGHT, 5), 'bob': TagInfo(TagTypes.TAG_TYPE_GENERAL, 7),
                  '!karryn!': TagInfo(TagTypes.TAG_TYPE_UNKNOWN, 12), 'eve': TagInfo(TagTypes.TAG_TYPE_UNKNOWN, 2)}
        for name, info in expect.items():
            self.assertEqual(info, lst.info(lst.find(name)), name)
        with open(self.out, encoding='utf-8') as f:
            exact = _parse_lines_exact(f)  # the line by line loader reads the same
        self.assertEqual({lst.name(i): (lst.posts[i], lst.types[i]) for i in range(len(lst))}, exact)

    def test_crlf_and_no_final_newline(self) -> None:
        src = self._write('crlf.json', b'{\r\n"x": "2 posts",\r\n"y y": "3 posts" \r\n"z": "4 posts"')
        ref = self._write('crlf_ref.json', '{\n"x": "1 posts (artist)",\n"z": "1 posts (character)",\n"y y": "1 posts (copyright)"\n}\n')
        res = self._type(refs=[ref], src=src)
        self.assertEqual(b'{\r\n"x": "2 posts (artist)",\r\n"y y": "3 posts (copyright)" \r\n"z": "4 posts (character)"',
                         self.out.read_bytes())
        self.assertEqual(3, sum(res.typed.values()))

    def test_references_keep_only_wanted_names_last_line_wins(self) -> None:
        ref = self._write('dup.json', '{\n"carol": "5 posts (character)",\n"carol": "6 posts (artist)",\n"other": "9 posts (artist)"\n}\n')
        refs = ttl.load_references([ref, self.ref1], {'carol', 'nobody'}, 0, ttl.DEFAULT_TYPES)
        self.assertEqual({'carol': 'artist'}, refs.types)
        self.assertEqual([1, 0], refs.won)
        # None: any type answers, the first reference's 'general' keeps a later reference's artist out
        self.assertEqual({'carol': 'general'}, ttl.load_references([self.ref1, self.ref2], {'carol'}, 0, None).types)

    def test_a_far_rarer_reference_row_gives_no_answer(self) -> None:
        # 'porky' is used 20,000 times more on the list than on the first reference: another thing of the same name; the second
        # reference may still answer, a gap at the limit still answers, 0 is no limit, a plain set of names has no posts to compare
        src = self._write('ratio.json', '{\n"porky": "200000 posts",\n"near": "100000 posts",\n"lone": "50000 posts"\n}\n')
        r1 = self._write('ratio_r1.json', '{\n"porky": "10 posts (artist)",\n"near": "10 posts (character)",\n"lone": "1 posts (artist)"\n}\n')
        r2 = self._write('ratio_r2.json', '{\n"porky": "90000 posts (copyright)"\n}\n')
        wanted = ttl.untyped_names(src)
        self.assertEqual({'porky': 200000, 'near': 100000, 'lone': 50000}, wanted)
        refs = ttl.load_references([r1, r2], wanted, 0, ttl.DEFAULT_TYPES, ttl.DEFAULT_MAX_COUNT_RATIO)
        self.assertEqual({'porky': 'copyright', 'near': 'character'}, refs.types)
        self.assertEqual([1, 1], refs.won)
        self.assertEqual({'porky': 'artist', 'near': 'character', 'lone': 'artist'},
                         ttl.load_references([r1, r2], wanted, 0, ttl.DEFAULT_TYPES, 0).types)
        self.assertEqual({'porky': 'artist', 'near': 'character', 'lone': 'artist'},
                         ttl.load_references([r1, r2], set(wanted), 0, ttl.DEFAULT_TYPES, ttl.DEFAULT_MAX_COUNT_RATIO).types)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, ttl.main(['--list', str(src), '--ref', str(r1), '--out', str(self.out)]))
        self.assertIn('"porky": "200000 posts",', self.out.read_text(encoding='utf-8'))
        self.assertIn('"lone": "50000 posts"\n', self.out.read_text(encoding='utf-8'))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, ttl.main(['--list', str(src), '--ref', str(r1), '--out', str(self.out), '--max-count-ratio', '0']))
        self.assertIn('"porky": "200000 posts (artist)",', self.out.read_text(encoding='utf-8'))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ttl.main(['--evaluate', str(src), '--ref', str(r1), '--max-count-ratio', '-1'])

    def test_conflicting_references_leave_the_name_untyped(self) -> None:
        # 'skip': a name two references type differently stays untyped; one that only one reference types, or that both type the
        # same, is typed; rows of a type not assigned, or stopped by --max-count-ratio, are no answer, so no conflict either
        src = self._write('c.json', '{\n"chi": "900 posts",\n"one": "5 posts",\n"same": "7 posts",\n"gen": "8 posts",\n'
                                    '"far": "90000 posts"\n}\n')
        r1 = self._write('c1.json', '{\n"chi": "3 posts (artist)",\n"one": "1 posts (artist)",\n"same": "2 posts (copyright)",\n'
                                    '"gen": "4 posts (general)",\n"far": "1 posts (artist)"\n}\n')
        r2 = self._write('c2.json', '{\n"chi": "50 posts (character)",\n"same": "9 posts (copyright)",\n"gen": "5 posts (character)",\n'
                                    '"far": "80000 posts (copyright)"\n}\n')
        wanted = ttl.untyped_names(src)
        refs = ttl.load_references([r1, r2], wanted, 0, ttl.DEFAULT_TYPES, ttl.DEFAULT_MAX_COUNT_RATIO, 'skip')
        self.assertEqual({'one': 'artist', 'same': 'copyright', 'gen': 'character', 'far': 'copyright'}, refs.types)
        self.assertEqual(1, refs.conflicts)
        first = ttl.load_references([r1, r2], wanted, 0, ttl.DEFAULT_TYPES, ttl.DEFAULT_MAX_COUNT_RATIO, 'first')
        self.assertEqual(('artist', 0), (first.types['chi'], first.conflicts))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(0, ttl.main(['--list', str(src), '--ref', str(r1), '--ref', str(r2), '--conflicts', 'skip',
                                          '--out', str(self.out)]))
        self.assertIn('disputed, left untyped: 1', out.getvalue())
        self.assertIn('"chi": "900 posts",', self.out.read_text(encoding='utf-8'))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, ttl.main(['--evaluate', str(r2), '--ref', str(r1), '--conflicts', 'skip']))

    def test_out_must_be_a_file_and_only_with_list(self) -> None:
        src = self._write('o.json', '{\n"a": "1 posts"\n}\n')
        for argv in (['--list', str(src), '--ref', str(src), '--out', str(self.tmp)],
                     ['--evaluate', str(src), '--ref', str(src), '--out', str(self.out)]):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
                ttl.main(argv)
            self.assertIn('--out', err.getvalue())

    def test_lines_that_are_not_rows_are_kept(self) -> None:
        text = '{\n"a": "1 posts",\n  junk line\n"b": 3,\n\n"c": "2 posts",\n}\n'
        src = self._write('junk.json', text)
        ref = self._write('junk_ref.json', '{\n"a": "1 posts (artist)",\n"b": "1 posts (artist)",\n"c": "1 posts (artist)"\n}\n')
        res = self._type(refs=[ref], src=src)
        self.assertEqual(text.replace('posts",', 'posts (artist)",'), self.out.read_text(encoding='utf-8'))
        self.assertEqual((2, 2), (res.rows, res.other))

    def test_retype_rows_of_a_type_from_a_same_site_reference(self) -> None:
        # a list that typed its artists 'general' and the same site's newer list: --retype general gives the general rows ('tag' is
        # general too) the newer type when it is one of --types and another one; other typed rows, an unrecognized type, a general
        # row the reference does not know or calls general, and a row stopped by --max-count-ratio stay; untyped rows are typed as
        # without --retype. Changed lines differ only by their type
        text = ('{\n"art": "50 posts (general)",\n"art_tag": "6 posts (tag)",\n"chr": "7 posts (character)",\n"gen": "9 posts (general)",\n'
                '"meta": "4 posts (general)",\n"new": "3 posts",\n"odd": "2 posts (weird)",\n"rare": "900000 posts (general)",\n'
                '"same": "8 posts (general)",\n"solo": "1 posts (general)",\n"unk": "5 posts (unknown)"\n}\n')
        src = self._write('same_site.json', text)
        ref = self._write('same_site_new.json', '{\n"art": "52 posts (artist)",\n"art_tag": "6 posts (artist)",\n'
                                                '"chr": "7 posts (artist)",\n"gen": "9 posts (general)",\n"meta": "4 posts (metadata)",\n'
                                                '"new": "3 posts (copyright)",\n"odd": "2 posts (artist)",\n"rare": "5 posts (artist)",\n'
                                                '"same": "8 posts (character)",\n"unk": "5 posts (character)"\n}\n')
        types = ('artist', 'character', 'copyright', 'metadata')
        wanted = ttl.untyped_names(src, ('general',))
        self.assertEqual({'art': 50, 'art_tag': 6, 'gen': 9, 'meta': 4, 'new': 3, 'rare': 900000, 'same': 8, 'solo': 1, 'unk': 5}, wanted)
        self.assertEqual({'new': 3, 'unk': 5}, ttl.untyped_names(src))  # the default: untyped rows only
        refs = ttl.load_references([ref], wanted, 0, types, ttl.DEFAULT_MAX_COUNT_RATIO)
        res = ttl.type_list(src, self.out, refs.types, types, ('general',))
        expect = text
        for name, before, now in (('art', 'general', 'artist'), ('art_tag', 'tag', 'artist'), ('meta', 'general', 'metadata'),
                                  ('same', 'general', 'character')):
            expect = expect.replace(f'"{name}": "{wanted[name]:d} posts ({before})"', f'"{name}": "{wanted[name]:d} posts ({now})"')
        expect = expect.replace('"new": "3 posts"', '"new": "3 posts (copyright)"').replace('(unknown)', '(character)')
        self.assertEqual(expect, self.out.read_text(encoding='utf-8'))
        self.assertEqual({('general', 'artist'): 2, ('general', 'metadata'): 1, ('general', 'character'): 1}, dict(res.retyped))
        self.assertEqual(({'copyright': 1, 'character': 1}, 0), (dict(res.typed), res.left))
        self.assertEqual((11, 5), (res.rows, res.typed_before))  # chr, gen, odd, rare, solo
        lst = _parse_tag_list(self.out)
        self.assertEqual((TagInfo(TagTypes.TAG_TYPE_ARTIST, 6), TagInfo(TagTypes.TAG_TYPE_GENERAL, 1)),
                         (lst.info(lst.find('art_tag')), lst.info(lst.find('solo'))))
        # without retype (the default) the general rows are kept as they are
        res = ttl.type_list(src, self.out, refs.types, types)
        self.assertEqual(text.replace('"new": "3 posts"', '"new": "3 posts (copyright)"').replace('(unknown)', '(character)'),
                         self.out.read_text(encoding='utf-8'))
        self.assertEqual(({}, 9), (dict(res.retyped), res.typed_before))

    def test_bom_and_escaped_values(self) -> None:
        src = self._write('bom.json', '\ufeff{\n"a": "1 posts",\n"b": "2 posts",\n"c": "3 posts"\n}\n')
        ref = self._write('esc_ref.json', '{\n"a": "5 posts (art\\u0069st)",\n"b":"5 posts (artist)",\n"c": "5 posts (character)"\n}\n')
        res = self._type(refs=[ref], src=src)  # 'b' is not in the core's format there: no answer
        self.assertEqual('\ufeff{\n"a": "1 posts (artist)",\n"b": "2 posts",\n"c": "3 posts (character)"\n}\n',
                         self.out.read_text(encoding='utf-8'))
        self.assertEqual((3, 0, 1), (res.rows, res.other, res.left))


class EvaluateTests(_Tmp):
    def test_precision_recall_counts(self) -> None:
        truth = self._write('truth.json', '{\n"a": "30 posts (artist)",\n"b": "30 posts (general)",\n"c": "5 posts (character)",\n'
                                          '"d": "25 posts (character)",\n"e": "1 posts",\n"f": "40 posts (copyright)"\n}\n')
        ref = self._write('eref.json', '{\n"a": "1 posts (artist)",\n"b": "1 posts (artist)",\n"c": "1 posts (character)",\n'
                                       '"d": "1 posts (copyright)",\n"e": "1 posts (character)",\n"f": "1 posts (general)"\n}\n')
        (whole, cut), refs = ttl.evaluate(truth, [ref], ttl.DEFAULT_TYPES, 0, (0, 20))
        self.assertEqual(5, whole.rows)  # 'e' has no type to compare with
        self.assertEqual({'artist': 2, 'character': 1, 'copyright': 1}, dict(whole.predicted))
        self.assertEqual({'artist': 1, 'character': 1}, dict(whole.correct))
        self.assertEqual({'artist': 1, 'general': 1, 'character': 2, 'copyright': 1}, dict(whole.truth))
        self.assertEqual({('artist', 'general'): 1, ('copyright', 'character'): 1}, dict(whole.confusions))
        self.assertEqual(4, cut.rows)
        self.assertEqual(({'artist': 2, 'copyright': 1}, {'artist': 1}), (dict(cut.predicted), dict(cut.correct)))
        self.assertEqual(4, len(refs.types))  # 'e' is not compared, 'f' is general there: no answer
        table = ttl.format_evaluation([whole, cut], (0, 20), ttl.DEFAULT_TYPES)
        self.assertIn('artist     |         2  50.0%       1 100.0% |         2  50.0%       1 100.0%', table)
        self.assertIn('overall    |         4  50.0%       4  50.0% |         3  33.3%       3  33.3%', table)
        self.assertIn('wrong (all rows): artist for general 1, copyright for character 1', table)


class CliTests(_Tmp):
    def _main(self, *argv: str) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(0, ttl.main(list(argv)))
        return buf.getvalue()

    def test_typing_summary(self) -> None:
        text = self._main('--list', str(self.list), '--ref', str(self.ref1), '--ref', str(self.ref2), '--out', str(self.out))
        self.assertIn('list.json: 9 rows, 2 typed already (kept), 7 untyped', text)
        self.assertIn('names typed per ref 3, 1', text)
        self.assertIn('typed now: character 2, copyright 1, artist 1 (total 4)', text)
        self.assertIn('left untyped: 3', text)
        self.assertEqual('artist', self._typed()['carol'][1])
        text = self._main('--list', str(self.list), '--ref', str(self.ref1), '--out', str(self.out), '--types', 'tag,Metadata',
                          '--min-ref-posts', '6')
        self.assertIn('typed now: general 1 (total 1)', text)  # carol; meta_tag has 5 posts in the reference

    def test_retype_summary(self) -> None:
        # bob is general in the list, an artist in the first reference: retyped; carol (general there) gets the second one's artist
        text = self._main('--list', str(self.list), '--ref', str(self.ref1), '--ref', str(self.ref2), '--out', str(self.out),
                          '--retype', 'Tag')
        self.assertIn('list.json: 9 rows, 1 typed already (kept), 1 retyped (general), 7 untyped', text)
        self.assertIn('names typed per ref 4, 1', text)
        self.assertIn('retyped: general -> artist 1 (total 1)', text)
        self.assertEqual((7, 'artist'), self._typed()['bob'])
        text = self._main('--list', str(self.list), '--ref', str(self.ref2), '--out', str(self.out), '--retype', 'general,character')
        self.assertIn('retyped: none (total 0)', text)
        self.assertNotIn('retyped', self._main('--list', str(self.list), '--ref', str(self.ref1), '--out', str(self.out)))
        self.assertEqual((7, 'general'), self._typed()['bob'])

    def test_bad_arguments(self) -> None:
        for argv in (['--list', str(self.list), '--ref', str(self.ref1)],  # no --out
                     ['--list', str(self.list), '--ref', str(self.ref1), '--out', str(self.out), '--retype', 'general,nope'],
                     ['--evaluate', str(self.ref1), '--ref', str(self.ref2), '--retype', 'general'],
                     ['--list', str(self.list), '--ref', str(self.ref1), '--out', str(self.out), '--types', 'artist,nope'],
                     ['--list', str(self.list), '--evaluate', str(self.ref1), '--ref', str(self.ref1)],
                     ['--list', str(self.tmp / 'missing.json'), '--ref', str(self.ref1), '--out', str(self.out)],
                     ['--list', str(self.list), '--ref', str(self.ref1), '--out', str(self.tmp / 'no' / 'out.json')]):
            with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
                ttl.main(argv)
        self.assertFalse(self.out.exists())

    def test_script_evaluate(self) -> None:
        r = subprocess.run([sys.executable, str(TOOL), '--evaluate', str(self.ref1), '--ref', str(self.ref2)],
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(0, r.returncode, r.stderr)
        self.assertIn('ref1.json: types hidden, inferred from ref2.json', r.stdout)
        self.assertIn('overall', r.stdout)


class CommittedListsTests(TestCase):
    def test_rule34_and_xbooru_lists_type_artists(self) -> None:
        # 2tags/rx_tags.json and xb_tags.json typed every artist 'general'; since 2026-11-21 their general rows that the same site's
        # newer list calls artists are artists (--types artist --retype general), nothing else changed: 'patreon_username' (metadata
        # on Rule34 now) stays general, and 'chi-chi' (an artist on Rule34 now, a character elsewhere) is still the character that
        # makes the third reference of the Paheal / Rule34Hentai typing dispute that name
        expect = {'rx_tags.json': {'palcomix': 'artist', 'chi-chi': 'character', 'patreon_username': 'general', 'big_breasts': 'general'},
                  'xb_tags.json': {'palcomix': 'artist', 'shadman': 'artist', 'midna': 'character'}}
        for name, names in expect.items():
            path = ROOT / '2tags' / name
            if not path.is_file():
                self.skipTest(f'no 2tags/{name} (a source archive without the lists)')
            with self.subTest(name):
                found = {n: t for n, _, t in ttl.iter_rows(path) if n in names}
                self.assertEqual(names, found)

#
#
#########################################
