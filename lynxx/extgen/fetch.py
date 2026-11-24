# coding=UTF-8
"""
Lynxx extension generator: the only way the generator (skill, AI worker, sandbox test runs) talks to a website.
Rules enforced here, not left to a model (plan Part C): https only (R10 / SSRF), public addresses only, allowed hosts only,
robots.txt (RFC 9309, R2), minimal interval per host incl. Crawl-delay (R4), request budget, response size cap, no cookies
or credentials (R1 / R12), stop on bot challenges (R5). Every response can be recorded as a test fixture.
"""
#########################################
#
#

from __future__ import annotations

import ipaddress
import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.cookiejar import DefaultCookiePolicy
from threading import Lock
from urllib import parse as url_parse

import requests

from lynxx.denylist import ascii_host, denied, denied_message
from lynxx.network import LYNXX_USER_AGENT, is_cloudflare_challenge, parse_retry_after
from lynxx.robots import ROBOTS_MAX_BYTES, RobotsRules

__all__ = ('FetchPolicyError', 'FetchResult', 'SafeFetcher', 'host_of', 'normalize_site')

ROBOTS_AGENT = 'Lynxx'
USER_AGENT = f'{LYNXX_USER_AGENT} (extension generator)'
MAX_REDIRECTS = 3
TIMEOUT = (10, 30)
re_unsafe_url_char = re.compile(r'[\\\x00-\x20\x7f]')
'''backslash, whitespace, control characters: parsers disagree on where the host ends (urllib vs urllib3), never sent'''
re_secret_param = re.compile(r'([?&](?:api_key|login|user_id|password|pass_hash|token|key|access_token)=)[^&#]*', re.IGNORECASE)


class FetchPolicyError(Exception):
    """A request refused by a hard rule: rule is the plan rule id (R2 robots, R4 rate/budget, R5 challenge, R10 address).
    redirect: the refused address is where the site redirected the request (never contacted), not the address asked for"""

    def __init__(self, rule: str, message: str, *, redirect: bool = False) -> None:
        super().__init__(f'[{rule}] {message}')
        self.rule = rule
        self.message = message
        self.redirect = redirect


@dataclass
class FetchResult:
    url: str
    status: int
    headers: dict[str, str]
    body: bytes
    truncated: bool = False
    elapsed: float = 0.0

    @property
    def content_type(self) -> str:
        return self.headers.get('content-type', '').split(';')[0].strip().lower()

    def text(self, limit: int = 0) -> str:
        t = self.body.decode('utf-8', errors='replace')
        return t[:limit] if limit else t


def redact_url(url: str) -> str:
    return re_secret_param.sub(r'\1<REDACTED>', url)


def host_of(url: str) -> str:
    return (url_parse.urlparse(url).hostname or '').lower().rstrip('.')


def strip_html(text: str) -> str:
    """page text for reading: scripts / styles / svg removed, blank lines collapsed"""
    text = re.sub(r'(?is)<(script|style|noscript|svg)\b.*?</\1>', '', text)
    return re.sub(r'\n\s*\n+', '\n', text)


def check_denied(host_or_url: str) -> None:
    """Raises FetchPolicyError (rule R6 / R7 / R8 of the entry) if the host is on Lynxx's domain denylist"""
    if d := denied(host_or_url):
        raise FetchPolicyError(d.rule, denied_message(d))


def normalize_site(url: str) -> str:
    """'example.com/posts?x' -> 'https://example.com/' (the site root the extension is made for). Internationalized names
    become their ascii (punycode) form; hosts on the domain denylist are refused"""
    u = url.strip()
    if '://' not in u:
        u = f'https://{u}'
    authority = re.split(r'[/?#]', u.split('://', 1)[1], maxsplit=1)[0]  # the path is dropped: only the host part matters
    if re_unsafe_url_char.search(authority):
        raise FetchPolicyError('R10', f'not a site address: {url!r}')
    p = url_parse.urlparse(u)
    if not p.hostname:
        raise FetchPolicyError('R10', f'not a site address: {url}')
    check_denied(p.hostname)  # first: the clearest reason for a denied site, whatever else is wrong with the address
    if p.scheme != 'https':
        raise FetchPolicyError('R10', f'only https sites are supported: {url}')
    if p.username or p.password:
        raise FetchPolicyError('R1', 'credentials in urls are not allowed')
    try:
        host = ascii_host(p.hostname)
        port = f':{p.port}' if p.port and p.port != 443 else ''
    except ValueError as err:
        raise FetchPolicyError('R10', f'not a site address: {url} ({err})')
    check_denied(host)
    return f'https://{f"[{host}]" if ":" in host else host}{port}/'


