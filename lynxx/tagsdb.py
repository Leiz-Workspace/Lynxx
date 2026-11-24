# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

import bisect
import hashlib
import heapq
import itertools
import json
import operator
import os
import pathlib
import re
import sys
import tempfile
import threading
import time
import zlib
from array import array
from collections import OrderedDict
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import NamedTuple, TextIO

from .defines import FILE_NAME_ALIASES, MODULE_CHOICES, TAG_AUTOCOMPLETE_LENGTH_MIN, TAG_AUTOCOMPLETE_NUMBER_MAX, UTF8
from .logger import trace
from .module import ProcModule
from .rex import re_wtag
from .tags import TAG_TYPES_BY_NAME, TagInfo, TagTypes, tag_type_from_name
from .utils import format_exception

__all__ = ('TAGS_MAX_LOADED', 'TAG_ALIASES', 'BundledList', 'TagsDB', 'is_wtag', 'load_tag_aliases', 'wildcard_pattern')

TAG_ALIASES: dict[str, str] = {}

TAGS_MAX_LOADED = 2
'''tag lists kept in memory at once, the least recently used one is unloaded (a reload from the binary cache is fast)'''
TAGS_CACHE_MIN_SIZE = 1024 * 1024
'''smaller lists parse in milliseconds and are not cached'''
TAGS_CACHE_VERSION = 1
'''bump when parsing semantics or the cache layout change'''
TAGS_VIEW_CACHE_ENTRIES = 8
TAGS_VIEW_CACHE_ITEMS = 4 * 1024 * 1024
'''filtered / popularity sorted browse results kept per loaded list (entries, total indices)'''

_ENC_ERRORS = 'surrogatepass'
'''utf-8 byte order equals code point order (python str order), surrogatepass keeps it for lone surrogates from escaped keys'''
_U32 = 'I' if array('I').itemsize == 4 else 'L'
_TYPE_BY_CODE: list[TagTypes] = [TagTypes.TAG_TYPE_UNKNOWN] * 256
for _tt in TagTypes:
    _TYPE_BY_CODE[int(_tt)] = _tt
_CODE_BY_NAME: dict[str, int] = {str(k): int(v) for k, v in TAG_TYPES_BY_NAME.items()}
_CODE_UNKNOWN = int(TagTypes.TAG_TYPE_UNKNOWN)
_CACHE_MAGIC = b'LYNXX-TAGS\x00'
_CACHE_TMP_MAX_AGE = 3600.0
_PARSE_CHUNK_CHARS = 4 * 1024 * 1024

FUZZY_MIN_LENGTH = 3
'''fuzzy search: shorter texts (underscores not counted) get no suggestions'''
FUZZY_TWO_EDITS_LENGTH = 6
'''fuzzy search: texts of at least this length (underscores not counted) allow 2 edits, shorter ones 1'''
FUZZY_MAX_LENGTH = 256
'''fuzzy search: longer texts get no suggestions'''
WILDCARD_SCAN_MAX = 100000
'''wildcard existence check: names tested at most (a pattern without a literal prefix scans the list from the start)'''
_FOLD_PLANES = 8
_FOLD_SPLIT = bytes((0x31 if x == 0 else 0x30) for x in range(256))
'''record separator (0) -> '1', anything else -> '0' (see _FuzzyIndex)'''
_FOLD_BIT_TABLES = tuple(bytes((0x31 if (x >> b) & 1 else 0x30) for x in range(256)) for b in range(_FOLD_PLANES))

re_plain_tag_line = re.compile(r'^"([^"\\\n]*)": "([0-9]+) posts(?: \(([^()\\\n]*)\))?",?$', re.MULTILINE)
'''fast path: one unescaped '"tag": "N posts (type)",' line (type is optional, '' -> unknown), parsed exactly as TagsDB._parse_line would.
Anything else (escaped keys, braces, junk) goes through it'''


