"""Opt-in desktop shortcuts, URI handling and native completion notices.

Registration is per-user. A URI can only launch Keep; it can never carry a
download URL, a shell command, a file path or options for the server.
"""
import base64
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

PROTOCOL = 'keep://launch'
REGISTRY_PATH = r'Software\Classes\keep'
DESKTOP_ID = 'keep.desktop'
PROTOCOL_DESKTOP_ID = 'keep-protocol.desktop'
OWNER = 'Keep.Application'

# BEGIN EMBEDDED WINDOWS LAUNCHER
WINDOWS_LAUNCHER_SHA256 = 'a1f44aa907273524e395ca6c7ec7510acb8f455970fc34d1be3fafe021e336e1'
WINDOWS_LAUNCHER_BASE64 = (
    'TVqQAAMAAAAEAAAA//8AALgAAAAAAAAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAAA4fug4AtAnNIbgB'
    'TM0hVGhpcyBwcm9ncmFtIGNhbm5vdCBiZSBydW4gaW4gRE9TIG1vZGUuDQ0KJAAAAAAAAABQRQAATAEDAKw03GoAAAAAAAAAAOAA'
    'AgELAQsAAAoAAAAWAAAAAAAADigAAAAgAAAAQAAAAABAAAAgAAAAAgAABAAAAAAAAAAEAAAAAAAAAACAAAAAAgAAAAAAAAIAQIUA'
    'ABAAABAAAAAAEAAAEAAAAAAAABAAAAAAAAAAAAAAALQnAABXAAAAAEAAAGASAAAAAAAAAAAAAAAAAAAAAAAAAGAAAAwAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAIAAACAAAAAAAAAAAAAAACCAAAEgAAAAAAAAAAAAA'
    'AC50ZXh0AAAAFAgAAAAgAAAACgAAAAIAAAAAAAAAAAAAAAAAACAAAGAucnNyYwAAAGASAAAAQAAAABQAAAAMAAAAAAAAAAAAAAAA'
    'AABAAABALnJlbG9jAAAMAAAAAGAAAAACAAAAIAAAAAAAAAAAAAAAAAAAQAAAQgAAAAAAAAAAAAAAAAAAAADwJwAAAAAAAEgAAAAC'
    'AAUAYCEAAFQGAAABAAAAAQAABgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABswBgDyAAAA'
    'AQAAEQKOaRcwJAKOaRczIAIWmnIBAABwKAQAAAosEQIWmnIdAABwKAQAAAosAhgqKAUAAApvBgAACgoGcjsAAHBySwAAcCgHAAAK'
    'CwZyYwAAcCgIAAAKDAcoCQAACiwICCgJAAAKLRVyewAAcHL5AABwFh8QKAoAAAomFyoHcwsAAAoNCXIDAQBwCHIXAQBwAo5pFy4H'
    'chsBAHArBXIdAQBwKAwAAApvDQAACgkGbw4AAAoJFm8PAAAKCRdvEAAACgkoEQAACiYWEwXeJhMEcjsBAHARBG8SAAAKKBMAAApy'
    '+QAAcBYfECgKAAAKJhcTBd4AEQUqAAABEAAAAAB5AFDJACYPAAABQlNKQgEAAQAAAAAADAAAAHY0LjAuMzAzMTkAAAAABQBsAAAA'
    'sAEAACN+AAAcAgAAEAIAACNTdHJpbmdzAAAAACwEAACAAQAAI1VTAKwFAAAQAAAAI0dVSUQAAAC8BQAAmAAAACNCbG9iAAAAAAAA'
    'AAIAAAFHFQIACQAAAAD6JTMAFgAAAQAAAA8AAAACAAAAAQAAAAEAAAATAAAAAwAAAAEAAAABAAAAAwAAAAAACgABAAAAAAAGADAA'
    'KQAGAGEAQQAGAIcAQQAGAKoAKQAGAL0AKQAGANIAKQAGAAoBAAEGABcBAAEKADgBIwEKAEMBIwEKAFABIwEKAGIBIwEOAIkBdgEO'
    'AOsBdgEGAPkBKQAAAAAAAQAAAAAAAQABAIABEAATAAAABQABAAEAUCAAAAAAkQA3AAoAAQAAAAEAPAARAIEAEAAZAIEAFQAhAIEA'
    'FQApAMQAHgAxANwAJAAxAO4AKQA5AA8BLQA5AA8BNABBABwBOgBJAHEBPwBpAIEASgApAJoBTwBpAKEBSgBpAK8BSgBpAMQBVwBp'
    'ANgBVwBxAPMBXAB5AAMCKQApAJoBNAAgABsAGQAuAAsAbgAuABMAdwBjAASAAAAAAAAAAAAAAAAAAAAAAKUAAAAEAAAAAAAAAAAA'
    'AAABACAAAAAAAAQAAAAAAAAAAAAAAAEAIwEAAAAABAAAAAAAAAAAAAAAAQApAAAAAAAAAAAAADxNb2R1bGU+AEtlZXAuZXhlAEtl'
    'ZXBMYXVuY2hlcgBtc2NvcmxpYgBTeXN0ZW0AT2JqZWN0AE1haW4AYXJncwBTeXN0ZW0uUnVudGltZS5Db21waWxlclNlcnZpY2Vz'
    'AENvbXBpbGF0aW9uUmVsYXhhdGlvbnNBdHRyaWJ1dGUALmN0b3IAUnVudGltZUNvbXBhdGliaWxpdHlBdHRyaWJ1dGUAS2VlcABT'
    'VEFUaHJlYWRBdHRyaWJ1dGUAU3RyaW5nAG9wX0luZXF1YWxpdHkAQXBwRG9tYWluAGdldF9DdXJyZW50RG9tYWluAGdldF9CYXNl'
    'RGlyZWN0b3J5AFN5c3RlbS5JTwBQYXRoAENvbWJpbmUARmlsZQBFeGlzdHMAU3lzdGVtLldpbmRvd3MuRm9ybXMATWVzc2FnZUJv'
    'eABEaWFsb2dSZXN1bHQATWVzc2FnZUJveEJ1dHRvbnMATWVzc2FnZUJveEljb24AU2hvdwBTeXN0ZW0uRGlhZ25vc3RpY3MAUHJv'
    'Y2Vzc1N0YXJ0SW5mbwBDb25jYXQAc2V0X0FyZ3VtZW50cwBzZXRfV29ya2luZ0RpcmVjdG9yeQBzZXRfVXNlU2hlbGxFeGVjdXRl'
    'AHNldF9DcmVhdGVOb1dpbmRvdwBQcm9jZXNzAFN0YXJ0AEV4Y2VwdGlvbgBnZXRfTWVzc2FnZQAAABtrAGUAZQBwADoALwAvAGwA'
    'YQB1AG4AYwBoAAAdawBlAGUAcAA6AC8ALwBsAGEAdQBuAGMAaAAvAAAPcgB1AG4AdABpAG0AZQAAF3AAeQB0AGgAbwBuAHcALgBl'
    'AHgAZQAAF2wAYQB1AG4AYwBoAGUAcgAuAHAAeQAAfUUAeAB0AHIAYQBpAGEAIABvACAAcABhAGMAbwB0AGUAIABjAG8AbQBwAGwA'
    'ZQB0AG8AIABkAG8AIABLAGUAZQBwACAAYQBuAHQAZQBzACAAZABlACAAYQBiAHIAaQByACAAbwAgAGEAcABsAGkAYwBhAHQAaQB2'
    'AG8ALgAACUsAZQBlAHAAABMtAFgAIAB1AHQAZgA4ACAAIgABAyIAAAEAHSAAawBlAGUAcAA6AC8ALwBsAGEAdQBuAGMAaAAAQ04A'
    '4wBvACAAZgBvAGkAIABwAG8AcwBzAO0AdgBlAGwAIABpAG4AaQBjAGkAYQByACAAbwAgAEsAZQBlAHAAOgAgAAEA8I33Ays+TkS9'
    'ncRdLbzRVAAIt3pcVhk04IkFAAEIHQ4EIAEBCAMgAAEEAQAAAAUAAgIODgQAABIZAyAADgYAAw4ODg4FAAIODg4EAAECDgoABBEp'
    'Dg4RLRExBCABAQ4HAAQODg4ODgQgAQECBgABEjkSNQoHBg4ODhI1Ej0ICAEACAAAAAAAHgEAAQBUAhZXcmFwTm9uRXhjZXB0aW9u'
    'VGhyb3dzAQAA3CcAAAAAAAAAAAAA/icAAAAgAAAAAAAAAAAAAAAAAAAAAAAAAAAAAPAnAAAAAAAAAAAAAAAAAAAAAAAAAABfQ29y'
    'RXhlTWFpbgBtc2NvcmVlLmRsbAAAAAAA/yUAIEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAEAAMAAAAwAACADgAAAGAAAIAQAAAAeAAAgBgAAACQAACAAAAAAAAAAAAAAAAAAAAEAAIAAACoAACAAwAAAMAA'
    'AIAEAAAA2AAAgAUAAADwAACAAAAAAAAAAAAAAAAAAAABAAB/AAAIAQCAAAAAAAAAAAAAAAAAAAABAAEAAAAgAQCAAAAAAAAAAAAA'
    'AAAAAAABAAEAAAA4AQCAAAAAAAAAAAAAAAAAAAABAAAAAABQAQAAAAAAAAAAAAAAAAAAAAABAAAAAABgAQAAAAAAAAAAAAAAAAAA'
    'AAABAAAAAABwAQAAAAAAAAAAAAAAAAAAAAABAAAAAACAAQAAAAAAAAAAAAAAAAAAAAABAAAAAACQAQAAAAAAAAAAAAAAAAAAAAAB'
    'AAAAAACgAQAAAAAAAAAAAAAAAAAAAAABAAAAAACwAQAAAEQAACgBAAAAAAAAAAAAAChFAACGAQAAAAAAAAAAAACwRgAAzgEAAAAA'
    'AAAAAAAAgEgAAKkHAAAAAAAAAAAAADBQAAA+AAAAAAAAAAAAAADAQQAAPAIAAAAAAAAAAAAAcFAAAOoBAAAAAAAAAAAAADwCNAAA'
    'AFYAUwBfAFYARQBSAFMASQBPAE4AXwBJAE4ARgBPAAAAAAC9BO/+AAABAAAAAAAAAAAAAAAAAAAAAAA/AAAAAAAAAAQAAAABAAAA'
    'AAAAAAAAAAAAAAAARAAAAAEAVgBhAHIARgBpAGwAZQBJAG4AZgBvAAAAAAAkAAQAAABUAHIAYQBuAHMAbABhAHQAaQBvAG4AAAAA'
    'AAAAsAScAQAAAQBTAHQAcgBpAG4AZwBGAGkAbABlAEkAbgBmAG8AAAB4AQAAAQAwADAAMAAwADAANABiADAAAAAsAAIAAQBGAGkA'
    'bABlAEQAZQBzAGMAcgBpAHAAdABpAG8AbgAAAAAAIAAAADAACAABAEYAaQBsAGUAVgBlAHIAcwBpAG8AbgAAAAAAMAAuADAALgAw'
    'AC4AMAAAADQACQABAEkAbgB0AGUAcgBuAGEAbABOAGEAbQBlAAAASwBlAGUAcAAuAGUAeABlAAAAAAAoAAIAAQBMAGUAZwBhAGwA'
    'QwBvAHAAeQByAGkAZwBoAHQAAAAgAAAAPAAJAAEATwByAGkAZwBpAG4AYQBsAEYAaQBsAGUAbgBhAG0AZQAAAEsAZQBlAHAALgBl'
    'AHgAZQAAAAAANAAIAAEAUAByAG8AZAB1AGMAdABWAGUAcgBzAGkAbwBuAAAAMAAuADAALgAwAC4AMAAAADgACAABAEEAcwBzAGUA'
    'bQBiAGwAeQAgAFYAZQByAHMAaQBvAG4AAAAwAC4AMAAuADAALgAwAAAAAAAAAIlQTkcNChoKAAAADUlIRFIAAAAgAAAAIAgGAAAA'
    'c3p69AAAAO9JREFUeNpjYMACTO3kHLJqbBqu/qj8Tw0MMgtkJgMhAFK0YFf0fmpZjI5BZuN0CEiCVhajYwxH0NNyrI6gZbDji44B'
    '8z1KKFCS2t3CDMCYktzBQIkPxBUkwJgSM0YdMOqAUQcQ5YBlhxLBFoFoQg7ApZYqDkA3GN0BuNRRJQqwGY7sAHIsJzkNoFuCi03T'
    'RIhsGTom1XKycwE2R5BjOUEHgAzFZTAxwY5PP1EOIJTPCVlATDnBQMuCZvg4oKzHgyxMlAPwNclwZTdSMd4mGb5GKSiBket7GMaX'
    'SOFN8wFtlg+KjsmAd80GRed0oLrnAFygYsb6m8ZgAAAAAElFTkSuQmCCiVBORw0KGgoAAAANSUhEUgAAADAAAAAwCAYAAABXAvmH'
    'AAABTUlEQVR42u2asQ3CMBBFbwuoECPQ0CFEhZiAhoqWAZCQKBiAIagYggaJHdiFDuRIRiFKHDu+79jmIn0pRQL/5Rznzmcii2N3'
    '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'
    'AElFTkSuQmCCAAAAAAAAAAAAAQAEACAgAAABACAAKAEAAAIAMDAAAAEAIACGAQAAAwBAQAAAAQAgAM4BAAAEAAAAAAABACAAqQcA'
    'AAUAAADvu788P3htbCB2ZXJzaW9uPSIxLjAiIGVuY29kaW5nPSJVVEYtOCIgc3RhbmRhbG9uZT0ieWVzIj8+DQo8YXNzZW1ibHkg'
    'eG1sbnM9InVybjpzY2hlbWFzLW1pY3Jvc29mdC1jb206YXNtLnYxIiBtYW5pZmVzdFZlcnNpb249IjEuMCI+DQogIDxhc3NlbWJs'
    'eUlkZW50aXR5IHZlcnNpb249IjEuMC4wLjAiIG5hbWU9Ik15QXBwbGljYXRpb24uYXBwIi8+DQogIDx0cnVzdEluZm8geG1sbnM9'
    'InVybjpzY2hlbWFzLW1pY3Jvc29mdC1jb206YXNtLnYyIj4NCiAgICA8c2VjdXJpdHk+DQogICAgICA8cmVxdWVzdGVkUHJpdmls'
    'ZWdlcyB4bWxucz0idXJuOnNjaGVtYXMtbWljcm9zb2Z0LWNvbTphc20udjMiPg0KICAgICAgICA8cmVxdWVzdGVkRXhlY3V0aW9u'
    'TGV2ZWwgbGV2ZWw9ImFzSW52b2tlciIgdWlBY2Nlc3M9ImZhbHNlIi8+DQogICAgICA8L3JlcXVlc3RlZFByaXZpbGVnZXM+DQog'
    'ICAgPC9zZWN1cml0eT4NCiAgPC90cnVzdEluZm8+DQo8L2Fzc2VtYmx5Pg0KAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAIAAADAAAABA4AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'
    'AAAAAA=='
)
# END EMBEDDED WINDOWS LAUNCHER
WINDOWS_LAUNCHER_PREVIOUS_SHA256 = frozenset()


