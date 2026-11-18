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

## Desenvolvimento e teste

`scripts/gerar-segredos-dev.sh` cria segredos aleatórios e uma autoridade interna de teste em `.segredos/`, fora do
Git. Esses arquivos nunca vão para piloto ou produção. A partida recusa `APP_ENV=piloto` ou `producao` sem autoridade
interna e sem `DB_SSLMODE=verify-full`.

## Se um segredo vazar

1. Bloqueio de um toque no aplicativo ou na pessoa afetada.
2. Trocar o segredo na origem (banco, provedor de login, sistema conectado).
3. Para chaves de sistemas: revogar no sistema e apagar a linha cifrada; a pessoa autoriza de novo.
4. Registrar o motivo no extrato e conferir o último fechamento no vigia.
