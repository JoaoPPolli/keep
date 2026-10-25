"""Apply an already verified application update after the old Keep process exits.

This helper never executes code from the received archive. Only the fixed list of
application files below can be replaced; user files and installed tools stay put.
"""
from contextlib import contextmanager
from datetime import datetime
import argparse
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time
import uuid

COMMON_FILES = (
    'server.py', 'app_updates.py', 'updater_runner.py', 'preview_media.py',
    'dist/index.html', 'dist/app.js', 'dist/style.css', 'dist/icon.svg',
    'dist/update-bridge.html', 'dist/update-bridge.js',
)
PLATFORM_FILES = {
    'windows': COMMON_FILES + (
        'bootstrap.py', 'components.json', 'LEIA-ME.txt', 'update_keep.py',
        'Iniciar Keep.bat', 'Atualizar mecanismo.bat', 'Start Keep.bat', 'Update engine.bat',
    ),
    'linux': COMMON_FILES + (
        'bootstrap_linux.py', 'LEIA-ME-LINUX.txt', 'Iniciar Keep.sh',
        'Atualizar mecanismo.sh', 'Atualizar Keep.sh', 'update_keep_linux.py',
    ),
}

# Reserved application paths let this release receive the following roadmap
# stages without accepting arbitrary files or touching local user data.
OPTIONAL_FILES = frozenset({
    'desktop_integration.py', 'launcher.py', 'media_editor.py', 'maintenance.py',
    'dist/keep.ico', 'dist/keep.png',
})


def package_files(files, platform):
    if platform not in PLATFORM_FILES or not isinstance(files, dict):
        raise ValueError('O pacote não contém todos os arquivos do Keep para este sistema.')
    required = set(PLATFORM_FILES[platform])
    selected = set(files)
    if not required <= selected or selected - required - OPTIONAL_FILES:
        raise ValueError('O pacote contém arquivos incompatíveis com esta instalação.')
    return tuple(files)


class RollbackError(RuntimeError):
    """Some old files could not be restored; starting the app would be unsafe."""


def safe_path(root, relative):
    relative = str(relative)
    part = PurePosixPath(relative)
    if (not relative or part.is_absolute() or '..' in part.parts or '\\' in relative
            or ':' in relative or '\0' in relative or part.as_posix() != relative or relative == '.'):
        raise ValueError('O pacote contém um caminho inválido.')
    path = root.joinpath(*part.parts)
    try:
        path.resolve().relative_to(root.resolve())
    except (ValueError, RuntimeError):
        raise ValueError('A pasta contém um vínculo para fora do Keep.') from None
    for current in (path, *path.parents):
        if current == root:
            break
        if current.is_symlink() or getattr(current, 'is_junction', lambda: False)():
            raise ValueError('A pasta contém um vínculo. Use uma pasta normal do Keep.')
    return path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,4}\.\d{1,4}(?:\.\d{1,4})?', value):
        raise ValueError('A versão informada pelo pacote é inválida.')
    values = tuple(int(piece) for piece in value.split('.'))
    return values + (0,) * (3 - len(values))


def installed_version(root):
    source = safe_path(Path(root), 'server.py')
    if not source.is_file() or source.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('Não foi possível identificar a versão instalada do Keep.')
    try:
        content = source.read_text(encoding='utf-8')
    except (OSError,UnicodeError):
        raise ValueError('Não foi possível ler a identificação da versão do Keep.') from None
    match = re.search(r"^VERSION\s*=\s*['\"]([^'\"]+)['\"]", content, re.M)
    if not match or 'Keep:' not in content:
        raise ValueError('Não foi possível identificar a versão instalada do Keep.')
    version_tuple(match.group(1))
    return match.group(1)


