# coding=UTF-8
"""
Lynxx extension 'hypnohub' (hh) for https://hypnohub.net/: its own tag list update (manifest "tagUpdate": {"entry": "tags.py:read_tags"}).
Lynxx runs it only when the user asks (Tags A-Z, "Cập nhật"), never on a schedule; the extension ships the list it made
('hh_tags.json'). The core makes every request through job.get_text / job.get_json: its own site only, the pacing,
robots.txt, Retry-After, a request budget, no cookies.
Gelbooru 0.2 DAPI tag index: index.php?page=dapi&s=tag&q=index&limit=1000&pid=N. Page number paging (the index is not in id order
and ignores order / orderby), until an empty page: a short page is not the end.
"""
#########################################
#
#

import html
import re

PAGE_LIMIT = 1000
MAX_PAGES = 10000
TYPES = {0: 'general', 1: 'artist', 3: 'copyright', 4: 'character', 5: 'metadata', 6: 'general'}
re_tag = re.compile(r'<tag\s([^>]*?)/?>')
re_attr = re.compile(r'(\w+)="([^"]*)"')


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _auth(api_key: str) -> str:
    """'KEY,USER_ID' of the user (manifest auth.apiKey), '' without one"""
    key, _, user_id = (api_key or '').partition(',')
    return f'&api_key={key.strip()}&user_id={user_id.strip()}' if key.strip() and user_id.strip() else ''


def read_tags(job):
    """Yields (name, posts, type) for each tag of the site"""
    auth = _auth(job.api_key)
    previous = None
    for pid in range(MAX_PAGES):
        text = job.get_text(f'{job.site}index.php?page=dapi&s=tag&q=index&limit={PAGE_LIMIT}&pid={pid}{auth}', accept='application/xml')
        if 'Missing authentication' in text:
            raise RuntimeError('the site needs your own API key (KEY,USER_ID)')
        rows = [dict(re_attr.findall(m.group(1))) for m in re_tag.finditer(text)]
        rows = [r for r in rows if r.get('id') or r.get('name')]  # an empty page: '<tags type="array"/>'
        if not rows:
            return
        page = tuple((r.get('id'), r.get('name')) for r in rows)
        if page == previous:  # a site that ignores pid answers the same page forever
            return
        previous = page
        for r in rows:
            if name := html.unescape(r.get('name', '')).strip():
                yield name, _int(r.get('count')), TYPES.get(_int(r.get('type')), 'general')

#
#
#########################################
