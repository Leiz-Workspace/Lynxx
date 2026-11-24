# coding=UTF-8
"""
Lynxx: tests of the signed remote extension catalog (lynxx/ed25519.py, lynxx/trust.py, lynxx/extensions.py,
tools/sign_catalog.py), offline. Development key pairs are generated in temporary folders for these tests only.
Run: python -m unittest lynxx.tests.test_catalog_trust
"""
#########################################
#
#

import base64
import contextlib
import datetime
import hashlib
import importlib.util
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from unittest import TestCase, mock

from lynxx.logger import Logger

Logger.init(True, True)

from lynxx import ed25519, trust  # noqa: E402
from lynxx.extensions import ExtensionError, Extensions  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[2]

# RFC 8032 section 7.1: (secret key, public key, message, signature)
RFC8032 = [
    ('9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60',
     'd75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a', '',
     'e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b'),
    ('4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb',
     '3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c', '72',
     '92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00'),
    ('c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7',
     'fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025', 'af82',
     '6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a'),
    ('833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42',
     'ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf', hashlib.sha512(b'abc').hexdigest(),
     'dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b58909351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704'),
]
SEED = bytes(range(32))  # development key of these tests only
OTHER_SEED = bytes(range(1, 33))
CATALOG_URL = 'https://catalog.example/lynxx/index.json'


def _key(seed: bytes) -> trust.TrustedKey:
    pub = ed25519.public_key(seed)
    return trust.TrustedKey(trust.key_id(pub), pub)


def _sig(index: bytes, seed: bytes = SEED) -> bytes:
    return json.dumps(trust.make_signature(index, seed)).encode()