def verify_restore_point(root, point_id, platform, current_version):
    """Validate only fixed application files from a local Keep-owned backup.

    Older backups predate stored hashes. They receive full bounds/path/core
    checks plus a new digest for the staged copy. New backups additionally
    verify every byte against the hash saved when the backup was created.
    """
    root = Path(root).resolve()
    if (not isinstance(point_id, str) or len(point_id) > 180
            or not re.fullmatch(r'Keep-antes-[A-Za-z0-9._ -]+', point_id)):
        raise ValueError('Escolha uma cópia de segurança válida da lista do Keep.')
    backup = safe_path(root, 'backups/' + point_id)
    if not backup.is_dir() or backup.parent != safe_path(root, 'backups'):
        raise ValueError('A cópia de segurança não foi encontrada nesta instalação.')
    descriptor = safe_path(backup, 'backup.json')
    if not descriptor.is_file() or descriptor.stat().st_size > 64 * 1024:
        raise ValueError('A cópia não contém uma descrição válida dos arquivos.')
    try:
        metadata = json.loads(descriptor.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('Não foi possível conferir a descrição da cópia de segurança.') from None
    if not isinstance(metadata, dict) or not isinstance(metadata.get('files'), dict):
        raise ValueError('A cópia não contém uma lista válida dos arquivos do Keep.')
    flags = metadata['files']
    if any(type(value) is not bool for value in flags.values()):
        raise ValueError('A lista de arquivos da cópia de segurança é inválida.')
    package_files(flags, platform)
    if not all(flags[name] for name in PLATFORM_FILES[platform]):
        raise ValueError('Esta cópia é antiga ou incompleta. A restauração exige o núcleo completo do Keep 1.7 ou posterior.')
    version = installed_version(backup)
    if version_tuple(version) < (1,7,0):
        raise ValueError('Esta cópia é anterior ao Keep 1.7 e não permite restauração automática segura.')
    if version_tuple(version) >= version_tuple(current_version):
        raise ValueError('Escolha uma cópia de uma versão anterior à instalada.')
    if metadata.get('version') not in (None, version) or metadata.get('platform') not in (None, platform):
        raise ValueError('A versão ou o sistema da cópia não corresponde aos arquivos.')
    saved_hashes = metadata.get('hashes')
    if saved_hashes is not None and (not isinstance(saved_hashes, dict)
            or set(saved_hashes) != {name for name,value in flags.items() if value}):
        raise ValueError('A cópia contém uma lista de verificações inválida.')
    hashes, total = {}, 0
    for relative, existed in flags.items():
        if not existed:
            continue
        path = safe_path(backup, relative)
        if not path.is_file():
            raise ValueError('Falta um arquivo obrigatório na cópia de segurança.')
        total += path.stat().st_size
        if total > 16 * 1024 * 1024:
            raise ValueError('A cópia de segurança excede o tamanho permitido para os arquivos do aplicativo.')
        value = digest(path)
        if saved_hashes is not None:
            expected = saved_hashes[relative]
            if not isinstance(expected,str) or not re.fullmatch(r'[0-9a-f]{64}',expected) or expected != value:
                raise ValueError('A cópia de segurança foi alterada ou está incompleta. Nenhum arquivo foi restaurado.')
        hashes[relative] = value
    created = metadata.get('created', backup.stat().st_mtime)
    if type(created) not in (int,float) or not math.isfinite(created) or created < 0:
        created = backup.stat().st_mtime
    return {'id':point_id,'version':version,'created':created,'path':backup,'files':hashes,
            'integrity':'verified' if saved_hashes is not None else 'legacy'}


def list_restore_points(root, platform, current_version):
    root = Path(root).resolve()
    backups = safe_path(root, 'backups')
    points, skipped = [], 0
    if backups.is_dir():
        for path in sorted(backups.iterdir(),key=lambda p:p.name,reverse=True)[:256]:
            try:
                point = verify_restore_point(root,path.name,platform,current_version)
                points.append({key:point[key] for key in ('id','version','created','integrity')})
            except (OSError,ValueError,UnicodeError,RuntimeError):
                skipped += 1
    points.sort(key=lambda p:p['created'],reverse=True)
    return {'points':points,'skipped':skipped,
            'message':'Escolha uma versão anterior. Seus downloads, histórico e preferências atuais serão mantidos.'
                if points else 'Não há uma cópia anterior completa compatível. Cópias anteriores ao Keep 1.7 ou incompletas não podem ser restauradas automaticamente.'}


def validate_staged(stage, platform):
    if platform not in PLATFORM_FILES:
        raise ValueError('Este sistema não é compatível com o atualizador.')
    manifest = json.loads(safe_path(stage, 'manifest.json').read_text(encoding='utf-8'))
    version_tuple(manifest.get('version'))
    files = manifest.get('files')
    package_files(files, platform)
    for relative, expected in files.items():
        if not isinstance(expected, str) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError('O pacote contém uma verificação de arquivo inválida.')
        source = safe_path(stage, 'payload/' + relative)
        if not source.is_file() or digest(source) != expected:
            raise ValueError('A verificação do pacote falhou. Seus arquivos não foram alterados.')
    server_source = safe_path(stage, 'payload/server.py').read_text(encoding='utf-8')
    match = re.search(r"^VERSION\s*=\s*['\"]([^'\"]+)['\"]", server_source, re.M)
    if not match or match.group(1) != manifest['version'] or 'Keep:' not in server_source:
        raise ValueError('A versão dos arquivos não corresponde à versão do pacote.')
    return manifest


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


def apply_staged(stage, target, platform):
    stage, target = Path(stage).resolve(), Path(target).resolve()
    manifest = validate_staged(stage, platform)
    if not safe_path(target, 'server.py').is_file():
        raise ValueError('A pasta do Keep não foi encontrada.')
    backup = safe_path(target, 'backups/Keep-antes-' + manifest['version'] + '-'
                       + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8])
    backup.mkdir(parents=True)
    existed, hashes = {}, {}
    selected = set(package_files(manifest['files'], platform))
    selected.update(relative for relative in OPTIONAL_FILES if safe_path(target,relative).exists())
    for relative in sorted(selected):
        destination = safe_path(target, relative)
        if destination.exists() and not destination.is_file():
            raise ValueError('Um arquivo do aplicativo foi substituído por uma pasta. Nada foi alterado.')
        existed[relative] = destination.exists()
        if existed[relative]:
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, saved)
            if digest(saved) != digest(destination):
                raise RuntimeError('Não foi possível verificar a cópia de segurança. Nada foi alterado.')
            hashes[relative] = digest(saved)
    from maintenance import snapshot_preferences_for_restore
    settings_snapshot = snapshot_preferences_for_restore(target,backup)
    (backup / 'backup.json').write_text(json.dumps({'files': existed,'hashes':hashes,
        'version':installed_version(target),'platform':platform,'created':time.time(),
        'settings_snapshot':settings_snapshot},ensure_ascii=False), encoding='utf-8')
    changed = []
    try:
        for relative in package_files(manifest['files'], platform):
            destination = safe_path(target, relative)
            changed.append(relative)
            atomic_copy(safe_path(stage, 'payload/' + relative), destination)
        for relative, expected in manifest['files'].items():
            if digest(safe_path(target, relative)) != expected:
                raise RuntimeError('A conferência dos arquivos instalados falhou.')
    except Exception as error:
        failures = []
        for relative in reversed(changed):
            try:
                destination = safe_path(target, relative)
                if existed[relative]:
                    shutil.copy2(backup / relative, destination)
                else:
                    destination.unlink(missing_ok=True)
            except Exception:
                failures.append(relative)
        if failures:
            raise RollbackError('Falha na restauração. Não abra o Keep; restaure a cópia: ' + str(backup)) from error
        raise RuntimeError('A atualização falhou e os arquivos antigos foram restaurados. Cópia: ' + str(backup)) from error
    return backup


