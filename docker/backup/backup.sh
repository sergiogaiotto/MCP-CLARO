#!/usr/bin/env bash
# Conteiner de backup do MCP-CLARO (pgBackRest 2.60.1).
#
#   backup.sh servir        (padrao) servidor TLS do pgBackRest + agenda das copias e da copia logica
#   backup.sh completa | diferencial | incremental    uma copia agora
#   backup.sh logico        uma copia logica (pg_dump) agora
#   backup.sh conferir      pgbackrest check (o banco manda WAL e o repositorio responde)
#   backup.sh verificar     pgbackrest verify (le o repositorio inteiro e confere cada arquivo)
#   backup.sh info          copias guardadas
#   backup.sh saude         para o healthcheck: servidor TLS no ar e copia recente
#
# Agenda (UTC; parametros pelo ambiente):
#   BACKUP_COMPLETA_DIA_SEMANA=7 (domingo) e BACKUP_HORA=3      copia completa por semana
#   BACKUP_HORA=3                                              diferencial nos outros dias
#   BACKUP_INCREMENTAL_HORAS=1                                 incremental a cada N horas
#   BACKUP_LOGICO_HORA=4                                       copia logica (pg_dump) por dia
#   BACKUP_VERIFICAR_DIA_SEMANA=6                              verify por semana, na BACKUP_HORA
#   BACKUP_SAUDE_HORAS=26                                      idade maxima da ultima copia no healthcheck
# Sem nenhuma copia completa guardada, a primeira sai assim que o banco responder.
set -euo pipefail

STANZA="${PGBACKREST_STANZA:-mcp-claro}"
LOCAL="${BACKUP_PASTA_LOCAL:-/var/lib/mcp-claro-backup}"
ESTADO="$LOCAL/agenda"
: "${BACKUP_COMPLETA_DIA_SEMANA:=7}" "${BACKUP_HORA:=3}" "${BACKUP_INCREMENTAL_HORAS:=1}"
: "${BACKUP_LOGICO_HORA:=4}" "${BACKUP_VERIFICAR_DIA_SEMANA:=6}" "${BACKUP_SAUDE_HORAS:=26}"

