#!/usr/bin/env python3
"""Build complete Keep distributions from checksum-pinned public tool bundles.

Run after build_update.py. Only allowlisted application files are overlaid;
the published runtimes, launchers, tools, and licenses remain byte-identical.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import stat
import tarfile
import tempfile
from urllib.request import Request, urlopen
import zipfile

from build_update import (APP, REPOSITORY, constants, digest, encode_json,
                          source, version_tuple)

BASE_VERSION = '2.0.0'
BASELINES = {
    'Windows-Portatil.zip': ('windows', 'd061c26998ad5caaaccfdfdb733c4c4bb15584bb5d48edc479140cfd9d62809c'),
    'Windows-Instalacao-Leve.zip': ('windows', '9adca2f3f38d9ec7d3696d025f6fd1942d66c96c13c0e5824474d5b7af908c36'),
    'Linux.tar.gz': ('linux', '24d6029a0dd82fad85fd291a801ff39e40110398f1af6d8b146d2e63ba1dfcd9'),
}
MAX_BASE_BYTES = 128 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_MEMBERS = 1024


def file_digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            value.update(chunk)
    return value.hexdigest()


def baseline(cache, suffix, expected):
    name = 'Keep-' + BASE_VERSION + '-' + suffix
    destination = cache / name
    if destination.exists():
        if file_digest(destination) != expected:
            raise ValueError('O cache não corresponde ao SHA-256 publicado: ' + name)
        return destination
    cache.mkdir(parents=True, exist_ok=True)
    request = Request('https://github.com/JoaoPPolli/keep/releases/download/v' + BASE_VERSION + '/' + name,
                      headers={'User-Agent': 'Keep-distribution-builder'})
    with tempfile.NamedTemporaryFile(prefix='.download-', dir=cache, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            size = 0
            with urlopen(request, timeout=45) as response:
                while chunk := response.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_BASE_BYTES:
                        raise ValueError('O pacote de origem excede o limite permitido.')
                    stream.write(chunk)
            stream.close()
            if file_digest(temporary) != expected:
                raise ValueError('O pacote de origem não corresponde ao SHA-256 publicado: ' + name)
            temporary.replace(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return destination


def clean_name(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or '..' in path.parts or '\\' in name
            or ':' in name or '\0' in name or path.as_posix() != name
            or len(path.parts) < 2 or path.parts[0] != 'Keep'):
        raise ValueError('O pacote de origem contém um caminho inválido.')
    return '/'.join(path.parts[1:])


def read_baseline(path, allowed):
    """Read regular files without extracting archive-controlled paths."""
    entries, total = {}, 0

    def accept(name, size, mode, content):
        nonlocal total
        relative = clean_name(name)
        if relative in entries:
            raise ValueError('O pacote de origem contém um arquivo duplicado.')
        if (relative not in allowed and relative not in ('Keep.exe', 'manifest.json')
                and not relative.startswith(('runtime/', 'tools/', 'licenses/'))):
            raise ValueError('O pacote de origem contém um arquivo inesperado: ' + relative)
        total += size
        if total > MAX_EXPANDED_BYTES or len(entries) >= MAX_MEMBERS:
            raise ValueError('O pacote de origem expandido excede o limite permitido.')
        value = content.read(size + 1)
        if len(value) != size:
            raise ValueError('O pacote de origem está incompleto.')
        entries[relative] = (value, 0o755 if mode & 0o111 else 0o644)

    if path.name.endswith('.zip'):
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                if member.is_dir():
                    clean_name(member.filename.rstrip('/'))
                    continue
                mode = member.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise ValueError('O pacote de origem contém um vínculo ou arquivo especial.')
                with archive.open(member) as stream:
                    accept(member.filename, member.file_size, mode, stream)
    else:
        with tarfile.open(path, 'r:gz') as archive:
            for member in archive:
                if member.isdir():
                    clean_name(member.name)
                    continue
                if not member.isfile():
                    raise ValueError('O pacote de origem contém um vínculo ou arquivo especial.')
                with archive.extractfile(member) as stream:
                    accept(member.name, member.size, member.mode, stream)
    if not set(allowed) <= set(entries):
        raise ValueError('O pacote de origem não contém todos os arquivos de aplicativo esperados.')
    return entries


def write_distribution(destination, entries, platform):
    """Use fixed archive metadata while retaining executable Linux tool modes."""
    if platform == 'windows':
        with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, (content, mode) in sorted(entries.items()):
                item = zipfile.ZipInfo('Keep/' + name, date_time=(1980, 1, 1, 0, 0, 0))
                item.create_system = 3
                item.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(item, content, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    else:
        with destination.open('wb') as output:
            with gzip.GzipFile(fileobj=output, mode='wb', filename='', mtime=0, compresslevel=9) as compressed:
                with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive:
                    for name, (content, mode) in sorted(entries.items()):
                        item = tarfile.TarInfo('Keep/' + name)
                        item.size, item.mode, item.mtime = len(content), mode, 0
                        archive.addfile(item, io.BytesIO(content))


def build(output):
    output = output.resolve()
    if output == REPOSITORY or output.is_relative_to(APP):
        raise ValueError('Use artifacts/ ou outra pasta de saída fora de app/.')
    version = constants('server.py')['VERSION']
    version_tuple(version)
    manifest_path = output / 'updates.json'
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes)
    if manifest.get('version') != version:
        raise ValueError('Execute build_update.py antes de preparar as distribuições.')
    deliverables = []
    for platform, package in manifest['packages'].items():
        suffix = 'Windows-Atualizacao.zip' if platform == 'windows' else 'Linux-Atualizacao.tar.gz'
        name = 'Keep-' + version + '-' + suffix
        path = output / name
        if path.stat().st_size != package['size'] or file_digest(path) != package['sha256']:
            raise ValueError('O pacote pequeno não corresponde ao manifesto: ' + name)
        deliverables.append(name)
    with tempfile.TemporaryDirectory(prefix='.keep-distribution-', dir=output) as directory:
        temporary = Path(directory)
        for suffix, (platform, expected) in BASELINES.items():
            updater = 'update_keep.py' if platform == 'windows' else 'update_keep_linux.py'
            selected = constants(updater)
            if selected['VERSION'] != version:
                raise ValueError('A versão dos atualizadores difere de server.py.')
            allowed = selected['APP_FILES']
            original = read_baseline(baseline(output / '.cache', suffix, expected), allowed)
            entries = dict(original)
            for relative in allowed:
                entries[relative] = (source(relative), 0o755 if relative.endswith('.sh') else 0o644)
            if platform == 'linux':
                full_manifest = {'version': version, 'platform': platform, 'layout': 'root',
                                 'files': {name: digest(entries[name][0]) for name in allowed}}
                entries['manifest.json'] = (encode_json(full_manifest), 0o644)
            name = 'Keep-' + version + '-' + suffix
            destination = temporary / name
            write_distribution(destination, entries, platform)
            verified = read_baseline(destination, allowed)
            if verified != entries:
                raise ValueError('A conferência da distribuição gerada falhou: ' + name)
            for relative in original.keys() - set(allowed) - {'manifest.json'}:
                if verified[relative] != original[relative]:
                    raise ValueError('Uma ferramenta ou licença foi alterada: ' + relative)
            destination.replace(output / name)
            deliverables.append(name)
            print(str(output / name) + ' (' + str((output / name).stat().st_size) + ' bytes)', flush=True)
        for platform, guide in [('Windows', 'LEIA-ME.txt'), ('Linux', 'LEIA-ME-LINUX.txt')]:
            name = 'Keep-' + version + '-LEIA-ME-' + platform + '.txt'
            (output / name).write_bytes(source(guide))
            deliverables.append(name)
        (output / 'CHANGELOG.txt').write_bytes((REPOSITORY / 'CHANGELOG.txt').read_bytes())
        deliverables.append('CHANGELOG.txt')
        sums = ''.join(file_digest(output / name) + '  ' + name + '\n' for name in sorted(deliverables))
        (output / 'SHA256SUMS.txt').write_text(sums, encoding='ascii')
    if manifest_path.read_bytes() != manifest_bytes:
        raise ValueError('O manifesto de atualização foi alterado durante a preparação.')
    print('Distribuições verificadas; ferramentas, licenças e manifesto público preservados.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=REPOSITORY / 'artifacts')
    args = parser.parse_args()
    try:
        build(args.output)
    except (ValueError, OSError, zipfile.BadZipFile, tarfile.TarError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
