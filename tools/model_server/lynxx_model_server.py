# coding=UTF-8
"""
Entry point of the standalone gender model server bundle (tools/build_model_server.py, PyInstaller one-dir):
  lynxx-model-server [--host 127.0.0.1] [--port 8765] [--device auto|cpu|gpu] ...   same options as python -m lynxx.model_server
The model runs in a child process (lynxx.model_server.ModelProcess, multiprocessing 'spawn'): a frozen program re-runs its own
executable for that child, which freeze_support() handles before anything else is imported.
"""
#########################################
#
#

import multiprocessing
import sys


def _setup_streams() -> None:
    # PyInstaller ignores PYTHONUNBUFFERED: systemd / launchd / Task Scheduler logs get lines as they are printed
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, 'reconfigure', None)
        if reconfigure is not None:
            reconfigure(encoding='utf-8', errors='replace', line_buffering=True)


if __name__ == '__main__':
    multiprocessing.freeze_support()
    _setup_streams()
    from lynxx.model_server import main
    sys.exit(main())

#
#
#########################################
