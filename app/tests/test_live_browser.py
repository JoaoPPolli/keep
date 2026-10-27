"""Browser interaction uses explicit API fixtures; capture is tested with real FFmpeg separately."""
import json
import unittest
from urllib.parse import urlsplit
import test_browser as fixture


@unittest.skipUnless(fixture.sync_playwright and fixture.CHROMIUM and fixture.FFMPEG, 'Browser and FFmpeg required')
class LiveBrowserTests(unittest.TestCase):
    setUpClass = classmethod(fixture.BrowserTests.setUpClass.__func__)
    tearDownClass = classmethod(fixture.BrowserTests.tearDownClass.__func__)
    make_video = classmethod(fixture.BrowserTests.make_video.__func__)
    setUp = fixture.BrowserTests.setUp
    tearDown = fixture.BrowserTests.tearDown
    update_route = fixture.BrowserTests.update_route
    video_route = fixture.BrowserTests.video_route
    open_app = fixture.BrowserTests.open_app

    def routes(self):
        self.context.route('**/fixture-*.mp4*',self.video_route)
        self.live_calls = []
        self.exports = []
        self.buffer = 0
        self.state = 'connecting'
        info = {'url': 'https://www.twitch.tv/example', 'platform': 'Twitch', 'title': 'Live example',
                'creator': 'Example', 'live': True, 'duration': None, 'heights':[720], 'video_index':1}
        self.context.route('**/api/inspect', lambda route: route.fulfill(content_type='application/json',body=json.dumps(info)))
        preview = {'id':'a'*32,'status':'ready','duration':2,'width':640,'height':360,'fps':12,
                   'media_url':'/fixture-landscape.mp4?key=live-capability'}
        def live_route(route):
            path = urlsplit(route.request.url).path
            body = route.request.post_data_json if route.request.method == 'POST' else None
            self.live_calls.append((path, body))
            if path == '/api/live/start': self.state = 'connecting'
            if path == '/api/live/stop': self.state = 'stopped'
            if path == '/api/live/clip':
                result = {'video':{**info,'live':False,'kind':'Clipe da live','duration':2,'width':640,'height':360},
                          'preview':preview,'job':{'id':'saved-original','status':'complete'}}
            else:
                result = {'id':'live-fixture','status':self.state,'buffer_seconds':self.buffer,'stage':'Live acompanhada'}
            route.fulfill(content_type='application/json',body=json.dumps(result))
        self.context.route('**/api/live**',live_route)
        self.context.route('**/api/duplicates', lambda route: route.fulfill(content_type='application/json',body='{"matches":[]}'))
        def export(route):
            self.exports.append(route.request.post_data_json)
            route.fulfill(status=202,content_type='application/json',body='{"id":"export-fixture"}')
        self.context.route('**/api/download',export)
        self.context.route('**/api/preview/cancel',lambda route:route.fulfill(content_type='application/json',body='{}'))

    def test_live_buffer_clip_preview_composition_export_and_stop(self):
        self.routes();self.open_app()
        self.page.locator('#video-url').fill('https://www.twitch.tv/example')
        self.page.locator('#inspect').click()
        self.page.wait_for_selector('#live-panel',state='visible')
        self.assertFalse(self.page.locator('#video-result').is_visible())
        self.assertTrue(self.page.locator('#live-clip').is_disabled())
        self.page.locator('#live-start').click()
        self.page.wait_for_function("()=>liveId==='live-fixture'")
        self.buffer=30;self.state='buffering'
        self.page.wait_for_function("()=>!$('live-clip').disabled")
        self.page.locator('#live-seconds').select_option('15')
        self.page.locator('#live-clip').click()
        self.page.wait_for_function("()=>$('preview-player').readyState>=2")
        self.assertTrue(self.page.locator('#video-result').is_visible())
        self.assertIn('Clipe original salvo',self.page.locator('#message').inner_text())
        self.assertTrue(self.page.locator('#prepare-preview').is_disabled())
        self.assertTrue(self.page.locator('#quality').is_disabled())
        self.page.locator('#vertical-enabled').check()
        self.page.locator('#vertical-layout-stacked').click()
        self.page.wait_for_function("()=>$('vertical-preview').width>0")
        self.page.locator('#download-button').click()
        self.page.wait_for_function("()=>!startingDownload")
        self.assertEqual(len(self.exports),1)
        exported=self.exports[0]
        self.assertEqual(exported['clip'],{'id':'a'*32,'key':'live-capability'})
        self.assertEqual(exported['edit']['layout'],'stacked')
        self.assertEqual(exported['edit']['sections'],[{'start':0,'end':2}])
        self.assertEqual(next(body for path,body in self.live_calls if path=='/api/live/clip')['seconds'],15)
        self.page.locator('#live-stop').click()
        self.page.wait_for_function("()=>liveState?.status==='stopped'")
        self.assertTrue(self.page.locator('#live-start').is_visible())
        self.assertTrue(self.page.locator('#live-clip').is_enabled())

    def test_public_launch_link_and_mobile_live_panel_do_not_require_login(self):
        self.routes();self.open_app()
        self.assertEqual(self.page.locator('#configure-hosted-launch').get_attribute('href'),
                         'https://joaoppolli.github.io/keep/#abrir-keep')
        self.assertEqual(self.page.locator('a[href*="chatgpt.site"]').count(),0)
        self.assertEqual(self.page.locator('#disable-hosted-launch').count(),0)
        self.context.set_default_timeout(7000)
        self.page.set_viewport_size({'width':390,'height':900})
        self.page.locator('#video-url').fill('https://www.twitch.tv/example')
        self.page.locator('#inspect').click()
        self.page.wait_for_selector('#live-panel',state='visible')
        self.assertTrue(self.page.evaluate('document.documentElement.scrollWidth<=innerWidth'))

    def test_changing_live_to_recorded_video_hides_panel_and_stops_capture(self):
        self.routes();self.open_app()
        self.page.locator('#video-url').fill('https://www.twitch.tv/example')
        self.page.locator('#inspect').click()
        self.page.wait_for_selector('#live-panel',state='visible')
        self.page.locator('#live-start').click()
        self.page.wait_for_function("()=>liveId==='live-fixture'")
        recorded={'url':'https://youtu.be/example','platform':'YouTube','title':'Recorded','duration':2,'live':False,'video_index':1,'heights':[360]}
        self.context.route('**/api/inspect',lambda route:route.fulfill(content_type='application/json',body=json.dumps(recorded)))
        self.page.locator('#video-url').fill('https://youtu.be/example')
        self.page.locator('#inspect').click()
        self.page.wait_for_selector('#video-result',state='visible')
        self.assertFalse(self.page.locator('#live-panel').is_visible())
        self.page.wait_for_function("()=>liveId===''&&liveCandidate===null")
        self.assertTrue(any(path=='/api/live/stop' for path,body in self.live_calls))
        self.assertEqual(self.page.locator('#video-title').inner_text(),'Recorded')

    def test_import_from_pc_opens_real_preview_without_a_video_link(self):
        self.open_app()
        self.page.locator('#local-video').set_input_files({'name':'Meu vídeo.mp4','mimeType':'video/mp4','buffer':self.landscape})
        self.page.wait_for_function("()=>current?.local_file===true&&$('preview-player').readyState>=2",timeout=20000)
        self.assertFalse(self.page.locator('#live-panel').is_visible())
        self.assertEqual(self.page.locator('#video-title').inner_text(),'Meu vídeo.mp4')
        self.assertTrue(self.page.locator('#download-button').is_enabled())
        self.page.locator('#vertical-enabled').check()
        self.page.locator('#vertical-layout-stacked').click()
        self.assertTrue(self.page.locator('#vertical-preview').is_visible())

    def test_history_window_selection_opens_clip_in_editor(self):
        self.routes();self.open_app()
        self.page.locator('#video-url').fill('https://www.twitch.tv/example')
        self.page.locator('#inspect').click()
        self.page.wait_for_selector('#live-panel',state='visible')
        self.context.route('**/api/live/history',lambda route:route.fulfill(content_type='application/json',body=json.dumps({'id':'history-fixture','duration':3600,'note':'Janela disponível.'})))
        requests=[]
        def history_clip(route):
            requests.append(route.request.post_data_json)
            data={'video':{'url':'https://www.twitch.tv/example','platform':'Twitch','title':'Historic','duration':2,'video_index':1,'live':False},'preview':{'id':'b'*32,'status':'ready','duration':2,'width':640,'height':360,'fps':12,'media_url':'/fixture-landscape.mp4?key=history-key'}}
            route.fulfill(content_type='application/json',body=json.dumps(data))
        self.context.route('**/api/live/history/clip',history_clip)
        self.page.locator('#live-history').click()
        self.page.wait_for_selector('#live-history-fields',state='visible')
        self.page.locator('#live-history-start').fill('23:10')
        self.page.locator('#live-history-end').fill('23:12')
        self.page.locator('#live-history-preview').click()
        self.page.wait_for_function("()=>current?.title==='Historic'&&$('preview-player').readyState>=2")
        self.assertEqual(requests,[{'id':'history-fixture','section':{'start':1390,'end':1392}}])
        self.assertTrue(self.page.locator('#download-button').is_enabled())
        self.assertEqual(self.page.evaluate('current.clip.key'),'history-key')


if __name__=='__main__':unittest.main()
