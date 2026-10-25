#!/bin/sh
set -eu
KEEP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$KEEP_DIR"
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Falta instalar Python 3.10 ou mais recente.' 'Veja os comandos da sua distribuição em LEIA-ME-LINUX.txt.'
    exit 1
fi
python3 -X utf8 "$KEEP_DIR/bootstrap_linux.py"
printf '\n%s\n' 'Keep 1.7.0 para Linux. Deixe este terminal aberto durante os downloads.' 'Para fechar, pressione Ctrl+C; os downloads poderão ser retomados.'
exec python3 -X utf8 "$KEEP_DIR/server.py" --open "$@"
