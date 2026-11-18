#!/usr/bin/env bash
# Copia logica (pg_dump) dos bancos do MCP-CLARO, para completar o pgBackRest.
#
# Cada copia e uma pasta com um carimbo de hora UTC (AAAAMMDDTHHMMSSZ):
#   globais.sql.gpg       papeis e permissoes do servidor, sem senhas (pg_dumpall --globals-only)
#   <banco>.dump.gpg      cada banco no formato custom do pg_dump
#   SHA256SUMS            conferencia dos arquivos cifrados
#   copia.txt             quando, de onde e com que versao (sem segredo)
# Tudo cifrado com gpg (AES-256, com conferencia de integridade) pela senha do segredo backup_cifra, a
# mesma do repositorio do pgBackRest. Essa senha nunca fica junto da copia (docs/restauracao.md).
#
# Ambiente (valores iniciais entre parenteses):
#   LOGICO_HOST (banco)  LOGICO_PORTA (5432)  LOGICO_USUARIO (mcp_claro_backup)
#   LOGICO_SENHA_ARQUIVO (/run/secrets/db_backup)  LOGICO_CIFRA_ARQUIVO (/run/secrets/backup_cifra)
#   LOGICO_SSLMODE (verify-full)  LOGICO_SSLROOTCERT (/run/secrets/ca_interna.pem)
#   LOGICO_BANCOS ("mcp_claro grafana cofre login")  LOGICO_DESTINO (/var/lib/mcp-claro-backup/logico)
#   LOGICO_GUARDA_DIAS (35)  LOGICO_ENVIO (local; ou s3, ou sftp: manda a pasta para fora da maquina)
#   s3:   usa PGBACKREST_REPO1_S3_ENDPOINT, _BUCKET e _REGION, as chaves dos segredos backup_s3_chave e
#         backup_s3_segredo, e LOGICO_S3_PREFIXO (mcp-claro/logico)
#   sftp: usa PGBACKREST_REPO1_SFTP_HOST, _HOST_USER e _PRIVATE_KEY_FILE, LOGICO_SFTP_PASTA
#         (/mcp-claro/logico) e LOGICO_SFTP_HOST_SHA256 (impressao da chave do servidor, em base64)
set -euo pipefail

: "${LOGICO_HOST:=banco}" "${LOGICO_PORTA:=5432}" "${LOGICO_USUARIO:=mcp_claro_backup}"
: "${LOGICO_SENHA_ARQUIVO:=/run/secrets/db_backup}" "${LOGICO_CIFRA_ARQUIVO:=/run/secrets/backup_cifra}"
: "${LOGICO_SSLMODE:=verify-full}" "${LOGICO_SSLROOTCERT:=/run/secrets/ca_interna.pem}"
: "${LOGICO_BANCOS:=mcp_claro grafana cofre login}" "${LOGICO_DESTINO:=/var/lib/mcp-claro-backup/logico}"
: "${LOGICO_GUARDA_DIAS:=35}" "${LOGICO_ENVIO:=local}"

