# coding=UTF-8
"""
Lynxx: extension manifest (manifest.json) model and validation
"""
#########################################
#
#

from __future__ import annotations

import json
import pathlib
import re
from dataclasses import asdict, dataclass, field

from .defines import UTF8

__all__ = ('MANIFEST_API_VERSION', 'MANIFEST_FILE_NAME', 'ExtensionManifest', 'ManifestError', 'SearchKind', 'SearchSpec', 'TagSyntax')

MANIFEST_API_VERSION = 1
MANIFEST_FILE_NAME = 'manifest.json'
KNOWN_MEDIA = ('image', 'video', 'audio')

re_ext_id = re.compile(r'^[a-z][a-z\d_-]{1,31}$', re.ASCII)
re_abbr = re.compile(r'^[a-z][a-z\d]{1,3}$', re.ASCII)
re_version = re.compile(r'^\d+\.\d+\.\d+(?:[-+][\w.]+)?$', re.ASCII)
re_entry = re.compile(r'^(?:[\w.]+|[\w-]+\.py):[A-Za-z_]\w*$', re.ASCII)
re_min_sdk = re.compile(r'^\d{1,3}\.\d{1,3}(?:\.\d{1,3})?$', re.ASCII)
re_tag_update_entry = re.compile(r'^[A-Za-z_][\w-]*\.py:[A-Za-z_]\w*$', re.ASCII)

TAG_UPDATE_KEYS = ('entry', 'merge')
'''keys of a manifest's tagUpdate. Cores before 2.0.10 know only 'entry' and refuse a manifest with 'merge' (the installed version
stays)'''
TAG_UPDATE_KEYS_SINCE = {'merge': '2.0.10'}
'''the Lynxx release that first accepts each tagUpdate key after 'entry': older cores refuse the whole manifest
(tools/build_catalog.py warns about a catalog that holds one)'''
TAG_MERGE_MIN_POSTS = (2, 1_000_000)
'''bounds of tagUpdate.merge.minPosts (1 would be every tag of the site: a whole list, not a merge)'''
MANIFEST_KEYS_SINCE = {'search': '2.0.11'}
'''the Lynxx release that first accepts each top-level key added after 2.0.7: older cores refuse the whole manifest with
'unknown field(s): <key>' (the installed version stays; tools/build_catalog.py warns about a catalog that holds one)'''
NEWER_CORE_HINT = ' (bản extension này viết cho Lynxx mới hơn: cập nhật Lynxx rồi cài lại)'
'''added by the installer and the scan of installed extensions (lynxx/extensions.py, with_newer_core_hint) to the 'unknown
field(s)' refusals: there a key this core does not know is almost always one of a newer Lynxx (the owner read "unknown field(s):
search" from 2.0.10 on a 2.0.11 extension zip, 2026-11-21, without a hint of what to do). Not added here: the generator checks the
manifests it writes with the same parser, where an unknown key is a mistake of the draft, not a newer core'''
UNKNOWN_KEY_REFUSALS = ('unknown field(s): ', 'unknown tagUpdate field(s): ')
'''the refusals of keys this core does not know (_from_dict, _check_tag_update)'''


def with_newer_core_hint(err: Exception) -> str:
    """The message of a refused manifest, with NEWER_CORE_HINT when it names keys this core does not know"""
    text = str(err)
    return f'{text}{NEWER_CORE_HINT}' if text.startswith(UNKNOWN_KEY_REFUSALS) and not text.endswith(NEWER_CORE_HINT) else text
SDK_VERSIONS_SINCE = {'1.2': '2.0.11', '1.3': '2.0.14'}
'''the Lynxx release that first has each SDK version after 1.1 (2.0.7 to 2.0.10: SDK 1.1): older cores refuse to install a manifest
whose minSdkVersion is newer than their SDK (the installed version stays; tools/build_catalog.py warns about it)'''

