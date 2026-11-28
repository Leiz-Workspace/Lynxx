#!/usr/bin/env python3
# coding=UTF-8
"""
Builds the frozen Lynxx core shipped with the desktop app: a PyInstaller one-dir bundle 'lynxx-core' (entry lynxx/frozen_main.py,
command 'server') in app/build/core/lynxx-core. electron-builder copies it to <resources>/core (app/package.json
build.extraResources) and app/main.js runs it instead of python when the app is packaged.

usage: python tools/build_core.py [--out DIR] [--no-smoke]
The core uses the standard library only; the running interpreter needs PyInstaller:
  python -m pip install 'pyinstaller>=6.21'
"""

import argparse
import json
import os
import pathlib
import struct
import subprocess
import sys
import tempfile
import zipfile
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
NAME = 'lynxx-core'
ENTRY = ROOT / 'lynxx' / 'frozen_main.py'
DEFAULT_OUT = ROOT / 'app' / 'build' / 'core'

EXCLUDED_MODULES = ('tkinter', '_tkinter', 'PyInstaller', 'pip', 'setuptools', 'ruff', 'pydoc', 'lib2to3', 'lynxx.tests')
PACKAGES = ('lynxx', 'lynxx/vcs', 'lynxx/formats', 'lynxx/export', 'lynxx/library', 'lynxx/artifacts', 'lynxx/policy', 'lynxx/sources')


def _lynxx_modules() -> list[str]:
    """Every module of the core: the document converters are imported by name at run time (lynxx.formats.convert), so PyInstaller
    would not find them by itself"""
    names = []
    for pkg in PACKAGES:
        for p in sorted((ROOT / pkg).glob('*.py')):
            if p.stem not in ('__main__', 'frozen_main'):
                names.append(pkg.replace('/', '.') if p.stem == '__init__' else f'{pkg.replace("/", ".")}.{p.stem}')
    return names


def _dir_size(path: pathlib.Path) -> int:
    return sum(f.stat().st_size for f in path.rglob('*') if f.is_file() and not f.is_symlink())


def _smoke_test(exe: pathlib.Path) -> None:
    """The bundle must start the sidecar (ready notification) and convert a file of every converter family"""
    with tempfile.TemporaryDirectory() as data_dir:
        env = {**os.environ, 'LYNXX_DATA_DIR': data_dir}
        env.pop('PYTHONPATH', None)
        r = subprocess.run((str(exe), 'server'), input='{"jsonrpc":"2.0","method":"app.shutdown"}\n', env=env, capture_output=True,
                           text=True, encoding='utf-8', timeout=120)
        ready = next((json.loads(line) for line in r.stdout.splitlines() if line.startswith('{') and '"ready"' in line), None)
        if r.returncode != 0 or not ready:
            raise SystemExit(f'smoke test failed: server exit {r.returncode}\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}')
        print(f'smoke test: server ready, version {ready["params"].get("version")}, python {ready["params"].get("python")}')
        _smoke_test_books(exe, env, pathlib.Path(data_dir))


def _tiny_png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 1, 1, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(b'\x00\xff\x00\x00')) + chunk(b'IEND', b''))


