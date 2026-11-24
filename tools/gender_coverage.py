#!/usr/bin/env python3
# coding=UTF-8
"""
Lynxx: measure tier-0 gender label coverage on a website (docs/plans/gender-filter-model.md §2.6, AC-G07)
Reads post TAGS only (search pages), never downloads images. Output: JSON summary.
Usage: python tools/gender_coverage.py --module db --n 1000 [--sample latest|random|both] [--out report.json]
"""
import argparse
import json
import math
import pathlib
import sys
import time
from multiprocessing.dummy import current_process

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, ROOT.as_posix())

from lynxx.logger import Logger  # noqa: E402

Logger.init(True, True)

from lynxx.cmdargs import prepare_arglist  # noqa: E402
from lynxx.downloaders import _make_downloader  # noqa: E402
from lynxx.extensions import Extensions  # noqa: E402
from lynxx.gender import GenderConf, GenderLabel, infer_from_tags, rules_for_module  # noqa: E402
from lynxx.module import ProcModule  # noqa: E402
from lynxx.safety import is_blocked  # noqa: E402

RANDOM_TAG = {'db': 'random:200', 'en': 'order:random', 'rx': 'sort:random', 'xb': 'sort:random', 'bb': 'sort:random'}
'''a sort tag alone is not a valid query, it is combined with LATEST_TAG'''
LATEST_TAG = 'id:>0'


def collect(module: str, n: int, sample: str) -> list[list[str]]:
    pid = ProcModule.PROC_MODULES_BY_NAME[module]
    query = RANDOM_TAG.get(module, LATEST_TAG) if sample == 'random' else LATEST_TAG
    seen: dict[str, list[str]] = {}
    with _make_downloader(pid) as dwn:
        me = current_process()
        me.killed = False
        dwn.reset_root_thread(me)
        dwn.no_conflict_delay = True
        tags = [LATEST_TAG, query] if query != LATEST_TAG else [LATEST_TAG]
        dwn._parse_args(prepare_arglist(['-module', module, *tags]), False)
        dwn.url = dwn._form_tags_search_address(dwn._consume_custom_module_tags(dwn.tags_str_arr[0]))
        pages = math.ceil(n / dwn._get_items_per_page()) + (2 if sample == 'random' else 0)
        for page in range(pages):
            raw = dwn.fetch_html(dwn._form_page_num_address(page))
            if raw is None:
                break
            for h in dwn._argswitch2(dwn._get_all_post_tags, raw.raw, raw.html, dwn._parses_raw_pages()):
                info = dwn._extract_item_info(str(h))
                if info.id and info.id not in seen:
                    seen[info.id] = info.tags.split(' ') if info.tags else []
            if len(seen) >= n:
                break
            time.sleep(1.0)
    return list(seen.values())[:n]


def summarize(module: str, posts: list[list[str]]) -> dict:
    rules = rules_for_module(module)
    blocked = 0
    table: dict[str, dict[str, int]] = {str(lb): {} for lb in GenderLabel}
    covered = conflicts = 0
    for tags in posts:
        if is_blocked(tags):
            blocked += 1
            continue
        res = infer_from_tags(tags, rules)
        conf = res.conf.name.lower() if res.conf else 'none'
        table[str(res.label)][conf] = table[str(res.label)].get(conf, 0) + 1
        covered += res.label != GenderLabel.UNKNOWN and res.conf is not None and res.conf >= GenderConf.MEDIUM
        conflicts += res.conflict
    rated = len(posts) - blocked
    return {
        'module': module, 'posts': len(posts), 'blocked_when_filtering': blocked, 'rated': rated,
        'coverage_medium_plus': round(covered / rated, 4) if rated else 0.0,
        'unknown_rate': round(sum(table['unknown'].values()) / rated, 4) if rated else 0.0,
        'conflict_rate': round(conflicts / rated, 4) if rated else 0.0,
        'labels': table,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('--module', default='db')
    ap.add_argument('--n', type=int, default=1000)
    ap.add_argument('--sample', choices=('latest', 'random', 'both'), default='both')
    ap.add_argument('--out', default='')
    args = ap.parse_args()
    Extensions.ensure_loaded(include_catalog=True)
    report = {'generated': time.strftime('%F %T'), 'note': 'tags only, no images were downloaded', 'samples': []}
    for sample in (('latest', 'random') if args.sample == 'both' else (args.sample,)):
        posts = collect(args.module, args.n, sample)
        report['samples'].append({'sample': sample, **summarize(args.module, posts)})
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        pathlib.Path(args.out).write_text(text + '\n', encoding='utf-8')
    print(text)
    return 0


if __name__ == '__main__':
    sys.exit(main())
