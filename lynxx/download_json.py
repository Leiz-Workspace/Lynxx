# coding=UTF-8
"""
Lynxx: base class of JSON API boorus (e621, Moebooru, Philomena, Szurubooru... see lynxx/extgen/templates.py)
A subclass declares the site, the search url and how one post of the API maps to a JsonPost; the base class does the rest:
counting (from the page, a count endpoint or a binary search over pages), the '<post_id="…" …>' item strings understood by
the core helpers, file / sample / preview urls, dates, tags, unavailable files. Extensions (incl. generated drafts) may
derive from it: lynxx.extgen.policy allows this module.
"""
#########################################
#
#

from __future__ import annotations

import datetime
import json
import re
from collections.abc import MutableSet
from dataclasses import dataclass, field
from multiprocessing.dummy import current_process
from typing import Any, Never
from urllib import parse as url_parse

from bs4 import BeautifulSoup

from .defines import FILE_NAME_FULL_MAX_LEN, FMT_DATE, KNOWN_EXTENSIONS_VID, DownloadModes, ItemInfo, MediaKind, media_kind_by_ext
from .download import Downloader
from .logger import trace
from .module import ProcModule
from .network import LYNXX_USER_AGENT, thread_exit
from .rex import re_item_info_part_xml, re_orig_file_link, re_tags_exclude_rx, re_tags_to_process_rx
from .search_kinds import module_search

__all__ = ('DownloaderJsonApi', 'JsonPost')

ITEM_INFO_FIELDS = {'file_url': 'ext', 'post_id': 'id', 'created_at': 'date'}
re_ext_attr = re.compile(r' ext="([a-z\d]{2,5})"')
TAG_NO_FILE_URL = 'json_file_unavailable'
'''internal marker: the API gives no file url for this post (hidden / restricted / deleted): skipped, never rebuilt (rule R6)'''


@dataclass
class JsonPost:
    """One post as the core needs it. created_at: ISO 8601 or unix seconds. Urls may be relative to the site"""
    id: str
    file_url: str
    tags: list[str] = field(default_factory=list)
    created_at: str | int | float = ''
    sample_url: str = ''
    preview_url: str = ''
    md5: str = ''
    width: int = 0
    height: int = 0
    score: int | str = 0
    rating: str = ''
    source: str = ''
    parent_id: str = ''
    has_children: bool = False
    ext: str = ''
    '''file extension when the file url has none (ex. shimmie '/_images/<hash>/<name>')'''


def _attr(value: object) -> str:
    return str(value if value is not None else '').replace('"', '\'').replace('\n', ' ')