SEARCH_KIND_IDS = ('artist', 'character', 'copyright', 'model', 'category', 'uploader', 'favorites', 'pool', 'collection', 'keyword')
'''ids of search kinds ("Tim theo" in the app, which labels them): an unknown id is skipped (a later core may know it)'''
SEARCH_KIND_INPUTS = ('tag', 'text', 'id')
SEARCH_KIND_KEYS = ('id', 'token', 'value', 'input', 'tagType', 'listing', 'pageSize', 'example', 'label', 'use', 'order')
SEARCH_KEYS = ('kinds', 'metaNames', 'idSearch')
SEARCH_TAG_TYPES = ('general', 'artist', 'character', 'copyright', 'metadata', 'species', 'lore')
'''tag list types a kind may filter autocomplete by (lynxx/tags.py names)'''
SEARCH_KINDS_MAX = 16
SEARCH_META_NAMES_MAX = 64
SEARCH_VALUE_MAX_LEN = 200
'''longest value regex of a kind'''
SEARCH_PAGE_SIZE_MAX = 1000
re_kind_token = re.compile(r'^[a-z][a-z_]{0,23}[:=]$', re.ASCII)
re_meta_name_decl = re.compile(r'^[a-z][a-z\d_]{0,31}$', re.ASCII)


class ManifestError(ValueError):
    pass


@dataclass
class TagSyntax:
    """Tag syntax / limits of a website. Values are only required for modules unknown to the core"""
    concat: str = '+'
    idSep: str = ':'
    maxTagsStringLen: int = 3300
    maxTags: int = 0
    '''0 = unlimited'''
    maxTagsSeparate: bool = False
    maxWildcards: int = 0
    freeMetaPrefixes: list[str] = field(default_factory=list)
    localPositiveOverflow: bool = False
    metaCount: str = r':(?:(?:[<>]=?|=)?[a-z\d\-_]+?|[a-z\d_]+:[a-z\d_]+?)'
    sort: str = r'sort(?::[^:]+?){1,2}'
    fav: str = r'favorited_by:\d+?'
    pool: str = r'pool:\d+?'

    @staticmethod
    def from_dict(d: dict | None) -> TagSyntax:
        syn = TagSyntax()
        if d is not None and not isinstance(d, dict):
            raise ManifestError('syntax must be an object')
        for k, v in (d or {}).items():
            if not hasattr(syn, k):
                raise ManifestError(f'unknown syntax field \'{k}\'')
            default = getattr(syn, k)
            if isinstance(default, bool):
                ok = isinstance(v, bool)
            elif isinstance(default, int):
                ok = isinstance(v, int) and not isinstance(v, bool) and v >= 0
            elif isinstance(default, list):
                ok = isinstance(v, list) and all(isinstance(x, str) for x in v)
            else:
                ok = isinstance(v, str)
            if not ok:
                raise ManifestError(f'syntax.{k} has invalid type')
            setattr(syn, k, v)
        for rk in ('metaCount', 'sort', 'fav', 'pool'):
            try:
                re.compile(getattr(syn, rk))
            except re.error as err:
                raise ManifestError(f'syntax.{rk} is not a valid regex: {err}')
        if syn.concat not in ('+', ' ') or syn.idSep not in (':', '='):
            raise ManifestError('syntax.concat must be \'+\' or \' \', syntax.idSep must be \':\' or \'=\'')
        return syn


def unsafe_regex_reason(pattern: str) -> str:
    """'' when a kind's value regex is safe to run on user input, else why not: back references, look-arounds and a repeat
    inside a repeat (catastrophic backtracking, ex. '(a+)+') are refused"""
    try:
        import re._parser as sre_parse  # Python 3.11+
    except ImportError:  # pragma: no cover
        import sre_parse  # type: ignore[no-redef]
    try:
        tree = sre_parse.parse(pattern)
    except Exception as err:
        return f'not a valid regex: {err}'
    repeats = ('MAX_REPEAT', 'MIN_REPEAT', 'POSSESSIVE_REPEAT')

    def walk(items, in_repeat: bool) -> str:
        for op, av in items:
            name = str(op)
            if name in ('GROUPREF', 'GROUPREF_EXISTS'):
                return 'back references are not allowed'
            if name in ('ASSERT', 'ASSERT_NOT'):
                return 'look-arounds are not allowed'
            if name in repeats:
                _, hi, sub = av
                many = hi > 1
                if many and in_repeat:
                    return 'a repeat inside a repeat is not allowed (catastrophic backtracking)'
                if why := walk(sub, in_repeat or many):
                    return why
            elif name == 'SUBPATTERN':
                if why := walk(av[-1], in_repeat):
                    return why
            elif name == 'ATOMIC_GROUP':
                if why := walk(av, in_repeat):
                    return why
            elif name == 'BRANCH':
                for sub in av[1]:
                    if why := walk(sub, in_repeat):
                        return why
        return ''
    return walk(tree, False)


