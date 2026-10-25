"""Keep: a loopback-only video downloader. No third-party web framework required."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import importlib.util
import json
import math
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import subprocess
import sys
import threading
import tempfile
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote, parse_qs
from urllib.request import urlopen

from maintenance import export_preferences, import_preferences, release_notes
from media_editor import validate_edit, transform_command, probe_media, final_suffix, frame_aligned_edit
import desktop_integration as desktop

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DOWNLOADS = ROOT / 'downloads'
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.RLock()
JOBS = {}
PROCESSES = {}
PORT = 8765
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
VERSION = '2.0.1'
IS_WINDOWS = sys.platform == 'win32'
IS_LINUX = sys.platform.startswith('linux')
SETTINGS = {}
ACTIVE_WORKERS = set()
WORKER_THREADS = {}
QUEUED_COOKIES = {}
SHUTTING_DOWN = False
MAX_DOWNLOADS = 2
IMPORTED_COOKIES = {}
MAX_COOKIE_BYTES = 1024 * 1024
COOKIE_GENERATION = 0
INSPECTIONS = {}
BROWSERS = ('none', 'imported', 'firefox', 'chrome', 'edge', 'brave')
QUALITIES = ('best', '2160', '1080', '720', '480')
PREVIEWS = None
UPDATES = None
HTTP_SERVER = None

def preference_status():
    with LOCK:
        return {'paste_and_download': SETTINGS.get('paste_and_download', False),
                'default_browser': SETTINGS.get('default_browser', 'none'),
                'default_quality': SETTINGS.get('default_quality', '1080'),
                'platform_folders': SETTINGS.get('platform_folders', False),
                'notifications': SETTINGS.get('notifications', False)}

def download_root():
    with LOCK:
        return Path(SETTINGS.get('download_folder') or DOWNLOADS).resolve()

def folder_status():
    return {'folder': str(download_root()), 'default_folder': str(DOWNLOADS.resolve()), 'preferences': preference_status()}

def stored_path(path):
    path = path.resolve()
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)

def load_settings():
    try:
        saved = json.loads((DATA / 'settings.json').read_text(encoding='utf-8'))
        folder = saved.get('download_folder')
        if folder and (not isinstance(folder, str) or not Path(folder).is_absolute()):
            raise ValueError('Pasta inválida')
        with LOCK:
            SETTINGS.clear()
            SETTINGS.update({'download_folder': folder})
            SETTINGS.update({key: value for key, value in saved.items()
                if (key in ('paste_and_download', 'platform_folders', 'notifications') and isinstance(value, bool))
                or (key == 'default_browser' and value in BROWSERS)
                or (key == 'default_quality' and value in QUALITIES)})
    except (OSError, ValueError, AttributeError):
        with LOCK:
            SETTINGS.clear()

def save_download_folder(value):
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError('Informe um caminho de pasta válido.')
    value = value.strip()
    path = Path(value) if value else DOWNLOADS
    if not path.is_absolute():
        raise ValueError('Informe o caminho completo da pasta, como ' + ('C:\\Videos' if IS_WINDOWS else '/home/seu-usuario/Videos') + ', ou use Procurar pasta.')
    path = path.resolve()
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path):
            pass
        DATA.mkdir(exist_ok=True)
        with LOCK:
            settings = dict(SETTINGS, download_folder=str(path) if path != DOWNLOADS.resolve() else None)
            temporary = DATA / 'settings.tmp'
            temporary.write_text(json.dumps(settings, ensure_ascii=False), encoding='utf-8')
            temporary.replace(DATA / 'settings.json')
            SETTINGS.clear()
            SETTINGS.update(settings)
    except OSError as error:
        raise ValueError('Não foi possível gravar nessa pasta. Confira o caminho e a permissão de escrita.') from error
    return folder_status()

def save_preferences(values):
    if not isinstance(values, dict) or set(values) - {'paste_and_download', 'default_browser', 'default_quality', 'platform_folders', 'notifications'}:
        raise ValueError('Preferência inválida.')
    if (any(key in values and not isinstance(values[key], bool) for key in ('paste_and_download', 'platform_folders', 'notifications'))
            or 'default_browser' in values and values['default_browser'] not in BROWSERS
            or 'default_quality' in values and values['default_quality'] not in QUALITIES):
        raise ValueError('Escolha uma opção válida nas Preferências.')
    DATA.mkdir(exist_ok=True)
    with LOCK:
        settings = dict(SETTINGS, **values)
        temporary = DATA / 'settings.tmp'
        temporary.write_text(json.dumps(settings, ensure_ascii=False), encoding='utf-8')
        temporary.replace(DATA / 'settings.json')
        SETTINGS.clear()
        SETTINGS.update(settings)
    return preference_status()

def choose_download_folder():
    if IS_LINUX:
        if shutil.which('zenity'):
            command = ['zenity', '--file-selection', '--directory', '--title=Escolha a pasta dos downloads', '--filename=' + str(download_root()) + '/']
        elif shutil.which('kdialog'):
            command = ['kdialog', '--getexistingdirectory', str(download_root()), '--title', 'Escolha a pasta dos downloads']
        else:
            raise ValueError('A seleção visual precisa de Zenity ou KDialog. Você também pode digitar o caminho completo e clicar em Salvar pasta.')
        result = subprocess.run(command, capture_output=True, encoding='utf-8', timeout=300)
        if result.returncode == 1:
            return ''
        if result.returncode:
            raise ValueError('Não foi possível abrir a seleção de pasta. Digite o caminho e clique em Salvar pasta.')
        return result.stdout.strip()
    if not IS_WINDOWS:
        raise ValueError('Digite o caminho completo da pasta e clique em Salvar pasta.')
    script = ("Add-Type -AssemblyName System.Windows.Forms; "
              "$d=New-Object System.Windows.Forms.FolderBrowserDialog; "
              "$d.Description='Escolha onde o Keep deve salvar os novos downloads'; "
              "$d.SelectedPath=$env:KEEP_DOWNLOAD_FOLDER; "
              "if ($d.ShowDialog() -eq 'OK') { [Console]::OutputEncoding=[Text.Encoding]::UTF8; "
              "[Console]::Write($d.SelectedPath) }")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-STA', '-Command', script],
                            capture_output=True, encoding='utf-8', timeout=300,
                            env=dict(os.environ, KEEP_DOWNLOAD_FOLDER=str(download_root())), creationflags=CREATE_FLAGS)
    if result.returncode:
        raise ValueError('Não foi possível abrir a seleção de pasta. Digite o caminho e clique em Salvar pasta.')
    return result.stdout.strip().lstrip('\ufeff')

def create_download_directory(root, title, platform, created):
    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', ' ', title)
    safe_title = re.sub(r'\s+', ' ', safe_title).strip(' .')[:45].rstrip(' .') or 'Vídeo'
    prefix = datetime.fromtimestamp(created).strftime('%Y-%m-%d %H-%M-%S')
    name = f'{prefix} - {platform} - {safe_title}'
    try:
        root.mkdir(parents=True, exist_ok=True)
        for index in range(1000):
            folder = root / (name if not index else f'{name} ({index + 1})')
            try:
                folder.mkdir()
                return folder.resolve()
            except FileExistsError:
                continue
    except OSError as error:
        raise ValueError('Não foi possível criar a pasta do download. Escolha outra pasta nas Preferências.') from error
    raise ValueError('Não foi possível criar uma pasta para este download. Tente novamente.')

def validate_custom_name(value):
    if value is None or value == '':
        return None
    if (not isinstance(value, str) or len(value) > 160 or any(ord(c) < 32 for c in value)
            or any(c in value for c in '/\\%')):
        raise ValueError('Use um nome de arquivo sem caminhos, barras ou sinal de porcentagem.')
    value = re.sub(r'\.(?:mp4|mp3|gif|mkv|webm)$', '', value.strip(), flags=re.I)
    value = re.sub(r'[<>:"|?*\x7f]', ' ', value)
    value = re.sub(r'\s+', ' ', value).strip(' .')
    if not value:
        raise ValueError('Escolha um nome de arquivo com letras ou números.')
    if re.fullmatch(r'(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', value, re.I):
        value = '_' + value
    return value[:100].rstrip(' .')

def source_identity(url, video_id=None):
    url, platform = validate_url(url)
    parsed = urlparse(url)
    if platform == 'YouTube':
        identifier = (parse_qs(parsed.query).get('v', [''])[0] if parsed.path == '/watch'
                      else parsed.path.rstrip('/').rsplit('/', 1)[-1])
    elif platform == 'X':
        post = re.search(r'/status/(\d+)', parsed.path)
        identifier = (post.group(1) if post else '') + ':' + str(video_id or '')
    else:
        identifier = parsed.netloc.lower() + parsed.path.rstrip('/')
    return platform, identifier

def requested_edit(values, duration=None):
    value = values.get('edit')
    mode = values.get('mode', 'video')
    if value is None:
        if mode == 'gif':
            raise ValueError('Escolha um trecho de até 30 segundos para criar o GIF.')
        return None
    if not isinstance(value, dict):
        raise ValueError('Escolha os trechos e o enquadramento no editor.')
    request = dict(value, mode=mode)
    if duration is None:
        # Duplicate inspection only normalizes a comparison key. Download
        # creation always supplies the platform's independently fetched bound.
        sections = request.get('sections')
        if not isinstance(sections, list) or not sections:
            raise ValueError('Selecione os trechos da edição.')
        ends = [section.get('end') for section in sections if isinstance(section, dict)]
        if len(ends) != len(sections) or any(isinstance(end, bool) or not isinstance(end, (int, float)) or not math.isfinite(end) for end in ends):
            raise ValueError('Escolha horários válidos para os trechos.')
        duration = max(ends)
    return validate_edit(request, duration)

def edit_bounds(edit):
    return {'start': min(section['start'] for section in edit['sections']),
            'end': max(section['end'] for section in edit['sections'])}

def duplicate_matches(values):
    url, platform = validate_url(values.get('url'))
    video_id = values.get('video_id')
    if video_id is not None and (not isinstance(video_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', video_id)):
        raise ValueError('Identificação de vídeo inválida.')
    mode, quality = values.get('mode', 'video'), values.get('quality', '1080')
    if mode not in ('video', 'audio', 'gif') or quality not in QUALITIES:
        raise ValueError('Escolha um formato e uma qualidade válidos.')
    edit = requested_edit(values)
    section = edit_bounds(edit) if edit else validate_section(values.get('section'))
    identity = source_identity(url, video_id)
    with LOCK:
        matches = []
        for job in JOBS.values():
            try:
                if (job.get('status') != 'complete' or job.get('mode', 'video') != mode
                        or job.get('quality', '1080') != quality or job.get('section') != section
                        or job.get('edit') != edit
                        or source_identity(job.get('url'), job.get('video_id')) != identity
                        or completed_file_status(job['id'])['file_missing']):
                    continue
                matches.append({key: job.get(key) for key in ('id', 'title', 'filename', 'created')})
            except (OSError, ValueError, TypeError, KeyError):
                continue
    return matches

def reorder_queue(ids):
    with LOCK:
        queued = {id for id, job in JOBS.items() if job.get('status') == 'queued'}
        if (not isinstance(ids, list) or not all(isinstance(id, str) for id in ids)
                or len(ids) != len(set(ids)) or set(ids) != queued):
            raise ValueError('A fila mudou. Atualize a lista e tente ordenar novamente.')
        # Place the reordered pending items before any subsequently added job.
        order = time.time() - len(ids)
        for index, id in enumerate(ids):
            JOBS[id]['queue_order'] = order + index
        persist()
    return {'ok': True}

def storage_status():
    seen = set()
    total, count, jobs_with_files = 0, 0, 0
    extensions = {'.mp4', '.mp3', '.gif', '.mkv', '.webm', '.m4a', '.aac', '.opus', '.ts', '.wav', '.ogg', '.flv', '.ytdl', '.tmp', '.temp'}
    with LOCK:
        for id in JOBS:
            try:
                folder = checked_job_directory(id)
                if any(is_link(path) for path in (folder, *folder.parents)) or not folder.is_dir():
                    continue
                found = False
                for path in folder.iterdir():
                    if is_link(path) or not path.is_file() or path.name in ('.keep-job.json', 'result.jsonl'):
                        continue
                    if path.suffix.lower() not in extensions and not re.search(r'\.part(?:-|$)', path.name):
                        continue
                    details = path.stat()
                    key = (details.st_dev, details.st_ino) if details.st_ino else str(path.resolve())
                    found = True
                    if key not in seen:
                        seen.add(key)
                        total += details.st_size
                        count += 1
                jobs_with_files += int(found)
            except (OSError, ValueError, KeyError, TypeError):
                continue
    return {'total_bytes': total, 'file_count': count, 'download_count': jobs_with_files}

def notify_completed_job(id):
    """Attempt one native notice per completed job, without delaying its worker."""
    try:
        with LOCK:
            job = JOBS.get(id)
            if (not SETTINGS.get('notifications', False) or not job or job.get('status') != 'complete'
                    or job.get('notification_attempted')):
                return False
            job['notification_attempted'] = True
            persist()
            title = str(job.get('title') or 'Vídeo')
        def deliver():
            try:
                desktop.notify(ROOT, 'Keep', 'Download concluído: ' + title)
            except Exception:
                pass  # A missing native notification service never changes the download.
        threading.Thread(target=deliver, daemon=True).start()
        return True
    except Exception:
        return False


def record_latest_download(job, folder):
    # Called after successful completion under LOCK, so concurrent downloads
    # always leave the last completed item in this small, readable index.
    marker = folder.parent / 'ULTIMO DOWNLOAD.txt'
    try:
        content = ('ÚLTIMO DOWNLOAD CONCLUÍDO NO KEEP\n\n'
                   + 'Concluído em: ' + datetime.now().strftime('%d/%m/%Y %H:%M:%S') + '\n'
                   + 'Vídeo: ' + job.get('title', 'Vídeo') + '\n'
                   + 'Pasta: ' + str(folder) + '\n'
                   + 'Arquivo: ' + job.get('filename', '') + '\n')
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=folder.parent,
                                         prefix='.keep-ultimo-', suffix='.tmp', delete=False) as temporary:
            path = Path(temporary.name)
            temporary.write(content)
        try:
            path.replace(marker)
        finally:
            path.unlink(missing_ok=True)
    except OSError:
        pass  # The media remains available even if a text editor locks the index.

def job_file(job):
    filename = (ROOT / job.get('file', 'missing')).resolve()
    directory = (ROOT / job['directory']).resolve() if job.get('directory') else DOWNLOADS.resolve()
    if not filename.is_relative_to(directory) or not filename.is_file():
        return None
    return filename

def parse_cookie_file(content):
    """Accept Netscape exports; retain only supported platform domains, never log values."""
    if not isinstance(content, str) or len(content.encode('utf-8')) > MAX_COOKIE_BYTES:
        raise ValueError('Escolha um arquivo cookies.txt de até 1 MB.')
    lines = content.lstrip('\ufeff').splitlines()
    if not lines or lines[0].strip() not in ('# Netscape HTTP Cookie File', '# HTTP Cookie File'):
        raise ValueError('Formato inválido. Exporte cookies.txt no formato Netscape; não use JSON ou uma página HTML.')
    grouped = {}
    counts = {}
    ignored = 0
    for line in lines[1:]:
        if not line.strip() or (line.startswith('#') and not line.startswith('#HttpOnly_')):
            continue
        fields = line.removeprefix('#HttpOnly_').split('\t')
        if len(fields) != 7:
            raise ValueError('O arquivo tem uma linha inválida. Exporte os cookies novamente no formato Netscape.')
        domain, include_subdomains, path, secure, expires, name, value = fields
        if (not domain or include_subdomains not in ('TRUE', 'FALSE') or secure not in ('TRUE', 'FALSE')
                or not path.startswith('/') or not name or (expires != '' and not expires.isdigit())
                or domain.startswith('.') != (include_subdomains == 'TRUE')
                or any(ord(c) < 32 or ord(c) == 127 for field in fields for c in field)):
            raise ValueError('O arquivo contém cookies inválidos. Exporte os cookies novamente.')
        host = domain.lstrip('.').lower()
        if not re.fullmatch(r'[a-z0-9-]+(?:\.[a-z0-9-]+)*', host):
            raise ValueError('O arquivo contém um domínio inválido. Exporte os cookies novamente.')
        platform = next((label for suffix, label in [('youtube.com', 'YouTube'), ('x.com', 'X'), ('twitter.com', 'X'), ('twitch.tv', 'Twitch')]
                         if host == suffix or host.endswith('.' + suffix)), None)
        if platform is None or (expires and int(expires) and int(expires) <= time.time()):
            ignored += 1
            continue
        fields[4] = expires or '0'
        grouped.setdefault(platform, []).append(('#HttpOnly_' if line.startswith('#HttpOnly_') else '') + '\t'.join(fields))
        counts[platform] = counts.get(platform, 0) + 1
    if not grouped:
        raise ValueError('Nenhum cookie válido do YouTube, X ou Twitch encontrado. Abra o site e exporte os cookies novamente; o arquivo pode estar vencido.')
    return {key: '# Netscape HTTP Cookie File\n' + '\n'.join(rows) + '\n' for key, rows in grouped.items()}, counts, ignored

def cookie_status():
    with LOCK:
        return {'platforms': sorted(IMPORTED_COOKIES)}

def imported_cookie_text(browser, url):
    if browser != 'imported':
        session_arguments(browser)
        return None
    _, platform = validate_url(url)
    with LOCK:
        content = IMPORTED_COOKIES.get(platform)
    if content is None:
        raise ValueError(f'Importe um arquivo de cookies do {platform} antes de buscar ou baixar. Os cookies importados são esquecidos ao fechar o Keep.')
    return content

@contextmanager
def cookie_options(browser, url, snapshot=None):
    if browser != 'imported':
        yield session_arguments(browser)
        return
    _, platform = validate_url(url)
    content = snapshot if snapshot is not None else imported_cookie_text(browser, url)
    valid, _, _ = parse_cookie_file(content)
    if platform not in valid:
        raise ValueError(f'Importe cookies válidos do {platform}.')
    # yt-dlp needs a path and may update the jar. Each operation gets a private
    # temporary copy; cleanup runs on success, failure, cancellation and timeout.
    with tempfile.TemporaryDirectory(prefix='keep-session-') as folder:
        path = Path(folder) / 'cookies.txt'
        path.write_text(valid[platform], encoding='utf-8')
        yield ['--cookies', str(path)]

def tool_path(name):
    bundled = ROOT / 'tools' / (name + ('.exe' if IS_WINDOWS else ''))
    return str(bundled) if bundled.is_file() else shutil.which(name)

def is_link(path):
    # Path.is_junction was introduced in Python 3.12; Linux supports 3.10+.
    return path.is_symlink() or getattr(path, 'is_junction', lambda: False)()

def process_env():
    return dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')

def validate_url(value):
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError('Cole o link de um vídeo para continuar.')
    u = urlparse(value.strip())
    if u.scheme not in ('http', 'https') or u.username or u.password or u.port not in (None, 80, 443):
        raise ValueError('Use um link HTTPS válido.')
    host = (u.hostname or '').lower()
    if host in ('youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'):
        valid = u.path == '/watch' and bool(re.search(r'(?:^|&)v=[\w-]+(?:&|$)', u.query)) or bool(re.fullmatch(r'/(?:shorts|live|embed)/[\w-]+/?', u.path))
        platform = 'YouTube'
    elif host == 'youtu.be':
        valid, platform = bool(re.fullmatch(r'/[\w-]+/?', u.path)), 'YouTube'
    elif host in ('x.com', 'www.x.com', 'twitter.com', 'www.twitter.com', 'mobile.twitter.com'):
        valid, platform = bool(re.fullmatch(r'/(?:[\w]+/status|i/status|i/web/status)/\d+(?:/video/[1-9]\d?)?/?', u.path)), 'X'
    elif host in ('twitch.tv', 'www.twitch.tv', 'm.twitch.tv', 'go.twitch.tv', 'clips.twitch.tv', 'player.twitch.tv'):
        # Normalize known video/clip forms; never accept channel live streams or collections.
        clip = None
        vod = None
        if host == 'clips.twitch.tv':
            if u.path.rstrip('/') == '/embed':
                slug = parse_qs(u.query).get('clip', [''])[0]
                clip = re.fullmatch(r'[A-Za-z0-9_-]+', slug)
            else:
                clip = re.fullmatch(r'/([A-Za-z0-9_-]+)/?', u.path)
                slug = clip.group(1) if clip else ''
        elif host == 'player.twitch.tv':
            candidate = parse_qs(u.query).get('video', [''])[0]
            vod = re.fullmatch(r'v?([0-9]+)', candidate) if u.path in ('', '/') else None
        else:
            vod = re.fullmatch(r'/(?:videos|[A-Za-z0-9_]+/v(?:ideo)?)/([0-9]+)/?', u.path)
            clip = re.fullmatch(r'/[A-Za-z0-9_]+/clip/([A-Za-z0-9_-]+)/?', u.path)
            slug = clip.group(1) if clip else ''
        if vod:
            return 'https://www.twitch.tv/videos/' + vod.group(1), 'Twitch'
        if clip:
            return 'https://clips.twitch.tv/' + slug, 'Twitch'
        raise ValueError('Na Twitch, cole o link de um clipe ou VOD, como twitch.tv/videos/123. Links de canal e transmissões ao vivo não são aceitos.')
    else:
        raise ValueError('Escolha um vídeo do YouTube ou X, ou um clipe/VOD da Twitch.')
    if not valid:
        raise ValueError('Esse link parece ser de um perfil ou página. Cole o link de um vídeo específico.')
    return value.strip(), platform

def base_command():
    engine = tool_path('yt-dlp')
    if engine and not IS_WINDOWS and Path(engine) == ROOT / 'tools' / 'yt-dlp':
        command = [sys.executable, engine]  # Official universal Python zipapp.
    else:
        command = [engine] if engine else [sys.executable, '-m', 'yt_dlp']
    command += ['--ignore-config', '--encoding', 'utf-8', '--no-playlist', '--playlist-items', '1', '--no-colors', '--socket-timeout', '20', '--retries', '2', '--restrict-filenames', '--match-filter', '!is_live']
    if tool_path('node'):
        command += ['--js-runtimes', 'node:' + tool_path('node')]
    if tool_path('ffmpeg'):
        command += ['--ffmpeg-location', tool_path('ffmpeg')]
    return command

def video_command(video_index=1, video_id=None, all_videos=False):
    if isinstance(video_index, bool) or not isinstance(video_index, int) or not 1 <= video_index <= 32:
        raise ValueError('Escolha um vídeo válido deste post.')
    command = base_command()
    position = command.index('--playlist-items')
    command[position + 1] = '1-32' if all_videos else str(video_index)
    if all_videos:
        command.remove('--no-playlist')
        command.append('--yes-playlist')
    if video_id:
        if not isinstance(video_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', video_id):
            raise ValueError('Identificação de vídeo inválida. Busque o post novamente.')
        command[command.index('--match-filter') + 1] = f"!is_live & id='{video_id}'"
    return command

def source_arguments(url):
    if validate_url(url)[1] == 'Twitch':
        return ['--abort-on-unavailable-fragments', '--fragment-retries', '5']
    return []

def session_arguments(browser):
    if browser not in ('none', 'firefox', 'chrome', 'edge', 'brave'):
        raise ValueError('Selecione uma opção de sessão válida.')
    return [] if browser == 'none' else ['--cookies-from-browser', browser]

DIAGNOSTIC_ACTIONS = {
    'cookies': 'Escolha outra opção de cookies ou importe um arquivo válido e tente novamente.',
    'authentication': 'Abra o vídeo no navegador e confira a opção de cookies selecionada no Keep.',
    'restricted': 'Confira se o vídeo ainda existe e se sua conta tem permissão para acessá-lo.',
    'format': 'Escolha outra qualidade ou Melhor disponível e tente novamente.',
    'processing': 'Confira o intervalo escolhido e atualize os componentes do Keep antes de tentar novamente.',
    'storage': 'Confira o disco, o espaço livre e a permissão da pasta de destino.',
    'network': 'Confira sua conexão; se a plataforma estiver limitando acessos, aguarde antes de tentar novamente.',
    'platform': 'Abra o link no navegador e atualize o mecanismo do Keep se o problema continuar.',
    'unknown': 'Tente novamente e, se continuar, atualize o mecanismo ou copie o relatório para suporte.',
}

def safe_diagnostic(text, exit_code=None):
    """Classify in memory; never retain raw engine logs, URLs or credentials."""
    lower = str(text).lower()
    if any(term in lower for term in ('cookie', 'dpapi', 'decrypt', 'database is locked')):
        category = 'cookies'
    elif any(term in lower for term in ('sign in', 'login', 'not a bot', 'confirm you', 'antirrobô', 'autenticad')):
        category = 'authentication'
    elif any(term in lower for term in ('private', 'privado', 'age-restricted', 'subscriber-only', 'assinantes', '403', '404', 'removed', 'expirado')):
        category = 'restricted'
    elif any(term in lower for term in ('requested format', 'qualidade não', 'formato não')):
        category = 'format'
    elif any(term in lower for term in ('ffmpeg', 'postprocess', 'conversion', 'conversão', 'duração escolhida', 'conferir a duração')):
        category = 'processing'
    elif any(term in lower for term in ('no space left', 'disk full', 'permission denied', 'access is denied', 'pasta do download não', 'permissão')):
        category = 'storage'
    elif (any(term in lower for term in ('timed out', 'timeout', 'connection reset', 'connection aborted', 'connection refused',
            'network is unreachable', 'temporary failure', 'remote end closed', 'conexão', 'http error 429', 'http 429', 'rate-limit'))
          or re.search(r'\b(?:http(?: error)?|status(?: code)?)\s*[:=]?\s*5\d\d\b', lower)):
        category = 'network'
    elif any(term in lower for term in ('unavailable', 'not available', 'not found', 'failed to download fragment')):
        category = 'platform'
    else:
        category = 'unknown'
    result = {'category': category, 'action': DIAGNOSTIC_ACTIONS[category], 'retryable': category == 'network'}
    if isinstance(exit_code, int) and not isinstance(exit_code, bool) and -255 <= exit_code <= 255:
        result['exit_code'] = exit_code
    return result

def diagnostic_report(id):
    with LOCK:
        if not isinstance(id, str) or id not in JOBS:
            raise ValueError('Download não encontrado.')
        job = JOBS[id]
        diagnostic = job.get('diagnostic') if isinstance(job.get('diagnostic'), dict) else {}
        category = diagnostic.get('category')
        if category not in DIAGNOSTIC_ACTIONS:
            category = safe_diagnostic(job.get('error') or '')['category']
        report = {'version': VERSION,
            'platform': job.get('platform') if job.get('platform') in ('YouTube', 'X', 'Twitch') else 'desconhecida',
            'system': 'Windows' if IS_WINDOWS else 'Linux' if IS_LINUX else 'outro',
            'status': job.get('status') if job.get('status') in ('queued', 'downloading', 'complete', 'paused', 'failed', 'cancelled') else 'desconhecido',
            'mode': job.get('mode') if job.get('mode') in ('video', 'audio', 'gif') else 'desconhecido',
            'quality': job.get('quality') if job.get('quality') in QUALITIES else 'desconhecida',
            'category': category, 'action': DIAGNOSTIC_ACTIONS[category]}
        code = diagnostic.get('exit_code')
        if isinstance(code, int) and not isinstance(code, bool) and -255 <= code <= 255:
            report['exit_code'] = code
        try:
            section = validate_section(job.get('section'))
            if section:
                report['section'] = section
        except (ValueError, TypeError):
            pass
    return {'text': 'KEEP — RELATÓRIO DE DIAGNÓSTICO\n' + json.dumps(report, ensure_ascii=False, indent=2)}

def fail_download(id, text, exit_code=None):
    diagnostic = safe_diagnostic(text, exit_code)
    if JOBS[id].get('phase') == 'processing':
        diagnostic['retryable'] = False
    update_job(id, status='failed', error=friendly_error(str(text), JOBS[id].get('platform')), diagnostic=diagnostic)

def friendly_error(text, platform=None):
    clean = re.sub(r'\x1b\[[0-9;]*m', '', text)
    lower = clean.lower()
    if 'dpapi' in lower or 'decrypt' in lower:
        return 'O mecanismo não conseguiu descriptografar os cookies desse navegador. Escolha Importar arquivo de cookies ou use os cookies do Firefox. No Linux, o chaveiro da sessão também precisa estar desbloqueado.'
    if 'could not copy chrome' in lower or 'database is locked' in lower:
        return 'O arquivo de cookies está bloqueado. Abra o Keep em outro navegador e feche completamente o navegador selecionado, ou escolha Importar arquivo de cookies.'
    if any(s in lower for s in ['cookie database', 'cookies database', 'could not find firefox', 'could not find chrome', 'could not find edge']):
        return 'Não foi possível encontrar ou ler os cookies desse navegador. Abra o site nele primeiro ou escolha Importar arquivo de cookies.'
    if platform == 'Twitch' or 'twitch' in lower:
        if any(s in lower for s in ['subscriber-only', 'subscribers only', 'vod_manifest_restricted', 'unauthorized_entitlements']):
            return 'Este conteúdo da Twitch é restrito a assinantes. Use cookies de uma conta que já tenha acesso ao vídeo.'
        if any(s in lower for s in ['does not exist', 'not found', 'no longer available', 'unavailable', '404']):
            return 'A Twitch não disponibilizou este clipe ou VOD. Ele pode ter expirado, sido removido ou ter acesso restrito. Confira se o link abre no navegador.'
        if 'fragment' in lower and any(s in lower for s in ['failed', 'unable', 'not found', 'unavailable']):
            return 'Um trecho do vídeo não pôde ser baixado. O Keep interrompeu o download para não marcar um arquivo incompleto como pronto. Tente novamente mais tarde.'
    if "not a bot" in lower or 'confirm you' in lower:
        return 'O YouTube bloqueou esta tentativa com uma verificação antirrobô. Isso também acontece com vídeos públicos. Abra o vídeo no navegador e, abaixo do link, escolha usar a sessão desse navegador. Se persistir, aguarde e tente mais tarde.'
    if '429' in clean or 'rate-limit' in clean.lower():
        return 'A plataforma limitou temporariamente as solicitações (HTTP 429). Aguarde alguns minutos antes de tentar novamente.'
    if any(s in lower for s in ['sign in', 'login', 'cookies']):
        return 'A plataforma solicitou uma sessão autenticada. Abra o vídeo no navegador, entre na sua conta e escolha esse navegador na opção de sessão do Keep.'
    if 'private' in lower:
        return 'A plataforma informou que o vídeo é privado. Use apenas uma conta que tenha acesso a ele.'
    if 'age-restricted' in lower:
        return 'A plataforma exige uma conta com a idade verificada para acessar este vídeo.'
    if 'requested format' in lower:
        return 'Essa qualidade não está disponível. Tente Melhor disponível ou outra resolução.'
    diagnostic = safe_diagnostic(clean)
    if diagnostic['category'] == 'network':
        return 'A conexão com a plataforma foi interrompida temporariamente. ' + diagnostic['action']
    if 'no video' in lower or 'not available' in lower or 'unavailable' in lower:
        return 'A plataforma não disponibilizou o vídeo. Confira o link ou tente usar sua sessão do navegador.'
    if diagnostic['category'] == 'storage':
        return 'Não foi possível gravar o arquivo. ' + diagnostic['action']
    if diagnostic['category'] == 'processing':
        return 'Não foi possível finalizar ou conferir o arquivo. ' + diagnostic['action']
    return 'A plataforma não forneceu o vídeo. ' + diagnostic['action']

def metadata(url, browser='none', cookie_snapshot=None):
    url, platform = validate_url(url)
    with cookie_options(browser, url, cookie_snapshot) as options:
        result = subprocess.run((video_command(all_videos=True) if platform == 'X' else base_command()) + options + source_arguments(url) + ['--dump-single-json', '--skip-download', '--', url], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=100, creationflags=CREATE_FLAGS, env=process_env())
    if result.returncode:
        raise ValueError(friendly_error(result.stderr, platform))
    info = json.loads(result.stdout)
    entries = info.get('entries') if isinstance(info.get('entries'), list) else [info]
    items = []
    kind = ('Clipe' if urlparse(url).hostname == 'clips.twitch.tv' else 'VOD') if platform == 'Twitch' else None
    for index, entry in enumerate(entries[:32], 1):
        if not isinstance(entry, dict) or not entry.get('formats'):
            continue
        video_formats = [item for item in entry['formats'] if isinstance(item, dict) and item.get('vcodec') != 'none'
                         and isinstance(item.get('height'), (int, float)) and not isinstance(item.get('height'), bool)]
        largest = max(video_formats, key=lambda item: item.get('height', 0), default={})
        def dimension(key):
            value = entry.get(key) or largest.get(key)
            return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and 0 < value <= 32768 else None
        fps = entry.get('fps') or largest.get('fps')
        fps = float(fps) if isinstance(fps, (int, float)) and not isinstance(fps, bool) and math.isfinite(fps) and 0 < fps <= 240 else None
        items.append({'url': url, 'platform': platform, 'kind': kind,
            'video_index': entry.get('playlist_index') or index, 'video_id': str(entry.get('id') or ''),
            'title': entry.get('title') or 'Vídeo sem título',
            'creator': entry.get('uploader') or entry.get('channel') or platform,
            'duration': entry.get('duration'), 'thumbnail': entry.get('thumbnail'),
            'width': dimension('width'), 'height': dimension('height'), 'fps': fps,
            'heights': sorted({f['height'] for f in entry.get('formats', []) if f.get('height') and f.get('vcodec') != 'none' and f.get('ext') != 'mhtml'}, reverse=True),
            'live': entry.get('is_live', False)})
    if not items:
        raise ValueError('Nenhum vídeo disponível foi encontrado neste post.')
    inspection_id = secrets.token_urlsafe(24)
    with LOCK:
        now = time.monotonic()
        for key in list(INSPECTIONS):
            if now - INSPECTIONS[key]['created'] > 600:
                INSPECTIONS.pop(key)
        while len(INSPECTIONS) >= 32:
            INSPECTIONS.pop(next(iter(INSPECTIONS)))
        INSPECTIONS[inspection_id] = {'created': now, 'url': url, 'browser': browser,
            'generation': COOKIE_GENERATION, 'items': items}
    return dict(items[0], items=items, inspection_id=inspection_id)

def selected_metadata(body, url, browser, cookie_snapshot=None):
    if body.get('inspection_id') is not None and not isinstance(body['inspection_id'], str):
        raise ValueError('Busque o vídeo novamente para confirmar a seleção.')
    with LOCK:
        cached = INSPECTIONS.get(body.get('inspection_id', ''))
        if (cached and (cached['url'] != url or cached['browser'] != browser
                or cached['generation'] != COOKIE_GENERATION or time.monotonic() - cached['created'] > 600)):
            cached = None
        items = list(cached['items']) if cached else None
    if items is None:
        info = metadata(url, browser, cookie_snapshot)
        items = info.get('items') or [dict(info, video_index=1, video_id=info.get('video_id', ''))]
    index = body.get('video_index')
    if index is None:
        if len(items) > 1:
            raise ValueError('Escolha quais vídeos deste post você quer baixar.')
        index = items[0].get('video_index', 1)
    if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= 32:
        raise ValueError('Escolha um vídeo válido deste post.')
    selected = next((item for item in items if item.get('video_index', 1) == index), None)
    if not selected or body.get('video_id') is not None and body['video_id'] != selected.get('video_id'):
        raise ValueError('O vídeo escolhido mudou ou não está disponível. Busque o post novamente.')
    return dict(selected)

def persist():
    DATA.mkdir(exist_ok=True)
    temp = DATA / 'history.tmp'
    temp.write_text(json.dumps(JOBS, ensure_ascii=False), encoding='utf-8')
    temp.replace(DATA / 'history.json')

def update_job(id, **values):
    with LOCK:
        JOBS[id].update(values)
        if values.get('status') in ('complete', 'failed', 'cancelled', 'queued', 'paused'):
            persist()

def format_arguments(mode, quality, has_ffmpeg):
    if mode == 'audio':
        if not has_ffmpeg:
            raise ValueError('O MP3 precisa do FFmpeg. ' + ('Instale-o conforme LEIA-ME-LINUX.txt e reabra o Keep.' if IS_LINUX else 'Abra Iniciar Keep.bat para preparar os componentes.'))
        return ['-f', 'bestaudio/best', '-x', '--audio-format', 'mp3', '--audio-quality', '0']
    cap = '' if quality == 'best' else '[height<=' + quality + ']'
    if has_ffmpeg:
        return ['-f', f'bv*{cap}+ba/b{cap}', '--merge-output-format', 'mkv', '--remux-video', 'mp4']
    return ['-f', f'b[ext=mp4]{cap}']

def validate_section(section, duration=None):
    if section is None:
        return None
    if not isinstance(section, dict) or set(section) != {'start', 'end'}:
        raise ValueError('Informe o início e o fim do trecho.')
    start, end = section['start'], section['end']
    if any(isinstance(value, bool) or not isinstance(value, (int, float))
           or not math.isfinite(value) or value < 0 or value > 2147483647
           for value in (start, end)):
        raise ValueError('Informe horários válidos para o início e o fim do trecho.')
    if end <= start:
        raise ValueError('O fim do trecho precisa ser depois do início.')
    if round(end, 3) <= round(start, 3):
        raise ValueError('Escolha um trecho de pelo menos um milésimo de segundo.')
    if duration is not None:
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
            raise ValueError('A plataforma não informou a duração. Baixe o vídeo inteiro.')
        if start >= duration or end > duration + 0.001:
            raise ValueError('O trecho precisa estar dentro da duração do vídeo. Busque o vídeo novamente e confira os horários.')
    return {'start': round(start, 3), 'end': round(end, 3)}

def section_arguments(section, has_ffmpeg, platform=None):
    if section is None:
        return []
    if not has_ffmpeg:
        raise ValueError('O corte precisa do FFmpeg. ' + ('Instale-o conforme LEIA-ME-LINUX.txt e reabra o Keep.' if IS_LINUX else 'Abra Iniciar Keep.bat para preparar os componentes.'))
    options = ['--download-sections', f"*{section['start']:.3f}-{section['end']:.3f}", '--force-keyframes-at-cuts']
    # X's HLS video and audio can seek to different timestamps. Prefer the
    # equivalent direct MP4 at the selected resolution when cutting.
    if platform == 'X':
        options += ['-S', 'res,proto:https']
    return options

def duration_seconds(text):
    match = re.search(r'Duration: (\d+):(\d+):(\d+\.\d+)', text)
    if match:
        hours, minutes, seconds = map(float, match.groups())
        value = hours * 3600 + minutes * 60 + seconds
        return value if math.isfinite(value) else None
    return None

class InputDurationProbe:
    """Read source durations from FFmpeg inputs, never its output/progress clock."""
    def __init__(self):
        self.in_input = False
        self.durations = []

    def feed(self, line):
        text = line.strip()
        if re.match(r'Input #\d+,', text):
            self.in_input = True
        elif text.startswith(('Output #', 'Stream mapping:')):
            self.in_input = False
        elif self.in_input and text.startswith('Duration:'):
            duration = duration_seconds(text)
            if duration is not None and duration > 0:
                self.durations.append(duration)
            self.in_input = False

def effective_section(job, input_durations):
    section = job.get('section')
    if not section:
        return None
    effective = dict(section)
    reported = job.get('source_duration')
    # YouTube lengthSeconds is rounded, while FFmpeg reads the media clock.
    # Only adjust an explicitly selected end-of-video, with independent source
    # evidence for a sub-second rounding difference. Keep all other cuts strict.
    if (job.get('platform') == 'YouTube' and isinstance(reported, (int, float))
            and not isinstance(reported, bool) and math.isfinite(reported)
            and abs(section['end'] - reported) <= 0.001 and input_durations):
        source_end = max(input_durations)
        if 0 < reported - source_end < 1:
            if section['start'] >= source_end:
                raise ValueError('O início escolhido fica depois do fim real do vídeo. Escolha um início anterior.')
            effective['end'] = round(source_end, 3)
    return effective

def verify_section_output(path, section):
    result = subprocess.run([tool_path('ffmpeg'), '-hide_banner', '-i', str(path)],
                            capture_output=True, text=True, encoding='utf-8', errors='replace',
                            timeout=30, creationflags=CREATE_FLAGS, env=process_env())
    actual = duration_seconds(result.stderr)
    if actual is None:
        raise ValueError('Não foi possível conferir a duração do trecho. Tente novamente ou atualize o mecanismo.')
    if abs(actual - (section['end'] - section['start'])) > 0.25:
        raise ValueError('A plataforma não entregou o trecho com a duração escolhida. O arquivo não foi marcado como pronto. Tente novamente ou atualize o mecanismo.')

def job_directory(job):
    return (ROOT / job['directory'] if job.get('directory') else DOWNLOADS / job['id']).absolute()

def checked_job_directory(id):
    job = JOBS[id]
    folder = job_directory(job)
    if folder in (ROOT, DATA, DOWNLOADS) or is_link(folder):
        raise ValueError('A pasta deste download não é uma pasta exclusiva segura do Keep.')
    root = (ROOT / job['managed_root']).absolute() if job.get('managed_root') else folder.parent
    if folder.parent.resolve() != root.resolve() or folder.resolve().parent != root.resolve():
        raise ValueError('A pasta do download está fora do destino original.')
    if any(is_link(p) for p in [folder, *folder.parents] if p != root and p.is_relative_to(root)):
        raise ValueError('A pasta contém um vínculo. Os parciais foram preservados por segurança.')
    marker = folder / '.keep-job.json'
    if marker.exists():
        if marker.is_symlink() or json.loads(marker.read_text(encoding='utf-8')).get('id') != id:
            raise ValueError('A identificação da pasta não corresponde ao download.')
    elif not ((folder.parent.resolve() == DOWNLOADS.resolve() and folder.name == id)
              or re.match(r'^\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2} - (YouTube|X|Twitch) - ', folder.name)):
        raise ValueError('Não foi possível confirmar que esta pasta pertence ao download.')
    return folder

def checked_completed_file(id):
    """Return only a regular final file directly owned by this completed job."""
    job = JOBS[id]
    if job.get('status') != 'complete' or not isinstance(job.get('file'), str):
        return None
    folder = checked_job_directory(id)
    filename = (ROOT / job['file']).absolute()
    # Check the lexical path before resolving: a link must never authorize a
    # destructive action, even if it currently points back into the same folder.
    if filename.parent != folder or filename.name in ('.keep-job.json', 'result.jsonl'):
        raise ValueError('O arquivo final não está diretamente na pasta deste download.')
    if any(is_link(path) for path in (filename, *filename.parents)):
        raise ValueError('O caminho contém um vínculo. O arquivo foi preservado por segurança.')
    if filename.resolve().parent != folder.resolve():
        raise ValueError('O arquivo está fora da pasta deste download.')
    return filename if filename.is_file() else None

def completed_file_status(id):
    try:
        filename = checked_completed_file(id)
        return {'file_missing': filename is None, 'file_bytes': filename.stat().st_size if filename else 0}
    except (OSError, ValueError, KeyError, TypeError):
        return {'file_missing': True, 'file_bytes': 0}

def delete_completed_file(id, confirmed=False):
    if confirmed is not True:
        raise ValueError('Confirme que deseja apagar o arquivo do computador.')
    with LOCK:
        if not isinstance(id, str) or id not in JOBS:
            raise ValueError('Download não encontrado.')
        job = JOBS[id]
        if (job.get('status') != 'complete' or id in ACTIVE_WORKERS
                or id in PROCESSES and PROCESSES[id].poll() is None):
            raise ValueError('Só é possível excluir o arquivo de um download concluído e parado.')
        filename = checked_completed_file(id)
        if filename is None:
            job['file_missing'] = True
            persist()
            return {'ok': True, 'deleted': False}
        try:
            filename.unlink()
        except OSError as error:
            raise ValueError('Não foi possível excluir o arquivo. Feche programas que o estejam usando e tente novamente.') from error
        job.update(file_deleted=True, file_missing=True, file_deleted_at=time.time())
        persist()
        return {'ok': True, 'deleted': True}

def partial_size(job):
    folder = job_directory(job)
    if not folder.is_dir() or is_link(folder):
        return 0
    total = 0
    for path in folder.iterdir():
        try:
            if path.is_file() and not path.is_symlink() and path.name not in ('.keep-job.json','result.jsonl'):
                total += path.stat().st_size
        except OSError:
            pass
    return total

def cleanup_job_files(id):
    with LOCK:
        job = JOBS[id]
        if job['status'] in ('complete', 'downloading', 'queued') or id in ACTIVE_WORKERS or (id in PROCESSES and PROCESSES[id].poll() is None):
            raise ValueError('Espere o download parar antes de limpar seus parciais.')
        folder = checked_job_directory(id)
        if not folder.exists():
            job.update(partial_bytes=0, cleanup_error=None)
            return True
        failures = []
        for path in folder.iterdir():
            # Only generated files in this job's own folder. Never recurse or
            # follow links, and leave unrelated text/documents untouched.
            generated = (path.name == 'result.jsonl' or '.part' in path.name or path.suffix.lower() in
                         {'.ytdl','.tmp','.temp','.mp4','.mkv','.webm','.m4a','.mp3','.gif','.aac','.opus','.ts','.wav','.ogg','.flv'})
            if job.get('edit'):
                source_prefix = '.keep-edit-source-' + id + '.'
                recorded = (ROOT / job['file']).absolute() if job.get('file') else None
                pending_final = (ROOT / job['editor_output']).absolute() if job.get('editor_output') else None
                generated = generated and (path.name == 'result.jsonl'
                    or path.name.startswith(source_prefix) or path.name.startswith('.keep-edit-output-')
                    or recorded == path.absolute() or pending_final == path.absolute())
            if not generated:
                continue
            if is_link(path) or path.is_dir():
                failures.append(path.name)
                continue
            try:
                if path.resolve().parent != folder.resolve():
                    raise ValueError('Arquivo fora da pasta')
                path.unlink()
            except (OSError, ValueError):
                failures.append(path.name)
        job.update(partial_bytes=partial_size(job), cleanup_error=(
            'Alguns parciais não puderam ser apagados. Feche programas que usam a pasta e clique em Limpar parciais.' if failures else None))
        if not failures:
            for key in ('file','filename','bytes','editor_output'):
                job.pop(key, None)
        persist()
        return not failures

def clean_inactive_job(id):
    try:
        return cleanup_job_files(id)
    except (OSError, ValueError) as error:
        with LOCK:
            JOBS[id]['cleanup_error'] = 'Não foi possível limpar os parciais: ' + str(error)
            persist()
        return False

def parse_progress(id, line):
    if not line.startswith('KEEP_PROGRESS:'):
        return False
    raw = line.split(':',1)[1].strip()
    try:
        data = json.loads(raw)
    except ValueError:
        match = re.search(r'([\d.]+)%', raw)
        if match:
            update_job(id, progress=min(99,float(match[1])))
        return True
    if not isinstance(data,dict):
        return True
    def number(key):
        value = data.get(key)
        return value if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value) and value >= 0 else None
    downloaded, total = number('downloaded_bytes'), number('total_bytes') or number('total_bytes_estimate')
    finished = data.get('status') == 'finished'
    values = dict(downloaded_bytes=downloaded, total_bytes=total, speed=None if finished else number('speed'),
                  eta=None if finished else number('eta'), phase='processing' if finished else 'download',
                  stage='Preparando arquivo' if finished else ('Baixando trecho' if JOBS[id].get('section') else 'Baixando'))
    if downloaded is not None and total:
        values['progress'] = min(99,100*downloaded/total)
    with LOCK:
        JOBS[id].update(values)
        if time.monotonic() - JOBS[id].get('_last_checkpoint',0) > 3:
            JOBS[id]['_last_checkpoint'] = time.monotonic()
            persist()
    return True

def snapshot_jobs():
    with LOCK:
        queued = sorted((j for j in JOBS.values() if j['status']=='queued'), key=lambda j:j.get('queue_order',j.get('created',0)))
        positions = {j['id']:index+1 for index,j in enumerate(queued)}
        return [dict(j, queue_position=positions.get(j['id']),
                     diagnostic_action=DIAGNOSTIC_ACTIONS.get((j.get('diagnostic') or {}).get('category')) if isinstance(j.get('diagnostic'), dict) else None,
                     **({'partial_bytes':partial_size(j)} if j['status']=='paused' else {}),
                     **(completed_file_status(j['id']) if j['status']=='complete' else {}))
                for j in reversed(list(JOBS.values()))]

def enqueue_job(id, browser=None):
    with LOCK:
        job=JOBS[id]
        if job['status'] not in ('paused','failed','cancelled','queued') or id in ACTIVE_WORKERS or (id in PROCESSES and PROCESSES[id].poll() is None):
            raise ValueError('Espere o download parar antes de retomar.')
        if job.get('cleanup_error'):
            raise ValueError('Limpe os parciais deste download antes de tentar novamente.')
        selected = browser if browser is not None else job.get('browser','none')
        snapshot = imported_cookie_text(selected,job['url'])
        validate_url(job['url'])
        validate_section(job.get('section'))
        stop_orphan_process(job)
        job.update(browser=selected, status='queued', stage='Na fila', queue_order=time.time(), speed=None, eta=None,
                   phase='download', error=None, stop_request=None)
        QUEUED_COOKIES[id]=snapshot
        persist()
    dispatch_queue()

def dispatch_queue():
    with LOCK:
        if SHUTTING_DOWN:
            return
        waiting=sorted((j for j in JOBS.values() if j['status']=='queued'),key=lambda j:j.get('queue_order',j.get('created',0)))
        for job in waiting:
            occupied=ACTIVE_WORKERS | {key for key,proc in PROCESSES.items() if proc.poll() is None}
            if len(occupied)>=MAX_DOWNLOADS:
                break
            id=job['id']
            if job.get('browser')=='imported' and id not in QUEUED_COOKIES:
                job.update(status='paused',stage='Importe os cookies para retomar',error='Importe novamente os cookies desta plataforma e clique em Retomar.')
                persist()
                continue
            snapshot=QUEUED_COOKIES.pop(id,None)
            ACTIVE_WORKERS.add(id)
            job.update(status='downloading',stage='Iniciando download',speed=None,eta=None,phase='download',stop_request=None)
            persist()
            worker=threading.Thread(target=run_queued_job,args=(id,snapshot),daemon=True)
            WORKER_THREADS[id]=worker
            worker.start()

def run_queued_job(id, cookie_snapshot=None):
    try:
        job=JOBS[id]
        # Paused sections saved by older versions did not persist this bound.
        if job.get('section') and job.get('platform') == 'YouTube' and job.get('source_duration') is None and not job.get('stop_request'):
            info = metadata(job['url'], job.get('browser', 'none'), cookie_snapshot)
            validate_section(job['section'], info.get('duration'))
            update_job(id, source_duration=info.get('duration'))
        has_ffmpeg=bool(tool_path('ffmpeg'))
        args=format_arguments('video' if job.get('edit') else job.get('mode','video'),job.get('quality','1080'),has_ffmpeg)+section_arguments(job.get('section'),has_ffmpeg,job.get('platform'))
        run_with_retries(id,args,cookie_snapshot)
    except Exception as error:
        with LOCK:
            if id in JOBS and not JOBS[id].get('stop_request'):
                fail_download(id, str(error))
    finally:
        with LOCK:
            job=JOBS[id]
            if job.get('stop_request'):
                requested=job.pop('stop_request')
                update_job(id,status='paused' if requested=='pause' else 'cancelled',stage='Interrompido' if requested=='pause' else 'Cancelado',error=None,speed=None,eta=None)
            ACTIVE_WORKERS.discard(id)
            WORKER_THREADS.pop(id,None)
            if JOBS[id]['status'] in ('failed','cancelled'):
                clean_inactive_job(id)
        dispatch_queue()

def wait_for_retry(id, seconds):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        with LOCK:
            if SHUTTING_DOWN or JOBS[id].get('stop_request') or JOBS[id].get('status') != 'downloading':
                return False
        time.sleep(min(0.1, max(0, until - time.monotonic())))
    return True

def run_with_retries(id, args, cookie_snapshot=None):
    # Keep the worker reservation while waiting, so another job cannot consume
    # its slot and cancellation can still find and stop this worker.
    for attempt in range(1, 4):
        with LOCK:
            job = JOBS[id]
            if job.get('stop_request') or job.get('status') in ('paused', 'cancelled') or SHUTTING_DOWN:
                return
            update_job(id, status='downloading', attempt=attempt, max_attempts=3, error=None, diagnostic=None)
        run_download(id, args, cookie_snapshot)
        with LOCK:
            job = JOBS[id]
            diagnostic = job.get('diagnostic') or {}
            if (job.get('status') != 'failed' or not diagnostic.get('retryable') or attempt >= 3
                    or job.get('stop_request') or SHUTTING_DOWN):
                return
            delay = attempt * 2
            update_job(id, status='downloading', phase='retry', speed=None, eta=None,
                stage=f'Falha temporária. Nova tentativa {attempt + 1} de 3 em {delay} s…',
                error=None, retry_at=time.time() + delay)
        if not wait_for_retry(id, delay):
            with LOCK:
                JOBS[id].pop('retry_at', None)
                persist()
            return
        with LOCK:
            JOBS[id].pop('retry_at', None)
            update_job(id, phase='download', stage=f'Iniciando tentativa {attempt + 1} de 3')

def terminate_process(proc):
    if proc is None:
        return
    if IS_LINUX and getattr(proc, '_keep_group', False):
        # Each download has its own session, including FFmpeg descendants.
        # Kill the group even if its leader has already exited.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            if sig == signal.SIGTERM:
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        proc.wait(timeout=5)
        return
    if proc.poll() is not None:
        return
    if os.name=='nt':
        try:
            subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True,timeout=20,creationflags=CREATE_FLAGS)
        except (OSError,subprocess.TimeoutExpired):
            proc.kill()
    else:
        proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)

def linux_process_identity(pid):
    try:
        # Field 22 is the start tick, after the parenthesized command name.
        stat = (Path('/proc') / str(pid) / 'stat').read_text()
        return stat.rsplit(')', 1)[1].split()[19]
    except FileNotFoundError:
        return None

def editor_orphan_command(job, command):
    if job.get('process_kind') != 'editor' or not isinstance(job.get('edit'), dict):
        return False
    try:
        validate_edit(dict(job['edit'], mode=job['mode']), job['source_duration'])
        folder = job_directory(job)
        marker = folder / '.keep-job.json'
        if (not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', job['id'])
                or any(is_link(path) for path in (folder, marker, *folder.parents))
                or json.loads(marker.read_text(encoding='utf-8')).get('id') != job['id']):
            return False
        source = str(folder / ('.keep-edit-source-' + job['id'] + '.mp4'))
        output = str(folder / '.keep-edit-output-')
        if IS_WINDOWS:
            source, output, command = source.casefold(), output.casefold(), command.casefold()
        return source in command and output in command and '-filter_complex' in command
    except (ValueError, TypeError, KeyError, OSError):
        return False

def stop_linux_orphan(job):
    pid = job.get('process_pid')
    identity = job.get('process_identity')
    if not isinstance(pid, int) or pid <= 0 or not identity:
        # A forced close can happen between Popen and the first history write.
        # Recover only the exact engine, source URL and output-folder template.
        engine = tool_path('yt-dlp')
        if not engine and not (job.get('process_kind') == 'editor' and tool_path('ffmpeg')):
            return
        folder = str(job_directory(job)).replace('%', '%%') + os.sep
        for candidate in Path('/proc').iterdir():
            if not candidate.name.isdigit():
                continue
            try:
                candidate_pid = int(candidate.name)
                if candidate.stat().st_uid != os.getuid() or os.getpgid(candidate_pid) != candidate_pid:
                    continue
                argv = (candidate / 'cmdline').read_bytes().decode('utf-8', 'replace').split('\0')
                editor = bool(argv and argv[0] == tool_path('ffmpeg') and editor_orphan_command(job, '\0'.join(argv)))
                if not editor and (engine not in argv or job.get('url') not in argv or '-o' not in argv):
                    continue
                if not editor:
                    output = argv[argv.index('-o') + 1]
                    if not output.startswith(folder) or os.sep in output[len(folder):]:
                        continue
                start = linux_process_identity(candidate_pid)
            except (OSError, IndexError):
                # Other users' /proc entries may be hidden. Their unreadable
                # processes have not been identified as this download.
                continue
            if start:
                # Once positively identified, a failure to stop must block resume.
                stop_linux_orphan(dict(job, process_pid=candidate_pid, process_identity=start))
        return
    proc = Path('/proc') / str(pid)
    try:
        current = linux_process_identity(pid)
        if current is None:
            if job.get('process_kind') == 'editor':
                # FFmpeg starts its own group, unlike an engine's descendants.
                # Recover it by exact owned intermediate/output arguments.
                scan = dict(job)
                scan.pop('process_pid', None)
                scan.pop('process_identity', None)
                stop_linux_orphan(scan)
                job.pop('process_pid', None)
                job.pop('process_identity', None)
                return
            # A surviving FFmpeg may outlive its leader. Only touch a group
            # containing our FFmpeg and this exact job's output directory.
            matches = []
            for candidate in Path('/proc').iterdir():
                if not candidate.name.isdigit():
                    continue
                try:
                    if candidate.stat().st_uid != os.getuid() or os.getpgid(int(candidate.name)) != pid:
                        continue
                    argv = (candidate / 'cmdline').read_bytes().decode('utf-8', 'replace').split('\0')
                    if (Path(argv[0]).name == 'ffmpeg' and str(job_directory(job)) in '\0'.join(argv)
                            and int(linux_process_identity(int(candidate.name)) or 0) >= int(identity)):
                        matches.append(candidate)
                except (FileNotFoundError, ProcessLookupError, PermissionError):
                    continue
            if not matches:
                job.pop('process_pid', None)
                return
        else:
            argv = (proc / 'cmdline').read_bytes().decode('utf-8', 'replace').split('\0')
            engine = tool_path('yt-dlp')
            folder = str(job_directory(job))
            command = '\0'.join(argv)
            editor = bool(argv and argv[0] == tool_path('ffmpeg') and editor_orphan_command(job, command))
            if (str(current) != str(identity) or (not engine and not editor) or (engine not in argv and not editor)
                    or (folder not in command and folder.replace('%', '%%') not in command)
                    or proc.stat().st_uid != os.getuid() or os.getpgid(pid) != pid):
                job.pop('process_pid', None)
                return
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except (PermissionError, OSError) as error:
        raise ValueError('Não foi possível parar o processo anterior. Feche o Keep antigo antes de retomar.') from error
    job.pop('process_pid', None)
    job.pop('process_identity', None)

def stop_orphan_process(job):
    if IS_LINUX:
        return stop_linux_orphan(job)
    pid=job.get('process_pid')
    if os.name!='nt' or not isinstance(pid,int) or pid<=0:
        return
    # PID alone is not identity: verify the executable and this job's output
    # directory before stopping a surviving child after a forced app close.
    query="$p=Get-CimInstance Win32_Process -Filter ('ProcessId = '+$env:KEEP_ORPHAN_PID); if ($p) { $p | Select-Object ExecutablePath,CommandLine | ConvertTo-Json -Compress }"
    result=subprocess.run(['powershell.exe','-NoProfile','-NonInteractive','-Command',query],capture_output=True,text=True,encoding='utf-8',errors='replace',
                          timeout=15,env=dict(os.environ,KEEP_ORPHAN_PID=str(pid)),creationflags=CREATE_FLAGS)
    if result.returncode:
        raise ValueError('Não foi possível conferir o processo anterior. Reinicie o Windows antes de retomar este download.')
    if result.stdout.strip():
        info=json.loads(result.stdout.lstrip('\ufeff'))
        executable=str(info.get('ExecutablePath') or '').casefold()
        expected={str(tool_path('yt-dlp') or '').casefold(),str(Path(sys.executable)).casefold()}
        folder=str(job_directory(job)).casefold()
        command=str(info.get('CommandLine') or '').casefold()
        if editor_orphan_command(job, command):
            expected.add(str(tool_path('ffmpeg') or '').casefold())
        if executable in expected and (folder in command or folder.replace('%','%%') in command):
            killed=subprocess.run(['taskkill','/PID',str(pid),'/T','/F'],capture_output=True,timeout=20,creationflags=CREATE_FLAGS)
            if killed.returncode:
                raise ValueError('O processo anterior ainda está usando o arquivo. Feche o Keep antigo ou reinicie o Windows antes de retomar.')
    job.pop('process_pid',None)

def stop_job(id, pause=False):
    with LOCK:
        if id not in JOBS or JOBS[id]['status'] not in ('queued','downloading','paused','failed','cancelled'):
            raise ValueError('Este download não pode ser parado.')
        job=JOBS[id]
        job.pop('retry_at', None)
        if id not in ACTIVE_WORKERS and id not in PROCESSES:
            QUEUED_COOKIES.pop(id,None)
            update_job(id,status='paused' if pause else 'cancelled',stage='Interrompido' if pause else 'Cancelado',speed=None,eta=None,stop_request=None)
            if not pause:
                clean_inactive_job(id)
            return
        job.update(stop_request='pause' if pause else 'cancel',stage='Pausando…' if pause else 'Cancelando…',speed=None,eta=None)
        persist()
        proc=PROCESSES.get(id)
        worker=WORKER_THREADS.get(id)
    terminate_process(proc)
    if worker and worker is not threading.current_thread():
        worker.join(timeout=20)

def load_jobs():
    try:
        saved=json.loads((DATA/'history.json').read_text(encoding='utf-8'))
        if not isinstance(saved,dict):
            return
        with LOCK:
            for id,job in saved.items():
                if not isinstance(job,dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',id):
                    continue
                job['id']=id
                interrupted=job.get('status')=='downloading' or (job.get('status')=='failed' and 'Keep foi fechado' in str(job.get('error')))
                if interrupted or (job.get('status')=='queued' and job.get('browser')=='imported'):
                    job.update(status='paused',stage='Interrompido',error=None,stop_request=None,speed=None,eta=None)
                    try:
                        stop_orphan_process(job)
                    except (ValueError,OSError,subprocess.TimeoutExpired) as error:
                        job['error']=str(error)
                JOBS[id]=job
            for id,job in JOBS.items():
                if job.get('status') in ('failed','cancelled'):
                    clean_inactive_job(id)
            persist()
    except (OSError,ValueError,KeyError,AttributeError):
        pass

def shutdown_downloads():
    global SHUTTING_DOWN
    with LOCK:
        SHUTTING_DOWN=True
        ids=list(ACTIVE_WORKERS)
    for id in ids:
        try:
            stop_job(id,pause=True)
        except (OSError,subprocess.TimeoutExpired):
            pass
    with LOCK:
        persist()

def open_job_location(id, select_file=False):
    with LOCK:
        if id not in JOBS:
            raise ValueError('Download não encontrado.')
        job=JOBS[id]
        folder=checked_job_directory(id)
        path=job_file(job) if job['status']=='complete' else None
        if select_file and job['status']=='complete' and path is None:
            raise ValueError('O arquivo final não está mais disponível nesta pasta.')
    if IS_LINUX:
        target = path or folder
        if not target.exists():
            raise ValueError('A pasta deste download não está mais disponível.')
        if select_file and path and shutil.which('gdbus'):
            try:
                result = subprocess.run(['gdbus', 'call', '--session', '--dest', 'org.freedesktop.FileManager1',
                    '--object-path', '/org/freedesktop/FileManager1', '--method', 'org.freedesktop.FileManager1.ShowItems',
                    json.dumps([path.as_uri()]), ''], capture_output=True, timeout=8)
                if result.returncode == 0:
                    return
            except (OSError, subprocess.TimeoutExpired):
                pass
        opener = shutil.which('xdg-open')
        if not opener or not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
            raise ValueError('Abra esta pasta no gerenciador de arquivos: ' + str(folder))
        target = folder if folder.is_dir() else folder.parent
        subprocess.Popen([opener, str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return
    if not IS_WINDOWS:
        raise ValueError('Esta opção está disponível no Windows e no Linux.')
    if select_file:
        target=path or folder
        if not target.exists():
            raise ValueError('A pasta deste download não está mais disponível.')
        subprocess.Popen(['explorer.exe','/select,',str(target)],creationflags=CREATE_FLAGS)
    else:
        target=folder if folder.is_dir() else folder.parent
        if not target.is_dir():
            raise ValueError('A pasta não está mais disponível. Confira o disco de destino.')
        os.startfile(str(target))

def run_download(id, args, cookie_snapshot=None):
    try:
        with cookie_options(JOBS[id].get('browser', 'none'), JOBS[id]['url'], cookie_snapshot) as options:
            _run_download(id, args, options)
    except OSError:
        if JOBS.get(id, {}).get('status') == 'downloading':
            fail_download(id, 'A pasta do download não está disponível.')
    except Exception:
        if JOBS.get(id, {}).get('status') == 'downloading':
            fail_download(id, 'Não foi possível usar os cookies importados.')
    finally:
        with LOCK:
            job=JOBS[id]
            requested=job.get('stop_request')
            if requested:
                update_job(id,status='paused' if requested=='pause' else 'cancelled',stage='Interrompido' if requested=='pause' else 'Cancelado',
                           error=None,stop_request=None,partial_bytes=partial_size(job))
            if job['status']!='downloading':
                job.update(speed=None,eta=None)
            if job['status'] in ('failed','cancelled') and id not in ACTIVE_WORKERS:
                clean_inactive_job(id)

def output_name(job, folder):
    if job.get('edit'):
        # This explicitly owned intermediate is never a user-visible final
        # download. It is fetched solely as input for this job's local edit.
        return '.keep-edit-source-' + job['id'] + '.%(ext)s'
    if job.get('output_name'):
        return job['output_name']
    # Adopt the filename already used by an interrupted older version. This
    # allows yt-dlp's .part/.ytdl continuation to find the same streams again.
    for path in sorted(folder.iterdir()):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name.endswith(('.part','.ytdl')):
            name=re.sub(r'\.(part|ytdl)$','',path.name)
            name=re.sub(r'\.part$','',name)
            name=re.sub(r'\.f[\w-]+(?=\.[^.]+$)','',name)
            stem=Path(name).stem
            return stem.replace('%','%%')+'.%(ext)s'
    section=job.get('section')
    suffix=f"_trecho_{section['start']:g}-{section['end']:g}" if section else ''
    budget=max(16,min(60,235-len(str(folder))-len(suffix)-8))
    if job.get('custom_name'):
        name = validate_custom_name(job['custom_name'])
        while len(name.encode('utf-8')) > budget and len(name) > 1:
            name = name[:-1]
        return name.rstrip(' .') + '.%(ext)s'
    return f'%(title).{budget}B'+suffix+'.%(ext)s'

def effective_job_edit(job, effective):
    edit = validate_edit(dict(job['edit'], mode=job['mode']), job['source_duration'])
    sections = []
    for section in edit['sections']:
        section = dict(section)
        if section['end'] > effective['end']:
            if section['end'] != job['section']['end']:
                raise ValueError('Um trecho interno ultrapassa o fim real do vídeo. Ajuste o horário final.')
            section['end'] = effective['end']
        if section['end'] <= section['start']:
            raise ValueError('O trecho começa depois do fim real do vídeo. Escolha outro intervalo.')
        sections.append(section)
    return validate_edit(dict(edit, sections=sections), max(job['source_duration'], effective['end']))

def editor_final_name(job, folder):
    name = job.get('custom_name') or re.sub(r'[<>:"/\\|?*%\x00-\x1f\x7f]', ' ', job.get('title') or 'Vídeo')
    name = validate_custom_name(name[:150].strip(' .') or 'Vídeo')
    budget = max(16, min(100, 235 - len(str(folder)) - 8))
    while len(name.encode('utf-8')) > budget and len(name) > 1:
        name = name[:-1]
    return name.rstrip(' .') + final_suffix(job['mode'])

def publish_editor_output(temporary, final, on_created=None):
    # Exclusive creation preserves a file created by another program between
    # the final-name check and publication, including on POSIX systems.
    try:
        os.link(temporary, final, follow_symlinks=False)
        if on_created:
            on_created()
    except FileExistsError:
        raise ValueError('O nome final já existe nesta pasta. O arquivo existente foi preservado.') from None
    except OSError:
        # External/FAT destinations can lack hard links. This fallback also
        # refuses to replace an existing file; failed outputs use job cleanup.
        try:
            with final.open('xb') as output, temporary.open('rb') as source:
                if on_created:
                    on_created()
                shutil.copyfileobj(source, output, 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
        except FileExistsError:
            raise ValueError('O nome final já existe nesta pasta. O arquivo existente foi preservado.') from None


def apply_job_edit(id):
    """Transform only this job's explicitly owned, newly downloaded intermediate."""
    with LOCK:
        job = JOBS[id]
        folder = checked_job_directory(id)
        source = (ROOT / job['file']).absolute()
        expected_source = folder / ('.keep-edit-source-' + id + '.mp4')
        if (source != expected_source or not source.is_file()
                or any(is_link(path) for path in (source, *source.parents))):
            raise ValueError('A origem temporária da edição não corresponde a este download.')
        effective = job.get('effective_section') or job['section']
        edit = effective_job_edit(job, effective)
        final = folder / editor_final_name(job, folder)
        if final.exists() or is_link(final):
            raise ValueError('O nome final já existe nesta pasta. Escolha outro nome para preservar o arquivo existente.')
        temporary = folder / ('.keep-edit-output-' + secrets.token_hex(8) + final_suffix(edit['mode']))
        update_job(id, phase='processing', stage='Criando GIF' if edit['mode'] == 'gif' else 'Montando os trechos', speed=None, eta=None)
    ffmpeg = tool_path('ffmpeg')
    if not ffmpeg:
        raise ValueError('A edição precisa do FFmpeg. Prepare os componentes e tente novamente.')
    info = probe_media(ffmpeg, source, process_env())
    edit, alignment_note = frame_aligned_edit(edit, info, offset=effective['start'])
    command, note = transform_command(ffmpeg, source, temporary, edit, info, offset=effective['start'])
    note = ' '.join(value for value in (alignment_note, note) if value) or None
    process = None
    try:
        with LOCK:
            if JOBS[id].get('stop_request') or JOBS[id].get('status') in ('paused', 'cancelled'):
                return
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding='utf-8', errors='replace', creationflags=CREATE_FLAGS,
                env=process_env(), start_new_session=IS_LINUX)
            process._keep_group = IS_LINUX
            PROCESSES[id] = process
            JOBS[id]['process_kind'] = 'editor'
            if isinstance(process.pid, int):
                JOBS[id]['process_pid'] = process.pid
                if IS_LINUX:
                    JOBS[id]['process_identity'] = linux_process_identity(process.pid)
            persist()
        for _line in process.stdout:
            pass  # Drain FFmpeg without retaining source paths or arbitrary logs.
        code = process.wait()
        with LOCK:
            if JOBS[id].get('stop_request') or JOBS[id].get('status') in ('paused', 'cancelled'):
                return
        if code or is_link(temporary) or not temporary.is_file():
            raise ValueError('O FFmpeg não concluiu a edição. Confira os trechos e tente novamente.')
        result = probe_media(ffmpeg, temporary, process_env())
        if abs(result['duration'] - edit['duration']) > .25:
            raise ValueError('A edição não ficou com a duração escolhida. Ajuste os trechos e tente novamente.')
        if edit['mode'] == 'video':
            if not result['width'] or not result['height'] or info['audio'] and not result['audio']:
                raise ValueError('Não foi possível conferir a imagem ou o áudio da edição.')
            if edit['vertical'] and abs(result['width'] / result['height'] - 9 / 16) > .015:
                raise ValueError('O vídeo não ficou em 9:16. Confira o enquadramento e tente novamente.')
        if edit['mode'] == 'audio' and not result['audio']:
            raise ValueError('A edição não contém o áudio esperado.')
        with LOCK:
            if JOBS[id].get('stop_request') or JOBS[id].get('status') in ('paused', 'cancelled'):
                return
            if final.exists() or any(is_link(path) for path in (temporary, final, source, folder, *folder.parents)):
                raise ValueError('O caminho da edição mudou. Os arquivos existentes foram preservados.')
            publish_editor_output(temporary, final,
                on_created=lambda: update_job(id, editor_output=stored_path(final)))
            update_job(id, file=stored_path(final), filename=final.name, bytes=final.stat().st_size,
                effective_edit=edit, edit_note=note, editor_output=None)
            # Only the generated intermediate with this exact job ID can be
            # discarded. User-visible final downloads are never removed here.
            if source == expected_source and source.parent == folder and not is_link(source):
                try:
                    source.unlink(missing_ok=True)
                except OSError:
                    update_job(id, intermediate_note='O arquivo editado foi salvo. Uma cópia temporária continua nesta pasta porque outro programa está usando o arquivo.')
    finally:
        if process and (process.poll() is None or IS_LINUX and getattr(process, '_keep_group', False)):
            terminate_process(process)
        if process and process.stdout:
            process.stdout.close()
        with LOCK:
            if PROCESSES.get(id) is process:
                PROCESSES.pop(id, None)
            for key in ('process_pid', 'process_identity', 'process_kind'):
                JOBS[id].pop(key, None)
        if temporary.is_file() and not any(is_link(path) for path in (temporary, *temporary.parents)):
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

