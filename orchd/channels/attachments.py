"""Safe attachment content paths."""
import re
from pathlib import Path

MAX_ATTACHMENT_BYTES = 25 * 1024 * 1024

def content_path(channel_home, source_id, digest):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', source_id) or not re.fullmatch(r'[0-9a-f]{64}', digest):
        raise ValueError('invalid attachment path')
    root = Path(channel_home).absolute()
    target = root / 'email' / 'state' / source_id / 'content' / digest
    current = root
    if current.is_symlink():
        raise ValueError('unsafe attachment path')
    for part in ('email', 'state', source_id, 'content', digest):
        current = current / part
        if current.is_symlink():
            raise ValueError('unsafe attachment path')
    return target
