# coding=UTF-8
"""
Lynxx: regression tests for audit batch D, core part (docs/reports/audit-2026-11-16.md), offline
Run: python -m unittest lynxx.tests.test_batch_d_core
"""
#########################################
#
#

import itertools
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
from multiprocessing.dummy import current_process
from unittest import TestCase, mock

from bs4 import BeautifulSoup

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx.cmdargs import prepare_arglist  # noqa: E402
from lynxx.defines import ItemInfo, ThreadInterruptException  # noqa: E402
from lynxx.downloaders import _make_downloader  # noqa: E402
from lynxx.extensions import Extensions  # noqa: E402
from lynxx.module import ProcModule  # noqa: E402
from lynxx.network import HTMLContent  # noqa: E402
from lynxx.tagsdb import TagsDB  # noqa: E402
from lynxx.task import MAX_SPLIT_TASKS, extract_neg_and_groups, split_tags_into_tasks  # noqa: E402
from lynxx.validators import ModuleValidator, module_id_from_setting  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _module(abbr: str) -> int:
    Extensions.ensure_loaded(include_catalog=True)
    return ProcModule.PROC_MODULES_BY_NAME[abbr]


class TagLimitTests(TestCase):
    """D7: account tag limit (-tags_limit), OR groups split cap"""

    def tearDown(self) -> None:
        ProcModule.set(ProcModule.RX)

    def test_tags_limit_overrides_module_default(self) -> None:
        Extensions.ensure_loaded()
        TagsDB.clear()
        ProcModule.set(ProcModule.DB)
        required: list[str] = []
        tags, _ = extract_neg_and_groups('a b c d e', False, required)
        self.assertEqual((['a', 'b'], ['c', 'd', 'e']), (sorted(tags), sorted(required)))
        required = []
        tags, _ = extract_neg_and_groups('a b c d e', False, required, 6)
        self.assertEqual((['a', 'b', 'c', 'd', 'e'], []), (sorted(tags), required))
        self.assertEqual(6, prepare_arglist(['-module', 'db', '-tags_limit', '6', '--', 'a']).tags_limit)
        with self.assertRaises(SystemExit):
            prepare_arglist(['-module', 'db', '-tags_limit', 'x', '--', 'a'])

    def test_tags_limit_reaches_the_downloader(self) -> None:
        Extensions.ensure_loaded()
        TagsDB.clear()
        with _make_downloader(ProcModule.DB) as dwn:
            dwn._parse_args(prepare_arglist(['-module', 'db', '-tags_limit', '3', '--', 'a', 'b', 'c', 'd']), False)
            self.assertEqual(3, dwn.tags_limit)
            self.assertEqual(['d'], dwn.required_tags)

    def test_or_split_cap(self) -> None:
        self.assertEqual(64, len(split_tags_into_tasks(['(+a+~+b+)'] * 6 + ['x'], '+', ':', True)))
        with mock.patch('lynxx.network.time.sleep'), self.assertRaises(ThreadInterruptException) as ctx:
            split_tags_into_tasks(['(+a+~+b+)'] * 7 + ['x'], '+', ':', True)
        self.assertIn(f'limit {MAX_SPLIT_TASKS:d}', str(ctx.exception))


class NetworkPolicyTests(TestCase):
    """D6: user agents, user headers win over module defaults, no silent ToS consent, cross-process api rate limit"""

    def setUp(self) -> None:
        current_process().killed = False

    def tearDown(self) -> None:
        ProcModule.set(ProcModule.RX)

    def test_user_headers_win(self) -> None:
        with _make_downloader(ProcModule.DB) as dwn:
            dwn._parse_args(prepare_arglist(['-module', 'db', '--', 'a']), False)
            self.assertTrue(dwn.add_headers['User-Agent'].startswith('Lynxx/'))
            self.assertNotIn('github.com', dwn.add_headers['User-Agent'])  # no upstream repository as contact
        with _make_downloader(ProcModule.DB) as dwn:
            dwn._parse_args(prepare_arglist(['-module', 'db', '-header', 'user-agent=Mozilla/5.0 test', '--', 'a']), False)
            self.assertEqual('Mozilla/5.0 test', dwn.add_headers['User-Agent'])

    def test_e621_user_agent_names_the_account(self) -> None:
        en = _module('en')
        with _make_downloader(en) as dwn:
            dwn._parse_args(prepare_arglist(['-module', 'en', '-api_key', f'{"a" * 24},some_user', '--', 'cat']), False)
            self.assertRegex(dwn.add_headers['User-Agent'], r'^Lynxx/\S+ \(by some_user on e621\)$')
        with _make_downloader(en) as dwn:
            dwn._parse_args(prepare_arglist(['-module', 'en', '--', 'cat']), False)
            self.assertNotIn('by', dwn.add_headers['User-Agent'])

    def test_paheal_does_not_accept_terms_silently(self) -> None:
        with _make_downloader(_module('rp')) as dwn:
            self.assertNotIn('ui-tnc-agreed', dwn._get_module_specific_default_cookies())

    def test_shared_rate_limit_across_processes(self) -> None:
        code = ('import sys, time; sys.path.insert(0, sys.argv[1]); from lynxx.network import shared_rate_wait\n'
                'for _ in range(3):\n    assert shared_rate_wait("https://rate.test/", 0.2); print(time.time(), flush=True)\n')
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, 'LYNXX_DATA_DIR': tmp}
            procs = [subprocess.Popen([sys.executable, '-c', code, str(ROOT)], stdout=subprocess.PIPE, env=env) for _ in range(2)]
            stamps = sorted(float(x) for p in procs for x in p.communicate(timeout=60)[0].split())
        self.assertEqual(6, len(stamps))
        gaps = [b - a for a, b in itertools.pairwise(stamps)]
        # the stamps are printed after the wait returns: a process descheduled in between shortens one gap and lengthens
        # the next (a loaded CI runner measured 0.133 s once). The limiter's promise is the pace over the 6 requests.
        self.assertGreaterEqual(stamps[-1] - stamps[0], 5 * 0.2 * 0.9, gaps)
        self.assertGreaterEqual(min(gaps), 0.1, gaps)


