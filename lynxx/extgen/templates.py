# coding=UTF-8
"""
Lynxx extension generator: manifest.json and downloader.py for engines with a known API (plan B.4, QĐ2).
Rendering is plain, controlled string substitution of validated values (no template engine executing code).
Other engines get a downloader.py written by the AI (or a developer) and checked by policy + sandbox tests.
"""
#########################################
#
#

from __future__ import annotations

import datetime
import json
import re

from lynxx.module import ProcModule
from lynxx.sdk import SDK_VERSION
from lynxx.tags_updater import ENGINE_TAG_SOURCES
from lynxx.vcs.version import APP_VERSION

from .fetch import host_of

__all__ = ('TEMPLATE_ENGINES', 'make_manifest', 'render_downloader', 'suggest_names')

re_id = re.compile(r'^[a-z][a-z\d_-]{1,31}$')
re_abbr = re.compile(r'^[a-z][a-z\d]{1,3}$')
re_safe_name = re.compile(r'^[\w .,()\'&+-]{1,64}$')

GELBOORU_SYNTAX = {
    'concat': '+', 'idSep': ':', 'maxTagsStringLen': 3300,
    'metaCount': ':(?:(?:[<>]=?|=)?[a-z\\d\\-_]+?|[a-z\\d_]+:[a-z\\d_]+?)',
    'sort': 'sort(?::[^:]+?){1,2}', 'fav': 'favorited_by:\\d+?', 'pool': 'pool:\\d+?',
}
DANBOORU_SYNTAX = {
    'concat': '+', 'idSep': ':', 'maxTagsStringLen': 3300, 'maxTags': 2, 'maxTagsSeparate': False, 'maxWildcards': 0,
    'freeMetaPrefixes': ['rating:', 'id:', 'date:', 'score:', 'status:', 'parent:'], 'localPositiveOverflow': True,
    'metaCount': ':(?:[<>]=?|!)?[a-z\\d\\-_.,]+?', 'sort': 'order:[a-z_]+', 'fav': 'favorited_by:[^:]+?', 'pool': 'pool:[^:]+?',
}
E621_SYNTAX = {
    'concat': '+', 'idSep': ':', 'maxTagsStringLen': 3300, 'maxTags': 40, 'maxWildcards': 1,
    'metaCount': ':(?:[<>]=?|!)?[a-z\\d\\-_.]+?', 'sort': 'order:[^:]+(?:_(?:a|de)sc)?', 'fav': 'favorited_by:[^:]+?',
    'pool': '(?:pool|set):[^:]+?',
}
MOEBOORU_SYNTAX = {  # favorites: 'favorited_by:<name>', the site's 'vote:3:<name>' (_query_tags)
    'concat': '+', 'idSep': ':', 'maxTagsStringLen': 3300, 'maxTags': 6, 'localPositiveOverflow': True,
    'metaCount': ':(?:[<>]=?|!)?[a-z\\d\\-_.,]+?', 'sort': 'order:[a-z_]+', 'fav': 'favorited_by:[^:]+?', 'pool': 'pool:[^:]+?',
}
PHILOMENA_SYNTAX = {  # tags joined with ',' and '_' sent as ' ' by the downloader (_query_tags); favorites: the site's 'faved_by:'
    'concat': '+', 'idSep': ':', 'maxTagsStringLen': 3300, 'metaCount': '(?:\\.(?:gt|gte|lt|lte))?:[a-z\\d\\-_.]+?',
    'sort': '', 'fav': 'favorited_by:[^:]+?', 'pool': '',
}
SZURUBOORU_SYNTAX = {  # favorites: the site's 'fav:<name>' (_query_tags)
    'concat': '+', 'idSep': ':', 'maxTagsStringLen': 3300, 'metaCount': ':[a-z\\d\\-_.,]+?', 'sort': 'sort:[a-z\\-_]+',
    'fav': 'favorited_by:[^:]+?', 'pool': 'pool:\\d+?',
}
SHIMMIE_SYNTAX = {
    'concat': ' ', 'idSep': '=', 'maxTagsStringLen': 1000, 'metaCount': '(?:[<>]=?|=)[a-z\\d\\-_]+?',
    'sort': 'order=(?:id|score)_(?:a|de)sc', 'fav': 'favorited_by=[^:=]+?', 'pool': '',
}
CUSTOM_SYNTAX = {
    'concat': '+', 'idSep': ':', 'maxTagsStringLen': 3300, 'metaCount': ':(?:[<>]=?|!)?[a-z\\d\\-_.,]+?',
    'sort': '', 'fav': '', 'pool': '',
}
'''an engine without a template ('custom'): no favorites, pool or sort syntax until the author sets the site's own (and its
_query_tags) from what the site really answers. A fav / pool / sort regex the downloader does not handle is worse than none:
the query is accepted, then sent in a form the site does not know'''
SYNTAX_BY_ENGINE = {'gelbooru_dapi': GELBOORU_SYNTAX, 'danbooru_json': DANBOORU_SYNTAX, 'e621_json': E621_SYNTAX,
                    'moebooru_json': MOEBOORU_SYNTAX, 'philomena_json': PHILOMENA_SYNTAX, 'szurubooru_json': SZURUBOORU_SYNTAX,
                    'shimmie_api': SHIMMIE_SYNTAX, 'shimmie': SHIMMIE_SYNTAX}
