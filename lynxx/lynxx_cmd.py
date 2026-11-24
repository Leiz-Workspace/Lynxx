# coding=UTF-8
"""
Author: trickerer (https://github.com/trickerer, https://github.com/trickerer01)
"""
#########################################
#
#

import json
import os
import sys
import time
from multiprocessing.dummy import current_process
from threading import Thread

from .cmdargs import prepare_arglist
from .downloaders import get_new_downloader
from .logger import Logger
from .module import ProcModule
from .tagsdb import TagsDB
from .utils import ensure_compatibility

__all__ = ('run_cmd',)

ENV_IPC = 'LYNXX_IPC'
ENV_SECRET_ARGS = 'LYNXX_SECRET_ARGS'
'''JSON list of extra arguments (ex. ["-api_key", "KEY,USER"]), removed from the environment once read'''
'''desktop app mode: machine-readable progress lines on stdout, 'cancel' command on stdin'''
IPC_PROGRESS_PREFIX = '@@LYNXX_PROGRESS '


def _pop_secret_args() -> list[str]:
    raw = os.environ.pop(ENV_SECRET_ARGS, '')
    if not raw:
        return []
    try:
        extra = json.loads(raw)
        assert isinstance(extra, list) and all(isinstance(x, str) for x in extra)
        return extra
    except Exception:
        print(f'ERROR: {ENV_SECRET_ARGS} must be a JSON list of strings', file=sys.stderr)
        return []


def _start_ipc_threads(dwn) -> None:
    root = current_process()

    def report() -> None:
        last = ''
        while True:
            state = {
                'state': int(dwn.current_state), 'stateName': dwn.current_state.name,
                'task': dwn.current_task_num, 'tasks': len(dwn.tags_str_arr),
                'total': dwn.total_count, 'totalAll': dwn.total_count_all, 'processed': dwn.processed_count,
                'success': dwn.success_count, 'failed': dwn.fail_count, 'pages': dwn.total_pages if dwn.maxpage else 0,
                'cfChallenge': dwn.cf_challenge_hit, 'genderFiltered': dwn.gender_filtered_count, 'blocked': dwn.safety_blocked_count,
                'known': dwn.known_skipped_count, 'streaming': dwn.streaming_active, 'scanning': dwn.stream_scanning,
                'bytes': dwn.bytes_received, 'genderVerified': dwn.gender_verified_count, 'genderMismatch': dwn.gender_mismatch_count,
            }
            line = json.dumps(state, separators=(',', ':'))
            if line != last:
                with Logger.print_lock:
                    print(f'{IPC_PROGRESS_PREFIX}{line}', flush=True)
                last = line
            time.sleep(0.4)

    def watch_stdin() -> None:
        for line in sys.stdin:
            if line.strip() == 'cancel':
                root.killed = True
                break

    Thread(target=report, daemon=True, name='ipc-progress').start()
    Thread(target=watch_stdin, daemon=True, name='ipc-stdin').start()


EXIT_OK = 0
EXIT_ERROR = 1
'''fatal error, cloudflare challenge or every item failed (argparse errors exit with 2)'''
EXIT_PARTIAL = 3
'''finished, some items failed'''
EXIT_CANCELLED = 130


def run_exit_code(dwn) -> tuple[int, str]:
    """(exit code, error message) of a finished run"""
    if dwn.interrupted:
        return EXIT_CANCELLED, ''
    if dwn.launch_error:
        return EXIT_ERROR, dwn.launch_error
    if dwn.cf_challenge_hit and dwn.success_count == 0:
        return EXIT_ERROR, 'cloudflare challenge'
    if dwn.fail_count > 0:
        return (EXIT_PARTIAL, '') if dwn.success_count > 0 else (EXIT_ERROR, f'{dwn.fail_count:d} item(s) failed')
    return EXIT_OK, ''


def run_cmd(args: list[str]) -> int:
    """Returns the process exit code: 0 ok, 1 error, 2 invalid arguments (argparse), 3 some items failed, 130 cancelled"""
    Logger.init(True)
    ensure_compatibility()
    current_process().killed = False
    ipc = os.environ.get(ENV_IPC) == '1'
    # API keys, cookies, headers, proxy credentials: passed by the desktop app in the environment, not in argv
    TagsDB.locate_default_lists()  # cheap: lists are only read when needed (tag limit overflow)
    secret_args = _pop_secret_args()
    arglist = prepare_arglist([*secret_args, *args])  # also loads extensions
    ProcModule.set_cur_module_by_name(arglist.module)
    try:
        cdwn_ctx = get_new_downloader()
    except KeyError as err:  # module extension is not installed
        msg = str(err.args[0] if err.args else err)
        print(f'ERROR: {msg}', flush=True)
        if ipc:
            final = {'state': 0, 'stateName': 'DONE', 'ok': False, 'exitCode': EXIT_ERROR, 'error': msg}
            print(f'{IPC_PROGRESS_PREFIX}{json.dumps(final, separators=(",", ":"))}', flush=True)
        return EXIT_ERROR
    with cdwn_ctx as cdwn:
        cdwn.save_cmdline(args)
        if ipc:
            _start_ipc_threads(cdwn)
        if arglist.get_maxid:
            cdwn.launch_get_max_id(arglist)
        else:
            cdwn.launch_download(arglist)
        code, error = run_exit_code(cdwn)
        if ipc:
            final = {'state': 0, 'stateName': 'DONE', 'success': cdwn.success_count, 'failed': cdwn.fail_count,
                     'totalAll': cdwn.total_count_all, 'processed': cdwn.processed_count, 'failedItems': cdwn.failed_items[:200],
                     'cfChallenge': cdwn.cf_challenge_hit, 'genderStats': cdwn.gender_stats, 'genderFiltered': cdwn.gender_filtered_count,
                     'blocked': cdwn.safety_blocked_count, 'known': cdwn.known_skipped_count, 'ok': code in (EXIT_OK, EXIT_PARTIAL),
                     'exitCode': code, 'error': error,
                     'cancelled': code == EXIT_CANCELLED, 'streaming': cdwn.streaming_active, 'scanning': False,
                     'bytes': cdwn.bytes_received, 'maxId': cdwn.max_item_id, 'genderModel': cdwn.gender_model_checked,
                     'genderVerified': cdwn.gender_verified_count, 'genderMismatch': cdwn.gender_mismatch_count}
            with Logger.print_lock:
                print(f'{IPC_PROGRESS_PREFIX}{json.dumps(final, separators=(",", ":"))}', flush=True)
        return code


if __name__ == '__main__':
    if len(sys.argv) >= 2:
        sys.exit(run_cmd(sys.argv[1:]))
    else:
        print('ERROR: Lynxx cmd arguments required. To run GUI use lynxx_gui.py')
        sys.exit(-1)

#
#
#########################################
