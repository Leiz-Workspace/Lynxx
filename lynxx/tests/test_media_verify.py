# coding=UTF-8
"""
Lynxx: -gender_verify_media (docs/plans/gender-filter-model.md §3.5, P4, AC-G16): frames of videos / animated pictures
(lynxx/media_frames.py), POST /v1/frames of the model server, the client (ModelApi.predict_frames, classify_frames) and the
downloader moving files that do not match the gender filter into _gender_mismatch/.
A stub stands for the model; media decoding tests run only where Pillow / ffmpeg exist (skipped otherwise)
"""
#########################################
#
#

from __future__ import annotations

import http.client
import itertools
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from fractions import Fraction
from unittest import TestCase, mock

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx import gender_model, media_frames  # noqa: E402
from lynxx.cmdargs import prepare_arglist  # noqa: E402
from lynxx.defines import DownloadModes, ThreadInterruptException  # noqa: E402
from lynxx.download_base import GENDER_MISMATCH_DIR  # noqa: E402
from lynxx.downloaders import _make_downloader  # noqa: E402
from lynxx.gender import GenderConf, GenderLabel, GenderResult  # noqa: E402
from lynxx.gender_model import MediaUnreadable, ModelApi, ModelUnavailable, ModelVerdict, classify_frames, media_support  # noqa: E402
from lynxx.media_frames import Frames, MediaError, file_media_kind, sniff, spread  # noqa: E402
from lynxx.model_server import make_server  # noqa: E402
from lynxx.module import ProcModule  # noqa: E402
from lynxx.network import FileDownloadResult  # noqa: E402

TOKEN = 'test-token-1234567890'
PNG = b'\x89PNG\r\n\x1a\n' + b'\0' * 32
BOY = b'BOY-frame'
GIRL = {'1girl': 0.95, 'solo': 0.9}
COUPLE = {'1girl': 0.9, '1boy': 0.9, 'hetero': 0.85}
MP4 = b'\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2' + b'\0' * 64
HW = {'gpus': [], 'providers': ['CPUExecutionProvider'], 'runtime': '1.32.0', 'ramMiB': 1, 'ramFreeMiB': 1, 'cpus': 1, 'os': 'linux'}
MEDIA = {'video': 'ffmpeg test', 'animated': 'pillow test'}
GIF_FRAME = b'\x21\xf9\x04\x00\x0a\x00\x00\x00' + b'\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00' + b'\x02\x02\x44\x01\x00'


def _gif(frames: int) -> bytes:
    """a 1x1 GIF with this many frames (written by hand: no Pillow needed)"""
    return b'GIF89a\x01\x00\x01\x00\x80\x00\x00' + b'\x00\x00\x00\xff\xff\xff' + GIF_FRAME * frames + b'\x3b'


def _png(actl_frames: int | None) -> bytes:
    ihdr = b'\x00\x00\x00\x0dIHDR' + b'\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00' + b'\0' * 4
    actl = b'' if actl_frames is None else b'\x00\x00\x00\x08acTL' + actl_frames.to_bytes(4, 'big') + b'\0' * 8
    return b'\x89PNG\r\n\x1a\n' + ihdr + actl + b'\x00\x00\x00\x00IDAT' + b'\0' * 4 + b'\x00\x00\x00\x00IEND' + b'\0' * 4


def _webp(animated: bool) -> bytes:
    return b'RIFF\x30\x00\x00\x00WEBPVP8X\x0a\x00\x00\x00' + bytes([0x02 if animated else 0x10]) + b'\0' * 32


def _has_pillow() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except ImportError:
        return False


class _StubTagger:
    """a frame starting with BOY shows a couple, a PNG / BMP frame one girl; anything else is not a picture"""
    model_id = 'stub-tagger-1'

    def __init__(self) -> None:
        self.calls = 0

    def predict(self, image: bytes) -> dict[str, float]:
        self.calls += 1
        if image.startswith(BOY):
            return dict(COUPLE)
        if image.startswith((PNG[:8], b'BM')):
            return dict(GIRL)
        raise OSError('cannot identify image file')