def _smoke_test_books(exe: pathlib.Path, env: dict[str, str], data_dir: pathlib.Path) -> None:
    """The document converters are imported by name at run time (lynxx.formats): every family must be in the bundle"""
    src = data_dir / 'books'
    src.mkdir()
    chapter = 'Chương 1\n\nMột đoạn văn.\n\nChương 2\n\nĐoạn khác.\n'
    (src / 'a.txt').write_text(chapter, encoding='utf-8')
    (src / 'a.md').write_text('# Chương 1\n\nMột *đoạn* văn.\n', encoding='utf-8')
    (src / 'a.html').write_text('<html><head><title>T</title></head><body><h1>Chương 1</h1><p>Văn bản.</p></body></html>',
                                encoding='utf-8')
    (src / 'a.rtf').write_text(r'{\rtf1\ansi{\fonttbl{\f0 Arial;}}\f0 Chapter 1\par Text.\par}', encoding='ascii')
    (src / 'a.fb2').write_text('<?xml version="1.0" encoding="utf-8"?><FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">'
                               '<description><title-info><book-title>T</book-title></title-info></description><body><section>'
                               '<title><p>Chương 1</p></title><p>Văn bản.</p></section></body></FictionBook>', encoding='utf-8')
    (src / 'a.fodt').write_text('<?xml version="1.0" encoding="UTF-8"?><office:document '
                                'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                                'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:mimetype='
                                '"application/vnd.oasis.opendocument.text"><office:body><office:text><text:h text:outline-level="1">'
                                'Chương 1</text:h><text:p>Văn bản.</text:p></office:text></office:body></office:document>',
                                encoding='utf-8')
    with zipfile.ZipFile(src / 'a.cbz', 'w') as zf:
        zf.writestr('001.png', _tiny_png())
        zf.writestr('002.png', _tiny_png())
    with zipfile.ZipFile(src / 'a.epub', 'w') as zf:
        zf.writestr('mimetype', 'application/epub+zip', compress_type=zipfile.ZIP_STORED)
        zf.writestr('META-INF/container.xml', '<?xml version="1.0"?><container version="1.0" '
                    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="OEBPS/content.opf" '
                    'media-type="application/oebps-package+xml"/></rootfiles></container>')
        zf.writestr('OEBPS/content.opf', '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" '
                    'unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title>'
                    '<dc:identifier id="id">x</dc:identifier></metadata><manifest><item id="c1" href="c1.xhtml" '
                    'media-type="application/xhtml+xml"/></manifest><spine><itemref idref="c1"/></spine></package>')
        zf.writestr('OEBPS/c1.xhtml', '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>C</title></head>'
                    '<body><h1>Chương 1</h1><p>Văn bản.</p></body></html>')
    names = sorted(p.name for p in src.iterdir())
    lines = [json.dumps({'jsonrpc': '2.0', 'id': i, 'method': 'library.import', 'params': {'source': str(src / name)}})
             for i, name in enumerate(names, 1)]
    lines.append(json.dumps({'jsonrpc': '2.0', 'id': 0, 'method': 'formats.supported', 'params': {}}))
    lines.append('{"jsonrpc":"2.0","method":"app.shutdown"}')
    r = subprocess.run((str(exe), 'server'), input='\n'.join(lines) + '\n', env=env, capture_output=True, text=True, encoding='utf-8',
                       timeout=180)
    replies = {m['id']: m for m in (json.loads(line) for line in r.stdout.splitlines() if line.startswith('{')) if 'id' in m}
    bad = [f'{name}: {replies.get(i, {}).get("error") or replies.get(i)}' for i, name in enumerate(names, 1)
           if not (replies.get(i, {}).get('result') or {}).get('chapters')]
    supported = replies.get(0, {}).get('result')
    if bad or not supported:
        raise SystemExit('smoke test failed: book import\n' + '\n'.join(bad) + f'\nformats.supported: {supported}\n{r.stderr[-2000:]}')
    print(f'smoke test: book import ok ({", ".join(names)})')


def build(out_dir: pathlib.Path, smoke: bool) -> pathlib.Path:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit('PyInstaller is not installed: python -m pip install \'pyinstaller>=6.21\'')
    out_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='lynxx-core-build-') as tmp:
        stage = pathlib.Path(tmp)
        cmd = [
            sys.executable, '-m', 'PyInstaller', str(ENTRY),
            '--name', NAME, '--onedir', '--console', '--noconfirm', '--clean', '--noupx', '--log-level', 'WARN',
            '--distpath', str(out_dir), '--workpath', str(stage / 'work'), '--specpath', str(stage),
            '--paths', str(ROOT),
        ]
        for name in _lynxx_modules():
            if name not in EXCLUDED_MODULES:
                cmd += ['--hidden-import', name]
        for name in EXCLUDED_MODULES:
            cmd += ['--exclude-module', name]
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
    ap.add_argument('--no-smoke', action='store_true', help='skip starting the built core')
    args = ap.parse_args()
    build(args.out.resolve(), not args.no_smoke)


if __name__ == '__main__':
    main()

#
#
#########################################
