# coding=UTF-8
"""
Lynxx: gender tier 1 for posts without usable tags (docs/plans/gender-filter-model.md §3, option A0).
A pretrained anime image tagger (WD tagger v3, ViT, SmilingWolf, Apache-2.0) runs on this computer: its predicted danbooru
tags go through the same tag rules as tier 0 (lynxx/gender.py, 'db' ruleset), so a missing '1girl' / '1boy' / 'no_humans'
is recovered from the picture. Its minor coded tags (loli, shota, child...) are an extra safety gate (§4.2).
Two backends:
- a model server (lynxx/model_server.py, HTTP API): the app and the CLI only send the picture and get the predicted tags back,
  so they need neither numpy / onnxruntime nor the model (the desktop app bundle is built without them). Address:
  -gender_model_url / LYNXX_GENDER_MODEL_URL, optional token: -gender_model_token / LYNXX_GENDER_MODEL_TOKEN;
- in process (no address given): needs numpy + onnxruntime + Pillow (pip install "lynxx[model]") and the model files.
The model runs on a GPU when onnxruntime has a GPU provider (onnxruntime-gpu: CUDA; ROCm, DirectML) and the CPU otherwise
(LYNXX_GENDER_MODEL_DEVICE: auto, cpu, gpu). lynxx/model_hw.py tells which, the app warns before turning the model on.
Images are fetched by the downloader like thumbnails and never stored; the model server decodes them in memory. Only the
verdict (label, confidence, evidence) is cached per post. The model is a download of ~380 MB (pinned revision, sha256 checked).
-gender_verify_media (plan §3.5, P4): a downloaded video / animated picture is sent to the model server (POST /v1/frames), which
tags up to 8 of its frames (lynxx/media_frames.py); classify_frames turns them into one verdict.

  python -m lynxx.gender_model status | install | remove | classify FILE...
"""
#########################################
#
#

from __future__ import annotations

import csv
import dataclasses
import hashlib
import io
import itertools
import json
import os
import pathlib
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from .gender import GenderConf, GenderLabel, GenderResult, infer_from_tags, rules_for_module
from .model_hw import GPU_PROVIDERS, hardware_check
from .safety import blocked_tag

__all__ = ('MODEL_ID', 'BadImage', 'GenderTagger', 'MediaUnreadable', 'ModelUnavailable', 'classify_frames', 'combine', 'install_model',
           'media_support', 'model_status', 'remove_model', 'verdict_for_media')

MODEL_ID = 'wd-vit-tagger-v3'
MODEL_REPO = 'SmilingWolf/wd-vit-tagger-v3'
MODEL_REVISION = '7f6b584d0bd3f55c4531f14ba3d4761b2bccdc0f'
MODEL_LICENSE = 'Apache-2.0'
MODEL_FILES = {  # name: (size, sha256) of the pinned revision
    'model.onnx': (378536310, '35f23693620b668f4d53fd3c62bf65e40af739bc52c7eb0fbc49258b58d065b6'),
    'selected_tags.csv': (308468, '298633d94d0031d2081c0893f29c82eab7f0df00b08483ba8f29d1e979441217'),
}
MODEL_URL = 'https://huggingface.co/{repo}/resolve/{rev}/{name}'
INPUT_SIZE = 448
GENERAL_THRESHOLD = 0.35
'''tags kept as model evidence (the threshold SmilingWolf suggests for general tags)'''
SAFETY_THRESHOLD = 0.15
'''minor coded tag at this probability or above: the post is blocked (low on purpose, plan §4.2)'''
REAL_THRESHOLD = 0.5
'''photo_(medium) / realistic: the model is meant for drawings and 3D renders, a photo stays 'unknown' (plan §4.3)'''
CLIP_T_HI = GENERAL_THRESHOLD
'''a tag is seen on a frame of a clip at this probability or above (as on a picture)'''
CLIP_T_LO = 0.2
'''a tag is absent from a clip when every frame has it at this probability or below; in between it is undecided (plan §3.5)'''
CLIP_UNDECIDED_MAX = 8
'''undecided rule tags of a clip are tried in every combination; more of them: the clip is unknown'''
GENDER_TAGS_PREFIX_MAX = 64
ENV_MODEL_DIR = 'LYNXX_GENDER_MODEL_DIR'
'''tests / developers / model server: a folder holding model.onnx + selected_tags.csv'''
ENV_MODEL_URL = 'LYNXX_GENDER_MODEL_URL'
'''address of a model server (python -m lynxx.model_server): the model runs there'''
ENV_MODEL_TOKEN = 'LYNXX_GENDER_MODEL_TOKEN'
'''token of the model server (client and server side)'''
ENV_MODEL_DEVICE = 'LYNXX_GENDER_MODEL_DEVICE'
'''where the model runs: auto (a GPU when onnxruntime has a GPU provider, else the CPU), cpu, gpu (fails without one)'''
DEVICES = ('auto', 'cpu', 'gpu')
API_DEFAULT_URL = 'http://127.0.0.1:8765'
API_VERSION = 'v1'
API_TIMEOUT = 60
FRAMES_TIMEOUT = 300
'''seconds to wait for the answer of /v1/frames (frames read by ffmpeg, then tagged one by one)'''
API_LAN_SUFFIXES = ('.local', '.lan', '.home.arpa', '.internal')


