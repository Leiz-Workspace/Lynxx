# coding=UTF-8
"""
Lynxx: Ed25519 signatures (RFC 8032, section 5.1), pure Python, after the reference implementation of RFC 8032 section 6.
Used to verify the signed remote extension catalog (lynxx/trust.py): verification needs only public data, so the lack of
constant time arithmetic does not matter there. sign() exists for tools/sign_catalog.py and tests; the tool prefers the
'cryptography' package when it is installed (same deterministic signatures). Tests: RFC 8032 section 7.1 vectors
(lynxx/tests/test_catalog_trust.py).
"""
#########################################
#
#

from __future__ import annotations

import hashlib

__all__ = ('public_key', 'sign', 'verify')

_P = 2 ** 255 - 19
_Q = 2 ** 252 + 27742317777372353535851937790883648493
'''order of the base point (L in RFC 8032)'''
_D = -121665 * pow(121666, _P - 2, _P) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)

Point = tuple[int, int, int, int]
'''extended homogeneous coordinates (X, Y, Z, T), x = X/Z, y = Y/Z, x * y = T/Z'''


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


def _add(a: Point, b: Point) -> Point:
    ra = (a[1] - a[0]) * (b[1] - b[0]) % _P
    rb = (a[1] + a[0]) * (b[1] + b[0]) % _P
    rc = 2 * a[3] * b[3] * _D % _P
    rd = 2 * a[2] * b[2] % _P
    e, f, g, h = rb - ra, rd - rc, rd + rc, rb + ra
    return e * f % _P, g * h % _P, f * g % _P, e * h % _P


def _mul(s: int, pt: Point) -> Point:
    acc: Point = (0, 1, 1, 0)  # neutral element
    while s > 0:
        if s & 1:
            acc = _add(acc, pt)
        pt = _add(pt, pt)
        s >>= 1
    return acc


def _equal(a: Point, b: Point) -> bool:
    return (a[0] * b[2] - b[0] * a[2]) % _P == 0 and (a[1] * b[2] - b[1] * a[2]) % _P == 0


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P != 0:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


_GY = 4 * _inv(5) % _P
_GX = _recover_x(_GY, 0) or 0
_G: Point = (_GX, _GY, 1, _GX * _GY % _P)


def _compress(pt: Point) -> bytes:
    zinv = _inv(pt[2])
    x, y = pt[0] * zinv % _P, pt[1] * zinv % _P
    return int.to_bytes(y | ((x & 1) << 255), 32, 'little')


def _decompress(data: bytes) -> Point | None:
    if len(data) != 32:
        return None
    y = int.from_bytes(data, 'little')
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    if x is None:
        return None
    return x, y, 1, x * y % _P


def _hash_int(data: bytes) -> int:
    return int.from_bytes(hashlib.sha512(data).digest(), 'little') % _Q


def _expand(secret: bytes) -> tuple[int, bytes]:
    if len(secret) != 32:
        raise ValueError('an Ed25519 secret key is 32 bytes')
    h = hashlib.sha512(secret).digest()
    a = int.from_bytes(h[:32], 'little')
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def public_key(secret: bytes) -> bytes:
    """32 byte public key of a 32 byte secret key (seed)"""
    a, _ = _expand(secret)
    return _compress(_mul(a, _G))


def sign(secret: bytes, message: bytes) -> bytes:
    """64 byte signature (deterministic). Not constant time: for offline signing tools and tests"""
    a, prefix = _expand(secret)
    pub = _compress(_mul(a, _G))
    r = _hash_int(prefix + message)
    rs = _compress(_mul(r, _G))
    h = _hash_int(rs + pub + message)
    s = (r + h * a) % _Q
    return rs + int.to_bytes(s, 32, 'little')


def verify(public: bytes, message: bytes, signature: bytes) -> bool:
    """True only for a valid signature of message by public (RFC 8032 5.1.7, cofactorless check, S < L enforced)"""
    if not isinstance(public, (bytes, bytearray)) or not isinstance(signature, (bytes, bytearray)):
        return False
    if len(public) != 32 or len(signature) != 64:
        return False
    a = _decompress(bytes(public))
    if a is None:
        return False
    rs = bytes(signature[:32])
    r = _decompress(rs)
    if r is None:
        return False
    s = int.from_bytes(signature[32:], 'little')
    if s >= _Q:
        return False
    h = _hash_int(rs + bytes(public) + message)
    return _equal(_mul(s, _G), _add(r, _mul(h, a)))

#
#
#########################################