def _load_tool(name: str = 'sign_catalog'):
    spec = importlib.util.spec_from_file_location(f'{name}_tool', ROOT / 'tools' / f'{name}.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Ed25519Tests(TestCase):
    def test_rfc8032_vectors(self) -> None:
        for sk, pk, msg, sig in RFC8032:
            sk, pk, msg, sig = (bytes.fromhex(x) for x in (sk, pk, msg, sig))
            with self.subTest(pk=pk.hex()[:8]):
                self.assertEqual(pk, ed25519.public_key(sk))
                self.assertEqual(sig, ed25519.sign(sk, msg))
                self.assertTrue(ed25519.verify(pk, msg, sig))
                self.assertFalse(ed25519.verify(pk, msg + b'\0', sig))
                bad = bytearray(sig)
                bad[5] ^= 1
                self.assertFalse(ed25519.verify(pk, msg, bytes(bad)))

    def test_malformed_inputs(self) -> None:
        _sk, pk, msg, sig = (bytes.fromhex(x) for x in RFC8032[1])
        s = int.from_bytes(sig[32:], 'little')
        malleable = sig[:32] + int.to_bytes(s + ed25519._Q, 32, 'little')  # S + L: same point equation, must be refused
        self.assertFalse(ed25519.verify(pk, msg, malleable))
        self.assertFalse(ed25519.verify(pk[:31], msg, sig))
        self.assertFalse(ed25519.verify(pk, msg, sig[:63]))
        self.assertFalse(ed25519.verify(b'\xff' * 32, msg, sig))  # not a curve point
        self.assertFalse(ed25519.verify('not bytes', msg, sig))  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            ed25519.public_key(b'short')


class CatalogTrustTests(TestCase):
    index = json.dumps({'extensions': [{'id': 'demo', 'url': 'https://x.example/demo.zip', 'sha256': 'ab' * 32}]}).encode()

    def test_signed_catalog(self) -> None:
        res = trust.verify_catalog(self.index, _sig(self.index), keys=[_key(SEED)])
        self.assertEqual((True, _key(SEED).key_id), (res.signed, res.key_id))

    def test_refusals(self) -> None:
        keys = [_key(SEED)]
        tampered = self.index.replace(b'demo.zip', b'evil.zip')
        cases = {
            'tampered index': (tampered, _sig(self.index), keys, 'không hợp lệ'),
            'other key': (self.index, _sig(self.index, OTHER_SEED), keys, 'không tin cậy'),
            'unsigned': (self.index, None, keys, 'chưa được ký'),
            'placeholder keys': (self.index, _sig(self.index), [], 'khoá giữ chỗ'),
            'bad format': (self.index, b'{"format": "x"}', keys, 'định dạng'),
            'not json': (self.index, b'\x00garbage', keys, 'định dạng'),
            'short signature': (self.index, json.dumps({**json.loads(_sig(self.index)), 'signature': 'AAAA'}).encode(), keys, 'Chữ ký'),
        }
        # same key id but the signature of another message (no signing context): a raw Ed25519 signature of the bytes is not accepted
        raw = base64.b64encode(ed25519.sign(SEED, self.index)).decode()
        cases['no context'] = (self.index, json.dumps({**json.loads(_sig(self.index)), 'signature': raw}).encode(), keys, 'không hợp lệ')
        for name, (index, sig, k, words) in cases.items():
            with self.subTest(name), self.assertRaises(trust.CatalogTrustError) as ctx:
                trust.verify_catalog(index, sig, keys=k)
            self.assertIn(words, str(ctx.exception))

    def test_expiry(self) -> None:
        now = datetime.datetime(2026, 11, 17, tzinfo=datetime.UTC)
        for expires, ok in (('2026-12-01T00:00:00Z', True), ('2026-11-01', False), ('soon', False)):
            index = json.dumps({'expires': expires, 'extensions': []}).encode()
            with self.subTest(expires=expires):
                if ok:
                    self.assertEqual(expires, trust.verify_catalog(index, _sig(index), keys=[_key(SEED)], now=now).expires)
                else:
                    with self.assertRaises(trust.CatalogTrustError):
                        trust.verify_catalog(index, _sig(index), keys=[_key(SEED)], now=now)

    def test_developer_overrides(self) -> None:
        with mock.patch.dict(os.environ, {trust.ENV_ALLOW_UNSIGNED: '1'}):
            self.assertFalse(trust.verify_catalog(self.index, None, keys=[]).signed)
            with self.assertRaises(trust.CatalogTrustError):  # a signature that does not verify is never accepted
                trust.verify_catalog(self.index.replace(b'demo', b'evil'), _sig(self.index), keys=[_key(SEED)])
        with mock.patch.dict(os.environ, {trust.ENV_ALLOW_UNSIGNED: 'yes'}), self.assertRaises(trust.CatalogTrustError):
            trust.verify_catalog(self.index, None, keys=[])  # only the exact value '1'
        with tempfile.TemporaryDirectory() as tmp:
            keys_file = pathlib.Path(tmp) / 'dev-keys.json'
            pub = ed25519.public_key(SEED)
            keys_file.write_text(json.dumps({'keys': [{'keyId': trust.key_id(pub), 'publicKey': base64.b64encode(pub).decode()}]}))
            with mock.patch.dict(os.environ, {trust.ENV_KEYS_FILE: str(keys_file)}):
                self.assertTrue(trust.verify_catalog(self.index, _sig(self.index)).signed)
            keys_file.write_text(json.dumps({'keys': [{'keyId': '0000000000000000', 'publicKey': base64.b64encode(pub).decode()}]}))
            with mock.patch.dict(os.environ, {trust.ENV_KEYS_FILE: str(keys_file)}), self.assertRaises(trust.CatalogTrustError):
                trust.trusted_keys()

    def test_shipped_keys_and_sig_url(self) -> None:
        keys = trust.load_keys(trust.KEYS_FILE)
        self.assertIn('b852144452eea5dc', [k.key_id for k in keys])  # the official catalog key ships with the app
        self.assertEqual('https://h.example/c/index.json.sig?v=2', trust.sig_url('https://h.example/c/index.json?v=2#x'))


class RemoteCatalogTests(TestCase):
    """fetch_remote_catalog / install_from_remote_catalog with a fake https transport"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.patches = [mock.patch.object(Extensions, 'user_dir', return_value=self.root / 'user'),
                        mock.patch.object(Extensions, 'catalog_dir', return_value=self.root / 'catalog'),
                        mock.patch.object(trust, 'trusted_keys', return_value=[_key(SEED)])]
        for p in self.patches:
            p.start()
        self.served: dict[str, bytes | None] = {}
        self.requested: list[str] = []

    def tearDown(self) -> None:
        for p in self.patches:
            p.stop()
        self._tmp.cleanup()

    def _get(self, url, limit, timeout, missing_ok=False):
        self.requested.append(url)
        if url not in self.served:
            if missing_ok:
                return None
            raise ExtensionError(f'404 {url}')
        return self.served[url]

    def _publish(self, entries: list[dict], seed: bytes | None = SEED, tamper: bool = False) -> None:
        index = json.dumps({'extensions': entries}).encode()
        self.served[CATALOG_URL] = index + (b' ' if tamper else b'')  # one byte added after signing: tampered
        if seed is not None:
            self.served[trust.sig_url(CATALOG_URL)] = _sig(index, seed)

    def _archive(self, ext_id: str = 'remotedemo', abbr: str = 'zr1') -> tuple[str, str]:
        from lynxx.extgen.sandbox import pack
        from lynxx.extgen.scaffold import create_draft, survey
        from lynxx.tests.test_extgen import SITE, fake_fetcher
        draft = create_draft(self.root / 'drafts', survey(SITE, fake_fetcher()), ext_id=ext_id, abbr=abbr, engine='gelbooru_dapi')
        z = pack(draft, self.root / f'{ext_id}.zip')
        url = f'https://files.example/{ext_id}.zip'
        self.served[url] = pathlib.Path(z['path']).read_bytes()
        return url, z['sha256']

    def test_entries_are_verified_and_flagged(self) -> None:
        self._publish([{'id': 'good', 'url': 'https://f.example/g.zip', 'sha256': 'a' * 64, 'site': 'https://booru.example/'},
                       {'id': 'yt', 'url': 'https://f.example/y.zip', 'sha256': 'b' * 64, 'site': 'https://www.youtube.com/'},
                       {'id': 'plain-http', 'url': 'http://f.example/p.zip', 'sha256': 'c' * 64},
                       {'id': 'BAD ID', 'url': 'https://f.example/b.zip', 'sha256': 'd' * 64}])
        with mock.patch.object(Extensions, '_https_get', side_effect=self._get):
            items = Extensions.fetch_remote_catalog(CATALOG_URL)
        self.assertEqual(['good', 'yt'], [i['id'] for i in items])
        self.assertTrue(all(i['signed'] and i['keyId'] == _key(SEED).key_id for i in items))
        self.assertNotIn('denied', items[0])
        self.assertEqual('R7', items[1]['denied']['rule'])
        self.assertIn(trust.sig_url(CATALOG_URL), self.requested)

    def test_untrusted_catalogs_are_refused(self) -> None:
        for name, kw in (('unsigned', {'seed': None}), ('other key', {'seed': OTHER_SEED}), ('tampered', {'tamper': True})):
            self.served.clear()
            self._publish([{'id': 'good', 'url': 'https://f.example/g.zip', 'sha256': 'a' * 64}], **kw)
            with self.subTest(name), mock.patch.object(Extensions, '_https_get', side_effect=self._get), \
                    self.assertRaises(ExtensionError) as ctx:
                Extensions.fetch_remote_catalog(CATALOG_URL)
            self.assertRegex(str(ctx.exception), 'chưa được ký|không tin cậy|không hợp lệ')

    def test_install_from_the_signed_catalog(self) -> None:
        url, sha = self._archive()
        self._publish([{'id': 'remotedemo', 'name': 'Remote demo', 'url': url, 'sha256': sha, 'site': 'https://fakebooru.test/'}])
        with mock.patch.object(Extensions, '_https_get', side_effect=self._get):
            with self.assertRaises(ExtensionError) as ctx:  # the catalog changed since the user confirmed another sha256
                Extensions.install_from_remote_catalog(CATALOG_URL, 'remotedemo', 'f' * 64)
            self.assertIn('Catalog đã thay đổi', str(ctx.exception))
            self.assertNotIn(url, self.requested)
            try:
                m = Extensions.install_from_remote_catalog(CATALOG_URL, 'remotedemo', sha)
                self.assertEqual('remotedemo', m.id)
                self.assertTrue((self.root / 'user' / 'remotedemo' / 'manifest.json').is_file())
            finally:
                Extensions.uninstall('remotedemo')

    def test_install_refusals(self) -> None:
        url, sha = self._archive('remotedemo2', 'zr2')
        entries = [{'id': 'announced', 'url': url, 'sha256': sha},  # the archive holds another extension id
                   {'id': 'ytdemo', 'url': url, 'sha256': sha, 'site': 'https://youtu.be/'}]
        self._publish(entries)
        with mock.patch.object(Extensions, '_https_get', side_effect=self._get):
            with self.assertRaises(ExtensionError) as ctx:
                Extensions.install_from_remote_catalog(CATALOG_URL, 'announced')
            self.assertIn('not \'announced\'', str(ctx.exception))
            self.requested.clear()
            with self.assertRaises(ExtensionError) as ctx:
                Extensions.install_from_remote_catalog(CATALOG_URL, 'ytdemo')
            self.assertIn('danh sách chặn', str(ctx.exception))
            self.assertNotIn(url, self.requested)  # a denied entry is never downloaded
            with self.assertRaises(ExtensionError):
                Extensions.install_from_remote_catalog(CATALOG_URL, 'missing')
            self.served[CATALOG_URL] += b' '  # tampered after signing: refused before any archive download
            self.requested.clear()
            with self.assertRaises(ExtensionError):
                Extensions.install_from_remote_catalog(CATALOG_URL, 'announced')
            self.assertNotIn(url, self.requested)
        self.assertFalse((self.root / 'user' / 'remotedemo2').exists())

    def test_sidecar_install_method(self) -> None:
        from lynxx import server
        url, sha = self._archive('remotedemo3', 'zr3')
        self._publish([{'id': 'remotedemo3', 'url': url, 'sha256': sha}])
        with mock.patch.object(Extensions, '_https_get', side_effect=self._get):
            try:
                res = server.m_ext_install({'remoteId': 'remotedemo3', 'catalogUrl': CATALOG_URL, 'sha256': sha})
                self.assertEqual('remotedemo3', res['id'])
            finally:
                Extensions.uninstall('remotedemo3')


class SignToolTests(TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.tool = _load_tool()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _run(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = self.tool.main(list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_genkey_sign_verify(self) -> None:
        key = self.root / 'signing-key.pem'
        code, out, _ = self._run('genkey', '--out', str(key))
        self.assertEqual(0, code)
        entry = json.loads(out)['entry']
        if os.name == 'posix':
            self.assertEqual(0o600, key.stat().st_mode & 0o777)
        self.assertEqual(2, self._run('genkey', '--out', str(key))[0])  # never overwritten
        self.assertEqual(entry, json.loads(self._run('pubkey', '--key', str(key))[1]))
        index = self.root / 'index.json'
        index.write_text(json.dumps({'extensions': [{'id': 'demo', 'url': 'https://x.example/d.zip', 'sha256': 'ab' * 32}]}))
        code, out, _ = self._run('sign', str(index), '--key', str(key), '--expires-days', '30')
        self.assertEqual(0, code)
        self.assertEqual(entry['keyId'], json.loads(out)['keyId'])
        self.assertTrue(json.loads(index.read_text())['expires'].endswith('Z'))
        self.assertEqual(0, self._run('verify', str(index), '--pubkey', entry['publicKey'])[0])
        self.assertEqual(1, self._run('verify', str(index))[0])  # not one of the shipped keys
        pub = base64.b64decode(entry['publicKey'])
        res = trust.verify_catalog(index.read_bytes(), (self.root / 'index.json.sig').read_bytes(),
                                   keys=[trust.TrustedKey(entry['keyId'], pub)])
        self.assertTrue(res.signed)
        index.write_text(index.read_text().replace('d.zip', 'e.zip'))
        self.assertEqual(1, self._run('verify', str(index), '--pubkey', entry['publicKey'])[0])
        index.write_text(json.dumps({'extensions': [{'id': 'demo', 'url': 'http://x.example/d.zip'}]}))
        self.assertEqual(2, self._run('sign', str(index), '--key', str(key))[0])  # entries the app would skip

    def test_pem_format(self) -> None:
        pem = self.tool.pem_encode(SEED)
        self.assertEqual(SEED, self.tool.pem_decode(pem))
        with self.assertRaises(self.tool.ToolError):
            self.tool.pem_decode(pem.replace('PRIVATE KEY', 'PUBLIC KEY'))

    @unittest.skipUnless(shutil.which('openssl'), 'openssl is not installed')
    def test_openssl_interop(self) -> None:
        key = self.root / 'ossl.pem'
        # Ed25519 signatures need the one-shot 'pkeyutl -rawin' of OpenSSL 3: not in OpenSSL 1.1 or LibreSSL (macOS runners)
        usage = subprocess.run(['openssl', 'pkeyutl', '-help'], capture_output=True, text=True)
        if '-rawin' not in usage.stdout + usage.stderr:
            version = subprocess.run(['openssl', 'version'], capture_output=True, text=True).stdout.strip()
            self.skipTest(f'{version or "openssl"} has no pkeyutl -rawin')
        if subprocess.run(['openssl', 'genpkey', '-algorithm', 'ed25519', '-out', str(key)], capture_output=True).returncode:
            self.skipTest('this openssl has no Ed25519')
        seed = self.tool.read_key(key)
        pub_der = subprocess.run(['openssl', 'pkey', '-in', str(key), '-pubout', '-outform', 'DER'], capture_output=True, check=True).stdout
        self.assertEqual(pub_der[-32:], ed25519.public_key(seed))
        msg = self.root / 'msg.bin'
        msg.write_bytes(trust.signed_message(b'{"extensions": []}\n'))
        sig = self.root / 'sig.bin'
        sig.write_bytes(ed25519.sign(seed, msg.read_bytes()))
        r = subprocess.run(['openssl', 'pkeyutl', '-verify', '-inkey', str(key), '-rawin', '-in', str(msg), '-sigfile', str(sig)],
                           capture_output=True, text=True)
        self.assertEqual(0, r.returncode, r.stdout + r.stderr)


class BuildCatalogTests(TestCase):
    """tools/build_catalog.py: the official catalog is built from extensions/"""
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self._tmp.name)
        self.tool = _load_tool('build_catalog')

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_archives_are_deterministic_and_the_index_signable(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, self.tool.main(['--out', str(self.root / 'a')]))
            self.assertEqual(0, self.tool.main(['--out', str(self.root / 'b'), '--base-url', 'https://h.example/c']))
        index = json.loads((self.root / 'a' / 'index.json').read_text(encoding='utf-8'))
        ids = sorted(p.parent.name for p in (ROOT / 'extensions').glob('*/manifest.json'))
        self.assertEqual(ids, [e['id'] for e in index['extensions']])
        self.assertEqual([], _load_tool()._check_index(index))  # the signing tool would sign every entry
        for e in index['extensions']:
            name = f"{e['id']}-{e['version']}.zip"
            self.assertEqual(self.tool.OFFICIAL_BASE_URL + name, e['url'])
            data = (self.root / 'a' / name).read_bytes()
            self.assertEqual(data, (self.root / 'b' / name).read_bytes())  # same files, same bytes
            self.assertEqual(e['sha256'], hashlib.sha256(data).hexdigest())
        other = json.loads((self.root / 'b' / 'index.json').read_text(encoding='utf-8'))
        self.assertTrue(all(e['url'].startswith('https://h.example/c/') for e in other['extensions']))
        # a catalog signed with a trusted key installs: the archive passes the checks of the core
        raw = (self.root / 'a' / 'index.json').read_bytes()
        self.assertTrue(trust.verify_catalog(raw, _sig(raw), keys=[_key(SEED)]).signed)
        entry = index['extensions'][0]
        with mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': str(self.root / 'data')}):
            m = Extensions.install_from_archive((self.root / 'a' / f"{entry['id']}-{entry['version']}.zip").read_bytes(), entry['sha256'],
                                                expect_id=entry['id'])
        self.assertEqual((entry['id'], entry['version']), (m.id, m.version))

    def test_tag_lists_come_from_the_lists_folder(self) -> None:
        # tag lists stay out of the repository (docs/plans/ext-tag-jobs.md): --lists puts each one into its extension's zip
        exts = self.root / 'ext'
        shutil.copytree(ROOT / 'extensions' / 'bbooru', exts / 'bbooru', ignore=shutil.ignore_patterns('__pycache__'))
        lists = self.root / 'lists'
        lists.mkdir()
        (lists / 'bb_tags.json').write_text('{"blue_sky": "12 posts (general)"}\n', encoding='utf-8')
        names = {}
        with contextlib.redirect_stdout(io.StringIO()):
            for run in ('a', 'b'):
                self.assertEqual(0, self.tool.main(['--out', str(self.root / run), '--extensions', str(exts), '--lists', str(lists)]))
                entry = json.loads((self.root / run / 'index.json').read_text(encoding='utf-8'))['extensions'][0]
                names[run] = zipfile_names(self.root / run / f"{entry['id']}-{entry['version']}.zip")
        data = (self.root / 'a' / f"{entry['id']}-{entry['version']}.zip").read_bytes()
        self.assertEqual(data, (self.root / 'b' / f"{entry['id']}-{entry['version']}.zip").read_bytes())  # still deterministic
        self.assertIn('bb_tags.json', names['a'])
        with mock.patch.dict(os.environ, {'LYNXX_DATA_DIR': str(self.root / 'data')}):
            Extensions.install_from_archive(data, entry['sha256'], expect_id='bbooru')
            self.assertTrue((Extensions.user_dir() / 'bbooru' / 'bb_tags.json').is_file())  # the app uses it as soon as installed
        # a list that is not in the folder: built without it, and said so
        err = io.StringIO()
        (lists / 'bb_tags.json').unlink()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(0, self.tool.main(['--out', str(self.root / 'c'), '--extensions', str(exts), '--lists', str(lists)]))
        self.assertNotIn('bb_tags.json', zipfile_names(self.root / 'c' / f"{entry['id']}-{entry['version']}.zip"))
        self.assertIn('bbooru: no bb_tags.json', err.getvalue())
        # not a tag list, or a zip the installer would refuse: no catalog
        (lists / 'bb_tags.json').write_text('[]', encoding='utf-8')
        with self.assertRaises(SystemExit):
            self.tool.build(exts, self.root / 'd', self.tool.OFFICIAL_BASE_URL, lists)
        (lists / 'bb_tags.json').write_text('{"blue_sky": "12 posts (general)"}', encoding='utf-8')
        with mock.patch.object(self.tool, 'ARCHIVE_SIZE_MAX', 100), self.assertRaises(SystemExit) as ctx:
            self.tool.build(exts, self.root / 'e', self.tool.OFFICIAL_BASE_URL, lists)
        self.assertIn('over the installer limits', str(ctx.exception))

    def test_extensions_without_a_tag_source_take_the_repository_list(self) -> None:
        # Rule34Hentai reads no list from its site: without a list in its zip, an update from the catalog would leave rn with none.
        # Its list comes from 2tags/ (as in the catalog bundled with the core), and a build without it anywhere stops
        exts = self.root / 'ext'
        shutil.copytree(ROOT / 'extensions' / 'rule34hentai', exts / 'rule34hentai', ignore=shutil.ignore_patterns('__pycache__', '*_tags.json'))
        tags = self.root / '2tags'
        tags.mkdir()
        (tags / 'rn_tags.json').write_text('{"blue_sky": "12 posts (general)"}\n', encoding='utf-8')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            index = self.tool.build(exts, self.root / 'a', self.tool.OFFICIAL_BASE_URL, None, tags)
        entry = index['extensions'][0]
        zpath = self.root / 'a' / f"{entry['id']}-{entry['version']}.zip"
        self.assertIn('rn_tags.json', zipfile_names(zpath))
        self.assertEqual('', err.getvalue())
        with zipfile.ZipFile(zpath) as zf:
            self.assertEqual((tags / 'rn_tags.json').read_bytes(), zf.read('rn_tags.json'))
        lists = self.root / 'lists'  # a list given with --lists wins
        lists.mkdir()
        (lists / 'rn_tags.json').write_text('{"red_sky": "3 posts (general)"}\n', encoding='utf-8')
        self.tool.build(exts, self.root / 'b', self.tool.OFFICIAL_BASE_URL, lists, tags)
        with zipfile.ZipFile(self.root / 'b' / zpath.name) as zf:
            self.assertEqual((lists / 'rn_tags.json').read_bytes(), zf.read('rn_tags.json'))
        (tags / 'rn_tags.json').unlink()
        with self.assertRaises(SystemExit) as ctx:
            self.tool.build(exts, self.root / 'c', self.tool.OFFICIAL_BASE_URL, None, tags)
        self.assertIn('rule34hentai: no rn_tags.json', str(ctx.exception))
        # the repository's own lists: the official build of rn, rp and rs carries them
        for ext_id, name in (('rule34hentai', 'rn_tags.json'), ('paheal', 'rp_tags.json'), ('rule34us', 'rs_tags.json')):
            manifest = json.loads((ROOT / 'extensions' / ext_id / 'manifest.json').read_text(encoding='utf-8'))
            in_folder = (ROOT / 'extensions' / ext_id / name).is_file()  # a maintainer's copy next to the manifest is used as is
            self.assertEqual(None if in_folder else ROOT / '2tags' / name, self.tool.tag_list_source(ROOT / 'extensions' / ext_id, manifest, None))
        manifest = json.loads((ROOT / 'extensions' / 'bbooru' / 'manifest.json').read_text(encoding='utf-8'))
        self.assertIsNone(self.tool.tag_list_source(ROOT / 'extensions' / 'bbooru', manifest, None), 'bbooru reads its list from its source')

    def test_a_merge_source_takes_the_repository_list_and_needs_one(self) -> None:
        # Paheal 1.0.3 updates its list with its own update, which reads only the tags with 100+ posts and merges them into the list
        # in use (tagUpdate.merge): its zip still carries the 2tags/ list (as 1.0.2 did), the base of every merge; without it
        # anywhere, or with a list its update wrote alone, there is no catalog
        exts = self.root / 'ext'
        shutil.copytree(ROOT / 'extensions' / 'paheal', exts / 'paheal', ignore=shutil.ignore_patterns('__pycache__', '*_tags.json'))
        manifest = json.loads((exts / 'paheal' / 'manifest.json').read_text(encoding='utf-8'))
        src = self.tool.tag_source(exts / 'paheal')
        self.assertEqual((True, 100), (src.merge, src.merge_min_posts))
        self.assertTrue(self.tool.needs_shipped_list(src))
        tags = self.root / '2tags'
        tags.mkdir()
        (tags / 'rp_tags.json').write_text('{\n"blue_sky": "120 posts",\n"red_sky": "12 posts"\n}\n', encoding='utf-8')
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            index = self.tool.build(exts, self.root / 'a', self.tool.OFFICIAL_BASE_URL, None, tags)
        entry = index['extensions'][0]
        self.assertEqual(('paheal', '1.0.4'), (entry['id'], entry['version']))
        self.assertEqual({'id', 'name', 'abbr', 'version', 'url', 'sha256', 'site', 'description'}, set(entry))  # no manifest field
        with zipfile.ZipFile(self.root / 'a' / f"paheal-{manifest['version']}.zip") as zf:
            self.assertEqual((tags / 'rp_tags.json').read_bytes(), zf.read('rp_tags.json'))
            self.assertIn('tags.py', zf.namelist())
        # cores before 2.0.10 refuse the manifest: the build says so (publish once 2.0.10 is out, docs/operations.md 3d)
        self.assertIn('paheal 1.0.4: tagUpdate.merge (Lynxx 2.0.10): older cores refuse to install it', err.getvalue())
        lists = self.root / 'lists'  # a list its own update wrote alone (the tags with 100+ posts): refused
        lists.mkdir()
        (lists / 'rp_tags.json').write_text('{\n"blue_sky": "130 posts (unknown)"\n}\n', encoding='utf-8')
        (tags / 'rp_tags.json').write_text('{\n"a": "1 posts",\n"b": "2 posts",\n"blue_sky": "120 posts"\n}\n', encoding='utf-8')
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            self.tool.build(exts, self.root / 'b', self.tool.OFFICIAL_BASE_URL, lists, tags)
        self.assertIn('has 1 tags, under half of', str(ctx.exception))
        (lists / 'rp_tags.json').write_text('{\n"a": "1 posts",\n"blue_sky": "130 posts (unknown)"\n}\n', encoding='utf-8')
        self.assertEqual(lists / 'rp_tags.json', self.tool.tag_list_source(exts / 'paheal', manifest, lists, tags))  # merged: taken
        (tags / 'rp_tags.json').unlink()
        with self.assertRaises(SystemExit) as ctx, contextlib.redirect_stderr(io.StringIO()):
            self.tool.build(exts, self.root / 'c', self.tool.OFFICIAL_BASE_URL, None, tags)
        self.assertIn('paheal: no rp_tags.json', str(ctx.exception))
        self.assertIn('its own update reads only the tags with 100+ posts and merges them into this list', str(ctx.exception))

    def test_a_replace_job_never_takes_the_repository_list(self) -> None:
        # an extension whose own update reads the whole list (TBIB) is built without a list when none is given: the user makes it
        # with "Cập nhật", the core stays small; a list in 2tags/ is not taken for it
        tags = self.root / '2tags'
        tags.mkdir()
        (tags / 'tb_tags.json').write_text('{"blue_sky": "12 posts (general)"}\n', encoding='utf-8')
        manifest = json.loads((ROOT / 'extensions' / 'tbib' / 'manifest.json').read_text(encoding='utf-8'))
        self.assertFalse(self.tool.tag_source(ROOT / 'extensions' / 'tbib').merge)
        self.assertIsNone(self.tool.tag_list_source(ROOT / 'extensions' / 'tbib', manifest, None, tags))
        self.assertEqual([], self.tool.newer_core_keys(manifest))  # tagUpdate.entry: every core since 2.0.7 knows it

    def test_repository_extensions_carry_no_tag_list(self) -> None:
        # a list is data, not code: it goes into the catalog zips (tools/build_catalog.py), not into the repository, which the app
        # also bundles (tools/build_core.py copies extensions/ into the frozen core). Committed files only: a maintainer keeps the
        # lists next to their manifest, where .gitignore leaves them out
        tracked = subprocess.run(('git', 'ls-files', '-z', '--', 'extensions'), cwd=ROOT, capture_output=True, timeout=60)
        if tracked.returncode == 0:
            files = [ROOT / x for x in tracked.stdout.decode('utf-8').split('\0') if x]
        else:  # not a git checkout (source archive)
            files = [p for p in (ROOT / 'extensions').rglob('*') if p.is_file()]
        big = [str(p.relative_to(ROOT)) for p in files if p.is_file() and (p.name.endswith('_tags.json') or p.stat().st_size > 1024 * 1024)]
        self.assertEqual([], big, 'tag lists belong in the catalog zips: tools/build_catalog.py (docs/plans/ext-tag-jobs.md)')
        ignored = subprocess.run(('git', 'check-ignore', '-q', 'extensions/bbooru/bb_tags.json'), cwd=ROOT, capture_output=True,
                                 timeout=60)
        if tracked.returncode == 0:
            self.assertEqual(0, ignored.returncode, '.gitignore must leave out extensions/*/*_tags.json')


def zipfile_names(path: pathlib.Path) -> list[str]:
    import zipfile
    with zipfile.ZipFile(path) as zf:
        return zf.namelist()


if __name__ == '__main__':
    sys.exit(unittest.main())

#
#
#########################################
