# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

import sys

from .gui import run_lynxx_gui

__all__ = ('run_gui',)


def run_gui() -> None:
    run_lynxx_gui()


if __name__ == '__main__':
    if len(sys.argv) <= 1:
        run_gui()
        sys.exit(0)
    else:
        print('ERROR: Lynxx cmd arguments found. To run CMD use lynxx_cmd.py')
        sys.exit(-1)

#
#
#########################################
