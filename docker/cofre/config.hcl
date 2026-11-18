# Cofre do MCP-CLARO: OpenBao 2.7.2 no Compose de desenvolvimento e teste.
# Guarda so as chaves de cifra (Transit) e a chave comum do estado das confirmacoes (KV v2).
# Partida: docker/cofre/partida.sh (confere o selo e le a senha do banco). Preparo: scripts/iniciar-cofre.sh.

ui                 = false
api_addr           = "https://cofre:8200"
disable_clustering = true
log_level          = "info"
log_format         = "json"

# Armazenamento no PostgreSQL do produto: banco e usuario `cofre` (docker/postgres/iniciar/10-papeis.sh).
# A senha nao fica aqui: vem do segredo db_cofre em PGPASSWORD, que o pgx junta a esta URL.
# verify-full confere a autoridade interna e o nome `banco` no certificado. O OpenBao cria a tabela.
storage "postgresql" {
  connection_url      = "postgres://cofre@banco:5432/cofre?sslmode=verify-full&sslrootcert=/run/secrets/ca_interna.pem&application_name=openbao"
  table               = "openbao_kv_store"
  max_connect_retries = 10
}

# Selo static (VAULT_SEAL_MODE=static, so teste; decisao 6): a chave de 32 bytes em hexadecimal vem do segredo
# do Docker cofre_selo.key, fora do backup, e o cofre destrava sozinho a cada partida. O OpenBao le o arquivo como
# esta: 64 caracteres, sem quebra de linha. Para trocar a chave, a atual passa para previous_key/previous_key_id e a
# nova entra em current_key, com outro current_key_id.
seal "static" {
  current_key_id = "mcp-claro-selo-1"
  current_key    = "file:///run/secrets/cofre_selo.key"
}

# Porta interna com TLS da autoridade interna e certificado de cliente obrigatorio (mTLS).
listener "tcp" {
  address                            = "0.0.0.0:8200"
  tls_cert_file                      = "/run/secrets/cofre_cert.pem"
  tls_key_file                       = "/run/secrets/cofre_key.pem"
  tls_min_version                    = "tls12"
  tls_client_ca_file                 = "/run/secrets/ca_interna.pem"
  tls_require_and_verify_client_cert = "true"
}
