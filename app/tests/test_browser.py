"""Browser checks for public update notices and the actual 9:16 video preview.

Run with: python3 -m unittest discover -s app/tests -p test_browser.py -v
Requires Playwright's Python package, Chromium, and FFmpeg. No network is used.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from urllib.parse import urlsplit
from urllib.request import urlopen

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

APP = Path(__file__).resolve().parents[1]
CHROMIUM = os.environ.get('KEEP_TEST_CHROMIUM') or shutil.which('chromium') or shutil.which('chromium-browser')
FFMPEG = shutil.which('ffmpeg')


@unittest.skipUnless(sync_playwright and CHROMIUM and FFMPEG, 'Playwright, Chromium and FFmpeg are required')
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='keep-browser-tests-')
        cls.root = Path(cls.temp.name)
        cls.app = cls.root / 'app'
        shutil.copytree(APP, cls.app, ignore=shutil.ignore_patterns(
            '__pycache__', 'tests', 'data', 'downloads', 'backups', '.keep-*.lock', '*.pyc'))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            cls.port = sock.getsockname()[1]
        cls.base = f'http://127.0.0.1:{cls.port}'
        cls.server = subprocess.Popen([sys.executable, str(cls.app / 'server.py'), '--port', str(cls.port)],
                                      cwd=cls.app, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for attempt in range(100):
            try:
                with urlopen(cls.base + '/api/session', timeout=.5) as response:
                    cls.version = json.load(response)['version']
                break
            except OSError:
                if cls.server.poll() is not None:
                    raise RuntimeError('Keep exited during browser-test startup')
                time.sleep(.05)
        else:
            raise RuntimeError('Keep did not start for browser tests')
        cls.landscape = cls.make_video('landscape.mp4', 640, 360, False)
        cls.portrait = cls.make_video('portrait.mp4', 180, 640, True)
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(executable_path=CHROMIUM, headless=True,
                                                   args=['--no-sandbox', '--autoplay-policy=no-user-gesture-required'])

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()
        cls.server.terminate()
        try:
            cls.server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            cls.server.kill()
            cls.server.wait()
        cls.temp.cleanup()

    @classmethod
    def make_video(cls, name, width, height, portrait):
        target = cls.root / name
        boxes = []
        for phase, colors in enumerate([['red', 'lime', 'blue'], ['yellow', 'magenta', 'cyan']]):
            for index, color in enumerate(colors):
                x, y = (0, index * height // 3) if portrait else (index * width // 3, 0)
                w, h = (width, height // 3 + 1) if portrait else (width // 3 + 1, height)
                boxes.append(f"drawbox=x={x}:y={y}:w={w}:h={h}:color={color}:t=fill:enable='{'lt(t,1)' if phase == 0 else 'gte(t,1)'}'")
        subprocess.run([FFMPEG, '-hide_banner', '-loglevel', 'error', '-y', '-f', 'lavfi', '-i',
                        f'color=c=black:s={width}x{height}:r=12:d=2', '-vf', ','.join(boxes),
                        '-c:v', 'libx264', '-threads', '1', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(target)],
                       check=True, capture_output=True)
        return target.read_bytes()

    def setUp(self):
        self.context = self.browser.new_context(viewport={'width': 1440, 'height': 1100})
        self.context.add_init_script("localStorage.setItem('keep-seen-version', " + json.dumps(self.version) + ")")
        self.page = self.context.new_page()
        self.page.set_default_timeout(7000)
        self.errors = []
        self.page.on('pageerror', lambda error: self.errors.append(str(error)))
        self.check_calls = 0
        self.download_calls = 0
        self.status = {'state': 'idle', 'current_version': self.version}
        self.check_result = {'state': 'available', 'current_version': self.version,
                             'latest_version': '2.1.0', 'update_available': True,
                             'can_download': True, 'can_apply': False,
                             'message': 'Keep 2.1.0 disponível.'}
        self.context.route('**/api/update**', self.update_route)
        self.context.route('**/fixture-*.mp4', self.video_route)

    def tearDown(self):
        self.context.close()
        self.assertEqual(self.errors, [], 'Browser JavaScript must finish without exceptions')

    def update_route(self, route):
        path = urlsplit(route.request.url).path
        if path == '/api/update/check':
            self.check_calls += 1
            self.status = dict(self.check_result)
        elif path == '/api/update/download':
            self.download_calls += 1
            self.status = {**self.status, 'state': 'ready', 'can_download': False,
                           'can_apply': True, 'message': 'Pacote verificado.'}
        elif path != '/api/update':
            route.continue_()
            return
        route.fulfill(status=200, content_type='application/json', body=json.dumps(self.status))

    def video_route(self, route):
        content = self.portrait if 'portrait' in route.request.url else self.landscape
        # Match the production server's byte-range contract so Chromium can seek.
        headers = {'Accept-Ranges': 'bytes'}
        requested = route.request.headers.get('range')
        status = 200
        if requested and requested.startswith('bytes='):
            start, end = requested[6:].split('-', 1)
            start, end = int(start or 0), int(end) if end else len(content) - 1
            end = min(end, len(content) - 1)
            headers['Content-Range'] = f'bytes {start}-{end}/{len(content)}'
            content = content[start:end + 1]
            status = 206
        route.fulfill(status=status, headers=headers, content_type='video/mp4', body=content)

    def open_app(self):
        self.page.goto(self.base)
        self.page.wait_for_function("() => typeof token === 'string' && token.length > 0")
        self.page.wait_for_function("() => updateStatus?.state === 'available' || updateStatus?.state === 'current'")
        # Release notes may open automatically; they are unrelated to these controls.
        self.page.evaluate("if ($('release-dialog').open) $('release-dialog').close()")

    def test_update_published_after_open_is_found_without_manual_click(self):
        self.check_result={'state':'current','current_version':self.version,'latest_version':self.version,'update_available':False}
        self.page.clock.install()
        self.open_app()
        self.assertEqual(self.check_calls,1)
        self.assertFalse(self.page.locator('#update-banner').is_visible())
        self.check_result={'state':'available','current_version':self.version,'latest_version':'9.9.9','update_available':True,'can_download':True}
        self.page.clock.fast_forward(300001)
        self.page.wait_for_function("()=>updateStatus?.latest_version==='9.9.9'")
        self.assertGreaterEqual(self.check_calls,2)
        self.assertTrue(self.page.locator('#update-banner').is_visible())
        self.assertEqual(self.download_calls,0)

    def test_returning_to_the_tab_checks_again_after_a_minute(self):
        self.open_app()
        initial=self.check_calls
        self.page.evaluate("lastUpdateCheckAt=Date.now()-61000;document.dispatchEvent(new Event('visibilitychange'))")
        self.page.wait_for_function("()=>!updateTransferBusy")
        self.assertEqual(self.check_calls,initial+1)
        self.assertEqual(self.download_calls,0)

    def test_update_checks_automatically_and_notice_opens_settings(self):
        self.open_app()
        self.assertEqual(self.check_calls, 1)
        self.assertTrue(self.page.locator('#update-banner').is_visible())
        self.assertTrue(self.page.locator('#update-badge').is_visible())
        self.assertIn('2.1.0', self.page.locator('#update-banner-text').inner_text())
        self.page.locator('#update-banner-action').click()
        self.assertTrue(self.page.locator('#settings-view').is_visible())
        self.assertTrue(self.page.locator('#download-update').is_visible())
        self.page.locator('#download-update').click()
        self.page.wait_for_function("() => updateStatus?.state === 'ready'")
        self.assertEqual(self.download_calls, 1)
        self.assertTrue(self.page.locator('#apply-update').is_visible())
        self.assertTrue(self.page.locator('#apply-update').is_enabled())
        self.assertEqual(len(self.context.pages), 1, 'Public checks must not open login pop-ups')

    def test_manual_check_and_current_version_clear_notice(self):
        self.open_app()
        self.page.locator('#update-banner-action').click()
        self.check_result = {'state': 'current', 'current_version': self.version,
                             'latest_version': self.version, 'update_available': False,
                             'can_download': False, 'can_apply': False}
        self.page.locator('#check-update').click()
        self.page.wait_for_function("() => updateStatus?.state === 'current'")
        self.assertEqual(self.check_calls, 2)
        self.assertFalse(self.page.locator('#update-banner').is_visible())
        self.assertFalse(self.page.locator('#update-badge').is_visible())
        self.assertEqual(len(self.context.pages), 1)

    def test_known_update_remains_visible_after_failed_recheck(self):
        self.open_app()
        self.page.locator('#update-banner-action').click()
        self.check_result = {**self.check_result, 'state': 'failed', 'can_download': False,
                             'error': 'Sem conexão para verificar novamente.'}
        self.page.locator('#check-update').click()
        self.page.wait_for_function("() => updateStatus?.state === 'failed'")
        self.assertTrue(self.page.locator('#update-banner').is_visible())
        self.assertTrue(self.page.locator('#update-badge').is_visible())
        self.assertIn('Sem conexão', self.page.locator('#update-status').inner_text())

    def prepare_crop(self, portrait=False):
        self.check_result = {'state': 'current', 'current_version': self.version,
                             'latest_version': self.version, 'update_available': False}
        self.open_app()
        width, height = (180, 640) if portrait else (640, 360)
        metadata = {'title': 'Prévia de teste', 'platform': 'YouTube', 'duration': 2,
                    'width': width, 'height': height, 'heights': [height], 'video_index': 1}
        self.context.route('**/api/inspect', lambda route: route.fulfill(
            content_type='application/json', body=json.dumps(metadata)))
        self.context.route('**/api/preview', lambda route: route.fulfill(
            content_type='application/json', body=json.dumps({
                'id': 'fixture', 'status': 'ready', 'fps': 12, 'duration': 2,
                'width': width, 'height': height,
                'media_url': '/fixture-portrait.mp4' if portrait else '/fixture-landscape.mp4'})))
        self.page.locator('#video-url').fill('https://www.youtube.com/watch?v=fixture')
        self.page.locator('#inspect').click()
        self.page.locator('#video-result').wait_for(state='visible')
        self.page.locator('#download-scope').select_option('section')
        self.page.locator('#vertical-enabled').check()
        self.assertTrue(self.page.locator('#vertical-preview-note').is_visible())
        self.assertIn('Prepare a prévia', self.page.locator('#vertical-preview-note').inner_text())
        self.assertFalse(self.page.locator('#vertical-preview').is_visible())
        self.page.locator('#prepare-preview').click()
        self.page.wait_for_function("() => $('preview-player').readyState >= 2 && !$('vertical-preview').hidden")
        self.page.locator('#vertical-output').scroll_into_view_if_needed()

    def pixel(self):
        return self.page.evaluate("Array.from($('vertical-preview').getContext('2d').getImageData(180,320,1,1).data)")

    def assert_color(self, color):
        self.page.wait_for_function("""color => {
            const p=$('vertical-preview').getContext('2d').getImageData(180,320,1,1).data;
            return color.every((value,index)=>Math.abs(value-p[index])<30);
        }""", arg=color)

    def set_slider(self, slider, value):
        self.page.locator('#' + slider).evaluate("(node,value)=>{node.value=String(value);node.dispatchEvent(new Event('input',{bubbles:true}));}", value)

    def test_vertical_preview_pixels_follow_slider_drag_seek_and_reset(self):
        self.prepare_crop()
        self.assert_color([0, 255, 0])
        self.set_slider('crop-x', 0)
        self.assert_color([255, 0, 0])
        self.set_slider('crop-x', 100)
        self.assert_color([0, 0, 255])
        self.page.locator('#center-crop').click()
        self.assert_color([0, 255, 0])
        # Drag the actual overlay frame to the left edge of the original image.
        frame = self.page.locator('#crop-drag-handle').bounding_box()
        self.page.mouse.move(frame['x'] + 2, frame['y'] + frame['height']/2)
        self.page.mouse.down()
        self.page.mouse.move(frame['x'] - frame['width'], frame['y'] + frame['height']/2, steps=6)
        self.page.mouse.up()
        self.assertLess(float(self.page.locator('#crop-x').input_value()), 10)
        self.assert_color([255, 0, 0])
        self.page.locator('#center-crop').click()
        self.page.evaluate("$('preview-player').currentTime=1.5")
        self.assert_color([255, 0, 255])
        self.page.locator('#cancel-preview').click()
        self.assertFalse(self.page.locator('#vertical-preview').is_visible())
        self.assertEqual(self.pixel(), [0, 0, 0, 0], 'Discarded media must not leave a stale cropped frame')
        self.assertFalse(self.page.locator('#preview-player-wrap').is_visible())

    def test_vertical_preview_tracks_playback(self):
        self.prepare_crop()
        self.assert_color([0, 255, 0])
        self.page.evaluate("$('preview-player').play()")
        self.assert_color([255, 0, 255])
        self.page.evaluate("$('preview-player').pause()")

    def test_mobile_preview_and_update_notice_fit_viewport(self):
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.open_app()
        self.assertTrue(self.page.locator('#update-banner').is_visible())
        self.assertLessEqual(self.page.evaluate('() => document.documentElement.scrollWidth'), 390)
        self.prepare_crop()
        self.assert_color([0, 255, 0])
        self.assertLessEqual(self.page.evaluate('() => document.documentElement.scrollWidth'), 390)

    def test_portrait_crop_moves_vertically_and_native_9_16_is_not_recropped(self):
        self.prepare_crop(portrait=True)
        self.assert_color([0, 255, 0])
        self.set_slider('crop-y', 0)
        self.assert_color([255, 0, 0])
        self.set_slider('crop-y', 100)
        self.assert_color([0, 0, 255])
        self.page.evaluate("current.width=180;current.height=320;updateVertical()")
        self.assertTrue(self.page.locator('#vertical-enabled').is_disabled())
        self.assertFalse(self.page.locator('#vertical-enabled').is_checked())
        self.assertFalse(self.page.locator('#vertical-output').is_visible())
        self.assertEqual(self.pixel(), [0, 0, 0, 0])


if __name__ == '__main__':
    unittest.main()
