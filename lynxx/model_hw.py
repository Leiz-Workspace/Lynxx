# coding=UTF-8
"""
Lynxx: the hardware the gender model runs on (lynxx/model_server.py reports it, the app checks it before turning the model on).
  detect_hardware(): GPUs with their VRAM (NVIDIA: nvidia-smi; AMD / Intel on Linux: sysfs), the ONNX runtime providers of this
  python, memory and CPUs. No numpy / onnxruntime import at module level: the desktop core imports hardware_check only.
  hardware_check(hw, device): can the model use a GPU, or would it run on the CPU ('level' 'ok' / 'warn', 'code' for the UI)
"""
#########################################
#
#

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import threading
import time

__all__ = ('GPU_PROVIDERS', 'VRAM_MIN_MIB', 'detect_hardware', 'hardware_check')

VRAM_MIN_MIB = 1536
'''free VRAM the model needs on a GPU: measured 1258 MiB on CUDA (onnxruntime-gpu 1.32, RTX 4070 Ti SUPER: CUDA context,
~380 MB of weights, activations at 448 px, cuDNN / cuBLAS workspace), plus room for a second picture at once'''
GPU_PROVIDERS = (  # onnxruntime execution provider: (device name, GPU vendors it runs on, '' = any)
    ('CUDAExecutionProvider', 'cuda', ('nvidia',)),
    ('ROCMExecutionProvider', 'rocm', ('amd',)),
    ('MIGraphXExecutionProvider', 'migraphx', ('amd',)),
    ('DmlExecutionProvider', 'directml', ('nvidia', 'amd', 'intel', '')),
)
PCI_VENDORS = {'0x10de': 'nvidia', '0x1002': 'amd', '0x8086': 'intel'}
VENDOR_NAMES = {'nvidia': 'NVIDIA', 'amd': 'AMD', 'intel': 'Intel'}
CACHE_SECONDS = 10
NAME_MAX = 80

_cache: tuple[float, dict] | None = None
_cache_lock = threading.Lock()


