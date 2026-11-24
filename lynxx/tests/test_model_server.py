# coding=UTF-8
"""
Lynxx: gender model server (lynxx/model_server.py) and its client (lynxx/gender_model.py: ModelApi, backend, verdicts_for),
the hardware check before the app turns the model on (lynxx/model_hw.py), GPU / CPU choice of the tagger.
A stub stands for the model: no numpy / onnxruntime / model file needed
"""
#########################################
#
#

from __future__ import annotations

import http.client
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import types
from unittest import TestCase, mock

from lynxx import gender_model, model_hw
from lynxx.gender_model import (
    MODEL_FILES,
    GenderTagger,
    ModelApi,
    ModelUnavailable,
    VerdictCache,
    api_status,
    backend,
    check_api_url,
    classify,
    verdicts_for,
)
from lynxx.model_hw import VRAM_MIN_MIB, drm_gpus, hardware_check, nvidia_gpus
from lynxx.model_server import ModelProcess, check_bind, describe_hardware, make_server

PNG = b'\x89PNG\r\n\x1a\n' + b'\0' * 32
JPEG = b'\xff\xd8\xff\xe0' + b'\0' * 32
TOKEN = 'test-token-1234567890'
RTX = {'vendor': 'nvidia', 'name': 'NVIDIA GeForce RTX 4070 Ti SUPER', 'vramMiB': 16376, 'vramFreeMiB': 15249, 'shared': False}
APU = {'vendor': 'amd', 'name': 'AMD GPU 0x164e', 'vramMiB': 512, 'vramFreeMiB': 496, 'shared': False}
CPU_ONLY = ['AzureExecutionProvider', 'CPUExecutionProvider']
CUDA = ['CUDAExecutionProvider', 'CPUExecutionProvider']


def _hw(gpus: list[dict], providers: list[str]) -> dict:
    return {'gpus': gpus, 'providers': providers, 'runtime': '1.32.0', 'ramMiB': 30728, 'ramFreeMiB': 20938, 'cpus': 16, 'os': 'linux'}


HW_NONE = _hw([APU], CPU_ONLY)


class _StubTagger:
    model_id = 'stub-tagger-1'

    def __init__(self) -> None:
        self.calls = 0

    def predict(self, image: bytes) -> dict[str, float]:
        self.calls += 1
        if image.startswith(PNG[:8]):
            return {'1girl': 0.97, 'solo': 0.95, 'long_hair': 0.6}
        if image.startswith(JPEG[:4]):
            return {'no_humans': 0.93, 'scenery': 0.9, 'loli': 0.3}
        raise OSError('cannot identify image file')


def _stub_factory() -> _StubTagger:  # model of a ModelProcess in tests (spawn: the child imports this module by name)
    return _StubTagger()


def _no_gpu_factory():
    raise ModelUnavailable('no GPU runtime: onnxruntime here is a CPU build')


class _Crashing(_StubTagger):
    """a JPEG kills the model process (as the kernel would on out of memory)"""
    def predict(self, image: bytes) -> dict[str, float]:
        if image.startswith(JPEG[:4]):
            os._exit(9)
        if image == b'boom':
            raise RuntimeError('CUDA out of memory')
        return super().predict(image)


def _crash_factory() -> _Crashing:
    return _Crashing()


class _Server:
    def __init__(self, token: str = TOKEN, workers: int = 2, **kwargs) -> None:
        self.tagger = _StubTagger()
        if 'loader' not in kwargs:
            kwargs['tagger'] = self.tagger
        kwargs.setdefault('hardware', lambda: HW_NONE)
        self.srv = make_server('127.0.0.1', 0, token=token, workers=workers, quiet=True, **kwargs)
        self.srv.log = lambda _text: None  # model loaded / unloaded lines
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


