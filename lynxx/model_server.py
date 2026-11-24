# coding=UTF-8
"""
Lynxx: gender model server. Runs the image model of the gender filter (lynxx/gender_model.py, WD tagger v3) behind a small
HTTP API, so the desktop app and the CLI only send pictures and get predicted tags back: they need neither numpy / onnxruntime
nor the 380 MB model (the app bundle is built without them). The gender rules, the safety gate and the verdict cache stay on the
client: the server only tags pictures, it never sees the query, the site or the post.

  python -m lynxx.model_server [--host 127.0.0.1] [--port 8765] [--workers 2] [--device auto|cpu|gpu] [--preload]
                                [--idle-unload 900] [--no-download]
  needs numpy + onnxruntime + Pillow (pip install "lynxx[model]"); the model is downloaded on the first start (pinned revision,
  sha256 checked) into LYNXX_GENDER_MODEL_DIR or <data dir>/models/wd-vit-tagger-v3
  token: LYNXX_GENDER_MODEL_TOKEN (Authorization: Bearer). Required to listen on anything but this computer.
  The model is loaded on the first picture (or /v1/load, --preload) and its memory freed after --idle-unload seconds unused
  (or /v1/unload): a server started with the session costs little until the app turns the model on. It runs on a GPU when
  onnxruntime has a GPU provider (onnxruntime-gpu), else on the CPU; /v1/status reports the GPUs and their VRAM.
  The model lives in a child process (ModelProcess): unloading ends it, which is the only way to hand back everything CUDA
  holds (context, cuDNN / cuBLAS host buffers); a child that dies is started again on the next picture.

API v1 (JSON answers):
  GET  /v1/status   {"id", "repo", "revision", "license", "inputSize", "deviceSetting", "server", "workers", "loaded", "device",
                     "idleUnload", "media": {"video", "animated"},
                     "hardware": {"gpus": [{vendor, name, vramMiB, vramFreeMiB, shared}], "providers", "runtime", "ramMiB",
                     "ramFreeMiB", "cpus", "os"}}
  POST /v1/tag      body: the picture (jpeg / png / webp / gif, <= 12 MB) -> {"id", "tags": {tag: probability >= 0.05}, "ms"}
  POST /v1/frames   body: a video (mp4 / webm / mkv / mov / avi) or an animated picture (gif / apng / webp), <= 200 MB ->
                    {"id", "kind": "video" | "animated", "decoder", "length", "frames": [{"at", "tags"}], "ms"}: up to 8 frames
                    evenly spaced, the first and last 5 % left out (lynxx/media_frames.py, -gender_verify_media)
  POST /v1/load     no body: load the model now -> the status; 503 when it cannot be loaded (with the reason)
  POST /v1/unload   no body: free the model memory -> the status (the next picture loads it again)
  errors: 400 not a picture / an unreadable video, 401 token, 404, 405, 411 no Content-Length, 413 too large, 415 no decoder
  for this media here (/v1/status "media": {"video": "ffmpeg ..." | "pyav ..." | "", "animated": "pillow ..." | ""}),
  500 model error, 503 busy (retry later) or the model cannot be loaded
Pictures are decoded in memory and never stored or logged. A video goes to a private temporary folder (mode 0700) for ffmpeg
and is deleted as soon as its frames are read.
"""
#########################################
#
#

from __future__ import annotations

import argparse
import gc
import hmac
import itertools
import json
import os
import pathlib
import shutil
import signal
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .gender_model import (
    API_VERSION,
    DEVICES,
    ENV_MODEL_DEVICE,
    ENV_MODEL_TOKEN,
    IMAGE_MAX_BYTES,
    INPUT_SIZE,
    MODEL_ID,
    MODEL_LICENSE,
    MODEL_REPO,
    MODEL_REVISION,
    BadImage,
    GenderTagger,
    ModelUnavailable,
    _is_loopback,
    install_model,
    model_dir,
    model_status,
)
from .media_frames import MEDIA_MAX_BYTES, Frames, MediaError, decoders, extract_frames
from .model_hw import detect_hardware, hardware_check

__all__ = ('main', 'make_server')

