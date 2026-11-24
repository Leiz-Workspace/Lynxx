# coding=UTF-8
"""
Lynxx: evaluation tooling of the gender filter (docs/plans/gender-filter-model.md §3.2, §3.7, §4; AC-G08, AC-S04).
Never stores pictures: they are fetched in memory through the module's own downloader session (headers, proxy, request
pacing), shown or sent to the model, then dropped. Outputs hold post ids, md5, urls, tags, labels and metrics only.

  python -m lynxx.gender_eval sample --module db --query "random:100 rating:g" [--query ...] [--quota ...] --out manifest.json
      stratified post set (metadata only), exclusion order of §3.2 with a count per step, tier-0 label and slices (§3.7)
  python -m lynxx.gender_eval label --manifest manifest.json --annotator NAME [--port 8790] [--only agree.json]
      local labeling page on 127.0.0.1 (keys 1-4 / f m b n, s skip, x unsafe: drop, z undo), one labels file per annotator
  python -m lynxx.gender_eval agree labels-a.json labels-b.json [--arbiter labels-c.json] [--out agree.json] [--gold gold.json]
      Cohen's kappa of two annotators, disagreement list for an arbiter, gold labels (agreed + arbiter)
  python -m lynxx.gender_eval evaluate --manifest manifest.json (--gold gold.json | --silver) [--inputs sample,thumb]
                                        [--model-url URL] --out-dir DIR
      tier 0 / model / tier 0 + model against gold labels, or the model alone against strong tags (silver):
      report.json + report.md (precision per class, coverage, macro-F1, confusion, ECE, risk-coverage, slices, bias flags)
"""
#########################################
#
#

from __future__ import annotations

import argparse
import hashlib
import json
import os
import pathlib
import random
import secrets
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse as url_parse

from .gender import GenderConf, GenderLabel, GenderResult, effective_label, infer_from_tags, rules_for_module
from .gender_model import (
    API_DEFAULT_URL,
    ENV_MODEL_TOKEN,
    ENV_MODEL_URL,
    GENERAL_THRESHOLD,
    IMAGE_EXTS,
    IMAGE_MAX_BYTES,
    ModelUnavailable,
    ModelVerdict,
    backend,
    classify,
    combine,
)
from .safety import blocked_tag

__all__ = ('agree', 'build_manifest', 'cohen_kappa', 'evaluate', 'main', 'make_label_server', 'render_markdown')

SCHEMA = 1
CLASSES = ('only_female', 'only_male', 'both', 'none')
'''the four classes of §1.1 (unknown is the absence of a verdict, not a class)'''
LABEL_CHOICES = (*CLASSES, 'skip')
EXTRA_EXCLUDE_TAGS = frozenset(('petite', 'flat_chest', 'shortstack', 'school_uniform', 'age_difference', 'father_and_daughter',
                                'mother_and_son', 'aunt_and_nephew'))
'''§4.2: excluded from the dataset (not from results): no proof of a minor, but the model must not learn from that border area'''
REAL_TAGS = frozenset(('photo_(medium)', 'cosplay_photo', 'photo', 'real', 'photography_(artwork)', 'real_person'))
'''§4.3: photos / real people never enter the dataset (rulesets add their own 'real' flag tags)'''
SPARSE_GENERAL_MAX = 8
'''§3.2 "thiếu tag": at most this many general tags, or no gender tag at all'''
STYLE_TAGS: dict[str, frozenset[str]] = {
    '3d': frozenset(('3d', 'blender_(medium)', 'koikatsu_(medium)', 'source_filmmaker_(medium)', 'mikumikudance_(medium)', 'realistic',
                     'render', '3d_(artwork)')),
    'furry': frozenset(('furry', 'furry_female', 'furry_male', 'anthro', 'feral', 'kemono')),
    'western': frozenset(('western_comics_(style)', 'toon_(style)', 'cartoonized', 'western', 'cartoon', 'toon')),
    'ai': frozenset(('ai-generated', 'ai_generated', 'ai-assisted', 'ai_assisted')),
    'monochrome': frozenset(('monochrome', 'greyscale', 'sketch', 'lineart')),
}
'''§3.7 style slices (a post can be in several); '2d_anime' = none of 3d / furry / western'''
POV_TAGS = frozenset(('pov', 'male_pov', 'close-up', 'disembodied_penis', 'faceless_male', 'head_out_of_frame', 'out_of_frame'))
CROWD_TAGS = frozenset(('6+girls', '6+boys', '6+others', 'crowd', 'everyone'))
DARK_SKIN_TAGS = frozenset(('dark_skin', 'dark-skinned_female', 'dark-skinned_male'))
SLICE_DIMENSIONS = ('site', 'style', 'edge', 'density', 'media', 'tier0')
DEFAULT_QUOTAS = {'only_female': 60, 'only_male': 60, 'both': 60, 'none': 60, 'sparse': 80, 'weak': 20}
'''posts kept per stratum: tier-0 label (high), 'sparse' (thiếu tag), 'weak' (gender tags of medium / low confidence)'''
BIAS_MIN_N = 150
BIAS_MARGIN = 0.05
RELEASE_PRECISION_MIN = 0.85
ECE_BINS = 15
RISK_THRESHOLDS = (0.35, 0.5, 0.6, 0.7, 0.8, 0.85, 0.9, 0.95, 0.98)
ERRORS_MAX = 300
'''posts listed where a verdict differs from the reference (first ones a human should look at)'''
KAPPA_MIN = 0.8
GOLD_MIN_POSTS = 3000
GOLD_SPARSE_MIN = 0.3
PAGE_DELAY = 1.0
'''seconds between two search pages of the sampler (on top of the module's own request interval)'''
IMAGE_INTERVAL = 1.0
'''seconds between two pictures (on top of the module's own request interval): the danbooru CDN challenges fast clients'''
LABEL_PORT = 8790
POST_PAGE = {'db': 'https://danbooru.donmai.us/posts/{id}', 'rx': 'https://rule34.xxx/index.php?page=post&s=view&id={id}',
             'en': 'https://e621.net/posts/{id}'}
EXCLUSION_STEPS = (
    ('blacklist', '§4.1 danh sách chặn cứng (tag và alias)'),
    ('extra', '§4.2 danh sách loại bổ sung'),
    ('real', '§4.3 ảnh / người thật'),
    ('modelGate', '§4.2 safety gate của model (cần ảnh: áp ở label / evaluate)'),
    ('duplicate', 'trùng md5'),
    ('noFile', 'đã xoá / không có file'),
)


class EvalError(Exception):
    pass


def _log(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def _now() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%S')


def read_json(path: pathlib.Path | str) -> dict:
    return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))


def write_json(path: pathlib.Path | str, obj) -> None:
    """atomic: a crash never leaves a half written labels file or manifest"""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.tmp')
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    os.replace(tmp, path)


def sha256_file(path: pathlib.Path | str) -> str:
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()


def unblocked_tags(tags: Iterable[str]) -> list[str]:
    """tags of a dropped post that the blacklist does not block: candidates to review for lynxx/safety.py (§4.1)"""
    return sorted({t.strip().lower() for t in tags if t and t.strip() and not blocked_tag([t])})


def drop_from_manifest(path: pathlib.Path | str, drops: list[dict], by: str) -> int:
    """§3.2 / §4: a post found unsafe (by a human or the model gate) is not kept. Removes the posts from the manifest file
    and records them in its 'dropped' list: key, reason, model gate tag, never the post's urls or tags.
    drops: [{'key', 'reason', 'gateTag'?}]. Returns the number of posts removed"""
    keys = {d['key'] for d in drops}
    manifest = read_json(path)
    before = len(manifest.get('posts', []))
    manifest['posts'] = [p for p in manifest.get('posts', []) if p['key'] not in keys]
    known = {d.get('key') for d in manifest.setdefault('dropped', [])}
    at = _now()
    for d in drops:
        if d['key'] not in known:
            known.add(d['key'])
            manifest['dropped'].append({'key': d['key'], 'reason': d['reason'], **({'gateTag': d['gateTag']} if d.get('gateTag') else {}),
                                        'at': at, 'by': by})
    write_json(path, manifest)
    return before - len(manifest['posts'])


# ---------------- tags: aliases, exclusions, slices ----------------

_aliases_loaded = False


def _global_aliases() -> dict[str, str]:
    """all_tag_aliases.json (cross-site normalization map), loaded once; empty when the file is missing"""
    global _aliases_loaded
    from .tagsdb import TAG_ALIASES, load_tag_aliases
    if not _aliases_loaded:
        _aliases_loaded = True
        try:
            load_tag_aliases()
        except Exception:
            pass
    return TAG_ALIASES


def tags_with_aliases(tags: Iterable[str], module: str) -> list[str]:
    """the tags (lowercase) followed by their alias targets (ruleset aliases, all_tag_aliases.json)"""
    base = [t.strip().lower() for t in tags if t and t.strip()]
    maps = (rules_for_module(module).aliases, _global_aliases())
    extra = [a for t in base for m in maps if (a := m.get(t)) and a != t]
    return base + extra


def exclusion_hit(step: str, tags: list[str], module: str) -> str:
    """the tag that excludes a post at step 'blacklist' / 'extra' / 'real' (aliases included), '' if none"""
    full = tags_with_aliases(tags, module)
    if step == 'blacklist':
        return blocked_tag(full)
    if step == 'extra':
        return next((t for t in full if t in EXTRA_EXCLUDE_TAGS), '')
    if step == 'real':
        real = REAL_TAGS | rules_for_module(module).flags.get('real', frozenset())
        return next((t for t in full if t in real), '')
    raise ValueError(step)


