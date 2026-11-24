#!/usr/bin/env python3
# coding=UTF-8
"""
Lynxx: types the untyped rows of a tag list from other, typed tag lists by exact name (offline, no network)
A list whose site gives no tag types ('"name": "N posts",') shows nothing under the type chips of the app's A-Z browser. A tag of the
same name in a typed list (usually a site of the same audience) is mostly of the same type: this tool copies that type. Only rows
without a type (or '(unknown)') get one; rows that have a type, every posts count, the order and the exact bytes of every other line
stay as they are. The first --ref that gives a name one of the assigned types wins: a reference row of another type (or untyped, or
under --min-ref-posts, or used --max-count-ratio times less than the list's row) gives no answer, so a later reference may still
type that name.
--retype TYPES: rows of these types may get another type of --types too, the rest stays as above. Only for a newer list of the
same site as the reference (another site's type would replace the site's own): a list whose source typed its artists 'general'.
Usage:
  python tools/type_tag_list.py --list <untyped list> --ref <typed list> [--ref <second> ...] --out <file>
                                [--types artist,character,copyright] [--min-ref-posts N] [--max-count-ratio R]
                                [--conflicts first|skip] [--retype general]
  python tools/type_tag_list.py --evaluate <typed list> --ref <typed list> [...] [--types ...] [--min-ref-posts N]
                                [--max-count-ratio R]
    hides the types of that list, infers them from the references and prints precision / recall per type (all rows, >= 20 posts)
  python tools/type_tag_list.py --list 2tags/rx_tags.json --ref <newer Rule34 list, artist types> --out 2tags/rx_tags.json
                                --types artist --retype general
    how 2tags/rx_tags.json and xb_tags.json got their artist types on 2026-11-21, each with the same site's list the tag updater
    wrote as the only --ref: 186,454 and 53,988 general rows became artists; posts, order and every other line stayed (putting
    'general' back gives the old files). Artists only: the same lists would also turn general rows into characters, series or
    metadata (5,753 on Rule34, 217 on Xbooru: 'girlfriend' a series, 'side_view' metadata; e621 and Xbooru disagree for most of
    them), and they call 1,796 of the old Rule34 characters artists ('night_fury', 'chi-chi'; e621 calls 76% of those it knows
    characters): rows typed character / copyright / metadata are kept, as --retype general only retypes general rows.

The measurements below were made before that, with the committed Rule34 and Xbooru lists typing their artists 'general'. With the
retyped 2tags/rx_tags.json in place of the old one (then db), >= 20 posts: Xbooru artists 97.4/42.0 -> 95.2/87.6, overall
96.7/69.8 -> 96.2/83.9; Hypnohub artists 97.1/20.2 -> 98.1/50.8, overall 97.5/51.2 -> 97.7/61.0. As the third reference of the 7bf
recipe (--conflicts skip, below) either file gives byte for byte the same rp_tags.json and rn_tags.json.
Measured 2026-11-21 (--evaluate, --ref 2tags/rx_tags.json --ref 2tags/db_tags.json, default types): precision / recall in %, all
rows | rows with >= 20 posts. Most low count misses are the evaluated site's own gaps (a rarely used tag keeps the default 'general'
there: tenshinhan, ushiwakamaru_(swimsuit_assassin)_(fate) on Xbooru), so the >= 20 posts column is the better estimate.
  evaluated list          artist     character  copyright  overall    | artist     character  copyright  overall
  Xbooru (artist types)   72.3/31.4  79.4/65.5  77.3/62.9  77.2/51.2  | 97.4/42.0  96.8/84.7  96.1/76.9  96.7/69.8
  Rule34.xyz              100/38.5   99.5/98.0  99.5/98.3  99.6/67.9  | 100/38.6   99.5/99.0  99.5/99.0  99.6/68.2
  Rule34.world            100/41.6   99.2/97.5  99.3/97.9  99.4/71.0  | 100/42.1   99.2/98.9  99.3/98.8  99.4/71.8
  Hypnohub                98.3/24.5  96.7/47.3  94.0/61.5  96.8/38.0  | 97.1/20.2  98.4/63.5  95.4/73.4  97.5/51.2
  Zerochan (>= 20 only)   97.2/41.1  93.9/69.1  85.0/61.2  93.1/57.9  |
  2tags/xb_tags.json gave the same character / copyright figures (96.9/84.9, 96.1/77.0 >= 20) but typed its artists 'general' then,
  as 2tags/rx_tags.json did. TBIB 53.8/87.6 and Safebooru 30.8/69.7 (58.2/85.9 >= 20) type most of their artists, characters and series
  'general' (blue_archive, hololive on TBIB, mochi_au_lait on Safebooru), Realbooru (photos) shares few names: 62.0/10.7 (86.6/18.5).
Decisions from these numbers:
- default types artist, character, copyright. 'general' is wrong wherever the first reference calls its artists 'general' (the
  committed Rule34 list then: 10.7% precise on Rule34.xyz, 12.3% Rule34.world, 13.6% Hypnohub, 62.6% Xbooru); with a Rule34 list that has
  artist types it is 86-99% (Hypnohub 44%), --types adds it then. 'metadata' is 5-65% precise everywhere: never by default.
- a second reference helps: Danbooru after Rule34 brings the artists the committed Rule34 list lacked, overall >= 20 posts recall
  55.1 -> 69.8 (Xbooru), 48.4 -> 68.2 (Rule34.xyz), 43.4 -> 51.2 (Hypnohub), 34.2 -> 57.9 (Zerochan) for at most 0.2 points of
  precision. Rule34 first: Danbooru first is 0-0.3 points less precise.
- a reference row of a type not assigned gives no answer: if the Rule34 'general' of an artist kept Danbooru out, Xbooru artist
  recall (>= 20 posts) would fall from 42.0% to 1.4%, Rule34.xyz from 38.6% to 0.6%.
- --min-ref-posts defaults to 0: 2 or 5 changes precision by at most 0.6 points on the lists above and loses recall (Rule34.xyz
  artists 38.5 -> 34.3).
- a Rule34 list with artist types (as the tag updater writes them now) as the first reference: Xbooru 95.2/85.1, Rule34.xyz
  100/98.7 (>= 20 posts).
- --max-count-ratio defaults to 10000: a name used 10,000 times more on the list than on the reference is another thing of the
  same name. On 2tags/rp_tags.json with that Rule34 list and 2tags/db_tags.json it stops one row ('porkyman', Paheal's name for
  Pokemon, 478,678 posts, a 45 post artist on Rule34) and none on 2tags/rn_tags.json; at 1000 it would stop 31 rows on rp, most of
  them right ('spyro_(series)' 10,884 posts vs 10, 'hu_tao' 4,942 vs 2: Rule34 rarely uses them), at 100 694 rows.
- --conflicts skip with a third reference that names characters as characters (the committed 2tags/rx_tags.json): a name two
  references type differently stays untyped ('chi-chi', 'sona', 'mallow': artists on Rule34, characters elsewhere). Against two
  references and 'first' (>= 20 posts): Xbooru 95.2 -> 96.0% precise (recall 85.1 -> 84.9), Hypnohub 97.2 -> 97.6, Zerochan
  91.9 -> 92.3, Rule34.xyz / Rule34.world 99.7-100 unchanged. 2tags/rp_tags.json and rn_tags.json were typed so (1,317 and 566
  names left untyped), docs/dev_log.md 7bf.
"""
import argparse
import json
import os
import pathlib
import re
import sys
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from typing import NamedTuple

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, ROOT.as_posix())

