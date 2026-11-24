# coding=UTF-8
"""
Lynxx: gender evaluation tooling tests (offline: fake search page, fake pictures, stub model, no network)
docs/plans/gender-filter-model.md §3.2 (exclusion order), §3.7 (metrics, slices, bias rule), AC-S04, AC-G08
Run: python -m unittest lynxx.tests.test_gender_eval
"""
#########################################
#
#

import http.client
import json
import os
import pathlib
import tempfile
import threading
import unittest
from unittest import TestCase, mock

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx import gender_eval as ge  # noqa: E402
from lynxx.network import HTMLContent  # noqa: E402
from lynxx.safety import blocked_tag  # noqa: E402

PNG = b'\x89PNG\r\n\x1a\n'
IMAGE_MAGIC = (PNG, b'\xff\xd8\xff', b'GIF8', b'RIFF')

# (id, tags, general tag count, extra fields)
POSTS = [
    (1, '1girl solo long_hair smile', 12, {}),
    (2, '1boy male_focus solo', 10, {}),
    (3, '1girl 1boy hetero', 15, {}),
    (4, 'no_humans scenery sky', 9, {}),
    (5, 'loli 1girl', 10, {}),  # blacklist
    (6, 'tiny_tot 1girl', 10, {}),  # blacklist through an alias (tiny_tot -> toddler, patched in)
    (7, 'petite 1girl solo', 10, {}),  # §4.2 extra list
    (8, 'photo_(medium) 1girl', 10, {}),  # §4.3 real
    (9, '1girl solo', 10, {'md5': f'{1:032d}'}),  # same md5 as post 1
    (10, '1girl solo', 10, {'file_url': ''}),  # no file for this user
    (11, '1girl solo', 10, {'is_deleted': True}),
    (12, 'scenery sky', 2, {}),  # no gender tag: sparse, unknown
    (13, 'trap 1boy otoko_no_ko', 12, {}),
    (14, '2girls yuri', 3, {}),  # sparse, only_female high
    (15, '1girl 1other', 11, {}),  # weak (medium)
]


def _page() -> bytes:
    out = []
    for i, tags, gen, extra in POSTS:
        p = {'id': i, 'created_at': '2026-11-16T04:24:19.090-05:00', 'tag_string': tags, 'file_ext': 'jpg', 'md5': f'{i:032d}',
             'file_url': f'https://cdn.donmai.us/original/{i}.jpg', 'large_file_url': f'https://cdn.donmai.us/sample/{i}.jpg',
             'preview_file_url': f'https://cdn.donmai.us/180x180/{i}.jpg', 'rating': 'g', 'score': 1, 'tag_count_general': gen}
        p.update(extra)
        out.append(p)
    return json.dumps(out).encode()


def _manifest(quotas: dict | None = None) -> dict:
    page = HTMLContent(None, _page())  # type: ignore[arg-type]
    with mock.patch('lynxx.download_db.DownloaderDb.fetch_html', return_value=page), \
            mock.patch.dict('lynxx.tagsdb.TAG_ALIASES', {'tiny_tot': 'toddler'}), mock.patch.object(ge, '_aliases_loaded', True), \
            mock.patch.object(ge, '_log'):
        candidates = ge.collect('db', 'scenery rating:g', pages=1, page_delay=0)
        quotas = quotas or dict.fromkeys(ge.DEFAULT_QUOTAS, -1)
        return ge.build_manifest(candidates, quotas=quotas, queries=['scenery rating:g'], log=lambda _: None)


