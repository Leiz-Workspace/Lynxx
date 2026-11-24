# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

import datetime

from .vcs import __LYNXX_DEBUG__, APP_NAME, APP_REV_DATE, APP_VERSION

APP_AUTHOR_NICKNAME = 'trickerer'
APP_AUTHOR = f'Dmitriy \'{APP_AUTHOR_NICKNAME}\' Komardin'
APP_ADDRESS = 'github.com/trickerer01'  # author of the original work (MIT), Lynxx is a derivative work
APP_YEARS = f'2020-{datetime.date.today().year}'
APP_BUILD_TYPE_STR = 'Debug' if __LYNXX_DEBUG__ else 'Release'

# cmdline args help messages
HELP_ARG_HELP = 'Print this message'
HELP_ARG_VERSION = 'Show program version string and exit'
HELP_ARG_MODULE = 'Download module to use, default is \'rx\''
HELP_ARG_DOWNLOAD_MODE = '[Debug] Download mode, default is \'full\''
HELP_ARG_DOWNLOAD_LIMIT = 'Maximum number of posts to download, default is \'0\' (disabled)'
HELP_ARG_TAGS_LIMIT = ('Maximum tags per search allowed by your account, overrides the module default '
                       '(ex. danbooru: 2 for free accounts, 6 Gold, 12 Platinum)')
HELP_ARG_REVERSE_DOWNLOAD_ORDER = 'Download in reverse order (highest id to lowest, unless changed by sort:X type tags)'
HELP_ARG_SKIP_IMAGES = 'Skip all images'
HELP_ARG_SKIP_VIDEOS = 'Skip all videos'
HELP_ARG_SKIP_AUDIO = 'Skip all audio files (modules with audio media)'
HELP_ARG_HIDE_PERSONAL_INFO = 'Mask API key, proxy and other sensitive info in logs'
HELP_ARG_INCLUDE_PARCHI = 'Include all parent posts and all child posts (recursive, downloads whole chain, no filters)'
HELP_ARG_GET_MAXID = 'Print maximum id (for module) and exit'
HELP_ARG_PREFER_WEBM = 'Prefer webm over mp4 when possible'
HELP_ARG_PREFER_MP4 = 'Prefer mp4 over webm when possible'
HELP_ARG_PREFER_LOWRES = 'Prefer low resolution images over the originals (images only: the video file is chosen by -max_vres)'
HELP_ARG_MAX_VIDEO_HEIGHT = ('Highest video height to download, ex. 720: the best video file not higher than that, else the smallest '
                             'one. Default is \'0\' (the best file). Modules offering a single video file ignore it')
HELP_ARG_PICK_PAGE = ('Picked posts: index of the results page they were picked on (0 is the first page), as the desktop Preview '
                      'shows it. Needs -pick_ids')
HELP_ARG_PICK_SIZE = 'Picked posts: page size of that results page, as the Preview reported it. Default: the Preview\'s default'
HELP_ARG_PICK_IDS = ('Picked posts: comma separated post ids (at most 1000) to download from that results page (and up to 2 '
                     'pages after it, newer posts push the others down). Date, gender, required / excluded tag filters and '
                     '-dlimit do not apply to them; media type, -skip_known, -safety_blacklist and existing files do')
HELP_ARG_PRESERVE_DATE = 'Preserve original modification date for saved files'
HELP_ARG_GENDER = ('Keep only posts with these character gender labels (comma separated): '
                   'only_female,only_male,both,none,unknown. Labels come from post tags')
HELP_ARG_GENDER_EXCLUDE = ('Drop posts with these character gender labels and keep all the others (comma separated, same labels '
                           'as -gender, not all of them). Ex. robot -gender_exclude only_male: robots shown female only, both, '
                           'with no character or unknown. With -gender: the labels of -gender minus these')
HELP_ARG_GENDER_MODE = ('Gender labeling mode: \'tags\' (default) or \'model\': posts whose tags are not conclusive are looked at '
                        'by an image model, through a model server (-gender_model_url) or in this process '
                        '(pip install "lynxx[model]", python -m lynxx.gender_model install, ~380 MB)')
