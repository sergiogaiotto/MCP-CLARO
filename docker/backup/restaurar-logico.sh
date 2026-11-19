#!/usr/bin/env bash
# Restaura uma copia logica do MCP-CLARO (feita por logico.sh). Roteiro: docs/restauracao.md.
#
#   restaurar-logico.sh <pasta da copia> [banco ...]
#
# Sem lista, restaura todos os bancos da copia. Cada banco e apagado e criado de novo, como estava
# (dono, permissoes e ajustes), com o usuario dono do servidor. Com RESTAURAR_COMO=<nome> e um banco so,
# restaura com outro nome, sem apagar nada (ensaio de restauracao).
#
# Ambiente (valores iniciais entre parenteses):
#   RESTAURAR_HOST (banco)  RESTAURAR_PORTA (5432)  RESTAURAR_USUARIO (mcp_claro_migracao)
#   RESTAURAR_SENHA_ARQUIVO (/run/secrets/db_migracao)  LOGICO_CIFRA_ARQUIVO (/run/secrets/backup_cifra)
#   RESTAURAR_SSLMODE (verify-full)  RESTAURAR_SSLROOTCERT (/run/secrets/ca_interna.pem)
#   RESTAURAR_GLOBAIS (ligado)  RESTAURAR_COMO (vazio)  RESTAURAR_QUEM (roteiro de restauracao)
#
# Depois de restaurar o banco do gateway, grava a marca "restaurado" (continuidade_marcar_restaurado): uma
# copia logica posta de volta no mesmo servidor nao muda o identificador nem a linha do tempo do
# PostgreSQL, e sem a marca o despachante nao teria como perceber. A marca tambem adianta o numero de
# protocolo; ela e gravada aqui, logo depois do pg_restore e com o gateway ainda parado (passo 1 do roteiro),
# antes de a entrada voltar a dar numeros.
set -euo pipefail

: "${RESTAURAR_HOST:=banco}" "${RESTAURAR_PORTA:=5432}" "${RESTAURAR_USUARIO:=mcp_claro_migracao}"
: "${RESTAURAR_SENHA_ARQUIVO:=/run/secrets/db_migracao}" "${LOGICO_CIFRA_ARQUIVO:=/run/secrets/backup_cifra}"
: "${RESTAURAR_SSLMODE:=verify-full}" "${RESTAURAR_SSLROOTCERT:=/run/secrets/ca_interna.pem}"
: "${RESTAURAR_GLOBAIS:=ligado}" "${RESTAURAR_COMO:=}" "${RESTAURAR_QUEM:=roteiro de restauracao}"

log() { printf '%s restaurar-logico: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

pasta="${1:?uso: restaurar-logico.sh <pasta da copia> [banco ...]}"
shift
[ -d "$pasta" ] || { log "pasta nao encontrada: $pasta"; exit 2; }
[ -s "$LOGICO_CIFRA_ARQUIVO" ] || { log "falta a senha de cifra ($LOGICO_CIFRA_ARQUIVO)"; exit 2; }

# 1. Confere cada arquivo antes de decifrar: uma copia alterada ou pela metade para aqui.
(cd "$pasta" && sha256sum --check --quiet SHA256SUMS) || { log "a conferencia SHA256SUMS falhou; nada restaurado"; exit 3; }

carimbo="$(sed -n 's/^carimbo: //p' "$pasta/copia.txt")"
carimbo="${carimbo:-$(basename "$pasta")}"

bancos=("$@")
if [ ${#bancos[@]} -eq 0 ]; then
  for arquivo in "$pasta"/*.dump.gpg; do
    nome="$(basename "$arquivo")"
    bancos+=("${nome%.dump.gpg}")
  done
fi
if [ -n "$RESTAURAR_COMO" ] && [ ${#bancos[@]} -ne 1 ]; then
  log "RESTAURAR_COMO vale para um banco so"
  exit 2
fi

TEMP="$(mktemp -d)"
trap 'rm -rf -- "$TEMP"' EXIT
umask 077
printf '*:*:*:%s:%s\n' "$RESTAURAR_USUARIO" "$(tr -d '\n' < "$RESTAURAR_SENHA_ARQUIVO" | sed 's/[\\:]/\\&/g')" \
  > "$TEMP/pgpass"
export PGPASSFILE="$TEMP/pgpass" PGHOST="$RESTAURAR_HOST" PGPORT="$RESTAURAR_PORTA" PGUSER="$RESTAURAR_USUARIO"
export PGSSLMODE="$RESTAURAR_SSLMODE" PGSSLROOTCERT="$RESTAURAR_SSLROOTCERT" PGCONNECT_TIMEOUT=10
export PGAPPNAME=mcp-claro-restauracao GNUPGHOME="$TEMP/gnupg"
install -d -m 700 "$GNUPGHOME"

decifrar() {
  gpg --batch --quiet --no-symkey-cache --pinentry-mode loopback --passphrase-file "$LOGICO_CIFRA_ARQUIVO" \
    --decrypt "$1"
}

# 2. Papeis e permissoes do servidor. Num banco novo do produto os papeis ja existem (com as senhas
#    novas, do script de iniciacao): os avisos "already exists" sao esperados e nao param a restauracao.
if [ "$RESTAURAR_GLOBAIS" = "ligado" ] && [ -z "$RESTAURAR_COMO" ]; then
  log "restaurando papeis e permissoes (avisos de papel que ja existe sao esperados)"
  decifrar "$pasta/globais.sql.gpg" > "$TEMP/globais.sql" || { log "a copia nao abriu: senha de cifra errada?"; exit 4; }
  psql -X -q -v ON_ERROR_STOP=0 --dbname=postgres -f "$TEMP/globais.sql" > "$TEMP/globais.log" 2>&1 || true
  grep -v 'already exists' "$TEMP/globais.log" >&2 || true
  rm -f -- "$TEMP/globais.sql"
fi

# 3. Cada banco.
for banco in "${bancos[@]}"; do
  arquivo="$pasta/$banco.dump.gpg"
  [ -s "$arquivo" ] || { log "a copia nao tem o banco $banco"; exit 2; }
  if [ -n "$RESTAURAR_COMO" ]; then
    log "restaurando $banco como $RESTAURAR_COMO (ensaio)"
    createdb --template=template0 "$RESTAURAR_COMO"
    decifrar "$arquivo" | pg_restore --exit-on-error --dbname="$RESTAURAR_COMO"
  else
    log "restaurando $banco (apaga e cria de novo)"
    decifrar "$arquivo" | pg_restore --exit-on-error --clean --if-exists --create --dbname=postgres
  fi
  alvo="${RESTAURAR_COMO:-$banco}"
  tem_marca="$(psql -X -At --dbname="$alvo" \
    -c "SELECT to_regprocedure('public.continuidade_marcar_restaurado(text,text)') IS NOT NULL")"
  if [ "$tem_marca" = "t" ]; then
    # Pela entrada padrao: o psql so troca :'quem' e :'motivo' (com aspas seguras) fora do -c.
    psql -X -q -At -v ON_ERROR_STOP=1 --dbname="$alvo" -v quem="$RESTAURAR_QUEM" \
      -v motivo="copia logica $carimbo" > /dev/null <<'SQL'
SELECT continuidade_marcar_restaurado(:'quem', :'motivo');
SQL
    log "marca restaurado gravada em $alvo: a porta de saida fecha na proxima partida do despachante"
  fi
done
log "pronto. Siga o roteiro (docs/restauracao.md): cofre, gateway e a decisao do ADMIN de reabrir a saida."