class ApiUrlTests(TestCase):
    def test_check_api_url(self) -> None:
        self.assertEqual('http://127.0.0.1:8765', check_api_url(' http://127.0.0.1:8765/ '))
        self.assertEqual('http://[::1]:8765', check_api_url('http://[::1]:8765'))
        self.assertEqual('http://192.168.1.20:8765', check_api_url('http://192.168.1.20:8765'))
        self.assertEqual('http://nas.local:8765', check_api_url('http://nas.local:8765'))
        self.assertEqual('https://models.example.com/lynxx', check_api_url('https://models.example.com/lynxx/'))
        for bad in ('http://models.example.com:8765',  # plain http over the internet: pictures and token in clear
                    'ftp://127.0.0.1', '127.0.0.1:8765', 'http://user:pw@127.0.0.1:8765', 'http://127.0.0.1:8765/?x=1',
                    'http://127.0.0.1:99999', 'http://8.8.8.8:8765', ''):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                check_api_url(bad)

    def test_bind_needs_a_token_off_this_computer(self) -> None:
        self.assertEqual('', check_bind('127.0.0.1', '', False))
        self.assertEqual('', check_bind('::1', '', False))
        self.assertIn('token', check_bind('0.0.0.0', '', False))
        self.assertEqual('', check_bind('0.0.0.0', TOKEN, False))
        self.assertEqual('', check_bind('0.0.0.0', '', True))


class ModelServerTests(TestCase):
    def test_status_and_token(self) -> None:
        with _Server() as s:
            with self.assertRaisesRegex(ModelUnavailable, 'token'):
                ModelApi(s.url).status()
            with self.assertRaisesRegex(ModelUnavailable, 'token'):
                ModelApi(s.url, 'wrong-token-123').status()
            info = ModelApi(s.url, TOKEN).status()
            self.assertEqual(('stub-tagger-1', 'Apache-2.0', 'lynxx-model/1'), (info['id'], info['license'], info['server']))
            self.assertEqual(0, s.tagger.calls)  # a refused request never reaches the model

    def test_tag_pictures(self) -> None:
        with _Server() as s:
            api = ModelApi(s.url, TOKEN)
            api.status()
            self.assertEqual('stub-tagger-1', api.model_id)
            probs = api.predict(PNG)
            self.assertEqual(0.97, probs['1girl'])
            self.assertEqual('only_female', str(classify(probs).result.label))
            self.assertEqual('loli', classify(api.predict(JPEG)).safety_tag)  # the safety gate runs on the client
            self.assertEqual({}, api.predict(b'not a picture'))  # 400: this post only, no verdict

    def test_raw_http_errors(self) -> None:
        with _Server() as s:
            host, port = s.srv.server_address[:2]
            auth = {'Authorization': f'Bearer {TOKEN}'}

            def call(method: str, path: str, headers: dict, body: bytes | None = None) -> tuple[int, dict]:
                c = http.client.HTTPConnection(host, port, timeout=5)
                try:
                    c.putrequest(method, path)
                    for k, v in headers.items():
                        c.putheader(k, v)
                    c.endheaders()
                    if body:
                        c.send(body)
                    r = c.getresponse()
                    return r.status, json.loads(r.read() or b'{}')
                finally:
                    c.close()
            self.assertEqual(413, call('POST', '/v1/tag', {**auth, 'Content-Length': str(64 * 1024 * 1024)})[0])  # body never read
            self.assertEqual(411, call('POST', '/v1/tag', auth)[0])
            self.assertEqual(404, call('GET', '/v1/other', auth)[0])
            self.assertEqual(404, call('POST', '/v2/tag', {**auth, 'Content-Length': '0'})[0])
            self.assertEqual(405, call('PUT', '/v1/tag', {**auth, 'Content-Length': '0'})[0])
            self.assertEqual(401, call('POST', '/v1/tag', {'Content-Length': str(len(PNG))}, PNG)[0])
            code, body = call('POST', '/v1/tag', {**auth, 'Content-Length': str(len(PNG))}, PNG)
            self.assertEqual((200, 'stub-tagger-1'), (code, body['id']))
            self.assertEqual(1, s.tagger.calls)

    def test_unreachable_server(self) -> None:
        with _Server() as s:
            url = s.url
        st = api_status(url, TOKEN)
        self.assertFalse(st['reachable'])
        self.assertIn('not reachable', st['error'])
        self.assertTrue(st['local'])
        self.assertIn('https', api_status('http://models.example.com:8765')['error'])


