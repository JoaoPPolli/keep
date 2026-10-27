KEEP 2.1.1
Seus vídeos, sempre por perto.

Baixe vídeos do YouTube, X e Twitch, escolha trechos e organize os arquivos
no seu computador. O Keep funciona como um aplicativo local com interface
no navegador, em português brasileiro.

Site do Keep
https://joaoppolli.github.io/keep/

Versão atual, downloads e notas da entrega
https://github.com/JoaoPPolli/keep/releases/tag/v2.1.1


BAIXAR O KEEP

WINDOWS — PRIMEIRA INSTALAÇÃO
Pacote portátil completo para Windows 10/11 de 64 bits, Intel/AMD.
Inclui Python, FFmpeg, Node e o mecanismo de download.

Keep-2.1.1-Windows-Portatil.zip
https://github.com/JoaoPPolli/keep/releases/download/v2.1.1/Keep-2.1.1-Windows-Portatil.zip

LINUX — PRIMEIRA INSTALAÇÃO
Pacote para Linux x64 ou ARM64. Requer Python 3.10 ou posterior e FFmpeg.
A primeira preparação dos demais componentes precisa de internet.

Keep-2.1.1-Linux.tar.gz
https://github.com/JoaoPPolli/keep/releases/download/v2.1.1/Keep-2.1.1-Linux.tar.gz

JÁ TEM O KEEP? BAIXE SOMENTE A ATUALIZAÇÃO DO SEU SISTEMA

Windows — Keep-2.1.1-Windows-Atualizacao.zip
https://github.com/JoaoPPolli/keep/releases/download/v2.1.1/Keep-2.1.1-Windows-Atualizacao.zip

Linux — Keep-2.1.1-Linux-Atualizacao.tar.gz
https://github.com/JoaoPPolli/keep/releases/download/v2.1.1/Keep-2.1.1-Linux-Atualizacao.tar.gz

A Release também oferece a instalação leve para Windows. Ela prepara os
componentes na primeira abertura e exige internet; para começar com todos
os componentes incluídos, escolha o pacote portátil completo acima.


COMEÇAR NO WINDOWS

1. Extraia todo o ZIP portátil para uma pasta do seu computador.
2. Abra Keep.exe. A alternativa é Iniciar Keep.bat.
3. Cole um link, clique em Buscar vídeo e escolha o formato e a qualidade.
4. Para criar o atalho do menu Iniciar, use Instalar inicializador nas
   Preferências. Depois, fixe o Keep pelas opções do próprio Windows.


COMEÇAR NO LINUX

1. Instale Python 3.10 ou posterior e FFmpeg pelo gerenciador de pacotes
   da sua distribuição.
2. Extraia o TAR.GZ completo e abra um terminal na pasta do Keep.
3. Execute: bash "Iniciar Keep.sh"
4. O iniciador verifica e prepara os demais componentes. Em Preferências,
   Instalar inicializador adiciona o Keep ao menu do seu usuário.

Integrações de menu, seleção de pasta, protocolo e notificações dependem
das ferramentas e da sessão gráfica da sua distribuição. Confira o guia TXT.


ATUALIZAR PRESERVANDO SEUS ARQUIVOS

No Keep 1.8 ou posterior, importe o pacote pequeno de atualização nas
Preferências. Use o ZIP no Windows e o TAR.GZ no Linux.

Para atualizar pelo método manual, encerre completamente o Keep nas
Preferências, extraia o pacote de atualização e execute Atualizar Keep.bat
no Windows ou bash "Atualizar Keep.sh" no Linux. Selecione a pasta antiga
do aplicativo. Downloads, histórico e configurações são preservados.

A partir do Keep 2.0.1, Verificar atualização consulta o canal público,
sem login. O app verifica ao abrir e mostra um aviso quando há uma versão
nova. Você escolhe baixar o pacote e depois atualizar e reiniciar.

Quem usa o Keep 2.0.0 precisa importar o pacote 2.1.1 uma vez nas
Preferências. Depois disso, as verificações usam o canal público.


O QUE O KEEP FAZ

- YouTube e Shorts, posts do X e clipes/VODs gravados da Twitch.
- Vídeo MP4, áudio MP3 e GIF de até 30 segundos.
- Fila com até dois downloads ativos, entrada de vários links, reordenação,
  pausa, retomada e tentativas limitadas para falhas temporárias.
- Seleção dos vídeos de posts do X que tenham mais de um vídeo.
- Prévia, linha do tempo, marcação de início/fim e navegação por quadros.
  O Keep avisa quando o passo por quadro precisar ser estimado.