def wait_for_exit(pid, timeout=60):
    if not isinstance(pid, int) or pid <= 1 or pid == os.getpid():
        raise ValueError('O processo do Keep não foi identificado.')
    if os.name == 'nt':
        import ctypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            if ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: PID no longer exists.
                return
            raise RuntimeError('Não foi possível confirmar que o Keep fechou. Nenhum arquivo foi alterado.')
        try:
            if kernel.WaitForSingleObject(handle, int(timeout * 1000)) != 0:
                raise RuntimeError('O Keep ainda está aberto. Nenhum arquivo foi alterado.')
        finally:
            kernel.CloseHandle(handle)
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            raise RuntimeError('Não foi possível confirmar que o Keep fechou.') from None
        time.sleep(0.2)
    raise RuntimeError('O Keep ainda está aberto. Nenhum arquivo foi alterado.')


@contextmanager
def installation_lock(target, platform):
    if platform == 'windows':
        import msvcrt
        lock = safe_path(target, '.keep-app.lock')
        with lock.open('a+b') as handle:
            if not handle.seek(0, os.SEEK_END):
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise RuntimeError('O Keep voltou a abrir antes da atualização. Nenhum arquivo foi alterado.') from None
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl
    lock = safe_path(target, '.keep-linux.lock')
    descriptor = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(descriptor, 'a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise RuntimeError('O Keep voltou a abrir antes da atualização. Nenhum arquivo foi alterado.') from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def write_result(target, result):
    destination = safe_path(target, 'data/update-result.json')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, destination)


