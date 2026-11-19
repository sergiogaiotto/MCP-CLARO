# Programas que rodam sozinhos

Um programa que roda sozinho é um aplicativo sem pessoa por trás: um robô que lê filas toda manhã, um relatório
que roda à noite. No MCP-CLARO, cada programa é um aplicativo próprio, com um responsável e um substituto com
nome. Plano: decisão 9, "Programas que rodam sozinhos: nossa sugestão", e a tabela de parâmetros
"Programas que rodam sozinhos".

**Na Fase 2, o programa só lê.** Ações de programas ficam desligadas (`PROGRAM_ACTIONS_ENABLED=falso`). Com o
parâmetro ligado, a partida é recusada (veja o fim desta página).

## O que o programa pode fazer na Fase 2

- Ler, só com ferramentas da classe "só lê", dentro da ordem de serviço que o ADMIN deu a ele.
- Só em sistemas com **chave própria do programa** (tipo de chave "Chave do programa"), guardada cifrada no
  cofre. Sistema com chave por pessoa ou com chave compartilhada não aparece na vitrine do programa, e a chamada
  é recusada (`programa_sem_chave_propria`). Do outro lado, nenhuma pessoa usa a chave de um programa: sistema
  com chave de programa não aparece na vitrine de um assistente (`chave_de_programa`).
- Até `PROGRAM_MAX_REQUESTS_PER_MINUTE` pedidos por minuto (30 no início). O pedido seguinte, no mesmo minuto,
  é recusado (`programa_limite_minuto`). A conta é feita no PostgreSQL, num só comando, mesmo com pedidos ao
  mesmo tempo. A regra de um programa nunca afrouxa esse padrão.
- Cada leitura vai ao extrato como "Leitura em X feita pelo programa P, responsável R".

O programa só passa se o cadastro dele vale agora: situação "ativo", data de fim igual ou depois de hoje (no
fuso de Brasília), responsável e substituto ativos. Senão, a resposta diz por quê, nada é enviado e a recusa
vai ao extrato:

| Motivo | Quando |
| --- | --- |
| `programa_sem_cadastro` | O aplicativo é do tipo programa, mas não tem cadastro com responsável |
| `programa_pausado` | O ADMIN pausou, ou uma rotina pausou (veja abaixo) |
| `programa_encerrado` | O programa foi encerrado. Só volta se o ADMIN cadastrar o programa de novo |
| `programa_vencido` | A data de fim já passou |
| `programa_sem_responsavel` | O responsável ou o substituto não está mais ativo |
| `programa_limite_minuto` | Passou do limite de pedidos neste minuto |
| `programa_sem_chave_propria` | O sistema não usa chave própria do programa |
| `programa_so_le` | O programa pediu uma ação. Nada foi enviado |

A entrada confere tudo isso em cada pedido. O despachante confere de novo o cadastro e o tipo de chave antes de
abrir qualquer chave.

## Cadastro no provedor de login (Keycloak)

Cada programa é um cliente próprio no realm `mcp-claro`, pré-cadastrado pelo ADMIN. Não há cadastro automático.
O programa entra com conta de serviço e JWT assinado (`private_key_jwt`, ES256): ele assina um pedido curto com a
chave privada dele, e o Keycloak guarda só a chave pública. Não há segredo compartilhado para vazar.

1. Gere o par de chaves do programa, uma por programa, nunca a mesma (os comandos estão em
   [vigia.md](vigia.md), "Identidades no provedor de login"). A chave privada fica só com o programa.
2. Crie o cliente no realm, igual ao `mcp-claro-ronda` de
   [`docker/login/realm-mcp-claro.json`](../docker/login/realm-mcp-claro.json):
   - `publicClient` falso, `clientAuthenticatorType` = `client-jwt`, `serviceAccountsEnabled` verdadeiro;
   - `standardFlowEnabled`, `directAccessGrantsEnabled` e `implicitFlowEnabled` falsos: só conta de serviço;
   - `fullScopeAllowed` falso;
   - atributos: `token.endpoint.auth.signing.alg` = `ES256`, `token.endpoint.auth.signing.max.exp` = `60`,
     `access.token.lifespan` = `PROGRAM_TOKEN_TTL_SECONDS` (300), `use.jwks.string` = `true`,
     `use.jwks.url` = `false`, `use.refresh.tokens` = `false`;
   - escopos padrão: `basic` e `mcp-claro` (o escopo `mcp-claro` põe o endereço do MCP-CLARO na audiência).
3. Ponha a chave **pública** no atributo `jwks.string` do cliente (pela API de administração ou pela tela do
   Keycloak, aba Keys). No rodízio, a lista leva a chave antiga e a nova ao mesmo tempo, cada uma com o seu `kid`.
4. Anote o identificador da conta de serviço (o `sub` do crachá do programa). No Keycloak: Clients, o cliente,
   aba "Service account roles", link do usuário `service-account-<client_id>`, campo ID. Pela linha de comando:
   `kcadm.sh get clients/<id-interno>/service-account-user --fields id`.

O programa pede o crachá ao token endpoint com `grant_type=client_credentials`,
`client_assertion_type=urn:ietf:params:oauth:client-assertion-type:jwt-bearer`, a afirmação assinada (`iss` e
`sub` iguais ao `client_id`, `aud` no emissor, `jti` novo, validade de até 60 s), o escopo `mcp-claro` e o
`resource` do MCP-CLARO. O kit oficial já traz esse cliente. O crachá vale `PROGRAM_TOKEN_TTL_SECONDS` e fica
preso ao endereço do gateway.

## Cadastro no MCP-CLARO

Tudo nas telas do ADMIN, e cada passo vai ao extrato com o nome de quem fez.

