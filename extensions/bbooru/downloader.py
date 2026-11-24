# coding=UTF-8
"""
Lynxx extension 'bbooru' (bb). Originally part of the core.
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

import base64
import re
from typing import Never, final

from lynxx.defines import (
    FILE_NAME_PREFIX_BB,
    ID_VALUE_SEPARATOR_CHAR_BB,
    ITEMS_PER_PAGE_BB,
    MODULE_ABBR_BB,
    SITENAME_B_BB,
    TAGS_CONCAT_CHAR_BB,
    Comment,
    ModuleCaps,
)
from lynxx.download_gelbooru import DownloaderGelbooru
from lynxx.logger import trace
from lynxx.network import thread_exit
from lynxx.rex import (
    re_post_page_bb,
    re_tags_exclude_bb,
    re_tags_to_process_bb,
)

__all__ = ('DownloaderBb',)

SITENAME = base64.b64decode(SITENAME_B_BB).decode()
ITEMS_PER_PAGE = ITEMS_PER_PAGE_BB
ITEMS_PER_PAGE_F = 50
ITEMS_PER_PAGE_P = 20000  # unknown, all posts are on a single page always
MAX_SEARCH_DEPTH = 200000 + ITEMS_PER_PAGE - 1  # set by site devs
HTML_SEARCH_REFUSED = ('{} searches are not supported: they read html pages of the site, and its terms allow automated '
                       'access through the API only')


@final
class DownloaderBb(DownloaderGelbooru):
    """
    DownloaderBb
    """
    CAPABILITIES = frozenset((ModuleCaps.PARENT_CHILD, ModuleCaps.SOURCES, ModuleCaps.DATE_FILTER, ModuleCaps.PREVIEW, ModuleCaps.GENDER_TAGS))

    def _get_page_fetch_delay(self) -> float:
        return 1.0

    def __init__(self) -> None:
        super().__init__()

    def _get_id_bounds(self) -> Never:
        raise NotImplementedError

    def _form_item_string_manually(self, *ignored) -> Never:
        raise NotImplementedError

    def _get_re_post_page(self) -> re.Pattern:
        return re_post_page_bb

    def _get_sitename(self) -> str:
        return SITENAME

    def _get_min_request_interval(self) -> float:
        return 1.0  # ToS allows automated access through the API only, "without hammering"

    def _extract_favorite_user(self, fav_user_tags: list[re.Match | None]) -> None:
        if fav_user_tags:  # favorites are html pages, not the API
            thread_exit(HTML_SEARCH_REFUSED.format('favorited_by:'), code=-460)
        super()._extract_favorite_user(fav_user_tags)

    def _extract_pool_id(self, pool_tags: list[re.Match]) -> None:
        if pool_tags:  # pools are html pages, not the API
            thread_exit(HTML_SEARCH_REFUSED.format('pool:'), code=-461)
        super()._extract_pool_id(pool_tags)

    def _get_module_abbr(self) -> str:
        return MODULE_ABBR_BB

    def _get_module_abbr_p(self) -> str:
        return FILE_NAME_PREFIX_BB

    def _get_items_per_page(self) -> int:
        return ITEMS_PER_PAGE_F if self.favorites_search_user else ITEMS_PER_PAGE_P if self.pool_search_str else ITEMS_PER_PAGE

    def _get_max_search_depth(self) -> int:
        return MAX_SEARCH_DEPTH

    def get_re_tags_to_process(self) -> re.Pattern:
        return re_tags_to_process_bb

    def get_re_tags_to_exclude(self) -> re.Pattern:
        return re_tags_exclude_bb

    def _get_tags_concat_char(self) -> str:
        return TAGS_CONCAT_CHAR_BB

    def _get_idval_equal_seaparator(self) -> str:
        return ID_VALUE_SEPARATOR_CHAR_BB

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

#
#
#########################################