DEFAULT_PORT = 8765
DEFAULT_WORKERS = 2
BUSY_WAIT = 60
'''seconds a request waits for a free worker before 503'''
SOCKET_TIMEOUT = 30
SERVER_NAME = 'lynxx-model/1'
DEFAULT_IDLE_UNLOAD = 900
'''seconds without a picture before the model memory is freed (0: never)'''
MODEL_START_TIMEOUT = 300
'''seconds the model process may take to load (disk, CUDA initialisation)'''
PREDICT_TIMEOUT = 120


class ModelError(RuntimeError):
    """the model failed on a picture in the model process (kind: the exception type there): 500, the client stops"""
    def __init__(self, kind: str, text: str) -> None:
        super().__init__(text)
        self.kind = kind


def _model_worker(conn, device: str, workers: int, factory: Callable[[], object] | None) -> None:
    """Child process: loads the model, then tags the pictures it is sent ((n, bytes) -> (n, kind, payload)) until None or
    until the parent is gone (end of the pipe)"""
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C reaches the whole process group: the parent ends this process
    try:
        tagger = factory() if factory is not None else GenderTagger(model_dir(), device)
    except ModelUnavailable as err:
        conn.send(('error', str(err)))
        return
    except Exception as err:
        conn.send(('error', f'the model failed to load ({type(err).__name__}: {str(err)[:200]})'))
        return
    conn.send(('ready', str(getattr(tagger, 'device', 'cpu'))))
    lock = threading.Lock()

    def run(n: int, image: bytes) -> None:
        try:
            out = ('ok', tagger.predict(image))
        except (BadImage, OSError) as err:
            out = ('bad', str(err)[:200])
        except ModelUnavailable as err:
            out = ('unavailable', str(err))
        except Exception as err:  # the runtime itself (GPU out of memory...)
            out = ('error', (type(err).__name__, str(err)[:300]))
        try:
            with lock:
                conn.send((n, *out))
        except (OSError, ValueError):
            pass
    with ThreadPoolExecutor(max(1, workers), thread_name_prefix='model') as pool:
        while True:
            try:
                msg = conn.recv()
            except (EOFError, OSError):
                break
            if msg is None:
                break
            pool.submit(run, *msg)


class ModelProcess:
    """The model in a child process, with GenderTagger's predict(). close() ends the process once the pictures being tagged
    are done: everything it holds (RAM, VRAM, the CUDA context) goes back to the system, which dropping an onnxruntime
    session in this process does not do (measured on CUDA: 675 MB of RAM and 224 MiB of VRAM stay)"""
    def __init__(self, device: str = 'auto', workers: int = DEFAULT_WORKERS, factory: Callable[[], object] | None = None) -> None:
        import multiprocessing
        ctx = multiprocessing.get_context('spawn')  # never fork a process with server threads
        self._send_lock = threading.Lock()
        self._cond = threading.Condition()
        self._waiting: dict[int, list] = {}
        self._pending = 0
        self._seq = itertools.count(1)
        self._dead = ''
        self._conn, child = ctx.Pipe()
        self._proc = ctx.Process(target=_model_worker, args=(child, device, workers, factory), name='lynxx-model', daemon=True)
        self._proc.start()
        child.close()
        try:
            msg = self._conn.recv() if self._conn.poll(MODEL_START_TIMEOUT) else ('error', 'the model process did not start in time')
        except (EOFError, OSError):
            self._proc.join(5)
            msg = ('error', f'the model process exited while loading (code {self._proc.exitcode})')
        if msg[0] != 'ready':
            self._stop()
            raise ModelUnavailable(msg[1])
        self.device = msg[1]
        self.pid = self._proc.pid
        threading.Thread(target=self._read, name='model-results', daemon=True).start()

    def _read(self) -> None:
        while True:
            try:
                n, kind, payload = self._conn.recv()
            except (EOFError, OSError, ValueError):  # the process ended, or the pipe was closed
                break
            with self._cond:
                if (slot := self._waiting.pop(n, None)) is not None:
                    slot[:] = [kind, payload]
                    self._cond.notify_all()
        self._proc.join(2)
        with self._cond:
            self._dead = self._dead or f'the model process exited (code {self._proc.exitcode})'
            for slot in self._waiting.values():
                slot[:] = ['unavailable', self._dead]
            self._waiting.clear()
            self._cond.notify_all()

    @property
    def alive(self) -> bool:
        return not self._dead and self._proc.is_alive()

    def predict(self, image: bytes) -> dict[str, float]:
        n, slot = next(self._seq), []
        with self._cond:
            if self._dead:
                raise ModelUnavailable(self._dead)
            self._waiting[n] = slot
            self._pending += 1
        try:
            try:
                with self._send_lock:
                    self._conn.send((n, image))
            except (OSError, ValueError):
                raise ModelUnavailable('the model process is gone') from None
            with self._cond:
                if not self._cond.wait_for(lambda: bool(slot), timeout=PREDICT_TIMEOUT):
                    raise ModelUnavailable(f'the model process did not answer in {PREDICT_TIMEOUT} s')
        finally:
            with self._cond:
                self._waiting.pop(n, None)
                self._pending -= 1
                self._cond.notify_all()
        kind, payload = slot
        if kind == 'ok':
            return payload
        if kind == 'bad':
            raise BadImage(payload)
        if kind == 'unavailable':
            raise ModelUnavailable(payload)
        raise ModelError(*payload)

    def close(self, wait: float = 60) -> None:
        with self._cond:
            self._cond.wait_for(lambda: self._pending == 0, timeout=wait)
            self._dead = self._dead or 'the model was unloaded'
        self._stop()

    def _stop(self) -> None:
        try:
            with self._send_lock:
                self._conn.send(None)
        except (OSError, ValueError):
            pass
        self._proc.join(10)
        for end in (self._proc.terminate, self._proc.kill):
            if self._proc.is_alive():
                end()
                self._proc.join(5)
        self._conn.close()