from lynxx.tags import TAG_NAMES_BY_TYPE, TAG_TYPES_BY_NAME, TagTypes  # noqa: E402

__all__ = ('DEFAULT_MAX_COUNT_RATIO', 'DEFAULT_TYPES', 'EVAL_MIN_POSTS', 'Evaluation', 'References', 'TypingResult', 'evaluate', 'load_references', 'main',
           'type_list')

DEFAULT_TYPES = ('artist', 'character', 'copyright')
'''types assigned unless --types says otherwise (see the measurements in the module docstring)'''
EVAL_MIN_POSTS = 20
'''--evaluate: the second table counts only rows with at least this many posts'''
DEFAULT_MAX_COUNT_RATIO = 10000
'''a reference row answers only if the list's row has at most this many times its posts (0: no limit), see the module docstring'''

re_row = re.compile(rb'(?P<pre>(?:\xef\xbb\xbf)?[ \t]*)(?P<key>"(?:[^"\\\r\n]|\\[^\r\n])*"): "(?P<posts>[0-9]+) posts'
                    rb'(?: \((?P<type>[^()\\\r\n]*)\))?"(?P<post>,?[ \t]*(?:\r?\n)?)')
'''one '"name": "N posts (type)",' line of the bundled list format (lynxx/tags_updater.py write_tag_list), the type is optional.
Escaped names (\\" or \\\\ inside) are read as json, as TagsDB._parse_line does'''
re_value = re.compile(r'([0-9]+) posts(?: \(([^()]*)\))?')
_UNKNOWN = TagTypes.TAG_TYPE_UNKNOWN
_CANON: dict[str, str] = {str(text): str(TAG_NAMES_BY_TYPE[t]) for text, t in TAG_TYPES_BY_NAME.items() if t != _UNKNOWN}
'''type text of a list line -> canonical type name ('tag' -> 'general'); '', 'unknown' and unrecognized texts are absent'''
_CANON_B: dict[bytes, str] = {k.encode(): v for k, v in _CANON.items()}
TYPE_NAMES = tuple(dict.fromkeys(_CANON.values()))
TYPE_ORDER = ('artist', 'character', 'copyright', 'general', 'metadata')
'''display order of the evaluation table (other known types follow)'''
_UNTYPED = (None, b'', b'unknown')
'''type texts of the rows this tool may type'''


