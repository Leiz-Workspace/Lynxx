#!/usr/bin/env python3
# coding=UTF-8
"""
Lynxx tag lists updater (cron / systemd timer / manual), for a repository checkout: lists go to the repository's 2tags/.
Usage: python tools/update_tags.py [--modules rx,db] [--out 2tags] [--min-posts 1] [--delay 0.5] [--api-key KEY,USER] [--force] [--log FILE]
The packaged app runs the same updater as 'lynxx-core tags' (output: <data dir>/tags), see lynxx/tags_updater.py main.
Exit code: 0 = all requested lists updated (or skipped by sanity check), 1 = at least one module failed
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, ROOT.as_posix())

from lynxx.tags_updater import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main(default_out=ROOT / '2tags'))
