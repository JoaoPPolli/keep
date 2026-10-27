"""Portable preference backups and release notes for the installed version."""
import json

PREFERENCE_DEFAULTS = {'paste_and_download': False, 'default_browser': 'none',
                       'default_quality': '1080', 'platform_folders': False,
                       'notifications': False}
BROWSERS = ('none', 'imported', 'firefox', 'chrome', 'edge', 'brave')
QUALITIES = ('best', '2160', '1080', '720', '480')

NOTES = {
    '1.8.0': [
        'Excluir o arquivo do computador mantendo o histórico.',
        'Identificação de arquivos removidos e botão Copiar link original.',
    ],
    '1.9.0': [
        'Escolha do nome do arquivo e pastas automáticas por plataforma.',
        'Vários links por vez, organização da fila e aviso de duplicados.',
        'Espaço ocupado pelos downloads e tentativas limitadas para falhas temporárias.',
        'Diagnóstico simples e relatório sem cookies, links ou dados de sessão.',
        'Backup portátil das preferências e novidades após atualizar.',
    ],
    '1.10.0': [
        'Linha do tempo visual, navegação por quadros e vários trechos.',
        'Junção de trechos, criação de GIF e enquadramento vertical 9:16.',
    ],
    '1.11.0': [
        'Inicializador com ícone para Windows e Linux.',
        'Abertura pelo navegador com integração opcional e notificações nativas.',
        'Retorno à versão anterior com preservação dos arquivos e configurações.',
    ],
    '2.0.0': [
        'A entrega final antes identificada como 1.11.0 agora se chama Keep 2.0.0.',
        'Esta mudança de número reúne os mesmos recursos, sem alterar seus downloads ou preferências.',
    ],
    '2.0.1': [
        'Verificação pública de atualizações, sem página privada ou login.',
        'Aviso no app quando uma versão mais recente estiver disponível.',
        'Prévia do resultado vertical 9:16 enquanto você ajusta o enquadramento.',
    ],
    '2.1.0': [
        'Clipes de lives do YouTube e da Twitch: acompanhe e salve até dois minutos recentes para editar.',
        'Abertura pelo site público sem login e remoção da configuração automática privada.',
        'Modo Empilhado: escolha a área da câmera e do conteúdo, com posição e tamanho independentes.',
        'Ajuste a altura da câmera na composição vertical; o modo Cheia continua disponível.',
        'Legendas automáticas locais em português e inglês, com revisão do texto antes de exportar.',
        'Confira câmera, conteúdo e legendas na prévia sincronizada, no vídeo inteiro ou nos trechos.',
    ],
    '2.1.1': [
        'O changelog do app agora mostra apenas as novidades da versão instalada, sem acumular atualizações anteriores.',
    ],
    '2.2.0': ['Legendas: modelo oficial de inglês, preparação de caminhos compatíveis no Windows e opção para reparar os modelos.', 'Correção do painel de live que permanecia ao trocar para um vídeo normal; o acompanhamento anterior é encerrado.', 'Cortes do YouTube: alternativa de baixar a origem e montar os trechos localmente quando o corte remoto falhar.', 'Importação de vídeos do PC para cortar, compor a câmera e legendar, preservando o arquivo original.', 'Consulta ao histórico disponível da live: gravação em andamento quando identificada ou janela HLS acessível, com escolha de horários.'],
}


def export_preferences(preferences):
    # Explicit fields only: no local folder, history, token, cookie or profile.
    values = {key: preferences.get(key, default) for key, default in PREFERENCE_DEFAULTS.items()}
    return {'schema': 'keep-preferences', 'version': 1, 'preferences': values}


def import_preferences(backup):
    if (not isinstance(backup, dict) or set(backup) != {'schema', 'version', 'preferences'}
            or backup['schema'] != 'keep-preferences' or type(backup['version']) is not int
            or backup['version'] != 1):
        raise ValueError('Escolha um backup de preferências exportado pelo Keep.')
    values = backup['preferences']
    legacy_keys = set(PREFERENCE_DEFAULTS) - {'notifications'}
    if not isinstance(values, dict) or set(values) not in (legacy_keys, set(PREFERENCE_DEFAULTS)):
        raise ValueError('O backup contém preferências desconhecidas ou dados que não podem ser importados.')
    # Backups exported by 1.9/1.10 predate notifications. Importing them never
    # enables a new operating-system behavior without the saved preference.
    values = dict(PREFERENCE_DEFAULTS, **values)
    if (type(values['paste_and_download']) is not bool or type(values['platform_folders']) is not bool
            or type(values['notifications']) is not bool
            or values['default_browser'] not in BROWSERS or values['default_quality'] not in QUALITIES):
        raise ValueError('As opções do backup são inválidas. Nenhuma preferência foi alterada.')
    return dict(values)


def release_notes(version):
    notes = list(NOTES.get(version, ()))
    return {'version': version, 'notes': notes, 'show': True}


def snapshot_preferences_for_restore(root, backup):
    """Keep an opaque local snapshot; never import it over current settings."""
    from pathlib import Path
    import shutil
    from updater_runner import digest, safe_path
    root,backup=Path(root).resolve(),Path(backup)
    try:
        relative=backup.relative_to(root).as_posix()
    except ValueError:
        raise ValueError('O destino da cópia de preferências não pertence ao Keep.') from None
    destination_root=safe_path(root,relative)
    if not relative.startswith('backups/') or destination_root.parent!=safe_path(root,'backups'):
        raise ValueError('O destino da cópia de preferências é inválido.')
    source=safe_path(root,'data/settings.json')
    if not source.exists():
        return None
    if not source.is_file() or source.stat().st_size>1024*1024:
        raise ValueError('Não foi possível conferir as preferências atuais. Nenhum arquivo foi alterado.')
    destination=safe_path(destination_root,'preferences-before-restore.json')
    try:
        shutil.copy2(source,destination)
        expected=digest(source)
        if digest(destination)!=expected:
            raise ValueError('A cópia das preferências atuais falhou. Nenhum arquivo foi alterado.')
    except OSError:
        raise ValueError('Não foi possível copiar as preferências atuais. Confira as permissões e o espaço disponível. Nenhum arquivo foi alterado.') from None
    return {'file':'preferences-before-restore.json','sha256':expected}
