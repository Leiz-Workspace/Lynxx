# coding=UTF-8
"""
Lynxx extension 'sakugabooru' (sk) for https://www.sakugabooru.com/: its own tag list update (manifest "tagUpdate": {"entry": "tags.py:read_tags"}).
Lynxx runs it only when the user asks (Tags A-Z, "Cập nhật"), never on a schedule; the extension ships the list it made
('sk_tags.json'). The core makes every request through job.get_text / job.get_json: its own site only, the pacing,
robots.txt, Retry-After, a request budget, no cookies.
Moebooru tag index: tag.json?limit=1000&page=N&order=name (pages from 1), until a page shorter than the limit.
"""
#########################################
#
#

PAGE_LIMIT = 1000
MAX_PAGES = 5000
TYPES = {0: 'general', 1: 'artist', 3: 'copyright', 4: 'general', 5: 'metadata'}
'''Sakugabooru's own types (its /tag page, 2026-11-21): General 0, Artist 1, Copyright 3, Terminology 4, Meta 5 ('presumed',
'remake'). Not yande.re's: there 4 is character and 5 'circle' (a group of artists)'''


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def read_tags(job):
    """Yields (name, posts, type) for each tag of the site"""
    seen = set()
    for page in range(1, MAX_PAGES + 1):
        tags = job.get_json(f'{job.site}tag.json?limit={PAGE_LIMIT}&page={page}&order=name')
        if not isinstance(tags, list) or not tags:
            return
        new = 0
        for t in tags:
            name = str(t.get('name') or '').strip() if isinstance(t, dict) else ''
            if not name or name in seen:
                continue
            seen.add(name)
            new += 1
            yield name, _int(t.get('count')), TYPES.get(_int(t.get('type')), 'general')
        if not new:
            raise RuntimeError(f'the site ignores the page parameter (page {page} repeats earlier tags): the list would be incomplete')
        if len(tags) < PAGE_LIMIT:
            return

#
#
#########################################