def _trim_memory() -> None:
    """glibc keeps freed memory in the process: hand it back to the system after the model is dropped"""
    if sys.platform.startswith('linux'):
        try:
            import ctypes
            ctypes.CDLL('libc.so.6').malloc_trim(0)
        except (OSError, AttributeError):
            pass


class ModelServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, addr: tuple[str, int], loader: Callable[[], object], token: str, workers: int, info: dict, *,
                 quiet: bool = False, idle_unload: int = 0, tagger=None, hardware: Callable[[], dict] = detect_hardware,
                 media: Callable[[], dict] = decoders, extract: Callable[[pathlib.Path], Frames] = extract_frames) -> None:
        super().__init__(addr, _Handler)
        self.loader = loader
        self.tagger = tagger
        self.device = str(getattr(tagger, 'device', 'cpu')) if tagger is not None else ''
        self.hardware = hardware
        self.media = media
        '''decoders of /v1/frames: {'video': 'ffmpeg ...' | 'pyav ...' | '', 'animated': 'pillow ...' | ''}'''
        self.extract = extract
        '''frames of a video / animated picture file (lynxx/media_frames.py extract_frames; tests: a stub)'''
        self.load_lock = threading.Lock()
        self.last_used = time.monotonic()
        self.idle_unload = max(0, int(idle_unload))
        self.token = token.encode()
        self.slots = threading.BoundedSemaphore(max(1, workers))
        self.info = {**info, 'server': SERVER_NAME, 'workers': max(1, workers)}
        self.quiet = quiet
        self._closing = threading.Event()
        if self.idle_unload:
            threading.Thread(target=self._idle_loop, name='idle-unload', daemon=True).start()

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f'http://{f"[{host}]" if ":" in host else host}:{port}'

    def log(self, text: str) -> None:
        print(f'{time.strftime("%H:%M:%S")} {text}', file=sys.stderr, flush=True)

    def model(self):
        """the model, loaded on first use (ModelUnavailable when it cannot be); a model process that died is started again"""
        with self.load_lock:
            if self.tagger is not None and getattr(self.tagger, 'alive', True) is False:
                self.log('the model process is gone: loading the model again')
                self.tagger, self.device = None, ''
            if self.tagger is None:
                t0 = time.monotonic()
                self.tagger = self.loader()
                self.device = str(getattr(self.tagger, 'device', 'cpu'))
                self.log(f'model loaded on {self.device} in {time.monotonic() - t0:.1f} s')
            self.last_used = time.monotonic()
            return self.tagger

    def unload(self, why: str = '') -> bool:
        """drop the model; pictures being tagged finish first. A model process ends (in the background: it waits for them)"""
        with self.load_lock:
            if self.tagger is None:
                return False
            tagger, self.tagger, self.device = self.tagger, None, ''
        if callable(close := getattr(tagger, 'close', None)):
            threading.Thread(target=close, name='model-close', daemon=True).start()
        else:
            del tagger
            gc.collect()
            _trim_memory()
        self.log(f'model unloaded{why}')
        return True

    def unload_idle(self, now: float | None = None) -> bool:
        if not self.idle_unload or self.tagger is None:
            return False
        if (time.monotonic() if now is None else now) - self.last_used < self.idle_unload:
            return False
        return self.unload(f' after {self.idle_unload} s unused')

    def _idle_loop(self) -> None:
        while not self._closing.wait(min(30, self.idle_unload)):
            self.unload_idle()

    def status(self) -> dict:
        try:
            hw = self.hardware()
        except Exception:  # hardware probing must never break the status
            hw = None
        try:
            media = self.media()
        except Exception:
            media = {'video': '', 'animated': ''}
        return {**self.info, 'loaded': self.tagger is not None, 'device': self.device, 'idleUnload': self.idle_unload,
                'hardware': hw, 'media': media}

    def server_close(self) -> None:
        self._closing.set()
        super().server_close()
        with self.load_lock:
            tagger, self.tagger, self.device = self.tagger, None, ''
        if callable(close := getattr(tagger, 'close', None)):  # the model process ends with the server
            close(wait=5)


