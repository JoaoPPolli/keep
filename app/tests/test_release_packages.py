"""Exercise generated archives through both manual updaters on isolated copies.

The prior installation fixture uses the current allowlisted file layout with a
2.0.0 server version; it does not need downloaded releases or a live app process.
Windows tests exercise its file-update logic, not its native launcher UI.
"""
import importlib.util
import io
import json
from pathlib import Path
import re
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import Mock
import zipfile

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from app_updates import inspect_archive
import update_keep
import update_keep_linux
from updater_runner import installed_version

spec = importlib.util.spec_from_file_location('keep_build_update', APP.parent / 'scripts/build_update.py')
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ReleasePackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='keep-release-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.version = builder.constants('server.py')['VERSION']

    def prepare(self, platform):
        updater = update_keep if platform == 'windows' else update_keep_linux
        entries = builder.entries_for(platform, self.version)
        content = builder.archive_for(platform, entries)
        manifest, _ = inspect_archive(content, platform)
        extracted = self.root / 'package'
        if platform == 'windows':
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                for name in archive.namelist():
                    destination = extracted / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.read(name))
        else:
            with tarfile.open(fileobj=io.BytesIO(content), mode='r:gz') as archive:
                for member in archive.getmembers():
                    destination = extracted / member.name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(archive.extractfile(member).read())
                    destination.chmod(member.mode)
        target = self.root / 'old-installation'
        old_files = {}
        for relative in updater.APP_FILES:
            value = (APP / relative).read_bytes()
            if relative == 'server.py':
                value = re.sub(rb"(?m)^VERSION = '[^']+'", b"VERSION = '2.0.0'", value, count=1)
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(value)
            old_files[relative] = value
        personal = {
            'data/settings.json': b'{"download_folder":"/my-videos","notifications":true}\n',
            'data/history.json': b'[{"id":"saved-job","status":"completed"}]\n',
            'downloads/video.mp4': b'personal-video-sentinel\x00\xff',
            'runtime/python.exe': b'existing-embedded-python-sentinel',
            'tools/ffmpeg': b'existing-ffmpeg-sentinel',
            'backups/my-existing-backup/note.txt': b'existing-backup-sentinel',
        }
        for relative, value in personal.items():
            destination = target / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(value)
        self.assertEqual(installed_version(target), '2.0.0')
        return updater, extracted / 'Atualizar Keep', target, manifest, old_files, personal

    def assert_upgrade(self, platform):
        updater, package, target, manifest, old_files, personal = self.prepare(platform)
        running_check = Mock()
        result, backup = updater.apply_update(package, target, running_check=running_check)
        self.assertEqual(result, target)
        self.assertEqual(installed_version(target), self.version)
        self.assertEqual(backup.parent, target / 'backups')
        self.assertTrue(backup.is_dir())
        self.assertGreaterEqual(running_check.call_count, 2)
        for call in running_check.call_args_list:
            self.assertEqual(call.args, (target,))
        for relative, expected in manifest['files'].items():
            self.assertEqual(builder.digest((target / relative).read_bytes()), expected, relative)
            self.assertEqual((backup / relative).read_bytes(), old_files[relative], relative)
        metadata = json.loads((backup / 'backup.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['files'], {name: True for name in updater.APP_FILES})
        self.assertEqual(installed_version(backup), '2.0.0')
        for relative, expected in personal.items():
            self.assertEqual((target / relative).read_bytes(), expected, relative)
        for relative in manifest['files']:
            self.assertFalse(relative.startswith(('data/', 'downloads/', 'runtime/', 'tools/', 'backups/')))
        if platform == 'linux':
            for relative in updater.APP_FILES:
                if relative.endswith('.sh'):
                    self.assertEqual((target / relative).stat().st_mode & 0o777, 0o755)

    def test_windows_manual_upgrade_preserves_user_files_and_backup(self):
        self.assert_upgrade('windows')

    @unittest.skipUnless(sys.platform.startswith('linux'), 'Linux updater uses its native installation lock')
    def test_linux_manual_upgrade_preserves_user_files_and_backup(self):
        self.assert_upgrade('linux')

    def test_corrupt_payload_is_rejected_before_existing_installation_changes(self):
        for platform in ('windows', 'linux'):
            with self.subTest(platform=platform):
                updater, package, target, _, old_files, personal = self.prepare(platform)
                (package / 'payload/server.py').write_bytes(b'tampered-release')
                running_check = Mock()
                with self.assertRaisesRegex(ValueError, 'verificação do pacote falhou'):
                    updater.apply_update(package, target, running_check=running_check)
                running_check.assert_not_called()
                for relative, expected in {**old_files, **personal}.items():
                    self.assertEqual((target / relative).read_bytes(), expected, relative)
                self.assertEqual(list((target / 'backups').iterdir()), [target / 'backups/my-existing-backup'])

    def test_archives_are_reproducible_for_identical_sources(self):
        for platform in ('windows', 'linux'):
            with self.subTest(platform=platform):
                entries = builder.entries_for(platform, self.version)
                first = builder.archive_for(platform, entries)
                self.assertEqual(first, builder.archive_for(platform, entries))
                manifest, payload = inspect_archive(first, platform)
                self.assertEqual(manifest['version'], self.version)
                self.assertEqual(payload['server.py'], (APP / 'server.py').read_bytes())


if __name__ == '__main__':
    unittest.main()
