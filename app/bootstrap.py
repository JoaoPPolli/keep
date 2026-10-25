"""Prepara componentes oficiais na própria pasta, sem instalar programas no Windows."""
import hashlib
import json
from pathlib import Path
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / 'tools'
LICENSES = ROOT / 'licenses'

def fetch(url, path):
    request = urllib.request.Request(url, headers={'User-Agent': 'Keep/2.0.0'})
    with urllib.request.urlopen(request, timeout=90) as response, path.open('wb') as file:
        while chunk := response.read(1024 * 1024):
            file.write(chunk)

def prepare(name, component, force=False):
    target = TOOLS / ({'engine': 'yt-dlp', 'ffmpeg': 'ffmpeg', 'node': 'node'}[name] + '.exe')
    if target.is_file() and not force:
        return
    labels = {'engine': 'mecanismo de download', 'ffmpeg': 'processador de mídia', 'node': 'runtime de vídeo'}
    print('Preparando ' + labels[name] + '...', flush=True)
    temp = TOOLS / (name + '.download')
    extracted = TOOLS / (name + '.ready')
    try:
        fetch(component['url'], temp)
        if hashlib.sha256(temp.read_bytes()).hexdigest() != component['sha256']:
            raise RuntimeError('A verificação do arquivo falhou. Nenhum componente foi substituído.')
        if name == 'engine':
            temp.replace(target)
        else:
            with zipfile.ZipFile(temp) as archive:
                suffix = '/node.exe' if name == 'node' else '.exe'
                member = next(n for n in archive.namelist() if n.endswith(suffix))
                extracted.write_bytes(archive.read(member))
                license_member = next((n for n in archive.namelist() if n.endswith('/LICENSE')), None)
                if license_member:
                    (LICENSES / (name.upper() + '-PACKAGE-LICENSE.txt')).write_bytes(archive.read(license_member))
            extracted.replace(target)
    finally:
        temp.unlink(missing_ok=True)
        extracted.unlink(missing_ok=True)

def main():
    TOOLS.mkdir(exist_ok=True)
    LICENSES.mkdir(exist_ok=True)
    components = json.loads((ROOT / 'components.json').read_text(encoding='utf-8'))
    if '--update' in sys.argv:
        req = urllib.request.Request('https://api.github.com/repos/yt-dlp/yt-dlp-nightly-builds/releases/latest', headers={'User-Agent': 'Keep/2.0.0'})
        with urllib.request.urlopen(req, timeout=30) as response:
            release = json.load(response)
        asset = next(a for a in release['assets'] if a['name'] == 'yt-dlp.exe')
        digest = asset.get('digest', '')
        if not digest.startswith('sha256:'):
            raise RuntimeError('A versão oficial não forneceu um SHA256. Atualização cancelada.')
        components['engine'] = {'url': asset['browser_download_url'], 'sha256': digest.split(':')[1]}
        prepare('engine', components['engine'], force=True)
        print('Mecanismo atualizado: ' + release['tag_name'], flush=True)
    for name, component in components.items():
        prepare(name, component)
    print('Componentes prontos.', flush=True)

if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Não foi possível preparar o Keep: ' + str(error), flush=True)
        print('Verifique a conexão e tente abrir novamente. Os componentes existentes foram preservados.', flush=True)
        sys.exit(1)
