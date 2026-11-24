# coding=UTF-8
"""
Lynxx: character gender labels from post tags (tier 0 of docs/plans/gender-filter-model.md, no ML)
Labels describe how fictional characters are drawn and counted by the website, not real people.
"""
#########################################
#
#

from __future__ import annotations

import json
import pathlib
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from threading import Lock

from .defines import UTF8

__all__ = ('GENDER_LABELS', 'GenderConf', 'GenderLabel', 'GenderResult', 'GenderRules', 'decide', 'infer_from_tags')

RULES_DIR = pathlib.Path(__file__).resolve().parent / 'gender_rules'
RULESET_BY_MODULE = {'db': 'db', 'rx': 'rx', 'bb': 'bb', 'en': 'en', 'rs': 'gelbooru_generic', 'xb': 'gelbooru_generic'}
GENERIC_RULESET = 'gelbooru_generic'
UNMEASURED_COMPLETENESS = 0.5
'''modules without a ruleset (paheal, rule34hentai, unknown extensions): free-form tags, absence of evidence means little'''
STRICT_FLAGS = frozenset(('futa', 'futaMale', 'trap', 'trapFemale', 'other'))


class GenderLabel(StrEnum):
    ONLY_FEMALE = 'only_female'
    ONLY_MALE = 'only_male'
    BOTH = 'both'
    NONE = 'none'
    UNKNOWN = 'unknown'


GENDER_LABELS = tuple(str(x) for x in GenderLabel)


