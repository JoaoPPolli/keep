"""Apply a Keep release without touching user data or installed runtime/tools."""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import uuid
from urllib.error import URLError
from urllib.request import urlopen

VERSION = '2.3.0'
APP_FILES = (
    'server.py',
    'app_updates.py',
    'updater_runner.py',
    'preview_media.py',
    'maintenance.py',
    'media_editor.py',
    'desktop_integration.py',
    'launcher.py',
    'dist/keep.ico',
    'dist/index.html',
    'dist/app.js',
    'dist/style.css',
    'dist/icon.svg',
    'dist/update-bridge.html',
    'dist/update-bridge.js',
    'bootstrap.py',
    'components.json',
    'LEIA-ME.txt',
    'update_keep.py',
    'Iniciar Keep.bat',
    'Atualizar mecanismo.bat',
    'Start Keep.bat',
    'Update engine.bat',
)
CREATE_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def checked_path(root, relative):
    """Reject traversal, links and junctions before touching any destination."""
    part = PurePosixPath(relative)
    if part.is_absolute() or '..' in part.parts or '\\' in relative or ':' in relative:
        raise ValueError('O pacote contém um caminho inválido. Extraia um ZIP original do Keep.')
    path = root.joinpath(*part.parts)
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        raise ValueError('A pasta contém um atalho ou vínculo para fora do Keep.') from None
    for current in [path, *path.parents]:
        if current == root:
            break
        if current.is_symlink() or current.is_junction():
            raise ValueError('A pasta contém um atalho ou vínculo. Use uma pasta normal do Keep.')
    return path


