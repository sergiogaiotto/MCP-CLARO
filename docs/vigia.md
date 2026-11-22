# Ronda das portas e vigia de fora

Esta página explica o que a ronda e o vigia fazem, como ligar cada um e o que fazer quando eles
acusam algo. Plano: Fase 1, "Vigia", "O que é só de acrescentar" e os parâmetros `PATROL_*` e `WATCH_*`.

## Ronda das portas

A cada `PATROL_INTERVAL_SECONDS` (300 s), o papel `rotinas` tenta entrar no próprio gateway pelo
endereço público (`PUBLIC_BASE_URL`), como um aplicativo de fora. Cada rodada tem sete casos:

| Caso | O que a ronda manda | O que precisa voltar |
| --- | --- | --- |
| `controle` | Crachá de verdade da ronda, lista de ferramentas (`tools/list`) certa na 2026-07-28 | A vitrine da ronda. Qualquer recusa deixa o caso inconclusivo |
| `cracha_falso` | Cópia do crachá de verdade, com o mesmo `kid`, assinada por chave da própria ronda | 401 |
| `cracha_vencido` | Crachá de verdade guardado de rondas anteriores, já vencido | 401 |
| `outro_condominio` | Crachá de verdade do mesmo provedor, com a audiência de outro recurso | 401 |
| `outra_pessoa` | Crachá de verdade da ronda chamando uma ferramenta da vitrine de outra pessoa | Recusa `fora_da_vitrine`, e só ela |
| `versao_antiga_initialize` | `initialize` da geração antiga | 400 com -32022 |
| `versao_antiga_cabecalho` | Pedido com o cabeçalho `2025-11-25` | 400 com -32022 |

Cada caso tem um desfecho:

- **conforme**: a porta recusou como devia;
- **passou**: passagem indevida, a porta deixou entrar;
- **inconclusivo**: sem resposta, erro do servidor ou resposta que não prova nada;
- **pulado**: o caso não pode ser feito agora. Exemplo: nas duas primeiras rodadas depois da partida, ainda não
  há crachá vencido guardado.

No caso `outra_pessoa`, só a recusa `fora_da_vitrine` prova que a vitrine segurou a ferramenta. A recusa que a
entrada dá antes de olhar a vitrine não prova nada e fica **inconclusiva**: aplicativo ou conta da ronda sem
valer, cadastro de programa da ronda sem valer, limite por minuto, entrada fechada, bloqueio ou pausa geral.
Qualquer outra recusa, ou um resultado, é **passagem**. Exemplo: com a vitrine furada, a entrada acha a
ferramenta da Ana e recusa depois, porque a ronda é um programa e o sistema usa chave por pessoa
(`programa_sem_chave_propria`). Isso é passagem.

O `controle` só é **conforme** quando a entrada devolve a vitrine da ronda. Toda recusa da lista de ferramentas
vem antes de olhar a vitrine, e o caso fica **inconclusivo**: aplicativo ou conta da ronda sem valer, cadastro de
programa dela pausado, encerrado, vencido, sem responsável ativo ou ausente, limite por minuto, entrada fechada,
bloqueio ou pausa geral (de tudo, do aplicativo ou da conta da ronda) e qualquer motivo novo. Sem o controle
conforme, a rodada nunca sai "conforme": sai inconclusiva ou, se outro caso passou, "passou". Essa passagem vai
ao extrato e ao log, mas não conta para fechar a entrada (veja a seguir).

Cada tentativa vira uma linha na tabela `ronda`, só de acrescentar. A coluna `defendeu` é falsa só quando
houve passagem; o desfecho fica em `detalhes.situacao`. Nenhum crachá vai à tabela, ao log nem ao extrato.

### Quando algo passa

