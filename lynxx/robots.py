# coding=UTF-8
"""
Lynxx: robots.txt rules as RFC 9309 reads them (the tag list updater, lynxx/tags_updater.py).
urllib.robotparser takes the first matching rule and no wildcards: with 'Disallow: /*.json' after 'Allow: /' it would allow
'/tags.json'. Here the most specific rule (the longest pattern) wins, '*' and a final '$' are understood, Allow wins a tie,
groups for the same agent are merged and the '*' group is used only when no group names the agent. A Crawl-delay before any
user-agent line applies to every agent.
"""
#########################################
#
#

from __future__ import annotations

import re
from urllib import parse as url_parse

__all__ = ('ROBOTS_MAX_BYTES', 'RobotsRules')

ROBOTS_MAX_BYTES = 512 * 1024
'''RFC 9309 2.5: parse at least 500 KiB, ignore the rest'''


def _norm(path: str) -> str:
    """percent-decoding of both sides: '/a%2Db' and '/a-b' are the same rule"""
    return url_parse.unquote(path)


def _pattern(path: str) -> re.Pattern[str]:
    anchored = path.endswith('$')
    body = path[:-1] if anchored else path
    return re.compile(''.join('.*' if c == '*' else re.escape(c) for c in body) + ('$' if anchored else ''), re.DOTALL)


class RobotsRules:
    """Rules of one robots.txt for one product token (ex. 'Lynxx')"""
    __slots__ = ('_rules', 'crawl_delay', 'status')

    def __init__(self, rules: list[tuple[bool, str]], crawl_delay: float = 0.0, status: str = 'found') -> None:
        self._rules = [(allow, len(path.encode('utf-8')), _pattern(_norm(path))) for allow, path in rules if path]
        self.crawl_delay = crawl_delay
        self.status = status

    @staticmethod
    def allow_all(status: str) -> RobotsRules:
        """RFC 9309 2.3.1.3: robots.txt unavailable (4xx): no restrictions"""
        return RobotsRules([], 0.0, status)

    @staticmethod
    def disallow_all(status: str) -> RobotsRules:
        """RFC 9309 2.3.1.4: robots.txt unreachable (5xx, network error): complete disallow"""
        return RobotsRules([(False, '/')], 0.0, status)

    @staticmethod
    def parse(text: str, agent: str) -> RobotsRules:
        token = agent.split('/', maxsplit=1)[0].strip().lower()
        groups: list[tuple[list[str], list[tuple[bool, str]], list[float]]] = []
        agents: list[str] = []
        leading: list[float] = []
        '''Crawl-delay lines before any user-agent line (www.zerochan.net: 'Crawl-delay: 10' on the first line). RFC 9309 has no
        Crawl-delay and puts such lines in no group; a polite reader (R4) applies them to every agent'''
        in_rules = True  # a 'user-agent' line after rules starts a new group
        for raw in text[:ROBOTS_MAX_BYTES].splitlines():
            line = raw.split('#', 1)[0].strip()
            if ':' not in line:
                continue
            key, value = (x.strip() for x in line.split(':', 1))
            key = key.lower().replace('_', '-')
            if key == 'user-agent':
                if in_rules:
                    agents = []
                    groups.append((agents, [], []))
                    in_rules = False
                agents.append(value.split('/')[0].strip().lower())
                continue
            if not groups:
                if key == 'crawl-delay':
                    try:
                        leading.append(max(0.0, float(value)))
                    except ValueError:
                        pass
                continue  # rules before any user-agent line belong to no group
            in_rules = True
            if key in ('allow', 'disallow'):
                groups[-1][1].append((key == 'allow', value))
            elif key == 'crawl-delay':
                try:
                    groups[-1][2].append(max(0.0, float(value)))
                except ValueError:
                    continue
        mine = [g for g in groups if token in g[0]] or [g for g in groups if '*' in g[0]]
        rules = [r for g in mine for r in g[1]]
        delays = [d for g in mine for d in g[2]] + leading
        return RobotsRules(rules, max(delays, default=0.0))

    def allows(self, url: str) -> bool:
        """url: absolute url or path (with query). The most specific matching rule wins, Allow on a tie, no rule: allowed"""
        p = url_parse.urlsplit(url)
        path = _norm((p.path or '/') + (f'?{p.query}' if p.query else ''))
        if path == '/robots.txt':
            return True
        best_len, best_allow = -1, True
        for allow, length, rex in self._rules:
            if rex.match(path) and (length > best_len or (length == best_len and allow)):
                best_len, best_allow = length, allow
        return best_allow

#
#
#########################################
