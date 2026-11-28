# coding=UTF-8
"""
Entry point of the frozen core shipped with the desktop app (PyInstaller one-dir build, see tools/build_core.py):
  lynxx-core server        JSON-RPC sidecar over stdio (lynxx.server)
"""

import multiprocessing
import sys

__all__ = ('main',)

USAGE = 'usage: lynxx-core server'


def _setup_streams() -> None:
    # PyInstaller ignores PYTHONUNBUFFERED / PYTHONIOENCODING: the app reads RPC lines as they are printed
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is not None:
            reconfigure(encoding='utf-8', errors='replace', line_buffering=True)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    _setup_streams()
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(USAGE, file=sys.stderr)
        return 0 if argv else 2
    if argv[0] == 'server':
        from lynxx.server import main as server_main
        return server_main()
    print(f'unknown command \'{argv[0]}\'\n{USAGE}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    multiprocessing.freeze_support()
    sys.exit(main())
