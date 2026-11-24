# coding=UTF-8
"""
Lynxx extension 'zerochan' (zc) for https://www.zerochan.net/
Written for lynxx.extgen (engine 'custom') on 2026-11-19. Review before installing.

Zerochan's read-only API (https://www.zerochan.net/api): GET only, 'json' or 'xml' appended to a normal url, 60 requests per
minute at most; robots.txt asks for 10 s between requests, so this module waits 10 s. Used here:
- listing: '/<tag>[,<tag>...]?p=N&l=100&s=id&xml' (RSS): 100 posts per page, newest first, every tag of each post
  (media:keywords), the 600 px sample (media:content) and the 240 px thumbnail. No file url, no date. The same listing with
  'json' answered HTTP 500 with a broken body on 2026-11-19: the core cannot read it;
- one post: '/<id>?json': its full size file url ('full'), asked only for the posts that are downloaded (after the tag and
  safety filters), one request each.
Guests only see public posts (members only posts are not in the listing; no url is built for them, rule R6) and at most 100
listing pages (page 100 answered, page 600 answered 403 'Page number too high').
The API asks every client to send '<project> - <Zerochan username>' as its User-Agent: the user sets it in the app's per-site
headers ({"zc": {"User-Agent": "Lynxx - <username>"}}), which take precedence over the module default.
"""
#########################################
#
#

import html
import json
import re
from typing import final
from urllib import parse as url_parse

from lynxx.sdk import (
    FILE_NAME_FULL_MAX_LEN,
    KNOWN_EXTENSIONS_IMG,
    DownloaderJsonApi,
    JsonPost,
    MediaKind,
    ModuleCaps,
    thread_exit,
    trace,
)

__all__ = ('DownloaderZc',)

SITENAME = 'https://www.zerochan.net/'
MODULE_ABBR = 'zc'
PER_PAGE = 100
'''posts on one RSS listing page (the xml listing answers 100, whatever 'l' says)'''
GUEST_MAX_PAGES = 100
'''deepest listing page Zerochan serves to guests that was seen working (p=100 ok, p=600: 'Page number too high')'''

re_rss = re.compile(r'<rss\b[^>]*>\s*<channel>')
re_item = re.compile(r'<item>(.*?)</item>', re.DOTALL)
re_guid = re.compile(r'<guid>[^<]*/(\d+)</guid>')
re_keywords = re.compile(r'<media:keywords>([^<]*)</media:keywords>')
re_content = re.compile(r'<media:content\s([^>]*)>')
re_thumbnail = re.compile(r'<media:thumbnail\s([^>]*)>')
re_attr = re.compile(r'(\w+)="([^"]*)"')
re_total = re.compile(r'<description>Zerochan has ([\d,]+) ')
re_full = re.compile(r'"full"\s*:\s*"(https://[^"\\]+)"')


def _attrs(m: re.Match | None) -> dict[str, str]:
    return {k: html.unescape(v) for k, v in re_attr.findall(m.group(1))} if m else {}


def _int(value: object) -> int:
    try:
        return int(str(value).replace(',', ''))
    except (TypeError, ValueError):
        return 0


def _tags(keywords: str) -> list[str]:
    """'Female, Fanart, Twin\\n\\tTails, ...' (the feed wraps long lines inside tag names) -> ['female', 'fanart', 'twin_tails', ...]"""
    out: list[str] = []
    for raw in html.unescape(keywords).split(','):
        if name := '_'.join(raw.split()).lower():
            out.append(name)
    return list(dict.fromkeys(out))