def _run_download(id, args, session_options):
    folder = ROOT / JOBS[id]['directory'] if JOBS[id].get('directory') else DOWNLOADS / id
    folder.mkdir(parents=True, exist_ok=True)
    JOBS[id].setdefault('id',id)
    checked_job_directory(id)
    report = folder / 'result.jsonl'
    section = JOBS[id].get('section')
    with LOCK:
        JOBS[id]['output_name']=output_name(JOBS[id],folder)
        persist()
    template = str(folder).replace('%', '%%') + os.sep + JOBS[id]['output_name']
    # yt-dlp's missing numeric fields otherwise render as bare NA, which is
    # invalid JSON. Explicit null defaults also cover estimates not yet known.
    progress='download:KEEP_PROGRESS:{'+','.join('"'+key+'":%(progress.'+key+'|null)s' for key in ['downloaded_bytes','total_bytes','total_bytes_estimate','speed','eta'])+',"status":%(progress.status|unknown)j}'
    command = video_command(JOBS[id].get('video_index', 1), JOBS[id].get('video_id')) if JOBS[id].get('platform') == 'X' else base_command()
    cmd = command + session_options + source_arguments(JOBS[id]['url']) + ['--no-continue' if section else '--continue', '--newline', '--no-simulate', '--progress', '--progress-template', progress, '--progress-template','postprocess:KEEP_PROCESSING:%(progress.status)s', '--print-to-file', 'after_move:%(filepath)j', str(report), '-o', template] + args + ['--', JOBS[id]['url']]
    errors = []
    source_probe = InputDurationProbe()
    proc = None
    try:
        with LOCK:
            if JOBS[id]['status'] in ('cancelled','paused') or JOBS[id].get('stop_request'):
                return
            report.unlink(missing_ok=True)
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', creationflags=CREATE_FLAGS, env=process_env(), start_new_session=IS_LINUX)
            proc._keep_group = IS_LINUX
            PROCESSES[id] = proc
            if isinstance(proc.pid,int):
                JOBS[id]['process_pid']=proc.pid
                if IS_LINUX:
                    JOBS[id]['process_identity']=linux_process_identity(proc.pid)
            persist()
        for line in proc.stdout:
            source_probe.feed(line)
            if parse_progress(id,line):
                pass
            elif line.startswith('KEEP_PROCESSING:'):
                update_job(id,phase='processing',stage='Finalizando arquivo',speed=None,eta=None)
            elif line.startswith('KEEP_FILE:'):
                path = Path(line.split(':', 1)[1].strip()).resolve()
                if path.is_relative_to(folder.resolve()) and path.is_file():
                    update_job(id, file=stored_path(path), filename=path.name, bytes=path.stat().st_size)
            else:
                errors.append(line)
                errors = errors[-30:]
                if '[Merger]' in line or '[ExtractAudio]' in line or '[VideoRemuxer]' in line:
                    update_job(id, phase='processing',stage='Finalizando arquivo',speed=None,eta=None)
        code = proc.wait()
        # Use a UTF-8 JSON report instead of decoding a Windows console pathname.
        # This preserves accents, spaces and non-ASCII user directory names.
        if code == 0 and report.is_file():
            for value in report.read_text(encoding='utf-8').splitlines():
                path = Path(json.loads(value)).resolve()
                if path.is_relative_to(folder.resolve()) and path.is_file():
                    update_job(id, file=stored_path(path), filename=path.name, bytes=path.stat().st_size)
        if code == 0 and section and JOBS[id].get('file') and not JOBS[id].get('stop_request'):
            update_job(id, stage='Conferindo trecho')
            effective = effective_section(JOBS[id], source_probe.durations)
            verify_section_output(ROOT / JOBS[id]['file'], effective)
            note = None
            if effective['end'] != section['end']:
                end = f"{effective['end']:.2f}".replace('.', ',')
                note = f'O vídeo termina em {end} s; o trecho foi salvo até o fim real, sem acrescentar conteúdo.'
            update_job(id, effective_section=effective, section_note=note)
        if code == 0 and JOBS[id].get('edit') and JOBS[id].get('file') and not JOBS[id].get('stop_request'):
            apply_job_edit(id)
        with LOCK:
            if JOBS[id]['status'] not in ('cancelled','paused') and not JOBS[id].get('stop_request'):
                if code == 0 and JOBS[id].get('file'):
                    update_job(id, status='complete', progress=100, stage='Pronto', completed=time.time())
                    record_latest_download(JOBS[id], folder)
                    notify_completed_job(id)
                else:
                    fail_download(id, ''.join(errors) if code else 'O mecanismo terminou sem confirmar um arquivo final.', code)
    except Exception as error:
        if JOBS[id]['status'] not in ('cancelled','paused') and not JOBS[id].get('stop_request'):
            fail_download(id, str(error))
    finally:
        if proc and (proc.poll() is None or (IS_LINUX and getattr(proc, '_keep_group', False) is True and JOBS[id].get('status') != 'complete')):
            terminate_process(proc)
        if proc and proc.stdout:
            proc.stdout.close()
        with LOCK:
            PROCESSES.pop(id, None)
            JOBS[id].pop('process_pid',None)
            JOBS[id].pop('process_identity',None)
            JOBS[id].pop('process_kind',None)

