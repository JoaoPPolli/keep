#!/bin/sh
set -eu
KEEP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$KEEP_DIR"
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Falta instalar Python 3.10 ou mais recente.' 'Veja LEIA-ME-LINUX.txt.'
    exit 1
fi
printf '%s\n' 'Feche o Keep antes de atualizar o mecanismo.'
exec python3 -X utf8 "$KEEP_DIR/bootstrap_linux.py" --update
