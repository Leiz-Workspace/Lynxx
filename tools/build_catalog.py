# coding=UTF-8
"""
Lynxx: builds the remote extension catalog from extensions/ (docs/operations.md, 3d).

  python tools/build_catalog.py --out <dir> [--base-url URL] [--extensions DIR] [--lists DIR]

Writes one zip per extension (<id>-<version>.zip, manifest.json at the root) and index.json listing them with the sha256
of each archive. The zips are deterministic: same files, same bytes, same sha256, so an unchanged extension keeps its
checksum between catalog builds. Sign the result with tools/sign_catalog.py, then publish index.json, index.json.sig and
the zips side by side under --base-url (the official catalog: the "catalog" release of the GitHub repository).

Tag lists stay out of the repository (docs/plans/ext-tag-jobs.md): an extension's manifest names its list ("tagList":
"<abbr>_tags.json"). A list kept next to its manifest on the maintainer's machine (.gitignore leaves extensions/*/*_tags.json
out) is used first; --lists is a folder for the others (made with 'lynxx-core tags --modules <abbr> --out <dir>', or taken
out of the zips of the catalog in use). Each list goes into its extension's zip, next to manifest.json, so
the extension has it as soon as it is installed. An extension the app cannot make the whole list of by itself takes the
repository's 2tags/<abbr>_tags.json when neither place has its list, like the catalog bundled with the core (tools/build_core.py
_stage_catalog), and the build stops when that is missing too: without a list in its zip, an update from this catalog would leave
the module with no list (or with only the most used tags). That is one with no tag source (no 'tagUpdate', not a reviewed module, no
engine with a tag listing: lynxx/tags_updater.py source_for_manifest; rn, rs), or one whose own update reads only the most used
tags and merges them into its list (tagUpdate.merge; rp). For a merge source a list from --lists or its folder that is under half
of the 2tags/ one is refused (a list its own update wrote alone). Any other extension whose list is not there is built without it:
the app reads the list with the extension's own update when the user asks (Tags A-Z, "Cập nhật"). A zip over the installer's limits
is refused. A manifest older cores refuse (a minSdkVersion newer than their SDK, a top-level or tagUpdate key they do not know:
lynxx/modspec.py SDK_VERSIONS_SINCE, MANIFEST_KEYS_SINCE, TAG_UPDATE_KEYS_SINCE) is said on stderr: users of those cores are offered
an update they cannot install (docs/operations.md, 3d).
"""
#########################################
#
#

from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import sys
import zipfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lynxx.extensions import ARCHIVE_ENTRIES_MAX, ARCHIVE_SIZE_MAX, ARCHIVE_UNPACKED_MAX  # noqa: E402

OFFICIAL_BASE_URL = 'https://github.com/Leiz-Workspace/Lynxx/releases/download/catalog/'
TAGS_DIR = ROOT / '2tags'
SKIP_NAMES = {'__pycache__', '.DS_Store'}
ZIP_DATE = (1980, 1, 1, 0, 0, 0)


def _files(ext_dir: pathlib.Path) -> dict[str, pathlib.Path]:
    return {p.relative_to(ext_dir).as_posix(): p for p in ext_dir.rglob('*')
            if p.is_file() and not SKIP_NAMES & set(p.relative_to(ext_dir).parts) and p.suffix != '.pyc'}


