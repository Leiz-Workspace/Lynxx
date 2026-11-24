# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

import re
from collections.abc import Iterable
from urllib import parse as url_parse

from .defines import (
    TAGS_STRING_LENGTH_MAX_BB,
    TAGS_STRING_LENGTH_MAX_DB,
    TAGS_STRING_LENGTH_MAX_EN,
    TAGS_STRING_LENGTH_MAX_RN,
    TAGS_STRING_LENGTH_MAX_RP,
    TAGS_STRING_LENGTH_MAX_RS,
    TAGS_STRING_LENGTH_MAX_RX,
    TAGS_STRING_LENGTH_MAX_XB,
)
from .logger import trace
from .module import ProcModule
from .network import thread_exit
from .search_kinds import is_listing_tag, is_module_meta
from .vcs import __LYNXX_DEBUG__

__all__ = ('extract_neg_and_groups', 'register_module_limits', 'split_tags_into_tasks')

MAX_STRING_LENGTHS = {
    ProcModule.RX: TAGS_STRING_LENGTH_MAX_RX,
    ProcModule.RN: TAGS_STRING_LENGTH_MAX_RN,
    ProcModule.RS: TAGS_STRING_LENGTH_MAX_RS,
    ProcModule.RP: TAGS_STRING_LENGTH_MAX_RP,
    ProcModule.EN: TAGS_STRING_LENGTH_MAX_EN,
    ProcModule.XB: TAGS_STRING_LENGTH_MAX_XB,
    ProcModule.BB: TAGS_STRING_LENGTH_MAX_BB,
    ProcModule.DB: TAGS_STRING_LENGTH_MAX_DB,
}
MAX_NEGATIVE_TAGS = {
    ProcModule.RX: (0, False),
    ProcModule.RN: (0, False),
    ProcModule.RS: (0, False),
    ProcModule.RP: (3, False),
    ProcModule.EN: (40, False),
    ProcModule.XB: (0, False),
    ProcModule.BB: (0, False),
    ProcModule.DB: (2, False),
}
MAX_WILDCARDS = {
    ProcModule.RX: 0,
    ProcModule.RN: 0,
    ProcModule.RS: 0,
    ProcModule.RP: 0,
    ProcModule.EN: 1,
    ProcModule.XB: 0,
    ProcModule.BB: 0,
    ProcModule.DB: 0,
}
FREE_META_PREFIXES: dict[int, tuple[str, ...]] = {
    # metatags which do not count towards module's max tags limit (verified against live API)
    ProcModule.DB: ('rating:', 'id:', 'date:', 'score:', 'status:', 'parent:'),
}
LOCAL_POSITIVE_OVERFLOW: dict[int, bool] = {
    # extra positive tags (above max tags limit) are checked locally instead of failing the search
    ProcModule.DB: True,
}
re_negative_and_group = re.compile(r'^-\(([^,]+(?:,[^,]+)+)\)$')
KNOWN_META_NAMES = frozenset((
    'id', 'score', 'rating', 'user', 'md5', 'width', 'height', 'parent', 'source', 'updated', 'sort', 'order', 'date', 'status',
    'fav', 'favorited_by', 'favoritedby', 'ordfav', 'pool', 'ordpool', 'set', 'poster', 'filetype', 'filesize', 'limit', 'age', 'mpixels',
    'ratio', 'favcount', 'tagcount', 'gentags', 'arttags', 'chartags', 'copytags', 'metatags', 'commenter', 'comm', 'noter', 'approver',
    'is', 'has', 'pixiv', 'pixiv_id', 'search', 'upvote', 'downvote', 'random', 'duration', 'child', 'appealer', 'flagger', 'disapproved',
    'unaliased', 'exif', 'ai', 'embedded', 'commentary', 'note', 'notes', 'comments', 'description', 'type', 'delreason', 'deletedby',
))
'''metatag names: a tag like 'honkai:_star_rail' or 'saber_(fate)' is a plain tag, 'id:>5' or '-user:bob' is a metatag'''
re_meta_name = re.compile(r'^([a-z_][a-z\d_]*)(?::|=|[<>])')


def _is_meta(tag: str) -> bool:
    """A metatag of the current module: a core known name, or a kind token / metatag name its manifest declares ('search')"""
    m = re_meta_name.match(tag.lstrip('-'))
    return bool(m and m.group(1) in KNOWN_META_NAMES) or is_module_meta(tag)