class _FakeDownloader:
    """what verdicts_for uses of a downloader"""
    def __init__(self, url: str, token: str) -> None:
        self.gender_model_url = url
        self.gender_model_token = token

    @staticmethod
    def _get_module_abbr() -> str:
        return 'zz'

    @staticmethod
    def _media_kind(raw: str) -> str:
        return 'video' if raw.endswith('.mp4') else 'image'


class ClientBackendTests(TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = VerdictCache(pathlib.Path(self.tmp.name) / 'gm.sqlite')
        gender_model._api_clients.clear()

    def tearDown(self) -> None:
        self.cache.close()
        self.tmp.cleanup()
        gender_model._api_clients.clear()

    def test_verdicts_through_the_server_are_cached(self) -> None:
        with _Server() as s, mock.patch.object(gender_model, '_shared_cache', lambda: self.cache), \
                mock.patch.object(gender_model, 'image_url_for', lambda _d, raw: f'https://img.example/{raw}'), \
                mock.patch.object(gender_model, '_fetch_image', lambda _d, url: PNG if url.endswith(('.png', '.mp4')) else b'') as _:
            dwn = _FakeDownloader(s.url, TOKEN)
            out = verdicts_for(dwn, [('1', 'a.png'), ('2', 'b.mp4'), ('3', 'gone.png.missing')])
            self.assertEqual({'1', '2'}, set(out))  # no picture: no verdict, the post stays unknown
            self.assertEqual(('only_female', 'model'), (str(out['1'].result.label), out['1'].result.source))
            self.assertEqual('MEDIUM', out['2'].result.conf.name)  # a video poster frame: at most medium
            calls = s.tagger.calls
            again = verdicts_for(dwn, [('1', 'a.png'), ('2', 'b.mp4')])
            self.assertEqual(calls, s.tagger.calls)  # cached per post and model id
            self.assertEqual(str(out['1'].result.label), str(again['1'].result.label))
            self.assertIsNotNone(self.cache.get('zz:1', gender_model.verdict_cache_model('stub-tagger-1')))
            self.assertIsNone(self.cache.get('zz:1', gender_model.verdict_cache_model(gender_model.MODEL_ID)))  # another model: not reused

    def test_cached_verdicts_follow_the_rules(self) -> None:
        # the cache keeps labels, not the predicted tags: a verdict made before a change of the 'db' rules is made again
        # (here: female body parts became female evidence; before, 1boy + penis + breasts was only_male)
        from lynxx.gender import RULES_DIR, GenderRules

        class Tagger(_StubTagger):
            def predict(self, image: bytes) -> dict[str, float]:
                self.calls += 1
                return {'1boy': 0.95, 'penis': 0.93, 'breasts': 0.9, '1girl': 0.2}
        tagger = Tagger()
        raw = json.loads((RULES_DIR / 'db.json').read_text(encoding='utf-8'))
        raw.pop('femaleBodypart', None)
        old_rules = GenderRules.from_dict(raw, 'db')
        dwn = _FakeDownloader('', '')
        with mock.patch.object(gender_model, '_shared_cache', lambda: self.cache), \
                mock.patch.object(gender_model, 'backend', lambda *_: tagger), \
                mock.patch.object(gender_model, 'image_url_for', lambda _d, raw: 'https://img.example/x.png'), \
                mock.patch.object(gender_model, '_fetch_image', lambda _d, url: PNG):
            with mock.patch.object(gender_model, 'rules_for_module', lambda _abbr: old_rules):
                self.assertEqual('only_male', str(verdicts_for(dwn, [('1', 'a.png')])['1'].result.label))
                self.assertEqual('only_male', str(verdicts_for(dwn, [('1', 'a.png')])['1'].result.label))
            self.assertEqual(1, tagger.calls, 'same rules: cached')
            self.assertEqual('both', str(verdicts_for(dwn, [('1', 'a.png')])['1'].result.label))
            self.assertEqual(2, tagger.calls, 'other rules: looked at again')
            self.assertEqual('both', str(verdicts_for(dwn, [('1', 'a.png')])['1'].result.label))
            self.assertEqual(2, tagger.calls)

    def test_server_down_stops_the_filter(self) -> None:
        with _Server() as s:
            url = s.url
        dwn = _FakeDownloader(url, TOKEN)
        # a client that once reached the server (an in-process server keeps serving its open keep-alive connections, so the
        # client is set up without one); the server process is gone now
        gender_model._api_clients[(url, TOKEN)] = ModelApi(url, TOKEN)
        with mock.patch.object(gender_model, '_shared_cache', lambda: self.cache), \
                mock.patch.object(gender_model, 'image_url_for', lambda _d, raw: 'https://img.example/x.png'), \
                mock.patch.object(gender_model, '_fetch_image', lambda _d, url: PNG):
            with self.assertRaisesRegex(ModelUnavailable, 'not reachable'):
                verdicts_for(dwn, [('1', 'a.png')])  # every post unknown silently would look like a working filter

    def test_backend_choice(self) -> None:
        with _Server() as s:
            self.assertIsInstance(backend(s.url, TOKEN), ModelApi)
            with mock.patch.dict(os.environ, {gender_model.ENV_MODEL_URL: s.url, gender_model.ENV_MODEL_TOKEN: TOKEN}):
                self.assertIsInstance(backend(), ModelApi)
        with self.assertRaisesRegex(ModelUnavailable, 'https'):
            backend('http://models.example.com:8765')

    def test_client_needs_no_model_runtime(self) -> None:
        """the desktop app core is built without numpy / onnxruntime / Pillow: the API path must not import them"""
        with _Server() as s:
            blocked = dict.fromkeys(('numpy', 'onnxruntime', 'PIL'))
            with mock.patch.dict(sys.modules, blocked):
                api = ModelApi(s.url, TOKEN)
                api.status()
                self.assertEqual('only_female', str(classify(api.predict(PNG)).result.label))

    def test_cli_arguments(self) -> None:
        from lynxx.cmdargs import prepare_arglist
        a = prepare_arglist(['-module', 'rx', '-gender', 'only_female', '-gender_mode', 'model', '-gender_model_url',
                             'http://127.0.0.1:8765/', '-gender_model_token', TOKEN, 'x'])
        self.assertEqual(('http://127.0.0.1:8765', TOKEN), (a.gender_model_url, a.gender_model_token))
        for bad in (['-gender_model_url', 'http://example.com'], ['-gender_model_token', 'short']):
            with self.subTest(bad=bad), self.assertRaises(SystemExit), mock.patch('sys.stderr'):
                prepare_arglist(['-module', 'rx', *bad, 'x'])



class HardwareTests(TestCase):
    def test_nvidia_smi(self) -> None:
        out = 'NVIDIA GeForce RTX 4070 Ti SUPER, 16376, 15249\nOdd, name, 8192, 100\n'
        run = mock.Mock(return_value=types.SimpleNamespace(returncode=0, stdout=out))
        with mock.patch.object(model_hw.shutil, 'which', return_value='/usr/bin/nvidia-smi'):
            gpus = nvidia_gpus(run)
            self.assertEqual([RTX, {'vendor': 'nvidia', 'name': 'Odd, name', 'vramMiB': 8192, 'vramFreeMiB': 100, 'shared': False}], gpus)
            self.assertEqual([], nvidia_gpus(mock.Mock(return_value=types.SimpleNamespace(returncode=9, stdout=out))))
            self.assertEqual([], nvidia_gpus(mock.Mock(side_effect=OSError)))
        with mock.patch.object(model_hw.shutil, 'which', return_value=None):
            self.assertEqual([], nvidia_gpus(run))

    def test_linux_sysfs(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            def card(name: str, files: dict[str, str]) -> None:
                dev = pathlib.Path(d, name, 'device')
                dev.mkdir(parents=True)
                for k, v in files.items():
                    (dev / k).write_text(v)
            card('card0', {'vendor': '0x1002', 'device': '0x744c', 'mem_info_vram_total': str(8 << 30), 'mem_info_vram_used': str(1 << 30)})
            card('card1', {'vendor': '0x8086', 'device': '0xa780'})
            card('card2', {'vendor': '0x10de', 'device': '0x2705'})
            pathlib.Path(d, 'card0-DP-1').mkdir()  # a connector, not a card
            gpus = drm_gpus(d)
            self.assertEqual([('amd', 8192, 7168, False), ('intel', None, None, True)],
                             [(g['vendor'], g['vramMiB'], g['vramFreeMiB'], g['shared']) for g in gpus])
            self.assertEqual('AMD GPU 0x744c', gpus[0]['name'])
            every = drm_gpus(d, skip=())  # no nvidia-smi: the NVIDIA card is listed, VRAM unknown
            self.assertEqual(('NVIDIA GPU 0x2705', None, False), (every[2]['name'], every[2]['vramMiB'], every[2]['shared']))
        self.assertEqual([], drm_gpus('/nonexistent/drm'))

    def test_check(self) -> None:
        codes = {
            'no GPU': (hardware_check(_hw([], CPU_ONLY)), 'no_gpu'),
            'APU only': (hardware_check(HW_NONE), 'no_gpu'),
            'GPU, CPU build of onnxruntime': (hardware_check(_hw([RTX, APU], CPU_ONLY)), 'gpu_no_runtime'),
            'GPU and CUDA': (hardware_check(_hw([APU, RTX], CUDA)), 'gpu'),
            'VRAM in use': (hardware_check(_hw([{**RTX, 'vramFreeMiB': VRAM_MIN_MIB - 1}], CUDA)), 'vram_low'),
            'loaded on CUDA': (hardware_check(_hw([{**RTX, 'vramFreeMiB': 10}], CUDA), 'cuda'), 'gpu'),
            'fell back to the CPU': (hardware_check(_hw([RTX], CUDA), 'cpu'), 'gpu_fallback'),
            'told to use the CPU': (hardware_check(_hw([RTX], CUDA), '', 'cpu'), 'cpu_forced'),
            'told to use the CPU, loaded': (hardware_check(_hw([RTX], CUDA), 'cpu', 'cpu'), 'cpu_forced'),
            'told to use the CPU, no GPU': (hardware_check(_hw([], CPU_ONLY), '', 'cpu'), 'no_gpu'),
            'CUDA without NVIDIA': (hardware_check(_hw([{**APU, 'vramMiB': 8192, 'vramFreeMiB': 8000}], CUDA)), 'gpu_no_runtime'),
            'old server': (hardware_check(None), 'unknown'),
        }
        for case, (got, code) in codes.items():
            with self.subTest(case):
                self.assertEqual(code, got['code'])
                self.assertEqual('ok' if code == 'gpu' else 'warn', got['level'])
        self.assertEqual('NVIDIA GeForce RTX 4070 Ti SUPER', hardware_check(_hw([APU, RTX], CPU_ONLY))['gpu']['name'])
        self.assertEqual('cuda', hardware_check(_hw([RTX], CUDA))['runtime'])
        self.assertIn('CPU only', describe_hardware(_hw([RTX], CPU_ONLY)))
        unread = {'vendor': 'nvidia', 'name': 'NVIDIA GPU 0x2689', 'vramMiB': None, 'vramFreeMiB': None, 'shared': False}
        igpu = {'vendor': 'intel', 'name': 'Intel GPU 0xa780', 'vramMiB': None, 'vramFreeMiB': None, 'shared': True}
        line = describe_hardware(_hw([unread, igpu, APU], CPU_ONLY))  # a CPU container on an NVIDIA machine
        self.assertIn('NVIDIA GPU 0x2689 VRAM unknown (no nvidia-smi)', line)
        self.assertIn('Intel GPU 0xa780 shared memory', line)
        self.assertIn('AMD GPU 0x164e 496/512 MiB VRAM free', line)
        self.assertEqual('no_gpu', hardware_check(_hw([unread], CPU_ONLY))['code'])
        self.assertIn('with cuda', describe_hardware(_hw([RTX], CUDA)))


class _FakeSession:
    def __init__(self, path: str, sess_options=None, providers: list[str] | None = None) -> None:
        self.asked = list(providers or [])
        self.loaded = [p for p in self.asked if p not in _FakeSession.broken]

    broken: set[str] = set()

    def get_providers(self) -> list[str]:
        return self.loaded

    def get_inputs(self) -> list:
        return [types.SimpleNamespace(name='input')]


class TaggerDeviceTests(TestCase):
    """GPU when onnxruntime has a GPU provider, the CPU otherwise; device 'gpu' never runs silently on the CPU"""
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        for name in MODEL_FILES:
            (self.dir / name).write_text('name,category\n1girl,0\n' if name.endswith('.csv') else 'x')
        self.ort = types.SimpleNamespace(SessionOptions=types.SimpleNamespace, InferenceSession=_FakeSession,
                                         get_available_providers=lambda: list(self.providers))
        self.providers = CPU_ONLY
        _FakeSession.broken = set()
        self.patches = [mock.patch.dict(sys.modules, {'onnxruntime': self.ort}),
                        mock.patch.object(gender_model, 'runtime_available', lambda: '')]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_device_choice(self) -> None:
        t = GenderTagger(self.dir)
        self.assertEqual(('cpu', ['CPUExecutionProvider']), (t.device, t.session.asked))
        with self.assertRaisesRegex(ModelUnavailable, 'no GPU runtime'):
            GenderTagger(self.dir, 'gpu')
        self.providers = CUDA
        t = GenderTagger(self.dir)
        self.assertEqual(('cuda', CUDA), (t.device, t.session.asked))
        self.assertEqual(['CPUExecutionProvider'], GenderTagger(self.dir, 'cpu').session.asked)
        _FakeSession.broken = {'CUDAExecutionProvider'}  # CUDA / cuDNN libraries missing: onnxruntime falls back to the CPU
        self.assertEqual('cpu', GenderTagger(self.dir).device)
        with self.assertRaisesRegex(ModelUnavailable, 'did not load'):
            GenderTagger(self.dir, 'gpu')
        with self.assertRaisesRegex(ModelUnavailable, 'device'):
            GenderTagger(self.dir, 'tpu')


class LoadUnloadTests(TestCase):
    def test_lazy_load_unload_idle(self) -> None:
        stub = _StubTagger()
        loads = []

        def loader():
            loads.append(1)
            return stub
        with _Server(loader=loader, idle_unload=60) as s:
            api = ModelApi(s.url, TOKEN)
            st = api.status()
            self.assertEqual((False, '', 60, 'no_gpu'), (st['loaded'], st['device'], st['idleUnload'],
                                                          hardware_check(st['hardware'])['code']))
            self.assertEqual(0, len(loads))  # nothing loaded before the first picture
            self.assertEqual(0.97, api.predict(PNG)['1girl'])
            self.assertEqual((1, True, 'cpu'), (len(loads), api.status()['loaded'], api.status()['device']))
            self.assertFalse(api.unload()['loaded'])
            api.predict(PNG)
            self.assertEqual(2, len(loads))  # loaded again when needed
            self.assertFalse(s.srv.unload_idle())  # used just now
            self.assertTrue(s.srv.unload_idle(time.monotonic() + 61))
            self.assertTrue(api.load()['loaded'])
            self.assertEqual(3, len(loads))
            st = api_status(s.url, TOKEN, 'unload')
            self.assertEqual((True, False, 'no_gpu', ''), (st['reachable'], st['model']['loaded'], st['check']['code'], st['error']))

    def test_load_errors(self) -> None:
        def loader():
            raise ModelUnavailable('no GPU runtime: onnxruntime here is a CPU build')
        with _Server(loader=loader) as s:
            api = ModelApi(s.url, TOKEN)
            t0 = time.monotonic()
            with self.assertRaisesRegex(ModelUnavailable, 'HTTP 503: no GPU runtime'):
                api.load()
            self.assertLess(time.monotonic() - t0, 1)  # not retried like a busy server
            with self.assertRaisesRegex(ModelUnavailable, 'no GPU runtime'):
                api.predict(PNG)  # a job stops: every picture would fail
            st = api_status(s.url, TOKEN, 'load')
            self.assertTrue(st['reachable'])
            self.assertIn('no GPU runtime', st['error'])
            host, port = s.srv.server_address[:2]
            c = http.client.HTTPConnection(host, port, timeout=5)
            c.request('POST', '/v1/load', body=b'x', headers={'Authorization': f'Bearer {TOKEN}'})
            self.assertEqual(400, c.getresponse().status)
            c.close()

    def test_model_error_is_not_a_bad_picture(self) -> None:
        class Broken(_StubTagger):
            def predict(self, image: bytes) -> dict[str, float]:
                raise RuntimeError('CUDA out of memory')
        with _Server(loader=Broken) as s:
            with self.assertRaisesRegex(ModelUnavailable, r'HTTP 500: model error \(RuntimeError\)'):
                ModelApi(s.url, TOKEN).predict(PNG)

    def test_status_reports_gpu(self) -> None:
        class OnGpu(_StubTagger):
            device = 'cuda'
        with _Server(loader=OnGpu, hardware=lambda: _hw([RTX], CUDA)) as s:
            before = api_status(s.url, TOKEN)
            after = api_status(s.url, TOKEN, 'load')
            self.assertEqual(('gpu', '', 'cuda'), (before['check']['code'], before['model']['device'], after['model']['device']))
            self.assertEqual('NVIDIA GeForce RTX 4070 Ti SUPER', after['check']['gpu']['name'])

    def test_model_process(self) -> None:
        """the model in a child process: an unload ends it (the only way CUDA hands back its memory)"""
        mp = ModelProcess(factory=_stub_factory)
        try:
            self.assertTrue(mp.alive)
            self.assertNotEqual(os.getpid(), mp.pid)
            self.assertEqual('cpu', mp.device)
            self.assertEqual(0.97, mp.predict(PNG)['1girl'])
            with self.assertRaises(gender_model.BadImage):
                mp.predict(b'not a picture')
            results = []
            threads = [threading.Thread(target=lambda: results.append(mp.predict(JPEG)['no_humans'])) for _ in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertEqual([0.93] * 6, results)
        finally:
            mp.close()
        self.assertFalse(mp.alive)
        with self.assertRaisesRegex(ModelUnavailable, 'unloaded'):
            mp.predict(PNG)
        with self.assertRaisesRegex(ModelUnavailable, 'no GPU runtime'):
            ModelProcess(factory=_no_gpu_factory)

    def test_model_process_dies(self) -> None:
        loads = []

        def loader():
            loads.append(ModelProcess(factory=_crash_factory))
            return loads[-1]
        with _Server(loader=loader) as s:
            api = ModelApi(s.url, TOKEN)
            self.assertEqual(0.97, api.predict(PNG)['1girl'])
            with self.assertRaisesRegex(ModelUnavailable, 'HTTP 503: the model process exited'):
                api.predict(JPEG)  # dies, started again once, dies again: the client stops
            self.assertEqual(2, len(loads))
            self.assertEqual(0.97, api.predict(PNG)['1girl'])  # the next picture starts a new process
            self.assertEqual(3, len(loads))
            with self.assertRaisesRegex(ModelUnavailable, r'HTTP 500: model error \(RuntimeError\)'):
                api.predict(b'boom')
            proc = loads[-1]
            self.assertTrue(proc.alive)
            api.unload()
            for _ in range(100):  # closed in the background
                if not proc.alive:
                    break
                time.sleep(0.05)
            self.assertFalse(proc.alive)
            self.assertTrue(all(not p.alive for p in loads))

    def test_device_setting_in_status(self) -> None:
        srv = make_server('127.0.0.1', 0, device='cpu', quiet=True, hardware=lambda: _hw([RTX], CUDA))
        try:
            st = srv.status()
            self.assertEqual(('cpu', False), (st['deviceSetting'], st['loaded']))  # nothing loaded to tell it
        finally:
            srv.server_close()
        with _Server() as s:
            self.assertEqual('', ModelApi(s.url, TOKEN).status()['deviceSetting'])

    def test_rpc(self) -> None:
        from lynxx.server import RpcError, m_gender_model_load, m_gender_model_unload
        for m in (m_gender_model_load, m_gender_model_unload):
            with self.subTest(m.__name__), self.assertRaises(RpcError):
                m({'url': ''})
            with self.subTest(m.__name__), self.assertRaises(RpcError):
                m({'url': 1})
        self.assertIn('not reachable', m_gender_model_load({'url': 'http://127.0.0.1:9'})['error'])

#
#
#########################################
