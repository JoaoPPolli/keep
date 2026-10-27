"""Private, disposable local media for the cut preview.

The HTTP layer validates requests and supplies the engine command. The manager
keeps cookies inside that command's context, never exposes remote media URLs, and
only serves a finished H.264/AAC MP4 through an unguessable capability URL.
"""
from contextlib import nullcontext
from dataclasses import dataclass, field
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys
import threading
import time
import shutil


MIB = 1024 * 1024
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
ID_PATTERN = re.compile(r'^[0-9a-f]{32}$')
MAX_FRAME_INDEX = 500000
MAX_FRAME_INDEX_BYTES = 8 * MIB
FRAME_INDEX_TIMEOUT = 30
FRAME_LINE = re.compile(r'\[Parsed_showinfo_[^\]]+\]\s+n:\s*(\d+)\s+.*?\bpts_time:\s*([-+\d.eE]+)')


def _is_link(path):
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()


def range_response(size, range_header=None):
    """Return (status, headers, offset, count) for one RFC 9110 byte range.

    Multiple or invalid ranges are answered with 416; never silently return the
    entire video after a browser asks to seek beyond its end.
    """
    if not isinstance(size, int) or size < 0:
        raise ValueError('Invalid file size')
    headers = {'Accept-Ranges': 'bytes', 'Content-Type': 'video/mp4',
               'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
    if not range_header:
        headers['Content-Length'] = str(size)
        return 200, headers, 0, size
    match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_header.strip())
    invalid = not match or not any(match.groups()) or size == 0
    if not invalid:
        first, last = match.groups()
        try:
            if first:
                start = int(first)
                end = min(int(last), size - 1) if last else size - 1
                invalid = start >= size or end < start
            else:
                suffix = int(last)
                invalid = suffix == 0
                start, end = max(0, size - suffix), size - 1
        except ValueError:
            invalid = True
    if invalid:
        headers.update({'Content-Range': f'bytes */{size}', 'Content-Length': '0'})
        return 416, headers, 0, 0
    count = end - start + 1
    headers.update({'Content-Range': f'bytes {start}-{end}/{size}', 'Content-Length': str(count)})
    return 206, headers, start, count


def terminate_tree(process):
    """Stop only a process started by this module, including its FFmpeg child."""
    if process is None:
        return
    if os.name != 'nt' and getattr(process, '_keep_group', False):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                break
            if sig == signal.SIGTERM:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
    elif process.poll() is None:
        if os.name == 'nt':
            try:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                               capture_output=True, timeout=15, creationflags=CREATE_FLAGS)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
        else:
            process.kill()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


@dataclass
class _Preview:
    id: str
    key: str
    folder: Path
    request: object
    created: float
    touched: float
    expected_duration: float | None = None
    status: str = 'preparing'
    stage: str = 'Preparando uma cópia leve para assistir e escolher o trecho…'
    error: str | None = None
    duration: float | None = None
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    frame_times: list | None = None
    frames_indexed: bool = False
    live_clip: bool = False
    imported: bool = False
    frame_note: str | None = None
    process: object = None
    thread: object = None
    cancelled: threading.Event = field(default_factory=threading.Event)