def _stub_extract(path: pathlib.Path) -> Frames:
    """frames written in the media file itself: 'FRAMES:G,G,B' -> girl, girl, boy"""
    data = path.read_bytes()
    if data.startswith(b'UNSUPPORTED'):
        raise MediaError('no video decoder here: install ffmpeg (on PATH) or PyAV', True)
    if not data.startswith(b'FRAMES:'):
        raise MediaError('unreadable video')
    names = data[7:].decode().split(',')
    return Frames('video', 'stub', float(len(names)), [(float(i), PNG if n == 'G' else BOY) for i, n in enumerate(names)])


class _Server:
    def __init__(self, **kwargs) -> None:
        self.tagger = _StubTagger()
        kwargs.setdefault('extract', _stub_extract)
        kwargs.setdefault('media', lambda: dict(MEDIA))
        self.srv = make_server('127.0.0.1', 0, tagger=self.tagger, token=TOKEN, quiet=True, hardware=lambda: HW, **kwargs)
        self.srv.log = lambda _text: None
        self.thread = threading.Thread(target=self.srv.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)

    def __enter__(self) -> _Server:
        self.thread.start()
        return self

    def __exit__(self, *_) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    @property
    def url(self) -> str:
        return self.srv.url


class MediaKindTests(TestCase):
    """what a downloaded file is, from its first bytes (the app core has no decoder)"""
    def test_sniff_and_kind(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            cases = {
                'anim.gif': (_gif(3), 'gif', 'animated'), 'still.gif': (_gif(1), 'gif', ''),
                'anim.png': (_png(4), 'png', 'animated'), 'one.png': (_png(1), 'png', ''), 'still.png': (_png(None), 'png', ''),
                'anim.webp': (_webp(True), 'webp', 'animated'), 'still.webp': (_webp(False), 'webp', ''),
                'clip.mp4': (MP4, 'mov', 'video'), 'clip.webm': (b'\x1aE\xdf\xa3' + b'\0' * 40, 'matroska', 'video'),
                'clip.avi': (b'RIFF\0\0\0\0AVI LIST' + b'\0' * 40, 'avi', 'video'),
                'photo.jpg': (b'\xff\xd8\xff\xe0' + b'\0' * 40, '', ''), 'list.m3u8': (b'#EXTM3U\nfile:///etc/passwd\n', '', ''),
                'empty.mp4': (b'', '', ''),
            }
            for name, (data, fmt, kind) in cases.items():
                with self.subTest(name):
                    p = pathlib.Path(d, name)
                    p.write_bytes(data)
                    self.assertEqual(fmt, sniff(data[:32]))
                    self.assertEqual(kind, file_media_kind(p))

    def test_spread(self) -> None:
        pos = spread(8)
        self.assertEqual(8, len(pos))
        self.assertGreater(pos[0], 0.05)
        self.assertLess(pos[-1], 0.95)
        steps = {round(b - a, 9) for a, b in itertools.pairwise(pos)}
        self.assertEqual(1, len(steps))  # evenly spaced
        self.assertEqual([0.5], spread(1))
        self.assertEqual([], spread(0))

    def test_unknown_media_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d, 'list.mp4')
            p.write_bytes(b'#EXTM3U\n#EXT-X-VERSION:3\nhttp://example.com/a.ts\n')  # a playlist named .mp4 is never opened
            with self.assertRaises(MediaError) as ctx:
                media_frames.extract_frames(p)
            self.assertTrue(ctx.exception.unsupported)

    def test_decoders(self) -> None:
        try:
            with mock.patch.object(media_frames, '_tool', return_value=''), mock.patch.dict(sys.modules, {'av': None}):
                self.assertEqual('', media_frames.decoders(refresh=True)['video'])
                with tempfile.TemporaryDirectory() as d:
                    p = pathlib.Path(d, 'clip.mp4')
                    p.write_bytes(MP4)
                    with self.assertRaisesRegex(MediaError, 'ffmpeg') as ctx:
                        media_frames.extract_frames(p)
                    self.assertTrue(ctx.exception.unsupported)  # 415: no decoder here, the file stays unchecked
        finally:
            media_frames.decoders(refresh=True)