def _launcher_digest(path):
    if path.is_symlink() or (hasattr(path, 'is_junction') and path.is_junction()):
        raise ValueError('Keep.exe é um vínculo. O arquivo existente foi preservado.')
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > 128 * 1024:
        raise ValueError('Já existe um Keep.exe não reconhecido nesta pasta. O arquivo foi preservado.')
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ensure_windows_launcher(root):
    """Materialize only our hash-pinned executable, on explicit user action.

    The compact updater does not need permission to deliver an executable.
    All bytes and the expected hash are part of this reviewed application
    module. Unknown existing files are never replaced.
    """
    root = _root(root)
    path = root / 'Keep.exe'
    _checked_parent(path)
    if not (root / 'runtime' / 'pythonw.exe').is_file():
        raise ValueError('O runtime completo do Keep para Windows precisa incluir pythonw.exe. Extraia novamente o pacote portátil.')
    data = base64.b64decode(WINDOWS_LAUNCHER_BASE64, validate=True)
    if (not data.startswith(b'MZ') or len(data) > 128 * 1024
            or hashlib.sha256(data).hexdigest() != WINDOWS_LAUNCHER_SHA256):
        raise ValueError('A verificação do inicializador falhou. Reinstale o pacote original do Keep.')
    existing = _launcher_digest(path)
    if existing == WINDOWS_LAUNCHER_SHA256:
        return path
    if existing is not None and existing not in WINDOWS_LAUNCHER_PREVIOUS_SHA256:
        raise ValueError('Já existe um Keep.exe não reconhecido nesta pasta. O arquivo foi preservado.')
    with tempfile.NamedTemporaryFile(dir=root, prefix='.keep-launcher-', suffix='.tmp', delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != WINDOWS_LAUNCHER_SHA256:
            raise ValueError('A gravação do inicializador falhou. Tente novamente.')
        if _launcher_digest(path) != existing:
            raise ValueError('Keep.exe foi alterado durante a instalação. O arquivo existente foi preservado.')
        if existing is None:
            # Windows rename refuses to replace an existing destination and
            # also works on portable FAT/exFAT drives. POSIX uses an atomic
            # hard link with the same no-overwrite property in host tests.
            try:
                if os.name == 'nt':
                    temporary.rename(path)
                else:
                    os.link(temporary, path)
            except FileExistsError:
                raise ValueError('Keep.exe foi criado por outro processo. O arquivo existente foi preservado.') from None
        else:
            temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _platform(value=None):
    value = value or sys.platform
    return 'windows' if value in ('windows', 'win32') else 'linux' if value.startswith('linux') else value


def _root(value):
    root = Path(value).resolve()
    if any(ord(char) < 32 for char in str(root)):
        raise ValueError('O caminho do Keep contém caracteres incompatíveis com o inicializador.')
    if not (root / 'launcher.py').is_file() or not (root / 'server.py').is_file():
        raise ValueError('Extraia o pacote completo do Keep antes de criar o inicializador.')
    return root


def _environment(env=None):
    return dict(os.environ if env is None else env)


def _powershell(script, env, *, wait=True):
    # Values travel in a child-only environment. No path/title/URI is inserted
    # into PowerShell source, and a shell is never used.
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded]
    options = {'env': env, 'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)}
    if wait:
        return subprocess.run(command, capture_output=True, timeout=20, **options)
    return subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **options)