ENGINE_NOTES = {
    'moebooru_json': ('Tag type 5 is not the same on every Moebooru site: an artist group ("circle") on some, "meta" on others. '
                      'The engine\'s tag list (lynxx/tags_updater.py MOEBOORU_TYPES) reads 5 as artist: check the legend of the '
                      'site\'s tag page; if 5 is not an artist group there, give the extension its own tags.py (manifest tagUpdate, '
                      'SDK 1.1) with the site\'s map.'),
}
'''notes written into specs.md for drafts of an engine (scaffold.create_draft)'''
TEMPLATE_ENGINES = ('gelbooru_dapi', 'danbooru_json', 'e621_json', 'moebooru_json', 'philomena_json', 'szurubooru_json', 'shimmie_api')


def _taken_abbrs() -> set[str]:
    """abbreviations of builtin, installed and catalog modules"""
    from lynxx.extensions import Extensions
    Extensions.ensure_loaded(include_catalog=True)
    return set(ProcModule.PROC_MODULES_BY_NAME)


def known_extensions() -> list[dict]:
    """builtin, installed and catalog extensions: id, name, abbr, site, installed"""
    from lynxx.extensions import Extensions
    return [{'id': r['id'], 'name': r.get('name', ''), 'abbr': r.get('abbr', ''), 'site': r.get('site', ''), 'installed': bool(r.get('installed'))}
            for r in Extensions.list_all()]


def existing_extensions(site: str) -> list[dict]:
    """known extensions for the same site (same host or parent domain): a new draft would duplicate them"""
    host = host_of(site)
    base = '.'.join(host.split('.')[-2:])
    out = []
    for e in known_extensions():
        h = host_of(e['site']) if e['site'] else ''
        if h and (h == host or '.'.join(h.split('.')[-2:]) == base):
            out.append(e)
    return out


