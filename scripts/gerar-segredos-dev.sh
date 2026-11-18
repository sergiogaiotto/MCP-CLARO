#!/usr/bin/env bash
# Gera segredos e certificados internos SO para desenvolvimento e teste, em .segredos/.
# Nunca use estes arquivos em piloto ou producao. A pasta .segredos/ fica fora do Git.
set -euo pipefail
cd "$(dirname "$0")/.."
DEST=.segredos
mkdir -p "$DEST"
chmod 700 "$DEST"

aleatorio() { openssl rand -base64 48 | tr -d '\n=/+' | cut -c1-40; }

for nome in db_migracao db_entrada db_despachante db_rotinas db_grafana_leitor db_cofre db_login db_grafana \
            webhook_assinatura grafana_webhook smtp cofre_chave_static login_admin grafana_admin; do
  [ -s "$DEST/$nome" ] || aleatorio > "$DEST/$nome"
done
# Chave do selo static do OpenBao: 32 bytes em hexadecimal.
[ -s "$DEST/cofre_selo.key" ] || openssl rand -hex 32 > "$DEST/cofre_selo.key"

# Autoridade interna e certificados de cada peca (mTLS na rede interna).
if [ ! -s "$DEST/ca_interna.pem" ]; then
  openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 365 \
    -subj "/CN=MCP-CLARO autoridade interna de teste" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -addext "subjectKeyIdentifier=hash" \
    -keyout "$DEST/ca_interna_key.pem" -out "$DEST/ca_interna.pem" 2>/dev/null
fi
for peca in portao entrada despachante rotinas banco cofre login grafana backup vigia; do
  [ -s "$DEST/${peca}_cert.pem" ] && continue
  openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=$peca" \
    -keyout "$DEST/${peca}_key.pem" -out "$DEST/${peca}.csr" 2>/dev/null
  printf "subjectAltName=DNS:%s,DNS:localhost,IP:127.0.0.1\nextendedKeyUsage=serverAuth,clientAuth\nkeyUsage=critical,digitalSignature,keyAgreement\nbasicConstraints=critical,CA:FALSE\nauthorityKeyIdentifier=keyid\nsubjectKeyIdentifier=hash\n" "$peca" \
    > "$DEST/${peca}.ext"
  openssl x509 -req -in "$DEST/${peca}.csr" -CA "$DEST/ca_interna.pem" -CAkey "$DEST/ca_interna_key.pem" \
    -CAcreateserial -days 90 -extfile "$DEST/${peca}.ext" -out "$DEST/${peca}_cert.pem" 2>/dev/null
  rm -f "$DEST/${peca}.csr" "$DEST/${peca}.ext"
done
chmod 644 "$DEST"/*  # a pasta fica 700; os conteineres leem como usuarios sem privilegio
echo "Segredos de desenvolvimento em $DEST/ (fora do Git)."
