# Roteiro de segredos

## Regra

Nenhum segredo fica em arquivo do repositório nem em variável de ambiente com valor. Senhas e chaves vêm de
**segredos do Docker** (arquivos em `/run/secrets`, apontados por variáveis `*_FILE`) ou do **cofre** (OpenBao). A
varredura de segredos (gitleaks) roda a cada mudança no CI.

## Onde fica cada segredo

| Segredo | Onde fica | Quem lê | Rodízio |
| --- | --- | --- | --- |
| Senha de cada papel no banco | Segredo do Docker `db_<papel>` | O próprio papel | Na troca de operador ou a cada 180 dias |
| Certificados internos (mTLS) | Segredos `<peça>_cert.pem` e `<peça>_key.pem`, autoridade `ca_interna.pem` | Cada peça | 90 dias, ou PKI do OpenBao |
| Chaves das pessoas e dos programas para os sistemas | Tabela `chaves_cifradas`, cifradas pelo Transit do OpenBao | Só o despachante decifra | Renovação pelo próprio sistema |
| Chaves guardadas para servidores de terceiros (Fase 3) | Tabela `chaves_cifradas`, cifradas com a chave Transit `mcp-claro-chaves-terceiros` (`VAULT_TRANSIT_KEY_THIRD_PARTY`), separada da dos sistemas próprios (`mcp-claro-chaves`). As duas são `derived`: cada cifrado fica amarrado ao sistema | Só o despachante decifra. A chave aberta de uma chamada vai à sala de visitas, só pelo mTLS da rede `visitas`, e vive só no processo daquela chamada. Na troca do código e na renovação (OAuth), o despachante manda o código, a chave de renovação e o segredo do cliente direto ao endereço de troca de chave do terceiro, sem passar pela sala, e só depois de conferir que o endereço é `https://` e da internet | Renovação pelo próprio terceiro. Girar a chave Transit dos terceiros (no cofre, pela equipe que opera) não toca nos sistemas próprios |
| Certificados da sala de visitas (Fase 3) | Autoridade própria `ca_visitas.pem`, que emite só o certificado de servidor da sala (`visitas_cert.pem` e `visitas_key.pem`, só para servidor, `serverAuth`). Autoridade `ca_clientes_visitas.pem`, que emite só o certificado de cliente do despachante para a sala (`despachante_visitas_cliente_cert.pem` e `despachante_visitas_cliente_key.pem`). As chaves das duas autoridades (`ca_visitas_key.pem` e `ca_clientes_visitas_key.pem`) não vão a contêiner nenhum: servem só para emitir | A sala lê o próprio certificado, a chave dele, a `ca_clientes_visitas` e, só para a própria conferência de saúde, a `ca_visitas`. O despachante lê a `ca_visitas` e o certificado de cliente dele (`VISITS_CA_FILE`, `VISITS_CLIENT_CERT_FILE`, `VISITS_CLIENT_KEY_FILE`) | 90 dias, como os outros certificados internos |
| Chave do estado das confirmações | OpenBao, caminho separado | Entrada | `REQUEST_STATE_KEY_ROTATION_DAYS` |
| Login de máquina no cofre (AppRole) | Segredos `cofre_<papel>_role_id` e `cofre_<papel>_secret_id` (entrada, despachante, rotinas) | Cada papel, o seu | Token de 15 min, renovado; máximo de 1 h |
| Chave de destravar o cofre | `static`: segredo do Docker fora do backup. Piloto: partes Shamir com guardiões | Guardiões | Por decisão |
| Senha do SMTP e segredo dos webhooks | Segredos `smtp` e `webhook_assinatura` | Rotinas | 180 dias |
| Credenciais do vigia | Segredos próprios na segunda máquina | Vigia | Separadas das do gateway |
| Senha da cifra da cópia lógica | Segredo `backup_cifra`, guardado também fora das duas máquinas | Backup; roteiro de restauração | Na troca de operador |
| Usuário da cópia lógica (só leitura) | Segredo `db_backup` | Backup | 180 dias |
| Senha do ADMIN do Grafana | Segredo `grafana_admin` | Equipe que opera | 180 dias |
| Segredo do cliente das telas no provedor de login | Segredo `telas_cliente` | Entrada e provedor de login | 180 dias |
| Senha do webhook interno do Grafana | Segredo `grafana_webhook` | Grafana e rotinas | 180 dias |
| Chaves dos programas da ronda e do vigia | Segredos `PATROL_KEY_FILE` e `WATCH_KEY_FILE`; a pública vai ao provedor | Rotinas e vigia | `PROGRAM_KEY_MAX_AGE_DAYS` |

## Sala de visitas (Fase 3)

A sala de visitas fala MCP com os servidores de terceiros e fica exposta a eles. Por isso ela recebe o mínimo:

- **Nunca recebe:** senha de banco, login do cofre (AppRole), chave do estado das confirmações, segredo do SMTP ou
  dos webhooks, nem chave de outro sistema. A partida da sala recusa qualquer variável `DATABASE_*`, `DB_*` ou
  `VAULT_*`, e qualquer outro arquivo de segredo (`*_FILE`) além do próprio certificado. A sala não tem usuário no
  banco nem política no cofre.
- **Recebe só:** o próprio certificado e a chave dele, a `ca_clientes_visitas` (para aceitar só o despachante), a
  `ca_visitas` (só para a própria conferência de saúde) e, em cada chamada, a chave do terceiro daquela chamada,
  que o despachante abriu no cofre.
- **Não recebe o que é da troca de chave:** o código de autorização, a chave de renovação e o segredo do cliente do
  MCP-CLARO num terceiro ficam com o despachante, que faz a troca e a renovação (OAuth) direto com o endereço de
  troca de chave do terceiro.
- **Sem certificado de saúde:** a `ca_clientes_visitas` só emite o certificado do despachante. A conferência de
  saúde da sala só faz o aperto de mãos TLS, sem certificado de cliente.
- **Certificado só de servidor, de outra autoridade:** a chave do certificado da sala fica no contêiner exposto aos
  terceiros. Por isso ela vem da `ca_visitas`, e não da `ca_interna`: nem o cofre nem as portas internas aceitam
  esse certificado.

A chave Transit dos terceiros é criada por `scripts/iniciar-cofre.sh`, junto com a dos sistemas próprios, as duas
`derived`, não exportáveis e sem permissão de apagar. A política do despachante no cofre só cifra e decifra com as
duas. Na partida, o despachante confere que a chave dos terceiros existe e é `derived` (cifrar sem contexto tem de
ser recusado); se não, ele não sobe.

## Desenvolvimento e teste

`scripts/gerar-segredos-dev.sh` cria segredos aleatórios e uma autoridade interna de teste em `.segredos/`, fora do
Git. Esses arquivos nunca vão para piloto ou produção. A partida recusa `APP_ENV=piloto` ou `producao` sem autoridade
interna e sem `DB_SSLMODE=verify-full`.

## Se um segredo vazar

1. Bloqueio de um toque no aplicativo ou na pessoa afetada.
2. Trocar o segredo na origem (banco, provedor de login, sistema conectado).
3. Para chaves de sistemas: revogar no sistema e apagar a linha cifrada; a pessoa autoriza de novo.
4. Registrar o motivo no extrato e conferir o último fechamento no vigia.
5. Certificado da sala de visitas: trocar o certificado da sala e, se a suspeita for da autoridade, a `ca_visitas`
   inteira. O certificado da sala só serve para ela se apresentar ao despachante.