def slices_for(module: str, tags: list[str], tier0: GenderResult, general: int | None, media: str) -> dict:
    """§3.7 slices of a post: site, style (several), edge cases (several), tag density, media kind"""
    t = {x.lower() for x in tags}
    style = [name for name, lst in STYLE_TAGS.items() if t & lst]
    if not {'3d', 'furry', 'western'} & set(style):
        style.insert(0, '2d_anime')
    flags = tier0.flags
    rules = rules_for_module(module)
    edge = []
    if flags & {'futa', 'futaMale'}:
        edge.append('futa')
    if flags & {'trap', 'trapFemale'}:
        edge.append('trap')
    if 'pov' in flags or t & POV_TAGS or t & rules.male_bodypart:
        edge.append('pov_bodypart')
    if 'crowd' in flags or t & CROWD_TAGS:
        edge.append('crowd6')
    if 'muscular_female' in t:
        edge.append('muscular_female')
    if t & DARK_SKIN_TAGS:
        edge.append('dark_skin')
    no_gender_tag = tier0.label == GenderLabel.UNKNOWN
    sparse = no_gender_tag or (general is not None and general <= SPARSE_GENERAL_MAX)
    return {'site': module, 'style': style, 'edge': edge, 'density': 'sparse' if sparse else 'full', 'media': media or 'image',
            'generalTags': general, 'noGenderTag': no_gender_tag}


def stratum_for(tier0: GenderResult, slices: dict) -> str:
    if slices['density'] == 'sparse':
        return 'sparse'
    if tier0.conf == GenderConf.HIGH and str(tier0.label) in CLASSES:
        return str(tier0.label)
    return 'weak'


def _general_count_from_lists(module: str, tags: list[str]) -> int | None:
    """general tags of a post from the module tag list (tags missing from the list count as general), None without a list"""
    try:
        from .tags import TagTypes
        from .tagsdb import TagsDB
        TagsDB.locate_default_lists()
        n = 0
        for t in tags:
            info = TagsDB.find_tag(module, t)
            n += info is None or info.tag_type == TagTypes.TAG_TYPE_GENERAL
        return n
    except Exception:
        return None


# ---------------- sample: metadata through the module's downloader ----------------

def _page_extras(raw: bytes | None) -> dict[str, dict]:
    """fields of JSON api pages the raw item strings drop: general tag count, deleted flag (danbooru, e621 shapes)"""
    try:
        data = json.loads(raw or b'')
    except Exception:
        return {}
    posts = data.get('posts') if isinstance(data, dict) else data
    if not isinstance(posts, list):
        return {}
    out: dict[str, dict] = {}
    for p in posts:
        if not isinstance(p, dict) or p.get('id') is None:
            continue
        gen = p.get('tag_count_general')
        if not isinstance(gen, int) and isinstance(p.get('tags'), dict) and isinstance(p['tags'].get('general'), list):
            gen = len(p['tags']['general'])
        flags = p.get('flags') if isinstance(p.get('flags'), dict) else {}
        out[str(p['id'])] = {'generalTags': gen if isinstance(gen, int) else None,
                             'deleted': bool(p.get('is_deleted') or p.get('is_banned') or flags.get('deleted'))}
    return out


def _attr(h: str, name: str) -> str:
    key = f' {name}="'
    i = h.find(key)
    return h[i + len(key):h.find('"', i + len(key))] if i >= 0 else ''


def _absolute(url: str, site: str) -> str:
    if url.startswith('//'):
        return f'https:{url}'
    if url.startswith('/'):
        return f'{site.rstrip("/")}{url}'
    return url if url.startswith(('https://', 'http://')) else ''


def _module_pid(module: str) -> int:
    from .extensions import Extensions
    from .module import ProcModule
    Extensions.ensure_loaded(include_catalog=True)
    if (pid := ProcModule.PROC_MODULES_BY_NAME.get(module)) is None:
        raise EvalError(f'module \'{module}\' is not installed')
    return pid


def _ready_downloader(dwn, module: str, tags: list[str]) -> None:
    from multiprocessing.dummy import current_process

    from .cmdargs import prepare_arglist
    me = current_process()
    me.killed = False
    dwn.reset_root_thread(me)
    dwn.no_conflict_delay = True
    dwn._parse_args(prepare_arglist(['-module', module, '--', *tags]), False)


def collect(module: str, query: str, *, pages: int = 1, page_delay: float = PAGE_DELAY) -> list[dict]:
    """posts (metadata only) of the first `pages` search pages of query, through the module's downloader"""
    from .download_db import TAG_NO_FILE_URL
    from .downloaders import _make_downloader
    out: list[dict] = []
    with _make_downloader(_module_pid(module)) as dwn:
        _ready_downloader(dwn, module, query.split())
        if not dwn._can_extract_item_info_without_fetch():
            raise EvalError(f'{module}: search pages carry no tags, the blacklist could not be checked (not supported)')
        if len(dwn.tags_str_arr) != 1:
            raise EvalError(f'{query!r}: one search per --query (no OR groups)')
        required = {t.lower() for t in dwn.required_tags}
        dwn.url = dwn._form_tags_search_address(dwn._consume_custom_module_tags(dwn.tags_str_arr[0]))
        site = dwn._get_sitename()
        delay = max(page_delay, float(dwn._get_page_fetch_delay() or 0))
        for page in range(max(1, pages)):
            if page:
                time.sleep(delay)
            raw = dwn.fetch_html(dwn._form_page_num_address(page))
            if raw is None:
                _log(f'  {query!r} page {page + 1}: no answer{" (bot challenge: stopping)" if dwn.cf_challenge_hit else ""}')
                break
            json_pages = bool(dwn._parses_raw_pages())
            items = dwn._get_all_post_tags(raw.raw if json_pages else raw.html)
            if not items:
                break
            extras = _page_extras(raw.raw) if json_pages else {}
            for h in map(str, items):
                info = dwn._extract_item_info(h)
                tags = [t for t in (info.tags or '').split(' ') if t]
                no_file = TAG_NO_FILE_URL in tags or '#unavailable' in _attr(h, 'file_url')
                tags = [t for t in tags if t != TAG_NO_FILE_URL]
                if required and not required <= {t.lower() for t in tags}:
                    continue
                post_id = dwn._extract_id(dwn._local_addr_from_string(h))
                ex = extras.get(post_id, {})
                out.append({
                    'site': module, 'id': post_id, 'md5': (getattr(info, 'md5', '') or '').lower(), 'tags': tags,
                    'rating': _attr(h, 'rating'), 'mediaKind': str(dwn._media_kind(h)),
                    'width': int(info.width) if str(info.width).isdigit() else 0,
                    'height': int(info.height) if str(info.height).isdigit() else 0,
                    'urls': {'preview': _absolute(dwn.extract_preview_url(h), site), 'sample': _absolute(_attr(h, 'sample_url'), site),
                             'file': '' if no_file else _absolute(_attr(h, 'file_url'), site)},
                    'generalTags': ex.get('generalTags'), 'deleted': bool(ex.get('deleted')) or no_file, 'query': query,
                })
            _log(f'  {query!r} page {page + 1}: {len(items)} posts')
    return out


def build_manifest(candidates: list[dict], *, quotas: dict[str, int] | None = None, seed: int = 0, queries: Iterable[str] = (),
                   log: Callable[[str], None] = _log) -> dict:
    """§3.2 exclusion order (count per step), tier-0 label + confidence, §3.7 slices, stratified selection. No pictures"""
    quotas = dict(DEFAULT_QUOTAS if quotas is None else quotas)
    pool: list[dict] = []
    seen: set[str] = set()
    repeats = 0
    for c in candidates:
        key = f'{c["site"]}:{c["id"]}'
        if key in seen:
            repeats += 1
            continue
        seen.add(key)
        pool.append({**c, 'key': key})
    log(f'{len(pool)} posts collected ({repeats} repeated across queries)')
    steps: list[dict] = []

    def run(name: str, hit_of: Callable[[dict], str | bool]) -> None:
        nonlocal pool
        kept, hits = [], Counter()
        for c in pool:
            if hit := hit_of(c):
                hits[hit if isinstance(hit, str) else 'yes'] += 1
            else:
                kept.append(c)
        removed = len(pool) - len(kept)
        step = {'step': len(steps) + 1, 'name': name, 'removed': removed, 'left': len(kept)}
        if name in ('blacklist', 'extra', 'real'):
            step['tags'] = dict(hits.most_common())
        steps.append(step)
        log(f'step {step["step"]} {name}: -{removed} ({len(kept)} left)')
        pool = kept

    run('blacklist', lambda c: exclusion_hit('blacklist', c['tags'], c['site']))
    run('extra', lambda c: exclusion_hit('extra', c['tags'], c['site']))
    run('real', lambda c: exclusion_hit('real', c['tags'], c['site']))
    steps.append({'step': 4, 'name': 'modelGate', 'removed': None, 'left': len(pool),
                  'note': 'needs the picture: applied by label (before a human sees it) and evaluate'})
    log('step 4 modelGate: applied later by label / evaluate (needs the picture)')
    md5_seen: set[str] = set()

    def duplicate(c: dict) -> bool:
        if not c['md5']:
            return False
        if c['md5'] in md5_seen:
            return True
        md5_seen.add(c['md5'])
        return False
    run('duplicate', duplicate)
    run('noFile', lambda c: bool(c.get('deleted')) or not (c['urls'].get('preview') or c['urls'].get('sample')))

    by_stratum: dict[str, list[dict]] = {}
    for c in pool:
        rules = rules_for_module(c['site'])
        res = infer_from_tags(c['tags'], rules)
        general = c.get('generalTags')
        if general is None:
            general = _general_count_from_lists(c['site'], c['tags'])
        sl = slices_for(c['site'], c['tags'], res, general, c.get('mediaKind', 'image'))
        post = {k: c[k] for k in ('key', 'site', 'id', 'md5', 'rating', 'mediaKind', 'width', 'height', 'urls', 'tags', 'query') if k in c}
        if page := POST_PAGE.get(c['site']):
            post['postUrl'] = page.format(id=c['id'])
        post['tier0'] = res.as_dict()
        post['slices'] = sl
        post['stratum'] = stratum_for(res, sl)
        by_stratum.setdefault(post['stratum'], []).append(post)

    rng = random.Random(seed)
    strata: dict[str, dict] = {}
    selected: list[dict] = []
    for name in sorted(set(by_stratum) | set(quotas)):
        group = sorted(by_stratum.get(name, []), key=lambda p: p['key'])
        rng.shuffle(group)
        quota = quotas.get(name, 0)
        take = group if quota < 0 else group[:quota]
        strata[name] = {'available': len(group), 'quota': quota, 'selected': len(take)}
        selected += take
        log(f'stratum {name}: {len(take)} / {len(group)} available (quota {"all" if quota < 0 else quota})')
    selected.sort(key=lambda p: (p['stratum'], p['site'], int(p['id']) if p['id'].isdigit() else 0, p['key']))
    sparse = sum(1 for p in selected if p['slices']['density'] == 'sparse')
    return {
        'schema': SCHEMA, 'kind': 'lynxx-gender-eval-manifest', 'created': _now(), 'queries': list(queries), 'seed': seed,
        'note': 'metadata only: no pictures are stored. Tags, urls and labels of posts',
        'exclusions': steps, 'repeatedAcrossQueries': repeats, 'strata': strata,
        'sparseShare': round(sparse / len(selected), 4) if selected else 0.0, 'dropped': [], 'posts': selected,
    }


