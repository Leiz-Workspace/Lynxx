#!/usr/bin/env python3
# coding=UTF-8
"""
Builds the frozen Lynxx core shipped with the desktop app: a PyInstaller one-dir bundle 'lynxx-core' (entry lynxx/frozen_main.py,
commands 'server' and 'cli ...') in app/build/core/lynxx-core. electron-builder copies it to <resources>/core (app/package.json
build.extraResources) and app/main.js runs it instead of python when the app is packaged.

usage: python tools/build_core.py [--tags aliases|none|all|rx,db,...] [--out DIR] [--no-smoke]
The running interpreter needs the runtime requirements and PyInstaller:
  python -m pip install -r requirements.txt 'pyinstaller>=6.21'
Bundled data: builtin extension manifests, gender rules, the domain denylist and trusted catalog keys (lynxx/safety_data), the
local extension catalog (extensions/, with the 2tags/ list next to the manifest of each extension the app cannot make the whole list
of by itself: rn, rs with no tag source, rp whose own update merges, ~26 MB), tag aliases and optionally tag lists (2tags/, large:
~124 MB for all modules; the app can download fresh lists into its data dir instead).
"""
#########################################
#
#

import argparse
import ast
import importlib.util
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
NAME = 'lynxx-core'
ENTRY = ROOT / 'lynxx' / 'frozen_main.py'
DEFAULT_OUT = ROOT / 'app' / 'build' / 'core'
TAGS_DIR = ROOT / '2tags'
ALIASES_FILE = 'all_tag_aliases.json'

# the legacy Tk GUI is not part of the core: keeps tkinter / Tcl out of the bundle
EXCLUDED_MODULES = (
    'tkinter', '_tkinter', 'tkinterdnd2', 'lxml', 'PyInstaller', 'pip', 'setuptools', 'ruff', 'pydoc', 'lib2to3',
    'lynxx.tests', 'lynxx.release',
    # developer tool (python -m lynxx.aigen.eval), not a command of the app core
    'lynxx.aigen.eval',
    # gender image model: runs in the model server (lynxx/model_server.py), the app core only calls its API. gender_model.py
    # imports these lazily for its in-process backend; without the exclusion a build environment that has them adds ~110 MB
    'numpy', 'onnxruntime', 'PIL', 'lynxx.model_server',
)


def _lynxx_modules() -> list[str]:
    """Core modules importable without tkinter (GUI modules fail to import and are left out). Catalog extensions are loaded from
    files at run time and import lynxx modules the core itself may not use, so every importable module is bundled"""
    names = []
    for pkg in ('lynxx', 'lynxx/vcs', 'lynxx/extgen', 'lynxx/aigen'):
        for p in sorted((ROOT / pkg).glob('*.py')):
            # sandbox_boot runs on import (added below without the import probe); extgen.__main__ is the extgen command
            if p.stem not in ('__init__', '__main__', 'frozen_main', 'sandbox_boot') or (pkg, p.stem) == ('lynxx/extgen', '__main__'):
                names.append(f'{pkg.replace("/", ".")}.{p.stem}')
    probe = (
        'import importlib, json, sys\n'
        'sys.modules["tkinter"] = None\n'
        'ok = []\n'
        'for n in json.loads(sys.argv[1]):\n'
        '    try:\n'
        '        importlib.import_module(n)\n'
        '        ok.append(n)\n'
        '    except BaseException:\n'  # dev scripts may exit on import
        '        pass\n'
        'sys.__stdout__.write("@@" + json.dumps(ok) + "\\n")\n'  # lynxx.server points sys.stdout at stderr
    )
    with tempfile.TemporaryDirectory() as data_dir:
        env = {**os.environ, 'LYNXX_DATA_DIR': data_dir, 'PYTHONPATH': str(ROOT)}
        out = subprocess.run((sys.executable, '-c', probe, json.dumps(names)), cwd=ROOT, env=env, capture_output=True, text=True, check=True)
    return json.loads(next(x for x in out.stdout.splitlines() if x.startswith('@@'))[2:])


def _extension_imports() -> list[str]:
    """Top-level modules imported by catalog extensions (their downloader.py files are not analyzed by PyInstaller)"""
    names: set[str] = set()
    for src in (ROOT / 'extensions').glob('*/*.py'):
        for node in ast.walk(ast.parse(src.read_text(encoding='utf-8'))):
            if isinstance(node, ast.Import):
                names.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                names.add(node.module)
    # collections.abc is always bundled (base_library.zip) but PyInstaller reports it as a missing hidden import
    return sorted(n for n in names if not n.startswith('lynxx') and n != 'collections.abc')


