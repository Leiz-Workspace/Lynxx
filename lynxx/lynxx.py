# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

import sys

__all__ = ('lynxx_main',)


def lynxx_main(argv: list[str]) -> int:
    """Returns the process exit code (see lynxx_cmd.run_cmd)"""
    if argv:
        from .lynxx_cmd import run_cmd
        return run_cmd(argv)
    from .lynxx_gui import run_gui  # tkinter is only needed here
    run_gui()
    return 0


if __name__ == '__main__':
    sys.exit(lynxx_main(sys.argv[1:]))

#
#
#########################################
