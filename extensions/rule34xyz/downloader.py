# coding=UTF-8
"""
Lynxx extension 'rule34xyz' (rz) for https://rule34.xyz/ (Angular SPA rendered on the server).
Search: the site's own search pages, GET https://rule34.xyz/<tag1>%7C<tag2>/?page=N (html, 30 posts, pages 1..300, deeper pages
are clamped to page 300 by the site). The site's search API (api/v2/post/search/root) is POST only: not used.
Post: GET https://rule34.xyz/api/v2/post/<id> (json: tags with types, dates, size, likes, sources, 'files' = available variants).
File: the site's own "Download" action (post menu): variant 10 (image) / 100 (video) of the post's 'files', served by the
storage the post lists (app.json: storage 2 = Bunny pull zone 'rule34xyz'). See specs.md (R6).
1.1.1: -lowres takes an image's variant 14 (picsmall.jpg, the post page's default image) and -max_vres H (Lynxx 2.0.14+) a video's
reduced MP4 (111 / 112 / 113 / 114: 360 / 480 / 720 / 1080) when the original is above H, only when the post lists that variant.
The class only builds urls and parses responses: every request goes through the core (self.fetch_html).
"""
#########################################
#
#

import datetime
import json
import re
from collections.abc import MutableSet
from multiprocessing.dummy import current_process
from typing import Never, final
from urllib import parse as url_parse

from bs4 import BeautifulSoup

from lynxx.sdk import (
    FILE_NAME_FULL_MAX_LEN,
    FMT_DATE,
    LYNXX_USER_AGENT,
    Downloader,
    DownloadModes,
    HTMLContent,
    ItemInfo,
    ModuleCaps,
    re_tags_exclude_rx,
    re_tags_to_process_rx,
    thread_exit,
    trace,
)

__all__ = ('DownloaderRz',)

SITENAME = 'https://rule34.xyz/'
MODULE_ABBR = 'rz'
ITEMS_PER_PAGE = 30
'''posts on one search page'''
MAX_PAGES = 300
'''deepest search page: ?page=301 and above answer page 300 again'''
TAGS_CONCAT = '+'
SITE_TAGS_SEP = '%7C'
'''the site joins search tags with '|' in the path (/overwatch|brown_hair), at most 5 tags for guests (more are dropped)'''
STORAGE_BASES = {2: 'https://rule34xyz.b-cdn.net/'}
'''https://rule34.xyz/app.json "storage": {"id": 2, "type": 1 (Cdn), "parameters": {"PullZoneName": "rule34xyz"}}'''
FILE_IMAGE = '10'
FILE_VIDEO = '100'
'''variants of the site's Download action: post.type === 1 ? 100 : 10 (main-*.js, download())'''
FILE_IMAGE_SMALL = '14'
'''-lowres: the image the post page shows by default (image scale "optimized": variant 14 when the post lists it, else 10)'''
VIDEO_LADDER = {'111': 360, '112': 480, '113': 720, '114': 1080}
'''-max_vres: the reduced h264 MP4 files of the site's player (variant -> 'p' height; the player plays 113, else 100, and 112 on
phones); the original (100) has the post's own size'''
FILE_NAMES = {FILE_IMAGE: 'pic.jpg', FILE_VIDEO: 'mov.mp4', FILE_IMAGE_SMALL: 'picsmall.jpg',
              '111': '360.mp4', '112': 'mov480.mp4', '113': 'mov720.mp4', '114': '1080.mp4'}
'''file name suffix of a variant: posts/<id // 1000>/<id>/<id>.<suffix>, as the site's postFileResolverService names them
(its map, read 2026-11-22: 11 pic256.jpg, 13 picpreview.jpg, 30 picavif.avif, 34 picsmallavif.avif, 101 mov256.mp4, 2xx hevc,
3xx av1, ...: not used)'''
POST_TYPE_VIDEO = 1
POST_STATUS_POSTED = 2
TAG_ID_SIGN_IN_REQUIRED = -10
'''post page: authRequiredIsEnabled = post.tags.find(t => t.id === -10): the site shows such a post to members only'''

re_post_id = re.compile(r'post_id="(\d+)"|/post/(\d+)')


def _attr(value: object) -> str:
    return str(value if value is not None else '').replace('"', '\'').replace('\n', ' ')


def _url_segment(tag: str) -> str:
    """one search tag as the site's router writes it in the path (encodeURIComponent, then ':' '@' '$' ',' '&' kept)"""
    return url_parse.quote(url_parse.unquote(tag).replace(' ', '_'), safe='!*\'@:$,&')


