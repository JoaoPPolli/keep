"""Prepare verified Linux components in Keep's own folder; never install system packages."""
import hashlib
import json
from pathlib import Path
import platform
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / 'tools'
LICENSES = ROOT / 'licenses'
VERSION = '2.4.0'
ENGINE = {
    'url': 'https://github.com/yt-dlp/yt-dlp-nightly-builds/releases/download/2026.10.10.233141/yt-dlp',
    'sha256': 'f40b6a9e4c5786360953911cab5d2f87507626b8fb7bfee3f8f1c2e5cf341fae',
}
NODE_VERSION = '22.20.0'
NODE = {
    'x64': 'eeaccb0378b79406f2208e8b37a62479c70595e20be6b659125eb77dd1ab2a29',
    'arm64': '4181609e03dcb9880e7e5bf956061ecc0503c77a480c6631d868cb1f65a2c7dd',
}
DEPENDENCY_HELP = (
    'Instale Python 3.10 ou mais recente, FFmpeg e certificados HTTPS pelo gerenciador da sua distribuição.\n'
    'Ubuntu / Debian / Mint: sudo apt install python3 ffmpeg ca-certificates\n'
    'Fedora: sudo dnf install python3 ffmpeg-free ca-certificates\n'
    'Arch / Manjaro: sudo pacman -S python ffmpeg ca-certificates\n'
    'O Keep não executa esses comandos nem pede a senha de administrador.'
)


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_verified(url, target, expected):
    """Download next to the destination and replace only after validating the whole file."""
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix='.keep-download-', dir=target.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        request = urllib.request.Request(url, headers={'User-Agent': 'Keep/' + VERSION})
        with urllib.request.urlopen(request, timeout=90) as response, temporary.open('wb') as output:
            shutil.copyfileobj(response, output, length=1024 * 1024)
        if sha256(temporary) != expected:
            raise RuntimeError('A verificação SHA256 falhou. O componente existente foi preservado.')
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)


def architecture():
    machine = platform.machine().lower()
    if machine in {'x86_64', 'amd64'}:
        return 'x64'
    if machine in {'aarch64', 'arm64'}:
        return 'arm64'
    raise RuntimeError('Esta edição suporta Linux de 64 bits x86_64 ou ARM64. Arquitetura encontrada: ' + machine)


def check_system():
    if platform.system() != 'Linux':
        raise RuntimeError('Use esta edição em Linux. No Windows, use Iniciar Keep.bat.')
    if sys.version_info < (3, 10):
        raise RuntimeError('É necessário Python 3.10 ou mais recente.\n' + DEPENDENCY_HELP)
    architecture()
    missing = [name for name in ('ffmpeg', 'ffprobe') if not shutil.which(name)]
    if missing:
        raise RuntimeError('Falta instalar ' + ' e '.join(missing) + '.\n' + DEPENDENCY_HELP)
    for executable in ('ffmpeg', 'ffprobe'):
        try:
            result = subprocess.run([executable, '-version'], capture_output=True, timeout=15)
            if result.returncode:
                raise OSError(executable + ' retornou um erro')
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError('Não foi possível executar ' + executable + '.\n' + DEPENDENCY_HELP) from error


def prepare_engine(component=None, force=False):
    component = dict(component or ENGINE)
    target = TOOLS / 'yt-dlp'
    stamp = TOOLS / '.linux-engine.json'
    expected = ENGINE['sha256']
    if not force:
        try:
            saved = json.loads(stamp.read_text(encoding='utf-8'))
            if isinstance(saved.get('sha256'), str) and len(saved['sha256']) == 64:
                expected = saved['sha256']
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        if target.is_file() and sha256(target) == expected:
            target.chmod(0o755)
            return
    print('Preparando o mecanismo de download para Linux...', flush=True)
    fetch_verified(component['url'], target, component['sha256'])
    target.chmod(0o755)
    stamp.write_text(json.dumps(component, ensure_ascii=False), encoding='utf-8')


