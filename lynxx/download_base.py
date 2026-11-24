# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

import datetime
import os
import pathlib
import re
import sqlite3
from abc import abstractmethod
from collections.abc import Callable, MutableSet
from threading import Lock
from typing import Any, TypeVar, final

from bs4 import BeautifulSoup

from .defines import (
    DATE_MIN_DEFAULT,
    FMT_DATE,
    NEGATIVE_GROUP_MATCH_LIST_MAX_LEN,
    APIKey,
    DownloaderStates,
    DownloadModes,
    ItemInfo,
    MediaKind,
    ModuleCaps,
    ModuleConfigType,
    PageCheck,
)
from .dlindex import DownloadIndex, norm_md5
from .gender import GenderConf, GenderResult, decide, infer_from_tags, rules_for_module
from .logger import trace
from .network import PART_SUFFIX, ThreadedHtmlWorker, thread_exit
from .robots import ROBOTS_MAX_BYTES, RobotsRules
from .safety import blocked_tag
from .search_kinds import KindMatch
from .tags_parser import is_module_sort_tag
from .utils import as_date

re_preview_url_attr = re.compile(r'\bpreview_url="([^"]+)"')
re_sample_url_attr = re.compile(r'\bsample_url="([^"]+)"')
re_img_src = re.compile(r'<img[^>]+?src="([^"]+)"')

re_id_order = re.compile(r'^(?:order[:=]id_desc|sort:id(?::desc)?)$')
'''sort tags that keep the newest first (default) order: never a custom sort. 'order:id' is oldest first where the sort syntax is
'order:...': a custom sort'''

ROBOTS_FILE_NAME = 'robots.txt'
'''the site's robots.txt as the extension's author saved it, next to its manifest (SDK 1.2 robots_allows)'''
ROBOTS_AGENT = 'Lynxx'
_saved_robots_cache: dict[str, tuple[tuple[int, int], RobotsRules]] = {}
_saved_robots_lock = Lock()


def saved_robots_rules(abbr: str) -> RobotsRules | None:
    """Rules of the robots.txt saved in the folder of the module's extension (RFC 9309, lynxx/robots.py), None without one"""
    from .extensions import Extensions  # local: extensions imports the downloaders
    m = Extensions.get_by_abbr(abbr)
    path = m.path / ROBOTS_FILE_NAME if m is not None and m.path is not None else None
    try:
        st = path.stat() if path is not None and path.is_file() else None
    except OSError:
        st = None
    if path is None or st is None:
        return None
    key, stamp = path.as_posix(), (st.st_mtime_ns, st.st_size)
    with _saved_robots_lock:
        if (cached := _saved_robots_cache.get(key)) is not None and cached[0] == stamp:
            return cached[1]
    with open(path, 'rb') as f:
        text = f.read(ROBOTS_MAX_BYTES).decode('utf-8', errors='replace')
    rules = RobotsRules.parse(text, ROBOTS_AGENT)
    with _saved_robots_lock:
        _saved_robots_cache[key] = (stamp, rules)
    return rules

GENDER_MISMATCH_DIR = '_gender_mismatch'
'''-gender_verify_media: subfolder of the destination for downloaded files that do not match the gender filter'''

ST1 = TypeVar('ST1')
ST2 = TypeVar('ST2')