def parse_quotas(text: str) -> dict[str, int]:
    """'only_female=60,sparse=80,weak=all' -> quotas ('all' keeps every post of the stratum)"""
    out = dict(DEFAULT_QUOTAS)
    for part in filter(None, (x.strip() for x in str(text or '').split(','))):
        name, _, value = part.partition('=')
        if name not in (*CLASSES, 'sparse', 'weak'):
            raise EvalError(f'unknown stratum {name!r} (only_female, only_male, both, none, sparse, weak)')
        out[name] = -1 if value.strip() == 'all' else int(value)
    return out


# ---------------- pictures: in memory, through the downloader session ----------------

def _site_domain(host: str) -> str:
    return '.'.join(host.lower().rstrip('.').split('.')[-2:])


def same_site(url: str, site: str) -> bool:
    """a picture url on the site's own domain (danbooru.donmai.us -> cdn.donmai.us): a manifest cannot send requests elsewhere"""
    u, s = url_parse.urlsplit(url), url_parse.urlsplit(site)
    return u.scheme in ('http', 'https') and bool(u.hostname) and bool(s.hostname) and _site_domain(u.hostname) == _site_domain(s.hostname)


def picture_url(post: dict, kind: str) -> str:
    """what the model looks at: 'sample' = the sample when it is an image (lynxx/gender_model.py image_url_for), else the
    thumbnail; 'thumb' = the thumbnail (150-180 px)"""
    urls = post.get('urls') or {}
    sample = str(urls.get('sample') or '')
    if kind == 'sample' and sample and url_parse.urlparse(sample).path.lower().endswith(IMAGE_EXTS):
        return sample
    return str(urls.get('preview') or '')


class ImageFetcher:
    """Pictures through the module's downloader session (its headers, proxy and request interval), one at a time on one
    worker thread (one keep-alive session), at most one per `interval` seconds. Bytes are returned, never written"""

    def __init__(self, interval: float = IMAGE_INTERVAL, fetch: Callable[[str, str], bytes] | None = None) -> None:
        self.interval = max(0.0, interval)
        self._fetch = fetch
        self._dwns: dict[str, object] = {}
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='eval-img')
        self._next = 0.0
        self.challenged = False
        self.requests = 0

    def _downloader(self, module: str):
        if (dwn := self._dwns.get(module)) is None:
            from .downloaders import _make_downloader
            dwn = _make_downloader(_module_pid(module))
            _ready_downloader(dwn, module, ['id:>0'])
            self._dwns[module] = dwn
        return dwn

    def _get(self, module: str, url: str) -> bytes:
        if self.challenged:
            return b''
        if (wait := self._next - time.monotonic()) > 0:
            time.sleep(wait)
        try:
            if self._fetch is not None:
                return self._fetch(module, url)
            dwn = self._downloader(module)
            if not same_site(url, dwn._get_sitename()):
                return b''
            r = dwn.wrap_request(url, 2, 'GET')
            self.challenged = bool(dwn.cf_challenge_hit)
            if r is None or not str(r.headers.get('content-type', '')).lower().startswith('image/'):
                return b''
            data = r.content
            return data if len(data) <= IMAGE_MAX_BYTES else b''
        finally:
            self.requests += 1
            self._next = time.monotonic() + self.interval

    def get(self, module: str, url: str) -> bytes:
        if not url:
            return b''
        return self._pool.submit(self._get, module, url).result()

    def close(self) -> None:
        self._pool.shutdown(wait=True)
        for dwn in self._dwns.values():
            try:
                dwn.__exit__(None, None, None)
            except Exception:
                pass
        self._dwns.clear()


# ---------------- model verdicts ----------------

def model_verdict(probs: dict[str, float], *, video: bool = False) -> tuple[ModelVerdict, float | None]:
    """classify() verdict and the probability it rests on: the weakest predicted tag among its evidence (§3.4 confidence)"""
    v = classify(probs, video=video)
    if v.result.label == GenderLabel.UNKNOWN or v.result.conf is None:
        return v, None
    res = infer_from_tags([t for t, p in probs.items() if p >= GENERAL_THRESHOLD], rules_for_module('db'))
    return v, min((probs.get(t, 0.0) for t in res.evidence), default=None)


class PredictionCache:
    """predicted tags per (post, picture url, model): a rerun after a stop (bot challenge, Ctrl-C) does not fetch again.
    JSON lines of probabilities, no pictures"""

    def __init__(self, path: pathlib.Path | None) -> None:
        self.path = path
        self._rows: dict[tuple[str, str, str], dict[str, float]] = {}
        if path is not None and path.is_file():
            for line in path.read_text(encoding='utf-8').splitlines():
                try:
                    r = json.loads(line)
                    self._rows[(r['key'], r['url'], r['model'])] = {str(k): float(v) for k, v in r['probs'].items()}
                except Exception:
                    continue

    def get(self, key: str, url: str, model: str) -> dict[str, float] | None:
        return self._rows.get((key, url, model))

    def put(self, key: str, url: str, model: str, probs: dict[str, float]) -> None:
        self._rows[(key, url, model)] = probs
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open('a', encoding='utf-8') as f:
                f.write(json.dumps({'key': key, 'url': url, 'model': model, 'probs': {k: round(v, 4) for k, v in probs.items()}}) + '\n')


# ---------------- agreement ----------------

def cohen_kappa(pairs: list[tuple[str, str]]) -> dict:
    """Cohen's kappa of two annotators over the same posts: (p_o - p_e) / (1 - p_e)"""
    n = len(pairs)
    if not n:
        return {'n': 0, 'kappa': None, 'observed': None, 'expected': None}
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[c] * cb[c] for c in set(ca) | set(cb)) / (n * n)
    kappa = 1.0 if pe >= 1 else (po - pe) / (1 - pe)
    return {'n': n, 'kappa': round(kappa, 4), 'observed': round(po, 4), 'expected': round(pe, 4)}


def _labels_of(doc: dict) -> dict[str, str]:
    return {k: str(v.get('label') if isinstance(v, dict) else v) for k, v in (doc.get('labels') or {}).items()}


def agree(a: dict, b: dict, *, arbiter: dict | None = None, manifest: dict | None = None) -> tuple[dict, dict]:
    """(agreement report, gold labels). Posts dropped as unsafe by anyone are out; 'skip' is not a label"""
    dropped: dict[str, dict] = {}
    for who, doc in (('a', a), ('b', b), ('arbiter', arbiter or {})):
        for key, d in (doc.get('dropped') or {}).items():
            dropped.setdefault(key, {'by': [], 'reason': d.get('reason', ''), 'tags': d.get('tags', []),
                                     'unblockedTags': d.get('unblockedTags', [])})['by'].append(who)
    if manifest is not None:
        for d in manifest.get('dropped') or []:
            dropped.setdefault(d['key'], {'by': ['manifest'], 'reason': d.get('reason', ''), 'tags': [], 'unblockedTags': []})
    la, lb, lc = _labels_of(a), _labels_of(b), _labels_of(arbiter or {})
    keys = sorted((set(la) | set(lb)) - set(dropped))
    both = [k for k in keys if la.get(k) in CLASSES and lb.get(k) in CLASSES]
    pairs = [(la[k], lb[k]) for k in both]
    kappa = cohen_kappa(pairs)
    confusion = {x: {y: sum(1 for p in pairs if p == (x, y)) for y in CLASSES} for x in CLASSES}
    disagreements = [{'key': k, 'a': la[k], 'b': lb[k], **({'arbiter': lc[k]} if lc.get(k) in CLASSES else {})}
                     for k in both if la[k] != lb[k]]
    gold = {k: la[k] for k in both if la[k] == lb[k]}
    resolved = 0
    for d in disagreements:
        if 'arbiter' in d:
            gold[d['key']] = d['arbiter']
            resolved += 1
    unsafe_tags = Counter(t for d in dropped.values() for t in d.get('unblockedTags', []))
    report = {
        'schema': SCHEMA, 'kind': 'lynxx-gender-eval-agreement', 'generated': _now(),
        'annotators': [a.get('annotator', 'a'), b.get('annotator', 'b')], 'arbiter': (arbiter or {}).get('annotator', ''),
        **kappa, 'meetsKappa': kappa['kappa'] is not None and kappa['kappa'] >= KAPPA_MIN, 'kappaMin': KAPPA_MIN,
        'confusion': confusion,
        'skipped': {'a': sum(1 for k in keys if la.get(k) == 'skip'), 'b': sum(1 for k in keys if lb.get(k) == 'skip')},
        'labeledByOneOnly': sum(1 for k in keys if (la.get(k) in CLASSES) != (lb.get(k) in CLASSES)),
        'disagreements': disagreements, 'resolvedByArbiter': resolved, 'unresolved': len(disagreements) - resolved,
        'dropped': [{'key': k, **v} for k, v in sorted(dropped.items())],
        'unsafeTagsNotBlocked': dict(unsafe_tags.most_common()),
        'gold': {'posts': len(gold)},
    }
    if manifest is not None:
        posts = {p['key']: p for p in manifest.get('posts', [])}
        sparse = sum(1 for k in gold if posts.get(k, {}).get('slices', {}).get('density') == 'sparse')
        report['gold'].update({'sparse': sparse, 'sparseShare': round(sparse / len(gold), 4) if gold else 0.0,
                               'meetsSize': len(gold) >= GOLD_MIN_POSTS, 'meetsSparseShare': bool(gold) and sparse / len(gold) >= GOLD_SPARSE_MIN})
    gold_doc = {'schema': SCHEMA, 'kind': 'lynxx-gender-eval-gold', 'generated': _now(), 'annotators': report['annotators'],
                'arbiter': report['arbiter'], 'kappa': kappa['kappa'], 'labels': dict(sorted(gold.items())),
                'dropped': {d['key']: {'reason': d['reason']} for d in report['dropped']}}
    return report, gold_doc


