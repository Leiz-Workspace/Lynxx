# coding=UTF-8
"""
Lynxx extension generator: static policy check of an extension's downloader.py (plan B.6, rules R1 R4 R5 R6 R10).
A downloader only parses website responses and builds urls: the core does every request, download, rate limit and safety filter.
Errors block an extension; warnings are shown in the report for the reviewer.
"""
#########################################
#
#

from __future__ import annotations

import ast
import re
from dataclasses import asdict, dataclass

__all__ = ('Violation', 'check_manifest', 'check_source')

MAX_SOURCE_BYTES = 200 * 1024
MIN_INTERVAL_FLOOR = 0.5  # seconds between api requests of a non builtin extension (R4)

ALLOWED_LYNXX = {
    'lynxx.sdk',  # the stable API for extensions (lynxx/sdk.py): what generated code uses
    'lynxx.defines', 'lynxx.download', 'lynxx.download_base', 'lynxx.download_gelbooru', 'lynxx.download_db', 'lynxx.download_json',
    'lynxx.logger', 'lynxx.rex', 'lynxx.utils', 'lynxx.tags', 'lynxx.vcs', 'lynxx.vcs.version', 'lynxx.vcs.debug',
}
ALLOWED_NETWORK_NAMES = {'HTMLContent', 'thread_exit', 'DownloadInterruptException', 'LYNXX_USER_AGENT'}
ALLOWED_STDLIB = {
    're', 'json', 'datetime', 'time', 'math', 'html', 'base64', 'itertools', 'collections', 'collections.abc', 'typing',
    'dataclasses', 'functools', 'enum', 'string', 'urllib', 'urllib.parse', '__future__', 'bs4',
}
ALLOWED_FROM_URLLIB = {'parse'}
BANNED_LIBS_HINT = ('cloudscraper', 'curl_cffi', 'selenium', 'playwright', 'undetected_chromedriver', 'js2py', 'execjs',
                    'pyppeteer', 'tls_client', 'twocaptcha', 'anticaptcha', 'capsolver')
BANNED_CALLS = {'eval', 'exec', 'compile', '__import__', 'open', 'input', 'breakpoint', 'globals', 'locals', 'vars', 'exit', 'quit'}
DYNAMIC_ATTR_CALLS = {'getattr', 'setattr', 'delattr', 'hasattr'}
BANNED_ATTRS = {
    '__subclasses__', '__globals__', '__builtins__', '__code__', '__closure__', '__dict__', '__bases__', '__mro__',
    '__getattribute__', '__import__', '__reduce__', '__reduce_ex__', '__loader__', '__spec__', 'f_globals', 'f_locals',
    'f_back', 'gi_frame', 'tb_frame', 'cr_frame', 'system', 'popen', 'spawnl', 'execv', 'execve',
}
PROTECTED_METHODS = {
    # network, downloads and pacing are done by the core: a downloader must not replace them (R4 R5 R10)
    'fetch_html', 'wrap_request', 'download_file', '_download_to_part', '_download', '_download_session', '_thread_session',
    'make_session', 'close_sessions', '_respect_rate_limit', '_respect_download_rate', '_wait_backoff', '_note_backoff',
    'sleep_interruptible', 'is_killed', 'catch_cancel_or_ctrl_c', 'reset_root_thread',
    # safety and content filters (R9) and the pipeline
    '_filter_items_safety', '_filter_items_by_gender', '_filter_existing_items', '_launch', 'launch_download',
    'launch_check_tags', 'launch_get_max_id', '_process_item_safe', '_run_parallel', '_get_executor', '_stream_run',
    '_on_thread_exception', '_parse_args', '_solve_argument_conflicts',
}
CREDENTIAL_COOKIES = re.compile(r'cf_clearance|pass_hash|session(?:id)?|PHPSESSID|remember_token', re.IGNORECASE)
re_url_host = re.compile(r'https?://([a-z\d.-]+)', re.IGNORECASE)


@dataclass
class Violation:
    rule: str
    severity: str  # error | warning
    line: int
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


def _host_ok(host: str, allowed: set[str]) -> bool:
    host = host.lower().rstrip('.')
    return any(host == h or host.endswith(f'.{h}') for h in allowed)