@final
class DownloaderZc(DownloaderJsonApi):
    """Zerochan read-only API: RSS listing (tags) + one '/<id>?json' request per downloaded post (file url)"""
    CAPABILITIES = frozenset((ModuleCaps.PREVIEW,))
    SITE = SITENAME
    ABBR = MODULE_ABBR
    ITEMS_PER_PAGE = PER_PAGE
    MAX_PAGES = 0
    '''the guest page limit is applied by the count (_get_items_query_size_or_html): the core never asks beyond it'''
    TAGS_CONCAT = '+'
    ACCEPT = 'application/xml, application/json;q=0.9, */*;q=0.8'

    def _get_min_request_interval(self) -> float:
        return 10.0  # robots.txt 'Crawl-delay: 10' (the API itself allows 60 requests per minute)

    # ----- search

    def _search_url(self, tags: str, page: int, limit: int) -> str:
        # tags: joined with '+', spaces of a tag as '_' (Lynxx) -> Zerochan path '/Tag+One,Tag+Two' (urlencoded like the site)
        names = [url_parse.unquote(t).replace('_', ' ').strip() for t in tags.split('+') if t]
        if any(n.startswith('-') for n in names):
            thread_exit(f'ERROR: [{MODULE_ABBR.upper()}] Zerochan cannot exclude tags on the server: keep at most one positive tag in '
                        f'the website query (manifest maxTags), -tags are then checked locally', code=-448)
        path = ','.join(url_parse.quote_plus(n, safe='') for n in names if n)
        return f'{SITENAME}{path}?p={page + 1:d}&l={PER_PAGE:d}&s=id&xml'

    def _parse(self, raw: bytes) -> dict | None:
        text = raw.decode('utf-8', errors='replace')
        if not re_rss.search(text):
            return None  # not an RSS answer: html page (unknown tag), error page...
        posts: list[dict] = []
        for m in re_item.finditer(text):
            body = m.group(1)
            if not (guid := re_guid.search(body)):
                continue
            kw = re_keywords.search(body)
            content = _attrs(re_content.search(body))
            posts.append({'id': guid.group(1), 'tags': _tags(kw.group(1)) if kw else [], 'sample': content.get('url', ''),
                          'width': _int(content.get('width')), 'height': _int(content.get('height')),
                          'thumbnail': _attrs(re_thumbnail.search(body)).get('url', '')})
        total = re_total.search(text)
        return {'posts': posts, 'total': _int(total.group(1)) if total else None}

    def _posts(self, data: object) -> list[dict] | None:
        return data.get('posts') if isinstance(data, dict) else None

    def _post(self, p: dict) -> JsonPost | None:
        post_id = str(p.get('id', ''))
        sample = str(p.get('sample') or '')
        return JsonPost(
            # file_url: the post's page. The file itself comes from '/<id>?json' when the post is downloaded (_send_to_download):
            # the listing has no file url and none is built from the thumbnail (rule R6)
            id=post_id, file_url=f'{SITENAME}{post_id}', tags=list(p.get('tags') or []),
            sample_url=sample if sample.startswith('https://') else '', preview_url=str(p.get('thumbnail') or ''),
            width=_int(p.get('width')), height=_int(p.get('height')))

    def _listing_size(self, n: int, tries: int | None) -> int:
        """posts on listing page n (0 based); an empty RSS page (past the posts guests can see) counts 0"""
        page = self.fetch_html(self._form_page_num_address(n), tries=tries, do_cache=True)
        if page is None:
            thread_exit(f'ERROR: [{MODULE_ABBR.upper()}] unable to retreive listing page {n + 1:d}', code=-445)
        posts = self._posts(self._parse(page.raw))
        if posts is None:
            thread_exit(f'ERROR: [{MODULE_ABBR.upper()}] listing page {n + 1:d} is not an RSS answer', code=-447)
        return len(posts)

    def _get_items_query_size_or_html(self, url: str, tries: int | None = None) -> int:
        """The feed tells the site's count ('Zerochan has N ... images'), members only posts included: an upper bound for a
        guest. The exact count is found from the last page that holds posts (one request, or a binary search over the pages
        when the last pages are empty for a guest), never deeper than GUEST_MAX_PAGES"""
        page = self.fetch_html(self._form_page_num_address(0), tries=tries, do_cache=True)
        if page is None:
            thread_exit('ERROR: GetItemsQueSize: unable to retreive html', code=-444)
        data = self._parse(page.raw)
        if data is None:
            trace(f'Warning (W2): [{MODULE_ABBR.upper()}] no RSS listing for this search (unknown tag?)', True)
            return 0
        first = len(data['posts'])
        if first < PER_PAGE:
            return first
        total = data['total']
        last = min(GUEST_MAX_PAGES, (total + PER_PAGE - 1) // PER_PAGE) if total else GUEST_MAX_PAGES
        if last <= 1:
            return first
        n = self._listing_size(last - 1, tries)
        if n >= PER_PAGE:
            if not total or total > last * PER_PAGE:
                trace(f'Warning (W3): [{MODULE_ABBR.upper()}] Zerochan shows guests the first {GUEST_MAX_PAGES:d} pages of a search'
                      f' ({GUEST_MAX_PAGES * PER_PAGE:d} newest posts{f" of {total:d}" if total else ""}): older posts are not listed')
            return last * PER_PAGE
        if n > 0:
            return (last - 1) * PER_PAGE + n
        lo, hi = 0, last - 1  # page lo is full, page hi is empty
        while hi - lo > 1:
            mid = (lo + hi) // 2
            n = self._listing_size(mid, tries)
            if n >= PER_PAGE:
                lo = mid
            elif n > 0:
                return mid * PER_PAGE + n
            else:
                hi = mid
        return (lo + 1) * PER_PAGE

    # ----- download

    def _full_url(self, post_id: str) -> tuple[str, str]:
        """full size file url and md5 of a post from '/<id>?json' ('', '' when the API gives none: members only, deleted)"""
        page = self.fetch_html(f'{SITENAME}{post_id}?json')
        if page is None:
            return '', ''
        text = page.raw.decode('utf-8', errors='replace')
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if not isinstance(data, dict):  # stray characters around the object: read the field itself
            m = re_full.search(text)
            return (m.group(1), '') if m else ('', '')
        full = data.get('full')
        if str(data.get('id', '')) != post_id or not isinstance(full, str) or not full.startswith('https://'):
            return '', ''
        return full, str(data.get('hash') or '')

    def _send_to_download(self, raw: str, item_id: str, is_video: bool) -> None:
        post_id = self._extract_id(raw)
        address, md5 = '', ''
        if self.low_res:  # the 600 px sample of the listing: no extra request
            sample = self._extract_sample_url(raw)[0]
            address = sample if self._file_ext(sample) in KNOWN_EXTENSIONS_IMG else ''
        if not address:
            address, md5 = self._full_url(post_id)
        fmt = self._file_ext(address)
        if not address or fmt not in KNOWN_EXTENSIONS_IMG:
            trace(f'Warning (W2): {item_id}: the API gives no image file for this post (members only or deleted?), skipped', True)
            return
        info = self._item_info_for(item_id)
        info.ext = fmt
        if md5:
            info.md5 = md5
        subfolder = self._subfolder_for(MediaKind.IMAGE)
        hint_maxlen = FILE_NAME_FULL_MAX_LEN - (len(self.dest_base_s.as_posix()) + len(item_id) + 1 + len(fmt))
        self._download(address, item_id, self.dest_base_s / subfolder / f'{self._try_append_extra_info(item_id, hint_maxlen)}.{fmt}')

#
#
#########################################
