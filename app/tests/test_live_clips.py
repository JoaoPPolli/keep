"""Real rolling capture and clipping; no dependency on a third-party live being online."""
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from media_editor import probe_media
from preview_media import LiveManager, PreviewManager

FFMPEG = shutil.which('ffmpeg')


@unittest.skipUnless(FFMPEG, 'FFmpeg required')
class LiveClipTests(unittest.TestCase):
    def test_real_stream_recent_clip_audio_preview_and_editor_export(self):
        def producer(request):
            return [FFMPEG, '-hide_banner', '-loglevel', 'error', '-re', '-f', 'lavfi',
                    '-i', 'testsrc2=size=320x180:rate=15', '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100',
                    '-t', '28', '-vf', "drawbox=c=red:t=fill:enable='lt(t,3)',drawbox=c=blue:t=fill:enable='gte(t,3)'", '-c:v', 'libx264', '-preset', 'ultrafast', '-threads', '1',
                    '-g', '30', '-c:a', 'aac', '-f', 'mpegts', 'pipe:1']
        with tempfile.TemporaryDirectory(prefix='keep-real-live-') as directory:
            root = Path(directory)
            live = LiveManager(root / 'lives', producer, FFMPEG)
            previews = PreviewManager(root / 'previews', lambda *args: None, FFMPEG)
            try:
                task = live.start({'info': {'url': 'https://www.twitch.tv/example', 'title': 'Example'}})
                with self.assertRaises(ValueError): live.start({})
                with self.assertRaises(ValueError): live.status('wrong-id')
                with self.assertRaises(ValueError): live.clip(task['id'], True, lambda *args: None)
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    status = live.status(task['id'])
                    if status['buffer_seconds'] >= 18 or status['status'] == 'failed': break
                    time.sleep(.1)
                self.assertEqual(status['status'], 'buffering', status)
                self.assertGreaterEqual(status['buffer_seconds'], 18, status)
                self.assertLessEqual(status['buffer_seconds'], 120)
                def consume(path, info):
                    self.assertAlmostEqual(info['duration'], 15, delta=.15)
                    self.assertTrue(info['audio'])
                    self.assertLessEqual(info['height'], 180)
                    pixel = subprocess.check_output([FFMPEG,'-hide_banner','-loglevel','error','-i',str(path),
                        '-vf','scale=1:1','-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'])
                    self.assertGreater(pixel[2], 200, 'Recent clip must exclude the first three red seconds')
                    self.assertLess(pixel[0], 30)
                    return previews.import_clip(path)
                preview = live.clip(task['id'], 15, consume)
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    preview = previews.status(preview['id'])
                    if preview['status'] in ('ready', 'failed'): break
                    time.sleep(.05)
                self.assertEqual(preview['status'], 'ready', preview)
                key = preview['media_url'].split('key=')[1]
                source = previews.live_file(preview['id'], key)
                self.assertTrue(probe_media(FFMPEG, source)['audio'])
                with self.assertRaises(ValueError): previews.live_file(preview['id'], 'wrong-key')
                # Exercise the actual local export job, including the compositor,
                # without dispatching a network download or writing real user data.
                with patch.multiple(server, ROOT=root, DATA=root/'data', DOWNLOADS=root/'downloads',
                                    SETTINGS={}, JOBS={}, PROCESSES={}, ACTIVE_WORKERS=set(),
                                    SHUTTING_DOWN=False), patch.object(server, 'dispatch_queue'), patch.object(server, 'notify_completed_job'):
                    (root / 'data').mkdir()
                    info = probe_media(FFMPEG, source)
                    request = {'url': 'https://www.twitch.tv/example', 'title': 'Live clip',
                               'mode': 'video', 'edit': {'sections': [{'start': 2, 'end': 4}], 'vertical': True,
                               'layout': 'stacked', 'camera': {'x':0,'y':0,'width':.3,'height':.4},
                               'content': {'x':.3,'y':0,'width':.7,'height':1},
                               'captions': {'enabled':True,'cues':[{'start':2,'end':4,'text':'Clipe da live'}]}}}
                    job = server.save_live_clip(source, info, request, request)
                    server.JOBS[job['id']]['status'] = 'downloading'
                    server.run_local_clip(job['id'])
                    saved = server.JOBS[job['id']]
                    self.assertEqual(saved['status'], 'complete', saved)
                    final = probe_media(FFMPEG, root / saved['file'])
                    self.assertAlmostEqual(final['duration'], 2, delta=.15)
                    self.assertAlmostEqual(final['width']/final['height'], 9/16, delta=.015)
                    self.assertTrue(final['audio'])
                    self.assertFalse((root / saved['directory'] / '.keep-live-source.mp4').exists())
                stopped = live.stop(task['id'])
                self.assertEqual(stopped['status'], 'stopped')
                self.assertGreater(stopped['buffer_seconds'], 0)
                self.assertTrue(all(p.poll() is not None for p in live._session['processes']))
            finally:
                live.close()
                previews.close()

    def test_ring_never_uses_the_open_segment_or_unbounded_history(self):
        with tempfile.TemporaryDirectory(prefix='keep-live-ring-') as directory:
            manager = LiveManager(directory, lambda request: [], FFMPEG)
            folder = Path(directory) / 'fixture'
            folder.mkdir()
            for index in range(90):
                (folder / f'segment-{index:08d}.ts').write_bytes(b'fixture')
            session = {'folder': folder, 'segments': [], 'status': 'connecting'}
            manager._refresh_segments(session)
            self.assertEqual(len(session['segments']), 60)
            self.assertEqual(session['segments'][-1].name, 'segment-00000088.ts')
            self.assertTrue((folder / 'segment-00000089.ts').exists())
            self.assertFalse((folder / 'segment-00000000.ts').exists())
            self.assertEqual(session['status'], 'buffering')
            manager.close()


class LiveURLTests(unittest.TestCase):
    def test_live_and_recorded_links_are_normalized_and_other_sites_rejected(self):
        self.assertEqual(server.validate_url('https://twitch.tv/Example'), ('https://www.twitch.tv/Example', 'Twitch'))
        self.assertEqual(server.validate_url('https://youtube.com/@Example/live')[1], 'YouTube')
        self.assertEqual(server.validate_url('https://www.twitch.tv/videos/123')[0], 'https://www.twitch.tv/videos/123')
        for url in ('https://twitch.tv/directory', 'https://twitch.tv/example/collections',
                    'http://127.0.0.1/live', 'https://twitch.tv@evil.example/test'):
            with self.subTest(url=url), self.assertRaises(ValueError): server.validate_url(url)


if __name__ == '__main__': unittest.main()
