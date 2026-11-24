# coding=UTF-8
"""
Lynxx extension 'realbooru' (rb) for https://realbooru.com/ (Gelbooru 0.2 software, HTML pages only).
The site's DAPI answers 'API offline ... shut off indefinitely' (2026-11-19), so this module reads the public HTML pages:
- listing: index.php?page=post&s=list&tags=<tags>&pid=<offset>, 42 posts per page, pid is an offset. Each thumbnail carries
  the post id, the post page link, the thumbnail and the full tag list (title="tag one, tag two", '_' shown as ' '), and the
  site marks video posts with a blue border. Item infos and every filter (tags, safety list) use this, no extra request.
- post page: index.php?page=post&s=view&id=<id>, fetched once per post that is really downloaded: the 'Original' file link,
  the displayed (sample) image, the video player sources, 'Posted at <Mon>, <dd> <yyyy>', score and source.
- search kinds (manifest 'search', Lynxx 2.0.11+, SDK 1.2), listings of their own, in the site's order (checked 2026-11-21):
  'pool:<id>' reads index.php?page=pool&s=show&id=<id>, every post of the pool on one page (<span class="thumb" id="p<ID>">,
  title="<tags> rating:<r> score:<n> user:<name>"); 'favorited_by:<user id>' reads index.php?page=favorites&s=view&id=<id>
  (<span class="thumb"><a id="p<ID>">, title="<tags>", pages of 'pid' offsets). The tags of the query are checked locally
  (the core moves them out of the site query), other site metatags cannot be used with them. Then the post page step above.
  The addresses are checked against the site's robots.txt saved next to the manifest (robots.txt, RFC 9309).
Only file links the post page gives are used (rule R6): a post without them (deleted, hidden) is skipped, never rebuilt.
Not supported: comments, date filter (the date is only on the post page: filtering by it would need one request for every
listed post).
"""
#########################################
#
#

import datetime
import html
import re
from multiprocessing.dummy import current_process
from typing import Never, final
from urllib import parse as url_parse

from bs4 import BeautifulSoup

from lynxx.sdk import (
    FILE_NAME_FULL_MAX_LEN,
    FMT_DATE,
    KNOWN_EXTENSIONS_VID,
    LYNXX_USER_AGENT,
    Downloader,
    DownloadModes,
    HTMLContent,
    ItemInfo,
    ModuleCaps,
    re_tags_exclude_rx,
    re_tags_to_process_rx,
    split_kind,
    thread_exit,
    trace,
)

__all__ = ('DownloaderRb',)

SITENAME = 'https://realbooru.com/'
MODULE_ABBR = 'rb'
ITEMS_PER_PAGE = 42
'''posts on one listing page (fixed by the site, pid is an offset)'''
MAX_SEARCH_DEPTH = 0
'''no page depth limit seen: the last page of all posts (pid=871038) is served'''

ROUTES = {
    'pool': 'index.php?page=pool&s=show&id=',
    'favorites': 'index.php?page=favorites&s=view&id=',
}
'''the module's listing kinds (manifest 'search': 'pool:<id>', 'favorited_by:<user id>') -> their page on the site'''
POOL_PAGE_SIZE = 5000
'''a pool's page shows every post of the pool (401 thumbnails, no paginator, 2026-11-21): one page. A bigger pool would only
make the core ask for the same (cached) page again, its posts already seen'''
FAVORITES_PER_PAGE = 50
'''posts on a favorites page until its paginator was read (the Gelbooru 0.2 default): the paginator's offsets give the size'''

MONTHS = {m: i + 1 for i, m in enumerate(('jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec'))}
VIDEO_BORDER = '#0000ff'
'''the listing marks video posts with style="border: 3px solid #0000ff;"'''
VIDEO_TAGS = frozenset(('webm', 'mp4'))
VIDEO_TAGS_ROUTE = frozenset(('webm', 'mp4', 'video', 'sound'))
'''pool / favorites pages have no video border: the tags tell (the post page decides when the post is downloaded)'''