class ClassifyFramesTests(TestCase):
    """aggregation of plan §3.5: present on >= 2 frames (1 for a clip of < 3), absent when every frame <= t_lo, else undecided"""
    def label(self, frames: list[dict]) -> tuple[str, GenderConf | None]:
        v = classify_frames(frames)
        return str(v.result.label), v.result.conf

    def test_aggregation(self) -> None:
        H, M = GenderConf.HIGH, GenderConf.MEDIUM
        self.assertEqual(('only_female', H), self.label([GIRL] * 8))  # not capped at medium like a poster frame
        self.assertEqual(('unknown', None), self.label([GIRL] * 7 + [COUPLE]))  # a boy on one frame: undecided
        self.assertEqual(('both', H), self.label([GIRL] * 6 + [COUPLE] * 2))  # on two frames: present
        self.assertEqual(('both', M), self.label([GIRL] * 6 + [{**COUPLE, 'hetero': 0.7}] * 2))  # weakest evidence 0.70
        self.assertEqual(('unknown', None), self.label([GIRL] * 7 + [{'1girl': 0.95, '1boy': 0.25}]))  # between t_lo and t_hi
        self.assertEqual(('only_female', H), self.label([GIRL] * 7 + [{'1girl': 0.95, '1boy': 0.15}]))  # <= t_lo: absent
        self.assertEqual(('only_female', H), self.label([{'1girl': 0.95}, {'1girl': 0.3}]))  # 2 frames: 1 is enough
        self.assertEqual(('unknown', None), self.label([{'1girl': 0.95}, {'1girl': 0.3}, {'1girl': 0.3}]))  # 3 frames: 2 needed
        self.assertEqual(('none', H), self.label([{'no_humans': 0.9, 'scenery': 0.9}] * 8))
        self.assertEqual(('only_female', M), self.label([{'1girl': 0.7}] * 8))  # weakest evidence -> confidence
        self.assertEqual(('unknown', None), self.label([]))

    def test_flags_safety_and_photos(self) -> None:
        v = classify_frames([{**GIRL, 'futanari': 0.3}] + [GIRL] * 7)  # undecided futa: the label holds, the flag is kept
        self.assertEqual(('only_female', {'futa'}), (str(v.result.label), set(v.result.flags)))
        v = classify_frames([GIRL] * 7 + [{**GIRL, 'loli': 0.2}])  # safety gate on any frame
        self.assertEqual('loli', v.safety_tag)
        self.assertEqual('', classify_frames([GIRL] * 7 + [{**GIRL, 'loli': 0.1}]).safety_tag)
        photo = {**GIRL, 'photo_(medium)': 0.9, 'realistic': 0.9}
        v = classify_frames([photo] * 2 + [GIRL] * 6)
        self.assertEqual(('unknown', True), (str(v.result.label), v.real))
        self.assertFalse(classify_frames([photo] + [GIRL] * 7).real)  # one frame is not a photo clip
        many = {f'{n}girls': 0.3 for n in (2, 3, 4, 5)} | {f'{n}boys': 0.3 for n in (2, 3, 4, 5, 6)}
        self.assertEqual(('unknown', None), self.label([{**GIRL, **many}] + [GIRL] * 7))  # too many undecided tags


