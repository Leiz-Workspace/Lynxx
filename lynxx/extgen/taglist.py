# coding=UTF-8
"""
Lynxx extension generator: the site's tag list goes into the draft, so a new extension has autocompletion and Tags A-Z at once.
The list is read by lynxx.tags_updater (the same reader as the app's update: robots.txt applies strictly to these sites,
Crawl-delay, Retry-After, a request budget) and saved as the manifest's 'tagList' ('<abbr>_tags.json') next to manifest.json;
lynxx.extgen.sandbox.pack puts it into the archive and the installed extension's folder keeps it (lynxx/tagsdb.py finds it there).
A list that needs more than the draft's budget (requests, seconds, size) or the user's API key is left to the app, which reads
it right after the install. A site without a tag listing gets its list learned from the posts the user previews and downloads
(lynxx/tags_learned.py).
"""
#########################################
#
#

from __future__ import annotations

import pathlib
import shutil
import tempfile
import time

from lynxx.modspec import ExtensionManifest
from lynxx.tags_updater import ENGINE_TAG_SOURCES, TagSourceError, source_for_manifest, update_tag_list

__all__ = ('DRAFT_TAG_MAX_BYTES', 'DRAFT_TAG_REQUESTS', 'DRAFT_TAG_SECONDS', 'bundle_tag_list', 'draft_tag_list')

DRAFT_TAG_REQUESTS = 30
'''robots.txt + 29 pages: about 29,000 tags on a site answering 1000 per page'''
DRAFT_TAG_SECONDS = 90.0
DRAFT_TAG_MAX_BYTES = 8 * 1024 * 1024
'''an extension archive holds 10 MB at most (lynxx/extensions.py ARCHIVE_SIZE_MAX); tag lists compress about 4 times'''

LATER = 'the app reads it right after the install'
LEARNED = 'the app learns the list from the posts you preview and download (no request)'


def draft_tag_list(draft: pathlib.Path) -> pathlib.Path | None:
    """The tag list file of a draft (or an installed extension folder): the manifest's tagList, '<abbr>_tags.json' only"""
    try:
        m = ExtensionManifest.load(pathlib.Path(draft) / 'manifest.json', 'user')
    except Exception:
        return None
    if m.tagList != f'{m.abbr}_tags.json':
        return None
    path = pathlib.Path(draft) / m.tagList
    return path if path.is_file() else None


def bundle_tag_list(draft: pathlib.Path, *, session=None, allow_local=False, min_interval: float | None = None,
                    budget: int = DRAFT_TAG_REQUESTS, max_seconds: float = DRAFT_TAG_SECONDS, max_bytes: int = DRAFT_TAG_MAX_BYTES) -> dict:
    """Reads the site's tag list into the draft. session / allow_local: the generator's transport (tests: a fake site).
    -> {status: bundled | later | learned | failed, file, count, requests, message}:
    bundled: '<abbr>_tags.json' is in the draft; later: the app reads it after the install (too big, needs the user's key, the site
    asked to wait); learned: no tag listing, or robots.txt disallows it; failed: the site did not answer (the app tries again), or
    the extension's own update merges (tagUpdate.merge) and the draft has no list for it to go into (the app can never make one)"""
    draft = pathlib.Path(draft)
    m = ExtensionManifest.load(draft / 'manifest.json', 'user')
    src = source_for_manifest(m, jobs=False)  # a draft's own code runs only in the sandbox, never here
    engine = str((m.generator or {}).get('engine', ''))
    job = str(m.tagUpdate.get('entry', ''))
    if src is None and job and m.tagList == f'{m.abbr}_tags.json':
        if (path := draft / m.tagList).is_file():
            count = sum(1 for line in path.read_text(encoding='utf-8').splitlines() if line.strip() not in ('', '{', '}'))
            return {'status': 'bundled', 'file': m.tagList, 'count': count, 'requests': 0, 'message': f'{count:d} tags, the extension\'s own list'}
        if isinstance(merge := m.tagUpdate.get('merge'), dict):  # it reads only the most used tags: never a whole list
            return {'status': 'failed', 'file': '', 'count': 0, 'requests': 0,
                    'message': f'{job} reads only the tags with {merge.get("minPosts")}+ posts and merges them into the list the '
                               f'extension ships (tagUpdate.merge): put {m.tagList} next to manifest.json, or remove \'merge\' if the '
                               f'job reads the whole list'}
        return {'status': 'later', 'file': '', 'count': 0, 'requests': 0,
                'message': f'{job} is the extension\'s own update: the app runs it only when you ask (Tags A-Z, Cập nhật)'}
    if src is None or m.tagList != f'{m.abbr}_tags.json':
        why = f'engine \'{engine or "?"}\' has no tag listing' if engine not in ENGINE_TAG_SOURCES else 'the manifest names no tag list'
        return {'status': 'learned', 'file': '', 'count': 0, 'requests': 0, 'message': f'{why}: {LEARNED}'}
    if src.key == 'required':
        return {'status': 'later', 'file': '', 'count': 0, 'requests': 0, 'message': f'the site needs your own API key: {LATER}, with your key'}
    with tempfile.TemporaryDirectory(prefix='.taglist-', dir=draft) as tmp:
        try:
            res = update_tag_list(m.abbr, pathlib.Path(tmp), source=src, budget=budget, allow_local=allow_local, session=session,
                                  min_interval=min_interval, deadline=time.monotonic() + max_seconds)
        except TagSourceError as err:
            status = {'robots': 'learned', 'budget': 'later'}.get(err.kind, 'failed')
            tail = LEARNED if status == 'learned' else LATER
            return {'status': status, 'file': '', 'count': 0, 'requests': 0, 'message': f'{err}: {tail}'}
        except Exception as err:
            return {'status': 'failed', 'file': '', 'count': 0, 'requests': 0, 'message': f'{err}: {LATER}'}
        if not res.written:
            return {'status': 'failed', 'file': '', 'count': res.count, 'requests': res.requests, 'message': f'{res.message}: {LATER}'}
        size = res.path.stat().st_size
        if size > max_bytes:
            return {'status': 'later', 'file': '', 'count': res.count, 'requests': res.requests,
                    'message': f'{res.count:d} tags, {size / 1048576:.1f} MB: too big for the draft, {LATER}'}
        dest = draft / m.tagList
        shutil.move(res.path, dest)
    return {'status': 'bundled', 'file': m.tagList, 'count': res.count, 'requests': res.requests,
            'message': f'{res.count:d} tags from {src.description} ({res.requests:d} requests)'}

#
#
#########################################