# predicted tags per (post key, input): what the stub model 'sees'
PROBS = {
    ('db:1', 'sample'): {'1girl': 0.95, 'solo': 0.9}, ('db:1', 'thumb'): {'1girl': 0.7},
    ('db:2', 'sample'): {'1boy': 0.9, 'male_focus': 0.9}, ('db:2', 'thumb'): {'1girl': 0.9},
    ('db:3', 'sample'): {'1girl': 0.9, '1boy': 0.9}, ('db:3', 'thumb'): {},  # the server refused this picture
    ('db:4', 'sample'): {'no_humans': 0.97}, ('db:4', 'thumb'): {'no_humans': 0.4},
    ('db:12', 'sample'): {'1girl': 0.9}, ('db:12', 'thumb'): {'1girl': 0.9},
    ('db:13', 'sample'): {'1girl': 0.95}, ('db:13', 'thumb'): {'1boy': 0.9},
    ('db:14', 'sample'): {'1girl': 0.9}, ('db:14', 'thumb'): {'1girl': 0.9, 'loli': 0.3},  # model safety gate
    ('db:15', 'sample'): {'1girl': 0.9, 'solo': 0.9}, ('db:15', 'thumb'): {'1girl': 0.9},
}


class StubModel:
    model_id = 'stub-model'
    info = {'device': 'cpu'}

    def __init__(self) -> None:
        self.calls = 0

    def predict(self, data: bytes) -> dict[str, float]:
        self.calls += 1
        key, kind = data[len(PNG):].decode().split('|')
        return dict(PROBS[(key, kind)])


def _fetch(calls: list | None = None):
    def fetch(post: dict, kind: str) -> bytes:
        if calls is not None:
            calls.append((post['key'], kind))
        return PNG + f'{post["key"]}|{kind}'.encode()
    return fetch


def _no_pictures_in(folder: pathlib.Path) -> list[str]:
    return [str(p) for p in folder.rglob('*') if p.is_file() and p.read_bytes()[:8].startswith(IMAGE_MAGIC)]


class SampleTests(TestCase):
    def test_exclusion_order_and_counts(self) -> None:  # §3.2, AC-S04
        m = _manifest()
        steps = {s['name']: s for s in m['exclusions']}
        self.assertEqual(['blacklist', 'extra', 'real', 'modelGate', 'duplicate', 'noFile'], [s['name'] for s in m['exclusions']])
        self.assertEqual(2, steps['blacklist']['removed'])
        self.assertEqual({'loli': 1, 'toddler': 1}, steps['blacklist']['tags'])  # the alias target caught post 6
        self.assertEqual((1, {'petite': 1}), (steps['extra']['removed'], steps['extra']['tags']))
        self.assertEqual((1, {'photo_(medium)': 1}), (steps['real']['removed'], steps['real']['tags']))
        self.assertIsNone(steps['modelGate']['removed'])
        self.assertEqual(1, steps['duplicate']['removed'])
        self.assertEqual(2, steps['noFile']['removed'])
        keys = [p['key'] for p in m['posts']]
        self.assertEqual(sorted(['db:1', 'db:2', 'db:3', 'db:4', 'db:12', 'db:13', 'db:14', 'db:15']), sorted(keys))
        with mock.patch.dict('lynxx.tagsdb.TAG_ALIASES', {'tiny_tot': 'toddler'}):
            self.assertFalse([p['key'] for p in m['posts'] if blocked_tag(ge.tags_with_aliases(p['tags'], 'db'))])
        self.assertNotIn('db_file_unavailable', json.dumps(m))

    def test_labels_slices_and_strata(self) -> None:  # §3.7 slices
        m = _manifest()
        by = {p['key']: p for p in m['posts']}
        self.assertEqual(('only_female', 'high'), (by['db:1']['tier0']['label'], by['db:1']['tier0']['conf']))
        self.assertEqual('only_female', by['db:1']['stratum'])
        self.assertEqual('full', by['db:1']['slices']['density'])
        self.assertEqual(('sparse', True), (by['db:12']['slices']['density'], by['db:12']['slices']['noGenderTag']))
        self.assertEqual(('sparse', 3), (by['db:14']['stratum'], by['db:14']['slices']['generalTags']))
        self.assertEqual('weak', by['db:15']['stratum'])
        self.assertIn('trap', by['db:13']['slices']['edge'])
        self.assertEqual(['2d_anime'], by['db:1']['slices']['style'])
        self.assertEqual('https://cdn.donmai.us/sample/1.jpg', by['db:1']['urls']['sample'])
        self.assertEqual('https://danbooru.donmai.us/posts/1', by['db:1']['postUrl'])
        self.assertEqual(round(2 / 8, 4), m['sparseShare'])
        self.assertEqual({'available': 2, 'quota': -1, 'selected': 2}, m['strata']['sparse'])

    def test_quotas(self) -> None:
        m = _manifest({'only_female': 1, 'only_male': 0, 'both': 1, 'none': 1, 'sparse': 1, 'weak': 0})
        self.assertEqual({'only_female': 1, 'both': 1, 'none': 1, 'sparse': 1}, dict(__import__('collections').Counter(p['stratum'] for p in m['posts'])))
        self.assertEqual(2, m['strata']['only_male']['available'])
        self.assertEqual({**ge.DEFAULT_QUOTAS, 'sparse': 30, 'weak': -1}, ge.parse_quotas('sparse=30,weak=all'))
        with self.assertRaises(ge.EvalError):
            ge.parse_quotas('girls=3')

    def test_pictures_stay_on_the_site(self) -> None:
        self.assertTrue(ge.same_site('https://cdn.donmai.us/sample/1.jpg', 'https://danbooru.donmai.us/'))
        self.assertFalse(ge.same_site('https://evil.example/1.jpg', 'https://danbooru.donmai.us/'))
        self.assertFalse(ge.same_site('file:///etc/passwd', 'https://danbooru.donmai.us/'))
        post = {'urls': {'preview': 'https://cdn.donmai.us/180x180/1.jpg', 'sample': 'https://cdn.donmai.us/sample/1.webm'}}
        self.assertEqual(post['urls']['preview'], ge.picture_url(post, 'sample'))  # video sample: the model looks at the poster
        resp = mock.Mock(headers={'content-type': 'image/jpeg'}, content=b'\xff\xd8\xffdata')
        with mock.patch('lynxx.download_db.DownloaderDb.wrap_request', return_value=resp) as wrap:
            f = ge.ImageFetcher(0)
            try:
                self.assertEqual(b'', f.get('db', 'https://evil.example/x.jpg'))
                self.assertEqual(0, wrap.call_count)
                self.assertEqual(resp.content, f.get('db', 'https://cdn.donmai.us/sample/1.jpg'))
                self.assertEqual(1, wrap.call_count)
            finally:
                f.close()