class _TagList:
    """Immutable compact tag list. Name i is blob[offs[i]:offs[i + 1]] (utf-8, sorted, unique), posts[i] / types[i] (TagTypes value) go with it.
    Readers keep a reference and need no lock: unloading only drops the list from TagsDB"""
    __slots__ = ('_fuzzy', '_fuzzy_lock', '_lock', '_type_counts', '_views', '_views_items', 'added', 'blob', 'floor', 'offs', 'posts',
                 'types')

    def __init__(self, blob: bytes, offs: array, posts: array, types: bytes, added: frozenset[str] = frozenset()) -> None:
        self.blob, self.offs, self.posts, self.types = blob, offs, posts, types
        self.added = added
        '''names inserted from learned additions on top of an extension's bundled list (_with_additions): not in the bundled list'''
        self.floor = 0 if posts.typecode == _U32 or not posts else min(posts)
        '''lowest posts count: a min_posts filter at or below it selects everything'''
        self._lock = threading.Lock()
        self._views: OrderedDict[tuple, array] = OrderedDict()
        self._views_items = 0
        self._fuzzy: _FuzzyIndex | None = None
        '''typo search index, built on first use and dropped with the list'''
        self._fuzzy_lock = threading.Lock()
        self._type_counts: dict[int, int] | None = None
        '''names per type code, counted on first use (the list is immutable)'''

    def __len__(self) -> int:
        return len(self.offs) - 1

    def name(self, i: int) -> str:
        return self.blob[self.offs[i]:self.offs[i + 1]].decode(UTF8, _ENC_ERRORS)

    def info(self, i: int) -> TagInfo:
        return TagInfo(_TYPE_BY_CODE[self.types[i]], self.posts[i])

    def row(self, i: int) -> tuple[str, TagInfo]:
        return self.name(i), self.info(i)

    def bisect(self, key: bytes, lo: int = 0) -> int:
        """bisect_left by encoded name (from lo)"""
        blob, offs = self.blob, self.offs
        hi = len(offs) - 1
        while lo < hi:
            mid = (lo + hi) >> 1
            if blob[offs[mid]:offs[mid + 1]] < key:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def prefix_range(self, prefix: str) -> range:
        """Indices of names starting with prefix (utf-8 never contains 0xFF, so incrementing the last byte gives the upper bound)"""
        key = prefix.encode(UTF8, _ENC_ERRORS)
        if not key:
            return range(len(self))
        return range(self.bisect(key), self.bisect(key[:-1] + bytes((key[-1] + 1,))))

    def find(self, tag: str) -> int:
        key = tag.encode(UTF8, _ENC_ERRORS)
        i = self.bisect(key)
        return i if i < len(self) and self.blob[self.offs[i]:self.offs[i + 1]] == key else -1

    def select(self, prefix: str, non_alpha: bool, codes: frozenset[int], min_posts: int, by_posts: bool) -> Sequence[int]:
        """Indices of matching names, alphabetical or most popular first (stable: equal counts stay alphabetical).
        non_alpha: names not starting with a latin letter (prefix ignored). Computed results are cached until the list is unloaded"""
        if non_alpha:
            ranges = (range(self.bisect(b'a')), range(self.bisect(b'{'), len(self)))  # 'z...' < '{'
        else:
            ranges = (self.prefix_range(prefix),)
        filtered = bool(codes) or min_posts > self.floor
        if not filtered and not by_posts and not non_alpha:
            return ranges[0]
        vkey = (None if non_alpha else prefix, codes, min_posts if filtered else None, by_posts)
        with self._lock:
            if (cached := self._views.get(vkey)) is not None:
                self._views.move_to_end(vkey)
                return cached
        result = array(_U32)
        for r in ranges:
            if not filtered:
                result.extend(r)
                continue
            sel = None
            if codes:
                table = bytes(1 if c in codes else 0 for c in range(256))
                sel = self.types[r.start:r.stop].translate(table)
            if min_posts > self.floor:
                over = map(min_posts.__le__, self.posts[r.start:r.stop])
                sel = map(operator.and_, sel, over) if sel is not None else over
            result.extend(itertools.compress(r, sel))
        if by_posts:
            result = array(_U32, sorted(result, key=self.posts.__getitem__, reverse=True))
        with self._lock:
            if vkey not in self._views:
                self._views[vkey] = result
                self._views_items += len(result)
                while len(self._views) > 1 and (len(self._views) > TAGS_VIEW_CACHE_ENTRIES or self._views_items > TAGS_VIEW_CACHE_ITEMS):
                    self._views_items -= len(self._views.popitem(last=False)[1])
        return result

    def type_counts(self) -> dict[int, int]:
        """Names per type code (only the codes the list has)"""
        if (counts := self._type_counts) is None:
            counts = {code: n for code in set(self.types) if (n := self.types.count(code))}
            self._type_counts = counts
        return counts

    def top(self, prefix: str, limit: int | None, scan_max: int, codes: frozenset[int] = frozenset()) -> list[int]:
        """Names starting with prefix, most popular first (stable), limited to the first scan_max matches alphabetically if scan_max > 0.
        codes: only names of these types, picked inside that range (no cached view: autocomplete must not evict browse results)"""
        r = self.prefix_range(prefix)
        if scan_max > 0:
            r = r[:scan_max]
        if codes:
            table = bytes(1 if c in codes else 0 for c in range(256))
            r = list(itertools.compress(r, self.types[r.start:r.stop].translate(table)))
        key = self.posts.__getitem__
        if limit is not None and limit >= 0:
            return heapq.nlargest(limit, r, key=key)  # == sorted(r, key=key, reverse=True)[:limit]
        return sorted(r, key=key, reverse=True)[:limit]

    def fuzzy(self, text: str, limit: int, codes: frozenset[int] = frozenset()) -> list[tuple[int, int]]:
        """(distance, index) of names within 1 edit (texts shorter than FUZZY_TWO_EDITS_LENGTH) or 2 edits of text, closest first,
        then most popular, then alphabetical. Edits: insert / delete / replace a char, swap two adjacent chars; underscores are ignored
        ('catears' == 'cat_ears'). Texts shorter than FUZZY_MIN_LENGTH or longer than FUZZY_MAX_LENGTH give nothing.
        codes: only names of these types"""
        folded = _fold(text)
        if not FUZZY_MIN_LENGTH <= len(folded) <= FUZZY_MAX_LENGTH or limit <= 0 or not len(self):
            return []
        max_edits = 2 if len(folded) >= FUZZY_TWO_EDITS_LENGTH else 1
        with self._fuzzy_lock:
            if self._fuzzy is None:
                self._fuzzy = _FuzzyIndex(self)
            index = self._fuzzy
        found = index.search(folded, max_edits)
        exact_text = _folds_exactly(text)
        stripped = text.replace('_', '')
        result = []
        for dist, i in found:
            if codes and self.types[i] not in codes:
                continue
            raw = self.blob[self.offs[i]:self.offs[i + 1]]
            if not (exact_text and _folds_exactly_raw(raw)):  # hashed non-ascii chars may collide: check the real distance
                dist = _osa_distance(stripped, raw.decode(UTF8, _ENC_ERRORS).replace('_', ''))
                if dist > max_edits:
                    continue
            result.append((dist, i))
        posts = self.posts
        return heapq.nsmallest(limit, result, key=lambda r: (r[0], -posts[r[1]], r[1]))

    def wildcard_any(self, wtag: str) -> bool:
        """Whether some name matches a wildcard pattern ('*': any chars, '?': one char). Not known after WILDCARD_SCAN_MAX names
        (a pattern starting with a wildcard has no literal prefix to narrow the range) -> True"""
        cut = min((p for p in (wtag.find('*'), wtag.find('?')) if p >= 0), default=len(wtag))
        rng = self.prefix_range(wtag[:cut])
        rex = wildcard_pattern(wtag)
        if any(rex.fullmatch(self.name(i)) for i in rng[:WILDCARD_SCAN_MAX]):
            return True
        return len(rng) > WILDCARD_SCAN_MAX

    @staticmethod
    def build(names: list[str], posts: list[int], codes: bytes | bytearray, extra: Mapping[str, tuple[int, int]] | None = None,
              ) -> '_TagList | None':
        """From parallel names / posts counts / type codes in any order (usually sorted already) + extra {name: (posts, type code)}.
        None if names are not unique"""
        if all(map(operator.lt, names, itertools.islice(names, 1, None))):
            if extra and (merged := _merge_sorted(names, posts, codes, extra)) is None:
                return None
            names, posts, codes = merged if extra else (names, posts, codes)
        else:  # sort, check duplicates
            if extra:
                names, posts, codes = [*names, *extra], [*posts, *(v[0] for v in extra.values())], bytes([*codes, *(v[1] for v in extra.values())])
            order = sorted(range(len(names)), key=names.__getitem__)
            names = [names[i] for i in order]
            if any(map(operator.eq, names, itertools.islice(names, 1, None))):
                return None
            posts, codes = [posts[i] for i in order], bytes([codes[i] for i in order])
        # utf-8 encodes code points independently (surrogatepass too): joined slices encode at once, ascii names need no own encode
        step = 65536
        blob = b''.join(''.join(names[i:i + step]).encode(UTF8, _ENC_ERRORS) for i in range(0, len(names), step))
        offs = array(_U32, itertools.accumulate((len(n) if n.isascii() else len(n.encode(UTF8, _ENC_ERRORS)) for n in names), initial=0))
        if offs[-1] != len(blob):
            raise AssertionError('tag list encoding mismatch')
        return _TagList(blob, offs, _posts_array(posts), bytes(codes))

    @staticmethod
    def from_mapping(tags: Mapping[str, TagInfo | tuple[TagTypes, int]]) -> '_TagList':
        names = list(tags)
        infos = [TagInfo(*tags[n]) for n in names]
        return _TagList.build(names, [i.posts_count for i in infos], bytes([int(i.tag_type) for i in infos]))