def _stage_catalog(dest: pathlib.Path, src: pathlib.Path = ROOT / 'extensions', tags_dir: pathlib.Path = TAGS_DIR) -> pathlib.Path:
    """The local extension catalog of the core: extensions/ without the tag lists a maintainer keeps next to the manifests (they go
    into the catalog zips, tools/build_catalog.py). An extension whose manifest names a tagList and whose whole list the app cannot
    make by itself gets its list from 2tags/ next to its manifest, so installing it (or updating it from the catalog) gives the
    module its list: one with no tag source (no 'tagUpdate', not a reviewed module, no engine with a tag listing:
    lynxx/tags_updater.py source_for_manifest; rn, rs), or one whose own update reads only the most used tags and merges them into
    that list (tagUpdate.merge; rp). The others read theirs from their source, which keeps the core small"""
    sys.path.insert(0, str(ROOT))
    from lynxx.extensions import SOURCE_CATALOG, Extensions
    from lynxx.tags_updater import source_for_manifest
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.*', '*_tags.json'))
    for m in Extensions._scan(dest, SOURCE_CATALOG):
        source = source_for_manifest(m)
        if not m.tagList or (source is not None and not source.merge):
            continue
        lst = tags_dir / f'{m.abbr}_tags.json'
        if m.tagList != lst.name or not lst.is_file():  # the app uses '<abbr>_tags.json' only (TagsDB.extension_lists)
            print(f'note: {m.id} names the tag list {m.tagList}, {lst} is not it or not there: bundled without a list')
            continue
        shutil.copy2(lst, dest / m.id / lst.name)
    return dest


def _stage_data(stage: pathlib.Path, tags: str) -> list[tuple[pathlib.Path, str]]:
    data = [
        (ROOT / 'lynxx' / 'builtin_ext', 'lynxx/builtin_ext'),
        (ROOT / 'lynxx' / 'gender_rules', 'lynxx/gender_rules'),
        # domain denylist and trusted catalog keys: read at run time, a missing file refuses (fail closed)
        (ROOT / 'lynxx' / 'safety_data', 'lynxx/safety_data'),
    ]
    # lynxx.aigen reads real sources as references for the model (lynxx/aigen/tools.py: REF_FILES)
    sys.path.insert(0, str(ROOT))
    from lynxx.aigen.tools import REF_FILES
    refs = stage / 'aigen_refs'
    for rel in REF_FILES:
        (refs / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, refs / rel)
    data.append((refs, 'aigen_refs'))
    data.append((_stage_catalog(stage / 'extensions'), 'extensions'))
    tag_files: list[pathlib.Path] = []
    if tags != 'none' and (TAGS_DIR / ALIASES_FILE).is_file():
        tag_files.append(TAGS_DIR / ALIASES_FILE)
    if tags == 'all':
        tag_files.extend(sorted(TAGS_DIR.glob('*_tags.json')))
    elif tags not in ('none', 'aliases'):
        for abbr in (t.strip() for t in tags.split(',') if t.strip()):
            f = TAGS_DIR / f'{abbr}_tags.json'
            if not f.is_file():
                raise SystemExit(f'tag list not found: {f}')
            tag_files.append(f)
    if tag_files:
        tags_stage = stage / '2tags'
        tags_stage.mkdir()
        for f in tag_files:
            shutil.copy2(f, tags_stage / f.name)
        data.append((tags_stage, '2tags'))
    return data


def _dir_size(path: pathlib.Path) -> int:
    return sum(f.stat().st_size for f in path.rglob('*') if f.is_file() and not f.is_symlink())


