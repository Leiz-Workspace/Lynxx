# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

from __future__ import annotations

import contextlib
import datetime
import email.utils
import hashlib
import json
import os
import pathlib
import re
import sys
import time
from abc import ABC, abstractmethod
from argparse import Namespace
from http.cookiejar import CookieJar
from multiprocessing.dummy import current_process
from threading import Lock, Thread, local
from urllib import parse as url_parse
from warnings import filterwarnings

from bs4 import BeautifulSoup
from requests import Response, Session, adapters, exceptions, structures

from .defines import (
    CONNECT_RETRIES_BASE,
    CONNECT_RETRIES_PROXY_MULT,
    CONNECT_TIMEOUT_BASE,
    DOWNLOAD_CHUNK_SIZE,
    KNOWN_EXTENSIONS_AUD,
    KNOWN_EXTENSIONS_VID,
    RETRY_AFTER_MAX_SECONDS,
    UTF8,
    DownloadModes,
    ThreadInterruptException,
)
from .logger import trace
from .useragent import UAManager
from .utils import format_exception
from .vcs.version import APP_VERSION

__all__ = ('DownloadInterruptException', 'HTMLContent', 'ThreadedHtmlWorker', 'thread_exit')

filterwarnings('ignore', category=UserWarning)

LYNXX_USER_AGENT = f'Lynxx/{APP_VERSION.replace(" ", "-")}'
'''client name for apis asking for one (danbooru, e621); no spaces in the version (HTTP product token)'''
CLIENT_CONNECTOR_ERRORS = (exceptions.ProxyError, exceptions.SSLError)
CLIENT_ERRORS_NO_RETRY = (400, 401, 403, 405, 409, 410, 422)
DOWNLOAD_PERMANENT_ERRORS = (400, 401, 403, 404, 405, 410, 451)
STREAM_READ_SIZE = 65536
CF_STREAK_STOP = 3
'''after this many bot challenges in a row the remaining files of the run are not requested'''
'''file download: these statuses are not retried (the original code retried a 404 ten times, > 20 s per dead file)'''
re_content_range_total = re.compile(r'bytes \*/(\d+)')
PART_SUFFIX = '.part'
'''incomplete downloads'''
'''errors which do not consume normal retries (proxy hiccups), see CONNECT_RETRIES_PROXY_MULT for their cap'''


def shared_rate_wait(site: str, interval: float) -> bool:
    """Site api rate limits are per client address: the desktop app runs the sidecar and several download jobs as separate processes,
    so the minimal interval is kept in a lock file shared by all of them (the original code limited each process separately: 3 e621 jobs = 6 req/s).
    Returns False if the lock file cannot be used (the caller falls back to its in-process limit)"""
    try:
        from .extensions import data_dir  # local import: extensions imports this module indirectly
        folder = data_dir() / 'ratelimit'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'{hashlib.sha1(site.encode()).hexdigest()[:16]}.ts'
        with open(path, 'a+b') as f:
            _lock_file(f, True)
            try:
                f.seek(0)
                raw = f.read(32).strip()
                last = float(raw) if raw else 0.0
                now = time.time()
                if 0 < (wait := last + interval - now) <= interval:  # ignore clock jumps
                    time.sleep(wait)
                f.seek(0)
                f.truncate()
                f.write(f'{time.time():.6f}'.encode())
                f.flush()
            finally:
                _lock_file(f, False)
        return True
    except Exception:
        return False


RATE_LIMIT_MARGIN = 1.05
'''pace a rate limit with this margin: 60 requests / 60 s -> one request every 1.05 s'''
RATE_LIMITS_FILE = 'limits.json'
'''<data dir>/ratelimit/limits.json: {"rx": 120}, requests a minute the site gave the user's API key (the app writes it)'''
ENV_RATE_LIMIT = 'LYNXX_RATE_LIMIT_'
_limits_cache: list = [None, {}]
'''[(path, mtime_ns, size) of the file read, its limits]'''


