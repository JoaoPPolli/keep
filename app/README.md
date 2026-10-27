# Aplicativo Keep

O código desta pasta foi recuperado dos pacotes públicos de atualização do Keep 2.0.0, publicados em `JoaoPPolli/keep`. Os arquivos comuns dos pacotes Windows e Linux eram idênticos. Os SHA-256 dos arquivos originais foram conferidos antes da extração:

| Pacote de origem | SHA-256 |
| --- | --- |
| `Keep-2.0.0-Windows-Atualizacao.zip` | `1f22e915354c624f0d69b6b7282d705c5afd878382d6e0e0828c86a59c6b1c37` |
| `Keep-2.0.0-Linux-Atualizacao.tar.gz` | `3e45038b88a09267439e3ca8f736462bd9e8de079531c353d3f23f8b9887105d` |

Esta pasta reúne os fontes dos dois sistemas; não inclui Python portátil, FFmpeg, mecanismo de download ou dados pessoais. A raiz do repositório continua sendo o site público.

## Desenvolvimento

Com Python 3.10 ou posterior, a partir da raiz do repositório:

```sh
python3 app/server.py
```

O servidor atende apenas no endereço local e não precisa de um framework web. Para baixar e preparar prévias de vídeos, instale FFmpeg e prepare o mecanismo e o runtime pelo iniciador da plataforma, conforme `LEIA-ME-LINUX.txt` ou `LEIA-ME.txt`. As pastas de ferramentas, downloads, dados e backups são locais e ignoradas pelo Git. A interface fica em `app/dist/`.

Execute os testes com:

```sh
python3 -m unittest discover -s app/tests -v
```

Os testes de navegador precisam do pacote Python `playwright`, Chromium e FFmpeg; os testes de comparação do recorte precisam de Node e FFmpeg. Essas suítes indicam explicitamente quando os pré-requisitos estão ausentes. Não são dependências para executar o servidor.

## Composição e legendas

A versão 2.1.0 preserva o modo Cheia e adiciona Empilhado: duas áreas normalizadas do mesmo vídeo, para câmera e conteúdo, com altura da câmera entre 15% e 60% da saída 9:16. A prévia e o FFmpeg usam a mesma geometria de pixels pares. Os efeitos podem ser aplicados ao vídeo inteiro ou aos trechos.

As legendas usam Vosk CPU local por `ctypes`, inclusive com o Python portátil Windows; não exigem pip nem chave de API. A primeira geração baixa binários nativos oficiais e um modelo verificado do idioma para `tools/subtitles`. Português usa o modelo oficial hospedado em `alphacephei.com`; Inglês usa um snapshot oficial do projeto Vosk. O áudio e a transcrição permanecem no computador. A interface permite cancelar, revisar o texto e conferir os mesmos tempos na prévia e no vídeo exportado.

A geração exige uma prévia pronta, sua capacidade de acesso e o token da sessão local. A exportação valida os textos e horários e usa arquivos auxiliares para manter os comandos limitados, mesmo com muitas legendas. Esses arquivos temporários são removidos após a edição. Os modelos e ferramentas ficam fora dos pacotes pequenos de atualização; a primeira utilização precisa de rede.

## Atualizações públicas

O aplicativo consulta o manifesto público `updates.json` deste repositório, compara a versão disponível com a instalada e mostra o aviso no app. A verificação não instala uma atualização: o usuário escolhe baixar o pacote e depois aplicar e reiniciar. A importação manual de ZIP/TAR.GZ e a restauração de uma cópia anterior continuam disponíveis.

O Keep 2.0.0 ainda usa a consulta privada original. Para migrar uma instalação existente, importe uma vez o pacote de atualização atual nas Preferências. A partir dessa versão, a consulta usa o canal público. Apenas publicar um novo manifesto no site não modifica o aplicativo já instalado.

Para preparar os dois pacotes de atualização, execute na raiz:

```sh
python3 scripts/build_update.py
```

O comando gera os pacotes de atualização Windows e Linux, `SHA256SUMS.txt` e um **candidato** a `updates.json` em `artifacts/`. Em seguida, `python3 scripts/build_distribution.py` gera os pacotes completos a partir das bases públicas 2.0.0 com SHA-256 fixo, preservando ferramentas e licenças, e agrega os guias e checksums. Os arquivos são produzidos com metadados fixos, validados pelo mesmo leitor do app e incluem somente os caminhos de aplicativo permitidos pelos atualizadores. Arquivos de usuário e ferramentas instaladas não entram no pacote. O comando não altera o manifesto público nem publica arquivos.

Para publicar uma versão:

1. Atualize `VERSION` em `server.py`, `update_keep.py` e `update_keep_linux.py`, além das notas e dos rótulos da interface. Preserve a versão anterior nas listas de migração dos atualizadores manuais.
2. Execute os testes, gere os pacotes e teste a importação em uma cópia isolada de uma instalação anterior.
3. Publique uma release `v<versão>` no GitHub com os dois pacotes e `SHA256SUMS.txt`, usando os nomes e as URLs definidos no candidato a manifesto. Confirme que os downloads públicos correspondem aos SHA-256 gerados.
4. Somente depois de os downloads estarem disponíveis, copie `artifacts/updates.json` para `updates.json` na raiz e publique essa alteração no ramo `main`/site. Atualize os links do site e o changelog conforme a entrega.

Até a publicação dos novos pacotes, o manifesto da raiz deve continuar anunciando a última versão efetivamente disponível. Um pacote gerado localmente não está publicado e não deve ser anunciado como download público.


A automação `.github/workflows/release.yml` é acionada por tags `v*`. Ela
confere a versão, executa os testes, gera todos os pacotes, publica primeiro
um rascunho e valida os arquivos enviados antes de tornar a release pública.
Ela também valida os downloads sem autenticação. Para evitar links quebrados,
mantenha `main` na versão publicada até essa execução terminar; depois use
o `updates.json` anexado à release e publique o site/manifesto em `main`.
O push final deve usar a autenticação normal do publicador para disparar a
publicação do GitHub Pages, pois pushes com GITHUB_TOKEN não a disparam.

Lives em andamento do YouTube e da Twitch têm um buffer local de até dois
minutos. Acompanhe antes do momento desejado e salve 15, 30, 60 ou 120 segundos;
o original vai para downloads e a prévia permite editar e legendar o clipe.
A captura usa yt-dlp e FFmpeg em até 720p, sem enviar o vídeo a outro serviço.
A abertura pelo site aponta para https://joaoppolli.github.io/keep/ sem login.
