# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

import base64
import re
from typing import Never, final

from requests import exceptions

from .defines import (
    FILE_NAME_PREFIX_RX,
    ID_VALUE_SEPARATOR_CHAR_RX,
    ITEMS_PER_PAGE_RX,
    MODULE_ABBR_RX,
    RATE_LIMIT_RX,
    SITENAME_B_RX,
    TAGS_CONCAT_CHAR_RX,
    Comment,
    ModuleCaps,
)
from .download_gelbooru import DownloaderGelbooru
from .logger import trace
from .network import key_rate_interval
from .rex import (
    re_post_page_rx,
    re_tags_exclude_rx,
    re_tags_to_process_rx,
)

__all__ = ('DownloaderRx',)

SITENAME = base64.b64decode(SITENAME_B_RX).decode()
ITEMS_PER_PAGE = ITEMS_PER_PAGE_RX
ITEMS_PER_PAGE_F = 50
ITEMS_PER_PAGE_P = 45
MAX_SEARCH_DEPTH_HTML = 200000 + 42 - 1  # set by site devs
MAX_SEARCH_DEPTH_DAPI = 200000  # in case of API abuse this limit is set and we cannot predict when this happens
MAX_SEARCH_DEPTH = MAX_SEARCH_DEPTH_DAPI



@final
class DownloaderRx(DownloaderGelbooru):
    """
    DownloaderRx
    """
    CAPABILITIES = frozenset((ModuleCaps.API_KEY, ModuleCaps.API_KEY_REQUIRED, ModuleCaps.PARENT_CHILD, ModuleCaps.SOURCES, ModuleCaps.DATE_FILTER, ModuleCaps.COMMENTS, ModuleCaps.PREVIEW, ModuleCaps.GENDER_TAGS))

    def __init__(self) -> None:
        super().__init__()
        self._base_cookies = {'gdpr-consent': '1', 'gdpr': '1'}

    def _get_module_specific_default_headers(self) -> dict[str, str]:
        return {}

    def _get_module_specific_default_cookies(self) -> dict[str, str]:
        return self._base_cookies

    def _get_id_bounds(self) -> Never:
        raise NotImplementedError

    def _use_chunked_download(self, is_video_ext: bool) -> bool:
        return is_video_ext

    def _fix_link_after_download_error(self, link: str, original_link: str, err: Exception) -> str:
        if isinstance(err, exceptions.HTTPError) and err.response is not None and err.response.status_code == 404 and link != original_link:
            trace(f'Warning (W3): catched HTTPError 404 for normalized link. Switching to slow link \'{original_link}\'...', True)
            return original_link
        return link

    def _form_item_string_manually(self, *ignored) -> Never:
        raise NotImplementedError

    def _get_re_post_page(self) -> re.Pattern:
        return re_post_page_rx

    def _get_min_request_interval(self) -> float:
        # the API allows each key RATE_LIMIT_RX (more when the site's admins gave the user's key more: Settings > API keys). Every process
        # using the key keeps this pace together (shared_rate_wait): download jobs, Preview, the tag list updater. Favorites and pools
        # are html pages of the main site, not the API
        return key_rate_interval(MODULE_ABBR_RX, RATE_LIMIT_RX) if self._get_sitename() == SITENAME else 0.0

    def _get_sitename(self) -> str:
        return SITENAME.replace('api.', '') if self.favorites_search_user or self.pool_search_str else SITENAME

    def _get_module_abbr(self) -> str:
        return MODULE_ABBR_RX

    def _get_module_abbr_p(self) -> str:
        return FILE_NAME_PREFIX_RX

    def _get_items_per_page(self) -> int:
        return ITEMS_PER_PAGE_F if self.favorites_search_user else ITEMS_PER_PAGE_P if self.pool_search_str else ITEMS_PER_PAGE

    def _get_max_search_depth(self) -> int:
        return MAX_SEARCH_DEPTH

    def get_re_tags_to_process(self) -> re.Pattern:
        return re_tags_to_process_rx

    def get_re_tags_to_exclude(self) -> re.Pattern:
        return re_tags_exclude_rx

    def _get_tags_concat_char(self) -> str:
        return TAGS_CONCAT_CHAR_RX

    def _get_idval_equal_seaparator(self) -> str:
        return ID_VALUE_SEPARATOR_CHAR_RX

    def _extract_comments(self, item_id: str) -> None:
        if raw_html := self.fetch_html(self._form_comments_search_address(item_id)):
            full_item_id = f'{self._get_module_abbr_p() if self.add_filename_prefix else ""}{item_id}'
            comment_divs = raw_html.html.find_all('comment')
            for comment_div in comment_divs:
                author = comment_div.get('creator')
                body = comment_div.get('body')
                self._item_info_for(full_item_id).comments.append(Comment(author, body))
        else:
            trace(f'Warning (W3): ProcItem: unable to retreive comments for {item_id}!', True)

    def _get_api_key(self) -> str:
        # the user's own key only: Lynxx ships no shared key (a run without one stops in _solve_argument_conflicts)
        return f'&api_key={self.api_key.key}&user_id={self.api_key.user_id}' if self.api_key else ''

#
#
#########################################