@contextmanager
def preview_command(request, folder):
    with cookie_options(request['browser'], request['url'], request.get('cookie_snapshot')) as options:
        command = video_command(request.get('video_index', 1), request.get('video_id')) if request['platform'] == 'X' else base_command()
        yield command + options + source_arguments(request['url']) + [
            '--no-continue', '--no-simulate', '--no-progress', '--max-filesize', '1G',
            '-f', 'bv*[height<=480]+ba/b[height<=480]/b[height<=720]',
            '-S', 'res,proto:https', '--merge-output-format', 'mkv',
            '-o', str(folder).replace('%', '%%') + os.sep + 'source.%(ext)s', '--', request['url']]

def ensure_services():
    global PREVIEWS, UPDATES
    with LOCK:
        if PREVIEWS is None:
            from preview_media import PreviewManager
            PREVIEWS = PreviewManager(DATA / 'previews', preview_command, tool_path('ffmpeg'),
                error_formatter=lambda text, request: friendly_error(text, request.get('platform')),
                process_env=process_env, timeout=7200)
        if UPDATES is None:
            from app_updates import UpdateManager
            UPDATES = UpdateManager(ROOT, VERSION, 'windows' if IS_WINDOWS else 'linux', PORT)

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def safe_host(self):
        return self.headers.get('Host') in (f'127.0.0.1:{PORT}', f'localhost:{PORT}')

    def respond(self, code, data):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def do_HEAD(self):
        self.do_GET()

    def send_preview(self, id, key):
        from preview_media import range_response
        ensure_services()
        try:
            path = PREVIEWS.media_file(id, key)
            status, headers, offset, count = range_response(path.stat().st_size, self.headers.get('Range'))
        except (OSError, ValueError, KeyError):
            return self.respond(404, {'error': 'Esta prévia não está mais disponível. Prepare-a novamente.'})
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command == 'HEAD' or not count:
            return
        try:
            with path.open('rb') as media:
                media.seek(offset)
                while count:
                    chunk = media.read(min(256 * 1024, count))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    count -= len(chunk)
        except OSError:
            pass

    def do_GET(self):
        if not self.safe_host():
            return self.respond(403, {'error': 'Acesso somente neste computador.'})
        path = urlparse(self.path).path
        if path == '/api/session':
            return self.respond(200, {'token': TOKEN, 'version': VERSION, 'platform': 'linux' if IS_LINUX else 'windows' if IS_WINDOWS else 'other', 'ffmpeg': bool(tool_path('ffmpeg')), 'engine': bool(tool_path('yt-dlp') or importlib.util.find_spec('yt_dlp')), 'runtime': bool(tool_path('node') or tool_path('deno')), **folder_status()})
        if path.startswith('/api/'):
            if self.headers.get('X-Keep-Token') != TOKEN:
                return self.respond(403, {'error': 'Reabra o Keep para reconectar.'})
            if path == '/api/jobs':
                return self.respond(200, snapshot_jobs())
            if path == '/api/cookies':
                return self.respond(200, cookie_status())
            if path == '/api/settings':
                return self.respond(200, folder_status())
            if path == '/api/storage':
                return self.respond(200, storage_status())
            if path == '/api/release-notes':
                return self.respond(200, release_notes(VERSION))
            if path == '/api/desktop':
                try:
                    return self.respond(200, desktop.status(ROOT))
                except (OSError, ValueError) as error:
                    return self.respond(400, {'error': str(error)})
            if path == '/api/update/restore-points':
                ensure_services()
                try:
                    return self.respond(200, UPDATES.restore_points())
                except (OSError, ValueError) as error:
                    return self.respond(400, {'error': str(error)})
            if path == '/api/preview':
                ensure_services()
                try:
                    return self.respond(200, PREVIEWS.status(parse_qs(urlparse(self.path).query).get('id', [''])[0]))
                except (ValueError, KeyError):
                    return self.respond(404, {'error': 'Prévia não encontrada. Prepare-a novamente.'})
            if path == '/api/update':
                ensure_services()
                return self.respond(200, UPDATES.snapshot())
            return self.respond(404, {'error': 'Não encontrado'})
        if path.startswith('/preview/'):
            return self.send_preview(path.rsplit('/', 1)[-1], parse_qs(urlparse(self.path).query).get('key', [''])[0])
        if path.startswith('/download/'):
            id = path.rsplit('/', 1)[-1]
            with LOCK:
                job = JOBS.get(id, {})
                filename = job_file(job)
            if job.get('status') != 'complete' or filename is None:
                return self.respond(404, {'error': 'O arquivo não está mais disponível.'})
            return self.send_file(filename, attachment=True)
        filename = ROOT / 'dist' / ('index.html' if path == '/' else unquote(path.lstrip('/')))
        if not filename.resolve().is_relative_to((ROOT / 'dist').resolve()) or not filename.is_file():
            return self.respond(404, {'error': 'Não encontrado'})
        self.send_file(filename)

    def send_file(self, path, attachment=False):
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(path.stat().st_size))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' https: data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if attachment:
            self.send_header('Content-Disposition', 'attachment; filename="' + path.name.replace('"', '') + '"')
        self.end_headers()
        try:
            if self.command != 'HEAD':
                with path.open('rb') as file:
                    shutil.copyfileobj(file, self.wfile)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_POST(self):
        global COOKIE_GENERATION, SHUTTING_DOWN
        try:
            length = int(self.headers.get('Content-Length', '0'))
            limit = (8 * 1024 * 1024 if self.path == '/api/update/receive' else 2 * MAX_COOKIE_BYTES
                     if self.path == '/api/cookies/import' else 64 * 1024 if self.path == '/api/settings/import' else 8192)
            if not 0 < length <= limit:
                raise ValueError('Tamanho de solicitação inválido.')
            # Drain the bounded body before rejecting to avoid a TCP reset on Windows.
            self.connection.settimeout(15)
            raw_body = self.rfile.read(length)
            if not self.safe_host() or self.headers.get('X-Keep-Token') != TOKEN or self.headers.get('Origin') not in (None, f'http://127.0.0.1:{PORT}', f'http://localhost:{PORT}'):
                return self.respond(403, {'error': 'Esta solicitação precisa vir do app Keep.'})
            body = json.loads(raw_body)
            if not isinstance(body, dict):
                raise ValueError('Solicitação inválida.')
            if self.path == '/api/settings/folder':
                return self.respond(200, save_download_folder(body.get('folder')))
            if self.path == '/api/settings/preferences':
                return self.respond(200, save_preferences(body))
            if self.path in ('/api/desktop/launcher', '/api/desktop/protocol'):
                if SHUTTING_DOWN:
                    raise ValueError('O Keep está fechando. Abra novamente para configurar a integração.')
                if self.path == '/api/desktop/launcher':
                    return self.respond(200, desktop.install_launcher(ROOT))
                enabled = body.get('enabled')
                if type(enabled) is not bool:
                    raise ValueError('Escolha ativar ou desativar a abertura pelo site.')
                return self.respond(200, desktop.set_protocol_enabled(ROOT, enabled))
            if self.path == '/api/settings/export':
                return self.respond(200, export_preferences(preference_status()))
            if self.path == '/api/settings/import':
                return self.respond(200, {'preferences': save_preferences(import_preferences(body.get('backup')))})
            if self.path == '/api/quit':
                if HTTP_SERVER is None:
                    raise ValueError('Esta janela do Keep não pode ser encerrada agora.')
                SHUTTING_DOWN = True
                self.respond(200, {'ok': True})
                threading.Thread(target=HTTP_SERVER.shutdown, daemon=True).start()
                return
            if self.path == '/api/preview':
                ensure_services()
                if SHUTTING_DOWN:
                    raise ValueError('O Keep está fechando. Aguarde e tente novamente.')
                url, platform = validate_url(body.get('url'))
                browser = body.get('browser', preference_status()['default_browser'])
                snapshot = imported_cookie_text(browser, url)
                info = selected_metadata(body, url, browser, snapshot)
                if info.get('live'):
                    raise ValueError('A prévia está disponível para vídeos gravados, clipes e VODs.')
                if not tool_path('ffmpeg'):
                    raise ValueError('A prévia precisa do FFmpeg. Prepare os componentes e tente novamente.')
                request = {'url': url, 'platform': platform, 'browser': browser, 'cookie_snapshot': snapshot,
                    'video_index': info.get('video_index', 1), 'video_id': info.get('video_id') if platform == 'X' else None}
                return self.respond(202, PREVIEWS.create(request, duration=info.get('duration')))
            if self.path == '/api/preview/cancel':
                ensure_services()
                PREVIEWS.cancel(body.get('id'))
                return self.respond(200, {'ok': True})
            if self.path.startswith('/api/update/'):
                ensure_services()
                operation = self.path.rsplit('/', 1)[-1]
                if operation == 'check':
                    origin = self.headers.get('Origin') or f'http://127.0.0.1:{PORT}'
                    return self.respond(200, UPDATES.begin_check(origin))
                if operation == 'download':
                    origin = self.headers.get('Origin') or f'http://127.0.0.1:{PORT}'
                    return self.respond(200, UPDATES.begin_download(origin))
                if operation == 'receive':
                    return self.respond(200, UPDATES.receive(body.get('package_base64'), filename=body.get('filename')))
                if operation == 'rollback':
                    if body.get('confirm') is not True:
                        raise ValueError('Confirme que deseja preparar o retorno à versão anterior.')
                    with LOCK:
                        if SHUTTING_DOWN:
                            raise ValueError('O Keep está fechando. Aguarde e tente novamente.')
                        if ACTIVE_WORKERS or any(job['status'] == 'downloading' for job in JOBS.values()):
                            raise ValueError('Pause ou aguarde os downloads em andamento antes de preparar a restauração. A fila e os pausados serão mantidos.')
                        return self.respond(200, UPDATES.prepare_rollback(body.get('id')))
                if operation == 'apply':
                    with LOCK:
                        if SHUTTING_DOWN:
                            raise ValueError('O Keep já está fechando para atualizar. Aguarde o reinício.')
                        if ACTIVE_WORKERS or any(job['status'] == 'downloading' for job in JOBS.values()):
                            raise ValueError('Pause ou aguarde os downloads em andamento antes de atualizar. A fila e os pausados serão mantidos.')
                        if HTTP_SERVER is None:
                            raise ValueError('Reabra o Keep pelo iniciador antes de aplicar a atualização.')
                        SHUTTING_DOWN = True
                        try:
                            result = UPDATES.start_apply(parent_pid=os.getpid(), port=PORT)
                        except Exception:
                            SHUTTING_DOWN = False
                            raise
                    self.respond(200, result)
                    threading.Thread(target=HTTP_SERVER.shutdown, daemon=True).start()
                    return
                raise ValueError('Opção de atualização inválida.')
            if self.path == '/api/settings/choose-folder':
                return self.respond(200, {'folder': choose_download_folder()})
            if self.path in ('/api/open-folder','/api/show-file'):
                open_job_location(body.get('id'),select_file=self.path=='/api/show-file')
                return self.respond(200,{'ok':True})
            if self.path == '/api/delete-file':
                return self.respond(200, delete_completed_file(body.get('id'), body.get('confirm')))
            if self.path == '/api/queue/reorder':
                return self.respond(200, reorder_queue(body.get('ids')))
            if self.path == '/api/duplicates':
                return self.respond(200, {'matches': duplicate_matches(body)})
            if self.path == '/api/diagnostic':
                return self.respond(200, diagnostic_report(body.get('id')))
            if self.path == '/api/cleanup':
                id=body.get('id')
                if id not in JOBS or JOBS[id]['status'] not in ('failed','cancelled'):
                    raise ValueError('Use Descartar para apagar os parciais de um download interrompido.')
                return self.respond(200,{'ok':clean_inactive_job(id)})
            if self.path == '/api/resume':
                if body.get('id') not in JOBS:
                    raise ValueError('Download não encontrado.')
                enqueue_job(body['id'],body.get('browser'))
                return self.respond(202,{'id':body['id'],'status':JOBS[body['id']]['status']})
            if self.path in ('/api/pause','/api/cancel'):
                stop_job(body.get('id'),pause=self.path=='/api/pause')
                return self.respond(200,{'ok':True})
            if self.path == '/api/cookies/import':
                parsed, counts, ignored = parse_cookie_file(body.get('content'))
                with LOCK:
                    COOKIE_GENERATION += 1
                    INSPECTIONS.clear()
                    IMPORTED_COOKIES.clear()
                    IMPORTED_COOKIES.update(parsed)
                    status = cookie_status()
                return self.respond(200, dict(status, counts=counts, ignored=ignored))
            if self.path == '/api/cookies/remove':
                with LOCK:
                    COOKIE_GENERATION += 1
                    INSPECTIONS.clear()
                    IMPORTED_COOKIES.clear()
                return self.respond(200, cookie_status())
            if self.path == '/api/inspect':
                return self.respond(200, metadata(body.get('url'), body.get('browser', 'none')))
            if self.path == '/api/download':
                url, platform = validate_url(body.get('url'))
                mode, quality = body.get('mode', 'video'), body.get('quality', '1080')
                if mode not in ('video', 'audio', 'gif') or quality not in ('best', '2160', '1080', '720', '480'):
                    raise ValueError('Escolha um formato e uma qualidade válidos.')
                browser = body.get('browser', preference_status()['default_browser'])
                custom_name = validate_custom_name(body.get('custom_name'))
                cookie_snapshot = imported_cookie_text(browser, url)
                has_ffmpeg = bool(tool_path('ffmpeg'))
                edit = None
                wants_edit = body.get('edit') is not None
                if wants_edit and not isinstance(body['edit'], dict):
                    raise ValueError('Escolha os trechos e o enquadramento no editor.')
                if mode == 'gif' and not wants_edit:
                    raise ValueError('Escolha um trecho de até 30 segundos para criar o GIF.')
                if wants_edit and not has_ffmpeg:
                    raise ValueError('A edição precisa do FFmpeg. Prepare os componentes e tente novamente.')
                section = None if wants_edit else validate_section(body.get('section'))
                source_duration = None
                info = None
                if wants_edit or section or platform == 'X' or body.get('inspection_id'):
                    info = selected_metadata(body, url, browser, cookie_snapshot)
                    if info['live']:
                        raise ValueError('Espere a transmissão terminar para baixar a gravação.')
                if wants_edit:
                    if info.get('duration') is None:
                        raise ValueError('A plataforma não informou a duração. Baixe o vídeo inteiro.')
                    edit = requested_edit(body, info['duration'])
                    section = edit_bounds(edit)
                if section:
                    if info.get('duration') is None:
                        raise ValueError('A plataforma não informou a duração. Baixe o vídeo inteiro.')
                    section = validate_section(section, info['duration'])
                    source_duration = info['duration']
                format_arguments('video' if edit else mode, quality, has_ffmpeg)
                section_arguments(section, has_ffmpeg, platform)
                with LOCK:
                    if SHUTTING_DOWN:
                        raise ValueError('O Keep está fechando. Abra novamente para adicionar downloads.')
                    matches = duplicate_matches({'url': url, 'video_id': (info or {}).get('video_id') if platform == 'X' else None,
                        'mode': mode, 'quality': quality, 'section': section, 'edit': edit})
                    if matches and body.get('allow_duplicate') is not True:
                        return self.respond(409, {'error': 'Este vídeo já foi baixado com essas opções. Deseja baixar novamente?',
                            'code': 'duplicate', 'matches': matches})
                    id = secrets.token_hex(8)
                    title = str((info or {}).get('title') or body.get('title') or platform + ' vídeo')[:200]
                    created = time.time()
                    root=download_root()
                    if preference_status()['platform_folders']:
                        root = root / platform
                        if any(is_link(path) for path in (root, *root.parents)):
                            raise ValueError('A pasta da plataforma contém um vínculo. Escolha uma pasta normal nas Preferências.')
                    directory = create_download_directory(root, title, platform, created)
                    (directory/'.keep-job.json').write_text(json.dumps({'id':id}),encoding='utf-8')
                    JOBS[id] = {'id': id, 'url': url, 'platform': platform, 'title': title, 'browser': browser, 'mode': mode, 'quality': quality, 'section': section, 'source_duration': source_duration, 'edit': edit,
                        'video_index': (info or {}).get('video_index', 1), 'video_id': (info or {}).get('video_id') if platform == 'X' else None, 'custom_name': custom_name,
                        'directory': stored_path(directory), 'managed_root':stored_path(root), 'status': 'queued', 'progress': 0, 'stage': 'Na fila', 'created': created,'queue_order':created}
                    QUEUED_COOKIES[id]=cookie_snapshot
                    persist()
                dispatch_queue()
                return self.respond(202, {'id': id,'status':JOBS[id]['status']})
            if self.path == '/api/clear':
                with LOCK:
                    for id in list(JOBS):
                        if JOBS[id]['status'] not in ('downloading','queued','paused') and id not in ACTIVE_WORKERS and id not in PROCESSES and not JOBS[id].get('cleanup_error'):
                            del JOBS[id]
                    persist()
                return self.respond(200, {'ok': True})
            return self.respond(404, {'error': 'Não encontrado'})
        except subprocess.TimeoutExpired:
            self.respond(504, {'error': 'A plataforma demorou para responder. Tente novamente.'})
        except (ValueError, TypeError) as error:
            self.respond(400, {'error': str(error)})
        except Exception:
            self.respond(500, {'error': 'Ocorreu um erro. Reinicie o Keep e tente novamente.'})

