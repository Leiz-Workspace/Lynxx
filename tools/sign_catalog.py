#!/usr/bin/env python3
# coding=UTF-8
"""
Lynxx remote extension catalog signing (Ed25519, lynxx/trust.py). The maintainer keeps the private key offline; the app
ships only the public key (lynxx/safety_data/catalog_keys.json) and refuses unsigned or badly signed catalogs.

usage:
  python tools/sign_catalog.py genkey [--out signing-key.pem]
        new key pair: the private key (PKCS#8 PEM, mode 0600, never overwritten) and the catalog_keys.json entry to add
  python tools/sign_catalog.py pubkey --key signing-key.pem
        the catalog_keys.json entry (keyId + public key) of an existing private key
  python tools/sign_catalog.py sign index.json --key signing-key.pem [--expires-days N] [--out index.json.sig]
        signs the exact bytes of index.json into index.json.sig (publish both side by side). --expires-days writes an
        'expires' date into index.json first (the app refuses the catalog after it: re-sign before)
  python tools/sign_catalog.py verify index.json [--sig index.json.sig] [--pubkey BASE64 ...]
        checks a signature like the app does (default keys: the ones shipped with Lynxx)
The private key is the same PEM format as `openssl genpkey -algorithm ed25519`. Signing uses the 'cryptography' package when
it is installed, else lynxx.ed25519 (pure Python, RFC 8032; same deterministic signatures). Never commit a private key
(.gitignore ignores *.pem, *.key, signing-key*).
Exit code: 0 ok, 1 verification failed, 2 usage / input error.
"""
import argparse
import base64
import binascii
import datetime
import json
import os
import pathlib
import re
import secrets
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, ROOT.as_posix())

from lynxx import ed25519  # noqa: E402
from lynxx.trust import (  # noqa: E402
    SIG_SUFFIX,
    CatalogTrustError,
    TrustedKey,
    key_id,
    make_signature,
    trusted_keys,
    verify_catalog,
)

# PKCS#8 (RFC 8410) Ed25519 private key: v1 (OpenSSL) and v2 with the public key appended
PKCS8_V1_PREFIX = bytes.fromhex('302e020100300506032b657004220420')
PKCS8_V2_PREFIX = bytes.fromhex('3051020101300506032b657004220420')
PKCS8_V2_PUB = bytes.fromhex('812100')
re_pem = re.compile(r'-----BEGIN PRIVATE KEY-----\s*([A-Za-z0-9+/=\s]+?)\s*-----END PRIVATE KEY-----')
re_sha256 = re.compile(r'^[0-9a-fA-F]{64}$')


class ToolError(Exception):
    pass


def pem_encode(seed: bytes) -> str:
    body = base64.b64encode(PKCS8_V1_PREFIX + seed).decode('ascii')
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return '-----BEGIN PRIVATE KEY-----\n' + '\n'.join(lines) + '\n-----END PRIVATE KEY-----\n'


def pem_decode(text: str) -> bytes:
    """32 byte seed of an Ed25519 PKCS#8 PEM private key"""
    m = re_pem.search(text)
    if not m:
        raise ToolError('not a PEM private key (-----BEGIN PRIVATE KEY-----)')
    try:
        der = base64.b64decode(''.join(m.group(1).split()), validate=True)
    except (binascii.Error, ValueError):
        raise ToolError('the PEM body is not valid base64')
    if len(der) == 48 and der.startswith(PKCS8_V1_PREFIX):
        return der[16:]
    if len(der) == 83 and der.startswith(PKCS8_V2_PREFIX) and der[48:51] == PKCS8_V2_PUB:
        seed = der[16:48]
        if ed25519.public_key(seed) != der[51:]:
            raise ToolError('the public key stored in the PEM file does not match its private key')
        return seed
    raise ToolError('not an Ed25519 private key (PKCS#8, RFC 8410)')


def read_key(path: pathlib.Path) -> bytes:
    try:
        return pem_decode(pathlib.Path(path).read_text(encoding='ascii', errors='replace'))
    except OSError as err:
        raise ToolError(f'cannot read the key file: {err}')


def key_entry(seed: bytes, comment: str = '') -> dict:
    pub = ed25519.public_key(seed)
    return {'keyId': key_id(pub), 'publicKey': base64.b64encode(pub).decode('ascii'), 'comment': comment}


def _signer():
    """cryptography's Ed25519 (constant time) when installed, else the pure Python one"""
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    except ImportError:
        return None, 'lynxx.ed25519 (pure Python)'
    return (lambda seed, msg: Ed25519PrivateKey.from_private_bytes(seed).sign(msg)), 'cryptography'


def _inside_repo(path: pathlib.Path) -> bool:
    try:
        path.resolve().relative_to(ROOT)
        return True
    except ValueError:
        return False


def cmd_genkey(a: argparse.Namespace) -> int:
    out = pathlib.Path(a.out)
    seed = secrets.token_bytes(32)
    try:
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ToolError(f'{out} already exists: a key is never overwritten')
    with os.fdopen(fd, 'w', encoding='ascii') as f:
        f.write(pem_encode(seed))
    entry = key_entry(seed, a.comment)
    print(json.dumps({'privateKey': str(out), 'entry': entry}, indent=1))
    print(f'\nAdd "entry" to the "keys" list of {ROOT / "lynxx" / "safety_data" / "catalog_keys.json"} (public data).', file=sys.stderr)
    print(f'Keep {out} offline and backed up; never commit it.', file=sys.stderr)
    if _inside_repo(out):
        print('Warning: the private key is inside the repository folder (git ignores *.pem, but move it out anyway).', file=sys.stderr)
    return 0