class PreviewManager:
    """At most two full, seekable previews with bounded temporary storage.

    command_factory(request, folder) returns a context manager yielding argv.
    It must download one selected video as folder/source.%(ext)s. Returning argv
    directly also works for factories which do not hold a cookie context.
    Long videos require preparing the entire low-quality copy before seeking.
    """
    def __init__(self, cache_root, command_factory, ffmpeg, stop_process=None,
                 error_formatter=None, process_env=None, *, max_previews=2,
                 max_preview_bytes=1024 * MIB, max_cache_bytes=2048 * MIB,
                 expiry=2 * 3600, timeout=2 * 3600):
        self.root = Path(cache_root).absolute()
        if _is_link(self.root):
            raise ValueError('A pasta de prévias não pode ser um link.')
        self.root.mkdir(parents=True, exist_ok=True)
        self.root = self.root.resolve()
        self.command_factory = command_factory
        self.ffmpeg = ffmpeg
        self.stop_process = stop_process or terminate_tree
        self.error_formatter = error_formatter
        self.process_env = process_env
        self.max_previews = max_previews
        self.max_preview_bytes = max_preview_bytes
        self.max_cache_bytes = max_cache_bytes
        self.expiry, self.timeout = expiry, timeout
        self._lock = threading.RLock()
        self._items = {}
        self._closed = False
        # Only managed directories are cleaned; nothing outside this root is
        # traversed. An unknown directory/file is deliberately left untouched.
        self._clean_abandoned()

    def _checked_folder(self, folder, id):
        if not ID_PATTERN.fullmatch(id) or folder.parent != self.root or _is_link(folder):
            raise ValueError('Pasta de prévia inválida.')
        if not folder.is_dir() or folder.resolve().parent != self.root:
            raise ValueError('Pasta de prévia inválida.')
        marker = folder / '.keep-preview.json'
        if _is_link(marker):
            raise ValueError('Identificação da prévia inválida.')
        try:
            data = json.loads(marker.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise ValueError('Identificação da prévia inválida.') from error
        if data != {'id': id, 'type': 'keep-preview'}:
            raise ValueError('Identificação da prévia inválida.')
        return folder

    def _remove_folder(self, folder, id):
        try:
            folder = self._checked_folder(folder, id)
            for child in folder.iterdir():
                if _is_link(child) or not child.is_file():
                    continue
                if (child.name.startswith('source.') or child.name.startswith('preview.')
                        or child.name in ('download.log', 'convert.log', 'probe.log', '.keep-process.json')):
                    child.unlink(missing_ok=True)
            remaining = list(folder.iterdir())
            if len(remaining) == 1 and remaining[0].name == '.keep-preview.json':
                remaining[0].unlink()
                folder.rmdir()
        except (OSError, ValueError):
            # Never widen deletion because a malicious link or a locked file
            # prevented cleanup. A later launch retries the owned files.
            pass

    def _clean_abandoned(self):
        for folder in self.root.iterdir():
            if ID_PATTERN.fullmatch(folder.name) and folder.is_dir() and not _is_link(folder):
                if self._stop_abandoned(folder):
                    self._remove_folder(folder, folder.name)

    def _stop_abandoned(self, folder):
        """A forced close must not leave an old engine writing into the cache.

        A saved PID alone is insufficient: require the executable, process start
        identity and this exact randomly named output directory to still match.
        If inspection fails, preserve the folder instead of risking another app.
        """
        try:
            self._checked_folder(folder, folder.name)
            marker = folder / '.keep-process.json'
            if not marker.exists():
                return True
            if _is_link(marker):
                return False
            saved = json.loads(marker.read_text(encoding='utf-8'))
            pid = saved.get('pid')
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                return False
            if sys.platform.startswith('linux'):
                process_root = Path('/proc') / str(pid)
                try:
                    identity = (process_root / 'stat').read_text().rsplit(')', 1)[1].split()[19]
                    command = (process_root / 'cmdline').read_bytes().replace(b'\0', b' ').decode('utf-8', 'replace')
                    executable = os.readlink(process_root / 'exe')
                except FileNotFoundError:
                    return True
                if identity != saved.get('identity') or executable != saved.get('executable'):
                    return True
                if str(folder) not in command and str(folder).replace('%', '%%') not in command:
                    return False
                if os.getpgid(pid) != pid:
                    return False
                os.killpg(pid, signal.SIGTERM)
                time.sleep(0.2)
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                return True
            if os.name == 'nt':
                script = ('Get-CimInstance Win32_Process -Filter ("ProcessId="+$env:KEEP_PREVIEW_PID) | '
                          'Select-Object ExecutablePath,CommandLine,CreationDate | ConvertTo-Json -Compress')
                result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', script], capture_output=True,
                                        text=True, encoding='utf-8', errors='replace', timeout=12,
                                        creationflags=CREATE_FLAGS, env=dict(os.environ, KEEP_PREVIEW_PID=str(pid)))
                if result.returncode:
                    return False
                if not result.stdout.strip():
                    return True
                current = json.loads(result.stdout)
                command = current.get('CommandLine') or ''
                executable = (current.get('ExecutablePath') or '').casefold()
                if executable != str(saved.get('executable') or '').casefold():
                    return True
                if saved.get('identity') is not None and current.get('CreationDate') != saved['identity']:
                    return True
                if str(folder) not in command and str(folder).replace('%', '%%') not in command:
                    return False
                result = subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True,
                                        timeout=15, creationflags=CREATE_FLAGS)
                return result.returncode == 0
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return False
        return False

    def _record_process(self, item, process, command):
        identity = None
        executable = str(Path(command[0]).resolve())
        if sys.platform.startswith('linux'):
            try:
                process_root = Path('/proc') / str(process.pid)
                identity = (process_root / 'stat').read_text().rsplit(')', 1)[1].split()[19]
                executable = os.readlink(process_root / 'exe')
            except OSError:
                pass
        # Windows checks the live command's output folder in addition to the
        # executable, so PID reuse cannot match an unrelated process.
        (item.folder / '.keep-process.json').write_text(json.dumps({
            'pid': process.pid, 'identity': identity, 'executable': executable,
        }), encoding='utf-8')

    def _folder_size(self, item):
        folder = self._checked_folder(item.folder, item.id)
        return sum(child.stat().st_size for child in folder.iterdir()
                   if not _is_link(child) and child.is_file())

    def _cache_size(self):
        total = 0
        for folder in self.root.iterdir():
            if not folder.is_dir() or _is_link(folder) or not ID_PATTERN.fullmatch(folder.name):
                continue
            try:
                self._checked_folder(folder, folder.name)
                total += sum(child.stat().st_size for child in folder.iterdir()
                             if not _is_link(child) and child.is_file())
            except (OSError, ValueError):
                continue
        return total

    def _expire(self):
        now = time.monotonic()
        for id, item in list(self._items.items()):
            if item.status != 'preparing' and now - item.touched > self.expiry:
                self._remove_folder(item.folder, id)
                del self._items[id]

    @staticmethod
    def _snapshot(item):
        return {'id': item.id, 'status': item.status, 'stage': item.stage,
                'error': item.error, 'duration': item.duration,
                'width': item.width, 'height': item.height, 'fps': item.fps,
                'frame_times': item.frame_times if item.status == 'ready' else None,
                'frames_indexed': item.frames_indexed if item.status == 'ready' else False,
                'frame_note': item.frame_note,
                'media_url': f'/preview/{item.id}?key={item.key}' if item.status == 'ready' else None}

    def create(self, request, duration=None):
        with self._lock:
            if self._closed:
                raise ValueError('O Keep está fechando. Abra-o novamente para preparar uma prévia.')
            if not self.ffmpeg:
                raise ValueError('A prévia precisa do FFmpeg. Prepare os componentes e reabra o Keep.')
            self._expire()
            # Reuse the small cache by evicting the least recently used finished
            # preview; active workers are never evicted or raced by cleanup.
            while len(self._items) >= self.max_previews:
                idle = [item for item in self._items.values() if item.status != 'preparing']
                if not idle:
                    raise ValueError('Já existem duas prévias sendo preparadas. Aguarde ou cancele uma delas.')
                oldest = min(idle, key=lambda item: item.touched)
                self._remove_folder(oldest.folder, oldest.id)
                del self._items[oldest.id]
            if self._cache_size() >= self.max_cache_bytes:
                raise ValueError('Não há espaço no limite temporário das prévias. Cancele uma prévia e tente novamente.')
            id, key = secrets.token_hex(16), secrets.token_urlsafe(32)
            folder = self.root / id
            folder.mkdir()
            (folder / '.keep-preview.json').write_text(json.dumps({'id': id, 'type': 'keep-preview'}), encoding='utf-8')
            if isinstance(request, dict) and request.get('local_clip'):
                try:
                    shutil.copyfile(request['local_clip'], folder / 'source.mp4')
                except OSError:
                    self._remove_folder(folder, id)
                    raise ValueError('O clipe não está mais disponível. Salve outro trecho da live.') from None
            now = time.monotonic()
            valid_duration = isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) and duration > 0
            item = _Preview(id, key, folder, request, now, now, duration if valid_duration else None)
            item.live_clip = isinstance(request, dict) and bool(request.get('local_clip'))
            item.imported = isinstance(request,dict) and request.get('imported') is True
            self._items[id] = item
            item.thread = threading.Thread(target=self._prepare, args=(item,), daemon=True, name='Keep-preview-' + id)
            result = self._snapshot(item)
            item.thread.start()
            return result

    def status(self, id):
        if not isinstance(id, str) or not ID_PATTERN.fullmatch(id):
            raise ValueError('Prévia não encontrada.')
        with self._lock:
            self._expire()
            item = self._items.get(id)
            if item is None:
                raise ValueError('A prévia expirou. Prepare-a novamente.')
            item.touched = time.monotonic()
            return self._snapshot(item)

    def import_clip(self, path, imported=False):
        """Import a trusted, completed live clip without reducing its quality."""
        return self.create({'local_clip': str(path), 'imported': imported})

    def live_file(self, id, key):
        path = self.media_file(id, key)
        with self._lock:
            if not self._items[id].live_clip:
                raise ValueError('Escolha um clipe criado a partir de uma live no Keep.')
            if self._items[id].imported:
                original=self._checked_folder(self._items[id].folder,id)/'source.original'
                if _is_link(original) or not original.is_file():raise ValueError('O arquivo importado expirou. Importe novamente.')
                return original
        return path

    def media_file(self, id, key):
        if not isinstance(id, str) or not ID_PATTERN.fullmatch(id):
            raise ValueError('Prévia não encontrada.')
        with self._lock:
            self._expire()
            item = self._items.get(id)
            if item is None or not isinstance(key, str) or not key.isascii() or not hmac.compare_digest(item.key, key):
                raise ValueError('Prévia não encontrada.')
            if item.status != 'ready':
                raise ValueError('A prévia ainda não está pronta.')
            folder = self._checked_folder(item.folder, id)
            path = folder / 'preview.mp4'
            if _is_link(path) or not path.is_file():
                raise ValueError('Prévia não encontrada.')
            item.touched = time.monotonic()
            return path

    def cancel(self, id):
        if not isinstance(id, str) or not ID_PATTERN.fullmatch(id):
            raise ValueError('Prévia não encontrada.')
        with self._lock:
            item = self._items.get(id)
            if item is None:
                raise ValueError('Prévia não encontrada.')
            item.cancelled.set()
            process = item.process
            if item.status != 'preparing':
                item.status, item.stage, item.error = 'cancelled', 'Prévia descartada.', None
                self._remove_folder(item.folder, id)
        if process is not None:
            self.stop_process(process)
        return self.status(id)

    def close(self):
        with self._lock:
            self._closed = True
            items = list(self._items.values())
        for item in items:
            try:
                self.cancel(item.id)
            except ValueError:
                pass
        deadline = time.monotonic() + 20
        for item in items:
            if item.thread:
                item.thread.join(timeout=max(0, deadline - time.monotonic()))

    def _check_limits(self, item, deadline):
        if item.cancelled.is_set():
            raise ValueError('Prévia cancelada.')
        if time.monotonic() > deadline:
            raise ValueError('A preparação da prévia demorou demais. Tente novamente ou informe o trecho pelos horários.')
        if self._folder_size(item) > self.max_preview_bytes * 2 or self._cache_size() > self.max_cache_bytes:
            raise ValueError('A prévia excedeu o limite temporário de espaço. Escolha o trecho pelos horários para este vídeo.')
        for child in item.folder.iterdir():
            if not _is_link(child) and child.is_file() and child.stat().st_size > self.max_preview_bytes:
                raise ValueError('Este vídeo é grande demais para a prévia. Escolha o trecho pelos horários.')

    def _run(self, item, command, log_name, deadline, allow_probe=False):
        if log_name == 'frames.log':
            return self._run_frame_index(item, command, deadline)
        self._check_limits(item, deadline)
        log = item.folder / log_name
        with log.open('wb') as output:
            with self._lock:
                if item.cancelled.is_set():
                    raise ValueError('Prévia cancelada.')
                process = subprocess.Popen(list(map(str, command)), stdout=output, stderr=subprocess.STDOUT,
                                           creationflags=CREATE_FLAGS, start_new_session=os.name != 'nt',
                                           env=self.process_env() if callable(self.process_env) else self.process_env)
                process._keep_group = os.name != 'nt'
                item.process = process
                try:
                    self._record_process(item, process, command)
                except Exception:
                    self.stop_process(process)
                    if process.stdout:
                        process.stdout.close()
                    item.process = None
                    raise
            try:
                while process.poll() is None:
                    self._check_limits(item, deadline)
                    time.sleep(0.15)
                self._check_limits(item, deadline)
            finally:
                if process.poll() is None:
                    self.stop_process(process)
                with self._lock:
                    if item.process is process:
                        item.process = None
                (item.folder / '.keep-process.json').unlink(missing_ok=True)
        # Logs never enter public status; only a small friendly error does.
        with log.open('rb') as source:
            source.seek(max(0, log.stat().st_size - 24000))
            text = source.read().decode('utf-8', 'replace')
        if process.returncode and not allow_probe:
            message = self.error_formatter(text, item.request) if self.error_formatter else None
            raise ValueError(message or 'Não foi possível preparar a prévia. Tente novamente ou informe o trecho pelos horários.')
        return text

    def _run_frame_index(self, item, command, deadline):
        """Capture frame timestamps without retaining FFmpeg's verbose logs."""
        self._check_limits(item, deadline)
        log = item.folder / 'frames.log'
        if _is_link(log):
            raise ValueError('Não foi possível preparar o índice de quadros.')
        failed, finished = threading.Event(), threading.Event()
        counters = {'frames': 0, 'bytes': 0, 'last': -1.0}
        with log.open('wb') as output:
            with self._lock:
                if item.cancelled.is_set():
                    raise ValueError('Prévia cancelada.')
                process = subprocess.Popen(list(map(str, command)), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding='utf-8', errors='replace', bufsize=1, creationflags=CREATE_FLAGS,
                    start_new_session=os.name != 'nt',
                    env=self.process_env() if callable(self.process_env) else self.process_env)
                process._keep_group = os.name != 'nt'
                item.process = process
                try:
                    self._record_process(item, process, command)
                except Exception:
                    self.stop_process(process)
                    if process.stdout:
                        process.stdout.close()
                    item.process = None
                    raise

            def capture():
                try:
                    for line in iter(lambda: process.stdout.readline(8192), ''):
                        match = FRAME_LINE.search(line)
                        if not match:
                            continue
                        index, timestamp = int(match[1]), float(match[2])
                        if (index != counters['frames'] or index >= MAX_FRAME_INDEX
                                or not math.isfinite(timestamp) or timestamp < 0
                                or timestamp <= counters['last']):
                            failed.set()
                            return
                        value = f'{timestamp:.6f}\n'.encode('ascii')
                        if counters['bytes'] + len(value) > MAX_FRAME_INDEX_BYTES:
                            failed.set()
                            return
                        output.write(value)
                        counters.update(frames=index + 1, bytes=counters['bytes'] + len(value), last=timestamp)
                except (OSError, ValueError, TypeError):
                    failed.set()
                finally:
                    finished.set()

            reader = threading.Thread(target=capture, daemon=True, name='Keep-frame-index-' + item.id)
            reader.start()
            try:
                while process.poll() is None:
                    self._check_limits(item, deadline)
                    if failed.is_set():
                        raise ValueError('O índice de quadros excedeu o limite ou ficou incompleto.')
                    time.sleep(.05)
                self._check_limits(item, deadline)
            finally:
                if process.poll() is None:
                    self.stop_process(process)
                reader.join(timeout=5)
                if process.stdout:
                    process.stdout.close()
                with self._lock:
                    if item.process is process:
                        item.process = None
                (item.folder / '.keep-process.json').unlink(missing_ok=True)
            if process.returncode or failed.is_set() or not finished.is_set() or not counters['frames']:
                raise ValueError('Não foi possível indexar todos os quadros desta prévia.')
        return ''

    def _index_frames(self, item, target, deadline):
        """A missing index must never discard an otherwise usable preview."""
        log = item.folder / 'frames.log'
        try:
            with self._lock:
                item.stage = 'Preparando a navegação por quadros…'
            self._run(item, [self.ffmpeg, '-hide_banner', '-nostdin', '-nostats', '-loglevel', 'info',
                '-i', target, '-map', '0:v:0', '-an', '-sn', '-dn', '-vf', 'showinfo',
                '-frames:v', str(MAX_FRAME_INDEX + 1), '-vsync', '0', '-f', 'null', '-'],
                'frames.log', min(deadline, time.monotonic() + FRAME_INDEX_TIMEOUT))
            if _is_link(log) or log.stat().st_size > MAX_FRAME_INDEX_BYTES:
                return None
            times = []
            with log.open('r', encoding='ascii') as source:
                for line in source:
                    value = float(line)
                    if (len(times) >= MAX_FRAME_INDEX or not math.isfinite(value) or value < 0
                            or times and value <= times[-1]):
                        return None
                    times.append(value)
            return times or None
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            if item.cancelled.is_set():
                raise ValueError('Prévia cancelada.') from None
            return None
        finally:
            if log.is_file() and not _is_link(log):
                log.unlink(missing_ok=True)

    def _probe(self, item, path, deadline):
        text = self._run(item, [self.ffmpeg, '-hide_banner', '-i', path], 'probe.log', deadline, allow_probe=True)
        match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', text)
        if not match or 'Video:' not in text:
            raise ValueError('A plataforma não entregou um vídeo válido para a prévia.')
        hours, minutes, seconds = map(float, match.groups())
        duration = hours * 3600 + minutes * 60 + seconds
        if duration <= 0 or not math.isfinite(duration):
            raise ValueError('A plataforma não informou a duração da prévia.')
        return duration, text

    def _prepare(self, item):
        deadline = time.monotonic() + self.timeout
        try:
            local_clip = item.request.get('local_clip') if isinstance(item.request, dict) else None
            if local_clip:
                context = nullcontext(None)
            else:
                context = self.command_factory(item.request, item.folder)
            if isinstance(context, (list, tuple)):
                context = nullcontext(context)
            with context as command:
                if command:
                    self._run(item, command, 'download.log', deadline)
            sources = [path for path in item.folder.iterdir() if path.name in
                       ('source.mp4', 'source.mkv', 'source.webm', 'source.mov', 'source.flv', 'source.ts')
                       and path.is_file() and not _is_link(path)]
            if len(sources) != 1:
                raise ValueError('A plataforma não entregou o arquivo da prévia. Tente novamente.')
            source = sources[0]
            source_duration, probe = self._probe(item, source, deadline)
            with self._lock:
                item.stage = 'Preparando o vídeo para assistir no Keep…'
            target = item.folder / 'preview.mp4'
            video_line = next((line for line in probe.splitlines() if 'Video:' in line), '')
            dimensions = re.search(r'\b(\d{2,5})x(\d{2,5})\b', video_line)
            within_height = bool(dimensions) and int(dimensions.group(2)) <= 480
            copy = (within_height or bool(local_clip)) and bool(re.search(r'Video:\s*h264(?:\s|\()', video_line)) and bool(re.search(r',\s*yuv420p(?:\(|,|\s)', video_line)) and (
                'Audio:' not in probe or bool(re.search(r'Audio:\s*aac(?:\s|\()', probe)))
            command = [self.ffmpeg, '-hide_banner', '-nostdin', '-y', '-i', source, '-map', '0:v:0', '-map', '0:a:0?', '-sn', '-dn']
            if copy:
                command += ['-c', 'copy']
            else:
                command += ['-vf', 'scale=480:480:force_original_aspect_ratio=decrease:force_divisible_by=2',
                            '-c:v', 'libx264', '-preset', 'veryfast', '-threads', '2', '-pix_fmt', 'yuv420p',
                            '-b:v', '420k', '-maxrate', '600k', '-bufsize', '1200k', '-c:a', 'aac', '-b:a', '64k']
            command += ['-movflags', '+faststart', target]
            self._run(item, command, 'convert.log', deadline)
            duration, final_probe = self._probe(item, target, deadline)
            if abs(source_duration - duration) > max(1.0, source_duration * 0.002):
                raise ValueError('A prévia ficou incompleta. Tente novamente ou informe o trecho pelos horários.')
            frame_times = self._index_frames(item, target, deadline)
            with self._lock:
                if item.cancelled.is_set():
                    raise ValueError('Prévia cancelada.')
                if item.imported:source.rename(item.folder/'source.original')
                else:source.unlink()
                for name in ('download.log', 'convert.log', 'probe.log'):
                    (item.folder / name).unlink(missing_ok=True)
                item.status, item.stage, item.duration = 'ready', 'Prévia pronta.', duration
                item.frame_times = frame_times
                item.frames_indexed = frame_times is not None
                item.frame_note = ('Navegação pelos quadros reais desta prévia.' if item.frames_indexed else
                    'Não foi possível indexar todos os quadros. O avanço usa um passo estimado; confira a imagem antes de marcar o corte.')
                final_video = next((line for line in final_probe.splitlines() if 'Video:' in line), '')
                final_dimensions = re.search(r'\b(\d{2,5})x(\d{2,5})\b', final_video)
                final_fps = re.search(r'([\d.]+) fps', final_video)
                if final_dimensions:
                    item.width, item.height = map(int, final_dimensions.groups())
                if final_fps:
                    value = float(final_fps.group(1))
                    item.fps = value if math.isfinite(value) and 0 < value <= 240 else None
                item.request = None  # Release imported cookies as soon as preparation finishes.
                item.touched = time.monotonic()
        except Exception as error:
            with self._lock:
                process = item.process
            if process is not None:
                try:
                    self.stop_process(process)
                except (OSError, subprocess.SubprocessError):
                    pass
            with self._lock:
                cancelled = item.cancelled.is_set()
                item.status = 'cancelled' if cancelled else 'failed'
                item.stage = 'Prévia cancelada.' if cancelled else 'Não foi possível preparar a prévia.'
                item.error = None if cancelled else str(error)[:700]
                item.request = None
                item.process = None
                self._remove_folder(item.folder, item.id)

