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

## Ainda em aberto

Estes pontos não travam o código e ficam como parâmetro ou roteiro até a resposta:

- sistema real e ações da Fase 2; servidores de terceiros da Fase 3; equipes da Fase 4;
- prazo e tamanho da equipe;
- revisão de LGPD;
- servidor de e-mail, destinos de webhook, local da segunda máquina e do repositório de backup;
- certificados do portão em arquivos, fora do PostgreSQL, com cópia cifrada no backup.
