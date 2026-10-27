"""Public release checks and verified, bounded application-package imports."""
import base64
import binascii
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import stat
import subprocess
import sys
import tarfile
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import URLError
from http.client import HTTPException
import zipfile

from updater_runner import (PLATFORM_FILES, package_files, safe_path, validate_staged,
                            version_tuple, verify_restore_point, list_restore_points)

PUBLIC_MANIFEST_URL = 'https://raw.githubusercontent.com/JoaoPPolli/keep/main/updates.json'
MAX_MANIFEST_BYTES = 64 * 1024
NETWORK_TIMEOUT = 10
DOWNLOAD_DEADLINE = 40

MAX_ARCHIVE_BYTES = 4 * 1024 * 1024
MAX_EXPANDED_BYTES = 16 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 96
MAX_JSON_BYTES = 6 * 1024 * 1024


def _validate_public_url(url, kind, version=None, platform=None, redirect=False):
    if not isinstance(url, str) or len(url) > 8192:
        raise ValueError('O endereço da atualização é inválido.')
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.fragment):
        raise ValueError('A atualização exige um endereço HTTPS oficial do Keep.')
    if kind == 'manifest':
        valid = (parsed._replace(query='').geturl() == PUBLIC_MANIFEST_URL
                 and (not parsed.query or re.fullmatch(r'keep_check=[0-9a-f]{16}', parsed.query)))
    elif kind == 'release':
        valid = (parsed.hostname == 'github.com' and not parsed.query
                 and parsed.path == '/JoaoPPolli/keep/releases/tag/v' + version)
    elif redirect and parsed.hostname in ('release-assets.githubusercontent.com',
                                          'objects.githubusercontent.com',
                                          'github-releases.githubusercontent.com'):
        valid = parsed.path.startswith('/')
    else:
        suffix = '-Windows-Atualizacao.zip' if platform == 'windows' else '-Linux-Atualizacao.tar.gz'
        expected = '/JoaoPPolli/keep/releases/download/v' + version + '/Keep-' + version + suffix
        valid = parsed.hostname == 'github.com' and not parsed.query and parsed.path == expected
    if not valid:
        raise ValueError('A atualização aponta para um endereço não autorizado.')
    return url


def _fetch_public(url, limit, kind, version=None, platform=None):
    """Bound reads and validate every redirect before sending an HTTPS request."""
    _validate_public_url(url, kind, version, platform)

    class OfficialRedirects(HTTPRedirectHandler):
        redirects = 0

        def redirect_request(self, req, fp, code, msg, headers, newurl):
            self.redirects += 1
            if self.redirects > 4 or time.monotonic() - started > DOWNLOAD_DEADLINE:
                raise ValueError('A consulta da atualização demorou demais. Tente novamente.')
            _validate_public_url(newurl, kind, version, platform, redirect=True)
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    request_url = url + '?keep_check=' + secrets.token_hex(8) if kind == 'manifest' else url
    request = Request(request_url, headers={'User-Agent': 'Keep-update-check', 'Accept': 'application/json' if kind == 'manifest' else 'application/octet-stream', 'Cache-Control': 'no-cache'})
    started = time.monotonic()
    with build_opener(OfficialRedirects()).open(request, timeout=NETWORK_TIMEOUT) as response:
        _validate_public_url(response.geturl(), kind, version, platform, redirect=True)
        length = response.headers.get('Content-Length')
        if length is not None and (not length.isdigit() or int(length) > limit):
            raise ValueError('A resposta da atualização excede o tamanho permitido.')
        data = bytearray()
        while len(data) <= limit:
            if time.monotonic() - started > DOWNLOAD_DEADLINE:
                raise ValueError('A consulta da atualização demorou demais. Tente novamente.')
            # One socket read per iteration keeps a slow stream subject to the deadline.
            chunk = response.read1(min(64 * 1024, limit + 1 - len(data)))
            if not chunk:
                return bytes(data)
            data.extend(chunk)
        raise ValueError('A resposta da atualização excede o tamanho permitido.')