def _is_plain_positive(tag: str) -> bool:
    """Positive tags that can be checked locally against post tags: no groups, wildcards or metatags"""
    return bool(tag) and not tag.startswith(('-', '(')) and '*' not in tag and not _is_meta(tag)


def register_module_limits(proc_id: int, max_string_len: int, max_tags: int, max_tags_separate: bool, max_wildcards: int,
                           free_meta_prefixes: Iterable[str] = (), local_positive_overflow=False) -> None:
    """Registers tag limits of a module provided by an extension (modules known to the core keep their predefined limits)"""
    if proc_id in MAX_STRING_LENGTHS:
        return
    MAX_STRING_LENGTHS[proc_id] = max_string_len
    MAX_NEGATIVE_TAGS[proc_id] = (max_tags, max_tags_separate)
    MAX_WILDCARDS[proc_id] = max_wildcards
    if free_meta_prefixes:
        FREE_META_PREFIXES[proc_id] = tuple(free_meta_prefixes)
    if local_positive_overflow:
        LOCAL_POSITIVE_OVERFLOW[proc_id] = True


def _is_counted_tag(tag: str) -> bool:
    """Takes one of the module's tag slots: not a free metatag (freeMetaPrefixes), not the token of a listing kind"""
    free = FREE_META_PREFIXES.get(ProcModule.value(), ())
    return not (free and tag.lstrip('-').startswith(free)) and not is_listing_tag(tag)


def _move_all_local(tags_list: list[str], required_out: list[str]) -> list[str]:
    """A listing kind ('artist:x' -> the site's /models/x/ listing) cannot be combined with tags on the site: every plain positive
    tag goes to the local 'required tags' filter, every plain negative one is returned (for the local negative group).
    Metatags, wildcards and the listing token stay in the site query"""
    negatives: list[str] = []
    for tag in list(tags_list):
        if _is_plain_positive(tag):
            tags_list.remove(tag)
            required_out.append(url_parse.unquote(tag))
        elif tag.startswith('-') and not tag.startswith('-(') and not _is_meta(tag):
            tags_list.remove(tag)
            negatives.append(url_parse.unquote(tag[1:]))
    if required_out or negatives:
        listing = next((t for t in tags_list if is_listing_tag(t)), '')
        trace(f'Info: {listing} is a site listing: {len(required_out) + len(negatives):d} tag(s) will be checked locally: '
              f'\'{" ".join([*required_out, *(f"-{t}" for t in negatives)])}\'')
    return negatives


def _count_tags(tags: Iterable[str]) -> int:
    return sum(1 for t in tags if _is_counted_tag(t))


def _pick_local_required_tags(tags_list: list[str], limit: int, required_out: list[str]) -> None:
    """Keeps the rarest positive plain tags for server-side search, moves the rest to local 'required tags' filter"""
    from .tagsdb import TagsDB  # local import: tagsdb is optional and heavy
    positives = [t for t in tags_list if _is_counted_tag(t) and _is_plain_positive(t)]
    # plain negative tags are not counted here: they will be moved into local negative group if there is no room left for them.
    # negative metatags ('-user:bob') must stay in the website query, they take a slot like any other non-plain tag
    others_count = sum(1 for t in tags_list if _is_counted_tag(t) and t not in positives and (not t.startswith('-') or _is_meta(t)))
    keep_count = max(0, limit - others_count)
    if len(positives) <= keep_count:
        return

    def popularity(tag: str) -> int:
        try:
            info = TagsDB.get_tag_info(ProcModule.name(), url_parse.unquote(tag)) if not TagsDB.is_empty() else None
        except Exception:
            info = None
        return info.posts_count if info else 0  # unknown tags are treated as the rarest

    by_rarity = sorted(positives, key=popularity)
    for tag in by_rarity[keep_count:]:
        tags_list.remove(tag)
        required_out.append(url_parse.unquote(tag))
    trace(f'Info: {ProcModule.name().upper()} tag limit is {limit:d}, searching by \'{" ".join(by_rarity[:keep_count])}\', '
          f'{len(required_out):d} tag(s) will be checked locally: \'{" ".join(required_out)}\'')


MAX_SPLIT_TASKS = 64
'''OR groups split into separate searches multiply: (a~b) (c~d) (e~f) = 8 searches. The original code had no cap (12 groups of 2 = 4096 searches)'''


