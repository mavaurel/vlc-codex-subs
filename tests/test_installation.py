"""Exercise the downloadable archive, not the developer's installed VLC data."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

from build_release import build, release_files


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='vlc release with spaces ')
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        project = Path(__file__).resolve().parents[1]
        for path in release_files(project):
            target = self.source / path.relative_to(project)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)

    def test_release_excludes_private_data_and_native_binaries(self):
        sentinel = 'PRIVATE ' + self.root.name
        for name in ['.env', 'config.json', 'jobs/private/request.json',
                     'codex_subs/bin/dvd-subtitles', 'codex_subs/__pycache__/private.pyc',
                     'Film.ru.srt']:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(sentinel)
        archive, manifest = build(self.source, self.root / 'first')
        again, _ = build(self.source, self.root / 'second')
        self.assertEqual(archive.read_bytes(), again.read_bytes())
        self.assertEqual(manifest.read_text().split()[0], hashlib.sha256(archive.read_bytes()).hexdigest())
        with zipfile.ZipFile(archive) as bundle:
            names = bundle.namelist()
            self.assertTrue(any(n.endswith('/Install.command') for n in names))
            self.assertTrue(any(n.endswith('/native/dvd_subtitles.c') for n in names))
            self.assertTrue(any(n.endswith('/LICENSE') for n in names))
            for entry in bundle.infolist():
                self.assertNotIn(sentinel.encode(), bundle.read(entry))
                if entry.filename.endswith('/Install.command'):
                    self.assertEqual((entry.external_attr >> 16) & 0o777, 0o755)
            self.assertIsNone(bundle.testzip())

    def test_archive_installs_upgrades_and_uninstalls_without_losing_data(self):
        archive, _ = build(self.source, self.root / 'dist')
        unpack = self.root / 'unpacked'
        with zipfile.ZipFile(archive) as bundle:
            bundle.extractall(unpack)
        project = unpack / archive.stem
        fake = self.root / 'fake-codex'
        fake.write_text('#!/bin/sh\nexit 0\n')
        fake.chmod(0o755)
        extensions, data = self.root / 'VLC extensions', self.root / 'VLC data'
        args = [str(project/'install.py'), '--extension-dir', str(extensions),
                '--data-dir', str(data), '--codex', str(fake)]
        def install(extra=()):
            result = subprocess.run([sys.executable, *args, *extra], capture_output=True,
                                    text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result
        install()
        script = (extensions/'codex_russian_subtitles.lua').read_text()
        for marker in ('@PYTHON@', '@APP@', '@DATA@', '@CONFIG@'):
            self.assertNotIn(marker, script)
        self.assertIn(str(data/'app'), script)
        config = data/'config.json'
        settings = json.loads(config.read_text())
        self.assertEqual(settings['codex'], str(fake))
        self.assertEqual(settings['workers'], 4)
        settings['workers'] = 15
        settings['service_tier'] = 'fast'
        config.write_text(json.dumps(settings))
        subtitle = data/'jobs'/'saved'/'Russian.srt'
        subtitle.parent.mkdir()
        subtitle.write_text('saved result')
        install()
        self.assertEqual(json.loads(config.read_text())['workers'], 15)
        self.assertEqual(json.loads(config.read_text())['service_tier'], 'fast')
        installed = data/'app/codex_subs/__init__.py'
        self.assertEqual(installed.read_bytes(), (project/'codex_subs/__init__.py').read_bytes())
        removed = subprocess.run(['bash', str(project/'Install.command'),
                                  *args[1:], '--uninstall'], capture_output=True,
                                 text=True, timeout=10)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse((extensions/'codex_russian_subtitles.lua').exists())
        self.assertTrue(config.exists())
        self.assertEqual(subtitle.read_text(), 'saved result')