class CommentsInfoTests(TestCase):
    """D8 + streaming regression: comments / sources of items whose info is only in item_info_dict_all"""

    def setUp(self) -> None:
        current_process().killed = False

    def tearDown(self) -> None:
        ProcModule.set(ProcModule.RX)

    def test_rx_comments(self) -> None:
        xml = b'<comments><comment creator="alice" body="nice" id="1"/><comment creator="bob" body="ok" id="2"/></comments>'
        with _make_downloader(ProcModule.RX) as dwn:
            dwn.reset_root_thread(current_process())
            info = ItemInfo()
            dwn.item_info_dict_all['77'] = info
            with mock.patch.object(dwn, 'fetch_html', return_value=HTMLContent(None, xml)):
                dwn._extract_comments('77')
            self.assertEqual(['alice', 'bob'], [c.author for c in info.comments])
            with mock.patch.object(dwn, 'fetch_html', return_value=HTMLContent(None, xml)):
                dwn._extract_comments('78')  # no info at all: no KeyError

    def test_e621_comments(self) -> None:
        with _make_downloader(_module('en')) as dwn:
            info = ItemInfo()
            dwn.item_info_dict_all['5'] = info
            dwn._extract_comments(json.dumps([{'creator_name': 'c1', 'body': 'b', 'score': 3}]).encode(), '5')
            self.assertEqual(['c1'], [c.author for c in info.comments])
            dwn._extract_comments(b'[]', '6')

    def test_paheal_comments(self) -> None:
        html = ('<div class="comment"><a class="username">dave</a><span class="bbcode"> hello </span></div>'
                '<div class="comment"><span class="username">eve</span><span class="bbcode">hi</span></div>')
        with _make_downloader(_module('rp')) as dwn:
            info = ItemInfo()
            dwn.item_info_dict_all['9'] = info
            dwn._extract_comments(BeautifulSoup(html, 'html.parser'), '9')
            self.assertEqual([('dave', 'hello'), ('eve', 'hi')], [(c.author, c.body) for c in info.comments])


class LegacyGuiSettingsTests(TestCase):
    """D5: the Tk GUI saves the module abbreviation (external module ids depend on load order), reads legacy integers"""

    def test_module_setting(self) -> None:
        self.assertEqual(ProcModule.DB, module_id_from_setting('db'))
        self.assertEqual(ProcModule.DB, module_id_from_setting(str(ProcModule.DB - 1)))
        self.assertEqual(ProcModule.RX, module_id_from_setting(0))
        self.assertTrue(ModuleValidator()('rx'))
        self.assertFalse(ModuleValidator()('nope'))
        self.assertFalse(ModuleValidator()('99'))


class HeadersJsonTests(TestCase):
    """-headers / -cookies json: the original code stripped every backslash (escaped quotes rejected, '\\\\' lost, unicode escapes broken)"""

    def test_valid_json(self) -> None:
        from argparse import ArgumentTypeError

        from lynxx.validators import valid_json
        self.assertEqual({'X': 'a"b'}, valid_json(json.dumps({'X': 'a"b'})))
        self.assertEqual({'X': 'a\\b'}, valid_json(json.dumps({'X': 'a\\b'})))
        self.assertEqual({'X': '\u00e9'}, valid_json(json.dumps({'X': '\u00e9'})))
        self.assertEqual({'a': 'b'}, valid_json("'{\"a\": \"b\"}'"))  # legacy shell quoting
        for bad in ('{"a": 1}', '[1]', 'x', '{"a": ["b"]}'):
            with self.assertRaises(ArgumentTypeError):
                valid_json(bad)
        args = prepare_arglist(['-module', 'rx', '-headers', json.dumps({'X-T': 'q"\\z'}), '--', 'a'])
        self.assertEqual({'X-T': 'q"\\z'}, args.headers)


