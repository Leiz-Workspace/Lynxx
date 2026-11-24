# coding=UTF-8
"""
Entry point of the frozen core shipped with the desktop app (PyInstaller one-dir build, see tools/build_core.py):
  lynxx-core server        JSON-RPC sidecar over stdio (lynxx.server)
  lynxx-core cli ARGS...   command line downloader (lynxx CLI; exit codes of lynxx_cmd.run_cmd)
  lynxx-core aigen         extension generator session worker (lynxx.aigen.worker, stdin / stdout protocol)
  lynxx-core extgen ARGS   extension generator tools (python -m lynxx.extgen: survey / new / record / check / pack)
  lynxx-core sandbox       test run of an extension draft (lynxx.extgen.sandbox_boot, started by lynxx.extgen.sandbox)
  lynxx-core index ARGS    download index tools (python -m lynxx.dlindex_tools: clean / import / retag)
The legacy Tk GUI is not bundled.
"""
#########################################
#
#

import multiprocessing
import sys

__all__ = ('main',)

USAGE = ('usage: lynxx-core server | lynxx-core cli ARGS... | lynxx-core aigen | lynxx-core extgen ARGS... | lynxx-core sandbox'
         ' | lynxx-core index ARGS... | lynxx-core tags ARGS...')


def _setup_streams() -> None:
    # PyInstaller ignores PYTHONUNBUFFERED / PYTHONIOENCODING: the app reads progress and RPC lines as they are printed
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
    command, rest = argv[0], argv[1:]
    if command == 'server':
        from lynxx.server import main as server_main
        return server_main()
    if command == 'cli':
        if not rest:
            print('cli: arguments required (the legacy GUI is not bundled)', file=sys.stderr)
            return 2
        from lynxx.lynxx import lynxx_main
        return lynxx_main(rest)
    if command == 'aigen':
        from lynxx.aigen.worker import main as worker_main
        return worker_main()
    if command == 'extgen':
        from lynxx.extgen.__main__ import main as extgen_main
        return extgen_main(rest)
    if command == 'index':
        from lynxx.dlindex_tools import main as index_main
        return index_main(rest)
    if command == 'tags':  # tag lists update: the app's scheduled run (cron / Task Scheduler), see lynxx/tags_updater.py main
        from lynxx.tags_updater import main as tags_main
        return tags_main(rest)
    if command == 'sandbox':
        import runpy
        runpy.run_module('lynxx.extgen.sandbox_boot', run_name='__main__')
        return 0
    print(f'unknown command \'{command}\'\n{USAGE}', file=sys.stderr)
    return 2


if __name__ == '__main__':
    multiprocessing.freeze_support()
    sys.exit(main())

#
#
#########################################