def _windows_paths(root, env):
    appdata = env.get('APPDATA')
    if not appdata or not Path(appdata).is_absolute():
        raise ValueError('O Windows não informou a pasta de aplicativos deste usuário.')
    shortcut = Path(appdata) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs' / 'Keep.lnk'
    executable = root / 'Keep.exe'
    if executable.is_file():
        return shortcut, executable, []
    python = root / 'runtime' / 'pythonw.exe'
    if not python.is_file():
        raise ValueError('O inicializador precisa do runtime completo do Keep para Windows.')
    return shortcut, python, ['-X', 'utf8', str(root / 'launcher.py')]


def _linux_applications(env):
    configured = env.get('XDG_DATA_HOME', '')
    home = env.get('HOME') or str(Path.home())
    data = Path(configured) if configured and Path(configured).is_absolute() else Path(home) / '.local' / 'share'
    return data / 'applications'


def _checked_parent(path):
    # Do not write through redirected application-menu directories.
    for candidate in [path, *path.parents]:
        if candidate.is_symlink() or (hasattr(candidate, 'is_junction') and candidate.is_junction()):
            raise ValueError('O destino do inicializador contém um vínculo. Escolha uma instalação sem redirecionamento.')
    path.parent.mkdir(parents=True, exist_ok=True)


