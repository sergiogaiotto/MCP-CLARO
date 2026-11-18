# Roteiro de restauração

Este roteiro diz, passo a passo, como pôr o banco do MCP-CLARO de volta a partir do backup, como destravar
o cofre depois, e como o gateway volta a atender. Foi escrito para quem opera o gateway, não para quem
programa. Leia inteiro antes de começar.

## O que você precisa saber antes

- O MCP-CLARO tem **um banco só**, o PostgreSQL. Lá ficam o gateway, o cofre (OpenBao), o login e o Grafana,
  cada um no seu banco.
- Há **duas formas de backup**, e as duas são restauradas em ensaio:
  - **pgBackRest**: cópia completa (uma vez por semana), diferencial (todo dia) e incremental (a cada hora),
    mais cada WAL, que é o registro contínuo das mudanças. Permite voltar a **qualquer momento** que o
    repositório cobre. É o caminho normal.
  - **Cópia lógica (pg_dump)**: uma por dia. Serve quando o repositório do pgBackRest não puder ser usado.
    Perde o que aconteceu depois da cópia.
- Tudo o que vai para o backup sai **cifrado**. Sem a senha da cifra, o backup não abre.
- Depois de qualquer restauração, o gateway volta **com a porta de saída fechada** e **com os aplicativos
  pausados**. Nada vai para sistema nenhum até uma pessoa ADMIN decidir reabrir, e cada aplicativo volta
  por decisão própria. Isso é de propósito: o banco voltou no tempo, e o que estava em andamento pode já ter
  acontecido ou não.

## O meio de destravar: onde fica (e onde nunca fica)

Para restaurar, você precisa de coisas que **nunca ficam junto do backup**. Se elas estivessem no mesmo
lugar, quem levasse o backup levaria também a chave dele.

| O quê | Para que serve | Onde fica | Onde nunca fica |
| --- | --- | --- | --- |
| Senha da cifra do backup (segredo `backup_cifra`) | Abrir o repositório do pgBackRest e as cópias lógicas | No cofre de senhas da organização, com duas pessoas nomeadas que conseguem abrir; uma cópia em papel lacrada, guardada em outro prédio | No repositório de backup, no bucket ou servidor SFTP, no volume `backup_local`, nas duas máquinas do produto fora de `.segredos/`, em e-mail ou chat |
| Meio de destravar o cofre (OpenBao) | Abrir as chaves de cifra; sem ele as chaves das pessoas não abrem | Teste: `cofre_selo.key` (modo `static`), segredo do Docker fora do backup. Piloto: partes Shamir 2 de 3 com os guardiões nomeados (decisão 6) | No backup. O banco restaurado traz o cofre cifrado, nunca a chave dele |
| Credencial do repositório fora das máquinas (chaves S3 ou chave SFTP) | Ler o repositório | Com a equipe que opera o gateway, no cofre de senhas da organização | No próprio repositório |
| Senha do dono do banco (segredo `db_migracao`) | Restaurar como dono | Segredo do Docker da máquina; numa máquina nova, criada de novo | No backup |

Confira a cada ensaio que as duas pessoas que abrem o cofre de senhas ainda conseguem abrir.

## Antes de começar, em qualquer caminho

1. **Anote** a hora do problema e o que aconteceu. Isso vai para o registro da restauração.
2. **Avise** o ADMIN da organização. Se houver suspeita de ataque, peça o bloqueio de um toque de tudo
   pelas telas antes de qualquer outra coisa.
3. **Separe** o meio de destravar (tabela acima). Numa máquina nova, ponha a senha da cifra em
   `.segredos/backup_cifra` e o resto dos segredos com `scripts/gerar-segredos-dev.sh` (teste) ou o
   processo de segredos da instalação (piloto e produção).
4. Os comandos abaixo usam este começo, que chamamos de `MC`:

   ```sh
   MC="docker compose -f docker/compose.yml -f docker/grafana/compose.grafana.yml -f docker/backup/compose.backup.yml"
   ```

## Caminho A: pgBackRest (o normal)

1. **Pare o gateway e o que usa o banco**, deixando o banco e o backup no ar:

   ```sh
   $MC stop portao entrada despachante rotinas grafana login cofre
   ```

2. **Veja as cópias guardadas** e escolha até onde voltar:

   ```sh
   $MC exec backup backup.sh info
   ```

   Sem escolha, o banco volta até o último WAL guardado (o mais perto possível do problema). Para voltar a
   um momento anterior ao problema (por exemplo, antes de um apagamento errado), anote o momento em UTC.

3. **Pare o banco**:

   ```sh
   $MC stop banco
   ```

4. **Restaure**. O comando recusa se o banco ainda estiver no ar.

   ```sh
   # ate o ultimo WAL guardado
   $MC run --rm --no-deps --entrypoint /usr/local/bin/restaurar.sh banco
   # ou ate um momento (UTC)
   $MC run --rm --no-deps --entrypoint /usr/local/bin/restaurar.sh banco --ate "2026-11-18 10:00:00+00"
   ```

   Numa máquina nova, o volume do banco começa vazio; o comando é o mesmo. Com repositório fora das
   máquinas (piloto e produção), as variáveis `PGBACKREST_REPO1_*` do backup precisam estar iguais às da
   instalação (ver "Repositório fora das duas máquinas").