class MetricTests(TestCase):
    def test_kappa(self) -> None:
        pairs = [('only_female', 'only_female')] * 20 + [('only_female', 'none')] * 5 + [('none', 'only_female')] * 10 + [('none', 'none')] * 15
        k = ge.cohen_kappa(pairs)
        self.assertEqual((50, 0.7, 0.5, 0.4), (k['n'], k['observed'], k['expected'], k['kappa']))
        self.assertEqual(1.0, ge.cohen_kappa([('both', 'both')] * 3)['kappa'])

    def test_ece(self) -> None:
        self.assertEqual(0.4, ge.expected_calibration_error([(0.9, True), (0.9, False)]))
        self.assertEqual(0.0, ge.expected_calibration_error([(1.0, True)]))
        self.assertIsNone(ge.expected_calibration_error([]))

    def test_bias_rule(self) -> None:  # §3.7: n >= 150 and > 5 points under the global precision
        def row(i: int, ok: bool, slices: list) -> dict:
            return {'key': f'db:{i}', 'ref': 'only_female', 'pred': 'only_female' if ok else 'only_male', 'any': 'only_female' if ok else 'only_male',
                    'score': 0.9, 'conf': 'high', 'slices': slices}
        rows = [row(i, i % 5 != 0, [('site', 'db'), ('style', '3d')]) for i in range(150)]
        rows += [row(1000 + i, True, [('site', 'db'), ('style', '2d_anime')]) for i in range(150)]
        rows += [row(2000 + i, False, [('site', 'db'), ('edge', 'futa')]) for i in range(10)]
        a = ge._analyze(rows, 'silver')
        flagged = {(f['slice'], f['metric']) for f in a['biasFlags']}
        self.assertIn(('style=3d', 'micro'), flagged)
        self.assertNotIn(('style=2d_anime', 'micro'), flagged)
        self.assertIn('edge=futa', a['notJudged'])  # n < 150: not judged
        flag = next(f for f in a['biasFlags'] if f['slice'] == 'style=3d' and f['metric'] == 'micro')
        self.assertIn('chặn phát hành', flag['action'])  # precision 0.8 < 0.85


