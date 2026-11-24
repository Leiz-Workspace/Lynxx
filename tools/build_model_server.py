#!/usr/bin/env python3
# coding=UTF-8
"""
Builds the standalone gender model server for desktop app users who have no Python or Lynxx sources: a PyInstaller one-dir
bundle 'lynxx-model-server' (entry tools/model_server/lynxx_model_server.py, same options as python -m lynxx.model_server).
The model (~380 MB) is not bundled: the server downloads it on its first start (pinned revision, sha256 checked) into the
Lynxx data folder, or uses LYNXX_GENDER_MODEL_DIR.

usage: python tools/build_model_server.py [--variant cpu|directml] [--out DIR] [--no-smoke] [--keep-env]
  cpu       onnxruntime (CPU), every OS (default)
  directml  onnxruntime-directml: any DirectX 12 GPU on Windows (not tested by the maintainers yet)
NVIDIA GPUs: run the server from a Python environment with onnxruntime-gpu[cuda,cudnn] or the GPU Docker image instead
(docs/operations.md §3c); a frozen CUDA bundle would carry ~2.4 GB of NVIDIA libraries under their own license terms.

A private build environment (uv, or venv + pip) gets numpy, the onnxruntime variant, Pillow, requests, yarl and PyInstaller,
so the result does not depend on the calling interpreter (a CUDA onnxruntime-gpu there would add its CUDA provider libraries,
useless without the NVIDIA pip packages).
Output: DIR/lynxx-model-server/ and DIR/lynxx-model-server-<os>-<arch>-<variant>_v<version>.tar.gz (.zip on Windows), named
like the app installers of a release (lynxx-linux-x86_64_v<version>.AppImage)
"""
#########################################
#
#

import argparse
import json
import os
import pathlib
import platform
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.error
import urllib.request
import zlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
NAME = 'lynxx-model-server'
ENTRY = ROOT / 'tools' / 'model_server' / 'lynxx_model_server.py'
DEFAULT_OUT = ROOT / 'dist-model-server'
VARIANTS = {
    'cpu': ['onnxruntime>=1.18'],
    'directml': ['onnxruntime-directml>=1.18'],
}
COMMON = ['numpy>=1.26', 'pillow>=10', 'requests>=2.31', 'yarl>=1.25', 'pyinstaller>=6.21']
# the server needs lynxx.model_server and what it imports; the downloaders, the extension generator and the GUI stay out
EXCLUDED_MODULES = (
    'tkinter', '_tkinter', 'tkinterdnd2', 'lxml', 'PyInstaller', 'pip', 'setuptools', 'pydoc', 'lib2to3', 'matplotlib',
    'IPython', 'pytest', 'anthropic', 'bs4', 'lynxx.tests', 'lynxx.release', 'lynxx.aigen', 'lynxx.extgen', 'lynxx.gui',
)
HIDDEN_IMPORTS = ('lynxx.model_server', 'lynxx.model_hw', 'lynxx.gender_model', 'lynxx.gender', 'lynxx.safety')
# run in the background at login: the helper of this OS goes into the bundle (users of the bundle have no source tree)
BACKGROUND_HELPERS = {
    'win32': 'tools/windows/install-model-server.ps1',
    'darwin': 'tools/launchd/install-model-server.sh',
    'linux': 'tools/systemd/lynxx-model-server-bundle.service',
}


def _version() -> str:
    with (ROOT / 'pyproject.toml').open('rb') as f:
        return tomllib.load(f)['project']['version']


def _platform_tag() -> str:
    os_name = {'win32': 'windows', 'darwin': 'macos'}.get(sys.platform, 'linux' if sys.platform.startswith('linux') else sys.platform)
    arch = {'amd64': 'x86_64', 'aarch64': 'arm64'}.get(platform.machine().lower(), platform.machine().lower())
    return f'{os_name}-{arch}'


def _env_python(env_dir: pathlib.Path) -> pathlib.Path:
    return env_dir / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')


def make_env(env_dir: pathlib.Path, variant: str) -> pathlib.Path:
    packages = [*COMMON, *VARIANTS[variant]]
    if uv := shutil.which('uv'):
        subprocess.run([uv, 'venv', '--quiet', '--python', sys.executable, str(env_dir)], check=True)
        py = _env_python(env_dir)
        subprocess.run([uv, 'pip', 'install', '--quiet', '--python', str(py), *packages], check=True)
    else:
        subprocess.run([sys.executable, '-m', 'venv', str(env_dir)], check=True)
        py = _env_python(env_dir)
        subprocess.run([str(py), '-m', 'pip', 'install', '--quiet', *packages], check=True)
    return py


def build(py: pathlib.Path, out: pathlib.Path, work: pathlib.Path) -> pathlib.Path:
    cmd = [str(py), '-m', 'PyInstaller', str(ENTRY), '--name', NAME, '--onedir', '--console', '--noconfirm', '--clean', '--noupx',
           '--log-level', 'WARN', '--distpath', str(out), '--workpath', str(work / 'work'), '--specpath', str(work),
           '--paths', str(ROOT), '--add-data', f'{ROOT / "lynxx" / "gender_rules"}{os.pathsep}lynxx/gender_rules']
    if (ROOT / 'images' / 'lynxx.ico').is_file():
        cmd += ['--icon', str(ROOT / 'images' / 'lynxx.ico')]
    for name in HIDDEN_IMPORTS:
        cmd += ['--hidden-import', name]
    for name in EXCLUDED_MODULES:
        cmd += ['--exclude-module', name]
    print(' '.join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)
    bundle = out / NAME
    for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md'):
        shutil.copy2(ROOT / name, bundle / name)
    helper = BACKGROUND_HELPERS.get('linux' if sys.platform.startswith('linux') else sys.platform)
    if helper:
        shutil.copy2(ROOT / helper, bundle / pathlib.Path(helper).name)
    return bundle


