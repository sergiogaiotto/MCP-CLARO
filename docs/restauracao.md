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
  por decisão dela, um de cada vez. Isso é de propósito: o banco voltou no tempo, e o que estava em andamento
  pode já ter acontecido ou não.
- Os **números de protocolo** dados depois da cópia se perderam com ela, mas as pessoas e os aplicativos podem
  tê-los guardado. Por isso o número salta para frente na restauração (1.000.000 na marca "restaurado" e mais
  1.000.000 na conferência do despachante), para que nenhum deles seja dado de novo a outra ação. O protocolo
  (`CLR-AAAA-NNNNNNNN`) tem oito dígitos ou mais: depois de alguns saltos, ele pode ficar mais comprido.

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
   MC="docker compose -f docker/compose.yml"
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

6. **Grave a marca "restaurado"**, com o seu nome e o motivo, **antes de subir a entrada** (passo 9). Este
   passo não é opcional. A marca deixa registrado quem restaurou e por quê, e também faz o número de
   protocolo saltar 1.000.000 para frente. O despachante perceberia a volta sozinho pela linha do tempo nova,
   mas só na partida dele, e a entrada não espera o despachante para subir: sem a marca, um pedido que
   chegasse nesse meio-tempo poderia receber um número já dado antes da cópia.

   ```sh
   $MC run --rm --no-deps --entrypoint python migrar -m mcp_claro.continuidade \
     marcar-restaurado --quem "Nome da pessoa" --motivo "Restauracao depois do problema de 18/11 as 10h"
   ```

7. **Destrave o cofre**. Suba o cofre; em teste (`static`) ele destrava sozinho com `cofre_selo.key`. No
   piloto (`shamir`), cada guardião entra com a sua parte (`bao operator unseal`), até completar 2 de 3.
   Enquanto o cofre estiver trancado, as chaves dos sistemas não abrem, e nenhuma ação chega a um sistema. A
   porta de saída fecha no passo 8; não a reabra antes de destravar o cofre (veja "Reabrir a saída").

   ```sh
   $MC up -d cofre
   ```

   Depois do passo 8, o despachante confere o cofre a cada `VAULT_CHECK_SECONDS` (15 segundos no início). Se
   ele encontrar o cofre trancado, o ADMIN recebe um aviso. Quando o encontra destravado de novo, recebe outro.
   A saída continua fechada mesmo assim: quem a fechou foi a conferência do banco, e só o ADMIN a reabre.