def _write_owned(path, text):
    _checked_parent(path)
    if path.exists() and not _owned_desktop_entry(path.read_text(encoding='utf-8')):
        raise ValueError('Já existe outro aplicativo com esse nome no menu. O arquivo existente foi preservado.')
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='\n', dir=path.parent,
                                     prefix='.keep-desktop-', delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(text)
    try:
        temporary.chmod(0o644)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _desktop_string(value):
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t')


def _owned_desktop_entry(content, root=None):
    # A root that is merely a prefix of another installation is not its owner.
    lines = set(content.splitlines())
    return ('X-Keep-Owner=' + OWNER in lines
            and (root is None or 'X-Keep-Root=' + _desktop_string(root) in lines))


def _desktop_argument(value):
    value = str(value)
    if any(ord(char) < 32 for char in value):
        raise ValueError('Caminho inválido para o inicializador.')
    value = value.replace('%', '%%')
    for char in ('\\', '"', '`', '$'):
        value = value.replace(char, '\\' + char)
    return _desktop_string('"' + value + '"')


def _desktop_content(root, protocol=False):
    # Reuse the interpreter that is already running Keep successfully. The
    # generic python3 on PATH may be older than this installation requires.
    python = sys.executable or shutil.which('python3')
    command = ' '.join(_desktop_argument(part) for part in [python, '-X', 'utf8', root / 'launcher.py'])
    if protocol:
        command += ' %u'
    return ('[Desktop Entry]\nVersion=1.0\nType=Application\nName=Keep\n'
            'Comment=Seus vídeos, sempre por perto\nExec=' + command + '\n'
            'Path=' + _desktop_string(root) + '\nIcon=' + _desktop_string(root / 'dist' / 'icon.svg') + '\n'
            'Terminal=false\nCategories=AudioVideo;Video;\nStartupNotify=false\n'
            + ('NoDisplay=true\nMimeType=x-scheme-handler/keep;\n' if protocol else '')
            + 'X-Keep-Owner=' + OWNER + '\nX-Keep-Root=' + _desktop_string(root) + '\n')