1. A rodada com passagem registra no extrato: "A ronda das portas achou uma passagem indevida: …".
2. A ronda conta as rodadas seguidas com passagem **e com o controle conforme**, desde a última mudança do
   estado da entrada. Rodada sem controle conforme (o crachá da ronda não passou, o gateway não respondeu ou
   recusou a ronda antes da vitrine) não conta e zera a contagem: alarme falso não fecha a entrada. Por isso,
   com a ronda bloqueada ou pausada, ou com a entrada fechada, uma passagem fica na tabela `ronda`, no extrato
   e no log, mas não fecha a entrada.
3. A cada `PATROL_FAILS_TO_CLOSE` (2) rodadas seguidas, a entrada desce um degrau: aberta, depois só de
   leitura, depois fechada. Cada degrau vai ao extrato ("A ronda das portas achou passagem indevida em 2 rondas
   seguidas e deixou a entrada só de leitura. Só o ADMIN reabre, depois de apurar.") e a um aviso ao ADMIN, por
   e-mail (`NOTIFY_ADMIN_EMAILS`) e pelos webhooks cadastrados.
4. Com `PATROL_AUTOCLOSE_ENABLED=desligado`, a ronda não fecha nada: registra "o fechamento sozinho está
   desligado" e avisa o ADMIN, uma vez por sequência.

**Para reabrir:** a equipe apura o que passou (tabela `ronda`, coluna `detalhes`), corrige, e o ADMIN reabre a
entrada pelas telas. A reabertura muda o estado e zera a contagem da ronda.

## Vigia de fora

O vigia é o mesmo pacote, no papel `vigia`, numa segunda máquina, em outro provedor ou outra rede (decisão 12),
com banco próprio. Ele não tem acesso ao banco do gateway e não depende do Grafana nem das rotinas.

| Parte | Ritmo | O que faz |
| --- | --- | --- |
| Pulso | `WATCH_PING_SECONDS` (60 s) | Pede `/.well-known/oauth-protected-resource/mcp` no endereço público. Sem resposta boa por `WATCH_SILENCE_ALARM_SECONDS` (900 s), dispara o alarme de silêncio, uma vez por silêncio. Quando o gateway volta, avisa que voltou |
| Ronda de fora | `WATCH_PROBE_INTERVAL_SECONDS` (600 s) | Os mesmos sete casos da ronda, com a identidade do vigia. Com `PATROL_FAILS_TO_CLOSE` rodadas seguidas com passagem e controle conforme, dispara alarme. O vigia não fecha a entrada |
| Fechamentos | `WATCH_PROBE_INTERVAL_SECONDS` | Busca os fechamentos na entrada com o crachá do programa `mcp-claro-vigia` e guarda só acrescentando |
| Continuidade | Sob pedido | Diz ao despachante, na partida, qual é o último fechamento guardado |

### Guarda dos fechamentos

A cada volta, o vigia pede `GET /vigia/fechamentos?desde=N` a partir dos 4 últimos que já guardou; na partida e
uma vez por dia, pede todos. Ele confere:

- os que já guardou precisam vir iguais, e nenhum pode sumir;
- no que o gateway mandou, cada fechamento precisa encadear no anterior: `hash_anterior` igual ao `hash` do
  anterior, e o trecho começando onde o anterior terminou;
- o primeiro novo precisa encadear no último guardado. O que não encadeia não é guardado.

Qualquer diferença vira alarme (`fechamento_mudou`, `fechamento_sumiu` ou `encadeamento_quebrado`). O banco do
vigia confere o encadeamento de novo, por gatilho, e recusa mudar ou apagar o que já guardou. Se o vigia não
consegue buscar os fechamentos por `WATCH_SILENCE_ALARM_SECONDS`, também dispara alarme.

O vigia não cobre alteração feita antes do fechamento, nem por quem tiver acesso às duas máquinas.

### Alarmes

Cada alarme é uma linha na tabela `alarmes` do banco do vigia, com chave única: o mesmo problema visto de novo
não repete o aviso. Os avisos saem pela fila `avisos` do banco do vigia, com novas tentativas, por:

- e-mail para cada endereço de `WATCH_ALARM_EMAILS`, pelo SMTP do vigia (`WATCH_SMTP_*`);
- webhook assinado (Standard Webhooks) para cada endereço de `WATCH_WEBHOOK_TARGETS`, com o segredo
  `WATCH_WEBHOOK_SECRET_FILE`.

| Alarme | Quando |
| --- | --- |
| `silencio` | O gateway não responde no endereço público |
| `voltou` | O gateway voltou a responder depois de um silêncio |
| `ronda` | A ronda de fora achou passagem indevida em rodadas seguidas |
| `fechamento_mudou` | Um fechamento já guardado não é mais igual no gateway |
| `fechamento_sumiu` | Um fechamento já guardado sumiu do gateway |
| `encadeamento_quebrado` | Um fechamento não encadeia no anterior |
| `fechamentos_sem_resposta` | O vigia não consegue buscar os fechamentos |

## Rotas

| Rota | Onde | Quem entra | O que devolve |
| --- | --- | --- | --- |
| `GET /vigia/fechamentos?desde=N&limite=M` | Entrada, no endereço público | Só o crachá do programa `WATCH_CLIENT_ID` (401 sem crachá válido, 403 de outro aplicativo, e a recusa vai ao extrato) | Fechamentos de número maior que `desde`: número, `desde_xid`, `ate_xid`, linhas, `hash_anterior` e `hash` em hexadecimal, hora do cálculo; `mais` diz se há outra página. `limite` de 1 a 1000, padrão 500 |
| `GET /ultimo-fechamento` | Vigia, porta 9445 | `Authorization: Bearer <WATCH_CONTINUITY_TOKEN_FILE>`, e mTLS em piloto e produção | Número, hash e horas do último fechamento guardado |
| `GET /saude` | Vigia, porta 9445 | Docker | Vivo, com o último desfecho de cada laço |

## Identidades no provedor de login

A ronda (`mcp-claro-ronda`) e o vigia (`mcp-claro-vigia`) são programas pré-cadastrados no realm
(`docker/login/realm-mcp-claro.json`), com conta de serviço e JWT assinado (`private_key_jwt`, ES256). O
provedor guarda só a chave pública. Cada um pede o crachá ao token endpoint com `client_credentials`, a
afirmação do cliente assinada (`aud` no emissor, no Keycloak), o escopo `mcp-claro` e o `resource` do MCP-CLARO.

Para gerar a chave de cada programa (uma por máquina, nunca a mesma):

```
openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out ronda_chave.pem
python -c "import json,sys; from cryptography.hazmat.primitives import serialization as s; \
from jwt.algorithms import ECAlgorithm as E; k=s.load_pem_private_key(open(sys.argv[1],'rb').read(), None); \
print(json.dumps({'keys':[E.to_jwk(k.public_key(), as_dict=True)|{'kid':sys.argv[2],'use':'sig','alg':'ES256'}]}))" \
ronda_chave.pem mcp-claro-ronda-1
```

A chave privada vai a um segredo do Docker (`PATROL_KEY_FILE`, `WATCH_KEY_FILE`). A saída do segundo comando
vai ao atributo `jwks.string` do cliente no realm, e o `kid` vai a `PATROL_KEY_ID` / `WATCH_KEY_ID`.

**Crachá de outro condomínio.** É um crachá de verdade do mesmo provedor, pedido com o escopo `outro-servico`,
que põe na audiência um segundo recurso de teste. Para isso, nos clientes `mcp-claro-ronda` e
`mcp-claro-vigia`, os escopos `mcp-claro` e `outro-servico` precisam ser **opcionais** (não padrão): com
`mcp-claro` como padrão, o crachá de outro condomínio também citaria o MCP-CLARO, e a ronda pula o caso. Sem o
escopo `outro-servico` no cliente, o provedor recusa o pedido e o caso fica inconclusivo. Nenhum dos dois fecha
a entrada por engano.

**Outra pessoa.** Para o caso exercitar a vitrine, o programa da ronda precisa estar habilitado como aplicativo
do tipo programa, com a pessoa da conta de serviço marcada como de teste, sem ordem de serviço de sistema real.
A ronda escolhe no cadastro uma ferramenta que está na ordem de outra pessoa e não na dela. O vigia, sem acesso
ao cadastro, usa `WATCH_FOREIGN_TOOL` ou um nome inventado.

**Cadastro em Programas (Fase 2).** A entrada só deixa passar um programa com cadastro valendo. Por isso, a ronda
e o vigia precisam de cadastro nas telas do ADMIN, em **Programas** ([programas.md](programas.md)): responsável,
substituto e data de fim. Sem esse cadastro, ou com ele pausado, vencido ou sem responsável ativo, a ronda e a
ronda de fora ficam inconclusivas.

A rotina dos programas nunca pausa nem encerra a ronda e o vigia (`PATROL_CLIENT_ID` e `WATCH_CLIENT_ID`). Quando
a revisão periódica vence, a chave passa do prazo, o responsável sai, a data de fim passa ou um deles para de
fazer pedidos, a rotina avisa com destaque ("IMPORTANTE") o ADMIN, o responsável e o substituto, uma vez por
situação, e registra no extrato. O ADMIN resolve na tela do programa: confirma que ele segue valendo, registra a
troca da chave, troca o responsável ou estende a data de fim (até um ano a partir de hoje).

## Segunda máquina

O arquivo `docker/vigia/compose.vigia.yml` sobe, na segunda máquina, o banco do vigia (`postgres:18.7`, com
TLS), as migrações do banco do vigia (`mcp-claro migrar-vigia`, de `migrations_vigia/`) e o vigia (porta 9445).
Os segredos ficam em `.segredos/vigia/`, só nessa máquina:

| Segredo | Para que serve |
| --- | --- |
| `db_vigia_migracao`, `db_vigia` | Senhas do dono do banco do vigia e do usuário do papel (`mcp_claro_vigia`, só lê e acrescenta) |
| `banco_vigia_cert.pem`, `banco_vigia_key.pem`, `ca_vigia.pem` | TLS do banco do vigia |
| `vigia_chave.pem` | Chave privada do programa `mcp-claro-vigia` |
| `vigia_smtp`, `vigia_webhook_assinatura` | Senha do SMTP e segredo dos webhooks do vigia, separados dos do gateway |
| `vigia_continuidade` | Segredo da conferência de continuidade, o mesmo no despachante. Pelo menos 32 caracteres |
| `vigia_cert.pem`, `vigia_key.pem`, `ca_despachante.pem` | TLS da porta 9445 e a autoridade dos certificados de cliente aceitos. Ponha nela também a autoridade do próprio vigia, para a conferência de saúde do Docker |

Abra a porta 9445 no firewall só para o endereço da máquina principal.

**Parâmetros do gateway na segunda máquina.** A partida do vigia e a do `mcp-claro migrar-vigia` conferem os mesmos
parâmetros que as outras peças do gateway, sem saber o papel. Isso inclui a sala de visitas da Fase 3
(`VISITS_INTERNAL_URL`). Toda peça confere este valor na partida, menos a própria sala. Em piloto e produção, ponha
o mesmo `VISITS_INTERNAL_URL` em todos os papéis, também no vigia da segunda máquina e nos comandos `migrar`,
`migrar-vigia` e `conferir`. O vigia não fala com a sala, mas sem o valor ele não sobe. O
`docker/vigia/compose.vigia.yml` do repositório está em `APP_ENV: teste` e não precisa dele.

## Parâmetros

Parâmetros que já existiam: `PATROL_INTERVAL_SECONDS`, `PATROL_FAILS_TO_CLOSE`, `PATROL_AUTOCLOSE_ENABLED`,
`WATCH_PUBLIC_URL`, `WATCH_PROBE_INTERVAL_SECONDS` e `WATCH_SILENCE_ALARM_SECONDS`. Os novos, abaixo, valem
com o valor inicial enquanto não entram em `config/settings.py`.

| Parâmetro | Tipo | Valor inicial | Quem lê | Regra de recusa |
| --- | --- | --- | --- | --- |
| `PATROL_CLIENT_ID` | texto | `mcp-claro-ronda` | rotinas e despachante (a conferência do banco na partida não pausa a ronda) | Vazio |
| `PATROL_KEY_FILE` | caminho | nenhum | rotinas | Obrigatório em piloto e produção |
| `PATROL_KEY_ID` | texto | nenhum | rotinas | |
| `PATROL_TOKEN_URL` | URL | derivado do emissor (Keycloak) ou descoberta OIDC | rotinas | Fora de `https://` em piloto e produção |
| `PATROL_TOKEN_SCOPE` | texto | `mcp-claro` | rotinas | |
| `PATROL_OTHER_AUDIENCE_SCOPE` | texto | `outro-servico` | rotinas | Vazio desliga o caso |
| `PATROL_CA_FILE` | caminho | nenhum | rotinas | Só em desenvolvimento |
| `WATCH_CLIENT_ID` | texto | `mcp-claro-vigia` | vigia, entrada, rotinas e despachante (a rotina dos programas e a conferência do banco não pausam o vigia) | Vazio |
| `WATCH_KEY_FILE` | caminho | nenhum | vigia | Obrigatório em piloto e produção |
| `WATCH_KEY_ID` | texto | nenhum | vigia | |
| `WATCH_TOKEN_URL` | URL | como `PATROL_TOKEN_URL` | vigia | Fora de `https://` em piloto e produção |
| `WATCH_TOKEN_SCOPE` | texto | `mcp-claro` | vigia | |
| `WATCH_OTHER_AUDIENCE_SCOPE` | texto | `outro-servico` | vigia | |
| `WATCH_FOREIGN_TOOL` | texto | `mcp_claro_vigia_ferramenta_alheia` | vigia | |
| `WATCH_CA_FILE` | caminho | nenhum | vigia | Só em desenvolvimento |
| `WATCH_PING_SECONDS` | inteiro | 60 (10 a 3600) | vigia | Precisa caber duas vezes em `WATCH_SILENCE_ALARM_SECONDS` |
| `WATCH_ALARM_EMAILS` | lista | nenhum | vigia | E-mail inválido; lista sem `WATCH_SMTP_HOST` e `WATCH_SMTP_FROM`; em piloto e produção, sem e-mail nem webhook |
| `WATCH_SMTP_HOST`, `WATCH_SMTP_PORT`, `WATCH_SMTP_FROM` | texto, inteiro, texto | nenhum, 587, nenhum | vigia | |
| `WATCH_SMTP_USERNAME`, `WATCH_SMTP_PASSWORD_FILE` | texto, caminho | nenhum | vigia | |
| `WATCH_SMTP_STARTTLS` | liga/desliga | `ligado` | vigia | |
| `WATCH_WEBHOOK_TARGETS` | lista de URLs | nenhum | vigia | Endereço fora de `https://`; lista sem `WATCH_WEBHOOK_SECRET_FILE` |
| `WATCH_WEBHOOK_SECRET_FILE` | caminho | nenhum | vigia | Base64 de 24 a 64 bytes, como `WEBHOOK_SECRET_FILE` |
| `WATCH_INTERNAL_PORT` | inteiro | 9445 | vigia | |
| `WATCH_CONTINUITY_TOKEN_FILE` | caminho | nenhum | vigia e despachante | Menos de 32 caracteres |
| `WATCH_CONTINUITY_URL` | URL | nenhum | despachante | Fora de `https://` em piloto e produção |
| `WATCH_CONTINUITY_CA_FILE` | caminho | nenhum | despachante | |
| `VISITS_INTERNAL_URL` | URL | nenhum | despachante; a partida de todo papel, menos a sala, confere | Vazio ou fora de `https://` em piloto e produção; em qualquer ambiente, um valor que não seja só o esquema (`http://` ou `https://`), o nome e a porta da sala |