class FilesIdentifyTests(TestCase):
    """Search by file name / path / post link (files.identify)"""

    def setUp(self) -> None:
        Extensions.ensure_loaded(include_catalog=True)

    def test_names_and_links(self) -> None:
        from lynxx import server
        md5 = '0123456789abcdef0123456789abcdef'
        cases = {
            'db_12345.jpg': ('db', 'id', '12345'),
            '/home/me/Lynxx/rx_6789_score(12)_cat_ears.png': ('rx', 'id', '6789'),
            '00042.jpg': ('xb', 'id', '42'),  # no prefix: the selected module
            f'C:\\pics\\{md5}.webm': ('xb', 'md5', md5),
            f'__hatsune_miku_drawn_by_x__{md5}.png': ('db', 'md5', md5),
            f'sample_{md5}.jpg': ('xb', 'md5', md5),
            'https://danbooru.donmai.us/posts/5000?q=a': ('db', 'id', '5000'),
            'https://rule34.xxx/index.php?page=post&s=view&id=777': ('rx', 'id', '777'),
            f'https://cdn.donmai.us/original/01/23/{md5}.jpg': ('db', 'md5', md5),
            'https://e621.net/posts/42': ('en', 'id', '42'),
            'https://rule34.paheal.net/post/view/99': ('rp', 'id', '99'),
            'https://rule34.us/index.php?r=posts/view&id=55': ('rs', 'id', '55'),
            'IMG_2024.jpg': ('xb', '', ''),
            'db_!info_1-2.json': ('xb', '', ''),
            'https://example.com/a.jpg': ('xb', '', ''),
        }
        r = server.m_files_identify({'names': list(cases), 'module': 'xb'})
        for it in r['items']:
            with self.subTest(name=it['name']):
                self.assertEqual(cases[it['name']], (it['module'], it['kind'], it['value']))

    def test_queries(self) -> None:
        from lynxx import server
        r = server.m_files_identify({'names': ['db_1.jpg', 'db_2.jpg', 'db_1.png', f'__a__{"a" * 32}.jpg', 'rx_5.jpg', 'rx_6.jpg'],
                                     'module': 'db'})
        qs = {q['module']: q for q in r['queries']}
        self.assertEqual('(id:1,2~md5:' + 'a' * 32 + ')', qs['db']['tags'])  # danbooru lists, OR between ids and md5s
        self.assertEqual(3, qs['db']['count'])
        self.assertEqual('(id:5~id:6)', qs['rx']['tags'])
        many = server.build_file_queries([{'module': 'rx', 'kind': 'id', 'value': str(i)} for i in range(70)])
        self.assertEqual((64, 6), (many[0]['count'], many[0]['dropped']))
        lists = server.build_file_queries([{'module': 'db', 'kind': 'id', 'value': str(i)} for i in range(250)])
        self.assertEqual(3, lists[0]['tags'].count('id:'))  # chunks of 100 ids
        for module, tags in (('db', qs['db']['tags']), ('rx', qs['rx']['tags'])):
            self.assertTrue(server.m_tags_validate({'module': module, 'tags': tags})['valid'], (module, tags))


class SidecarFramingTests(TestCase):
    """D8: json-rpc framing of the real sidecar process"""

    def test_framing(self) -> None:
        lines = [
            'not json',
            '{"jsonrpc":"2.0","id":1,"method":"no.such.method"}',
            '{"jsonrpc":"2.0","id":2,"method":"app.info"}',
            '{"jsonrpc":"2.0","id":3,"method":"tags.validate","params":{"module":"rx","tags":"a b -c"}}',
            '',
            '{"jsonrpc":"2.0","method":"app.shutdown"}',
        ]
        with tempfile.TemporaryDirectory() as tmp:
            env = {**os.environ, 'LYNXX_DATA_DIR': tmp, 'PYTHONPATH': str(ROOT)}
            t0 = time.monotonic()
            r = subprocess.run([sys.executable, '-m', 'lynxx.server'], input='\n'.join(lines).encode() + b'\n', capture_output=True,
                               cwd=ROOT, env=env, timeout=120)
        msgs = [json.loads(x) for x in r.stdout.decode().splitlines() if x.strip()]
        self.assertEqual('ready', msgs[0].get('method'), r.stderr.decode()[-500:])
        by_id = {m.get('id'): m for m in msgs if 'id' in m}
        self.assertEqual(-32700, by_id[None]['error']['code'])
        self.assertEqual(-32601, by_id[1]['error']['code'])
        self.assertEqual('Lynxx', by_id[2]['result']['name'])
        self.assertTrue(by_id[3]['result']['valid'])
        self.assertEqual(0, r.returncode)
        self.assertLess(time.monotonic() - t0, 60)

#
#
#########################################
