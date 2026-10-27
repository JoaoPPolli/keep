"""Pixel-level browser checks for stacked framing and editable caption previews.

ASR responses here are explicit UI fixtures; real recognition is covered by the
caption-engine tests. Video decoding, seeking, composition and drawing are real.
"""
import json
import unittest
from urllib.parse import urlsplit

import test_browser as fixture


@unittest.skipUnless(fixture.sync_playwright and fixture.CHROMIUM and fixture.FFMPEG,
                     'Playwright, Chromium and FFmpeg are required')
class EditorBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.BrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.BrowserTests.tearDownClass.__func__)
    make_video = classmethod(fixture.BrowserTests.make_video.__func__)
    setUp = fixture.BrowserTests.setUp
    tearDown = fixture.BrowserTests.tearDown
    update_route = fixture.BrowserTests.update_route
    video_route = fixture.BrowserTests.video_route
    open_app = fixture.BrowserTests.open_app
    prepare_crop = fixture.BrowserTests.prepare_crop
    set_slider = fixture.BrowserTests.set_slider
    assert_color = fixture.BrowserTests.assert_color

    def pixel_at(self, x, y):
        return self.page.evaluate("([x,y])=>Array.from($('vertical-preview').getContext('2d').getImageData(x,y,1,1).data)", [x, y])

    def assert_pixel(self, x, y, color):
        self.page.wait_for_function("""([x,y,color])=>{
            const pixel=$('vertical-preview').getContext('2d').getImageData(x,y,1,1).data;
            return color.every((value,index)=>Math.abs(pixel[index]-value)<30);
        }""", arg=[x, y, color])

    def set_region(self, name, **values):
        self.page.locator('#region-select').select_option(name)
        for key, value in values.items():
            self.page.locator('#region-' + key).evaluate(
                "(node,value)=>{node.value=String(value);node.dispatchEvent(new Event('input',{bubbles:true}));}", value)

    def stacked(self):
        self.prepare_crop()
        self.page.locator('#vertical-layout-stacked').click()
        # Red left third for the camera; blue right third for the content.
        self.set_region('camera', x=0, y=0, width=25, height=100)
        self.set_region('content', width=25, height=100, x=75, y=0)
        self.set_slider('camera-height', 30)
        self.assert_pixel(180, 80, [255, 0, 0])
        self.assert_pixel(180, 450, [0, 0, 255])

    def caption_route(self, route):
        path = urlsplit(route.request.url).path
        if path == '/api/subtitles/cancel':
            self.caption_cancels += 1
            data = {'id': 'caption-fixture', 'status': 'cancelled'}
        else:
            if route.request.method == 'POST':
                self.caption_requests.append(route.request.post_data_json)
            if self.caption_responses:
                data = self.caption_responses.pop(0)
                self.caption_last = data
            else:
                data = self.caption_last
        route.fulfill(status=200, content_type='application/json', body=json.dumps(data))

    def mock_captions(self, statuses=None, cues=None):
        if cues is None:
            cues = [{'start': .1, 'end': .8, 'text': 'Olá mundo'},
                    {'start': 1.1, 'end': 1.8, 'text': 'Outra legenda'}]
        statuses = statuses or ['ready']
        self.caption_requests, self.caption_cancels = [], 0
        self.caption_responses = [{'id': 'caption-fixture', 'status': state,
                                   'stage': state, 'cues': cues if state == 'ready' else []}
                                  for state in statuses]
        self.caption_last = self.caption_responses[-1]
        self.context.route('**/api/subtitles**', self.caption_route)

    def seek(self, time):
        self.page.evaluate("time=>{$('preview-player').currentTime=time}", time)
        self.page.wait_for_function("time=>!$('preview-player').seeking&&Math.abs($('preview-player').currentTime-time)<.1", arg=time)

    def white_caption_pixels(self):
        return self.page.evaluate("""()=>{
            const c=$('vertical-preview'),d=c.getContext('2d').getImageData(0,Math.floor(c.height*.76),c.width,Math.floor(c.height*.2)).data;
            let n=0;for(let i=0;i<d.length;i+=4)if(d[i]>225&&d[i+1]>225&&d[i+2]>225)n++;
            return n;
        }""")

    def test_stacked_regions_and_divider_change_actual_preview_and_full_mode_survives(self):
        self.stacked()
        self.assertTrue(self.page.locator('#camera-frame').is_visible())
        self.assertTrue(self.page.locator('#content-frame').is_visible())
        self.assertFalse(self.page.locator('#crop-frame').is_visible())
        self.assert_pixel(180, 250, [0, 0, 255])
        self.set_slider('camera-height', 60)
        self.assert_pixel(180, 250, [255, 0, 0])
        self.page.locator('#stack-divider').focus()
        self.page.keyboard.press('ArrowUp')
        self.assertEqual(self.page.locator('#camera-height').input_value(), '59')
        self.page.locator('#vertical-layout-full').click()
        self.assertTrue(self.page.locator('#crop-frame').is_visible())
        self.assertFalse(self.page.locator('#camera-frame').is_visible())
        self.assert_color([0, 255, 0])
        self.page.locator('#vertical-layout-stacked').click()
        self.assert_pixel(180, 80, [255, 0, 0])
        self.assert_pixel(180, 450, [0, 0, 255])

    def test_camera_and_content_can_be_dragged_and_camera_resized(self):
        self.stacked()
        self.page.locator('#camera-drag-handle').scroll_into_view_if_needed()
        before = self.page.evaluate('({...editorRegions.camera})')
        # The transparent 18px hit stroke extends outside the clipped SVG.
        # Start on the visible 2px frame, as the UI instruction specifies.
        frame = self.page.locator('#camera-frame').bounding_box()
        self.page.mouse.move(frame['x'] + 2, frame['y'] + frame['height'] * .35)
        self.page.mouse.down()
        self.page.mouse.move(frame['x'] + 72, frame['y'] + frame['height'] * .35, steps=6)
        self.page.mouse.up()
        self.assertGreater(self.page.evaluate('editorRegions.camera.x'), before['x'] + .03)
        self.assertEqual(self.page.evaluate('editorRegions.content.x'), .75)
        handle = self.page.locator('#camera-resize-handle')
        handle.scroll_into_view_if_needed()
        box = handle.bounding_box()
        before_width = self.page.evaluate('editorRegions.camera.width')
        # At an image edge the outward half of the handle is clipped.
        self.page.mouse.move(box['x'] + box['width']/4, box['y'] + box['height']/4)
        self.page.mouse.down()
        self.page.mouse.move(box['x'] + 45, box['y'] + box['height']/4, steps=6)
        self.page.mouse.up()
        self.assertGreater(self.page.evaluate('editorRegions.camera.width'), before_width + .02)
        frame = self.page.locator('#content-frame')
        frame.scroll_into_view_if_needed()
        box = frame.bounding_box()
        before_x = self.page.evaluate('editorRegions.content.x')
        self.page.mouse.move(box['x'] + 2, box['y'] + box['height'] * .35)
        self.page.mouse.down()
        self.page.mouse.move(box['x'] - 70, box['y'] + box['height'] * .35, steps=6)
        self.page.mouse.up()
        self.assertLess(self.page.evaluate('editorRegions.content.x'), before_x - .03)
        self.set_region('content', x=35)
        self.assert_pixel(180, 500, [0, 255, 0])

    def test_whole_video_export_keeps_composition_and_selected_regions(self):
        self.stacked()
        self.page.locator('#download-scope').select_option('full')
        requests = self.page.evaluate('buildDownloadRequests()')
        self.assertEqual(len(requests), 1)
        edit = requests[0]['edit']
        self.assertEqual(edit['sections'], [{'start': 0, 'end': 2}])
        self.assertEqual(edit['layout'], 'stacked')
        self.assertEqual(edit['camera']['width'], .25)
        self.assertEqual(edit['content']['x'], .75)
        self.assertEqual(edit['camera_height'], .3)

    def test_captions_follow_source_clock_are_editable_and_exported(self):
        self.prepare_crop()
        self.mock_captions()
        self.page.locator('#captions-enabled').check()
        self.assertFalse(self.page.locator('#download-button').is_enabled())
        self.page.locator('#generate-captions').click()
        self.page.wait_for_function("() => captionState==='ready'")
        self.assertTrue(self.page.locator('#download-button').is_enabled())
        self.assertEqual(self.caption_requests[0]['preview_id'], 'fixture')
        self.assertEqual(self.caption_requests[0]['language'], 'pt')
        self.seek(.4)
        self.page.wait_for_function("""()=>{
            const c=$('vertical-preview'),p=c.getContext('2d').getImageData(0,480,360,130).data;
            for(let i=0;i<p.length;i+=4)if(p[i]>225&&p[i+1]>225&&p[i+2]>225)return true;
            return false;
        }""")
        self.assertGreater(self.white_caption_pixels(), 20)
        self.seek(.95)
        self.page.wait_for_function("""()=>{
            const c=$('vertical-preview'),p=c.getContext('2d').getImageData(0,480,360,130).data;
            for(let i=0;i<p.length;i+=4)if(p[i]>225&&p[i+1]>225&&p[i+2]>225)return false;
            return true;
        }""")
        self.assertEqual(self.white_caption_pixels(), 0)
        long_text = 'Esta legenda editada mantém todas as palavras mesmo quando ocupa mais de duas linhas normais.'
        self.page.locator('#captions-review summary').click()
        self.page.locator('#captions-list textarea').first.fill(long_text)
        request = self.page.evaluate('buildDownloadRequests()[0]')
        self.assertEqual(request['edit']['captions']['cues'][0]['text'], long_text)
        lines = self.page.evaluate('wrapCaptionText(' + json.dumps(long_text) + ')')
        self.assertLessEqual(len(lines), 2)
        self.assertEqual(' '.join(lines), long_text)
        self.seek(.4)
        self.page.wait_for_function("() => captionCues[0].text.startsWith('Esta legenda')")
        self.assertGreater(self.white_caption_pixels(), 20)
        self.page.locator('#captions-enabled').uncheck()
        self.assertEqual(self.white_caption_pixels(), 0)

    def test_caption_job_polls_queued_and_transcribing_then_can_be_cancelled(self):
        self.prepare_crop()
        self.mock_captions(['queued', 'transcribing', 'ready'])
        self.page.locator('#captions-enabled').check()
        self.page.locator('#generate-captions').click()
        self.page.wait_for_function("() => captionState==='queued'")
        self.assertFalse(self.page.locator('#download-button').is_enabled())
        self.assertTrue(self.page.locator('#cancel-captions').is_visible())
        self.page.wait_for_function("() => captionState==='ready'")
        self.assertTrue(self.page.locator('#download-button').is_enabled())
        self.caption_responses = [{'id': 'caption-fixture', 'status': 'preparing'}]
        self.page.locator('#generate-captions').click()
        self.page.wait_for_function("() => captionState==='preparing'")
        self.page.locator('#cancel-captions').click()
        self.assertFalse(self.page.locator('#download-button').is_enabled())
        self.assertGreaterEqual(self.caption_cancels, 1)

    def test_caption_failure_or_source_change_never_exports_old_captions(self):
        self.prepare_crop()
        self.mock_captions(['failed'])
        self.caption_responses[0]['error'] = 'Falha de reconhecimento para teste'
        self.page.locator('#captions-enabled').check()
        self.page.locator('#generate-captions').click()
        self.page.wait_for_function("() => captionState==='failed'")
        self.assertFalse(self.page.locator('#download-button').is_enabled())
        self.assertIn('Falha de reconhecimento', self.page.locator('#captions-status').inner_text())
        self.caption_responses = [{'id': 'caption-fixture', 'status': 'ready', 'cues': [
            {'start': .1, 'end': .8, 'text': 'Legenda da origem antiga'}]}]
        self.page.locator('#generate-captions').click()
        self.page.wait_for_function("() => captionState==='ready'")
        self.page.locator('#video-url').fill('https://www.youtube.com/watch?v=changed')
        self.page.wait_for_function("() => captionCues.length===0 && captionBinding===''")
        self.assertFalse(self.page.locator('#captions-review').is_visible())

    def test_late_caption_task_is_cancelled_after_source_changes(self):
        self.prepare_crop()
        pending, cancelled = [], []

        def route_subtitles(route):
            if urlsplit(route.request.url).path.endswith('/cancel'):
                cancelled.append(route.request.post_data_json['id'])
                route.fulfill(content_type='application/json', body='{"status":"cancelled"}')
            else:
                pending.append(route)

        self.context.route('**/api/subtitles**', route_subtitles)
        self.page.locator('#captions-enabled').check()
        with self.page.expect_request('**/api/subtitles'):
            self.page.locator('#generate-captions').click()
        self.assertEqual(len(pending), 1)
        self.page.locator('#video-url').fill('https://www.youtube.com/watch?v=new-source')
        with self.page.expect_response('**/api/subtitles/cancel'):
            pending[0].fulfill(content_type='application/json', body=json.dumps({
                'id': 'late-caption-task', 'status': 'queued', 'preview_id': 'fixture'}))
        self.assertEqual(cancelled, ['late-caption-task'])
        self.assertEqual(self.page.evaluate('captionCues'), [])
        self.assertEqual(self.page.evaluate('captionBinding'), '')
        self.assertFalse(self.page.locator('#captions-review').is_visible())

    def test_stacked_caption_preview_fits_a_small_screen(self):
        self.page.set_viewport_size({'width': 390, 'height': 844})
        self.stacked()
        self.mock_captions()
        self.page.locator('#captions-enabled').check()
        self.page.locator('#generate-captions').click()
        self.page.wait_for_function("() => captionState==='ready'")
        self.seek(.4)
        self.assertLessEqual(self.page.evaluate('document.documentElement.scrollWidth'), 390)
        self.assertTrue(self.page.locator('#vertical-preview').is_visible())


if __name__ == '__main__':
    unittest.main()