def _read_registry():
    import winreg
    # HKCR is the merged current-user/machine view. Its read-only fallback
    # prevents a new HKCU registration from shadowing another installed app.
    for hive,path,scope in ((winreg.HKEY_CURRENT_USER,REGISTRY_PATH,'user'),
                            (winreg.HKEY_CLASSES_ROOT,'keep','machine')):
        try:
            key=winreg.OpenKey(hive,path)
        except FileNotFoundError:
            continue
        except OSError:
            return {'owner':None,'scope':scope}
        with key:
            try:
                owner = winreg.QueryValueEx(key, 'KeepOwner')[0]
                root = winreg.QueryValueEx(key, 'KeepRoot')[0]
            except FileNotFoundError:
                return {'owner':None,'scope':scope}
            except OSError:
                return {'owner':None,'scope':scope}
            if owner != OWNER:
                return {'owner':owner,'scope':scope}
        try:
            with winreg.OpenKey(hive,path+r'\shell\open\command') as key:
                command=winreg.QueryValueEx(key,'')[0]
        except OSError:
            command=''
        return {'owner':owner,'root':root,'command':command,'scope':scope}
    return None


def _linux_default(env):
    command = shutil.which('xdg-mime')
    if not command:
        return None
    try:
        result = subprocess.run([command, 'query', 'default', 'x-scheme-handler/keep'],
                                capture_output=True, text=True, timeout=10, env=env)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def status(root, *, platform=None, env=None):
    root, platform, env = _root(root), _platform(platform), _environment(env)
    enabled = installed = False
    launcher_path = ''
    if platform == 'windows':
        shortcut, _, _ = _windows_paths(root, env)
        launcher_path, installed = str(shortcut), shortcut.is_file()
        registration = _read_registry()
        enabled = bool(registration and registration.get('owner') == OWNER
                       and registration.get('root') == str(root) and registration.get('command'))
    elif platform == 'linux':
        applications = _linux_applications(env)
        entry = applications / DESKTOP_ID
        launcher_path = str(entry)
        installed = entry.is_file() and _owned_desktop_entry(entry.read_text(encoding='utf-8'), root)
        handler = applications / PROTOCOL_DESKTOP_ID
        enabled = (handler.is_file() and _owned_desktop_entry(handler.read_text(encoding='utf-8'), root)
                   and _linux_default(env) == PROTOCOL_DESKTOP_ID)
    return {'supported': platform in ('windows', 'linux'), 'platform': platform,
            'launcher_installed': installed, 'launcher_path': launcher_path,
            'protocol_enabled': bool(enabled), 'protocol_url': PROTOCOL,
            'message': 'O navegador pode pedir confirmação para abrir o Keep. A integração funciona apenas neste computador.'}


