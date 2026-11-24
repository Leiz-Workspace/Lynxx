# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

import datetime
import json
import pathlib
import re
from abc import ABC, abstractmethod
from argparse import ArgumentError, ArgumentTypeError
from ipaddress import ip_address

from .defines import API_KEY_LEN_RX, DMODE_CHOICES, FMT_DATE, PICK_IDS_MAX, THREADS_MAX_ITEMS
from .gui_defines import (
    OPTION_VALUES_IMAGES,
    OPTION_VALUES_PARCHI,
    OPTION_VALUES_PROXYTYPE,
    OPTION_VALUES_THREADING,
    OPTION_VALUES_VIDEOS,
)
from .module import ProcModule
from .utils import sanitize_path_name

# pure annotations
if False is True:
    from tkinter import Tk

__all__ = (
    'APIKeyKeyValidator',
    'APIKeyUserIdValidator',
    'BoolStrValidator',
    'DateValidator',
    'DummyValidator',
    'FolderPathValidator',
    'ImagesCBValidator',
    'InfoSaveModeValidator',
    'JsonValidator',
    'ModuleValidator',
    'ParchiCBValidator',
    'ProxyTypeValidator',
    'ProxyValidator',
    'RetriesValidator',
    'ThreadsCBValidator',
    'TimeoutValidator',
    'Validator',
    'VideosCBValidator',
    'WindowPosValidator',
    'module_id_from_setting',
    'valid_api_key',
    'valid_date',
    'valid_download_mode',
    'valid_folder_name',
    'valid_folder_path',
    'valid_id_list',
    'valid_json',
    'valid_kwarg',
    'valid_positive_int',
    'valid_proxy',
    'valid_thread_count',
    'valid_window_position',
)


re_login_name = re.compile(r'^[\w.\-]{1,64}$')
re_post_id_token = re.compile(r'[A-Za-z0-9_-]{1,64}')
'''a post id as the Preview gives it (digits on most sites, a word on a few)'''
re_hostname = re.compile(r'^(?=.{1,253}$)(?:[a-z\d](?:[a-z\d\-]{0,61}[a-z\d])?\.)*[a-z\d](?:[a-z\d\-]{0,61}[a-z\d])?$', re.IGNORECASE)


def valid_proxy_type(ptype: str) -> str:
    try:
        assert ptype in OPTION_VALUES_PROXYTYPE
        return ptype
    except Exception:
        raise ArgumentError


def valid_proxy(prox: str, with_type=True) -> str:
    """Accepts 'type://[user:pass@]host:port' where host is IPv4, [IPv6] or a hostname.
    socks5h:// resolves hostnames on the proxy side (useful when local DNS is filtered)"""
    if len(prox) == 0:
        return prox
    try:
        from urllib import parse as url_parse
        if with_type is False:
            assert '://' not in prox
            url = url_parse.urlparse(f'{OPTION_VALUES_PROXYTYPE[0]}://{prox}')
        else:
            url = url_parse.urlparse(prox)
        _ = valid_proxy_type(url.scheme)
        host = url.hostname or ''
        try:
            _ = ip_address(host)
        except ValueError:
            assert re_hostname.fullmatch(host)
        assert 0 < url.port < 65536
        return prox
    except Exception:
        raise ArgumentTypeError('invalid proxy (expected type://[user:pass@]host:port)')


def valid_gender_list(val: str) -> list[str]:
    try:
        from .gender import GENDER_LABELS
        labels = [v.strip().lower() for v in val.split(',') if v.strip()]
        assert labels and all(lb in GENDER_LABELS for lb in labels)
        return list(dict.fromkeys(labels))
    except Exception:
        raise ArgumentError


def valid_gender_exclude(val: str) -> list[str]:
    """-gender_exclude: same labels as -gender, but not every label (nothing would be left)"""
    from .gender import GENDER_LABELS
    labels = valid_gender_list(val)
    if set(labels) >= set(GENDER_LABELS):
        raise ArgumentError
    return labels


def valid_gender_model_url(val: str) -> str:
    """-gender_model_url: http(s) address of a model server; plain http only to this computer or a local network"""
    from .gender_model import check_api_url
    try:
        return check_api_url(val)
    except ValueError:
        raise ArgumentError