def _name(key: bytes) -> str:
    """The tag name of a quoted key (json for escaped ones, as the core reads them). UnicodeDecodeError / ValueError if unreadable"""
    text = key.decode('utf-8')
    return json.loads(text) if '\\' in text else text[1:-1]


def iter_rows(path: pathlib.Path) -> Iterator[tuple[str, int, str | None]]:
    """(name, posts, canonical type or None) of every tag line of a list, streamed. Lines the core cannot read either are skipped,
    a line with an escape the regex does not take (in the value) is read as json, as TagsDB._parse_line does"""
    with open(path, 'rb') as f:
        for raw in f:
            if m := re_row.fullmatch(raw):
                try:
                    name = _name(m['key'])
                except ValueError:
                    continue
                yield name, int(m['posts']), _CANON_B.get(m['type'] or b'')
                continue
            s = raw.decode('utf-8', 'replace').strip().lstrip('\ufeff').removesuffix(',')
            if '\\' not in s:  # lists are written in the format above (tags_updater.write_tag_list): other lines are junk
                continue
            try:
                ((name, value),) = json.loads('{' + s + '}').items()
            except (ValueError, AttributeError):
                continue
            if isinstance(value, str) and (v := re_value.fullmatch(value)):
                yield name, int(v[1]), _CANON.get(v[2] or '')


class References(NamedTuple):
    types: dict[str, str]
    '''{name: canonical type}'''
    won: list[int]
    '''names each reference typed (same order as the paths)'''
    conflicts: int = 0
    '''conflicts 'skip': names left untyped because two references answered different types'''


def load_references(paths: Iterable[pathlib.Path], wanted: Mapping[str, int] | set[str], min_posts: int = 0,
                    types: Iterable[str] | None = None, max_ratio: int = 0, conflicts: str = 'first') -> References:
    """Types of the wanted names from typed lists, the first list that types a name wins. Within one list the last line of a name
    counts (as when the app loads it). Rows without a type, or with fewer than min_posts posts, give no answer.
    wanted: names, or {name: posts in the list being typed}; with the posts and max_ratio > 0 a reference row whose posts times
    max_ratio is below the list's posts gives no answer either (another thing of the same name).
    conflicts: 'first' (the first reference that answers wins), 'skip' (a name two references answer with different types gets no
    type: a name used for two things, 'chi-chi' an artist on one site and a character on another)
    types: rows of other types give no answer either, a later list may still type the name; None: any type answers, so a name the
    first list calls 'general' stays untyped even when a later list knows it as an artist"""
    keep = None if types is None else frozenset(types)
    result: dict[str, str] = {}
    won: list[int] = []
    disputed: set[str] = set()
    for path in paths:
        here: dict[str, tuple[int, str | None]] = {}
        for name, posts, ttype in iter_rows(path):
            if name in wanted:
                here[name] = (posts, ttype)
        before = len(result)
        for name, (posts, ttype) in here.items():
            if ttype is None or posts < min_posts or (keep is not None and ttype not in keep):
                continue
            if max_ratio > 0 and isinstance(wanted, Mapping) and wanted[name] > max_ratio * max(posts, 1):
                continue
            if conflicts == 'skip' and result.get(name, ttype) != ttype:
                disputed.add(name)
            result.setdefault(name, ttype)
        won.append(len(result) - before)
        del here
    if disputed:
        result = {name: ttype for name, ttype in result.items() if name not in disputed}
    return References(result, won, len(disputed))