@final
class DownloaderRz(Downloader):
    """Search pages (html) + one json request per post"""
    CAPABILITIES = frozenset((ModuleCaps.DATE_FILTER, ModuleCaps.PREVIEW, ModuleCaps.SOURCES, 'video_quality_choice'))
    '''video_quality_choice: a plain string, cores before 2.0.14 have no ModuleCaps name for it (the extension still loads there)'''

    def __init__(self) -> None:
        super().__init__()
        self._task_tags = ''
        self._unavailable: set[str] = set()
        self._local_negative: set[str] = set()

    # ----- module description

    def _get_module_specific_default_headers(self) -> dict[str, str]:
        return {'User-Agent': LYNXX_USER_AGENT}

    def _get_module_specific_default_cookies(self) -> dict[str, str]:
        return {}

    def _get_min_request_interval(self) -> float:
        return 1.0  # html pages, no published limit, no Crawl-delay (R4)

    def _is_pool_search_conversion_required(self) -> bool:
        return False

    def _is_fav_search_conversion_required(self) -> bool:
        return False

    def _is_fav_search_single_step(self) -> bool:
        return True

    def _supports_native_id_filter(self) -> bool:
        return False

    def _get_id_bounds(self) -> tuple[int, int]:
        return 0, 0

    def _get_sitename(self) -> str:
        return SITENAME

    def _get_module_abbr(self) -> str:
        return MODULE_ABBR

    def _get_module_abbr_p(self) -> str:
        return f'{MODULE_ABBR}_'

    def _get_items_per_page(self) -> int:
        return ITEMS_PER_PAGE

    def _get_max_search_depth(self) -> int:
        return ITEMS_PER_PAGE * MAX_PAGES

    def _form_item_string_manually(self, *ignored) -> Never:
        raise NotImplementedError

    def _is_search_overload_page(self, raw_html_page: BeautifulSoup) -> bool:
        return False

    def get_re_tags_to_process(self) -> re.Pattern:
        return re_tags_to_process_rx

    def get_re_tags_to_exclude(self) -> re.Pattern:
        return re_tags_exclude_rx

    def _get_tags_concat_char(self) -> str:
        return TAGS_CONCAT

    def _get_idval_equal_seaparator(self) -> str:
        return ':'

    def _split_or_group_into_tasks_always(self) -> bool:
        return True

    def _can_extract_item_info_without_fetch(self) -> bool:
        return False

    # ----- search

    def _consume_custom_module_tags(self, tags: str) -> str:
        """The site ignores '-tag' (/overwatch|-brown_hair/ gives the posts of /overwatch/): plain negative tags are moved to the
        core's own local exclusion filter instead of the url. The site has no wildcard search"""
        if not tags:
            return tags
        keep: list[str] = []
        for tag in tags.split(TAGS_CONCAT):
            if tag.startswith('-') and len(tag) > 1:
                self._exclude_locally(tag)
            elif '*' in tag:
                thread_exit(f'Error: [{MODULE_ABBR.upper()}] wildcards are not supported by the website: \'{url_parse.unquote(tag)}\'', -706)
            elif tag:
                keep.append(tag)
        return TAGS_CONCAT.join(keep)

    def _exclude_locally(self, neg_tag: str) -> None:
        name = url_parse.unquote(neg_tag[1:]).replace(' ', '_').lower()
        if name in self._local_negative:
            return
        tasks = [str(t).split(TAGS_CONCAT) for t in self.tags_str_arr]
        if any(neg_tag not in task for task in tasks):
            thread_exit(f'Error: [{MODULE_ABBR.upper()}] the website ignores negative tags and \'{neg_tag}\' is inside an \'or\' group: '
                        f'it cannot be checked locally for one search only. Move it out of the group', -707)
        self._local_negative.add(name)
        pattern = re.escape(name).replace(r'\*', '.*').replace(r'\?', '.')
        self.neg_and_groups.append([re.compile(f'^{pattern}$')])
        trace(f'Info: [{MODULE_ABBR.upper()}] the website ignores negative tags: \'{neg_tag}\' is checked locally')

    def _search_url(self, tags: str, page: int) -> str:
        path = SITE_TAGS_SEP.join(_url_segment(t) for t in tags.split(TAGS_CONCAT) if t) if tags else ''
        # trailing '/': a last tag with a dot (d.va) is otherwise answered 404 like a missing static file
        return f'{SITENAME}{path}/?page={page + 1:d}' if path else f'{SITENAME}?page={page + 1:d}'

    def _form_tags_search_address(self, tags: str, maxlim: int | None = None) -> str:
        self._task_tags = tags
        return self._search_url(tags, 0)

    def _form_page_num_address(self, n: int) -> str:
        return self._search_url(self._task_tags, n)

    @staticmethod
    def _is_search_page(soup: BeautifulSoup) -> bool:
        return soup.find('app-home-page') is not None

    def _get_all_post_tags(self, raw_html_page: BeautifulSoup) -> list:
        out: list[str] = []
        for a in raw_html_page.select('a.box[data-post-id]'):
            post_id = str(a.get('data-post-id', ''))
            if not post_id.isdigit():
                continue
            img = a.find('img', src=True)
            preview = str(img.get('src', '')) if img is not None else ''
            video = '1' if a.find('video') is not None else '0'
            out.append(f'<post_id="{post_id}" video="{video}" preview_url="{_attr(preview)}">')
        return out

    def _page_len(self, page: int, tries: int | None) -> int:
        raw = self.fetch_html(self._search_url(self._task_tags, page), tries=tries, do_cache=True)
        if raw is None:
            thread_exit('ERROR: GetItemsQueSize: unable to retreive html', code=-444)
        if not self._is_search_page(raw.html):
            thread_exit(f'ERROR: [{MODULE_ABBR.upper()}] not a search page (bot check or site change?)', code=-447)
        return len(self._get_all_post_tags(raw.html))

    def _get_items_query_size_or_html(self, url: str, tries: int | None = None) -> int:
        """The search pages show no total: binary search for the last non-empty page (~log2(300) requests, cached)"""
        if (first := self._page_len(0, tries)) < ITEMS_PER_PAGE:
            return first
        lo, hi = 0, MAX_PAGES - 1
        if self._page_len(hi, tries) >= ITEMS_PER_PAGE:
            return MAX_PAGES * ITEMS_PER_PAGE
        while hi - lo > 1:  # page lo is full, page hi is not
            mid = (lo + hi) // 2
            if self._page_len(mid, tries) >= ITEMS_PER_PAGE:
                lo = mid
            else:
                hi = mid
        return hi * ITEMS_PER_PAGE + self._page_len(hi, tries)

    # ----- items

    def _local_addr_from_string(self, h: str) -> str:
        return h

    def _extract_id(self, addr: str) -> str:
        m = re_post_id.search(addr)
        return (m.group(1) or m.group(2)) if m else ''

    def _is_video(self, h: str) -> bool:
        return 'video="1"' in h

    @staticmethod
    def _post_url(post_id: str) -> str:
        return f'{SITENAME}api/v2/post/{post_id}'

    def _get_item_html(self, h: str) -> HTMLContent | None:
        return self.fetch_html(self._post_url(self._extract_id(h)), do_cache=True)

    def _post_json(self, post_id: str) -> dict | None:
        raw = self.fetch_html(self._post_url(post_id), do_cache=True)
        if raw is None:  # 204 No Content: deleted or missing
            return None
        try:
            data = json.loads(raw.raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) and str(data.get('id', '')) == post_id else None

    @staticmethod
    def _post_date(data: dict) -> str | None:
        value = str(data.get('posted') or data.get('created') or '')
        try:
            return datetime.datetime.fromisoformat(value).strftime(FMT_DATE)
        except ValueError:
            return None

    def _extract_post_date(self, raw_or_html: HTMLContent | str | None) -> str | None:
        raw = raw_or_html.raw if isinstance(raw_or_html, HTMLContent) else raw_or_html
        try:
            data = json.loads(raw or b'')
        except ValueError:
            return None
        return self._post_date(data) if isinstance(data, dict) else None

    @staticmethod
    def _storage(data: dict, variant: str) -> str:
        """the known storage of a variant the post lists, '' if it does not list it (R6: a file is never rebuilt)"""
        storages = (data.get('files') or {}).get(variant)
        if not isinstance(storages, list):
            return ''
        return next((STORAGE_BASES[s] for s in storages if s in STORAGE_BASES), '')

    @classmethod
    def _variant(cls, data: dict, max_height: int, low_res: bool) -> str:
        """the variant downloaded: the original, or with -lowres an image's small one, with -max_vres H a video's best reduced file
        not above H when the original is above it (else the smallest one listed); only variants the post lists"""
        if data.get('type') != POST_TYPE_VIDEO:
            return FILE_IMAGE_SMALL if low_res and cls._storage(data, FILE_IMAGE_SMALL) else FILE_IMAGE
        sides = [v for v in (data.get('width'), data.get('height')) if isinstance(v, int) and v > 0]
        original = min(sides) if sides else 0  # the 'p' number: the shorter side
        if not max_height or not original or original <= max_height:
            return FILE_VIDEO
        reduced = sorted((q, v) for v, q in VIDEO_LADDER.items() if q < original and cls._storage(data, v))
        fits = [v for q, v in reduced if q <= max_height]
        return fits[-1] if fits else reduced[0][1] if reduced else FILE_VIDEO

    def _file_url(self, data: dict) -> str:
        """the file downloaded, '' if the site does not offer the post's Download action to guests (R6: never rebuilt)"""
        if data.get('status') != POST_STATUS_POSTED:
            return ''
        if any(isinstance(t, dict) and t.get('id') == TAG_ID_SIGN_IN_REQUIRED for t in data.get('tags') or []):
            return ''
        if not self._storage(data, FILE_VIDEO if data.get('type') == POST_TYPE_VIDEO else FILE_IMAGE):
            return ''  # site: "You can't download this post"
        # getattr: cores before 2.0.14 have no max_video_height (the original video then, as in 1.1.0)
        variant = self._variant(data, getattr(self, 'max_video_height', 0) or 0, bool(getattr(self, 'low_res', False)))
        post_id = int(data['id'])
        return f'{self._storage(data, variant)}posts/{post_id // 1000:d}/{post_id:d}/{post_id:d}.{FILE_NAMES[variant]}'

    def _extract_item_info(self, item: str) -> ItemInfo:
        try:
            item_info = ItemInfo()
            if self.is_killed():
                return item_info
            post_id = self._extract_id(item)
            item_info.id = post_id
            data = self._post_json(post_id)
            if data is None:
                trace(f'Warning (W2): Item info was not extracted for {post_id}', True)
                self._unavailable.add(post_id)
                return item_info
            tags = (str(t.get('value', '')).strip().replace(' ', '_') for t in data.get('tags') or [] if isinstance(t, dict))
            item_info.tags = ' '.join(t for t in tags if t)
            item_info.width = str(data.get('width') or '')
            item_info.height = str(data.get('height') or '')
            item_info.score = str(data.get('likes') or 0)
            item_info.date = self._post_date(data) or ''
            sources = (data.get('data') or {}).get('sources') or []
            item_info.source = ' '.join(str(s) for s in sources if s)
            file_url = self._file_url(data)
            item_info.ext = file_url[file_url.rfind('.') + 1:] if file_url else ''
            if not file_url:
                self._unavailable.add(post_id)
            return item_info
        except Exception:
            self._on_thread_exception(current_process().name)
            raise

    def _execute_module_filters(self, parents: MutableSet[str]) -> None:
        abbrp = self._get_module_abbr_p()
        total_count_old = len(self.items_raw_per_task)
        removed = 0
        for idx in reversed(range(total_count_old)):
            post_id = self._extract_id(self.items_raw_per_task[idx])
            if post_id not in self._unavailable:
                continue
            idstring = f'{(abbrp if self.add_filename_prefix else "")}{post_id}'
            parents.discard(post_id)
            self.item_info_dict_per_task.pop(idstring, None)
            del self.items_raw_per_task[idx]
            self.filtered_out_ids_cache.add(idstring)
            removed += 1
        if removed:
            trace(f'Filtered out {removed:d} / {total_count_old:d} items: no file for guests (hidden, members only or deleted)!')

    # ----- download

    def _get_image_address(self, h: str) -> tuple[str, str]:
        return h, h[h.rfind('.'):]

    def _get_video_address(self, h: str) -> tuple[str, str]:
        return h, h[h.rfind('.'):]

    def _send_to_download(self, raw: str, item_id: str, is_video: bool) -> None:
        address, fmt = self._get_video_address(raw) if is_video else self._get_image_address(raw)
        subfolder = self.subfolder_vid if is_video else self.subfolder_img
        hint_maxlen = FILE_NAME_FULL_MAX_LEN - (len(self.dest_base_s.as_posix()) + len(item_id) + len(fmt))
        self._download(address, item_id, self.dest_base_s / subfolder / f'{self._try_append_extra_info(item_id, hint_maxlen)}{fmt}')

    # threaded
    def _process_item(self, raw: str) -> None:
        if self.is_killed():
            return
        try:
            post_id = self._extract_id(raw)
            if self.download_mode == DownloadModes.SKIP:
                self._inc_proc_count()
                return
            data = self._post_json(post_id)  # cached by the info step
            file_url = self._file_url(data) if data is not None else ''
            item_id = f'{self._get_module_abbr_p()}{post_id}' if self.add_filename_prefix is True else post_id
            if not file_url:
                trace(f'Warning (W2): ProcItem: no file for guests for {post_id} (hidden, members only or deleted), skipped', True)
            elif file_url.endswith('.mp4'):
                self._process_video(file_url, item_id)
            else:
                self._process_image(file_url, item_id)
            self._inc_proc_count()
        except Exception:
            self._on_thread_exception(current_process().name)
            raise

#
#
#########################################
