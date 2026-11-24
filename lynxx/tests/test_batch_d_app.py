# coding=UTF-8
"""
Lynxx: regression tests for audit batch D, desktop app side (docs/reports/audit-2026-11-16.md, D3 / D4), offline
- frozen core entry (lynxx/frozen_main.py): 'server' / 'cli' dispatch used by the packaged app
- headers / cookies with '"' or '\\' as the app passes them ('-header name=value' / '-cookie name=value', see app/main.js connectionArgs)
- tools/build_core.py data staging: everything the frozen core needs at run time; the catalog extensions without a tag source, and
  the one whose own update merges the most used tags into its list (Paheal 1.0.3 and later), carry their 2tags/ list, which an install from
  that catalog brings to the core; an install a core refuses keeps the version installed before
- tools/check_selftest.py: a failed step names its false checks and shows each value on a line of its own
Run: python -m unittest lynxx.tests.test_batch_d_app
"""
#########################################
#
#

import contextlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from unittest import TestCase, mock

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx.cmdargs import prepare_arglist  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent.parent


def _run_frozen_main(*args: str, stdin: str = '') -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as data_dir:
        env = {**os.environ, 'LYNXX_DATA_DIR': data_dir, 'PYTHONPATH': str(ROOT), 'LYNXX_CATALOG_AUTOLOAD': '0'}
        return subprocess.run((sys.executable, '-m', 'lynxx.frozen_main', *args), cwd=ROOT, env=env, input=stdin,
                              capture_output=True, text=True, encoding='utf-8', timeout=120)