def _rate_limits_file() -> dict[str, int]:
    try:
        from .extensions import data_dir  # local import: extensions imports this module indirectly
        path = data_dir() / 'ratelimit' / RATE_LIMITS_FILE
        st = path.stat()
    except Exception:
        return {}
    stamp = (path, st.st_mtime_ns, st.st_size)
    if _limits_cache[0] != stamp:
        try:
            raw = json.loads(path.read_text(encoding=UTF8))
        except (OSError, ValueError):
            raw = {}
        _limits_cache[:] = [stamp, {str(k): v for k, v in (raw.items() if isinstance(raw, dict) else ())
                                    if isinstance(v, int) and not isinstance(v, bool) and 0 < v <= 100000}]
    return _limits_cache[1]


def key_rate_limit(module: str, published: tuple[int, int] | None) -> tuple[int, int] | None:
    """(requests, seconds) the site allows the user's API key of 'module', None when no limit is known.
    Asking the site's admins may get a key more than the published limit: LYNXX_RATE_LIMIT_<MODULE> (requests a minute, CLI / cron)
    wins, then <data dir>/ratelimit/limits.json (the app: Settings > API keys), then the limit the site publishes"""
    raw = os.environ.get(f'{ENV_RATE_LIMIT}{module.upper()}', '').strip()
    n = int(raw) if raw.isdecimal() and 0 < int(raw) <= 100000 else _rate_limits_file().get(module, 0)
    return (n, 60) if n else published


def key_rate_interval(module: str, published: tuple[int, int] | None) -> float:
    """Seconds between two requests that keep the key under its limit (0: no known limit). Every process using the key keeps
    this pace together through shared_rate_wait (download jobs, Preview, the tag list updater)"""
    limit = key_rate_limit(module, published)
    return limit[1] / limit[0] * RATE_LIMIT_MARGIN if limit else 0.0


def _lock_file(f, lock: bool) -> None:
    if sys.platform == 'win32':
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_LOCK if lock else msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX if lock else fcntl.LOCK_UN)


def parse_retry_after(value: str | None) -> float:
    """Parses 'Retry-After' header (delta-seconds or HTTP-date), returns 0 if absent or invalid"""
    if not value:
        return 0.0
    value = value.strip()
    try:
        return min(float(max(0, int(value))), RETRY_AFTER_MAX_SECONDS)
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
        delta = (when - datetime.datetime.now(when.tzinfo)).total_seconds()
        return min(max(0.0, delta), RETRY_AFTER_MAX_SECONDS)
    except Exception:
        return 0.0


def is_cloudflare_challenge(response: Response | None) -> bool:
    """True if the response is a Cloudflare bot challenge page (cannot be passed by a plain HTTP client)"""
    if response is None or response.status_code not in (403, 429, 503):
        return False
    return response.headers.get('cf-mitigated', '').lower() == 'challenge'


CF_CHALLENGE_HINT = (
    'Website responded with a bot challenge page (cf-mitigated: challenge). Lynxx does not bypass bot protection: you pass the '
    'check yourself. In the desktop app, use the \'Xác minh trong Lynxx\' button (Cài đặt → Kết nối, or the one shown with the '
    'error): pass the check in that window and the app keeps the website\'s \'cf_clearance\' cookie and that window\'s User-Agent, '
    'for that website only. Or pass it in your browser and copy its \'cf_clearance\' cookie and the browser\'s \'User-Agent\' by '
    'hand into Cài đặt → Kết nối, per-site Headers / Cookies. The cookie expires: verify again when the challenge comes back.'
)

re_content_range_str = re.compile(r'bytes (\d+)-(\d+)?(?:/(\d+))?')


class DownloadInterruptException(BaseException):
    pass


