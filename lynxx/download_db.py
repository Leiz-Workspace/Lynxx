# coding=UTF-8
"""
Lynxx: Danbooru downloader (default extension 'danbooru', abbreviation 'db')
Danbooru JSON API: posts.json, counts/posts.json. Anonymous users may search at most 2 tags at once,
free metatags (rating:, id:, date:, score:, status:, parent:) do not count (see task.py FREE_META_PREFIXES)
"""
#########################################
#
#

import base64
import datetime
import json
import re
from collections.abc import MutableSet
from multiprocessing.dummy import current_process
from typing import Never, final
from urllib import parse as url_parse

from bs4 import BeautifulSoup

from .defines import (
    FILE_NAME_FULL_MAX_LEN,
    FILE_NAME_PREFIX_DB,
    FMT_DATE,
    ID_VALUE_SEPARATOR_CHAR_DB,
    ITEMS_PER_PAGE_DB,
    KNOWN_EXTENSIONS_VID,
    MODULE_ABBR_DB,
    SITENAME_B_DB,
    TAGS_CONCAT_CHAR_DB,
    DownloadModes,
    ItemInfo,
    ModuleCaps,
)
from .download import Downloader
from .logger import trace
from .network import LYNXX_USER_AGENT, thread_exit
from .rex import (
    re_favorited_by_tag,
    re_item_info_part_xml,
    re_orig_file_link,
    re_tags_exclude_rx,
    re_tags_to_process_rx,
)
from .utils import as_date

__all__ = ('DownloaderDb',)

SITENAME = base64.b64decode(SITENAME_B_DB).decode()
ITEMS_PER_PAGE = ITEMS_PER_PAGE_DB
MAX_SEARCH_DEPTH_PAGES = 1000  # 'You cannot go beyond page 1000.'
MAX_SEARCH_DEPTH = MAX_SEARCH_DEPTH_PAGES * ITEMS_PER_PAGE

ITEM_INFO_FIELDS = {'file_url': 'ext', 'post_id': 'id', 'created_at': 'date'}
TAG_NO_FILE_URL = 'db_file_unavailable'
'''internal marker: post has no downloadable file for current user (banned / restricted / deleted)'''

re_sample_url_db = re.compile(r' sample_url="([^"]+)"')
'''note: core re_sample_file_link matches file_url (upstream behavior), danbooru has real samples'''


