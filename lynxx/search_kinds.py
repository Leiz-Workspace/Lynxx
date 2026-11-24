# coding=UTF-8
"""
Lynxx: search kinds of each module (manifest 'search', Lynxx 2.0.11+, lynxx/modspec.py SearchSpec).
The query stays one string: a kind with a token is one tag of it ('artist:some_artist', 'user:Some.Name'), recognized by its token.
Registered for every module, the modules known to the core included (their tag syntax comes from core tables, their kinds,
metatag names and id search from their manifest). No site names here: everything site specific is in the manifests.
"""
#########################################
#
#

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import NamedTuple
from urllib import parse as url_parse

from .modspec import SearchKind, SearchSpec
from .module import ProcModule

__all__ = (
    'KindMatch',
    'ModuleSearch',
    'find_kind',
    'id_search_supported',
    'is_listing_tag',
    'is_module_meta',
    'is_site_order_tag',
    'kind_of',
    'kind_tokens',
    'kind_value_ok',
    'module_search',
    'register_module_search',
    'split_kind',
)

re_meta_name_prefix = re.compile(r'^([a-z_][a-z\d_]*)(?::|=|[<>])')
'''a metatag's name: declared names may hold digits ('sha512_hash', modspec re_meta_name_decl)'''


class KindMatch(NamedTuple):
    """A kind found in a query: its id ('artist'...), its token ('artist:') and the value after it, url escapes undone"""
    id: str
    token: str
    value: str


class ModuleSearch:
    """Compiled 'search' of one module"""
    __slots__ = ('by_token', 'id_search', 'kinds', 'meta_names', 'patterns')

    def __init__(self, spec: SearchSpec, fav: re.Pattern[str] | None = None, pool: re.Pattern[str] | None = None) -> None:
        self.kinds: tuple[SearchKind, ...] = tuple(spec.kinds)
        self.by_token: dict[str, SearchKind] = {k.token: k for k in self.kinds if k.token}
        self.patterns: dict[str, re.Pattern[str]] = {}
        '''token -> the pattern a tag of that kind must match to be accepted without the core's tag characters'''
        for k in self.by_token.values():
            if k.use:
                rex = fav if k.use == 'fav' else pool
                if rex is not None and rex.pattern not in ('^$', ''):
                    self.patterns[k.token] = rex
            else:
                self.patterns[k.token] = re.compile(f'^-?{re.escape(k.token)}(?:{k.value})$')
        self.meta_names = frozenset(spec.metaNames)
        self.id_search = spec.idSearch

    def kind_of(self, tag: str) -> SearchKind | None:
        """The kind whose token starts the tag ('-' ignored). Tokens end with ':' or '=' and have neither before: one at most"""
        t = tag.lstrip('-')
        cut = min((i for i in (t.find(':'), t.find('=')) if i >= 0), default=-1)
        return self.by_token.get(t[:cut + 1]) if cut > 0 else None


_REGISTRY: dict[int, ModuleSearch] = {}


def register_module_search(proc_id: int, spec: SearchSpec, fav: re.Pattern[str] | None = None, pool: re.Pattern[str] | None = None,
                           ) -> None:
    """Registers (or replaces) the search kinds of a module. fav / pool: the module's registered favorites / pool regexes
    (lynxx/tags_parser.py RE_FAVS / RE_POOLS), which the 'use' kinds go through"""
    _REGISTRY[proc_id] = ModuleSearch(spec, fav, pool)


def module_search(proc_id: int | None = None) -> ModuleSearch | None:
    return _REGISTRY.get(ProcModule.value() if proc_id is None else proc_id)


def _search_of(module: int | str | None) -> ModuleSearch | None:
    """The registry entry of module (None: the current module). An abbr that is not registered (not installed, not loaded) has
    none: no kinds, no metatag names, id search on (never the current module's)"""
    if module is None:
        return module_search()
    if isinstance(module, str):
        pid = ProcModule.PROC_MODULES_BY_NAME.get(module)
        return None if pid is None else _REGISTRY.get(pid)
    return _REGISTRY.get(module)