def build_archive(ext_dir: pathlib.Path, dest: pathlib.Path, extra: dict[str, pathlib.Path] | None = None) -> str:
    """Zips one extension folder deterministically, plus extra files (archive name -> source, ex. its tag list); returns the
    sha256 of the archive. Raises SystemExit when the zip would be refused by the installer (lynxx/extensions.py limits)"""
    files = {**_files(ext_dir), **(extra or {})}
    unpacked = 0
    with zipfile.ZipFile(dest, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for name in sorted(files):
            data = files[name].read_bytes()
            unpacked += len(data)
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            zf.writestr(info, data)
    size = dest.stat().st_size
    if size > ARCHIVE_SIZE_MAX or unpacked > ARCHIVE_UNPACKED_MAX or len(files) > ARCHIVE_ENTRIES_MAX:
        raise SystemExit(f'{dest.name}: {size / 1048576:.1f} MB packed, {unpacked / 1048576:.1f} MB unpacked, {len(files):d} files: '
                         f'over the installer limits ({ARCHIVE_SIZE_MAX // 1048576:d} MB, {ARCHIVE_UNPACKED_MAX // 1048576:d} MB, '
                         f'{ARCHIVE_ENTRIES_MAX:d} files)')
    return hashlib.sha256(dest.read_bytes()).hexdigest()


def tag_source(ext_dir: pathlib.Path):
    """Where the app reads this extension's tag list from (lynxx/tags_updater.py source_for_manifest), None: nowhere"""
    from lynxx.extensions import SOURCE_CATALOG
    from lynxx.modspec import ExtensionManifest
    from lynxx.tags_updater import source_for_manifest
    return source_for_manifest(ExtensionManifest.load(ext_dir / 'manifest.json', SOURCE_CATALOG))


def needs_shipped_list(src) -> bool:
    """The app cannot make the whole list by itself: no tag source, or its own update reads only part of the site's list
    (TagSource.merge)"""
    return src is None or src.merge


def _tag_count(path: pathlib.Path) -> int:
    with open(path, 'rb') as f:
        return sum(1 for line in f if line.strip() not in (b'{', b'}', b''))


def tag_list_source(ext_dir: pathlib.Path, manifest: dict, lists: pathlib.Path | None,
                    tags_dir: pathlib.Path | None = TAGS_DIR) -> pathlib.Path | None:
    """The tag list file to put into the zip of this extension (None: no list declared, or already in its folder, or not found).
    Next to its manifest, else in lists, else (needs_shipped_list: no tag source, or a merge source) the repository's 2tags/. Raises
    SystemExit when such an extension has its list nowhere, or (a merge source) a list under half of the 2tags/ one"""
    name = str(manifest.get('tagList') or '')
    if not name or pathlib.PurePosixPath(name).name != name:
        return None
    src = tag_source(ext_dir)
    repo = tags_dir / name if tags_dir is not None and name == f'{manifest.get("abbr", "")}_tags.json' else None
    path = ext_dir / name if (ext_dir / name).is_file() else lists / name if lists is not None else None
    if src is not None and src.merge and path is not None and path.is_file() and repo is not None and repo.is_file():
        # its own update reads only the most used tags: a list that update wrote alone would be all a user gets, then the base of
        # every merge after it
        if (n := _tag_count(path)) < (full := _tag_count(repo)) * 0.5:
            raise SystemExit(f'{manifest.get("id")}: {path} has {n:d} tags, under half of {repo} ({full:d}): its own update reads only '
                             f'the tags with {src.merge_min_posts:d}+ posts, a list it made must be merged into this one')
    if (ext_dir / name).is_file():
        return None
    if path is None or not path.is_file():
        if not needs_shipped_list(src):
            return None  # read from its source after the install, or made by its own update when the user asks
        path = repo
        if path is None or not path.is_file():
            why = ('the app has no source to read it from' if src is None else
                   f'its own update reads only the tags with {src.merge_min_posts:d}+ posts and merges them into this list')
            raise SystemExit(f'{manifest.get("id")}: no {name} next to its manifest, in --lists or in {tags_dir}, and {why}: an update '
                             f'from this catalog would leave the module without a list')
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except ValueError as err:
        raise SystemExit(f'{path}: not a tag list ({err})')
    if not isinstance(data, dict) or not data:
        raise SystemExit(f'{path}: not a tag list (expected {{"tag": "N posts (type)", ...}})')
    return path


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split('.') if x.isdigit())