@final
class DownloaderDb(Downloader):
    """
    DownloaderDb
    """
    CAPABILITIES = frozenset((ModuleCaps.API_KEY, ModuleCaps.PARENT_CHILD, ModuleCaps.SOURCES, ModuleCaps.DATE_FILTER, ModuleCaps.PREVIEW, ModuleCaps.GENDER_TAGS))

    def __init__(self) -> None:
        super().__init__()
        # descriptive client name asked by danbooru; Lynxx has no public contact address (the original project's repository is not one)
        self._base_headers = {'User-Agent': LYNXX_USER_AGENT}
        self._base_cookies = {}
        self._last_query_tags = ''

    # ----- network / module hooks -----

    def _get_module_specific_default_headers(self) -> dict[str, str]:
        return self._base_headers

    def _get_module_specific_default_cookies(self) -> dict[str, str]:
        return self._base_cookies

    def _get_min_request_interval(self) -> float:
        return 0.25

    def _parses_raw_pages(self) -> bool:
        return True

    def _supports_native_date_filter(self) -> bool:
        return True

    # ----- module description -----

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
        return SITENAME

    def _get_module_abbr(self) -> str:
        return MODULE_ABBR_DB

    def _get_module_abbr_p(self) -> str:
        return FILE_NAME_PREFIX_DB

    def _get_items_per_page(self) -> int:
        return ITEMS_PER_PAGE

    def _get_max_search_depth(self) -> int:
        return MAX_SEARCH_DEPTH

    def _form_item_string_manually(self, *ignored) -> Never:
        raise NotImplementedError

    def _is_search_overload_page(self, raw_html_page: BeautifulSoup) -> bool:
        return False

    def get_re_tags_to_process(self) -> re.Pattern:
        return re_tags_to_process_rx

    def get_re_tags_to_exclude(self) -> re.Pattern:
        return re_tags_exclude_rx

    def _get_tags_concat_char(self) -> str:
        return TAGS_CONCAT_CHAR_DB

    def _get_idval_equal_seaparator(self) -> str:
        return ID_VALUE_SEPARATOR_CHAR_DB

    def _split_or_group_into_tasks_always(self) -> bool:
        return True

    def _can_extract_item_info_without_fetch(self) -> bool:
        return True

    def _is_custom_sort_tag(self, tag_str: str) -> bool:
        return tag_str.startswith('order:') and tag_str != 'order:id_desc'  # 'order:id' is oldest first

    # ----- addresses -----

    def _auth_str(self) -> str:
        return f'&login={url_parse.quote(self.api_key.user_id)}&api_key={self.api_key.key}' if self.api_key else ''

    def _form_tags_search_address(self, tags: str, maxlim: int | None = None) -> str:
        self._last_query_tags = tags
        return f'{self._get_sitename()}posts.json?tags={tags}&limit={maxlim or self._get_items_per_page():d}{self._auth_str()}'

    def _form_page_num_address(self, n: int) -> str:
        return f'{self.url}&page={n + 1:d}'

    def _supports_id_cursor(self) -> bool:
        return True

    def _form_cursor_page_address(self, cursor: int | None, ascending: bool) -> str:
        # 'a<id>': the posts right above id, 'b<id>': below id; both in descending order (checked 2026-11-16)
        if cursor is None:
            return f'{self.url}&page=a0' if ascending else f'{self.url}&page=1'
        return f'{self.url}&page={"a" if ascending else "b"}{cursor:d}'

    def _get_query_size_or_none(self, url: str) -> int | None:
        raw_count = self.fetch_html(self._form_count_address(self._last_query_tags))
        if raw_count is None:
            thread_exit('ERROR: GetItemsQueSize: unable to retreive post count', code=-444)
        if err := self._api_error(raw_count.raw):
            thread_exit(f'ERROR: [{MODULE_ABBR_DB.upper()}] {err}', code=-447)
        count = (json.loads(raw_count.raw).get('counts') or {}).get('posts')
        return count if isinstance(count, int) else None

    def _page_error(self, raw: bytes) -> str:
        return self._api_error(raw)

    def _form_count_address(self, tags: str) -> str:
        return f'{self._get_sitename()}counts/posts.json?tags={tags}{self._auth_str()}'

    def _consume_custom_module_tags(self, tags: str) -> str:
        if not tags:
            return tags
        taglist = tags.split(TAGS_CONCAT_CHAR_DB)
        for tidx, tag in enumerate(taglist):
            if re_favorited_by_tag.fullmatch(tag):
                taglist[tidx] = f'fav:{tag[tag.find(":") + 1:]}'
        # date limits are converted into 'date:' metatag which does not count towards tags limit
        if self._has_date_limits():
            dmin = as_date(self.date_min).isoformat()
            dmax = as_date(self.date_max).isoformat()
            if not any(t.startswith('date:') for t in taglist):
                taglist.append(f'date:{dmin}..{dmax}')
        return TAGS_CONCAT_CHAR_DB.join(taglist)

    # ----- search -----

    @staticmethod
    def _api_error(raw: bytes) -> str:
        try:
            parsed = json.loads(raw)
        except Exception:
            return 'invalid json response'
        if isinstance(parsed, dict) and parsed.get('success') is False:
            return str(parsed.get('message') or parsed.get('error') or 'unknown error')
        return ''

    def _get_items_query_size_or_html(self, url: str, tries: int | None = None) -> int:
        raw_count = self.fetch_html(self._form_count_address(self._last_query_tags), tries=tries)
        if raw_count is None:
            thread_exit('ERROR: GetItemsQueSize: unable to retreive post count', code=-444)
        if err := self._api_error(raw_count.raw):
            thread_exit(f'ERROR: [{MODULE_ABBR_DB.upper()}] {err}', code=-447)
        count = (json.loads(raw_count.raw).get('counts') or {}).get('posts')
        if isinstance(count, int):
            return count
        # count is unavailable for heavy queries (database timeout): binary search for the last non-empty page (~11 requests)
        trace(f'Info: [{MODULE_ABBR_DB.upper()}] post count is unavailable, looking for the last page...')

        def page_len(p: int) -> int:
            raw_page = self.fetch_html(self._form_page_num_address(p), tries=tries, do_cache=True)
            if raw_page is None:
                thread_exit('ERROR: GetItemsQueSize: unable to retreive html', code=-445)
            if err := self._api_error(raw_page.raw):
                thread_exit(f'ERROR: [{MODULE_ABBR_DB.upper()}] {err}', code=-447)
            return len(json.loads(raw_page.raw))

        if (first := page_len(0)) < ITEMS_PER_PAGE:
            return first
        lo, hi = 0, MAX_SEARCH_DEPTH_PAGES - 1
        if page_len(hi) >= ITEMS_PER_PAGE:
            return MAX_SEARCH_DEPTH
        while hi - lo > 1:  # page lo is full, page hi is not
            mid = (lo + hi) // 2
            if page_len(mid) >= ITEMS_PER_PAGE:
                lo = mid
            else:
                hi = mid
        return hi * ITEMS_PER_PAGE + page_len(hi)

    def _get_all_post_tags(self, raw_html_page: bytes) -> list:
        try:
            parsed = json.loads(raw_html_page)
        except ValueError:
            trace(f'ERROR: [{MODULE_ABBR_DB.upper()}] invalid json page (bot challenge or truncated response?)', True)
            return []
        if not isinstance(parsed, list):
            trace(f'ERROR: [{MODULE_ABBR_DB.upper()}] {self._api_error(raw_html_page)}', True)
            return []
        posts: list[str] = []
        for p in parsed:
            post_tags_list: list[str] = [t for t in str(p.get('tag_string') or '').split(' ') if t]
            post_id = str(p['id'])
            file_ext = str(p.get('file_ext') or '')
            post_furl = str(p.get('file_url') or '')
            post_surl = str(p.get('large_file_url') or post_furl)
            post_purl = str(p.get('preview_file_url') or post_surl)
            if file_ext == 'zip' and post_surl.endswith('.webm'):  # ugoira: use converted webm
                post_furl = post_surl
            if not post_furl:
                post_tags_list.append(TAG_NO_FILE_URL)
                post_furl = f'{SITENAME}posts/{post_id}#unavailable.none'
                post_surl = post_purl = post_furl
            post_tags = ' '.join(post_tags_list).replace('"', '\'')
            post_source = str(p.get('source') or '').replace('"', '\'')
            post_str = (
                f'<post_id="{post_id}" height="{p.get("image_height") or 0}" width="{p.get("image_width") or 0}" '
                f'md5="{p.get("md5") or ""}" file_url="{post_furl}" sample_url="{post_surl}" preview_url="{post_purl}" '
                f'created_at="{p.get("created_at") or ""}" score="{p.get("score") or 0}" '
                f'has_children="{str(bool(p.get("has_children"))).lower()}" parent_id="{p.get("parent_id") or ""}" '
                f'rating="{p.get("rating") or ""}" source="{post_source}" tags="{post_tags}">'
            )
            posts.append(post_str)
        return posts

    def _local_addr_from_string(self, h: str) -> str:
        return h

    def _extract_id(self, addr: str) -> str:
        id_idx = addr.find('post_id="') + len('post_id="')
        return addr[id_idx:addr.find('"', id_idx + 1)]

    def _is_video(self, h: str) -> bool:
        _, file_ext = self._extract_file_url(h)
        return file_ext in KNOWN_EXTENSIONS_VID

    def _get_item_html(self, h: str) -> str:
        return h

    def _extract_post_date(self, raw: str) -> str:
        try:
            # '2026-11-16T04:24:19.090-05:00' -> '16-11-2026'
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
                name, value = tuple(str(part).split('=', 1))
                name = ITEM_INFO_FIELDS.get(name, name)
                if name == 'ext':
                    value = value[value.rfind('.') + 1:]
                elif name == 'date':
                    value = self._extract_post_date(item)
                if name in item_info.__slots__:
                    setattr(item_info, name, value.replace('\n', ' ').replace('"', '').strip())
            return item_info
        except Exception:
            self._on_thread_exception(current_process().name)
            raise

    # ----- download -----

    def _send_to_download(self, raw: str, item_id: str, is_video: bool) -> None:
        address, fmt = self._get_video_address(raw) if is_video else self._get_image_address(raw)
        subfolder = self.subfolder_vid if is_video else self.subfolder_img
        hint_maxlen = FILE_NAME_FULL_MAX_LEN - (len(self.dest_base_s.as_posix()) + len(item_id) + 1 + len(fmt))
        self._download(address, item_id, self.dest_base_s / subfolder / f'{self._try_append_extra_info(item_id, hint_maxlen)}.{fmt}')

    # threaded
    def _process_item(self, raw: str) -> None:
        if self.is_killed():
            return
        try:
            h = raw
            item_id = self._extract_id(h)
            if self.download_mode == DownloadModes.SKIP:
                self._inc_proc_count()
                return
            if self.add_filename_prefix is True:
                item_id = f'{self._get_module_abbr_p()}{item_id}'
            if re_orig_file_link.search(h):
                if self._is_video(h):
                    self._process_video(h, item_id)
                else:
                    self._process_image(h, item_id)
            else:
                trace(f'Warning (W2): unable to extract file url from {h}, deleted?', True)
            self._inc_proc_count()
        except Exception:
            self._on_thread_exception(current_process().name)
            raise

    def _execute_module_filters(self, parents: MutableSet[str]) -> None:
        abbrp = self._get_module_abbr_p()
        total_count_old = len(self.items_raw_per_task)
        removed: list[str] = []
        idx: int
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

    @staticmethod
    def _extract_file_url(h: str) -> tuple[str, str]:
        if file_re_res := re_orig_file_link.search(h):
            file_url = file_re_res.group(1)
            return file_url, file_url[file_url.rfind('.') + 1:]
        return '', ''

    @staticmethod
    def _extract_sample_url(h: str) -> tuple[str, str]:
        if sample_re_res := re_sample_url_db.search(h):
            file_url = sample_re_res.group(1)
            return file_url, file_url[file_url.rfind('.') + 1:]
        return '', ''

#
#
#########################################
