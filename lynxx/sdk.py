# coding=UTF-8
"""
Lynxx SDK: the stable API for extension authors (plan site-extension-skill-and-ai-generator, QĐ3 / G1).
A downloader.py imports what it needs from here instead of the core modules:

    from lynxx.sdk import DownloaderJsonApi, JsonPost, ModuleCaps

An extension may also ship its own tag list update (manifest "tagUpdate": {"entry": "tags.py:read_tags"}, "minSdkVersion": "1.1"):
read_tags(job: TagJob) yields (name, posts, type) for each tag of the site, its requests go through job.get_text / job.get_json.
Lynxx runs it only when the user asks (Tags A-Z), never on a schedule.
"merge": {"minPosts": N} in tagUpdate declares an update that reads only the tags with N+ posts; Lynxx 2.0.10+ merges it into the
list in use. The job must give names in the list's form (lowercase where the site searches without case, '_' for spaces), because
the core matches names exactly.
A row without a type - (name, posts), or None, '' or 'unknown' as its type - takes the type the list the extension ships (manifest
"tagList") gives that very name when it is a specific one (artist, character, copyright, metadata...): a job on a site that has no
tag types keeps the types the shipped list was given. A type the job gives, 'general' included, is written as it is (a merge
keeps the type of the list in use over 'general', as before). Lynxx 2.0.12+: older cores write 'general' ('unknown' for
'unknown') for such a row, so (name, posts) is safe for every core; no new name, the SDK version stays. See TagJob.

Search kinds (manifest "search", Lynxx 2.0.11+, SDK 1.2): a kind with a token is one tag of the query ('artist:some_artist').
split_kind(tags) in _query_tags / _search_url gives (KindMatch(id, token, value) | None, the other tags); self.current_kind is the
query's kind, set before any search address is built; a DownloaderJsonApi pages a kind's listing with _items_per_page() (the
kind's pageSize) and _max_pages(). A route built from user input is checked with self.robots_allows(url) /
self.require_robots_allowed(url) against the site's robots.txt saved next to the manifest (RFC 9309).

Media choices (Lynxx 2.0.14+, SDK 1.3): self.max_video_height is the highest video height the user wants (-max_vres, the app's
video quality choice; 0 = the best file). A module with several video files of a post takes the best one whose height is <= it,
else the smallest one, and declares 'video_quality_choice' (ModuleCaps.VIDEO_QUALITY_CHOICE) in CAPABILITIES: the app shows the
choice only for such modules, the others ignore the option silently. Write the capability as the plain string and read the value
with getattr(self, 'max_video_height', 0) or 0: cores before 2.0.14 have neither, and the extension keeps loading there (without
the choice) instead of failing on import or while downloading. self.low_res (-lowres, "Bản nhẹ" in the app) is for images only:
the reduced image (sample) instead of the original; a module never uses it to pick a video file. self.prefer_webm / prefer_mp4
pick a video format (ModuleCaps.VIDEO_FORMAT_CHOICE).

Versioning (SDK_VERSION, 'MAJOR.MINOR'): a MINOR release only adds names; a MAJOR release may remove or change one, after the
name was marked deprecated for at least one MINOR release. An extension that needs names added in a later release declares
"minSdkVersion": "MAJOR.MINOR" in its manifest.json: Lynxx refuses to load it with a clear message when its SDK is older
(lynxx/extensions.py). Extensions written before the SDK (importing lynxx.defines / lynxx.download... directly) keep
working; the generator's templates and the AI use the SDK.

What the core does, and an extension must not redo: every request (self.fetch_html), file downloads, retries, rate limits,
Retry-After, bot challenge detection, date / tag / safety filters (lynxx.extgen.policy enforces it for generated code).
"""
#########################################
#
#

from __future__ import annotations

import re