def valid_gender_model_token(val: str) -> str:
    if not re.fullmatch(r'[\x21-\x7e]{8,256}', val or ''):
        raise ArgumentError
    return val


def valid_date(date: str) -> str:
    try:
        _ = datetime.datetime.strptime(date, FMT_DATE)
        return date
    except Exception:
        raise ArgumentError


def valid_json(json_str: str) -> dict[str, str]:
    """-headers / -cookies: a JSON object of strings. The original code removed every backslash before parsing: escaped quotes were rejected,
    '\\\\' was lost and '\\u00e9' became 'u00e9'. Shell quoted input ('{...}' / "{...}") is still accepted"""
    try:
        s = json_str.strip()
        try:
            val = json.loads(s)
        except ValueError:
            val = None
        if val is None and len(s) >= 2 and s[0] == s[-1] and s[0] in '\'"':
            val = json.loads(s[1:-1])
        elif isinstance(val, str):  # a json string holding the object
            val = json.loads(val)
        assert isinstance(val, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in val.items())
        return val
    except Exception:
        raise ArgumentTypeError('invalid JSON object')


def valid_kwarg(kwarg: str) -> tuple[str, str]:
    try:
        k, v = tuple(kwarg.split('=', 1))
        return k, v
    except Exception:
        raise ArgumentTypeError('invalid name=value pair')


def valid_positive_int(val: str, *, lb=0, ub=4294967295) -> int:
    try:
        val = int(val)
        assert lb <= val <= ub
        return val
    except Exception:
        raise ArgumentError


def valid_id_list(val: str) -> list[str]:
    """-pick_ids: comma separated post ids, 1..PICK_IDS_MAX after removing duplicates (order kept). Ids stay strings: they are
    compared with the ids of the listing as the Preview gives them"""
    ids = list(dict.fromkeys(v.strip() for v in str(val).split(',') if v.strip()))
    if not ids or len(ids) > PICK_IDS_MAX or not all(re_post_id_token.fullmatch(i) for i in ids):
        raise ArgumentTypeError(f'expected 1 to {PICK_IDS_MAX:d} comma separated post ids')
    return ids


def valid_thread_count(val: str) -> int:
    return valid_positive_int(val, lb=1, ub=THREADS_MAX_ITEMS)


def valid_folder_path(pathstr: str) -> pathlib.Path:
    try:
        newpath = pathlib.Path(pathstr.strip('\'"')).resolve()
        assert next(reversed(newpath.parents)).is_dir()
        return newpath
    except Exception:
        raise ArgumentError


def valid_folder_name(folder_name: str) -> str:
    try:
        assert sanitize_path_name(folder_name) == folder_name
        return folder_name
    except Exception:
        raise ArgumentError


def valid_download_mode(mode: str) -> str:
    try:
        assert mode in DMODE_CHOICES
        return mode
    except Exception:
        raise ArgumentError


def valid_window_position(val: str, tk: 'Tk') -> str:
    try:
        x, y = tuple(val.split('x', 1))
        assert 0 < int(x) + 25 < tk.winfo_screenwidth() and 0 < int(y) + 25 < tk.winfo_screenheight()
        return f'+{x}+{y}'
    except Exception:
        raise ArgumentError


def valid_api_key_key(key: str) -> str:
    try:
        if key:
            # RX keys are 128 alnum chars, other modules (ex. danbooru) use shorter keys
            assert 16 <= len(key) <= max(API_KEY_LEN_RX, 256)
            assert key.replace('_', '').replace('-', '').isalnum()
        return key
    except Exception:
        raise ArgumentTypeError('invalid API key')


def valid_api_key_userid(user_id: str) -> str:
    try:
        if user_id:
            # numeric user id (RX) or login name (danbooru)
            assert user_id.isnumeric() or re_login_name.fullmatch(user_id)
        return user_id
    except Exception:
        raise ArgumentTypeError('invalid API user id / login')


def valid_api_key(val: str) -> str:
    try:
        key, user_id = tuple(val.split(',', 1))
        _ = valid_api_key_key(key)
        _ = valid_api_key_userid(user_id)
        return f'{key},{user_id}'
    except Exception:
        raise ArgumentTypeError('invalid API key (expected KEY,USER_ID or API_KEY,LOGIN)')