class DownloaderJsonApi(Downloader):
    """Override the constants and _search_url / _posts / _post (and _count / _count_url when the API can count)"""
    SITE = ''
    '''site root, 'https://host/' '''
    ABBR = ''
    ITEMS_PER_PAGE = 100
    MAX_PAGES = 1000
    '''deepest page the API serves, 0: no limit'''
    TAGS_CONCAT = '+'
    ACCEPT = 'application/json'

    def __init__(self) -> None:
        super().__init__()
        self._task_tags = ''

    # ----- what a subclass provides

    def _search_url(self, tags: str, page: int, limit: int) -> str:
        """search page 'page' (0 based) of 'tags' (already joined with TAGS_CONCAT), 'limit' posts per page"""
        raise NotImplementedError

    def _posts(self, data: Any) -> list[dict] | None:
        """the posts of a parsed search page, None if the page is not a search result (api error)"""
        raise NotImplementedError

    def _post(self, p: dict) -> JsonPost | None:
        """one post of _posts(), None to ignore it"""
        raise NotImplementedError

    def _count(self, data: Any) -> int | None:
        """total results carried by a parsed search page, None if the API does not tell"""
        return None

    def _count_url(self, tags: str) -> str:
        """separate count endpoint ('' if none): its response goes to _count_from"""
        return ''

    def _count_from(self, raw: bytes) -> int | None:
        return None

    def _query_tags(self, tags: str) -> str:
        """last chance to adapt the tags string to the site syntax"""
        return tags

    def _cursor_url(self, tags: str, cursor: int | None, ascending: bool, limit: int) -> str:
        """search page sorted by id: the posts right below the cursor id (newest first) or, ascending, right above it (oldest
        first); cursor None: the first page. '' (default): the API cannot, results deeper than MAX_PAGES are refused"""
        return ''

    def _items_per_page(self) -> int:
        """SDK 1.2: posts per search page of the query being run. The page size its kind declares (manifest search.kinds[].pageSize,
        self.current_kind), else ITEMS_PER_PAGE. Used for page addresses, the search depth and the count"""
        if (kind := self.current_kind) is not None:
            ms = module_search(ProcModule.PROC_MODULES_BY_NAME.get(self._get_module_abbr()))
            spec = ms.by_token.get(kind.token) if ms is not None else None
            if spec is not None and spec.pageSize > 0:
                return spec.pageSize
        return self.ITEMS_PER_PAGE

    def _max_pages(self) -> int:
        """SDK 1.2: deepest page the site serves for the query being run (0: no limit), MAX_PAGES by default"""
        return self.MAX_PAGES

    # ----- module description

    def _get_module_specific_default_headers(self) -> dict[str, str]:
        return {'User-Agent': LYNXX_USER_AGENT, 'Accept': self.ACCEPT}

    def _get_module_specific_default_cookies(self) -> dict[str, str]:
        return {}

    def _get_min_request_interval(self) -> float:
        return 1.0

    def _parses_raw_pages(self) -> bool:
        return True

    def _is_pool_search_conversion_required(self) -> bool:
        return False

    def _is_fav_search_conversion_required(self) -> bool:
        return False

    def _is_fav_search_single_step(self) -> bool:
        return True

    def _supports_native_id_filter(self) -> bool:
        return True

    def _get_id_bounds(self) -> Never:
        raise NotImplementedError

    def _get_sitename(self) -> str:
        return self.SITE

    def _get_module_abbr(self) -> str:
        return self.ABBR

    def _get_module_abbr_p(self) -> str:
        return f'{self.ABBR}_'

    def _get_items_per_page(self) -> int:
        return self._items_per_page()

    def _get_max_search_depth(self) -> int:
        return self._max_pages() * self._items_per_page()

    def _form_item_string_manually(self, *ignored) -> Never:
        raise NotImplementedError

    def _is_search_overload_page(self, raw_html_page: BeautifulSoup) -> bool:
        return False

    def get_re_tags_to_process(self):
        return re_tags_to_process_rx

    def get_re_tags_to_exclude(self):
        return re_tags_exclude_rx

    def _get_tags_concat_char(self) -> str:
        return self.TAGS_CONCAT

    def _get_idval_equal_seaparator(self) -> str:
        return ':'

    def _split_or_group_into_tasks_always(self) -> bool:
        return True

    def _can_extract_item_info_without_fetch(self) -> bool:
        return True

    def _consume_custom_module_tags(self, tags: str) -> str:
        return self._query_tags(tags) if tags else tags

    # ----- addresses

    def _form_tags_search_address(self, tags: str, maxlim: int | None = None) -> str:
        self._task_tags = tags
        return self._search_url(tags, 0, maxlim or self._items_per_page())

    def _form_page_num_address(self, n: int) -> str:
        return self._search_url(self._task_tags, n, self._items_per_page())

    # id cursor (streaming pipeline, default sorting): used when the results are deeper than MAX_PAGES

    def _supports_id_cursor(self) -> bool:
        return bool(self._cursor_url('x', 1, False, 1))

    def _form_cursor_page_address(self, cursor: int | None, ascending: bool) -> str:
        return self._cursor_url(self.cursor_base_tags, cursor, ascending, self._items_per_page())

    def _prefer_id_cursor(self, count: int | None) -> bool:
        depth = self._get_max_search_depth()
        return count is None or 0 < depth < count

    def _cursor_first_page_ok(self, ids: list[int], ascending: bool) -> bool:
        return ids == sorted(ids, reverse=not ascending)  # the site really sorted by id

    def _absolute(self, url: str) -> str:
        url = str(url or '')
        if url.startswith('//'):
            return f'https:{url}'
        return url_parse.urljoin(self.SITE, url) if url and not url.startswith(('https://', 'http://')) else url

    # ----- search

    def _parse(self, raw: bytes) -> Any:
        """a search page as data for _posts / _count: json by default; override for XML or HTML listings (None: unreadable)"""
        try:
            return json.loads(raw)
        except ValueError:
            return None

    def _page_error(self, raw: bytes) -> str:
        data = self._parse(raw)
        if data is None:
            return 'invalid json response (bot challenge or truncated response?)'
        return '' if self._posts(data) is not None else 'unexpected api response'

    def _page_len(self, page: int, tries: int | None) -> int:
        raw = self.fetch_html(self._form_page_num_address(page), tries=tries, do_cache=True)
        if raw is None:
            thread_exit('ERROR: GetItemsQueSize: unable to retreive html', code=-445)
        posts = self._posts(self._parse(raw.raw))
        if posts is None:
            thread_exit(f'ERROR: [{self.ABBR.upper()}] {self._page_error(raw.raw)}', code=-447)
        return len(posts)

    def _get_items_query_size_or_html(self, url: str, tries: int | None = None) -> int:
        if count_url := self._count_url(self._task_tags):
            raw_count = self.fetch_html(count_url, tries=tries)
            if raw_count is not None and isinstance(count := self._count_from(raw_count.raw), int):
                return count
        raw = self.fetch_html(self._form_page_num_address(0), tries=tries, do_cache=True)
        if raw is None:
            thread_exit('ERROR: GetItemsQueSize: unable to retreive html', code=-444)
        data = self._parse(raw.raw)
        posts = self._posts(data) if data is not None else None
        if posts is None:
            thread_exit(f'ERROR: [{self.ABBR.upper()}] {self._page_error(raw.raw)}', code=-447)
        if isinstance(count := self._count(data), int):
            return count
        per_page = self._items_per_page()
        if len(posts) < per_page:
            return len(posts)
        # no count: binary search for the last non-empty page (~log2(MAX_PAGES) requests)
        trace(f'Info: [{self.ABBR.upper()}] post count is unavailable, looking for the last page...')
        max_pages = self._max_pages() if self._max_pages() > 0 else 1000
        lo, hi = 0, max_pages - 1
        if self._page_len(hi, tries) >= per_page:
            return max_pages * per_page
        while hi - lo > 1:  # page lo is full, page hi is not
            mid = (lo + hi) // 2
            if self._page_len(mid, tries) >= per_page:
                lo = mid
            else:
                hi = mid
        return hi * per_page + self._page_len(hi, tries)

    def _get_all_post_tags(self, raw_html_page: bytes) -> list:
        data = self._parse(raw_html_page)
        posts = self._posts(data) if data is not None else None
        if posts is None:
            trace(f'ERROR: [{self.ABBR.upper()}] {self._page_error(raw_html_page)}', True)
            return []
        out: list[str] = []
        for p in posts:
            post = self._post(p) if isinstance(p, dict) else None
            if post is None or not str(post.id).isdigit():
                continue
            out.append(self._item_string(post))
        return out

    def _item_string(self, post: JsonPost) -> str:
        tags = [str(t).strip().replace(' ', '_') for t in post.tags if str(t).strip()]
        furl = self._absolute(post.file_url)
        audio = media_kind_by_ext(self._file_ext(furl) or str(post.ext)) == MediaKind.AUDIO
        # an audio file is never a sample / thumbnail: without a cover image the preview shows a placeholder
        surl = self._absolute(post.sample_url) or ('' if audio else furl)
        purl = self._absolute(post.preview_url) or ('' if audio else surl)
        if not furl:
            tags.append(TAG_NO_FILE_URL)
            furl = surl = purl = f'{self.SITE}#unavailable.none'
        ext = str(post.ext or '').lower().lstrip('.')
        ext_attr = f' ext="{ext}"' if ext and furl and not self._file_ext(furl) and re.fullmatch(r'[a-z\d]{2,5}', ext) else ''
        created = post.created_at
        if isinstance(created, (int, float)) or (isinstance(created, str) and created.isdigit()):
            created = datetime.datetime.fromtimestamp(int(created), datetime.UTC).isoformat()
        return (
            f'<post_id="{_attr(post.id)}" height="{int(post.height or 0)}" width="{int(post.width or 0)}" md5="{_attr(post.md5)}" '
            f'file_url="{_attr(furl)}"{ext_attr} sample_url="{_attr(surl)}" preview_url="{_attr(purl)}" created_at="{_attr(created)}" '
            f'score="{_attr(post.score or 0)}" has_children="{str(bool(post.has_children)).lower()}" '
            f'parent_id="{_attr(post.parent_id)}" rating="{_attr(post.rating)}" source="{_attr(post.source)}" '
            f'tags="{_attr(" ".join(tags))}">'
        )

    # ----- items

    def _local_addr_from_string(self, h: str) -> str:
        return h

    def _extract_id(self, addr: str) -> str:
        id_idx = addr.find('post_id="') + len('post_id="')
        return addr[id_idx:addr.find('"', id_idx + 1)]

    def _get_item_html(self, h: str) -> str:
        return h

    @staticmethod
    def _file_ext(url: str) -> str:
        path = url_parse.urlparse(url).path
        return path[path.rfind('.') + 1:].lower() if '.' in path.rsplit('/', 1)[-1] else ''

    def _extract_file_url(self, h: str) -> tuple[str, str]:
        if m := re_orig_file_link.search(h):
            url = m.group(1)
            ext = self._file_ext(url) or ((e := re_ext_attr.search(h)) and e.group(1)) or ''
            return url, ext
        return '', ''

    def _extract_sample_url(self, h: str) -> tuple[str, str]:
        idx = h.find(' sample_url="')
        if idx < 0:
            return '', ''
        url = h[idx + len(' sample_url="'):h.find('"', idx + len(' sample_url="'))]
        return url, self._file_ext(url)

    def _is_video(self, h: str) -> bool:
        return self._extract_file_url(h)[1] in KNOWN_EXTENSIONS_VID

    def _media_kind(self, h: str) -> str:
        return media_kind_by_ext(self._extract_file_url(h)[1])

    def _extract_post_date(self, raw: str) -> str:
        try:
            date_idx = raw.find('created_at="') + len('created_at="')
            d = datetime.datetime.fromisoformat(raw[date_idx:raw.find('"', date_idx + 1)])
            return d.strftime(FMT_DATE)
        except Exception:
            thread_exit(f'Unable to extract post date from raw: {raw}', -446)

    def _get_image_address(self, h: str) -> tuple[str, str]:
        address, fmt = self._extract_sample_url(h) if self.low_res else self._extract_file_url(h)
        if not address:
            address, fmt = self._extract_file_url(h) if self.low_res else self._extract_sample_url(h)
        if not address:
            trace(f'FATAL: GetPicAddr could not find anything!\nstring:\n\n{h}', True)
            assert False
        return address, fmt

    def _get_video_address(self, h: str) -> tuple[str, str]:
        address, fmt = self._extract_file_url(h)
        if not address:
            address, fmt = self._extract_sample_url(h)
        if not address:
            trace(f'FATAL: GetVidAddr could not find anything!\nstring:\n\n{h}', True)
            assert False
        return address, fmt

    def _extract_item_info(self, item: str) -> ItemInfo:
        try:
            item_info = ItemInfo()
            if self.is_killed():
                return item_info
            for part in re_item_info_part_xml.findall(item):
                field_name, value = tuple(str(part).split('=', 1))
                name = ITEM_INFO_FIELDS.get(field_name, field_name)
                if field_name == 'file_url':  # extension of the url, or the explicit ext="" attribute of the item
                    value = f'"{self._extract_file_url(item)[1]}"'
                elif name == 'date':
                    value = self._extract_post_date(item)
                if name in item_info.__slots__:
                    setattr(item_info, name, value.replace('\n', ' ').replace('"', '').strip())
            return item_info
        except Exception:
            self._on_thread_exception(current_process().name)
            raise

    # ----- download

    def _send_to_download(self, raw: str, item_id: str, is_video: bool) -> None:
        kind = self._media_kind(raw)
        # audio: always the original file (a 'sample' of an audio post is usually its cover image)
        address, fmt = self._get_video_address(raw) if kind != MediaKind.IMAGE else self._get_image_address(raw)
        subfolder = self._subfolder_for(kind)
        hint_maxlen = FILE_NAME_FULL_MAX_LEN - (len(self.dest_base_s.as_posix()) + len(item_id) + 1 + len(fmt))
        self._download(address, item_id, self.dest_base_s / subfolder / f'{self._try_append_extra_info(item_id, hint_maxlen)}.{fmt}')

    # threaded
    def _process_item(self, raw: str) -> None:
        if self.is_killed():
            return
        try:
            item_id = self._extract_id(raw)
            if self.download_mode == DownloadModes.SKIP:
                self._inc_proc_count()
                return
            if self.add_filename_prefix is True:
                item_id = f'{self._get_module_abbr_p()}{item_id}'
            if re_orig_file_link.search(raw):
                if self._is_video(raw):
                    self._process_video(raw, item_id)
                else:
                    self._process_image(raw, item_id)
            else:
                trace(f'Warning (W2): unable to extract file url from {raw}, deleted?', True)
            self._inc_proc_count()
        except Exception:
            self._on_thread_exception(current_process().name)
            raise

    def _execute_module_filters(self, parents: MutableSet[str]) -> None:
        abbrp = self._get_module_abbr_p()
        total_count_old = len(self.items_raw_per_task)
        removed: list[str] = []
        for idx in reversed(range(total_count_old)):
            self.catch_cancel_or_ctrl_c()
            h = self.items_raw_per_task[idx]
            item_id = self._extract_id(h)
            idstring = f'{(abbrp if self.add_filename_prefix else "")}{item_id}'
            item_info = self.item_info_dict_per_task.get(idstring)
            if item_info is not None and TAG_NO_FILE_URL in item_info.tags.split(' '):
                removed.append(f'{abbrp}{item_id} has no downloadable file for current user (restricted or deleted), skipped!')
                parents.discard(item_id)
                del self.item_info_dict_per_task[idstring]
                del self.items_raw_per_task[idx]
                self.filtered_out_ids_cache.add(idstring)
        if removed:
            trace('\n'.join(removed))
            trace(f'Filtered out {len(removed):d} / {total_count_old:d} items!')

#
#
#########################################
