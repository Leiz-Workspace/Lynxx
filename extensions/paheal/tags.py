# coding=UTF-8
"""
Lynxx extension 'paheal' (rp) for https://rule34.paheal.net/: its own tag list update, a merge of the most used tags
(manifest "tagUpdate": {"entry": "tags.py:read_tags", "merge": {"minPosts": 100}}). Lynxx runs it only when the user asks (Tags A-Z,
"Cập nhật"), never on a schedule, when the app opens or after an install; the extension ships its list ('rp_tags.json', from the
repository's 2tags/). The core makes every request through job.get_text: its own site only, the pacing, robots.txt, Retry-After, a
request budget, no cookies, a stop on bot challenges.

Source: the site's tag list by popularity, '/tags/popularity?mincount=N' (Shimmie's tag list; the site links it itself, 'Show All'
is mincount=1): every tag with at least N posts on one html page, most used first, each with its post count, grouped under log10
headings. The other listings do not fit: '/tags' ('/tags/map') and '/tags/alphabetic' show no counts; 'api/danbooru/find_tags'
answers the whole tag table at once, with no paging (over the core's 32 MB page limit, and heavy for the site);
'/api/internal/autocomplete' answers one prefix.
robots.txt (read 2026-11-20): 'User-agent: *', 'Crawl-delay: 3', 'Disallow:' /comment, /thumb/, /_thumbs/, /source_history,
/tag_history, /post/next, /post/prev. '/tags/popularity' is allowed; check any other path against these lines first.
Terms (https://rule34.paheal.net/wiki/Terms_of_use and /wiki/rules, read 2026-11-20): no clause on automated access, bots,
scraping, crawling or rate limits. The site's terms dialog is never accepted for the user (no cookie is sent).
Pacing: one request every 3 s (robots.txt Crawl-delay, as the downloader).
Cost: 2 requests (robots.txt and one page) of the core's 5,000; the core asks with min_posts >= 100 (merge.minPosts), the app with
1, so N = 100. Seen on 2026-11-20: 98 tags with 20,000+ posts (44 KB), 3,200 with 1,000+ (586 KB, about 175 bytes a tag), 26,343
with 100+ (4.54 MB, 2.75 s).
The site cannot answer a page big enough to replace the shipped list (162,526 tags with 5+ posts: the sanity check needs N <= about
18, and mincount=10 and 18 answered HTTP 500 with an empty body), so the manifest declares merge: Lynxx merges these tags into the
list in use (fresh counts, new tags added, every other tag kept; the tags under 100 posts get new ones only from the learning,
lynxx/tags_learned.py).
Names: the link text, html-unescaped once ('Five_Nights_at_Freddy&#039;s' -> "five_nights_at_freddy's"), lowercased, spaces as
'_'. The site keeps the case its uploaders typed ('Porkyman') but searches without case ('porkyman' finds the same 493,734 posts);
Lynxx's tag lookups, download index and lists are lowercase (the shipped rp_tags.json too), and a merge matches names exactly, so a
name in mixed case would be a second tag. Two names that differ only by case keep the first, most used one (none on 2026-11-20).
The site has no tag types (none on its post pages, tag lists, danbooru API or autocomplete: docs/dev_log.md 7bf): every tag read is
'unknown'. The shipped list has artist / character / copyright types inferred by name since 1.0.4 (tools/type_tag_list.py); a merge
keeps the type of the list it goes into, a new tag is 'unknown'.
"""
#########################################
#
#

import html
import re
from collections.abc import Iterator

__all__ = ('read_tags',)

MIN_POSTS = 100
'''equal to the manifest's tagUpdate.merge.minPosts: one page holds every tag down to it. mincount=10 and 18 answered HTTP 500 with
an empty body (2026-11-20); 100 answered 26,343 tags, 4.54 MB, in 2.75 s'''
TAG_TYPE = 'unknown'
'''the site has no tag types'''

LIST_START = 'Results grouped by log'
'''the heading of the list on the page (the tags follow it, grouped by the log10 of their count)'''
LIST_END = '</section>'
re_link = re.compile(r'<a[\s>]', re.IGNORECASE)
'''every link of the list, whatever its class: each one must be a tag with its count'''
re_row = re.compile(r'<a(?:\s[^>]*)?>([^<]*)</a>\s*\((\d{1,10})\)', re.IGNORECASE)


def _list_block(text: str) -> str:
    start = text.find(LIST_START)
    if start < 0:
        raise RuntimeError('not the tag list by popularity (tags/popularity): the site changed the page, or answered another one')
    end = text.find(LIST_END, start)
    if end < 0:
        raise RuntimeError('the tag list page is cut short: the list would be incomplete')
    return text[start:end]


def read_tags(job) -> Iterator[tuple[str, int, str]]:
    """lynxx.sdk.TagJob -> (name, posts, 'unknown') for each tag of the site with at least max(job.min_posts, MIN_POSTS) posts"""
    min_posts = max(int(job.min_posts or 0), MIN_POSTS)
    job.log(f'tags with {min_posts:d} or more posts, one page sorted by post count')
    block = _list_block(job.get_text(f'{job.site}tags/popularity?mincount={min_posts:d}', accept='text/html'))
    rows = re_row.findall(block)
    links = len(re_link.findall(block))
    if links != len(rows):
        raise RuntimeError(f'{links - len(rows):d} of {links:d} links of the tag list have no post count: the page changed')
    if not rows:
        raise RuntimeError(f'the tag list has no tag with {min_posts:d} or more posts: the site changed the page')
    seen: set[str] = set()
    last = -1
    same = 0
    for raw, count_text in rows:
        count = int(count_text)
        if 0 <= last < count:
            raise RuntimeError('the tag list is not sorted by post count: not the list by popularity')
        last = count
        name = '_'.join(html.unescape(raw).lower().split())
        if not name:
            continue
        if name in seen:
            same += 1  # the same name in another case: the first one has more posts
            continue
        if count >= min_posts:
            seen.add(name)
            yield name, count, TAG_TYPE
    job.log(f'{len(seen):d} tags, down to {last:d} posts' + (f' ({same:d} names that differ only by case kept once)' if same else ''))

#
#
#########################################
