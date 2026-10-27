"""Update a Linux Keep installation, preserving user data and installed tools."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import uuid
from urllib.error import URLError
from urllib.request import urlopen

VERSION = '2.2.0'
APP_FILES = (
    'server.py',
    'app_updates.py',
    'updater_runner.py',
    'preview_media.py',
    'maintenance.py',
    'media_editor.py',
    'desktop_integration.py',
    'launcher.py',
    'dist/index.html',
    'dist/app.js',
    'dist/style.css',
    'dist/icon.svg',
    'dist/update-bridge.html',
    'dist/update-bridge.js',
    'bootstrap_linux.py',
    'LEIA-ME-LINUX.txt',
    'Iniciar Keep.sh',
    'Atualizar mecanismo.sh',
    'Atualizar Keep.sh',
    'update_keep_linux.py',
)
SUPPORTED_VERSIONS = {'1.1', '1.1.0', '1.2', '1.2.0', '1.3', '1.3.0', '1.4', '1.4.0',
                      '1.5', '1.5.0', '1.6', '1.6.0', '1.6.1', '1.6.2', '1.7.0', '1.8.0', '1.9.0', '1.10.0', '1.11.0', '2.0.0', '2.0.1', '2.1.0', '2.1.1', VERSION}
LOCK_FILE = '.keep-linux.lock'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_path(root, relative):
    """Keep every read/write in the selected directory without following links."""
    relative = str(relative)
    part = PurePosixPath(relative)
    if (not relative or part.is_absolute() or '..' in part.parts or '\\' in relative
            or ':' in relative or part.as_posix() != relative or relative == '.'):
        raise ValueError('O pacote contém um caminho inválido. Extraia um pacote original do Keep.')
    path = root.joinpath(*part.parts)
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, RuntimeError):
        raise ValueError('A pasta contém um vínculo para fora do Keep.') from None
    for current in [path, *path.parents]:
        if current == root:
            break
        if current.is_symlink() or getattr(current, 'is_junction', lambda: False)():
            raise ValueError('A pasta contém um vínculo. Use uma pasta normal do Keep.')
    return path


def load_package(package):
    manifest_path = checked_path(package, 'manifest.json')
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if (not isinstance(manifest, dict) or manifest.get('version') != VERSION
            or manifest.get('platform') != 'linux' or manifest.get('layout') not in ('root', 'payload')
            or not isinstance(manifest.get('files'), dict)
            or set(manifest['files']) != set(APP_FILES)):
        raise ValueError('O pacote de atualização está incompleto ou não corresponde ao Keep para Linux.')
    source_root = package if manifest['layout'] == 'root' else checked_path(package, 'payload')
    for relative, expected in manifest['files'].items():
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError('O pacote contém uma verificação de arquivo inválida.')
        source = checked_path(source_root, relative)
        if not source.is_file() or digest(source) != expected:
            raise ValueError('A verificação do pacote falhou. Extraia novamente o pacote original; nada foi alterado.')
    return manifest, source_root


def validate_target(target):
    selected = Path(target).expanduser()
    if selected.is_symlink() or getattr(selected, 'is_junction', lambda: False)():
        raise ValueError('Selecione a pasta real do Keep, sem usar um vínculo.')
    target = selected.resolve()
    for relative in ['server.py', 'dist/index.html']:
        if not checked_path(target, relative).is_file():
            raise ValueError('Selecione a pasta do Keep que contém server.py e a pasta dist.')
    source = checked_path(target, 'server.py').read_text(encoding='utf-8')
    version = re.search(r"^VERSION\s*=\s*['\"](\d+\.\d+(?:\.\d+)?)['\"]", source, re.M)
    if 'Keep:' not in source or not version or version.group(1) not in SUPPORTED_VERSIONS:
        raise ValueError('Esta atualização serve para o Keep 1.1 até 2.2.0. Para outras versões, use o pacote Linux completo.')
    for relative in APP_FILES:
        path = checked_path(target, relative)
        if path.exists() and not path.is_file():
            raise ValueError('Um arquivo do aplicativo foi substituído por uma pasta. Nada foi alterado.')
    return target


def process_uses_target(arguments, cwd, target):
    if not arguments or not re.fullmatch(r'(?:python|pypy)(?:\d+(?:\.\d+)*)?t?', Path(arguments[0]).name, re.I):
        return False
    server = target / 'server.py'
    for argument in arguments[1:]:
        if not argument or argument.startswith('-'):
            continue
        path = Path(argument)
        if path.name != 'server.py':
            continue
        if not path.is_absolute():
            path = cwd / path
        if path.resolve() == server:
            return True
    # Also recognize a manual `python3 -m server` launch from the Keep folder.
    return cwd.resolve() == target and any(
        arguments[index:index + 2] == ['-m', 'server'] for index in range(len(arguments) - 1))


def running_processes(target, proc_root=Path('/proc')):
    """Detect old releases on custom ports without executing or terminating them."""
    found = []
    try:
        processes = list(proc_root.iterdir())
    except OSError:
        raise RuntimeError('Não foi possível verificar os processos. Feche o Keep e atualize em um Linux com /proc disponível.') from None
    for entry in processes:
        if not entry.name.isdecimal() or int(entry.name) == os.getpid():
            continue
        try:
            raw = (entry / 'cmdline').read_bytes()
            arguments = [os.fsdecode(value) for value in raw.split(b'\0') if value]
            if not arguments:
                continue
            cwd = (entry / 'cwd').resolve(strict=True)
            if process_uses_target(arguments, cwd, target):
                found.append(int(entry.name))
        except (OSError, RuntimeError):
            # Processes can exit during enumeration; other users' private processes
            # cannot be this user's Keep installation.
            continue
    return found


def check_not_running(target):
    # The HTTP check covers releases launched before the Linux process lock existed.
    try:
        with urlopen('http://127.0.0.1:8765/api/session', timeout=1) as response:
            session = json.loads(response.read(8192))
        if isinstance(session, dict) and {'token', 'version', 'folder'} <= session.keys():
            raise RuntimeError('O Keep está aberto. Feche o terminal que executa o Keep e tente novamente.')
    except (URLError, TimeoutError, OSError, ValueError):
        pass
    if running_processes(target):
        raise RuntimeError('O Keep dessa pasta ainda está em execução. Feche o terminal do Keep e tente novamente.')


@contextmanager
def installation_lock(target):
    """Serialize updates and prevent a new server from starting midway through one."""
    try:
        import fcntl
    except ImportError:
        raise RuntimeError('Este atualizador deve ser executado no Linux.') from None
    lock_path = checked_path(target, LOCK_FILE)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'a+') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (BlockingIOError, OSError):
            raise RuntimeError('O Keep ou outro atualizador está aberto nessa pasta. Feche-o e tente novamente.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.keep-update-' + uuid.uuid4().hex)
    try:
        shutil.copy2(source, temporary)
        if destination.suffix == '.sh':
            temporary.chmod(0o755)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def apply_update(package, target, running_check=check_not_running):
    package = Path(package).resolve()
    manifest, source_root = load_package(package)
    target = validate_target(target)
    if target == package or target == source_root:
        raise ValueError('Escolha a pasta da versão antiga do Keep. A pasta extraída já contém a versão nova.')
    with installation_lock(target):
        running_check(target)
        backup_relative = 'backups/Keep-antes-' + VERSION + '-Linux-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
        backup = checked_path(target, backup_relative)
        backup.mkdir(parents=True)
        existed = {}
        # Finish and verify all backups before replacing the first application file.
        for relative in APP_FILES:
            destination = checked_path(target, relative)
            existed[relative] = destination.exists()
            if existed[relative]:
                saved = backup / relative
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(destination, saved)
                if digest(saved) != digest(destination):
                    raise RuntimeError('Não foi possível verificar a cópia de segurança. Nada foi atualizado.')
        (backup / 'backup.json').write_text(json.dumps({'files': existed}, indent=2), encoding='utf-8')
        running_check(target)
        changed = []
        try:
            for relative in APP_FILES:
                destination = checked_path(target, relative)
                changed.append(relative)
                atomic_copy(checked_path(source_root, relative), destination)
            for relative, expected in manifest['files'].items():
                if digest(checked_path(target, relative)) != expected:
                    raise RuntimeError('Um arquivo ficou diferente do pacote durante a atualização.')
        except Exception as error:
            failures = []
            for relative in reversed(changed):
                try:
                    destination = checked_path(target, relative)
                    if existed[relative]:
                        # copy2 restores the original mode as well as its contents.
                        shutil.copy2(backup / relative, destination)
                    else:
                        destination.unlink(missing_ok=True)
                except Exception:
                    failures.append(relative)
            if failures:
                raise RuntimeError('A atualização falhou e alguns arquivos não puderam ser restaurados. '
                                   'Não abra o Keep. Restaure os arquivos da cópia de segurança: ' + str(backup)) from error
            raise RuntimeError('A atualização falhou. Os arquivos antigos foram restaurados; seus vídeos e histórico foram preservados. '
                               'Cópia de segurança: ' + str(backup)) from error
    return target, backup


def select_folder():
    try:
        value = input('Cole o caminho da pasta antiga do Keep (Enter cancela): ').strip()
    except EOFError:
        raise RuntimeError('Informe a pasta com --target "/caminho/para/Keep".') from None
    # File managers often copy paths surrounded by quotes. No shell evaluation occurs.
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    return value


def main():
    parser = argparse.ArgumentParser(description='Atualizar Keep no Linux preservando seus arquivos.')
    parser.add_argument('--target', help='Pasta de uma instalação existente do Keep')
    args = parser.parse_args()
    if not sys.platform.startswith('linux'):
        raise RuntimeError('Este pacote é para Linux. No Windows, use Atualizar Keep.bat do pacote Windows.')
    if sys.version_info < (3, 10):
        raise RuntimeError('O Keep precisa de Python 3.10 ou mais recente. Atualize o Python da sua distribuição.')
    print(f'Keep {VERSION} — atualização para Linux\n', flush=True)
    print('Feche o terminal do Keep antes de continuar.\n'
          'Seus downloads, histórico e componentes instalados serão preservados.\n', flush=True)
    target = args.target or select_folder()
    if not target:
        print('Atualização cancelada. Nenhum arquivo foi alterado.', flush=True)
        return 0
    _, backup = apply_update(Path(__file__).resolve().parent, target)
    print(f'\nKeep atualizado para {VERSION}!\nAbra um terminal na pasta antiga e execute: sh "Iniciar Keep.sh"\n'
          'Atualize a aba do navegador com Ctrl+F5.\nCópia de segurança: ' + str(backup), flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print('\nNão foi possível atualizar: ' + str(error), flush=True)
        sys.exit(1)