def install_launcher(root, *, platform=None, env=None):
    root, platform, env = _root(root), _platform(platform), _environment(env)
    if platform == 'windows':
        ensure_windows_launcher(root)
        shortcut, executable, arguments = _windows_paths(root, env)
        _checked_parent(shortcut)
        child_env = dict(env, KEEP_SHORTCUT=str(shortcut), KEEP_EXECUTABLE=str(executable),
                         KEEP_ARGUMENTS=subprocess.list2cmdline(arguments), KEEP_ROOT=str(root),
                         KEEP_ICON=str(root / 'dist' / 'keep.ico'))
        script = ("$ErrorActionPreference='Stop'; $shell=New-Object -ComObject WScript.Shell; "
                  "$link=$shell.CreateShortcut($env:KEEP_SHORTCUT); "
                  "if ((Test-Path -LiteralPath $env:KEEP_SHORTCUT) -and "
                  "(-not [String]::Equals([IO.Path]::GetFullPath($link.TargetPath), "
                  "[IO.Path]::GetFullPath($env:KEEP_EXECUTABLE), [StringComparison]::OrdinalIgnoreCase))) "
                  "{ throw 'Outro aplicativo já usa este atalho' }; "
                  "$link.TargetPath=$env:KEEP_EXECUTABLE; $link.Arguments=$env:KEEP_ARGUMENTS; "
                  "$link.WorkingDirectory=$env:KEEP_ROOT; $link.IconLocation=$env:KEEP_ICON; "
                  "$link.Description='Keep — Seus vídeos, sempre por perto'; $link.Save()")
        if _powershell(script, child_env).returncode:
            raise ValueError('Não foi possível criar o atalho no Iniciar. Confira a permissão da pasta do usuário.')
    elif platform == 'linux':
        _write_owned(_linux_applications(env) / DESKTOP_ID, _desktop_content(root))
    else:
        raise ValueError('O inicializador está disponível para Windows e Linux.')
    return status(root, platform=platform, env=env)


