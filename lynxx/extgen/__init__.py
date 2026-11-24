# coding=UTF-8
"""
Lynxx extension generator toolkit (docs/plans/site-extension-skill-and-ai-generator.md).
Shared by the Claude Code skill (.claude/skills/new-site-extension) and the in-app AI generator (lynxx.aigen):
- fetch: bounded, robots.txt aware https fetcher (the only way the generator talks to a website)
- engines: site engine detection (gelbooru DAPI, danbooru, e621, moebooru, philomena, szurubooru, shimmie)
- templates: downloader.py / manifest.json for engines with a known API
- policy: static check of a downloader.py (imports, dangerous calls, protected hooks)
- sandbox: runs an extension draft in an isolated process (no sockets: requests go through the parent's fetcher)
- pack: zip + sha256 of a draft for a manual install
A generated extension is a draft folder: it is never installed automatically.
"""

__all__ = ()