def _load_tool(name: str):
    spec = importlib.util.spec_from_file_location(f'lynxx_{name}', ROOT / 'tools' / f'{name}.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_build_core():
    return _load_tool('build_core')


class FrozenMainTests(TestCase):
    def test_usage_and_unknown_command(self) -> None:
        r = _run_frozen_main()
        self.assertEqual(2, r.returncode)
        self.assertIn('usage: lynxx-core', r.stderr)
        r = _run_frozen_main('bogus')
        self.assertEqual(2, r.returncode)
        r = _run_frozen_main('cli')  # no arguments: the legacy GUI is not bundled
        self.assertEqual(2, r.returncode)

    def test_server_ready_and_shutdown(self) -> None:
        r = _run_frozen_main('server', stdin='{"jsonrpc":"2.0","method":"app.shutdown"}\n')
        self.assertEqual(0, r.returncode, r.stderr[-2000:])
        lines = [json.loads(x) for x in r.stdout.splitlines() if x.startswith('{')]
        self.assertTrue(lines and lines[0].get('method') == 'ready', r.stdout[-2000:])
        self.assertEqual('Lynxx', lines[0]['params']['name'])

    def test_cli_parser(self) -> None:
        r = _run_frozen_main('cli', '--help')
        self.assertEqual(0, r.returncode, r.stderr[-2000:])
        self.assertIn('-module', r.stdout)


class HttpPairTests(TestCase):
    def test_header_and_cookie_values_with_quotes_and_backslashes(self) -> None:
        # what app/main.js connectionArgs sends: one 'name=value' per entry, split at the first '='
        value_h, value_c = 'a"b\\c=d', '"q\\z"'
        args = prepare_arglist(['-header', f'X-Test={value_h}', '-cookie', f'sid={value_c}', '-header', 'If-None-Match="etag"',
                                '-module', 'db', '--', 'scenery'])
        self.assertEqual([('X-Test', value_h), ('If-None-Match', '"etag"')], args.header)
        self.assertEqual([('sid', value_c)], args.cookie)
        self.assertEqual(['scenery'], args.tags)


class BuildCoreTests(TestCase):
    def test_extension_imports_are_bundled_as_hidden_imports(self) -> None:
        bc = _load_build_core()
        names = bc._extension_imports()
        self.assertIn('bs4', names)
        self.assertFalse(any(n.startswith('lynxx') for n in names))

    def test_stage_data(self) -> None:
        bc = _load_build_core()
        with tempfile.TemporaryDirectory() as tmp:
            stage = pathlib.Path(tmp)
            data = dict((dest, src) for src, dest in bc._stage_data(stage, 'aliases'))
            self.assertEqual({'lynxx/builtin_ext', 'lynxx/gender_rules', 'lynxx/safety_data', 'extensions', '2tags', 'aigen_refs'},
                             set(data))
            # domain denylist + trusted catalog keys: read at run time by the core (fail closed when missing)
            self.assertTrue((data['lynxx/safety_data'] / 'denied_domains.txt').is_file())
            self.assertTrue((data['lynxx/safety_data'] / 'catalog_keys.json').is_file())
            self.assertTrue((data['lynxx/builtin_ext'] / 'danbooru' / 'manifest.json').is_file())
            from lynxx.aigen.tools import REF_FILES  # sources the AI generator shows the model (frozen core: no .py on disk)
            self.assertTrue(all((data['aigen_refs'] / rel).is_file() for rel in REF_FILES))
            self.assertTrue(any(data['lynxx/gender_rules'].glob('*.json')))
            catalog = data['extensions']
            self.assertTrue(all((d / 'manifest.json').is_file() for d in catalog.iterdir() if d.is_dir()))
            self.assertFalse(any(catalog.rglob('__pycache__')))
            self.assertTrue((catalog / 'rule34hentai' / 'rn_tags.json').is_file())  # _stage_catalog, below
            self.assertFalse((catalog / 'bbooru' / 'bb_tags.json').exists())
            self.assertEqual(['all_tag_aliases.json'], sorted(f.name for f in data['2tags'].iterdir()))
        with tempfile.TemporaryDirectory() as tmp:
            data = dict((dest, src) for src, dest in bc._stage_data(pathlib.Path(tmp), 'none'))
            self.assertNotIn('2tags', data)

    def test_files_the_release_rule_calls_not_shipped_are_not_in_the_build(self) -> None:
        # release.yml skips a release when every file changed since the last one matches NOT_SHIPPED (staging otherwise asks for a
        # version bump): a 2tags/ list or a tool it names must never reach the frozen core or its bundled catalog, and the lists
        # the build does take must not match (a change to them is a change of the app)
        import re
        text = (ROOT / '.github' / 'workflows' / 'release.yml').read_text(encoding='utf-8')
        rule = re.compile(re.search(r"NOT_SHIPPED: '([^']+)'", text).group(1))
        bc = _load_build_core()
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            staged = {p.name for p in bc._stage_catalog(pathlib.Path(tmp) / 'extensions').glob('*/*_tags.json')}
        self.assertIn("ap.add_argument('--tags', default='aliases'", (ROOT / 'tools' / 'build_core.py').read_text(encoding='utf-8'))
        lists = sorted(p.name for p in (ROOT / '2tags').glob('*_tags.json'))
        self.assertTrue(lists)
        for name in lists:
            with self.subTest(name):
                self.assertEqual(name not in staged, bool(rule.match(f'2tags/{name}')),
                                 'a list the build bundles must not match NOT_SHIPPED, the others must')
        self.assertFalse(rule.match(f'2tags/{bc.ALIASES_FILE}'))
        for tool in ('build_catalog', 'sign_catalog', 'check_selftest', 'type_tag_list'):
            self.assertTrue(rule.match(f'tools/{tool}.py'), tool)
        # the tag list typing tool is a maintainer tool: neither the build nor the core uses it
        users = [p for p in [ROOT / 'tools' / 'build_core.py', *(ROOT / 'lynxx').rglob('*.py')]
                 if 'tests' not in p.parts and 'type_tag_list' in p.read_text(encoding='utf-8', errors='replace')]
        self.assertEqual([], users)
        for path in ('tools/build_core.py', 'lynxx/tagsdb.py', 'extensions/paheal/tags.py', 'app/main.js', 'app/site-verify.js',
                     'app/renderer/app.js'):
            self.assertFalse(rule.match(path), path)
        # the app's own tests are not shipped: electron-builder packs only the 'files' of app/package.json, none under test/
        packed = json.loads((ROOT / 'app' / 'package.json').read_text(encoding='utf-8'))['build']['files']
        self.assertFalse([f for f in packed if f.split('/', 1)[0] in ('test', '**', '*') or f.startswith('!')], packed)
        tests = sorted(p.relative_to(ROOT).as_posix() for p in (ROOT / 'app' / 'test').glob('*.js'))
        self.assertTrue(tests)
        for path in tests:
            self.assertTrue(rule.match(path), path)

    def test_catalog_extensions_that_need_their_shipped_list_carry_it(self) -> None:
        # Rule34Hentai, Rule34.us read no list from their site (no tagUpdate, not reviewed, no engine), and Paheal's own update reads
        # only the tags with 100+ posts and merges them into its list (tagUpdate.merge): the bundled catalog holds their 2tags/ list
        # next to the manifest. Bbooru, e621, Xbooru read theirs; the extensions whose own update reads the whole list get none, even
        # with a list of their module in 2tags/ (the core stays small)
        bc = _load_build_core()
        with tempfile.TemporaryDirectory() as tmp:
            catalog = bc._stage_catalog(pathlib.Path(tmp) / 'extensions')
            with_list = {p.parent.name: p.name for p in catalog.glob('*/*_tags.json')}
            self.assertEqual({'rule34hentai': 'rn_tags.json', 'paheal': 'rp_tags.json', 'rule34us': 'rs_tags.json'}, with_list)
            versions = {'rule34hentai': '1.0.4', 'paheal': '1.0.4', 'rule34us': '1.0.2'}  # installs before are offered the update
            for ext_id, name in with_list.items():
                self.assertEqual((ROOT / '2tags' / name).stat().st_size, (catalog / ext_id / name).stat().st_size)
                manifest = json.loads((catalog / ext_id / 'manifest.json').read_text(encoding='utf-8'))
                self.assertEqual((name, versions[ext_id]), (manifest['tagList'], manifest['version']))
            self.assertTrue((catalog / 'paheal' / 'tags.py').is_file())
            for ext_id in ('bbooru', 'e621', 'xbooru', 'gelbooru', 'tbib'):
                self.assertEqual([], list((catalog / ext_id).glob('*_tags.json')))
            tags = pathlib.Path(tmp) / '2tags'  # a list of a replace job's module in 2tags/: never staged
            tags.mkdir()
            for name in ('tb_tags.json', 'gb_tags.json', 'rp_tags.json'):
                (tags / name).write_text('{"blue_sky": "120 posts"}\n', encoding='utf-8')
            with contextlib.redirect_stdout(io.StringIO()):
                other = bc._stage_catalog(pathlib.Path(tmp) / 'other', tags_dir=tags)
            self.assertEqual({'paheal'}, {p.parent.name for p in other.glob('*/*_tags.json')})
            self._install_staged(catalog, pathlib.Path(tmp) / 'data')

    def test_paheal_updates_to_its_merge_job_and_a_refused_install_keeps_1_0_2(self) -> None:
        # the install is transactional: a core that refuses the 1.0.3 manifest ('merge' unknown: what Lynxx 2.0.7 to 2.0.10-rc.109
        # say, checked by hand with their code, docs/dev_log.md 7bd) keeps 1.0.2, its list and the learning on top of it. Here the
        # new core with the older list of keys stands in for them
        from lynxx import modspec
        from lynxx.extensions import Extensions
        from lynxx.modspec import ManifestError
        from lynxx.tags_updater import tag_source
        from lynxx.tagsdb import TagsDB
        bc = _load_build_core()
        with tempfile.TemporaryDirectory() as tmp:
            catalog = bc._stage_catalog(pathlib.Path(tmp) / 'extensions')
            data_dir = pathlib.Path(tmp) / 'data'
            self._keep_registry()
            with mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': data_dir.as_posix(), 'LYNXX_CATALOG_DIR': catalog.as_posix()}):
                os.environ.pop('LYNXX_TAGS_DIR', None)  # restored by the patch
                old = pathlib.Path(tmp) / 'old' / 'paheal'  # 1.0.2 as installed before: no tagUpdate, its list
                shutil.copytree(catalog / 'paheal', old, ignore=shutil.ignore_patterns('tags.py'))
                manifest = json.loads((old / 'manifest.json').read_text(encoding='utf-8'))
                manifest.pop('tagUpdate')
                (old / 'manifest.json').write_text(json.dumps({**manifest, 'version': '1.0.2'}), encoding='utf-8')
                Extensions.install_from_dir(old)
                installed = Extensions.user_dir() / 'paheal'
                listed = (installed / 'rp_tags.json').read_bytes()
                with mock.patch.object(modspec, 'TAG_UPDATE_KEYS', ('entry',)), \
                        self.assertRaisesRegex(ManifestError, r'unknown tagUpdate field\(s\): merge'):
                    Extensions.install_from_catalog('paheal')
                self.assertEqual('1.0.2', json.loads((installed / 'manifest.json').read_text(encoding='utf-8'))['version'])
                self.assertEqual(listed, (installed / 'rp_tags.json').read_bytes())
                self.assertFalse((installed / 'tags.py').exists())
                m = Extensions.install_from_catalog('paheal')  # "Cập nhật v1.0.2 → v1.0.4"
                self.assertEqual(('rp', '1.0.4'), (m.abbr, m.version))
                self.assertTrue(tag_source('rp').merge)
                TagsDB.clear()
                TagsDB.locate_default_lists(reload=('rp',))
                self.assertEqual(installed / 'rp_tags.json', TagsDB.get_file('rp'))
                from lynxx import server
                row = next(r for r in server.m_tags_status({}) if r['module'] == 'rp')
                self.assertEqual((True, '1.0.4', False), (row['shipped'], row['version'], row['bundled']))
                Extensions.uninstall('paheal')

    def _keep_registry(self) -> None:
        """The extension registry lives for the whole test process: put it back as it was after the test"""
        from lynxx.downloaders import DOWNLOADERS_BY_PROC_MODULE
        from lynxx.extensions import Extensions
        from lynxx.module import ProcModule
        from lynxx.tagsdb import TagsDB
        loaded, downloaders, catalog_loaded = dict(Extensions.loaded), dict(DOWNLOADERS_BY_PROC_MODULE), Extensions._catalog_loaded

        def restore() -> None:
            Extensions.loaded.clear()
            Extensions.loaded.update(loaded)
            DOWNLOADERS_BY_PROC_MODULE.clear()
            DOWNLOADERS_BY_PROC_MODULE.update(downloaders)
            Extensions._catalog_loaded = catalog_loaded
            TagsDB.clear()

        self.addCleanup(restore)
        for ext_id in ('rule34hentai', 'paheal'):  # an earlier test may have loaded the catalog's version (Extensions._register keeps it)
            if (stale := Extensions.loaded.pop(ext_id, None)) is not None:
                DOWNLOADERS_BY_PROC_MODULE.pop(ProcModule.PROC_MODULES_BY_NAME.get(stale.abbr, -1), None)

    def _install_staged(self, catalog: pathlib.Path, data_dir: pathlib.Path) -> None:
        """An install (or an update of 1.0.1) from the staged catalog brings the list, the core uses it; the others fit the limits"""
        from lynxx import tagsdb
        from lynxx.denylist import denied_in_extension
        from lynxx.extensions import Extensions
        from lynxx.tagsdb import TagsDB
        # an earlier test may have loaded the catalog's 1.0.2 (test_tag_sources), and the 1.0.1 installed below would then not
        # register (Extensions._register keeps a loaded id)
        self._keep_registry()
        # the frozen core has no 2tags/rn_tags.json (tools/build_core.py bundles the aliases only): its tagsdb.py is elsewhere
        frozen_tagsdb = (data_dir.parent / 'core' / 'lynxx' / 'tagsdb.py').as_posix()
        with mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': data_dir.as_posix(), 'LYNXX_CATALOG_DIR': catalog.as_posix()}), \
                mock.patch.object(tagsdb, '__file__', frozen_tagsdb):
            os.environ.pop('LYNXX_TAGS_DIR', None)  # restored by the patch
            old = data_dir.parent / 'old' / 'rule34hentai'  # 1.0.1 as installed before: no list
            shutil.copytree(catalog / 'rule34hentai', old, ignore=shutil.ignore_patterns('*_tags.json'))
            manifest = json.loads((old / 'manifest.json').read_text(encoding='utf-8'))
            (old / 'manifest.json').write_text(json.dumps({**manifest, 'version': '1.0.1'}), encoding='utf-8')
            Extensions.install_from_dir(old)
            TagsDB.clear()
            TagsDB.locate_default_lists()
            self.assertIsNone(TagsDB.get_file('rn'))
            row = next(r for r in Extensions.list_all() if r['id'] == 'rule34hentai')
            self.assertEqual((True, '1.0.4'), (row['updateAvailable'], row['catalogVersion']))  # "có bản v1.0.4"
            m = Extensions.install_from_catalog('rule34hentai')  # "Cập nhật lên v1.0.4"
            self.assertEqual(('rn', '1.0.4'), (m.abbr, m.version))
            installed = Extensions.user_dir() / 'rule34hentai' / 'rn_tags.json'
            self.assertTrue(installed.is_file())
            TagsDB.clear()
            TagsDB.locate_default_lists(reload=('rn',))
            self.assertEqual(installed, TagsDB.get_file('rn'))
            self.assertGreater(TagsDB.count('rn'), 10000)
            from lynxx.tags import TagTypes
            kinds = TagsDB.type_counts('rn')  # 2tags/rn_tags.json typed by name since 1.0.3 (tools/type_tag_list.py, dev log 7bf)
            self.assertGreater(min(kinds.get(t, 0) for t in (TagTypes.TAG_TYPE_ARTIST, TagTypes.TAG_TYPE_CHARACTER,
                                                              TagTypes.TAG_TYPE_COPYRIGHT)), 1000, kinds)
            for ext_id in ('paheal', 'rule34us'):  # within the installer's limits (size, files, denied domains)
                Extensions._check_tree(catalog / ext_id)
                self.assertIsNone(denied_in_extension(catalog / ext_id))
            Extensions.uninstall('rule34hentai')



class CheckSelftestTests(TestCase):
    """tools/check_selftest.py reads the app's self-test report in the release workflow"""

    def test_failed_step_names_its_false_checks(self) -> None:
        tool = _load_tool('check_selftest')
        report = {'core': {'ok': True}, 'contract': {'ok': True, 'notAllowed': {'ok': False}},
                  'indexTools': {'ok': False, 'cleanOk': True, 'importOk': False, 'importJob': {'status': 'done', 'totalAll': 0},
                                 'importCard': {'text': 'x' * 5000}, 'ui': {'ok': True, 'page': 'y' * 5000}}}
        self.assertEqual(['indexTools'], tool.failed_steps(report))  # contract.notAllowed is false on purpose
        lines = tool.describe('indexTools', report['indexTools'])
        self.assertEqual('  indexTools: false: importOk', lines[0])
        self.assertIn('    importJob: {"status": "done", "totalAll": 0}', lines)  # not hidden behind the long card text
        self.assertTrue(all(len(x) <= tool.VALUE_MAX + 40 for x in lines))
        self.assertFalse(any(x.startswith('    ui:') for x in lines))  # a nested step that passed is left out
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / 'report.json'
            path.write_text(json.dumps(report), encoding='utf-8')
            out = subprocess.run((sys.executable, str(ROOT / 'tools' / 'check_selftest.py'), str(path)), capture_output=True, text=True,
                                 timeout=60)
        self.assertEqual(1, out.returncode)
        self.assertIn('self-test failed: indexTools', out.stderr)
        self.assertIn('indexTools: false: importOk', out.stderr)


#
#
#########################################