_EMPTY_LIST = _TagList(b'', array(_U32, [0]), array(_U32), b'')


def _with_additions(base: _TagList, extra: _TagList) -> _TagList:
    """base + the names of extra that base lacks (the tags learned on top of an extension's bundled list, lynxx/tags_learned.py):
    a name base has keeps base's count and type. Byte slices and shifted offsets only, no name of base is decoded
    (rule34.us, 544k tags + 1,000 additions: ~35 ms)"""
    picks: list[tuple[int, int]] = []
    '''(base index to insert before, extra index), in name order'''
    lo = 0
    for j in range(len(extra)):
        raw = extra.blob[extra.offs[j]:extra.offs[j + 1]]
        i = lo = base.bisect(raw, lo)
        if i < len(base) and base.blob[base.offs[i]:base.offs[i + 1]] == raw:
            continue
        picks.append((i, j))
    if not picks:
        return base
    blob_in = memoryview(base.blob)
    parts: list = []
    offs = array(_U32, [0])
    posts = array(base.posts.typecode)
    posts_max = (1 << 8 * posts.itemsize) - 1 if posts.typecode == _U32 else (1 << 63) - 1
    '''a learned count is the number of posts seen here: never near the limit of the bundled list's counts'''
    types = bytearray()
    added: list[str] = []
    prev = shift = 0

    def copy_base(stop: int) -> None:
        if stop > prev:
            parts.append(blob_in[base.offs[prev]:base.offs[stop]])
            seg = base.offs[prev + 1:stop + 1]
            offs.extend(array(_U32, map(shift.__add__, seg)) if shift else seg)
            posts.extend(base.posts[prev:stop])
            types.extend(base.types[prev:stop])

    for i, j in picks:
        copy_base(i)
        prev = i
        raw = extra.blob[extra.offs[j]:extra.offs[j + 1]]
        parts.append(raw)
        shift += len(raw)
        offs.append(offs[-1] + len(raw))
        posts.append(min(extra.posts[j], posts_max))
        types.append(extra.types[j])
        added.append(raw.decode(UTF8, _ENC_ERRORS))
    copy_base(len(base))
    blob = b''.join(parts)
    if offs[-1] != len(blob):
        raise AssertionError('tag list merge mismatch')
    return _TagList(blob, offs, posts, bytes(types), frozenset(added))


def _merge_sorted(names: list[str], posts: list[int], codes: bytes | bytearray, extra: Mapping[str, tuple[int, int]],
                  ) -> tuple[list[str], list[int], bytearray] | None:
    """Inserts a few extra entries into strictly increasing names (copying slices). None if an extra name is already there"""
    out_n: list[str] = []
    out_p: list[int] = []
    out_c = bytearray()
    prev = 0
    for name in sorted(extra):
        i = bisect.bisect_left(names, name, prev)
        if i < len(names) and names[i] == name:
            return None
        out_n += names[prev:i]
        out_p += posts[prev:i]
        out_c += codes[prev:i]
        out_n.append(name)
        out_p.append(extra[name][0])
        out_c.append(extra[name][1])
        prev = i
    return out_n + names[prev:], out_p + posts[prev:], out_c + codes[prev:]


def _posts_array(values: list[int]) -> array:
    for typecode in (_U32, 'q'):
        try:
            return array(typecode, values)
        except OverflowError:
            continue
    return array('q', [max(-2 ** 63, min(2 ** 63 - 1, v)) for v in values])


# ----- typo tolerant search -----

def _fold(name: str) -> bytes:
    """Fuzzy search key: underscores dropped, one byte per char. Ascii chars stay, others (and NUL, the record separator) are hashed
    into 0x80..0xfe: equal chars give equal bytes, a collision of different chars is re-checked with _osa_distance"""
    return bytes(o if 0 < o < 0x80 else 0x80 + o % 0x7F for o in map(ord, name) if o != 0x5F)


def _folds_exactly(name: str) -> bool:
    return name.isascii() and '\x00' not in name


def _folds_exactly_raw(raw: bytes) -> bool:
    return raw.isascii() and b'\x00' not in raw


def _osa_distance(a: str, b: str) -> int:
    """Optimal string alignment distance: insertions, deletions, substitutions, adjacent transpositions"""
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            v = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                v = min(v, prev2[j - 2] + 1)
            cur[j] = v
        prev2, prev = prev, cur
    return prev[-1]