class EvaluateTests(TestCase):
    def test_silver(self) -> None:
        m = _manifest()
        with tempfile.TemporaryDirectory() as tmp:
            cache = ge.PredictionCache(pathlib.Path(tmp) / 'predictions.jsonl')
            model = StubModel()
            r = ge.evaluate(m, model=model, fetch=_fetch(), cache=cache, log=lambda _: None)
            self.assertEqual('silver', r['mode'])
            self.assertEqual({'modelGate': 1}, r['posts']['excluded'])
            self.assertEqual({'loli': 1}, r['posts']['modelGateTags'])
            self.assertEqual([{'key': 'db:14', 'gateTag': 'loli', 'unblockedTags': ['2girls', 'yuri']}], r['posts']['modelGateDropped'])
            self.assertEqual({'thumb': 1}, r['posts']['noPicture'])
            s = r['systems']['model']['sample']['global']
            self.assertEqual((5, 1.0, 0.8), (s['n'], s['coverage'], s['microPrecision']))
            self.assertEqual((0.5, 1, 2), tuple(s['precision']['only_female'][k] for k in ('value', 'tp', 'predicted')))
            self.assertEqual(1.0, s['precision']['none']['value'])
            self.assertEqual(0.8333, s['macroF1'])
            self.assertEqual({'only_female': 1, 'only_male': 1, 'both': 0, 'none': 0, 'unknown': 0}, s['confusion']['only_male'])
            self.assertEqual(['db:13'], r['systems']['model']['sample']['trapAsOnlyFemale']['onlyFemale'])
            self.assertEqual([{'key': 'db:13', 'ref': 'only_male', 'pred': 'only_female', 'score': 0.95}],
                             r['systems']['model']['sample']['disagreements'])
            t = r['systems']['model']['thumb']['global']
            self.assertEqual((5, 0.6, 0.6667), (t['n'], t['coverage'], t['microPrecision']))
            self.assertEqual(0.8, t['coverageAnswered'])  # db:4 answered 'none' at low confidence
            self.assertEqual(1, t['confusion']['none']['unknown'])
            self.assertEqual({'sparse': {'n': 1, 'only_female': 1}, 'full': {'n': 1, 'only_female': 1}}, r['noReference']['sample'])
            self.assertEqual({'n': 5, 'same': 1, 'sameShare': 0.2}, r['inputAgreement'])  # only db:1 gets the same verdict
            slices = {(x['dimension'], x['value']): x['metrics']['n'] for x in r['systems']['model']['sample']['slices']}
            self.assertEqual(5, slices[('site', 'db')])
            self.assertEqual(1, slices[('edge', 'trap')])
            md = ge.render_markdown(r)
            for text in ('Silver, không phải tập vàng', 'Ma trận nhầm lẫn', 'db:13', 'Loại trừ khi lấy mẫu', 'thumbnail (150–180 px)'):
                self.assertIn(text, md)
            # rerun: every prediction comes from the cache, no picture is fetched again
            calls: list = []
            model2 = StubModel()
            r2 = ge.evaluate(m, model=model2, fetch=_fetch(calls), cache=ge.PredictionCache(pathlib.Path(tmp) / 'predictions.jsonl'),
                             log=lambda _: None)
            self.assertEqual(([], 0), (calls, model2.calls))
            self.assertEqual(s, r2['systems']['model']['sample']['global'])
            self.assertEqual([], _no_pictures_in(pathlib.Path(tmp)))  # AC-S04: predicted tags only

    def test_gold_and_stop(self) -> None:
        m = _manifest()
        gold = {'kind': 'lynxx-gender-eval-gold', 'annotators': ['a', 'b'], 'kappa': 0.9,
                'labels': {'db:1': 'only_female', 'db:2': 'only_male', 'db:3': 'both', 'db:4': 'none', 'db:12': 'none',
                           'db:13': 'only_male', 'db:15': 'only_female'}}
        r = ge.evaluate(m, model=StubModel(), fetch=_fetch(), gold=gold, inputs=('sample',), log=lambda _: None)
        self.assertEqual({'model', 'tier0', 'tier0+model'}, set(r['systems']))
        t0 = r['systems']['tier0']['tags']['global']
        self.assertEqual((7, 0.8571, 1.0), (t0['n'], t0['coverage'], t0['microPrecision']))
        tm = r['systems']['tier0+model']['sample']['global']
        self.assertEqual((1.0, 0.8571), (tm['coverage'], tm['microPrecision']))  # scenery post: model says only_female, gold none
        self.assertIn(('tier0', 'unknown/-'), {(x['dimension'], x['value']) for x in r['systems']['tier0']['tags']['slices']})
        self.assertEqual(1, r['posts']['excluded']['noGoldLabel'])  # db:14 has no gold label
        # a stop (bot challenge) leaves the rest out of every number
        r2 = ge.evaluate(m, model=StubModel(), fetch=_fetch(), gold=gold, inputs=('sample',), log=lambda _: None,
                         should_stop=iter([''] * 3 + ['challenge'] * 99).__next__)
        self.assertEqual((3, True, 'challenge'), (r2['posts']['processed'], r2['posts']['incomplete'], r2['posts']['stopReason']))
        # dropped posts are out
        m['dropped'] = [{'key': 'db:1', 'reason': 'unsafe'}]
        r3 = ge.evaluate(m, model=StubModel(), fetch=_fetch(), gold=gold, inputs=('sample',), log=lambda _: None)
        self.assertEqual(1, r3['posts']['excluded']['dropped'])

    def test_cli_drops_gated_posts_from_the_manifest(self) -> None:  # §3.2 / §4: like label, the model gate's posts are not kept
        def fetch(_self, module: str, url: str) -> bytes:  # https://cdn.donmai.us/<sample|180x180>/<id>.jpg
            kind = 'thumb' if '/180x180/' in url else 'sample'
            return PNG + f'db:{url.rsplit("/", 1)[1].split(".", maxsplit=1)[0]}|{kind}'.encode()
        with tempfile.TemporaryDirectory() as tmp:
            folder = pathlib.Path(tmp)
            manifest_path = folder / 'manifest.json'
            ge.write_json(manifest_path, _manifest())
            logged: list[str] = []
            with mock.patch.object(ge, '_model_backend', return_value=StubModel()), mock.patch.object(ge.ImageFetcher, 'get', fetch), \
                    mock.patch.object(ge, '_log', logged.append):
                self.assertEqual(0, ge.main(['evaluate', '--manifest', str(manifest_path), '--silver', '--image-interval', '0',
                                             '--out-dir', str(folder / 'report')]))
                man = ge.read_json(manifest_path)
                self.assertNotIn('db:14', [p['key'] for p in man['posts']])
                self.assertEqual(7, len(man['posts']))
                entry = next(d for d in man['dropped'] if d['key'] == 'db:14')
                self.assertEqual(('modelGate', 'loli', 'evaluate'), (entry['reason'], entry['gateTag'], entry['by']))
                self.assertFalse({'tags', 'urls', 'unblockedTags', 'postUrl'} & set(entry))  # the gate tag only
                self.assertNotIn('14.jpg', json.dumps(man))
                self.assertTrue(any('db:14' in x and '2girls, yuri' in x and 'safety.py' in x for x in logged))  # tags to review
                report = ge.read_json(folder / 'report' / 'report.json')
                self.assertEqual((1, ge.sha256_file(manifest_path)), (report['manifest']['removedFromManifest'], report['manifest']['sha256']))
                self.assertIn('đã bị xoá khỏi manifest', (folder / 'report' / 'report.md').read_text(encoding='utf-8'))
                # a second run: the post is gone, nothing is dropped twice
                self.assertEqual(0, ge.main(['evaluate', '--manifest', str(manifest_path), '--silver', '--image-interval', '0',
                                             '--out-dir', str(folder / 'report')]))
                man2 = ge.read_json(manifest_path)
                self.assertEqual((7, 1), (len(man2['posts']), sum(d['key'] == 'db:14' for d in man2['dropped'])))
            self.assertEqual([], _no_pictures_in(folder))


