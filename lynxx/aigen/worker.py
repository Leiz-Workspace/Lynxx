# coding=UTF-8
"""
python -m lynxx.aigen.worker        (spawned by the app: one generation session per process)
stdin, one JSON object per line:
  first  {"cmd": "start", "url": "...", "media": ["image", "video"], "mode": "ai" | "template", "model": "...",
          "maxUsd": 2, "maxTurns": 30, "query": "", "apiKey": "..."}
  later  {"cmd": "cancel"}             (end of stdin also cancels: the app went away)
stdout: '@@GEN {json}' lines, types start / phase / log / usage / done (done carries the report). Other output goes to stderr.
The API key arrives only through stdin and stays in this process memory: never in argv, environment, files, events or logs.
The session writes a draft folder under <data dir>/extgen/drafts/<session>/: it never installs anything.
Exit code: 0 ready, 1 failed / refused / cancelled, 2 error.
"""
#########################################
#
#

import json
import os
import sys
import threading
from typing import Any, TextIO


def drafts_root():
    from lynxx.extensions import data_dir
    return data_dir() / 'extgen' / 'drafts'


def _read_commands(stream: TextIO, cancel: threading.Event) -> None:
    for line in stream:
        try:
            cmd = json.loads(line).get('cmd')
        except (ValueError, AttributeError):
            continue
        if cmd == 'cancel':
            cancel.set()
    cancel.set()  # stdin closed: the app is gone


def main() -> int:
    out = sys.stdout
    sys.stdout = sys.stderr  # core / library prints must not mix with the event stream
    lock = threading.Lock()

    def emit(kind: str, data: dict[str, Any]) -> None:
        line = '@@GEN ' + json.dumps({'type': kind, **data}, ensure_ascii=False, default=str)
        with lock:
            out.write(line + '\n')
            out.flush()

    def fail(summary: str) -> int:
        emit('done', {'status': 'error', 'summary': summary, 'session': '', 'ruleIds': [], 'draftDir': '', 'extId': '', 'usd': 0})
        return 2

    try:
        cfg = json.loads(sys.stdin.readline() or '{}')
        if not isinstance(cfg, dict) or cfg.get('cmd') != 'start':
            return fail('Lệnh khởi động không hợp lệ.')
    except ValueError:
        return fail('Lệnh khởi động không hợp lệ.')
    api_key = str(cfg.pop('apiKey', '') or '')
    os.environ.pop('ANTHROPIC_API_KEY', None)  # only the key given by the app is used

    from lynxx.logger import Logger
    Logger.init(True, True)
    from .session import GenRequest, GenSession
    try:
        req = GenRequest.from_dict(cfg)
    except (TypeError, ValueError) as err:
        return fail(f'Tham số không hợp lệ: {err}')
    client = None
    if req.mode == 'ai':
        if not api_key:
            return fail('Chưa có khoá Claude API: thêm khoá trong Cài đặt, hoặc dùng chế độ không AI.')
        try:
            import anthropic
        except ImportError:
            return fail('Thiếu thư viện anthropic: cài bằng  pip install "lynxx[ai]"  (hoặc pip install anthropic).')
        client = anthropic.Anthropic(api_key=api_key, max_retries=3)
    api_key = ''
    cancel = threading.Event()
    threading.Thread(target=_read_commands, args=(sys.stdin, cancel), daemon=True).start()
    session = GenSession(req, drafts_root=drafts_root(), client=client, emit=emit, cancel=cancel)
    report = session.run()
    return 0 if report['status'] == 'ready' else 1 if report['status'] in ('failed', 'refused', 'cancelled') else 2


if __name__ == '__main__':
    sys.exit(main())

#
#
#########################################
