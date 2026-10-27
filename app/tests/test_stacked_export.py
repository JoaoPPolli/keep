"""Exercise source selections and the actual stacked FFmpeg output."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from media_editor import probe_media, stacked_geometry, transform_command, validate_edit


def request(**changes):
    return dict({'sections': [{'start': 0, 'end': .6}], 'mode': 'video',
                 'vertical': True, 'layout': 'stacked',
                 'camera': {'x': 0, 'y': 0, 'width': 1 / 3, 'height': .4},
                 'content': {'x': .5, 'y': 0, 'width': .5, 'height': 1},
                 'camera_height': .3}, **changes)


class StackedValidationTests(unittest.TestCase):
    def test_legacy_full_mode_and_stack_defaults(self):
        original = validate_edit({'sections': [{'start': 0, 'end': 1}]}, 1)
        self.assertEqual(original['layout'], 'full')
        self.assertFalse(original['vertical'])
        stacked = validate_edit({'sections': [{'start': 0, 'end': 1}],
                                 'vertical': True, 'layout': 'stacked'}, 1)
        self.assertEqual(stacked['camera_height'], .3)
        self.assertEqual(stacked['camera']['width'], .28)

    def test_invalid_regions_and_modes_are_rejected(self):
        invalid = [request(vertical=False), request(mode='audio'), request(mode='gif'),
                   request(layout='sideways'), request(camera_height=True),
                   request(camera_height=float('nan')), request(camera_height=.61)]
        for rectangle in ({'x': .9, 'y': 0, 'width': .2, 'height': 1},
                          {'x': 0, 'y': 0, 'width': 0, 'height': 1},
                          {'x': 0, 'y': 0, 'width': True, 'height': 1},
                          {'x': 0, 'y': 0, 'width': float('inf'), 'height': 1},
                          {'x': 0, 'y': 0, 'width': .5, 'height': 1, 'command': 'anything'}):
            invalid.append(request(camera=rectangle))
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_edit(value, .6)

    def test_geometry_stays_even_and_inside_selections(self):
        edit = validate_edit(request(camera={'x': .03, 'y': .02, 'width': .337, 'height': .313}), .6)
        geometry = stacked_geometry(edit, {'width': 1919, 'height': 1079})
        self.assertEqual(geometry['width'], 1080)
        self.assertEqual(geometry['height'], 1920)
        self.assertEqual(geometry['camera_height'] + geometry['content_height'], 1920)
        for pane in ('camera', 'content'):
            crop = geometry[pane]
            self.assertTrue(all(number % 2 == 0 for number in crop.values()))
            self.assertGreaterEqual(crop['width'], 2)
            self.assertGreaterEqual(crop['height'], 2)
            self.assertLessEqual(crop['x'] + crop['width'], 1919)
            self.assertLessEqual(crop['y'] + crop['height'], 1079)
        with self.assertRaises(ValueError):
            stacked_geometry(validate_edit(request(camera={'x': 0, 'y': 0, 'width': .00001, 'height': 1}), .6),
                             {'width': 640, 'height': 360})


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
class StackedExportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='keep-stacked-export-')
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source.mp4'
        subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                        '-f', 'lavfi', '-i', 'color=blue:s=360x200:r=10:d=0.6',
                        '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.6',
                        '-vf', 'drawbox=x=0:y=0:w=120:h=80:color=red:t=fill',
                        '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p',
                        '-c:a', 'aac', '-shortest', str(self.source)],
                       check=True, capture_output=True)

    def tearDown(self):
        self.temporary.cleanup()

    def test_export_stacks_selected_camera_over_content_and_preserves_audio(self):
        info = probe_media('ffmpeg', self.source)
        edit = validate_edit(request(), info['duration'])
        output = self.root / 'stacked.mp4'
        command, _ = transform_command('ffmpeg', self.source, output, edit, info)
        subprocess.run(command, check=True, capture_output=True)
        final = probe_media('ffmpeg', output)
        self.assertEqual((final['width'], final['height']), (360, 640))
        self.assertTrue(final['audio'])
        self.assertAlmostEqual(final['duration'], edit['duration'], delta=.1)
        frame = subprocess.check_output(['ffmpeg', '-hide_banner', '-loglevel', 'error',
                                         '-i', str(output), '-frames:v', '1', '-threads', '1',
                                         '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'])
        def pixel(x, y):
            index = (y * final['width'] + x) * 3
            return tuple(frame[index:index + 3])
        top, bottom = pixel(180, 80), pixel(180, 450)
        self.assertGreater(top[0], 220)
        self.assertLess(max(top[1:]), 35)
        self.assertGreater(bottom[2], 220)
        self.assertLess(max(bottom[:2]), 35)

    def test_native_vertical_source_still_permits_stacked_composition(self):
        edit = validate_edit(request(), .6)
        command, note = transform_command('ffmpeg', self.source, self.root / 'unused.mp4', edit,
                                          {'width': 180, 'height': 320, 'audio': False})
        self.assertIn('vstack=inputs=2', command[command.index('-filter_complex') + 1])
        self.assertIsNone(note)


if __name__ == '__main__':
    unittest.main()