class DownloaderBase(ThreadedHtmlWorker):
    """DownloaderBase !Abstract!"""
    CAPABILITIES: frozenset[str] = frozenset((ModuleCaps.SOURCES, ModuleCaps.DATE_FILTER, ModuleCaps.COMMENTS))
    '''optional features supported by module, see ModuleCaps'''

    @abstractmethod
    def __init__(self) -> None:
        super().__init__()

        # config
        self.hide_personal_info: bool = False
        self.add_filename_prefix: bool = False
        self._existing_ids_cache: set[str] | None = None
        self.dump_tags: bool = False
        self.dump_sources: bool = False
        self.dump_comments: bool = False
        self.dump_hashes: bool = False
        self.dump_per_item: bool = False
        self.merge_lists: bool = False
        self.append_info: bool = False
        self.download_mode: str = DownloadModes.FULL
        self.download_limit: int = 0
        self.reverse_download_order: bool = False
        self.maxthreads_items: int = 1
        self.include_parchi: bool = False
        self.skip_images: bool = False
        self.skip_videos: bool = False
        self.skip_audio: bool = False
        self.prefer_webm: bool = False
        self.prefer_mp4: bool = False
        self.low_res: bool = False
        '''-lowres: the reduced image (sample) instead of the original. Images only: never used to pick a video file'''
        self.max_video_height: int = 0
        '''-max_vres: modules with ModuleCaps.VIDEO_QUALITY_CHOICE take the best video file not higher than this, else the smallest
        one; 0 = the best file. Extensions read it with getattr(self, 'max_video_height', 0) (cores before 2.0.14 lack it)'''
        # the day this downloader was made: the default max date, and a max date on or after it limits nothing. Per downloader,
        # not per process (defines.LAUCH_DATE): the app's core makes one for each request and runs for days
        self.date_today: datetime.date = datetime.date.today()
        self.date_max_default: str = self.date_today.strftime(FMT_DATE)
        self.date_min: str = DATE_MIN_DEFAULT
        self.date_max: str = self.date_max_default
        self.preserve_date: bool = False
        self.dest_base: pathlib.Path = pathlib.Path().resolve().parent
        self.subfolder_vid: str = ''
        self.subfolder_img: str = ''
        self.subfolder_aud: str = ''
        self.warn_nonempty: bool = False
        self.api_key: APIKey = APIKey()
        self.tags_str_arr: list[str] = []
        # extra
        self.cmdline: list[str] = []
        self.options: dict[str, bool | int | str] = {}
        self.get_max_id: bool = False
        self.check_tags: bool = False

        # results
        self.url: str = ''
        self.minpage: int = 0
        self.maxpage: int = 0
        self.success_count: int = 0
        self.fail_count: int = 0
        self.failed_items: list[str] = []
        self.max_item_id: int = 0
        '''highest post id processed by this run (or found by -get_maxid): saved queries download only newer posts next time'''
        self.total_count: int = 0
        self.total_count_old: int = 0
        self.processed_count: int = 0
        self.current_task_num: int = 0
        self.orig_tasks_count: int = 0
        self.current_state: DownloaderStates = DownloaderStates.IDLE
        self.items_raw_per_task: list[str] = []
        self.items_raw_per_page: dict[int, list[str]] = {}
        self.items_raw_all: list[str] = []
        self.item_info_dict_per_task: dict[str, ItemInfo] = {}
        self.item_info_dict_all: dict[str, ItemInfo] = {}
        self.neg_and_groups: list[list[re.Pattern[str]]] = []
        self.required_tags: list[str] = []
        # content filter (character gender, tier 0 = tags). The safety blacklist is a separate option (-safety_blacklist)
        self.gender_select: frozenset[str] = frozenset()
        self.gender_mode: str = 'tags'
        self.gender_model_checked: int = 0
        self.gender_model_url: str = ''
        '''model server of '-gender_mode model' (empty: LYNXX_GENDER_MODEL_URL, else the model in process)'''
        self.gender_model_token: str = ''
        '''posts looked at by the gender image model in this run'''
        self.gender_min_conf: GenderConf = GenderConf.MEDIUM
        self.gender_strict: bool = False
        self.gender_results: dict[str, GenderResult] = {}
        self.gender_stats: dict[str, int] = {}
        self.gender_filtered_count: int = 0
        self.gender_verify_media: bool = False
        '''-gender_verify_media: downloaded videos / animated pictures labeled by the model are checked on several frames'''
        self.gender_verified_count: int = 0
        self.gender_mismatch_count: int = 0
        '''files moved into GENDER_MISMATCH_DIR by -gender_verify_media'''
        self.safety_blacklist: bool = False
        '''-safety_blacklist: drop posts whose tags (or the image model's minor coded tags) suggest minors; off by default'''
        self.safety_blocked_count: int = 0
        '''positive tags which could not be sent to website (tags limit) and are checked locally'''
        self.known_parents: set[str] = set()
        self.filtered_out_ids_cache: set[str] = set()
        self.default_sort: bool = True
        self.current_kind: KindMatch | None = None
        '''the search kind of the query (manifest 'search', lynxx/search_kinds.py): its listing kind if there is one, else the first
        tag of a kind; set while the arguments are parsed, before any search address is built (SDK 1.2)'''
        self.site_order_kind: str = ''
        '''the tag of a kind whose results come in the site's order (a listing, or order 'site'): like a sort tag'''
        self.favorites_search_user: str = ''
        self.pool_search_str: str = ''
        self.current_task_subfolder: str = ''
        self.skip_known: bool = False
        '''-skip_known: posts already in the download index (md5 or module + id) whose file is still on disk are skipped'''
        self.known_skipped_count: int = 0
        self._known_skipped_ids: set[str] = set()
        self._known_db: DownloadIndex | None = None

    @property
    def dest_base_s(self) -> pathlib.Path:
        return self.dest_base / self.current_task_subfolder

    @property
    def total_pages(self) -> int:
        return self._num_pages()

    @abstractmethod
    def _is_pool_search_conversion_required(self) -> bool: ...

    @abstractmethod
    def _is_fav_search_conversion_required(self) -> bool: ...

    @abstractmethod
    def _is_fav_search_single_step(self) -> bool: ...

    @abstractmethod
    def _supports_native_id_filter(self) -> bool: ...

    @abstractmethod
    def _get_id_bounds(self) -> tuple[int, int]: ...

    @abstractmethod
    def _get_module_abbr(self) -> str: ...

    @abstractmethod
    def _get_module_abbr_p(self) -> str: ...

    @abstractmethod
    def _get_items_per_page(self) -> int: ...

    @abstractmethod
    def _get_max_search_depth(self) -> int: ...

    @abstractmethod
    def _form_item_string_manually(self, raw_html_page: BeautifulSoup) -> str: ...

    @abstractmethod
    def _is_search_overload_page(self, raw_html_page: BeautifulSoup) -> bool: ...

    @abstractmethod
    def _form_page_num_address(self, n: int) -> str: ...

    @abstractmethod
    def _get_all_post_tags(self, raw_html_page: BeautifulSoup | bytes) -> list: ...

    @abstractmethod
    def _local_addr_from_string(self, h: str) -> str: ...

    @abstractmethod
    def _extract_id(self, h: str) -> str: ...

    @abstractmethod
    def _is_video(self, h: str) -> bool: ...

    @abstractmethod
    def _get_item_html(self, h: str) -> BeautifulSoup | str | None: ...

    @abstractmethod
    def _extract_post_date(self, raw_or_html: BeautifulSoup | str) -> str: ...

    @abstractmethod
    def _get_items_query_size_or_html(self, url: str, tries: int | None = None) -> int | BeautifulSoup: ...

    @abstractmethod
    def _get_image_address(self, h: str) -> tuple[str, str]: ...

    @abstractmethod
    def _get_video_address(self, h: str) -> tuple[str, str]: ...

    @abstractmethod
    def _extract_item_info(self, item: str) -> ItemInfo: ...

    @abstractmethod
    def get_re_tags_to_process(self) -> re.Pattern:
        """Public, needed by tagging tools"""

    @abstractmethod
    def get_re_tags_to_exclude(self) -> re.Pattern:
        """Public, needed by tagging tools"""

    @abstractmethod
    def _get_tags_concat_char(self) -> str: ...

    @abstractmethod
    def _get_idval_equal_seaparator(self) -> str: ...

    @abstractmethod
    def _split_or_group_into_tasks_always(self) -> bool: ...

    @abstractmethod
    def _can_extract_item_info_without_fetch(self) -> bool: ...

    @abstractmethod
    def _consume_custom_module_tags(self, tags: str) -> str: ...

    @abstractmethod
    def _form_tags_search_address(self, tags: str, maxlim: int | None = None) -> str: ...

    def _execute_module_filters(self, parents: MutableSet[str]) -> None:
        pass

    def extract_preview_url(self, h: str) -> str:
        """Public, needed by desktop preview: thumbnail url of a raw item string (empty if unknown)"""
        for rex in (re_preview_url_attr, re_sample_url_attr, re_img_src):
            if m := rex.search(h):
                url = m.group(1)
                return f'{self._get_sitename().rstrip("/")}{url}' if url.startswith('/') and not url.startswith('//') else url
        return ''

    def _item_info_for(self, full_item_id: str) -> ItemInfo:
        """Info of an item while it is being downloaded: in item_info_dict_all once its task is done (always with the streaming
        pipeline, which clears the per task dict after each batch), else in the per task dict. A throwaway ItemInfo if there is none
        (the original code raised KeyError for items of earlier tasks, ex. '-dump_comments' with OR groups)"""
        return self.item_info_dict_all.get(full_item_id) or self.item_info_dict_per_task.get(full_item_id) or ItemInfo()

    def _item_idstring(self, item_id: str) -> str:
        """Key of item infos / filtered ids caches (prefixed only when file names are prefixed). The original code mixed both forms"""
        return f'{(self._get_module_abbr_p() if self.add_filename_prefix else "")}{item_id}'

    def _has_capability(self, cap: str) -> bool:
        return cap in self.CAPABILITIES

    def _has_date_limits(self) -> bool:
        """-mindate / -maxdate other than their defaults (no limit)"""
        return self.date_min != DATE_MIN_DEFAULT or self.date_max != self.date_max_default

    def _parses_raw_pages(self) -> bool:
        """True if module parses raw page bytes (json api) instead of BeautifulSoup html"""
        return False

    def _supports_native_date_filter(self) -> bool:
        """True if module converts date limits into search query itself (no page bisection by date required)"""
        return False

    def _get_page_fetch_delay(self) -> float:
        """Per-thread delay after fetching a search page (seconds)"""
        return 0.0

    def _get_page_scan_threads_divisor(self) -> int:
        return 2

    def _supports_id_cursor(self) -> bool:
        """Module can page by id cursor (see _form_cursor_page_address): used by the streaming pipeline with default sorting"""
        return False

    def _form_cursor_page_address(self, cursor: int | None, ascending: bool) -> str:
        """Search page right above (ascending) / below the cursor id, posts in descending id order. None: the first page"""
        raise NotImplementedError

    def _prefer_id_cursor(self, count: int | None) -> bool:
        """Cursor pages are fetched one after another, page numbers in parallel: modules may keep page numbers for small results"""
        return True

    def _cursor_first_page_ok(self, ids: list[int], ascending: bool) -> bool:
        """Module check of the first cursor page (ex. the website really sorted by id), False: use page numbers"""
        return True

    def _get_query_size_or_none(self, url: str) -> int | None:
        """Posts count for the cursor pipeline, None when the website cannot count (the scan does not need it)"""
        res = self._get_items_query_size_or_html(url)
        return res if isinstance(res, int) else None

    def _page_error(self, raw: bytes) -> str:
        """Module hook: api error message carried by a search page, '' if none"""
        return ''

    def _solve_module_specific_conflicts(self) -> bool:
        """Module hook for argument conflicts not covered by capabilities, returns True if something was changed"""
        return False

    def _is_custom_sort_tag(self, tag_str: str) -> bool:
        """A sort tag other than newest first: 'sort:...', or a tag matching the module's sort syntax (manifest syntax.sort,
        core tables for the modules known to the core) except the newest first id order ('order:id_desc', 'sort:id'...)"""
        if tag_str.startswith('sort:') and tag_str not in ('sort:id', 'sort:id:desc'):
            return True
        return is_module_sort_tag(tag_str) and not re_id_order.fullmatch(tag_str)

    def robots_allows(self, url: str) -> bool:
        """SDK 1.2, for routes an extension builds from user input (a kind's listing): whether the site's robots.txt saved in the
        extension folder ('robots.txt' next to manifest.json) allows url, as RFC 9309 reads it for agent 'Lynxx'.
        False when the extension has no saved robots.txt (the check it asks for cannot be made)"""
        rules = saved_robots_rules(self._get_module_abbr())
        return rules is not None and rules.allows(url)

    def require_robots_allowed(self, url: str) -> None:
        """robots_allows, or the run stops with a clear message (nothing is requested)"""
        if self.robots_allows(url):
            return
        abbr = self._get_module_abbr().upper()
        if saved_robots_rules(self._get_module_abbr()) is None:
            thread_exit(f'Error: [{abbr}] the extension has no saved {ROBOTS_FILE_NAME} to check {url} against: not requested', -480)
        thread_exit(f'Error: [{abbr}] the site\'s robots.txt does not allow {url}: Lynxx does not request it', -481)

    def _get_api_key(self) -> str:
        return ''

    @staticmethod
    def _get_default_api_key() -> str:
        return ''

    @staticmethod
    def _get_maxid_fetch_tags_string() -> str:
        return ''

    @staticmethod
    @final
    def get_module_specific_default_value(value_type: ModuleConfigType) -> int | str | None:
        if value_type == ModuleConfigType.CONFIG_API_KEY:
            return DownloaderBase._get_default_api_key()
        return None

    @staticmethod
    @final
    def _argswitch2(func: Callable[[ST1 | ST2], Any], arg1: ST1, arg2: ST2, pred: bool) -> Any:
        return func(arg1 if pred else arg2)

    def _extract_favorite_user(self, fav_user_tags: list[re.Match | None]) -> None:
        self.favorites_search_user = str(fav_user_tags[-1].group(1)) if fav_user_tags else ''

    def _clean_favorite_user(self) -> None:
        self.favorites_search_user = ''

    def _extract_pool_id(self, pool_tags: list[re.Match]) -> None:
        self.pool_search_str = pool_tags[-1].group(1) if pool_tags else ''

    def _clean_pool_id(self) -> None:
        self.pool_search_str = ''

    def _tasks_count(self) -> int:
        return len(self.tags_str_arr)

    def _num_pages(self) -> int:
        return (self.maxpage - self.minpage) + 1

    def _extract_minmax_id(self) -> tuple[int, int]:
        min_id, max_id = 10 ** 18, 0
        for iraw in self.items_raw_all:
            id_ = int(self._extract_id(iraw))
            if id_ < min_id:
                min_id = id_
            if id_ > max_id:
                max_id = id_
        return min_id, max_id

    def _get_page_boundary_by_id(self, minpage: bool, boundary: int) -> int:
        if 1 <= self._num_pages() <= 2 or boundary == 0:
            pnum = self.minpage if minpage else self.maxpage
        else:
            p_chks = [PageCheck() for _ in range(self.maxpage + 3)]

            lim_backw = self.minpage - 1
            lim_forw = self.maxpage + 1

            dofinal = False
            cur_step = 0
            pdivider = 1
            step_dir = 1  # forward
            pnum = self.minpage

            while True:
                self.catch_cancel_or_ctrl_c()

                if dofinal is True:
                    trace(f'{"min" if minpage else "max"} page reached: {pnum + 1:d}')
                    break

                cur_step += 1
                trace(f'step {cur_step:d}')

                pdivider = min(pdivider * 2, self._num_pages())
                pnum += (self._num_pages() // pdivider) * step_dir
                trace(f'page {pnum + 1:d} (div {pdivider:d}, step {step_dir:d})')

                half_div = pdivider > self._num_pages() // 2

                if pnum < self.minpage:
                    trace('Page < minpage, returning minpage!')
                    pnum = self.minpage
                    break
                if pnum > self.maxpage:
                    trace('Page > maxpage, returning maxpage!')
                    pnum = self.maxpage
                    break

                items_per_page = self._get_items_per_page()

                prev = pnum * items_per_page
                curmax = min((pnum + 1) * items_per_page, self.total_count)
                i_count = curmax - prev

                raw_html_page = self.fetch_html(self._form_page_num_address(pnum), do_cache=True)
                if raw_html_page is None:
                    thread_exit(f'ERROR: ProcPage: unable to retreive html for page {pnum + 1:d}!', -14)

                items_raw = self._argswitch2(self._get_all_post_tags, raw_html_page.raw, raw_html_page.html, self._parses_raw_pages())
                if len(items_raw) == 0:
                    thread_exit(f'ERROR: ProcPage: cannot find picture or video on page {pnum + 1:d}', -15)

                # check first item if moving forward and last item if backwards
                h = self._local_addr_from_string(str(items_raw[0 if step_dir == 1 else i_count - 1]))
                item_id = self._extract_id(h)
                item_id_int = int(item_id)

                def forward() -> tuple[int, int]:
                    return pnum, 1

                def backward() -> tuple[int, int]:
                    return pnum, -1

                cur_step_dir = step_dir

                if minpage is True:
                    if item_id_int > boundary:
                        if half_div and (p_chks[lim_forw + 1].first or p_chks[pnum + 1].last):
                            dofinal = True
                        else:
                            trace(f'Info: [{self._get_module_abbr().upper()}] TagProc: > maxid at {item_id} - stepping forward...')
                            lim_backw, step_dir = forward()
                    else:
                        if half_div and (p_chks[lim_backw + 1].last or p_chks[pnum + 1].first):
                            dofinal = True
                        else:
                            trace(f'Info: [{self._get_module_abbr().upper()}] TagProc: <= maxid at {item_id} - stepping backwards...')
                            lim_forw, step_dir = backward()
                else:
                    if item_id_int < boundary:
                        if half_div and (p_chks[lim_backw + 1].last or p_chks[pnum + 1].first):
                            dofinal = True
                        else:
                            trace(f'Info: [{self._get_module_abbr().upper()}] TagProc: < minid at {item_id} - stepping backwards...')
                            lim_forw, step_dir = backward()
                    else:
                        if half_div and (p_chks[lim_forw + 1].first or p_chks[pnum + 1].last):
                            dofinal = True
                        else:
                            trace(f'Info: [{self._get_module_abbr().upper()}] TagProc: >= minid at {item_id} - stepping forward...')
                            lim_backw, step_dir = forward()

                if cur_step_dir == 1:
                    p_chks[pnum + 1].first = True
                else:
                    p_chks[pnum + 1].last = True

        return pnum

    def _page_edge_date(self, pnum: int, first: bool) -> str | None:
        """Date of the first (newest) or last (oldest) item of a search page, None if the page is empty"""
        raw_html_page = self.fetch_html(self._form_page_num_address(pnum), do_cache=True)
        if raw_html_page is None:
            thread_exit(f'ERROR: ProcPage: unable to retreive html for page {pnum + 1:d}!', -16)
        items_raw = self._argswitch2(self._get_all_post_tags, raw_html_page.raw, raw_html_page.html, self._parses_raw_pages())
        if len(items_raw) == 0:
            return None
        h = self._local_addr_from_string(str(items_raw[0 if first else -1]))
        raw_html_item = self._get_item_html(h)
        if raw_html_item is None:
            thread_exit(f'ERROR: TagProc: unable to retreive html for {self._extract_id(h)}! Stopping search', -18)
        return self._extract_post_date(raw_html_item)

    def _get_page_boundary_by_date(self, minpage: bool) -> int:
        """Binary search over result pages (newest first, dates non-increasing with page index).
        minpage: first page holding an item <= date_max (its last item is <= date_max);
        maxpage: last page holding an item >= date_min (its first item is >= date_min).
        Replaces the original bisection which could stop one page off when pages count is not a power of 2"""
        if 1 <= self._num_pages() <= 2:
            trace('Only a page or two! Skipping')
            return self.minpage if minpage else self.maxpage
        if minpage is True and as_date(self.date_max) >= self.date_today:
            trace('Min page: max date irrelevant! Skipping')
            return self.minpage
        if minpage is False and as_date(self.date_min) <= as_date(DATE_MIN_DEFAULT):
            trace('Max page: min date irrelevant! Skipping')
            return self.maxpage
        lo, hi = self.minpage, self.maxpage
        if minpage:
            limit, answer = as_date(self.date_max), self.maxpage
            while lo <= hi:
                self.catch_cancel_or_ctrl_c()
                mid = (lo + hi) // 2
                date = self._page_edge_date(mid, first=False)
                trace(f'page {mid + 1:d}: last item date {date}')
                if date is None or as_date(date) <= limit:
                    answer, hi = mid, mid - 1
                else:
                    lo = mid + 1
        else:
            limit, answer = as_date(self.date_min), self.minpage
            while lo <= hi:
                self.catch_cancel_or_ctrl_c()
                mid = (lo + hi) // 2
                date = self._page_edge_date(mid, first=True)
                trace(f'page {mid + 1:d}: first item date {date}')
                if date is not None and as_date(date) >= limit:
                    answer, lo = mid, mid + 1
                else:
                    hi = mid - 1
        trace(f'{"min" if minpage else "max"} page reached: {answer + 1:d}')
        return answer

    def _single_item_date(self, item_raw: str) -> str | None:
        raw_html_item = self._get_item_html(self._local_addr_from_string(str(item_raw)))
        return self._extract_post_date(raw_html_item) if raw_html_item is not None else None

    def _first_index_by_date(self, items_raw_list: list[str], lo: int, hi: int, pred: Callable[[str | None], bool]) -> int:
        """Binary search over items sorted newest first: first index in [lo, hi) where pred(item date) is True (hi if none).
        pred must be monotonic over the range (False... True). One item page is fetched per step"""
        while lo < hi:
            self.catch_cancel_or_ctrl_c()
            mid = (lo + hi) // 2
            if pred(self._single_item_date(items_raw_list[mid])):
                hi = mid
            else:
                lo = mid + 1
        return lo

    def _filter_last_items(self) -> None:
        """Removes trailing items older than date_min. Only the last 2 pages worth of items can be out of range
        (page boundaries are found by date first). Replaces the original bisection which stopped one step off and skipped single items"""
        trace('Filtering trailing back items...')

        if as_date(self.date_min) <= as_date(DATE_MIN_DEFAULT):
            trace('last items filter is irrelevant! Skipping!')
            return

        items_raw_list = self.items_raw_all if self.current_state == DownloaderStates.DOWNLOADING else self.items_raw_per_task
        orig_len = len(items_raw_list)
        if orig_len == 0:
            return

        trace(f'mindate at {self.date_min}, filtering...')
        date_min = as_date(self.date_min)
        boundary = orig_len - min(orig_len, self._get_items_per_page() * 2)
        # items without a date are kept (original behavior)
        cut = self._first_index_by_date(items_raw_list, boundary, orig_len, lambda d: d is not None and as_date(d) < date_min)
        trace(f'Filtered out {orig_len - cut:d} / {orig_len:d} items')
        del items_raw_list[cut:]

    def _filter_first_items(self) -> None:
        """Removes leading items newer than date_max (see _filter_last_items)"""
        trace('Filtering trailing front items...')

        if as_date(self.date_max) >= self.date_today:
            trace('first items filter is irrelevant! Skipping!')
            return

        items_raw_list = self.items_raw_all if self.current_state == DownloaderStates.DOWNLOADING else self.items_raw_per_task
        orig_len = len(items_raw_list)
        if orig_len == 0:
            return

        trace(f'maxdate at {self.date_max}, filtering...')
        date_max = as_date(self.date_max)
        boundary = min(orig_len, self._get_items_per_page() * 2)
        cut = self._first_index_by_date(items_raw_list, 0, boundary, lambda d: d is None or as_date(d) <= date_max)
        trace(f'Filtered out {cut:d} / {orig_len:d} items')
        del items_raw_list[:cut]

    def _media_kind(self, h: str) -> str:
        """image / video / audio of a raw item. Default: from _is_video; modules with audio media override it (download_json.py)"""
        return MediaKind.VIDEO if self._is_video(h) else MediaKind.IMAGE

    def _subfolder_for(self, kind: str) -> str:
        return {MediaKind.VIDEO: self.subfolder_vid, MediaKind.AUDIO: self.subfolder_aud}.get(kind, self.subfolder_img)

    def _filter_items_by_type(self) -> None:
        trace('Filtering items by type...')

        if self.skip_images is False and self.skip_videos is False and self.skip_audio is False:
            trace('All types are enabled! Skipping')
            return
        skipped = {MediaKind.IMAGE: self.skip_images, MediaKind.VIDEO: self.skip_videos, MediaKind.AUDIO: self.skip_audio}

        total_count_temp = self.total_count

        idx: int
        for idx in reversed(range(len(self.items_raw_per_task))):
            self.catch_cancel_or_ctrl_c()
            h = str(self.items_raw_per_task[idx])
            if skipped.get(self._media_kind(h), False):
                del self.items_raw_per_task[idx]
                self.total_count -= 1

        if total_count_temp != self.total_count:
            trace(f'Filtered out {total_count_temp - self.total_count:d} / {total_count_temp:d} items!')

    def _filter_items_by_previous_tasks(self) -> None:
        trace('Filtering items by previous tasks...')

        if self.current_task_num <= 1:
            return

        total_count_temp = self.total_count

        idx: int
        for idx in reversed(range(len(self.items_raw_per_task))):
            self.catch_cancel_or_ctrl_c()
            full_itemid = self._item_idstring(self._extract_id(self._local_addr_from_string(str(self.items_raw_per_task[idx]))))
            if full_itemid in self.item_info_dict_all or full_itemid in self.filtered_out_ids_cache:
                del self.items_raw_per_task[idx]
                self.total_count -= 1

        if total_count_temp != self.total_count:
            trace(f'Filtered out {total_count_temp - self.total_count:d} / {total_count_temp:d} items!')

    def _filter_existing_items(self) -> None:
        trace('Filtering out existing items...')

        folders = [self.dest_base_s] + [self.dest_base_s / sub for sub in dict.fromkeys((self.subfolder_img, self.subfolder_vid,
                                                                                         self.subfolder_aud)) if sub]
        if self.gender_verify_media:  # files already checked and moved aside are not downloaded and checked again
            folders.append(self.dest_base_s / GENDER_MISMATCH_DIR)
        abbrp = self._get_module_abbr_p()
        re_owned_file = re.compile(fr'^(?:{re.escape(abbrp)})?(\d+)[._]')
        existing_ids: set[str] = set()
        if self._existing_ids_cache is not None:  # streaming: the folders are listed once per task, not per batch
            folders, existing_ids = [], self._existing_ids_cache
        for folder in folders:
            if not folder.is_dir():
                continue
            with os.scandir(folder.as_posix()) as listing:
                for f in listing:
                    # incomplete (.part) and empty files are not 'existing': they will be downloaded again
                    if f.name.endswith(PART_SUFFIX) or not f.is_file() or f.stat().st_size == 0:
                        continue
                    if m := re_owned_file.match(f.name):
                        existing_ids.add(m.group(1))
        if self.streaming_active and self._existing_ids_cache is None:
            self._existing_ids_cache = existing_ids

        if not existing_ids:
            return

        total_count_temp = self.total_count
        idx: int
        for idx in reversed(range(len(self.items_raw_per_task))):
            self.catch_cancel_or_ctrl_c()
            h = self._local_addr_from_string(str(self.items_raw_per_task[idx]))
            if self._extract_id(h) in existing_ids:
                del self.items_raw_per_task[idx]
                self.total_count -= 1

        if total_count_temp != self.total_count:
            trace(f'Filtered out {total_count_temp - self.total_count:d} / {total_count_temp:d} items!')

    def _known_lookup(self, post_ids: list[str], md5s: list[str]) -> tuple[dict[str, str], dict[str, str]]:
        """-skip_known: download index lookup (lynxx/dlindex.py), only files still on disk count"""
        try:
            if self._known_db is None:
                self._known_db = DownloadIndex()
            return self._known_db.find_known(self._get_module_abbr(), post_ids, md5s)
        except (sqlite3.Error, OSError) as err:
            trace(f'Warning (W2): download index is not readable, \'-skip_known\' is ignored: {err}')
            self.skip_known = False
            return {}, {}

    def _close_known_db(self) -> None:
        db, self._known_db = self._known_db, None
        if db is not None:
            db.close()

    def _note_known_skipped(self, idstring: str) -> None:
        self.filtered_out_ids_cache.add(idstring)
        if idstring not in self._known_skipped_ids:  # multi task queries may meet a post more than once
            self._known_skipped_ids.add(idstring)
            self.known_skipped_count += 1

    def _filter_known_items_by_id(self) -> None:
        """-skip_known, before item infos are collected (modules fetching item pages for infos skip those requests too)"""
        if not self.skip_known or not self.items_raw_per_task:
            return
        trace('Filtering out items already downloaded (download index, same module and id)...')
        ids = [self._extract_id(self._local_addr_from_string(str(x))) for x in self.items_raw_per_task]
        by_id, _ = self._known_lookup(ids, [])
        if not by_id:
            return
        total = len(self.items_raw_per_task)
        idx: int
        for idx in reversed(range(total)):
            if ids[idx] in by_id:
                del self.items_raw_per_task[idx]
                self._note_known_skipped(self._item_idstring(ids[idx]))
        trace(f'Filtered out {total - len(self.items_raw_per_task):d} / {total:d} items already downloaded!')

    def _filter_known_items_by_md5(self, parents: MutableSet[str]) -> None:
        """-skip_known, md5 reported by the website (the same file downloaded earlier, by any module)"""
        if not self.skip_known or not self.items_raw_per_task:
            return
        trace('Filtering out items already downloaded (download index, same md5)...')
        rows = []
        for raw in self.items_raw_per_task:
            item_id = self._extract_id(self._local_addr_from_string(str(raw)))
            idstring = self._item_idstring(item_id)
            info = self.item_info_dict_per_task.get(idstring)
            rows.append((item_id, idstring, info, norm_md5(info.md5) if info else ''))
        _, by_md5 = self._known_lookup([], [r[3] for r in rows if r[3]])
        if not by_md5:
            return
        total = len(rows)
        removed = 0
        idx: int
        for idx in reversed(range(total)):
            item_id, idstring, info, md5 = rows[idx]
            if md5 and md5 in by_md5:
                self._drop_task_item(idx, item_id, idstring, info, parents)
                self._note_known_skipped(idstring)
                removed += 1
        trace(f'Filtered out {removed:d} / {total:d} items already downloaded (same file)!')

    def _filter_items_matching_negative_and_groups(self, parents: MutableSet[str]) -> None:
        trace('Filtering out items using custom filters...')

        if len(self.neg_and_groups) == 0:
            return

        m_dict: dict[str, list[str]] = {}

        def match_neg_group(p: re.Pattern[str], t: str, pl: list[re.Pattern[str]]) -> bool:
            ngm = p.fullmatch(t)
            if ngm:
                pp_str = f'-({",".join(pp.pattern[1:-1] for pp in pl)})'
                if len(pp_str) > NEGATIVE_GROUP_MATCH_LIST_MAX_LEN:
                    pp_str = f'{pp_str[:NEGATIVE_GROUP_MATCH_LIST_MAX_LEN - 3]}...'
                if pp_str not in m_dict:
                    m_dict[pp_str] = []
                m_dict[pp_str].append(t)
            return ngm is not None

        abbrp = self._get_module_abbr_p()
        total_count_old = len(self.items_raw_per_task)
        removed_count = 0
        removed_messages: list[str] = []
        idx: int
        for idx in reversed(range(total_count_old)):
            h = self._local_addr_from_string(str(self.items_raw_per_task[idx]))
            item_id = self._extract_id(h)
            idstring = f'{(abbrp if self.add_filename_prefix else "")}{item_id}'
            item_info = self.item_info_dict_per_task[idstring]
            tags_list = item_info.tags.lower().split(' ')
            m_dict.clear()
            if any(all(any(match_neg_group(patt, tag, plist) for tag in tags_list) for patt in plist) for plist in self.neg_and_groups):
                # Note: above algorithm is minimal match, only first matching combination will be reported
                if self.verbose and self._supports_native_id_filter():
                    removed_messages.append('\n'.join(f'{abbrp}{item_id} contains excluded tags combination \'{mk}\': '
                                            f'{",".join(m_dict[mk])}. Skipped!' for mk in m_dict if len(m_dict[mk]) > 1))
                if item_id in parents:
                    parents.remove(item_id)
                if item_info.parent_id in parents:
                    parents.remove(item_info.parent_id)
                del self.item_info_dict_per_task[idstring]
                del self.items_raw_per_task[idx]
                self.filtered_out_ids_cache.add(idstring)
                removed_count += 1

        if removed_count > 0:
            trace('\n'.join(removed_messages))
            trace(f'Filtered out {removed_count:d} / {total_count_old:d} items!')

    def _filter_items_missing_required_tags(self, parents: MutableSet[str]) -> None:
        trace('Filtering out items missing required tags...')

        if len(self.required_tags) == 0:
            return

        abbrp = self._get_module_abbr_p()
        required = {t.lower() for t in self.required_tags}
        total_count_old = len(self.items_raw_per_task)
        removed_count = 0
        idx: int
        for idx in reversed(range(total_count_old)):
            self.catch_cancel_or_ctrl_c()
            h = self._local_addr_from_string(str(self.items_raw_per_task[idx]))
            item_id = self._extract_id(h)
            idstring = f'{(abbrp if self.add_filename_prefix else "")}{item_id}'
            item_info = self.item_info_dict_per_task.get(idstring)
            if item_info is None:
                continue
            if not required.issubset(set(item_info.tags.lower().split(' '))):
                if item_id in parents:
                    parents.remove(item_id)
                del self.item_info_dict_per_task[idstring]
                del self.items_raw_per_task[idx]
                self.filtered_out_ids_cache.add(idstring)
                removed_count += 1

        if removed_count > 0:
            trace(f'Filtered out {removed_count:d} / {total_count_old:d} items missing required tags!')

    def _task_item_tags(self, idx: int) -> tuple[str, str, list[str], ItemInfo | None]:
        h = self._local_addr_from_string(str(self.items_raw_per_task[idx]))
        item_id = self._extract_id(h)
        idstring = f'{(self._get_module_abbr_p() if self.add_filename_prefix else "")}{item_id}'
        info = self.item_info_dict_per_task.get(idstring)
        return item_id, idstring, (info.tags.split(' ') if info and info.tags else []), info

    def _drop_task_item(self, idx: int, item_id: str, idstring: str, info: ItemInfo | None, parents: MutableSet[str]) -> None:
        parents.discard(item_id)
        if info is not None and info.parent_id:
            parents.discard(info.parent_id)
        self.item_info_dict_per_task.pop(idstring, None)
        del self.items_raw_per_task[idx]
        self.filtered_out_ids_cache.add(idstring)

    def _filter_items_safety(self, parents: MutableSet[str]) -> None:
        """Hard blacklist (lynxx/safety.py), with -safety_blacklist, for every task incl. parent tasks"""
        if not self.safety_blacklist:
            return
        trace('Filtering out items using safety blacklist...')
        total = len(self.items_raw_per_task)
        removed = 0
        idx: int
        for idx in reversed(range(total)):
            self.catch_cancel_or_ctrl_c()
            item_id, idstring, tags, info = self._task_item_tags(idx)
            if blocked_tag(tags):
                self._drop_task_item(idx, item_id, idstring, info, parents)
                removed += 1
        if removed > 0:
            self.safety_blocked_count += removed
            trace(f'Blocked {removed:d} / {total:d} items (safety blacklist)')

    def _filter_items_by_gender(self, parents: MutableSet[str]) -> None:
        """Gender filter: tier 0 labels from post tags (lynxx/gender.py); with '-gender_mode model' posts whose tags are not
        conclusive are looked at by the image model (lynxx/gender_model.py); with -safety_blacklist its safety gate blocks posts
        as well"""
        if not self.gender_select:
            return
        trace('Filtering items by character gender...')
        rules = rules_for_module(self._get_module_abbr())
        total = len(self.items_raw_per_task)
        removed: dict[str, int] = {}
        results: dict[int, tuple] = {}
        for idx in range(total):
            self.catch_cancel_or_ctrl_c()
            item_id, idstring, tags, info = self._task_item_tags(idx)
            results[idx] = (item_id, idstring, info, infer_from_tags(tags, rules))
        verdicts = self._gender_model_verdicts(results) if self.gender_mode == 'model' else {}
        model_blocked = 0
        idx: int
        for idx in reversed(range(total)):
            self.catch_cancel_or_ctrl_c()
            item_id, idstring, info, res = results[idx]
            if (v := verdicts.get(item_id)) is not None:
                if v.safety_tag and self.safety_blacklist:  # model safety gate (plan §4.2): blocked like a blacklisted tag
                    self._drop_task_item(idx, item_id, idstring, info, parents)
                    model_blocked += 1
                    continue
                from .gender_model import combine
                res = combine(res, v.result, self.gender_min_conf)
            keep, eff = decide(res, self.gender_select, self.gender_min_conf, self.gender_strict)
            self.gender_results[idstring] = res
            self.gender_stats[str(eff)] = self.gender_stats.get(str(eff), 0) + 1
            if not keep:
                self._drop_task_item(idx, item_id, idstring, info, parents)
                removed[str(eff)] = removed.get(str(eff), 0) + 1
        if model_blocked:
            self.safety_blocked_count += model_blocked
            trace(f'Blocked {model_blocked:d} / {total:d} items (gender model safety gate)')
        if removed:
            count = sum(removed.values())
            self.gender_filtered_count += count
            details = ', '.join(f'{k}: {v:d}' for k, v in sorted(removed.items()))
            trace(f'Filtered out {count:d} / {total:d} items by gender ({details})')

    def _gender_model_verdicts(self, results: dict[int, tuple]) -> dict:
        """image model verdicts for the posts tier 0 could not label confidently (cached per post)"""
        from .gender_model import ModelUnavailable, needs_model, verdicts_for
        posts = [(item_id, str(self.items_raw_per_task[idx])) for idx, (item_id, _, _, res) in results.items()
                 if needs_model(res, self.gender_min_conf)]
        if not posts:
            return {}
        trace(f'Gender model: looking at {len(posts):d} / {len(results):d} item(s) without conclusive tags...')
        try:
            verdicts = verdicts_for(self, posts)
        except ModelUnavailable as err:
            thread_exit(f'Error: {err}')
        decided = sum(1 for v in verdicts.values() if str(v.result.label) != 'unknown')
        trace(f'Gender model: {decided:d} labeled, {len(posts) - decided:d} still unknown')
        self.gender_model_checked += len(posts)
        return verdicts

    def _filter_items_by_module_filters(self, parents: MutableSet[str]) -> None:
        trace('Filtering out items using module filters...')
        self._execute_module_filters(parents)

#
#
#########################################