log() { printf '%s logico: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

[ -s "$LOGICO_CIFRA_ARQUIVO" ] || { log "falta a senha de cifra ($LOGICO_CIFRA_ARQUIVO)"; exit 2; }
[ -s "$LOGICO_SENHA_ARQUIVO" ] || { log "falta a senha do usuario $LOGICO_USUARIO ($LOGICO_SENHA_ARQUIVO)"; exit 2; }

TEMP="$(mktemp -d)"
trap 'rm -rf -- "$TEMP"' EXIT
umask 077
# Senha do banco num .pgpass temporario (nunca na linha de comando), com ":" e "\" escapados.
printf '*:*:*:%s:%s\n' "$LOGICO_USUARIO" "$(tr -d '\n' < "$LOGICO_SENHA_ARQUIVO" | sed 's/[\\:]/\\&/g')" \
  > "$TEMP/pgpass"
export PGPASSFILE="$TEMP/pgpass" PGHOST="$LOGICO_HOST" PGPORT="$LOGICO_PORTA" PGUSER="$LOGICO_USUARIO"
export PGSSLMODE="$LOGICO_SSLMODE" PGSSLROOTCERT="$LOGICO_SSLROOTCERT" PGCONNECT_TIMEOUT=10
export PGAPPNAME=mcp-claro-copia-logica GNUPGHOME="$TEMP/gnupg"
install -d -m 700 "$GNUPGHOME"

cifrar() {
  gpg --batch --yes --quiet --no-symkey-cache --pinentry-mode loopback --passphrase-file "$LOGICO_CIFRA_ARQUIVO" \
    --symmetric --cipher-algo AES256 --compress-algo none --output "$1"
}

carimbo="$(date -u +%Y%m%dT%H%M%SZ)"
[ -d "$LOGICO_DESTINO" ] || install -d -m 700 "$LOGICO_DESTINO"
parcial="$LOGICO_DESTINO/.$carimbo.parcial"
final="$LOGICO_DESTINO/$carimbo"
install -d -m 700 "$parcial"

log "comecando a copia logica $carimbo de: $LOGICO_BANCOS"
pg_dumpall --globals-only --no-role-passwords --database=postgres | cifrar "$parcial/globais.sql.gpg"
for banco in $LOGICO_BANCOS; do
  pg_dump --format=custom --compress=zstd --dbname="$banco" | cifrar "$parcial/$banco.dump.gpg"
done
{
  echo "copia logica do MCP-CLARO"
  echo "carimbo: $carimbo"
  echo "servidor: $LOGICO_HOST:$LOGICO_PORTA"
  echo "bancos: $LOGICO_BANCOS"
  echo "versao: $(pg_dump --version)"
  echo "cifra: gpg AES256 simetrica, senha do segredo backup_cifra"
} > "$parcial/copia.txt"
(cd "$parcial" && sha256sum -- *.gpg copia.txt > SHA256SUMS)
chmod 600 "$parcial"/*
mv -- "$parcial" "$final"
log "copia logica pronta em $final"

enviar_s3() {
  local endpoint="${PGBACKREST_REPO1_S3_ENDPOINT:?falta PGBACKREST_REPO1_S3_ENDPOINT}"
  local bucket="${PGBACKREST_REPO1_S3_BUCKET:?falta PGBACKREST_REPO1_S3_BUCKET}"
  local regiao="${PGBACKREST_REPO1_S3_REGION:?falta PGBACKREST_REPO1_S3_REGION}"
  local prefixo="${LOGICO_S3_PREFIXO:-mcp-claro/logico}"
  local chave segredo arquivo
  chave="$(tr -d '\n' < /run/secrets/backup_s3_chave)"
  segredo="$(tr -d '\n' < /run/secrets/backup_s3_segredo)"
  for arquivo in "$final"/*; do
    # A credencial vai pela entrada padrao (--config -), nunca na linha de comando.
    printf 'user = "%s:%s"\n' "$chave" "$segredo" | curl --config - --fail --silent --show-error \
      --aws-sigv4 "aws:amz:${regiao}:s3" --upload-file "$arquivo" \
      "https://${endpoint}/${bucket}/${prefixo}/${carimbo}/$(basename "$arquivo")"
  done
}

enviar_sftp() {
  local host="${PGBACKREST_REPO1_SFTP_HOST:?falta PGBACKREST_REPO1_SFTP_HOST}"
  local usuario="${PGBACKREST_REPO1_SFTP_HOST_USER:?falta PGBACKREST_REPO1_SFTP_HOST_USER}"
  local chave="${PGBACKREST_REPO1_SFTP_PRIVATE_KEY_FILE:?falta PGBACKREST_REPO1_SFTP_PRIVATE_KEY_FILE}"
  local impressao="${LOGICO_SFTP_HOST_SHA256:?falta LOGICO_SFTP_HOST_SHA256}"
  local pasta="${LOGICO_SFTP_PASTA:-/mcp-claro/logico}"
  local arquivo
  for arquivo in "$final"/*; do
    curl --fail --silent --show-error --ftp-create-dirs --key "$chave" --hostpubsha256 "$impressao" \
      --upload-file "$arquivo" "sftp://${usuario}@${host}${pasta}/${carimbo}/"
  done
}

case "$LOGICO_ENVIO" in
  local) log "copia guardada so neste volume (LOGICO_ENVIO=local; em producao use s3 ou sftp)" ;;
  s3) enviar_s3; log "copia enviada ao S3" ;;
  sftp) enviar_sftp; log "copia enviada por SFTP" ;;
  *) log "LOGICO_ENVIO desconhecido: $LOGICO_ENVIO"; exit 2 ;;
esac

# Guarda: apaga as copias locais mais velhas que LOGICO_GUARDA_DIAS e sobras de copias interrompidas.
find "$LOGICO_DESTINO" -mindepth 1 -maxdepth 1 -type d -name '20*' -mtime +"$LOGICO_GUARDA_DIAS" -exec rm -rf -- {} +
find "$LOGICO_DESTINO" -mindepth 1 -maxdepth 1 -type d -name '.*.parcial' -mtime +1 -exec rm -rf -- {} +
echo "$final"
