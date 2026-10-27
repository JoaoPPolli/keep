import json,shutil,subprocess,tempfile,time,unittest
from pathlib import Path
from urllib.request import Request,urlopen
from unittest.mock import patch
import test_subtitle_api as api
import server
from preview_media import freeze_hls

class HistoryTests(unittest.TestCase):
 def test_frozen_window_has_only_complete_https_segments(self):
  text='#EXTM3U\n#EXT-X-TARGETDURATION:4\n#EXTINF:4,\na.ts\n#EXTINF:3.5,\nb.ts\n#EXTINF:4,\n'
  frozen,duration=freeze_hls(text,'https://cdn.example/path/index.m3u8')
  self.assertEqual(duration,7.5);self.assertIn('https://cdn.example/path/a.ts',frozen)
  self.assertTrue(frozen.endswith('#EXT-X-ENDLIST\n'));self.assertNotIn('#EXTINF:4,\n#EXT-X-ENDLIST',frozen)
  for url in ('file:///secret','http://127.0.0.1/a','https://user:secret@host/a'):
   with self.subTest(url=url),self.assertRaises(ValueError):freeze_hls('#EXTM3U\n#EXTINF:2,\n'+url,'https://cdn.example/x')
 def test_history_preview_seeks_and_stops_at_selected_interval(self):
  if not shutil.which('ffmpeg'):self.skipTest('FFmpeg needed')
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory)
   subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=s=320x180:r=15:d=8','-c:v','libx264','-threads','1','-g','15','-f','hls','-hls_time','2','-hls_list_size','0',str(root/'index.m3u8')],check=True)
   frozen,duration=freeze_hls((root/'index.m3u8').read_text(),'https://cdn.example/index.m3u8')
   # A local file fixture exercises the real demuxer without external services.
   frozen=frozen.replace('https://cdn.example/',root.as_uri()+'/')
   request={'history_playlist':frozen,'history_headers':{},'start':3,'duration':2}
   folder=root/'preview';folder.mkdir()
   with server.preview_command(request,folder) as command:
    result=subprocess.run(command,capture_output=True)
   self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
   info=server.probe_media('ffmpeg',folder/'source.mp4')
   self.assertAlmostEqual(info['duration'],2,delta=.15)

class ImportTests(unittest.TestCase):
 setUp=api.SubtitleAPITests.setUp
 tearDown=api.SubtitleAPITests.tearDown
 request=api.SubtitleAPITests.request
 def test_upload_preserves_original_and_uses_a_ready_capability(self):
  if not shutil.which('ffmpeg'):self.skipTest('FFmpeg needed')
  video=self.root/'upload.mp4'
  subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=s=640x360:r=12:d=2','-c:v','libx264','-threads','1',str(video)],check=True)
  content=video.read_bytes()
  request=Request(f'http://127.0.0.1:{self.port}/api/import',data=content,
   headers={'X-Keep-Token':server.TOKEN,'Origin':f'http://127.0.0.1:{self.port}','Content-Type':'application/octet-stream','X-Keep-Filename':'video.mp4'})
  with urlopen(request,timeout=20) as response:data=json.load(response)
  self.assertTrue(data['video']['local_file']);id=data['preview']['id']
  deadline=time.monotonic()+20
  while time.monotonic()<deadline:
   status=self.previews.status(id)
   if status['status'] in ('ready','failed'):break
   time.sleep(.05)
  self.assertEqual(status['status'],'ready',status)
  key=status['media_url'].split('key=')[1]
  self.assertEqual(self.previews.live_file(id,key).read_bytes(),content)
  self.assertEqual(video.read_bytes(),content)
  with patch.multiple(server,ROOT=self.root,DATA=self.root/'data',DOWNLOADS=self.root/'downloads',JOBS={},SETTINGS={}):
   (self.root/'data').mkdir(exist_ok=True)
   code,saved=self.request('download',{'clip':{'id':id,'key':key},'local_file':True,'url':'','title':'Imported','mode':'video'})
   self.assertEqual(code,202,saved)
   job=server.JOBS[saved['id']]
   self.assertEqual(job['platform'],'Local');self.assertEqual((self.root/job['file']).read_bytes(),content)

class FallbackTests(unittest.TestCase):
 def test_youtube_processing_failure_retries_as_full_source(self):
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory)
   job={'id':'a','platform':'YouTube','section':{'start':8632,'end':9184},'source_duration':10000,
        'edit':{'mode':'video','sections':[{'start':8632,'end':8724},{'start':8885,'end':8923},{'start':9062,'end':9184}]},
        'status':'downloading','quality':'1080'}
   calls=[]
   def download(id,args,cookie_snapshot=None):
    calls.append(list(args))
    job.update(status='failed',diagnostic={'category':'processing'}) if len(calls)==1 else job.update(status='complete')
   with patch.multiple(server,JOBS={'a':job},SHUTTING_DOWN=False),patch.object(server,'persist'),patch.object(server,'checked_job_directory',return_value=root),patch.object(server,'run_download',side_effect=download):
    server.run_with_retries('a',['--download-sections','*8632-9184'])
   self.assertEqual(len(calls),2);self.assertNotIn('--download-sections',calls[1]);self.assertTrue(job['full_source_fallback'])
