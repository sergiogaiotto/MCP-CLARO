#!/usr/bin/env bash
# Restaura o PostgreSQL do MCP-CLARO a partir do repositorio do pgBackRest. Roteiro: docs/restauracao.md.
#
# Roda num conteiner de uma vez, com o servico "banco" parado e o servico "backup" no ar:
#   docker compose ... stop banco
#   docker compose ... run --rm --no-deps --entrypoint /usr/local/bin/restaurar.sh banco [opcoes]
#
# Opcoes:
#   --ate "AAAA-MM-DD HH:MM:SS+00"   recupera ate esse momento (recuperacao para um ponto no tempo)
#   --copia <rotulo>                 parte dessa copia (pgbackrest info mostra os rotulos)
# Sem opcao, recupera ate o ultimo WAL guardado. Nos dois casos o banco abre numa nova linha do tempo, e o
# despachante percebe sozinho na partida seguinte (conferencia de continuidade).
set -euo pipefail

STANZA="${PGBACKREST_STANZA:-mcp-claro}"
PGDATA="${PGDATA:-/var/lib/postgresql/18/docker}"
ate=""
copia=""
while [ $# -gt 0 ]; do
  case "$1" in
    --ate) ate="${2:?falta o momento depois de --ate}"; shift 2 ;;
    --copia) copia="${2:?falta o rotulo depois de --copia}"; shift 2 ;;
    *) echo "restaurar: opcao desconhecida: $1" >&2; exit 2 ;;
  esac
done

# Se o banco ainda responde na rede interna, nada feito: restaurar por cima de um banco no ar estraga tudo.
if pg_isready -q -h "${RESTAURAR_BANCO_HOST:-banco}" -p 5432 -t 3; then
  echo "restaurar: o banco ainda esta no ar. Pare o servico banco antes (docker compose ... stop banco)." >&2
  exit 2
fi

/usr/local/bin/preparar-tls.sh
/usr/local/bin/pgbackrest-banco.sh preparar
install -d -o postgres -g postgres -m 700 "$PGDATA"

opcoes=(--stanza="$STANZA" --delta --log-level-console=info)
if [ -n "$ate" ]; then
  opcoes+=(--type=time "--target=$ate" --target-action=promote)
fi
if [ -n "$copia" ]; then
  opcoes+=("--set=$copia")
fi

echo "restaurar: copias disponiveis no repositorio:"
gosu postgres pgbackrest --stanza="$STANZA" info
gosu postgres pgbackrest "${opcoes[@]}" restore
cat <<'FIM'
restaurar: pronto. Proximos passos (docs/restauracao.md):
  1. suba o banco (docker compose ... up -d banco); ele le os WAL do repositorio, termina a recuperacao
     e abre numa nova linha do tempo;
  2. grave a marca "restaurado" (python -m mcp_claro.continuidade marcar-restaurado ...);
  3. suba o resto; a porta de saida fica fechada e os aplicativos pausados ate a decisao do ADMIN.
FIM