def _is_public(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    return ip.is_global and not ip.is_multicast


@dataclass
class _HostState:
    robots: RobotsRules | None = None
    robots_status: str = ''
    robots_text: str | None = None
    '''the robots.txt read (a comment for a 4xx answer: no restrictions), None when unreachable: saved into drafts by record'''
    crawl_delay: float = 0.0
    last: float = 0.0


@dataclass
class SafeFetcher:
    """allowed_hosts: hosts (and their subdomains) the fetcher may contact.
    allow_local: tests only (local http servers); never set from the app, the CLI or a model"""
    allowed_hosts: set[str]
    budget: int = 40
    min_interval: float = 1.0
    max_bytes: int = 2 * 1024 * 1024
    allow_local: bool = False
    recorder: Callable[[FetchResult], None] | None = None
    log: list[dict] = field(default_factory=list)
    _count: int = 0
    _hosts: dict[str, _HostState] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def __post_init__(self) -> None:
        self.allowed_hosts = {h.lower().rstrip('.') for h in self.allowed_hosts if h}
        self._session = requests.Session()
        self._session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))  # no cookies, ever
        self._session.headers.update({'User-Agent': USER_AGENT, 'Accept-Encoding': 'gzip, deflate'})
        self._session.trust_env = False  # no proxy / netrc credentials from the environment

    @property
    def used(self) -> int:
        return self._count

    def host_allowed(self, host: str) -> bool:
        host = host.lower().rstrip('.')
        return any(host == h or host.endswith(f'.{h}') for h in self.allowed_hosts)

    def _check_address(self, url: str) -> None:
        if re_unsafe_url_char.search(url):
            raise FetchPolicyError('R10', f'url with a backslash, a space or a control character: {redact_url(url)!r}')
        p = url_parse.urlparse(url)
        if p.scheme != 'https' and not (self.allow_local and p.scheme == 'http'):
            raise FetchPolicyError('R10', f'only https is allowed: {redact_url(url)}')
        host = (p.hostname or '').lower()
        check_denied(host)  # before anything else, also for redirects and robots.txt (never contacted)
        if not host or not self.host_allowed(host):
            raise FetchPolicyError('R10', f'host is not allowed for this site: {host or url}')
        if p.username or p.password:
            raise FetchPolicyError('R1', 'credentials in urls are not allowed')
        if self.allow_local:
            return
        try:
            infos = socket.getaddrinfo(host, p.port or 443, proto=socket.IPPROTO_TCP)
        except OSError as err:
            raise FetchPolicyError('R10', f'cannot resolve {host}: {err}')
        bad = [i[4][0] for i in infos if not _is_public(i[4][0])]
        if bad or not infos:
            raise FetchPolicyError('R10', f'{host} resolves to a non public address ({", ".join(bad) or "none"})')

    def _robots(self, url: str) -> _HostState:
        p = url_parse.urlparse(url)
        key = f'{p.scheme}://{p.netloc.lower()}'
        st = self._hosts.get(key)
        if st is not None:
            return st
        st = _HostState()
        self._hosts[key] = st
        robots_url = f'{key}/robots.txt'
        # RFC 9309 (lynxx/robots.py, as the tag list updater): groups of the same agent are merged and the longest rule wins.
        # urllib.robotparser kept only the first 'User-agent: *' group: with Cloudflare's managed 'Allow: /' block first, a later
        # 'Disallow: /api' of the site (manebooru.art, furbooru.org) was ignored
        try:
            # paced like any other request of the host: the first page after it waits min_interval (it went out at once before)
            self._pace(robots_url, st)
            r = self._raw_get(robots_url, count=True)
            if 200 <= r.status < 300:
                st.robots_text = r.text()[:ROBOTS_MAX_BYTES]
                rp = RobotsRules.parse(st.robots_text, ROBOTS_AGENT)
                st.robots_status = 'found'
            elif 400 <= r.status < 500:
                st.robots_status = f'none ({r.status})'
                st.robots_text = f'# {robots_url} answered HTTP {r.status}: no robots.txt, no restrictions (RFC 9309 2.3.1.3)\n'
                rp = RobotsRules.allow_all(st.robots_status)  # RFC 9309: unavailable (4xx) -> no restrictions
            else:
                st.robots_status = f'unreachable ({r.status})'
                rp = RobotsRules.disallow_all(st.robots_status)  # RFC 9309: unreachable (5xx) -> complete disallow
        except FetchPolicyError:
            raise
        except Exception as err:
            st.robots_status = f'unreachable ({type(err).__name__})'
            rp = RobotsRules.disallow_all(st.robots_status)
        st.robots = rp
        st.crawl_delay = min(float(rp.crawl_delay), 30.0) if rp.crawl_delay else 0.0
        return st

    def robots_txt(self, url: str) -> str | None:
        """The robots.txt this fetcher read for the host of url (None: not read yet, or unreachable). Never makes a request"""
        p = url_parse.urlparse(url)
        st = self._hosts.get(f'{p.scheme}://{p.netloc.lower()}')
        return st.robots_text if st is not None else None

    def robots_info(self, url: str) -> dict:
        st = self._robots(url)
        return {'status': st.robots_status, 'crawlDelay': st.crawl_delay,
                'allowed': bool(st.robots and st.robots.allows(url))}

    def _pace(self, url: str, st: _HostState) -> None:
        interval = max(self.min_interval, st.crawl_delay)
        wait = st.last + interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        st.last = time.monotonic()

    def _raw_get(self, url: str, *, count: bool, method: str = 'GET', headers: dict | None = None) -> FetchResult:
        with self._lock:
            if count:
                if self._count >= self.budget:
                    raise FetchPolicyError('R4', f'request budget of this session is used up ({self.budget})')
                self._count += 1
        current = url
        for hop in range(MAX_REDIRECTS + 1):
            try:
                self._check_address(current)
            except FetchPolicyError as err:
                if not hop:
                    raise
                raise FetchPolicyError(err.rule, f'{redact_url(url)} redirects to a refused address: {err.message}',
                                       redirect=True) from None
            t0 = time.monotonic()
            with self._session.request(method, current, headers=headers or {}, timeout=TIMEOUT, stream=True,
                                       allow_redirects=False) as r:
                if r.is_redirect or r.is_permanent_redirect:
                    nxt = url_parse.urljoin(current, r.headers.get('location', ''))
                    current = nxt
                    continue
                body = b''
                truncated = False
                if method != 'HEAD':
                    for chunk in r.iter_content(65536):
                        body += chunk
                        if len(body) > self.max_bytes:
                            body, truncated = body[:self.max_bytes], True
                            break
                res = FetchResult(current, r.status_code, {k.lower(): v for k, v in r.headers.items()}, body, truncated,
                                  time.monotonic() - t0)
                if is_cloudflare_challenge(r):
                    raise FetchPolicyError('R5', f'{host_of(current)} answered with a bot challenge: Lynxx does not bypass it')
                return res
        raise FetchPolicyError('R10', f'too many redirects: {redact_url(url)}')

    def fetch(self, url: str, *, method: str = 'GET', accept: str = '') -> FetchResult:
        """GET / HEAD a url of an allowed host after robots.txt and pacing. Raises FetchPolicyError on a rule"""
        if method not in ('GET', 'HEAD'):
            raise FetchPolicyError('R10', f'method {method} is not allowed')
        self._check_address(url)
        st = self._robots(url)
        if not (st.robots and st.robots.allows(url)):
            raise FetchPolicyError('R2', f'robots.txt of {host_of(url)} does not allow {redact_url(url)}')
        self._pace(url, st)
        headers = {'Accept': accept} if accept else {}
        res = self._raw_get(url, count=True, method=method, headers=headers)
        if res.status == 429:
            wait = parse_retry_after(res.headers.get('retry-after')) or 5.0
            if wait > 30:
                raise FetchPolicyError('R4', f'{host_of(url)} asks to wait {wait:.0f} s (429): try again later')
            time.sleep(wait)
            self._pace(url, st)
            res = self._raw_get(url, count=True, method=method, headers=headers)
        self.log.append({'url': redact_url(res.url), 'status': res.status, 'bytes': len(res.body), 'type': res.content_type})
        if self.recorder:
            self.recorder(res)
        return res

#
#
#########################################