re_thumb_id = re.compile(r'^s(\d+)$')
re_route_thumb_id = re.compile(r'^p(\d+)$')
re_pid = re.compile(r'[?&;]pid=(\d+)')
re_title_metas = re.compile(r'\s+rating:\S+\s+score:-?\d+\s+user:\S*\s*$')
'''what a pool page appends to the tags of a thumbnail's title'''
re_posted = re.compile(r'Posted (?:at|on) ([A-Za-z]{3})[a-z]*\.?,? (\d{1,2}),? (\d{4})')
re_md5 = re.compile(r'(?:/|thumbnail_|sample_)([\da-f]{32})\.[a-z\d]{2,5}$')
'''md5 named files (thumbnails, newer posts). Older posts have sha1 named originals (40 hex): their md5 stays the thumbnail's'''
re_attr = re.compile(r'\b([a-z_]+)="([^"]*)"')


def _attr(value: object) -> str:
    return html.escape(str(value or ''), quote=True)


def _ext(url: str) -> str:
    name = url.split('?', 1)[0].rsplit('/', 1)[-1]
    return name.rsplit('.', 1)[-1].lower() if '.' in name else ''


@final
class DownloaderRb(Downloader):
    """Realbooru: HTML listing pages (the post listing, a pool, a user's favorites), one post page per downloaded post"""
    CAPABILITIES = frozenset((ModuleCaps.SOURCES, ModuleCaps.PREVIEW))

    def __init__(self) -> None:
        super().__init__()
        self._favorites_size: tuple[str, int] = ('', 0)
        '''(favorites page address, posts per page read from its paginator)'''

    # ----- module description

    def _get_module_specific_default_headers(self) -> dict[str, str]:
        return {'User-Agent': LYNXX_USER_AGENT}

    def _get_module_specific_default_cookies(self) -> dict[str, str]:
        return {}

    def _get_min_request_interval(self) -> float:
        return 1.0  # html pages: at least 1 second between requests (R4); robots.txt has no Crawl-delay

    def _is_pool_search_conversion_required(self) -> bool:
        return False

    def _is_fav_search_conversion_required(self) -> bool:
        return False

    def _is_fav_search_single_step(self) -> bool:
        return True

    def _supports_native_id_filter(self) -> bool:
        return True  # 'id:<N' / 'id:>N' meta tags of gelbooru 0.2

    def _get_id_bounds(self) -> Never:
        raise NotImplementedError

    def _get_sitename(self) -> str:
        return SITENAME

    def _get_module_abbr(self) -> str:
        return MODULE_ABBR

    def _get_module_abbr_p(self) -> str:
        return f'{MODULE_ABBR}_'

    def _get_items_per_page(self) -> int:
        route = self._route()
        return POOL_PAGE_SIZE if route == 'pool' else self._favorites_page_size() if route == 'favorites' else ITEMS_PER_PAGE

    def _get_max_search_depth(self) -> int:
        return MAX_SEARCH_DEPTH

    def _form_item_string_manually(self, *ignored) -> Never:
        raise NotImplementedError

    def _is_search_overload_page(self, raw_html_page: BeautifulSoup) -> bool:
        return len(raw_html_page.find_all('h1', string=re.compile(r'Search is overloaded'))) > 0

    def get_re_tags_to_process(self) -> re.Pattern:
        return re_tags_to_process_rx

    def get_re_tags_to_exclude(self) -> re.Pattern:
        return re_tags_exclude_rx

    def _get_tags_concat_char(self) -> str:
        return '+'

    def _get_idval_equal_seaparator(self) -> str:
        return ':'

    def _split_or_group_into_tasks_always(self) -> bool:
        return True

    def _can_extract_item_info_without_fetch(self) -> bool:
        return True  # id, tags, md5 and thumbnail are on the listing (and on pool / favorites pages)

    def _consume_custom_module_tags(self, tags: str) -> str:
        return tags

    # ----- search

    def _route(self) -> str:
        """'pool' / 'favorites' when the query is one of the module's listing kinds (manifest 'search'), '' for the post listing"""
        kind = self.current_kind
        return kind.id if kind is not None and kind.id in ROUTES else ''

    def _form_tags_search_address(self, tags: str, *_) -> str:
        # no page size: the site's pages are fixed (the app's Preview then uses the module's page size)
        kind, rest = split_kind(tags, MODULE_ABBR)
        if kind is None or kind.id not in ROUTES:
            return f'{SITENAME}index.php?page=post&s=list&tags={tags or "all"}'
        tag = f'{kind.token}{kind.value}'
        if not (kind.value.isascii() and kind.value.isdigit()):  # the manifest's value regex says so: nothing else goes into the url
            thread_exit(f'Error: [RB] \'{tag}\': the id must be a number', code=-462)
        if rest:
            others = ' '.join(url_parse.unquote(t) for t in rest.split(self._get_tags_concat_char()) if t)
            thread_exit(f'Error: [RB] \'{tag}\' reads the {"pool" if kind.id == "pool" else "favorites"} page of the site, which takes no '
                        f'search: \'{others}\' cannot be used with it (plain tags and -tags are checked locally)', code=-463)
        url = f'{SITENAME}{ROUTES[kind.id]}{kind.value}'
        self.require_robots_allowed(url)
        return url

    def _form_page_num_address(self, n: int) -> str:
        route = self._route()
        if route == 'pool' or (route == 'favorites' and n == 0):
            return self.url  # a pool is one page; the first favorites page as the site links it
        return f'{self.url}&pid={n * self._get_items_per_page():d}'

    def _fetch_page(self, url: str, tries: int | None, code: int) -> HTMLContent:
        raw_html = self.fetch_html(url, tries=tries, do_cache=True)  # the scan asks for the same pages: fetched once
        if raw_html is None:
            thread_exit('ERROR: GetItemsQueSize: unable to retreive html', code=code)
        return raw_html

    @staticmethod
    def _paginator_offsets(page: BeautifulSoup) -> tuple[int, int]:
        """(page size, last offset) from the paginator's links ('pid' offsets in href or onclick), (0, 0) without other pages.
        The smallest offset is the second page's: the size of a page (the first page links the second one)"""
        paginator = page.find('div', id='paginator')
        pids: set[int] = set()
        for a in (paginator.find_all('a') if paginator is not None else []):
            for name in ('href', 'onclick'):
                pids.update(int(m.group(1)) for m in re_pid.finditer(str(a.get(name) or '')))
        pids.discard(0)
        return (min(pids), max(pids)) if pids else (0, 0)

    def _favorites_page_size(self) -> int:
        """posts per favorites page, from the paginator of its first page (the count reads that page too: one request)"""
        url = self.url
        if not url.startswith(f'{SITENAME}{ROUTES["favorites"]}'):
            return FAVORITES_PER_PAGE  # address not built yet (the app's Preview asks first)
        if self._favorites_size[0] != url:
            page = self._fetch_page(url, None, -444).html
            size, _ = self._paginator_offsets(page)
            shown = len(self._get_all_post_tags(page))
            if 0 < size < shown:  # a page holds at most its size (fewer when the site hides some): not offsets
                thread_exit(f'Error: [RB] unexpected favorites pages: the first page shows {shown:d} posts but its paginator steps by '
                            f'{size:d}: not read, the result would be wrong', code=-465)
            self._favorites_size = (url, size or FAVORITES_PER_PAGE)
        return self._favorites_size[1]

    def _get_all_post_tags(self, raw_html_page: BeautifulSoup) -> list:
        """thumbnails of a listing, pool or favorites page as item strings, once per post, in the page's order:
        <post_id="…" page_url="…" preview_url="…" video="0|1" tags="…">"""
        out: list[str] = []
        seen: set[str] = set()
        for el in raw_html_page.find_all(('div', 'span'), class_='thumb'):
            a = el.find('a', href=True)
            img = el.find('img')
            if a is None or img is None:
                continue
            title = str(img.get('title', ''))
            if el.name == 'div':  # the post listing: <div class="col thumb" id="s<ID>">
                if not (m := re_thumb_id.match(str(el.get('id', '')))):
                    continue
                post_id = m.group(1)
                page_url = str(a['href'])
                if not page_url.startswith('https://'):
                    page_url = f'{SITENAME}{page_url.lstrip("/")}'
                # title: the post's tags joined by ', ' with '_' shown as ' ' (a tag never holds a space)
                tags = [t.strip().replace(' ', '_') for t in title.split(', ') if t.strip()]
                video = VIDEO_BORDER in str(img.get('style', '')).lower() or any(t in VIDEO_TAGS for t in tags)
            else:  # a pool: <span class="thumb" id="p<ID>">; favorites: <span class="thumb"><a id="p<ID>">
                if not (m := re_route_thumb_id.match(str(el.get('id', ''))) or re_route_thumb_id.match(str(a.get('id', '')))):
                    continue
                post_id = m.group(1)
                page_url = f'{SITENAME}index.php?page=post&s=view&id={post_id}'
                # title: the post's tags separated by spaces (a pool page adds 'rating:… score:… user:…')
                tags = re_title_metas.sub('', title).split()
                video = any(t in VIDEO_TAGS_ROUTE for t in tags)
            if post_id in seen:  # a pool may hold a post twice
                continue
            seen.add(post_id)
            preview = str(img.get('src', ''))
            if preview.startswith('//'):
                preview = f'https:{preview}'
            out.append(f'<post_id="{post_id}" page_url="{_attr(page_url)}" preview_url="{_attr(preview)}" '
                       f'video="{int(video):d}" tags="{_attr(" ".join(tags))}">')
        return out

    def _get_items_query_size_or_html(self, url: str, tries: int | None = None) -> int:
        route = self._route()
        if route == 'pool':
            return self._pool_size(url, tries)
        raw_html = self._fetch_page(url if route else f'{url}&pid=0', tries, -444)  # same url as page 0: fetched once
        first_count = len(self._get_all_post_tags(raw_html.html))
        _, last_pid = self._paginator_offsets(raw_html.html)
        if last_pid <= 0:
            if route and first_count == 0:
                trace(f'Warning (W2): [RB] the site shows no favorites of user {url.rsplit("=", 1)[-1]} (none, or no user with this id)')
            return first_count
        page_size = self._get_items_per_page()
        raw_html = self._fetch_page(self._form_page_num_address(last_pid // page_size), tries, -445)
        # items on all full pages plus items on the last page
        return (last_pid // page_size) * page_size + len(self._get_all_post_tags(raw_html.html))

    def _pool_size(self, url: str, tries: int | None) -> int:
        """posts of a pool: its page shows them all, in the pool's order"""
        page = self._fetch_page(url, tries, -444).html
        pool_id = url.rsplit('=', 1)[-1]
        show = page.find('div', id='pool-show')
        if show is None:
            thread_exit(f'Error: [RB] the site shows no pool {pool_id}: there is no pool with this id, or it is private (shown only to '
                        f'its creator)', code=-464)
        title = show.find('h4')
        name = title.get_text(' ', strip=True).removeprefix('Pool:').strip() if title is not None else ''
        count = len(self._get_all_post_tags(page))
        named = f' \'{name}\'' if name else ''
        trace(f'[RB] pool {pool_id}{named}: {count:d} post(s), downloaded in the pool\'s order')
        if count == 0:
            trace(f'Warning (W2): [RB] pool {pool_id}{named} shows no posts (an empty pool, or one the site does not show to guests)')
        return count

    # ----- items (listing strings)

    @staticmethod
    def _item_attr(h: str, name: str) -> str:
        idx = h.find(f' {name}="')
        if idx < 0:
            return ''
        idx += len(name) + 3
        return html.unescape(h[idx:h.find('"', idx)])

    def _local_addr_from_string(self, h: str) -> str:
        return h

    def _extract_id(self, addr: str) -> str:
        id_idx = addr.find('post_id="') + len('post_id="')
        return addr[id_idx:addr.find('"', id_idx + 1)]

    def _is_video(self, h: str) -> bool:
        return self._item_attr(h, 'video') == '1'

    def _get_item_html(self, h: str) -> BeautifulSoup | None:
        page = self.fetch_html(self._item_attr(h, 'page_url'), do_cache=True)
        return page.html if page is not None else None

    def _extract_post_date(self, raw_or_html: BeautifulSoup | str) -> str:
        text = raw_or_html.get_text(' ') if isinstance(raw_or_html, BeautifulSoup) else str(raw_or_html)
        if m := re_posted.search(text):
            month = MONTHS.get(m.group(1).lower())
            if month:
                return datetime.date(int(m.group(3)), month, int(m.group(2))).strftime(FMT_DATE)
        return ''

    def _extract_item_info(self, item: str) -> ItemInfo:
        try:
            item_info = ItemInfo()
            if self.is_killed():
                return item_info
            item_info.id = self._extract_id(item)
            item_info.tags = self._item_attr(item, 'tags')
            if m := re_md5.search(self._item_attr(item, 'preview_url').split('?', 1)[0]):  # favorites: 'thumbnail_<md5>.jpg?<id>'
                item_info.md5 = m.group(1)
            return item_info
        except Exception:
            self._on_thread_exception(current_process().name)
            raise

    # ----- post page

    @staticmethod
    def _post_links(page: BeautifulSoup) -> tuple[str, str, bool]:
        """(original, displayed, is video) file links of a post page, as the site gives them ('' if missing)"""
        original = page.find('a', string='Original')
        orig = str(original.get('href', '')) if original is not None else ''
        video = page.find('video')
        if video is not None:
            sources = [str(s.get('src', '')) for s in video.find_all('source') if s.get('src')]
            return orig or (sources[0] if sources else ''), '', True
        image = page.find('img', id='image')
        shown = str(image.get('src', '')) if image is not None else ''
        return orig or shown, shown, _ext(orig or shown) in KNOWN_EXTENSIONS_VID

    def _update_info(self, item_id: str, page: BeautifulSoup, link: str) -> None:
        """date, source, score, exact tags, file extension and md5 of a downloaded post: shown by the info dumps"""
        info = self._item_info_for(self._item_idstring(item_id))
        if date := self._extract_post_date(page):
            info.date = date
        if (source := page.find('input', id='source')) is not None and len(str(source.get('value', '')).strip()) > 1:
            info.source = str(source.get('value')).strip()
        if (score := page.find('span', id=f'psc{item_id}')) is not None and score.text.strip().lstrip('-').isdigit():
            info.score = score.text.strip()
        if (tags := page.find('textarea', id='tags')) is not None and tags.text.strip():
            info.tags = ' '.join(tags.text.split())
        info.ext = _ext(link)
        if m := re_md5.search(link.split('?', 1)[0]):
            info.md5 = m.group(1)

    # ----- download

    def _get_image_address(self, h: str) -> tuple[str, str]:
        return h, _ext(h)

    def _get_video_address(self, h: str) -> tuple[str, str]:
        return h, _ext(h)

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
            item_id = self._extract_id(raw)
            if self.download_mode == DownloadModes.SKIP:
                self._inc_proc_count()
                return
            page: HTMLContent | None = self.fetch_html(self._item_attr(raw, 'page_url'))
            if page is None:
                trace(f'ERROR: ProcItem: unable to retreive html for {item_id}!', True)
                self._inc_proc_count()
                return
            orig, shown, is_vid = self._post_links(page.html)
            link = shown if (self.low_res and not is_vid and shown) else orig
            if not link or not link.startswith('https://') or not _ext(link):
                trace(f'Warning (W2): ProcItem: no file link for {item_id}, seems like post was deleted or hidden', True)
                self._inc_proc_count()
                return
            if self.skip_videos if is_vid else self.skip_images:  # pool / favorites pages tell videos by their tags only
                trace(f'Info: ProcItem: {item_id} is {"a video" if is_vid else "an image"} (its post page): skipped', True)
                self._inc_proc_count()
                return
            self._update_info(item_id, page.html, orig or link)
            if self.add_filename_prefix is True:
                item_id = f'{self._get_module_abbr_p()}{item_id}'
            if is_vid:
                self._process_video(link, item_id)
            else:
                self._process_image(link, item_id)
            self._inc_proc_count()
        except Exception:
            self._on_thread_exception(current_process().name)
            raise

#
#
#########################################