class ModelUnavailable(Exception):
    pass


class BadImage(ValueError):
    """this picture only: not an image, or one Pillow cannot decode (the model itself is fine)"""


class MediaUnreadable(ValueError):
    """this video / animated picture only (-gender_verify_media): too large, unreadable, or no decoder for it on the model
    server. The file stays where it is, not checked"""


def model_dir() -> pathlib.Path:
    if env := os.environ.get(ENV_MODEL_DIR):
        return pathlib.Path(env)
    from .extensions import data_dir
    return data_dir() / 'models' / MODEL_ID


def runtime_available() -> str:
    """'' when numpy + onnxruntime + Pillow can be imported, else the missing package"""
    for mod in ('numpy', 'onnxruntime', 'PIL'):
        try:
            __import__(mod)
        except ImportError:
            return {'PIL': 'pillow'}.get(mod, mod)
    return ''


def model_status() -> dict:
    d = model_dir()
    files = {name: (d / name).is_file() and (d / name).stat().st_size == size for name, (size, _) in MODEL_FILES.items()}
    return {'id': MODEL_ID, 'repo': MODEL_REPO, 'revision': MODEL_REVISION, 'license': MODEL_LICENSE, 'path': str(d),
            'installed': all(files.values()), 'bytes': sum(s for s, _ in MODEL_FILES.values()), 'missingRuntime': runtime_available(),
            'source': f'https://huggingface.co/{MODEL_REPO}'}


def install_model(progress: Callable[[int, int], None] | None = None, cancel: threading.Event | None = None) -> dict:
    """Downloads the pinned model files (size + sha256 checked, written to .part then renamed). Already complete files are kept"""
    import requests

    from .vcs.version import APP_VERSION
    d = model_dir()
    d.mkdir(parents=True, exist_ok=True)
    total = sum(s for s, _ in MODEL_FILES.values())
    done = 0
    for name, (size, sha) in MODEL_FILES.items():
        dest = d / name
        if dest.is_file() and dest.stat().st_size == size and _sha256(dest) == sha:
            done += size
            continue
        part = dest.with_name(f'{name}.part')
        h = hashlib.sha256()
        got = 0
        url = MODEL_URL.format(repo=MODEL_REPO, rev=MODEL_REVISION, name=name)
        with requests.get(url, stream=True, timeout=60, headers={'User-Agent': f'Lynxx/{APP_VERSION}'}) as r:
            r.raise_for_status()
            with part.open('wb') as f:
                for chunk in r.iter_content(1 << 20):
                    if cancel is not None and cancel.is_set():
                        f.close()
                        part.unlink(missing_ok=True)
                        raise ModelUnavailable('cancelled')
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if progress:
                        progress(done + got, total)
        if got != size or h.hexdigest() != sha:
            part.unlink(missing_ok=True)
            raise ModelUnavailable(f'{name}: size / checksum mismatch (got {got} bytes)')
        part.replace(dest)
        done += size
    (d / 'manifest.json').write_text(json.dumps({'id': MODEL_ID, 'repo': MODEL_REPO, 'revision': MODEL_REVISION, 'license': MODEL_LICENSE,
                                                 'installedAt': int(time.time())}, indent=1), encoding='utf-8')
    GenderTagger.reset()
    return model_status()


def remove_model() -> dict:
    GenderTagger.reset()
    d = model_dir()
    for name in (*MODEL_FILES, 'manifest.json', *(f'{n}.part' for n in MODEL_FILES)):
        (d / name).unlink(missing_ok=True)
    return model_status()


