# coding=UTF-8
"""
python -m lynxx.extgen <command> ...   (used by the Claude Code skill new-site-extension and by developers)
  survey <url>                         robots.txt, engine detection, proposed id / abbr (a few requests)
  fetch <url> [--accept A] [--max-chars N]
                                       one GET through the guarded fetcher (robots.txt, https only, no cookies, 2 MB):
                                       status, type and the body (scripts / styles removed) to read an api / page
  new <url> --out DIR [--id --abbr --name --media image,video --engine E] [--no-tags]
                                       create DIR/<id>/ (manifest.json, specs.md, downloader.py for template engines) and read
                                       the site's tag list into it (<abbr>_tags.json, see tags)
  tags DIR                             read the site's tag list into the draft (engines with a tag listing, robots.txt
                                       applies, 30 requests / 90 s / 8 MB at most: bigger lists are read by the app after the install)
  record DIR --query "tags" [--limit 3]
                                       live test run in the sandbox: records fixtures + expected.json
  check DIR                            offline: manifest + static policy + replay of the fixtures
  pack DIR --out FILE.zip              zip + sha256 for a manual install (never installed by this tool)
Output is JSON on stdout. Exit code 0: ok, 1: checks failed, 2: refused / error.
"""
#########################################
#
#

import argparse
import json
import pathlib
import sys

from lynxx.logger import Logger


def _print(data: dict) -> None:
    print(json.dumps(data, indent=1, ensure_ascii=False, default=str))


def main(argv: list[str] | None = None) -> int:
    Logger.init(True, True)
    from .fetch import FetchPolicyError, normalize_site, redact_url, strip_html
    from .sandbox import check_draft, pack, record
    from .scaffold import create_draft, fetcher_for, survey
    from .taglist import bundle_tag_list

    p = argparse.ArgumentParser(prog='python -m lynxx.extgen', allow_abbrev=False)
    sub = p.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('survey')
    s.add_argument('url')
    f = sub.add_parser('fetch')
    f.add_argument('url')
    f.add_argument('--accept', default='')
    f.add_argument('--max-chars', type=int, default=20000)
    n = sub.add_parser('new')
    n.add_argument('url')
    n.add_argument('--out', required=True)
    n.add_argument('--id', default='')
    n.add_argument('--abbr', default='')
    n.add_argument('--name', default='')
    n.add_argument('--media', default='image,video')
    n.add_argument('--engine', default='')
    n.add_argument('--no-tags', action='store_true')
    g = sub.add_parser('tags')
    g.add_argument('draft')
    r = sub.add_parser('record')
    r.add_argument('draft')
    r.add_argument('--query', required=True)
    r.add_argument('--limit', type=int, default=3)
    c = sub.add_parser('check')
    c.add_argument('draft')
    k = sub.add_parser('pack')
    k.add_argument('draft')
    k.add_argument('--out', required=True)
    a = p.parse_args(argv)
    try:
        if a.cmd == 'survey':
            _print(survey(a.url))
            return 0
        if a.cmd == 'fetch':
            fetcher = fetcher_for(normalize_site(a.url), budget=3)
            res = fetcher.fetch(a.url, accept=a.accept)
            body = res.text()
            if 'html' in res.content_type:
                body = strip_html(body)
            _print({'url': redact_url(res.url), 'status': res.status, 'contentType': res.content_type, 'bytes': len(res.body),
                    'truncated': res.truncated, 'robots': fetcher.robots_info(a.url)['status'],
                    'body': body[:max(200, min(a.max_chars, 200000))]})
            return 0 if res.status < 400 else 1
        if a.cmd == 'new':
            sv = survey(a.url)
            draft = create_draft(pathlib.Path(a.out), sv, ext_id=a.id, abbr=a.abbr, name=a.name, engine=a.engine,
                                 media=[x.strip() for x in a.media.split(',') if x.strip()])
            has_code = (draft / 'downloader.py').is_file()
            tags = {'status': 'skipped', 'message': '--no-tags'} if a.no_tags else bundle_tag_list(draft)
            _print({'draft': str(draft), 'survey': sv, 'downloaderGenerated': has_code, 'tagList': tags,
                    'next': f'python -m lynxx.extgen record {draft} --query "<tags>"' if has_code else
                    'write downloader.py (no template for this engine), then record'})
            return 0
        if a.cmd == 'tags':
            res = bundle_tag_list(pathlib.Path(a.draft))
            _print(res)
            return 0 if res['status'] in ('bundled', 'later', 'learned') else 1
        if a.cmd == 'record':
            draft = pathlib.Path(a.draft)
            site = json.loads((draft / 'manifest.json').read_text(encoding='utf-8'))['site']
            res = record(draft, query=a.query.split(), limit=max(1, min(a.limit, 10)), fetcher=fetcher_for(site, budget=25))
            _print(res)
            return 0 if res.get('ok') else 1
        if a.cmd == 'check':
            res = check_draft(pathlib.Path(a.draft))
            _print(res)
            return 0 if res.get('ok') else 1
        if a.cmd == 'pack':
            _print(pack(pathlib.Path(a.draft), pathlib.Path(a.out)))
            return 0
    except FetchPolicyError as err:
        _print({'refused': True, 'rule': err.rule, 'message': err.message})
        return 2
    except (ValueError, FileExistsError, OSError) as err:
        _print({'error': str(err)})
        return 2
    return 2


if __name__ == '__main__':
    sys.exit(main())

#
#
#########################################