def cmd_pubkey(a: argparse.Namespace) -> int:
    print(json.dumps(key_entry(read_key(pathlib.Path(a.key)), a.comment), indent=1))
    return 0


def _check_index(data: object) -> list[str]:
    if not isinstance(data, dict) or not isinstance(data.get('extensions'), list):
        raise ToolError('the index must be a json object with an "extensions" list')
    problems = []
    for n, e in enumerate(data['extensions']):
        if not isinstance(e, dict):
            problems.append(f'entry {n}: not an object')
            continue
        if not str(e.get('url', '')).lower().startswith('https://'):
            problems.append(f'entry {n} ({e.get("id")}): url must be https')
        if not re_sha256.fullmatch(str(e.get('sha256', ''))):
            problems.append(f'entry {n} ({e.get("id")}): sha256 of the archive is required')
    return problems


def cmd_sign(a: argparse.Namespace) -> int:
    index_path = pathlib.Path(a.index)
    seed = read_key(pathlib.Path(a.key))
    try:
        raw = index_path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError) as err:
        raise ToolError(f'cannot read the index: {err}')
    if problems := _check_index(data):
        raise ToolError('the app would skip these entries, fix them first:\n  ' + '\n  '.join(problems))
    if a.expires_days is not None:
        if a.expires_days < 1:
            raise ToolError('--expires-days must be at least 1')
        when = datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=a.expires_days)
        data['expires'] = when.strftime('%Y-%m-%dT%H:%M:%SZ')
        raw = (json.dumps(data, indent=2, ensure_ascii=False) + '\n').encode('utf-8')
        index_path.write_bytes(raw)
    signer, how = _signer()
    doc = make_signature(raw, seed, signer=signer)
    sig_path = pathlib.Path(a.out) if a.out else index_path.with_name(index_path.name + SIG_SUFFIX)
    sig_path.write_text(json.dumps(doc, indent=1) + '\n', encoding='utf-8')
    shipped = {k.key_id for k in trusted_keys()}
    print(json.dumps({'index': str(index_path), 'signature': str(sig_path), 'keyId': doc['keyId'], 'signer': how,
                      'expires': data.get('expires', ''), 'entries': len(data['extensions']),
                      'trustedByThisLynxx': doc['keyId'] in shipped}, indent=1))
    if doc['keyId'] not in shipped:
        print('Warning: this key is not in lynxx/safety_data/catalog_keys.json: Lynxx builds without it refuse the catalog.',
              file=sys.stderr)
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    index_path = pathlib.Path(a.index)
    sig_path = pathlib.Path(a.sig) if a.sig else index_path.with_name(index_path.name + SIG_SUFFIX)
    try:
        raw = index_path.read_bytes()
        sig = sig_path.read_bytes() if sig_path.is_file() else None
    except OSError as err:
        raise ToolError(str(err))
    keys = None
    if a.pubkey:
        keys = []
        for value in a.pubkey:
            try:
                pub = base64.b64decode(value, validate=True)
            except (binascii.Error, ValueError):
                pub = b''
            if len(pub) != 32:
                raise ToolError(f'--pubkey {value[:12]}…: expected base64 of 32 bytes')
            keys.append(TrustedKey(key_id(pub), pub, '', '--pubkey'))
    try:
        res = verify_catalog(raw, sig, keys=keys)
    except CatalogTrustError as err:
        print(json.dumps({'ok': False, 'error': str(err)}, ensure_ascii=False, indent=1))
        return 1
    print(json.dumps({'ok': True, 'signed': res.signed, 'keyId': res.key_id, 'expires': res.expires}, indent=1))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog='tools/sign_catalog.py', description='Sign / verify the Lynxx remote extension catalog')
    sub = ap.add_subparsers(dest='cmd', required=True)
    g = sub.add_parser('genkey', help='new Ed25519 key pair')
    g.add_argument('--out', default='signing-key.pem')
    g.add_argument('--comment', default='Lynxx catalog key')
    k = sub.add_parser('pubkey', help='catalog_keys.json entry of a private key')
    k.add_argument('--key', required=True)
    k.add_argument('--comment', default='Lynxx catalog key')
    s = sub.add_parser('sign', help='sign index.json')
    s.add_argument('index')
    s.add_argument('--key', required=True)
    s.add_argument('--expires-days', type=int, default=None)
    s.add_argument('--out', default='')
    v = sub.add_parser('verify', help='verify index.json like the app')
    v.add_argument('index')
    v.add_argument('--sig', default='')
    v.add_argument('--pubkey', action='append', default=[], help='base64 public key to trust instead of the shipped keys')
    a = ap.parse_args(argv)
    try:
        return {'genkey': cmd_genkey, 'pubkey': cmd_pubkey, 'sign': cmd_sign, 'verify': cmd_verify}[a.cmd](a)
    except (ToolError, CatalogTrustError) as err:
        print(f'error: {err}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
