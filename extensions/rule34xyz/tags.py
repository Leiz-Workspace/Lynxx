# coding=UTF-8
"""
Lynxx extension 'rule34xyz' (rz) for https://rule34.xyz/: its own tag list update (manifest "tagUpdate": {"entry": "tags.py:read_tags"}).
Lynxx runs it only when the user asks (Tags A-Z, "Cập nhật"), never on a schedule; the extension ships the list it made
('rz_tags.json'). The core makes every request through job.get_text / job.get_json: its own site only, the pacing,
robots.txt, Retry-After, a request budget, no cookies.
The site has no GET tag index: its tag search reads the text from a POST body (GET api/v2/tag/search ignores searchText and
answers the 20 most popular tags) and the tag sitemap is a gzip file of names only. What GET gives, with post counts and types,
is the site's Trends rankings: api/v2/tag-statistics/top-n (100 tags per tag type, period and metric), periods from
api/v2/tag-statistics/bounds. Their union is the site's popular tags, not every tag (cost and coverage: specs.md).
"""
#########################################
#
#

import re

TYPES = {1: 'general', 2: 'copyright', 4: 'character', 8: 'artist', 16: 'metadata', 32: 'metadata'}
RANKED_TYPES = (1, 8, 4, 2)
'''tag types the rankings know (16 / 32 answer nothing)'''
METRICS = (0, 1)
'''likes, views'''
YEAR, MONTH, WEEK = 4, 3, 2
PERIODS = ((YEAR, 30), (MONTH, 72), (WEEK, 8))
'''period kinds, most tags per request first, and how many periods of each (newest first) are read'''
RANK_LIMIT = 100
'''tags per ranking (the site answers 100 at most)'''
re_date = re.compile(r'^(\d{4})-(\d{2})-(\d{2})')


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _ymd(text) -> tuple[int, int, int] | None:
    m = re_date.match(str(text or ''))
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def _days(y: int, m: int, d: int) -> int:
    """days since 1970-01-01 (proleptic Gregorian calendar)"""
    y -= 1 if m <= 2 else 0
    era = y // 400
    yoe = y - era * 400
    doy = (153 * (m + (-3 if m > 2 else 9)) + 2) // 5 + d - 1
    return era * 146097 + yoe * 365 + yoe // 4 - yoe // 100 + doy - 719468


def _civil(z: int) -> tuple[int, int, int]:
    z += 719468
    era = z // 146097
    doe = z - era * 146097
    yoe = (doe - doe // 1460 + doe // 36524 - doe // 146096) // 365
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    m = mp + (3 if mp < 10 else -9)
    return yoe + era * 400 + (1 if m <= 2 else 0), m, doy - (153 * mp + 2) // 5 + 1


def _period_starts(kind: int, lo: tuple[int, int, int], hi: tuple[int, int, int], limit: int) -> list[str]:
    """period start dates from hi back to lo, newest first"""
    out: list[str] = []
    y, m, d = hi
    while len(out) < limit and (y, m, d) >= lo:
        out.append(f'{y:04d}-{m:02d}-{d:02d}')
        if kind == YEAR:
            y -= 1
        elif kind == MONTH:
            y, m = (y - 1, 12) if m == 1 else (y, m - 1)
        else:
            y, m, d = _civil(_days(y, m, d) - 7)
    return out


def _tag(t) -> tuple[str, int, str] | None:
    if not isinstance(t, dict):
        return None
    name = '_'.join(str(t.get('value') or '').split())
    return (name, _int(t.get('count')), TYPES.get(_int(t.get('type')), 'general')) if name else None


def read_tags(job):
    """Yields (name, posts, type) for each tag of the site's rankings (each tag once)"""
    api = f'{job.site}api/v2/'
    min_posts = max(1, _int(getattr(job, 'min_posts', 1)))
    seen: set[str] = set()

    def fresh(rows):
        for t in rows if isinstance(rows, list) else ():
            if (tag := _tag(t)) and tag[0] not in seen:
                seen.add(tag[0])
                if tag[1] >= min_posts:
                    yield tag

    yield from fresh(job.get_json(f'{api}tag/search'))  # the 20 most popular tags
    for kind, max_periods in PERIODS:
        for metric in METRICS:
            bounds = job.get_json(f'{api}tag-statistics/bounds?periodType={kind:d}&source={metric:d}')
            lo, hi = (_ymd(bounds.get('minDate')), _ymd(bounds.get('maxDate'))) if isinstance(bounds, dict) else (None, None)
            if not lo or not hi:
                continue
            for start in _period_starts(kind, lo, hi, max_periods):
                for tag_type in RANKED_TYPES:
                    data = job.get_json(f'{api}tag-statistics/top-n?periodType={kind:d}&source={metric:d}&periodStart={start}'
                                        f'&tagType={tag_type:d}&limit={RANK_LIMIT:d}')
                    items = data.get('items') if isinstance(data, dict) else None
                    yield from fresh([i.get('tag') for i in items if isinstance(i, dict)] if isinstance(items, list) else [])

#
#
#########################################