class _FuzzyIndex:
    """Typo search over a whole tag list without per-name python work at query time.
    Names are folded (_fold) and grouped by folded length L. A group is one record per name, b'\\0' + folded name (L + 1 bytes), and is
    kept as 8 bit planes: big integers with one bit per byte (bit b of the byte), the first byte being the highest bit, so 'next char'
    is '>> 1'. A query runs the bit-parallel edit distance automaton (Wu-Manber, with adjacent transpositions) on every record of the
    groups of length len(text) +- k at once: about 20 integer operations per text char and allowed edit, each one a C loop over
    the group (danbooru, 766k tags: ~0.4 s to build, ~16 MB, a few ms per query)"""
    __slots__ = ('groups',)

    def __init__(self, lst: _TagList) -> None:
        blob, offs = lst.blob, lst.offs
        records: dict[int, bytearray] = {}
        indices: dict[int, array] = {}
        has_nul = b'\x00' in blob
        for i in range(len(lst)):
            raw = blob[offs[i]:offs[i + 1]]
            if raw.isascii() and not (has_nul and b'\x00' in raw):
                key = raw.replace(b'_', b'')
            else:
                key = _fold(raw.decode(UTF8, _ENC_ERRORS))
            if (data := records.get(len(key))) is None:
                records[len(key)] = data = bytearray()
                indices[len(key)] = array(_U32)
            data += b'\x00'
            data += key
            indices[len(key)].append(i)
        records.pop(0, None)
        self.groups: dict[int, tuple[tuple[int, ...], int, int, array]] = {}
        '''folded length -> (bit planes, record starts mask, bits, tag indices)'''
        for length in sorted(records):
            data = records.pop(length)
            planes = tuple(int(data.translate(table), 2) for table in _FOLD_BIT_TABLES)
            self.groups[length] = (planes, int(data.translate(_FOLD_SPLIT), 2), len(data), indices[length])

    def search(self, text: bytes, k: int) -> list[tuple[int, int]]:
        """(distance, tag index) of every name whose folded form is within k edits of folded text"""
        result: list[tuple[int, int]] = []
        for length in range(max(1, len(text) - k), len(text) + k + 1):
            if (group := self.groups.get(length)) is not None:
                result.extend(self._search_group(text, k, length, *group))
        return result

    @staticmethod
    def _search_group(text: bytes, k: int, length: int, planes: tuple[int, ...], starts: int, nbits: int, indices: array,
                      ) -> Iterator[tuple[int, int]]:
        ones = (1 << nbits) - 1
        body = starts ^ ones
        eqs: dict[int, int] = {}
        for c in set(text):  # positions holding char c
            v = ones
            for b, plane in enumerate(planes):
                if (c >> b) & 1:
                    v &= plane
                elif plane:  # an all-zero plane (bit 7 of ascii-only lists) matches every 0 bit
                    v &= plane ^ ones
            eqs[c] = v
        # states[d]: bit at record position j (0: before the first char) = the first j chars of the name match the text read so far
        # with at most d edits
        states = []
        acc = 0
        for d in range(k + 1):
            if d <= length:
                acc |= starts >> d
            states.append(acc)
        prev: list[int] = []
        prev_eq = 0
        for c in text:
            eq = eqs[c]
            new = [(states[0] >> 1) & eq]
            for d in range(1, k + 1):
                v = ((states[d] >> 1) & eq) | (((states[d - 1] | new[d - 1]) >> 1) & body) | states[d - 1]  # match, replace / delete, insert
                if prev:
                    v |= ((((prev[d - 1] >> 1) & eq) >> 1) & prev_eq)  # swap
                new.append(v)
            prev, states, prev_eq = states, new, eq
        last = starts >> length
        seen = 0
        for d in range(k + 1):
            hits = states[d] & last & ~seen
            if not hits:
                continue
            seen |= hits
            bits = format(hits, f'0{nbits}b')
            pos = bits.find('1')
            while pos >= 0:
                yield d, indices[pos // (length + 1)]
                pos = bits.find('1', pos + 1)


def _parse_lines_exact(lines: Iterable[str]) -> dict[str, tuple[int, int]]:
    """Original line by line loader: TagsDB._parse_line + posts count + type, malformed lines skipped, the last duplicate wins"""
    result: dict[str, tuple[int, int]] = {}
    for line in lines:
        try:
            kv_k, kv_v = TagsDB._parse_line(line)
            posts_count = int(kv_v[:kv_v.find(' ')])
            tag_type = tag_type_from_name(kv_v, kv_v.find('('), kv_v.rfind(')'))
            result[kv_k] = (posts_count, int(tag_type))
        except Exception:
            continue
    return result


def _iter_line_chunks(f: TextIO) -> Iterator[str]:
    """Text of whole lines, about _PARSE_CHUNK_CHARS at a time (keeps the peak memory of a cold parse low)"""
    tail = ''
    while chunk := f.read(_PARSE_CHUNK_CHARS):
        chunk = tail + chunk
        cut = chunk.rfind('\n') + 1
        tail = chunk[cut:]
        if cut:
            yield chunk[:cut]
    if tail:
        yield tail


def _parse_tag_list(path: pathlib.Path) -> _TagList:
    """Plain lines in bulk (regex), the rest (escaped keys, braces, junk) through the exact loader.
    Duplicate names fall back to the original sequential loader so that the last line wins exactly as before"""
    names: list[str] = []
    posts: list[int] = []
    codes = bytearray()
    rest: dict[str, tuple[int, int]] = {}
    with open(path, 'rt', encoding=UTF8) as dbfile:
        for chunk in _iter_line_chunks(dbfile):
            plain = re_plain_tag_line.findall(chunk)
            names += [t[0] for t in plain]
            posts += [int(t[1]) for t in plain]
            codes += bytes([_CODE_BY_NAME.get(t[2], _CODE_UNKNOWN) for t in plain])
            del plain
            rest.update(_parse_lines_exact(line for line in re_plain_tag_line.sub('', chunk).split('\n') if line))
    lst = _TagList.build(names, posts, codes, rest)
    if lst is None:
        del names, posts, codes, rest
        with open(path, 'rt', encoding=UTF8) as dbfile:
            exact = _parse_lines_exact(dbfile)
        lst = _TagList.build(list(exact), [v[0] for v in exact.values()], bytes([v[1] for v in exact.values()]))
    return lst


# ----- binary cache: <data dir>/cache/tags/<list name>-<source path hash>.bin -----

def _cache_dir() -> pathlib.Path:
    from .extensions import data_dir
    return data_dir() / 'cache' / 'tags'


def _cache_file(source: str, list_name: str) -> pathlib.Path:
    digest = hashlib.sha1(source.encode(UTF8, _ENC_ERRORS)).hexdigest()[:16]
    return _cache_dir() / f'{list_name.removesuffix(".json")}-{digest}.bin'


def _source_stamp(source: str, st: os.stat_result) -> dict:
    return {'v': TAGS_CACHE_VERSION, 'src': source, 'mtime': st.st_mtime_ns, 'size': st.st_size, 'ino': st.st_ino, 'order': sys.byteorder}


def _read_cache_header(f) -> dict | None:
    if f.read(len(_CACHE_MAGIC)) != _CACHE_MAGIC:
        return None
    hlen = int.from_bytes(f.read(4), 'little')
    return json.loads(f.read(hlen).decode(UTF8)) if 0 < hlen <= 65536 else None


def _cache_crc(offs: array, posts: array, types: bytes, blob: bytes) -> int:
    return zlib.crc32(blob, zlib.crc32(types, zlib.crc32(posts, zlib.crc32(offs))))


def _read_cache(cpath: pathlib.Path, stamp: dict) -> _TagList | None:
    try:
        with open(cpath, 'rb') as f:
            hdr = _read_cache_header(f)
            if not hdr or any(hdr.get(k) != v for k, v in stamp.items()):
                return None
            count, blob_len = int(hdr['n']), int(hdr['blob'])
            offs, posts = array(hdr['offs']), array(hdr['posts'])
            if [offs.itemsize, posts.itemsize] != hdr['isz']:
                return None
            offs.fromfile(f, count + 1)
            posts.fromfile(f, count)
            types, blob = f.read(count), f.read(blob_len)
            if len(types) != count or len(blob) != blob_len or f.read(1):
                return None
        if offs[0] != 0 or offs[-1] != blob_len or _cache_crc(offs, posts, types, blob) != hdr['crc']:
            return None
        return _TagList(blob, offs, posts, types)
    except FileNotFoundError:
        return None
    except Exception:
        trace(f'Warning: ignoring unreadable tag list cache {cpath}: {format_exception("row").strip()}')
        return None


def _write_cache(cpath: pathlib.Path, stamp: dict, lst: _TagList) -> None:
    """Atomic (temp file + replace): concurrent processes (sidecar, CLI jobs) never see a partial cache"""
    try:
        hdr = {**stamp, 'n': len(lst), 'blob': len(lst.blob), 'offs': lst.offs.typecode, 'posts': lst.posts.typecode,
               'isz': [lst.offs.itemsize, lst.posts.itemsize], 'crc': _cache_crc(lst.offs, lst.posts, lst.types, lst.blob)}
        raw_hdr = json.dumps(hdr, ensure_ascii=True).encode(UTF8)
        cpath.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=cpath.parent, prefix=f'.{cpath.name}.', suffix='.tmp')
        try:
            with os.fdopen(fd, 'wb') as f:
                f.write(_CACHE_MAGIC + len(raw_hdr).to_bytes(4, 'little') + raw_hdr)
                lst.offs.tofile(f)
                lst.posts.tofile(f)
                f.write(lst.types)
                f.write(lst.blob)
            os.replace(tmp, cpath)
        except BaseException:
            pathlib.Path(tmp).unlink(missing_ok=True)
            raise
        _prune_cache(cpath.parent)
    except Exception:
        trace(f'Warning: unable to write tag list cache {cpath}: {format_exception("row").strip()}')