class HTMLContent:
    """Response body. The BeautifulSoup tree is built on first access: json pages (danbooru, e621) never need it
    and parsing a 200 posts json page as html cost tens of ms per page (the original code parsed every response)"""
    __slots__ = ('_html', 'raw')

    def __init__(self, html: BeautifulSoup | None, raw: bytes) -> None:
        self._html = html
        self.raw = raw

    @property
    def html(self) -> BeautifulSoup:
        if self._html is None:
            self._html = BeautifulSoup(self.raw, 'html.parser')
        return self._html

    @staticmethod
    def init_empty() -> HTMLContent:
        return HTMLContent(BeautifulSoup(), b'')

    def __bool__(self) -> bool:
        return bool(len(self.raw))

    def __str__(self) -> str:
        return self.raw.decode(errors='replace')


class FileDownloadResult:
    def __init__(self) -> None:
        self.file_size: int = 0
        self.expected_size: int = 0
        self.retries: int = 0
        self.result_str: str = ''
        self.reason: str = ''
        '''why the file was not downloaded when no retry would help (HTTP 404, bot challenge...)'''


def thread_exit(err_str='', code=-1) -> None:
    """The message travels with the exception: it becomes the error reported to the app when it stops the run"""
    trace(f'Exiting with code {code:d}, message:\n{err_str}', True)
    time.sleep(0.15)
    raise ThreadInterruptException(err_str.strip())


class ThreadedWorker(ABC):
    """
    ThreadedWorker !Abstract!
    """
    @abstractmethod
    def __init__(self) -> None:
        self.my_root_thread: Thread | None = None
        self.item_lock: Lock = Lock()
        self.items_all_lock: Lock = Lock()

    def reset_root_thread(self, thread: Thread) -> None:
        self.my_root_thread = thread

    def sleep_interruptible(self, seconds: float) -> None:
        """Sleeps in small steps so that cancel is not delayed by long waits (Retry-After, retries)"""
        deadline = time.monotonic() + max(0.0, seconds)
        while not self.is_killed() and (left := deadline - time.monotonic()) > 0:
            time.sleep(min(0.25, left))

    def catch_cancel_or_ctrl_c(self, message: str = '') -> None:
        if self.is_killed():
            thread_exit(message, code=1)

    def is_killed(self) -> bool:
        return self.my_root_thread and getattr(self.my_root_thread, 'killed', False) is True