def newer_core_keys(manifest: dict) -> list[str]:
    """What cores before a release refuse (the whole manifest): 'minSdkVersion <v> (Lynxx <version>)' for an SDK newer than the
    one of the oldest supported cores (SDK_VERSIONS_SINCE), '<key> (Lynxx <version>)' for each top-level key (MANIFEST_KEYS_SINCE,
    ex. 'search') and 'tagUpdate.<key> (Lynxx <version>)' for each tagUpdate key (TAG_UPDATE_KEYS_SINCE)"""
    from lynxx.modspec import MANIFEST_KEYS_SINCE, SDK_VERSIONS_SINCE, TAG_UPDATE_KEYS_SINCE
    out = []
    sdk = manifest.get('minSdkVersion')
    if isinstance(sdk, str) and (since := [rel for v, rel in SDK_VERSIONS_SINCE.items() if _version_key(sdk)[:2] >= _version_key(v)]):
        out.append(f'minSdkVersion {sdk} (Lynxx {max(since, key=_version_key)})')
    out += [f'{k} (Lynxx {MANIFEST_KEYS_SINCE[k]})' for k in manifest if k in MANIFEST_KEYS_SINCE]
    tu = manifest.get('tagUpdate')
    keys = tu if isinstance(tu, dict) else {}
    return out + [f'tagUpdate.{k} (Lynxx {TAG_UPDATE_KEYS_SINCE[k]})' for k in keys if k in TAG_UPDATE_KEYS_SINCE]


def build(extensions_dir: pathlib.Path, out: pathlib.Path, base_url: str, lists: pathlib.Path | None = None,
          tags_dir: pathlib.Path | None = TAGS_DIR) -> dict:
    if not base_url.lower().startswith('https://'):
        raise SystemExit('--base-url must be https')
    if lists is not None and not lists.is_dir():
        raise SystemExit(f'--lists: {lists} is not a folder')
    base_url = base_url if base_url.endswith('/') else base_url + '/'
    out.mkdir(parents=True, exist_ok=True)
    entries = []
    for manifest_path in sorted(extensions_dir.glob('*/manifest.json')):
        ext_dir = manifest_path.parent
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        ext_id, version = manifest['id'], manifest['version']
        name = f'{ext_id}-{version}.zip'
        tag_list = tag_list_source(ext_dir, manifest, lists, tags_dir)
        sha256 = build_archive(ext_dir, out / name, {manifest['tagList']: tag_list} if tag_list else None)
        if manifest.get('tagList') and not tag_list and not (ext_dir / str(manifest['tagList'])).is_file():
            print(f'{ext_id}: no {manifest["tagList"]} in --lists: built without its tag list (the app reads it from its source '
                  f'when asked)', file=sys.stderr)
        if newer := newer_core_keys(manifest):
            print(f'{ext_id} {version}: {", ".join(newer)}: older cores refuse to install it (the version they have stays). Publish '
                  f'this catalog once that release is out, or build from a copy of the extensions that keeps the version before',
                  file=sys.stderr)
        entries.append({'id': ext_id, 'name': manifest.get('name', ext_id), 'abbr': manifest.get('abbr', ''), 'version': version,
                        'url': base_url + name, 'sha256': sha256, 'site': manifest.get('site', ''),
                        'description': manifest.get('description', '')})
    index = {'extensions': entries}
    (out / 'index.json').write_text(json.dumps(index, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    return index


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description='Build the Lynxx remote extension catalog (zips + index.json)')
    ap.add_argument('--out', required=True, help='output directory')
    ap.add_argument('--base-url', default=OFFICIAL_BASE_URL, help='https url the files are published under')
    ap.add_argument('--extensions', default=str(ROOT / 'extensions'), help='folder with one extension per subfolder')
    ap.add_argument('--lists', default=None, help='folder with the tag lists named by the manifests ("tagList"), put into the zips')
    a = ap.parse_args(argv)
    index = build(pathlib.Path(a.extensions), pathlib.Path(a.out), a.base_url, pathlib.Path(a.lists) if a.lists else None)
    for e in index['extensions']:
        print(f"{e['id']} {e['version']} {e['sha256']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())

#
#
#########################################
