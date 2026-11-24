# coding=UTF-8
"""
Lynxx: post identity from a file name, a path or a post / file link (module + post id, or md5), and search queries for them.
Used by the sidecar ('files.identify': search by file name) and by the library import of the download index
(lynxx/dlindex_tools.py). Names only: files are never opened here
"""
#########################################
#
#

from __future__ import annotations

import re
from urllib import parse as url_parse

from .module import ProcModule

__all__ = ('ID_LIST_CHUNK', 'ID_LIST_MODULES', 'build_file_queries', 'host_module', 'identify_file_name', 'site_hosts')

ID_LIST_MODULES = frozenset(('db',))
'''modules accepting lists in one metatag: 'id:1,2,3' / 'md5:a,b' (danbooru, checked 2026-11-17); others get an OR group'''
ID_LIST_CHUNK = 100
re_scheme = re.compile(r'^[a-z][a-z0-9+.-]*://', re.IGNORECASE)
re_md5_token = re.compile(r'(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])')
re_prefixed_id = re.compile(r'^([a-z0-9]{2,4})_(\d+)(?:[_.(\s-]|$)')
re_plain_id = re.compile(r'^(\d+)(?:[_.(\s-]|$)')
re_danbooru_download = re.compile(r'^__.+__[0-9a-f]{32}$')
POST_URL_PATTERNS = (
    re.compile(r'/posts?/(?:view/|show/)?(\d+)(?:[/?#]|$)'),  # danbooru, e621 /posts/N; shimmie /post/view/N
    re.compile(r'[?&]id=(\d+)(?:&|$)'),  # gelbooru 'page=post&s=view&id=N', rule34.us 'r=posts/view&id=N'
)


def site_hosts() -> dict[str, str]:
    """host -> module abbr of every known module (builtin, installed and catalog extensions)"""
    from .extensions import Extensions  # local import: the extensions loader imports downloaders
    hosts: dict[str, str] = {}
    for row in Extensions.list_all():
        site = str(row.get('site') or '')
        abbr = str(row.get('abbr') or '')
        host = (url_parse.urlparse(site).hostname or '').lower()
        if host and abbr:
            # full host and its base domain: file links come from cdn hosts (cdn.donmai.us, wimg.rule34.xxx)
            for h in (host, host.removeprefix('www.'), host.removeprefix('api.'), '.'.join(host.split('.')[-2:])):
                hosts.setdefault(h, abbr)
    return hosts


def host_module(host: str, hosts: dict[str, str]) -> str:
    host = host.lower()
    return hosts.get(host) or hosts.get('.'.join(host.split('.')[-2:]), '')


def identify_file_name(raw: str, module: str, hosts: dict[str, str]) -> dict:
    """One pasted / dropped item: a file name (Lynxx 'db_12345_score(3).jpg' or '12345.jpg', booru original '<md5>.jpg',
    danbooru download '__tags__<md5>.jpg'), a path, or a post / file link. Returns {name, module, kind: 'id'|'md5'|'', value, note}"""
    s = str(raw).strip().strip('"\'')
    item = {'name': s, 'module': module, 'kind': '', 'value': '', 'note': ''}
    known = ProcModule.PROC_MODULES_BY_NAME
    if re_scheme.match(s):
        u = url_parse.urlparse(s)
        if host_mod := host_module(u.hostname or '', hosts):
            item['module'] = host_mod
        last = u.path.rsplit('/', 1)[-1].lower()
        if m := re_md5_token.search(last):  # file links: cdn.donmai.us/original/0a/1b/<md5>.jpg
            item.update(kind='md5', value=m.group(1))
            return item
        target = f'{u.path}?{u.query}'
        for rx in POST_URL_PATTERNS:
            if m := rx.search(target):
                item.update(kind='id', value=m.group(1))
                if not host_mod:
                    item['note'] = 'unknown site, current module assumed'
                return item
        item['note'] = 'no post id or md5 in the link'
        return item
    name = s.replace('\\', '/').rsplit('/', 1)[-1]
    stem = (name.rsplit('.', 1)[0] if '.' in name.lstrip('.') else name).lower()
    if '!info_' in stem:
        item['note'] = 'info list file, not a post'
    elif (m := re_prefixed_id.match(stem)) and m.group(1) in known:
        item.update(module=m.group(1), kind='id', value=str(int(m.group(2))))
    elif m := re_plain_id.match(stem):
        item.update(kind='id', value=str(int(m.group(1))))
    elif m := re_md5_token.search(stem):
        item.update(kind='md5', value=m.group(1))
        if re_danbooru_download.match(stem) and 'db' in known:
            item['module'] = 'db'
    else:
        item['note'] = 'not recognized'
    return item


def build_file_queries(items: list[dict]) -> list[dict]:
    """Recognized items -> one query per module: id / md5 lists (danbooru) or an OR group of metatags (one search per item,
    at most task.MAX_SPLIT_TASKS). Queries are ordered by number of items"""
    from .task import MAX_SPLIT_TASKS
    by_module: dict[str, dict[str, list[str]]] = {}
    for it in items:
        if it['kind']:
            vals = by_module.setdefault(it['module'], {'id': [], 'md5': []})[it['kind']]
            if it['value'] not in vals:
                vals.append(it['value'])
    queries = []
    for module, v in by_module.items():
        terms: list[str] = []
        if module in ID_LIST_MODULES:
            for kind in ('id', 'md5'):
                for i in range(0, len(v[kind]), ID_LIST_CHUNK):
                    terms.append(f'{kind}:{",".join(v[kind][i:i + ID_LIST_CHUNK])}')
        else:
            terms = [f'id:{x}' for x in v['id']] + [f'md5:{x}' for x in v['md5']]
        total = len(v['id']) + len(v['md5'])
        dropped = 0
        if len(terms) > MAX_SPLIT_TASKS:
            dropped = len(terms) - MAX_SPLIT_TASKS if module not in ID_LIST_MODULES else 0
            terms = terms[:MAX_SPLIT_TASKS]
        tags = terms[0] if len(terms) == 1 else f'({"~".join(terms)})'
        queries.append({'module': module, 'tags': tags, 'count': total - dropped, 'dropped': dropped})
    queries.sort(key=lambda q: -q['count'])
    return queries

#
#
#########################################
