# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
Lynxx: downloader classes are registered by extensions (see extensions.py), builtin ones are 'rx' and 'db'
"""
#########################################
#
# From this module only import to top level (tests.py, gui.py, lynxx_cmd.py, lynxx_gui.py, server.py)
#

from .download import Downloader
from .module import ProcModule

__all__ = ('DOWNLOADERS_BY_PROC_MODULE', 'get_new_downloader')


DOWNLOADERS_BY_PROC_MODULE: dict[int, type[Downloader]] = {}
'''filled by Extensions._register()'''


def _make_downloader(proc_module: int) -> Downloader:
    from .extensions import Extensions
    if proc_module not in DOWNLOADERS_BY_PROC_MODULE and Extensions.catalog_autoload():
        # legacy behavior: cli / tk gui / tests can use any locally available extension (the desktop app disables it)
        Extensions.ensure_loaded(include_catalog=True)
    if proc_module not in DOWNLOADERS_BY_PROC_MODULE:
        name = ProcModule.PROC_MODULES_NAMES_BY_ID.get(proc_module, str(proc_module))
        raise KeyError(f'Module \'{name}\' is not installed. Install its extension first')
    ProcModule.set(proc_module)
    return DOWNLOADERS_BY_PROC_MODULE[proc_module]()


def get_new_downloader() -> Downloader:
    return _make_downloader(ProcModule.CUR_PROC_MODULE)

#
#
#########################################
