"""Keep's windowless desktop/URI entry point. No user input becomes a command."""
import json
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import urlopen
import webbrowser

ROOT = Path(__file__).resolve().parent
URL = 'http://127.0.0.1:8765'


def accepted_arguments(arguments):
    return not arguments or arguments in (['keep://launch'], ['keep://launch/'])


def running():
    try:
        with urlopen(URL + '/api/session', timeout=1) as response:
            data = json.load(response)
        return (isinstance(data, dict) and isinstance(data.get('token'), str)
                and 'engine' in data and 'version' in data)
    except Exception:
        return False


@contextmanager
def startup_lock(root):
    """Two clicks must not write/download the same bootstrap files at once."""
    path = root / '.keep-launcher.lock'
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise RuntimeError('O arquivo de controle do inicializador contém um vínculo.')
    handle = path.open('a+b')
    acquired = False
    deadline = time.monotonic() + 1830
    try:
        if os.name == 'nt':
            import msvcrt
            if path.stat().st_size == 0:
                handle.write(b'\0'); handle.flush()
            def lock():
                handle.seek(0); msvcrt.locking(handle.fileno(),msvcrt.LK_NBLCK,1)
            def unlock():
                handle.seek(0); msvcrt.locking(handle.fileno(),msvcrt.LK_UNLCK,1)
        else:
            import fcntl
            def lock():
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            def unlock():
                fcntl.flock(handle,fcntl.LOCK_UN)
        while True:
            try:
                lock(); acquired = True; break
            except (BlockingIOError,PermissionError):
                if running():
                    yield False
                    return
                if time.monotonic() >= deadline:
                    raise RuntimeError('Outra abertura do Keep não terminou. Aguarde ou consulte data/launcher.log.') from None
                time.sleep(0.25)
        yield True
    finally:
        if acquired:
            unlock()
        handle.close()


def start(root=ROOT):
    root = Path(root).resolve()
    if running():
        webbrowser.open(URL)
        return True
    windows = sys.platform == 'win32'
    interpreter = root / 'runtime' / 'pythonw.exe' if windows else Path(sys.executable)
    if not interpreter.is_file() or not (root / 'server.py').is_file():
        raise RuntimeError('Extraia o pacote completo do Keep antes de abrir o aplicativo.')
    data = root / 'data'
    data.mkdir(exist_ok=True)
    options = {'cwd': str(root), 'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}
    if not windows:
        options['start_new_session'] = True
    with startup_lock(root) as acquired:
        if not acquired or running():
            webbrowser.open(URL)
            return True
        # Bootstrap output is retained locally. It contains component status,
        # never browser cookies. The server starts only after setup succeeds.
        with (data / 'launcher.log').open('ab') as log:
            bootstrap = 'bootstrap.py' if windows else 'bootstrap_linux.py'
            result = subprocess.run([str(interpreter), '-X', 'utf8', str(root / bootstrap)],
                                    stdout=log, stderr=log, timeout=1800, **options)
            if result.returncode:
                raise RuntimeError('Não foi possível preparar o Keep. Confira data/launcher.log ou use o iniciador de texto.')
            subprocess.Popen([str(interpreter), '-X', 'utf8', str(root / 'server.py')],
                             stdin=subprocess.DEVNULL, stdout=log, stderr=log, **options)
        for _ in range(120):
            if running():
                webbrowser.open(URL)
                return True
            time.sleep(0.25)
    raise RuntimeError('O Keep não respondeu. Confira se outra versão ocupa a porta 8765 e consulte data/launcher.log.')


def main(arguments=None):
    arguments = list(sys.argv[1:] if arguments is None else arguments)
    if not accepted_arguments(arguments):
        return 2
    try:
        start()
        return 0
    except Exception as error:
        message = str(error)
        if sys.platform == 'win32':
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, 'Keep — Não foi possível iniciar', 0x10)
        else:
            print(message, file=sys.stderr)
            import shutil
            if shutil.which('zenity'):
                subprocess.run(['zenity', '--error', '--title=Keep', '--text=' + message], timeout=60)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