def _smoke_test(exe: pathlib.Path) -> None:
    """The bundle must start the sidecar (ready notification, builtin modules loaded) and run the CLI argument parser"""
    with tempfile.TemporaryDirectory() as data_dir:
        env = {**os.environ, 'LYNXX_DATA_DIR': data_dir}
        env.pop('PYTHONPATH', None)
        r = subprocess.run((str(exe), 'server'), input='{"jsonrpc":"2.0","method":"app.shutdown"}\n', env=env, capture_output=True,
                           text=True, encoding='utf-8', timeout=120)
        ready = next((json.loads(line) for line in r.stdout.splitlines() if line.startswith('{') and '"ready"' in line), None)
        if r.returncode != 0 or not ready:
            raise SystemExit(f'smoke test failed: server exit {r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}')
        print(f'smoke test: server ready, version {ready["params"].get("version")}, python {ready["params"].get("python")}')
        # gender model: the core is a client of the model server (no numpy / onnxruntime in the bundle), port 9 answers nothing
        r = subprocess.run((str(exe), 'server'), env=env, capture_output=True, text=True, encoding='utf-8', timeout=120,
                           input='{"jsonrpc":"2.0","id":1,"method":"gender.modelStatus","params":{"url":"http://127.0.0.1:9"}}\n'
                                 '{"jsonrpc":"2.0","method":"app.shutdown"}\n')
        st = next((json.loads(line) for line in r.stdout.splitlines() if line.startswith('{') and '"id":1' in line.replace(' ', '')), {})
        if st.get('result', {}).get('reachable') is not False or 'not reachable' not in st['result'].get('error', ''):
            raise SystemExit(f'smoke test failed: gender.modelStatus\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}')
        print('smoke test: gender model client ok')
        r = subprocess.run((str(exe), 'cli', '--help'), env=env, capture_output=True, text=True, encoding='utf-8', timeout=120)
        if r.returncode != 0 or '-module' not in r.stdout:
            raise SystemExit(f'smoke test failed: cli --help exit {r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}')
        print('smoke test: cli ok')
        # download index tools (library import jobs of the app): a dry run recognizes names only, no request
        lib = pathlib.Path(data_dir) / 'lib'
        lib.mkdir()
        (lib / 'db_1.jpg').write_bytes(b'x')
        r = subprocess.run((str(exe), 'index', 'import', str(lib), '--dry-run', '--json'), env=env, capture_output=True, text=True,
                           encoding='utf-8', timeout=120)
        if r.returncode != 0 or '"totalAll": 1' not in r.stdout:
            raise SystemExit(f'smoke test failed: index import exit {r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}')
        print('smoke test: index tools ok')
        r = subprocess.run((str(exe), 'aigen'), input='{"cmd":"start","url":"http://example.com/","mode":"template"}\n', env=env,
                           capture_output=True, text=True, encoding='utf-8', timeout=120)
        if '"R10"' not in r.stdout:
            raise SystemExit(f'smoke test failed: aigen exit {r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}')
        print('smoke test: aigen ok')
        # domain denylist bundled (lynxx/safety_data): a denied site is refused before any request
        r = subprocess.run((str(exe), 'aigen'), input='{"cmd":"start","url":"https://www.youtube.com/","mode":"template"}\n', env=env,
                           capture_output=True, text=True, encoding='utf-8', timeout=120)
        if '"R7"' not in r.stdout:
            raise SystemExit(f'smoke test failed: denylist exit {r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}')
        r = subprocess.run((str(exe), 'server'), env=env, capture_output=True, text=True, encoding='utf-8', timeout=120,
                           input='{"jsonrpc":"2.0","id":1,"method":"ext.remoteCatalog","params":{"url":"http://127.0.0.1:9/index.json"}}\n'
                                 '{"jsonrpc":"2.0","method":"app.shutdown"}\n')
        if 'only https' not in r.stdout:
            raise SystemExit(f'smoke test failed: ext.remoteCatalog\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}')
        keys = next(exe.parent.rglob('catalog_keys.json'), None)
        if keys is None or not next(exe.parent.rglob('denied_domains.txt'), None):
            raise SystemExit('smoke test failed: lynxx/safety_data is not bundled')
        print('smoke test: denylist and catalog keys ok')


def build(out_dir: pathlib.Path, tags: str, smoke: bool) -> pathlib.Path:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit('PyInstaller is not installed: python -m pip install -r requirements.txt \'pyinstaller>=6.21\'')
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='lynxx-core-build-') as tmp:
        stage = pathlib.Path(tmp)
        data = _stage_data(stage, tags)
        cmd = [
            sys.executable, '-m', 'PyInstaller', str(ENTRY),
            '--name', NAME, '--onedir', '--console', '--noconfirm', '--clean', '--noupx', '--log-level', 'WARN',
            '--distpath', str(out_dir), '--workpath', str(stage / 'work'), '--specpath', str(stage),
            '--paths', str(ROOT),
            '--collect-data', 'fake_useragent',
            '--hidden-import', 'socks', '--hidden-import', 'urllib3.contrib.socks',
        ]
        if importlib.util.find_spec('anthropic') is not None:  # optional: the AI mode of the extension generator
            cmd += ['--collect-submodules', 'anthropic']
        else:
            print('note: the anthropic package is not installed: the bundled core will offer only the template mode of the generator')
        for name in (*_lynxx_modules(), 'lynxx.extgen.sandbox_boot', *_extension_imports()):
            if name not in EXCLUDED_MODULES:
                cmd += ['--hidden-import', name]
        for name in EXCLUDED_MODULES:
            cmd += ['--exclude-module', name]
        for src, dest in data:
            cmd += ['--add-data', f'{src}{os.pathsep}{dest}']
        print(' '.join(cmd))
        subprocess.run(cmd, cwd=ROOT, check=True)
    bundle = out_dir / NAME
    exe = bundle / (f'{NAME}.exe' if sys.platform == 'win32' else NAME)
    if not exe.is_file():
        raise SystemExit(f'build failed: {exe} not found')
    print(f'\n{bundle}: {_dir_size(bundle) / 1048576:.1f} MiB')
    if smoke:
        _smoke_test(exe)
    return bundle


def main() -> None:
    ap = argparse.ArgumentParser(description='Build the frozen Lynxx core (PyInstaller one-dir) for the desktop app')
    ap.add_argument('--out', type=pathlib.Path, default=DEFAULT_OUT, help=f'output folder (default: {DEFAULT_OUT})')
    ap.add_argument('--tags', default='aliases',
                    help='tag lists to bundle from 2tags/: aliases (default: aliases file only), none, all, or abbreviations: rx,db')
    ap.add_argument('--no-smoke', action='store_true', help='skip starting the built core')
    args = ap.parse_args()
    build(args.out.resolve(), args.tags.strip().lower(), not args.no_smoke)


if __name__ == '__main__':
    main()

#
#
#########################################
