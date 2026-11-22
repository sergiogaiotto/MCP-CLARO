# Decisões do MCP-CLARO

Registro das decisões que guiam o código. Cada uma diz o que foi decidido, quem decidiu e o efeito no produto.
Data de todas: 18/11/2026.

## Nome e repositório

- **Nome da aplicação:** MCP-CLARO, o gateway MCP da Claro. Pacote Python `mcp_claro`, imagem `mcp-claro`.
- **Repositório:** `sergiogaiotto/mcp-claro`.

## Versão do MCP (decisão 8, ratificada)

Só a versão mais atual publicada pela fundação do MCP, ligada à Linux Foundation: **2026-07-28**. Vale dos dois lados:

- aplicativos de IA que se ligam ao MCP-CLARO;
- sistemas e servidores MCP que o MCP-CLARO chama.

Quem só fala a geração antiga recebe uma recusa legível e entra numa contagem. Não há porta de exceção. O valor fica em
`src/mcp_claro/versao.py`; a partida recusa `MCP_ACCEPTED_VERSIONS` e `MCP_UPSTREAM_VERSION` diferentes dele. A
próxima versão do padrão está prevista para 30/03/2027; uma revisão de compatibilidade, antes dessa data, decide a
migração, com mudança de código revisada.

## Decisões 1 a 6: seguir as recomendações

O dono do produto pediu para seguir as recomendações do plano. Elas deixam de ser premissas e passam a valer:

| Decisão | O que vale |
| --- | --- |
| 1. Para quem é o produto | Uso interno, uma organização por instalação |
| 2. Ligação com os sistemas | Kit oficial `mcp` 2.3.0 no núcleo, nas duas pontas. FastMCP só em sistemas de exemplo e testes. Gateways prontos só como referência |
| 3. Provedor de login | Keycloak 26.8.2 como padrão (`IDP_KIND=keycloak`), por ter conta de serviço com JWT assinado e funcionar inteiro sem licença paga. O verificador de crachá é genérico (OIDC com JWKS), e o Logto 1.46.0 segue como alternativa testada pela mesma bateria |
| 4. Aplicativos-piloto | Claude Code como primeiro piloto. Segundo piloto: Goose, que tem registro público de falar a 2026-07-28. MCP Inspector em modo `modern` para testes |
| 5. Primeiro sistema | Sistemas de exemplo próprios até a escolha de um sistema real, só leitura e com chave por pessoa |
| 6. Peças | Caddy no portão e OpenBao no cofre. `VAULT_SEAL_MODE=static` em teste; no piloto, `shamir` 2 de 3, com guardiões nomeados |

## Decisões 7 a 14

| Decisão | O que vale |
| --- | --- |
| 7. Apigee | Fica para depois. Tudo é implementado e testado com o portão próprio. `EDGE_MODE=apigee` existe e a partida o recusa |
| 9. Programas que rodam sozinhos | Parametrização em duas camadas: padrões da instalação e regras de cada programa, nunca mais frouxas. O responsável humano é cadastro obrigatório, não parâmetro. Programas entram na Fase 2, só lendo |
| 10. Ordem das fases 3 e 4 | Servidores de terceiros antes dos selos |
| 11. Valores iniciais | Cada valor vira parâmetro, começa conservador e é ajustado no piloto |
| 12. Avisos e segunda máquina | E-mail e webhook assinado, além da caixa de pendências. Vigia em outro provedor ou outra rede |
| 13. Extensão Tasks | Desligada por `FEATURE_TASKS`. A consulta pelo número de protocolo é o caminho principal |
| 14. Grafana | Usado sem alteração. Parecer sobre a AGPL só se o produto for oferecido a clientes ou distribuído |

## Provas da Fase 2: resposta perdida (19/11/2026)

- **Toxiproxy trocado por um intermediário de teste.** O plano fixava o Toxiproxy 2.13.0
  (`ghcr.io/shopify/toxiproxy:2.13.0`) para cortar respostas. A imagem não baixa no ambiente de testes (o
  `ghcr.io` fica bloqueado). No lugar dele entra `tests/cortador.py`, um intermediário TCP só de teste, com os
  mesmos cortes: a resposta não volta, volta pela metade, ou sai inteira de quem respondeu e é engolida no
  caminho, com a conexão fechada ou muda até quem pediu desistir. Ele corta nos dois caminhos: entre o despachante e o sistema
  (`tests/integracao/test_saida_resposta_perdida.py`) e entre o aplicativo e a entrada
  (`tests/integracao/test_saida_resposta_perdida_aplicativo.py`). O teste com o Toxiproxy ficou nesse segundo
  arquivo e roda quando a imagem existe na máquina. Sem a imagem, ele é pulado e diz o motivo.
- **Teste público da discussão sobre repetição** (issue 3394 do repositório `modelcontextprotocol`). Lemos a
  discussão. Nela, a resposta do primeiro `tools/call` se perde depois de o efeito acontecer. O aplicativo
  desiste pelo prazo e repete uma vez, com os mesmos argumentos e um id novo, e o efeito acontece duas vezes
  (20 de 20 rodadas). Os scripts da discussão ficam num repositório à parte e não foram copiados. O mesmo
  cenário foi refeito no teste do caminho do aplicativo, pelo gateway inteiro, com o sistema de exemplo
  contando cada chamada. Na 2026-07-28 por HTTP, o aplicativo cancela fechando o próprio pedido, sem mandar
  notificação. O teste confere que o efeito acontece uma vez e que a repetição devolve o mesmo protocolo. Roda
  3 vezes a cada mudança e 20 vezes na rodada diária (`MCP_CLARO_TESTE_LONGO=1`).

## Mudanças deliberadas da Fase 2 (19/11/2026)

Durante a Fase 2, a equipe mudou de propósito alguns pontos do plano, depois das revisões. Cada mudança diz o
que o plano previa, o que vale agora, por quê e onde está a prova. Ficam registradas para o dono do produto
conferir. O texto do plano foi ajustado onde ele descrevia o comportamento antigo.

- **Travas consultivas por alcance, e não só nas linhas.** O plano previa travar, com `FOR SHARE`, as linhas da
  pessoa e do aplicativo no envio, e com `FOR UPDATE` no bloqueio. Agora o envio, o pedido e a confirmação da
  ficha tomam, compartilhadas e sempre na mesma ordem, travas consultivas do PostgreSQL por alcance (tudo,
  sistema, aplicativo e pessoa). Quem bloqueia toma, exclusiva, a do seu alcance; fechar a porta de saída e a
  restauração tomam a de "tudo". Só depois vêm as linhas da ficha e da fila. Por quê: travar uma linha exige a
  permissão de alterar a tabela, e o despachante não altera o cadastro. Efeito: ou o bloqueio entra antes e o
  envio já o vê, ou o envio marca "enviando" antes e o comprovante mostra a ação em conferência (ou como já
  seguida, se o envio terminou antes). Prova:
  `src/mcp_claro/saida/travas.py`, `tests/integracao/test_saida_disputas.py` e, sem impasse com o vencimento
  de fichas, `tests/integracao/test_saida_correcoes.py`.
- **401 no envio vira "não saiu".** O plano previa "não confirmada" para um 401. Agora 401, 403,
  redirecionamento e os erros do protocolo que o sistema devolve antes de executar (método ou argumentos
  inválidos) viram "não saiu". No 401, a chave da pessoa fica dada como vencida: o próximo pedido a renova antes
  de enviar, e a pessoa só autoriza de novo se a renovação for recusada. Nada é reenviado. Por quê: o sistema
  recusou na porta, antes de executar; "não confirmada" pediria à pessoa uma conferência sem motivo e faria o
  pedido igual devolver uma ação que não aconteceu. Prova:
  `tests/unidade/test_despachante_classifica.py`.
- **`PROGRAM_ACTIONS_ENABLED` recusado na partida.** O plano dizia que o parâmetro liga depois das provas da
  Fase 2. Agora a partida recusa o valor ligado, em todos os papéis, enquanto faltarem as pendências do
  responsável e a reconferência completa do cadastro do programa na porta de saída. Mudar só o parâmetro não
  liga nada; ligar pede mudança de código revisada e a decisão do dono (decisão 9). Prova:
  `Parametros.problemas` em `src/mcp_claro/config/settings.py` e `tests/unidade/test_settings_fase2.py`.
  Detalhes em [programas.md](programas.md).
- **Intermediário de teste no lugar do Toxiproxy.** Veja "Provas da Fase 2: resposta perdida", acima.
- **O ADMIN marca os campos de destinatário.** O plano previa o limite com "destinatários", sem dizer quais
  campos do pedido os trazem. A primeira versão adivinhava pelo tipo do campo, o que podia deixar livre uma
  cópia para fora. Agora o ADMIN marca, na revisão da ferramenta, quais campos são de destinatário
  (`ferramentas_revisadas.campos_destinatario`, migração 0005). Num limite "só estes destinatários", o campo
  escolhido aceita só os valores escritos, os outros campos marcados têm de vir vazios, o resto do pedido fica
  livre e um campo fora do esquema revisado manda o pedido para a ficha. Quando o ADMIN marca um campo novo, os
  limites "só estes" que não o conferem são encerrados. Prova: `tests/integracao/test_telas_rodada2.py` e
  `tests/integracao/test_saida_rodada2.py`.
- **Vigia sem resposta não para a fila.** O plano dizia que, se o vigia não responder, o que estava na fila vira
  "resultado não confirmado". Agora isso vale só com sinal de restauração (marca, identificador, linha do tempo
  ou fechamento mais novo no vigia). Só com o vigia sem resposta, a porta de saída fecha, os aplicativos voltam
  pausados, o extrato registra o intervalo que pode faltar e o número de protocolo salta, mas a fila fica como
  está. Com a saída reaberta, a ação de um aplicativo ainda pausado fica parada, sem sair; para o que espera
  seguir, os aplicativos voltam antes de reabrir. Por quê: o silêncio da segunda máquina não é sinal de que o
  banco voltou no tempo, e parar a fila cada vez que o vigia não responde na partida jogaria fora ações que as
  pessoas já confirmaram. Como falta a testemunha de fora, a saída fecha e o número salta assim mesmo. Prova:
  `tests/integracao/test_saida_rodada2.py` e `tests/integracao/test_saida_rodada2_conferencia.py`. Roteiro em
  [restauracao.md](restauracao.md).
- **Pedido igual que ainda pode sair conta em qualquer idade.** O plano define `DUPLICATE_WINDOW_MINUTES` como
  o intervalo em que um pedido igual conta como repetição. Agora a ação que ainda pode sair (esperando a ficha,
  liberada ou enviando) conta sempre, e a janela vale para a que já saiu ou ficou não confirmada, contada desde
  a última mudança dela. Por quê: uma ficha pode esperar mais que a janela (a de um programa, por exemplo), e o
  pedido repetido viraria uma segunda ação. Prova: `tests/integracao/test_saida_correcoes.py`.
- **Quem reativa os aplicativos depois de uma restauração é o ADMIN.** A proposta e o plano dizem que, depois
  de restaurar o banco, cada pessoa reativa os seus aplicativos. Agora a pausa é do aplicativo inteiro
  (`aplicativos.situacao`), não de cada pessoa, e quem a tira é a pessoa ADMIN: um assistente na tela
  **Aplicativos** da área do ADMIN, botão **Reativar**, e um programa na tela do programa. O passo vai ao
  extrato com o nome de quem fez. Por quê: um aplicativo serve a várias pessoas, e a restauração atinge todas
  ao mesmo tempo; quem decide quando ele volta é quem cuida da instalação e reabre a porta de saída. O motivo
  da proposta continua valendo: um bloqueio feito depois da cópia pode ter se perdido. Por isso o extrato
  registra o intervalo que pode faltar, e quem tinha bloqueado algo nesse intervalo precisa conferir e
  bloquear de novo. Prova: `tests/integracao/test_telas_rodada3.py` e
  `tests/integracao/test_programas_rodada3.py`. Roteiro em [restauracao.md](restauracao.md).

## Decisões da Fase 3: servidores de terceiros (21/11/2026)

Na Fase 3, o MCP-CLARO passa a receber servidores MCP que não são da organização. No cadastro, eles têm a origem
"de terceiros" (`sistemas.origem = 'terceiro'`). Neste texto, "terceiro" quer dizer um servidor assim, e "próprio",
um sistema da organização. "A chave da Fase 3" é o parâmetro `FEATURE_PHASE3_THIRD_PARTY`. "Pessoa de teste" é a
pessoa marcada como de teste no cadastro (`pessoas.teste`).

O dono do produto pediu para seguir as recomendações do desenho técnico da Fase 3 (D1 a D33), com as correções da
revisão crítica que veio depois dele. A tabela diz como cada uma ficou no código.

