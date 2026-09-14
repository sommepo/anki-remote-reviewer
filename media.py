"""Media helpers independent of Anki and Qt."""
import re
from html import escape, unescape
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

SOUND = re.compile(r"\[sound:([^\]]+)\]", re.I)
COMMAND = re.compile(r"\[anki:play:([qa]):(\d+)\]", re.I)

def audio_filenames(card, side):
    """Preserve AV indexes (including unsupported TTS slots) and duplicates."""
    try:
        return [getattr(tag, 'filename', None) for tag in
                getattr(card, 'question_av_tags' if side == 'q' else 'answer_av_tags')()]
    except (AttributeError, TypeError):
        # Explicit opt-in field lists are handled by the caller. Never guess
        # template order from note order: it can play the wrong pronunciation.
        return []

def media_url(filename):
    return '/media/' + quote(filename, safe='')

def rewrite_media(html, tags):
    def button(filename):
        if not filename:
            return '<span class="audio-unavailable" title="This audio requires desktop Anki">Audio unavailable</span>'
        return '<button type="button" class="anki-remote-audio" data-sound="' + media_url(filename) + '" aria-label="Play audio">▶</button>'
    def command(match):
        files = tags.get(match[1].lower(), [])
        return button(files[int(match[2])] if int(match[2]) < len(files) else None)
    html = SOUND.sub(lambda m: button(m[1]), html)
    html = COMMAND.sub(command, html)
    def attribute(match):
        value = unescape(match[3])
        if not value or value.startswith(('#', '/', 'data:', 'blob:')) or urlsplit(value).scheme:
            return match[0]
        return match[1] + '=' + match[2] + media_url(unquote(value)) + match[2]
    return re.sub(r'\b(src|href)\s*=\s*([\x22\x27])(.*?)\2', attribute, html, flags=re.I)

def rewrite_css(css):
    def url(match):
        value = match[1].strip().strip('\x22\x27')
        if not value or value.startswith(('#', '/', 'data:', 'blob:')) or urlsplit(value).scheme:
            return match[0]
        return 'url("' + media_url(unquote(value)) + '")'
    return re.sub(r'url\(\s*([^)]*?)\s*\)', url, css, flags=re.I)

def safe_media_file(directory, encoded_name):
    name = unquote(encoded_name)
    if not name or any(c in name for c in ('/', '\\', ':', '\x00')) or name in ('.', '..'):
        raise ValueError('Invalid media file name')
    root = Path(directory).resolve()
    target = (root / name).resolve()
    if target.parent != root or not target.is_file():
        raise ValueError('Media file was not found')
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