# The optional caption recognizer lives in this existing update-allowlisted
# module. Native Vosk binaries use ctypes, including embedded Windows Python;
# no pip, API key, audio upload, or machine-wide installation is required.
CAPTION_NATIVE = {
    'linux-x64': ('vosk-linux-x86_64-0.3.45.zip', 'bbdc8ed85c43979f6443142889770ea95cbfbc56cffb5c5dcd73afa875c5fbb2'),
    'linux-arm64': ('vosk-linux-aarch64-0.3.45.zip', '45e95d37755deb07568e79497d7feba8c03aee5a9e071df29961aa023fd94541'),
    'windows-x64': ('vosk-win64-0.3.45.zip', 'f1dcc9cca460630f81ea8f71794f69c80bed6556d2a4e6237b5785e1d2dff34b'),
}
CAPTION_MODELS = {
    'pt': {
        'name': 'vosk-model-small-pt-0.3',
        'url': 'https://alphacephei.com/vosk/models/vosk-model-small-pt-0.3.zip',
        'sha256': '6e1ce909032e1afa7a88e68a3d628ecafff302bdf195befab308826c395e93b7',
    },
    'en': {
        'name': 'vosk-model-small-en-us-0.15',
        'url': 'https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip',
        'sha256': '30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498',
    },
}