# ---------------- evaluate ----------------

def _f1(p: float | None, r: float | None) -> float:
    return 2 * p * r / (p + r) if p and r else 0.0


def _macro_f1(rows: list[dict], field: str) -> float | None:
    """mean F1 of the four classes; a missing verdict ('unknown') is a miss of the true class. Classes absent from both
    the reference and the predictions are left out"""
    scores = []
    for c in CLASSES:
        tp = sum(1 for r in rows if r['ref'] == c and r[field] == c)
        pred = sum(1 for r in rows if r[field] == c)
        ref = sum(1 for r in rows if r['ref'] == c)
        if not pred and not ref:
            continue
        scores.append(_f1(tp / pred if pred else None, tp / ref if ref else None))
    return round(sum(scores) / len(scores), 4) if scores else None


def expected_calibration_error(pairs: list[tuple[float, bool]], bins: int = ECE_BINS) -> float | None:
    """ECE over (probability, correct) pairs, equal-width bins"""
    if not pairs:
        return None
    total = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        sel = [(p, ok) for p, ok in pairs if (lo < p <= hi) or (b == 0 and p <= hi)]
        if sel:
            total += abs(sum(ok for _, ok in sel) / len(sel) - sum(p for p, _ in sel) / len(sel)) * len(sel) / len(pairs)
    return round(total, 4)


def _ratio(a: int, b: int) -> float | None:
    return round(a / b, 4) if b else None


def metrics(rows: list[dict], *, full: bool = True) -> dict:
    """rows: {'ref': class, 'pred': label at the operating point, 'any': label at any confidence, 'score': probability the
    verdict rests on or None, 'conf': high / medium / low / None}"""
    n = len(rows)
    out: dict = {'n': n}
    if not n:
        return out
    decided = [r for r in rows if r['pred'] != 'unknown']
    tp = Counter(r['ref'] for r in decided if r['pred'] == r['ref'])
    pred = Counter(r['pred'] for r in decided)
    ref = Counter(r['ref'] for r in rows)
    out['coverage'] = _ratio(len(decided), n)
    out['microPrecision'] = _ratio(sum(tp.values()), len(decided))
    out['precision'] = {c: {'value': _ratio(tp[c], pred[c]), 'tp': tp[c], 'predicted': pred[c]} for c in CLASSES}
    out['recall'] = {c: _ratio(tp[c], ref[c]) for c in CLASSES}
    out['reference'] = {c: ref[c] for c in CLASSES}
    out['macroF1'] = _macro_f1(rows, 'pred')
    answered = [r for r in rows if r['any'] != 'unknown']
    out['coverageAnswered'] = _ratio(len(answered), n)
    out['macroF1Answered'] = _macro_f1(answered, 'any')
    if not full:
        return out
    out['confusion'] = {c: {col: sum(1 for r in rows if r['ref'] == c and r['pred'] == col) for col in (*CLASSES, 'unknown')}
                        for c in CLASSES}
    scored = [r for r in answered if r['score'] is not None]
    out['ece'] = expected_calibration_error([(r['score'], r['any'] == r['ref']) for r in scored])
    out['eceN'] = len(scored)
    out['riskCoverage'] = []
    for t in RISK_THRESHOLDS if scored else ():
        sel = [r for r in scored if r['score'] >= t]
        ok = sum(r['any'] == r['ref'] for r in sel)
        out['riskCoverage'].append({'threshold': t, 'n': len(sel), 'coverage': _ratio(len(sel), n),
                                    'risk': round(1 - ok / len(sel), 4) if sel else None})
    out['byConfidence'] = {}
    for level in ('high', 'medium', 'low'):
        floor = GenderConf.parse(level)
        sel = [r for r in answered if r['conf'] and GenderConf.parse(r['conf']) >= floor]
        ok = sum(r['any'] == r['ref'] for r in sel)
        out['byConfidence'][level] = {'n': len(sel), 'coverage': _ratio(len(sel), n), 'precision': _ratio(ok, len(sel))}
    return out


def _slice_keys(post: dict, mode: str, tier0: GenderResult) -> list[tuple[str, str]]:
    sl = post.get('slices') or {}
    keys = [('site', str(sl.get('site') or post.get('site', '')))]
    keys += [('style', s) for s in sl.get('style', [])]
    keys += [('edge', e) for e in sl.get('edge', [])]
    keys += [('density', str(sl.get('density', ''))), ('media', str(sl.get('media', 'image')))]
    if mode == 'gold':
        keys.append(('tier0', f'{tier0.label}/{tier0.conf.name.lower() if tier0.conf else "-"}'))
    return keys


def _bias(global_m: dict, slices: list[dict]) -> tuple[list[dict], list[str]]:
    """§3.7 rule: slice with n >= 150 and precision more than 5 points under the global one"""
    flags, not_judged = [], []
    for s in slices:
        m = s['metrics']
        name = f'{s["dimension"]}={s["value"]}'
        if m['n'] < BIAS_MIN_N:
            not_judged.append(name)
            continue
        checks = [('micro', m.get('microPrecision'), global_m.get('microPrecision'))]
        checks += [(c, m['precision'][c]['value'], global_m['precision'][c]['value']) for c in ('only_female', 'only_male', 'both')]
        for metric, sv, gv in checks:
            if sv is not None and gv is not None and sv < gv - BIAS_MARGIN:
                action = ('nâng ngưỡng riêng của site' if s['dimension'] == 'site' else
                          f'ghi giới hạn đã biết{"; chặn phát hành (precision < 0,85)" if sv < RELEASE_PRECISION_MIN else ""}'
                          if s['dimension'] == 'style' else 'xem lại')
                flags.append({'slice': name, 'metric': metric, 'slicePrecision': sv, 'globalPrecision': gv, 'n': m['n'],
                              'action': action})
    return flags, not_judged


def _analyze(rows: list[dict], mode: str) -> dict:
    g = metrics(rows)
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        for k in r['slices']:
            groups.setdefault(k, []).append(r)
    order = {d: i for i, d in enumerate(SLICE_DIMENSIONS)}
    slices = [{'dimension': d, 'value': v, 'metrics': metrics(rs, full=False)}
              for (d, v), rs in sorted(groups.items(), key=lambda x: (order.get(x[0][0], 99), -len(x[1]), x[0][1]))]
    flags, not_judged = _bias(g, slices)
    trap = [r for r in rows if ('edge', 'trap') in r['slices']]
    wrong = [{'key': r['key'], 'ref': r['ref'], 'pred': r['pred'], 'score': None if r['score'] is None else round(r['score'], 3)}
             for r in rows if r['pred'] not in ('unknown', r['ref'])]
    return {'global': g, 'slices': slices, 'biasFlags': flags, 'notJudged': not_judged,
            'trapAsOnlyFemale': {'n': len(trap), 'onlyFemale': sorted(r['key'] for r in trap if r['pred'] == 'only_female')},
            'disagreements': sorted(wrong, key=lambda x: (x['ref'], x['pred'], x['key']))[:ERRORS_MAX]}


def _row(key: str, ref: str, res: GenderResult, score: float | None, min_conf: GenderConf, slices: list) -> dict:
    return {'key': key, 'ref': ref, 'pred': str(effective_label(res, min_conf, False)), 'any': str(res.label),
            'score': score, 'conf': res.conf.name.lower() if res.conf else None, 'slices': slices}


