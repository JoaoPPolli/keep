"""Exercise the production Vosk DLL with real speech in Unicode Windows paths."""
from pathlib import Path
import hashlib
import shutil
import sys
import tempfile
import threading
from urllib.request import urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
from preview_media import CAPTION_NATIVE,CAPTION_MODELS,_caption_component,_CaptionRecognizer
with tempfile.TemporaryDirectory(prefix='keep-windows-asr-') as temporary:
    root=Path(temporary)/'Keep João 日本語';root.mkdir()
    native_name,native_hash=CAPTION_NATIVE['windows-x64']
    native=_caption_component(root,native_name[:-4],'https://github.com/alphacep/vosk-api/releases/download/v0.3.45/'+native_name,native_hash,threading.Event())
    model=CAPTION_MODELS['en']
    model_path=_caption_component(root,model['name'],model['url'],model['sha256'],threading.Event())
    with urlopen('https://raw.githubusercontent.com/alphacep/vosk-api/master/python/example/test.wav',timeout=30) as response:sample=response.read()
    assert hashlib.sha256(sample).hexdigest()=='dcfea5712c43a43ba7ae8083afb39d36993e5a69c46e88b68aaa72b65cb615bb'
    audio=root/'speech.wav';audio.write_bytes(sample)
    recognizer=_CaptionRecognizer(native/'libvosk.dll',model_path)
    try:
        cues=recognizer.transcribe(audio,threading.Event(),lambda progress:None)
        text=' '.join(c['text'] for c in cues).lower()
        assert 'zero' in text and 'one' in text,cues
        print('Real Windows ASR passed with Unicode installation path:',len(cues),'timed captions')
    finally:recognizer.close()
    portuguese=CAPTION_MODELS['pt']
    portuguese_path=_caption_component(root,portuguese['name'],portuguese['url'],portuguese['sha256'],threading.Event())
    recognizer=_CaptionRecognizer(native/'libvosk.dll',portuguese_path)
    recognizer.close()
    print('Portuguese model loaded successfully on Windows with a Unicode installation path')
