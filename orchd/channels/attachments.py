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


# Defaults exclude executable and opaque content. ZIP is inspected before caching.
DEFAULT_MIME_ALLOWLIST = frozenset({'text/plain', 'text/csv', 'application/pdf', 'application/zip', 'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/heic', 'video/mp4', 'video/quicktime', 'video/webm', 'audio/mp4', 'audio/m4a', 'audio/x-m4a', 'audio/mp3', 'audio/mpeg', 'audio/wav', 'audio/x-wav', 'audio/ogg', 'audio/webm'})
MAX_EXPANDED_BYTES = 20 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100


def rejection_reason(channel_home, source_id, mime_type, data):
    import io
    import tomllib
    import zipfile
    config = channel_home / 'email' / 'config.toml'
    allowed = DEFAULT_MIME_ALLOWLIST
    try:
        with config.open('rb') as stream:
            sources = tomllib.load(stream).get('sources', [])
        matches = [source for source in sources if source.get('id') == source_id]
        if len(matches) > 1:
            raise ValueError('duplicate source attachment policy')
        if matches and 'attachment_mime_allowlist' in matches[0]:
            allowed = matches[0]['attachment_mime_allowlist']
            if not isinstance(allowed, list) or not all(isinstance(x, str) and x and x == x.lower() for x in allowed):
                raise ValueError('invalid attachment MIME allowlist')
    except FileNotFoundError:
        pass
    if mime_type.lower() not in allowed:
        return 'mime_rejected'
    is_zip = zipfile.is_zipfile(io.BytesIO(data))
    if mime_type.lower() == 'application/zip' and not is_zip:
        return 'compression_rejected'
    # Do not allow unsupported compressed containers to evade the ZIP guard.
    if data.startswith((b'\x1f\x8b', b'BZh', b'\xfd7zXZ', b'7z\xbc\xaf\x27\x1c', b'Rar!')):
        return 'compression_rejected'
    if is_zip:
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as archive:
                total = 0
                for member in archive.infolist():
                    total += member.file_size
                    if total > MAX_EXPANDED_BYTES or total > max(len(data), 1) * MAX_COMPRESSION_RATIO or member.flag_bits & 1:
                        return 'compression_rejected'
                    # Bounded streaming verifies declared sizes and catches corrupt entries.
                    with archive.open(member) as stream:
                        seen = 0
                        while True:
                            chunk = stream.read(65536)
                            if not chunk:
                                break
                            seen += len(chunk)
                            if seen > member.file_size or seen > MAX_EXPANDED_BYTES:
                                return 'compression_rejected'
                            if seen == len(chunk) and (chunk.startswith((b'PK\x03\x04', b'\x1f\x8b', b'BZh', b'\xfd7zXZ', b'7z\xbc\xaf\x27\x1c', b'Rar!'))):
                                return 'compression_rejected'
        except (ValueError, OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile):
            return 'compression_rejected'
    return None