class Validator(ABC):
    tk: 'Tk | None' = None

    @abstractmethod
    def __call__(self, val: int | str) -> bool: ...

    @abstractmethod
    def get_value_type(self) -> type: ...


class IntValidator(Validator):
    @abstractmethod
    def __call__(self, val: int) -> bool: ...

    def get_value_type(self) -> type:
        return int


class StrValidator(Validator):
    @abstractmethod
    def __call__(self, val: str) -> bool: ...

    def get_value_type(self) -> type:
        return str


class DummyValidator(IntValidator, StrValidator):
    def __call__(self, val: int | str) -> bool:
        return True

    def get_value_type(self) -> type:
        return str


def module_id_from_setting(val: str | int) -> int:
    """Module setting: abbreviation ('db'), or the legacy 0-based integer of the original configs (builtin modules only)"""
    s = str(val).strip().lower()
    if s in ProcModule.PROC_MODULES_BY_NAME:
        return ProcModule.PROC_MODULES_BY_NAME[s]
    if s.isdigit() and ProcModule.PROC_MODULE_MIN <= int(s) + 1 <= ProcModule.PROC_MODULE_MAX:
        return int(s) + 1
    raise ValueError(f'unknown module \'{val}\'')


class ModuleValidator(StrValidator):
    def __call__(self, val: str) -> bool:
        try:
            module_id_from_setting(val)
            return True
        except Exception:
            return False


class FolderPathValidator(StrValidator):
    def __call__(self, val: str) -> bool:
        try:
            _ = valid_folder_path(val)
            return True
        except Exception:
            return False


class VideosCBValidator(IntValidator):
    def __call__(self, val: int) -> bool:
        return 0 <= val <= len(OPTION_VALUES_VIDEOS) - 1


class ImagesCBValidator(IntValidator):
    def __call__(self, val: int) -> bool:
        return 0 <= val <= len(OPTION_VALUES_IMAGES) - 1


class ParchiCBValidator(IntValidator):
    def __call__(self, val: int) -> bool:
        return 0 <= val <= len(OPTION_VALUES_PARCHI) - 1


class ThreadsCBValidator(IntValidator):
    def __call__(self, val: int) -> bool:
        return 0 <= val <= len(OPTION_VALUES_THREADING) - 1


class DateValidator(StrValidator):
    def __call__(self, val: str) -> bool:
        try:
            _ = valid_date(val)
            return True
        except Exception:
            return False


class JsonValidator(StrValidator):
    def __call__(self, val: str) -> bool:
        try:
            _ = valid_json(val)
            return True
        except Exception:
            return False


class BoolStrValidator(StrValidator):
    def __call__(self, val: str) -> bool:
        return val in ('0', '1')


class ProxyValidator(StrValidator):
    def __call__(self, val: str) -> bool:
        try:
            _ = valid_proxy(val, False)
            return True
        except Exception:
            return False


class ProxyTypeValidator(StrValidator):
    def __call__(self, val: str) -> bool:
        try:
            _ = valid_proxy_type(val)
            return True
        except Exception:
            return False


class TimeoutValidator(StrValidator):
    def __call__(self, val: str) -> bool:
        try:
            _ = valid_positive_int(val, lb=3, ub=300)
            return True
        except Exception:
            return False


class RetriesValidator(StrValidator):
    def __call__(self, val: str) -> bool:
        try:
            _ = valid_positive_int(val, lb=5, ub=100)
            return True
        except Exception:
            return False


class WindowPosValidator(StrValidator):
    def __call__(self, val: str) -> bool:
        try:
            _ = valid_window_position(val, self.tk)
            return True
        except Exception:
            return False


class InfoSaveModeValidator(IntValidator):
    def __call__(self, val: int) -> bool:
        try:
            _ = valid_positive_int(str(val), ub=2)
            return True
        except Exception:
            return False


class APIKeyKeyValidator(StrValidator):
    def __call__(self, val: str) -> bool:
        try:
            _ = valid_api_key_key(val)
            return True
        except Exception:
            return False


class APIKeyUserIdValidator(StrValidator):
    def __call__(self, val: str) -> bool:
        try:
            _ = valid_api_key_userid(val)
            return True
        except Exception:
            return False

#
#
#########################################
