# coding=UTF-8
"""Where Lynxx keeps its data: the same folder as Electron's app.getPath('userData') for the app name 'Lynxx'."""

from __future__ import annotations

import os
import pathlib
import sys

ENV_DATA_DIR = 'LYNXX_DATA_DIR'

__all__ = ('ENV_DATA_DIR', 'data_dir')


def data_dir() -> pathlib.Path:
    """Per-user data folder; LYNXX_DATA_DIR overrides it (the app passes its userData, tests a scratch folder)"""
    if env := os.environ.get(ENV_DATA_DIR):
        return pathlib.Path(env).expanduser()
    if sys.platform == 'win32':
        base = pathlib.Path(os.environ.get('APPDATA') or pathlib.Path.home() / 'AppData' / 'Roaming')
    elif sys.platform == 'darwin':
        base = pathlib.Path.home() / 'Library' / 'Application Support'
    else:
        base = pathlib.Path(os.environ.get('XDG_CONFIG_HOME') or pathlib.Path.home() / '.config')
    return base / 'Lynxx'