class _CaptionCancelled(Exception):
    pass


def caption_cues(words):
    """Turn timed ASR words into short, non-overlapping source-clock captions."""
    cues, group = [], []
    previous = 0.0

    def finish():
        if not group:
            return
        text = ' '.join(word['word'] for word in group)
        cues.append({'start': round(group[0]['start'], 3),
                     'end': round(max(group[-1]['end'], group[0]['start'] + .1), 3),
                     'text': text[0].upper() + text[1:]})
        group.clear()

    if not isinstance(words, list):
        raise ValueError('O reconhecedor não retornou palavras válidas.')
    for item in words:
        if not isinstance(item, dict):
            raise ValueError('O reconhecedor retornou um horário inválido.')
        start, end, text = item.get('start'), item.get('end'), item.get('word')
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)
                or not 0 <= start < end
                or start < previous - .05 or not isinstance(text, str)):
            raise ValueError('O reconhecedor retornou um horário inválido.')
        text = ' '.join(''.join(c for c in text if ord(c) >= 32).split())
        if not text or len(text) > 100:
            continue
        word = {'start': max(start, previous), 'end': end, 'word': text}
        if word['start'] >= word['end']:
            continue
        if group and (len(group) >= 8 or len(' '.join(x['word'] for x in group)) + len(text) > 68
                      or start - group[-1]['end'] > .7 or end - group[0]['start'] > 4.5):
            finish()
        group.append(word)
        previous = end
    finish()
    return cues


def _caption_digest(path):
    import hashlib
    value = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def _caption_platform():
    import platform
    machine = platform.machine().lower()
    arch = 'x64' if machine in ('x86_64', 'amd64') else 'arm64' if machine in ('aarch64', 'arm64') else ''
    system = 'windows' if sys.platform == 'win32' else 'linux' if sys.platform.startswith('linux') else ''
    key = system + '-' + arch
    if key not in CAPTION_NATIVE:
        raise ValueError('As legendas locais precisam de Windows x64 ou Linux x64/ARM64.')
    return key


