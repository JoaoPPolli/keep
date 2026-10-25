#!/usr/bin/env python3
"""Build deterministic, allowlisted Keep update packages without publishing."""
import argparse
import ast
import gzip
import hashlib
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import zipfile

REPOSITORY = Path(__file__).resolve().parents[1]
APP = REPOSITORY / 'app'
sys.path.insert(0, str(APP))
from app_updates import inspect_archive  # noqa: E402
from updater_runner import package_files, safe_path, version_tuple  # noqa: E402


def constants(name):
    """Read release constants without running the server or manual updaters."""
    values = {}
    for node in ast.parse((APP / name).read_text(encoding='utf-8')).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ('VERSION', 'APP_FILES'):
                    values[target.id] = ast.literal_eval(node.value)
    return values


def encode_json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def digest(content):
    return hashlib.sha256(content).hexdigest()


def source(relative):
    path = safe_path(APP, relative)
    if not path.is_file():
        raise ValueError('Arquivo obrigatório ausente: app/' + relative)
    return path.read_bytes()


def entries_for(platform, version):
    updater = 'update_keep.py' if platform == 'windows' else 'update_keep_linux.py'
    release = constants(updater)
    if release['VERSION'] != version:
        raise ValueError('A versão de ' + updater + ' difere de server.py.')
    payload = {name: source(name) for name in release['APP_FILES']}
    hashes = {name: digest(content) for name, content in payload.items()}
    package_files(hashes, platform)
    manifest = {'version': version, 'platform': platform, 'layout': 'payload', 'files': hashes}
    wrappers = ('update_keep.py', 'Atualizar Keep.bat', 'LEIA-ME.txt') if platform == 'windows' else (
        'update_keep_linux.py', 'Atualizar Keep.sh', 'LEIA-ME-LINUX.txt')
    entries = {'Atualizar Keep/' + name: source(name) for name in wrappers}
    entries['Atualizar Keep/manifest.json'] = encode_json(manifest)
    entries.update({'Atualizar Keep/payload/' + name: content for name, content in payload.items()})
    return entries


def archive_for(platform, entries):
    output = io.BytesIO()
    if platform == 'windows':
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, content in sorted(entries.items()):
                entry = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                entry.create_system = 3
                entry.external_attr = (0o100644 << 16)
                archive.writestr(entry, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    else:
        with gzip.GzipFile(fileobj=output, mode='wb', filename='', mtime=0, compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive:
                for name, content in sorted(entries.items()):
                    entry = tarfile.TarInfo(name)
                    entry.size = len(content)
                    entry.mode = 0o755 if name.endswith('.sh') else 0o644
                    entry.mtime = 0
                    archive.addfile(entry, io.BytesIO(content))
    return output.getvalue()


def build(output):
    output = output.resolve()
    if output == REPOSITORY or output.is_relative_to(APP):
        raise ValueError('Use artifacts/ ou outra pasta de saída fora de app/, sem sobrescrever a raiz do repositório.')
    version = constants('server.py')['VERSION']
    version_tuple(version)
    release_url = 'https://github.com/JoaoPPolli/keep/releases/tag/v' + version
    descriptor = {'schema_version': 1, 'version': version, 'release_url': release_url, 'packages': {}}
    files = {}
    for platform, suffix in (('windows', 'Windows-Atualizacao.zip'), ('linux', 'Linux-Atualizacao.tar.gz')):
        content = archive_for(platform, entries_for(platform, version))
        manifest, _ = inspect_archive(content, platform)
        if manifest['version'] != version:
            raise ValueError('A versão do pacote gerado não corresponde à versão do servidor.')
        filename = 'Keep-' + version + '-' + suffix
        files[filename] = content
        descriptor['packages'][platform] = {
            'url': 'https://github.com/JoaoPPolli/keep/releases/download/v' + version + '/' + filename,
            'sha256': digest(content), 'size': len(content),
        }
    files['SHA256SUMS.txt'] = ''.join(digest(content) + '  ' + name + '\n' for name, content in sorted(files.items())).encode('ascii')
    files['updates.json'] = encode_json(descriptor)
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.keep-build-', dir=output) as directory:
        temporary = Path(directory)
        for name, content in files.items():
            (temporary / name).write_bytes(content)
        for name in files:
            (temporary / name).replace(output / name)
    for name, content in files.items():
        print(str(output / name) + ' (' + str(len(content)) + ' bytes)')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=REPOSITORY / 'artifacts', help='Pasta de saída (padrão: artifacts/)')
    args = parser.parse_args()
    try:
        build(args.output)
    except (ValueError, OSError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
