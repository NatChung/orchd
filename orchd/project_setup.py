"""First-run project selection for a human running doctor in a terminal."""
import json
import os
import tempfile
import tomllib
from pathlib import Path

from . import paths


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
    config = paths.config_dir(home, env) / 'config.toml'
    try:
        original = config.read_text(encoding='utf-8')
    except FileNotFoundError:
        original = ''
    if 'projects_dir' in tomllib.loads(original):
        return  # an explicitly selected missing directory is a real error
    print('尚未設定專案目錄。請輸入存放各個 Git repo 的上層目錄。')
    print(f'輸入後會保存至 {config}，之後 doctor 與派工都會沿用。')
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
    # Put the top-level key before existing tables, preserving comments and settings.
    text = f'projects_dir = {json.dumps(str(selected), ensure_ascii=False)}\n' + original
    config.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=config.parent,
                                         prefix='.config-', delete=False) as output:
            temp = Path(output.name)
            output.write(text)
        temp.replace(config)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)
    print(f'已保存專案目錄：{selected}')
