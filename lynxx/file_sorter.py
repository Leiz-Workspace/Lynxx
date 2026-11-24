# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
Moved files keep their download index rows (dlindex.rename_indexed_files)
"""
#########################################
#
#

import pathlib
import re
from collections.abc import Collection, Iterable, Sequence
from enum import IntEnum, auto

from .defines import Mem, media_kind_by_ext
from .dlindex import rename_indexed_files

__all__ = ('FileTypeFilter', 'sort_files_by_score', 'sort_files_by_size', 'sort_files_by_type')


# PyCharm bug PY-53388 (IDE thinks auto() needs an argument)
# noinspection PyArgumentList
class FileTypeFilter(IntEnum):
    BY_EXTENSION = auto()
    INVALID = auto()


def _get_threshold_index(thresholds: Collection[int | float], val: int | float) -> int:
    for i, threshold in enumerate(thresholds):
        if val < threshold:
            return i
    return len(thresholds)  # folder names array size is len(thresholds) + 1


def _move_file(old_fullpath: pathlib.Path, new_folder: pathlib.Path, file_name: str, moved: list[tuple[pathlib.Path, pathlib.Path]]) -> None:
    new_folder.mkdir(parents=True, exist_ok=True)
    old_fullpath.rename(new_folder / file_name)
    moved.append((old_fullpath, new_folder / file_name))


def sort_files_by_type(files: Iterable[pathlib.Path], filter_type: FileTypeFilter) -> int:
    assert filter_type != FileTypeFilter.INVALID
    moved_count = 0
    moved: list[tuple[pathlib.Path, pathlib.Path]] = []
    for full_path in files:
        try:
            base_path, full_name, ext = full_path.parent, full_path.name, full_path.suffix[1:]
            if filter_type == FileTypeFilter.BY_EXTENSION:
                sub_name = 'jpg' if ext == 'jpeg' else ext
            else:
                sub_name = 'flash' if ext == 'swf' else media_kind_by_ext(ext)
            _move_file(full_path, base_path / sub_name, full_name, moved)
            moved_count += 1
        except Exception:
            continue
    rename_indexed_files(moved)

    return moved_count


def sort_files_by_size(files: Sequence[pathlib.Path], thresholds_mb: Collection[float]) -> int:
    assert all(threshold_mb > 0.01 for threshold_mb in thresholds_mb)
    thresholds_mb = sorted(thresholds_mb)
    moved_count = 0
    moved: list[tuple[pathlib.Path, pathlib.Path]] = []
    try:
        base_path = files[0].parent
        folder_names = [f'size({f"{th - 0.01:.2f}MB-"})' for th in thresholds_mb] + [f'size({f"{thresholds_mb[-1]:.2f}MB+"})']
        for full_path in files:
            try:
                file_size = full_path.stat().st_size
                my_folder = folder_names[_get_threshold_index(thresholds_mb, file_size / Mem.MB)]
                _move_file(full_path, base_path / my_folder, full_path.name, moved)
                moved_count += 1
            except Exception:
                continue
    except Exception:
        pass
    rename_indexed_files(moved)

    return moved_count


def sort_files_by_score(files: Sequence[pathlib.Path], thresholds: Collection[int]) -> int:
    thresholds = sorted(thresholds)
    moved_count = 0
    moved: list[tuple[pathlib.Path, pathlib.Path]] = []
    try:
        base_path = files[0].parent
        folder_names = [f'score({f"{th - 1:d}-"})' for th in thresholds] + [f'score({f"{thresholds[-1]:d}+"})']
        re_media_scored_name = re.compile(r'^(?:[a-z]{2}_)?(?:\d+?)_(?:score)?\([-+]?(\d+)\).+?$')
        for full_path in files:
            try:
                score = re_media_scored_name.fullmatch(full_path.name).group(1)
                my_folder = folder_names[_get_threshold_index(thresholds, int(score))]
                _move_file(full_path, base_path / my_folder, full_path.name, moved)
                moved_count += 1
            except Exception:
                continue
    except Exception:
        pass
    rename_indexed_files(moved)

    return moved_count

#
#
#########################################