def set_protocol_enabled(root, enabled, *, platform=None, env=None):
    if not isinstance(enabled, bool):
        raise ValueError('Escolha ativar ou desativar a integração.')
    root, platform, env = _root(root), _platform(platform), _environment(env)
    if platform == 'windows':
        import winreg
        registration = _read_registry()
        if registration and registration.get('owner') != OWNER:
            raise ValueError('O endereço keep:// já pertence a outro aplicativo. O registro existente foi preservado.')
        if enabled:
            ensure_windows_launcher(root)
            _, executable, arguments = _windows_paths(root, env)
            command = subprocess.list2cmdline([str(executable), *arguments]) + ' "%1"'
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH) as key:
                for name, value in [('', 'URL:Keep'), ('URL Protocol', ''), ('KeepOwner', OWNER), ('KeepRoot', str(root))]:
                    winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH + r'\DefaultIcon') as key:
                winreg.SetValueEx(key, '', 0, winreg.REG_SZ, str(root / 'dist' / 'keep.ico'))
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH + r'\shell\open\command') as key:
                winreg.SetValueEx(key, '', 0, winreg.REG_SZ, command)
        elif registration and registration.get('root') == str(root):
            if registration.get('scope','user') != 'user':
                raise ValueError('Esta integração foi instalada para todo o computador. Desative-a pela instalação que a criou; o Keep não altera registros globais.')
            # Only remove the exact keys created above. Never recurse through a
            # key other software could have extended in the meantime.
            for suffix in (r'\shell\open\command', r'\shell\open', r'\shell', r'\DefaultIcon', ''):
                try:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REGISTRY_PATH + suffix)
                except FileNotFoundError:
                    pass
    elif platform == 'linux':
        applications = _linux_applications(env)
        handler = applications / PROTOCOL_DESKTOP_ID
        command = shutil.which('xdg-mime')
        if enabled:
            if not command:
                raise ValueError('Instale xdg-utils para permitir que o navegador abra o Keep.')
            current = _linux_default(env)
            if current and current != PROTOCOL_DESKTOP_ID:
                raise ValueError('O endereço keep:// já pertence a outro aplicativo. A associação existente foi preservada.')
            previous = handler.read_text(encoding='utf-8') if handler.exists() else None
            _write_owned(handler, _desktop_content(root, protocol=True))
            result = subprocess.run([command, 'default', PROTOCOL_DESKTOP_ID, 'x-scheme-handler/keep'],
                                    capture_output=True, timeout=15, env=env)
            if result.returncode:
                if previous is None:
                    handler.unlink(missing_ok=True)
                else:
                    _write_owned(handler, previous)
                raise ValueError('O ambiente gráfico não aceitou a integração. Confira os aplicativos padrão do sistema.')
        elif handler.exists():
            _checked_parent(handler)
            content = handler.read_text(encoding='utf-8')
            if _owned_desktop_entry(content, root):
                # A deleted desktop entry is no longer a usable association.
                # Do not rewrite the user's mimeapps.list or other defaults.
                handler.unlink()
        updater = shutil.which('update-desktop-database')
        if updater:
            subprocess.run([updater, str(applications)], capture_output=True, timeout=15, env=env)
    else:
        raise ValueError('A integração está disponível para Windows e Linux.')
    return status(root, platform=platform, env=env)