log() { printf '%s backup: %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

segredo() {
  # segredo <arquivo>: conteudo sem quebra de linha final; vazio se o arquivo nao existir.
  if [ -s "$1" ]; then tr -d '\n' < "$1"; fi
}

preparar() {
  install -d -m 700 /tmp/pgbackrest-tls
  install -m 600 "${BACKUP_CERT_ARQUIVO:-/run/secrets/backup_cert.pem}" /tmp/pgbackrest-tls/cert.pem
  install -m 600 "${BACKUP_CHAVE_ARQUIVO:-/run/secrets/backup_key.pem}" /tmp/pgbackrest-tls/key.pem
  install -m 644 "${BACKUP_CA_ARQUIVO:-/run/secrets/ca_interna.pem}" /tmp/pgbackrest-tls/ca.pem
  PGBACKREST_REPO1_CIPHER_PASS="$(segredo "${BACKUP_CIFRA_ARQUIVO:-/run/secrets/backup_cifra}")"
  if [ -z "$PGBACKREST_REPO1_CIPHER_PASS" ]; then
    log "falta a senha de cifra do repositorio (segredo backup_cifra); nada foi copiado."
    exit 2
  fi
  export PGBACKREST_REPO1_CIPHER_PASS
  # Repositorio S3 compativel (piloto e producao): chaves por segredo, nunca no ambiente do Compose.
  if [ "${PGBACKREST_REPO1_TYPE:-posix}" = "s3" ]; then
    PGBACKREST_REPO1_S3_KEY="$(segredo /run/secrets/backup_s3_chave)"
    PGBACKREST_REPO1_S3_KEY_SECRET="$(segredo /run/secrets/backup_s3_segredo)"
    export PGBACKREST_REPO1_S3_KEY PGBACKREST_REPO1_S3_KEY_SECRET
  fi
  install -d -m 700 "$ESTADO" "$LOCAL/logico"
}

pgbr() { pgbackrest --stanza="$STANZA" "$@"; }

tem_completa() {
  local saida
  saida="$(pgbr --output=json info 2>/dev/null)" || return 1
  [[ "$saida" == *'"type":"full"'* ]]
}

copiar() {
  # copiar <full|diff|incr>
  log "comecando copia $1"
  if pgbr --type="$1" backup; then
    log "copia $1 terminada"
    pgbr expire || log "a limpeza das copias vencidas falhou; tenta de novo na proxima"
    return 0
  fi
  log "a copia $1 falhou; tenta de novo na proxima volta"
  return 1
}

servidor_tls() {
  while true; do
    pgbackrest server || true
    log "o servidor TLS do pgBackRest parou; sobe de novo em 5 segundos"
    sleep 5
  done
}

esperar_stanza() {
  # O banco precisa estar no ar, com o servidor TLS dele. stanza-create nao muda nada se ja existir.
  local tentativa=0
  until pgbr stanza-create; do
    tentativa=$((tentativa + 1))
    if [ $((tentativa % 30)) -eq 1 ]; then
      log "o repositorio ainda nao esta pronto (banco fora do ar, ou outra instalacao no mesmo repositorio?)."
      log "se o banco foi refeito por copia logica, crie um repositorio novo (docs/restauracao.md)."
    fi
    sleep 10
  done
  pgbr check || log "pgbackrest check falhou: o WAL do banco ainda nao chegou; confira o archive_command"
}

lido() { cat "$ESTADO/$1" 2>/dev/null || true; }

agenda() {
  while true; do
    local dia hora dia_semana semana fatia
    dia="$(date -u +%F)"
    hora=$((10#$(date -u +%H)))
    dia_semana="$(date -u +%u)"
    semana="$(date -u +%G-%V)"
    fatia=$(( $(date -u +%s) / (3600 * BACKUP_INCREMENTAL_HORAS) ))

    if ! tem_completa; then
      copiar full && { echo "$semana" > "$ESTADO/completa"; echo "$dia" > "$ESTADO/diferencial"; echo "$fatia" > "$ESTADO/incremental"; }
    elif [ "$hora" -ge "$BACKUP_HORA" ] && [ "$dia_semana" = "$BACKUP_COMPLETA_DIA_SEMANA" ] \
         && [ "$(lido completa)" != "$semana" ]; then
      copiar full && { echo "$semana" > "$ESTADO/completa"; echo "$dia" > "$ESTADO/diferencial"; echo "$fatia" > "$ESTADO/incremental"; }
    elif [ "$hora" -ge "$BACKUP_HORA" ] && [ "$(lido diferencial)" != "$dia" ]; then
      copiar diff && { echo "$dia" > "$ESTADO/diferencial"; echo "$fatia" > "$ESTADO/incremental"; }
    elif [ "$(lido incremental)" != "$fatia" ]; then
      copiar incr && echo "$fatia" > "$ESTADO/incremental"
    fi

    if [ "$hora" -ge "$BACKUP_LOGICO_HORA" ] && [ "$(lido logico)" != "$dia" ]; then
      if /usr/local/bin/logico.sh; then echo "$dia" > "$ESTADO/logico"; else log "a copia logica falhou"; fi
    fi
    if [ "$hora" -ge "$BACKUP_HORA" ] && [ "$dia_semana" = "$BACKUP_VERIFICAR_DIA_SEMANA" ] \
       && [ "$(lido verificar)" != "$semana" ]; then
      if pgbr verify; then echo "$semana" > "$ESTADO/verificar"; else log "pgbackrest verify achou problema no repositorio"; fi
    fi
    sleep 60
  done
}

saude() {
  # Servidor TLS no ar e ultima copia com menos de BACKUP_SAUDE_HORAS.
  (exec 3<>/dev/tcp/127.0.0.1/8432) 2>/dev/null || { echo "servidor TLS do pgBackRest fora do ar"; return 1; }
  local info ultima agora
  info="$(pgbr --output=json info)" || { echo "o repositorio nao respondeu"; return 1; }
  ultima="$(grep -o '"stop":[0-9]*' <<< "$info" | cut -d: -f2 | sort -n | tail -n 1 || true)"
  agora="$(date -u +%s)"
  if [ -z "$ultima" ] || [ $((agora - ultima)) -gt $((BACKUP_SAUDE_HORAS * 3600)) ]; then
    echo "a ultima copia tem mais de $BACKUP_SAUDE_HORAS horas (ou nao ha copia)"
    return 1
  fi
}

comando="${1:-servir}"
preparar
case "$comando" in
  servir)
    servidor_tls &
    esperar_stanza
    agenda
    ;;
  completa) copiar full ;;
  diferencial) copiar diff ;;
  incremental) copiar incr ;;
  logico) exec /usr/local/bin/logico.sh ;;
  conferir) pgbr check ;;
  verificar) pgbr verify ;;
  info) pgbr info ;;
  saude) saude ;;
  *) echo "uso: $0 [servir|completa|diferencial|incremental|logico|conferir|verificar|info|saude]" >&2; exit 2 ;;
esac
