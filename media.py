"""Media and path helpers independent of Anki and Qt."""
import re
from pathlib import Path
from urllib.parse import unquote

SOUND = re.compile(r"\[sound:([^\]]+)\]", re.I)

# Explicit types: Windows' registry can map .js to text/plain, which browsers
# refuse to execute under nosniff.
TYPES = {
    '.js': 'text/javascript; charset=utf-8', '.mjs': 'text/javascript; charset=utf-8',
    '.css': 'text/css; charset=utf-8', '.json': 'application/json', '.map': 'application/json',
    '.html': 'text/html; charset=utf-8', '.htm': 'text/html; charset=utf-8',
    '.txt': 'text/plain; charset=utf-8', '.svg': 'image/svg+xml',
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.gif': 'image/gif',
    '.webp': 'image/webp', '.avif': 'image/avif', '.bmp': 'image/bmp', '.ico': 'image/x-icon',
    '.mp3': 'audio/mpeg', '.m4a': 'audio/mp4', '.aac': 'audio/aac', '.ogg': 'audio/ogg',
    '.oga': 'audio/ogg', '.opus': 'audio/ogg', '.wav': 'audio/wav', '.flac': 'audio/flac',
    '.mp4': 'video/mp4', '.webm': 'video/webm', '.ogv': 'video/ogg',
    '.woff': 'font/woff', '.woff2': 'font/woff2', '.ttf': 'font/ttf', '.otf': 'font/otf',
    '.ttc': 'font/collection', '.wasm': 'application/wasm',
}


def content_type(name):
    return TYPES.get(Path(name).suffix.lower(), 'application/octet-stream')


def av_entries(tags):
    """Map AV tags to playable entries, keeping indexes for [anki:play:x:N]."""
    entries = []
    for tag in tags:
        filename = getattr(tag, 'filename', None)
        if filename:
            entries.append({'file': filename})
        elif getattr(tag, 'field_text', None) is not None:
            entries.append({'tts': {
                'text': tag.field_text,
                'lang': getattr(tag, 'lang', '') or '',
                'speed': float(getattr(tag, 'speed', 1) or 1),
            }})
        else:
            entries.append(None)
    return entries


def safe_media_file(directory, encoded_name):
    name = unquote(encoded_name)
    if not name or any(c in name for c in ('/', '\\', ':', '\x00')) or name in ('.', '..'):
        raise ValueError('Invalid media file name')
    root = Path(directory).resolve()
    target = (root / name).resolve()
    if target.parent != root or not target.is_file():
        raise ValueError('Media file was not found')
    return target


def safe_asset(directory, encoded_path):
    """Resolve a '/'-separated path below directory, rejecting traversal."""
    parts = unquote(encoded_path).split('/')
    if any(not p or p in ('.', '..') or any(c in p for c in ('\\', ':', '\x00')) for p in parts):
        raise ValueError('Invalid path')
    root = Path(directory).resolve()
    target = root.joinpath(*parts).resolve()
    if root not in target.parents or not target.is_file():
        raise ValueError('File was not found')
    return target


def byte_range(header, size):
    if not header:
        return 0, size - 1
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
    if not match or not any(match.groups()) or size == 0:
        raise ValueError('Invalid byte range')
    first, last = match.groups()
    if not first:
        if int(last) == 0:
            raise ValueError('Invalid byte range')
        return max(0, size - int(last)), size - 1
    start, end = int(first), min(int(last), size - 1) if last else size - 1
    if start > end:
        raise ValueError('Invalid byte range')
    return start, end