@contextmanager
def application_lock():
    if IS_WINDOWS:
        import msvcrt
        lock_path = ROOT / '.keep-app.lock'
        if is_link(lock_path):
            raise RuntimeError('O arquivo de controle do Keep contém um vínculo. Use uma pasta normal do app.')
        with lock_path.open('a+b') as handle:
            if handle.seek(0, 2) == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError('Esta pasta do Keep já está em uso ou sendo atualizada. Acesse http://127.0.0.1:8765 ou espere a atualização terminar.') from None
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    if not IS_LINUX:
        yield
        return
    import fcntl
    lock_path = ROOT / '.keep-linux.lock'
    if lock_path.is_symlink():
        raise RuntimeError('A pasta do Keep contém um atalho no arquivo de controle. Use uma pasta normal do aplicativo.')
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('Esta pasta do Keep já está em uso ou sendo atualizada. Acesse http://127.0.0.1:8765 ou espere a atualização terminar.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)

def main():
    with application_lock():
        serve()

def serve():
    global PORT, SHUTTING_DOWN, HTTP_SERVER
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--open', action='store_true')
    args = parser.parse_args()
    PORT = args.port
    DATA.mkdir(exist_ok=True)
    DOWNLOADS.mkdir(exist_ok=True)
    load_settings()
    url = f'http://127.0.0.1:{PORT}'
    try:
        server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    except OSError:
        try:
            with urlopen(url + '/api/session', timeout=2) as response:
                active = json.load(response)
            if 'token' in active and 'engine' in active:
                import webbrowser
                webbrowser.open(url)
                print('O Keep já está aberto. Use a janela existente.', flush=True)
                return
        except Exception:
            pass
        raise RuntimeError('A porta 8765 está ocupada. Feche a versão anterior do Keep e tente novamente.')
    SHUTTING_DOWN=False
    HTTP_SERVER=server
    load_jobs()
    dispatch_queue()
    print(f'Keep pronto em {url}', flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(url)
    try:
        if IS_LINUX:
            # Terminal close and SIGTERM preserve partials just like Ctrl+C.
            def stop_signal(signum, frame):
                raise KeyboardInterrupt
            signal.signal(signal.SIGTERM, stop_signal)
            signal.signal(signal.SIGHUP, stop_signal)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_downloads()
        if PREVIEWS is not None:
            PREVIEWS.close()
        server.server_close()
        HTTP_SERVER=None

if __name__ == '__main__':
    main()
