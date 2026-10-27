"""Exercise authenticated subtitle routes with real preview capabilities."""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from preview_media import PreviewManager, _Preview


class RecognizerFixture:
    def __init__(self):
        self.calls = []

    def create(self, source, **options):
        self.calls.append((source, options))
        return {'id': 'caption-test', 'status': 'queued'}

    def status(self, id):
        if id != 'caption-test':
            raise ValueError('Legendas não encontradas.')
        return {'id': id, 'status': 'ready', 'cues': []}

    def cancel(self, id):
        self.status(id)
        return {'id': id, 'status': 'cancelled'}


class SubtitleAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='keep-subtitle-api-')
        self.root = Path(self.temp.name)
        self.previews = PreviewManager(self.root / 'previews', lambda *args: None, 'ffmpeg')
        self.id, self.key = 'a' * 32, 'private-preview-capability'
        folder = self.root / 'previews' / self.id
        folder.mkdir()
        (folder / '.keep-preview.json').write_text(json.dumps({'id': self.id, 'type': 'keep-preview'}))
        self.source = folder / 'preview.mp4'
        self.source.write_bytes(b'trusted-ready-preview-fixture')
        self.previews._items[self.id] = _Preview(self.id, self.key, folder, {}, time.monotonic(),
            time.monotonic(), status='ready', duration=2)
        self.recognizer = RecognizerFixture()
        self.http = server.ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.port = self.http.server_port
        self.scope = patch.multiple(server, PORT=self.port, PREVIEWS=self.previews,
                                    UPDATES=object(), SUBTITLES=self.recognizer, LIVES=object())
        self.scope.start()
        self.worker = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.worker.start()

    def tearDown(self):
        self.http.shutdown()
        self.worker.join(timeout=3)
        self.http.server_close()
        self.previews.close()
        self.scope.stop()
        self.temp.cleanup()

    def request(self, path, body=None, *, token=True, origin=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['X-Keep-Token'] = server.TOKEN
        if origin:
            headers['Origin'] = origin
        request = Request(f'http://127.0.0.1:{self.port}/api/' + path,
            data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urlopen(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_live_clip_export_rejects_wrong_capability_and_recorded_preview(self):
        body = {'url': 'https://twitch.tv/example', 'clip': {'id': self.id, 'key': 'wrong-key'}}
        code, _ = self.request('download', body)
        self.assertEqual(code, 400)
        body['clip']['key'] = self.key
        code, result = self.request('download', body)
        self.assertEqual(code, 400)
        self.assertIn('clipe', result['error'])

    def test_live_start_rechecks_the_stream_and_requires_app_authorization(self):
        body = {'url': 'https://twitch.tv/example', 'browser': 'none'}
        with patch.object(server, 'metadata', return_value={'live': False}) as inspect:
            self.assertEqual(self.request('live/start', body, token=False)[0], 403)
            self.assertEqual(self.request('live/start', body, origin='https://example.com')[0], 403)
            inspect.assert_not_called()
            code, result = self.request('live/start', body)
            self.assertEqual(code, 400)
            self.assertIn('ao vivo', result['error'])
            inspect.assert_called_once()

    def test_video_inspection_still_calls_the_metadata_service(self):
        with patch.object(server, 'metadata', return_value={'title': 'Recorded video', 'live': False}) as inspect:
            code, result = self.request('inspect', {'url': 'https://youtu.be/example', 'browser': 'none'})
            self.assertEqual(code, 200)
            self.assertEqual(result['title'], 'Recorded video')
            inspect.assert_called_once_with('https://youtu.be/example', 'none')

    def test_generation_uses_only_the_ready_preview_verified_by_capability(self):
        status, result = self.request('subtitles', {'preview_id': self.id, 'key': self.key, 'language': 'pt'})
        self.assertEqual(status, 202)
        self.assertEqual(result['status'], 'queued')
        source, options = self.recognizer.calls[0]
        self.assertEqual(source, self.source)
        self.assertEqual(options, {'language': 'pt', 'preview_id': self.id, 'duration': 2})

    def test_wrong_capability_or_arbitrary_path_cannot_reach_the_recognizer(self):
        for body in [{'preview_id': self.id, 'key': 'wrong'}, {'source': '/etc/passwd'}]:
            with self.subTest(body=body):
                status, _ = self.request('subtitles', body)
                self.assertEqual(status, 400)
        self.assertEqual(self.recognizer.calls, [])

    def test_session_token_and_local_origin_are_required(self):
        body = {'preview_id': self.id, 'key': self.key}
        self.assertEqual(self.request('subtitles', body, token=False)[0], 403)
        self.assertEqual(self.request('subtitles', body, origin='https://example.com')[0], 403)
        self.assertEqual(self.request('subtitles?id=caption-test', token=False)[0], 403)
        self.assertEqual(self.recognizer.calls, [])

    def test_status_cancel_and_unknown_task_are_distinct(self):
        self.assertEqual(self.request('subtitles?id=caption-test')[1]['status'], 'ready')
        self.assertEqual(self.request('subtitles/cancel', {'id': 'caption-test'})[1]['status'], 'cancelled')
        self.assertEqual(self.request('subtitles?id=missing')[0], 404)


if __name__ == '__main__':
    unittest.main()
