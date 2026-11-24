# coding=UTF-8
"""
Lynxx: domains no extension may ever be generated for or installed for (plan site-extension-skill-and-ai-generator, Part C:
R6 paywall bypass, R7 DRM / commercial streaming, R8 illegal content). The list is lynxx/safety_data/denied_domains.txt.
An entry matches the domain itself and every subdomain ('youtube.com' matches 'www.youtube.com', 'm.youtube.com').
Hosts are compared in every form a network library could reach: lowercase, without trailing dots or percent escapes, and the
ASCII (punycode) form of internationalized names (IDNA 2008 / UTS 46 as requests uses, and IDNA 2003 nameprep, which also
maps fullwidth letters, ideographic dots and zero width characters). Entries may be stored hashed ('sha256:<hex of the
ascii domain>') so a list of illegal sites can be shipped without printing the names.
Enforced in the generator (lynxx.extgen.fetch: site normalization and every request, redirects included), the offline draft
check, every install (lynxx.extensions) and the remote catalog. IP literals are not mapped back to names.
"""
#########################################
#
#

from __future__ import annotations

import functools
import hashlib
import ipaddress
import json
import pathlib
import re
from dataclasses import dataclass
from urllib import parse as url_parse

__all__ = ('DENIED_DOMAINS_FILE', 'DeniedDomain', 'DenyList', 'ascii_host', 'denied', 'denied_in_extension', 'denied_message', 'host_variants')

DENIED_DOMAINS_FILE = pathlib.Path(__file__).resolve().parent / 'safety_data' / 'denied_domains.txt'
RULES = ('R6', 'R7', 'R8')
RULE_TEXT_VI = {
    'R6': 'kho đăng lại nội dung trả phí / riêng tư của người khác',
    'R7': 'dịch vụ streaming thương mại / nội dung có DRM',
    'R8': 'site có nội dung bất hợp pháp',
}
re_ascii_host = re.compile(r'^[a-z\d_](?:[a-z\d_-]*[a-z\d_])?(?:\.[a-z\d_](?:[a-z\d_-]*[a-z\d_])?)*$')
re_sha_entry = re.compile(r'^sha256:([0-9a-f]{64})$')
DOTS = ('。', '．', '｡')  # ideographic / fullwidth / halfwidth ideographic full stops: IDNA label separators


@dataclass(frozen=True)
class DeniedDomain:
    entry: str
    '''the matching entry of the list: an ascii domain or 'sha256:<hex>' '''
    rule: str
    reason: str
    host: str
    '''the host that matched (ascii form)'''


def _strip_dots(h: str) -> str:
    for d in DOTS:
        h = h.replace(d, '.')
    return h.strip().strip('.')


def ascii_host(host: str) -> str:
    """The host as a network library sends it: lowercase ascii (punycode for internationalized names), no trailing dot.
    IP literals are returned as they are. Raises ValueError for a host that is not a valid name"""
    h = _strip_dots(str(host or '').strip('[]'))
    if not h:
        raise ValueError('empty host')
    try:
        return str(ipaddress.ip_address(h))
    except ValueError:
        pass
    if h.isascii():
        h = h.lower()
    else:
        import idna  # dependency of requests: same mapping as requests uses for non ascii hosts
        try:
            h = idna.encode(h, uts46=True).decode('ascii').lower().strip('.')
        except (idna.IDNAError, UnicodeError) as err:
            raise ValueError(f'invalid host name: {err}')
    if len(h) > 253 or not re_ascii_host.fullmatch(h):
        raise ValueError(f'invalid host name \'{host}\'')
    return h


def host_variants(host: str) -> set[str]:
    """Every ascii form the host could take (see the module docstring). Empty for an unusable host"""
    raw = str(host or '').strip().strip('[]')
    out: set[str] = set()
    for h in {raw, url_parse.unquote(raw)}:
        h = _strip_dots(h)
        if not h:
            continue
        if h.isascii():
            out.add(h.lower().strip('.'))
            continue
        try:
            out.add(ascii_host(h))
        except ValueError:
            pass
        try:  # IDNA 2003 (nameprep): also maps characters UTS 46 refuses
            out.add(h.encode('idna').decode('ascii').lower().strip('.'))
        except UnicodeError:
            pass
    return {v for v in out if v}


def _suffixes(host: str) -> list[str]:
    labels = host.split('.')
    return ['.'.join(labels[i:]) for i in range(len(labels))]


def _sha(domain: str) -> str:
    return hashlib.sha256(domain.encode('ascii', 'replace')).hexdigest()


