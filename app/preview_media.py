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
            now = time.monotonic()
            valid_duration = isinstance(duration, (int, float)) and not isinstance(duration, bool) and math.isfinite(duration) and duration > 0
            item = _Preview(id, key, folder, request, now, now, duration if valid_duration else None)
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
            context = self.command_factory(item.request, item.folder)
            if isinstance(context, (list, tuple)):
                context = nullcontext(context)
            with context as command:
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
            copy = within_height and bool(re.search(r'Video:\s*h264(?:\s|\()', video_line)) and bool(re.search(r',\s*yuv420p(?:\(|,|\s)', video_line)) and (
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
                source.unlink()
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
