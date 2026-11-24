# coding=UTF-8
"""
Lynxx extension 'gelbooru' (gb) for https://gelbooru.com/: its own tag list update (manifest "tagUpdate": {"entry": "tags.py:read_tags"}).
Lynxx runs it only when the user asks (Tags A-Z, "Cập nhật"), never on a schedule; the extension ships the list it made
('gb_tags.json'). The core makes every request through job.get_text / job.get_json: its own site only, the pacing,
robots.txt, Retry-After, a request budget, no cookies.

Source: the DAPI tag index as json, most used first, with the user's own key:
index.php?page=dapi&s=tag&q=index&json=1&orderby=count&order=desc&limit=100&pid=N (N = page number), until a page whose last tag
has fewer than FLOOR posts, an empty or repeated page, or MAX_PAGES.
Recorded with the owner's key on 2026-11-21 (lynxx/tests/fixtures/gelbooru/):
- the xml answer has child elements, '<tags type="array" limit="100" offset="0" count="1487082"><tag><id>827834</id>
  <name>..</name><count>8</count><type>4</type><ambiguous>0</ambiguous></tag>..', not the attributes of rule34.xxx
  ('<tag id=".." name=".." .../>'): 1.0.1 read them, found no tag, and the core stopped with "tags.py:read_tags gave no tag";
- 'limit' is capped at 100 (limit=1000 answered 100 tags);
- '&json=1': {"@attributes": {"limit": 100, "offset": 0, "count": 1487082}, "tag": [{"id", "name", "count", "type",
  "ambiguous"}, ...]}, names html escaped ("jonathan_joestar&#039;s_pose"); pid is a page number (offset = pid * limit);
- without an order the pages are not stable (a tag came on page 0 and again on page 1); '&orderby=count&order=desc' sorts by
  post count and deep pages answer: rank 0 has 14,115,642 posts (a tag without a name, skipped), rank 100,000 94 posts,
  rank 300,000 15 posts.
FLOOR = max(job.min_posts, MIN_POSTS). MIN_POSTS = 20: about 250,000 tags in about 2,500 requests, inside the core's budget of
5,000 requests a list; the app sets no time limit on an update, and at the core's pace for extension sites (1 s at least) the
list takes about 45 minutes.
Types: 0 general, 1 artist, 3 copyright, 4 character, 5 metadata. 6 (deprecated) is left out: on 2026-11-21 82 of the 100 most
used rows were deprecated misspellings with millions of posts ('1firl' 8,540,770, '1gir', '1woman'), which would come before the
real tags in autocompletion (4 of 100 at rank 100,000, none at rank 300,000); they still count for the floor, as the site sorts them.
'ambiguous' is not a type: the tag keeps its own.
"""
#########################################
#
#

import html
import itertools
import json
from collections.abc import Iterator

__all__ = ('read_tags',)

PAGE_LIMIT = 100
'''tags a page: the site answers 100 at most, whatever 'limit' asks (2026-11-21)'''
MIN_POSTS = 20
'''about 250,000 tags, 2,500 pages (rank 100,000: 94 posts, rank 300,000: 15 posts on 2026-11-21)'''
MAX_PAGES = 10000
TYPES = {0: 'general', 1: 'artist', 3: 'copyright', 4: 'character', 5: 'metadata'}
DEPRECATED = 6
'''type of the site's deprecated tags: not written (module docstring)'''
KEY_NEEDED = 'the site needs your own API key (KEY,USER_ID)'
'''gelbooru.com answers the DAPI only with the user's key (account options): HTTP 401 without one'''


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _auth(api_key: str) -> str:
    """'KEY,USER_ID' of the user (manifest auth.apiKey), '' without one"""
    key, _, user_id = (api_key or '').partition(',')
    return f'&api_key={key.strip()}&user_id={user_id.strip()}' if key.strip() and user_id.strip() else ''