def _png(w: int = 64, h: int = 64) -> bytes:
    """a white picture, no image library needed"""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    raw = b''.join(b'\0' + b'\xff\xff\xff' * w for _ in range(h))
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw))
            + chunk(b'IEND', b''))


def _call(url: str, method: str = 'GET', body: bytes | None = None, timeout: float = 120) -> dict:
    req = urllib.request.Request(url, data=body, method=method, headers={'Content-Type': 'application/octet-stream'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _installed_model() -> str:
    if env := os.environ.get('LYNXX_GENDER_MODEL_DIR'):
        return env
    try:
        sys.path.insert(0, str(ROOT))
        from lynxx.extensions import data_dir
        folder = data_dir() / 'models' / 'wd-vit-tagger-v3'
    except Exception:
        return ''
    return str(folder) if (folder / 'model.onnx').is_file() and (folder / 'selected_tags.csv').is_file() else ''


def smoke(bundle: pathlib.Path) -> None:
    exe = bundle / (f'{NAME}.exe' if sys.platform == 'win32' else NAME)
    r = subprocess.run([str(exe), '--help'], capture_output=True, text=True, timeout=120)
    if r.returncode != 0 or '--device' not in r.stdout:
        raise SystemExit(f'smoke test failed: --help\n{r.stdout[-2000:]}\n{r.stderr[-2000:]}')
    with tempfile.TemporaryDirectory() as data:
        env = {**os.environ, 'LYNXX_DATA_DIR': data}
        env.pop('LYNXX_GENDER_MODEL_TOKEN', None)
        model = _installed_model()
        if not model:  # numpy / onnxruntime / Pillow import, then the missing model is reported
            r = subprocess.run([str(exe), '--no-download'], capture_output=True, text=True, timeout=180, env=env)
            if r.returncode != 1 or 'not installed' not in r.stderr:
                raise SystemExit(f'smoke test failed: no model\n{r.stderr[-2000:]}')
            print('smoke test: runtime ok (no model on this machine: tagging not tried)')
            return
        env['LYNXX_GENDER_MODEL_DIR'] = model
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        url = f'http://127.0.0.1:{port}/v1'
        proc = subprocess.Popen([str(exe), '--port', str(port), '--no-download', '--idle-unload', '0', '--quiet'], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for _ in range(240):
                try:
                    st = _call(f'{url}/status', timeout=5)
                    break
                except (urllib.error.URLError, ConnectionError, TimeoutError):
                    if proc.poll() is not None:
                        raise SystemExit(f'smoke test failed: the server exited\n{proc.stdout.read()[-3000:]}') from None
                    time.sleep(0.5)
            else:
                raise SystemExit('smoke test failed: the server did not answer')
            t0 = time.monotonic()
            tags = _call(f'{url}/tag', 'POST', _png())
            loaded = _call(f'{url}/status')
            if not isinstance(tags.get('tags'), dict) or not loaded.get('loaded'):
                raise SystemExit(f'smoke test failed: tag {tags} / status {loaded}')
            unloaded = _call(f'{url}/unload', 'POST', b'')
            if unloaded.get('loaded'):
                raise SystemExit(f'smoke test failed: unload {unloaded}')
            print(f'smoke test: model {st["id"]} on {loaded["device"]}, first picture {time.monotonic() - t0:.1f} s '
                  f'(model process start included), unload ok')
        finally:
            proc.terminate()
            try:
                proc.wait(30)
            except subprocess.TimeoutExpired:
                proc.kill()


def _size(path: pathlib.Path) -> int:
    return sum(p.stat().st_size for p in path.rglob('*') if p.is_file())


def main() -> int:
    ap = argparse.ArgumentParser(description='Build the standalone Lynxx gender model server (PyInstaller one-dir)')
    ap.add_argument('--variant', choices=sorted(VARIANTS), default='cpu', help='onnxruntime build to bundle (default: cpu)')
    ap.add_argument('--out', type=pathlib.Path, default=DEFAULT_OUT, help=f'output folder (default: {DEFAULT_OUT})')
    ap.add_argument('--no-smoke', action='store_true', help='skip starting the built server')
    ap.add_argument('--keep-env', action='store_true', help='keep the build environment (printed path)')
    args = ap.parse_args()
    if args.variant == 'directml' and sys.platform != 'win32':
        ap.error('--variant directml builds on Windows only')
    out = args.out.resolve()
    work = pathlib.Path(tempfile.mkdtemp(prefix='lynxx-model-server-build-'))
    try:
        py = make_env(work / 'env', args.variant)
        if (out / NAME).exists():
            shutil.rmtree(out / NAME)
        bundle = build(py, out, work)
        print(f'{bundle}: {_size(bundle) / 1048576:.1f} MiB')
        if not args.no_smoke:
            smoke(bundle)
        base = out / f'{NAME}-{_platform_tag()}-{args.variant}_v{_version()}'
        archive = shutil.make_archive(str(base), 'zip' if sys.platform == 'win32' else 'gztar', root_dir=out, base_dir=NAME)
        print(f'{archive}: {pathlib.Path(archive).stat().st_size / 1048576:.1f} MiB')
    finally:
        if args.keep_env:
            print(f'build environment kept: {work}')
        else:
            shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())

#
#
#########################################
