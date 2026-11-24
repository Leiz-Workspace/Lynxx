# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
Re-tag / un-tag tools. Post infos come from the download index (item_infos_from_index) and / or the per-run info files
(file_parser.gather_item_infos_in_dir). Renamed files keep their index rows (dlindex.rename_indexed_files)
"""
#########################################
#
#

import os
import pathlib
import re
from collections.abc import Iterable, Sequence

from .defines import FILE_NAME_FULL_MAX_LEN, ItemInfo
from .dlindex import DownloadIndex, rename_indexed_files
from .module import ProcModule
from .tagger import append_filtered_tags, append_filtered_tags_by_type
from .tags import TagTypes
from .tagsdb import TagsDB, load_tag_aliases
from .utils import format_score, trim_underscores

__all__ = ('item_infos_from_index', 'retag_files_any', 'retag_files_tagtype', 'untag_files')

re_media_lynxx_name = re.compile(r'^(?:([a-z]{2})_)?(\d+)(?=[_.(\s-]|$)')
'''Lynxx file name: optional module prefix, post id, then a separator ('<md5>.jpg' or '20240101abc.png' are not ids)'''


def _info_for(infos: dict[str, ItemInfo], name: str) -> ItemInfo | None:
    """Index infos are keyed by the file's own name (without extension), info files by post id ('rx_123' -> '123')"""
    return infos.get(name) or infos.get(name[3:] if name[0].isalpha() else name)


def item_infos_from_index(files: Iterable[pathlib.Path], index_path: str | pathlib.Path | None = None) -> dict[str, ItemInfo]:
    """Post infos of Lynxx-named files from the download index, keyed by each file's name without extension: the row of the file
    itself (same path), else the most recent download of the same post (module prefix + id). Only names the re-tag tools rename
    ('[xx_]<id>...') whose id (and prefix) is the row's post: a file recorded by md5 under another name is never renamed after an id
    it does not have. Unprefixed names need the path. Files the index does not know are left out: callers fall back to the info files"""
    out: dict[str, ItemInfo] = {}
    named: dict[str, tuple[pathlib.Path, str, str]] = {}  # path -> (file, module prefix or '', post id)
    for f in files:
        if m := re_media_lynxx_name.match(f.name):
            named[os.path.abspath(f)] = (f, m.group(1) or '', str(int(m.group(2))))  # the form downloads are recorded with
    try:
        with DownloadIndex(index_path) as index:
            by_path = index.rows_for_paths(named)
            wanted: dict[str, list[str]] = {}
            for p, (_, module, post_id) in named.items():
                if p not in by_path and module:
                    wanted.setdefault(module, []).append(post_id)
            by_post = {(module, pid): row for module, ids in wanted.items() for pid, row in index.rows_for_posts(module, ids).items()}
    except Exception:
        return out
    for p, (f, module, post_id) in named.items():
        row = by_path.get(p) or by_post.get((module, post_id))
        if row is None or row['id'] != post_id or (module and row['module'] != module):
            continue
        ii = ItemInfo()
        ii.id = row['id']
        ii.tags = row['tags']
        ii.score = '' if row['score'] is None else str(row['score'])
        ii.md5 = row['md5']
        ii.ext = row['ext']
        out[f.with_suffix('').name] = ii
    return out


def untag_files(files: Iterable[pathlib.Path]) -> int:
    untagged_count = 0
    renamed: list[tuple[pathlib.Path, pathlib.Path]] = []
    try:
        re_media_tagged_name = re.compile(r'^([a-z]{2}_)?(\d+)[_.].+?$')
        for full_path in files:
            if fname_match := re_media_tagged_name.fullmatch(full_path.name):
                untagged_name_noext = f'{fname_match.group(1) or ""}{fname_match.group(2)}'
                new_path = full_path.parent / f'{untagged_name_noext}{full_path.suffix}'
                full_path.rename(new_path)
                renamed.append((full_path, new_path))
                untagged_count += 1
    except Exception:
        pass
    finally:
        rename_indexed_files(renamed)

    return untagged_count


def retag_files_any(files: Sequence[pathlib.Path], infos: dict[str, ItemInfo], re_process: re.Pattern, re_exclude: re.Pattern) -> int:
    retagged_count = 0
    renamed: list[tuple[pathlib.Path, pathlib.Path]] = []
    try:
        if not infos:
            return -1
        re_media_untagged_name = re.compile(r'^([a-z]{2}_)?(\d+).*?$')
        base_path = files[0].parent
        load_tag_aliases()
        for fpath in files:
            name = fpath.with_suffix('').name
            fname_match = re_media_untagged_name.fullmatch(name)
            if not fname_match:
                continue
            item_info = _info_for(infos, name)
            tags_str = item_info.tags if item_info else ''
            score_str = format_score(item_info.score) if item_info else ''
            if tags_str or score_str:
                maxlen = FILE_NAME_FULL_MAX_LEN - len(fpath.as_posix())
                name_notags_noext = f'{fname_match.group(1) or ""}{fname_match.group(2)}'
                add_str = append_filtered_tags(score_str, tags_str, re_process, re_exclude)
                new_name = trim_underscores(f'{name_notags_noext}_{add_str[:maxlen]}{fpath.suffix}')
                fpath.rename(base_path / new_name)
                renamed.append((fpath, base_path / new_name))
                retagged_count += 1
    except Exception:
        pass
    finally:
        rename_indexed_files(renamed)

    return retagged_count


def retag_files_tagtype(files: Sequence[pathlib.Path], infos: dict[str, ItemInfo], *tag_types: TagTypes) -> int:
    retagged_count = 0
    renamed: list[tuple[pathlib.Path, pathlib.Path]] = []
    try:
        if not infos:
            return -1
        if TagsDB.is_empty():
            return -2
        re_media_untagged_name = re.compile(r'^([a-z]{2}_)?(\d+).*?$')
        base_path = files[0].parent
        for fpath in files:
            name = fpath.with_suffix('').name
            fname_match = re_media_untagged_name.fullmatch(name)
            if not fname_match:
                continue
            module = name[:2]
            if module not in ProcModule.known_names():
                continue
            item_info = _info_for(infos, name)
            tags_str = item_info.tags if item_info else ''
            score_str = format_score(item_info.score) if item_info else ''
            if tags_str or score_str:
                maxlen = FILE_NAME_FULL_MAX_LEN - len(fpath.as_posix())
                name_notags_noext = f'{fname_match.group(1) or ""}{fname_match.group(2)}'
                add_str = append_filtered_tags_by_type(score_str, tags_str, module, *tag_types)
                new_name = trim_underscores(f'{name_notags_noext}_{add_str[:maxlen]}{fpath.suffix}')
                fpath.rename(base_path / new_name)
                renamed.append((fpath, base_path / new_name))
                retagged_count += 1
    except Exception:
        pass
    finally:
        rename_indexed_files(renamed)

    return retagged_count

#
#
#########################################