def _rows(data) -> list[dict] | None:
    """The tags of a json page ([] for an empty page), None when the answer is not a page of the tag index"""
    if isinstance(data, list):
        return [] if not data else None
    if not isinstance(data, dict) or not isinstance(data.get('@attributes'), dict):
        return None
    tags = data.get('tag', [])
    tags = [tags] if isinstance(tags, dict) else tags  # one tag: an object, not a list
    if not isinstance(tags, list) or not all(isinstance(t, dict) for t in tags):
        return None
    return tags


def _site_error(data) -> str:
    """The message of a json error answer ({"success": false, "message": ".."}), '' for any other answer"""
    if not isinstance(data, dict) or '@attributes' in data:
        return ''
    if str(data.get('success', '')).lower() in ('false', '0') or 'error' in data or 'message' in data:
        return str(data.get('message') or data.get('error') or data.get('reason') or 'error')[:200]
    return ''


def _page(job, pid: int, auth: str) -> tuple[list[dict] | None, str]:
    """(tags of page pid or None if the answer is not a tag page, the answer's text)"""
    url = f'{job.site}index.php?page=dapi&s=tag&q=index&json=1&orderby=count&order=desc&limit={PAGE_LIMIT:d}&pid={pid:d}{auth}'
    try:
        text = job.get_text(url, accept='application/json')
    except RuntimeError as err:
        if 'HTTP 401' in str(err) or 'HTTP 403' in str(err):
            raise RuntimeError(KEY_NEEDED) from err
        raise
    if 'Missing authentication' in text:
        raise RuntimeError(KEY_NEEDED)
    try:
        data = json.loads(text)
    except ValueError:
        return None, text
    if msg := _site_error(data):
        raise RuntimeError(f'{KEY_NEEDED}: the site answered {msg!r}')
    return _rows(data), text


def read_tags(job) -> Iterator[tuple[str, int, str]]:
    """lynxx.sdk.TagJob -> (name, posts, type) for each tag of the site with at least max(job.min_posts, MIN_POSTS) posts"""
    auth = _auth(job.api_key)
    if not auth:
        raise RuntimeError(KEY_NEEDED)
    floor = max(_int(getattr(job, 'min_posts', 1)), MIN_POSTS)
    job.log(f'tags with {floor:d} or more posts, {PAGE_LIMIT:d} a page sorted by post count')
    previous = None
    names = deprecated = 0
    lowest = -1
    for pid in range(MAX_PAGES):
        rows, text = _page(job, pid, auth)
        if rows is None:
            if pid == 0:
                raise RuntimeError(f'the tag index did not answer a json tag list: {text[:200]!r}')
            # the most used tags are read: keep them (the core still checks the list against the one in use)
            job.log(f'page {pid:d} is not a json tag list, stopping: {text[:200]!r}')
            break
        if not rows:
            break
        page = tuple((r.get('id'), r.get('name')) for r in rows)
        if page == previous:  # a site that ignores pid answers the same page forever
            job.log(f'page {pid:d} is the same as page {pid - 1:d}: the site ignores pid, stopping')
            break
        previous = page
        counts = [_int(r.get('count')) for r in rows]
        if any(a < b for a, b in itertools.pairwise(counts)):
            raise RuntimeError(f'page {pid:d} of the tag index is not sorted by post count (orderby=count ignored): the list would '
                               f'stop at the wrong tag')
        for r, count in zip(rows, counts, strict=True):
            name = '_'.join(html.unescape(str(r.get('name') or '')).lower().split())
            if name and count >= floor and _int(r.get('type')) == DEPRECATED:
                deprecated += 1
            elif name and count >= floor:
                names += 1
                lowest = count
                yield name, count, TYPES.get(_int(r.get('type')), 'general')
        if counts[-1] < floor:
            break
    job.log(f'{names:d} tags' + (f', down to {lowest:d} posts' if names else '') + (f' ({deprecated:d} deprecated left out)' if deprecated else ''))

#
#
#########################################
