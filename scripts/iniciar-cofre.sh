#!/bin/sh
# Prepara o cofre (OpenBao 2.7) para desenvolvimento e teste. Idempotente: rodar de novo nao refaz o que ja existe.
#
# Roda num conteiner com a CLI `bao` (servico `cofre-iniciar` do Compose):
#   docker compose -f docker/compose.yml run --rm cofre-iniciar
# Fora do conteiner, sem `bao` no PATH, o proprio script chama esse servico.
#
# Contra um OpenBao recem-subido:
#   1. inicia o cofre se preciso (com o selo static ele destrava sozinho) e guarda o token raiz e a chave de
#      recuperacao em .segredos/ (so teste: no piloto o cofre usa Shamir com guardioes, decisao 6);
#   2. liga o Transit e cria a chave `mcp-claro-chaves`, derived: cada texto cifrado fica amarrado ao contexto
#      (sistema, dono) e so o mesmo contexto decifra;
#   3. liga o KV v2 e cria a chave inicial do estado das confirmacoes (32 bytes aleatorios, ate 5 versoes);
#   4. grava as politicas de docker/cofre/politicas/ e cria os papeis AppRole entrada, despachante e rotinas,
#      com token curto e renovavel;
#   5. grava role_id e secret_id de cada papel em .segredos/cofre_<papel>_role_id e .segredos/cofre_<papel>_secret_id.
#      Um secret_id que ainda entra fica; um que nao entra mais (cofre refeito) e trocado.
#
# Variaveis: BAO_ADDR, BAO_CACERT, BAO_CLIENT_CERT e BAO_CLIENT_KEY (da CLI); BAO_TOKEN (se o cofre ja foi
# iniciado por outro meio); COFRE_SEGREDOS e COFRE_POLITICAS (pastas); COFRE_TOKEN_TTL e COFRE_TOKEN_MAX_TTL.
# Os nomes abaixo precisam bater com as politicas e com VAULT_TRANSIT_MOUNT, VAULT_TRANSIT_KEY, VAULT_KV_MOUNT e
# VAULT_STATE_KEY_PATH do gateway.
set -eu
umask 077

AQUI=$(cd "$(dirname "$0")" && pwd)

if ! command -v bao >/dev/null 2>&1; then
  if command -v docker >/dev/null 2>&1; then
    cd "$AQUI/.."
    if [ -n "${COMPOSE_FILE:-}" ]; then
      exec docker compose run --rm cofre-iniciar
    fi
    exec docker compose -f docker/compose.yml run --rm cofre-iniciar
  fi
  echo "iniciar-cofre: falta a CLI bao (ou o Docker, para rodar o servico cofre-iniciar)." >&2
  exit 1
fi

DEST=${COFRE_SEGREDOS:-$AQUI/../.segredos}
POLITICAS=${COFRE_POLITICAS:-$AQUI/../docker/cofre/politicas}
TRANSIT=transit
CHAVE=mcp-claro-chaves
KV=kv
ESTADO=mcp-claro/estado-confirmacoes
APPROLE=approle
PAPEIS="entrada despachante rotinas"
TOKEN_TTL=${COFRE_TOKEN_TTL:-15m}
TOKEN_MAX_TTL=${COFRE_TOKEN_MAX_TTL:-1h}

falhar() {
  echo "iniciar-cofre: $*" >&2
  exit 1
}

[ -d "$DEST" ] || falhar "a pasta $DEST nao existe; rode antes scripts/gerar-segredos-dev.sh."
for papel in $PAPEIS; do
  [ -s "$POLITICAS/$papel.hcl" ] || falhar "falta a politica $POLITICAS/$papel.hcl."
done
# Os arquivos ficam com o dono da pasta (o conteiner roda como root so para isso).
DONO=$(stat -c '%u:%g' "$DEST")

# gravar <arquivo> <modo>: grava o que vier pela entrada padrao, de uma vez so (arquivo temporario e mv).
# Grava no mesmo arquivo (mesmo inode): os papeis do Compose montam estes arquivos como segredos
# antes de o cofre-iniciar rodar, entao trocar o arquivo por outro deixaria o papel lendo o antigo.
gravar() {
  tmp="$1.tmp.$$"
  cat >"$tmp"
  [ -e "$1" ] || : >"$1"
  cat "$tmp" >"$1"
  rm -f "$tmp"
  chmod "$2" "$1"
  chown "$DONO" "$1" 2>/dev/null || true
}

# bao status: 0 destravado, 2 trancado ou nao iniciado, 1 sem resposta.
situacao() {
  set +e
  bao status >/dev/null 2>&1
  rc=$?
  set -e
  echo "$rc"
}