class FramesEndpointTests(TestCase):
    def setUp(self) -> None:
        gender_model._api_clients.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self) -> None:
        gender_model._api_clients.clear()
        self.tmp.cleanup()

    def media(self, data: bytes, name: str = 'clip.mp4') -> pathlib.Path:
        p = self.dir / name
        p.write_bytes(data)
        return p

    def test_frames_through_the_server(self) -> None:
        made: list[str] = []
        real_mkdtemp = tempfile.mkdtemp

        def mkdtemp(**kw):
            made.append(real_mkdtemp(**kw))
            return made[-1]
        with _Server() as s, mock.patch('lynxx.model_server.tempfile.mkdtemp', mkdtemp):
            api = ModelApi(s.url, TOKEN)
            self.assertEqual(MEDIA, api.status()['media'])
            frames = api.predict_frames(self.media(b'FRAMES:G,G,G,G,G,G,B,B'))
            self.assertEqual(8, len(frames))
            self.assertEqual(('both', 'model'), (str(classify_frames(frames).result.label), classify_frames(frames).result.source))
            self.assertEqual('unknown', str(classify_frames(api.predict_frames(self.media(b'FRAMES:G,G,G,G,G,G,G,B'))).result.label))
            self.assertEqual(2, len(made))
            self.assertFalse(any(os.path.exists(d) for d in made))  # the media is never kept
            with self.assertRaisesRegex(MediaUnreadable, 'ffmpeg'):
                api.predict_frames(self.media(b'UNSUPPORTED'))  # 415: kept unchecked, the job goes on
            with self.assertRaisesRegex(MediaUnreadable, 'unreadable'):
                api.predict_frames(self.media(b'garbage'))  # 400
            self.assertFalse(any(os.path.exists(d) for d in made))
            big = self.media(b'x')
            with mock.patch.object(media_frames, 'MEDIA_MAX_BYTES', 0), self.assertRaisesRegex(MediaUnreadable, 'larger'):
                api.predict_frames(big)  # never sent

    def test_raw_http(self) -> None:
        with _Server() as s:
            host, port = s.srv.server_address[:2]

            def call(headers: dict, body: bytes = b'') -> int:
                c = http.client.HTTPConnection(host, port, timeout=5)
                try:
                    c.putrequest('POST', '/v1/frames')
                    for k, v in headers.items():
                        c.putheader(k, v)
                    c.endheaders()
                    if body:
                        c.send(body)
                    return c.getresponse().status
                finally:
                    c.close()
            auth = {'Authorization': f'Bearer {TOKEN}'}
            body = b'FRAMES:G'
            self.assertEqual(401, call({'Content-Length': str(len(body))}, body))
            self.assertEqual(411, call(auth))
            self.assertEqual(413, call({**auth, 'Content-Length': str(media_frames.MEDIA_MAX_BYTES + 1)}))  # body never read
            self.assertEqual(400, call({**auth, 'Content-Length': '0'}))
            self.assertEqual(415, call({**auth, 'Content-Length': '11'}, b'UNSUPPORTED'))
            self.assertEqual(200, call({**auth, 'Content-Length': str(len(body))}, body))
            self.assertEqual(1, s.tagger.calls)  # refused requests never reach the model

    def test_old_server_and_server_down(self) -> None:
        with _Server() as s:
            api = ModelApi(s.url, TOKEN)
            api.status()
            self.assertEqual(MEDIA, media_support(s.url, TOKEN))
            url = s.url
        old = ModelApi(url, TOKEN)
        old.info = {'id': 'x'}  # status of a server older than /v1/frames: no 'media'
        gender_model._api_clients[(url, TOKEN)] = old
        with self.assertRaisesRegex(ModelUnavailable, 'update'):
            media_support(url, TOKEN)
        with self.assertRaisesRegex(ModelUnavailable, 'not reachable'):
            old.predict_frames(self.media(b'FRAMES:G'))  # the server is gone: the job stops
        not_found = types.SimpleNamespace(status_code=404, json=lambda: {'error': 'not found'})
        old._session = types.SimpleNamespace(post=lambda *a, **k: not_found)
        with self.assertRaisesRegex(ModelUnavailable, '/v1/frames'):
            old.predict_frames(self.media(b'FRAMES:G'))

    def test_busy_server_is_asked_again(self) -> None:
        answers = [types.SimpleNamespace(status_code=503, json=lambda: {'error': 'busy'}),
                   types.SimpleNamespace(status_code=200, json=lambda: {'frames': [{'at': 0, 'tags': GIRL}]})]
        sent: list[bytes] = []

        def post(url, data=None, **_):
            sent.append(data.read())  # the file is opened again for each try
            return answers.pop(0)
        api = ModelApi('http://127.0.0.1:9', TOKEN)
        api._session = types.SimpleNamespace(post=post)
        with mock.patch.object(gender_model.time, 'sleep'):
            self.assertEqual([GIRL], api.predict_frames(self.media(b'FRAMES:G')))
        self.assertEqual([b'FRAMES:G', b'FRAMES:G'], sent)


