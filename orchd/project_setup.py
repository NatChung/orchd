"""First-run project selection for a human running doctor in a terminal."""
import os
from pathlib import Path

from . import config, paths


def prompt_projects(home=None, env=None):
    """Ask only when no project root was configured or discovered; preserve other settings.

    Provider checks remain in Doctor and read-only. This explicit terminal setup
    step writes only the project root entered by the user.
    """
    home = Path(home or Path.home())
    env = os.environ if env is None else env
    if env.get('ORCHD_PROJECTS'):
        return
    try:
        if paths.projects_dir(home, env).is_dir():
            return
    except ValueError:
        return  # doctor reports invalid configuration; never overwrite it
    try:
        if config.file_projects_dir(env, home) is not None:
            return  # an explicitly selected missing directory is a real error
    except ValueError:
        return
    file = config.path(env, home)
    print('尚未設定專案目錄。請輸入存放各個 Git repo 的上層目錄。')
    print(f'輸入後會保存至 {file}，之後 doctor 與派工都會沿用。')
    while True:
        try:
            answer = input('專案目錄（絕對路徑或 ~/；Enter 跳過）：').strip()
        except (EOFError, KeyboardInterrupt):
            print('\n已跳過專案目錄設定。')
            return
        if not answer:
            return
        if answer == '~':
            selected = home
        elif answer.startswith('~/'):
            selected = home / answer[2:]
        else:
            selected = Path(answer)
        if not selected.is_absolute():
            print('請輸入絕對路徑，或以 ~/ 開頭。')
            continue
        if not selected.is_dir():
            print(f'{selected} 不是已存在的目錄，請重新輸入。')
            continue
        break
    config.set_value('projects-dir', str(selected), env, home)  # same writer as `orchd config set`: keeps CRLF, mode, other bytes
    print(f'已保存專案目錄：{selected}')