def untyped_names(path: pathlib.Path, retype: Iterable[str] = ()) -> dict[str, int]:
    """{name: posts} of the rows of a list that have no type (or 'unknown'), or a type of retype (canonical names: 'tag' is
    'general'), the last line of a name counts"""
    again = frozenset(retype)
    names: dict[str, int] = {}
    with open(path, 'rb') as f:
        for raw in f:
            if (m := re_row.fullmatch(raw)) and (m['type'] in _UNTYPED or _CANON_B.get(m['type']) in again):
                try:
                    names[_name(m['key'])] = int(m['posts'])
                except ValueError:
                    continue
    return names


class TypingResult(NamedTuple):
    rows: int
    '''tag lines'''
    typed_before: int
    '''tag lines that had a type (kept as they are; the retyped ones are not counted here)'''
    typed: Counter
    '''{type: lines typed now}'''
    left: int
    '''untyped lines that stay untyped'''
    other: int
    '''lines that are not tag lines (braces and blank lines not counted), kept as they are'''
    retyped: Counter
    '''{(type before, type now): lines of a retype type given another type}'''


def type_list(src: pathlib.Path, out: pathlib.Path, refs: dict[str, str], types: Iterable[str],
              retype: Iterable[str] = ()) -> TypingResult:
    """Copies src to out (atomic replace), giving the untyped rows whose name refs types (one of types) that type.
    retype: rows of these types (canonical names) get the type refs gives them too when it is one of types and another one; meant
    for a reference of the same site only (its newer list), as another site's type would replace the site's own.
    Every other line is copied byte for byte; a typed line differs only by ' (type)' after its posts count"""
    allowed = frozenset(types)
    again = frozenset(retype)
    rows = typed_before = left = other = 0
    typed: Counter = Counter()
    retyped: Counter = Counter()
    tmp = out.with_name(f'.{out.name}.{os.getpid():d}.tmp')
    try:
        with open(src, 'rb') as fin, open(tmp, 'wb') as fout:
            for raw in fin:
                m = re_row.fullmatch(raw)
                if m is None:
                    other += raw.strip(b' \t\r\n\xef\xbb\xbf') not in (b'', b'{', b'}')
                    fout.write(raw)
                    continue
                rows += 1
                before = None if m['type'] in _UNTYPED else _CANON_B.get(m['type'])
                if m['type'] not in _UNTYPED and before not in again:
                    typed_before += 1
                    fout.write(raw)
                    continue
                try:
                    ttype = refs.get(_name(m['key']))
                except ValueError:
                    ttype = None
                if before is not None:  # a row of a retype type: another assigned type, or the line as it is
                    if ttype is None or ttype not in allowed or ttype == before:
                        typed_before += 1
                        fout.write(raw)
                        continue
                    retyped[before, ttype] += 1
                elif ttype is None or ttype not in allowed:
                    left += 1
                    fout.write(raw)
                    continue
                else:
                    typed[ttype] += 1
                fout.write(b'%s%s: "%s posts (%s)"%s' % (m['pre'], m['key'], m['posts'], ttype.encode(), m['post']))
            fout.flush()
            os.fsync(fout.fileno())
        os.replace(tmp, out)
    finally:
        tmp.unlink(missing_ok=True)
    return TypingResult(rows, typed_before, typed, left, other, retyped)


class Evaluation(NamedTuple):
    rows: int
    '''typed rows of the evaluated list'''
    predicted: Counter
    '''{type: rows the tool would give that type}'''
    correct: Counter
    '''{type: of those, rows the list itself types so}'''
    truth: Counter
    '''{type: rows the list types so}'''
    confusions: Counter
    '''{(predicted type, list's type): rows} of wrong predictions'''


