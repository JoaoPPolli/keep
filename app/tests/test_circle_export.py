import json,shutil,subprocess,unittest
from pathlib import Path
import test_stacked_export as fixture
from media_editor import circle_geometry,probe_media,transform_command,validate_edit

@unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
class CircleExportTests(unittest.TestCase):
 setUp=fixture.StackedExportTests.setUp
 tearDown=fixture.StackedExportTests.tearDown
 def test_circle_is_round_and_positioned_over_content_with_audio(self):
  info=probe_media('ffmpeg',self.source)
  edit=validate_edit(fixture.request(layout='circle',camera_size=.4,camera_position={'x':.5,'y':.5}),info['duration'])
  geometry=circle_geometry(edit,info)
  target=self.root/'circle.mp4'
  command,_=transform_command('ffmpeg',self.source,target,edit,info)
  result=subprocess.run(command,capture_output=True)
  self.assertEqual(result.returncode,0,result.stderr.decode(errors='replace'))
  final=probe_media('ffmpeg',target)
  self.assertEqual((final['width'],final['height']),(360,640));self.assertTrue(final['audio'])
  raw=subprocess.check_output(['ffmpeg','-v','error','-i',str(target),'-frames:v','1','-pix_fmt','rgb24','-threads','1','-f','rawvideo','pipe:1'])
  def pixel(x,y):i=(y*final['width']+x)*3;return raw[i:i+3]
  x,y,d=geometry['left'],geometry['top'],geometry['diameter']
  self.assertGreater(pixel(x+d//2,y+d//2)[0],220)
  self.assertGreater(pixel(x+2,y+2)[2],220,'Circle corners must show the blue content')
  self.assertGreater(pixel(x+d+3,y+d//2)[2],220)
 def test_circle_validation_and_geometry_match_preview(self):
  edit=validate_edit(fixture.request(layout='circle',camera_size=.31,camera_position={'x':.2,'y':.8}),.6)
  expected=circle_geometry(edit,{'width':1919,'height':1079})
  # Execute the production JS geometry function without a DOM.
  js=(Path(__file__).resolve().parents[1]/'dist'/'app.js').read_text()
  code=js[js.index('function circleGeometry('):js.index('function currentCircleGeometry(')]
  args=[1919,1079,edit['camera'],edit['content'],edit['camera_size'],edit['camera_position']]
  result=subprocess.check_output(['node','-e',code+';process.stdout.write(JSON.stringify(circleGeometry(...'+json.dumps(args)+')));'])
  self.assertEqual(json.loads(result),expected)
  for change in ({'camera_size':True},{'camera_size':.9},{'camera_position':{'x':-1,'y':0}},{'vertical':False}):
   with self.subTest(change=change),self.assertRaises(ValueError):validate_edit(fixture.request(layout='circle',**change),.6)
