# coding=UTF-8
"""
Lynxx: trust of the remote extension catalog (plan site-extension-skill-and-ai-generator, B.8).
The maintainer signs the exact bytes of the catalog index with Ed25519 (tools/sign_catalog.py); the signature is a small
JSON file next to the index (<index url>.sig, see sig_url). Before any entry is read, the core verifies it against the public
keys shipped with Lynxx (lynxx/safety_data/catalog_keys.json). No trusted key, no signature, an unknown key, a bad
signature or an expired index ('expires' field) -> the whole catalog is refused (CatalogTrustError, Vietnamese message for the
app). Entries carry the sha256 of their archive, so a verified index pins every archive it lists.
Developer overrides, environment only (never settings, never the renderer):
- LYNXX_CATALOG_KEYS_FILE=<file>: extra trusted keys, same format as catalog_keys.json (a self-hosted test catalog);
- LYNXX_CATALOG_ALLOW_UNSIGNED=1: a catalog without a .sig file is accepted, its entries are flagged signed: false.
A signature that is present but does not verify is never accepted.
"""
#########################################
#
#

from __future__ import annotations

import base64
import binascii
import datetime
import hashlib
import json
import os
import pathlib
from dataclasses import dataclass
from urllib import parse as url_parse

from . import ed25519

__all__ = ('CatalogTrust', 'CatalogTrustError', 'TrustedKey', 'key_id', 'make_signature', 'sig_url', 'trusted_keys', 'verify_catalog')

KEYS_FILE = pathlib.Path(__file__).resolve().parent / 'safety_data' / 'catalog_keys.json'
ENV_KEYS_FILE = 'LYNXX_CATALOG_KEYS_FILE'
ENV_ALLOW_UNSIGNED = 'LYNXX_CATALOG_ALLOW_UNSIGNED'
SIG_FORMAT = 'lynxx-catalog-sig/1'
SIG_ALG = 'ed25519'
SIG_SUFFIX = '.sig'
SIG_SIZE_MAX = 4096
SIGNING_CONTEXT = b'lynxx-catalog-v1\n'
'''prepended to the index bytes before signing: a catalog signature can never be replayed as another kind of signature'''


class CatalogTrustError(Exception):
    """The catalog cannot be trusted. The message is shown to the user as it is"""


@dataclass(frozen=True)
class TrustedKey:
    key_id: str
    public_key: bytes
    comment: str = ''
    source: str = ''


@dataclass(frozen=True)
class CatalogTrust:
    signed: bool
    key_id: str = ''
    expires: str = ''


def key_id(public: bytes) -> str:
    """Short public key fingerprint: first 16 hex digits of its sha256"""
    return hashlib.sha256(public).hexdigest()[:16]


def _b64(value: object, size: int, what: str) -> bytes:
    try:
        raw = base64.b64decode(str(value), validate=True)
    except (binascii.Error, ValueError):
        raw = b''
    if len(raw) != size:
        raise CatalogTrustError(f'{what} không hợp lệ (cần {size} byte, mã base64).')
    return raw


def load_keys(path: pathlib.Path, source: str = '') -> list[TrustedKey]:
    """{"keys": [{"keyId", "publicKey" (base64 of 32 bytes), "comment"}]}. An empty list is the placeholder state"""
    try:
        data = json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError) as err:
        raise CatalogTrustError(f'Không đọc được file khoá tin cậy {pathlib.Path(path).name}: {err}')
    rows = data.get('keys') if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise CatalogTrustError(f'File khoá tin cậy {pathlib.Path(path).name} thiếu danh sách "keys".')
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise CatalogTrustError(f'File khoá tin cậy {pathlib.Path(path).name}: mục khoá không hợp lệ.')
        pub = _b64(row.get('publicKey'), 32, 'Khoá công khai')
        kid = key_id(pub)
        if str(row.get('keyId') or kid) != kid:
            raise CatalogTrustError(f'File khoá tin cậy {pathlib.Path(path).name}: keyId {row.get("keyId")} không khớp khoá ({kid}).')
        out.append(TrustedKey(kid, pub, str(row.get('comment') or '')[:200], source or str(path)))
    return out


