"""Exercise the production Vosk DLL with real speech in Unicode Windows paths."""
from pathlib import Path
import hashlib
import shutil
import sys
import tempfile
import threading
from urllib.request import urlopen
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'app'))
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')
from preview_media import CAPTION_NATIVE,CAPTION_MODELS,_caption_component,_CaptionRecognizer
def report_failure(kind, value, tb):
    import traceback
    message=''.join(traceback.format_exception(kind,value,tb))
    message=message.replace('%','%25').replace('\r','%0D').replace('\n','%0A')
    print('::error title=Windows speech validation::'+message)
sys.excepthook=report_failure
with tempfile.TemporaryDirectory(prefix='keep-windows-asr-', ignore_cleanup_errors=True) as temporary:
    root=Path(temporary)/'Keep João 日本語';root.mkdir()
    native_name,native_hash=CAPTION_NATIVE['windows-x64']
    print('Preparing Windows DLL',flush=True)
    native=_caption_component(root,native_name[:-4],'https://github.com/alphacep/vosk-api/releases/download/v0.3.45/'+native_name,native_hash,threading.Event())
    model=CAPTION_MODELS['en']
    print('Preparing official English model',flush=True)
    model_path=_caption_component(root,model['name'],model['url'],model['sha256'],threading.Event())
    with urlopen('https://raw.githubusercontent.com/alphacep/vosk-api/master/python/example/test.wav',timeout=30) as response:sample=response.read()
    assert hashlib.sha256(sample).hexdigest()=='dcfea5712c43a43ba7ae8083afb39d36993e5a69c46e88b68aaa72b65cb615bb'
    audio=root/'speech.wav';audio.write_bytes(sample)
    print('Loading Windows DLL and English model',flush=True)
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
