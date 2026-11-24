# coding=UTF-8
"""
Lynxx extension 'zerochan' (zc) for https://www.zerochan.net/: its own tag list update (manifest "tagUpdate": {"entry": "tags.py:read_tags"}).
Lynxx runs it only when the user asks (Tags A-Z, "Cập nhật"), never on a schedule; the extension ships the list it made
('zc_tags.json'). The core makes every request through job.get_text / job.get_json: its own site only, the pacing,
robots.txt, Retry-After, a request budget, no cookies.
Zerochan's public tag directory '/tags?p=N' (HTML, the API has no tag listing): ~300 tags per page with their post counts, most
used first, the tag type as the class of each row. It is read until a page without a 'Next' link, a page whose last tag has fewer
than min_posts posts, or MAX_PAGES pages: the ~30,000 most used tags (100 requests; the tags of page 100 had ~86 posts on
2026-11-19). Rarer tags are learned from the posts the user views / downloads.
"""
#########################################
#
#

import html
import re

MAX_PAGES = 100
TYPES = {
    'mangaka': 'artist', 'studio': 'artist',
    'character': 'character', 'charactergroup': 'character',
    'series': 'copyright', 'game': 'copyright', 'movie': 'copyright', 'ova': 'copyright', 'artbook': 'copyright',
    'visualnovel': 'copyright', 'vn': 'copyright', 'vtuber': 'copyright',
    'source': 'metadata', 'meta': 'metadata', 'medium': 'metadata',
}
'''row class (Zerochan tag type) -> Lynxx tag type; theme, hair, eyes, outfit, group (solo, duo...), ecchi... are general'''
re_row = re.compile(r'<li class="([^"]*)"\s*>\s*<s class="[^"]*"></s>\s*<a href="[^"]*">([^<]*)</a>\s*([\d,]+)\s*</li>')
re_not_letters = re.compile(r'[^a-z]')


def _type(css_class: str) -> str:
    return TYPES.get(re_not_letters.sub('', css_class.lower()), 'general')


def read_tags(job):
    """Yields (name, posts, type) for each tag of the site's tag directory, most used first"""
    seen = set()
    for page in range(1, MAX_PAGES + 1):
        text = job.get_text(f'{job.site}tags?p={page:d}', accept='text/html')
        start = text.find('<ul id="tags-list"')
        listing = text[start:text.find('</ul>', start)] if start >= 0 else ''
        rows = [(cls, html.unescape(name).strip(), int(count.replace(',', ''))) for cls, name, count in re_row.findall(listing)]
        if not rows:
            return
        for cls, name, count in rows:
            if name and name.lower() not in seen:
                seen.add(name.lower())
                yield name.lower(), count, _type(cls)
        counts = [r[2] for r in rows]
        if counts == sorted(counts, reverse=True) and counts[-1] < job.min_posts:
            return  # most used first: every tag of the next pages has fewer posts than asked
        if f'href="?p={page + 1:d}"' not in text:
            return

#
#
#########################################