def _sha256(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


@dataclass(slots=True)
class ModelVerdict:
    result: GenderResult
    safety_tag: str = ''
    real: bool = False


class GenderTagger:
    """One onnxruntime session per process, thread-safe (onnxruntime runs concurrent calls)"""
    model_id = MODEL_ID
    _instance: GenderTagger | None = None
    _lock = threading.Lock()

    def __init__(self, folder: pathlib.Path, device: str = 'auto') -> None:
        if device not in DEVICES:
            raise ModelUnavailable(f'device must be one of {", ".join(DEVICES)}: {device!r}')
        if missing := runtime_available():
            raise ModelUnavailable(f'python package \'{missing}\' is missing: pip install "lynxx[model]"')
        if not all((folder / n).is_file() for n in MODEL_FILES):
            raise ModelUnavailable('the gender model is not installed here: python -m lynxx.gender_model install (~380 MB), '
                                   'or use a model server (-gender_model_url, python -m lynxx.model_server)')
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 2) // 2))
        opts.log_severity_level = 3  # errors only: CUDA adds harmless 'Memcpy nodes' warnings on every load
        names = dict((prov, dev) for prov, dev, _ in GPU_PROVIDERS)
        gpu = [] if device == 'cpu' else [p for p in ort.get_available_providers() if p in names][:1]
        if device == 'gpu' and not gpu:
            raise ModelUnavailable('no GPU runtime: onnxruntime here is a CPU build (pip install "onnxruntime-gpu[cuda,cudnn]")')
        if gpu == ['CUDAExecutionProvider'] and hasattr(ort, 'preload_dlls'):
            try:  # onnxruntime-gpu[cuda,cudnn]: CUDA and cuDNN come from the nvidia-* pip packages, not the system
                ort.preload_dlls()
            except Exception:
                pass
        self.session = ort.InferenceSession(str(folder / 'model.onnx'), sess_options=opts, providers=[*gpu, 'CPUExecutionProvider'])
        self.device = names.get(self.session.get_providers()[0], 'cpu')  # a GPU provider that failed to load leaves the CPU
        if device == 'gpu' and self.device == 'cpu':
            raise ModelUnavailable(f'{gpu[0]} did not load (CUDA / cuDNN libraries, driver): the model would run on the CPU')
        self.input = self.session.get_inputs()[0].name
        with (folder / 'selected_tags.csv').open(encoding='utf-8', newline='') as f:
            rows = list(csv.DictReader(f))
        self.names = [r['name'] for r in rows]
        self.general = [i for i, r in enumerate(rows) if r['category'] == '0']

    @classmethod
    def get(cls, device: str = '') -> GenderTagger:
        """the model of this process; device: auto / cpu / gpu ('' = LYNXX_GENDER_MODEL_DEVICE, else auto)"""
        with cls._lock:
            if cls._instance is None:
                device = device or os.environ.get(ENV_MODEL_DEVICE, '').strip().lower() or 'auto'
                cls._instance = GenderTagger(model_dir(), device)
            return cls._instance

    @classmethod
    def reset(cls) -> None:
        with cls._lock:
            cls._instance = None

    @staticmethod
    def preprocess(image: bytes):
        """WD tagger input: RGB on white, padded to a square, 448 px, BGR float32 0-255, NHWC"""
        import numpy as np
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = 40_000_000  # decompression bombs: an error above twice this size
        with Image.open(io.BytesIO(image)) as im:
            im.draft('RGB', (INPUT_SIZE * 2, INPUT_SIZE * 2))  # jpeg: decode at a reduced size, faster
            im = im.convert('RGBA')
            canvas = Image.new('RGBA', im.size, (255, 255, 255, 255))
            canvas.alpha_composite(im)
            rgb = canvas.convert('RGB')
        side = max(rgb.size)
        square = Image.new('RGB', (side, side), (255, 255, 255))
        square.paste(rgb, ((side - rgb.width) // 2, (side - rgb.height) // 2))
        square = square.resize((INPUT_SIZE, INPUT_SIZE), Image.BICUBIC)
        arr = np.asarray(square, dtype=np.float32)[:, :, ::-1]
        return np.ascontiguousarray(arr)[None, ...]

    def predict(self, image: bytes) -> dict[str, float]:
        """general tags with their probability (>= 0.05). BadImage: this picture only; other errors come from the model"""
        try:
            x = self.preprocess(image)
        except Exception as err:  # not a picture, truncated, decompression bomb
            raise BadImage(f'{type(err).__name__}: {err}'[:200]) from None
        out = self.session.run(None, {self.input: x})[0][0]
        return {self.names[i]: float(out[i]) for i in self.general if out[i] >= 0.05}

    def predict_frames(self, path: pathlib.Path) -> list[dict[str, float]]:
        """predict() on up to 8 frames of a video / animated picture (lynxx/media_frames.py: ffmpeg or PyAV, Pillow)"""
        from .media_frames import MEDIA_MAX_BYTES, MediaError, extract_frames
        if path.stat().st_size > MEDIA_MAX_BYTES:
            raise MediaUnreadable(f'larger than {MEDIA_MAX_BYTES // 1048576} MB')
        try:
            clip = extract_frames(path)
        except MediaError as err:
            raise MediaUnreadable(str(err)) from None
        out = []
        for _, image in clip.frames:
            try:
                out.append(self.predict(image))
            except BadImage:
                continue
        return out


def classify(probs: dict[str, float], *, video: bool = False) -> ModelVerdict:
    """Predicted tags -> label through the tier 0 rules ('db': the tagger vocabulary is danbooru's).
    Confidence: the weakest probability among the tags the label rests on (>= 0.85 high, >= 0.6 medium), a video thumbnail
    is one frame of the video: at most medium (plan §3.5)"""
    safety = next((t for t, p in sorted(probs.items(), key=lambda x: -x[1]) if p >= SAFETY_THRESHOLD and blocked_tag([t])), '')
    real = _is_photo(probs)
    tags = [t for t, p in probs.items() if p >= GENERAL_THRESHOLD]
    res = infer_from_tags(tags, rules_for_module('db'))
    if real or res.label == GenderLabel.UNKNOWN or res.conf is None:
        return ModelVerdict(GenderResult(GenderLabel.UNKNOWN, None, res.flags, (), False, 'model'), safety, real)
    weakest = min((probs.get(t, 0.0) for t in res.evidence), default=0.0)
    conf = _conf_of(weakest)
    conf = min(conf, res.conf)
    if video:
        conf = min(conf, GenderConf.MEDIUM)
    evidence = tuple(f'{t}~{probs.get(t, 0.0):.2f}' for t in res.evidence)[:8]
    return ModelVerdict(GenderResult(res.label, conf, res.flags, evidence, res.conflict, 'model'), safety, False)


def _is_photo(probs: dict[str, float]) -> bool:
    photo = probs.get('photo_(medium)', 0.0)
    return max(photo, probs.get('realistic', 0.0) if photo > 0.2 else 0.0) >= REAL_THRESHOLD


def _conf_of(p: float) -> GenderConf:
    return GenderConf.HIGH if p >= 0.85 else GenderConf.MEDIUM if p >= 0.6 else GenderConf.LOW


def _rule_tags(rules) -> frozenset[str]:
    """every tag a ruleset reads (an undecided one of these can change a label)"""
    out = set(rules.both | rules.exclusive_female | rules.exclusive_male | rules.exclusive_futa | rules.solo | rules.other
              | rules.none | rules.male_bodypart | rules.female_bodypart) | set(rules.aliases)
    for table in (rules.female, rules.male, rules.flags):
        for tags in table.values():
            out |= tags
    return frozenset(out)


def classify_frames(frames: list[dict[str, float]]) -> ModelVerdict:
    """Predicted tags of several frames of one video / animated picture -> one verdict (plan §3.5). Per tag: present when at
    least 2 frames have it at p >= CLIP_T_HI (1 frame for a clip of fewer than 3), absent when every frame has p <= CLIP_T_LO,
    undecided otherwise. The present tags go through the tier 0 rules ('db') like the tags of a picture; the label holds only
    when every combination of the undecided rule tags gives the same label, else unknown (both outputs must be decided, §3.4).
    Flags of every combination are kept (strict mode). Confidence: the weakest evidence tag, at its probability on the frame
    that makes it present (no cap: unlike a poster frame, the clip itself was looked at). Safety gate on every frame; a photo
    on enough frames -> unknown"""
    n = len(frames)
    if not n:
        return ModelVerdict(GenderResult(GenderLabel.UNKNOWN, None, frozenset(), (), False, 'model'))
    need = 1 if n < 3 else 2
    safety, best = '', 0.0
    for probs in frames:
        for t, p in probs.items():
            if p >= SAFETY_THRESHOLD and p > best and blocked_tag([t]):
                safety, best = t, p
    real = sum(1 for probs in frames if _is_photo(probs)) >= need
    rules = rules_for_module('db')
    relevant = _rule_tags(rules)
    present: dict[str, tuple[float, int]] = {}
    undecided: list[str] = []
    for tag in sorted(set().union(*frames)):
        ps = sorted((probs.get(tag, 0.0) for probs in frames), reverse=True)
        seen = sum(1 for p in ps if p >= CLIP_T_HI)
        if seen >= need:
            present[tag] = (ps[need - 1], seen)
        elif ps[0] > CLIP_T_LO and tag in relevant:
            undecided.append(tag)
    res = infer_from_tags(present, rules)

    def unknown(flags: frozenset[str]) -> ModelVerdict:
        return ModelVerdict(GenderResult(GenderLabel.UNKNOWN, None, flags, (), False, 'model'), safety, real)
    if real or res.label == GenderLabel.UNKNOWN or res.conf is None or len(undecided) > CLIP_UNDECIDED_MAX:
        return unknown(res.flags)
    conf, flags = res.conf, set(res.flags)
    for k in range(1, len(undecided) + 1):
        for combo in itertools.combinations(undecided, k):
            alt = infer_from_tags([*present, *combo], rules)
            if alt.label != res.label or alt.conf is None:
                return unknown(res.flags | alt.flags)
            conf, flags = min(conf, alt.conf), flags | alt.flags
    weakest = min((present[t][0] for t in res.evidence if t in present), default=0.0)
    conf = min(conf, _conf_of(weakest))
    evidence = tuple(f'{t}~{present[t][0]:.2f} ({present[t][1]}/{n})' for t in res.evidence if t in present)[:8]
    return ModelVerdict(GenderResult(res.label, conf, frozenset(flags), evidence, res.conflict, 'model'), safety, False)


def combine(tag_res: GenderResult, model: GenderResult, min_conf: GenderConf) -> GenderResult:
    """Tier 0 + tier 1 (plan §3.4): tags confident enough win without the model; a weaker tag label must be confirmed by
    the model (conservative: disagreement -> unknown); a trap is never turned into a female by the model"""
    if tag_res.label != GenderLabel.UNKNOWN and tag_res.conf is not None and tag_res.conf >= min_conf:
        return tag_res
    if 'trap' in tag_res.flags and model.label == GenderLabel.ONLY_FEMALE:
        return GenderResult(GenderLabel.UNKNOWN, None, tag_res.flags, tag_res.evidence, True, 'tags+model')
    if tag_res.label != GenderLabel.UNKNOWN:
        if model.label == tag_res.label:
            conf = max(tag_res.conf or GenderConf.LOW, model.conf or GenderConf.LOW)
            return GenderResult(tag_res.label, conf, tag_res.flags | model.flags, tag_res.evidence + model.evidence, False, 'tags+model')
        return GenderResult(GenderLabel.UNKNOWN, None, tag_res.flags, tag_res.evidence, True, 'tags+model')
    if model.label != GenderLabel.UNKNOWN:
        return GenderResult(model.label, model.conf, tag_res.flags | model.flags, model.evidence, model.conflict, 'model')
    return tag_res


def needs_model(tag_res: GenderResult, min_conf: GenderConf) -> bool:
    """tier 0 is not confident enough and the post is not a photo of real people (plan §3.1)"""
    if 'real' in tag_res.flags:
        return False
    return tag_res.label == GenderLabel.UNKNOWN or tag_res.conf is None or tag_res.conf < min_conf


# ---------------- verdict cache: a post's picture does not change ----------------

class VerdictCache:
    def __init__(self, path: pathlib.Path | None = None) -> None:
        if path is None:
            from .extensions import data_dir
            path = data_dir() / 'cache' / 'gender_model.sqlite'
        self.path = path
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            self._conn.execute('PRAGMA journal_mode=WAL')
            self._conn.execute('CREATE TABLE IF NOT EXISTS verdicts (key TEXT PRIMARY KEY, model TEXT, label TEXT, conf INTEGER, '
                               'flags TEXT, evidence TEXT, safety TEXT, real INTEGER, at REAL)')
        return self._conn

    def get(self, key: str, model: str = MODEL_ID) -> ModelVerdict | None:
        with self._lock:
            row = self._db().execute('SELECT label, conf, flags, evidence, safety, real FROM verdicts WHERE key = ? AND model = ?',
                                     (key, model)).fetchone()
        if row is None:
            return None
        label, conf, flags, evidence, safety, real = row
        res = GenderResult(GenderLabel(label), GenderConf(conf) if conf else None, frozenset(json.loads(flags)), tuple(json.loads(evidence)),
                           False, 'model')
        return ModelVerdict(res, safety or '', bool(real))

    def put(self, key: str, v: ModelVerdict, model: str = MODEL_ID) -> None:
        r = v.result
        with self._lock:
            self._db().execute('INSERT OR REPLACE INTO verdicts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                               (key, model, str(r.label), int(r.conf) if r.conf else 0, json.dumps(sorted(r.flags)),
                                json.dumps(list(r.evidence)), v.safety_tag, int(v.real), time.time()))
            self._db().commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None


# ---------------- model server client (lynxx/model_server.py) ----------------

def _is_loopback(host: str) -> bool:
    import ipaddress
    host = host.strip('[]').lower()
    if host == 'localhost' or host.endswith('.localhost'):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_local_network(host: str) -> bool:
    import ipaddress
    host = host.strip('[]').lower()
    if _is_loopback(host) or host.endswith(API_LAN_SUFFIXES):
        return True
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_private or ip.is_link_local


def check_api_url(url: str) -> str:
    """Normalized model server address (scheme://host[:port][/path], no trailing slash). Plain http only to this computer or
    a local network: the pictures and the token would travel in clear otherwise. Raises ValueError"""
    from urllib import parse as url_parse
    url = str(url or '').strip()
    if len(url) > 300:
        raise ValueError('model server address is too long')
    u = url_parse.urlsplit(url)
    if u.scheme not in ('http', 'https') or not u.hostname:
        raise ValueError(f'model server address must look like http://host:port: {url!r}')
    if u.username is not None or u.password is not None or u.query or u.fragment:
        raise ValueError('model server address: no user name, password, query or fragment')
    _ = u.port  # ValueError on a bad port
    if u.scheme == 'http' and not _is_local_network(u.hostname):
        raise ValueError(f'model server address: {u.hostname} is not on this computer or the local network, use https')
    return f'{u.scheme}://{u.netloc}{u.path.rstrip("/")}'


def api_is_this_computer(url: str) -> bool:
    from urllib import parse as url_parse
    return _is_loopback(url_parse.urlsplit(url).hostname or '')


class ModelApi:
    """Client of a model server: sends a picture, gets the predicted tags back (same shape as GenderTagger.predict)"""
    TAGS_MAX = 4000

    def __init__(self, url: str, token: str = '') -> None:
        import requests
        try:
            self.url = check_api_url(url)
        except ValueError as err:
            raise ModelUnavailable(str(err)) from None
        self.model_id = MODEL_ID
        self.info: dict = {}
        self._session = requests.Session()
        self._session.trust_env = not api_is_this_computer(self.url)  # a system proxy must not catch calls to this computer
        self._session.headers['User-Agent'] = 'Lynxx gender-model client'
        if token:
            self._session.headers['Authorization'] = f'Bearer {token}'

    def _request(self, method: str, path: str, **kwargs):
        import requests
        for attempt in range(3):
            try:
                r = self._session.request(method, f'{self.url}/{API_VERSION}/{path}', allow_redirects=False, **kwargs)
            except requests.RequestException as err:
                raise ModelUnavailable(f'model server {self.url} is not reachable ({type(err).__name__})') from None
            if r.status_code == 503 and attempt < 2 and self._busy(r):  # every worker of the server is running the model
                time.sleep(1 + attempt)
                continue
            return r
        return r

    INFO_KEYS = ('id', 'repo', 'revision', 'license', 'inputSize', 'server', 'workers', 'loaded', 'device', 'idleUnload',
                 'deviceSetting')

    @staticmethod
    def _busy(r) -> bool:
        """503 'busy' is worth a retry; a model that cannot be loaded is not"""
        try:
            return r.json().get('error') == 'busy'
        except Exception:
            return True

    @staticmethod
    def _reason(r) -> str:
        try:
            return str(r.json().get('error') or '')[:200]
        except Exception:
            return ''

    @classmethod
    def _fail(cls, r) -> None:
        if r.status_code == 401:
            raise ModelUnavailable('model server: wrong or missing token')
        why = cls._reason(r)
        raise ModelUnavailable(f'model server: HTTP {r.status_code}{f": {why}" if why else ""}')

    def status(self, timeout: float = 5) -> dict:
        t0 = time.monotonic()
        r = self._request('GET', 'status', timeout=timeout)
        if r.status_code != 200:
            self._fail(r)
        try:
            info = r.json()
            assert isinstance(info, dict) and isinstance(info.get('id'), str)
        except Exception:
            raise ModelUnavailable(f'{self.url} is not a Lynxx model server') from None
        self.model_id = info['id'][:64]
        self.info = {k: info[k] for k in self.INFO_KEYS if isinstance(info.get(k), str | int | bool)}
        if isinstance(info.get('hardware'), dict):
            self.info['hardware'] = info['hardware']
        if isinstance(media := info.get('media'), dict):  # decoders of /v1/frames (servers before -gender_verify_media: none)
            self.info['media'] = {k: str(media.get(k) or '')[:80] for k in ('video', 'animated')}
        self.info['ms'] = int((time.monotonic() - t0) * 1000)
        return self.info

    def load(self, timeout: float = 180) -> dict:
        """loads the model now (the server would load it on the first picture), so the first job does not wait"""
        r = self._request('POST', 'load', timeout=timeout)
        if r.status_code != 200:
            self._fail(r)
        return self.status()

    def unload(self, timeout: float = 10) -> dict:
        """frees the memory (RAM / VRAM) the model holds on the server; the next picture loads it again"""
        r = self._request('POST', 'unload', timeout=timeout)
        if r.status_code != 200:
            self._fail(r)
        return self.status()

    def predict(self, image: bytes) -> dict[str, float]:
        r = self._request('POST', 'tag', data=image, headers={'Content-Type': 'application/octet-stream'}, timeout=API_TIMEOUT)
        if r.status_code in (400, 413, 415):  # this picture only: not an image, too large
            return {}
        if r.status_code != 200:
            self._fail(r)
        try:
            return self._tags(r.json()['tags'])
        except Exception:
            raise ModelUnavailable('model server: malformed answer') from None

    def _tags(self, tags) -> dict[str, float]:
        assert isinstance(tags, dict) and len(tags) <= self.TAGS_MAX
        return {str(t)[:128]: min(max(float(p), 0.0), 1.0) for t, p in tags.items() if isinstance(p, int | float)}

    FRAMES_MAX = 64

    def predict_frames(self, path: pathlib.Path) -> list[dict[str, float]]:
        """Tags of up to 8 frames of a video / animated picture file (POST /v1/frames, the file is streamed). MediaUnreadable:
        this file only (too large, unreadable, no decoder for it on the server); ModelUnavailable: the server cannot be used"""
        import requests

        from .media_frames import MEDIA_MAX_BYTES
        if path.stat().st_size > MEDIA_MAX_BYTES:
            raise MediaUnreadable(f'larger than {MEDIA_MAX_BYTES // 1048576} MB')
        for attempt in range(3):
            try:
                with path.open('rb') as f:  # opened again for a retry: the body is read from the file each time
                    r = self._session.post(f'{self.url}/{API_VERSION}/frames', data=f, allow_redirects=False,
                                           headers={'Content-Type': 'application/octet-stream'}, timeout=(10, FRAMES_TIMEOUT))
            except requests.RequestException as err:
                raise ModelUnavailable(f'model server {self.url} is not reachable ({type(err).__name__})') from None
            if r.status_code == 503 and attempt < 2 and self._busy(r):
                time.sleep(1 + attempt)
                continue
            break
        if r.status_code == 404:
            raise ModelUnavailable('model server: no /v1/frames, update it to check videos (-gender_verify_media)')
        if r.status_code in (400, 413, 415):
            raise MediaUnreadable(self._reason(r) or f'HTTP {r.status_code}')
        if r.status_code != 200:
            self._fail(r)
        try:
            frames = r.json()['frames']
            assert isinstance(frames, list) and len(frames) <= self.FRAMES_MAX
            return [self._tags(fr['tags']) for fr in frames]
        except Exception:
            raise ModelUnavailable('model server: malformed answer') from None


_api_clients: dict[tuple[str, str], ModelApi] = {}
_api_lock = threading.Lock()


def backend(api_url: str = '', token: str = ''):
    """The model a process uses: the model server at api_url (or LYNXX_GENDER_MODEL_URL), else the model in process.
    Raises ModelUnavailable (server not reachable, wrong token, model or runtime missing)"""
    url = api_url or os.environ.get(ENV_MODEL_URL, '')
    if not url:
        return GenderTagger.get()
    token = token or os.environ.get(ENV_MODEL_TOKEN, '')
    try:
        key = (check_api_url(url), token)
    except ValueError as err:
        raise ModelUnavailable(str(err)) from None
    with _api_lock:
        if (client := _api_clients.get(key)) is None:
            client = ModelApi(*key)
            client.status()  # fails early, learns the model id (verdict cache key)
            _api_clients[key] = client
        return client


def api_status(url: str, token: str = '', action: str = '') -> dict:
    """desktop app: can the model server at url be used, and on what hardware (check: lynxx/model_hw.py hardware_check).
    action 'load' / 'unload': load the model now / free its memory, then the status"""
    out: dict = {'url': str(url or ''), 'reachable': False, 'local': False, 'error': ''}
    try:
        out['url'] = check_api_url(url)
        out['local'] = api_is_this_computer(out['url'])
        api = ModelApi(out['url'], token)
        out['model'] = api.status()
        out['reachable'] = True
        if action in ('load', 'unload'):
            out['model'] = getattr(api, action)()
    except (ValueError, ModelUnavailable) as err:
        out['error'] = str(err)
    if m := out.get('model'):
        out['check'] = hardware_check(m.get('hardware'), str(m.get('device') or '') if m.get('loaded') else '',
                                      str(m.get('deviceSetting') or ''))
    return out


def classify_image(image: bytes, *, video: bool = False) -> ModelVerdict:
    return classify(GenderTagger.get().predict(image), video=video)


def media_support(api_url: str = '', token: str = '') -> dict[str, str]:
    """what -gender_verify_media can read with the model of this process: {'video': decoder or '', 'animated': decoder or ''}.
    ModelUnavailable: the model cannot be used, or the model server predates /v1/frames"""
    tagger = backend(api_url, token)
    if isinstance(tagger, ModelApi):
        media = tagger.info.get('media')
        if not isinstance(media, dict):
            raise ModelUnavailable(f'model server {tagger.url} cannot check videos (no /v1/frames): update it')
        return dict(media)
    from .media_frames import decoders
    return decoders()


def verdict_for_media(dwn, path: pathlib.Path) -> tuple[ModelVerdict, int]:
    """-gender_verify_media: verdict of a downloaded video / animated picture on several of its frames, and how many frames were
    tagged. Not cached: a file is checked once, right after its download. MediaUnreadable: this file only; ModelUnavailable:
    the model cannot be used (the job stops)"""
    tagger = backend(getattr(dwn, 'gender_model_url', ''), getattr(dwn, 'gender_model_token', ''))
    frames = tagger.predict_frames(path)
    if not frames:
        raise MediaUnreadable('no frame could be tagged')
    return classify_frames(frames), len(frames)


# ---------------- posts of a downloader (download pipeline and preview) ----------------

IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.webp', '.gif')
IMAGE_MAX_BYTES = 12 * 1024 * 1024
MODEL_WORKERS = 3
_cache: VerdictCache | None = None
_cache_lock = threading.Lock()


def _shared_cache() -> VerdictCache:
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = VerdictCache()
        return _cache


def image_url_for(dwn, raw: str) -> str:
    """picture the model looks at: the sample (~850 px, much better than a 180 px thumbnail) when it is an image, else the
    thumbnail (videos: their poster frame)"""
    from urllib import parse as url_parse
    h = dwn._local_addr_from_string(str(raw))
    i = h.find(' sample_url="')
    sample = h[i + 13:h.find('"', i + 13)] if i >= 0 else ''
    if sample and url_parse.urlparse(sample).path.lower().endswith(IMAGE_EXTS):
        url = sample
    else:
        url = dwn.extract_preview_url(h)
    if url.startswith('//'):
        url = f'https:{url}'
    elif url.startswith('/'):
        url = f'{dwn._get_sitename().rstrip("/")}{url}'
    return url if url.startswith(('https://', 'http://')) else ''


def _fetch_image(dwn, url: str) -> bytes:
    r = dwn.wrap_request(url, max(1, min(int(getattr(dwn, 'retries', 3) or 3), 3)), 'GET')
    if r is None or not str(r.headers.get('content-type', '')).lower().startswith('image/'):
        return b''
    data = r.content
    return data if len(data) <= IMAGE_MAX_BYTES else b''


CLASSIFY_VERSION = 1
'''bump when classify() turns predicted tags into a label differently (thresholds, confidence): cached verdicts are made again'''


def verdict_cache_model(model_id: str) -> str:
    """'model' column of the verdict cache: the model's id and what turns its tags into a label (classify, the 'db' ruleset). The
    cache keeps labels, not the predicted tags: a verdict made with other rules is not served again, the post is looked at anew"""
    rules = json.dumps(dataclasses.asdict(rules_for_module('db')), sort_keys=True, default=sorted, ensure_ascii=False)
    return f'{model_id}#c{CLASSIFY_VERSION:d}-{hashlib.sha256(rules.encode()).hexdigest()[:12]}'


def verdicts_for(dwn, posts: list[tuple[str, str]], *, workers: int = MODEL_WORKERS) -> dict[str, ModelVerdict]:
    """{post id: verdict} for (post id, raw item) pairs. Cached verdicts of the same model and rules are reused (verdict_cache_model);
    posts whose picture cannot be fetched are absent (they stay 'unknown'). Raises ModelUnavailable when the model server cannot be
    used, or the model or its runtime is missing (in process)"""
    from concurrent.futures import ThreadPoolExecutor
    tagger = backend(getattr(dwn, 'gender_model_url', ''), getattr(dwn, 'gender_model_token', ''))
    cache = _shared_cache()
    abbr = dwn._get_module_abbr()
    model = verdict_cache_model(tagger.model_id)

    def one(post_id: str, raw: str) -> tuple[str, ModelVerdict | None]:
        key = f'{abbr}:{post_id}'
        if (hit := cache.get(key, model)) is not None:
            return post_id, hit
        url = image_url_for(dwn, raw)
        data = _fetch_image(dwn, url) if url else b''
        if not data:
            return post_id, None
        try:
            probs = tagger.predict(data)
        except ModelUnavailable:
            raise
        except Exception:  # undecodable picture: no verdict
            return post_id, None
        if not probs:  # the model server refused this picture
            return post_id, None
        v = classify(probs, video=dwn._media_kind(raw) == 'video')
        cache.put(key, v, model)
        return post_id, v

    out: dict[str, ModelVerdict] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix='gender-model') as ex:
        for post_id, v in ex.map(lambda p: one(*p), posts):
            if v is not None:
                out[post_id] = v
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    cmd = args[0] if args else 'status'
    if cmd == 'status':
        url = os.environ.get(ENV_MODEL_URL, '')
        print(json.dumps(api_status(url, os.environ.get(ENV_MODEL_TOKEN, '')) if url else model_status(), indent=1))
        return 0
    if cmd == 'install':
        last = [0.0]

        def progress(done: int, total: int) -> None:
            if time.monotonic() - last[0] > 2 or done == total:
                last[0] = time.monotonic()
                print(f'{done / 1048576:.0f} / {total / 1048576:.0f} MB', file=sys.stderr)
        print(json.dumps(install_model(progress), indent=1))
        return 0
    if cmd == 'remove':
        print(json.dumps(remove_model(), indent=1))
        return 0
    if cmd == 'classify':  # a video / animated picture: on its frames, like -gender_verify_media
        from .media_frames import file_media_kind
        tagger = backend()  # LYNXX_GENDER_MODEL_URL: through the model server
        for f in args[1:]:
            frames = 0
            if file_media_kind(pathlib.Path(f)):
                probs = tagger.predict_frames(pathlib.Path(f))
                v, frames = classify_frames(probs), len(probs)
            else:
                v = classify(tagger.predict(pathlib.Path(f).read_bytes()))
            print(json.dumps({'file': f, **v.result.as_dict(), 'safety': v.safety_tag, 'real': v.real, 'frames': frames},
                             ensure_ascii=False))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == '__main__':
    sys.exit(main())

#
#
#########################################