def _caption_component(root, name, url, expected, cancelled, progress=None):
    """Verify download and extracted files; never execute archive paths."""
    import hashlib
    import shutil
    import stat
    import tempfile
    import zipfile
    from pathlib import PurePosixPath
    from urllib.request import Request, urlopen
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('github.com', 'alphacephei.com') or parsed.username or parsed.password:
        raise ValueError('O componente de legendas exige HTTPS de uma origem oficial.')
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+', name) or not re.fullmatch(r'[0-9a-f]{64}', expected):
        raise ValueError('A configuração do reconhecedor está inválida.')
    root = Path(root).absolute()
    if any(_is_link(p) for p in (root, *root.parents)):
        raise ValueError('A pasta do reconhecedor não pode ser um vínculo.')
    root.mkdir(parents=True, exist_ok=True)
    destination = root / name
    marker = destination / '.keep-caption-component.json'
    if destination.exists():
        if _is_link(destination) or _is_link(marker):
            raise ValueError('A pasta do reconhecedor não pode ser um vínculo.')
        try:
            saved = json.loads(marker.read_text(encoding='utf-8'))
            if saved['archive_sha256'] != expected or not isinstance(saved['files'], dict) or not saved['files']:
                raise ValueError()
            actual = {path.relative_to(destination).as_posix() for path in destination.rglob('*') if path.is_file()}
            if actual != set(saved['files']) | {marker.name}:
                raise ValueError()
            for relative, digest in saved['files'].items():
                part = PurePosixPath(relative)
                if part.is_absolute() or '..' in part.parts or '\\' in relative or ':' in relative:
                    raise ValueError()
                path = destination.joinpath(*part.parts)
                if _is_link(path) or any(_is_link(p) for p in path.parents if p != root):
                    raise ValueError()
                if not path.is_file() or _caption_digest(path) != digest:
                    raise ValueError()
            return destination
        except (OSError, ValueError, KeyError, TypeError):
            raise ValueError('Os arquivos do reconhecedor foram alterados. Reinstale o componente de legendas.') from None
    with tempfile.TemporaryDirectory(prefix='.keep-caption-', dir=root) as temporary:
        temporary = Path(temporary)
        archive_path = temporary / 'download.zip'
        hasher, size, started = hashlib.sha256(), 0, time.monotonic()
        request = Request(url, headers={'User-Agent': 'Keep-local-captions'})
        with urlopen(request, timeout=30) as response, archive_path.open('wb') as output:
            while chunk := response.read1(512 * 1024):
                if cancelled.is_set():
                    raise _CaptionCancelled()
                if time.monotonic() - started > 300:
                    raise ValueError('O download das legendas demorou demais. Tente novamente.')
                size += len(chunk)
                if size > 128 * MIB:
                    raise ValueError('O componente de legendas excede o tamanho permitido.')
                hasher.update(chunk); output.write(chunk)
                if progress:
                    progress(size)
        if hasher.hexdigest() != expected:
            raise ValueError('A verificação do componente de legendas falhou. Tente novamente.')
        target, files, total, prefix = temporary / 'ready', {}, 0, None
        target.mkdir()
        with zipfile.ZipFile(archive_path) as archive:
            if len(archive.infolist()) > 4096:
                raise ValueError('O componente de legendas contém arquivos demais.')
            for member in archive.infolist():
                raw = member.filename.rstrip('/') if member.is_dir() else member.filename
                part = PurePosixPath(raw)
                mode = member.external_attr >> 16
                if (not raw or part.is_absolute() or '..' in part.parts or '\\' in raw or ':' in raw
                        or '\0' in raw or part.as_posix() != raw or stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                    raise ValueError('O componente de legendas contém um caminho inválido.')
                prefix = prefix or part.parts[0]
                if part.parts[0] != prefix:
                    raise ValueError('O componente de legendas contém uma pasta inesperada.')
                if member.is_dir():
                    continue
                relative = '/'.join(part.parts[1:])
                if not relative or relative in files:
                    raise ValueError('O componente de legendas contém um arquivo inesperado.')
                total += member.file_size
                if total > 512 * MIB:
                    raise ValueError('O componente de legendas expandido excede o limite permitido.')
                if cancelled.is_set():
                    raise _CaptionCancelled()
                path = target.joinpath(*part.parts[1:]); path.parent.mkdir(parents=True, exist_ok=True)
                content = archive.read(member)
                path.write_bytes(content); files[relative] = hashlib.sha256(content).hexdigest()
        if not files:
            raise ValueError('O componente de legendas está vazio.')
        (target / marker.name).write_text(json.dumps({'archive_sha256': expected, 'files': files}), encoding='utf-8')
        target.replace(destination)
    return destination




def caption_model_path(model):
    """Vosk's Windows file APIs can fail for Unicode installation paths."""
    model = Path(model).resolve()
    if not ((model/'am'/'final.mdl').is_file() and (model/'conf'/'mfcc.conf').is_file() or (model/'final.mdl').is_file() and (model/'mfcc.conf').is_file()):
        raise ValueError('O modelo de voz está incompleto. Repare os componentes das legendas.')
    if os.name != 'nt' or str(model).isascii():
        return model, None
    import ctypes
    import tempfile
    buffer = ctypes.create_unicode_buffer(32768)
    short_path=ctypes.windll.kernel32.GetShortPathNameW
    short_path.argtypes=[ctypes.c_wchar_p,ctypes.c_wchar_p,ctypes.c_uint32]
    short_path.restype=ctypes.c_uint32
    if short_path(str(model), buffer, len(buffer)) and buffer.value.isascii():
        return Path(buffer.value), None
    candidates = [os.environ.get('PUBLIC'), os.environ.get('TEMP'), str(Path(os.environ.get('SystemRoot','C:/Windows'))/'Temp')]
    for value in candidates:
        if not value or not value.isascii():
            continue
        try:
            temporary = tempfile.TemporaryDirectory(prefix='keep-voice-', dir=value)
            destination = Path(temporary.name)/'model'
            shutil.copytree(model, destination)
            return destination, temporary
        except OSError:
            if 'temporary' in locals():temporary.cleanup()
    raise ValueError('O Windows não conseguiu preparar o modelo em um caminho compatível. Extraia o Keep em uma pasta sem acentos, como C:\\Keep, e tente novamente.')

class _CaptionRecognizer:
    """Small ctypes wrapper for the official cross-platform Vosk C API."""
    def __init__(self, library, model):
        import ctypes
        self.dll_directory = os.add_dll_directory(str(library.parent)) if os.name == 'nt' else None
        self.library = ctypes.CDLL(str(library))
        definitions = {
            'vosk_set_log_level': ([ctypes.c_int], None),
            'vosk_model_new': ([ctypes.c_char_p], ctypes.c_void_p),
            'vosk_model_free': ([ctypes.c_void_p], None),
            'vosk_recognizer_new': ([ctypes.c_void_p, ctypes.c_float], ctypes.c_void_p),
            'vosk_recognizer_set_words': ([ctypes.c_void_p, ctypes.c_int], None),
            'vosk_recognizer_accept_waveform': ([ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int], ctypes.c_int),
            'vosk_recognizer_result': ([ctypes.c_void_p], ctypes.c_char_p),
            'vosk_recognizer_final_result': ([ctypes.c_void_p], ctypes.c_char_p),
            'vosk_recognizer_free': ([ctypes.c_void_p], None),
        }
        for name, (args, result) in definitions.items():
            function = getattr(self.library, name); function.argtypes = args; function.restype = result
        self.library.vosk_set_log_level(-1)
        compatible, self.model_copy = caption_model_path(model)
        self.model = self.library.vosk_model_new(os.fsencode(compatible))
        if not self.model:
            if self.model_copy:self.model_copy.cleanup()
            raise ValueError('Não foi possível carregar o modelo de voz. Repare os componentes e tente novamente; no Windows, use uma pasta sem acentos se o erro persistir.')

    def transcribe(self, path, cancelled, progress):
        import wave
        recognizer = self.library.vosk_recognizer_new(self.model, 16000.0)
        if not recognizer:
            raise ValueError('Não foi possível iniciar o reconhecedor de voz.')
        self.library.vosk_recognizer_set_words(recognizer, 1)
        words = []
        try:
            with wave.open(str(path), 'rb') as audio:
                total = audio.getnframes()
                if audio.getnchannels() != 1 or audio.getsampwidth() != 2 or audio.getframerate() != 16000:
                    raise ValueError('O áudio da prévia não pôde ser preparado para as legendas.')
                read = 0
                while content := audio.readframes(4000):
                    if cancelled.is_set():
                        raise _CaptionCancelled()
                    accepted = self.library.vosk_recognizer_accept_waveform(recognizer, content, len(content))
                    if accepted < 0:
                        raise ValueError('O reconhecedor não conseguiu processar este áudio.')
                    if accepted:
                        words.extend(json.loads(self.library.vosk_recognizer_result(recognizer)).get('result', []))
                    read += len(content) // 2
                    progress(min(99, round(read / max(1, total) * 100)))
                words.extend(json.loads(self.library.vosk_recognizer_final_result(recognizer)).get('result', []))
            return caption_cues(words)
        finally:
            self.library.vosk_recognizer_free(recognizer)

    def close(self):
        if self.model:
            self.library.vosk_model_free(self.model); self.model = None
        if self.dll_directory is not None:
            self.dll_directory.close(); self.dll_directory = None
        if self.model_copy is not None:
            self.model_copy.cleanup();self.model_copy=None


class SubtitleManager:
    """Explicit, local, asynchronous ASR; one worker with bounded disposable jobs."""
    def __init__(self, root, ffmpeg, process_env=None):
        from concurrent.futures import ThreadPoolExecutor
        self.root = Path(root).resolve()
        self.cache = self.root / 'data' / 'subtitles'
        self.tools = self.root / 'tools' / 'subtitles'
        self.ffmpeg, self.process_env = ffmpeg, process_env
        self._lock, self._tasks, self._closed = threading.RLock(), {}, False
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='keep-captions')
        self._engine, self._language = None, None
        self._closed_event = threading.Event()

    def _snapshot(self, task):
        return {**{key: task[key] for key in ('id', 'preview_id', 'language', 'status', 'stage', 'progress', 'error')},
                'cues': [dict(cue) for cue in task['cues']]}

    def create(self, source, language='pt', preview_id='', duration=None, sections=None):
        if language not in CAPTION_MODELS:
            raise ValueError('Escolha um idioma disponível para as legendas.')
        if not self.ffmpeg:
            raise ValueError('As legendas precisam do FFmpeg.')
        source = Path(source).absolute()
        if not source.is_file() or any(_is_link(p) for p in (source, *source.parents)):
            raise ValueError('Prepare novamente a prévia antes de gerar as legendas.')
        if duration is not None and (type(duration) not in (int, float) or not math.isfinite(duration) or not duration > 0):
            raise ValueError('A duração do vídeo é inválida.')
        if sections is not None:
            from media_editor import validate_edit
            if duration is None:raise ValueError('Prepare uma prévia com duração informada.')
            requested=validate_edit({'sections':sections},duration)['sections']
            merged=[]
            for section in sorted(requested,key=lambda part:part['start']):
                if merged and section['start']<=merged[-1]['end']:
                    merged[-1]['end']=max(merged[-1]['end'],section['end'])
                else:merged.append(dict(section))
            sections=merged
        with self._lock:
            if self._closed:
                raise ValueError('O Keep está fechando.')
            active = [x for x in self._tasks.values() if x['status'] in ('queued', 'preparing', 'transcribing')]
            if len(active) >= 2:
                raise ValueError('Aguarde as legendas atuais terminarem ou cancele uma delas.')
            while len(self._tasks) >= 4:
                old = next((id for id, x in self._tasks.items() if x['status'] not in ('queued', 'preparing', 'transcribing')), None)
                if old is None:
                    break
                del self._tasks[old]
            task = {'id': secrets.token_hex(16), 'preview_id': str(preview_id), 'language': language,
                    'sections':sections,'duration':duration,'status': 'queued', 'stage': 'Aguardando o reconhecedor local…', 'progress': 0,
                    'cues': [], 'error': None, 'cancelled': threading.Event(), 'process': None}
            self._tasks[task['id']] = task
            self._executor.submit(self._run, task, source)
            return self._snapshot(task)

    def status(self, id):
        if not isinstance(id, str) or not ID_PATTERN.fullmatch(id):
            raise ValueError('Legendas não encontradas.')
        with self._lock:
            if id not in self._tasks:
                raise ValueError('Legendas não encontradas. Gere-as novamente.')
            return self._snapshot(self._tasks[id])

    def repair(self):
        with self._lock:
            if any(t['status'] in ('queued','preparing','transcribing') for t in self._tasks.values()):
                raise ValueError('Cancele ou aguarde a geração antes de reparar os modelos.')
            if self._engine:
                self._engine.close();self._engine=None;self._language=None
            for name in {m['name'] for m in CAPTION_MODELS.values()} | {'vosk-en-us-0.15-a5e58ec'}:
                path=self.tools/name
                if path.exists():
                    if any(_is_link(p) for p in (path,*path.parents)):
                        raise ValueError('A pasta dos modelos contém um vínculo.')
                    path.rename(path.with_name(name+'.previous-'+secrets.token_hex(6)))
            return {'message':'Modelos redefinidos. Clique em Gerar legendas para baixar uma cópia nova e verificada.'}

    def _update(self, task, **values):
        with self._lock:
            task.update(values)

    def _prepare(self, task):
        language = task['language']
        if self._engine is not None and self._language == language:
            return self._engine
        if self._engine is not None:
            self._engine.close(); self._engine = None
        key = _caption_platform(); name, expected = CAPTION_NATIVE[key]
        self._update(task, status='preparing', stage='Preparando o reconhecedor local. O primeiro uso precisa de internet…')
        native = _caption_component(self.tools, name[:-4],
                    'https://github.com/alphacep/vosk-api/releases/download/v0.3.45/' + name,
                    expected, task['cancelled'])
        model = CAPTION_MODELS[language]
        self._update(task, stage='Preparando o idioma das legendas. O primeiro download pode levar alguns minutos…')
        model_path = _caption_component(self.tools, model['name'], model['url'], model['sha256'], task['cancelled'])
        library = native / ('libvosk.dll' if key.startswith('windows') else 'libvosk.so')
        model_path = model_path.joinpath(*model.get('path', '').split('/')) if model.get('path') else model_path
        self._engine = _CaptionRecognizer(library, model_path); self._language = language
        return self._engine

    def _recognize_sections(self, task, source, folder, engine):
        # Small WAV files keep long videos compatible with RIFF and Python's
        # wave reader. Only requested source intervals are decoded.
        intervals=task['sections']
        if intervals is None:
            from media_editor import probe_media
            duration=task.get('duration') or probe_media(self.ffmpeg,source,self.process_env() if callable(self.process_env) else self.process_env)['duration']
            intervals=[{'start':0,'end':duration}]
        chunks=[]
        for section in intervals:
            start=section['start']
            while start<section['end']-.000001:
                end=min(section['end'],start+1200)
                chunks.append((start,end));start=end
        total=sum(end-start for start,end in chunks);elapsed=0;cues=[]
        self._update(task,status='transcribing',progress=0)
        for index,(start,end) in enumerate(chunks):
            if task['cancelled'].is_set():raise _CaptionCancelled()
            audio=folder/'audio.wav'
            self._update(task,stage=f'Preparando o áudio selecionado ({index+1}/{len(chunks)})…')
            with (folder/'ffmpeg.log').open('wb') as log:
                process=subprocess.Popen([self.ffmpeg,'-hide_banner','-nostdin','-v','error','-y','-ss',str(start),'-i',str(source),
                    '-t',str(end-start),'-vn','-ac','1','-ar','16000','-c:a','pcm_s16le',str(audio)],
                    stdout=subprocess.DEVNULL,stderr=log,creationflags=CREATE_FLAGS,start_new_session=os.name!='nt',
                    env=self.process_env() if callable(self.process_env) else self.process_env)
                process._keep_group=os.name!='nt';self._update(task,process=process)
                while process.poll() is None:
                    if task['cancelled'].wait(.1):terminate_tree(process);raise _CaptionCancelled()
                self._update(task,process=None)
                if process.returncode:raise ValueError('Não foi possível preparar o áudio selecionado. Confira se o vídeo contém áudio.')
            self._update(task,stage=f'Reconhecendo o trecho selecionado ({index+1}/{len(chunks)})…')
            batch=engine.transcribe(audio,task['cancelled'],lambda progress:self._update(task,progress=min(99,round((elapsed+(end-start)*progress/100)/total*100))))
            for cue in batch:
                a,b=max(start,start+cue['start']),min(end,start+cue['end'])
                if b>a and round(b,3)>round(a,3):cues.append({'start':round(a,3),'end':round(b,3),'text':cue['text']})
            elapsed+=end-start;audio.unlink(missing_ok=True)
        return cues

    def _run(self, task, source):
        import shutil
        import tempfile
        folder = None
        try:
            if task['cancelled'].is_set():
                raise _CaptionCancelled()
            engine = self._prepare(task)
            if task['cancelled'].is_set():
                raise _CaptionCancelled()
            if any(_is_link(path) for path in (self.cache, *self.cache.parents)):
                raise ValueError('A pasta temporária de legendas não pode ser um vínculo.')
            self.cache.mkdir(parents=True, exist_ok=True)
            folder = Path(tempfile.mkdtemp(prefix='caption-', dir=self.cache))
            cues=self._recognize_sections(task,source,folder,engine)
            if task['cancelled'].is_set():
                raise _CaptionCancelled()
            if not cues:
                self._update(task, status='ready', progress=100, cues=[], stage='Não foi encontrada fala reconhecível neste áudio.')
            else:
                self._update(task, status='ready', progress=100, cues=cues, stage='Legendas prontas. Confira o texto na prévia antes de baixar.')
        except _CaptionCancelled:
            self._update(task, status='cancelled', stage='Geração de legendas cancelada.', error=None, cues=[])
        except Exception as error:
            if task['cancelled'].is_set():
                self._update(task, status='cancelled', stage='Geração de legendas cancelada.', error=None, cues=[])
            else:
                message = str(error) if isinstance(error, ValueError) else 'Não foi possível preparar as legendas. Confira a conexão e tente novamente.'
                self._update(task, status='failed', error=message[:500], stage='Não foi possível gerar as legendas.', cues=[])
        finally:
            process = task.get('process')
            if process is not None:
                terminate_tree(process)
            self._update(task, process=None)
            if folder is not None:
                shutil.rmtree(folder, ignore_errors=True)

    def cancel(self, id):
        self.status(id)
        with self._lock:
            task = self._tasks[id]; task['cancelled'].set()
            process = task['process']
            if task['status'] not in ('ready', 'failed'):
                task.update(status='cancelled', stage='Geração de legendas cancelada.', error=None)
        if process is not None:
            terminate_tree(process)
        return self.status(id)

    def _release_engine(self):
        try:
            if self._engine is not None:
                self._engine.close(); self._engine = None; self._language = None
        finally:
            self._closed_event.set()

    def close(self):
        with self._lock:
            if self._closed:
                return
            self._closed = True
            ids = list(self._tasks)
        for id in ids:
            self.cancel(id)
        # Serialized behind any in-flight ctypes call, so native memory cannot
        # be freed while the recognizer is still reading it.
        self._executor.submit(self._release_engine)
        self._executor.shutdown(wait=False)
        self._closed_event.wait(2)