def node_works(path):
    try:
        result = subprocess.run([str(path), '--version'], capture_output=True, text=True, timeout=15)
        major = int(result.stdout.strip().lstrip('v').split('.')[0])
        return result.returncode == 0 and major >= 22
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def prepare_node():
    target = TOOLS / 'node'
    if target.is_file():
        target.chmod(0o755)
        if node_works(target):
            return
    # An already installed supported runtime also works, including musl distributions.
    system_node = shutil.which('node')
    if system_node and node_works(system_node):
        if target.exists():
            backup = TOOLS / ('node.incompativel-' + secrets.token_hex(8))
            target.replace(backup)
        return
    libc, libc_version = platform.libc_ver()
    if libc != 'glibc':
        raise RuntimeError('Este Linux não usa glibc. Instale Node.js 22 ou mais recente pelo gerenciador da distribuição e abra o Keep novamente.')
    try:
        libc_parts = tuple(int(part) for part in libc_version.split('.')[:2])
    except ValueError:
        libc_parts = (0, 0)
    if libc_parts < (2, 28):
        raise RuntimeError('O Node incluído precisa de glibc 2.28 ou mais recente. Atualize a distribuição ou instale um Node.js 22 compatível e abra novamente.')
    arch = architecture()
    prefix = 'node-v' + NODE_VERSION + '-linux-' + arch
    archive_path = TOOLS / '.node-linux.tar.gz'
    ready = TOOLS / '.node-linux.ready'
    try:
        print('Preparando o runtime de vídeo para Linux ' + arch + '...', flush=True)
        fetch_verified('https://nodejs.org/dist/v' + NODE_VERSION + '/' + prefix + '.tar.gz', archive_path, NODE[arch])
        # Read only the exact binary and license; never extract archive paths or links.
        with tarfile.open(archive_path, 'r:gz') as archive:
            for name, destination in ((prefix + '/bin/node', ready),
                                      (prefix + '/LICENSE', LICENSES / 'NODE-PACKAGE-LICENSE.txt')):
                member = archive.getmember(name)
                if not member.isfile():
                    raise RuntimeError('O pacote oficial de Node não contém o arquivo esperado.')
                with archive.extractfile(member) as source, destination.open('wb') as output:
                    shutil.copyfileobj(source, output)
        ready.chmod(0o755)
        if not node_works(ready):
            raise RuntimeError('O Node oficial não executou neste Linux. Instale Node.js 22 ou mais recente pelo gerenciador da distribuição e tente novamente.')
        ready.replace(target)
    finally:
        archive_path.unlink(missing_ok=True)
        ready.unlink(missing_ok=True)


def latest_engine():
    request = urllib.request.Request('https://api.github.com/repos/yt-dlp/yt-dlp-nightly-builds/releases/latest',
                                     headers={'User-Agent': 'Keep/' + VERSION})
    with urllib.request.urlopen(request, timeout=30) as response:
        release = json.load(response)
    asset = next(item for item in release['assets'] if item['name'] == 'yt-dlp')
    digest = asset.get('digest', '')
    if not isinstance(digest, str) or len(digest) != 71 or not digest.startswith('sha256:'):
        raise RuntimeError('A versão oficial não forneceu um SHA256. Atualização cancelada.')
    return release['tag_name'], {'url': asset['browser_download_url'], 'sha256': digest[7:]}


def main():
    check_system()
    from update_keep_linux import installation_lock, check_not_running
    with installation_lock(ROOT):
        check_not_running(ROOT)
        TOOLS.mkdir(exist_ok=True)
        LICENSES.mkdir(exist_ok=True)
        if '--update' in sys.argv:
            tag, component = latest_engine()
            prepare_engine(component, force=True)
            print('Mecanismo atualizado: ' + tag, flush=True)
        else:
            prepare_engine()
        prepare_node()
    print('Componentes do Keep para Linux prontos.', flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Não foi possível preparar o Keep: ' + str(error), flush=True)
        print('Confira também LEIA-ME-LINUX.txt. Seus downloads não foram alterados.', flush=True)
        sys.exit(1)