@unittest.skipUnless(_has_pillow(), 'Pillow is not installed')
class RealPictureFramesTests(TestCase):
    def test_animated_pictures(self) -> None:
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            frames = [Image.new('RGB', (64, 48), (i * 20, 100, 200)) for i in range(12)]
            for ext in ('gif', 'png', 'webp'):
                with self.subTest(ext):
                    p = pathlib.Path(d, f'anim.{ext}')
                    frames[0].save(p, save_all=True, append_images=frames[1:], duration=80, loop=0)
                    self.assertEqual('animated', file_media_kind(p))
                    clip = media_frames.extract_frames(p)
                    self.assertEqual(('animated', 'pillow', 12.0), (clip.kind, clip.decoder, clip.length))
                    self.assertEqual([1.0, 2.0, 3.0, 5.0, 6.0, 8.0, 9.0, 10.0], [at for at, _ in clip.frames])  # 5 % ends left out
                    self.assertTrue(all(img.startswith(PNG[:8]) for _, img in clip.frames))
            still = pathlib.Path(d, 'still.gif')
            frames[0].save(still)
            self.assertEqual('', file_media_kind(still))
            self.assertEqual(1, len(media_frames.extract_frames(still).frames))
            broken = pathlib.Path(d, 'broken.gif')
            broken.write_bytes((pathlib.Path(d) / 'anim.gif').read_bytes()[:40])
            with self.assertRaises(MediaError):
                media_frames.extract_frames(broken)

    def test_server_reads_a_gif(self) -> None:
        from PIL import Image
        with tempfile.TemporaryDirectory() as d, _Server(extract=media_frames.extract_frames) as s:
            p = pathlib.Path(d, 'anim.gif')
            pics = [Image.new('RGB', (32, 32), (i * 40, 0, 0)) for i in range(6)]  # identical frames would be merged
            pics[0].save(p, save_all=True, append_images=pics[1:], duration=50)
            frames = ModelApi(s.url, TOKEN).predict_frames(p)
            self.assertEqual(6, len(frames))
            self.assertEqual('only_female', str(classify_frames(frames).result.label))

    def test_model_in_process(self) -> None:
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d, 'anim.webp')
            pics = [Image.new('RGB', (32, 32), (i * 40, 0, 0)) for i in range(4)]
            pics[0].save(p, save_all=True, append_images=pics[1:], duration=50)
            stub = _StubTagger()
            self.assertEqual(4, len(gender_model.GenderTagger.predict_frames(stub, p)))
            p.write_bytes(b'UNSUPPORTED')
            with self.assertRaises(MediaUnreadable):
                gender_model.GenderTagger.predict_frames(stub, p)

    def test_pyav_when_there_is_no_ffmpeg(self) -> None:
        """PyAV path with a stand-in module (the real PyAV is not a dependency): seek, decode, frame times"""
        from PIL import Image
        seeks: list[float] = []

        class Frame:
            def __init__(self, t: float) -> None:
                self.time = t

            def to_image(self):
                return Image.new('RGB', (64, 48))

        class Stream:
            type, duration, time_base, frames = 'video', 500, Fraction(1, 100), 125

        class Container:
            streams, duration, pos = [Stream()], 5_000_000, 0.0

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def seek(self, offset, stream=None, backward=True, any_frame=False):
                self.pos = float(offset * stream.time_base)
                seeks.append(self.pos)

            def decode(self, stream):
                return (Frame(self.pos - 0.2 + i * 0.04) for i in range(20))
        fake_av = types.SimpleNamespace(open=lambda path, **_kw: Container(), __version__='0-test')
        with tempfile.TemporaryDirectory() as d, mock.patch.object(media_frames, '_tool', return_value=''), \
                mock.patch.dict(sys.modules, {'av': fake_av}):
            p = pathlib.Path(d, 'clip.mp4')
            p.write_bytes(MP4)
            clip = media_frames.extract_frames(p)
            self.assertEqual(('video', 'pyav', 5.0, 8), (clip.kind, clip.decoder, clip.length, len(clip.frames)))
            self.assertEqual([round(5 * x, 3) for x in spread(8)], [at for at, _ in clip.frames])
            self.assertEqual(8, len(seeks))


@unittest.skipUnless(shutil.which('ffmpeg'), 'ffmpeg is not on PATH')
class RealVideoFramesTests(TestCase):
    def test_ffmpeg(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            clips = {}
            for name, codec in (('clip.mp4', ['-pix_fmt', 'yuv420p']), ('clip.mkv', [])):
                p = pathlib.Path(d, name)
                r = subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc=duration=2:size=160x120:rate=10',
                                    *codec, str(p)], capture_output=True, timeout=60, check=False)
                if r.returncode != 0:
                    self.skipTest(f'ffmpeg cannot make a test clip: {r.stderr[-200:]!r}')
                clips[name] = p
            for name, p in clips.items():
                with self.subTest(name):
                    self.assertEqual('video', file_media_kind(p))
                    clip = media_frames.extract_frames(p)
                    self.assertEqual(('video', 'ffmpeg', 2.0, 8), (clip.kind, clip.decoder, clip.length, len(clip.frames)))
                    self.assertEqual([round(2 * x, 3) for x in spread(8)], [at for at, _ in clip.frames])
                    self.assertTrue(all(img.startswith(b'BM') for _, img in clip.frames))
                    self.assertEqual(8, len({img for _, img in clip.frames}))  # testsrc changes on every frame
            real_tool = media_frames._tool
            with mock.patch.object(media_frames, '_tool', lambda n: '' if n == 'ffprobe' else real_tool(n)):
                self.assertEqual(8, len(media_frames.extract_frames(clips['clip.mp4']).frames))  # duration from ffmpeg itself
            cut = pathlib.Path(d, 'cut.mp4')
            cut.write_bytes(clips['clip.mp4'].read_bytes()[:1500])
            with self.assertRaises(MediaError):
                media_frames.extract_frames(cut)
            with _Server(extract=media_frames.extract_frames) as s:
                self.assertEqual(8, len(ModelApi(s.url, TOKEN).predict_frames(clips['clip.mp4'])))
                self.assertEqual(8, s.tagger.calls)