@dataclass
class SearchKind:
    """One way to fill the search ("Tim theo" in the app). The query stays one string: a kind with a token is the tag
    '<token><value>' (ex. 'artist:some_artist'), a kind without one is a plain tag of tagType (autocomplete filtered by type)"""
    id: str
    token: str = ''
    '''^[a-z][a-z_]{0,23}[:=]$, '' for a kind that is a plain tag of tagType'''
    value: str = ''
    '''python regex of what follows the token: the core accepts '-?<token>(?:<value>)' instead of its own tag characters'''
    input: str = 'tag'
    '''tag (autocomplete) | text (free words, spaces as '_') | id (digits)'''
    tagType: str = ''
    '''tag list type the app's autocomplete is filtered by'''
    listing: bool = False
    '''the token selects another site listing (ex. a /models/<x>/ page): not counted towards maxTags, every plain tag of the query is
    checked locally, the site's order (no id cursor, no date limits), no 'or' group, one listing kind per query'''
    pageSize: int = 0
    '''posts per page of that listing (0: the module's), DownloaderJsonApi._items_per_page'''
    example: str = ''
    label: str = ''
    '''optional label instead of the app's own for the id'''
    use: str = ''
    '''fav | pool: the module's favorites / pool syntax (syntax.fav / syntax.pool), the token comes from there'''
    order: str = 'id'
    '''id (newest first, the default) | site: the site's own order (pool order...), handled like a sort tag'''