8. **Suba primeiro o despachante** e espere ele ficar pronto. Ele só fica pronto depois de conferir a
   continuidade do banco. Confira que a porta de saída ficou fechada (`saida_aberta: False`):

   ```sh
   $MC up -d --wait despachante
   $MC run --rm --no-deps --entrypoint python migrar -m mcp_claro.continuidade situacao
   ```

   Na partida, o despachante confere a continuidade do banco. A marca (e a linha do tempo nova) fazem a porta
   de saída fechar e os aplicativos voltarem pausados. O que estava "enviando" fica "não confirmada", o que
   esperava na fila fica "parada por restauração", e o número de protocolo salta de novo 1.000.000 para frente
   (com a marca, o salto total fica em 2.000.000). O extrato registra o que pode ter se perdido ("Pode faltar
   no extrato o que aconteceu entre ... e ...") e cada aplicativo pausado.

9. **Suba o resto** e faça uma **cópia completa nova**, que vira o novo ponto de partida:

   ```sh
   $MC up -d
   $MC exec backup backup.sh completa
   ```

10. **Confira** antes de reabrir (passo "Reabrir a saída").

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
   avisos de papel que já existe são esperados. O serviço `restaurar-logico` fica no perfil `restauracao` do
   Compose: ele não sobe com o resto (`$MC up -d`) e só roda quando chamado com `--profile restauracao`, como
   abaixo.

   ```sh
   $MC --profile restauracao run --rm -e RESTAURAR_QUEM="Nome da pessoa" restaurar-logico \
     /var/lib/mcp-claro-backup/logico/20261118T040000Z
   ```

   Sem a marca, uma cópia lógica posta de volta no **mesmo** servidor não seria percebida: o identificador e
   a linha do tempo do PostgreSQL não mudam. Por isso o script sempre grava a marca no banco do gateway, logo
   depois de restaurar e com o gateway ainda parado. A marca também faz o número de protocolo saltar
   1.000.000 para frente antes de a entrada voltar a dar números.

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

6. Siga os passos 7, 8 e 9 do caminho A (cofre, despachante e o resto), e depois "Reabrir a saída".

## Reabrir a saída

Reabrir é **decisão de uma pessoa ADMIN, registrada**. Antes de reabrir, confira:

- `$MC run --rm --no-deps --entrypoint python migrar -m mcp_claro.continuidade situacao` mostra a última
  conferência, a linha do tempo e se a saída está fechada;
- o último fechamento guardado no **vigia** bate com o último fechamento do banco, ou a diferença está
  explicada pelo intervalo perdido;
- o cofre está destravado e a ronda das portas passou;
- o que estava em andamento no intervalo perdido foi conferido nos sistemas, com as pessoas.

Depois, a pessoa ADMIN reabre na tela **Portas**, na área do ADMIN, com o motivo. Pela linha de comando,
com o identificador dela:

```sh
$MC run --rm --no-deps --entrypoint python despachante -m mcp_claro.continuidade \
  reabrir --quem admin:<id da pessoa ADMIN> --motivo "Conferido com o vigia e com as pessoas"
```

O identificador da pessoa ADMIN aparece nas telas do ADMIN, ou em
`SELECT id, nome FROM pessoas WHERE papel = 'admin'`.

Se a saída for reaberta com o cofre ainda trancado, a conferência do cofre a fecha de novo. Ela reabre sozinha
quando o cofre volta destravado (veja [porta de saída](saida.md), "O cofre está trancado").

### Os aplicativos voltam um por um

Reabrir a saída **não reativa** os aplicativos. Enquanto um aplicativo estiver pausado, a entrada recusa os
pedidos dele. Se uma ação dele ainda estiver na fila, o despachante a para na hora de sair, sem enviar. A tela
da ação diz "Parada por um bloqueio ou uma pausa. Nada seguiu.", com o detalhe "o aplicativo está pausado".

Cada aplicativo volta por decisão da pessoa ADMIN, um de cada vez. Antes, ela confere no extrato o que pode ter
se perdido, como um bloqueio feito depois da cópia. Quando há aplicativo pausado, a tela **Portas** avisa e
leva à lista deles.

- **assistente** (aplicativo usado por pessoas): na área do ADMIN, abra **Aplicativos**. O cartão
  **Aplicativos pausados** mostra o que a conferência do banco pausou. Cada assistente tem o botão
  **Reativar**. Só a pessoa ADMIN chega a essa tela. O passo vai ao extrato com o nome de quem fez
  (`admin_aplicativo_reativado`);
- **programa que roda sozinho**: o mesmo cartão leva à tela do programa (área do ADMIN, **Programas**). Lá, o
  ADMIN usa **Reativar o aplicativo**. O passo também vai ao extrato com o nome de quem fez.

Depois de reativado, os pedidos novos do aplicativo voltam a passar, se nenhum bloqueio ou pausa os segurar (no
programa, também é preciso que o cadastro dele valha). As ações dele que já pararam não voltam: quem precisar
pede de novo. O botão **Reativar** só vale para um assistente que ainda está pausado. Se nada mudar (por
exemplo, num segundo clique), a tela diz "Nada mudou".

Se as telas estiverem fora do ar, a equipe que opera pode fazer a volta de um assistente no banco, a pedido do
ADMIN. Nesse caso, o passo **não vai ao extrato**: registre no relatório da restauração quem pediu, quando e
por quê.

```sh
$MC exec banco sh -c 'PGPASSWORD=$(cat /run/secrets/db_migracao) psql -U mcp_claro_migracao -d mcp_claro'
```

```sql
SELECT id, nome FROM aplicativos WHERE situacao = 'pausado' AND tipo = 'assistente';
UPDATE aplicativos SET situacao = 'ativo', atualizado_em = now()
 WHERE id = '<id do aplicativo>' AND situacao = 'pausado' AND tipo = 'assistente';
```

### Quando só o vigia não respondeu

Em piloto e produção, a conferência exige o vigia (`CONTINUITY_REQUIRE_WATCH`). Se o vigia não responder e não
houver outro sinal de restauração (marca, identificador, linha do tempo ou fechamento mais novo no vigia), a
conferência faz menos:

- a porta de saída fecha, os aplicativos voltam pausados e o número de protocolo salta 1.000.000 para frente,
  como numa restauração;
- a fila **não para**: a conferência não muda o estado de nenhuma ação, e a saída fechada segura tudo;
- o extrato registra o motivo ("o vigia não respondeu") e o intervalo que pode faltar. Ele não diz que o banco
  foi restaurado. Diz que não houve sinal disso, que a fila não parou e que, quando a saída reabrir, o que
  espera é conferido de novo antes de sair. O motivo da saída fechada, na tela **Portas**, diz o mesmo. Sem o
  vigia, não dá para saber se o banco voltou de uma cópia lógica posta sem a marca.

Com a saída reaberta, o despachante confere de novo cada ação antes de ela sair. A de um aplicativo que já
voltou só sai depois dessa conferência. A de um aplicativo ainda pausado fica parada, sem sair. Por isso, para o
que espera seguir, a ordem é esta:

1. confira o vigia: por que não respondeu, e se o último fechamento dele bate com o do banco;
2. reative os aplicativos, um por um (veja acima);
3. só depois, reabra a saída.

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

Na partida, antes de enviar qualquer ação, o despachante lê o identificador (`system_identifier`) e a linha
do tempo (`timeline_id`) do PostgreSQL e compara com a última linha de `continuidade_banco`. Também compara o
último fechamento guardado no vigia com o último do banco. Se algo não bater, se o vigia não responder, ou se
a última linha for a marca "restaurado", a conferência "não confere". Então ele, numa só transação:

1. fecha a porta de saída;
2. pausa os aplicativos ativos (menos as identidades da ronda e do vigia, que não alcançam sistema real);
3. registra no extrato o motivo, o intervalo que pode ter se perdido e cada aplicativo pausado;
4. faz a sequência do número de protocolo saltar 1.000.000 para frente (`SALTO_NA_RESTAURACAO`);
5. com sinal de restauração (marca, identificador, linha do tempo ou fechamento mais novo no vigia), para a
   fila: o que estava "enviando" fica "não confirmada", e o que esperava a ficha ou a hora de sair fica
   "parada por restauração". Só com o vigia sem resposta, a fila fica como está (veja "Quando só o vigia não
   respondeu");
6. grava a conferência em `continuidade_banco`, que é só de acrescentar.

A marca "restaurado" também faz o número saltar 1.000.000, na hora em que é gravada. Por isso, depois de uma
restauração com a marca, os números podem pular 2.000.000. A sequência só anda para frente, e o protocolo
aceita oito dígitos ou mais.

Na primeira partida de um banco novo, ele só grava a linha.
