"""Compare the preview's selected pixels with the actual FFmpeg export filter."""
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
import unittest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from media_editor import transform_command


@unittest.skipUnless(shutil.which('node') and shutil.which('ffmpeg'), 'Node and FFmpeg are required')
class CropGeometryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (APP / 'dist/app.js').read_text(encoding='utf-8')
        # Run the browser's pure geometry helper without booting its DOM/API client.
        cls.helper = source[source.index('function cropPixels('):source.index('function cropGeometry(')]

    def geometry(self, cases):
        script = self.helper + '\nconsole.log(JSON.stringify(' + json.dumps(cases) + '.map(args=>cropPixels(...args))))'
        return json.loads(subprocess.check_output(['node', '-e', script], text=True))

    def test_preview_selects_the_same_pixels_as_export(self):
        cases = [(1920, 1080, .5, .5), (1920, 1080, 0, 0), (1920, 1080, 1, 1),
                 (720, 1600, .25, .75), (640, 480, .337, .991), (1920, 1080, .123, .876)]
        for (width, height, x, y), rectangle in zip(cases, self.geometry(cases)):
            with self.subTest(width=width, height=height, x=x, y=y):
                edit = {'sections': [{'start': 0, 'end': 1}], 'mode': 'video',
                        'vertical': True, 'crop': {'x': x, 'y': y}}
                command, _ = transform_command('ffmpeg', 'input.mp4', 'output.mp4', edit,
                                               {'width': width, 'height': height, 'audio': False})
                filters = command[command.index('-filter_complex') + 1]
                actual_filter = re.search(r'\[vjoin\](crop=.*?),scale=', filters).group(1)
                preview_filter = 'crop={width}:{height}:{x}:{y}'.format(**rectangle)
                source = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                          f'testsrc2=size={width}x{height}:rate=1', '-frames:v', '1', '-threads', '1']
                actual = subprocess.check_output(source + ['-vf', actual_filter, '-f', 'md5', '-'])
                preview = subprocess.check_output(source + ['-vf', preview_filter, '-f', 'md5', '-'])
                self.assertEqual(actual, preview)
                self.assertEqual(rectangle['width'] * 16, rectangle['height'] * 9)

    def test_unusable_small_frames_do_not_create_a_zero_size_crop(self):
        self.assertEqual(self.geometry([(10, 20, .5, .5), (1920, 20, 0, 1)]), [None, None])


if __name__ == '__main__':
    unittest.main()
