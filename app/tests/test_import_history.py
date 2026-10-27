import json,shutil,subprocess,tempfile,time,unittest
from pathlib import Path
from urllib.request import Request,urlopen
from unittest.mock import patch
import test_subtitle_api as api
import server

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

 def test_reported_long_video_intervals_cut_locally_without_offset_errors(self):
  if not shutil.which('ffmpeg'):self.skipTest('FFmpeg needed')
  with tempfile.TemporaryDirectory() as directory:
   root=Path(directory);folder=root/'job';folder.mkdir();(root/'data').mkdir()
   id='abc123';(folder/'.keep-job.json').write_text(json.dumps({'id':id}))
   source=folder/('.keep-edit-source-'+id+'.mp4')
   colors="drawbox=c=red:t=fill:enable='between(t,8632,8723)',drawbox=c=lime:t=fill:enable='between(t,8885,8922)',drawbox=c=blue:t=fill:enable='between(t,9062,9183)'"
   subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=c=black:s=64x36:r=1:d=9400','-vf',colors,'-c:v','libx264','-threads','1',str(source)],check=True)
   edit=server.validate_edit({'mode':'video','sections':[{'start':8632,'end':8724},{'start':8885,'end':8923},{'start':9062,'end':9184}]},9400)
   job={'id':id,'title':'Reported montage','mode':'video','source_duration':9400,'section':{'start':8632,'end':9184},
        'edit':edit,'directory':'job','managed_root':str(root),'file':'job/'+source.name,'status':'downloading',
        'effective_section':{'start':0,'end':9400}}
   with patch.multiple(server,ROOT=root,DATA=root/'data',JOBS={id:job},PROCESSES={}):
    server.apply_job_edit(id)
   final=root/job['file'];info=server.probe_media('ffmpeg',final)
   self.assertAlmostEqual(info['duration'],252,delta=.15)
   for position,color in ((0,0),(100,1),(200,2)):
    pixel=subprocess.check_output(['ffmpeg','-v','error','-ss',str(position),'-i',str(final),'-vf','scale=1:1','-frames:v','1','-f','rawvideo','-pix_fmt','rgb24','pipe:1'])
    self.assertGreater(pixel[color],200,(position,pixel))