@dataclass
class SearchSpec:
    """Manifest 'search' (Lynxx 2.0.11+): {"kinds": [...], "metaNames": [...], "idSearch": true}. Unknown keys and kind ids are
    skipped (notes), so a later core can add some without a new gate"""
    kinds: list[SearchKind] = field(default_factory=list)
    metaNames: list[str] = field(default_factory=list)
    '''site metatag names (not tags): chips show them as metas, the core never moves them to the local filters'''
    idSearch: bool = True
    '''False: 'id:' searches do not work on this module (auto update's 'id:>N', selected posts, search by file name)'''
    notes: list[str] = field(default_factory=list)
    '''what was skipped while reading it (traced when the extension is loaded), not public'''

    def to_public_dict(self) -> dict:
        return {'kinds': [asdict(k) for k in self.kinds], 'metaNames': list(self.metaNames), 'idSearch': self.idSearch}

    @staticmethod
    def from_dict(d: object, syntax: TagSyntax) -> SearchSpec:
        if not isinstance(d, dict):
            raise ManifestError('\'search\' must be an object')
        spec = SearchSpec()
        spec.notes.extend(f'unknown search field \'{k}\' skipped' for k in d if k not in SEARCH_KEYS)
        if 'idSearch' in d:
            if not isinstance(d['idSearch'], bool):
                raise ManifestError('search.idSearch must be a boolean')
            spec.idSearch = d['idSearch']
        names = d.get('metaNames', [])
        if not isinstance(names, list) or not all(isinstance(x, str) and re_meta_name_decl.fullmatch(x) for x in names):
            raise ManifestError('search.metaNames must be a list of metatag names (^[a-z][a-z0-9_]{0,31}$)')
        if len(names) > SEARCH_META_NAMES_MAX:
            raise ManifestError(f'search.metaNames: at most {SEARCH_META_NAMES_MAX:d} names')
        spec.metaNames = list(dict.fromkeys(names))
        kinds = d.get('kinds', [])
        if not isinstance(kinds, list):
            raise ManifestError('search.kinds must be a list')
        if len(kinds) > SEARCH_KINDS_MAX:
            raise ManifestError(f'search.kinds: at most {SEARCH_KINDS_MAX:d} kinds')
        for i, raw in enumerate(kinds):
            if (kind := SearchSpec._kind(i, raw, syntax, spec.notes)) is None:
                continue
            if any(k.id == kind.id for k in spec.kinds):
                raise ManifestError(f'search.kinds: more than one kind with id \'{kind.id}\'')
            if kind.token and any(k.token == kind.token for k in spec.kinds):
                raise ManifestError(f'search.kinds: more than one kind with token \'{kind.token}\'')
            spec.kinds.append(kind)
        return spec

    @staticmethod
    def _kind(i: int, raw: object, syntax: TagSyntax, notes: list[str]) -> SearchKind | None:
        where = f'search.kinds[{i:d}]'
        if not isinstance(raw, dict):
            raise ManifestError(f'{where} must be an object')
        for k in ('id', 'token', 'value', 'input', 'tagType', 'example', 'label', 'use', 'order'):
            if k in raw and not isinstance(raw[k], str):
                raise ManifestError(f'{where}.{k} must be a string')
        if 'listing' in raw and not isinstance(raw['listing'], bool):
            raise ManifestError(f'{where}.listing must be a boolean')
        size = raw.get('pageSize', 0)
        if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= SEARCH_PAGE_SIZE_MAX:
            raise ManifestError(f'{where}.pageSize must be an integer from 0 to {SEARCH_PAGE_SIZE_MAX:d}')
        kid = raw.get('id', '')
        if not kid:
            raise ManifestError(f'{where}: missing id')
        if kid not in SEARCH_KIND_IDS:
            notes.append(f'{where}: unknown kind id \'{kid}\' skipped')
            return None
        notes.extend(f'{where}: unknown field \'{k}\' skipped' for k in raw if k not in SEARCH_KIND_KEYS)
        kind = SearchKind(id=kid, input=raw.get('input', 'tag'), tagType=raw.get('tagType', ''), listing=raw.get('listing', False),
                          pageSize=size, example=raw.get('example', '').strip()[:80], label=raw.get('label', '').strip()[:40],
                          use=raw.get('use', ''), order=raw.get('order', 'id'))
        for k, allowed in (('input', SEARCH_KIND_INPUTS), ('use', ('', 'fav', 'pool')), ('order', ('id', 'site'))):
            if getattr(kind, k) not in allowed:
                notes.append(f'{where} ({kid}): unknown {k} \'{getattr(kind, k)}\', kind skipped')
                return None
        if kind.tagType and kind.tagType not in SEARCH_TAG_TYPES:
            notes.append(f'{where} ({kid}): unknown tagType \'{kind.tagType}\' ignored')
            kind.tagType = ''
        token, value = raw.get('token', ''), raw.get('value', '')
        if kind.use:
            if kind.listing:
                raise ManifestError(f'{where} ({kid}): a kind with "use" cannot be a listing')
            rex = syntax.fav if kind.use == 'fav' else syntax.pool
            if not rex:
                notes.append(f'{where} ({kid}): the module has no {kind.use} syntax, kind skipped')
                return None
            kind.token = f'{"favorited_by" if kind.use == "fav" else "pool"}{syntax.idSep}'
            if token and token != kind.token:
                notes.append(f'{where} ({kid}): token \'{token}\' replaced by \'{kind.token}\' (from syntax.{kind.use})')
            kind.value = rex[len(kind.token):] if rex.startswith(kind.token) else ''
            return SearchSpec._example_value(kind, where, notes)
        if not token:
            if not kind.tagType or kind.listing or kind.input != 'tag':
                raise ManifestError(f'{where} ({kid}): a kind without token is a plain tag of \'tagType\' (input tag, not a listing)')
            if value:
                raise ManifestError(f'{where} ({kid}): \'value\' needs a token')
            return kind
        if not re_kind_token.fullmatch(token):
            raise ManifestError(f'{where} ({kid}): invalid token \'{token}\' (expected ^[a-z][a-z_]{{0,23}}[:=]$, ex. \'artist:\')')
        kind.token = token
        kind.value = value or (r'\d+' if kind.input == 'id' else r'\S+')
        if len(kind.value) > SEARCH_VALUE_MAX_LEN:
            raise ManifestError(f'{where} ({kid}): value regex longer than {SEARCH_VALUE_MAX_LEN:d} characters')
        try:
            re.compile(f'^-?{re.escape(token)}(?:{kind.value})$')
        except re.error as err:
            raise ManifestError(f'{where} ({kid}): value is not a valid regex: {err}')
        if why := unsafe_regex_reason(kind.value):
            raise ManifestError(f'{where} ({kid}): unsafe value regex: {why}')
        return SearchSpec._example_value(kind, where, notes)

    @staticmethod
    def _example_value(kind: SearchKind, where: str, notes: list[str]) -> SearchKind:
        """'example' is the value alone (the app shows '<token><example>'): a leading token is dropped"""
        if kind.token and kind.example.lower().startswith(kind.token):
            notes.append(f'{where} ({kind.id}): example \'{kind.example}\' holds the token, \'{kind.token}\' dropped')
            kind.example = kind.example[len(kind.token):]
        return kind