5. **Suba o banco**. Ele lê os WAL do repositório, termina a recuperação sozinho e abre numa **linha do
   tempo nova**. Espere a recuperação acabar:

   ```sh
   $MC up -d banco
   $MC exec banco sh -c 'PGPASSWORD=$(cat /run/secrets/db_migracao) psql -At -U mcp_claro_migracao -d mcp_claro -c "SELECT pg_is_in_recovery()"'
   # repita ate aparecer "f"
   ```

6. **Grave a marca "restaurado"**, com o seu nome e o motivo. O despachante perceberia sozinho pela linha
   do tempo nova, mas a marca deixa registrado quem restaurou e por quê:

   ```sh
   $MC run --rm --no-deps --entrypoint python migrar -m mcp_claro.continuidade \
     marcar-restaurado --quem "Nome da pessoa" --motivo "Restauracao depois do problema de 18/11 as 10h"
   ```

7. **Destrave o cofre**. Suba o cofre; em teste (`static`) ele destrava sozinho com `cofre_selo.key`. No
   piloto (`shamir`), cada guardião entra com a sua parte (`bao operator unseal`), até completar 2 de 3.
   Enquanto o cofre estiver trancado, a porta de saída continua fechada e sai o aviso "cofre trancado".

   ```sh
   $MC up -d cofre
   ```

8. **Suba o resto** e faça uma **cópia completa nova**, que vira o novo ponto de partida:

   ```sh
   $MC up -d
   $MC exec backup backup.sh completa
   ```

   Na partida, o despachante confere a continuidade do banco: a marca (e a linha do tempo nova) fazem a
   porta de saída fechar e os aplicativos voltarem pausados. O extrato registra o que pode ter se perdido
   ("Pode faltar no extrato o que aconteceu entre ... e ...") e cada aplicativo pausado.

9. **Confira** antes de reabrir (passo "Reabrir a saída").

## Caminho B: cópia lógica (pg_dump)

Use quando o repositório do pgBackRest não puder ser usado. Tudo o que aconteceu depois da cópia escolhida
se perde.

1. **Pare tudo o que usa o banco**: o comando recria os bancos do gateway, do cofre, do login e do Grafana, e
   não pode haver ninguém ligado a eles.

   ```sh
   $MC stop portao entrada despachante rotinas grafana login cofre
   ```

2. **Escolha a cópia**. Cada uma é uma pasta com o carimbo da hora UTC:

   ```sh
   $MC exec backup ls /var/lib/mcp-claro-backup/logico
   ```

   Em produção, se a máquina foi perdida, baixe a pasta do S3 ou do SFTP para o volume `backup_local`
   (pasta `logico/`) da máquina nova.

3. **Restaure**. O comando confere cada arquivo (`SHA256SUMS`) antes de abrir, restaura papéis e
   permissões, recria cada banco como estava e **grava a marca "restaurado" sozinho**. Num banco novo, os
   avisos de papel que já existe são esperados.

   ```sh
   $MC --profile restauracao run --rm -e RESTAURAR_QUEM="Nome da pessoa" restaurar-logico \
     /var/lib/mcp-claro-backup/logico/20261118T040000Z
   ```

   Sem a marca, uma cópia lógica posta de volta no **mesmo** servidor não seria percebida: o identificador e
   a linha do tempo do PostgreSQL não mudam. Por isso o script sempre grava a marca no banco do gateway.

4. **Atenção aos fechamentos numa máquina nova.** Num servidor PostgreSQL novo, o contador de transações
   começa de novo. As linhas novas do extrato podem cair dentro de trechos já fechados, e a conferência
   desses fechamentos (`extrato_conferir`) deixa de bater, mesmo com a cópia íntegra. Até a correção no
   fechamento, registre no relatório da restauração o número do último fechamento da cópia e confira a cópia
   pelo `SHA256SUMS` e pelo vigia, não pela conta refeita. Na mesma máquina (ensaio com outro nome) isso não
   acontece.

5. **O repositório do pgBackRest antigo não serve mais** para este banco: depois de uma cópia lógica, o banco
   é outro para o pgBackRest. Deixe o repositório antigo como está (ele pode ter cópias úteis) e aponte o
   backup para um caminho novo (`PGBACKREST_REPO1_PATH`, por exemplo `/var/lib/pgbackrest/2026-11-18`, ou
   outro prefixo no bucket). O backup cria o repositório novo e faz a primeira cópia completa sozinho.

6. Siga os passos 7 e 8 do caminho A (cofre e o resto), e depois "Reabrir a saída".

## Reabrir a saída

Reabrir é **decisão de uma pessoa ADMIN, registrada**. Antes de reabrir, confira:

- `$MC run --rm --no-deps --entrypoint python migrar -m mcp_claro.continuidade situacao` mostra a última
  conferência, a linha do tempo e se a saída está fechada;
- o último fechamento guardado no **vigia** bate com o último fechamento do banco, ou a diferença está
  explicada pelo intervalo perdido;
- o cofre está destravado e a ronda das portas passou;
- o que estava em andamento no intervalo perdido foi conferido nos sistemas, com as pessoas.

Depois, a pessoa ADMIN reabre pelas telas. Pela linha de comando, com o identificador dela:

```sh
$MC run --rm --no-deps --entrypoint python despachante -m mcp_claro.continuidade \
  reabrir --quem admin:<id da pessoa ADMIN> --motivo "Conferido com o vigia e com as pessoas"
```

O identificador da pessoa ADMIN aparece nas telas do ADMIN, ou em
`SELECT id, nome FROM pessoas WHERE papel = 'admin'`. Os aplicativos **continuam pausados**: cada um volta
por decisão de quem responde por ele.

## Ensaio de restauração

A cada `BACKUP_RESTORE_DRILL_DAYS` (30 dias), a equipe ensaia os dois caminhos, numa máquina de ensaio
(nunca na de produção):

- **pgBackRest**: caminho A inteiro, até um momento escolhido, numa máquina de ensaio que lê o mesmo
  repositório só para leitura;
- **cópia lógica**: restauração com outro nome, sem apagar nada, e conferência do extrato:

  ```sh
  $MC --profile restauracao run --rm -e RESTAURAR_COMO=mcp_claro_ensaio -e RESTAURAR_GLOBAIS=desligado \
    restaurar-logico /var/lib/mcp-claro-backup/logico/<carimbo> mcp_claro
  ```

  e, no banco de ensaio, `SELECT extrato_conferir(<numero do ultimo fechamento>)` deve dar verdadeiro.
  Apague o banco de ensaio no fim.

Registre no extrato do ensaio: data, quem fez, quanto tempo levou, e se o meio de destravar abriu.

## Repositório fora das duas máquinas (piloto e produção)

O repositório do backup **não fica em nenhuma das duas máquinas** (a principal e a do vigia). Ele fica num
serviço de armazenamento que não é peça do produto, de outro provedor ou outra rede, com credencial só do
backup:

- **S3 compatível** (por exemplo MinIO, Ceph ou o serviço de objetos de um provedor): no serviço `backup`,
  `PGBACKREST_REPO1_TYPE=s3`, `PGBACKREST_REPO1_S3_BUCKET`, `PGBACKREST_REPO1_S3_ENDPOINT`,
  `PGBACKREST_REPO1_S3_REGION`, `PGBACKREST_REPO1_S3_URI_STYLE=path` (para MinIO e Ceph),
  `PGBACKREST_REPO1_PATH=/mcp-claro`, e as chaves nos segredos `backup_s3_chave` e `backup_s3_segredo`.
  Para a cópia lógica, `LOGICO_ENVIO=s3` manda cada pasta para o mesmo bucket (prefixo
  `LOGICO_S3_PREFIXO`). Ligue no bucket o versionamento e o bloqueio contra apagamento (object lock) pelo
  tempo da guarda; quem tem a chave do backup não deve conseguir apagar cópias antigas.
- **SFTP**: `PGBACKREST_REPO1_TYPE=sftp`, `PGBACKREST_REPO1_SFTP_HOST`, `PGBACKREST_REPO1_SFTP_HOST_USER`,
  `PGBACKREST_REPO1_SFTP_HOST_KEY_HASH_TYPE=sha256`, `PGBACKREST_REPO1_SFTP_HOST_FINGERPRINT`,
  `PGBACKREST_REPO1_SFTP_PRIVATE_KEY_FILE=/run/secrets/backup_sftp_chave` e `PGBACKREST_REPO1_PATH`. Para a
  cópia lógica, `LOGICO_ENVIO=sftp` e `LOGICO_SFTP_HOST_SHA256`.

O serviço `backup` está nas redes `interna` (banco) e `saida` (repositório). Em desenvolvimento, o
repositório é o volume separado `backup_repositorio` e a cópia lógica fica no volume `backup_local`.

## Como o gateway percebe a restauração

Na partida, o despachante lê o identificador (`system_identifier`) e a linha do tempo (`timeline_id`) do
PostgreSQL e compara com a última linha de `continuidade_banco`. Também compara o último fechamento guardado
no vigia com o último do banco. Se algo não bater, se o vigia não responder, ou se a última linha for a
marca "restaurado", ele, numa só transação:

1. fecha a porta de saída;
2. pausa os aplicativos ativos (menos as identidades da ronda e do vigia, que não alcançam sistema real);
3. registra no extrato o motivo, o intervalo que pode ter se perdido e cada aplicativo pausado;
4. grava a conferência em `continuidade_banco`, que é só de acrescentar.

Na primeira partida de um banco novo, ele só grava a linha.
