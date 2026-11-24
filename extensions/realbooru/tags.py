# coding=UTF-8
"""
Lynxx extension 'realbooru' (rb) for https://realbooru.com/: its own tag list update (manifest "tagUpdate": {"entry": "tags.py:read_tags"}).
Lynxx runs it only when the user asks (Tags A-Z, "Cập nhật"), never on a schedule; the extension ships the list it made
('rb_tags.json'). The core makes every request through job.get_text / job.get_json: its own site only, the pacing,
robots.txt, Retry-After, a request budget, no cookies.
Gelbooru 0.2. The DAPI tag index (index.php?page=dapi&s=tag&q=index) is tried first; on 2026-11-19 it answers
'API offline ... shut off indefinitely', so the HTML tag list is read: index.php?page=tags&s=list&sort=asc&order_by=tag&pid=N,
50 tags per page, pid is an offset. Sorted by name (unique), so offset paging stays stable (the default order, 'updated',
moves tags while they are read; 'index_count' has ties). About 60,200 tags: ~1,205 pages, 20-25 minutes at 1 request/s.
Tag type 'model' (the person shown) is written as 'character'.
"""
#########################################
#
#

import html
import re

DAPI_LIMIT = 1000
HTML_PER_PAGE = 50
MAX_PAGES = 5000
TYPES = {'general': 'general', 'artist': 'artist', 'copyright': 'copyright', 'character': 'character', 'metadata': 'metadata',
         'model': 'character'}
DAPI_TYPES = {0: 'general', 1: 'artist', 3: 'copyright', 4: 'character', 5: 'metadata', 6: 'general'}
re_xml_tag = re.compile(r'<tag\s([^>]*?)/?>')
re_xml_attr = re.compile(r'(\w+)="([^"]*)"')
re_row = re.compile(r'<tr><td>(\d+)</td><td><span class="tag-type-([a-z_]+)"><a href="[^"]*">([^<]*)</a></span></td>')
re_next = re.compile(r'<a href="[^"]*pid=\d+[^"]*" alt="next">')
re_bad_name = re.compile(r'[\s\x00-\x1f\x7f]')


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _name(raw: str) -> str:
    """the name as the site shows it; '' for names with spaces / control characters (broken entries of the site's table)"""
    name = html.unescape(raw)
    return '' if not name or re_bad_name.search(name) else name


def _read_dapi(job, first: str):
    previous = None
    text = first
    for pid in range(MAX_PAGES):
        if pid:
            text = job.get_text(f'{job.site}index.php?page=dapi&s=tag&q=index&limit={DAPI_LIMIT}&pid={pid}', accept='application/xml')
        rows = [dict(re_xml_attr.findall(m.group(1))) for m in re_xml_tag.finditer(text)]
        rows = [r for r in rows if r.get('id') or r.get('name')]
        if not rows:
            return
        page = tuple((r.get('id'), r.get('name')) for r in rows)
        if page == previous:  # a site that ignores pid answers the same page forever
            return
        previous = page
        for r in rows:
            if name := _name(r.get('name', '')):
                yield name, _int(r.get('count')), DAPI_TYPES.get(_int(r.get('type')), 'general')


def _read_html(job):
    previous = None
    seen = set()
    for page_num in range(MAX_PAGES):
        text = job.get_text(f'{job.site}index.php?page=tags&s=list&sort=asc&order_by=tag&pid={page_num * HTML_PER_PAGE}')
        rows = re_row.findall(text)
        if not rows:
            return
        if rows == previous:  # a site that ignores pid answers the same page forever
            return
        previous = rows
        for count, kind, raw_name in rows:
            name = _name(raw_name)
            if name and name not in seen:
                seen.add(name)
                yield name, _int(count), TYPES.get(kind, 'general')
        if not re_next.search(text):  # the last page has no 'next' link
            return
        if page_num and page_num % 100 == 0:
            job.log(f'{page_num:d} pages, {len(seen):d} tags')


def read_tags(job):
    """Yields (name, posts, type) for each tag of the site"""
    text = job.get_text(f'{job.site}index.php?page=dapi&s=tag&q=index&limit={DAPI_LIMIT}&pid=0', accept='application/xml')
    if re_xml_tag.search(text):
        yield from _read_dapi(job, text)
        return
    job.log('the DAPI tag index is off: reading the HTML tag list (50 tags per page)')
    yield from _read_html(job)

#
#
#########################################