class _Handler(BaseHTTPRequestHandler):
    server: ModelServer
    protocol_version = 'HTTP/1.1'  # keep-alive: a client sends many pictures
    disable_nagle_algorithm = True  # headers and body are two writes: Nagle + delayed ACK added ~40 ms to every picture
    server_version = SERVER_NAME
    sys_version = ''
    timeout = SOCKET_TIMEOUT

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body, separators=(',', ':')).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        if code == 401:
            self.send_header('WWW-Authenticate', 'Bearer')
        self.end_headers()
        self.wfile.write(data)

    def _error(self, code: int, message: str, close: bool = False) -> None:
        if close:  # the body was not read: the connection cannot be reused
            self.close_connection = True
        self._send(code, {'error': message})

    def _authorized(self) -> bool:
        if not self.server.token:
            return True
        auth = self.headers.get('Authorization', '')
        given = auth[7:].strip().encode() if auth[:7].lower() == 'bearer ' else b''
        return hmac.compare_digest(given, self.server.token)

    def do_GET(self) -> None:
        if not self._authorized():
            return self._error(401, 'token')
        if self.path.split('?', 1)[0] != f'/{API_VERSION}/status':
            return self._error(404, 'not found')
        self._send(200, self.server.status())

    def _control(self, action: str) -> None:
        """/v1/load, /v1/unload: no body"""
        if self.headers.get('Content-Length', '0').strip() not in ('', '0'):
            return self._error(400, 'no body expected', close=True)
        if action == 'unload':
            self.server.unload(' (asked by a client)')
            return self._send(200, self.server.status())
        try:
            self.server.model()
        except ModelUnavailable as err:
            return self._error(503, str(err))
        except Exception as err:
            self.server.log(f'model failed to load: {type(err).__name__}: {err}')
            return self._error(503, f'the model failed to load ({type(err).__name__})')
        self._send(200, self.server.status())

    def _body_length(self, limit: int, what: str) -> int:
        """Content-Length of a body to read (> 0), or -1 once an error was answered"""
        try:
            length = int(self.headers.get('Content-Length', ''))
        except ValueError:
            self._error(411, 'Content-Length is required', close=True)
            return -1
        if length <= 0:
            self._error(400, 'empty body')
            return -1
        if length > limit:
            self._error(413, f'{what} larger than {limit} bytes', close=True)
            return -1
        return length

    def _predict(self, image: bytes) -> dict[str, float]:
        """tags of one picture; a model process that died (killed, crashed) is started again once"""
        for attempt in (1, 2):
            tagger = self.server.model()
            try:
                return tagger.predict(image)
            except ModelUnavailable:
                if attempt == 2 or getattr(tagger, 'alive', True):
                    raise
        raise ModelUnavailable('the model process is gone')

    def _model_error(self, err: Exception) -> None:
        """the runtime itself (GPU out of memory...): every picture would fail, the client stops"""
        kind = getattr(err, 'kind', type(err).__name__)
        self.server.log(f'model error: {kind}: {str(err)[:300]}')
        self._error(500, f'model error ({kind})')

    def do_POST(self) -> None:
        path = self.path.split('?', 1)[0]
        if path not in (f'/{API_VERSION}/tag', f'/{API_VERSION}/frames', f'/{API_VERSION}/load', f'/{API_VERSION}/unload'):
            return self._error(404, 'not found', close=True)
        if not self._authorized():
            return self._error(401, 'token', close=True)
        if path == f'/{API_VERSION}/frames':
            return self._frames()
        if path != f'/{API_VERSION}/tag':
            return self._control(path.rsplit('/', 1)[1])
        if (length := self._body_length(IMAGE_MAX_BYTES, 'picture')) < 0:
            return None
        image = self.rfile.read(length)
        if len(image) != length:
            return self._error(400, 'truncated body', close=True)
        if not self.server.slots.acquire(timeout=BUSY_WAIT):
            return self._error(503, 'busy')
        t0 = time.monotonic()
        try:
            tags = self._predict(image)
        except ModelUnavailable as err:
            return self._error(503, str(err))
        except (BadImage, OSError):  # not a picture, or one Pillow cannot decode
            return self._error(400, 'not a picture')
        except Exception as err:
            return self._model_error(err)
        finally:
            self.server.last_used = time.monotonic()
            self.server.slots.release()
        self._send(200, {'id': self.server.info['id'], 'tags': {t: round(p, 4) for t, p in tags.items()},
                         'ms': int((time.monotonic() - t0) * 1000)})

    def _frames(self) -> None:
        """/v1/frames: the body goes to a private temporary folder (ffmpeg reads files), its frames are read and tagged, the
        folder is deleted before the answer. One worker slot covers reading the frames and tagging them"""
        if (length := self._body_length(MEDIA_MAX_BYTES, 'media')) < 0:
            return None
        folder = tempfile.mkdtemp(prefix='lynxx-frames-')  # mode 0700
        try:
            answer = self._frames_of(pathlib.Path(folder, 'media'), length)
        finally:
            shutil.rmtree(folder, ignore_errors=True)
        if isinstance(answer, dict):
            return self._send(200, answer)
        if answer[0] == 500:
            return self._model_error(answer[1])
        return self._error(answer[0], str(answer[1])[:200], close=answer[2])

    def _frames_of(self, media: pathlib.Path, length: int) -> dict | tuple[int, object, bool]:
        """the answer of /v1/frames, or (error code, message / exception, close the connection): sent once the media is deleted"""
        left = length
        try:
            with open(media, 'wb') as f:
                while left > 0 and (chunk := self.rfile.read(min(left, 1 << 20))):
                    f.write(chunk)
                    left -= len(chunk)
        except OSError as err:  # disk full, client gone (socket timeout)
            return 503, f'cannot store the media here ({err.strerror or type(err).__name__})', True
        if left:
            return 400, 'truncated body', True
        if not self.server.slots.acquire(timeout=BUSY_WAIT):
            return 503, 'busy', False
        t0 = time.monotonic()
        try:
            clip = self.server.extract(media)
            media.unlink(missing_ok=True)  # the frames are in memory now
            frames = []
            for at, image in clip.frames:
                try:
                    tags = self._predict(image)
                except (BadImage, OSError):  # a frame the model cannot decode: left out
                    continue
                frames.append({'at': at, 'tags': {t: round(p, 4) for t, p in tags.items()}})
        except MediaError as err:
            return 415 if err.unsupported else 400, str(err), False
        except ModelUnavailable as err:
            return 503, str(err), False
        except Exception as err:
            return 500, err, False
        finally:
            self.server.last_used = time.monotonic()
            self.server.slots.release()
        return {'id': self.server.info['id'], 'kind': clip.kind, 'decoder': clip.decoder, 'length': clip.length, 'frames': frames,
                'ms': int((time.monotonic() - t0) * 1000)}

    def do_PUT(self) -> None:
        self._error(405, 'method not allowed', close=True)

    do_DELETE = do_PATCH = do_PUT

    def log_message(self, fmt: str, *args) -> None:
        if not self.server.quiet:  # method, path and status only: never a picture or a header
            print(f'{time.strftime("%H:%M:%S")} {self.address_string()} {fmt % args}', file=sys.stderr, flush=True)