def restart_keep(target, port):
    log_path = safe_path(target, 'data/update-restart.log')
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
    with log_path.open('ab') as log:
        subprocess.Popen([sys.executable, str(target / 'server.py'), '--port', str(port)],
                         cwd=target, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         creationflags=flags, start_new_session=os.name != 'nt', close_fds=True)


def run_request(request_path):
    request_path = Path(request_path).resolve()
    request = json.loads(request_path.read_text(encoding='utf-8'))
    target = Path(request['root']).resolve()
    stage = safe_path(target, request['stage'])
    if request_path != stage / 'request.json' or stage.parent != safe_path(target, 'data/updates'):
        raise ValueError('O pedido de atualização não pertence a esta instalação.')
    platform, port = request['platform'], request['port']
    if platform not in PLATFORM_FILES or isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ValueError('O pedido de atualização é inválido.')
    try:
        wait_for_exit(request['parent_pid'])
    except Exception as error:
        write_result(target, {'state': 'failed', 'latest_version': request['version'], 'error': safe_error_message(error),
                             'message': 'O Keep não fechou a tempo. Nenhum arquivo foi alterado.'})
        return
    try:
        with installation_lock(target, platform):
            manifest = validate_staged(stage, platform)
            if manifest['version'] != request['version']:
                raise ValueError('A versão recebida foi alterada depois da conferência.')
            operation = request.get('operation','update')
            if operation == 'rollback':
                current = installed_version(target)
                if current != request.get('from_version'):
                    raise ValueError('A versão instalada mudou. Prepare a restauração novamente.')
                point = verify_restore_point(target,request.get('restore_point'),platform,current)
                if point['version'] != manifest['version'] or point['files'] != manifest['files']:
                    raise ValueError('A cópia de segurança mudou depois da preparação. Nenhum arquivo foi restaurado.')
            elif operation != 'update' or version_tuple(manifest['version']) <= version_tuple(installed_version(target)):
                raise ValueError('Esse pacote não é mais recente. Para voltar, escolha uma cópia na opção de restauração.')
            backup = apply_staged(stage, target, platform)
        write_result(target, {'state': 'current', 'latest_version': request['version'],
                             'message': ('Keep restaurado para ' if operation=='rollback' else 'Keep atualizado para ') + request['version'] + '.', 'backup': str(backup)})
    except Exception as error:
        write_result(target, {'state': 'failed', 'latest_version': request['version'], 'error': safe_error_message(error),
                             'message': 'Não foi possível aplicar a atualização.'})
        if isinstance(error, RollbackError):
            return
    restart_keep(target, port)


def safe_error_message(error):
    if isinstance(error,OSError):
        return 'Não foi possível acessar ou gravar os arquivos do aplicativo. Confira as permissões e o espaço disponível e tente novamente.'
    if isinstance(error,UnicodeError):
        return 'Um arquivo da cópia está inválido ou incompleto. Escolha outra cópia de segurança.'
    return str(error)[:600]


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', required=True)
    arguments = parser.parse_args()
    try:
        run_request(arguments.request)
    except Exception as error:
        print('Não foi possível atualizar o Keep: ' + str(error), file=sys.stderr)
        sys.exit(1)