def notify(root, title, message, *, platform=None, env=None):
    """Best-effort, non-blocking native notice. Notification failure is harmless."""
    platform, env = _platform(platform), _environment(env)
    title = ''.join(char for char in str(title) if ord(char) >= 32)[:63] or 'Keep'
    message = ''.join(char for char in str(message) if ord(char) >= 32)[:240]
    try:
        if platform == 'windows':
            child_env = dict(env, KEEP_NOTICE_TITLE=title, KEEP_NOTICE_MESSAGE=message,
                             KEEP_ICON=str(Path(root).resolve() / 'dist' / 'keep.ico'))
            script = ("Add-Type -AssemblyName System.Windows.Forms; Add-Type -AssemblyName System.Drawing; "
                      "$notice=New-Object System.Windows.Forms.NotifyIcon; "
                      "try { $notice.Icon=New-Object System.Drawing.Icon($env:KEEP_ICON); "
                      "$notice.Visible=$true; $notice.BalloonTipTitle=$env:KEEP_NOTICE_TITLE; "
                      "$notice.BalloonTipText=$env:KEEP_NOTICE_MESSAGE; $notice.ShowBalloonTip(6000); "
                      "Start-Sleep -Seconds 8 } finally { $notice.Dispose() }")
            _powershell(script, child_env, wait=False)
            return True
        if platform == 'linux':
            command = shutil.which('notify-send')
            if command:
                subprocess.Popen([command, '--app-name=Keep', '--icon', str(Path(root).resolve() / 'dist' / 'icon.svg'),
                                  '--', title, message], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
                return True
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return False