def _latency(ms: list[float]) -> dict:
    if not ms:
        return {'n': 0}
    s = sorted(ms)
    return {'n': len(s), 'meanMs': round(sum(s) / len(s), 1), 'p50Ms': round(s[len(s) // 2], 1),
            'p95Ms': round(s[min(len(s) - 1, int(len(s) * 0.95))], 1)}


def evaluate(manifest: dict, *, model, fetch: Callable[[dict, str], bytes], gold: dict | None = None,
             inputs: Iterable[str] = ('sample', 'thumb'), min_conf: GenderConf = GenderConf.MEDIUM,
             cache: PredictionCache | None = None, should_stop: Callable[[], str] = lambda: '',
             log: Callable[[str], None] = _log) -> dict:
    """Gold mode: tier 0, the model (picture only) and tier 0 + model (combine) against human labels.
    Silver mode (gold=None): reference = tier-0 label of high confidence, the model sees the picture only (tags hidden):
    how often the model agrees with strong tags. Posts the model safety gate trips on are out of every number"""
    inputs = tuple(inputs)
    mode = 'gold' if gold is not None else 'silver'
    model_id = str(getattr(model, 'model_id', 'model'))
    dropped = {d['key'] for d in manifest.get('dropped') or []} | set((gold or {}).get('dropped') or {})
    gold_labels = {k: v for k, v in _labels_of(gold).items() if v in CLASSES} if gold is not None else {}
    excluded: Counter = Counter()
    gate_tags: Counter = Counter()
    gate_dropped: list[dict] = []
    no_picture: Counter = Counter()
    ms: dict[str, list[float]] = {k: [] for k in inputs}
    probs_of: dict[tuple[str, str], dict[str, float]] = {}
    posts: list[tuple[dict, GenderResult, str | None]] = []
    for post in manifest.get('posts', []):
        if post['key'] in dropped:
            excluded['dropped'] += 1
            continue
        if blocked_tag(tags_with_aliases(post['tags'], post['site'])):  # defensive: a manifest edited by hand
            excluded['blacklist'] += 1
            continue
        tier0 = infer_from_tags(post['tags'], rules_for_module(post['site']))
        if mode == 'gold':
            ref = gold_labels.get(post['key'])
            if ref is None:
                excluded['noGoldLabel'] += 1
                continue
        else:
            ref = str(tier0.label) if tier0.conf == GenderConf.HIGH and str(tier0.label) in CLASSES else None
        posts.append((post, tier0, ref))
    stop_reason = ''
    processed: list[tuple[dict, GenderResult, str | None]] = []
    for i, (post, tier0, ref) in enumerate(posts):
        if stop_reason := should_stop():
            log(f'stopped after {i} / {len(posts)} posts: {stop_reason}')
            break
        url_probs: dict[str, dict[str, float]] = {}  # video: sample and thumbnail are the same poster, one request
        for kind in inputs:
            url = picture_url(post, kind)
            probs: dict[str, float] | None = None
            if url in url_probs:
                probs = url_probs[url]
            elif url and cache is not None and (hit := cache.get(post['key'], url, model_id)) is not None:
                probs = hit
            elif url and (data := fetch(post, kind)):
                t0 = time.monotonic()
                probs = model.predict(data) or {}  # {}: the model server refused this picture (not an image, too large)
                ms[kind].append((time.monotonic() - t0) * 1000)
                del data
                if cache is not None:
                    cache.put(post['key'], url, model_id, probs)
            if probs is not None:
                url_probs[url] = probs
            if not probs:  # no url, fetch failed (not cached: tried again on the next run), or refused
                no_picture[kind] += 1
                continue
            probs_of[(post['key'], kind)] = probs
        processed.append((post, tier0, ref))
        if (i + 1) % 25 == 0:
            log(f'{i + 1} / {len(posts)} posts')
    systems: dict[str, dict[str, list[dict]]] = {'model': {k: [] for k in inputs}}
    if mode == 'gold':
        systems['tier0'] = {'tags': []}
        systems['tier0+model'] = {k: [] for k in inputs}
    no_ref: dict[str, dict[str, Counter]] = {k: {} for k in inputs}
    evaluated = 0
    for post, tier0, ref in processed:
        key = post['key']
        verdicts = {k: model_verdict(probs_of[(key, k)], video=post.get('mediaKind') == 'video')
                    for k in inputs if (key, k) in probs_of}
        if gate := next((v.safety_tag for v, _ in verdicts.values() if v.safety_tag), ''):
            excluded['modelGate'] += 1
            gate_tags[gate] += 1
            gate_dropped.append({'key': key, 'gateTag': gate, 'unblockedTags': unblocked_tags(post['tags'])})
            continue
        evaluated += 1
        sl = _slice_keys(post, mode, tier0)
        if ref is None:  # silver: no strong tag label, only what the model says
            for k, (v, _) in verdicts.items():
                d = no_ref[k].setdefault(str(post['slices'].get('density', '')), Counter())
                d['n'] += 1
                d[str(effective_label(v.result, min_conf, False))] += 1
            continue
        for k in inputs:
            v, score = verdicts.get(k, (ModelVerdict(GenderResult(GenderLabel.UNKNOWN, None, source='model')), None))
            systems['model'][k].append(_row(key, ref, v.result, score, min_conf, sl))
            if mode == 'gold':
                systems['tier0+model'][k].append(_row(key, ref, combine(tier0, v.result, min_conf), score, min_conf, sl))
        if mode == 'gold':
            systems['tier0']['tags'].append(_row(key, ref, tier0, None, min_conf, sl))
    agreement = {}
    if len(inputs) == 2:
        a, b = inputs
        pairs = [(r1['pred'], r2['pred']) for r1, r2 in zip(systems['model'][a], systems['model'][b], strict=True)]
        agreement = {'n': len(pairs), 'same': sum(x == y for x, y in pairs), 'sameShare': _ratio(sum(x == y for x, y in pairs), len(pairs))}
    report = {
        'schema': SCHEMA, 'kind': 'lynxx-gender-eval-report', 'generated': _now(), 'mode': mode,
        'minConf': min_conf.name.lower(), 'inputs': list(inputs), 'model': {'id': model_id, **dict(getattr(model, 'info', {}) or {})},
        'gold': ({'annotators': gold.get('annotators', [gold.get('annotator', '')]), 'arbiter': gold.get('arbiter', ''),
                  'kappa': gold.get('kappa'), 'kind': gold.get('kind', '')} if gold is not None else None),
        'manifest': {'created': manifest.get('created'), 'queries': manifest.get('queries', []), 'posts': len(manifest.get('posts', [])),
                     'exclusions': manifest.get('exclusions', []), 'strata': manifest.get('strata', {}),
                     'sparseShare': manifest.get('sparseShare')},
        'posts': {'candidates': len(posts), 'processed': len(processed), 'evaluated': evaluated,
                  'withReference': len(systems['model'][inputs[0]]) if inputs else 0, 'excluded': dict(excluded),
                  'modelGateTags': dict(gate_tags), 'modelGateDropped': gate_dropped, 'noPicture': dict(no_picture),
                  'stopReason': stop_reason,
                  'incomplete': bool(stop_reason)},
        'systems': {name: {k: _analyze(rows, mode) for k, rows in per.items()} for name, per in systems.items()},
        'inputAgreement': agreement,
        'noReference': {k: {d: dict(c) for d, c in v.items()} for k, v in no_ref.items()},
        'latency': {k: _latency(v) for k, v in ms.items()},
    }
    return report


# ---------------- report.md ----------------

def _num(x, digits: int = 3) -> str:
    return '–' if x is None else f'{x:.{digits}f}'.replace('.', ',')


def _pct(x) -> str:
    return '–' if x is None else f'{x * 100:.1f} %'.replace('.', ',')


SYSTEM_NAMES = {'model': 'Model (chỉ ảnh)', 'tier0': 'Tầng 0 (tag)', 'tier0+model': 'Tầng 0 + model (combine)'}
INPUT_NAMES = {'sample': 'ảnh sample (~850 px)', 'thumb': 'thumbnail (150–180 px)', 'tags': 'tag'}


def render_markdown(report: dict) -> str:
    """report.md: the numbers of report.json in tables (Vietnamese, decimal comma)"""
    mode = report['mode']
    L: list[str] = []
    L.append(f'# Đánh giá model giới tính ({"silver" if mode == "silver" else "tập vàng"}) — {report["generated"][:10]}')
    L.append('')
    if mode == 'silver':
        L.append('> **Silver, không phải tập vàng.** Tham chiếu là nhãn tầng 0 ở mức `high` (tag mạnh), model chỉ nhận ảnh '
                 '(tag bị ẩn). Số đo cho biết model đồng ý với tag mạnh tới đâu, **không** phải precision thật: tag sai thì '
                 'model "sai" theo, và post thiếu tag (miền model nhắm tới) không có tham chiếu. Chưa có nhãn của người.')
    else:
        g = report.get('gold') or {}
        L.append(f'> Tập vàng: người gán {", ".join(x for x in g.get("annotators", []) if x) or "?"}'
                 f'{", trọng tài " + g["arbiter"] if g.get("arbiter") else ""}, κ = {_num(g.get("kappa"))}'
                 f'{" (một người gán: chưa phải tập vàng)" if g.get("kind") == "lynxx-gender-eval-labels" else ""}.')
    L.append('')
    m = report['model']
    device = f', thiết bị `{m["device"]}`' if m.get('device') else ''
    server = f', server `{m["url"]}`' if m.get('url') else ''
    L.append(f'- Model: `{m.get("id")}`{device}{server}; điểm vận hành: độ tin cậy ≥ `{report["minConf"]}`.')
    man = report['manifest']
    L.append(f'- Manifest: {man["posts"]} post, tạo lúc {man.get("created")}; truy vấn: {", ".join(f"`{q}`" for q in man.get("queries", []))}.')
    p = report['posts']
    L.append(f'- Post: {p["candidates"]} ứng viên, {p["processed"]} đã xử lý, {p["evaluated"]} được đánh giá, '
             f'{p["withReference"]} có tham chiếu. Loại khi đánh giá: {json.dumps(p["excluded"], ensure_ascii=False) or "{}"}; '
             f'không lấy được ảnh: {json.dumps(p["noPicture"])}.')
    if p.get('modelGateTags'):
        L.append(f'- Safety gate của model loại {sum(p["modelGateTags"].values())} post (tag: '
                 f'{", ".join(f"`{t}` {n}" for t, n in p["modelGateTags"].items())}); các post này không có trong mọi số đo.')
        if man.get('removedFromManifest'):
            keys = ', '.join(f'{d["key"]} (`{d["gateTag"]}`)' for d in p.get('modelGateDropped', []))
            L.append(f'- {man["removedFromManifest"]} post đó đã bị xoá khỏi manifest (ghi trong `dropped` của manifest, chỉ kèm tag '
                     f'của gate, không có URL hay tag của post): {keys}.')
    if p.get('incomplete'):
        L.append(f'- **Chưa chạy hết**: {p["stopReason"]}.')
    L.append('')
    L.append('## Loại trừ khi lấy mẫu (§3.2)')
    L.append('')
    L.append('| Bước | Lý do | Bị loại | Còn lại | Tag (số post) |')
    L.append('|---|---|---|---|---|')
    names = dict(EXCLUSION_STEPS)
    for s in man.get('exclusions', []):
        tags = ', '.join(f'`{t}` {n}' for t, n in list((s.get('tags') or {}).items())[:8])
        removed = s['removed'] if s.get('removed') is not None else (
            f'{p["excluded"].get("modelGate", 0)} (lúc đánh giá)' if s['name'] == 'modelGate' else '–')
        L.append(f'| {s["step"]} | {names.get(s["name"], s["name"])} | {removed} | {s["left"]} | {tags} |')
    L.append('')
    if man.get('strata'):
        L.append('Phân tầng: ' + '; '.join(f'{k} {v["selected"]}/{v["available"]}' for k, v in man['strata'].items())
                 + f'. Tỉ lệ "thiếu tag" trong manifest: {_pct(man.get("sparseShare"))}.')
        L.append('')
    for sname, per in report['systems'].items():
        for inp, a in per.items():
            g = a['global']
            L.append(f'## {SYSTEM_NAMES.get(sname, sname)} — {INPUT_NAMES.get(inp, inp)}')
            L.append('')
            if not g.get('n'):
                L.append('Không có post nào có tham chiếu.')
                L.append('')
                continue
            L.append('| n | Độ phủ | Precision (gộp) | only_female | only_male | both | none | Macro-F1 | Macro-F1 (khi trả lời) | ECE |')
            L.append('|---|---|---|---|---|---|---|---|---|---|')
            pc = ' | '.join(f'{_num(g["precision"][c]["value"])} ({g["precision"][c]["tp"]}/{g["precision"][c]["predicted"]})' for c in CLASSES)
            L.append(f'| {g["n"]} | {_pct(g["coverage"])} | {_num(g["microPrecision"])} | {pc} | {_num(g["macroF1"])} | '
                     f'{_num(g["macroF1Answered"])} (độ phủ {_pct(g["coverageAnswered"])}) | {_num(g.get("ece"))} (n {g.get("eceN", 0)}) |')
            L.append('')
            L.append('Ma trận nhầm lẫn (hàng: tham chiếu, cột: dự đoán ở điểm vận hành):')
            L.append('')
            L.append('| | only_female | only_male | both | none | unknown |')
            L.append('|---|---|---|---|---|---|')
            for c in CLASSES:
                L.append(f'| **{c}** ({g["reference"][c]}) | ' + ' | '.join(str(g['confusion'][c][col]) for col in (*CLASSES, 'unknown')) + ' |')
            L.append('')
            if g.get('riskCoverage'):
                L.append('Risk–coverage (ngưỡng trên xác suất mà nhãn dựa vào): '
                         + '; '.join(f'≥ {_num(x["threshold"], 2)}: phủ {_pct(x["coverage"])}, rủi ro {_pct(x["risk"])}' for x in g['riskCoverage']) + '.')
                L.append('')
            if g.get('byConfidence'):
                L.append('Theo độ tin cậy: ' + '; '.join(f'≥ {lv}: phủ {_pct(x["coverage"])}, precision {_num(x["precision"])}'
                                                        for lv, x in g['byConfidence'].items()) + '.')
                L.append('')
            L.append('| Slice | n | Độ phủ | Precision | only_female | only_male | both | none |')
            L.append('|---|---|---|---|---|---|---|---|')
            for s in a['slices']:
                sm = s['metrics']
                cells = ' | '.join(f'{_num(sm["precision"][c]["value"])} ({sm["precision"][c]["predicted"]})' for c in CLASSES)
                L.append(f'| {s["dimension"]}={s["value"]} | {sm["n"]} | {_pct(sm.get("coverage"))} | {_num(sm.get("microPrecision"))} | {cells} |')
            L.append('')
            if a['biasFlags']:
                L.append('**Cờ thiên lệch** (n ≥ 150, precision thấp hơn toàn cục > 5 điểm): ' + '; '.join(
                    f'{f["slice"]} {f["metric"]} {_num(f["slicePrecision"])} vs {_num(f["globalPrecision"])} → {f["action"]}' for f in a['biasFlags']) + '.')
            else:
                L.append('Cờ thiên lệch: không có.')
            if a['notJudged']:
                L.append(f'Slice có n < {BIAS_MIN_N} (chưa áp luật thiên lệch): {", ".join(a["notJudged"])}.')
            t = a['trapAsOnlyFemale']
            keys = f' ({", ".join(t["onlyFemale"])})' if t['onlyFemale'] else ''
            L.append(f'Slice trap: {t["n"]} post, {len(t["onlyFemale"])} bị gán only_female{keys}.')
            if a.get('disagreements'):
                L.append(f'Post có nhãn khác tham chiếu ({len(a["disagreements"])}, tham chiếu → dự đoán): '
                         + ', '.join(f'{d["key"]} ({d["ref"]} → {d["pred"]})' for d in a['disagreements']) + '.')
            L.append('')
    if report.get('inputAgreement'):
        ia = report['inputAgreement']
        L.append(f'## Thumbnail và sample\n\nModel cho cùng nhãn (ở điểm vận hành) với hai đầu vào ở {ia["same"]}/{ia["n"]} post ({_pct(ia["sameShare"])}).')
        L.append('')
    if any(report.get('noReference', {}).values()):
        L.append('## Post không có tham chiếu (silver)')
        L.append('')
        L.append('Tag không đủ mạnh (không có nhãn tầng 0 ở mức high): không đo được độ đúng, chỉ biết model gán nhãn bao nhiêu post.')
        L.append('')
        L.append('| Đầu vào | Mật độ tag | n | only_female | only_male | both | none | unknown |')
        L.append('|---|---|---|---|---|---|---|---|')
        for inp, by in report['noReference'].items():
            for dens, c in by.items():
                L.append(f'| {INPUT_NAMES.get(inp, inp)} | {dens} | {c.get("n", 0)} | ' + ' | '.join(str(c.get(x, 0)) for x in (*CLASSES, 'unknown')) + ' |')
        L.append('')
    lat = {k: v for k, v in report.get('latency', {}).items() if v.get('n')}
    if lat:
        L.append('## Độ trễ (gửi ảnh tới model, đã có ảnh)')
        L.append('')
        for k, v in lat.items():
            L.append(f'- {INPUT_NAMES.get(k, k)}: {v["n"]} ảnh, trung bình {_num(v["meanMs"], 1)} ms, p50 {_num(v["p50Ms"], 1)} ms, p95 {_num(v["p95Ms"], 1)} ms.')
        L.append('')
    L.append('## Cách đọc')
    L.append('')
    L.append('- Độ phủ: tỉ lệ post có nhãn khác `unknown` ở điểm vận hành. Precision (gộp): tỉ lệ nhãn đúng trong các nhãn đã đưa ra. '
             'Precision theo lớp kèm (đúng/số dự đoán).')
    L.append('- Macro-F1: trung bình F1 của 4 lớp, `unknown` tính là bỏ sót. Macro-F1 (khi trả lời): chỉ trên post mà hệ thống đưa ra '
             'nhãn ở bất kỳ độ tin cậy nào.')
    L.append(f'- ECE ({ECE_BINS} bin) và risk–coverage dùng xác suất mà nhãn dựa vào: xác suất yếu nhất trong các tag bằng chứng của model '
             '(cách `classify` tính độ tin cậy). Model A0 chưa được calibrate.')
    L.append('- Slice: một post có thể thuộc nhiều slice (phong cách, ca biên). "thiếu tag" (`density=sparse`) = ≤ 8 tag general '
             'hoặc không có tag giới.')
    return '\n'.join(L) + '\n'


# ---------------- label: local page for human annotators ----------------

LABEL_PAGE = r'''<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gán nhãn giới tính</title>
<style>
:root{--bg:#16171b;--fg:#e8e8ea;--mute:#9a9ba3;--card:#22242a;--acc:#f0a050;--bad:#e05a5a}
@media (prefers-color-scheme: light){:root{--bg:#f4f4f6;--fg:#1d1e22;--mute:#5d5f68;--card:#fff;--acc:#b8651a;--bad:#b42828}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
header{display:flex;gap:16px;align-items:center;padding:10px 16px;background:var(--card)}header b{color:var(--acc)}
main{display:grid;grid-template-columns:1fr 330px;gap:16px;padding:16px;height:calc(100vh - 52px)}
#stage{display:flex;align-items:center;justify-content:center;background:var(--card);border-radius:10px;min-height:0;position:relative}
#stage img{max-width:100%;max-height:100%;object-fit:contain}#msg{position:absolute;color:var(--mute);text-align:center;padding:16px}
aside{display:flex;flex-direction:column;gap:8px;overflow:auto}
button{font:inherit;color:var(--fg);background:var(--card);border:1px solid #444;border-radius:8px;padding:9px 12px;text-align:left;cursor:pointer}
button:hover{border-color:var(--acc)}button kbd{float:right;color:var(--mute)}button.bad{border-color:var(--bad)}
.rules{color:var(--mute);font-size:13px}.rules li{margin:2px 0}
@media (max-width:760px){main{grid-template-columns:1fr;height:auto}#stage{height:60vh}}
</style></head><body>
<header><span>Người gán: <b id="who"></b></span><span id="prog"></span><span id="kind"></span><span id="key" style="color:var(--mute)"></span></header>
<main><div id="stage"><img id="img" alt=""><div id="msg">Đang tải…</div></div>
<aside>
<button data-l="only_female">Chỉ nữ <kbd>1 / F</kbd></button>
<button data-l="only_male">Chỉ nam <kbd>2 / M</kbd></button>
<button data-l="both">Cả nam và nữ <kbd>3 / B</kbd></button>
<button data-l="none">Không có nhân vật nam / nữ <kbd>4 / N</kbd></button>
<button data-l="skip">Bỏ qua (không chắc, ảnh thật) <kbd>S / Space</kbd></button>
<button data-a="toggle">Đổi ảnh sample / thumbnail <kbd>T</kbd></button>
<button data-a="undo">Hoàn tác <kbd>Z</kbd></button>
<button data-a="drop" class="bad">Không an toàn: loại bỏ <kbd>X</kbd></button>
<ul class="rules">
<li>Gán theo cách nhân vật được vẽ, không đoán người thật.</li>
<li>Futanari tính là nữ; trap / otoko_no_ko / femboy tính là nam.</li>
<li>Nhân vật mơ hồ không tính. Chỉ có nhân vật mơ hồ hoặc không có ai → "Không có".</li>
<li>Thấy bộ phận cơ thể nam (POV, ngoài khung) → có nam.</li>
<li>Robot, thú… có giới rõ ràng thì tính theo giới đó.</li>
<li>Nghi là trẻ vị thành niên → X ngay: post bị xoá khỏi tập, tag được báo cáo.</li>
</ul></aside></main>
<script>
const T = '__TOKEN__';
let cur = null, kind = 'sample', busy = false, seq = 0, blobUrl = '';
const $ = id => document.getElementById(id);
async function api(path, body) {
  const r = await fetch(path, body ? {method: 'POST', headers: {'X-Token': T, 'Content-Type': 'application/json'}, body: JSON.stringify(body)}
                                  : {headers: {'X-Token': T}});
  return r.json();
}
function say(text) { $('msg').textContent = text; $('msg').style.display = text ? '' : 'none'; }
async function show() {
  const mine = ++seq;
  $('img').removeAttribute('src');
  if (blobUrl) { URL.revokeObjectURL(blobUrl); blobUrl = ''; }
  if (!cur) { say('Xong: không còn post nào.'); return; }
  say('Đang tải ảnh…');
  $('kind').textContent = kind === 'sample' ? 'ảnh sample' : 'thumbnail';
  $('key').textContent = cur.key + (cur.mediaKind !== 'image' ? ' · ' + cur.mediaKind + ' (ảnh đại diện)' : '');
  const r = await fetch('/api/img?key=' + encodeURIComponent(cur.key) + '&kind=' + kind, {headers: {'X-Token': T}});
  if (mine !== seq) return;  // the annotator moved on meanwhile
  if (r.ok) {
    const b = await r.blob();
    if (mine !== seq) return;
    blobUrl = URL.createObjectURL(b); $('img').src = blobUrl; say(''); return;
  }
  let why = 'không tải được ảnh'; try { why = (await r.json()).error || why; } catch (e) {}
  say(why + (r.status === 410 ? ' — post đã bị loại tự động.' : ' — bấm S để bỏ qua.'));
  if (r.status === 410) setTimeout(next, 1200);
}
async function next() {
  const r = await api('/api/next');
  $('who').textContent = r.annotator; $('prog').textContent = r.done + ' / ' + r.total;
  cur = r.post; kind = 'sample'; show();
}
async function act(body, path) { if (busy) return; busy = true; try { await api(path, body); await next(); } finally { busy = false; } }
function label(l) { if (cur) act({key: cur.key, label: l}, '/api/label'); }
function drop() { if (cur && confirm('Loại post này vì nội dung không an toàn? Post bị xoá khỏi tập, tag được báo cáo.')) act({key: cur.key}, '/api/drop'); }
document.querySelectorAll('button[data-l]').forEach(b => b.onclick = () => label(b.dataset.l));
document.querySelector('[data-a=toggle]').onclick = () => { kind = kind === 'sample' ? 'thumb' : 'sample'; show(); };
document.querySelector('[data-a=undo]').onclick = () => act({}, '/api/undo');
document.querySelector('[data-a=drop]').onclick = drop;
const keys = {'1': 'only_female', f: 'only_female', '2': 'only_male', m: 'only_male', '3': 'both', b: 'both', '4': 'none', n: 'none', s: 'skip', ' ': 'skip'};
document.addEventListener('keydown', e => {
  if (e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (keys[k]) { e.preventDefault(); label(keys[k]); }
  else if (k === 't') { kind = kind === 'sample' ? 'thumb' : 'sample'; show(); }
  else if (k === 'z' || k === 'backspace') { e.preventDefault(); act({}, '/api/undo'); }
  else if (k === 'x') drop();
});
next();
</script></body></html>
'''


class LabelSession:
    """State of one annotator: manifest posts in a per-annotator order, labels file written after every answer"""

    def __init__(self, manifest_path: pathlib.Path, labels_path: pathlib.Path, annotator: str, *, fetcher: ImageFetcher,
                 gate=None, only: Iterable[str] | None = None) -> None:
        self.manifest_path = manifest_path
        self.labels_path = labels_path
        self.annotator = annotator
        self.fetcher = fetcher
        self.gate = gate
        self._lock = threading.RLock()
        self._gated: dict[tuple[str, str], str] = {}
        manifest = read_json(manifest_path)
        self.doc = read_json(labels_path) if labels_path.is_file() else {
            'schema': SCHEMA, 'kind': 'lynxx-gender-eval-labels', 'annotator': annotator, 'manifest': manifest_path.name,
            'manifestSha256': sha256_file(manifest_path), 'created': _now(), 'labels': {}, 'dropped': {}}
        if self.doc.get('annotator') != annotator:
            raise EvalError(f'{labels_path} belongs to annotator {self.doc.get("annotator")!r}')
        dropped = set(self.doc['dropped']) | {d['key'] for d in manifest.get('dropped') or []}
        self.posts = {p['key']: p for p in manifest.get('posts', []) if p['key'] not in dropped}
        for key in [k for k, p in self.posts.items() if blocked_tag(tags_with_aliases(p['tags'], p['site']))]:
            self.posts.pop(key)  # never shown: the blacklist runs here too (§4.1 (c))
        order = sorted(self.posts)
        random.Random(hashlib.sha256(annotator.encode()).hexdigest()).shuffle(order)
        if only is not None:
            wanted = set(only)
            order = [k for k in order if k in wanted]
        self.order = order
        self.history: list[str] = []

    def save(self) -> None:
        self.doc['updated'] = _now()
        write_json(self.labels_path, self.doc)

    def state(self) -> dict:
        with self._lock:
            done = sum(1 for k in self.order if k in self.doc['labels'])
            key = next((k for k in self.order if k not in self.doc['labels'] and k in self.posts), None)
            post = self.posts.get(key) if key else None
            return {'annotator': self.annotator, 'done': done, 'total': len(self.order),
                    'post': {'key': key, 'mediaKind': post.get('mediaKind', 'image')} if post else None}

    def label(self, key: str, label: str) -> None:
        if label not in LABEL_CHOICES:
            raise EvalError(f'label must be one of {", ".join(LABEL_CHOICES)}')
        with self._lock:
            if key not in self.posts:
                raise EvalError('unknown post')
            self.doc['labels'][key] = {'label': label, 'at': _now()}
            self.history.append(key)
            self.save()

    def undo(self) -> str:
        with self._lock:
            while self.history:
                key = self.history.pop()
                if self.doc['labels'].pop(key, None) is not None:
                    self.save()
                    return key
            return ''

    def drop(self, key: str, reason: str = 'unsafe', gate_tag: str = '') -> dict:
        """§3.2: a post a human (or the model gate) finds unsafe leaves the set at once; its tags are reported so the
        blacklist (§4.1, lynxx/safety.py) can be extended. The manifest is rewritten without it"""
        with self._lock:
            post = self.posts.pop(key, None)
            if post is None:
                raise EvalError('unknown post')
            self.doc['labels'].pop(key, None)
            self.order = [k for k in self.order if k != key]
            entry = {'reason': reason, 'at': _now(), 'tags': sorted({t.lower() for t in post['tags']}),
                     'unblockedTags': unblocked_tags(post['tags'])}
            if gate_tag:
                entry['gateTag'] = gate_tag
            self.doc['dropped'][key] = entry
            self.save()
            drop_from_manifest(self.manifest_path, [{'key': key, 'reason': reason, 'gateTag': gate_tag}], self.annotator)
        _log(f'dropped {key} ({reason}{f": model gate {gate_tag}" if gate_tag else ""}); tags not covered by the blacklist, '
             f'to review for lynxx/safety.py (§4.1): {", ".join(entry["unblockedTags"]) or "-"}')
        return entry

    def image(self, key: str, kind: str) -> tuple[int, bytes, str]:
        """(status, body, content type): the picture in memory; the model safety gate runs before a human sees it"""
        with self._lock:
            post = self.posts.get(key)
        if post is None:
            return 410, b'', ''
        url = picture_url(post, 'thumb' if kind == 'thumb' else 'sample')
        data = self.fetcher.get(post['site'], url)
        if not data:
            return 502, json.dumps({'error': 'không tải được ảnh'}).encode(), 'application/json'
        if self.gate is not None:
            try:
                v = classify(self.gate.predict(data))
            except ModelUnavailable as err:
                return 503, json.dumps({'error': f'model server (safety gate): {err}'}).encode(), 'application/json'
            if v.safety_tag:
                self.drop(key, 'modelGate', v.safety_tag)
                return 410, json.dumps({'error': 'safety gate của model chặn post này'}).encode(), 'application/json'
        ctype = 'image/png' if data[:8] == b'\x89PNG\r\n\x1a\n' else 'image/gif' if data[:4] == b'GIF8' else \
            'image/webp' if data[8:12] == b'WEBP' else 'image/jpeg'
        return 200, data, ctype


class _LabelHandler(BaseHTTPRequestHandler):
    server_version = 'lynxx-label/1'
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt: str, *args) -> None:  # no request log: keys are enough noise
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        if ctype.startswith('text/html'):
            self.send_header('Content-Security-Policy', "default-src 'none'; img-src blob:; style-src 'unsafe-inline'; "
                                                        "script-src 'unsafe-inline'; connect-src 'self'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, obj) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode(), 'application/json; charset=utf-8')

    def _allowed(self, need_token: bool) -> bool:
        """this computer only: Host must be the address we listen on (DNS rebinding), and api calls carry the session token"""
        port = self.server.server_address[1]
        if self.headers.get('Host', '') not in (f'127.0.0.1:{port}', f'localhost:{port}'):
            self._json(403, {'error': 'host'})
            return False
        if need_token:
            token = self.headers.get('X-Token') or url_parse.parse_qs(url_parse.urlsplit(self.path).query).get('t', [''])[0]
            if not secrets.compare_digest(str(token), self.server.token):
                self._json(403, {'error': 'token'})
                return False
        return True

    def do_GET(self) -> None:
        u = url_parse.urlsplit(self.path)
        if u.path == '/':
            if self._allowed(False):
                self._send(200, LABEL_PAGE.replace('__TOKEN__', self.server.token).encode(), 'text/html; charset=utf-8')
            return
        if not self._allowed(True):
            return
        sess: LabelSession = self.server.session
        if u.path == '/api/next':
            self._json(200, sess.state())
        elif u.path == '/api/img':
            q = url_parse.parse_qs(u.query)
            code, body, ctype = sess.image(q.get('key', [''])[0], q.get('kind', ['sample'])[0])
            if code == 410 and not body:
                body, ctype = json.dumps({'error': 'post đã bị loại'}).encode(), 'application/json'
            self._send(code, body, ctype)
        else:
            self._json(404, {'error': 'not found'})

    def do_POST(self) -> None:
        if not self._allowed(True):
            return
        try:
            n = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(min(n, 65536)) or b'{}') if n else {}
            assert isinstance(body, dict)
        except Exception:
            self._json(400, {'error': 'bad request'})
            return
        sess: LabelSession = self.server.session
        path = url_parse.urlsplit(self.path).path
        try:
            if path == '/api/label':
                sess.label(str(body.get('key', '')), str(body.get('label', '')))
                self._json(200, {'ok': True})
            elif path == '/api/undo':
                self._json(200, {'undone': sess.undo()})
            elif path == '/api/drop':
                sess.drop(str(body.get('key', '')))
                self._json(200, {'ok': True})
            else:
                self._json(404, {'error': 'not found'})
        except EvalError as err:
            self._json(400, {'error': str(err)})


def make_label_server(session: LabelSession, port: int = LABEL_PORT) -> ThreadingHTTPServer:
    """listens on 127.0.0.1 only (no option to change it): the page shows pictures of a booru to whoever can reach it"""
    srv = ThreadingHTTPServer(('127.0.0.1', port), _LabelHandler)
    srv.daemon_threads = True
    srv.session = session
    srv.token = secrets.token_urlsafe(18)
    return srv


# ---------------- CLI ----------------

def _model_backend(url: str, token: str):
    try:
        m = backend(url, token)
        if hasattr(m, 'load'):  # model server: load now, so the status tells the device (cpu / cuda)
            m.load()
    except ModelUnavailable as err:
        raise EvalError(f'model: {err}') from None
    if hasattr(m, 'info'):
        m.info = {**m.info, 'url': m.url}
    else:  # in process
        m.info = {'device': getattr(m, 'device', '')}
    return m


def cmd_sample(args) -> int:
    quotas = parse_quotas(args.quota)
    candidates: list[dict] = []
    for q in args.query:
        _log(f'query {q!r} ({args.module}, {args.pages} page(s))')
        candidates += collect(args.module, q, pages=args.pages, page_delay=args.page_delay)
    manifest = build_manifest(candidates, quotas=quotas, seed=args.seed, queries=args.query)
    manifest['module'] = args.module
    write_json(args.out, manifest)
    _log(f'{len(manifest["posts"])} posts -> {args.out} (no pictures stored)')
    return 0


def cmd_label(args) -> int:
    manifest_path = pathlib.Path(args.manifest)
    labels_path = pathlib.Path(args.labels or manifest_path.with_name(f'labels-{args.annotator}.json'))
    only = None
    if args.only:
        doc = read_json(args.only)
        only = [d['key'] if isinstance(d, dict) else str(d) for d in (doc.get('disagreements', []) if isinstance(doc, dict) else doc)]
    gate = None
    if not args.no_model_gate:
        gate = _model_backend(args.model_url, args.model_token)
    else:
        _log('warning: model safety gate off (--no-model-gate): only the tag blacklist protects annotators')
    fetcher = ImageFetcher(args.image_interval)
    session = LabelSession(manifest_path, labels_path, args.annotator, fetcher=fetcher, gate=gate, only=only)
    srv = make_label_server(session, args.port)
    url = f'http://127.0.0.1:{srv.server_address[1]}/'
    _log(f'labeling {len(session.order)} posts as {args.annotator!r} -> {labels_path}\nopen {url}  (Ctrl-C to stop)')
    if args.open:
        import webbrowser
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        fetcher.close()
    return 0


def cmd_agree(args) -> int:
    report, gold = agree(read_json(args.a), read_json(args.b), arbiter=read_json(args.arbiter) if args.arbiter else None,
                         manifest=read_json(args.manifest) if args.manifest else None)
    if args.out:
        write_json(args.out, report)
    if args.gold:
        write_json(args.gold, gold)
    _log(f'kappa {report["kappa"]} over {report["n"]} posts ({"ok" if report["meetsKappa"] else f"< {KAPPA_MIN}"}), '
         f'{len(report["disagreements"])} disagreements ({report["unresolved"]} unresolved), gold {report["gold"]["posts"]} posts')
    if report['unsafeTagsNotBlocked']:
        _log(f'tags of posts dropped as unsafe, not blocked yet (review for §4.1): {", ".join(report["unsafeTagsNotBlocked"])}')
    print(json.dumps({k: report[k] for k in ('n', 'kappa', 'observed', 'expected', 'meetsKappa', 'resolvedByArbiter', 'unresolved', 'gold')},
                     ensure_ascii=False))
    return 0


def cmd_evaluate(args) -> int:
    if not args.silver and not args.gold:
        raise EvalError('give --gold FILE (human labels) or --silver (strong tags as reference)')
    manifest = read_json(args.manifest)
    gold = read_json(args.gold) if args.gold else None
    if gold is not None and gold.get('kind') == 'lynxx-gender-eval-labels':
        _log('warning: a single annotator labels file, not a gold set (agree builds one from two annotators)')
    out_dir = pathlib.Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    inputs = tuple(x for x in args.inputs.split(',') if x)
    if not inputs or any(x not in ('sample', 'thumb') for x in inputs):
        raise EvalError('--inputs: sample, thumb or both')
    model = _model_backend(args.model_url, args.model_token)
    fetcher = ImageFetcher(args.image_interval)
    cache = PredictionCache(pathlib.Path(args.cache) if args.cache else out_dir / 'predictions.jsonl')
    deadline = time.monotonic() + args.max_minutes * 60 if args.max_minutes > 0 else 0

    def should_stop() -> str:
        if fetcher.challenged:
            return 'the site answered with a bot challenge: run again later, cached predictions are kept'
        if deadline and time.monotonic() > deadline:
            return f'--max-minutes {args.max_minutes} reached'
        return ''
    try:
        report = evaluate(manifest, model=model, fetch=lambda p, k: fetcher.get(p['site'], picture_url(p, k)), gold=gold,
                          inputs=inputs, min_conf=GenderConf.parse(args.min_conf), cache=cache, should_stop=should_stop)
    finally:
        fetcher.close()
    if gated := report['posts'].get('modelGateDropped'):  # like label: posts the model gate trips on are not kept
        removed = drop_from_manifest(args.manifest, [{'key': d['key'], 'reason': 'modelGate', 'gateTag': d['gateTag']} for d in gated],
                                     'evaluate')
        report['manifest']['removedFromManifest'] = removed
        for d in gated:
            _log(f'dropped {d["key"]} (model gate {d["gateTag"]}) from {args.manifest}; tags not covered by the blacklist, '
                 f'to review for lynxx/safety.py (§4.1): {", ".join(d["unblockedTags"]) or "-"}')
    report['manifest']['path'] = str(args.manifest)
    report['manifest']['sha256'] = sha256_file(args.manifest)
    report['pictureRequests'] = fetcher.requests
    write_json(out_dir / 'report.json', report)
    (out_dir / 'report.md').write_text(render_markdown(report), encoding='utf-8')
    _log(f'report -> {out_dir / "report.json"}, {out_dir / "report.md"} ({fetcher.requests} picture requests)')
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog='python -m lynxx.gender_eval', description='gender filter evaluation (no pictures stored)')
    ap.add_argument('-v', '--verbose', action='store_true', help='downloader log')
    sub = ap.add_subparsers(dest='cmd', required=True)
    s = sub.add_parser('sample', help='stratified post set (metadata only) -> manifest.json')
    s.add_argument('--module', default='db')
    s.add_argument('--query', action='append', required=True, help='one search (repeat for several); its pages go into one pool')
    s.add_argument('--pages', type=int, default=1, help='search pages per query')
    s.add_argument('--page-delay', type=float, default=PAGE_DELAY)
    s.add_argument('--quota', default='', help='posts per stratum, ex. only_female=60,both=60,sparse=80,weak=all '
                                               f'(default {",".join(f"{k}={v}" for k, v in DEFAULT_QUOTAS.items())})')
    s.add_argument('--seed', type=int, default=0)
    s.add_argument('--out', required=True)
    lb = sub.add_parser('label', help='labeling page for one annotator (127.0.0.1)')
    lb.add_argument('--manifest', required=True)
    lb.add_argument('--annotator', required=True)
    lb.add_argument('--labels', default='', help='labels file (default labels-<annotator>.json next to the manifest)')
    lb.add_argument('--only', default='', help='agree report (its disagreements) or JSON list of post keys: arbiter session')
    lb.add_argument('--port', type=int, default=LABEL_PORT)
    lb.add_argument('--open', action='store_true', help='open the page in the default browser')
    lb.add_argument('--image-interval', type=float, default=IMAGE_INTERVAL)
    lb.add_argument('--no-model-gate', action='store_true', help='do not run the model safety gate before showing a picture')
    ag = sub.add_parser('agree', help='Cohen\'s kappa of two annotators, disagreements, gold labels')
    ag.add_argument('a')
    ag.add_argument('b')
    ag.add_argument('--arbiter', default='')
    ag.add_argument('--manifest', default='', help='checks AC-G08 (size, share of sparse posts)')
    ag.add_argument('--out', default='', help='agreement report (input of label --only for the arbiter)')
    ag.add_argument('--gold', default='', help='gold labels: agreed + arbiter decisions')
    ev = sub.add_parser('evaluate', help='metrics -> report.json + report.md')
    ev.add_argument('--manifest', required=True)
    ev.add_argument('--gold', default='')
    ev.add_argument('--silver', action='store_true', help='no human labels: tier-0 labels of high confidence are the reference')
    ev.add_argument('--inputs', default='sample,thumb')
    ev.add_argument('--min-conf', default='medium', choices=('low', 'medium', 'high'))
    ev.add_argument('--cache', default='', help='predicted tags cache (default <out-dir>/predictions.jsonl)')
    ev.add_argument('--image-interval', type=float, default=IMAGE_INTERVAL)
    ev.add_argument('--max-minutes', type=float, default=0)
    ev.add_argument('--out-dir', required=True)
    for p in (lb, ev):
        p.add_argument('--model-url', default=os.environ.get(ENV_MODEL_URL, '') or API_DEFAULT_URL,
                       help='model server (empty: the model in this process)')
        p.add_argument('--model-token', default=os.environ.get(ENV_MODEL_TOKEN, ''))
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from .logger import Logger
    Logger.init(True, not args.verbose)
    try:
        return {'sample': cmd_sample, 'label': cmd_label, 'agree': cmd_agree, 'evaluate': cmd_evaluate}[args.cmd](args)
    except (EvalError, KeyError) as err:
        _log(f'error: {err}')
        return 1


if __name__ == '__main__':
    sys.exit(main())

#
#
#########################################
