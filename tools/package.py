"""Build deterministic, allowlisted release archives without private settings."""
import hashlib
import json
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = (
    '__init__.py', 'backend.py', 'media.py', 'ports.py', 'manifest.json',
    'config.json', 'config.md', 'README.md', 'CHANGELOG.md', 'LICENSE',
    'web/index.html', 'web/app.js', 'web/app.css', 'web/frame.html', 'web/frame.js',
)
SOURCE = RUNTIME + (
    '.gitignore', 'tests/test_core.py', 'tests/audio.test.cjs', 'tests/fake_anki.py',
    'tests/dev_server.py', 'tests/cdp.mjs', 'tests/e2e.mjs', 'tools/package.py',
)

def build():
    version = re.search(r'^VERSION = "([^"]+)"', (ROOT / 'backend.py').read_text(encoding='utf-8'), re.M)[1]
    defaults = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    assert defaults['accessToken'] == '' and not defaults.get('cookieSecret')
    assert defaults['requireAccessToken'] is True
    assert defaults['rwkvNoteTypes'] == [] and defaults['audioFallbackFields'] == {}
    assert defaults['lanAddress'] == '' and defaults['autoplayAudio'] is True
    output = ROOT / 'dist'
    output.mkdir(exist_ok=True)
    checksums = []
    for suffix, files, prefix in (
        ('.ankiaddon', RUNTIME, ''),
        ('-source.zip', SOURCE, f'anki-remote-reviewer-{version}/'),
    ):
        target = output / f'anki-remote-reviewer-{version}{suffix}'
        with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name in sorted(files):
                data = (ROOT / name).read_bytes()
                info = zipfile.ZipInfo(prefix + name, date_time=(2026, 10, 22, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, data)
        with zipfile.ZipFile(target) as archive:
            assert archive.testzip() is None
            assert set(archive.namelist()) == {prefix + name for name in files}
        checksums.append(f'{hashlib.sha256(target.read_bytes()).hexdigest()}  {target.name}')
        print(target)
    (output / 'SHA256SUMS.txt').write_text('\n'.join(checksums) + '\n', encoding='utf-8')

if __name__ == '__main__':
    build()