def make_server(host: str, port: int, tagger=None, *, loader: Callable[[], object] | None = None, token: str = '',
                workers: int = DEFAULT_WORKERS, quiet: bool = False, idle_unload: int = 0, device: str = 'auto',
                hardware: Callable[[], dict] = detect_hardware, media: Callable[[], dict] = decoders,
                extract: Callable[[pathlib.Path], Frames] = extract_frames) -> ModelServer:
    """The model is loaded by loader on first use, default: a ModelProcess on device (unloading ends it). Tests: a tagger
    (any object with predict(bytes) -> {tag: probability}) starts loaded and comes back after an unload; media / extract:
    the decoders and the frame reader of /v1/frames"""
    own = loader is None and tagger is None  # the model of this server: its --device is reported
    if loader is None:
        loader = (lambda: tagger) if tagger is not None else (lambda: ModelProcess(device, workers))
    info = {'id': getattr(tagger, 'model_id', MODEL_ID), 'repo': MODEL_REPO, 'revision': MODEL_REVISION, 'license': MODEL_LICENSE,
            'inputSize': INPUT_SIZE, 'deviceSetting': device if own else ''}
    return ModelServer((host, port), loader, token, workers, info, quiet=quiet, idle_unload=idle_unload, tagger=tagger,
                       hardware=hardware, media=media, extract=extract)