def _mib(n: int | float | None) -> int | None:
    return None if n is None else int(n // 1048576)


def nvidia_gpus(run=subprocess.run) -> list[dict]:
    """NVIDIA GPUs through nvidia-smi (Linux and Windows drivers ship it): [] without it"""
    exe = shutil.which('nvidia-smi')
    if not exe:
        return []
    try:
        r = run([exe, '--query-gpu=name,memory.total,memory.free', '--format=csv,noheader,nounits'],
                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    out = []
    for line in (r.stdout or '').splitlines() if r.returncode == 0 else ():
        parts = [p.strip() for p in line.rsplit(',', 2)]
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            out.append({'vendor': 'nvidia', 'name': parts[0][:NAME_MAX], 'vramMiB': int(parts[1]), 'vramFreeMiB': int(parts[2]),
                        'shared': False})
    return out


def drm_gpus(root: str | os.PathLike = '/sys/class/drm', skip: tuple[str, ...] = ('nvidia',)) -> list[dict]:
    """Linux: AMD GPUs with their VRAM (amdgpu sysfs), Intel GPUs (memory shared with the system). NVIDIA: see nvidia_gpus"""
    out = []
    base = pathlib.Path(root)
    try:
        cards = sorted(p for p in base.iterdir() if p.name.startswith('card') and p.name[4:].isdigit())
    except OSError:
        return []
    for card in cards:
        dev = card / 'device'
        try:
            vendor = PCI_VENDORS.get((dev / 'vendor').read_text().strip(), '')
        except OSError:
            continue
        if not vendor or vendor in skip:
            continue
        gpu: dict = {'vendor': vendor, 'name': '', 'vramMiB': None, 'vramFreeMiB': None, 'shared': vendor == 'intel'}
        try:
            total = int((dev / 'mem_info_vram_total').read_text())
            used = int((dev / 'mem_info_vram_used').read_text())
            gpu['vramMiB'], gpu['vramFreeMiB'] = _mib(total), _mib(max(0, total - used))
        except (OSError, ValueError):
            pass
        try:
            gpu['name'] = (dev / 'product_name').read_text().strip()[:NAME_MAX]
        except OSError:
            pass
        if not gpu['name']:
            try:
                gpu['name'] = f'{VENDOR_NAMES.get(vendor, vendor)} GPU {(dev / "device").read_text().strip()}'
            except OSError:
                gpu['name'] = f'{VENDOR_NAMES.get(vendor, vendor)} GPU'
        out.append(gpu)
    return out


def _providers() -> tuple[list[str], str]:
    try:
        import onnxruntime as ort
    except ImportError:
        return [], ''
    return [str(p) for p in ort.get_available_providers()], str(getattr(ort, '__version__', ''))


def _memory() -> dict:
    out: dict = {'ramMiB': None, 'ramFreeMiB': None}
    try:
        info = dict(line.split(':', 1) for line in pathlib.Path('/proc/meminfo').read_text().splitlines() if ':' in line)
        out['ramMiB'] = int(info['MemTotal'].split()[0]) // 1024
        out['ramFreeMiB'] = int(info['MemAvailable'].split()[0]) // 1024
    except (OSError, KeyError, ValueError):
        try:
            out['ramMiB'] = _mib(os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES'))
        except (AttributeError, ValueError, OSError):
            pass
    return out


def detect_hardware(fresh: bool = False) -> dict:
    """{gpus: [{vendor, name, vramMiB, vramFreeMiB, shared}], providers, runtime, ramMiB, ramFreeMiB, cpus, os}.
    Cached a few seconds: the status of a model server is asked often (health checks)"""
    global _cache
    with _cache_lock:
        if not fresh and _cache and time.monotonic() - _cache[0] < CACHE_SECONDS:
            return _cache[1]
        gpus = nvidia_gpus()
        if sys.platform.startswith('linux'):
            gpus += drm_gpus(skip=('nvidia',) if gpus else ())
        elif sys.platform == 'darwin' and os.uname().machine == 'arm64':
            gpus.append({'vendor': 'apple', 'name': 'Apple Silicon', 'vramMiB': None, 'vramFreeMiB': None, 'shared': True})
        providers, runtime = _providers()
        hw = {'gpus': gpus, 'providers': providers, 'runtime': runtime, **_memory(), 'cpus': os.cpu_count() or 0,
              'os': sys.platform}
        _cache = (time.monotonic(), hw)
        return hw


def gpu_runtime(providers: list[str]) -> tuple[str, str, tuple[str, ...]]:
    """(provider, device, vendors) of the first GPU provider onnxruntime has, ('', '', ()) for a CPU only runtime"""
    for prov, device, vendors in GPU_PROVIDERS:
        if prov in providers:
            return prov, device, vendors
    return '', '', ()


def hardware_check(hw: dict | None, device: str = '', setting: str = '') -> dict:
    """Can the model run on a GPU? device: what a loaded model runs on ('cuda', 'cpu'...; '' = not loaded yet), setting: the
    server's --device (auto / cpu / gpu; '' unknown).
    code: 'gpu' (ok), 'no_gpu' (no GPU with its own VRAM: the CPU runs the model), 'gpu_no_runtime' (a GPU, but onnxruntime
    is a CPU build), 'cpu_forced' (the server is told to use the CPU: --device cpu), 'vram_low' (not enough free VRAM),
    'gpu_fallback' (GPU runtime present, the model still fell back to the CPU: CUDA / cuDNN libraries missing), 'unknown' (an
    older server that does not report its hardware)"""
    if not isinstance(hw, dict) or not isinstance(hw.get('gpus'), list):
        return {'level': 'warn', 'code': 'unknown', 'gpu': None, 'runtime': '', 'device': device}
    gpus = [g for g in hw['gpus'] if isinstance(g, dict)]
    dedicated = [g for g in gpus if not g.get('shared') and isinstance(g.get('vramMiB'), int) and g['vramMiB'] >= VRAM_MIN_MIB]
    _, rt_device, vendors = gpu_runtime([str(p) for p in hw.get('providers') or []])
    fit = [g for g in dedicated if g.get('vendor') in vendors or '' in vendors] if rt_device else []
    pool = fit or dedicated
    best = max(pool, key=lambda g: (g.get('vramFreeMiB') or 0, g['vramMiB'])) if pool else None
    out = {'level': 'warn', 'gpu': best, 'runtime': rt_device, 'device': device, 'vramMinMiB': VRAM_MIN_MIB,
           'ramFreeMiB': hw.get('ramFreeMiB')}
    if device and device != 'cpu':
        return {**out, 'level': 'ok', 'code': 'gpu'}
    if not dedicated:
        return {**out, 'code': 'no_gpu'}
    if not fit:
        return {**out, 'code': 'gpu_no_runtime'}
    if setting == 'cpu':
        return {**out, 'code': 'cpu_forced'}
    if device == 'cpu':
        return {**out, 'code': 'gpu_fallback'}
    free = best.get('vramFreeMiB') if best else None
    if isinstance(free, int) and free < VRAM_MIN_MIB:
        return {**out, 'code': 'vram_low'}
    return {**out, 'level': 'ok', 'code': 'gpu'}

#
#
#########################################