def split_tags_into_tasks(tag_groups_arr: Iterable[str], cc: str, sc: str, split_always: bool) -> list[str]:
    """
    Converts natively not processible tags into processible tags combinations\n
    Ex. ['(+a+~+b+)', '(+c+~+d+)', 'x', '-y'] => ['a+c+x+-y', 'a+d+x+-y', 'b+c+x+-y', 'b+d+x+-y']\n
    :param tag_groups_arr: list of tags ex. ['a', 'b', '(+c+~+d+)']
    :param cc: tags concatenation char (ex. 'a+b' => cc = '+')
    :param sc: meta tag type-value separator char (ex. 'id:123' => sc = ':')
    :param split_always: unconditionally separate all 'or' groups
    :return: list of fully formed tags directly injectable into request query template, len is up to max_or_group_len**2
    """
    new_tags_str_arr: list[str] = []
    or_tags_to_append: list[list[str]] = []
    has_negative = False
    for g_tags in tag_groups_arr:
        splitted = False
        add_list: list[str] = g_tags[2:-2].split('+~+') if len(g_tags) >= len('(+_+~+_+)') else []
        if len(add_list) > 1:
            do_split = split_always
            for add_s in add_list:
                add_s_negative = add_s.startswith('-')
                add_s_meta = sc in add_s
                if add_s_negative is True or split_always is True or add_s_meta is True:
                    assert (add_s_negative & add_s_meta) is False  # see tags_parser.py::split_or_group(str)
                    do_split = True
                    has_negative |= add_s_negative
            if do_split:
                splitted = True
                or_tags_to_append.append(add_list)
        if not splitted:
            new_tags_str_arr.append(g_tags)
    if len(or_tags_to_append) > 0:
        if has_negative and len(new_tags_str_arr) == 0:
            thread_exit('Error: -tag in \'or\' group found, but no +tags! Cannot search by only -tags', -701)
        combinations = 1
        for or_tags_list in or_tags_to_append:
            combinations *= len(or_tags_list)
        if combinations > MAX_SPLIT_TASKS:
            thread_exit(f'Error: {len(or_tags_to_append):d} \'or\' group(s) would be split into {combinations:d} separate searches '
                        f'(limit {MAX_SPLIT_TASKS:d}). Use fewer or smaller \'or\' groups', -702)
        tags_multi_list = [f'{cc.join(new_tags_str_arr)}' if len(new_tags_str_arr) > 0 else '']
        for or_tags_list in reversed(or_tags_to_append):
            toapp: list[str] = []
            for or_tag in or_tags_list:
                for tags_string in tags_multi_list:
                    toapp.append(f'{or_tag}{f"{cc}{tags_string}" if len(tags_string) > 0 else ""}')
            tags_multi_list = toapp
        contains_msg = 'meta/negative tag(s) in ' if not split_always else ''
        trace(f'\nWarning (W1): {contains_msg}{len(or_tags_to_append):d} \'or\' group(s) found. '
              f'Splitting into {len(tags_multi_list):d} tasks.')
        return tags_multi_list
    return [cc.join(tag_groups_arr)]