def _prune_cache(cdir: pathlib.Path) -> None:
    """Drops caches of lists that no longer exist and stale temp files of crashed writers"""
    now = time.time()
    for p in cdir.iterdir():
        try:
            if p.suffix == '.tmp':
                if now - p.stat().st_mtime > _CACHE_TMP_MAX_AGE:
                    p.unlink()
            elif p.suffix == '.bin':
                with open(p, 'rb') as f:
                    hdr = _read_cache_header(f)
                if not hdr or not os.path.isfile(hdr.get('src', '')):
                    p.unlink()
        except (OSError, ValueError):
            continue


def _read_tag_list(path: pathlib.Path, use_cache: bool, cache_min_size: int) -> _TagList | None:
    """None if the file does not exist. Stat is taken before reading: a list replaced meanwhile never matches the stamp again"""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    cpath = stamp = None
    if use_cache and st.st_size >= cache_min_size:
        source = str(pathlib.Path(path).resolve())
        cpath, stamp = _cache_file(source, pathlib.Path(path).name), _source_stamp(source, st)
        if (lst := _read_cache(cpath, stamp)) is not None:
            return lst
    try:
        lst = _parse_tag_list(path)
    except FileNotFoundError:
        return None
    if cpath is not None:
        _write_cache(cpath, stamp, lst)
    return lst


def _is_learned_list(module: str, path: pathlib.Path) -> bool:
    """The list at <data dir>/tags is the one lynxx/tags_learned.py wrote last (its state is a small sqlite file next to it)"""
    try:
        from .tags_learned import is_learned
        return is_learned(module, path)
    except Exception:  # an unreadable learning state: the list counts as any other
        return False


def _learned_additions(module: str) -> pathlib.Path | None:
    """The tags lynxx/tags_learned.py learned on top of the list it adds to (additions_path), when there are any"""
    try:
        from .tags_learned import additions_path
        path = additions_path(module)
        return path if path.is_file() else None
    except Exception:  # an unreadable data dir: nothing is added
        return None


def _has_no_tag_source(manifest) -> bool:
    """The extension has no tag list source Lynxx can read (lynxx/tags_updater.py source_for_manifest: not a reviewed module, no
    'tagUpdate', no engine with a tag listing): its bundled list is not updated, the learning adds to it"""
    try:
        from .tags_updater import source_for_manifest
        return source_for_manifest(manifest) is None
    except Exception:  # an unreadable manifest field or denylist: not learned on top of
        return False


def _merges(manifest) -> bool:
    """The extension's own update reads only the most used tags and merges them into the list in use (TagSource.merge,
    tagUpdate.merge): that list is never read whole again, the learning adds to it"""
    try:
        from .tags_updater import source_for_manifest
        src = source_for_manifest(manifest)
        return src is not None and src.merge
    except Exception:  # an unreadable manifest field or denylist: not learned on top of
        return False


def _merge_of(path: pathlib.Path, shipped: pathlib.Path) -> bool:
    """The list at <data dir>/tags is the one a merge update wrote there last, and its merges started from the list the extension
    ships now (lynxx/tags_updater.py merged_onto: the same content, whatever the file times)"""
    try:
        from .tags_updater import list_digest, merged_onto
        onto = merged_onto(path)
        return onto is not None and onto == list_digest(shipped)
    except Exception:  # an unreadable stamp: not the update's
        return False


class BundledList(NamedTuple):
    """A module's list shipped inside an installed extension that has no tag source"""
    path: pathlib.Path
    ext_id: str
    version: str