def inspect_public_release(release, platform):
    if (not isinstance(release, dict) or type(release.get('schema_version')) is not int
            or release['schema_version'] != 1 or not isinstance(release.get('packages'), dict)):
        raise ValueError('A descrição pública da atualização é inválida.')
    version = release.get('version')
    version_tuple(version)
    release_url = _validate_public_url(release.get('release_url'), 'release', version)
    package = release['packages'].get(platform)
    if (platform not in PLATFORM_FILES or not isinstance(package, dict)
            or not isinstance(package.get('sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', package['sha256'])
            or type(package.get('size')) is not int or not 0 < package['size'] <= MAX_ARCHIVE_BYTES):
        raise ValueError('A atualização não contém um pacote válido para este sistema.')
    _validate_public_url(package.get('url'), 'package', version, platform)
    return {'version': version, 'release_url': release_url,
            'package': {key: package[key] for key in ('url', 'size', 'sha256')}}


def _archive_members(content):
    """Read regular files only, without extracting any archive-controlled path."""
    entries, total = {}, 0
    if content[:4] == b'PK\x03\x04':
        archive = zipfile.ZipFile(io.BytesIO(content))
        members = archive.infolist()
        if len(members) > MAX_ARCHIVE_MEMBERS:
            raise ValueError('O pacote contém arquivos demais.')
        with archive:
            for member in members:
                mode = member.external_attr >> 16
                if member.is_dir():
                    continue
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                    raise ValueError('O pacote contém um vínculo ou arquivo especial.')
                total += member.file_size
                if total > MAX_EXPANDED_BYTES:
                    raise ValueError('O pacote de atualização é grande demais.')
                if member.filename in entries:
                    raise ValueError('O pacote repete um arquivo.')
                entries[member.filename] = archive.read(member)
    else:
        with tarfile.open(fileobj=io.BytesIO(content), mode='r:gz') as archive:
            for index, member in enumerate(archive):
                if index >= MAX_ARCHIVE_MEMBERS:
                    raise ValueError('O pacote contém arquivos demais.')
                if not member.isfile():
                    raise ValueError('O pacote contém um vínculo ou arquivo especial.')
                total += member.size
                if total > MAX_EXPANDED_BYTES:
                    raise ValueError('O pacote de atualização é grande demais.')
                if member.name in entries:
                    raise ValueError('O pacote repete um arquivo.')
                entries[member.name] = archive.extractfile(member).read(member.size + 1)
    return entries


def inspect_archive(content, platform):
    if platform not in PLATFORM_FILES:
        raise ValueError('O atualizador funciona no Windows e no Linux.')
    if not content or len(content) > MAX_ARCHIVE_BYTES:
        raise ValueError('Use o pacote pequeno de atualização do Keep, com até 4 MB.')
    try:
        entries = _archive_members(content)
    except (zipfile.BadZipFile, tarfile.TarError, EOFError, OSError, RuntimeError):
        raise ValueError('O pacote está inválido ou incompleto. Envie o ZIP ou TAR.GZ original.') from None
    clean = {}
    prefixes = set()
    for name, value in entries.items():
        part = PurePosixPath(name)
        if (not name or part.is_absolute() or '..' in part.parts or '\\' in name
                or ':' in name or '\0' in name or part.as_posix() != name or len(part.parts) < 2):
            raise ValueError('O pacote contém um caminho inválido.')
        prefixes.add(part.parts[0])
        relative = '/'.join(part.parts[1:])
        if relative in clean:
            raise ValueError('O pacote repete um arquivo.')
        clean[relative] = value
    if len(prefixes) != 1 or 'manifest.json' not in clean:
        raise ValueError('Selecione o pacote de atualização original do Keep.')
    try:
        manifest = json.loads(clean['manifest.json'])
    except (ValueError, UnicodeError):
        raise ValueError('A descrição do pacote de atualização é inválida.') from None
    if not isinstance(manifest, dict):
        raise ValueError('A descrição do pacote de atualização é inválida.')
    version_tuple(manifest.get('version'))
    if platform == 'linux' and (manifest.get('platform') != 'linux' or manifest.get('layout') != 'payload'):
        raise ValueError('No Linux, selecione o pacote Linux de atualização (.tar.gz).')
    if platform == 'windows' and manifest.get('platform') not in (None, 'windows'):
        raise ValueError('No Windows, selecione o pacote Windows de atualização (.zip).')
    expected = set(package_files(manifest.get('files'), platform))
    wrappers = {'manifest.json', 'update_keep.py', 'Atualizar Keep.bat', 'LEIA-ME.txt'} if platform == 'windows' else {
        'manifest.json', 'update_keep_linux.py', 'Atualizar Keep.sh', 'LEIA-ME-LINUX.txt'}
    if set(clean) != wrappers | {'payload/' + relative for relative in expected}:
        raise ValueError('O pacote contém arquivos inesperados. Use o pacote pequeno de atualização original.')
    payload = {}
    for relative, expected_hash in manifest['files'].items():
        value = clean['payload/' + relative]
        if (not isinstance(expected_hash, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_hash)
                or hashlib.sha256(value).hexdigest() != expected_hash):
            raise ValueError('A verificação do pacote falhou. Nenhum arquivo foi alterado.')
        payload[relative] = value
    return manifest, payload


class UpdateManager:
    def __init__(self, root, current_version, platform, port=8765):
        self.root = Path(root).resolve()
        self.current_version = current_version
        self.platform = platform
        self.port = port
        self.lock = threading.RLock()
        self.nonce = None
        self.challenge_time = 0
        self.release = None
        self.stage = None
        self.rollback = None
        self.operation_token = None
        self.state = {'state': 'idle', 'message': 'Verifique as atualizações públicas do Keep ou importe um pacote de atualização.'}
        try:
            result = json.loads(safe_path(self.root, 'data/update-result.json').read_text(encoding='utf-8'))
            if isinstance(result, dict) and result.get('state') in ('current', 'failed'):
                self.state = {key: result[key] for key in ('state', 'message', 'error', 'latest_version') if key in result}
        except (OSError, ValueError):
            pass

    def snapshot(self):
        with self.lock:
            available = bool(self.release and version_tuple(self.release['version']) > version_tuple(self.current_version))
            return {**({'latest_version': self.release['version']} if self.release else {}),
                    **self.state, 'current_version': self.current_version,
                    'can_apply': self.state['state'] == 'ready',
                    'can_download': available and self.state['state'] in ('available', 'failed'),
                    'update_available': available and self.rollback is None,
                    **({'release_url': self.release['release_url']} if self.release and 'release_url' in self.release else {})}

    def _ensure_not_applying(self):
        if self.state['state'] == 'applying':
            raise ValueError('A atualização já está sendo aplicada. Aguarde o Keep voltar.')

    def _check_origin(self, origin):
        if not re.fullmatch(r'http://(?:127\.0\.0\.1|localhost):' + str(self.port), origin or ''):
            raise ValueError('Abra o Keep no endereço local para verificar a atualização.')

    def begin_check(self, origin):
        self._check_origin(origin)
        with self.lock:
            self._ensure_not_applying()
            if self.state['state'] in ('checking', 'downloading', 'ready'):
                return self.snapshot()
            self.nonce = None
            self.challenge_time = time.monotonic()
            self.operation_token = token = secrets.token_hex(16)
            self.rollback = None
            self.state = {'state': 'checking', 'message': 'Consultando a versão pública mais recente do Keep…'}
            threading.Thread(target=self._public_check, args=(token,), daemon=True).start()
            return self.snapshot()

    def _public_check(self, token):
        try:
            raw = _fetch_public(PUBLIC_MANIFEST_URL, MAX_MANIFEST_BYTES, 'manifest')
            release = inspect_public_release(json.loads(raw), self.platform)
            with self.lock:
                if token != self.operation_token:
                    return
                self.release = release
                if version_tuple(release['version']) > version_tuple(self.current_version):
                    self.state = {'state': 'available', 'latest_version': release['version'],
                                  'message': 'Keep ' + release['version'] + ' disponível. Baixe a atualização para instalar.'}
                else:
                    self.state = {'state': 'current', 'latest_version': release['version'],
                                  'message': 'Você já está com a versão mais recente disponível.'}
        except (ValueError, OSError, URLError, HTTPException, RecursionError) as error:
            self._public_failure(token, error, 'Não foi possível verificar a atualização. Confira sua conexão e tente novamente.')

    def _public_failure(self, token, error, message):
        with self.lock:
            if token == self.operation_token:
                self.state = {'state': 'failed', 'message': message,
                              'error': str(error) if isinstance(error, ValueError) else message}
                if self.release:
                    self.state['latest_version'] = self.release['version']

    def begin_download(self, origin):
        """Download only after an explicit local user action; never during checks."""
        self._check_origin(origin)
        with self.lock:
            self._ensure_not_applying()
            if not self.snapshot()['can_download']:
                raise ValueError('Verifique a atualização antes de baixar o pacote.')
            self.operation_token = token = secrets.token_hex(16)
            release = self.release
            self.state = {'state': 'downloading', 'latest_version': release['version'],
                          'message': 'Baixando e verificando a atualização pública…'}
            threading.Thread(target=self._public_download, args=(token, release), daemon=True).start()
            return self.snapshot()

    def _public_download(self, token, release):
        try:
            package = release['package']
            content = _fetch_public(package['url'], package['size'], 'package', release['version'], self.platform)
            if len(content) != package['size'] or hashlib.sha256(content).hexdigest() != package['sha256']:
                raise ValueError('A verificação do pacote falhou. Nenhum arquivo foi alterado.')
            manifest, _ = inspect_archive(content, self.platform)
            if manifest['version'] != release['version']:
                raise ValueError('A versão do pacote difere da atualização anunciada.')
            with self.lock:
                if token != self.operation_token:
                    return
                self.receive(base64.b64encode(content).decode('ascii'), _public=True)
        except (ValueError, OSError, URLError, HTTPException, RecursionError) as error:
            self._public_failure(token, error, 'Não foi possível baixar a atualização. Tente novamente ou importe um pacote local.')

    def receive(self, package_base64, nonce=None, filename=None, _public=False):
        with self.lock:
            self._ensure_not_applying()
            if nonce is not None:
                raise ValueError('Use a consulta pública de atualização do Keep.')
            if not _public and self.state['state'] in ('checking', 'downloading'):
                raise ValueError('Aguarde a consulta ou o download da atualização terminar.')
            if not isinstance(package_base64, str) or len(package_base64) > ((MAX_ARCHIVE_BYTES + 2) // 3) * 4:
                raise ValueError('Use o pacote pequeno de atualização, com até 4 MB.')
            try:
                content = base64.b64decode(package_base64, validate=True)
            except (ValueError, binascii.Error):
                raise ValueError('O pacote de atualização chegou incompleto. Tente novamente.') from None
            manifest, payload = inspect_archive(content, self.platform)
            if version_tuple(manifest['version']) <= version_tuple(self.current_version):
                raise ValueError('Esse pacote não é mais recente que o Keep instalado.')
            stage = safe_path(self.root, 'data/updates/' + secrets.token_hex(12))
            stage.mkdir(parents=True)
            for relative, value in payload.items():
                destination = safe_path(stage, 'payload/' + relative)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(value)
            (stage / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
            validate_staged(stage, self.platform)
            self.stage = stage
            self.rollback = None
            self.state = {'state': 'ready', 'latest_version': manifest['version'],
                          'message': 'Keep ' + manifest['version'] + ' pronto para instalar. O aplicativo será reiniciado; seus arquivos serão preservados.'}
            self.nonce = None
            self.operation_token = None
            return self.snapshot()

    def restore_points(self):
        with self.lock:
            return list_restore_points(self.root,self.platform,self.current_version)

    def prepare_rollback(self, point_id):
        """Stage an explicit local restore point; imported downgrades stay blocked."""
        with self.lock:
            self._ensure_not_applying()
            if self.state['state'] in ('checking', 'downloading'):
                raise ValueError('Aguarde a consulta ou o download da atualização terminar.')
            point = verify_restore_point(self.root,point_id,self.platform,self.current_version)
            self.operation_token = None
            stage = safe_path(self.root,'data/updates/' + secrets.token_hex(12))
            stage.mkdir(parents=True)
            for relative,expected in point['files'].items():
                source = safe_path(point['path'],relative)
                content = source.read_bytes()
                if hashlib.sha256(content).hexdigest() != expected:
                    raise ValueError('A cópia de segurança mudou durante a preparação. Tente novamente.')
                destination = safe_path(stage,'payload/' + relative)
                destination.parent.mkdir(parents=True,exist_ok=True)
                destination.write_bytes(content)
            manifest = {'version':point['version'],'platform':self.platform,
                        'layout':'payload','files':point['files']}
            (stage/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False),encoding='utf-8')
            validate_staged(stage,self.platform)
            self.stage,self.release,self.nonce = stage,None,None
            self.rollback = {'id':point['id'],'version':point['version'],'from_version':self.current_version}
            self.state = {'state':'ready','latest_version':point['version'],'operation':'rollback',
                          'message':'Keep ' + point['version'] + ' pronto para restaurar. O aplicativo será reiniciado; seus downloads, histórico e preferências atuais serão mantidos.'}
            return self.snapshot()

    def start_apply(self, parent_pid=None, port=None):
        with self.lock:
            if self.state['state'] != 'ready' or self.stage is None:
                raise ValueError('Receba uma atualização antes de instalar.')
            manifest = validate_staged(self.stage, self.platform)
            if self.rollback is not None:
                point = verify_restore_point(self.root,self.rollback['id'],self.platform,self.current_version)
                if point['version'] != manifest['version'] or point['files'] != manifest['files']:
                    raise ValueError('A cópia de segurança mudou. Prepare a restauração novamente.')
            elif version_tuple(manifest['version']) <= version_tuple(self.current_version):
                raise ValueError('Esse pacote não é mais recente. Para voltar, escolha uma cópia na opção de restauração.')
            request = {'root': str(self.root), 'stage': self.stage.relative_to(self.root).as_posix(),
                       'platform': self.platform, 'version': manifest['version'],
                       'parent_pid': parent_pid or os.getpid(), 'port': port or self.port}
            if self.rollback is not None:
                request.update(operation='rollback',restore_point=self.rollback['id'],from_version=self.rollback['from_version'])
            request_path = self.stage / 'request.json'
            request_path.write_text(json.dumps(request), encoding='utf-8')
            log = safe_path(self.root, 'data/update-apply.log')
            flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
            with log.open('ab') as output:
                subprocess.Popen([sys.executable, str(self.root / 'updater_runner.py'), '--request', str(request_path)],
                                 cwd=self.root, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                 creationflags=flags, start_new_session=os.name != 'nt', close_fds=True)
            self.state.update(state='applying', message=('Restaurando a versão anterior.' if self.rollback is not None else 'Aplicando a atualização.') + ' Aguarde o Keep reiniciar automaticamente.')
            return self.snapshot()
