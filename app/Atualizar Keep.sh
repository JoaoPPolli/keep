#!/bin/sh
# Execute no terminal: sh "Atualizar Keep.sh" --target "/caminho/para/Keep antigo"
set -eu
KEEP_PACKAGE_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' 'Não foi encontrado Python 3. Instale Python 3.10 ou mais recente pelo gerenciador da sua distribuição.' >&2
    exit 1
fi
if ! python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    printf '%s\n' 'O Keep precisa de Python 3.10 ou mais recente. Atualize o Python da sua distribuição.' >&2
    exit 1
fi
exec python3 "$KEEP_PACKAGE_DIR/update_keep_linux.py" "$@"
