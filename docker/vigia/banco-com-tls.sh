#!/usr/bin/env bash
# Partida do PostgreSQL do vigia (segunda maquina): TLS sempre, como o banco principal.
# O PostgreSQL quer a chave do certificado com dono postgres e modo 0600; os segredos chegam como
# arquivos montados, entao copiamos para uma pasta propria, fora do PGDATA, antes de subir.
set -euo pipefail
mkdir -p /var/lib/postgresql/tls
cp /run/secrets/banco_vigia_cert.pem /var/lib/postgresql/tls/cert.pem
cp /run/secrets/banco_vigia_key.pem /var/lib/postgresql/tls/key.pem
cp /run/secrets/ca_vigia.pem /var/lib/postgresql/tls/ca.pem
chown -R postgres:postgres /var/lib/postgresql/tls
chmod 700 /var/lib/postgresql/tls
chmod 600 /var/lib/postgresql/tls/key.pem
exec docker-entrypoint.sh "$@" \
  -c ssl=on \
  -c ssl_cert_file=/var/lib/postgresql/tls/cert.pem \
  -c ssl_key_file=/var/lib/postgresql/tls/key.pem \
  -c ssl_ca_file=/var/lib/postgresql/tls/ca.pem \
  -c ssl_min_protocol_version=TLSv1.2