def kind_tokens(module: int | str | None = None) -> tuple[str, ...]:
    """Tokens of the module's kinds ('artist:', 'user:'...)"""
    ms = _search_of(module)
    return tuple(ms.by_token) if ms is not None else ()


def kind_of(tag: str, module: int | str | None = None) -> SearchKind | None:
    """Kind of a tag of the query of a module (default: the current module), None for anything else"""
    ms = _search_of(module)
    return ms.kind_of(tag) if ms is not None else None


def kind_value_ok(tag: str, module: int | str | None = None) -> SearchKind | None:
    """The kind of the tag when its value is what the kind declares (validation without the core's tag characters)"""
    ms = _search_of(module)
    if ms is None or (kind := ms.kind_of(tag)) is None:
        return None
    rex = ms.patterns.get(kind.token)
    return kind if rex is not None and rex.fullmatch(tag) else None


def is_listing_tag(tag: str, module: int | str | None = None) -> bool:
    """A positive tag of a listing kind (the token selects another site listing)"""
    return not tag.startswith('-') and bool((k := kind_of(tag, module)) and k.listing)


def is_site_order_tag(tag: str, module: int | str | None = None) -> bool:
    """A positive tag of a kind whose results come in the site's order (a listing, or order 'site')"""
    return not tag.startswith('-') and bool((k := kind_of(tag, module)) and (k.listing or k.order == 'site'))


def is_module_meta(tag: str, module: int | str | None = None) -> bool:
    """A tag of one of the module's kinds or with one of its declared metatag names ('vote:3:bob'), '-' ignored"""
    ms = _search_of(module)
    if ms is None:
        return False
    if ms.kind_of(tag) is not None:
        return True
    m = re_meta_name_prefix.match(tag.lstrip('-'))
    return bool(m and m.group(1) in ms.meta_names)


def id_search_supported(module: int | str | None = None) -> bool:
    """False when the module's manifest says 'id:' searches do not work there (search.idSearch false)"""
    ms = _search_of(module)
    return ms.id_search if ms is not None else True


def find_kind(tags: Iterable[str], module: int | str | None = None) -> KindMatch | None:
    """The kind of a query (its tags, escaped or not): the listing kind if there is one, else the first positive tag of a kind"""
    first: KindMatch | None = None
    for t in tags:
        if t.startswith('-') or (k := kind_of(t, module)) is None:
            continue
        match = KindMatch(k.id, k.token, url_parse.unquote(t[len(k.token):]))
        if k.listing:
            return match
        first = first or match
    return first


re_concat = re.compile(r'[+ ]')


def split_kind(query: str, module: int | str | None = None) -> tuple[KindMatch | None, str]:
    """(kind, the rest of the query) of a task string as the downloader gets it ('artist:some_artist+order:post_date'): tags joined
    by the module's concat char ('+' or ' ', a literal '+' of a tag is '%2b'), characters with a meaning in urls escaped
    (tags_parser.URL_ESCAPES: the kind's value is unescaped). The listing kind if there is one, else the first tag of a kind;
    tags inside an 'or' group are not looked at. (None, query) when the query has no kind"""
    if not query:
        return None, query
    sep = m.group(0) if (m := re_concat.search(query)) else '+'
    parts = re_concat.split(query)
    found: tuple[int, SearchKind] | None = None
    depth = 0
    for i, part in enumerate(parts):
        if part == '(':
            depth += 1
        elif part == ')':
            depth = max(0, depth - 1)
        elif not depth and not part.startswith('-') and (k := kind_of(part, module)) is not None:
            if k.listing:
                found = (i, k)
                break
            found = found or (i, k)
    if found is None:
        return None, query
    i, k = found
    return KindMatch(k.id, k.token, url_parse.unquote(parts[i][len(k.token):])), sep.join(parts[:i] + parts[i + 1:])

#
#
#########################################