1. **Programas, Cadastrar um programa:** o `client_id` do passo anterior, o nome, para que serve, o responsável,
   o substituto e a data de fim (até um ano). Responsável e substituto são obrigatórios, precisam ser pessoas
   diferentes, ativas e com nome (aparecem depois do primeiro login delas). Sem eles, o programa não é salvo.
2. **Sistemas:** cadastre o sistema com o tipo de chave "Chave do programa", confira a versão e revise as
   ferramentas. Para o programa, só servem as da classe "só lê".
3. **Autorização e chaves do sistema:** entregue ao cofre a chave do programa naquele sistema, escolhendo o
   programa. A tela não guarda a chave.
4. **Modelos de ordem:** monte um modelo só com essas ferramentas.
5. **Programas, o programa, Ordem de serviço do programa:** escolha o modelo, os dias e, na primeira ordem, o
   identificador da conta de serviço (passo 4 do Keycloak). Só aparecem os modelos que leem em sistemas com chave
   de programa.

Na mesma tela do programa, o ADMIN pausa (com motivo), retoma, encerra (com motivo), confirma que o programa
segue valendo, registra a troca da chave, troca o responsável e o substituto e **estende a data de fim**. A nova
data precisa ser depois da data atual e em até um ano a partir de hoje. Só vale para programa que não foi
encerrado, e vai ao extrato com a data antiga e a nova.

Encerrar para o programa de vez. Para voltar a usar um programa encerrado, o ADMIN o cadastra de novo em
**Programas, Cadastrar um programa**, com o **mesmo** `client_id`, responsável, substituto e uma nova data de fim.
A linha do programa volta a valer com os dados novos, a revisão conta a partir de hoje e o passo vai ao extrato.
A data da troca da chave não muda: se a chave passou do prazo, registre a troca antes. Confira também a ordem de
serviço dele. Um programa que ainda vale (ativo ou pausado) não é cadastrado de novo.

## Prazos e rotinas

A rotina `programas` roda a cada `ROUTINES_INTERVAL_SECONDS`. Cada mudança vai ao extrato e vira aviso por
e-mail ao responsável e ao substituto (e ao ADMIN, nas pausas e no encerramento). O aviso do responsável leva o
link de `/telas/programas`; o do ADMIN, o link da tela do programa na área do ADMIN
(`/telas/admin/programas/<id>`), onde fica o botão de retomar:

| O que acontece | Quando | O que fazer |
| --- | --- | --- |
| Revisão periódica | A cada `PROGRAM_OWNER_RECERT_DAYS` (90) dias, contados da última revisão | O responsável confirma em `/telas/programas`, ou o ADMIN na tela do programa. Sem isso, o programa pausa |
| Aviso da chave | `PROGRAM_KEY_EXPIRY_WARN_DAYS` (14) dias antes de a chave fazer `PROGRAM_KEY_MAX_AGE_DAYS` (90) dias | Troque a chave no Keycloak e registre a troca na tela do programa |
| Chave vencida | A chave fez `PROGRAM_KEY_MAX_AGE_DAYS` dias sem troca registrada | O programa pausa. Registre a troca e retome |
| Falta de uso | `PROGRAM_IDLE_PAUSE_DAYS` (30) dias sem pedido, contados do último pedido ou da última revisão | O programa pausa. Se ainda for preciso, o ADMIN retoma |
| Responsável inativo | O responsável ou o substituto foi desativado | O programa pausa. Troque-os e retome |
| Fim | A data de fim passou | O programa é encerrado. Antes disso, o ADMIN pode estender a data de fim na tela do programa |

Retomar é decisão do ADMIN e conta como revisão dele. Só retoma o que está pausado, com a data de fim valendo,
responsável e substituto ativos e chave dentro do prazo.

**A ronda e o vigia são a exceção.** Os programas `PATROL_CLIENT_ID` (`mcp-claro-ronda`) e `WATCH_CLIENT_ID`
(`mcp-claro-vigia`) testam as portas do MCP-CLARO, e a rotina nunca os pausa nem os encerra. Quando um deles
cumpre uma das condições da tabela, a rotina avisa com destaque ("IMPORTANTE") o ADMIN, o responsável e o
substituto, uma vez para cada situação, e registra no extrato. O ADMIN resolve na tela do programa. Enquanto o
cadastro da ronda não vale (pausado, sem cadastro, vencido ou sem responsável ativo), a entrada recusa os pedidos
dela e a ronda fica **inconclusiva**: ela não diz "conforme" sem ter testado a vitrine. Detalhes em
[vigia.md](vigia.md).

## `PROGRAM_ACTIONS_ENABLED`: por que fica desligado

Na Fase 2, o programa só lê. Com `PROGRAM_ACTIONS_ENABLED` ligado, a partida do MCP-CLARO é recusada: o que as
ações de programas precisam ainda não existe. Ligar será decisão do dono, registrada, depois que existir:

- **as pendências do responsável:** a ação de um programa precisa aparecer para o responsável decidir, com aviso.
  Hoje, a ficha ficaria presa à conta de serviço do programa e só venceria, sem ninguém ver;
- **a reconferência completa:** a porta de saída precisa conferir de novo o cadastro do programa (situação, data
  de fim, responsável e substituto) antes de enviar, como já faz na leitura
  (`mcp_claro.regras.programas.reconferir_leitura`);
- **a ordem de serviço com ações:** hoje a ordem de um programa só aceita modelos em que todas as ferramentas
  leem. Nenhuma ferramenta que muda algo chega à vitrine de um programa;
- **a prova do plano:** "em teste, com ações ligadas, um programa não age fora do limite; o que passa vai às
  pendências do responsável e vence".

Até lá, a entrada também recusa a ação de um programa com `programa_so_le`, e nada é enviado.
