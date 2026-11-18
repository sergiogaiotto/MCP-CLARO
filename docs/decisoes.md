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

## Ainda em aberto

Estes pontos não travam o código e ficam como parâmetro ou roteiro até a resposta:

- sistema real e ações da Fase 2; servidores de terceiros da Fase 3; equipes da Fase 4;
- prazo e tamanho da equipe;
- revisão de LGPD;
- servidor de e-mail, destinos de webhook, local da segunda máquina e do repositório de backup;
- certificados do portão em arquivos, fora do PostgreSQL, com cópia cifrada no backup.