class AgreeTests(TestCase):
    def test_agree_gold_and_cli(self) -> None:  # AC-G08 tooling
        a = {'annotator': 'a', 'labels': {'db:1': {'label': 'only_female'}, 'db:2': {'label': 'only_male'}, 'db:3': {'label': 'both'},
                                          'db:4': {'label': 'skip'}, 'db:12': {'label': 'none'}},
             'dropped': {'db:13': {'reason': 'unsafe', 'tags': ['1boy', 'small_kid_look'], 'unblockedTags': ['1boy', 'small_kid_look']}}}
        b = {'annotator': 'b', 'labels': {'db:1': {'label': 'only_female'}, 'db:2': {'label': 'both'}, 'db:3': {'label': 'both'},
                                          'db:4': {'label': 'none'}, 'db:12': {'label': 'only_female'}, 'db:13': {'label': 'only_male'}}}
        arb = {'annotator': 'c', 'labels': {'db:2': {'label': 'only_male'}}}
        report, gold = ge.agree(a, b, arbiter=arb, manifest=_manifest())
        self.assertEqual(4, report['n'])  # db:4 skipped by a, db:13 dropped
        self.assertEqual([{'key': 'db:12', 'a': 'none', 'b': 'only_female'}, {'key': 'db:2', 'a': 'only_male', 'b': 'both', 'arbiter': 'only_male'}],
                         report['disagreements'])
        self.assertEqual((1, 1), (report['resolvedByArbiter'], report['unresolved']))
        self.assertEqual({'db:1': 'only_female', 'db:2': 'only_male', 'db:3': 'both'}, gold['labels'])
        self.assertIn('db:13', gold['dropped'])
        self.assertEqual({'1boy': 1, 'small_kid_look': 1}, report['unsafeTagsNotBlocked'])
        self.assertFalse(report['gold']['meetsSize'])
        self.assertFalse(report['meetsKappa'])
        with tempfile.TemporaryDirectory() as tmp:
            pa, pb, out, gp = (pathlib.Path(tmp) / n for n in ('a.json', 'b.json', 'agree.json', 'gold.json'))
            ge.write_json(pa, a)
            ge.write_json(pb, b)
            with mock.patch('builtins.print'), mock.patch.object(ge, '_log'):
                self.assertEqual(0, ge.main(['agree', str(pa), str(pb), '--out', str(out), '--gold', str(gp)]))
            self.assertEqual(4, ge.read_json(out)['n'])
            self.assertEqual({'db:1': 'only_female', 'db:3': 'both'}, ge.read_json(gp)['labels'])


