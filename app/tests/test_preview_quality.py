"""Real preview encoding: resolution choices never replace the export source."""
from contextlib import contextmanager
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
from preview_media import PreviewManager, preview_resolution
import test_subtitle_api as api

FFMPEG = shutil.which('ffmpeg')


def wait_ready(manager, task):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        status = manager.status(task['id'])
        if status['status'] != 'preparing':
            if status['status'] != 'ready':
                raise AssertionError(status)
            return status, status['media_url'].split('key=')[1]
        time.sleep(.03)
    raise AssertionError('Preview preparation timed out')


class PreviewResolutionTests(unittest.TestCase):
    def test_custom_resolution_requires_pixels_and_rounds_to_even(self):
        self.assertEqual(preview_resolution(3), 2)
        self.assertEqual(preview_resolution(145), 144)
        self.assertEqual(preview_resolution(), 480)
        for value in (None, True, '144', 144.5, 0, 1, -2, 8193, float('nan')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                preview_resolution(value)

    def test_remote_preview_selects_video_resolution_with_independent_audio(self):
        @contextmanager
        def cookies(*args):
            yield []
        with patch.object(server, 'cookie_options', cookies), patch.object(server, 'base_command', return_value=['yt-dlp']):
            for height in (2, 144, 720, 1080):
                with server.preview_command({'url':'https://youtu.be/example', 'browser':'none',
                                             'platform':'YouTube', 'preview_height':height}, Path('/tmp/preview')) as command:
                    self.assertEqual(command[command.index('-S') + 1], f'res:{height},proto:https')
                    self.assertEqual(command[command.index('-f') + 1], 'bv+ba/b')


@unittest.skipUnless(FFMPEG, 'FFmpeg required')
class PreviewEncodingTests(unittest.TestCase):
    def test_all_resolutions_keep_the_original_and_identical_aac_packets(self):
        with tempfile.TemporaryDirectory(prefix='keep-preview-quality-') as directory:
            root = Path(directory)
            original = root/'original.mp4'
            subprocess.run([FFMPEG,'-v','error','-f','lavfi','-i','testsrc2=s=960x540:r=12:d=1',
                '-f','lavfi','-i','sine=frequency=440:sample_rate=48000:duration=1',
                '-c:v','libx264','-threads','1','-c:a','aac','-b:a','128k',str(original)], check=True)
            content = original.read_bytes()
            def audio(path):
                return subprocess.check_output([FFMPEG,'-v','error','-i',str(path),'-map','0:a:0',
                    '-c:a','copy','-f','md5','-'])
            expected_audio = audio(original)
            manager = PreviewManager(root/'previews', lambda *args: None, FFMPEG)
            try:
                for height in (2, 64, 144, 480, 720, 1080):
                    with self.subTest(height=height):
                        status, key = wait_ready(manager, manager.import_clip(original, imported=True, preview_height=height))
                        self.assertEqual(min(status['width'], status['height']), min(height, 540))
                        self.assertEqual(status['preview_height'], height)
                        self.assertAlmostEqual(status['duration'], 1, delta=.1)
                        self.assertEqual(audio(manager.media_file(status['id'], key)), expected_audio)
                        self.assertEqual(manager.live_file(status['id'], key).read_bytes(), content)
                        manager.cancel(status['id'])
                self.assertEqual(original.read_bytes(), content)
            finally:
                manager.close()

    def test_rebuilding_a_live_preview_protects_the_original_during_cache_eviction(self):
        with tempfile.TemporaryDirectory(prefix='keep-preview-rebuild-') as directory:
            root=Path(directory);source=root/'portrait.mp4'
            subprocess.run([FFMPEG,'-v','error','-f','lavfi','-i','testsrc2=s=180x320:r=12:d=1',
                '-c:v','libx264','-threads','1',str(source)],check=True)
            content=source.read_bytes()
            manager=PreviewManager(root/'previews',lambda *args:None,FFMPEG)
            try:
                first,key=wait_ready(manager,manager.import_clip(source,preview_height=24))
                self.assertEqual((first['width'],first['height']),(24,42))
                # Fill the two-entry cache. Rebuilding must evict the other item.
                other,_=wait_ready(manager,manager.import_clip(source))
                rebuilt,newkey=wait_ready(manager,manager.recreate(first['id'],key,144))
                self.assertEqual((rebuilt['width'],rebuilt['height']),(144,256))
                self.assertEqual(manager.live_file(first['id'],key).read_bytes(),content)
                self.assertEqual(manager.live_file(rebuilt['id'],newkey).read_bytes(),content)
                with self.assertRaises(ValueError):manager.status(other['id'])
                with self.assertRaises(ValueError):manager.recreate(first['id'],'wrong',2)
                with self.assertRaises(ValueError):manager.recreate(first['id'],key,1)
                self.assertEqual(manager.live_file(first['id'],key).read_bytes(),content)
                # An unedited export must keep the original pixels and bytes.
                with patch.multiple(server,ROOT=root,DATA=root/'data',DOWNLOADS=root/'downloads',
                                    JOBS={},SETTINGS={},SHUTTING_DOWN=False):
                    (root/'data').mkdir()
                    original=manager.live_file(rebuilt['id'],newkey)
                    job=server.save_live_clip(original,probe_media(FFMPEG,original),
                        {'url':'','local_file':True,'title':'Original preserved'}, {'mode':'video'})
                    self.assertEqual((root/server.JOBS[job['id']]['file']).read_bytes(),content)
            finally:manager.close()

    def test_non_aac_audio_has_the_same_settings_at_low_and_high_video_resolution(self):
        with tempfile.TemporaryDirectory(prefix='keep-preview-audio-') as directory:
            root=Path(directory);source=root/'pcm.mkv'
            subprocess.run([FFMPEG,'-v','error','-f','lavfi','-i','color=s=320x180:r=12:d=1',
                '-f','lavfi','-i','sine=sample_rate=48000:duration=1','-c:v','libx264','-threads','1',
                '-c:a','pcm_s16le',str(source)],check=True)
            manager=PreviewManager(root/'previews',lambda *args:None,FFMPEG)
            try:
                results=[]
                for height in (2,480):
                    status,key=wait_ready(manager,manager.import_clip(source,imported=True,preview_height=height))
                    audio=subprocess.check_output([FFMPEG,'-v','error','-i',str(manager.media_file(status['id'],key)),
                        '-map','0:a:0','-c:a','copy','-f','md5','-'])
                    results.append(audio)
                self.assertEqual(results[0],results[1])
            finally:manager.close()


class PreviewQualityAPITests(unittest.TestCase):
    setUp=api.SubtitleAPITests.setUp
    tearDown=api.SubtitleAPITests.tearDown
    request=api.SubtitleAPITests.request

    def test_remote_quality_is_validated_and_forwarded_separately(self):
        with patch.object(server,'selected_metadata',return_value={'duration':2}) as metadata, \
             patch.object(self.previews,'create',return_value={'id':'preview','status':'preparing'}) as create:
            body={'url':'https://youtu.be/example','preview_height':145}
            code,_=self.request('preview',body)
            self.assertEqual(code,202)
            request=create.call_args.args[0]
            self.assertEqual(request['preview_height'],144)
            self.assertNotIn('quality',request)
            for value in (1,'2',True,9000):
                metadata.reset_mock();create.reset_mock()
                self.assertEqual(self.request('preview',{**body,'preview_height':value})[0],400)
                metadata.assert_not_called();create.assert_not_called()

    def test_rebuild_cannot_read_arbitrary_paths_or_recorded_previews(self):
        for body in ({'clip':{'id':self.id,'key':'wrong'},'preview_height':2},
                     {'clip':{'id':self.id,'key':self.key},'preview_height':2},
                     {'clip':'/etc/passwd','preview_height':2},
                     {'source':'/etc/passwd','preview_height':2}):
            self.assertEqual(self.request('preview',body)[0],400)
        self.assertEqual(self.request('preview',{'clip':{},'preview_height':2},token=False)[0],403)