def _db_posts(posts: list[tuple[int, str, str]]) -> bytes:
    return json.dumps([{
        'id': i, 'created_at': '2026-11-16T04:24:19.090-05:00', 'tag_string': tags, 'file_ext': ext, 'md5': f'{i:032d}',
        'file_url': f'https://cdn.donmai.us/original/{i}.{ext}', 'large_file_url': f'https://cdn.donmai.us/sample/{i}.jpg',
        'preview_file_url': f'https://cdn.donmai.us/180x180/{i}.jpg', 'rating': 'g', 'score': 1,
    } for i, tags, ext in posts]).encode()


class VerifyDownloadedMediaTests(TestCase):
    """AC-G16: a file whose verdict does not match the filter is moved into _gender_mismatch/, never deleted"""
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dest = pathlib.Path(self.tmp.name)
        self.dwn = _make_downloader(ProcModule.DB)
        self.root = types.SimpleNamespace(killed=False)
        self.dwn.reset_root_thread(self.root)
        self.dwn.no_conflict_delay = True
        self.dwn.dest_base = self.dest
        self.dwn.gender_select = frozenset({'only_female'})
        self.dwn.gender_mode = 'model'
        self.dwn.gender_verify_media = True
        # 1: no gender tag (the model labeled it from its poster), 2: confident tags, 3: still picture
        items = self.dwn._get_all_post_tags(_db_posts([(1, 'animated video', 'mp4'), (2, '1girl solo video', 'mp4'),
                                                       (3, 'scenery', 'jpg')]))
        for it in items:
            info = self.dwn._extract_item_info(it)
            self.dwn.item_info_dict_all[info.id] = info
            self.dwn.gender_results[info.id] = GenderResult(GenderLabel.ONLY_FEMALE, GenderConf.MEDIUM, source='model')

    def tearDown(self) -> None:
        self.dwn.__exit__(None, None, None)
        self.tmp.cleanup()

    def file(self, name: str, data: bytes = MP4) -> pathlib.Path:
        p = self.dest / name
        p.write_bytes(data)
        return p

    @staticmethod
    def verdict(label: GenderLabel, safety: str = '') -> ModelVerdict:
        return ModelVerdict(GenderResult(label, GenderConf.HIGH, source='model'), safety)

    def test_mismatch_is_moved_not_deleted(self) -> None:
        p = self.file('1.mp4')
        with mock.patch('lynxx.gender_model.verdict_for_media', return_value=(self.verdict(GenderLabel.BOTH), 8)) as vm:
            moved = self.dwn._verify_media_file('1', p)
        vm.assert_called_once()
        self.assertEqual(self.dest / GENDER_MISMATCH_DIR / '1.mp4', moved)
        self.assertFalse(p.exists())
        self.assertEqual(MP4, moved.read_bytes())
        self.assertEqual((1, 1), (self.dwn.gender_verified_count, self.dwn.gender_mismatch_count))
        self.assertEqual('both', str(self.dwn.gender_results['1'].label))
        p = self.file('1.mp4', MP4 + b'again')  # a file of that name is already set aside: never overwritten
        with mock.patch('lynxx.gender_model.verdict_for_media', return_value=(self.verdict(GenderLabel.BOTH), 8)):
            again = self.dwn._verify_media_file('1', p)
        self.assertEqual(self.dest / GENDER_MISMATCH_DIR / '1_1.mp4', again)
        self.assertEqual((MP4, MP4 + b'again'), (moved.read_bytes(), again.read_bytes()))

    def test_match_stays(self) -> None:
        p = self.file('1.mp4')
        with mock.patch('lynxx.gender_model.verdict_for_media', return_value=(self.verdict(GenderLabel.ONLY_FEMALE), 8)):
            self.assertEqual(p, self.dwn._verify_media_file('1', p))
        self.assertTrue(p.exists())
        self.assertEqual((1, 0), (self.dwn.gender_verified_count, self.dwn.gender_mismatch_count))
        self.assertEqual(GenderConf.HIGH, self.dwn.gender_results['1'].conf)

    def test_safety_gate_on_a_frame(self) -> None:
        # with -safety_blacklist the model's minor coded tag on a frame moves the file away
        self.dwn.safety_blacklist = True
        p = self.file('1.mp4')
        with mock.patch('lynxx.gender_model.verdict_for_media', return_value=(self.verdict(GenderLabel.ONLY_FEMALE, 'loli'), 8)):
            moved = self.dwn._verify_media_file('1', p)
        self.assertEqual(self.dest / GENDER_MISMATCH_DIR / '1.mp4', moved)
        self.assertTrue(moved.exists())

    def test_no_safety_gate_without_the_option(self) -> None:
        # the gender filter alone does not turn the model's safety gate on: the frame label decides, only_female is kept
        self.assertFalse(self.dwn.safety_blacklist)
        p = self.file('1.mp4')
        with mock.patch('lynxx.gender_model.verdict_for_media', return_value=(self.verdict(GenderLabel.ONLY_FEMALE, 'loli'), 8)):
            kept = self.dwn._verify_media_file('1', p)
        self.assertEqual(p, kept)
        self.assertTrue(kept.exists())

    def test_not_checked(self) -> None:
        cases = {
            'confident tags': ('2', self.file('2.mp4')),
            'still picture': ('3', self.file('3.jpg', b'\xff\xd8\xff\xe0' + b'\0' * 64)),
            'parent post (not gender filtered)': ('9', self.file('9.mp4')),
        }
        for case, (item_id, p) in cases.items():
            with self.subTest(case), mock.patch('lynxx.gender_model.verdict_for_media') as vm:
                self.assertEqual(p, self.dwn._verify_media_file(item_id, p))
                vm.assert_not_called()
        p = self.file('1.mp4')
        with mock.patch('lynxx.gender_model.verdict_for_media', side_effect=MediaUnreadable('no video decoder here')):
            self.assertEqual(p, self.dwn._verify_media_file('1', p))  # kept, the job goes on
        self.assertTrue(p.exists())
        self.assertEqual((0, 0), (self.dwn.gender_verified_count, self.dwn.gender_mismatch_count))

    def test_server_down_stops_the_run(self) -> None:
        p = self.file('1.mp4')
        with mock.patch('lynxx.gender_model.verdict_for_media', side_effect=ModelUnavailable('model server x is not reachable')), \
                mock.patch('lynxx.network.time.sleep'):
            with self.assertRaises(ThreadInterruptException):
                self.dwn._verify_media_file('1', p)
        self.assertTrue(self.root.killed)
        self.assertIn('not reachable', self.dwn.launch_error)
        self.assertTrue(p.exists())

    def test_server_down_stops_a_threaded_run(self) -> None:
        """a download thread losing the model server stops the whole job with the error (not just its item)"""
        from multiprocessing.dummy import current_process

        from lynxx.lynxx_cmd import EXIT_ERROR, run_exit_code
        raws = self.dwn._get_all_post_tags(_db_posts([(i, 'animated', 'mp4') for i in range(10, 16)]))
        for it in raws:
            info = self.dwn._extract_item_info(it)
            self.dwn.item_info_dict_all[info.id] = info
            self.dwn.gender_results[info.id] = GenderResult(GenderLabel.ONLY_FEMALE, GenderConf.MEDIUM, source='model')

        def download_file(link, item_id, dest, mode, orig_date):
            dest.write_bytes(MP4)
            res = FileDownloadResult()
            res.file_size = res.expected_size = len(MP4)
            return res
        self.dwn.download_file = download_file
        args = prepare_arglist(['-module', 'db', '-gender', 'only_female', '-gender_mode', 'model', '-gender_verify_media', '-threads', '3',
                                '-path', str(self.dest), 'x'])
        try:
            with mock.patch('lynxx.gender_model.backend'), mock.patch('lynxx.gender_model.media_support', return_value=dict(MEDIA)), \
                    mock.patch('lynxx.network.time.sleep'), \
                    mock.patch('lynxx.gender_model.verdict_for_media', side_effect=ModelUnavailable('model server x is not reachable')):
                self.dwn._launch(args, lambda: self.dwn._run_parallel('dl', self.dwn._process_item_safe, [(r,) for r in raws]))
        finally:
            current_process().killed = False
        self.assertEqual((EXIT_ERROR, False), (run_exit_code(self.dwn)[0], self.dwn.interrupted))
        self.assertIn('-gender_verify_media', self.dwn.launch_error)
        self.assertIn('not reachable', self.dwn.launch_error)
        self.assertEqual(0, self.dwn.gender_mismatch_count)
        self.assertFalse((self.dest / GENDER_MISMATCH_DIR).exists())  # nothing moved, nothing deleted

    def test_download_then_check(self) -> None:
        indexed: list[pathlib.Path] = []
        self.dwn.download_mode = DownloadModes.FULL
        self.dwn._index_on = True
        self.dwn._index_file = lambda _id, dest, _size, _link='': indexed.append(dest)

        def download_file(link, item_id, dest, mode, orig_date):
            res = FileDownloadResult()
            if not dest.is_file():
                dest.write_bytes(MP4)
            else:
                res.result_str = 'already exists, '
            res.file_size = res.expected_size = dest.stat().st_size
            return res
        self.dwn.download_file = download_file
        with mock.patch('lynxx.gender_model.verdict_for_media', return_value=(self.verdict(GenderLabel.ONLY_MALE), 8)) as vm:
            self.dwn._download('https://cdn.donmai.us/original/1.mp4', '1', self.dest / '1.mp4')
            self.assertEqual([self.dest / GENDER_MISMATCH_DIR / '1.mp4'], indexed)  # the index records where the file is
            self.assertEqual(1, self.dwn.success_count)
            self.file('4.mp4')
            self.dwn.gender_results['4'] = self.dwn.gender_results['1']
            self.dwn.item_info_dict_all['4'] = self.dwn.item_info_dict_all['1']
            self.dwn._download('https://cdn.donmai.us/original/4.mp4', '4', self.dest / '4.mp4')  # already there: the user's file
            self.assertEqual(1, vm.call_count)
        self.assertTrue((self.dest / '4.mp4').exists())
        # files moved aside count as existing: not downloaded and checked again
        self.dwn.items_raw_per_task[:] = self.dwn._get_all_post_tags(_db_posts([(1, 'animated', 'mp4'), (5, 'animated', 'mp4')]))
        self.dwn.total_count = 2
        self.dwn._filter_existing_items()
        self.assertEqual(['5'], [self.dwn._extract_id(h) for h in self.dwn.items_raw_per_task])

    def test_arguments(self) -> None:
        def parse(*extra: str) -> bool:
            with _make_downloader(ProcModule.DB) as dwn:
                dwn.reset_root_thread(types.SimpleNamespace(killed=False))
                dwn.no_conflict_delay = True
                dwn._parse_args(prepare_arglist(['-module', 'db', *extra, '-gender_verify_media', 'x']), False)
                return dwn.gender_verify_media
        self.assertFalse(parse())  # no gender filter: ignored
        self.assertFalse(parse('-gender', 'only_female'))  # tags only: ignored
        with mock.patch('lynxx.gender_model.backend'), mock.patch('lynxx.gender_model.media_support', return_value=dict(MEDIA)):
            self.assertTrue(parse('-gender', 'only_female', '-gender_mode', 'model'))
            self.assertFalse(parse('-gender', 'only_female', '-gender_mode', 'model', '-dmode', 'touch'))
        with mock.patch('lynxx.gender_model.backend'), mock.patch('lynxx.network.time.sleep'), \
                mock.patch('lynxx.gender_model.media_support', side_effect=ModelUnavailable('cannot check videos: update it')):
            with self.assertRaises(ThreadInterruptException):
                parse('-gender', 'only_female', '-gender_mode', 'model')
        a = prepare_arglist(['-module', 'db', '-gender', 'only_female', '-gender_mode', 'model', '-gender_verify_media', 'x'])
        self.assertTrue(a.gender_verify_media)
        self.assertFalse(prepare_arglist(['-module', 'db', 'x']).gender_verify_media)

#
#
#########################################
