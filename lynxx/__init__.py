"""Lynxx core: the library, file converters, reader data and exports behind the desktop app (see lynxx/server.py)."""

import sys


def main(argv=None) -> None:
    """Console script entry: the JSON-RPC sidecar of the desktop app (it reads requests on stdin)"""
    from .server import main as server_main
    sys.exit(server_main())


__all__ = ('main',)
