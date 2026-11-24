# coding=UTF-8
"""
Lynxx: tests of the extension generator eval harness (lynxx.aigen.eval), offline.
Fixtures are recorded from the fake sites of test_extgen (and from a local HTTP server through the real requests library),
then replayed in the template mode, the simulated model mode and the ai mode with a scripted client. No paid API call.
Run: python -m unittest lynxx.tests.test_aigen_eval
"""
#########################################
#
#

import contextlib
import http.server
import json
import os
import pathlib
import shutil
import tempfile
import threading
from unittest import TestCase, mock

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx.aigen import eval as ev  # noqa: E402
from lynxx.aigen.session import GenRequest, GenSession  # noqa: E402
from lynxx.extgen.fetch import SafeFetcher  # noqa: E402
from lynxx.extgen.templates import render_downloader  # noqa: E402
from lynxx.tests.test_extgen import HOST, SITE, FakeJsonSite, FakeSession, ScriptedClient, _usage, fake_gelbooru, tool  # noqa: E402

FAKE_KEY = 'sk-ant-eval-FAKE-KEY-0123456789abcdef'


def _record(folder: pathlib.Path, name: str, route=fake_gelbooru) -> pathlib.Path:
    """records one template session against a fake site into folder/name (like --live --record)"""
    recorder = ev.Recorder(folder / name, SITE)
    fetcher = SafeFetcher(allowed_hosts={HOST}, min_interval=0.0, allow_local=True)
    inner = FakeSession(route)
    if isinstance(route, FakeJsonSite):
        route.session = inner
    fetcher._session = ev.RecordingSession(inner, recorder)
    with tempfile.TemporaryDirectory() as tmp:
        report = GenSession(GenRequest(url=SITE, mode='template'), drafts_root=pathlib.Path(tmp), fetcher=fetcher).run()
    assert report['status'] == 'ready', report['summary']
    return folder / name


class EvalHarnessTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.fixtures = self.root / 'fixtures'

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _targets(self) -> list:
        return [ev.Target('fakebooru', SITE, {'status': 'ready', 'engine': 'gelbooru_dapi'}, media=['image']),
                ev.Target('denied', 'https://m.youtube.com/', {'status': 'refused', 'ruleIds': ['R7']})]

    def test_suite_file_and_validation(self) -> None:
        targets = ev.load_suite(ev.DEFAULT_SUITE)
        self.assertGreaterEqual(len(targets), 6)
        self.assertTrue(any(t.expect.get('ruleIds') == ['R7'] for t in targets))
        self.assertTrue(any(t.modes == ['ai'] for t in targets))
        bad = self.root / 'bad.json'
        for doc in ({'targets': []}, {'targets': [{'name': 'Bad Name', 'url': SITE, 'expect': {'status': 'ready'}}]},
                    {'targets': [{'name': 'a', 'url': SITE, 'expect': {}}]},
                    {'targets': [{'name': 'a', 'url': SITE, 'expect': {'status': 'ready'}, 'modes': ['magic']}]}):
            bad.write_text(json.dumps(doc), encoding='utf-8')
            with self.assertRaises(ValueError, msg=doc):
                ev.load_suite(bad)

    def test_template_mode_replays_recorded_fixtures_offline(self) -> None:
        folder = _record(self.fixtures, 'fakebooru')
        index = json.loads((folder / 'index.json').read_text())
        self.assertEqual('lynxx-eval-fixtures/1', index['format'])
        self.assertTrue(any(r['url'].endswith('/robots.txt') for r in index['responses']))
        with mock.patch('socket.socket', side_effect=OSError('network is disabled in this test')):
            report = ev.run_suite(self._targets(), mode='template', out_dir=self.root / 'out', fixtures=self.fixtures)
        s = report['summary']
        self.assertEqual((2, 2, 1.0), (s['targets'], s['passed'], s['successRate']), report['targets'])
        self.assertEqual(0, s['missingRequests'])
        self.assertEqual(0.0, s['totalUsd'])  # no model
        fake, denied = report['targets']
        self.assertEqual(('ready', 'gelbooru_dapi'), (fake['status'], fake['engine']))
        self.assertGreater(fake['steps'], 3)
        self.assertGreater(fake['siteRequests'], 2)
        self.assertEqual({'survey_site', 'create_draft', 'static_check', 'http_fetch', 'test_run', 'finish'}, set(fake['tools']))
        self.assertEqual(('refused', ['R7'], 0), (denied['status'], denied['ruleIds'], denied['siteRequests']))
        self.assertTrue(all(c['ok'] for c in fake['checks']), fake['checks'])
        self.assertTrue((self.root / 'out' / 'report.json').is_file())
        md = (self.root / 'out' / 'report.md').read_text(encoding='utf-8')
        self.assertIn('Đạt: **2 / 2**', md)
        self.assertIn('| fakebooru | đạt |', md)

    def test_replay_reports_missing_requests(self) -> None:
        (self.fixtures / 'fakebooru').mkdir(parents=True)  # nothing recorded: every request is a 404 'not recorded'
        report = ev.run_suite(self._targets()[:1], mode='template', out_dir=self.root / 'out', fixtures=self.fixtures)
        row = report['targets'][0]
        self.assertFalse(row['ok'])
        self.assertTrue(row['missing'])
        self.assertTrue(any(f.startswith('status:') for f in row['failures']))
        self.assertEqual(len(row['missing']), report['summary']['missingRequests'])

    def test_simulated_model_cost_accounting(self) -> None:
        _record(self.fixtures, 'fakebooru')
        report = ev.run_suite(self._targets()[:1], mode='fake', out_dir=self.root / 'out', fixtures=self.fixtures)
        row = report['targets'][0]
        self.assertTrue(row['ok'], row['failures'])
        self.assertGreater(row['usd'], 0)
        self.assertEqual(row['turns'], row['usage']['requests'])
        self.assertGreater(row['usage']['cacheRead'], 0)  # the simulated model caches the prefix like the API
        self.assertEqual('simulated', report['model'])
        self.assertAlmostEqual(report['summary']['totalUsd'], row['usd'], places=4)

    def test_ai_mode_with_a_scripted_client_and_secret_check(self) -> None:
        _record(self.fixtures, 'fakebooru')
        good = render_downloader('gelbooru_dapi', ext_id='fakeai', abbr='zq9', site=SITE)

        def script(leak: bool):
            return ScriptedClient([
                tool('survey_site', usage=_usage(2000, 300)),
                tool('create_draft', {'engine': 'gelbooru_dapi', 'id': 'fakeai', 'abbr': 'zq9', 'name': 'Fake AI', 'media': ['image'],
                                      'description': ''}),
                tool('write_file', {'path': 'specs.md', 'content': f'notes {FAKE_KEY if leak else ""}\n'}),
                tool('write_file', {'path': 'downloader.py', 'content': good}),
                tool('test_run', {'query': 'scenery', 'limit': 2}),
                tool('finish', {'status': 'ready', 'summary': 'Xong.', 'rule_ids': []}),
            ])
        clients = []

        def factory(_t):
            clients.append(script(leak=len(clients) == 1))
            return clients[-1]
        targets = [self._targets()[0], ev.Target('fakebooru-leak', SITE, {'status': 'ready'})]
        shutil.copytree(self.fixtures / 'fakebooru', self.fixtures / 'fakebooru-leak')
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': FAKE_KEY}):
            report = ev.run_suite(targets, mode='ai', out_dir=self.root / 'out', fixtures=self.fixtures, client_factory=factory)
        ok_row, leak_row = report['targets']
        self.assertTrue(ok_row['ok'], ok_row['failures'])
        self.assertEqual(6, ok_row['turns'])
        self.assertEqual(6, ok_row['steps'])
        self.assertGreater(ok_row['usd'], 0)
        self.assertFalse(leak_row['ok'])
        self.assertTrue(any(f.startswith('noSecret') for f in leak_row['failures']), leak_row['failures'])
        self.assertNotIn(FAKE_KEY, (self.root / 'out' / 'report.json').read_text(encoding='utf-8'))
        self.assertEqual(1, report['summary']['passed'])

    def test_total_cost_cap_skips_the_rest(self) -> None:
        _record(self.fixtures, 'fakebooru')
        targets = [self._targets()[0], ev.Target('second', SITE, {'status': 'ready'})]
        report = ev.run_suite(targets, mode='fake', out_dir=self.root / 'out', fixtures=self.fixtures, max_total_usd=0.0001)
        self.assertEqual(['ready', 'skipped'], [r['status'] for r in report['targets']])
        self.assertIn('cost cap', report['targets'][1]['failures'][0])

    def test_ai_mode_needs_explicit_cost_confirmation(self) -> None:
        with mock.patch.object(ev, '_anthropic_factory', side_effect=AssertionError('no client without --confirm-cost')), \
                mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': str(self.root / 'data')}):
            code = ev.main(['run', '--mode', 'ai', '--fixtures', str(self.fixtures), '--only', 'safebooru'])
        self.assertEqual(2, code)
        self.assertFalse((self.root / 'data' / 'extgen' / 'eval').exists())

    def test_cost_estimate(self) -> None:
        est = ev.estimate_cost(4, max_usd=2.0)
        self.assertEqual(8.0, est['worstCaseUsd'])
        self.assertTrue(0 < est['modelEstimate']['perSessionUsd'] <= 2.0)
        bigger = ev.estimate_cost(4, max_usd=2.0, turns=20)
        self.assertGreater(bigger['modelEstimate']['perSessionUsd'], est['modelEstimate']['perSessionUsd'])
        self.assertEqual(0.5, ev.estimate_cost(1, max_usd=0.5, turns=60)['modelEstimate']['perSessionUsd'])  # capped
        past = self.root / 'past' / 'report.json'
        past.parent.mkdir()
        past.write_text(json.dumps({'targets': [{'mode': 'ai', 'model': 'claude-opus-5-5', 'usd': 0.6},
                                                {'mode': 'ai', 'model': 'claude-opus-5-5', 'usd': 1.0},
                                                {'mode': 'fake', 'model': '', 'usd': 9.0}]}))
        measured = ev.estimate_cost(10, reports=[past.parent])['measured']
        self.assertEqual((2, 0.8, 8.0), (measured['sessions'], measured['meanUsd'], measured['totalUsd']))


