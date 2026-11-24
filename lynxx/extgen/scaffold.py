# coding=UTF-8
"""
Lynxx extension generator: survey a site and create an extension draft folder.
"""
#########################################
#
#

from __future__ import annotations

import json
import pathlib

from lynxx.tags_updater import ENGINE_TAG_SOURCES

from .engines import ENGINES, detect_engine
from .fetch import FetchPolicyError, SafeFetcher, check_denied, host_of, normalize_site
from .templates import (
    ENGINE_NOTES,
    TEMPLATE_ENGINES,
    existing_extensions,
    make_manifest,
    render_downloader,
    suggest_names,
    validate_names,
)

__all__ = ('create_draft', 'fetcher_for', 'survey')

SPECS_TEMPLATE = '''# {name} ({abbr}) — extension specs

- Site: {site}
- Engine: {engine} ({engine_desc}), confidence {confidence}
- robots.txt: {robots}
- Tag list: {tag_list}
- Generated: lynxx.extgen ({how})

## Evidence
{evidence}

## Notes
{notes}

## Review checklist
- [ ] robots.txt / terms of the site allow automated access to the paths used
- [ ] no login, cookie or challenge bypass; only public data (or the user's own API key)
- [ ] request interval is polite (>= 0.5 s for an API, more if the site asks)
- [ ] file urls come from the site's own fields (no urls built for posts the site hides)
- [ ] search kinds (manifest 'search', Lynxx 2.0.11) only after the robots.txt / terms check of each kind's route (`search:<id>`)
- [ ] test run (`python -m lynxx.extgen check <folder>`) passes
'''


def fetcher_for(site: str, *, budget: int = 40, allow_local: bool = False) -> SafeFetcher:
    """Fetcher limited to the site host and its parent domain (api.example.com, cdn.example.com)"""
    host = host_of(site)
    base = '.'.join(host.split('.')[-2:]) if host.count('.') >= 1 and not host.replace('.', '').isdigit() else host
    return SafeFetcher({host, base}, budget=budget, allow_local=allow_local)


def survey(site_url: str, fetcher: SafeFetcher | None = None) -> dict:
    site = normalize_site(site_url) if not (fetcher and fetcher.allow_local) else site_url.rstrip('/') + '/'
    check_denied(site)  # also with a test fetcher (normalize_site checks it otherwise)
    fetcher = fetcher or fetcher_for(site)
    robots = fetcher.robots_info(site)
    if robots['status'].startswith('unreachable'):
        raise FetchPolicyError('R2', f'robots.txt of {host_of(site)} is {robots["status"]}: that means no access (RFC 9309)')
    guess = detect_engine(fetcher, site)
    if not guess.evidence and any(n.startswith('skipped') for n in guess.notes):
        raise FetchPolicyError('R2', f'robots.txt of {host_of(site)} does not allow any api or page Lynxx could use')
    return {'site': site, 'robots': robots, 'engine': guess.to_dict(), 'names': suggest_names(site),
            'existing': existing_extensions(site), 'requests': list(fetcher.log)}


def create_draft(base_dir: pathlib.Path, survey_result: dict, *, ext_id: str = '', abbr: str = '', name: str = '',
                 media: list[str] | None = None, engine: str = '', generator: dict | None = None, description: str = '') -> pathlib.Path:
    """Writes <base_dir>/<id>/ with manifest.json, specs.md and, for engines with a template, downloader.py"""
    site = survey_result['site']
    names = survey_result['names']
    ext_id, abbr, name = ext_id or names['id'], abbr or names['abbr'], name or names['name']
    validate_names(ext_id, abbr, name)
    eng = survey_result['engine']
    engine = engine or eng['engine']
    if engine not in ENGINES:
        raise ValueError(f'unknown engine \'{engine}\'')
    draft = pathlib.Path(base_dir) / ext_id
    if draft.exists() and any(draft.iterdir()):
        raise FileExistsError(f'{draft} already exists')
    (draft / 'tests').mkdir(parents=True, exist_ok=True)
    api_key = bool(eng.get('auth_required'))
    manifest = make_manifest(ext_id=ext_id, abbr=abbr, name=name, site=site, engine=engine, media=media or ['image', 'video'],
                             description=description or f'{name}: {ENGINES[engine]["description"]}', api_key=api_key,
                             generator=generator)
    (draft / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    if engine in TEMPLATE_ENGINES:
        (draft / 'downloader.py').write_text(render_downloader(engine, ext_id=ext_id, abbr=abbr, site=site, api_key=api_key),
                                             encoding='utf-8')
    evidence = '\n'.join(f'- `{e["url"]}` → {e["status"]} {e["type"]}' for e in eng.get('evidence', [])) or '- none'
    notes = '\n'.join(f'- {n}' for n in [*eng.get('notes', []), *([ENGINE_NOTES[engine]] if engine in ENGINE_NOTES else [])]) or '- none'
    robots = survey_result.get('robots', {})
    (draft / 'specs.md').write_text(SPECS_TEMPLATE.format(
        name=name, abbr=abbr, site=site, engine=engine, engine_desc=ENGINES[engine]['description'], confidence=eng['confidence'],
        robots=f'{robots.get("status", "?")}, crawl-delay {robots.get("crawlDelay", 0)} s', evidence=evidence, notes=notes,
        tag_list=(f'`{manifest["tagList"]}` from {ENGINE_TAG_SOURCES[engine]} (lynxx-core tags --modules {abbr}, robots.txt applies)'
                  if manifest.get('tagList') else 'none (no tag listing known for this engine)'),
        how='template' if engine in TEMPLATE_ENGINES else 'downloader.py must be written (AI or developer)'), encoding='utf-8')
    return draft

#
#
#########################################
