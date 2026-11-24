# coding=UTF-8
"""
Lynxx: hard safety blacklist for content suggesting minors (docs/plans/gender-filter-model.md §4.1)
Product decision 2026-11-18 (replaces the one of 2026-11-16, where the gender filter turned it on): the blacklist is its own
filter, -safety_blacklist, independent of -gender and off by default; the user turns it on. Once on it applies to previews
and downloads, fails closed when tags are unknown, and also enables the image model's safety gate. The list is hard-coded:
settings, CLI, environment and tag lists cannot change it; extensions may only ADD tags (manifest "safetyTags"). There is
intentionally no removal API.
"""
#########################################
#
#

from __future__ import annotations

import re
from collections.abc import Iterable
from threading import Lock

__all__ = ('BLOCKED_TAGS', 'BLOCKED_TOKENS', 'blocked_tag', 'is_blocked', 'register_extra_tags')

BLOCKED_TOKENS: frozenset[str] = frozenset((
    'loli', 'lolis', 'lolicon', 'shota', 'shotas', 'shotacon', 'toddler', 'toddlers', 'toddlercon', 'child', 'children', 'childs',
    'kid', 'kids', 'infant', 'infants', 'baby', 'babies', 'newborn', 'preteen', 'preteens', 'underage', 'underaged', 'minor', 'minors',
    'cub', 'cubs', 'young', 'younger', 'youngest', 'juvenile', 'tween', 'tweens', 'kindergarten', 'elementary', 'teen', 'teens',
    'teenager', 'teenagers', 'teenage', 'teenaged', 'jailbait', 'pedophilia', 'kodomo',
))
'''a tag is blocked if any of its tokens (split by _ - / ( ) and spaces) is listed. False positives (ex. kid_icarus) are accepted'''

BLOCKED_TAGS: frozenset[str] = frozenset((
    # db
    'aged_down', 'randoseru', 'oppai_loli', 'kindergarten_uniform', 'mesugaki', 'onee-shota', 'onee-loli', 'age_regression',
    'lolidom', 'shotadom', 'diaper',
    # rx / bb / rs / xb
    'younger_female', 'younger_male', 'young_female', 'young_male', 'very_young', 'futa_loli', 'straight_shota',
    # en
    'child_on_child', 'young_(lore)',
))
'''exact tags not caught by tokens'''

_ADULT_EXCEPTIONS: frozenset[str] = frozenset((
    'child_bearing_hips', 'childbearing_hips', 'young_adult',
))
'''hard-coded, not an API: exact tags with a blocked token that describe adults (body type, explicit adult age). Minimal on purpose:
series names with teenage characters (teen_titans, kid_icarus...) stay blocked'''

re_token_split = re.compile(r'[_\-/() ]+')

_extra_lock = Lock()
_extra_tags: set[str] = set()


def register_extra_tags(tags: Iterable[str]) -> None:
    """Extensions can only extend the blacklist (manifest 'safetyTags')"""
    with _extra_lock:
        _extra_tags.update(str(t).strip().lower() for t in tags if str(t).strip())


def blocked_tag(tags: Iterable[str]) -> str:
    """Returns the first tag that makes a post blocked, or an empty string"""
    for raw in tags:
        tag = raw.strip().lower()
        if not tag:
            continue
        if tag in BLOCKED_TAGS or tag in _extra_tags:
            return tag
        if tag in _ADULT_EXCEPTIONS:
            continue
        if any(tok in BLOCKED_TOKENS for tok in re_token_split.split(tag) if tok):
            return tag
    return ''


def is_blocked(tags: Iterable[str]) -> bool:
    return bool(blocked_tag(tags))

#
#
#########################################
