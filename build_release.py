#!/usr/bin/env python3
"""Build a reproducible source-only installation ZIP and SHA-256 manifest."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import zipfile

TOP_LEVEL = (
    'README.md', 'README.ru.md', 'LICENSE', 'THIRD_PARTY_NOTICES.md',
    'CHANGELOG.md', 'Install.command', 'install.py', 'setup_dvd.py',
    'build_release.py', 'extension.lua', '.gitignore',
)


def release_files(root):
    # An allowlist prevents local jobs, subtitles, credentials and optional
    # compiled dependencies from leaking into an installation archive.
    paths = [root / name for name in TOP_LEVEL]
    paths += sorted(p for p in (root / 'codex_subs').iterdir()
                    if p.is_file() and p.suffix in ('.py', '.swift'))
    paths += sorted((root / 'native').glob('*.c'))
    paths += sorted((root / 'tests').glob('test_*.py'))
    return paths


def build(root, destination):
    version = re.search(r'__version__ = "([^"\n]+)"',
                        (root / 'codex_subs/__init__.py').read_text())[1]
    destination.mkdir(parents=True, exist_ok=True)
    name = f'vlc-codex-subs-{version}'
    archive = destination / (name + '.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as bundle:
        for path in release_files(root):
            entry = zipfile.ZipInfo(str(Path(name) / path.relative_to(root)),
                                    date_time=(1980, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = (0o100755 if path.name == 'Install.command'
                                   else 0o100644) << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            bundle.writestr(entry, path.read_bytes())
    checksum = hashlib.sha256(archive.read_bytes()).hexdigest()
    manifest = destination / 'SHA256SUMS'
    manifest.write_text(f'{checksum}  {archive.name}\n', encoding='ascii')
    return archive, manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output-dir', type=Path, default=Path('dist'))
    args = parser.parse_args()
    for path in build(Path(__file__).resolve().parent, args.output_dir.resolve()):
        print(path)


if __name__ == '__main__':
    main()
