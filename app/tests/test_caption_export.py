"""Validate real subtitle pixels, timeline remapping and untrusted caption text."""
from pathlib import Path
import math
import shutil
import subprocess
import sys
import tempfile
import unittest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from media_editor import (caption_filter, caption_timeline, probe_media,
                          transform_command, validate_captions, validate_edit,
                          wrap_caption_text)


def cue(start=.5, end=1.5, text='Olá, mundo!'):
    return {'start': start, 'end': end, 'text': text}


def request(**changes):
    return dict({'sections': [{'start': 0, 'end': 3}], 'mode': 'video',
                 'captions': {'enabled': True, 'cues': [cue()]}}, **changes)


class CaptionValidationTests(unittest.TestCase):
    def test_invalid_cues_fail_before_export(self):
        invalid = [[], [cue(-1, 1)], [cue(1, 1)], [cue(1, 4)],
                   [cue(float('nan'), 1)], [cue(0, float('inf'))],
                   [cue(0, 1, '')], [cue(0, 1, 'x' * 181)],
                   [cue(0, 1, 'text\0control')], [cue(0, 2), cue(1, 2.5)],
                   [dict(cue(), filename='/etc/passwd')], [cue()] * 4001]
        for cues in invalid:
            with self.subTest(cues=cues[:2]), self.assertRaises(ValueError):
                validate_captions({'enabled': True, 'cues': cues}, 3)
        for mode in ('audio', 'gif'):
            with self.subTest(mode=mode), self.assertRaises(ValueError):
                validate_edit(request(mode=mode), 3)
        with self.assertRaises(ValueError):
            validate_captions({'enabled': 'yes', 'cues': [cue()]}, 3)

    def test_normalization_preserves_literal_caption_content(self):
        text = "Olá 100%: 'teste'; %{pts} [x] {\\an8} \\N"
        value = validate_captions({'enabled': True, 'cues': [cue(text=text)]}, 3)
        self.assertEqual(value['cues'][0]['text'], text)
        self.assertIsNone(validate_captions({'enabled': False}, 3))
        self.assertNotIn('captions', validate_edit(request(captions={'enabled': False}), 3))

    def test_long_text_keeps_all_words_and_uses_at_most_two_lines(self):
        text = 'Esta é uma legenda longa que foi revisada pelo usuário e deve manter todas as palavras sem cortar o final do texto.'
        lines = wrap_caption_text(text)
        self.assertEqual(' '.join(lines), text)
        self.assertLessEqual(len(lines), 2)
        self.assertLess(32 / max(map(len, lines)), 1)

    def test_timeline_clips_and_reorders_captions_with_sections(self):
        edit = validate_edit(request(
            sections=[{'start': 2, 'end': 3}, {'start': 0, 'end': 1}],
            captions={'enabled': True, 'cues': [cue(.1, .8, 'Earlier'), cue(1.8, 2.8, 'Later')]}), 3)
        mapped = caption_timeline(edit)
        self.assertEqual([x['text'] for x in mapped], ['Later', 'Earlier'])
        self.assertAlmostEqual(mapped[0]['start'], 0)
        self.assertAlmostEqual(mapped[0]['end'], .8)
        self.assertAlmostEqual(mapped[1]['start'], 1.1)
        self.assertAlmostEqual(mapped[1]['end'], 1.8)

    def test_many_captions_do_not_expand_ffmpeg_argv(self):
        cues = [cue(index * .1, index * .1 + .08, 'Legenda') for index in range(1000)]
        edit = validate_edit(request(sections=[{'start': 0, 'end': 100}],
                                     captions={'enabled': True, 'cues': cues}), 100)
        with tempfile.TemporaryDirectory() as directory:
            command, _ = transform_command('ffmpeg', 'input.mp4', 'output.mp4', edit,
                {'width': 480, 'height': 270, 'audio': False}, caption_dir=directory)
            self.assertLess(sum(map(len, command)), 5000)
            script = Path(directory, 'caption-commands.txt').read_text(encoding='utf-8')
            self.assertEqual(script.count(' text '), 2000)
            self.assertNotIn('Legenda', ' '.join(command))


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
class CaptionExportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory(prefix='keep-caption-export-')
        cls.root = Path(cls.temporary.name)
        cls.source = cls.root / 'black.mp4'
        cls.colored = cls.root / 'colored.mp4'
        for path, color, extra in [(cls.source, 'black', []), (cls.colored, 'blue',
                ['-vf', 'drawbox=x=0:y=0:w=160:h=100:color=red:t=fill'])]:
            subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
                '-f', 'lavfi', '-i', f'color={color}:s=480x270:r=10:d=3',
                '-f', 'lavfi', '-i', 'sine=frequency=440:duration=3',
                *extra, '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p',
                '-c:a', 'aac', '-shortest', str(path)], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def export(self, body, name, source=None):
        source = source or self.source
        info = probe_media('ffmpeg', source)
        edit = validate_edit(body, info['duration'])
        output, sidecars = self.root / (name + '.mp4'), self.root / (name + '-captions')
        command, _ = transform_command('ffmpeg', source, output, edit, info, caption_dir=sidecars)
        result = subprocess.run(command, cwd=sidecars, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        self.assertNotIn('Failed to process command', result.stderr)
        return output

    def frame(self, path, timestamp):
        return subprocess.check_output(['ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-ss', str(timestamp), '-i', str(path), '-frames:v', '1', '-threads', '1',
            '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-'])

    def whites(self, path, timestamp, width=480, height=270):
        frame = self.frame(path, timestamp)
        bottom = frame[math.floor(height * .65) * width * 3:]
        return sum(min(bottom[i:i + 3]) > 180 for i in range(0, len(bottom), 3))

    def test_captions_appear_only_during_their_interval(self):
        output = self.export(request(), 'timed')
        self.assertEqual(self.whites(output, .2), 0)
        self.assertGreater(self.whites(output, .8), 150)
        self.assertEqual(self.whites(output, 2), 0)
        self.assertTrue(probe_media('ffmpeg', output)['audio'])

    def test_joined_reordered_sections_render_the_corresponding_text(self):
        output = self.export(request(
            sections=[{'start': 2, 'end': 3}, {'start': 0, 'end': 1}],
            captions={'enabled': True, 'cues': [cue(.1, .8, 'I'), cue(2.1, 2.8, 'MMMMMMMM')]}), 'joined')
        self.assertGreater(self.whites(output, .5), self.whites(output, 1.5) * 4)
        self.assertEqual(self.whites(output, .95), 0)
        self.assertEqual(self.whites(output, 1.95), 0)

    def test_stacked_camera_content_and_caption_are_all_exported(self):
        output = self.export(request(vertical=True, layout='stacked', camera_height=.3,
            camera={'x': 0, 'y': 0, 'width': 1 / 3, 'height': .37},
            content={'x': .5, 'y': 0, 'width': .5, 'height': 1}), 'stacked', self.colored)
        info = probe_media('ffmpeg', output)
        self.assertEqual((info['width'], info['height']), (468, 832))
        self.assertTrue(info['audio'])
        frame = self.frame(output, .8)
        pixel = lambda x, y: frame[(y * 468 + x) * 3:(y * 468 + x) * 3 + 3]
        self.assertGreater(pixel(200, 100)[0], 220)
        self.assertGreater(pixel(200, 500)[2], 220)
        self.assertGreater(self.whites(output, .8, 468, 832), 100)
        self.assertEqual(self.whites(output, .2, 468, 832), 0)

    def test_runtime_text_matches_a_literal_textfile_for_filter_metacharacters(self):
        text = "Olá 100%: 'teste'; %{pts} [x] {\\an8} \\N"
        with tempfile.TemporaryDirectory(prefix='keep-caption-literal-') as temporary:
            directory = Path(temporary)
            edit = validate_edit(request(captions={'enabled': True, 'cues': [cue(0, 2, text)]}), 3)
            dynamic = caption_filter(edit, 480, directory)
            lines = wrap_caption_text(text)
            (directory / 'expected.txt').write_text('\n'.join(lines), encoding='utf-8')
            size = 480 * .052 * min(1, 32 / max(map(len, lines)))
            static = (f'drawtext=fontfile=caption-font.ttf:textfile=expected.txt:expansion=none:'
                f'fontsize={size:.6f}:line_spacing={round(size * .2)}:fontcolor=white:'
                f'bordercolor=black:borderw={size * .09:.6f}:x=(w-text_w)/2:y=h*0.92-text_h')
            def rendered(filters):
                command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi',
                    '-i', 'color=black:s=480x270:r=10:d=1', '-vf', filters,
                    '-frames:v', '1', '-threads', '1', '-pix_fmt', 'rgb24', '-f', 'rawvideo', '-']
                return subprocess.check_output(command, cwd=directory)
            actual, expected = rendered(dynamic), rendered(static)
            self.assertEqual(actual, expected, 'Quotes, percent macros and braces must be literal glyphs')
            self.assertGreater(sum(min(actual[i:i + 3]) > 180 for i in range(0, len(actual), 3)), 200)


if __name__ == '__main__':
    unittest.main()