class ThreadedHtmlWorker(ThreadedWorker):
    """
    ThreadedHtmlWorker !Abstract!
    """
    @abstractmethod
    def __init__(self) -> None:
        super().__init__()
        self.verbose: bool = False
        self.raw_html_cache: dict[str, HTMLContent] = {}
        self.add_headers: structures.CaseInsensitiveDict[str, str] = structures.CaseInsensitiveDict()
        self.add_cookies: structures.CaseInsensitiveDict[str, str] = structures.CaseInsensitiveDict()
        self.ignore_proxy: bool = False
        self.ignore_proxy_dwn: bool = False
        self.proxies: dict[str, str] | None = None
        self.timeout: int = CONNECT_TIMEOUT_BASE
        self.retries: int = CONNECT_RETRIES_BASE
        self.etags: dict[str, str] = {}
        self.session: Session | None = None
        self._tls = local()
        self._sessions_lock: Lock = Lock()
        self._thread_sessions: list[Session] = []
        self._cookies_lock: Lock = Lock()
        self._rate_lock: Lock = Lock()
        self._last_request_ts: float = 0.0
        self._backoff_until: dict[str, float] = {'api': 0.0, 'dl': 0.0}
        self._dl_cf_streak: int = 0
        self._next_download_ts: float = 0.0
        self.bytes_received: int = 0
        '''file bytes written by this run (all threads, resumed parts excluded): download speed in the app'''
        '''consecutive file downloads answered with a bot challenge'''
        '''429 / 503: every thread waits (the original code backed off only the thread that got the response)'''
        self.cf_challenge_hit: bool = False
        '''set if the website responded with a bot challenge, UI may show a hint'''

    @abstractmethod
    def _get_sitename(self) -> str:
        ...

    @abstractmethod
    def _get_module_specific_default_headers(self) -> dict[str, str]:
        ...

    @abstractmethod
    def _get_module_specific_default_cookies(self) -> dict[str, str]:
        ...

    def _get_min_request_interval(self) -> float:
        """Minimal interval between API/html requests across all threads (seconds), modules override to respect site rate limits"""
        return 0.0

    def _get_min_download_interval(self) -> float:
        """Minimal interval between file requests across all threads (seconds), for CDNs with a known rate limit.
        Not set for danbooru: its CDN answered with a bot challenge after ~180-190 files both at ~10 and at 5 files/s (2026-11-17)"""
        return 0.0

    def _respect_download_rate(self) -> None:
        interval = self._get_min_download_interval()
        if interval <= 0:
            return
        with self._rate_lock:
            start = max(time.monotonic(), self._next_download_ts)
            self._next_download_ts = start + interval
        self.sleep_interruptible(start - time.monotonic())

    def _use_chunked_download(self, is_video_ext: bool) -> bool:
        """Whether file should be downloaded using multiple ranged requests"""
        return True

    def _fix_link_after_download_error(self, link: str, original_link: str, err: Exception) -> str:
        """Module hook: returns a new link to retry with (ex. alternative cdn host) or the same link"""
        return link

    def make_session(self, *, for_download=False) -> Session:
        s = Session()
        s.adapters.clear()
        s.mount('http://', adapters.HTTPAdapter(pool_maxsize=1, max_retries=0))
        s.mount('https://', adapters.HTTPAdapter(pool_maxsize=1, max_retries=0))
        s.keep_alive = True
        s.headers['Referer'] = self._get_sitename()
        s.headers['User-Agent'] = UAManager.orig_user_agent()
        s.headers.update(self.add_headers.copy())
        self._add_session_cookies(s)
        if self.proxies and not self.ignore_proxy and not (for_download and self.ignore_proxy_dwn):
            s.proxies.update(self.proxies.copy())
        return s

    def _cookie_domain(self) -> str:
        """'.<site host>': the domain the worker's cookies are set for (-cookie / -cookies: the per-site cookies of Settings → Kết nối,
        a cf_clearance of the app's verify window; module defaults), so that requests sends them to the site and its subdomains only,
        never to a CDN or a redirect target on another domain. The site's host without a leading 'www.' or 'api.' (rule34.xxx answers
        its api on api.rule34.xxx, its pages on rule34.xxx). '' when there is no site host to bind them to (no site, an IP address, a
        name without a dot): the cookies then go to any host, as before"""
        try:
            host = (url_parse.urlsplit(str(self._get_sitename() or '')).hostname or '').lower().rstrip('.')
        except Exception:
            return ''
        for prefix in ('www.', 'api.'):
            if host.startswith(prefix) and host.count('.') >= 2:
                host = host[len(prefix):]
                break
        if '.' not in host or ':' in host or host.replace('.', '').isdigit():
            return ''
        return f'.{host}'

    def _add_session_cookies(self, s: Session) -> None:
        """The worker's cookies into a new session, bound to the site's domain (_cookie_domain). Cookies the site set meanwhile
        (wrap_request keeps them in add_cookies) keep the domain the site gave them"""
        with self._cookies_lock:
            cookies = self.add_cookies.copy()
        domain = self._cookie_domain()
        if isinstance(cookies, CookieJar):
            for c in cookies:
                if not c.domain and domain:
                    c.domain, c.domain_specified, c.domain_initial_dot = domain, True, True
                s.cookies.set_cookie(c)
            return
        for name, value in cookies.items():
            if domain:
                s.cookies.set(name, value, domain=domain)
            else:
                s.cookies.set(name, value)

    def _thread_session(self) -> Session:
        """requests.Session is not guaranteed to be thread-safe: every worker thread gets its own session"""
        if current_process() == self.my_root_thread or self.my_root_thread is None:
            if self.session is None:
                self.session = self.make_session()
            return self.session
        s: Session | None = getattr(self._tls, 'session', None)
        if s is None:
            s = self.make_session()
            self._tls.session = s
            with self._sessions_lock:
                self._thread_sessions.append(s)
        return s

    def close_sessions(self) -> None:
        with self._sessions_lock:
            for s in self._thread_sessions:
                s.close()
            self._thread_sessions.clear()
        self._tls = local()

    def _note_backoff(self, seconds: float, kind: str) -> None:
        with self._rate_lock:
            self._backoff_until[kind] = max(self._backoff_until[kind], time.monotonic() + max(0.0, seconds))

    def _wait_backoff(self, kind: str) -> None:
        if (left := self._backoff_until[kind] - time.monotonic()) > 0:
            self.sleep_interruptible(left)

    def _download_session(self) -> Session:
        """Per-thread keep-alive session for file downloads. The original code opened a new session per file and sent HEAD + GET:
        2 TCP+TLS handshakes per file. Workers live for the whole run (persistent executors), so connections are reused"""
        s: Session | None = getattr(self._tls, 'dl_session', None)
        if s is None:
            s = self.make_session(for_download=True)
            self._tls.dl_session = s
            with self._sessions_lock:
                self._thread_sessions.append(s)
        return s

    def _respect_rate_limit(self) -> None:
        self._wait_backoff('api')
        interval = self._get_min_request_interval()
        if interval <= 0:
            return
        with self._rate_lock:
            if shared_rate_wait(self._get_sitename(), interval):
                return
            wait = self._last_request_ts + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_request_ts = time.monotonic()

    def _parse_args(self, args: Namespace) -> None:
        self.verbose = args.verbose or self.verbose
        self.ignore_proxy = args.noproxy or self.ignore_proxy
        self.ignore_proxy_dwn = args.proxynodown or self.ignore_proxy_dwn
        self.proxies = {'http': str(args.proxy), 'https': str(args.proxy)} if args.proxy else None
        self.timeout = args.timeout or self.timeout
        self.retries = args.retries or self.retries
        if args.headers:
            self.add_headers.update(args.headers)
        if args.cookies:
            self.add_cookies.update(args.cookies)
        for container_base, container_ext in zip((self.add_headers, self.add_cookies), (args.header, args.cookie), strict=True):
            pair: tuple[str, str]
            for pair in container_ext or []:
                if pair[0] in container_base:  # names only: values are cookies / credentials (cf_clearance, Authorization...)
                    trace(f'Warning (W1): \'{pair[0]}\' is given more than once, the last value is used')
                container_base[pair[0]] = pair[1]
        # module defaults never override what the user set (ex. browser User-Agent + cf_clearance for a bot challenge)
        for container, defaults in ((self.add_headers, self._get_module_specific_default_headers()),
                                    (self.add_cookies, self._get_module_specific_default_cookies())):
            for k, v in defaults.items():
                if k not in container:
                    container[k] = v
        self.close_sessions()
        self.session = self.make_session()

    # threaded
    def download_file(self, link: str, item_id: str, dest: pathlib.Path, mode: DownloadModes, orig_date: bool) -> FileDownloadResult:
        fullname = dest.name
        ext_full = fullname[fullname.rfind('.') + 1:]
        ext_char = ext_full[0]
        is_video_ext = ext_full in KNOWN_EXTENSIONS_VID or ext_full in KNOWN_EXTENSIONS_AUD  # large streamed media (video or audio)

        result = FileDownloadResult()
        result.result_str = f'[{current_process().name}]{" <touch>" if mode == DownloadModes.TOUCH else ""} {item_id}({ext_char})... '

        # data is written to '<name>.part' and renamed only when complete: an interrupted run never leaves
        # a truncated file under the final name (which later runs would treat as already downloaded)
        part = dest.with_name(f'{dest.name}{PART_SUFFIX}')
        if mode == DownloadModes.FULL and dest.is_file() and dest.stat().st_size > 0:
            result.file_size = result.expected_size = dest.stat().st_size
            result.result_str = f'{result.result_str}already exists, '
            return result

        if mode == DownloadModes.TOUCH:
            dest.touch(exist_ok=True)  # never truncates an existing file
        elif mode == DownloadModes.FULL and self._dl_cf_streak >= CF_STREAK_STOP:
            # the CDN answers every file with a bot challenge: further requests only make the block last longer
            self.cf_challenge_hit = True
            result.reason = 'not requested, the website keeps answering with a bot challenge'
        elif mode == DownloadModes.FULL:
            try:
                self._download_to_part(link, item_id, part, dest, is_video_ext, orig_date, result)
            except (KeyboardInterrupt, ThreadInterruptException, DownloadInterruptException):
                part.unlink(missing_ok=True)
                result.file_size = 0
                trace(f'{result.result_str}{("interrupted by user." if current_process() == self.my_root_thread else "interrupted")}', True)
                raise DownloadInterruptException
            if not (0 < result.file_size == result.expected_size):
                part.unlink(missing_ok=True)
        return result

    # threaded
    def _download_to_part(self, link: str, item_id: str, part: pathlib.Path, dest: pathlib.Path, is_video_ext: bool, orig_date: bool,
                          result: FileDownloadResult) -> None:
        """One streaming GET per attempt written straight to the .part file (the original code accumulated every chunk in memory with
        quadratic 'bytes +=' and sent a HEAD first). A failed attempt is resumed with Range + If-Range when the server supports it,
        a server ignoring Range (200) restarts the file, permanent errors (404, 410, 403...) are not retried.
        Modules asking for chunked downloads (_use_chunked_download) get ranged requests of DOWNLOAD_CHUNK_SIZE on one connection"""
        original_link = link
        s = self._download_session()
        chunked = self._use_chunked_download(is_video_ext)
        accept_ranges = chunked
        expected = -1  # total file size, -1: not known yet
        validator = ''  # strong ETag or Last-Modified of the first response, sent as If-Range when resuming
        modification_time_ns = 0
        have = 0
        conn_errors = 0
        part.unlink(missing_ok=True)
        while True:
            if self.is_killed():
                raise ThreadInterruptException
            if result.retries >= self.retries:
                trace(f'{result.result_str}giving up after {result.retries:d} tries', True)
                break
            self._wait_backoff('dl')
            self._respect_download_rate()
            headers = {'Accept': '*/*', 'Accept-Encoding': 'identity', 'Connection': 'keep-alive'}
            headers.update(self.add_headers.copy())
            window_end = -1
            if chunked:
                window_end = have + DOWNLOAD_CHUNK_SIZE - 1
                if expected > 0:
                    window_end = min(window_end, expected - 1)
                headers['Range'] = f'bytes={have:d}-{window_end:d}'
            elif have > 0 and accept_ranges:
                headers['Range'] = f'bytes={have:d}-'
            if have > 0 and validator:
                headers['If-Range'] = validator
            try:
                with s.get(link, headers=headers, timeout=self.timeout, stream=True, allow_redirects=True) as r:
                    if r.status_code == 416:
                        m416 = re_content_range_total.search(r.headers.get('content-range', ''))
                        if expected > 0 and have == expected and (not m416 or int(m416.group(1)) == expected):
                            break  # everything was already received
                        trace(f'Warning (W3): {item_id} range not satisfiable, restarting file', True)
                        have, expected, validator = 0, -1, ''
                        result.retries += 1
                        continue
                    r.raise_for_status()
                    if r.status_code == 206:
                        m = re_content_range_str.search(r.headers.get('content-range', ''))
                        total = int(m.group(3)) if m and m.group(3) else -1
                        if not m or int(m.group(1)) != have or total <= 0 or (expected > 0 and total != expected):
                            trace(f'Warning (W3): {item_id} unexpected content range \'{r.headers.get("content-range", "")}\', restarting', True)
                            have, expected, validator = 0, -1, ''
                            chunked = accept_ranges = False
                            result.retries += 1
                            continue
                        expected = total
                        accept_ranges = True
                    else:
                        # full body: the server does not support ranges or the file changed (If-Range mismatch)
                        have = 0
                        chunked = False
                        clen = r.headers.get('content-length', '')
                        expected = int(clen) if clen.isdigit() else -1
                        accept_ranges = r.headers.get('accept-ranges', '').lower() == 'bytes'
                    if not validator:
                        etag = r.headers.get('etag', '')
                        validator = etag if etag and not etag.startswith('W/') else r.headers.get('last-modified', '')
                    if orig_date and not modification_time_ns and (last_modified := r.headers.get('last-modified', '')):
                        with contextlib.suppress(Exception):
                            modification_time_ns = int(email.utils.parsedate_to_datetime(last_modified).timestamp() * 10 ** 9)
                    if expected == 0:
                        raise ValueError(f'Warning (W2): fetched {item_id} file is empty.')
                    with open(part, 'r+b' if have > 0 and part.is_file() else 'wb') as outf:
                        outf.seek(have)
                        outf.truncate()
                        for chunk in r.iter_content(STREAM_READ_SIZE):  # small reads: a dropped connection loses < 64 KB
                            if self.is_killed():
                                raise ThreadInterruptException
                            if chunk:
                                outf.write(chunk)
                                have += len(chunk)
                                self.bytes_received += len(chunk)  # read by the progress thread only: no lock needed
                if expected > 0 and have > expected:
                    trace(f'Warning (W3): {item_id} received more data than expected ({have:d} / {expected:d}), restarting', True)
                    have, expected, validator = 0, -1, ''
                    result.retries += 1
                    continue
                if expected > 0 and have < expected:
                    if chunked and have == window_end + 1:
                        continue  # next window
                    trace(f'Warning (W2): {item_id} connection dropped at {have:d} / {expected:d} bytes, '
                          f'{"resuming" if accept_ranges else "restarting"}...', True)
                    if not accept_ranges:
                        have = 0
                    result.retries += 1
                    continue
                if expected < 0:
                    expected = have  # no Content-Length (chunked transfer encoding): the stream ended normally
                break
            except (KeyboardInterrupt, ThreadInterruptException):
                raise
            except exceptions.HTTPError as err:
                response = err.response
                if is_cloudflare_challenge(response):
                    self.cf_challenge_hit = True
                    self._dl_cf_streak += 1
                    if self._dl_cf_streak <= CF_STREAK_STOP:
                        trace(f'{result.result_str}failed: {CF_CHALLENGE_HINT}', True)
                    if self._dl_cf_streak == CF_STREAK_STOP:
                        trace(f'Warning (W3): {CF_STREAK_STOP:d} bot challenges in a row, remaining files are skipped', True)
                    result.reason = 'bot challenge'
                    break
                code = response.status_code if response is not None else 0
                new_link = self._fix_link_after_download_error(link, original_link, err)
                result.retries += 1
                if new_link != link:
                    link = new_link
                    have, expected, validator = 0, -1, ''
                    continue
                if code in DOWNLOAD_PERMANENT_ERRORS:
                    result.reason = f'HTTP {code:d}, not retried'
                    break
                wait = 2.0 * min(5, result.retries)
                if code in (429, 503):
                    wait = parse_retry_after(response.headers.get('Retry-After')) or wait
                    self._note_backoff(wait, 'dl')  # every download thread pauses
                trace(f'{result.result_str}HTTP {code:d}, retry {result.retries:d} in {wait:.1f} sec...', True)
                if code not in (429, 503):
                    self.sleep_interruptible(wait)
                continue
            except Exception as err:
                new_link = self._fix_link_after_download_error(link, original_link, err)
                if new_link != link:
                    link = new_link
                    have, expected, validator = 0, -1, ''
                if isinstance(err, CLIENT_CONNECTOR_ERRORS):
                    conn_errors += 1
                    if conn_errors >= self.retries * CONNECT_RETRIES_PROXY_MULT:
                        result.reason = f'too many proxy / ssl errors ({conn_errors:d})'
                        break
                else:
                    result.retries += 1
                trace(f'{result.result_str}{format_exception("row")} retry {result.retries:d}...', True)
                self.sleep_interruptible(2.0)
                continue

        result.expected_size = expected if expected > 0 else 0
        if expected > 0 and have == expected and part.is_file():
            creation_time_ns = int(part.stat().st_ctime_ns) if modification_time_ns else 0
            os.replace(part, dest)
            self._dl_cf_streak = 0
            result.file_size = dest.stat().st_size
            if creation_time_ns and modification_time_ns:
                os.utime(dest, ns=(creation_time_ns, modification_time_ns))
        else:
            result.file_size = 0

    # threaded
    def wrap_request(self, url: str, tries: int, method: str, **kwargs) -> Response | None:
        r: Response | None = None
        retries = 0
        conn_errors = 0
        max_conn_errors = max(1, tries) * CONNECT_RETRIES_PROXY_MULT
        session = self._thread_session()
        while retries < tries:
            self.catch_cancel_or_ctrl_c()
            r = None
            try:
                self._respect_rate_limit()
                r = session.request(method, url, timeout=self.timeout, stream=False, allow_redirects=True, **kwargs)
                r.raise_for_status()
                break
            except (KeyboardInterrupt, ThreadInterruptException):
                thread_exit('interrupted by user.', code=1)
            except Exception as err:
                if isinstance(err, CLIENT_CONNECTOR_ERRORS):
                    conn_errors += 1
                    if conn_errors >= max_conn_errors:
                        trace(f'Too many proxy / ssl errors ({conn_errors:d}), check your proxy settings. Aborting {url}', True)
                        return None
                else:
                    retries += 1
                sleep_time = 1.0
                threadname = f'{current_process().name}: ' if current_process() != self.my_root_thread else ''
                response = err.response if isinstance(err, exceptions.HTTPError) else None
                if is_cloudflare_challenge(response):
                    self.cf_challenge_hit = True
                    trace(f'{threadname}{CF_CHALLENGE_HINT}\nAborting {url}', True)
                    return None
                if response is not None and response.status_code in CLIENT_ERRORS_NO_RETRY:
                    if 'json' in response.headers.get('content-type', '') and response.content:
                        return response  # api error message, ex. danbooru 'You cannot search for more than 2 tags at a time.'
                    trace(f'{threadname}HTTP {response.status_code:d} for {url}, not retrying', True)
                    return None
                if response is not None and response.status_code == 404:
                    if r is not None and r.content and len(r.content.decode(errors='replace')) > 2:
                        trace(f'{threadname}received code 404 but received html'
                              f'\n{format_exception("row").strip()}.\nContinuing...', True)
                        break
                    trace(f'{threadname}catched err 404 {format_exception("row")}. Aborting...', True)
                    return None
                elif response is not None and response.status_code in (429, 503):  # Too Many Requests / Service Unavailable
                    retry_after = parse_retry_after(response.headers.get('Retry-After'))
                    sleep_time = retry_after if retry_after > 0 else sleep_time + float(min(9, retries))
                    self._note_backoff(sleep_time, 'api')  # all threads wait, not only this one
                    trace(f'{threadname}catched {format_exception("row")}.'
                          f'{f" Reconnecting in {sleep_time:.1f} sec... {retries:d}" if retries < tries else ""}', True)
                else:
                    trace(f'{threadname}catched {format_exception("row")}.'
                          f'{f" Reconnecting in {sleep_time:.1f} sec... {retries:d}" if retries < tries else ""}', True)
                self.sleep_interruptible(sleep_time)
                continue

        if retries >= tries:
            trace(f'Unable to connect. Aborting {url}', True)
            r = None
        elif r is None:
            trace('ERROR: Failed to receive any data', True)
        elif len(r.cookies) > 0:
            with self._cookies_lock:
                t = self.add_cookies.copy()
                self.add_cookies = r.cookies.copy()
                self.add_cookies.update(t)

        return r

    # threaded
    def fetch_html(self, url: str, *, tries=0, do_cache=False, method='GET', **kwargs) -> HTMLContent | None:
        if cached := self.raw_html_cache.get(url, HTMLContent.init_empty()):
            return cached
        r = self.wrap_request(url, tries or self.retries, method, **kwargs)
        content = r.content if r is not None else None
        result = HTMLContent(None, content) if content else None
        if result and do_cache:
            self.raw_html_cache[url] = result
        return result

#
#
#########################################
