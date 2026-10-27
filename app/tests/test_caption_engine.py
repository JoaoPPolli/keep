"""Component integrity and asynchronous caption lifecycle; ASR integration is explicit."""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import struct
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import wave
import zipfile

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
from preview_media import (SubtitleManager, _CaptionCancelled,
                           _caption_component, caption_cues)


class CaptionWordsTests(unittest.TestCase):
    def test_groups_timed_words_without_losing_the_source_clock(self):
        words = [{'start': .3, 'end': .7, 'word': 'olá'},
                 {'start': .8, 'end': 1.1, 'word': 'mundo'},
                 {'start': 3.0, 'end': 3.4, 'word': 'outra'},
                 {'start': 3.5, 'end': 3.9, 'word': 'frase'}]
        self.assertEqual(caption_cues(words), [
            {'start': .3, 'end': 1.1, 'text': 'Olá mundo'},
            {'start': 3.0, 'end': 3.9, 'text': 'Outra frase'}])

    def test_caps_line_length_and_does_not_join_a_long_pause(self):
        words = [{'start': i*.4, 'end': i*.4+.3, 'word': 'legenda'} for i in range(30)]
        cues = caption_cues(words)
        self.assertGreater(len(cues), 1)
        self.assertTrue(all(len(x['text']) <= 68 for x in cues))
        self.assertTrue(all(a['end'] <= b['start'] for a,b in zip(cues,cues[1:])))

    def test_rejects_invalid_or_unbounded_asr_timestamps(self):
        for start,end in [(float('nan'),1), (0,float('inf')),(-1,1),(1,0),(False,1)]:
            with self.subTest(start=start,end=end), self.assertRaises(ValueError):
                caption_cues([{'start':start,'end':end,'word':'teste'}])
        with self.assertRaises(ValueError):
            caption_cues([{'start':2,'end':3,'word':'primeiro'}, {'start':0,'end':1,'word':'fora da ordem'}])

    def test_accepts_more_than_the_old_cue_limit(self):
        words=[{'start':float(i),'end':i+.1,'word':'voz'} for i in range(4001)]
        self.assertTrue(caption_cues(words))


class CaptionComponentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='keep-caption-test-')
        self.root=Path(self.temp.name)
        self.cancelled=threading.Event()

    def tearDown(self):self.temp.cleanup()

    def archive(self, name='model/weights.bin', content=b'verified model'):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:z.writestr(name,content)
        return stream.getvalue()

    def install(self, body, expected=None):
        digest=expected or hashlib.sha256(body).hexdigest()
        with patch('urllib.request.urlopen',return_value=io.BytesIO(body)):
            return _caption_component(self.root,'test-model','https://alphacephei.com/vosk/models/test.zip',digest,self.cancelled)

    def test_install_and_cache_are_verified_without_repeating_the_download(self):
        body=self.archive(); result=self.install(body)
        self.assertEqual((result/'weights.bin').read_bytes(),b'verified model')
        with patch('urllib.request.urlopen',side_effect=AssertionError('cache should be reused')):
            again=_caption_component(self.root,'test-model','https://alphacephei.com/vosk/models/test.zip',hashlib.sha256(body).hexdigest(),self.cancelled)
        self.assertEqual(result,again)
        (result/'weights.bin').write_bytes(b'tampered')
        with self.assertRaises(ValueError):self.install(body)

    def test_rejects_hash_mismatch_before_extracting(self):
        with self.assertRaises(ValueError):self.install(self.archive(), '0'*64)
        self.assertFalse((self.root/'test-model').exists())
        self.assertEqual(list(self.root.iterdir()),[])

    def test_rejects_archive_path_escape_and_does_not_create_outside_files(self):
        for name in ['model/../../escape.bin','/absolute.bin','model/a\\b.bin','model/C:bad.bin']:
            with self.subTest(name=name),self.assertRaises(ValueError):self.install(self.archive(name))
        self.assertFalse((self.root.parent/'escape.bin').exists())
        self.assertFalse((self.root/'test-model').exists())

    def test_rejects_native_symlinks(self):
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:
            member=zipfile.ZipInfo('model/link');member.create_system=3;member.external_attr=0o120777<<16
            z.writestr(member,'../../outside')
        with self.assertRaises(ValueError):self.install(stream.getvalue())

    def test_cancelled_download_never_installs_a_partial_component(self):
        self.cancelled.set()
        with self.assertRaises(_CaptionCancelled):self.install(self.archive())
        self.assertFalse((self.root/'test-model').exists())

    def test_requires_official_https_origin(self):
        for url in ['http://alphacephei.com/model.zip','https://example.com/model.zip','https://user:pass@github.com/model.zip']:
            with self.subTest(url=url),self.assertRaises(ValueError):
                _caption_component(self.root,'model',url,'0'*64,self.cancelled)


@unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg is required')
class CaptionLifecycleTests(unittest.TestCase):
    """The recognizer is stubbed here to test media conversion and cancellation, not ASR accuracy."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='keep-caption-flow-')
        self.root=Path(self.temp.name)
        self.source=self.root/'source.wav'
        with wave.open(str(self.source),'wb') as wavefile:
            wavefile.setnchannels(1);wavefile.setsampwidth(2);wavefile.setframerate(8000)
            wavefile.writeframes(struct.pack('<h',0)*8000)
        self.manager=SubtitleManager(self.root,shutil.which('ffmpeg'))
        self.model=patch('preview_media.CAPTION_MODELS',{'pt':{}});self.model.start()

    def tearDown(self):
        self.manager.close();self.model.stop();self.temp.cleanup()

    def wait(self,id):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            status=self.manager.status(id)
            if status['status'] in ('ready','failed','cancelled'):return status
            time.sleep(.02)
        self.fail('caption worker did not finish')

    def test_actual_ffmpeg_decode_and_ready_result_cleanup(self):
        test=self
        class StubRecognizer:
            def transcribe(self,path,cancelled,progress):
                with wave.open(str(path),'rb') as audio:
                    test.assertEqual(audio.getframerate(),16000)
                    test.assertEqual(audio.getnchannels(),1)
                    test.assertEqual(audio.getsampwidth(),2)
                progress(60)
                return [{'start':0.,'end':.8,'text':'Texto de fluxo simulado'}]
        with patch.object(self.manager,'_prepare',return_value=StubRecognizer()):
            created=self.manager.create(self.source,'pt',preview_id='preview')
            result=self.wait(created['id'])
        self.assertEqual(result['status'],'ready',result)
        self.assertEqual(result['progress'],100)
        self.assertEqual(result['preview_id'],'preview')
        result['cues'][0]['text']='modified externally'
        self.assertEqual(self.manager.status(created['id'])['cues'][0]['text'],'Texto de fluxo simulado')
        deadline=time.monotonic()+2
        while self.manager.cache.exists() and list(self.manager.cache.iterdir()) and time.monotonic()<deadline:time.sleep(.02)
        self.assertFalse(list(self.manager.cache.iterdir()))
        self.assertTrue(self.source.exists())

    def test_only_selected_audio_is_decoded_with_source_timestamps(self):
        observed=[]
        class StubRecognizer:
            def transcribe(self,path,cancelled,progress):
                with wave.open(str(path),'rb') as audio:observed.append(audio.getnframes())
                return [{'start':.01,'end':.15,'text':'Fala selecionada'}]
        with patch.object(self.manager,'_prepare',return_value=StubRecognizer()):
            task=self.manager.create(self.source,duration=8000,sections=[{'start':.2,'end':.4},{'start':.7,'end':.9}])
            result=self.wait(task['id'])
        self.assertEqual(result['status'],'ready',result)
        self.assertEqual(observed,[3200,3200])
        self.assertEqual([(cue['start'],cue['end']) for cue in result['cues']],[(.21,.35),(.71,.85)])

    def test_overlapping_selected_ranges_are_recognized_once(self):
        observed=[]
        class StubRecognizer:
            def transcribe(self,path,cancelled,progress):
                with wave.open(str(path),'rb') as audio:observed.append(audio.getnframes())
                return [{'start':0,'end':.5,'text':'Trechos sobrepostos'}]
        with patch.object(self.manager,'_prepare',return_value=StubRecognizer()):
            task=self.manager.create(self.source,duration=1,sections=[{'start':.1,'end':.5},{'start':.3,'end':.7}])
            result=self.wait(task['id'])
        self.assertEqual(result['status'],'ready',result)
        self.assertEqual(observed,[9600])
        self.assertEqual(result['cues'][0]['start'],.1)

    def test_queued_task_can_be_cancelled_and_no_source_is_deleted(self):
        started=threading.Event();release=threading.Event()
        def preparing(task):
            started.set();release.wait(5)
            if task['cancelled'].is_set():raise _CaptionCancelled()
            raise ValueError('simulated failure')
        with patch.object(self.manager,'_prepare',side_effect=preparing):
            one=self.manager.create(self.source);started.wait(2)
            two=self.manager.create(self.source)
            self.manager.cancel(two['id']);self.manager.cancel(one['id']);release.set()
            self.assertEqual(self.wait(one['id'])['status'],'cancelled')
            self.assertEqual(self.wait(two['id'])['status'],'cancelled')
        self.assertTrue(self.source.exists())

    def test_rejects_unknown_language_source_links_and_invalid_duration(self):
        with self.assertRaises(ValueError):self.manager.create(self.source,'xx')
        with self.assertRaises(ValueError):self.manager.create(self.source,'pt',duration=float('inf'))
        linked=self.root/'linked.wav';linked.symlink_to(self.source)
        with self.assertRaises(ValueError):self.manager.create(linked)


@unittest.skipUnless(os.environ.get('KEEP_TEST_CAPTION_SAMPLE'),
                     'Set KEEP_TEST_CAPTION_SAMPLE to the official Vosk spoken sample for real ASR')
class RealCaptionRecognitionTests(unittest.TestCase):
    def test_official_spoken_audio_produces_timed_speech_cues(self):
        from preview_media import CAPTION_MODELS, CAPTION_NATIVE, _caption_platform
        sample=Path(os.environ['KEEP_TEST_CAPTION_SAMPLE'])
        self.assertTrue(sample.is_file())
        native=CAPTION_NATIVE[_caption_platform()][0][:-4]
        model=CAPTION_MODELS['en']['name']
        with tempfile.TemporaryDirectory(prefix='keep-real-caption-test-') as directory:
            root=Path(directory);tools=root/'tools'/'subtitles';tools.mkdir(parents=True)
            for component in (native,model):
                cached=APP/'tools'/'subtitles'/component
                self.assertTrue(cached.is_dir(),'Prepare the verified component cache before the real ASR test')
                shutil.copytree(cached,tools/component)
            manager=SubtitleManager(root,shutil.which('ffmpeg'))
            try:
                task=manager.create(sample,'en',preview_id='official-spoken-sample',duration=10)
                deadline=time.monotonic()+30
                while time.monotonic()<deadline:
                    status=manager.status(task['id'])
                    if status['status'] in ('ready','failed'):break
                    time.sleep(.05)
                self.assertEqual(status['status'],'ready',status)
                self.assertGreater(len(status['cues']),0)
                text=' '.join(cue['text'].lower() for cue in status['cues'])
                self.assertIn('zero',text)
                self.assertIn('one',text)
                self.assertTrue(all(0<=cue['start']<cue['end']<=10 for cue in status['cues']))
            finally:manager.close()


@unittest.skipUnless(os.environ.get('KEEP_TEST_CAPTION_PT_SAMPLE'),
                     'Set KEEP_TEST_CAPTION_PT_SAMPLE to the Portuguese spoken sample for real ASR')
class RealPortugueseRecognitionTests(unittest.TestCase):
    def test_portuguese_speech_produces_editable_timed_cues(self):
        from preview_media import CAPTION_MODELS, CAPTION_NATIVE, _caption_platform
        sample=Path(os.environ['KEEP_TEST_CAPTION_PT_SAMPLE'])
        self.assertTrue(sample.is_file())
        native=CAPTION_NATIVE[_caption_platform()][0][:-4]
        model=CAPTION_MODELS['pt']['name']
        with tempfile.TemporaryDirectory(prefix='keep-real-caption-test-') as directory:
            root=Path(directory);tools=root/'tools'/'subtitles';tools.mkdir(parents=True)
            for component in (native,model):
                cached=APP/'tools'/'subtitles'/component
                self.assertTrue(cached.is_dir(),'Prepare the verified component cache before the real ASR test')
                shutil.copytree(cached,tools/component)
            manager=SubtitleManager(root,shutil.which('ffmpeg'))
            try:
                task=manager.create(sample,'pt',preview_id='official-spoken-sample',duration=15)
                deadline=time.monotonic()+30
                while time.monotonic()<deadline:
                    status=manager.status(task['id'])
                    if status['status'] in ('ready','failed'):break
                    time.sleep(.05)
                self.assertEqual(status['status'],'ready',status)
                self.assertGreater(len(status['cues']),0)
                text=' '.join(cue['text'].lower() for cue in status['cues'])
                self.assertIn('vídeo',text)
                self.assertTrue(all(0<=cue['start']<cue['end']<=15 for cue in status['cues']))
            finally:manager.close()


if __name__=='__main__':unittest.main()