class LabelServerTests(TestCase):
    def test_label_page(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(ge, '_log'):
            folder = pathlib.Path(tmp)
            manifest_path = folder / 'manifest.json'
            ge.write_json(manifest_path, _manifest())

            class Gate:
                def predict(self, data: bytes) -> dict[str, float]:
                    return {'1girl': 0.9, 'loli': 0.5} if b'db:3|' in data else {'1girl': 0.9}
            fetched: list[str] = []

            def fetch(module: str, url: str) -> bytes:
                fetched.append(url)
                key = 'db:' + url.rsplit('/', 1)[1].split('.', maxsplit=1)[0]
                return PNG + f'{key}|x'.encode()
            fetcher = ge.ImageFetcher(0, fetch=fetch)
            labels_path = folder / 'labels-alice.json'
            session = ge.LabelSession(manifest_path, labels_path, 'alice', fetcher=fetcher, gate=Gate())
            srv = ge.make_label_server(session, 0)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            host, port = srv.server_address[:2]
            self.assertEqual('127.0.0.1', host)

            def call(method: str, path: str, body: dict | None = None, token: str | None = srv.token, hostname: str = f'127.0.0.1:{port}'):
                c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
                headers = {'Host': hostname, 'Content-Type': 'application/json'}
                if token:
                    headers['X-Token'] = token
                c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=headers)
                r = c.getresponse()
                data = r.read()
                c.close()
                return r.status, data, r.getheader('Content-Type', '')
            try:
                status, page, _ = call('GET', '/', token=None)
                self.assertEqual(200, status)
                self.assertIn(srv.token.encode(), page)
                self.assertEqual(403, call('GET', '/', token=None, hostname=f'evil.example:{port}')[0])  # DNS rebinding
                self.assertEqual(403, call('GET', '/api/next', token=None)[0])
                status, body, _ = call('GET', '/api/next')
                nxt = json.loads(body)
                self.assertEqual((200, 'alice', 8), (status, nxt['annotator'], nxt['total']))
                self.assertNotIn('tags', json.dumps(nxt))  # annotators label blind: no tags, no tier-0 label
                key = nxt['post']['key']
                status, img, ctype = call('GET', f'/api/img?key={key}&kind=sample&t={srv.token}', token=None)
                self.assertEqual((200, 'image/png'), (status, ctype))
                self.assertTrue(img.startswith(PNG))
                self.assertEqual(403, call('POST', '/api/label', {'key': key, 'label': 'only_female'}, token=None)[0])
                self.assertEqual(400, call('POST', '/api/label', {'key': key, 'label': 'girl'})[0])
                self.assertEqual(200, call('POST', '/api/label', {'key': key, 'label': 'only_female'})[0])
                self.assertEqual('only_female', ge.read_json(labels_path)['labels'][key]['label'])
                self.assertEqual(1, json.loads(call('GET', '/api/next')[1])['done'])
                self.assertEqual(key, json.loads(call('POST', '/api/undo', {})[1])['undone'])
                self.assertNotIn(key, ge.read_json(labels_path)['labels'])
                # the model safety gate trips before a human sees the picture: the post is dropped
                status, body, _ = call('GET', f'/api/img?key=db:3&kind=sample&t={srv.token}', token=None)
                self.assertEqual(410, status)
                doc = ge.read_json(labels_path)
                self.assertEqual(('modelGate', 'loli'), (doc['dropped']['db:3']['reason'], doc['dropped']['db:3']['gateTag']))
                # a human drops a post as unsafe: removed from the manifest, its tags reported
                self.assertEqual(200, call('POST', '/api/drop', {'key': 'db:13'})[0])
                doc = ge.read_json(labels_path)
                self.assertEqual(['1boy', 'otoko_no_ko', 'trap'], doc['dropped']['db:13']['unblockedTags'])
                man = ge.read_json(manifest_path)
                self.assertNotIn('db:13', [p['key'] for p in man['posts']])
                self.assertNotIn('db:3', [p['key'] for p in man['posts']])
                self.assertEqual({'db:3', 'db:13'}, {d['key'] for d in man['dropped']})
                self.assertEqual(6, json.loads(call('GET', '/api/next')[1])['total'])
                self.assertEqual(410, call('GET', f'/api/img?key=db:13&kind=sample&t={srv.token}', token=None)[0])
            finally:
                srv.shutdown()
                srv.server_close()
                fetcher.close()
            self.assertEqual(['labels-alice.json', 'manifest.json'], sorted(os.listdir(folder)))  # no picture on disk
            self.assertEqual([], _no_pictures_in(folder))
            self.assertTrue(all(u.startswith('https://cdn.donmai.us/') for u in fetched))


if __name__ == '__main__':
    unittest.main()

#
#
#########################################