HELP_ARG_GENDER_MODEL_URL = ('Address of the gender model server (python -m lynxx.model_server), ex. http://127.0.0.1:8765. '
                             'Default: environment variable LYNXX_GENDER_MODEL_URL, else the model in this process')
HELP_ARG_GENDER_MODEL_TOKEN = 'Token of the gender model server (default: environment variable LYNXX_GENDER_MODEL_TOKEN)'
HELP_ARG_GENDER_CONF = 'Minimal label confidence, lower confidence labels count as \'unknown\'. Default is \'medium\''
HELP_ARG_GENDER_STRICT = 'Posts with futanari, trap or ambiguous characters never count as only_female / only_male'
HELP_ARG_GENDER_VERIFY_MEDIA = ('With -gender_mode model: each downloaded video / animated picture whose tags were not conclusive '
                                'is checked again on up to 8 of its frames (model server: POST /v1/frames, ffmpeg or PyAV for '
                                'videos). A file that does not match the gender filter is moved into the \'_gender_mismatch\' '
                                'subfolder of the destination, never deleted')
HELP_ARG_SKIP_KNOWN = ('Skip posts already in the local download index (same md5, or same module and id) whose file is still on disk. '
                       'Default is \'off\'')
HELP_ARG_SAFETY_BLACKLIST = ('Drop posts whose tags suggest minors (fixed list of lynxx/safety.py, extensions may only add tags), '
                             'in previews and downloads; with -gender_mode model the model\'s minor coded tags block posts too. '
                             'Independent of -gender. Default is \'off\'')