esperar() { # esperar <codigos aceitos> <o que>
  i=0
  while :; do
    rc=$(situacao)
    case " $1 " in *" $rc "*) return 0 ;; esac
    i=$((i + 1))
    [ "$i" -lt 60 ] || falhar "o cofre em ${BAO_ADDR:-?} nao ficou $2 em 2 minutos."
    sleep 2
  done
}

esperar "0 2" "acessivel"

if ! bao status -format=json 2>/dev/null | grep -Eq '"initialized": *true'; then
  echo "iniciar-cofre: iniciando o cofre."
  bao operator init -recovery-shares=1 -recovery-threshold=1 -format=json | gravar "$DEST/cofre_inicio.json" 600
  sed -n 's/.*"root_token": *"\([^"]*\)".*/\1/p' "$DEST/cofre_inicio.json" | tr -d '\n' \
    | gravar "$DEST/cofre_raiz_token" 600
  [ -s "$DEST/cofre_raiz_token" ] || falhar "o cofre nao devolveu token raiz ao iniciar."
fi
esperar "0" "destravado"

if [ -z "${BAO_TOKEN:-}" ]; then
  [ -s "$DEST/cofre_raiz_token" ] || falhar "o cofre ja foi iniciado e falta $DEST/cofre_raiz_token (ou BAO_TOKEN)."
  BAO_TOKEN=$(cat "$DEST/cofre_raiz_token")
  export BAO_TOKEN
fi

montado() { bao secrets list -format=json | grep -q "\"$1/\""; }

# Transit: chave do gateway, derived (contexto obrigatorio), nao exportavel e sem apagar.
montado "$TRANSIT" || bao secrets enable -path="$TRANSIT" transit >/dev/null
if ! bao read "$TRANSIT/keys/$CHAVE" >/dev/null 2>&1; then
  bao write -f "$TRANSIT/keys/$CHAVE" type=aes256-gcm96 derived=true exportable=false >/dev/null
fi
[ "$(bao read -field=derived "$TRANSIT/keys/$CHAVE")" = "true" ] \
  || falhar "a chave $TRANSIT/keys/$CHAVE existe sem derived=true; ela nao amarra o contexto."

# KV v2: chave comum do estado das confirmacoes, num caminho so dela. A primeira escrita espera o KV ficar pronto.
montado "$KV" || bao secrets enable -path="$KV" -version=2 kv >/dev/null
i=0
until bao kv metadata put -mount="$KV" -max-versions=5 "$ESTADO" >/dev/null 2>&1; do
  i=$((i + 1))
  [ "$i" -lt 30 ] || falhar "o KV $KV nao ficou pronto."
  sleep 1
done
if ! bao kv get -mount="$KV" "$ESTADO" >/dev/null 2>&1; then
  bao write -f -field=random_bytes sys/tools/random/32 format=base64 | tr -d '\n' \
    | bao kv put -mount="$KV" "$ESTADO" chave=- >/dev/null
fi

# Politicas e papeis AppRole: um por papel do gateway, token curto e renovavel.
bao auth list -format=json | grep -q "\"$APPROLE/\"" || bao auth enable -path="$APPROLE" approle >/dev/null
for papel in $PAPEIS; do
  bao policy write "mcp-claro-$papel" "$POLITICAS/$papel.hcl" >/dev/null
  bao write "auth/$APPROLE/role/$papel" token_policies="mcp-claro-$papel" token_type=service \
    token_ttl="$TOKEN_TTL" token_max_ttl="$TOKEN_MAX_TTL" secret_id_ttl=0 secret_id_num_uses=0 >/dev/null

  bao read -field=role_id "auth/$APPROLE/role/$papel/role-id" | tr -d '\n' | gravar "$DEST/cofre_${papel}_role_id" 644
  rid=$(cat "$DEST/cofre_${papel}_role_id")
  sid="$DEST/cofre_${papel}_secret_id"
  vale=nao
  if [ -s "$sid" ]; then
    if tok=$(tr -d '\n' <"$sid" | BAO_TOKEN='' bao write -field=token "auth/$APPROLE/login" role_id="$rid" secret_id=- 2>/dev/null); then
      vale=sim
      BAO_TOKEN="$tok" bao token revoke -self >/dev/null 2>&1 || true
    fi
  fi
  if [ "$vale" = nao ]; then
    bao write -f -field=secret_id "auth/$APPROLE/role/$papel/secret-id" | tr -d '\n' | gravar "$sid" 644
  fi
done

echo "iniciar-cofre: cofre pronto (transit/$CHAVE, $KV/$ESTADO, AppRoles: $PAPEIS). Credenciais em $DEST/."