def evaluate(path: pathlib.Path, ref_paths: Iterable[pathlib.Path], types: Iterable[str], min_ref_posts: int = 0,
             min_posts: tuple[int, ...] = (0, EVAL_MIN_POSTS), fallthrough: bool = True,
             max_ratio: int = 0, conflicts: str = 'first') -> tuple[list[Evaluation], References]:
    """Hides the types of a typed list, infers them from the references as type_list would and compares (one Evaluation per
    min_posts cut: rows of the evaluated list with at least that many posts). fallthrough: types given to load_references"""
    allowed = frozenset(types)
    truth: dict[str, tuple[int, str]] = {}
    for name, posts, ttype in iter_rows(path):
        if ttype is None:
            truth.pop(name, None)  # the last line of a name counts
        else:
            truth[name] = (posts, ttype)
    refs = load_references(ref_paths, {name: posts for name, (posts, _) in truth.items()}, min_ref_posts,
                           allowed if fallthrough else None, max_ratio, conflicts)
    result = []
    for cut in min_posts:
        ev = Evaluation(0, Counter(), Counter(), Counter(), Counter())
        rows = 0
        for name, (posts, real) in truth.items():
            if posts < cut:
                continue
            rows += 1
            ev.truth[real] += 1
            guess = refs.types.get(name)
            if guess is None or guess not in allowed:
                continue
            ev.predicted[guess] += 1
            if guess == real:
                ev.correct[guess] += 1
            else:
                ev.confusions[guess, real] += 1
        result.append(ev._replace(rows=rows))
    return result, refs


def _pct(part: int, whole: int) -> str:
    return f'{100.0 * part / whole:5.1f}%' if whole else '     -'


def _cut_name(cut: int) -> str:
    return f'posts >= {cut:d}' if cut else 'all rows'


def format_evaluation(evs: list[Evaluation], cuts: tuple[int, ...], types: Iterable[str]) -> str:
    """Precision / recall per type and overall, one column group per cut. 'overall' counts the rows of the assigned types"""
    allowed = set(types)
    shown = [t for t in (*TYPE_ORDER, *TYPE_NAMES) if t in allowed or any(ev.truth[t] for ev in evs)]
    cells = lambda pred, good, real: f'{pred:>9} {_pct(good, pred)} {real:>7} {_pct(good, real)}'
    lines = [f'{"":<10}' + ''.join(f' | {_cut_name(c):<30}' for c in cuts),
             f'{"type":<10}' + ' | predicted   prec   truth recall' * len(evs)]
    for t in dict.fromkeys(shown):
        lines.append(f'{t:<10}' + ''.join(f' | {cells(ev.predicted[t], ev.correct[t], ev.truth[t])}' for ev in evs))
    overall = [cells(sum(ev.predicted.values()), sum(ev.correct.values()), sum(n for t, n in ev.truth.items() if t in allowed))
               for ev in evs]
    lines.append(f'{"overall":<10}' + ''.join(f' | {c}' for c in overall))
    lines.append(f'{"typed":<10}' + ''.join(f' | {f"{_pct(sum(ev.predicted.values()), ev.rows).strip()} of {ev.rows:d} rows":>30}'
                                            for ev in evs))
    for cut, ev in zip(cuts, evs, strict=True):
        if worst := ', '.join(f'{p} for {r} {n:d}' for (p, r), n in ev.confusions.most_common(4)):
            lines.append(f'wrong ({_cut_name(cut)}): {worst}')
    return '\n'.join(line.rstrip() for line in lines)