class DenyList:
    """Parsed denied_domains.txt: '<domain or sha256:hex> <rule> [reason...]' per line, '#' starts a comment"""

    def __init__(self, entries: dict[str, tuple[str, str]], hashed: dict[str, tuple[str, str]]) -> None:
        self.entries = entries
        self.hashed = hashed

    def __len__(self) -> int:
        return len(self.entries) + len(self.hashed)

    @staticmethod
    def from_text(text: str) -> DenyList:
        entries: dict[str, tuple[str, str]] = {}
        hashed: dict[str, tuple[str, str]] = {}
        for n, line in enumerate(text.splitlines(), 1):
            line = line.split('#', 1)[0].strip()
            if not line:
                continue
            parts = line.split(None, 2)
            if len(parts) < 2 or parts[1] not in RULES:
                raise ValueError(f'denied_domains.txt line {n}: expected \'<domain> <{"|".join(RULES)}> [reason]\'')
            name, rule = parts[0], parts[1]
            reason = parts[2].strip() if len(parts) > 2 else ''
            if m := re_sha_entry.fullmatch(name.lower()):
                hashed[m.group(1)] = (rule, reason)
                continue
            try:
                entries[ascii_host(name)] = (rule, reason)
            except ValueError as err:
                raise ValueError(f'denied_domains.txt line {n}: {err}')
        return DenyList(entries, hashed)

    @staticmethod
    def load(path: pathlib.Path) -> DenyList:
        return DenyList.from_text(pathlib.Path(path).read_text(encoding='utf-8'))

    def match(self, host_or_url: str) -> DeniedDomain | None:
        """The entry denying this host (or the host of this url), None if it is allowed"""
        value = str(host_or_url or '').strip()
        if '://' in value:
            value = url_parse.urlsplit(value).hostname or ''
        for variant in sorted(host_variants(value)):
            for suffix in _suffixes(variant):
                if hit := self.entries.get(suffix):
                    return DeniedDomain(suffix, hit[0], hit[1], variant)
                if self.hashed and (hit := self.hashed.get(_sha(suffix))):
                    return DeniedDomain(f'sha256:{_sha(suffix)}', hit[0], hit[1], variant)
        return None


@functools.lru_cache(maxsize=1)
def default_list() -> DenyList:
    """The list shipped with Lynxx. A missing or broken file is an error, never an empty list (fail closed)"""
    return DenyList.load(DENIED_DOMAINS_FILE)


def denied(host_or_url: str) -> DeniedDomain | None:
    return default_list().match(host_or_url)


re_url_host = re.compile(r'(?i)\b(?:https?|wss?)://(?:[^/\s"\'<>@]*@)?(\[[^\]]*\]|[^/\s"\'<>?#:\\]+)')
SCAN_SUFFIXES = ('.py', '.json')
SCAN_MAX_BYTES = 2 * 1024 * 1024


def denied_in_extension(folder: pathlib.Path, *, deny: DenyList | None = None) -> DeniedDomain | None:
    """An extension folder made for a denied site: its manifest site / homepage, or any url in its code or json files"""
    deny = deny or default_list()
    folder = pathlib.Path(folder)
    try:
        raw = json.loads((folder / 'manifest.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raw = {}
    if isinstance(raw, dict):
        gen = raw.get('$generator') if isinstance(raw.get('$generator'), dict) else {}
        for value in (raw.get('site'), raw.get('homepage'), gen.get('sourceUrl')):
            if isinstance(value, str) and value.strip() and (d := deny.match(value if '://' in value else f'https://{value}')):
                return d
    for p in sorted(folder.rglob('*')):
        if p.suffix.lower() not in SCAN_SUFFIXES or not p.is_file() or p.is_symlink() or p.stat().st_size > SCAN_MAX_BYTES:
            continue
        text = p.read_text(encoding='utf-8', errors='replace')
        for host in re_url_host.findall(text):
            if d := deny.match(host):
                return d
    return None


def denied_message(d: DeniedDomain) -> str:
    """User facing (Vietnamese) reason, shown by the app"""
    why = RULE_TEXT_VI.get(d.rule, 'site bị chặn')
    detail = f', {d.reason}' if d.reason and not d.entry.startswith('sha256:') else ''
    return (f'Tên miền {d.host} nằm trong danh sách chặn của Lynxx ({d.rule}: {why}{detail}). '
            f'Lynxx không tạo và không cài extension cho site này.')

#
#
#########################################