from .defines import (
    DATE_MIN_DEFAULT,
    FILE_NAME_FULL_MAX_LEN,
    FMT_DATE,
    KNOWN_EXTENSIONS,
    KNOWN_EXTENSIONS_AUD,
    KNOWN_EXTENSIONS_IMG,
    KNOWN_EXTENSIONS_VID,
    Comment,
    DownloadModes,
    ItemInfo,
    MediaKind,
    ModuleCaps,
    media_kind_by_ext,
)
from .download import Downloader
from .download_db import DownloaderDb
from .download_gelbooru import DownloaderGelbooru
from .download_json import DownloaderJsonApi, JsonPost
from .logger import trace
from .network import LYNXX_USER_AGENT, DownloadInterruptException, HTMLContent, thread_exit
from .rex import (
    re_favorited_by_tag,
    re_item_filename,
    re_item_info_part_xml,
    re_orig_file_link,
    re_sample_file_link,
    re_tags_exclude_rx,
    re_tags_to_process_rx,
)
from .search_kinds import KindMatch, split_kind
from .tags_updater import TagJob
from .utils import as_date, format_score

__all__ = (
    'DATE_MIN_DEFAULT',
    'FILE_NAME_FULL_MAX_LEN',
    'FMT_DATE',
    'KNOWN_EXTENSIONS',
    'KNOWN_EXTENSIONS_AUD',
    'KNOWN_EXTENSIONS_IMG',
    'KNOWN_EXTENSIONS_VID',
    'LYNXX_USER_AGENT',
    'SDK_VERSION',
    'Comment',
    'DownloadInterruptException',
    'DownloadModes',
    'Downloader',
    'DownloaderDb',
    'DownloaderGelbooru',
    'DownloaderJsonApi',
    'HTMLContent',
    'ItemInfo',
    'JsonPost',
    'KindMatch',
    'MediaKind',
    'ModuleCaps',
    'TagJob',
    'as_date',
    'format_score',
    'media_kind_by_ext',
    're_favorited_by_tag',
    're_item_filename',
    're_item_info_part_xml',
    're_orig_file_link',
    're_sample_file_link',
    're_tags_exclude_rx',
    're_tags_to_process_rx',
    'sdk_satisfies',
    'split_kind',
    'thread_exit',
    'trace',
)

SDK_VERSION = '1.3'
'''1.0 (2026-11-17): downloader bases (Downloader, DownloaderGelbooru, DownloaderDb, DownloaderJsonApi + JsonPost), item / media
types and constants, generic item regexes, network helpers allowed to extensions, trace, as_date, format_score.
1.1 (2026-11-19): TagJob, for the extension's own tag list update (manifest 'tagUpdate').
1.2 (2026-11-21): search kinds: split_kind + KindMatch, Downloader.current_kind, DownloaderJsonApi._items_per_page / _max_pages,
Downloader.robots_allows / require_robots_allowed (the extension's saved robots.txt).
1.3 (2026-11-22, Lynxx 2.0.14): Downloader.max_video_height (-max_vres) and ModuleCaps.VIDEO_QUALITY_CHOICE
('video_quality_choice'); low_res is for images only. An extension that reads them with getattr and writes the plain string
needs no minSdkVersion 1.3 (see the module docstring)'''
re_sdk_version = re.compile(r'^(\d{1,3})\.(\d{1,3})(?:\.\d{1,3})?$')


def parse_sdk_version(value: str) -> tuple[int, int]:
    """'1.2' (or '1.2.0') -> (1, 2). ValueError for anything else"""
    m = re_sdk_version.fullmatch(str(value).strip())
    if not m:
        raise ValueError(f'invalid SDK version \'{value}\' (expected MAJOR.MINOR, ex. \'1.0\')')
    return int(m.group(1)), int(m.group(2))


def sdk_satisfies(required: str, current: str = SDK_VERSION) -> bool:
    """True if this SDK provides what an extension requiring 'required' needs: same MAJOR, MINOR not older"""
    req, cur = parse_sdk_version(required), parse_sdk_version(current)
    return req[0] == cur[0] and req[1] <= cur[1]

#
#
#########################################