HELP_ARG_MINDATE = 'Skip everything posted before this date, default is \'01-01-1970\''
HELP_ARG_MAXDATE = 'Skip everything posted after this date, default is \'<today>\''
HELP_ARG_THREADS = 'Maximum simultaneous downloads (affects pages scan too), default is \'1\''
HELP_ARG_PATH = 'Full path to destination folder, default is \'<current folder>\''
HELP_ARG_SUBFOLDER_VIDEOS = 'Subfolder name to download videos into'
HELP_ARG_SUBFOLDER_IMAGES = 'Subfolder name to download images into'
HELP_ARG_SUBFOLDER_AUDIO = 'Subfolder name to download audio files into'
HELP_ARG_PROXY = 'Proxy server address'
HELP_ARG_NOPROXY = 'Ignore proxy during this run'
HELP_ARG_PROXYNODOWN = 'Do not use proxy for downloads, only for search'
HELP_ARG_CON_TIMEOUT = 'Connection timeout (in seconds), default is \'10\''
HELP_ARG_CON_RETRIES = 'Connection retries count is case of fail, default is \'10\''
HELP_ARG_API_KEY = 'API authentication info'
HELP_ARG_HEADERS = 'Append additional headers: \'{"name1": "value1", "name2": "value2", etc.}\''
HELP_ARG_COOKIES = 'Append additional cookies: \'{"name1": "value1", "name2": "value2", etc.}\''
HELP_ARG_HEADER = 'Append additional header. May override -headers values. Example: user_agent=googlebot/1.1. Can be used multiple times'
HELP_ARG_COOKIE = 'Append additional cookie. May override -cookies values. Example: shm_user=user1. Can be used multiple times'
HELP_ARG_PREFIX = 'Use module prefix in file names'
HELP_ARG_DUMP_TAGS = 'Save all tags to text file'
HELP_ARG_DUMP_SOURCES = 'Save all found source links to text file'
HELP_ARG_DUMP_COMMENTS = 'Save all found comments to text file'
HELP_ARG_DUMP_HASHES = 'Save all collected md5 hashes to text file'
HELP_ARG_DUMP_PER_ITEM = 'Create tags/sources/comments files for each downloaded file'
HELP_ARG_MERGE_LISTS = 'Merge exising tags/sources/comments files(s) with saved info (only if saving is enabled)'
HELP_ARG_APPEND_SOURCE_AND_TAGS = 'Append a shortened representation of major tags to file name'
HELP_ARG_WARN_NON_EMPTY_FOLDER = 'In GUI mode, warn if download folder is not empty'
HELP_ARG_VERBOSE = 'Enable verbose logging'
HELP_ARG_TAGS = 'Tags to search for, required'
# GUI help messages
HELP_TAGS_MSG_RX = (
    'Here is a quick list of supported tag patterns:\n\n'
    '\'tag1\'           \tplain text tag\n'
    '\'ta*\'            \ttag starting with \'ta\'\n'
    '\'t*1\'            \ttag starting with \'t\' and ending with \'1\'\n'
    '\'-tag1\'          \texclude tag \'tag1\'\n'
    '\'tag1 tag2\'      \ttag1 AND tag2\n'
    '\'(tag1~tag2)\'    \ttag1 OR tag2 (can be chained)\n'
    '\'-(tag1,tag2)\'   \tNOT BOTH tag1 AND tag2 (can be chained)\n'
    '\'tag:value\'      \tmeta tags (id:999999, score:>=1337, etc.)\n'
)
HELP_TAGS_MSG_RN = (
    'Here is a quick list of supported tag patterns:\n\n'
    '\'tag1\'           \tplain text tag\n'
    '\'ta*\'            \ttag starting with \'ta\'\n'
    '\'t*1\'            \ttag starting with \'t\' and ending with \'1\'\n'
    '\'-tag1\'          \texclude tag \'tag1\'\n'
    '\'tag1 tag2\'      \ttag1 AND tag2\n'
    '\'(tag1~tag2)\'    \ttag1 OR tag2 (can be chained)\n'
    '\'-(tag1,tag2)\'   \tNOT BOTH tag1 AND tag2 (can be chained)\n'
    '\'tag=value\'      \tmeta tags (id<999999, score>=1337, etc.)\n'
)
HELP_TAGS_MSG_RS = HELP_TAGS_MSG_RX
HELP_TAGS_MSG_RP = HELP_TAGS_MSG_RN
HELP_TAGS_MSG_EN = (
    'Here is a quick list of supported tag patterns:\n\n'
    '\'tag1\'           \tplain text tag\n'
    '\'ta*\'            \ttag starting with \'ta\'\n'
    '\'*ag1\'           \ttag ending with \'ag1\'\n'
    '\'t*1\'            \ttag starting with \'t\' and ending with \'1\'\n'
    '\'-tag1\'          \texclude tag \'tag1\'\n'
    '\'tag1 tag2\'      \ttag1 AND tag2\n'
    '\'(tag1~tag2)\'    \ttag1 OR tag2 (can be chained)\n'
    '\'-(tag1,tag2)\'   \tNOT BOTH tag1 AND tag2 (can be chained)\n'
    '\'tag:value\'      \tmeta tags (id:999999, score:>=1337, etc.)\n'
)
HELP_TAGS_MSG_XB = HELP_TAGS_MSG_RX
HELP_TAGS_MSG_BB = HELP_TAGS_MSG_RX
HELP_TAGS_MSG_DB = (
    'Danbooru: anonymous users can search at most 2 tags at once (negative tags and order: count too).\n'
    'Free metatags (do not count): rating:, id:, date:, score:, status:, parent:.\n'
    'Extra positive tags are checked locally (rarest tags are sent to the website), extra -tags become a local negative group.\n'
    'Meta syntax: id:>X, id:X..Y, score:>=X, date:2024-01-01..2024-02-01, rating:g,s, order:score.\n'
    'The usual syntax for OR groups (a~b) and negative groups -(a,b) is supported.'
)
ABOUT_MSG = (f'{APP_NAME} version {APP_VERSION} ({APP_REV_DATE}) {APP_BUILD_TYPE_STR}\n'
             f'\N{copyright sign} {APP_YEARS} {APP_AUTHOR}\n<{APP_ADDRESS}>')
# end help messages

#
#
#########################################