@contextlib.contextmanager
def _local_site():
    """the fake gelbooru served by a real HTTP server on 127.0.0.1 (real requests objects, no external network)"""
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if self.path == '/go':
                status, body, headers = 302, b'', {'location': '/index.php?page=dapi&s=post&q=index&limit=1'}
            else:
                status, body, headers = fake_gelbooru(f'https://{HOST}{self.path}')
            self.send_response(status)
            for k, v in headers.items():
                self.send_header(k, v)
            self.send_header('content-length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            return None

    server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f'http://127.0.0.1:{server.server_port}/'
    finally:
        server.shutdown()


class RecordingTests(TestCase):
    def test_record_through_requests_then_replay(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, _local_site() as base:
            recorder = ev.Recorder(pathlib.Path(tmp) / 'local', base)
            live = SafeFetcher(allowed_hosts={'127.0.0.1'}, min_interval=0.0, allow_local=True)
            live._session = ev.RecordingSession(live._session, recorder)
            urls = [f'{base}index.php?page=dapi&s=post&q=index&limit=2&tags=sky', f'{base}go', f'{base}missing']
            got = [live.fetch(u, accept='application/xml') for u in urls]
            self.assertEqual([200, 200, 404], [r.status for r in got])
            self.assertIn(b'<posts count="3"', got[0].body)
            replay = ev.ReplaySession(pathlib.Path(tmp) / 'local')
            offline = SafeFetcher(allowed_hosts={'127.0.0.1'}, min_interval=0.0, allow_local=True)
            offline._session = replay
            again = [offline.fetch(u, accept='application/xml') for u in urls]
            self.assertEqual([(r.status, r.body, r.content_type) for r in got], [(r.status, r.body, r.content_type) for r in again])
            self.assertEqual([], replay.missing)  # robots.txt and the redirect target were recorded too
            self.assertTrue(any(u.endswith('/robots.txt') for _, u in replay.calls))

#
#
#########################################
