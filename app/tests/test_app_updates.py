import base64
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_updates as updates
from updater_runner import PLATFORM_FILES, validate_staged


def package(version='2.0.1', platform='linux', unexpected=False):
    payload = {name: b'fixture\n' for name in PLATFORM_FILES[platform]}
    payload['server.py'] = ("# Keep: test application\nVERSION = '" + version + "'\n").encode()
    descriptor = {'version': version, 'platform': platform, 'layout': 'payload',
                  'files': {name: hashlib.sha256(content).hexdigest() for name, content in payload.items()}}
    wrappers = {'manifest.json': json.dumps(descriptor).encode()}
    wrappers.update({name: b'wrapper' for name in (
        ('update_keep.py', 'Atualizar Keep.bat', 'LEIA-ME.txt') if platform == 'windows'
        else ('update_keep_linux.py', 'Atualizar Keep.sh', 'LEIA-ME-LINUX.txt'))})
    entries = {**wrappers, **{'payload/' + name: content for name, content in payload.items()}}
    if unexpected:
        entries['payload/../../outside'] = b'untrusted'
    result = io.BytesIO()
    if platform == 'windows':
        with zipfile.ZipFile(result, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries.items():
                archive.writestr('KeepUpdate/' + name, content)
    else:
        with tarfile.open(fileobj=result, mode='w:gz') as archive:
            for name, content in entries.items():
                member = tarfile.TarInfo('KeepUpdate/' + name)
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
    return result.getvalue()


def release(content, version='2.0.1', platform='linux'):
    suffix = '-Windows-Atualizacao.zip' if platform == 'windows' else '-Linux-Atualizacao.tar.gz'
    return {'schema_version': 1, 'version': version,
            'release_url': 'https://github.com/JoaoPPolli/keep/releases/tag/v' + version,
            'packages': {platform: {
                'url': 'https://github.com/JoaoPPolli/keep/releases/download/v' + version + '/Keep-' + version + suffix,
                'size': len(content), 'sha256': hashlib.sha256(content).hexdigest()}}}


class PublicUpdatesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = updates.UpdateManager(self.root, '2.0.0', 'linux')
        self.origin = 'http://127.0.0.1:8765'
        self.content = package()
        self.release = release(self.content)

    def wait(self, manager=None):
        manager = manager or self.manager
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = manager.snapshot()
            if state['state'] not in ('checking', 'downloading'):
                return state
            time.sleep(.005)
        self.fail('Update operation remained busy')

    def check(self, descriptor=None):
        raw = json.dumps(self.release if descriptor is None else descriptor).encode()
        with patch.object(updates, '_fetch_public', return_value=raw) as fetch:
            initial = self.manager.begin_check(self.origin)
            self.assertEqual(initial['state'], 'checking')
            result = self.wait()
            fetch.assert_called_once_with(updates.PUBLIC_MANIFEST_URL, updates.MAX_MANIFEST_BYTES, 'manifest')
        return result

    def test_check_announces_update_without_downloading(self):
        state = self.check()
        self.assertEqual(state['state'], 'available')
        self.assertTrue(state['update_available'])
        self.assertTrue(state['can_download'])
        self.assertFalse(state['can_apply'])
        self.assertIsNone(self.manager.stage)
        self.assertFalse((self.root / 'data').exists())
        self.assertEqual(state['release_url'], self.release['release_url'])

    def test_current_or_newer_install_is_not_a_downgrade_offer(self):
        for version in ('2.0.0', '1.99.0'):
            with self.subTest(version=version):
                state = self.check(release(self.content, version=version))
                self.assertEqual(state['state'], 'current')
                self.assertFalse(state['update_available'])
                self.assertFalse(state['can_download'])

    def test_version_comparison_is_numeric(self):
        self.manager.current_version = '2.9.0'
        state = self.check(release(self.content, version='2.10.0'))
        self.assertEqual(state['state'], 'available')

    def test_explicit_download_verifies_and_stages_without_applying(self):
        (self.root / 'server.py').write_text('installed application')
        self.check()
        with patch.object(updates, '_fetch_public', return_value=self.content), patch.object(updates.subprocess, 'Popen') as spawn:
            self.assertEqual(self.manager.begin_download(self.origin)['state'], 'downloading')
            state = self.wait()
        self.assertEqual(state['state'], 'ready', state)
        self.assertTrue(state['can_apply'])
        self.assertFalse(state['can_download'])
        self.assertEqual(validate_staged(self.manager.stage, 'linux')['version'], '2.0.1')
        self.assertEqual((self.root / 'server.py').read_text(), 'installed application')
        spawn.assert_not_called()

    def test_digest_or_size_failure_never_stages(self):
        for content in (self.content[:-1], b'X' + self.content[1:]):
            with self.subTest(length=len(content)):
                self.check()
                with patch.object(updates, '_fetch_public', return_value=content):
                    self.manager.begin_download(self.origin)
                    state = self.wait()
                self.assertEqual(state['state'], 'failed')
                self.assertIsNone(self.manager.stage)
                self.assertTrue(state['can_download'])

    def test_archive_version_must_match_public_manifest(self):
        self.check(release(self.content, version='2.1.0'))
        with patch.object(updates, '_fetch_public', return_value=self.content):
            self.manager.begin_download(self.origin)
            state = self.wait()
        self.assertEqual(state['state'], 'failed')
        self.assertIn('versão', state['error'])
        self.assertIsNone(self.manager.stage)

    def test_network_timeout_does_not_leave_checking(self):
        with patch.object(updates, '_fetch_public', side_effect=TimeoutError('timed out')):
            self.manager.begin_check(self.origin)
            state = self.wait()
        self.assertEqual(state['state'], 'failed')
        self.assertFalse(state['can_download'])
        self.assertNotIn('timed out', state['error'])

    def test_invalid_manifest_fails_without_stage(self):
        for raw in (b'not json', b'[]', b'null', b'{}'):
            with self.subTest(raw=raw), patch.object(updates, '_fetch_public', return_value=raw):
                self.manager.begin_check(self.origin)
                self.assertEqual(self.wait()['state'], 'failed')
                self.assertIsNone(self.manager.stage)

    def test_untrusted_manifest_package_fields_are_rejected(self):
        fields = [('url', 'http://github.com/JoaoPPolli/keep/package.zip'),
                  ('url', 'https://github.com.evil.test/JoaoPPolli/keep/package.zip'),
                  ('url', 'https://github.com/other/repo/releases/download/v2.0.1/file.zip'),
                  ('url', 'https://user:pass@github.com/JoaoPPolli/keep/releases/download/v2.0.1/file.zip'),
                  ('size', True), ('size', updates.MAX_ARCHIVE_BYTES + 1),
                  ('size', 0), ('sha256', 'bad')]
        for key, value in fields:
            descriptor = json.loads(json.dumps(self.release))
            descriptor['packages']['linux'][key] = value
            with self.subTest(key=key, value=value):
                self.assertEqual(self.check(descriptor)['state'], 'failed')

    def test_manifest_schema_and_release_url_are_verified(self):
        for key, value in [('version', '2.0.1-evil'), ('schema_version', True),
                           ('release_url', 'https://evil.test/notes')]:
            descriptor = {**self.release, key: value}
            with self.subTest(key=key):
                self.assertEqual(self.check(descriptor)['state'], 'failed')

    def test_non_local_origin_cannot_start_network_operations(self):
        for origin in ('https://evil.test', 'http://127.0.0.1:9999',
                       'http://localhost:8765/evil', None):
            with self.subTest(origin=origin), patch.object(updates, '_fetch_public') as fetch:
                with self.assertRaises(ValueError):
                    self.manager.begin_check(origin)
                with self.assertRaises(ValueError):
                    self.manager.begin_download(origin)
                fetch.assert_not_called()

    def test_recheck_retains_known_update_version_during_network_and_failure(self):
        self.check()
        entered, finish = threading.Event(), threading.Event()
        def delayed(*args):
            entered.set()
            finish.wait(2)
            raise TimeoutError('offline')
        with patch.object(updates, '_fetch_public', side_effect=delayed):
            try:
                state = self.manager.begin_check(self.origin)
                self.assertTrue(entered.wait(1))
                self.assertEqual(state['state'], 'checking')
                self.assertEqual(state['latest_version'], '2.0.1')
                self.assertTrue(state['update_available'])
            finally:
                finish.set()
                state = self.wait()
        self.assertEqual(state['state'], 'failed')
        self.assertEqual(state['latest_version'], '2.0.1')
        self.assertTrue(state['update_available'])
        self.assertTrue(state['can_download'])

    def test_local_import_version_overrides_known_public_release_in_snapshot(self):
        self.check()
        state = self.manager.receive(base64.b64encode(package('2.0.2')).decode())
        self.assertEqual(state['state'], 'ready')
        self.assertEqual(state['latest_version'], '2.0.2')
        self.assertTrue(state['can_apply'])

    def test_deep_json_fails_without_leaving_check_busy(self):
        raw = b'[' * 2000 + b']' * 2000
        with patch.object(updates, '_fetch_public', return_value=raw):
            self.manager.begin_check(self.origin)
            self.assertEqual(self.wait()['state'], 'failed')

    def test_download_requires_successful_check(self):
        with self.assertRaises(ValueError):
            self.manager.begin_download(self.origin)

    def test_busy_check_cannot_race_import_rollback_or_another_check(self):
        entered, finish = threading.Event(), threading.Event()
        def delayed(*args):
            entered.set()
            finish.wait(2)
            return json.dumps(self.release).encode()
        with patch.object(updates, '_fetch_public', side_effect=delayed) as fetch:
            try:
                self.manager.begin_check(self.origin)
                self.assertTrue(entered.wait(1))
                with self.assertRaises(ValueError):
                    self.manager.receive(base64.b64encode(self.content).decode())
                with self.assertRaises(ValueError):
                    self.manager.prepare_rollback('missing')
                self.assertEqual(self.manager.begin_check(self.origin)['state'], 'checking')
                fetch.assert_called_once()
            finally:
                finish.set()
                self.wait()

    def test_ready_package_survives_automatic_check(self):
        self.manager.receive(base64.b64encode(self.content).decode())
        stage = self.manager.stage
        with patch.object(updates, '_fetch_public') as fetch:
            self.assertEqual(self.manager.begin_check(self.origin)['state'], 'ready')
        self.assertEqual(self.manager.stage, stage)
        fetch.assert_not_called()

    def test_windows_and_linux_import_validation_remains_available(self):
        for platform in ('windows', 'linux'):
            with self.subTest(platform=platform):
                manager = updates.UpdateManager(self.root / platform, '2.0.0', platform)
                state = manager.receive(base64.b64encode(package(platform=platform)).decode())
                self.assertEqual(state['state'], 'ready')
                self.assertTrue(state['can_apply'])
                self.assertEqual(validate_staged(manager.stage, platform)['version'], '2.0.1')

    def test_local_import_rejects_downgrade_and_traversal(self):
        for content in (package('2.0.0'), package(unexpected=True)):
            with self.subTest(size=len(content)), self.assertRaises(ValueError):
                self.manager.receive(base64.b64encode(content).decode())
        self.assertIsNone(self.manager.stage)


class BoundedFetchTest(unittest.TestCase):
    class Response(io.BytesIO):
        def __init__(self, content, url=updates.PUBLIC_MANIFEST_URL, headers=None):
            super().__init__(content)
            self.url = url
            self.headers = headers or {}
        def geturl(self):
            return self.url

    def test_oversized_manifest_rejected_even_without_content_length(self):
        with patch.object(updates, 'build_opener') as opener:
            opener.return_value.open.return_value = self.Response(b'x' * 11)
            with self.assertRaises(ValueError):
                updates._fetch_public(updates.PUBLIC_MANIFEST_URL, 10, 'manifest')

    def test_declared_oversized_response_rejected_before_reading(self):
        response = self.Response(b'{}', headers={'Content-Length': '999999'})
        with patch.object(updates, 'build_opener') as opener:
            opener.return_value.open.return_value = response
            with self.assertRaises(ValueError):
                updates._fetch_public(updates.PUBLIC_MANIFEST_URL, 10, 'manifest')
        self.assertEqual(response.tell() if not response.closed else 0, 0)

    def test_manifest_redirect_cannot_leave_official_manifest(self):
        with patch.object(updates, 'build_opener') as opener:
            opener.return_value.open.return_value = self.Response(b'{}', url='https://evil.test/updates.json')
            with self.assertRaises(ValueError):
                updates._fetch_public(updates.PUBLIC_MANIFEST_URL, 10, 'manifest')

    def test_package_redirect_accepts_github_cdn_but_rejects_other_origins(self):
        for url in ('https://evil.test/package.zip', 'http://release-assets.githubusercontent.com/file',
                    'https://user:pass@release-assets.githubusercontent.com/file',
                    'https://release-assets.githubusercontent.com:8443/file'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                updates._validate_public_url(url, 'package', '2.0.1', 'windows', redirect=True)
        updates._validate_public_url('https://release-assets.githubusercontent.com/path?signature=abc',
                                     'package', '2.0.1', 'windows', redirect=True)

    def test_valid_response_is_bounded_and_uses_timeout(self):
        with patch.object(updates, 'build_opener') as opener:
            opener.return_value.open.return_value = self.Response(b'{}')
            self.assertEqual(updates._fetch_public(updates.PUBLIC_MANIFEST_URL, 10, 'manifest'), b'{}')
            self.assertEqual(opener.return_value.open.call_args.kwargs['timeout'], updates.NETWORK_TIMEOUT)



class FreshManifestTests(unittest.TestCase):
    def test_each_public_manifest_request_has_a_fresh_cache_key(self):
        requests=[]
        class Response:
            headers={}
            def __init__(self,url):self.url=url;self.data=b'{}'
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def geturl(self):return self.url
            def read1(self,count):data,self.data=self.data,b'';return data
        class Opener:
            def open(self,request,timeout):requests.append(request.full_url);return Response(request.full_url)
        with patch.object(updates,'build_opener',return_value=Opener()):
            for attempt in range(2):self.assertEqual(updates._fetch_public(updates.PUBLIC_MANIFEST_URL,1024,'manifest'),b'{}')
        self.assertNotEqual(requests[0],requests[1])
        self.assertTrue(all(url.startswith(updates.PUBLIC_MANIFEST_URL+'?keep_check=') for url in requests))
        for url in (updates.PUBLIC_MANIFEST_URL+'?other=1',updates.PUBLIC_MANIFEST_URL+'?keep_check=evil',updates.PUBLIC_MANIFEST_URL.replace('raw.githubusercontent.com','evil.example')):
            with self.assertRaises(ValueError):updates._validate_public_url(url,'manifest')

if __name__ == '__main__':
    unittest.main()