class LiveManager:
    """One local rolling buffer. Only complete segments can enter a clip.

    The server owns URL/cookie validation. command_factory yields a streaming
    yt-dlp argv, keeping its cookie context alive until the pipeline stops.
    Remote URLs and process diagnostics never enter browser responses.
    """
    def __init__(self, cache_root, command_factory, ffmpeg, process_env=None):
        import tempfile
        self.root = Path(cache_root).absolute()
        if any(_is_link(p) for p in (self.root, *self.root.parents)):
            raise ValueError('A pasta temporária das lives não pode ser um link.')
        self.root.mkdir(parents=True, exist_ok=True)
        self.command_factory, self.ffmpeg = command_factory, ffmpeg
        self.process_env = process_env
        self._lock = threading.RLock()
        self._clip_lock = threading.Lock()
        self._session = None
        self._closed = False
        # Each session uses TemporaryDirectory; never recursively remove paths
        # found on disk, supplied by callers, or saved in a previous process.
        self._temporary = tempfile.TemporaryDirectory

    def start(self, request):
        with self._lock:
            if self._closed or not self.ffmpeg:
                raise ValueError('Reabra o Keep com o FFmpeg disponível para acompanhar lives.')
            if self._session and self._session['thread'].is_alive():
                raise ValueError('Pare a live atual antes de acompanhar outra.')
            old = self._session
            if old:
                old['temp'].cleanup()
            temp = self._temporary(prefix='keep-live-', dir=self.root)
            session = {'id': secrets.token_hex(16), 'request': request, 'temp': temp,
                       'source': dict(request.get('info') or {}),
                       'folder': Path(temp.name), 'status': 'connecting', 'error': None,
                       'stop': threading.Event(), 'segments': [], 'processes': [],
                       'started': time.monotonic(), 'last_segment': time.monotonic()}
            thread = threading.Thread(target=self._capture, args=(session,), daemon=True, name='Keep-live')
            session['thread'] = thread
            self._session = session
            thread.start()
            return self.status(session['id'])

    def _get(self, id):
        session = self._session
        if not isinstance(id, str) or not session or not hmac.compare_digest(session['id'], id):
            raise ValueError('Live não encontrada. Inicie o acompanhamento novamente.')
        return session

    def status(self, id):
        with self._lock:
            session = self._get(id)
            seconds = min(120, len(session['segments']) * 2)
            return {'id': session['id'], 'status': session['status'], 'buffer_seconds': seconds,
                    'error': session['error'], 'max_seconds': 120,
                    'stage': {'connecting': 'Conectando à live e preparando o primeiro trecho…',
                              'buffering': 'Acompanhando a live. Escolha quantos segundos salvar.',
                              'ended': 'A transmissão terminou. Você ainda pode salvar o trecho disponível.',
                              'stopped': 'Acompanhamento parado. O trecho recente continua disponível.',
                              'failed': 'Não foi possível continuar acompanhando esta live.'}[session['status']]}

    def source(self, id):
        with self._lock:
            return dict(self._get(id)['source'])

    def busy(self):
        with self._lock:
            return self._clip_lock.locked() or bool(self._session and self._session['thread'].is_alive())

    def stop(self, id):
        with self._lock:
            session = self._get(id)
            session['stop'].set()
            processes = list(session['processes'])
        for process in processes:
            terminate_tree(process)
        session['thread'].join(timeout=10)
        return self.status(id)

    def _capture(self, session):
        folder, stop = session['folder'], session['stop']
        producer = consumer = relay = None
        try:
            env = self.process_env() if callable(self.process_env) else self.process_env
            context = self.command_factory(session['request'])
            if isinstance(context, (list, tuple)):
                context = nullcontext(context)
            with context as command, (folder / 'source.log').open('wb') as source_log, (folder / 'capture.log').open('wb') as capture_log:
                with self._lock:
                    if stop.is_set():
                        return
                    producer = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=source_log,
                        creationflags=CREATE_FLAGS, start_new_session=os.name != 'nt', env=env)
                    producer._keep_group = os.name != 'nt'
                    session['processes'].append(producer)
                    consumer = subprocess.Popen([self.ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error',
                        '-i', 'pipe:0', '-map', '0:v:0', '-map', '0:a:0?', '-sn', '-dn',
                        '-vf', "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,fps=30",
                        '-c:v', 'libx264', '-preset', 'veryfast', '-threads', '2', '-pix_fmt', 'yuv420p',
                        '-b:v', '2000k', '-maxrate', '3000k', '-bufsize', '6000k',
                        '-force_key_frames', 'expr:gte(t,n_forced*2)', '-c:a', 'aac', '-b:a', '128k',
                        '-t', '14400', '-f', 'segment', '-segment_time', '2', '-segment_wrap', '76', '-segment_format', 'mpegts',
                        str(folder / 'segment-%08d.ts')], stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL, stderr=capture_log, creationflags=CREATE_FLAGS,
                        start_new_session=os.name != 'nt', env=env)
                    consumer._keep_group = os.name != 'nt'
                    session['processes'].append(consumer)
                    # Both pipe ends belong to Keep. A forced app exit closes
                    # them, so capture cannot continue unattended in children.
                    def relay_stream():
                        try:
                            shutil.copyfileobj(producer.stdout, consumer.stdin, 32768)
                        except (OSError, ValueError):
                            pass
                        finally:
                            for stream in (consumer.stdin, producer.stdout):
                                try:
                                    stream.close()
                                except (OSError, ValueError):
                                    pass
                    relay = threading.Thread(target=relay_stream, daemon=True, name='Keep-live-pipe')
                    relay.start()
                while not stop.wait(.25):
                    self._refresh_segments(session)
                    now = time.monotonic()
                    if consumer.poll() is not None:
                        if consumer.returncode != 0 or producer.poll() not in (None, 0):
                            raise ValueError('A plataforma interrompeu a live ou não entregou um vídeo acessível. Confira o link e os cookies e tente novamente.')
                        break
                    if now - session['started'] > 4 * 3600:
                        raise ValueError('O acompanhamento atingiu quatro horas. Inicie novamente para continuar.')
                    if now - session['last_segment'] > 90:
                        raise ValueError('A live parou de entregar vídeo. Confira se está no ar e tente novamente.')
                    with self._lock:
                        if sum(p.stat().st_size for p in folder.iterdir() if p.is_file()) > 256 * MIB:
                            raise ValueError('O limite temporário da live foi atingido. Inicie o acompanhamento novamente.')
            with self._lock:
                session['status'] = 'stopped' if stop.is_set() else 'ended'
        except Exception as error:
            with self._lock:
                session['status'] = 'stopped' if stop.is_set() else 'failed'
                session['error'] = None if stop.is_set() else (str(error) if isinstance(error, ValueError) else 'Não foi possível acessar a live. Confira se ela está no ar e tente novamente.')
        finally:
            for process in (consumer, producer):
                if process is not None:
                    terminate_tree(process)
            if relay:
                relay.join(timeout=3)
            self._refresh_segments(session)
            with self._lock:
                if stop.is_set():
                    session['status'], session['error'] = 'stopped', None
                session['request'] = None  # Release cookie snapshots.
                session['processes'] = []

    def _refresh_segments(self, session):
        with self._lock:
            files = sorted(session['folder'].glob('segment-*.ts'), key=lambda p: (p.stat().st_mtime_ns, p.name))
            # Exclude the open/current segment, including after abrupt failure.
            closed = [p for p in files[:-1] if p.is_file() and not _is_link(p)]
            if closed and (not session['segments'] or closed[-1] != session['segments'][-1]):
                session['last_segment'] = time.monotonic()
            session['segments'] = closed[-60:]
            if session['segments'] and session['status'] == 'connecting':
                session['status'] = 'buffering'
            for path in closed[:-60]:
                path.unlink(missing_ok=True)

    def clip(self, id, seconds, consume):
        """Snapshot at click time, then call consume(path) with the finished MP4."""
        if isinstance(seconds, bool) or not isinstance(seconds, int) or seconds not in (15, 30, 60, 120):
            raise ValueError('Escolha um clipe de 15, 30, 60 ou 120 segundos.')
        if not self._clip_lock.acquire(blocking=False):
            raise ValueError('Aguarde o clipe atual ficar pronto antes de salvar outro.')
        try:
            with self._temporary(prefix='keep-clip-', dir=self.root) as temporary:
                folder = Path(temporary)
                joined = folder / 'recent.ts'
                with self._lock:
                    session = self._get(id)
                    self._refresh_segments(session)
                    segments = list(session['segments'])
                    if not segments:
                        raise ValueError('Aguarde alguns segundos para a live formar o primeiro trecho.')
                    segments = segments[-min(len(segments), math.ceil(seconds / 2) + 1):]
                    # Hold the lock while copying so rolling cleanup cannot
                    # remove a segment included in this click's snapshot.
                    with joined.open('wb') as target:
                        for path in segments:
                            with path.open('rb') as source:
                                shutil.copyfileobj(source, target)
                from media_editor import probe_media
                env = self.process_env() if callable(self.process_env) else self.process_env
                info = probe_media(self.ffmpeg, joined, env)
                duration = min(float(seconds), info['duration'])
                output = folder / 'clip.mp4'
                result = subprocess.run([self.ffmpeg, '-hide_banner', '-nostdin', '-loglevel', 'error', '-y',
                    '-ss', str(max(0, info['duration'] - duration)), '-i', str(joined), '-t', str(duration),
                    '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'libx264', '-preset', 'veryfast',
                    '-threads', '2', '-pix_fmt', 'yuv420p', '-crf', '20', '-c:a', 'aac', '-b:a', '128k',
                    '-movflags', '+faststart', str(output)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=120, creationflags=CREATE_FLAGS, env=env)
                if result.returncode or not output.is_file() or output.stat().st_size == 0:
                    raise ValueError('Não foi possível montar este trecho da live. Aguarde e tente outro clipe.')
                actual = probe_media(self.ffmpeg, output, env)
                if not actual.get('width') or actual['duration'] < .1:
                    raise ValueError('A live ainda não entregou um trecho válido para salvar.')
                return consume(output, actual)
        finally:
            self._clip_lock.release()

    def close(self):
        with self._lock:
            self._closed = True
            session = self._session
        if session:
            self.stop(session['id'])
            if not session['thread'].is_alive():
                session['temp'].cleanup()