def suggest_names(site: str) -> dict:
    """id / abbr / name proposals for a site, id and abbr not used by any known extension / module"""
    host = host_of(site)
    labels = [x for x in host.split('.') if x not in ('www', 'api')]
    core = labels[0] if len(labels) > 1 else (labels[0] if labels else 'site')
    ext_id = re.sub(r'[^a-z\d_-]', '', core.lower()) or 'site'
    if not ext_id[0].isalpha():
        ext_id = f's{ext_id}'
    ext_id = ext_id[:32]
    taken_ids = {e['id'] for e in known_extensions()}
    if ext_id in taken_ids:
        ext_id = next((f'{ext_id[:30]}{n}' for n in range(2, 100) if f'{ext_id[:30]}{n}' not in taken_ids), ext_id)
    taken = _taken_abbrs()
    letters = re.sub(r'[^a-z\d]', '', ext_id) or 'site'
    candidates = [letters[:2], letters[0] + letters[-1], letters[:3], letters[0] + letters[2:4]]
    candidates += [f'{letters[0]}{d}' for d in range(10)] + [f'{letters[:2]}{d}' for d in range(10)]
    abbr = next((c for c in candidates if re_abbr.fullmatch(c) and c not in taken), '')
    name = core.replace('-', ' ').replace('_', ' ').title()[:64] or 'Site'
    return {'id': ext_id if re_id.fullmatch(ext_id) else 'site', 'abbr': abbr, 'name': name}


def validate_names(ext_id: str, abbr: str, name: str) -> None:
    if not re_id.fullmatch(ext_id):
        raise ValueError(f'invalid id \'{ext_id}\' (lowercase letters, digits, _ -, 2-32 chars, starting with a letter)')
    if not re_abbr.fullmatch(abbr):
        raise ValueError(f'invalid abbr \'{abbr}\' (2-4 lowercase letters / digits, starting with a letter)')
    if ext_id in {e['id'] for e in known_extensions()}:
        raise ValueError(f'id \'{ext_id}\' is already used by another extension (builtin, installed or catalog)')
    if abbr in _taken_abbrs():
        raise ValueError(f'abbr \'{abbr}\' is already used by another module')
    if not re_safe_name.fullmatch(name):
        raise ValueError(f'invalid name \'{name}\'')


def class_name(abbr: str) -> str:
    return f'Downloader{abbr.capitalize()}'