- Até 24 trechos separados ou em uma montagem na ordem escolhida.
- Vídeo vertical 9:16 nos modos Cheia e Empilhado, com prévia ao lado do
  original. No Empilhado, selecione e ajuste separadamente a câmera e o
  conteúdo, e defina quanto da altura fica para a câmera.
- Legendas automáticas em português e inglês, reconhecidas no computador,
  com revisão do texto e prévia sincronizada antes de gravar no vídeo.
  O primeiro uso baixa o reconhecedor e o modelo do idioma pela internet.
- Nome do arquivo, pasta de destino, pastas por plataforma e aviso de
  duplicados, com informações de velocidade, tempo restante e espaço usado.
- Abrir pasta, mostrar arquivo, copiar fonte e excluir o arquivo com
  confirmação, mantendo seu registro no histórico.
- Preferências portáteis, diagnóstico, relatório de erro sem dados de
  sessão, notificações opcionais e recuperação da versão anterior.

Montagens têm limite de duas horas. Prévias e junções de trechos distantes
podem precisar de mais tempo e espaço temporário. Instagram e transmissões
ao vivo não estão incluídos.


CHANGELOG E GUIAS

Histórico completo das 18 versões, da 1.0 à 2.1.1
https://joaoppolli.github.io/keep/changelog.html

Changelog em TXT no próprio GitHub
https://github.com/JoaoPPolli/keep/blob/main/CHANGELOG.txt

Guia Windows em TXT
https://github.com/JoaoPPolli/keep/releases/download/v2.1.1/Keep-2.1.1-LEIA-ME-Windows.txt

Guia Linux em TXT
https://github.com/JoaoPPolli/keep/releases/download/v2.1.1/Keep-2.1.1-LEIA-ME-Linux.txt


SOBRE A VERSÃO 2.1.1

- Cheia mantém o recorte vertical em uma só área.
- Empilhado permite mover e redimensionar a seleção da câmera e a do
  conteúdo no original, além de ajustar a divisão da saída vertical.
- Legendas locais com progresso, cancelamento e revisão do texto.
- Tudo aparece na prévia: composição, enquadramentos e legendas seguem
  a reprodução, a pausa e a busca no vídeo, inclusive no vídeo inteiro.

O reconhecimento de fala usa Vosk no computador e não envia seu áudio para
um serviço externo. A primeira preparação de cada idioma exige internet;
depois, os componentes são reutilizados. Confira as legendas antes de exportar.

A validação usa testes de navegador, exportações reais com FFmpeg, integridade
dos pacotes e reconhecimento de fala com modelos locais. Não houve execução
nativa no Windows nesta validação. Integrações de menu e notificações dependem
da sessão gráfica do computador.

PRIVACIDADE E INTEGRIDADE

Os pacotes distribuídos não incluem vídeos, histórico, preferências,
cookies, tokens ou backups pessoais. Os downloads são processados
localmente. Cookies são opcionais e devem ser usados somente no seu PC.

As plataformas podem exigir login ou verificações de acesso. Consulte o
diagnóstico no Keep quando isso ocorrer. Use os arquivos apenas quando
você tiver autorização para baixá-los.

A Release inclui SHA256SUMS.txt para conferir a integridade dos pacotes.
As licenças dos componentes distribuídos estão na pasta licenses/.


DESENVOLVIMENTO DO APLICATIVO

Os fontes do app estão em app/, recuperados dos pacotes públicos 2.0.0
com SHA-256 conferido. A versão 2.1.1 mantém câmera empilhada e legendas locais, com prévia completa,
além das atualizações públicas e do modo de enquadramento Cheia.
Consulte app/README.md para executar, testar e gerar os pacotes.

O manifesto updates.json anuncia somente pacotes já publicados. Para
migrar do 2.0.0, importe o pacote 2.1.1 uma vez nas Preferências;
o app instalado não muda apenas com a publicação do site.

CLIPES DE LIVES
YouTube e Twitch: acompanhe uma live e guarde os últimos 15, 30, 60 ou 120
segundos recebidos, com áudio e até 720p. O acompanhamento começa ao clicar;
não recupera momentos anteriores. O original é salvo e abre na prévia para
cortar, compor a câmera e legendar. Mantenha o Keep aberto durante a live.
O botão Abrir Keep do site público não exige login. A configuração antiga
de abertura automática pelo site privado foi removida.