| Decisão | O que vale |
| --- | --- |
| D1. Sala de visitas | A conversa com os terceiros roda num contêiner próprio, `visitas`, sem banco, sem cofre e sem extrato, numa rede só dele. Cada chamada roda num processo filho novo, só com os dados daquela chamada: um terceiro que tome o filho vê só a própria chamada. Fora de `desenvolvimento` e `teste`, a sala é obrigatória, mesmo com a chave da Fase 3 desligada; sem ela, nenhum pedido MCP vai a terceiro. O certificado da sala vem de uma autoridade só dela (`ca_visitas`, só para servidor), que o cofre não aceita. O despachante fala com os sistemas próprios, com a sala e com o endereço de troca de chave (OAuth) de cada terceiro; só o despachante abre as chaves |
| D2. Tipo de chave de terceiro | Chave por pessoa ou chave compartilhada (que só lê). Chave de programa fica para depois: o banco recusa, e a migração 0006 para, com uma frase, se achar um terceiro antigo com chave de programa |
| D3. Texto do fornecedor nos próprios | Não muda agora: o esquema de um sistema próprio vai à vitrine como o ADMIN revisou, como nas Fases 1 e 2. Fica para uma decisão própria |
| D4. Revisão do servidor inteiro nos próprios | Não. Só terceiros têm revisão do servidor |
| D5. O que conta como mudança num terceiro | Tudo o que entra na impressão de cada ferramenta (nome, título, descrição, esquemas de entrada e de saída e dicas): ferramenta mudada, mesmo só no texto, nova ou sumida. Ícones, `_meta`, `execution` e as `instructions` do servidor não contam: não chegam ao aplicativo |
| D6. Chave da Fase 3 na partida | A partida recusa a chave ligada, como recusa `PROGRAM_ACTIONS_ENABLED`, até o dono registrar a decisão, depois das provas que dependem de gente e de ambiente real. As provas de ponta a ponta não tiram essa recusa. O ADMIN prepara os terceiros com a chave desligada, e só pessoas de teste os usam |
| D7. Quem fornece | Obrigatório na conclusão da revisão do servidor, de 3 a 200 letras |
| D8. Origem fixa | A origem de um sistema não muda, nem pelo dono das tabelas do banco. Para mudar, cadastre o servidor de novo |
| D9. Endereço e autorização de um terceiro | Correção M1: num terceiro, o endereço e a autorização (OAuth) só mudam de vazio para um valor. Depois disso, o banco e o despachante recusam: "Para mudar, cadastre o servidor de novo." O nome de um sistema não se repete, então o cadastro novo leva outro nome. Mudar o tipo de chave de qualquer sistema apaga a versão, a sondagem e a revisão do servidor e volta o sistema a "pendente", como nas Fases 1 e 2; um sistema em quarentena continua em quarentena (ou vai a "desativado", se for esse o pedido) |
| D10. Dados de rastreio a terceiros | `traceparent`, `tracestate` e `baggage` nunca vão a um terceiro, nem nos cabeçalhos nem no `_meta` do pedido. Nenhum valor de `OBS_TRACE_PROPAGATE_DOWNSTREAM` muda isso |
| D11. Nome e versão do produto | Genéricos para terceiros na conversa MCP: o MCP-CLARO se apresenta como `mcp-claro`, versão "1", e o User-Agent (o nome que vai em cada pedido HTTP) é só `mcp-claro`. A troca e a renovação da chave (OAuth), que saem do despachante para o endereço de troca de chave do terceiro, ainda mandam a versão do produto no User-Agent (`mcp-claro/` e a versão), como para os sistemas próprios |
| D12. Pedido pelo fio antigo seguido de resultado, numa leitura | O resultado é descartado e a leitura devolve o erro legível do MCP-CLARO |
| D13. Erro -32021 de um terceiro | Sempre o erro legível do MCP-CLARO, nunca o -32021 ao aplicativo |
| D14. Quarentena por pedidos barrados | Não. Os pedidos barrados vão ao extrato e à tela do ADMIN |
| D15. Erro -32042 numa ação | Continua "não confirmada" |
| D16. Avisos às pessoas | Nenhum aviso novo por e-mail às pessoas (pedidos barrados, quarentena): só o extrato |
| D17. "Traz texto de fora" em ferramenta própria | Sim, a mesma marca dos terceiros. Com a correção B7, o sinal "leu texto de fora" de uma ferramenta própria marcada liga o alerta dos três sinais sempre; a chave da Fase 3 só controla o que vem de terceiros |
| D18. Dados estruturados de terceiro | O `structuredContent` vira texto dentro da moldura de estranhos, e a vitrine não anuncia esquema de saída. Com a correção M3, vale também para ferramenta própria marcada "traz texto de fora" |
| D19. Acento nos textos ao aplicativo e no extrato | Continuam sem acento por agora; fica para uma decisão à parte |
| D20. Soma dos sinais | Pelo par pessoa e aplicativo |
| D21. "Quer enviar algo" | Toda ação (classe "muda" ou "tem volta"), nunca a dica `openWorldHint` |
| D22. "Viu dado privado" | A ferramenta tem campos pessoais marcados pelo ADMIN |
| D23. Quando o alerta liga | No que vem de terceiros, com a chave da Fase 3 ligada ou para pessoa de teste; no que vem de ferramenta própria marcada, sempre (B7). Os sinais são gravados sempre |
| D24. Contar o alerta ao aplicativo | Não. O aplicativo recebe a resposta de qualquer ficha |
| D25. Fichas do alerta no teto por hora | Contam. O risco disso está em "Riscos aceitos da Fase 3", abaixo |
| D26. Fila e limites na quarentena | Correção M2: a quarentena cancela o que esperava para sair e suspende todos os limites ativos do servidor, com motivo e linha no extrato de cada pessoa. A pessoa faz o limite de novo depois da nova revisão |
| D27. Desativar um servidor em quarentena | Pode. Ele só volta por nova sondagem e nova revisão do servidor |
| D28. Terceiro que não responde | Correção A5: a conclusão da revisão do servidor exige a chave de leitura das declarações guardada e lê o servidor com ela; sem ela, recusa com "Guarde a chave de leitura das declarações deste servidor antes de concluir a revisão". Um terceiro que não responde à conferência do mudou-parou em 2 rodadas seguidas vai à quarentena ("não respondeu à conferência") |
| D29. Terceiro ativo que falha numa nova sondagem | Se a sondagem diz que ele não fala mais a 2026-07-28, ele vai à quarentena |
| D30. Alerta alto para recusa de terceiro no cadastro | Não: fica no painel e no resumo semanal. Um 404 sem resposta MCP não conta como versão antiga. Com a correção B12, não há visão própria para isso: o painel e o alerta filtram o motivo `cadastro_terceiro` em `medicoes.v_recusas_versao_sistema` |
| D31. Prova de saída "texto de estranhos" | Entra no plano: a resposta de um terceiro chega inteira dentro da marca, e nada passa fora dela. Fica com as provas de ponta a ponta ([andamento](andamento.md#fase-3-servidores-de-terceiros)) |
| D32. Definição declarada | Guardada em cada revisão de ferramenta (também nos próprios), para a tela mostrar o antes e o depois |
| D33. A pessoa apaga o alerta | Não. O alerta acaba sozinho, quando a janela passa |

## Mudanças deliberadas da Fase 3 (21/11/2026)

Durante a Fase 3, a equipe mudou de propósito alguns pontos do plano e do desenho técnico, depois das revisões.
Cada item diz o que vale agora, por quê e onde está a prova. Ficam registrados para o dono do produto conferir.
Onde um item vale também para os sistemas próprios das Fases 1 e 2, ele diz isso.

Sobre os números medidos: "tempo de CPU" é o tempo que o processador gastou na conta; "tempo de relógio" é o tempo
que passou, com a espera junto. As medidas foram feitas na máquina de testes do projeto (um servidor de nuvem com 4
processadores x86_64 de 2,1 GHz), em novembro de 2026, muitas vezes com outros trabalhos rodando ao mesmo tempo, e
por isso algumas faixas são largas. Outra máquina dá outros números. Nenhum deles é garantia.

### A chave da Fase 3 e a sala de visitas

- **A chave da Fase 3 continua recusada na partida (D6).** O desenho previa tirar a recusa depois das provas de
  ponta a ponta. Agora a partida recusa `FEATURE_PHASE3_THIRD_PARTY` ligada, em todos os papéis, até o dono
  registrar a decisão, depois das provas que dependem de gente e de ambiente real (lista no
  [andamento](andamento.md#fase-3-servidores-de-terceiros)). Mudar só o parâmetro não liga nada. Enquanto isso, o
  ADMIN cadastra, sonda e revisa os terceiros, e só pessoas de teste os veem e usam. Por quê: pôr e tirar a recusa
  na mesma entrega não protegeria nada, e as provas que faltam dependem de gente. Prova: `Parametros.problemas` em
  `src/mcp_claro/config/settings.py` e `tests/unidade/test_settings_fase3.py`.
- **A sala de visitas é obrigatória fora de desenvolvimento e teste (A2).** O desenho exigia o endereço da sala
  (`VISITS_INTERNAL_URL`) só com a chave da Fase 3 ligada. Agora, em piloto e produção, a partida exige esse
  endereço, com https, sempre. Sem sala, nenhum pedido MCP vai a um terceiro: a chamada fica como "nada foi
  enviado", o extrato ganha a linha `sala_fora` e um contador soma. Fora de desenvolvimento e teste, o despachante
  nunca chama um terceiro direto: a sondagem, a lista de ferramentas, a leitura e a ação vão pela sala, e a chamada
  direta só existe nos testes, posta de propósito. A exceção é a autorização (OAuth): a troca do código e a
  renovação da chave saem do despachante direto para o endereço de troca de chave do terceiro, conferido antes
  (https e IP público), como diz o D1. Por quê: com a chave desligada, o ADMIN já prepara os terceiros e as pessoas
  de teste já os chamam; um endereço esquecido faria esse tráfego sair do processo que abre todas as chaves. Prova:
  `tests/unidade/test_settings_fase3.py`, `tests/unidade/test_sala_salto.py` e, para a troca de chave,
  `tests/integracao/test_g3a_autorizacao_terceiros.py`.
- **Ao atualizar um piloto para a Fase 3, `VISITS_INTERNAL_URL` entra no ambiente de todos os papéis.** A
  partida de toda peça, menos a sala, confere os parâmetros do gateway inteiro (`Parametros.problemas`, que não sabe
  o papel), como nas Fases 1 e 2 com `SCREENS_CLIENT_SECRET_FILE`, `GRAFANA_WEBHOOK_PASSWORD_FILE` e
  `WATCH_CONTINUITY_URL`. Por isso, em piloto e produção, ponha o mesmo `VISITS_INTERNAL_URL` em todos os papéis,
  também no vigia da segunda máquina e nos comandos `migrar`, `migrar-vigia` e `conferir`. Sem ele, a partida sai
  com a frase que começa por "Fora de desenvolvimento e teste, VISITS_INTERNAL_URL e obrigatorio e usa https". Fazer
  a partida saber o papel, para exigir o valor só de quem fala com a sala, fica para uma decisão à parte. Prova:
  `src/mcp_claro/config/settings.py` (`_problemas_da_fase3`), `src/mcp_claro/__main__.py` e
  `tests/unidade/test_r6d_documentos.py`.
- **A sala não sobe sem `APP_ENV`.** A partida da sala de visitas recusa subir sem `APP_ENV` escrito no ambiente
  dela, com a frase "A sala de visitas precisa de APP_ENV escrito no ambiente dela (desenvolvimento, teste, piloto ou
  producao): sem ele, ela subiria como desenvolvimento e aceitaria enderecos locais." Por quê: o padrão do parâmetro
  é desenvolvimento, e em desenvolvimento a sala aceita endereços locais; uma sala sem o parâmetro chamaria um
  terceiro sem conferir o https nem o IP público (o muro da rede da sala ainda tira as rotas para endereços que não
  são públicos). Prova: `src/mcp_claro/visitas/partida.py` e `tests/unidade/test_r6e_ambiente_sala.py`.
- **Parâmetros novos.** `VISITS_INTERNAL_PORT`: 9446 no início, de 1024 a 65535, diferente das outras portas
  internas. `VISITS_INTERNAL_URL`: vazio em desenvolvimento e teste; obrigatório, com https, em piloto e produção.
  `VISITS_CA_FILE`, `VISITS_CLIENT_CERT_FILE` e `VISITS_CLIENT_KEY_FILE`: os arquivos do mTLS (conexão com
  certificado dos dois lados) do despachante com a sala. `VAULT_TRANSIT_KEY_THIRD_PARTY`: a chave do cofre só dos
  terceiros, `mcp-claro-chaves-terceiros` no início, diferente da chave dos próprios. No Compose, `MCP_CLARO_IMAGEM`
  escolhe a imagem do gateway. `THREE_SIGNALS_WINDOW_MINUTES` continua na política (`config/politica.py`): 240
  minutos no início, de 5 a 10.080. Prova: `tests/unidade/test_settings_fase3.py`.
- **Um processo novo para cada chamada (A1).** A sala atende cada chamada num processo filho novo, iniciado do zero
  (`spawn`), só com os dados daquela chamada. O processo pai só entrega o pedido e devolve o que o filho escreveu,
  sem ler. Para a chamada não esperar a partida do filho, a sala deixa 2 filhos prontos, sem dado nenhum, e atende
  até 32 chamadas ao mesmo tempo. Antes de receber o pedido, cada filho se fecha e avisa que se isolou; o pai só
  entrega o pedido a quem já avisou. O filho fala com o pai por soquetes, que outro processo não consegue reabrir
  pelo `/proc`; entra num domínio Landlock só dele (uma proteção do núcleo do Linux); troca a entrada e as saídas
  por `/dev/null`; e fecha o cano do rastreador de recursos do Python. Assim, um filho tomado não escreve no log da
  sala nem fala com outro processo. Os filhos nascem numa linha de execução (thread) própria da sala, que vive tanto
  quanto ela, porque o aviso de morte do pai (`PR_SET_PDEATHSIG`) vale pela thread que criou o filho; um teste prova
  que fechar a sala mata um filho já isolado. Por quê: uma sala que atendesse todos os terceiros num processo só
  deixaria um terceiro que explorasse uma falha do kit ler as chaves e as respostas dos outros. Prova:
  `src/mcp_claro/visitas/app.py`, `src/mcp_claro/visitas/filho.py` e `tests/unidade/test_visitas_app.py`.
- **A sala parte por `python -m mcp_claro visitas`.** No Compose, o serviço `visitas` não usa o programa `mcp-claro`
  da imagem. Quando o processo pai é um programa assim, o `spawn` roda o programa de novo em cada filho, antes do
  isolamento, e com ele o que ele importa (parâmetros, log); o programa de um pacote, o `spawn` pula. Medido na
  imagem do gateway: cada filho carrega 60 módulos e cerca de 5 MB de memória a menos, e a partida de um filho fica
  parecida, cerca de 0,75 s de tempo de relógio (mediana de 20 partidas). Prova: `docker/compose.yml` e
  `tests/integracao/test_sala_isolada.py`.
- **Prova de isolamento a cada mudança, com regra escrita para quando pula.** `tests/integracao/test_sala_isolada.py`
  sobe a sala, o despachante, o banco e o cofre e prova, de dentro da sala, que ela não alcança o banco nem o cofre,
  pelo nome e pelo IP, nem os metadados das nuvens (169.254.169.254) e os IPs privados da rede `saida_visitas`
  (`tests/integracao/test_sala_isolada.py::test_de_dentro_da_sala_nada_alem_do_despachante_e_da_internet`). A entrada,
  as rotinas e o login não sobem nessa prova. Com o Compose inteiro no ar, a prova de que a sala não os alcança, pelo
  nome nem pelo IP, é
  `tests/integracao/test_fase3_ensaios.py::test_de_dentro_da_sala_entrada_rotinas_login_no_ar_nao_alcancaveis_por_nome_nem_por_ip`.
  Com uma imagem pronta (`MCP_CLARO_IMAGEM`), a prova confere antes que os arquivos `.py` do pacote dentro da imagem
  são os do repositório e, se não forem, falha antes de subir
  (`tests/integracao/test_sala_isolada.py::test_a_imagem_da_prova_e_deste_codigo`). A prova só é pulada fora do CI
  quando o passo da construção da imagem que falhou, ou o bloco final do log, mostra um erro de rede, por exemplo um
  nome que não resolve ou uma rede inalcançável. No CI, a falta de rede também falha a prova. Um aviso de rede num
  passo que não foi o que falhou não conta: o pip que tentou de novo e instalou, o apt que só avisou, ou um passo que
  o Docker cancelou porque outro falhou. A linha "Failed to fetch" sozinha também não conta, porque o uv e o apt a
  escrevem também quando recusam um certificado. Qualquer outra falha de construção falha a prova também fora do CI.
  Exemplos: certificado recusado no pip, no uv ou no apt (atrás de um proxy que troca o certificado), `uv.lock` ou
  `Dockerfile` quebrado, e um log que não é do BuildKit (sem passo que falhou nem bloco final). O Compose escreve o
  progresso da construção na saída e o fim no erro; o `docker build` escreve tudo no erro. A prova lê os dois juntos.
  Prova: o próprio `tests/integracao/test_sala_isolada.py`.

### Banco: migração 0006

- **Caminho de busca fixo nas funções do banco (vale para todos).** Toda função de gatilho roda com
  `search_path = pg_catalog, public, pg_temp`, inclusive as das migrações 0001, 0002 e 0005 (bloco CAMINHO). Por
  quê: sem isso, um papel que criasse uma tabela temporária com o nome de uma tabela do esquema (por exemplo,
  `transicoes_saida`) mudaria o que a guarda lê, e a entrada levaria uma ação de "aguardando ficha" direto a
  "enviada". Prova: `tests/integracao/test_migracao_0006.py::test_toda_funcao_de_gatilho_tem_caminho_de_busca_fixo`
  e `::test_sombra_de_transicoes_nao_leva_a_acao_direto_a_enviada`.
- **Os papéis do gateway não criam tabela temporária.** A migração 0006 tira do grupo PUBLIC o direito de criar
  tabela temporária no banco do gateway (`REVOKE TEMPORARY ... FROM PUBLIC`), também nos bancos que já existem. O
  `docker/postgres/iniciar/10-papeis.sh` faz o mesmo, mas só na primeira partida de um banco novo. O direito de
  conectar continua. É a defesa de cima; a de baixo é o caminho de busca fixo, que vale mesmo se um papel ganhar
  esse direito por outro caminho. O MCP-CLARO não desfaz migrações (o downgrade levanta erro, como em todas as
  migrações): quem voltar um banco à mão devolve o padrão do PostgreSQL com
  `GRANT TEMPORARY ON DATABASE <banco> TO PUBLIC`. Prova:
  `tests/integracao/test_migracao_0006.py::test_migracao_tira_o_temporary_do_public_num_banco_que_ja_existe` e
  `tests/integracao/test_papeis_sem_tabela_temporaria.py`.
- **Origem, endereço e autorização fixos (D8 e M1).** A origem não muda, nem pelo dono das tabelas (antes, só a
  falta de permissão impedia). Num terceiro, o endereço e a autorização só mudam de vazio para um valor; tirar uma
  autorização já preenchida também é recusado. Por quê: mudar o destino mandaria as chaves já guardadas (de acesso,
  de renovação e o segredo do cliente) para outro lugar. Prova:
  `tests/integracao/test_migracao_0006.py::test_origem_nao_muda_nem_pelo_dono` e
  `::test_terceiro_endereco_e_autorizacao_so_mudam_de_vazio_para_um_valor`.
- **A entrada perde em `sistemas` o que nenhum código usa (vale para todos).** A 0006 tira da entrada, a peça
  voltada à internet, o direito de mudar o nome, o endereço e o tipo de chave de um sistema e o de gravar a
  autorização no cadastro de um sistema novo. Ficam com ela o cadastro (nome, endereço, tipo de chave e origem), a
  configuração da autorização, a desativação (D27) e a volta a "pendente". Essas mudanças são da equipe, pelo dono
  das tabelas do banco, como o roteiro do ADMIN já diz. Por quê: com a entrada tomada, mudar o endereço de um
  sistema próprio ativo era aceito, e uma sondagem aceita depois o punha ativo no endereço novo, com as chaves por
  pessoa já guardadas. Prova: `tests/integracao/test_r6b_grants_entrada.py`.
- **A revisão do servidor é só de acrescentar e só vale na transação que a grava.** Uma revisão do servidor nunca
  muda nem se apaga. A hora dela (`revisoes_servidor.revisado_em`) é a hora do banco. A revisão vigente de um
  terceiro só vale com ele "ativo" e passa a valer na mesma transação que a grava: uma revisão que deixou de valer
  não volta. Sair de "ativo" por qualquer caminho (quarentena, recusado, pendente ou desativado) apaga a revisão
  vigente. Uma sondagem aceita depois deixa o terceiro ativo e sem nada na vitrine, até o ADMIN concluir a revisão
  de novo. Uma revisão nova só vale para um servidor que estava ativo ou em quarentena, fica ativo e fala a
  2026-07-28. O banco só publica ferramenta revisada, ativa, do próprio servidor e igual à declarada; mas não confere
  a impressão do servidor inteiro contra a lista declarada: isso fica com o despachante, e o mudou-parou põe em
  quarentena uma revisão que não confere. Prova: `tests/integracao/test_migracao_0006.py`, por exemplo
  `::test_revisao_vigente_e_do_proprio_sistema_e_a_mais_recente` e `::test_terceiro_que_sai_de_ativo_perde_a_revisao`.
- **A quarentena só sai pela conclusão da revisão (D27).** Um servidor em quarentena só sai para "ativo", pelo
  despachante, com a versão 2026-07-28 e uma revisão nova no mesmo passo (a conclusão do ADMIN); ou para
  "desativado", pela entrada ou pelo dono das tabelas. Fecha três saídas indevidas que existiam: a entrada gravar
  "pendente", a sondagem recusada e depois aceita, e a mudança de endereço. Num sistema próprio em quarentena só
  existiria a saída para "desativado", porque a revisão do servidor é só de terceiro; hoje nenhum código põe um
  sistema próprio em quarentena. Prova:
  `tests/integracao/test_migracao_0006.py::test_tres_saidas_indevidas_da_quarentena_fechadas`.
- **O despachante só suspende limites ativos.** Para a quarentena pela sondagem (D29 e M2), o despachante ganhou
  permissão de mudar a situação e o motivo de um limite. A guarda do banco deixa ele fazer só isso: levar um limite
  de "ativo" a "suspenso". Ele não reativa, não encerra nem reescreve o motivo de um limite. Prova:
  `tests/integracao/test_migracao_0006.py::test_despachante_so_suspende_limite_ativo`.
- **Os sinais dos três sinais.** Cada sinal nasce com a hora do banco e com uma ferramenta do próprio sistema. Ele
  não muda, e só se apaga depois de 10.080 minutos (7 dias), a maior janela que a política aceita: ninguém esconde
  um sinal que ainda conta. Prova:
  `tests/integracao/test_migracao_0006.py::test_sinal_nao_muda_e_so_sai_depois_da_maior_janela`
  e `::test_janela_do_gatilho_e_a_maior_da_politica`.
- **Rodadas sem resposta ao mudou-parou (A5).** Duas colunas novas em `sistemas`: quantas rodadas seguidas o
  servidor não respondeu e a hora (do banco) da última rodada somada. Só as rotinas mudam as duas. Uma revisão
  vigente nova, uma quarentena ou um endereço novo zeram a contagem e apagam a hora. O risco de duas cópias das
  rotinas somarem as 2 rodadas em segundos foi fechado por essa coluna, por decisão do coordenador; não é um risco
  aberto (veja "Quarentena e mudou-parou", abaixo). Prova:
  `tests/integracao/test_migracao_0006.py::test_rodadas_sem_resposta_so_as_rotinas_contam`.
- **Versões anunciadas só com os algarismos 0 a 9.** O desenho filtrava as versões que um servidor anuncia com
  `\d`, que no Python aceita também algarismos de outras escritas (como os arábico-índicos e os de largura cheia).
  O banco recusa esses textos, e a gravação da sondagem inteira falharia. Agora vale só o formato AAAA-MM-DD com
  `[0-9]`, até 10 versões; o resto vira "outra". Prova:
  `tests/unidade/test_ligacao_sondagem_fase3.py::test_versoes_anunciadas_batem_com_o_check_do_banco`.

### Ligação com os sistemas: barreira de pedidos, rastro, endereço e sondagem

- **Barreira de pedidos, para todos os sistemas.** Na Fase 2 havia um defeito: quando um sistema mandava um pedido
  pelo fio antigo (o canal de eventos SSE, aberto na resposta), o kit respondia sozinho com um segundo POST, levando
  a chave. Se esse segundo POST recebesse 401, uma ação que o sistema tinha recebido virava "não saiu", e um pedido
  igual saía de novo (efeito em dobro). Agora nenhum POST que não seja pedido sai: a resposta que o kit daria fica
  no transporte, com um 202 que não conta como status. Cada pedido barrado vira uma categoria do nosso vocabulário,
  nunca com texto, link ou nome de campo do sistema. Prova:
  `tests/unidade/test_ligacao_pedidos.py::test_401_no_segundo_post_do_fio_antigo_nao_vira_nao_saiu`.
- **Pedido barrado fora de uma chamada também vai ao extrato (vale para todos).** Quando um sistema pede algo no
  meio da leitura das ferramentas (a lista das declarações, na revisão e no mudou-parou) ou no meio da sondagem de
  versão, o pedido continua barrado e agora fica registrado: o extrato ganha a linha
  `declaracoes_sistema_pediu_dados` ou `sondagem_sistema_pediu_dados`, sem pessoa, com as categorias do nosso
  vocabulário, e o contador `pedidos_do_sistema_barrados` soma, com `onde=declaracoes` ou `onde=sondagem`. Antes, o
  pedido era barrado sem deixar rastro, e o D14 conta com esse registro no lugar da quarentena. O quadro "Pedidos
  recusados deste sistema", na tela do ADMIN, conta essas linhas ("Na leitura das ferramentas" e "Na sondagem", sem
  ferramenta) e também as ações que terminaram "não confirmada" ("Numa ação sem confirmação") ou "não saiu" ("Numa
  ação, que não saiu") depois de um pedido barrado. Vale também quando a leitura das ferramentas falha depois do
  pedido (o servidor pede e depois manda um erro) e quando a sondagem passa do prazo depois do pedido: o pedido fica
  no extrato e no contador, e a falha conta como antes. Prova: `src/mcp_claro/despachante/servico.py`,
  `tests/integracao/test_r6e_pedidos_fora_da_chamada.py` e `tests/integracao/test_r6b_pedidos_na_tela.py`.
- **Uma chamada, um POST de pedido (vale para todos).** O segundo pedido na mesma chamada não sai, e a chamada vira
  "dúvida" (numa ação, "não confirmada"). Antes, "uma chamada é um POST" dependia do kit. Prova:
  `tests/unidade/test_ligacao_pedidos.py::test_segundo_pedido_na_mesma_chamada_nao_sai_e_vira_duvida`.
- **Um vocabulário só para o que o sistema pede no meio da chamada (vale para todos).** Na leitura e na ação, as
  mesmas palavras: "dados por formulário", "que a pessoa abrisse um link", "uma resposta da IA do aplicativo", "a
  lista de pastas do aplicativo", "uma nova volta" e, para o fio antigo, "algo fora do padrão 2026-07-28", com "e"
  antes do último. Nunca texto, link ou campo do sistema. Com isso, o desfecho das ações passou a usar as mesmas
  palavras da leitura (no extrato, sem acento, como as outras frases dele; na tela da ação, a pessoa lê o desfecho
  com acento). Prova:
  `src/mcp_claro/ligacao/ponto.py` e
  `tests/unidade/test_sala_salto.py::test_pedidos_da_leitura_com_as_palavras_da_porta`.
- **Sem dados de rastreio a terceiros (D10, com a correção A3).** Num terceiro, `traceparent`, `tracestate` e
  `baggage` não vão nos cabeçalhos nem no `_meta` do corpo, com ou sem um provedor de rastros instalado: o
  transporte reescreve o corpo, e o `_meta` do pedido leva só o envelope do MCP (`io.modelcontextprotocol/*`). Um
  `traceparent` dado para um terceiro é recusado. Nos sistemas próprios, os cabeçalhos também saem; mas, com um
  provedor de rastros instalado, o próprio kit põe no `_meta` um `traceparent` novo, sem ligação com quem chamou.
  Isso cabe em `OBS_TRACE_PROPAGATE_DOWNSTREAM=sistemas_proprios`, e fica assim por decisão do dono. O `baggage` e o
  rastro de quem chamou nunca vão a ninguém. Hoje nenhum código instala um provedor. Prova:
  `tests/unidade/test_ligacao_terceiro.py::test_com_provedor_o_kit_poe_traceparent_e_so_a_barreira_do_terceiro_tira`.
- **Sem seguir desvios.** O cliente não segue redirecionamento (`max_redirects=0`): sem isso, o kit seguiria um 307
  ou 308 para a mesma origem. Um desvio no `tools/list` de um terceiro, pelo cliente local ou pela sala, é recusado
  sem seguir; vai ao extrato como `sala_desvio_recusado`, com o contador `sala_desvio_recusado{onde=declaracoes}`.
  Um 401 ou 403 na listagem também é recusa do servidor, sem linha da sala. Prova:
  `tests/unidade/test_ligacao_terceiro.py::test_desvio_da_mesma_origem_nao_e_seguido`,
  `tests/integracao/test_revisao_servidor.py::test_desvio_na_listagem_de_terceiro_pelo_caminho_de_verdade_vai_ao_extrato_e_conta`
  e `::test_listagem_que_pede_chave_nao_e_desvio`.
- **Endereço combinado de um terceiro.** Só https (http só em desenvolvimento e teste), sem usuário, senha nem
  fragmento, e todo IP do nome tem de ser público (um IPv6 que carrega um IPv4 dentro vale pelo de dentro). O nome
  conferido é o mesmo que o cliente HTTP conecta: ele é lido pelo mesmo leitor do `httpx`, na forma internacional
  (IDNA), e um nome que o IDNA recusa é recusado. A conferência do nome tem um prazo curto e fixo, de 3 s (o mesmo
  de abrir a conexão), para um resolvedor lento não comer o prazo da chamada. Endereço recusado: nada sai, e o
  extrato ganha a linha `sala_endereco_recusado`. A troca de código e a renovação do OAuth de um terceiro conferem o
  endereço de troca de chave (`token_endpoint`) do mesmo jeito. Prova: `tests/unidade/test_ligacao_terceiro.py` e
  `tests/integracao/test_g3a_autorizacao_terceiros.py`.
- **Sondagem de versão: só "versão" conta como versão antiga (vale para todos).** A sondagem agora separa seis
  desfechos: aceito; versão (respondeu como MCP e não fala a 2026-07-28: um erro JSON-RPC do próprio servidor, como
  o 400 do kit 1.x, o -32022 ou o -32601, também num SSE de várias linhas; um `server/discover` sem a 2026-07-28; ou
  o HTTP 405, que um servidor do transporte antigo do MCP costuma dar); desvio (3xx); não é MCP (404 sem resposta
  JSON-RPC, página HTML, 406 e 415); sem acesso (401, 403 e 407); e inalcançável (408, 429, 5xx, rede, prazo). Antes,
  desvio, 404 e página HTML contavam como versão antiga. Pela sala, uma resposta com código JSON-RPC fora de 32 bits
  ou com status HTTP fora de 100 a 599 fica sem veredito. Prova:
  `tests/unidade/test_ligacao_sondagem_fase3.py::test_desfecho_da_sondagem` e
  `::test_sse_com_json_rpc_em_varias_linhas_data_e_versao`.
- **O que fica guardado do `server/discover` de um terceiro.** Só uma lista do que fica: `ttlMs`, `cacheScope`,
  `supportedVersions` só com a 2026-07-28, as capacidades sem `experimental` nem `extensions`, e `resultType`
  "complete". Saem as `instructions`, o `_meta` (nome, título, descrição e site do fornecedor) e qualquer campo
  desconhecido. As versões que o servidor anunciou ficam à parte, conferidas. Prova:
  `tests/unidade/test_ligacao_sondagem_fase3.py::test_terceiro_guarda_do_discover_so_a_lista_do_que_fica`.
- **Listagem de ferramentas.** O cliente fixado lê até 50 páginas da lista de ferramentas, também dentro da sala. Pela
  sala, até 5.000 ferramentas de um terceiro; acima disso, a listagem falha como dúvida, o lado seguro. Prova:
  `tests/unidade/test_ligacao_sala.py::test_ferramentas_demais_voltam_como_duvida_sem_derrubar_a_sala`.
- **O log não leva bytes do sistema (vale para todos).** Nos registradores do kit no caminho do cliente fixado ficam
  só a mensagem fixa e o tipo do erro. São eles o `mcp.client.streamable_http` e o `client`, onde o kit 2.3.0
  escreve as mensagens da sessão, como a notificação que ele recusa. No `httpx2` sai a linha do pedido sem a frase
  do status, e no `httpcore2` só o nome do evento. O registrador `mcp.shared.jsonrpc_dispatcher` fica sem filtro, por
  decisão do coordenador: o servidor da entrada usa o mesmo, e filtrá-lo tiraria os rastros dos nossos próprios
  erros; no cliente, ele só escreve valores do sistema no nível DEBUG. Prova: `src/mcp_claro/logs.py` e
  `tests/unidade/test_g3r_logs.py`.

### Sala de visitas: o contrato entre o despachante e a sala

- **Só a falha de conexão antes de enviar é "nada foi enviado" (B6).** O despachante fala com a sala por um POST, sem
  tentar de novo, com o prazo da chamada mais 10 s. Se a conexão com a sala falha antes de o pedido sair, nada
  chegou ao terceiro. Qualquer outra coisa que não seja HTTP 200 com corpo do contrato (um 500, um corpo cortado,
  queda ou prazo depois de enviar) é dúvida, porque a sala pode ter chamado o terceiro. Prova:
  `tests/unidade/test_sala_salto.py::test_resposta_da_sala_que_nao_e_200_do_contrato_e_duvida`.
- **Sala cheia: nada sai, sem dúvida.** A sala atende até 32 chamadas ao mesmo tempo, somando todos os terceiros.
  Uma chamada espera uma vaga por no máximo 2 s; sem vaga, a sala responde "nada enviado", com a exceção
  `sala_sem_vaga`, e nada vai a um filho. Na ação, isso é "não saiu" ("a sala de visitas estava cheia; nada saiu"),
  e um pedido igual depois é uma ação nova; na leitura, o aplicativo recebe o erro legível "A sala de visitas estava
  cheia; nada foi enviado ao servidor de terceiros", com o nome do servidor e o pedido para tentar de novo em
  instantes. Antes, a espera pela vaga contava no prazo do despachante, e a chamada a um terceiro que nem tinha saído
  podia ficar em dúvida ("não confirmada") por causa de outros terceiros lentos. Na leitura das declarações (revisão
  do servidor e mudou-parou), a sala cheia é falha do MCP-CLARO, como a sala fora: o despachante responde 503 com o
  motivo `sala_sem_vaga`, a conclusão da revisão é recusada com o motivo `sem_resposta`, e o mudou-parou não conta a
  rodada. Só vale a sala cheia dita pelo processo pai da sala: só ele põe na resposta o cabeçalho
  `mcp-claro-sala: sala_sem_vaga`, e o filho escreve só o status e o corpo. O mesmo corpo de sala cheia sem o
  cabeçalho, que um filho tomado pelo terceiro poderia escrever, continua contando como o servidor que não entregou
  (502 `declaracoes_indisponiveis`), para ele não fugir da quarentena por não responder (A5). O contador
  `sala_sem_vaga` soma com `onde=acao` na ação, `onde=leitura` na leitura, `onde=declaracoes` na leitura das
  declarações e `onde=sondagem` na conferência da versão. O que fica: com a sala cheia em toda rodada, nenhuma
  conferência de terceiro é feita (veja "Riscos aceitos da Fase 3"). Prova: `src/mcp_claro/visitas/app.py`,
  `tests/unidade/test_r6e_sala_vagas.py` e, para as declarações, `tests/unidade/test_r6e_sala_cheia_marcada.py`,
  `tests/integracao/test_r6e_sala_cheia.py` e
  `tests/unidade/test_docs_fase3_g8a.py::test_a_sala_cheia_dita_pelo_pai_nao_conta_rodada_e_o_corpo_sem_a_marca_conta`.
- **Sala fora e endereço recusado só valem quando nada saiu.** Uma resposta "dúvida" nunca vira "nada foi enviado".
  Prova: `tests/unidade/test_sala_salto.py::test_duvida_que_diz_sala_fora_nao_vira_nada_foi_enviado_na_leitura`.
- **Nomes que só o despachante dá.** Na resposta da sala, os nomes que só o despachante dá ficam fora do contrato:
  `SalaFora` e `SalaRespostaInvalida`, e os motivos de sondagem "sala de visitas fora do ar" e "a sala de visitas não
  deu uma resposta válida". A resposta vira dúvida (`SalaRespostaInvalida`), porque uma sala que respondeu nunca está
  fora. Assim, um filho da sala tomado pelo terceiro que diga "sala fora" não escapa da regra das duas rodadas sem
  resposta: a listagem dá 502 `declaracoes_indisponiveis`, que as rotinas contam. Na leitura, a falha é dúvida, e não
  "nada foi enviado". Na ação, a resposta é "não confirmada", e não "não saiu". Prova:
  `tests/unidade/test_ligacao_sala.py::test_chamada_da_sala_com_nome_do_despachante_e_a_reserva`.

### Cadastro, revisão de ferramenta e vitrine de terceiros

- **O esquema de um terceiro vai limpo à vitrine.** Fica só o que valida (as palavras do JSON Schema 2020-12 que
  mudam o veredito) e as de estrutura, como `$ref` e `$defs`. Saem descrição, título, exemplos, comentário, valor
  padrão, toda palavra desconhecida, `format` fora dos nomes do JSON Schema e as palavras `content*`. Continuam indo
  ao modelo de IA, porque são precisos para chamar: nomes de campo, valores de `enum` e `const`, e `pattern`. As dicas
  da ferramenta saem só da classe revisada, com `openWorldHint` sempre verdadeiro, e a descrição termina com "Servidor
  de terceiros: a resposta e texto escrito por estranhos." O ADMIN pode escrever a descrição de cada campo do primeiro
  nível, de até 300 letras. Prova: `tests/unidade/test_esquema_limpo.py` e `tests/unidade/test_como_tool_terceiro.py`.
- **Nomes neutros nas definições do esquema de terceiro.** No esquema que o aplicativo vê, os nomes das definições
  (`$defs` e `definitions`) viram nomes neutros (`d1`, `d2`, ...), com cada `$ref` reescrito, e o `$schema` da raiz
  sai: a entrada confere sempre como 2020-12. Os números seguem uma ordem fixa, a ordem em que o banco guarda as
  chaves (a mais curta antes), e não a ordem em que o fornecedor as escreveu: assim a revisão, a prévia do ADMIN e o
  aplicativo veem os mesmos nomes. Antes, esses dois textos, escritos pelo fornecedor, chegavam ao aplicativo e à
  prévia do ADMIN. A troca vem depois de todas as conferências da revisão, e por isso não muda o que é recusado; o
  veredito da conferência dos argumentos é o mesmo, e a impressão continua a da definição declarada (o mudou-parou
  não muda). A vitrine e a prévia do ADMIN fazem a troca de novo. Prova: `tests/unidade/test_r6b_nomes_neutros.py`,
  `tests/integracao/test_r6b_revisao_nomes.py` e `tests/integracao/test_r6b_nomes_do_banco.py`.
- **Toda origem que não seja "proprio" conta como terceiro.** Na marca de estranhos, na escolha da ligação e na
  chave do cofre, o MCP-CLARO trata qualquer origem que não seja `proprio` como terceiro: é o lado que aperta.
  Prova: `tests/unidade/test_estranhos.py` e
  `tests/unidade/test_ligacao_terceiro.py::test_ligacao_para_escolhe_pela_origem`.
- **A dica "destrói" sai da classe revisada (vale para todos).** Antes, a dica do fornecedor podia vencer: numa
  ferramenta da classe "muda", um "não destrói" escrito pelo fornecedor passava. Agora, numa ação, a dica sai sempre
  da classe: verdadeira em "muda" e falsa em "tem volta", diga o fornecedor o que disser. Prova:
  `tests/unidade/test_como_tool_terceiro.py`.
- **A definição declarada fica guardada (D32), também nos próprios, e a vitrine de terceiro a exige.** Uma
  ferramenta revisada de terceiro sem a definição declarada guardada não aparece na vitrine, e a conclusão da revisão
  do servidor recusa publicá-la ("revise a ferramenta X de novo antes de concluir a revisão do servidor"). Prova:
  `tests/integracao/test_revisao_servidor.py::test_revisada_sem_definicao_declarada_nao_e_publicada`.
- **Nenhuma regra de formato do fornecedor roda no MCP-CLARO.** Num servidor de terceiros, a entrada nunca confere
  `pattern` nem `uniqueItems` do fornecedor. Ela confere com uma cópia do esquema revisado sem essas palavras; quem
  confere é o servidor do terceiro. A vitrine continua mostrando o `pattern` ao aplicativo. Nenhuma lista é comparada
  item a item. Prova: `tests/unidade/test_esquema_limpo.py`.
- **Onde tirar a regra faria recusar um pedido válido, a cópia afrouxa.** `not` com regra dentro sai; `oneOf` com
  regra num ramo vira `anyOf` no mesmo lugar, ou sai se o lugar já tem `anyOf`; `if`, `then` e `else` saem quando a
  regra está na condição; `maxContains` sai quando o `contains` tem regra. Cada parte fica no lugar dela ou sai: a
  cópia nunca é maior nem mais funda que o esquema revisado. Todo pedido que o esquema aceita, a cópia aceita.
  Prova: `tests/unidade/test_esquema_limpo.py`.
- **Saiu a conta de custo das regras de formato.** Uma versão anterior lia a árvore interna do módulo `re` do Python
  (`re._parser`), com limites de 256 letras, 1.024 combinações e grau 2. Saiu: qualquer `pattern` válido passa na
  revisão, porque a entrada não o roda.
- **Palavras que a revisão de terceiro recusa.** Em qualquer lugar do esquema, inclusive na raiz e numa definição
  que ninguém cita: `patternProperties`, `$id`, `$anchor`, `$dynamicAnchor`, `$dynamicRef`, `unevaluatedProperties`
  e `unevaluatedItems`. Por quê, nas duas últimas: a biblioteca confere de novo cada parte abaixo da palavra, e o
  custo dobra a cada nível. Com 15 níveis e um pedido minúsculo, a conferência levou 12 s de tempo de relógio numa
  medida da etapa 3; refeita na etapa 5, levou cerca de 11 s, de relógio e de CPU, quase iguais. `$schema` é recusado
  fora da raiz: numa parte, ele trocaria a versão do JSON Schema daquela parte. E é recusada toda `$ref` que não seja
  `#/$defs/<nome>` ou `#/definitions/<nome>` para uma definição que existe. A tela do ADMIN diz o motivo de cada
  recusa. Prova: `tests/unidade/test_esquema_limpo.py`.
- **Tetos do esquema de terceiro.** Até 2.000 partes e 64 níveis. A raiz é o nível 0; a parte mais funda pode estar
  no nível 63.
  - Contam todas as partes do documento, inclusive as definições que nenhuma referência chama.
  - À parte, contam as partes e os níveis que a conferência visita seguindo as referências: a partir da raiz e também
    a partir de cada definição (`$defs` ou `definitions`, em qualquer lugar do esquema, inclusive dentro de outra
    definição), citada ou não, como se a raiz a chamasse (a definição fica no nível 1). Cada definição, sozinha, tem
    de caber nos dois tetos. Cada referência conta a definição de novo, e uma definição chamada de um lugar mais fundo
    conta os níveis dela a partir dali.
  - Nenhum esquema em ciclo, nem entre definições que nenhuma referência da raiz chama: um esquema recursivo, como uma
    árvore ou uma lista encadeada, não entra num terceiro, citado ou não.
  - Cada texto de `$ref` e cada nome de definição têm até 256 letras. A entrada lê o texto inteiro da referência
    cada vez que a segue: uma definição curta que chamava outra de nome com 120 mil letras, 600 vezes em cada item,
    passava na revisão e custava 4,3 s de tempo de CPU por pedido, dentro do orçamento de passos (abaixo). O
    orçamento também conta essas letras.
  - Cada `enum` com até 1.000 valores, e o esquema, depois da limpeza, com até 256 KiB (262.144 bytes em JSON
    compacto, UTF-8).
  - Por que as definições que ninguém cita contam: a cópia da entrada anda por todas as definições e segue as
    referências de dentro delas. Antes, uma cadeia de 20 definições que ninguém citava passava na revisão e dava erro
    interno (estouro da pilha do Python): na revisão, em vez da frase ao ADMIN, e, sem a guarda da revisão, em cada
    chamada da entrada.
  - Nenhum percurso do `cadastro/esquema.py` (revisão de terceiro e cópia) usa recursão: cada um anda com uma pilha
    própria. Se a pilha do Python estourar numa biblioteca que ele chama (por exemplo, o `json` com um dado muito
    fundo), a revisão recusa com o motivo "fundo". Antes dele, em próprio e em terceiro, roda a conferência do
    esquema declarado (`check_schema`, em `entrada/argumentos.py`), que ainda desce com recursão: um declarado com
    uns 100 níveis ou mais é recusado, e a tela do ADMIN diz "O esquema de entrada desta ferramenta tem partes demais
    umas dentro das outras para o MCP-CLARO conferir. Peça ao fornecedor um esquema mais simples." Antes, dava erro
    interno (erro 500 na tela do ADMIN). Num terceiro, com 65 a uns 99 níveis, a recusa é a do teto de níveis.
  - No teto de 64 níveis, a conferência de um esquema novo gasta cerca de 800 dos 1.000 quadros da pilha do Python.
    A entrada usa uns 60.

  Prova: `tests/unidade/test_esquema_limpo.py`, `tests/integracao/test_argumentos_fundos.py` e
  `tests/integracao/test_vitrine_terceiros.py`.
- **`format` e sistemas próprios.** A entrada não confere `format`, como nas Fases 1 e 2; num terceiro, `format` só
  fica com os nomes do JSON Schema. Num sistema próprio, nada disso vale (D3): o esquema é gravado como o ADMIN
  revisou, e a entrada confere `pattern` e `uniqueItems`.
- **Limpeza.** A função `ferramenta_por_id` do cadastro, sem nenhum chamador, foi apagada.

### Entrada: tetos, orçamento e recusas legíveis

- **Tetos de tamanho num terceiro.** Num servidor de terceiros, a entrada mede os argumentos antes de conferir o
  esquema e recusa:
  - texto de mais de 10.000 letras, inclusive nome de campo;
  - lista de mais de 1.000 itens;
  - número inteiro de mais de 100 dígitos;
  - mais de 20.000 letras somando tudo;
  - mais de 5.000 partes;
  - mais de 64 níveis de listas e objetos, um dentro do outro. É o mesmo teto de níveis do esquema de terceiro.

  A medida anda sem recursão e tem custo preso aos tetos. Medido em tempo de CPU, com a memória pelo `tracemalloc`:
  até 4 ms e menos de 0,1 MB, tanto com um pedido de 2 milhões de níveis (4 MB) quanto com 300 listas de 999
  números (medida da etapa 4, refeita na etapa 5 com o mesmo resultado). O que está abaixo do teto de níveis nem é
  aberto, e os números são comparados, nunca escritos em texto. Sem o teto de dígitos, os números eram a única parte
  sem tamanho: o leitor de JSON aceita inteiros de até 4.300 dígitos, e um pedido de 4 MB só de números passava nos
  tetos. A frase leva no máximo cinco problemas e corta o caminho em 10 pedaços: "Argumentos grandes demais para um
  servidor de terceiros: ... Nada foi enviado." (motivo `argumento_grande_demais`). Um teste prova que os tetos
  rodam antes da conferência do esquema, na leitura e na ação. Sistema próprio: sem tetos (D3). Prova:
  `tests/unidade/test_argumentos_terceiros.py`.
- **Risco de largura: o orçamento de passos.** A conferência custa até o tamanho do esquema (com as referências
  seguidas e os valores de `enum` e `const`) vezes o tamanho do pedido. Num terceiro, quatro coisas prendem o tempo e
  a memória:
  - os tetos da entrada prendem o pedido (item acima);
  - os tetos da revisão prendem o esquema: 2.000 partes, 64 níveis, 1.000 valores em cada `enum`, 256 letras em cada
    `$ref` e em cada nome de definição, e 256 KiB;
  - só os cinco primeiros erros são montados; os outros nem são criados;
  - um orçamento de 50.000 passos, contado e não medido no relógio. Cada palavra do esquema conta um passo, e mais um
    por item que percorre: valores do `enum`, ramos de `anyOf`, `oneOf` e `allOf`, campos de `properties` e
    `required`, e itens ou campos do pedido em `items`, `contains`, `additionalProperties` e `propertyNames`. `enum` e
    `const` comparam listas e objetos parte por parte, e cada par de partes comparado conta um passo. Cada vez que a
    conferência segue um `$ref`, conta 3 passos a mais, e mais um a cada 100 letras do texto da referência. Seguir
    uma referência, mesmo curta, custa uns 7 microssegundos de CPU nesta máquina, contra 1 a 3 de um passo comum, e
    a biblioteca lê o texto inteiro da referência cada vez que a segue. Com os 3 passos a mais, a conta de um pedido
    no máximo quadruplica: um pedido que gastava até um quarto do orçamento continua cabendo. Cada erro conta 4
    passos, e mais um a cada 100 letras da mensagem, em cada palavra por onde passa. Um esquema `false` num ramo de
    `anyOf` ou `oneOf`, ou em `not`, `if` e `contains`, é conferido como `{"not": {}}`, que dá o mesmo veredito, para
    o erro dele também contar.

  Passou do orçamento, a entrada recusa com "O esquema deste servidor ficou caro demais para conferir este pedido.
  Nada foi enviado." (motivo `esquema_caro_demais`). O mesmo pedido gasta sempre os mesmos passos.

  Medido em várias rodadas. Os tempos são de CPU, salvo onde está escrito "de relógio". A memória foi medida com o
  `tracemalloc` ligado, que deixa a conferência de 3 a 6 vezes mais lenta; por isso alguns casos têm dois tempos.
  "Faixa" é do menor ao maior tempo das rodadas; "mediana" é o tempo do meio:
  - `anyOf` de 1.990 `const` com lista de 300 itens: era 8,5 s de relógio e 1,7 GB, num número de antes da mudança
    sem o registro de como a memória foi medida; agora, faixa de 0,05 a 0,27 s (mediana de 0,06 a 0,1 s; de 0,2 a
    0,45 s com `tracemalloc`, e 23 MB);
  - `enum` de 200 mil valores com lista de 100 itens: era 7,7 s de relógio. Agora a revisão recusa o esquema (mais de
    1.000 valores), e o orçamento sozinho também recusaria, em 0,01 s;
  - `enum` de 24 listas de 5.000 partes, chamado por 995 referências (passa na revisão), com pedido válido: era 4,9 s
    de relógio pela entrada, dentro do orçamento; agora a entrada recusa pelo orçamento, em 0,06 a 0,08 s de
    relógio (faixa);
  - `not`, `if` ou `contains` com esquema `false`, centenas de vezes, com pedido de 500 KB: era de 0,9 a 1,2 s de
    relógio; agora de 0,01 a 0,03 s de relógio (faixa);
  - uma definição curta que chama outra de nome com 120 mil letras, chamada 600 vezes em cada item de uma lista de
    1.000 itens: passava na revisão e levava 4,3 s (4,7 s de relógio pela entrada). Agora a revisão recusa o nome, e,
    sem a revisão, o orçamento para em 0,014 s;
  - uma cadeia de 60 referências curtas em cada item de uma lista de 1.000 itens (o pior caso achado antes): levava,
    em faixa, de 0,28 a 0,49 s (de 1,6 a 2,3 s com `tracemalloc`), porque parava no orçamento só depois de seguir
    umas 49 mil referências. Com os 3 passos a mais por referência, para depois de umas 12 mil, em 0,07 a 0,12 s
    (de 0,4 a 0,7 s com `tracemalloc`). Os outros casos com referência ficaram, em faixa, entre 0,06 e 0,2 s (nomes
    escritos com `%XX` custam um pouco mais);
  - o pior caso achado agora: pela mediana, um `allOf` com 660 `if` vazios (`{"if": {}, "then": {}}`) em cada item
    de uma lista de 1.000 itens, em faixa de 0,11 a 0,20 s nas rodadas da etapa 4; numa rodada da etapa 5, com a
    máquina ocupada por outros trabalhos, de 0,13 a 0,30 s (mediana de 0,17 s). Pelo maior tempo, o `allOf` de 660
    `if` vazios chegou a 0,30 s na rodada da etapa 5; nas da etapa 4, o maior foi o `anyOf` de 1.990 `const`, com
    0,27 s. O maior pico de memória medido foi 23 MB.

  O que fica:
  - um esquema de terceiro caro pode recusar até pedido válido. Exemplo: com `items: {enum: [1.000 valores]}`, cada
    item custa 1.002 passos, e uma lista válida com mais de 49 itens é recusada. Outro: com uma cadeia de referências
    em cada item de uma lista de 1.000 itens, 12 referências por item ainda cabem (49.005 passos), e 13 não cabem
    mais (antes dos 3 passos a mais por referência, gastavam 14.005). O fornecedor precisa simplificar o esquema;
  - ficam fora da conta, presos só pelos tetos do pedido e da revisão, duas coisas: o que a biblioteca faz de uma vez
    sobre um valor só (comparar dois textos ou dois números, escrever um valor na mensagem de um erro antes de o erro
    ser contado) e a primeira conferência de cada esquema (`check_schema`), que fica guardada;
  - o tempo depende da máquina. Nada disso é garantia.

  Prova: `tests/unidade/test_argumentos_orcamento.py`, com as contas, que dão sempre o mesmo resultado; os tempos
  ficam fora dos testes, de propósito, porque uma máquina carregada faria um teste de tempo falhar sem motivo.
- **Em qualquer sistema, a conferência do esquema não vira erro interno (vale para todos).**
  - Aninhamento que estoura a pilha do Python vira a recusa `argumento_fora_do_esquema` ("(raiz): aninhamento fundo
    demais para conferir com o esquema"). Vale para o pedido fundo e para o esquema fundo: a primeira conferência de
    um esquema (`check_schema`) desce com recursão e estoura com uns 100 níveis de `properties`.
  - Número que a conta de `multipleOf` não consegue dividir (inteiro enorme com fração, infinito ou NaN; o leitor de
    JSON do kit aceita `NaN` e `Infinity`) vira a recusa `argumento_fora_do_esquema` ("(raiz): numero que nao da para
    conferir com o esquema").
  - Na revisão do ADMIN, em sistema próprio e de terceiro, um esquema declarado fundo demais para a pilha (uns 100
    níveis ou mais) é recusado (veja "Tetos do esquema de terceiro").

  Antes, os três davam erro interno; na revisão, era erro 500 na tela do ADMIN. Os cinco primeiros erros valem
  também para sistema próprio, por decisão do coordenador: nada que era aceito passou a ser recusado, e, com cinco
  erros ou menos, a frase é a mesma de antes. Prova: `tests/unidade/test_argumentos_orcamento.py` e
  `tests/integracao/test_argumentos_fundos.py`.
- **Ação com mais de 500 níveis (vale para todos).** Ação com mais de 500 níveis de listas e objetos é recusada antes
  de virar protocolo, com o motivo `aninhamento_fundo`: "O pedido tem mais de 500 niveis de listas e objetos, um
  dentro do outro, e o MCP-CLARO nao guarda pedido assim. Nada foi enviado." Antes, a partir de 500 níveis, a mesma
  ação já dava erro interno (a conferência do caractere nulo descia com recursão). Nada do que virava protocolo
  passou a ser recusado (D3). Leitura de sistema próprio: como antes.

  No envio, o cliente fixado não consegue escrever pedido com 256 níveis de argumentos ou mais: o escritor do
  pydantic, no kit, para aí. Antes, essa chamada falhava com a conexão já aberta e virava "dúvida", embora nada
  tivesse saído. Agora o cliente monta o pedido antes de abrir a conexão e, se não consegue, a chamada é "não saiu"
  (`pedido_fundo_demais`), sem nenhum byte enviado. Assim, uma ação de sistema próprio com 256 a 500 níveis vira
  "não saiu", e uma leitura de sistema próprio com 256 níveis ou mais volta com erro legível. Até 255 níveis, nada
  mudou. Num terceiro, isso não chega a acontecer: os tetos da entrada param em 64 níveis. Prova:
  `tests/unidade/test_ligacao_fundo.py` e `tests/integracao/test_saida_terceiros.py`.
- **Número NaN ou infinito (vale para todos).** Pedido com número NaN ou infinito é recusado com o motivo
  `numero_invalido`, em sistema próprio e de terceiro:
  - na ação, antes de virar protocolo: "O pedido traz um numero que nao e um numero de verdade (NaN ou infinito), e
    o MCP-CLARO nao guarda pedido assim. Nada foi enviado." O JSON do protocolo e o banco não guardam esses números;
  - na leitura, antes da catraca, sem gastar a cota do dia: "A leitura traz um numero que nao e um numero de verdade
    (NaN ou infinito), e o MCP-CLARO nao consegue enviar esse numero ao sistema. Nada foi enviado." O pedido da
    entrada ao despachante é JSON, e esse JSON não leva NaN nem infinito.

  O leitor de JSON do kit aceita `NaN`, `Infinity` e `1e400` (que vira infinito). Antes, a ação e a leitura davam
  erro interno (-32603), e a leitura nem chegava ao despachante. D3: só mudou o que dava erro interno. Com
  `multipleOf` no esquema, a recusa `argumento_fora_do_esquema` (item acima) vem antes. Prova:
  `tests/integracao/test_argumentos_numeros.py` e `tests/unidade/test_pedidos_numeros.py`.

### Texto de estranhos e leitura com medida

- **Leitura com medida: cada bloco só com o que é dele (vale para todos).** Cada bloco do resultado sai só com
  `type`, o texto e as `annotations`; um recurso embutido, só com `uri`, `mimeType` e `text`. O `_meta` do bloco e o
  do recurso embutido nunca passam: podiam levar dado pessoal ou instrução do sistema. Num terceiro, as `annotations`
  também saem. A máscara não usa recursão. Um texto JSON com campos pessoais com mais de 10.000 níveis, ou que o leitor
  de JSON não abre, é recusado com `resposta_funda_demais`. O teto é fixo para o desfecho não depender da máquina:
  o leitor de JSON abriu uns 40 mil níveis numa máquina de testes e 100 mil no CI. Prova: `tests/unidade/test_medida_meta.py`
  e `tests/integracao/test_estranhos_leitura.py::test_texto_json_fundo_com_campos_pessoais_e_mascarado_ou_recusado`.
- **Resultado fundo demais (vale para todos).** Na leitura, um resultado com mais de 199 níveis, ou que o kit não
  escreve, vira o erro nosso `resposta_funda_demais`. Na prática, o leitor do kit já recusa mais de 200 níveis numa
  mensagem, e a leitura volta como falha legível de dúvida. Prova:
  `tests/integracao/test_estranhos_leitura.py::test_resposta_de_300_niveis_vira_erro_nosso_legivel`.
- **A moldura de estranhos.** A resposta de um terceiro, e a de ferramenta própria marcada "traz texto de fora",
  chega ao aplicativo inteira dentro de uma moldura com uma marca sorteada (12 letras hexadecimais, uma por
  resposta), depois de um aviso do MCP-CLARO. Os dados estruturados viram mais um bloco emoldurado, com CPF e e-mail
  mascarados. Quem emoldura a leitura é o despachante; se uma resposta de estranhos chegar à entrada sem a moldura,
  a entrada emoldura. Na ação, o bloco "Protocolo ..." vem primeiro, fora da moldura. O aviso de partes sem texto
  retiradas pela medida fica fora da moldura só quando a porta de saída guardou essa conta; sem ela, fica dentro,
  como texto do sistema. A marca e o aviso reduzem o que uma IA enganada causa; não impedem. Prova:
  `tests/integracao/test_estranhos_leitura.py` e `tests/integracao/test_estranhos_acao.py`.
- **Ferramenta própria marcada não anuncia esquema de saída (M3).** Sem isso, o kit do aplicativo dá erro: a
  ferramenta anunciaria um esquema de saída e o resultado viria sem `structuredContent`. Prova:
  `tests/integracao/test_estranhos_leitura.py::test_propria_marcada_com_esquema_de_saida_e_lida_pelo_kit_sem_erro`.
- **O texto do terceiro não aparece nas telas da pessoa (A4).** Para o resultado de estranhos, a tela da ação diz "O
  servidor de terceiros respondeu. A resposta foi entregue ao aplicativo, marcada como texto de estranhos." O
  aplicativo continua recebendo esse texto, emoldurado. Prova:
  `tests/integracao/test_telas_fase3_pessoa.py::test_resposta_de_terceiro_com_pedido_de_senha_nao_chega_as_telas_da_pessoa`.

### Três sinais

- **O alerta vale sempre para ferramenta própria marcada (B7).** O sinal "leu texto de fora" de ferramenta própria
  marcada liga o alerta sempre; o de terceiro, só com a chave da Fase 3 ligada ou para pessoa de teste. "Viu dado
  privado" conta sempre. Prova:
  `tests/integracao/test_tres_sinais.py::test_texto_de_fora_de_terceiro_so_conta_com_terceiros_e_o_de_propria_marcada_sempre`.
- **Quem pede diz se terceiros valem.** No pedido de ação, o campo que diz se terceiros valem para quem pede
  (`Pedido.terceiros`) é obrigatório, sem valor padrão: um chamador que esquecesse o campo faria o sinal de terceiro
  não contar no alerta dos três sinais. Prova: `tests/unidade/test_f4_pedido_terceiros.py`.
- **Ação já liberada antes do alerta sai.** O alerta vale para o que for pedido depois. Prova:
  `src/mcp_claro/saida/pedidos.py` (docstring) e `tests/integracao/test_tres_sinais.py`.
- **A consulta pelo protocolo e o pedido igual gravam o sinal de novo.** Quando uma ação enviada volta com o
  resultado, pela consulta ou por um pedido igual, o texto do sistema chega de novo ao aplicativo, e o sinal é gravado
  de novo. Prova:
  `tests/integracao/test_tres_sinais.py::test_resultado_de_acao_de_terceiro_conta_enviada_e_consulta`.
- **A janela fica entre 1 e 10.080 minutos também no código.** A política já aceita só de 5 a 10.080; o código traz
  para dentro da maior janela de novo, por defesa. Prova: `src/mcp_claro/regras/sinais.py` (`janela_valida`).
- **Os sinais somem depois de 7 dias.** As rotinas apagam, de hora em hora, os sinais vistos há mais de 7 dias, sem
  extrato. Prova: `tests/integracao/test_rotinas_acoes.py` e
  `tests/integracao/test_migracao_0006.py::test_rotinas_apagam_o_sinal_velho`.

### Quarentena e mudou-parou

- **A quarentena suspende os limites antes de cancelar a fila.** Numa transação só: a trava do sistema, depois a
  linha do sistema, depois as linhas dos limites e por fim as da fila. A ordem das travas da porta de saída está
  escrita em `saida/fila.py`: as consultivas, depois a linha do limite, depois a da ficha, depois a da fila. A tela
  que encerra um limite segue a mesma ordem, e as duas não se travam uma à outra. Prova:
  `tests/integracao/test_saida_quarentena.py::test_tela_que_encerra_o_limite_e_quarentena_ao_mesmo_tempo_sem_impasse`
  e `tests/integracao/test_telas_fase3_pessoa.py::test_revisao_que_encerra_dois_limites_e_quarentena_ao_mesmo_tempo_sem_impasse`.
- **A quarentena só vale contra a revisão comparada (B1).** Se outra cópia das rotinas já pôs em quarentena, ou se o
  ADMIN concluiu uma revisão nova no meio, nada muda. Prova:
  `tests/integracao/test_rotinas_terceiros.py::test_conclusao_nova_no_meio_da_rodada_nao_vira_quarentena`.
- **O mudou-parou confere o servidor inteiro.** As rotinas conferem os terceiros ativos com revisão vigente, o
  servidor inteiro, contra a lista de ferramentas da revisão. Qualquer mudança (mudada, nova ou sumida) leva à
  quarentena, inclusive uma revisão gravada fora da receita (impressão que não confere com a lista), com zero
  mudanças. Em todos os casos há um aviso ao ADMIN, por e-mail e webhook, sem nome de ferramenta, na mesma transação
  da quarentena. Prova:
  `tests/integracao/test_rotinas_terceiros.py::test_mudanca_so_de_texto_em_terceiro_leva_o_servidor_inteiro_a_quarentena`
  e `::test_revisao_com_impressao_fora_da_receita_vai_a_quarentena`.
- **Avisos novos ao ADMIN.** Três eventos novos para os webhooks, cada um com o assunto que o e-mail leva (o assunto
  vai sem acento):
  `sistema_em_quarentena` (`MCP-CLARO: servidor de terceiros em quarentena (mudou, parou)`),
  `sistema_em_quarentena_sem_resposta` (`MCP-CLARO: servidor de terceiros em quarentena (nao respondeu a conferencia)`)
  e `sistema_em_quarentena_revisao_inconsistente`
  (`MCP-CLARO: servidor de terceiros em quarentena (revisao que nao confere)`). O último sai quando a revisão guardada
  não confere com a própria lista de ferramentas, no lugar do aviso de "mudou 0 ferramentas". Os três dizem que os
  limites das pessoas foram suspensos. Prova: `src/mcp_claro/extrato/frases.py` (`AVISOS_ADMIN`) e
  `tests/unidade/test_avisos_frases.py`.
- **Terceiro que não responde (A5).** Duas rodadas seguidas sem resposta levam à quarentena com "não respondeu à
  conferência". Uma resposta no meio zera a contagem. Uma rodada que o MCP-CLARO não conseguiu fazer não conta nem
  zera: o despachante fora do ar (sem conexão, 503 ou 404 dele), a sala de visitas fora, a sala de visitas cheia dita
  pelo processo pai dela (503 com o motivo `sala_sem_vaga`), a chave das declarações que o cofre não abriu e uma
  falha do MCP-CLARO no banco ou no grupo de conexões do despachante durante a conferência (503 com o motivo
  `conferencia_nao_feita`). Conta qualquer outra falha: o servidor que não entregou as declarações (502, também
  quando ele declarou o que o banco não guarda e quando o corpo de sala cheia vem sem o cabeçalho do processo pai),
  um erro 500 do despachante sem motivo nosso ou o prazo de leitura. Com o intervalo do início
  (`TOOL_DRIFT_CHECK_SECONDS`, 900 s), um terceiro que para de responder vai à quarentena em até cerca de 30
  minutos. Prova:
  `tests/integracao/test_rotinas_terceiros.py::test_terceiro_que_nao_responde_em_duas_rodadas_seguidas_sai_da_vitrine`,
  `::test_conferencia_nao_feita_pelo_mcp_claro_nao_conta_rodada`,
  `tests/integracao/test_r6e_vistas.py::test_duas_rodadas_com_o_banco_do_despachante_fora_nao_levam_a_quarentena` e
  `tests/integracao/test_r6e_sala_cheia.py::test_sala_cheia_nas_declaracoes_e_na_sondagem_nao_conta_contra_o_terceiro`.
- **A falha do MCP-CLARO na leitura das declarações não conta contra o servidor.** Na leitura das declarações
  (revisão e mudou-parou), com a sala de visitas fora, a resposta do despachante é 503 `sala_fora`; com a sala cheia
  dita pelo processo pai dela, 503 `sala_sem_vaga`. Se o cofre não abre a chave das declarações, ou se não dá para
  saber a origem do sistema, a resposta é 503 `chave_indisponivel`. Uma falha do banco ou do grupo de conexões do
  despachante durante a leitura (por exemplo, todas as conexões ocupadas) é 503 `conferencia_nao_feita`. As rotinas
  não contam essas rodadas como "sem resposta". O 503 `conferencia_nao_feita` é só para a falha nossa: um impasse no
  banco, a conexão que cai ou o grupo de conexões tomado. Quando o banco recusa o que o servidor declarou, a falha é
  do servidor: um nome de ferramenta com mais de 1.000 bytes, um texto com caractere nulo ou que não se escreve em
  UTF-8, ou um erro do banco causado pelo dado ao gravar (as classes 22 e 54 do PostgreSQL) dão 502
  `declaracoes_indisponiveis`, e a rodada conta. Um nome de mais de 128 letras que cabe em 1.000 bytes continua
  guardado: o ADMIN o vê, e a revisão o recusa. O 502 `declaracoes_indisponiveis` fica para o servidor que não
  entregou e vale também para o endereço de terceiro que não é público e para o corpo de sala cheia sem o cabeçalho
  do processo pai ("Sala cheia: nada sai, sem dúvida", acima). Prova: `tests/integracao/test_g3r_despachante.py`,
  `tests/integracao/test_r6e_vistas.py::test_pool_tomado_nas_declaracoes_e_503_conferencia_nao_feita`,
  `tests/unidade/test_r6e_falha_de_dado.py` e `tests/integracao/test_r6e_dados_do_terceiro.py`.
- **Duas cópias das rotinas contam uma rodada só.** Uma rodada sem resposta só soma quando a última soma tem mais de
  metade de `TOOL_DRIFT_CHECK_SECONDS` (com o padrão, 450 s). Assim, duas cópias das rotinas que rodam a segundos uma
  da outra contam uma rodada só. Prova:
  `tests/integracao/test_rotinas_terceiros.py::test_duas_rodadas_sem_resposta_a_segundos_uma_da_outra_somam_uma`.
- **A chave do aviso de quarentena leva a revisão comparada.** Um servidor que vai e volta avisa a cada quarentena, e
  um servidor mudo de novo depois de nova revisão avisa de novo. O desenho previa só as impressões novas na chave; a
  revisão comparada ficou por decisão do coordenador. Prova:
  `tests/integracao/test_rotinas_terceiros.py::test_servidor_que_vai_e_volta_avisa_o_admin_a_cada_quarentena`.
- **Aviso de ordem de serviço a vencer (corrige a Fase 1).** A rodada deixa de fora as ordens já avisadas e segue de
  lote em lote até o fim da janela. Antes, com mais de 500 ordens já avisadas vencendo antes, as seguintes não eram
  avisadas. Prova: `src/mcp_claro/rotinas/ordens.py` e `tests/integracao/test_rotinas_tarefas.py`.
- **Resumo semanal, painel e alerta.** No resumo semanal, "lado dos sistemas" passa a ser só dos sistemas próprios,
  com a linha nova "servidores de terceiros" (D30); a soma das três é o total da decisão 8. No Grafana, o bloco
  "Servidores de terceiros (Fase 3)": recusados por versão, em quarentena agora e por situação. O alerta alto
  "Sistema recusado por versão" não conta o motivo `cadastro_terceiro`. No painel "Ações paradas antes de sair, por
  motivo", as ações canceladas pela quarentena aparecem numa série só, com o motivo "o servidor entrou em quarentena
  (mudou, parou); nada seguiu": as da quarentena do mudou-parou, as da quarentena da sondagem (D29) e as que a
  porta de saída cancela por isso. Cada uma também fica no extrato da pessoa. O contador
  `quarentenas_pela_sondagem` conta as quarentenas, não as ações. Prova: `tests/integracao/test_grafana.py`,
  `tests/integracao/test_rotinas_terceiros.py::test_quarentena_do_mudou_parou_cancela_o_que_espera_e_conta` e
  `tests/integracao/test_g6ar_sondagem_paradas.py::test_sondagem_que_poe_em_quarentena_conta_as_acoes_paradas`.

### Despachante: chave separada, autorização, revisão do servidor e sondagem

- **Chave do cofre separada para terceiros.** A chave do cofre (Transit) sai da origem do sistema: terceiros usam a
  chave só deles. O despachante recusa a partida sem a chave de terceiros, ou com ela sem a derivação por contexto
  (`derived`). A origem de todos os sistemas é lida na partida; um sistema novo é lido por uma conexão própria, fora
  do grupo de conexões (pool) do despachante, numa busca só por sistema. A busca da origem que falha (rede, prazo,
  banco) é recusada sem guardar nada: na leitura, `chave_indisponivel`; na ação, "não saiu" com "não deu para abrir a
  chave no cofre". Para a conexão própria, o código usa um atributo interno do pool (`Pool._creator`), e a partida
  recusa se ele sumir numa versão nova da biblioteca; tirar isso pediria mudar a criação do banco, sem ganho de
  segurança agora (decisão do coordenador). Prova: `tests/integracao/test_chave_separada.py` e
  `tests/unidade/test_cifra_origem.py`.
- **Leitura com o cofre que não abre a chave: recusa legível (vale para todos).** Na leitura, quando o cofre não
  abre a chave do sistema (fora do ar, trancado, com o acesso negado ou com a chave de cifra indisponível), ou
  quando não dá para saber a origem do sistema, a leitura é recusada com o motivo `chave_indisponivel` e a frase
  "Não deu para abrir a chave deste sistema no cofre agora. Nada foi enviado. Tente de novo em instantes." Vale para
  a chave por pessoa e para a compartilhada. Antes, era erro interno. Nada sai ao sistema. Prova:
  `tests/integracao/test_estranhos_leitura.py::test_cofre_fora_na_chave_vira_recusa_legivel_sem_nada_enviado`.
- **Pedido do sistema no meio da leitura: linha própria no extrato (vale para todos).** Quando o sistema pede algo
  no meio de uma leitura, a linha do extrato é `leitura_sistema_pediu_dados` (por um programa,
  `programa_leitura_sistema_pediu_dados`), com as categorias do nosso vocabulário, no lugar de `leitura_feita`. O
  aplicativo recebe o erro legível do MCP-CLARO, e o resultado que vem depois de um pedido pelo fio antigo é
  descartado (D12). O contador `pedidos_do_sistema_barrados` soma, com `onde=leitura`. Prova:
  `tests/integracao/test_pedidos_do_servidor.py::test_pedido_do_sistema_vira_erro_nosso_com_um_post_so`.
- **Autorização de terceiro.** Num terceiro, a autorização só é configurada uma vez. Com a chave da Fase 3
  desligada, só pessoa de teste autoriza (`terceiros_desligados`); a tela diz à pessoa "Servidores de terceiros
  ainda estão desligados neste MCP-CLARO. Só pessoas de teste os autorizam. Nada foi guardado." A troca do código
  e a renovação conferem o endereço de troca de chave (https e IP público) e gravam `sala_endereco_recusado`
  quando ele não serve. Prova: `tests/integracao/test_g3a_autorizacao_terceiros.py`.
- **Endereço de volta só de cada servidor de terceiros.** Num servidor de terceiros, a volta da página de
  autorização chega por um endereço só dele, `/telas/autorizar/<número do servidor>/volta`, e a entrada diz ao
  despachante por qual endereço ela chegou. Os sistemas próprios continuam com `/telas/autorizar/volta`, o endereço
  já cadastrado nos servidores de autorização desde a Fase 2. O despachante recusa, com o motivo
  `estado_de_outro_sistema`, a volta que chega pelo endereço de outro sistema e a de um terceiro que chega pela volta
  dos próprios, antes de abrir o segredo do cliente, de decifrar o verificador e de trocar o código. Nada é enviado a
  ninguém nem guardado: o extrato ganha a linha `autorizacao_recusada`, e a pessoa lê "Este pedido de autorização não
  é deste sistema. Nada foi guardado. Comece de novo por aqui." O começo da autorização de um terceiro também recusa
  um endereço de volta que não seja o dele. Por quê: com um endereço de volta só para todos os sistemas, um terceiro
  mal-intencionado podia mandar a pessoa à página de autorização de outro sistema, com o pedido dele; o código desse
  outro sistema voltava pelo endereço comum e ia, com o verificador, ao endereço de troca do terceiro, que ficava
  com a chave da pessoa no outro sistema (a mistura de servidores de autorização). A defesa supõe que o servidor de
  autorização de cada sistema só aceita o endereço de volta que tem cadastrado (RFC 6749, 3.1.2). Ela não cobre a
  mistura entre dois sistemas próprios, que dividem a mesma volta. O `iss` na volta (RFC 9207) ainda não é
  conferido. Prova: `tests/integracao/test_r6a_mistura_autorizacao.py`.
- **Prazo total da troca de chave (OAuth) e espera curta da renovação (vale para todos).** A troca de chave tem um
  prazo total de 15 s: abrir a conexão, mandar o pedido e ler a resposta inteira. Vale para a troca do código, na
  autorização, e para a renovação, de próprio e de terceiro. Antes, o prazo de leitura de 10 s valia para cada pedaço
  lido, e um endereço de troca que mandasse um byte de cada vez segurava a renovação por horas, com a porta de saída
  parada para todos os sistemas. Esgotado o prazo, vale o que já valia para o servidor de autorização que não
  responde: na renovação, a chave atual, se ainda vale; senão, a ação fica "não saiu" ("não deu para renovar a
  chave") e a leitura é recusada; na autorização, a recusa `sistema_indisponivel`, sem nada guardado. A renovação
  espera no máximo 3 s pela vez, quando outra renovação da mesma pessoa no mesmo sistema já está em curso. Sem a vez,
  ela desiste sem segurar conexão do banco e sem nada enviado. Depois, lê de novo a chave atual e a usa se ela ainda
  não venceu, mesmo no último minuto, quando já seria renovada; senão, a ação fica "não saiu" ("não deu para renovar
  a chave") e a leitura é recusada. O risco que continua está em "Riscos aceitos da Fase 3", abaixo. Prova:
  `tests/integracao/test_r6a_renovacao_prazo.py`, com
  `::test_sem_a_trava_a_renovacao_usa_a_chave_atual_se_ela_ainda_vale`, e `tests/unidade/test_r6a_troca_prazo.py`.
- **Conclusão da revisão do servidor.** Exige a chave de leitura das declarações (A5), lê o servidor de novo e recusa
  se a situação muda no meio (por exemplo, uma quarentena que entrou entre a leitura e a trava). Recusa (400) texto
  que o banco não guarda. Publicar uma lista vazia é aceito: tira da quarentena sem nada na vitrine, e nada chega às
  pessoas (decisão do coordenador). Com a sala de visitas fora ou cheia, a recusa da conclusão diz que o servidor
  não entregou as declarações (motivo `sem_resposta`), sem um motivo próprio para a sala (decisão do coordenador).
  Prova:
  `tests/integracao/test_revisao_servidor.py`.
- **Sondagem.** A sondagem grava sempre o desfecho e as versões anunciadas. Um terceiro ativo e revisado recusado
  por versão vai à quarentena com motivo próprio (D29). Em quarentena, a sondagem, com ou sem veredito, mantém o
  motivo da quarentena. A sondagem decide a quarentena pela situação e pela revisão lidas sob a trava exclusiva do
  sistema, na transação da gravação, e compara a revisão lida ali. A ordem das travas é a da conclusão do ADMIN:
  primeiro a do sistema, depois a linha. Uma conclusão do ADMIN no meio da sondagem não escapa da quarentena, e as
  duas não se travam uma à outra. A contagem de versão antiga só entra depois de gravada, com o motivo `cadastro` ou
  `cadastro_terceiro`. Sistema desconhecido: 404 (antes, 500). Prova: `tests/integracao/test_sondagem_fase3.py`.
- **Rotas internas não recebem texto que o banco não guarda.** Nenhuma rota interna do despachante (`/interno/*`,
  inclusive as de autorização) aceita texto que o banco não guarda. Um texto do corpo com o caractere nulo ou com um
  pedaço de par UTF-16 sozinho é pedido inválido (400 `pedido_invalido`), recusado antes de a rota rodar, e nada
  muda. Também é pedido inválido um `trace_id`, uma `chave_sondagem` ou um `quem` que não seja texto. Os argumentos
  de uma leitura ficam de fora: vão ao sistema, não ao banco. Efeito novo: um código de autorização com caractere
  nulo na volta do provedor era 403 e gravava a linha `autorizacao_recusada`; agora é 400 e não grava linha. Guardar
  uma chave de um sistema que não existe é 404 (antes, 500). Prova: `tests/integracao/test_f4_rotas_internas.py`.

### Porta de saída com terceiros

O roteiro de quem cuida do dia a dia está em [Porta de saída](saida.md#ações-em-servidores-de-terceiros). As mudanças:

- **Reconferência de terceiros na saída.** A porta só envia ação de terceiro com a chave da Fase 3 ligada ou para
  pessoa de teste, e só com a ferramenta publicada na revisão vigente. Senão, a ação é cancelada com "servidores de
  terceiros desligados ou servidor sem revisao". Servidor em quarentena: cancelada com "o servidor entrou em
  quarentena (mudou, parou); nada seguiu". Prova: `tests/integracao/test_saida_terceiros.py`.
- **O envio sai sempre pela ligação de cada sistema.** Próprio pelo cliente fixado; terceiro pela sala. Sem a sala,
  nenhum pedido MCP vai ao terceiro: a ação fica "não saiu", com a linha `sala_fora`. Só a renovação da chave da
  pessoa, quando é preciso, sai do despachante direto para o endereço de troca de chave (OAuth) do terceiro,
  conferido antes (https e IP público), antes de o pedido ir à sala.
- **Pela sala, só a falha de conexão antes de enviar é "não saiu".** Um 500 ou um corpo fora do contrato é "não
  confirmada".
- **Só o último status 401 vence a chave por pessoa.** Um "recusado" sem status de recusa é "não confirmada".
- **O que o sistema pediu no meio da ação vai ao desfecho só com o nosso vocabulário.** Um resultado normal com
  pedido barrado continua "enviada". A linha do fim da ação leva as categorias em `dados.pedidos`: sempre em
  `acao_enviada` e `acao_sistema_pediu_dados`, e em `acao_nao_confirmada` e `acao_nao_saiu` quando houve pedido
  barrado.
- **O `_meta` guardado leva só a conta de partes retiradas.**
- **Resposta funda demais.** Pelo cliente local (sistemas próprios), a resposta com mais de 199 níveis, ou que o
  kit não escreve, deixa a ação "enviada" sem resultado. Antes, derrubava o turno da porta, e a ação ficava "não
  confirmada" pela retomada. Pela sala de visitas, o caminho de um terceiro, uma resposta assim volta como dúvida, e
  a ação fica "não confirmada" (decisão do coordenador; veja "Riscos aceitos da Fase 3").
- **Argumentos fundos demais.** Argumentos com mais de 255 níveis não saem por nenhum caminho: "não saiu", sem abrir
  a chave. Pela sala de visitas, argumentos com mais de uns 200 níveis também não chegam ao terceiro: "não saiu", com
  "o pedido não cabe no contrato da sala de visitas" (a chave já foi aberta). Com 256 níveis ou mais, o cliente da
  sala nem escreve o pedido: "não saiu", sem abrir conexão. Um teto de 200 níveis na porta, antes de abrir a chave,
  ficou de fora por decisão do coordenador (o estado já é "não saiu").
- **Linhas `sala_*` no extrato da ação.** Num terceiro, o que a sala barrou (`sala_fora`, `sala_endereco_recusado` e
  `sala_desvio_recusado`) vai ao extrato da pessoa, com o protocolo, e soma o contador do mesmo nome
  (`onde=acao`).

### Riscos aceitos da Fase 3

Riscos que ficam, decididos pelo dono ou pelo coordenador. O modelo de ameaças
([modelo-de-ameacas.md](modelo-de-ameacas.md)) diz o que cada um quer dizer para a segurança.

- **A sala sobe mesmo sem Landlock (decisão do dono).** Num núcleo sem Landlock (antigo ou com ele desligado), a
  sala não recusa a partida: o log de partida diz `landlock=0`, e cada filho fica com o aviso de isolado, a proteção
  contra depuração (`PR_SET_DUMPABLE`) e os canos por soquete. O que fica: um filho tomado ainda pode matar outros
  filhos, e essas chamadas viram dúvida. Se o núcleo também deixar um processo depurar o irmão (o Yama desligado),
  ele pode, na janela de partida de um filho novo, até o isolamento (cerca de 0,8 s de tempo de relógio, numa
  medida da etapa 3), prender-se a ele ou abrir a memória dele e ler o pedido depois. Com o Landlock 1 a 5, o sinal
  entre filhos ainda passa (negação de serviço). Se o dono quiser, a partida em piloto e produção pode passar a
  recusar núcleo sem Landlock. A versão do Landlock vem do núcleo do Linux de cada máquina, não do nome da
  distribuição. Conferido em 21/11/2026, só na máquina de testes da Fase 3: o sistema é o Ubuntu 24.04.5 (x86_64),
  mas o núcleo é o 6.18.44-fc-v726 (`uname -r`), do próprio ambiente de testes, e não um núcleo do Ubuntu
  (`/proc/version`). Esse núcleo anuncia o Landlock 7 (`filho.versao_landlock()`, que pergunta ao núcleo), e o Yama
  não está ligado nele (não existe `/proc/sys/kernel/yama`). Não foram conferidos o runner ubuntu-24.04 do CI nem as
  máquinas de piloto e produção: o log de partida da sala (`landlock=N`) diz a versão em cada uma.
- **"Não saiu" forjável por um terceiro.** Todo status ou código que vira "não saiu" pode ser forjado por um
  terceiro que executou a ação e respondeu assim: 401, 403 e 3xx no último status, e os códigos -32601, -32602,
  -32600, -32700 e -32021. Nesse caso, um pedido igual sai de novo, só para esse mesmo terceiro, sem alcance a outros
  sistemas. Vale igual para sistema próprio desde a Fase 2. Sem mudança de código. Na sala, um filho tomado pelo
  terceiro pode, depois de chamar o terceiro, escrever qualquer resposta "nada enviado" do contrato, também a de sala
  cheia (`sala_sem_vaga`): a sala devolve o que o filho escreveu sem ler, e a ação fica "não saiu", com o mesmo
  alcance.
- **Sala cheia em toda conferência.** Se as 32 vagas da sala ficarem ocupadas por mais de 2 s na hora de cada
  rodada do mudou-parou, nenhuma conferência de terceiro é feita, e um servidor que mudou só é visto quando a sala
  esvazia. Isso vale também para o terceiro lento que ocupa as vagas: enquanto a sala estiver cheia, a mudança dele
  não é vista; a vitrine continua só com o que foi revisado. O contador `sala_sem_vaga` (com `onde=declaracoes`)
  mostra; a equipe que opera o gateway confere. Só a sala cheia dita pelo processo pai não conta: o mesmo corpo
  escrito por um filho tomado conta como o servidor que não entregou (decisão do coordenador, na revisão cruzada).
- **Resposta funda demais pelos dois caminhos.** Pela sala, a resposta funda demais volta como dúvida, e a ação fica
  "não confirmada". Pelo cliente local (sistemas próprios), a ação fica "enviada" sem resultado. Os dois são do lado
  seguro.
- **Janela de DNS.** Entre a conferência do nome de um terceiro e a conexão, o DNS pode mudar. O https com
  certificado público dificulta o desvio para um endereço interno; na sala, a rede isolada também. A troca de chave
  do OAuth, que sai do despachante e não da sala, só conta com o https e a conferência do IP.
- **Troca de chave lenta ocupa a porta de saída.** Cada ação de um terceiro com o endereço de troca de chave lento
  ainda pode ocupar a porta única de saída por até 15 s da troca, mais a espera da vez (até 3 s) e a conferência do
  endereço de troca (até 3 s). Nesse tempo, nenhuma outra ação sai, de nenhum sistema. Um prazo a mais na porta de
  saída ficou de fora: o prazo total da troca e a espera curta da renovação bastam por agora (decisão do coordenador,
  na revisão cruzada). Vale igual para sistema próprio. Cada renovação também segura uma conexão do banco do
  despachante enquanto troca a chave. Renovações de pessoas diferentes no mesmo terceiro lento não dividem a vez, e
  cada uma pode segurar a sua conexão por até cerca de 15 s.
- **Fichas do alerta e o teto por aplicativo (B11).** O teto de fichas por hora (`CONFIRM_CAP_PER_HOUR`) é por
  aplicativo, somando todas as pessoas, e a pausa vale para todas. Com o alerta, ações que iam por limite viram
  fichas: poucas pessoas com o alerta ligado podem pausar um aplicativo muito usado para todo mundo. Medir no piloto
  (`medicoes.v_acoes_pedidas`, rota `tres_sinais`). O dono pode trocar para um teto por pessoa e aplicativo.
- **Sem teto para a soma dos `enum` do esquema inteiro.** A revisão limita cada `enum` a 1.000 valores, mas não a
  soma: um `anyOf` de 40 `enum` de 1.000 valores cabe. Quem segura esse caso é o orçamento de passos (decisão do
  coordenador).
- **O orçamento conta além do pedido literal.** A conta conta também um passo por item percorrido, por erro, por
  letra da mensagem e por par comparado em `enum` e `const`, e troca `false` por `{"not": {}}`. Cada pedaço fecha um
  caso medido na etapa 3 que passava na revisão e nos tetos e levava até 28 s de tempo de relógio dentro dos 50
  mil passos (seis casos, de 0,6 a 28 s). Todas essas medidas foram feitas com o `tracemalloc` ligado, uma
  ferramenta do Python que acompanha a memória e deixa a conta mais lenta. Foi decisão do coordenador. O teto de
  100 dígitos também é uma escolha da equipe, aceita.
- **Mudança que demora a ser vista.** Até a rodada seguinte do mudou-parou (`TOOL_DRIFT_CHECK_SECONDS`, 900 s no
  início), um terceiro que mudou continua sendo chamado. Um servidor pode esconder a mudança (falhar só na listagem,
  mostrar definições diferentes por chave, mudar o comportamento sem mudar o que declara). Atenuante: o texto dele
  chega ao aplicativo só emoldurado.
- **Listagem de mais de 50 páginas.** A listagem para em 50 páginas, também dentro da sala: um servidor maior parece
  ter ferramentas sumidas e vai à quarentena (quarentena falsa, para o lado seguro).
- **O downgrade da 0006 continua levantando erro, como em todas as migrações.**

## Ainda em aberto

Estes pontos não travam o código e ficam como parâmetro ou roteiro até a resposta:

- sistema real e ações da Fase 2; servidores de terceiros da Fase 3 (os primeiros reais); equipes da Fase 4;
- ligar a chave da Fase 3 (D6): decisão do dono, depois das provas que dependem de gente e de ambiente real
  ([andamento](andamento.md#fase-3-servidores-de-terceiros)) e da decisão sobre o teste de carga (item seguinte);
- o teste de carga com o `locust`, que o plano pede antes de abrir fase, não existe no repositório: não houve teste de
  carga nas Fases 1 e 2 nem nesta. Ligar a chave da Fase 3 (D6) depende dessa decisão do dono;
- prazo e tamanho da equipe;
- revisão de LGPD;
- servidor de e-mail, destinos de webhook, local da segunda máquina e do repositório de backup;
- certificados do portão em arquivos, fora do PostgreSQL, com cópia cifrada no backup.