def make_manifest(*, ext_id: str, abbr: str, name: str, site: str, engine: str, media: list[str], description: str = '',
                  syntax: dict | None = None, api_key: bool = False, generator: dict | None = None) -> dict:
    """manifest.json content. '$generator' records the origin (modspec ignores '$' keys). An engine with a tag listing gets
    'tagList': lynxx.tags_updater reads that site's tags into '<abbr>_tags.json' (autocompletion, Tags A-Z)"""
    m = {
        'id': ext_id, 'name': name, 'abbr': abbr, 'version': '1.0.0', 'apiVersion': 1, 'kind': 'downloader', 'default': False,
        'site': site, 'media': [x for x in media if x in ('image', 'video', 'audio')] or ['image', 'video'],
        **({'tagList': f'{abbr}_tags.json'} if engine in ENGINE_TAG_SOURCES else {}),
        'entry': f'downloader.py:{class_name(abbr)}', 'minSdkVersion': SDK_VERSION, 'description': description[:300],
        'syntax': syntax if syntax is not None else SYNTAX_BY_ENGINE.get(engine, CUSTOM_SYNTAX),
        '$generator': {'tool': 'lynxx.extgen', 'toolVersion': APP_VERSION, 'engine': engine, 'sourceUrl': site,
                       'createdAt': datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%dT%H:%M:%SZ'), **(generator or {})},
    }
    if api_key:
        m['auth'] = {'apiKey': True, 'apiKeyFormat': 'API_KEY,USER_ID' if engine == 'gelbooru_dapi' else 'API_KEY,LOGIN', 'cookies': []}
    return m


_HEADER = '''# coding=UTF-8
"""
Lynxx extension '{ext_id}' ({abbr}) for {site}
Generated by lynxx.extgen from the '{engine}' template on {date}. Review before installing.
"""
#########################################
#
#
'''

_GELBOORU = _HEADER + '''
import re
from typing import Never, final

from lynxx.sdk import DownloaderGelbooru, ModuleCaps, re_tags_exclude_rx, re_tags_to_process_rx

__all__ = ('{cls}',)

SITENAME = {site_lit}
MODULE_ABBR = {abbr_lit}
FILE_NAME_PREFIX = f'{{MODULE_ABBR}}_'
ITEMS_PER_PAGE = {per_page}  # dapi 'limit' (gelbooru 0.2 sites accept 100 at least)
ITEMS_PER_PAGE_F = 50  # favorites html page
ITEMS_PER_PAGE_P = 20000  # pool html page: all posts at once
MAX_SEARCH_DEPTH = 200000 + ITEMS_PER_PAGE - 1
re_post_page = re.compile(r'^(?:\\?page=post&(?:amp;)?s=list|document\\.location=\\'\\?page=favorites|\\?page=pool&(?:amp;)?s=show)&.+?$')


@final
class {cls}(DownloaderGelbooru):
    CAPABILITIES = frozenset((ModuleCaps.PARENT_CHILD, ModuleCaps.SOURCES, ModuleCaps.DATE_FILTER, ModuleCaps.PREVIEW{api_caps}))

    def __init__(self) -> None:
        super().__init__()

    def _get_min_request_interval(self) -> float:
        return {interval}  # polite floor for a site without a published rate limit

    def _get_id_bounds(self) -> Never:
        raise NotImplementedError

    def _form_item_string_manually(self, *ignored) -> Never:
        raise NotImplementedError

    def _get_re_post_page(self) -> re.Pattern:
        return re_post_page

    def _get_sitename(self) -> str:
        return SITENAME

    def _get_module_abbr(self) -> str:
        return MODULE_ABBR

    def _get_module_abbr_p(self) -> str:
        return FILE_NAME_PREFIX

    def _get_items_per_page(self) -> int:
        return ITEMS_PER_PAGE_F if self.favorites_search_user else ITEMS_PER_PAGE_P if self.pool_search_str else ITEMS_PER_PAGE

    def _get_max_search_depth(self) -> int:
        return MAX_SEARCH_DEPTH

    def get_re_tags_to_process(self) -> re.Pattern:
        return re_tags_to_process_rx

    def get_re_tags_to_exclude(self) -> re.Pattern:
        return re_tags_exclude_rx

    def _get_tags_concat_char(self) -> str:
        return '+'

    def _get_idval_equal_seaparator(self) -> str:
        return ':'

    def _extract_comments(self, item_id: str) -> None:
        return  # comments are not collected (capability not declared)
{api_key_method}
#
#
#########################################
'''

_GELBOORU_API_KEY = '''
    def _get_api_key(self) -> str:
        return f'&api_key={self.api_key.key}&user_id={self.api_key.user_id}' if self.api_key else ''
'''

_DANBOORU = _HEADER + '''
from typing import final

from lynxx.sdk import DownloaderDb

__all__ = ('{cls}',)

SITENAME = {site_lit}
MODULE_ABBR = {abbr_lit}
FILE_NAME_PREFIX = f'{{MODULE_ABBR}}_'


@final
class {cls}(DownloaderDb):
    """Danbooru compatible API: everything but the address and the module name is the builtin danbooru downloader"""

    def _get_min_request_interval(self) -> float:
        return {interval}  # polite floor for a site without a published rate limit

    def _get_sitename(self) -> str:
        return SITENAME

    def _get_module_abbr(self) -> str:
        return MODULE_ABBR

    def _get_module_abbr_p(self) -> str:
        return FILE_NAME_PREFIX

#
#
#########################################
'''


_JSON = _HEADER + '''
{imports}from typing import final

from lynxx.sdk import DownloaderJsonApi, JsonPost, ModuleCaps{sdk_names}

__all__ = ('{cls}',)

SITENAME = {site_lit}
MODULE_ABBR = {abbr_lit}
{helpers}

@final
class {cls}(DownloaderJsonApi):
    \"\"\"{doc}: the shared JSON API downloader (lynxx.sdk.DownloaderJsonApi) with the endpoints and fields of this engine\"\"\"
    CAPABILITIES = frozenset((ModuleCaps.SOURCES, ModuleCaps.DATE_FILTER, ModuleCaps.PREVIEW))
    SITE = SITENAME
    ABBR = MODULE_ABBR
    ITEMS_PER_PAGE = {per_page}
    MAX_PAGES = {max_pages}

    def _get_min_request_interval(self) -> float:
        return {interval}  # polite floor for a site without a published rate limit
{body}
#
#
#########################################
'''

_FAV_HELPER = '''

def _site_fav(tag: str, token: str, space: str = '_') -> str:
    """'favorited_by:<name>' (the core's favorites syntax, manifest syntax.fav; a leading '-' kept) as the site's '<token><name>',
    '' for any other tag"""
    neg = '-' if tag.startswith('-') else ''
    m = re_favorited_by_tag.fullmatch(tag[len(neg):])
    return f'{neg}{token}{m.group(1).replace("_", space)}' if m else ''
'''
'''module level helper of the json templates whose site names favorites its own way (inserted as is, not formatted)'''
_FAV = {'sdk_names': ', re_favorited_by_tag', 'helpers': _FAV_HELPER}

_JSON_ENGINES = {
    'e621_json': {
        'doc': 'e621 / e926 compatible API (posts.json -> {{"posts": [...]}})', 'per_page': 320, 'max_pages': 750, 'interval': 1.0,
        'imports': '', **_FAV, 'body': '''
    def _query_tags(self, tags: str) -> str:
        # favorites: the core's 'favorited_by:<name>' (manifest syntax.fav) is the site's 'fav:<name>'
        return self.TAGS_CONCAT.join(_site_fav(t, 'fav:') or t for t in tags.split(self.TAGS_CONCAT))

    def _search_url(self, tags: str, page: int, limit: int) -> str:
        return f'{{SITENAME}}posts.json?tags={{tags}}&limit={{limit}}&page={{page + 1}}'

    def _cursor_url(self, tags: str, cursor: int | None, ascending: bool, limit: int) -> str:
        extra = ('+order:id' if ascending else '') + ('' if cursor is None else f'+id:{{">" if ascending else "<"}}{{cursor}}')
        return self._search_url(f'{{tags}}{{extra}}', 0, limit)

    def _posts(self, data: object) -> list[dict] | None:
        posts = data.get('posts') if isinstance(data, dict) else None
        return posts if isinstance(posts, list) else None

    def _post(self, p: dict) -> JsonPost | None:
        file = p.get('file') or {{}}
        groups = p.get('tags') or {{}}
        rel = p.get('relationships') or {{}}
        score = p.get('score')
        return JsonPost(
            id=str(p.get('id', '')),
            file_url=file.get('url') or '',  # null: hidden by the site for this user, never rebuilt (R6)
            tags=[t for g in groups.values() if isinstance(g, list) for t in g],
            created_at=p.get('created_at') or '', sample_url=(p.get('sample') or {{}}).get('url') or '',
            preview_url=(p.get('preview') or {{}}).get('url') or '', md5=file.get('md5') or '',
            width=file.get('width') or 0, height=file.get('height') or 0,
            score=score.get('total', 0) if isinstance(score, dict) else score or 0, rating=p.get('rating') or '',
            source=' '.join(p.get('sources') or []), parent_id=str(rel.get('parent_id') or ''),
            has_children=bool(rel.get('has_children')))
'''},
    'moebooru_json': {
        'doc': 'Moebooru API (post.json, count from post.xml)', 'per_page': 100, 'max_pages': 1000, 'interval': 1.0,
        'imports': 'import re\n', **_FAV, 'body': '''
    def _query_tags(self, tags: str) -> str:
        # favorites: the core's 'favorited_by:<name>' (manifest syntax.fav) is the site's 'vote:3:<name>' (3: favorite)
        return self.TAGS_CONCAT.join(_site_fav(t, 'vote:3:') or t for t in tags.split(self.TAGS_CONCAT))

    def _search_url(self, tags: str, page: int, limit: int) -> str:
        return f'{{SITENAME}}post.json?tags={{tags}}&limit={{limit}}&page={{page + 1}}'

    def _count_url(self, tags: str) -> str:
        return f'{{SITENAME}}post.xml?tags={{tags}}&limit=1'

    def _cursor_url(self, tags: str, cursor: int | None, ascending: bool, limit: int) -> str:
        extra = f'+order:{{"id" if ascending else "id_desc"}}' + ('' if cursor is None else f'+id:{{">" if ascending else "<"}}{{cursor}}')
        return self._search_url(f'{{tags}}{{extra}}', 0, limit)

    def _count_from(self, raw: bytes) -> int | None:
        m = re.search(rb'<posts[^>]*\\scount="(\\d+)"', raw)
        return int(m.group(1)) if m else None

    def _posts(self, data: object) -> list[dict] | None:
        return data if isinstance(data, list) else None

    def _post(self, p: dict) -> JsonPost | None:
        return JsonPost(
            id=str(p.get('id', '')), file_url=p.get('file_url') or '', tags=str(p.get('tags') or '').split(),
            created_at=p.get('created_at') or '', sample_url=p.get('sample_url') or p.get('jpeg_url') or '',
            preview_url=p.get('preview_url') or '', md5=p.get('md5') or '', width=p.get('width') or 0,
            height=p.get('height') or 0, score=p.get('score') or 0, rating=p.get('rating') or '', source=p.get('source') or '',
            parent_id=str(p.get('parent_id') or ''), has_children=bool(p.get('has_children')))
'''},
    'philomena_json': {
        'doc': 'Philomena API (api/v1/json/search/images)', 'per_page': 50, 'max_pages': 1000, 'interval': 1.0,
        'imports': '', **_FAV, 'body': '''
    def _query_tags(self, tags: str) -> str:
        # philomena: tags joined with commas, spaces in tag names. Favorites: the core's 'favorited_by:<name>' (manifest
        # syntax.fav) is the site's 'faved_by:<name>' (a field name: its own '_' stays)
        return ','.join(_site_fav(t, 'faved_by:', ' ') or t.replace('_', ' ') for t in tags.split('+'))

    def _search_url(self, tags: str, page: int, limit: int) -> str:
        return f'{{SITENAME}}api/v1/json/search/images?q={{tags or "*"}}&per_page={{limit}}&page={{page + 1}}'

    def _cursor_url(self, tags: str, cursor: int | None, ascending: bool, limit: int) -> str:
        q = ','.join(x for x in (tags, '' if cursor is None else f'id.{{"gt" if ascending else "lt"}}:{{cursor}}') if x) or '*'
        return f'{{SITENAME}}api/v1/json/search/images?q={{q}}&sf=id&sd={{"asc" if ascending else "desc"}}&per_page={{limit}}&page=1'

    def _posts(self, data: object) -> list[dict] | None:
        images = data.get('images') if isinstance(data, dict) else None
        return images if isinstance(images, list) else None

    def _count(self, data: object) -> int | None:
        total = data.get('total') if isinstance(data, dict) else None
        return total if isinstance(total, int) else None

    def _post(self, p: dict) -> JsonPost | None:
        reps = p.get('representations') or {{}}
        hidden = p.get('hidden_from_users') or p.get('deletion_reason')
        return JsonPost(
            id=str(p.get('id', '')), file_url='' if hidden else (p.get('view_url') or reps.get('full') or ''),
            tags=[str(t) for t in p.get('tags') or []], created_at=p.get('created_at') or p.get('first_seen_at') or '',
            sample_url=reps.get('large') or reps.get('medium') or '', preview_url=reps.get('thumb') or reps.get('small') or '',
            width=p.get('width') or 0, height=p.get('height') or 0, score=p.get('score') or 0,
            source=' '.join(p.get('source_urls') or []) or p.get('source_url') or '')
'''},
    'szurubooru_json': {
        'doc': 'Szurubooru API (api/posts/, Accept: application/json)', 'per_page': 100, 'max_pages': 1000, 'interval': 1.0,
        'imports': '', **_FAV, 'body': '''
    def _query_tags(self, tags: str) -> str:
        # favorites: the core's 'favorited_by:<name>' (manifest syntax.fav) is the site's 'fav:<name>'
        return self.TAGS_CONCAT.join(_site_fav(t, 'fav:') or t for t in tags.split(self.TAGS_CONCAT))

    def _search_url(self, tags: str, page: int, limit: int) -> str:
        return f'{{SITENAME}}api/posts/?query={{tags}}&offset={{page * limit}}&limit={{limit}}'

    def _posts(self, data: object) -> list[dict] | None:
        results = data.get('results') if isinstance(data, dict) else None
        return results if isinstance(results, list) else None

    def _count(self, data: object) -> int | None:
        total = data.get('total') if isinstance(data, dict) else None
        return total if isinstance(total, int) else None

    def _post(self, p: dict) -> JsonPost | None:
        names = [(t.get('names') or [''])[0] for t in p.get('tags') or [] if isinstance(t, dict)]
        return JsonPost(
            id=str(p.get('id', '')), file_url=p.get('contentUrl') or '', tags=[n for n in names if n],
            created_at=p.get('creationTime') or '', preview_url=p.get('thumbnailUrl') or '', md5=p.get('checksumMD5') or '',
            width=p.get('canvasWidth') or 0, height=p.get('canvasHeight') or 0, score=p.get('score') or 0,
            rating=p.get('safety') or '', source=p.get('source') or '')
'''},
    'shimmie_api': {
        'doc': 'Shimmie2 danbooru compatible API (api/danbooru/find_posts, XML)', 'per_page': 100, 'max_pages': 0, 'interval': 1.0,
        'imports': 'import html\nimport re\n', 'body': '''
    TAGS_CONCAT = ' '
    ACCEPT = 'application/xml, text/xml'

    def _search_url(self, tags: str, page: int, limit: int) -> str:
        return f'{{SITENAME}}api/danbooru/find_posts?tags={{tags}}&limit={{limit}}&page={{page + 1}}'

    def _parse(self, raw: bytes) -> object:
        text = raw.decode('utf-8', 'replace')
        if '<posts' not in text:
            return None
        count = re.search(r'<posts[^>]*\\scount=["\\'](\\d+)', text)
        posts = [{{k: html.unescape(v) for k, _, v in re.findall(r'([a-z_]+)=(["\\'])(.*?)\\2', m)}}  # '' or "" quotes
                 for m in re.findall(r'<post\\s[^>]*>', text)]
        return {{'count': int(count.group(1)) if count else None, 'posts': posts}}

    def _posts(self, data: object) -> list[dict] | None:
        return data.get('posts') if isinstance(data, dict) else None

    def _count(self, data: object) -> int | None:
        return data.get('count') if isinstance(data, dict) else None

    def _post(self, p: dict) -> JsonPost | None:
        name = p.get('file_name') or ''
        return JsonPost(
            id=str(p.get('id', '')), file_url=p.get('file_url') or '', tags=str(p.get('tags') or '').split(),
            created_at=p.get('date') or '', preview_url=p.get('preview_url') or '', md5=p.get('md5') or '',
            width=int(p.get('width') or 0), height=int(p.get('height') or 0), score=p.get('score') or 0,
            rating=p.get('rating') or '', source=p.get('source') or '',
            ext=name[name.rfind('.') + 1:] if '.' in name else '')  # shimmie file urls may have no extension
'''},
}

HTML_EXAMPLE = '''# coding=UTF-8
\"\"\"
Example (reference 'template_html', not a ready template): a site whose HTML listing pages carry everything per post
(file link, tags, date) parsed with BeautifulSoup through DownloaderJsonApi._parse. Adapt the url, the selectors and the
attribute names to the real pages seen with http_fetch. If the file url or the tags are only on each post page, follow the
'rule34us' reference instead (a Downloader that fetches item pages).
\"\"\"

from typing import final

from bs4 import BeautifulSoup

from lynxx.sdk import DownloaderJsonApi, JsonPost, ModuleCaps

__all__ = ('DownloaderEx',)

SITENAME = "https://example.com/"
MODULE_ABBR = "ex"


@final
class DownloaderEx(DownloaderJsonApi):
    CAPABILITIES = frozenset((ModuleCaps.DATE_FILTER, ModuleCaps.PREVIEW))
    SITE = SITENAME
    ABBR = MODULE_ABBR
    ITEMS_PER_PAGE = 42  # posts on one listing page of the site
    MAX_PAGES = 0  # 0: the site has no page depth limit
    ACCEPT = 'text/html'

    def _get_min_request_interval(self) -> float:
        return 1.0  # html pages: at least 1 second between requests (R4)

    def _search_url(self, tags: str, page: int, limit: int) -> str:
        return f'{SITENAME}posts?tags={tags}&page={page + 1}'

    def _parse(self, raw: bytes) -> object:
        return BeautifulSoup(raw, 'html.parser')

    def _posts(self, data: object) -> list[dict] | None:
        listing = data.find('section', id='posts') if isinstance(data, BeautifulSoup) else None
        if listing is None:
            return None  # not a listing page (error page, challenge...)
        return [dict(a.attrs) for a in listing.find_all('article')]

    def _count(self, data: object) -> int | None:
        total = data.find(attrs={'data-total': True}) if isinstance(data, BeautifulSoup) else None
        return int(total['data-total']) if total is not None and str(total['data-total']).isdigit() else None

    def _post(self, p: dict) -> JsonPost | None:
        return JsonPost(
            id=str(p.get('data-id', '')), file_url=p.get('data-file-url', ''), tags=str(p.get('data-tags', '')).split(),
            created_at=p.get('data-created-at', ''), preview_url=p.get('data-preview-url', ''))
'''


def render_downloader(engine: str, *, ext_id: str, abbr: str, site: str, api_key: bool = False, per_page: int = 100,
                      interval: float = 0.5) -> str:
    if engine not in TEMPLATE_ENGINES:
        raise ValueError(f'no template for engine \'{engine}\'')
    validate_names(ext_id, abbr, 'x')
    if not re.fullmatch(r'https://[a-z\d.-]+(?::\d+)?/', site):
        raise ValueError(f'unexpected site address {site!r}')
    values = {
        'ext_id': ext_id, 'abbr': abbr, 'site': site, 'engine': engine, 'date': datetime.date.today().isoformat(),
        'cls': class_name(abbr), 'site_lit': json.dumps(site), 'abbr_lit': json.dumps(abbr),
        'per_page': int(max(10, min(per_page, 1000))), 'interval': float(max(0.5, interval)),
        'api_caps': ', ModuleCaps.API_KEY' if api_key else '', 'api_key_method': _GELBOORU_API_KEY if api_key else '',
    }
    if engine in _JSON_ENGINES:
        e = _JSON_ENGINES[engine]
        values.update(doc=e['doc'], per_page=e['per_page'], max_pages=e['max_pages'], interval=float(max(e['interval'], interval)),
                      imports=e['imports'], sdk_names=e.get('sdk_names', ''), helpers=e.get('helpers', ''),
                      body=e['body'].format(**values))
        return _JSON.format(**values)
    return (_GELBOORU if engine == 'gelbooru_dapi' else _DANBOORU).format(**values)

#
#
#########################################