class GenderConf(IntEnum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3

    @staticmethod
    def parse(name: str) -> GenderConf:
        return GenderConf[name.upper()]


@dataclass(slots=True)
class GenderResult:
    label: GenderLabel
    conf: GenderConf | None
    flags: frozenset[str] = frozenset()
    evidence: tuple[str, ...] = ()
    conflict: bool = False
    source: str = 'tags'
    '''tags | model | tags+model (lynxx/gender_model.py)'''

    def as_dict(self) -> dict:
        return {'label': str(self.label), 'conf': self.conf.name.lower() if self.conf else None, 'flags': sorted(self.flags),
                'evidence': list(self.evidence), 'conflict': self.conflict, 'source': self.source}


@dataclass
class GenderRules:
    module: str
    completeness: float
    female: dict[GenderConf, frozenset[str]]
    male: dict[GenderConf, frozenset[str]]
    both: frozenset[str]
    exclusive_female: frozenset[str]
    exclusive_male: frozenset[str]
    exclusive_futa: frozenset[str]
    solo: frozenset[str]
    pair_sep: str
    pair_words: dict[str, frozenset[str]]
    other: frozenset[str]
    none: frozenset[str]
    flags: dict[str, frozenset[str]]
    male_bodypart: frozenset[str]
    female_bodypart: frozenset[str] = frozenset()
    aliases: dict[str, str] = field(default_factory=dict)


    @staticmethod
    def from_dict(d: dict, module: str = '') -> GenderRules:
        fs = lambda xs: frozenset(str(x).lower() for x in (xs or ()))  # noqa: E731
        return GenderRules(
            module=module or d.get('module', '*'),
            completeness=float(d.get('completeness', UNMEASURED_COMPLETENESS)),
            female={GenderConf.HIGH: fs(d.get('female', {}).get('high')), GenderConf.MEDIUM: fs(d.get('female', {}).get('medium'))},
            male={GenderConf.HIGH: fs(d.get('male', {}).get('high')), GenderConf.MEDIUM: fs(d.get('male', {}).get('medium'))},
            both=fs(d.get('both')),
            exclusive_female=fs(d.get('exclusive', {}).get('only_female')),
            exclusive_male=fs(d.get('exclusive', {}).get('only_male')),
            exclusive_futa=fs(d.get('exclusiveFuta')),
            solo=fs(d.get('solo')),
            pair_sep=str(d.get('pairSeparator') or ''),
            pair_words={k: fs(v) for k, v in (d.get('pairWords') or {}).items()},
            other=fs(d.get('other')),
            none=fs(d.get('none')),
            flags={k: fs(v) for k, v in (d.get('flags') or {}).items()},
            male_bodypart=fs(d.get('maleBodypart')),
            female_bodypart=fs(d.get('femaleBodypart')),
            aliases={str(k).lower(): str(v).lower() for k, v in (d.get('aliases') or {}).items()},
        )

    @staticmethod
    def load_file(path: pathlib.Path, module: str = '') -> GenderRules:
        return GenderRules.from_dict(json.loads(path.read_text(encoding=UTF8)), module)


_rules_lock = Lock()
_rules_cache: dict[str, GenderRules] = {}
_extension_rules: dict[str, pathlib.Path] = {}
'''abbr -> ruleset file provided by an extension manifest ("genderRules")'''


def register_rules_file(abbr: str, path: pathlib.Path) -> None:
    with _rules_lock:
        _extension_rules[abbr] = path
        _rules_cache.pop(abbr, None)


def has_measured_rules(abbr: str) -> bool:
    return abbr in RULESET_BY_MODULE or abbr in _extension_rules


def rules_for_module(abbr: str) -> GenderRules:
    with _rules_lock:
        if abbr in _rules_cache:
            return _rules_cache[abbr]
        if abbr in _extension_rules:
            rules = GenderRules.load_file(_extension_rules[abbr], abbr)
        elif abbr in RULESET_BY_MODULE:
            rules = GenderRules.load_file(RULES_DIR / f'{RULESET_BY_MODULE[abbr]}.json', abbr)
        else:
            rules = GenderRules.load_file(RULES_DIR / f'{GENERIC_RULESET}.json', abbr)
            rules.completeness = UNMEASURED_COMPLETENESS
        _rules_cache[abbr] = rules
        return rules


def _strongest(t: Collection[str], table: dict[GenderConf, frozenset[str]]) -> tuple[GenderConf | None, list[str]]:
    for conf in (GenderConf.HIGH, GenderConf.MEDIUM):
        if hits := sorted(t & table[conf]):
            return conf, hits
    return None, []


def infer_from_tags(tags: Iterable[str], rules: GenderRules) -> GenderResult:
    """Tier 0: label from tags only. See docs/plans/gender-filter-model.md §1.2 (edge cases) and §2.2"""
    t = {rules.aliases.get(x, x) for x in (tag.strip().lower() for tag in tags) if x}
    flags = {name for name, lst in rules.flags.items() if t & lst}
    evidence: list[str] = []

    F, f_ev = _strongest(t, rules.female)
    M, m_ev = _strongest(t, rules.male)
    evidence += f_ev + m_ev
    if both_hits := sorted(t & rules.both):
        F, M = GenderConf.HIGH, GenderConf.HIGH
        evidence += both_hits

    # e621-style pair tags: 'male/female', 'gynomorph/male', ...
    if rules.pair_sep:
        for tag in sorted(t):
            if rules.pair_sep not in tag:
                continue
            for part in tag.split(rules.pair_sep):
                if part in rules.pair_words.get('female', ()):
                    F = GenderConf.HIGH
                elif part in rules.pair_words.get('male', ()):
                    M = GenderConf.HIGH
                elif part in rules.pair_words.get('futa', ()):
                    F = GenderConf.HIGH
                    flags.add('futa')
                elif part in rules.pair_words.get('futaMale', ()):
                    M = GenderConf.HIGH
                    flags.add('futaMale')
                elif part in rules.pair_words.get('other', ()):
                    flags.add('other')
                else:
                    continue
                evidence.append(tag)

    # 'futa_only' must mark the post as futa BEFORE body part tags are read: its penis is not a male character
    excl_futa = sorted(t & rules.exclusive_futa)
    if excl_futa:
        flags.add('futa')
    futa = 'futa' in flags or 'futaMale' in flags
    # futanari & co: drawn as female, penis does not count as a male character
    if 'futa' in flags and F is None:
        F = GenderConf.HIGH
    if 'futaMale' in flags and M is None:
        M = GenderConf.HIGH
    # otoko_no_ko / trap / femboy count as male, reverse_trap as female
    if 'trap' in flags and M is None:
        M = GenderConf.HIGH
    if 'trapFemale' in flags and F is None:
        F = GenderConf.HIGH
    if (bp := sorted(t & rules.male_bodypart)) and not futa:
        M = GenderConf.HIGH
        evidence += bp
    # the female mirror (pussy, breasts, female_pov...): a woman is in the picture, so a post with male evidence is 'both', never
    # only_male. With a futa / cuntboy / male_with_breasts flag the part may belong to that character: not counted. Evidence only
    # when the part is what makes F high (a redundant part must not lower the confidence of the model's verdict, gender_model.classify)
    if (fbp := sorted(t & rules.female_bodypart)) and not futa and F != GenderConf.HIGH:
        F = GenderConf.HIGH
        evidence += fbp
    if t & rules.other:
        flags.add('other')
    other = 'other' in flags

    conflict = False
    exclusive: GenderLabel | None = None
    if excl_futa:
        exclusive = GenderLabel.ONLY_FEMALE
        evidence += excl_futa
    elif excl := sorted(t & rules.exclusive_female):
        exclusive = GenderLabel.ONLY_FEMALE
        evidence += excl
    elif excl := sorted(t & rules.exclusive_male):
        exclusive = GenderLabel.ONLY_MALE
        evidence += excl
    elif (solo := sorted(t & rules.solo)) and (F is None) != (M is None):
        exclusive = GenderLabel.ONLY_FEMALE if F is not None else GenderLabel.ONLY_MALE
        evidence += solo

    label: GenderLabel
    conf: GenderConf | None
    if exclusive is not None:
        consistent = (exclusive == GenderLabel.ONLY_FEMALE and M is None) or (exclusive == GenderLabel.ONLY_MALE and F is None)
        if consistent:
            return GenderResult(exclusive, GenderConf.MEDIUM if other else GenderConf.HIGH, frozenset(flags),
                                tuple(dict.fromkeys(evidence)))
        conflict = True  # count tags win over the exclusivity marker, confidence drops to LOW below

    if F is not None and M is not None:
        label, conf = GenderLabel.BOTH, min(F, M)
    elif F is not None or M is not None:
        label = GenderLabel.ONLY_FEMALE if F is not None else GenderLabel.ONLY_MALE
        conf = F if F is not None else M
        if rules.completeness < 0.9:  # 'absence' of the other gender is weak evidence on loosely tagged sites
            conf = min(conf, GenderConf.MEDIUM)
        if other:
            conf = min(conf, GenderConf.MEDIUM)
    elif (none_hits := sorted(t & rules.none)) or other:
        evidence += none_hits
        label = GenderLabel.NONE
        conf = GenderConf.HIGH if rules.completeness >= 0.9 and not other else GenderConf.MEDIUM
    else:
        return GenderResult(GenderLabel.UNKNOWN, None, frozenset(flags), ())
    if conflict:
        conf = GenderConf.LOW
    return GenderResult(label, conf, frozenset(flags), tuple(dict.fromkeys(evidence)), conflict)


def effective_label(res: GenderResult, min_conf: GenderConf, strict: bool) -> GenderLabel:
    """Label used for selection: below threshold -> unknown; strict: futa/trap/ambiguous are never only_*"""
    if res.label == GenderLabel.UNKNOWN or res.conf is None or res.conf < min_conf:
        return GenderLabel.UNKNOWN
    if strict and res.label in (GenderLabel.ONLY_FEMALE, GenderLabel.ONLY_MALE) and res.flags & STRICT_FLAGS:
        return GenderLabel.UNKNOWN
    return res.label


def decide(res: GenderResult, select: Collection[str], min_conf: GenderConf, strict: bool) -> tuple[bool, GenderLabel]:
    eff = effective_label(res, min_conf, strict)
    return str(eff) in select, eff

#
#
#########################################
