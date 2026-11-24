# coding=UTF-8
"""
Lynxx extension 'tbib' (tb) for https://tbib.org/: its own tag list update (manifest "tagUpdate": {"entry": "tags.py:read_tags"}).
Lynxx runs it only when the user asks (Tags A-Z, "Cập nhật"), never on a schedule; the extension ships the list it made
('tb_tags.json'). The core makes every request through job.get_text / job.get_json: its own site only, the pacing,
robots.txt, Retry-After, a request budget, no cookies.
The site's html tag list sorted by post count, most used first: index.php?page=tags&s=list&sort=desc&order_by=index_count&pid=N
(N = offset, 50 tags per page), until a page below job.min_posts, an empty or repeated page, or MAX_PAGES.
Not the DAPI tag index: TBIB answers it 100 tags per page (limit=1000 is cut to 100) in no useful order, and its ~2.33 million
tags (most with 1 or 2 posts) would take over 23,000 requests, far past the core's request budget.
"""
#########################################
#
#

import html
import re

PER_PAGE = 50
MAX_PAGES = 1000  # the 50,000 most used tags (329 posts or more on 2026-11-19), 1,000 requests
TYPES = {'general': 'general', 'artist': 'artist', 'copyright': 'copyright', 'character': 'character', 'metadata': 'metadata'}
re_row = re.compile(r'<tr><td>\s*(\d+)\s*</td>\s*<td><span class="tag-type-([\w-]+)">\s*<a [^>]*>([^<]*)</a>')


def read_tags(job):
    """Yields (name, posts, type) for the most used tags of the site"""
    previous = None
    seen = set()
    for page in range(MAX_PAGES):
        text = job.get_text(f'{job.site}index.php?page=tags&s=list&sort=desc&order_by=index_count&pid={page * PER_PAGE:d}')
        rows = re_row.findall(text)
        if not rows:
            return
        names = tuple(name for _, _, name in rows)
        if names == previous:  # a site that ignores pid answers the same page forever
            return
        previous = names
        for posts, kind, name in rows:
            name = '_'.join(html.unescape(name).split())
            if name and name not in seen:
                seen.add(name)
                yield name, int(posts), TYPES.get(kind, 'general')
        if int(rows[-1][0]) < job.min_posts:  # sorted by count: the rest is rarer still
            return
        if page % 100 == 99:
            job.log(f'{page + 1:d} pages, {len(seen):d} tags (down to {int(rows[-1][0]):d} posts)')

#
#
#########################################