@dataclass
class ExtensionManifest:
    id: str
    name: str
    abbr: str
    version: str
    entry: str
    apiVersion: int = MANIFEST_API_VERSION
    kind: str = 'downloader'
    default: bool = False
    site: str = ''
    media: list[str] = field(default_factory=lambda: ['image', 'video'])
    tagList: str = ''
    description: str = ''
    homepage: str = ''
    auth: dict = field(default_factory=dict)
    syntax: TagSyntax = field(default_factory=TagSyntax)
    sha256: str = ''
    genderRules: str = ''
    '''optional gender ruleset file inside the extension folder (lynxx/gender_rules/*.json format)'''
    safetyTags: list[str] = field(default_factory=list)
    '''optional extra tags for the safety blacklist (can only add)'''
    minSdkVersion: str = ''
    '''lowest lynxx.sdk version ('MAJOR.MINOR') the extension needs; checked when it is installed and loaded'''
    tagUpdate: dict = field(default_factory=dict)
    '''the extension's own tag list update, {"entry": "tags.py:read_tags"}: a function of a .py file at the top of the extension folder,
    called with a lynxx.sdk.TagJob (SDK 1.1) only when the user asks (lynxx/tags_updater.py, EXTENSION_JOB). Optional
    "merge": {"minPosts": N} (Lynxx 2.0.10+): the function reads only the tags with N or more posts, every one of them, and Lynxx
    merges them into the list in use instead of replacing it (TagSource.merge)'''
    search: SearchSpec = field(default_factory=SearchSpec)
    '''search kinds, site metatag names, id search (Lynxx 2.0.11+: older cores refuse a manifest with 'search', MANIFEST_KEYS_SINCE)'''
    # runtime (not part of the file)
    generator: dict = field(default_factory=dict)
    '''origin of a generated extension, from the '$generator' key (lynxx.extgen / lynxx.aigen): shown as a badge (rule R13)'''
    path: pathlib.Path | None = None
    source: str = ''
    '''builtin | catalog | user'''

    @staticmethod
    def load(manifest_path: pathlib.Path, source: str) -> ExtensionManifest:
        try:
            raw = json.loads(manifest_path.read_text(encoding=UTF8))
        except Exception as err:
            raise ManifestError(f'{manifest_path}: unable to read manifest: {err}')
        return ExtensionManifest.from_dict(raw, manifest_path.parent, source)

    @staticmethod
    def from_dict(raw: dict, path: pathlib.Path | None, source: str) -> ExtensionManifest:
        try:
            return ExtensionManifest._from_dict(raw, path, source)
        except ManifestError:
            raise
        except Exception as err:  # any malformed value must surface as ManifestError (never crash the loader)
            raise ManifestError(f'malformed manifest: {err}')

    @staticmethod
    def _check_types(raw: dict) -> None:
        for k in ('id', 'name', 'abbr', 'version', 'entry', 'kind', 'site', 'tagList', 'description', 'homepage', 'sha256', 'genderRules',
                  'minSdkVersion'):
            if k in raw and not isinstance(raw[k], str):
                raise ManifestError(f'\'{k}\' must be a string')
        if 'apiVersion' in raw and (not isinstance(raw['apiVersion'], int) or isinstance(raw['apiVersion'], bool)):
            raise ManifestError('\'apiVersion\' must be an integer')
        if 'default' in raw and not isinstance(raw['default'], bool):
            raise ManifestError('\'default\' must be a boolean')
        for k in ('media', 'safetyTags'):
            if k in raw and (not isinstance(raw[k], list) or not all(isinstance(x, str) for x in raw[k])):
                raise ManifestError(f'\'{k}\' must be a list of strings')
        if 'auth' in raw and not isinstance(raw['auth'], dict):
            raise ManifestError('\'auth\' must be an object')
        if 'tagUpdate' in raw and not isinstance(raw['tagUpdate'], dict):
            raise ManifestError('\'tagUpdate\' must be an object')

    @staticmethod
    def _from_dict(raw: dict, path: pathlib.Path | None, source: str) -> ExtensionManifest:
        if not isinstance(raw, dict):
            raise ManifestError('manifest must be a json object')
        ExtensionManifest._check_types(raw)
        required = ('id', 'name', 'abbr', 'version', 'entry')
        if missing := [k for k in required if not raw.get(k)]:
            raise ManifestError(f'missing required field(s): {", ".join(missing)}')
        known = set(ExtensionManifest.__dataclass_fields__) - {'path', 'source', 'generator'}
        if unknown := [k for k in raw if k not in known and not k.startswith('$')]:
            raise ManifestError(f'unknown field(s): {", ".join(unknown)}')
        m = ExtensionManifest(**{k: v for k, v in raw.items() if k in known and k not in ('syntax', 'search')})
        m.syntax = TagSyntax.from_dict(raw.get('syntax'))
        if 'search' in raw:
            m.search = SearchSpec.from_dict(raw['search'], m.syntax)
        gen = raw.get('$generator')
        if isinstance(gen, dict):  # only short plain strings of known keys: it is displayed, never interpreted
            m.generator = {k: str(gen[k])[:80] for k in ('tool', 'mode', 'model', 'engine', 'createdAt', 'session')
                           if isinstance(gen.get(k), (str, int, float)) and str(gen.get(k)).strip()}
        m.path, m.source = path, source
        m.validate()
        return m

    def validate(self) -> None:
        if not re_ext_id.fullmatch(self.id):
            raise ManifestError(f'invalid id \'{self.id}\'')
        if not re_abbr.fullmatch(self.abbr):
            raise ManifestError(f'invalid abbr \'{self.abbr}\' (2-4 lowercase letters/digits)')
        if not re_version.fullmatch(self.version):
            raise ManifestError(f'invalid version \'{self.version}\' (semver expected)')
        if not re_entry.fullmatch(self.entry):
            raise ManifestError(f'invalid entry \'{self.entry}\' (expected \'downloader.py:ClassName\' or \'package.module:ClassName\')')
        if self.apiVersion != MANIFEST_API_VERSION:
            raise ManifestError(f'unsupported apiVersion {self.apiVersion} (supported: {MANIFEST_API_VERSION})')
        if self.kind != 'downloader':
            raise ManifestError(f'unsupported kind \'{self.kind}\'')
        if bad := [m for m in self.media if m not in KNOWN_MEDIA]:
            raise ManifestError(f'unknown media type(s): {", ".join(bad)}')
        if self.minSdkVersion and not re_min_sdk.fullmatch(self.minSdkVersion):
            raise ManifestError(f'invalid minSdkVersion \'{self.minSdkVersion}\' (expected \'MAJOR.MINOR\', ex. \'1.0\')')
        if self.tagUpdate:
            if unknown := [k for k in self.tagUpdate if k not in TAG_UPDATE_KEYS]:
                raise ManifestError(f'unknown tagUpdate field(s): {", ".join(map(str, unknown))}')
            if not isinstance(entry := self.tagUpdate.get('entry'), str) or not re_tag_update_entry.fullmatch(entry):
                raise ManifestError('invalid tagUpdate.entry (expected \'tags.py:function_name\', a file at the top of the extension folder)')
            if 'merge' in self.tagUpdate:
                merge = self.tagUpdate['merge']
                n = merge.get('minPosts') if isinstance(merge, dict) else None
                lo, hi = TAG_MERGE_MIN_POSTS
                if (not isinstance(merge, dict) or set(merge) != {'minPosts'} or not isinstance(n, int) or isinstance(n, bool)
                        or not lo <= n <= hi):
                    raise ManifestError(f'invalid tagUpdate.merge (expected {{"minPosts": N}}, N from {lo:d} to {hi:d}: the update reads '
                                        f'every tag with at least N posts)')

    @property
    def is_file_entry(self) -> bool:
        return self.entry.split(':', 1)[0].endswith('.py')

    def to_public_dict(self) -> dict:
        d = asdict(self)
        d['path'] = self.path.as_posix() if self.path else ''
        d['search'] = self.search.to_public_dict()
        return d

#
#
#########################################