def load_package(package):
    manifest = json.loads((package / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('version') != VERSION or set(manifest.get('files', {})) != set(APP_FILES):
        raise ValueError('O pacote de atualização está incompleto ou não corresponde a esta versão.')
    for relative, expected in manifest['files'].items():
        source = checked_path(package / 'payload', relative)
        if not source.is_file() or digest(source) != expected:
            raise ValueError('A verificação do pacote falhou. Extraia novamente o ZIP original; nada foi alterado.')
    return manifest


def validate_target(target):
    target = Path(target).resolve()
    for relative in ['server.py', 'Iniciar Keep.bat', 'runtime/python.exe', 'dist/index.html']:
        if not checked_path(target, relative).is_file():
            raise ValueError('Selecione a pasta do Keep que contém Iniciar Keep.bat, server.py e as pastas runtime e dist.')
    source = (target / 'server.py').read_text(encoding='utf-8')
    version = re.search(r"^VERSION\s*=\s*['\"](\d+\.\d+(?:\.\d+)?)['\"]", source, re.M)
    if 'Keep:' not in source or not version or version.group(1) not in ('1.1', '1.1.0', '1.2', '1.2.0', '1.3', '1.3.0', '1.4', '1.4.0', '1.5', '1.5.0', '1.6', '1.6.0', '1.6.1', '1.6.2', '1.7.0', '1.8.0', '1.9.0', '1.10.0', '1.11.0', '2.0.0', '2.0.1', '2.1.0', '2.1.1', '2.2.0', '2.2.1', VERSION):
        raise ValueError('Esta atualização serve para o Keep 1.1 até 2.3.0. Para outras versões, use o pacote portátil completo.')
    for relative in APP_FILES:
        path = checked_path(target, relative)
        if path.exists() and not path.is_file():
            raise ValueError('A pasta escolhida tem um arquivo do aplicativo substituído por uma pasta. Nada foi alterado.')
    return target


def check_not_running(target):
    # The default port also catches old versions launched with a system Python.
    try:
        with urlopen('http://127.0.0.1:8765/api/session', timeout=1) as response:
            session = json.loads(response.read(8192))
        if isinstance(session, dict) and {'token', 'version', 'folder'} <= session.keys():
            raise RuntimeError('O Keep está aberto. Feche a janela do Keep (a janela de comandos, não apenas a aba do navegador) e tente novamente.')
    except (URLError, TimeoutError, OSError, ValueError):
        pass
    if os.name == 'nt':
        env = dict(os.environ, KEEP_UPDATE_TARGET_EXE=str(target / 'runtime' / 'python.exe'), KEEP_UPDATE_PID=str(os.getpid()))
        script = ("$ErrorActionPreference='Stop'; $p=$env:KEEP_UPDATE_TARGET_EXE; "
                  "$found=@(Get-CimInstance Win32_Process | Where-Object { $_.ExecutablePath -eq $p -and $_.ProcessId -ne [int]$env:KEEP_UPDATE_PID }); "
                  "if ($found.Count -gt 0) { exit 2 }")
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command', script],
                                capture_output=True, timeout=20, env=env, creationflags=CREATE_FLAGS)
        if result.returncode == 2:
            raise RuntimeError('O Keep dessa pasta ainda está em execução. Feche a janela de comandos do Keep e tente novamente.')
        if result.returncode:
            raise RuntimeError('Não foi possível confirmar se o Keep está fechado. Reinicie o Windows e tente atualizar antes de abrir o Keep.')


def atomic_copy(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + '.keep-update-' + uuid.uuid4().hex)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def apply_update(package, target, running_check=check_not_running):
    package = Path(package).resolve()
    manifest = load_package(package)
    target = validate_target(target)
    running_check(target)
    backup_relative = 'backups/Keep-antes-' + VERSION + '-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:8]
    backup = checked_path(target, backup_relative)
    backup.mkdir(parents=True)
    existed = {}
    # Complete the backup before replacing the first application file.
    for relative in APP_FILES:
        destination = checked_path(target, relative)
        existed[relative] = destination.exists()
        if existed[relative]:
            saved = backup / relative
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(destination, saved)
    (backup / 'backup.json').write_text(json.dumps({'files': existed}, indent=2), encoding='utf-8')
    running_check(target)
    changed = []
    try:
        for relative in APP_FILES:
            destination = checked_path(target, relative)
            atomic_copy(package / 'payload' / relative, destination)
            changed.append(relative)
        for relative, expected in manifest['files'].items():
            if digest(checked_path(target, relative)) != expected:
                raise RuntimeError('Um arquivo ficou diferente do pacote durante a atualização.')
    except Exception as error:
        failures = []
        for relative in reversed(changed):
            try:
                destination = checked_path(target, relative)
                if existed[relative]:
                    shutil.copy2(backup / relative, destination)
                else:
                    destination.unlink(missing_ok=True)
            except Exception:
                failures.append(relative)
        if failures:
            raise RuntimeError('A atualização falhou e alguns arquivos não puderam ser restaurados. '
                               'Não abra o Keep. Copie os arquivos da cópia de segurança para a pasta do Keep: '
                               + str(backup)) from error
        raise RuntimeError('A atualização falhou. Os arquivos alterados foram restaurados; seus vídeos e histórico foram preservados. '
                           'Feche o Keep e tente novamente. Cópia de segurança: ' + str(backup)) from error
    return target, backup


def select_folder():
    script = ("Add-Type -AssemblyName System.Windows.Forms; "
              "$d=New-Object System.Windows.Forms.FolderBrowserDialog; "
              "$d.Description='Selecione a pasta do Keep que voce ja usa (onde esta Iniciar Keep.bat)'; "
              "$d.ShowNewFolderButton=$false; "
              "if ($d.ShowDialog() -eq 'OK') { [Console]::OutputEncoding=[Text.Encoding]::UTF8; "
              "[Console]::Write($d.SelectedPath) }")
    result = subprocess.run(['powershell.exe', '-NoProfile', '-STA', '-Command', script],
                            capture_output=True, encoding='utf-8', creationflags=CREATE_FLAGS)
    if result.returncode:
        raise RuntimeError('Não foi possível abrir a seleção de pasta. Extraia o pacote em uma pasta local e tente novamente.')
    return result.stdout.strip().lstrip('\ufeff')


def main():
    parser = argparse.ArgumentParser(description='Atualizar Keep preservando seus arquivos.')
    parser.add_argument('--target', help='Pasta de uma instalação existente do Keep')
    args = parser.parse_args()
    print(f'Keep {VERSION} — atualização para Windows\n', flush=True)
    print('Feche a janela de comandos do Keep antes de continuar.\n'
          'Seus downloads, histórico e componentes instalados serão preservados.\n', flush=True)
    target = args.target or select_folder()
    if not target:
        print('Atualização cancelada. Nenhum arquivo foi alterado.', flush=True)
        return 0
    _, backup = apply_update(Path(__file__).resolve().parent, target)
    print(f'\nKeep atualizado para {VERSION}!\nAbra Iniciar Keep.bat na mesma pasta que você já usava.\n'
          'Atualize a aba do navegador com Ctrl+F5.\nCópia de segurança: ' + str(backup), flush=True)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print('\nNão foi possível atualizar: ' + str(error), flush=True)
        sys.exit(1)