class TagsDB:
    """TagsDB !Static!
    Thread-safe: shared state is guarded by an internal lock (never held while parsing a list or while callers use the result),
    loading is serialized per module. Independent of the global ProcModule state, so it never needs the sidecar core lock"""
    DBFiles: dict[str, pathlib.Path] = {}
    Bundled: dict[str, BundledList] = {}
    '''modules whose located list ships inside an installed extension that has no tag source (lynxx/tags_learned.py adds to it)'''
    LearningBases: dict[str, pathlib.Path] = {}
    '''module -> the located list the learning adds to (lynxx/tags_learned.py): a list shipped inside an installed extension that
    has no tag source (Bundled), or the list in use of a module whose own update merges a partial read into it (TagSource.merge,
    Paheal from 1.0.3)'''
    Additions: dict[str, pathlib.Path] = {}
    '''module -> the tags learned on top of the list in LearningBases (lynxx/tags_learned.py additions_path; before the learning
    first wrote them there, its whole learned list): the names that list lacks are added at load'''
    AuxDB: dict[str, pathlib.Path] = {}
    AuxDBFiles: dict[str, pathlib.Path] = {}
    MaxLoaded = TAGS_MAX_LOADED
    CacheEnabled = True
    CacheMinSize = TAGS_CACHE_MIN_SIZE
    _lists: OrderedDict[str, _TagList] = OrderedDict()
    '''loaded lists, least recently used first'''
    _gens: dict[str, int] = {}
    '''per module generation: bumped on unload, a load that started before is discarded and repeated'''
    _epoch = 0
    _lock = threading.RLock()
    _load_locks: dict[str, threading.Lock] = {}

    def __init__(self) -> None:
        raise RuntimeError(f'{self.__class__.__name__} class should never be instanced!')

    @staticmethod
    def try_locate_file_single(filename: str) -> None:
        if filename in TagsDB.AuxDBFiles:
            return
        acwd = pathlib.Path(os.getcwd()).resolve()
        afld = pathlib.Path(__file__).resolve().parent.parent
        if acwd.name == 'lynxx':
            acwd = acwd.parent
        if afld.name == 'lynxx':
            afld = afld.parent
        trace(f'cwd: \'{acwd}\'')
        trace(f'fld: \'{afld}\'')
        for basepath in (acwd, afld):
            for folder_path in (basepath, basepath / 'tags', basepath / '2tags'):
                pfilename = folder_path / filename
                trace(f'looking for {pfilename}...')
                if pfilename.is_file():
                    TagsDB.AuxDBFiles[filename] = pfilename
                    trace('...found')
                    break
            if filename in TagsDB.AuxDBFiles:
                break

    @staticmethod
    def try_set_basepath(basepath: pathlib.Path, *, traverse=True) -> int:
        def collect_taglist_names(fpath: pathlib.Path, paths_dict: dict[str, pathlib.Path]) -> None:
            for module in dict.fromkeys((*MODULE_CHOICES, *ProcModule.known_names())):
                taglist_path = fpath / f'{module}_tags.json'
                if taglist_path.is_file():
                    paths_dict[module] = taglist_path

        def collect_aux_file_names(fpath: pathlib.Path) -> None:
            tag_aliases_path = fpath / 'all_tag_aliases.json'
            if tag_aliases_path.is_file():
                TagsDB.AuxDBFiles[FILE_NAME_ALIASES] = tag_aliases_path

        folder = pathlib.Path(__file__).resolve().parent.parent
        with TagsDB._lock:
            TagsDB.clear()
            while not TagsDB.DBFiles:
                folder_up, tail = folder.parent, folder.name
                for folder_path in (basepath, basepath / 'tags', basepath / '2tags'):
                    if folder_path.is_dir():
                        tlpaths: dict[str, pathlib.Path] = {}
                        collect_taglist_names(folder_path, tlpaths)
                        if tlpaths:
                            TagsDB.DBFiles.update(tlpaths)
                        collect_aux_file_names(folder_path)
                if not tail or not traverse:
                    break
                folder = folder_up
            return len(TagsDB.DBFiles)

    @staticmethod
    def clear() -> None:
        with TagsDB._lock:
            TagsDB._lists.clear()
            TagsDB.DBFiles.clear()
            TagsDB.Bundled.clear()
            TagsDB.LearningBases.clear()
            TagsDB.Additions.clear()
            TagsDB._epoch += 1

    @staticmethod
    def is_empty() -> bool:
        return not TagsDB.DBFiles

    @staticmethod
    def files() -> dict[str, pathlib.Path]:
        """Snapshot of located tag lists (module -> path)"""
        with TagsDB._lock:
            return dict(TagsDB.DBFiles)

    @staticmethod
    def get_file(module: str) -> pathlib.Path | None:
        with TagsDB._lock:
            return TagsDB.DBFiles.get(module)

    @staticmethod
    def bundled(module: str) -> BundledList | None:
        """The module's list ships inside an installed extension that has no tag source (the learning adds the tags it lacks)"""
        with TagsDB._lock:
            return TagsDB.Bundled.get(module)

    @staticmethod
    def learning_base(module: str) -> pathlib.Path | None:
        """The located list the learning adds to (LearningBases), None when the module has none"""
        with TagsDB._lock:
            return TagsDB.LearningBases.get(module)

    @staticmethod
    def get_additions(module: str) -> pathlib.Path | None:
        """The learned list served on top of the module's list (its learning base)"""
        with TagsDB._lock:
            return TagsDB.Additions.get(module)

    @staticmethod
    def is_loaded(module: str) -> bool:
        with TagsDB._lock:
            return module in TagsDB._lists

    @staticmethod
    def loaded_modules() -> list[str]:
        """Least recently used first"""
        with TagsDB._lock:
            return list(TagsDB._lists)

    @staticmethod
    def _token(module: str) -> tuple[int, int]:
        return TagsDB._epoch, TagsDB._gens.get(module, 0)

    @staticmethod
    def _touch(module: str) -> _TagList | None:
        if (lst := TagsDB._lists.get(module)) is not None:
            TagsDB._lists.move_to_end(module)
        return lst

    @staticmethod
    def _store(module: str, lst: _TagList) -> None:
        TagsDB._lists[module] = lst
        TagsDB._lists.move_to_end(module)
        while len(TagsDB._lists) > max(1, TagsDB.MaxLoaded):
            TagsDB._lists.popitem(last=False)

    @staticmethod
    def _load(module: str) -> _TagList:
        """Loaded list of module (loads it if needed: binary cache or parse). A missing list file gives an empty list that is not kept"""
        with TagsDB._lock:
            if (lst := TagsDB._touch(module)) is not None:
                return lst
            if module not in TagsDB.DBFiles:
                return _EMPTY_LIST
            load_lock = TagsDB._load_locks.setdefault(module, threading.Lock())
        with load_lock:
            while True:
                with TagsDB._lock:
                    if (lst := TagsDB._touch(module)) is not None:
                        return lst
                    token, path, extra = TagsDB._token(module), TagsDB.DBFiles.get(module), TagsDB.Additions.get(module)
                    use_cache, cache_min_size = TagsDB.CacheEnabled, TagsDB.CacheMinSize
                lst = _read_tag_list(path, use_cache, cache_min_size) if path else None
                if lst is not None and extra is not None and (added := _read_tag_list(extra, use_cache, cache_min_size)):
                    lst = _with_additions(lst, added)
                with TagsDB._lock:
                    if TagsDB._token(module) != token:
                        continue  # relocated / reloaded / cleared while reading
                    if lst is None:
                        return _EMPTY_LIST
                    TagsDB._store(module, lst)
                    return lst

    @staticmethod
    def load_from_dict(module: str, tags: Mapping[str, TagInfo | tuple[TagTypes, int]]) -> None:
        """Registers an in-memory list for module (tests, tools): {name: TagInfo(type, posts)}"""
        lst = _TagList.from_mapping(tags)
        with TagsDB._lock:
            TagsDB._gens[module] = TagsDB._gens.get(module, 0) + 1
            TagsDB._store(module, lst)

    @staticmethod
    def count(module: str) -> int:
        return len(TagsDB._load(module))

    @staticmethod
    def lacking(module: str, names: Iterable[str]) -> list[str]:
        """Names the module's own list lacks: not in its loaded list, or there only as learned additions (TagsDB.Additions)"""
        lst = TagsDB._load(module)
        return [n for n in names if n in lst.added or lst.find(n) < 0]

    @staticmethod
    def as_dict(module: str) -> dict[str, TagInfo]:
        """Whole list as {name: TagInfo} in alphabetical order (tests, tools: allocates what the compact storage saves)"""
        lst = TagsDB._load(module)
        return dict(map(lst.row, range(len(lst))))

    @staticmethod
    def _parse_line(line: str) -> tuple[str, str]:
        """One '"tag": "N posts (type)",' line. Fast path for plain keys, json for escaped ones (\\" or \\\\ inside the tag)"""
        s = line.strip().lstrip('﻿')
        if s.endswith(','):
            s = s[:-1]
        if '\\' in s:
            return next(iter(json.loads('{' + s + '}').items()))
        if not (s.startswith('"') and s.endswith('"')):
            raise ValueError
        sep = s.index('": "', 1)
        return s[1:sep], s[sep + 4:-1]

    @staticmethod
    def extension_lists() -> dict[str, pathlib.Path]:
        """Lists shipped inside installed extensions (builtin and user): the manifest's tagList, '<abbr>_tags.json' next to
        manifest.json (the extension generator puts the site's list there, lynxx/extgen/taglist.py). Manifests only, no code runs;
        an extension can only give the list of its own module"""
        return {abbr: path for abbr, (path, _) in TagsDB._extension_list_manifests().items()}

    @staticmethod
    def _installed_manifests() -> list:
        """Manifests of the installed extensions, builtin first (no code runs); [] when they cannot be read"""
        from .extensions import SOURCE_BUILTIN, SOURCE_USER, Extensions
        try:
            return [*Extensions._scan(Extensions.builtin_dir(), SOURCE_BUILTIN), *Extensions._scan(Extensions.user_dir(), SOURCE_USER)]
        except OSError:
            return []

    @staticmethod
    def _extension_list_manifests(manifests: list | None = None) -> dict[str, tuple]:
        """extension_lists with the manifest that names each list: {abbr: (path, manifest)}. manifests: _installed_manifests"""
        result: dict[str, tuple] = {}
        for m in TagsDB._installed_manifests() if manifests is None else manifests:
            if m.path is not None and m.abbr not in result and m.tagList == f'{m.abbr}_tags.json' and (m.path / m.tagList).is_file():
                result[m.abbr] = (m.path / m.tagList, m)
        return result

    @staticmethod
    def locate_default_lists(reload: Iterable[str] = ()) -> int:
        """LYNXX_TAGS_DIR wins, otherwise the most recently updated list per module among <data dir>/tags, the lists shipped
        inside installed extensions (extension_lists) and repo 2tags/. A list learned from the posts seen here (lynxx/tags_learned.py)
        is used only for a module that has no other list, however recent it is: a list from a source always wins. When the module's
        list ships inside an extension that has no tag source (TagsDB.Bundled), the learned tags are served on top of it: the names it
        lacks are added (TagsDB.Additions), and no other file of <data dir>/tags outranks it (nothing but the learning writes there for
        such a module: a learned list that lost its stamp is not taken for a newer list). The same goes for a module whose own update
        merges a partial read into its list (TagSource.merge): the learned tags are served on top of the list in use, the shipped one
        or the merged one (TagsDB.LearningBases), and a file of <data dir>/tags replaces the shipped list only when it is the merge of
        that very list (its stamp, lynxx/tags_updater.py merged_onto), and then whatever their file times. Loaded lists stay in
        memory unless their file changed or they are listed in 'reload'"""
        from .extensions import data_dir
        env = os.environ.get('LYNXX_TAGS_DIR')
        env_dir = pathlib.Path(env).expanduser() if env else None
        learned_dir = data_dir() / 'tags'
        dirs = [d for d in (env_dir, learned_dir, pathlib.Path(__file__).resolve().parent.parent / '2tags') if d]
        reload = set(reload)
        files: dict[str, pathlib.Path] = {}
        learned: dict[str, pathlib.Path] = {}
        alias_file: pathlib.Path | None = None

        def consider(module: str, f: pathlib.Path, from_env: bool) -> None:
            cur = files.get(module)
            if cur is None or from_env or (cur.parent != env_dir and f.stat().st_mtime > cur.stat().st_mtime):
                files[module] = f

        installed = TagsDB._installed_manifests()
        ext_lists = TagsDB._extension_list_manifests(installed)
        no_source = {module for module, (_, m) in ext_lists.items() if _has_no_tag_source(m)}
        first: dict[str, object] = {}
        for m in installed:  # the first manifest of an abbreviation is the module's (as in extension_lists)
            first.setdefault(m.abbr, m)
        merging = {module for module, m in first.items() if _merges(m)}
        for module, (f, _) in ext_lists.items():
            consider(module, f, False)
        for d in reversed(dirs):
            if not d.is_dir():
                continue
            for f in d.glob('*_tags.json'):
                module = f.name[:-len('_tags.json')]
                if d == learned_dir and d != env_dir:
                    if _is_learned_list(module, f):
                        learned[module] = f
                        continue
                    if module in no_source and files.get(module) == ext_lists[module][0]:
                        continue  # no source writes there for it: a learned list whose stamp was lost (a quit while it was written)
                    if module in merging and module in ext_lists and files.get(module) == ext_lists[module][0]:
                        # only the merge of the list it ships replaces it, newer or older: a learned list that lost its stamp never
                        # does, nor a merge of the list a version before shipped (the shipped list is the base of the next one)
                        if _merge_of(f, files[module]):
                            files[module] = f
                        continue
                consider(module, f, d == env_dir)
            if alias_file is None and (alias := d / FILE_NAME_ALIASES).is_file():
                alias_file = alias
        bundled = {module: BundledList(f, m.id, m.version) for module, (f, m) in ext_lists.items()
                   if module in no_source and files.get(module) == f}
        # before the learned lists are added: a learned list is never a base, it is what goes on top
        bases = {module: b.path for module, b in bundled.items()}
        bases.update({module: files[module] for module in merging if module in files})
        additions = {module: f for module in bases if (f := _learned_additions(module) or learned.get(module)) is not None}
        for module, f in learned.items():
            files.setdefault(module, f)
        with TagsDB._lock:
            old_files, old_additions = dict(TagsDB.DBFiles), dict(TagsDB.Additions)
            TagsDB.DBFiles.clear()
            TagsDB.DBFiles.update(files)
            TagsDB.Bundled.clear()
            TagsDB.Bundled.update(bundled)
            TagsDB.LearningBases.clear()
            TagsDB.LearningBases.update(bases)
            TagsDB.Additions.clear()
            TagsDB.Additions.update(additions)
            if alias_file is not None and FILE_NAME_ALIASES not in TagsDB.AuxDBFiles:
                TagsDB.AuxDBFiles[FILE_NAME_ALIASES] = alias_file
            for module in {*old_files, *files, *reload}:
                if module in reload or files.get(module) != old_files.get(module) or additions.get(module) != old_additions.get(module):
                    TagsDB.unload(module)
            return len(TagsDB.DBFiles)

    @staticmethod
    def unload(module: str) -> None:
        """Drops a loaded list (and its cached browse results); a load in progress is discarded and repeated"""
        with TagsDB._lock:
            TagsDB._lists.pop(module, None)
            TagsDB._gens[module] = TagsDB._gens.get(module, 0) + 1

    @staticmethod
    def top_matches(module: str, prefix: str, limit: int | None = None, *, scan_max=0, tag_types: Iterable[TagTypes] = (),
                    ) -> list[tuple[str, TagInfo]]:
        """Tags starting with prefix, most popular first (equal counts: alphabetical). limit None: all.
        scan_max > 0: only the first scan_max matches (alphabetically) are ranked. tag_types: only tags of these types (among them)"""
        lst = TagsDB._load(module)
        return [lst.row(i) for i in lst.top(prefix, limit, scan_max, frozenset(int(t) for t in tag_types))]

    @staticmethod
    def type_counts(module: str, exclude_prefixes: Iterable[str] = ()) -> dict[TagTypes, int]:
        """Tags of each type in the module's list (counted once per loaded list), only the types it has. exclude_prefixes: names
        starting with one of them are not counted (the rows of each prefix's range, read each time)"""
        lst = TagsDB._load(module)
        counts = dict(lst.type_counts())
        for prefix in {p for p in exclude_prefixes if p}:
            r = lst.prefix_range(prefix)
            for code in lst.types[r.start:r.stop]:
                counts[code] -= 1
        return {_TYPE_BY_CODE[code]: n for code, n in counts.items() if n > 0}

    @staticmethod
    def _get_tag_matches(module: str, tag: str) -> list[tuple[str, TagInfo]]:
        # most popular tags first (TagInfo compares by tag type first, so sort by posts count explicitly)
        return TagsDB.top_matches(module, tag)

    @staticmethod
    def autocomplete_tag(module: str, tag: str) -> list[tuple[str, TagInfo]]:
        matches: list[tuple[str, TagInfo]] = []
        if not is_wtag(tag) and len(tag) >= TAG_AUTOCOMPLETE_LENGTH_MIN:
            base_matches = TagsDB.top_matches(module, tag, TAG_AUTOCOMPLETE_NUMBER_MAX)
            matches.extend((mtag[len(tag):], tag_info) for mtag, tag_info in base_matches)
        return matches

    @staticmethod
    def browse(module: str, prefix: str = '', *, offset=0, limit=100, min_posts=0, tag_types: Iterable[TagTypes] = (),
               by_posts=False, non_alpha=False) -> tuple[int, list[tuple[str, TagInfo]]]:
        """A-Z tag browser: returns (total matches, page of tags) starting with prefix, sorted alphabetically or most popular first (by_posts).
        non_alpha: tags not starting with a latin letter a-z instead of prefix. Filtered / sorted results are cached for paging"""
        lst = TagsDB._load(module)
        selected = lst.select(prefix, non_alpha, frozenset(int(t) for t in tag_types), min_posts, by_posts)
        return len(selected), [lst.row(i) for i in selected[offset:offset + limit]]

    @staticmethod
    def get_tag_info(module: str, tag: str) -> TagInfo | None:
        if not is_wtag(tag):
            return TagsDB.find_tag(module, tag)
        return None

    @staticmethod
    def find_tag(module: str, tag: str) -> TagInfo | None:
        """Exact lookup, '*' / '?' taken literally (danbooru has tags like '?' and '!?')"""
        lst = TagsDB._load(module)
        return lst.info(i) if (i := lst.find(tag)) >= 0 else None

    @staticmethod
    def fuzzy_matches(module: str, text: str, limit: int, tag_types: Iterable[TagTypes] = ()) -> list[tuple[str, TagInfo, int]]:
        """Typo tolerant suggestions (name, info, distance), closest first, then most popular. See _TagList.fuzzy.
        The search index is built on first use (~0.4 s for 766k tags) and dropped with the list. tag_types: only tags of these types"""
        lst = TagsDB._load(module)
        return [(lst.name(i), lst.info(i), dist) for dist, i in lst.fuzzy(text, limit, frozenset(int(t) for t in tag_types))]

    @staticmethod
    def has_wildcard_match(module: str, wtag: str) -> bool:
        return TagsDB._load(module).wildcard_any(wtag)

    @staticmethod
    def load_aux_file(filename: str, dest: dict[str, str]) -> None:
        assert filename not in TagsDB.AuxDB
        if filepath := TagsDB.AuxDBFiles.get(filename):
            with open(filepath, 'rt', encoding=UTF8) as auxfile:
                dest.update(json.load(auxfile))
                TagsDB.AuxDB[filename] = filepath


def is_wtag(tag: str) -> bool:
    return bool(re_wtag.fullmatch(tag))


def wildcard_pattern(wtag: str) -> re.Pattern[str]:
    """'*': any chars, '?': one char, the rest literally (use with fullmatch)"""
    return re.compile(''.join('.*' if c == '*' else '.' if c == '?' else re.escape(c) for c in wtag), re.DOTALL)


_aliases_lock = threading.Lock()


def load_tag_aliases() -> None:
    """Loads all_tag_aliases.json once (thread-safe: the sidecar resolves tags from several threads)"""
    with _aliases_lock:
        if not TAG_ALIASES:
            try:
                trace('Loading tag aliases...')
                TagsDB.try_locate_file_single(FILE_NAME_ALIASES)
                TagsDB.load_aux_file(FILE_NAME_ALIASES, TAG_ALIASES)
            except Exception:
                trace(format_exception('full'))
                trace(f'Error: Failed to load tag aliases from {TagsDB.AuxDBFiles.get(FILE_NAME_ALIASES, FILE_NAME_ALIASES)}')
                TAG_ALIASES.update({'': ''})

#
#
#########################################