def check_source(source: str, *, allowed_hosts: set[str] | None = None, downloader: bool = True) -> list[Violation]:
    """allowed_hosts: site host (+ cdn hosts) of the manifest; urls in string constants outside them are warnings.
    downloader=False: the extension's tag update file (manifest 'tagUpdate'), same rules without the Downloader class"""
    out: list[Violation] = []

    def err(rule: str, node: ast.AST | None, msg: str, severity: str = 'error') -> None:
        out.append(Violation(rule, severity, getattr(node, 'lineno', 0), msg))

    if len(source.encode()) > MAX_SOURCE_BYTES:
        err('R10', None, f'{"downloader.py" if downloader else "the tag update file"} is larger than {MAX_SOURCE_BYTES // 1024} KB')
        return out
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        out.append(Violation('R10', 'error', e.lineno or 0, f'syntax error: {e.msg}'))
        return out

    has_downloader_class = False
    docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                  if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and n.body
                  and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)}
    for node in ast.walk(tree):
        # ---- imports
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_STDLIB and a.name not in ALLOWED_LYNXX:
                    err('R10', node, f'import of \'{a.name}\' is not allowed'
                        + (' (bot protection bypass tools are forbidden, R5)' if a.name.split('.')[0] in BANNED_LIBS_HINT else ''))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ''
            if node.level:
                err('R10', node, 'relative imports are not allowed')
            elif mod == 'lynxx.network':
                bad = [a.name for a in node.names if a.name not in ALLOWED_NETWORK_NAMES]
                if bad:
                    err('R10', node, f'from lynxx.network only {", ".join(sorted(ALLOWED_NETWORK_NAMES))} may be imported, not {bad}')
            elif mod == 'requests':
                if any(a.name != 'exceptions' for a in node.names):
                    err('R10', node, 'only \'from requests import exceptions\' is allowed: requests go through the core')
            elif mod == 'multiprocessing.dummy':
                if any(a.name != 'current_process' for a in node.names):
                    err('R10', node, 'only \'from multiprocessing.dummy import current_process\' is allowed')
            elif mod == 'urllib':
                if any(a.name not in ALLOWED_FROM_URLLIB for a in node.names):
                    err('R10', node, 'only \'from urllib import parse\' is allowed')
            elif mod not in ALLOWED_STDLIB and mod not in ALLOWED_LYNXX:
                err('R10', node, f'import from \'{mod}\' is not allowed'
                    + (' (bot protection bypass tools are forbidden, R5)' if mod.split('.')[0] in BANNED_LIBS_HINT else ''))
            if any(a.name == '*' for a in node.names):
                err('R10', node, 'star imports are not allowed')
        # ---- calls
        elif isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Name) and f.id in BANNED_CALLS:
                err('R10', node, f'call to {f.id}() is not allowed')
            if isinstance(f, ast.Name) and f.id in DYNAMIC_ATTR_CALLS:
                if len(node.args) < 2 or not (isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)):
                    err('R10', node, f'{f.id}() with a computed attribute name is not allowed')
                elif node.args[1].value.startswith('__'):
                    err('R10', node, f'{f.id}() on a dunder attribute is not allowed')
        # ---- attributes
        elif isinstance(node, ast.Attribute):
            if node.attr in BANNED_ATTRS:
                err('R10', node, f'access to .{node.attr} is not allowed')
        elif isinstance(node, ast.Name):
            if node.id in ('__builtins__', '__loader__', '__spec__'):
                err('R10', node, f'access to {node.id} is not allowed')
        # ---- classes: protected hooks, rate floor
        elif isinstance(node, ast.ClassDef):
            bases = {getattr(b, 'id', getattr(b, 'attr', '')) for b in node.bases}
            if bases & {'Downloader', 'DownloaderGelbooru', 'DownloaderDb', 'DownloaderJsonApi'}:
                has_downloader_class = True
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name in PROTECTED_METHODS:
                        err('R10', item, f'{node.name}.{item.name}() is a core hook and must not be overridden')
                    if item.name == '_get_min_request_interval':
                        rets = [r for r in ast.walk(item) if isinstance(r, ast.Return)]
                        vals = [r.value.value for r in rets if isinstance(r.value, ast.Constant) and isinstance(r.value.value, (int, float))]
                        if not vals or len(vals) != len(rets) or min(vals) < MIN_INTERVAL_FLOOR:
                            err('R4', item, f'_get_min_request_interval() must return a constant >= {MIN_INTERVAL_FLOOR} s')
        # ---- string constants: credential cookies, foreign hosts
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
            if CREDENTIAL_COOKIES.search(node.value) and len(node.value) < 40:
                err('R1', node, f'the string \'{node.value}\' looks like a login / challenge cookie: extensions must not use them',
                    'warning')
            if allowed_hosts:
                for host in re_url_host.findall(node.value):
                    if not _host_ok(host, allowed_hosts):
                        err('R10', node, f'url host \'{host}\' is not the site (or its cdn hosts)', 'warning')
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            err('R10', node, 'global / nonlocal statements are not allowed', 'warning')
    if downloader and not has_downloader_class:
        err('R10', None, 'no class derived from Downloader / DownloaderGelbooru / DownloaderDb / DownloaderJsonApi')
    return out


def check_manifest(data: dict) -> list[Violation]:
    """Generated manifests: no login / challenge cookies (R1), any other cookie is reported for review"""
    out: list[Violation] = []
    auth = data.get('auth') if isinstance(data, dict) else None
    cookies = auth.get('cookies') if isinstance(auth, dict) else None
    for name in cookies if isinstance(cookies, list) else []:
        if CREDENTIAL_COOKIES.search(str(name)):
            out.append(Violation('R1', 'error', 0, f'auth.cookies: \'{name}\' is a login / challenge cookie: generated extensions must not ask for it'))
        else:
            out.append(Violation('R1', 'warning', 0, f'auth.cookies: \'{name}\' asks the user for a cookie: check that it is not a login'))
    return out


def errors(violations: list[Violation]) -> list[Violation]:
    return [v for v in violations if v.severity == 'error']

#
#
#########################################