def extract_neg_and_groups(tags_str: str, split_always: bool, required_out: list[str] | None = None,
                           tags_limit: int = 0) -> tuple[list[str], list[list[re.Pattern[str]]]]:
    """
    Separates tags string into fully formed tags and negative tag patterns\n
    Ex. 'a b (+c+~+d+) -(ff,gg)' => (['a', 'b' , '(+c+~+d+)'], [[re.compile(r'^ff$'), re.compile(r'^gg$')]])\n
    :param tags_str: provided string of tags separated by space
    :param split_always: unconditionally separate all 'or' groups
    :param required_out: if provided, receives positive tags that exceed module's tags limit and must be checked locally
    :param tags_limit: account tags limit (-tags_limit) replacing the module default, 0: module default
    :return: 1) list of fully-formed tags without negative groups, 2) list of zero or more tag pattern lists
    """
    def form_plist(neg_tags_group: str) -> list[re.Pattern] | None:
        def esc(s: str) -> str:
            for c in '.[]()-+':
                s = s.replace(c, f'\\{c}')
            return s.replace('?', '.').replace('*', '.*')
        ngr = re_negative_and_group.fullmatch(neg_tags_group)
        return [re.compile(rf'^{esc(url_parse.unquote(s))}$') for s in ngr.group(1).split(',')] if ngr else None

    parsed: list[list[re.Pattern]] = []
    tags_list = tags_str.split(' ')
    tgi: int
    for tgi in reversed(range(len(tags_list))):
        tag_group = tags_list[tgi]
        if len(tag_group) < len('-(a,b)') or not tag_group.startswith('-('):
            continue
        if plist := form_plist(tag_group):
            parsed.append(plist)
            del tags_list[tgi]

    if required_out is not None and any(is_listing_tag(t) for t in tags_list):
        if listing_negatives := _move_all_local(tags_list, required_out):
            plist = form_plist(f'-(*,{"|".join(listing_negatives)})')
            assert plist is not None
            parsed.append(plist)

    total_len = len(tags_list) - 1  # concat chars count
    for t in tags_list:  # + length of each tag
        total_len += max(len(ogt) for ogt in t.split('+~+')) if split_always and t.startswith('(+') else len(t)

    max_tags_all, max_is_separate = MAX_NEGATIVE_TAGS[ProcModule.value()]
    if tags_limit > 0 and max_tags_all > 0:
        max_tags_all = tags_limit  # -tags_limit: the account allows more (or fewer) tags than the module default
    max_wtags_all = MAX_WILDCARDS[ProcModule.value()]
    max_string_len = MAX_STRING_LENGTHS[ProcModule.value()]
    neg_tags_list_all = list(filter(lambda x: x.startswith('-') and _is_counted_tag(x), tags_list))
    w_tags_list_all = list(filter(lambda x: '*' in x, tags_list))
    max_ntags = max(0, max_tags_all - (0 if max_is_separate else (_count_tags(tags_list) - _count_tags(neg_tags_list_all))),
                    ) if max_tags_all else 10**9
    max_wtags = max_wtags_all or 10**9

    def tags_fixed() -> None:
        return total_len <= max_string_len and len(neg_tags_list_all) <= max_ntags and len(w_tags_list_all) <= max_wtags

    if required_out is not None and max_tags_all and LOCAL_POSITIVE_OVERFLOW.get(ProcModule.value(), False):
        # positive tags first: negative tags are cheaper to filter locally but only if there is room for at least one of them
        moved_before = len(required_out)  # a listing kind's tags are not in tags_list any more (not in total_len)
        _pick_local_required_tags(tags_list, max_tags_all, required_out)
        for rtag in required_out[moved_before:]:
            total_len -= len(rtag) + 1
        max_ntags = max(0, max_tags_all - (_count_tags(tags_list) - _count_tags(neg_tags_list_all)))

    neg_tags_list: list[str] = []
    if not tags_fixed():
        trace('Warning (W3): either total tags length, maximum negative tags count or maximum wildcarded tags count '
              'exceeds acceptable limit, trying to extract negative tags into negative group...')
        # first pass: wildcarded negative tags - chance to ruin alias is lower (rx)
        # second pass: any negative tags
        for wildcardpass in (True, False):
            ti: int
            for ti in reversed(range(len(neg_tags_list_all))):
                if tags_fixed():
                    break
                ntag = neg_tags_list_all[ti]
                if wildcardpass is True and ntag.rfind('*') == -1:
                    continue
                if _is_meta(ntag):
                    continue  # '-user:bob' cannot be checked against post tags: it must stay in the website query
                neg_tags_list.append(url_parse.unquote(ntag[1:]))
                total_len -= len(ntag) + 1
                del neg_tags_list_all[ti]
                del tags_list[tags_list.index(ntag)]
                if wildcardpass is True and ntag in w_tags_list_all:
                    del w_tags_list_all[w_tags_list_all.index(ntag)]
        if not tags_fixed():
            thread_exit(f'Fatal: too many tags for {ProcModule.name().upper()} even after moving -tags to local filters '
                        f'(negative metatags like -user:x cannot be checked locally). Remove some tags. Aborting...', -609)
        assert len(neg_tags_list) > 0, 'No negative tags extracted!'
        extracted_neg_group_str = f'-(*,{"|".join(reversed(neg_tags_list))})'
        trace(f'Info: extracted negative group: {extracted_neg_group_str}')
        plist = form_plist(extracted_neg_group_str)
        assert plist is not None
        parsed.append(plist)
    if 0 < max_tags_all < _count_tags(tags_list) - (len(neg_tags_list_all) if max_is_separate else 0):
        thread_exit(f'Fatal: max tags exceeded for {ProcModule.name().upper()} '
                    f'({_count_tags(tags_list):d} > {max_tags_all:d}), final tags: \'{" ".join(tags_list)}\'')

    if __LYNXX_DEBUG__ and len(neg_tags_list) > 0:
        trace(f'Resulting args: {" ".join(tags_list)}')

    return tags_list, parsed

#
#
#########################################