def trusted_keys() -> list[TrustedKey]:
    """Keys shipped with Lynxx, plus the developer key file of LYNXX_CATALOG_KEYS_FILE"""
    keys = load_keys(KEYS_FILE, 'lynxx')
    if extra := os.environ.get(ENV_KEYS_FILE, '').strip():
        keys += load_keys(pathlib.Path(extra).expanduser(), ENV_KEYS_FILE)
    return keys


def allow_unsigned() -> bool:
    return os.environ.get(ENV_ALLOW_UNSIGNED, '').strip() == '1'


def sig_url(index_url: str) -> str:
    """'https://host/catalog/index.json?v=2' -> 'https://host/catalog/index.json.sig?v=2'"""
    p = url_parse.urlsplit(index_url)
    return url_parse.urlunsplit((p.scheme, p.netloc, p.path + SIG_SUFFIX, p.query, ''))


def signed_message(index: bytes) -> bytes:
    return SIGNING_CONTEXT + index


def make_signature(index: bytes, secret: bytes, *, signer=None) -> dict:
    """The .sig document for index bytes. signer(secret, message) -> 64 bytes (default: lynxx.ed25519.sign)"""
    pub = ed25519.public_key(secret)
    sig = (signer or ed25519.sign)(secret, signed_message(index))
    if not ed25519.verify(pub, signed_message(index), sig):
        raise ValueError('the new signature does not verify')
    return {'format': SIG_FORMAT, 'alg': SIG_ALG, 'keyId': key_id(pub), 'signature': base64.b64encode(sig).decode('ascii')}


def _expires(index: bytes, now: datetime.datetime) -> str:
    try:
        data = json.loads(index)
    except ValueError:
        return ''  # not json: the caller reports it
    value = data.get('expires') if isinstance(data, dict) else None
    if value is None:
        return ''
    try:
        when = datetime.datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        raise CatalogTrustError(f'Catalog có hạn dùng không hợp lệ: {str(value)[:40]}.')
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.UTC)
    if when < now:
        raise CatalogTrustError(f'Catalog đã hết hạn ({str(value)[:40]}): người bảo trì cần ký lại catalog.')
    return str(value)


def verify_catalog(index: bytes, signature: bytes | None, *, keys: list[TrustedKey] | None = None,
                   now: datetime.datetime | None = None) -> CatalogTrust:
    """signature: content of the .sig file, None if the catalog has none. Raises CatalogTrustError when it is not trusted"""
    now = now or datetime.datetime.now(datetime.UTC)
    if signature is None:
        if allow_unsigned():
            return CatalogTrust(False, '', _expires(index, now))
        raise CatalogTrustError('Catalog chưa được ký (không có file .sig): Lynxx chỉ dùng catalog có chữ ký hợp lệ của người bảo trì.')
    try:
        doc = json.loads(signature[:SIG_SIZE_MAX])
    except ValueError:
        doc = None
    if not isinstance(doc, dict) or doc.get('format') != SIG_FORMAT or doc.get('alg') != SIG_ALG:
        raise CatalogTrustError('File chữ ký của catalog không đúng định dạng (cần lynxx-catalog-sig/1, ed25519).')
    sig = _b64(doc.get('signature'), 64, 'Chữ ký')
    keys = trusted_keys() if keys is None else keys
    if not keys:
        raise CatalogTrustError('Bản Lynxx này chưa có khoá công khai nào để kiểm chữ ký catalog (khoá giữ chỗ): '
                                'chưa dùng được catalog từ xa.')
    kid = str(doc.get('keyId') or '')
    key = next((k for k in keys if k.key_id == kid), None)
    if key is None:
        raise CatalogTrustError(f'Catalog được ký bằng khoá {kid[:16] or "?"} mà Lynxx không tin cậy.')
    if not ed25519.verify(key.public_key, signed_message(index), sig):
        raise CatalogTrustError('Chữ ký của catalog không hợp lệ: catalog đã bị sửa sau khi ký, hoặc được ký bằng khoá khác.')
    return CatalogTrust(True, key.key_id, _expires(index, now))

#
#
#########################################