def _types_arg(text: str) -> tuple[str, ...]:
    result = []
    for part in text.split(','):
        if not (part := part.strip().lower()):
            continue
        if part not in _CANON:
            raise argparse.ArgumentTypeError(f'unknown type \'{part}\' (known: {", ".join(TYPE_NAMES)})')
        result.append(_CANON[part])
    if not result:
        raise argparse.ArgumentTypeError('no type given')
    return tuple(dict.fromkeys(result))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Types the untyped rows of a tag list from typed tag lists by exact name')
    what = ap.add_mutually_exclusive_group(required=True)
    what.add_argument('--list', type=pathlib.Path, help='list whose untyped rows get a type')
    what.add_argument('--evaluate', type=pathlib.Path, metavar='TYPED_LIST', help='measure the inference on a typed list instead')
    ap.add_argument('--ref', type=pathlib.Path, action='append', required=True, help='typed list (repeatable, the first one wins)')
    ap.add_argument('--out', type=pathlib.Path, help='output file (with --list)')
    ap.add_argument('--types', type=_types_arg, default=DEFAULT_TYPES, help=f'types to assign (default: {",".join(DEFAULT_TYPES)})')
    ap.add_argument('--min-ref-posts', type=int, default=0, metavar='N', help='ignore reference rows with fewer posts (default: 0)')
    ap.add_argument('--conflicts', choices=('first', 'skip'), default='first',
                    help='two references type a name differently: the first one wins (default), or skip: the name stays untyped')
    ap.add_argument('--max-count-ratio', type=int, default=DEFAULT_MAX_COUNT_RATIO, metavar='R',
                    help=f'ignore a reference row used R times less than the list\'s row (0: no limit, default: {DEFAULT_MAX_COUNT_RATIO:d})')
    ap.add_argument('--retype', type=_types_arg, default=(), metavar='TYPES',
                    help='rows of these types get another type of --types too (with --list; only with a newer list of the same site '
                         'as the reference: e.g. general, for a list that typed its artists general)')
    args = ap.parse_args(argv)
    if args.max_count_ratio < 0 or args.min_ref_posts < 0:
        ap.error('--min-ref-posts and --max-count-ratio take 0 or more')
    if args.list is not None and args.out is None:
        ap.error('--out is required with --list')
    if args.out is not None and args.out.is_dir():
        ap.error(f'--out is a folder: {args.out}')
    if args.out is not None and args.evaluate is not None:
        ap.error('--out is not used with --evaluate')
    if args.retype and args.evaluate is not None:
        ap.error('--retype is not used with --evaluate')
    if args.out is not None and not args.out.resolve().parent.is_dir():
        ap.error(f'no such folder: {args.out.resolve().parent}')
    for path in [*args.ref, args.list or args.evaluate]:
        if not path.is_file():
            ap.error(f'not a file: {path}')
    refs_text = ', '.join(p.name for p in args.ref)
    if args.evaluate is not None:
        cuts = (0, EVAL_MIN_POSTS)
        evs, refs = evaluate(args.evaluate, args.ref, args.types, args.min_ref_posts, cuts, max_ratio=args.max_count_ratio,
                             conflicts=args.conflicts)
        print(f'{args.evaluate.name}: types hidden, inferred from {refs_text} (types {",".join(args.types)}, '
              f'min ref posts {args.min_ref_posts:d}, max count ratio {args.max_count_ratio:d}, conflicts {args.conflicts}; '
              f'names typed per ref: {", ".join(map(str, refs.won))}; disputed, left untyped: {refs.conflicts:d})')
        print(format_evaluation(evs, cuts, args.types))
        return 0
    refs = load_references(args.ref, untyped_names(args.list, args.retype), args.min_ref_posts, args.types, args.max_count_ratio,
                           args.conflicts)
    res = type_list(args.list, args.out, refs.types, args.types, args.retype)
    untyped = sum(res.typed.values()) + res.left
    retyped = sum(res.retyped.values())
    print(f'{args.list.name}: {res.rows:d} rows, {res.typed_before:d} typed already (kept), '
          + (f'{retyped:d} retyped ({",".join(args.retype)}), ' if args.retype else '') + f'{untyped:d} untyped')
    print(f'refs {refs_text}: names typed per ref {", ".join(map(str, refs.won))}'
          + (f'; disputed, left untyped: {refs.conflicts:d}' if args.conflicts == 'skip' else ''))
    print('typed now: ' + (', '.join(f'{t} {n:d}' for t, n in sorted(res.typed.items(), key=lambda x: -x[1])) or 'none') +
          f' (total {sum(res.typed.values()):d})')
    if args.retype:
        print('retyped: ' + (', '.join(f'{a} -> {b} {n:d}' for (a, b), n in res.retyped.most_common()) or 'none') + f' (total {retyped:d})')
    print(f'left untyped: {res.left:d}' + (f'; lines kept as they are (not tag rows): {res.other:d}' if res.other else ''))
    print(f'-> {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