def _gpu_memory(g: dict) -> str:
    if isinstance(g.get('vramMiB'), int):
        return f'{g.get("vramFreeMiB")}/{g["vramMiB"]} MiB VRAM free'
    if g.get('shared'):
        return 'shared memory'
    # an NVIDIA card seen only through sysfs: no nvidia-smi here (a CPU container, or the driver tools are missing)
    return 'VRAM unknown (no nvidia-smi)' if g.get('vendor') == 'nvidia' else 'VRAM unknown'


def describe_hardware(hw: dict) -> str:
    """one line for the server log: GPUs, free VRAM, what onnxruntime can run on"""
    gpus = ', '.join(f'{g.get("name") or "GPU"} {_gpu_memory(g)}' for g in hw.get('gpus') or [] if isinstance(g, dict)) or 'no GPU'
    rt = hardware_check(hw)['runtime']
    return f'{gpus}; onnxruntime {hw.get("runtime") or "?"} {f"with {rt}" if rt else "CPU only"}'


def check_bind(host: str, token: str, allow_no_token: bool) -> str:
    """'' when the server may start; anyone who can reach a port on another address could use the model and see nothing
    else, but a token keeps strangers from using the machine's CPU"""
    if _is_loopback(host) or token or allow_no_token:
        return ''
    return (f'listening on {host} needs a token: set {ENV_MODEL_TOKEN} (same value in the app: Settings, gender model), '
            'or --allow-no-token on a trusted network')


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='python -m lynxx.model_server', description='Lynxx gender model server (HTTP API)')
    ap.add_argument('--host', default='127.0.0.1', help='address to listen on (default: 127.0.0.1, this computer only)')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT, help=f'port (default: {DEFAULT_PORT})')
    ap.add_argument('--workers', type=int, default=DEFAULT_WORKERS, help=f'pictures tagged at the same time (default: {DEFAULT_WORKERS})')
    ap.add_argument('--device', choices=DEVICES, default=os.environ.get(ENV_MODEL_DEVICE, '').strip().lower() or 'auto',
                    help='where the model runs: auto (a GPU when onnxruntime has a GPU provider, else the CPU), cpu, gpu '
                         f'(default: {ENV_MODEL_DEVICE} or auto)')
    ap.add_argument('--preload', action='store_true', help='load the model at start instead of on the first picture')
    ap.add_argument('--idle-unload', type=int, default=DEFAULT_IDLE_UNLOAD, metavar='SECONDS',
                    help=f'free the model memory after this many seconds without a picture (default: {DEFAULT_IDLE_UNLOAD}, 0: never)')
    ap.add_argument('--no-download', action='store_true', help='do not download a missing model, exit instead')
    ap.add_argument('--allow-no-token', action='store_true', help='listen on another address without a token')
    ap.add_argument('--quiet', action='store_true', help='no request log')
    args = ap.parse_args(argv)
    token = os.environ.get(ENV_MODEL_TOKEN, '')
    if token and not (8 <= len(token) <= 256 and token.isascii() and token.isprintable() and ' ' not in token):
        print(f'{ENV_MODEL_TOKEN}: 8 to 256 printable ASCII characters, no space', file=sys.stderr)
        return 2
    if msg := check_bind(args.host, token, args.allow_no_token):
        print(msg, file=sys.stderr)
        return 2
    st = model_status()
    if st['missingRuntime']:
        print(f'python package \'{st["missingRuntime"]}\' is missing: pip install "lynxx[model]"', file=sys.stderr)
        return 1
    if not st['installed']:
        if args.no_download:
            print(f'the model is not installed in {st["path"]}: python -m lynxx.gender_model install', file=sys.stderr)
            return 1
        print(f'downloading the model ({st["bytes"] / 1048576:.0f} MB, {MODEL_REPO}, {MODEL_LICENSE}) into {st["path"]}...', file=sys.stderr)
        last = [0.0]

        def progress(done: int, total: int) -> None:
            if time.monotonic() - last[0] > 3 or done == total:
                last[0] = time.monotonic()
                print(f'  {done / 1048576:.0f} / {total / 1048576:.0f} MB', file=sys.stderr, flush=True)
        try:
            install_model(progress)
        except Exception as err:
            print(f'download failed: {err}', file=sys.stderr)
            return 1
    try:
        srv = make_server(args.host, args.port, token=token, workers=args.workers, quiet=args.quiet,
                          idle_unload=max(0, args.idle_unload), device=args.device)
    except OSError as err:
        print(f'cannot listen on {args.host}:{args.port}: {err.strerror or err}', file=sys.stderr)
        return 1
    hw = detect_hardware()
    print(f'hardware: {describe_hardware(hw)}', file=sys.stderr, flush=True)
    media = decoders()
    print(f'media (/v1/frames): videos {media["video"] or "no (install ffmpeg on PATH, or PyAV)"}, '
          f'animated pictures {media["animated"] or "no (pillow)"}', file=sys.stderr, flush=True)
    if args.preload:
        try:
            srv.model()
        except ModelUnavailable as err:
            print(str(err), file=sys.stderr)
            srv.server_close()
            return 1
    elif args.device == 'gpu' and not hardware_check(hw)['runtime']:  # --preload says why when the GPU provider fails
        print('--device gpu: onnxruntime here has no GPU provider (pip install "onnxruntime-gpu[cuda,cudnn]")', file=sys.stderr)
        srv.server_close()
        return 1
    def stop(*_) -> None:  # docker stop / systemd: same as Ctrl-C
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    idle = f'memory freed after {srv.idle_unload} s unused' if srv.idle_unload else 'kept loaded'
    print(f'Lynxx model server: {srv.url} (model {MODEL_ID} {MODEL_REVISION[:8]}, '
          f'{f"loaded on {srv.device}" if srv.tagger is not None else "loaded on the first picture"}, {idle}, '
          f'{srv.info["workers"]} worker(s), token: {"yes" if token else "no"})', file=sys.stderr, flush=True)
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
    return 0


if __name__ == '__main__':
    sys.exit(main())

#
#
#########################################
