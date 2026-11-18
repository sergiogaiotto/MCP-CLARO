# Plano de implementação do MCP-CLARO

> Plano de implementacao, de 18/11/2026. As decisoes finais estao em [decisoes.md](decisoes.md); o andamento de cada etapa, em [andamento.md](andamento.md).


Nov 18, 2026 · 

## Resumo do plano

Este plano diz o que será construído, em que ordem e como cada passo será provado. Ele segue a proposta e as decisões que você deu hoje.

Vamos construir primeiro a base e a Fase 1, só de leitura. Sem Apigee e só com a versão 2026-07-28 do MCP. Na frente fica o portão próprio, o Caddy. Antes da Fase 1 vêm duas etapas: a fundação do código e "Provar a base", que escolhe as peças por teste.

A versão única vale dos dois lados. Aplicativo ou sistema que só fala a geração antiga recebe uma recusa legível e entra numa contagem. Isso simplifica o produto, mas deve deixar de fora, no começo, parte dos aplicativos e dos sistemas.

O núcleo será código próprio em Python, sobre o kit oficial do MCP, versão 2.3.0. Tudo roda em Docker, e o único banco é o PostgreSQL. O FastMCP entra só em sistemas de exemplo e testes. O Langfuse fica fora do produto.

Saberemos que funciona por provas, listadas em cada etapa. A Fase 1 só fica pronta quando dois aplicativos de IA lerem um sistema seu, cada pessoa vendo só o seu. Antes, ela passa no teste oficial do padrão e nas tentativas com crachá falso ou versão antiga. Passa também no ensaio do dublê mal-intencionado, um aplicativo falso que tenta burlar as regras. Da Fase 2 em diante, entram os testes de resposta perdida de propósito.

Para programas que rodam sozinhos, sugerimos parametrizar em duas camadas: padrões da instalação e regras de cada programa. O responsável humano não é parâmetro: é um cadastro com nome, obrigatório.

As decisões 1 a 6 seguem como premissas "a confirmar". Para a Fase 1 chegar ao piloto, faltam três respostas suas: as decisões 4, 5 e 6.

## Decisões tomadas

Oito decisões foram tomadas hoje, da 7 à 14. As seis primeiras da proposta seguem como premissas até você confirmar.

| Decisão | O que foi decidido | Efeito no plano |
| --- | --- | --- |
| 7. Apigee | Fica para depois. Implementar e testar só com o portão próprio | Nenhum código de Apigee agora. Ficam preparados o parâmetro do portão, o certificado na porta interna e as respostas inteiras |
| 8. Versão do MCP | Só a versão mais atual, a de 28 de julho de 2026 | Vale para aplicativos e para sistemas conectados. Quem fala só a versão antiga recebe uma recusa legível e entra em uma contagem |
| 9. Programas que rodam sozinhos | Você pediu a nossa sugestão | Sim, em duas camadas. Programas entram na Fase 2, só lendo; ações deles ligam depois, por decisão sua. Detalhes em "Respostas às perguntas" |
| 10. Ordem das fases 3 e 4 | Servidores de terceiros antes dos selos | Um ponto comum ganha valor ao ligar servidores comuns de terceiros. Os selos dependem de outras equipes adaptarem seus sistemas |
| 11. Valores iniciais | Seguir a recomendação | Cada valor vira parâmetro, começa conservador e é ajustado no piloto |
| 12. Avisos e segunda máquina | Seguir a recomendação | Aviso por e-mail e por webhook configurável, além da caixa de pendências. Vigia em outro provedor ou outra rede, com acessos separados |
| 13. Extensão Tasks | Seguir a recomendação | Desligada por uma chave própria. A consulta pelo número de protocolo é o caminho principal |
| 14. Grafana | Fica | Usado sem alteração. Parecer sobre a licença AGPL só se o produto for oferecido a clientes ou distribuído |

### A consequência da decisão 8

A decisão 8 muda a proposta, que previa falar versões antigas com os sistemas. Aceitar só a versão 2026-07-28 tem um custo de entrada. Gemini CLI e VS Code não têm registro público de falar essa versão e, hoje, seriam recusados. O Codex CLI tem a versão nova atrás de uma chave marcada "em desenvolvimento".

Do lado dos sistemas, o efeito é parecido. Servidores feitos com a linha 1 dos kits em TypeScript ou Rust não entram até migrar. O gateway recusa no cadastro, com mensagem legível, e conta.

Nem o kit oficial nem o FastMCP têm chave para recusar a geração antiga. Por isso o plano prevê um porteiro de versão próprio, pequeno, com testes de recusa. A próxima versão do padrão está prevista para 30 de março de 2027. Uma revisão de compatibilidade, antes dessa data, decide a migração.

### Premissas, a confirmar

| Decisão | O que o plano assume |
| --- | --- |
| 1. Para quem é o produto | Uso interno, com uma organização por instalação |
| 2. Ligação com os sistemas | Kit oficial do MCP em Python; FastMCP só em exemplos e testes; gateways prontos só como referência |
| 3. Provedor de login | Keycloak ou Logto, escolhido por teste na etapa "Provar a base" |
| 4. Aplicativos-piloto | Claude Code e um segundo aplicativo, de outra família, a escolher |
| 5. Primeiro sistema | Um sistema seu, só leitura e com chave por pessoa. Até lá, a equipe usa sistemas de exemplo próprios |
| 6. Peças | Caddy no portão e OpenBao no cofre. O modo de destravar o cofre fica por decidir e vira o parâmetro VAULT\_SEAL\_MODE: chave em arquivo nos testes; no piloto, sugerimos partes de chave guardadas por pessoas |

### Onde a pesquisa diverge da proposta

- A proposta dizia que, do lado dos sistemas, o gateway falaria também as gerações anteriores. A decisão 8 tira isso.
- A proposta citava só o Claude Code com registro público da versão nova. A pesquisa achou mais dois: Goose e OpenAI Agents SDK. Para testes, há o MCP Inspector em modo `modern`.
- A licença do FastMCP foi conferida no arquivo de licença: Apache-2.0. Isso fecha a pendência da proposta.
- A proposta guardava os painéis do Grafana no PostgreSQL. O plano guarda no PostgreSQL a configuração do Grafana; os painéis vêm de arquivos versionados.
- A proposta usava o OpenTelemetry Collector, um programa que repassa dados de operação, para enviar medições. O plano o usa só para rastros, o caminho de cada pedido, e para logs, as linhas de registro. As medições ficam no PostgreSQL.
- A proposta deixava o Temporal como opção para a porta de saída. O plano usa um laço próprio no PostgreSQL e deixa bibliotecas de fila fora do caminho das ações.
- A proposta deixava Cedar ou OPA como opção para o motor de regras. O plano faz as regras em código próprio, sobre tabelas no PostgreSQL.
- A proposta ensaiava o modo Apigee com um dublê de portão externo antes da chegada dele. Pela decisão 7, esse ensaio fica para quando a troca for marcada.
- A proposta previa avaliar o FastMCP para o núcleo em "Provar a base". A pesquisa achou conflitos com o desenho; agora ele começa fora e só entra por critério escrito.

## Respostas às perguntas

### FastMCP: recomendamos?

**Em parte: sim para sistemas de exemplo, servidores de teste e como receita para outras equipes; não no núcleo do gateway.**

Fatos conferidos:

- A versão mais recente é a 4.2.1, de 17/11/2026, uma correção de segurança ([PyPI](https://pypi.org/pypi/fastmcp/json)). Exige Python 3.11 ou mais novo.
- A licença é Apache-2.0, lida no [arquivo de licença](https://raw.githubusercontent.com/PrefectHQ/fastmcp/main/LICENSE).
- O "FastMCP-CLARO" é um [serviço hospedado da Prefect](https://github.com/PrefectHQ/fastmcp/blob/main/docs/deployment/fastmcp-gateway.mdx), não a biblioteca. Fica fora pelas regras do produto.
- Um servidor FastMCP atende sempre as duas gerações ([novidades da versão 4](https://github.com/PrefectHQ/fastmcp/blob/main/docs/getting-started/whats-new.mdx)). Não há chave documentada para atender só a 2026-07-28.
- O proxy repassa por padrão os cabeçalhos de entrada, inclusive o crachá, aos sistemas ([proxy](https://github.com/PrefectHQ/fastmcp/blob/main/docs/servers/providers/proxy.mdx)). O padrão MCP proíbe repassar o crachá do aplicativo.
- O proxy é um espelho. Manda cada chamada direto ao sistema e guarda a lista de ferramentas em cache por 300 segundos. Também repassa ao aplicativo pedidos de dados vindos do sistema.
- No [verificador de crachá JWT](https://github.com/PrefectHQ/fastmcp/blob/main/fastmcp_slim/fastmcp/server/auth/providers/jwt.py), a conferência que recusa crachá de outro condomínio vem desligada por padrão, sem aviso. No kit oficial ela também vem desligada, mas com aviso. Nos dois, o gateway a liga e a prova por teste. JWT é um formato de crachá assinado.
- As [tarefas](https://github.com/PrefectHQ/fastmcp/blob/main/docs/servers/tasks.mdx) ficam só em memória ou em Redis/Valkey. Isso fere a regra de só PostgreSQL.
- Houve seis alertas de segurança revisados em 2026: um crítico, dois altos e três moderados. Dois foram no proxy OAuth, e um no cliente OAuth de máquina para máquina. Em outro, o [GHSA-7xgj-8392-5q4r](https://raw.githubusercontent.com/github/advisory-database/main/advisories/github-reviewed/2026/10/GHSA-7xgj-8392-5q4r/GHSA-7xgj-8392-5q4r.json), uma ferramenta negada podia ser chamada pelo nome.
- Saíram 27 versões estáveis desde 31/08/2026, contando correções da linha 3.4. Algumas quebram código ([histórico](https://github.com/PrefectHQ/fastmcp/blob/main/docs/changelog.mdx)).
- Numa simulação de instalação, o kit oficial trouxe 28 pacotes, o FastMCP 62 e o FastMCP com tarefas 73 ([fastmcp-slim](https://pypi.org/pypi/fastmcp-slim/json)).
- A [política de segurança](https://github.com/PrefectHQ/fastmcp/blob/main/SECURITY.md) avisa que esconder uma ferramenta da lista não é barreira de segurança.

Por que não no núcleo, em três motivos:

1. O proxy dele manda cada chamada direto ao sistema. No nosso desenho, só o despachante envia, uma vez, a partir da fila.
2. Ele repassa o crachá do aplicativo e mostra a lista que o sistema declara, não a vitrine do cadastro revisado.
3. As tarefas dele pedem Redis, e o único banco é o PostgreSQL.

Por isso, o núcleo usa o kit oficial `mcp` 2.3.0 nas duas pontas, com o cliente fixado em 2026-07-28.

Onde o FastMCP 4.2.1 ou mais novo entra:

- Sistemas de exemplo, enquanto o primeiro sistema real não é escolhido.
- Servidores de teste que se comportam mal de propósito: mudam a definição, perdem a resposta, pedem senha ou ficam mudos.
- Um servidor de teste com Tasks, em memória, para ensaiar a chave `FEATURE_TASKS` sem tocar no produto.
- Uma receita curta para outras equipes: versão mínima, login obrigatório, audiência e emissor configurados, sem proxy OAuth.
- Um cliente de teste fixado em 2026-07-28, ou na geração antiga, para o ensaio do dublê.

Em todos os casos, ele fica em imagens de teste ou de terceiros, nunca na imagem do gateway.

Critério para entrar no núcleo depois, com as quatro condições juntas:

- o kit oficial não entrega algo que a Fase 1 precisa, e o FastMCP entrega;
- há chave documentada para atender só a 2026-07-28;
- não houve alerta alto ou crítico nos 90 dias anteriores;
- passa nos mesmos testes do núcleo: teste oficial, crachá de outro condomínio, ferramenta fora da vitrine e crachá não repassado.

### Langfuse: devemos usar?

**Não dentro do produto. Quem já usa o Langfuse pode ligar os rastros dele aos do gateway por padrões abertos.**

Fatos conferidos:

- A versão atual é a v4.82.0, de 17/11/2026 ([tag no repositório](https://github.com/langfuse/langfuse/tree/v4.82.0)).
- A v4 exige quatro armazenamentos: PostgreSQL, ClickHouse, Redis ou Valkey e um armazenamento compatível com S3. O [arquivo de Docker Compose](https://github.com/langfuse/langfuse/blob/v4.82.0/docker-compose.yml) sobe sete contêineres.
- Não há modo só com PostgreSQL. A última versão assim, a v2, [chegou ao fim da vida](https://github.com/langfuse/langfuse-docs/blob/main/content/self-hosting/upgrade/versioning.mdx) e não recebe correções.
- O núcleo é MIT. Guarda com prazo, máscara de dados no servidor e registro de auditoria exigem [chave paga](https://github.com/langfuse/langfuse-docs/blob/main/content/self-hosting/license-key.mdx).
- A [telemetria para fora](https://github.com/langfuse/langfuse-docs/blob/main/content/self-hosting/security/telemetry.mdx) vem ligada por padrão e pode ser desligada.
- Ele serve para rastrear chamadas a modelos, prompts e avaliações ([README](https://github.com/langfuse/langfuse/blob/v4.82.0/README.md)). O gateway não chama modelos: vê chamadas de ferramenta, que já ficam no extrato.
- Aceita OpenTelemetry só para rastros, por OTLP sobre HTTP. [Medições e logs ficam de fora](https://github.com/langfuse/langfuse-docs/blob/main/content/faq/all/opentelemetry-logs-metrics-span-links.mdx). OTLP é o formato de envio do OpenTelemetry.
- A [especificação 2026-07-28](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/index.mdx) reserva no `_meta` de cada pedido as chaves `traceparent`, `tracestate` e `baggage`. O `_meta` é um campo de dados extras do pedido.
- O Langfuse documenta esse caminho para [ligar rastros de cliente e servidor MCP](https://github.com/langfuse/langfuse-docs/blob/main/content/docs/observability/features/mcp-tracing.mdx).

Por que não. O Langfuse traria ClickHouse, Redis ou Valkey e S3, o que fere a regra do PostgreSQL único. Exportar argumentos e resultados criaria uma segunda cópia de dados pessoais fora do PostgreSQL.

O que se perde: dentro do produto, não haverá a vista em árvore do caminho inteiro de um pedido. Não conferimos se o Grafana monta essa vista lendo só o PostgreSQL. Quem precisar dela tem o caminho descrito logo abaixo, fora do produto.

Como a ligação funciona sem depender dele:

- Toda resposta leva o número de protocolo no `_meta`, com prefixo próprio. Quem busca o número no Langfuse acha o rastro; quem busca no extrato acha a operação.
- O gateway aceita o `traceparent` do aplicativo só para correlação. Nunca o usa para autorizar, para evitar repetição nem para montar a ficha.
- O gateway descarta `baggage` e `tracestate` recebidos, porque podem trazer dados pessoais ou internos.
- Se uma equipe quiser os rastros do gateway no Langfuse dela, aponta o Collector para lá. Os rastros saem sem conteúdo, e isso passa antes pela revisão de LGPD.

O que usamos no lugar:

- O gateway grava contagens e resumos no PostgreSQL, e o [Grafana 13.3.1](https://github.com/grafana/grafana/blob/v13.3.1/LICENSING.md) mostra painéis e [alertas de tendência](https://github.com/grafana/grafana/blob/v13.3.1/docs/sources/datasources/postgres/alerting/index.md).
- Os alertas do Grafana vão por webhook interno às rotinas do gateway, que mandam o e-mail e o webhook assinado.
- O alarme de gateway mudo fica no vigia, em outra máquina.
- O [OpenTelemetry Collector 0.165.0](https://github.com/open-telemetry/opentelemetry-collector-releases/blob/v0.165.0/distributions/otelcol/manifest.yaml) fica desligado e só liga para mandar rastros e logs a uma ferramenta que a empresa já tenha.

Os detalhes ficam em "O que vai rodar", "Parâmetros" e "Dados, segurança e operação".

### Programas que rodam sozinhos: nossa sugestão

**Sim, parametrizar serve, em duas camadas: padrões da instalação e regras de cada programa, nunca mais frouxas que o padrão. Nossa sugestão: cada programa é um aplicativo próprio, com responsável com nome, e começa só lendo.**

Programas entram na Fase 2, só lendo. Ações de programas ligam depois das provas da Fase 2, por decisão sua, registrada.

Fatos conferidos:

- Existe a extensão oficial [OAuth Client Credentials](https://raw.githubusercontent.com/modelcontextprotocol/ext-auth/main/specification/stable/oauth-client-credentials/2026-07-28/oauth-client-credentials.mdx), estável para a 2026-07-28. Ela cobre programas sem pessoa, com cadastro prévio e JWT assinado recomendado.
- A extensão [Enterprise-Managed Authorization](https://raw.githubusercontent.com/modelcontextprotocol/ext-auth/main/specification/stable/enterprise-managed-authorization/2026-07-28/enterprise-managed-authorization.mdx) também é estável, mas parte de uma pessoa. Não serve para programa sem pessoa.
- No Keycloak, todo cliente tem uma [conta de serviço](https://raw.githubusercontent.com/keycloak/keycloak/main/docs/documentation/server_admin/topics/clients/oidc/proc-using-a-service-account.adoc). Com JWT assinado, o Keycloak guarda só a chave pública.
- No Logto, aplicativos [máquina a máquina](https://raw.githubusercontent.com/logto-io/docs/master/docs/authorization/global-api-resources.mdx) recebem crachá preso à audiência. Isso não está na lista de recursos pagos.
- O kit oficial já traz o cliente para segredo e para JWT assinado ([clientes OAuth](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/docs/client/oauth-clients.md)).
- O [OWASP Non-Human Identities Top 10](https://raw.githubusercontent.com/OWASP/www-project-non-human-identities-top-10/main/index.md) lista os riscos: identidade esquecida ativa, segredo vazado, poder demais, segredo de vida longa e reuso.

Como funciona:

1. O ADMIN cadastra o programa como um aplicativo próprio, pré-cadastrado no provedor de login, com identificador só dele.
2. O cadastro pede um responsável com nome e um substituto. Sem eles, o programa não é salvo.
3. O programa entra com JWT assinado e recebe um crachá de poucos minutos, preso ao endereço do gateway.
4. Nos sistemas, ele usa uma chave própria, guardada no cofre. Se o sistema só tem chave por pessoa, o programa não entra nesse sistema.
5. Ele começa só lendo. Ações ficam desligadas até a Fase 2 passar nas provas.
6. Ação fora do limite vai para as pendências do responsável, com aviso por e-mail ou webhook. Sem resposta no prazo, vence e nada segue.
7. O programa acompanha cada ação pelo número de protocolo. O extrato mostra "programa X, responsável Y".

Camada 1, parâmetros da instalação, com valores em "Parâmetros":

- formas de login aceitas e validade do crachá;
- prazo de troca da chave e aviso antes de vencer;
- ações ligadas ou não e prazo das pendências;
- revisão periódica pelo responsável e pausa por falta de uso;
- limite de pedidos por programa.

Camada 2, cadastro de cada programa no PostgreSQL:

- responsável e substituto;
- ordem de serviço, com sistemas e ferramentas;
- horários, dias e origem de rede opcional;
- limites aprovados pelo responsável com passkey;
- data de fim e situação.

O que não é parametrizável, por regra fixa no código:

- O responsável humano é um cadastro com nome, não uma variável. Não existe valor-padrão para ele.
- Se o responsável sai ou é desativado, o programa pausa.
- Nenhum programa usa a chave de uma pessoa, e nenhuma pessoa usa a chave de um programa.
- O programa não aprova nada. Confirmação e limite vêm sempre do responsável, na tela do gateway.
- A regra de um programa nunca afrouxa o padrão da instalação.
- Não há cadastro automático de programas. Cada um é pré-cadastrado pelo ADMIN.

A afirmação de identidade empresarial fica fora agora, com a chave `AUTH_ENTERPRISE_ASSERTION_ENABLED=falso`. Ela pode ligar quando houver um provedor de login da organização.

## O que vai rodar

Na máquina principal rodam, em Docker, o portão, o gateway em três papéis, o cofre, o banco, o login, o Grafana e o backup. Na segunda máquina roda o vigia, com banco próprio.

Só dois papéis do gateway saem para a internet: o despachante, que fala com os sistemas, e as rotinas, que mandam avisos.

> Desenho: peças em Docker · máquina principal, vigia e quem fica fora (versao com desenhos no documento compartilhado).

Leia de cima para baixo. O pedido entra pelo portão e passa pela entrada; só o despachante fala com os sistemas e abre as chaves. As peças guardam dados no PostgreSQL, e o vigia confere tudo de fora, em outra máquina.

### Máquina principal

- **Portão (Caddy).** Atende a internet com TLS automático. Aplica o limite grosso de pedidos por endereço de origem. Não repete pedidos, e não decide nada pelos cabeçalhos MCP.
- **Gateway, papel `entrada`.** Recebe os pedidos MCP e as telas. Aplica o porteiro de versão, confere o crachá, monta a vitrine e decide pelas regras.
- **Gateway, papel `despachante`.** É o único que fala com os sistemas e o único que abre as chaves das pessoas. Atende leituras na hora, depois de reconferi-las, e envia ações só a partir da fila.
- **Gateway, papel `rotinas`.** Roda a ronda das portas, vencimentos, avisos, fechamento e resumos de medições. Recebe os alertas do Grafana e os manda pela fila de avisos.
- **Cofre (OpenBao).** Guarda as chaves de cifra. As chaves das pessoas ficam cifradas nas tabelas do gateway.
- **PostgreSQL.** O único banco. Tem bancos e usuários separados para gateway, cofre, provedor de login e Grafana. Roda numa imagem própria sobre a oficial, com o pgBackRest dentro.
- **Provedor de login (Keycloak ou Logto).** Emite os crachás. Pode, por configuração, ligar-se à conta que a organização já usa.
- **Grafana.** Painéis e alertas de tendência. Lê só visões de medições, com usuário só de leitura. Manda os alertas às rotinas, por webhook interno.
- **Backup (pgBackRest).** Cópia contínua do PostgreSQL, com recuperação para um ponto no tempo. O banco manda a ele cada WAL, o registro contínuo das mudanças. A cópia vai a um repositório fora das duas máquinas.
- **Collector (opcional).** Desligado por padrão. Liga só para mandar rastros e logs para fora.

### Segunda máquina: vigia de fora

- **Vigia.** O mesmo pacote Python, no papel `vigia`. Repete a ronda pelo endereço público, avisa se o gateway ficar mudo e guarda o número de fechamento.
- **PostgreSQL do vigia.** Banco só do vigia. Ele só acrescenta linhas.

A segunda máquina fica em outro provedor ou outra rede, com acessos separados dos do gateway (decisão 12). O vigia avisa por SMTP e webhook próprios, sem depender do Grafana.

### Redes do Docker

- `borda`: portão e provedor de login. Tem saída para a internet, para os certificados do portão e, se ligada, a conta da organização.
- `interna`: portão, entrada, despachante, rotinas, cofre, banco, provedor de login, Grafana, backup e, se ligado, o Collector. Sem saída para a internet.
- `saida`: só despachante, rotinas, backup e, se ligado, o Collector, com saída para a internet.

O Grafana não sai para a internet. Ele manda os alertas por webhook interno às rotinas, que os enviam pela fila de avisos, com assinatura e novas tentativas. O provedor de login fala com o banco pela `interna`, e o emissor dos crachás continua sendo o endereço público.

### Quem fala com quem

As portas internas são valores iniciais, ajustáveis por parâmetro. mTLS quer dizer certificado dos dois lados da conexão.

| De | Para | Porta | Credencial |
| --- | --- | --- | --- |
| Aplicativo de IA | Portão | 443, HTTPS | Crachá do provedor de login, preso ao endereço do gateway |
| Pessoa, no navegador | Portão, depois telas | 443, HTTPS | Sessão de login das telas; passkey nas ações de maior risco |
| Portão | Entrada | 8443, interna | mTLS: certificado de cliente do portão |
| Portão | Provedor de login | Interna, do provedor | Nenhuma; o login é da própria pessoa |
| Entrada | Provedor de login | Interna, do provedor | Nenhuma; busca as chaves públicas dos crachás (JWKS) |
| Entrada | Despachante | 9443, interna | mTLS: certificado da entrada. Serve leituras e a entrega do código de autorização. O despachante só atende ferramenta "lê" do cadastro revisado e da ordem de serviço; o resto é recusado e vai ao extrato |
| Entrada, despachante, rotinas | PostgreSQL | 5432, TLS | Usuário próprio de cada papel |
| Despachante | Cofre | 8200, TLS | AppRole do despachante: login de máquina com token curto |
| Entrada | Cofre | 8200, TLS | AppRole da entrada: só lê a chave do estado das confirmações |
| Cofre | PostgreSQL | 5432, TLS | Usuário do banco do cofre |
| Provedor de login | PostgreSQL | 5432, TLS | Usuário do banco do provedor |
| Despachante | Sistemas conectados | 443, HTTPS | Chave do cofre, por pessoa ou do programa; só o endereço cadastrado |
| Rotinas | SMTP e webhooks | 587 e 443 | Senha do SMTP e segredo de assinatura, em segredos do Docker |
| Grafana | PostgreSQL | 5432, TLS | Usuário `grafana` no banco próprio; `grafana_leitor`, só SELECT em visões de medições |
| Grafana | Rotinas | 9444, interna | Senha própria do webhook interno, em segredo do Docker |
| Banco e backup | Um ao outro | 8432, TLS do pgBackRest | Certificados de cliente dos dois lados. O `archive_command` do banco manda cada WAL ao backup |
| Entrada, despachante, rotinas | Collector, se ligado | 4318, OTLP sobre HTTP | Nenhuma; só na rede interna |
| Backup | Repositório de backup | Do provedor | Credencial só do backup; cópia cifrada |
| Vigia | Portão | 443, HTTPS | Crachás de teste da ronda e crachá próprio de programa |
| Vigia | SMTP e webhooks | 587 e 443 | Credenciais do vigia, separadas das do gateway |

JWKS é a lista de chaves públicas com que o provedor assina os crachás. AppRole é o login de máquina do OpenBao.

### Peças, imagens e onde guardam dados

| Peça | Imagem ou código | Para que serve | Guarda dados em |
| --- | --- | --- | --- |
| Portão | `caddy:2.11.9-builder` com o módulo `caddy-ratelimit` fixado por commit | TLS, limite grosso, mTLS para a entrada | Arquivos no volume `/data` (certificados), fora do PostgreSQL; exceção a confirmar, com cópia cifrada no backup |
| Entrada | Imagem própria `mcp-gateway`, papel `entrada` | Porteiro de versão, crachá, vitrine, regras, telas | PostgreSQL |
| Despachante | Mesma imagem, papel `despachante` | Leituras e envio de ações aos sistemas | PostgreSQL |
| Rotinas | Mesma imagem, papel `rotinas` | Ronda, vencimentos, avisos, fechamento, resumos, alertas do Grafana | PostgreSQL |
| Cofre | `openbao/openbao:2.7.2` | Chaves de cifra (Transit) e chaves de operação | PostgreSQL, banco próprio |
| Banco | Imagem própria sobre `postgres:18.7`, com pgBackRest 2.60.1 | Único banco do produto | Volume do Docker, com backup |
| Provedor de login | `quay.io/keycloak/keycloak:26.8.2` ou `ghcr.io/logto-io/logto:1.46.0` | Crachás e login das pessoas | PostgreSQL, banco próprio |
| Painéis | `grafana/grafana:13.3.1`, fixada por digest | Painéis e alertas de tendência | PostgreSQL, banco próprio; painéis em arquivos versionados |
| Backup | Imagem própria com o mesmo pgBackRest 2.60.1, do repositório apt do PGDG | Cópia contínua e restauração | Repositório fora das duas máquinas |
| Collector | `otelcol` 0.165.0, desligado | Rastros e logs para fora | Nada |
| Vigia | Imagem `mcp-gateway`, papel `vigia` | Ronda de fora, alarme de silêncio, número de fechamento | PostgreSQL do vigia |
| Banco do vigia | `postgres:18.7` | Banco só do vigia | Volume na segunda máquina |

Digest é a impressão digital de uma imagem Docker; fixar por ela evita trocar a imagem sem saber.

## Como o código se organiza

Um repositório, um pacote Python, `mcp_gateway`, e uma imagem Docker com quatro papéis. Os papéis são `entrada`, `despachante`, `rotinas` e `vigia`, escolhidos pelo comando de partida.

```
mcp-gateway/
  pyproject.toml, uv.lock
  src/mcp_gateway/
    config/      parâmetros e tabela de política
    entrada/     porteiro de versão, crachá, servidor MCP
    cadastro/    aplicativos, sistemas, ferramentas, mudou-parou
    regras/      ordem de serviço, limites, três sinais
    saida/       fila, despachante, estados, comprovante
    ligacao/     cliente MCP fixado em 2026-07-28
    cofre/       cliente fino do OpenBao
    telas/       pendências, ficha, extrato, passkey
    extrato/     gravação só de acrescentar, fechamento
    ronda/       ronda das portas
    vigia/       programa da segunda máquina
    avisos/      e-mail e webhook assinado
    medicoes/    contadores e resumos
    db/          acesso ao banco
  migrations/    migrações Alembic
  docker/        Dockerfiles, Compose, Caddyfile
  grafana/       painéis e alertas versionados
  exemplos/      sistemas de exemplo
  tests/         unidade, propriedades, integração, conformidade
  docs/          roteiros de operação e restauração
```

| Módulo | O que faz | Papel que roda |
| --- | --- | --- |
| `entrada` | Porteiro de versão, verificação do crachá, conferência dos argumentos, servidor MCP de baixo nível, limite fino de pedidos | entrada |
| `cadastro` | Aplicativos, sistemas, ferramentas revisadas, tipo de chave, selo, impressão digital das ferramentas | entrada, despachante, rotinas |
| `regras` | Ordem de serviço, catraca de leituras, limite por aproximação, alerta dos três sinais, pedido igual | entrada |
| `saida` | Fila da porta única de saída, despachante, estados e transições, cancelar, bloquear, comprovante | despachante, entrada |
| `ligacao` | Cliente MCP para os sistemas, fixado em 2026-07-28, sem seguir redirecionamento e sem volta automática. Troca o código de autorização e renova chaves, uma por vez. Também lê as definições para o mudou-parou | despachante |
| `cofre` | Login de máquina, cifra e decifra pelo Transit, rodízio de chaves | despachante, entrada |
| `telas` | Ordens de serviço, pendências, ficha, extrato, bloqueio, cadastro do ADMIN | entrada |
| `extrato` | Linhas só de acrescentar, frases-padrão, cálculo do número de fechamento | todos, por função própria |
| `ronda` | Tentativas de entrar com crachá falso, vencido, de outro condomínio e de outra pessoa | rotinas |
| `vigia` | Ronda de fora, alarme de silêncio, guarda do fechamento | vigia |
| `avisos` | Fila de avisos com novas tentativas, e-mail, webhook assinado e entrada dos alertas do Grafana | rotinas, vigia |
| `medicoes` | Contadores em memória, resumos por minuto, hora e dia, partições | todos |

O servidor MCP de baixo nível é a parte do kit que deixa o gateway montar a lista e o esquema de cada pedido. Assim, a vitrine sai do cadastro, pessoa por pessoa.

### Peças que a proposta não detalhava

**Porteiro de versão.** É um filtro ASGI na frente do kit, porque o kit não deixa recusar a geração antiga. Antes dele roda o verificador de crachá: sem crachá, o aplicativo recebe 401 e faz login. Depois, o porteiro faz quatro coisas:

- pedido sem cabeçalho de versão, como o `initialize`, que abre a conversa na geração antiga, recebe 400 com as versões aceitas;
- pedido com outra versão recebe 400 com o erro -32022 e a lista `supported`;
- GET e DELETE no endereço MCP recebem 405, e o cabeçalho de sessão é ignorado;
- cada recusa é contada em memória, já atribuída ao aplicativo do crachá. Ela vai ao banco nos resumos por minuto, sem uma linha por pedido.

A partida recusa `MCP_ACCEPTED_VERSIONS` e `MCP_UPSTREAM_VERSION` com valor diferente de 2026-07-28. Abrir outra versão pede mudança de código revisada, não só de configuração.

**Conferência dos argumentos.** O servidor de baixo nível do kit não confere os argumentos contra o esquema; a validação antiga foi removida. A entrada valida cada pedido contra o esquema revisado, com `jsonschema` (Draft 2020-12), antes das regras e da ficha. Ela não busca referências externas do esquema (`$ref` de fora).

**Versão no cadastro de sistemas.** Ao cadastrar um sistema, o despachante faz uma sondagem com `server/discover`. Se a resposta não for 2026-07-28, o cadastro é recusado com mensagem legível e contado. No uso diário, o cliente fica fixado na versão, com a sondagem guardada no banco.

**Impressão digital das ferramentas.** O mudou-parou usa um cálculo próprio, com a biblioteca padrão do Python. Ele inclui nome, descrição, esquemas de entrada e de saída e etiquetas. A receita do FastMCP para isso deixa a descrição de fora por padrão e não serve aqui.

### A porta de saída por dentro

A porta de saída é a parte mais delicada do produto. Aqui está como ela evita repetir e como envio, cancelamento e bloqueio disputam a mesma ação.

**Estados de uma ação.** Cada linha da `fila_saida` passa por estes estados:

- aguardando ficha: vira liberada, vencida ou cancelada;
- liberada, com hora de saída: vira enviando, cancelada ou parada por bloqueio;
- enviando: vira enviada, não saiu, sistema pediu dados ou não confirmada;
- não confirmada: muda só pela decisão registrada da pessoa, e a linha nunca é reenviada.

Vencida, enviada, não saiu, sistema pediu dados, cancelada e parada por bloqueio são estados finais.

**Regras de mudança.**

- Cada mudança é um UPDATE que exige o estado anterior, pela tabela de transições do módulo `saida`. Se o estado já mudou, nada acontece.
- Só vira "não saiu" a falha comprovada antes do envio, como conexão recusada. Qualquer falha depois disso, ou na dúvida, vira "não confirmada".
- O cliente do kit entrega tempo esgotado e resposta fora de 2xx como `MCPError`. Por isso essa separação tem regra escrita e teste próprio.
- Ao assumir a trava, o despachante muda todo "enviando" para "não confirmada" antes de enviar qualquer coisa.
- Resposta que chega depois só entra no extrato e na tela de decisão. Ela não muda o estado.

**Um despachante só, e as disputas.**

- O despachante segura uma trava consultiva do PostgreSQL, para haver um só ativo. Um despachante de reserva espera a trava.
- Toda gravação de "enviando" usa a conexão que segura a trava. Se ela cair, a trava some e a gravação falha.
- Numa transação, ele pega a linha liberada com `SELECT ... FOR UPDATE SKIP LOCKED`, que pula linhas já travadas. Trava também, com `FOR SHARE`, as linhas do aplicativo e da pessoa.
- Na mesma transação, confere bloqueio e cancelamento, grava "enviando" e faz COMMIT. Só depois chama o sistema, uma vez.
- Bloquear trava as mesmas linhas com `FOR UPDATE` e monta o comprovante na mesma transação. Criar uma linha liberada também passa por essa trava.
- Gastar um limite é um UPDATE que só passa se houver saldo. Cada ficha libera uma linha só, por restrição única.
- O despachante acorda por LISTEN/NOTIFY, os avisos do próprio PostgreSQL, e faz uma varredura de reserva. Não se usa PgBouncer em modo de transação, que quebra esses avisos e a trava.
- Bibliotecas de fila prontas, como `pgqueuer` e `procrastinate`, ficam fora do caminho das ações. Elas repetem tarefas, e a porta de saída tenta uma vez só.

**Leituras.** Leituras não passam pela fila. Elas chegam pela porta interna do despachante, que reconfere a classe "lê" no cadastro revisado e na ordem de serviço. Ação pedida por essa porta é recusada e vai ao extrato.

**Sem volta automática.** O guia de migração do kit diz que `Client.call_tool()` refaz sozinho as voltas de `InputRequiredResult`. Esse é o pedido de dados que o sistema devolve no meio de uma chamada. Por isso, o cliente do gateway segue três regras:

- não tem `elicitation_callback` nem `sampling_callback`, e assim não responde sozinho a pedidos do sistema;
- numa ação, `InputRequiredResult` vira o estado final "sistema pediu dados", sem segunda volta, também nos sistemas próprios;
- numa ação, não há reenvio depois de 401: o despachante renova a chave antes de enviar, e um 401 no envio vira "não confirmada".

O `OAuthClientProvider` do kit pode reenviar um pedido depois de um 401, para renovar a chave. Isso será conferido em "Provar a base", com um sistema de teste que conta as chamadas.

### Bibliotecas e licenças

Versões e datas conferidas no PyPI, no npm, no proxy Go ou nas imagens em 18/11/2026. Licenças pelos metadados de cada pacote, salvo onde se diz outra coisa.

| Biblioteca ou peça | Versão e data | Licença | Para quê |
| --- | --- | --- | --- |
| Python | 3.14 (imagem `python:3.14`) | PSF | Linguagem; o kit declara suporte até a 3.14 |
| `mcp` e `mcp-types` (kit oficial) | 2.3.0, 02/10/2026; mínimo 2.2.0 | MIT | Servidor e cliente MCP |
| `starlette` | 1.10.0, 24/10/2026 | BSD-3-Clause | Telas e API de administração |
| `uvicorn` | 0.54.0, 25/09/2026 | BSD-3-Clause | Servidor web, com mTLS na porta interna |
| `httpx2` | 2.16.2, 16/11/2026 | BSD-3-Clause | Todo HTTP de saída, sem seguir redirecionamento |
| `pydantic` e `pydantic-settings` | 2.14.3 e 2.16.3, novembro de 2026 | MIT | Validação e leitura de parâmetros |
| `sqlalchemy` (Core) | 2.1.9, 16/11/2026 | MIT | SQL explícito, inclusive nas travas da fila |
| `alembic` | 1.20.1, 22/10/2026 | MIT | Migrações do esquema |
| `asyncpg` | 0.32.0, 06/10/2026 | Apache-2.0 | Conexão ao PostgreSQL, com LISTEN/NOTIFY |
| `pyjwt` | 2.15.1 ou mais novo, 28/09/2026 | MIT | Conferir crachás; versões anteriores à 2.15.0 tinham falhas |
| `webauthn` | 3.0.1, 25/09/2026 | BSD-3-Clause | Passkey do lado do servidor |
| `jinja2` | 3.1.6, 05/03/2025 | BSD-3-Clause | Modelos das telas, com escape automático |
| `jsonschema` | A que vem com o kit | MIT, não conferida nesta rodada | Conferir argumentos contra o esquema revisado |
| `htmx` | 2.0.11, 22/09/2026 | 0BSD | Telas sem aplicação de página única, servido pelo gateway |
| `structlog` | 26.1.0, 06/06/2026 | MIT ou Apache-2.0 | Logs em JSON |
| `aiosmtplib` | 5.1.3, 08/09/2026 | MIT | Envio de e-mail |
| `standardwebhooks` | 1.3.0, 17/11/2026 | MIT | Assinatura dos webhooks |
| `opentelemetry-sdk` | 1.45.2, 29/10/2026, opcional | Apache-2.0 | Rastros, só com `OBS_TRACES_EXPORT=otlp` |
| `hvac` | 2.4.0, 30/10/2025, de reserva | Apache-2.0 | Cliente do cofre, se o cliente fino não servir |
| PostgreSQL | 18.7, 15/11/2026 | PostgreSQL License | Único banco |
| OpenBao | 2.7.2, 26/10/2026 | MPL-2.0 (anotar) | Cofre |
| Caddy, `caddy-ratelimit`, `xcaddy` | 2.11.9; commit de 12/06/2026; 0.4.7 | Apache-2.0 | Portão próprio |
| Keycloak ou Logto | 26.8.2, 12/11/2026; 1.46.0, 05/11/2026 | Apache-2.0; MPL-2.0 (anotar) | Provedor de login |
| Grafana | 13.3.1, 10/11/2026 | AGPL-3.0 (decisão 14) | Painéis |
| pgBackRest | 2.60.1, 16/11/2026 | MIT | Backup |
| OpenTelemetry Collector | 0.165.0, 07/11/2026 | Apache-2.0 | Opcional |

Ferramentas só de desenvolvimento e teste, fora da imagem do produto:

| Ferramenta | Versão e data | Licença | Para quê |
| --- | --- | --- | --- |
| `uv` | 0.13.9, 17/11/2026 | MIT ou Apache-2.0 | Dependências e trava de versões |
| `ruff` e `mypy` | 0.17.5 e 2.4.1 | MIT | Estilo, erros comuns e tipos |
| `pytest` | 9.2.0, 27/10/2026 | MIT | Testes, com o plugin do `anyio` |
| `hypothesis` | 6.170.15, 06/11/2026 | MPL-2.0 (anotar) | Testes de propriedades da fila |
| `testcontainers` | 4.15.0, 24/07/2026 | Apache-2.0 | Peças reais nos testes |
| `pytest-httpx2` ou `respx` | 1.0.0; 0.23.1 | BSD-3-Clause | Simular HTTP |
| `fastmcp` | 4.2.1, 17/11/2026 | Apache-2.0 | Sistemas de exemplo e servidores de teste |
| `mcp`, linha 1.x | 1.30.0, 07/09/2026 | MIT | Só o sistema de exemplo da geração antiga, em imagem de teste separada |
| Suíte de conformidade | `@modelcontextprotocol/conformance` 0.2.0-alpha.12, 01/10/2026 | MIT | Teste oficial do padrão |
| MCP Inspector | 2.16.0, 18/11/2026 | Em transição de MIT para Apache-2.0 | Testes rápidos com `--protocol-era modern` |
| Toxiproxy | 2.13.0, 14/10/2026 | MIT | Perder respostas de propósito |
| Mailpit e webhook-tester | 1.34.0; 2.3.0 | MIT | Receber avisos nos testes |
| `locust` | 2.47.0, 21/10/2026 | MIT | Carga, em contêiner próprio |
| `bandit` e `pip-audit` | 1.9.4 e 2.10.1 | Apache-2.0 | Código e dependências |
| Syft, Grype e Trivy | 1.57.2, 0.122.0 e 0.76.2 | Apache-2.0 | Lista de componentes e falhas conhecidas |

Licenças a anotar: MPL-2.0 (OpenBao, Logto, `certifi` e, só nos testes, `hypothesis`, `pathspec` e `bidict`) e AGPL-3.0 (Grafana). O `pathspec` vem com o `mypy`, e o `bidict`, com o `locust`.

O `psycopg` é LGPL-3.0; o plano usa o `asyncpg` para não tê-lo no produto. O `certifi` só chega se o `hvac` ou o exportador OTLP entrarem.

### Padrões de código

- **Assíncrono.** Todo o núcleo usa `asyncio`, com o `anyio` nos testes. Nada de chamada que trave o processo.
- **Migrações.** O esquema muda só por migração Alembic revisada. Isso inclui as tabelas do cofre e os gatilhos de aviso.
- **Configuração por parâmetros.** Parâmetros de implantação vêm do ambiente e de segredos do Docker, lidos pelo `pydantic-settings`. Valores de política ficam numa tabela versionada no PostgreSQL.
- **Partida que recusa erro.** O gateway não sobe com combinação inválida, como `EDGE_MODE=apigee` hoje, banco sem `verify-full` ou versão diferente de 2026-07-28.
- **Nada de segredo em arquivo do repositório.** Senhas e chaves vêm de segredos do Docker ou do cofre. A varredura de segredos roda a cada mudança.
- **SQL explícito.** SQLAlchemy Core, sem ORM pesado. As travas da fila ficam em SQL escrito à mão, com testes próprios.
- **HTTP de saída único.** Tudo pelo `httpx2`, sem seguir redirecionamento e só para endereço cadastrado.
- **Logs sem conteúdo.** Cada linha leva o número de protocolo e o `trace_id`. Nunca leva crachá, chave, argumento, resultado nem dado pessoal.
- **Kit atrás de uma camada.** O núcleo fala com o kit por poucas interfaces estáveis: ASGI, verificador de crachá e cliente. O middleware do kit, marcado como provisório, só mede e recusa.
- **Imagens e ações fixadas.** Imagens por digest, ações do CI por commit e dependências pelo `uv.lock`.

ASGI é a interface padrão entre servidores web e programas Python. CI é a integração contínua: testes automáticos a cada mudança.

## Parâmetros

Há dois tipos de parâmetro. Os de implantação vêm do ambiente e mudam com um reinício. Os de política ficam numa tabela versionada no PostgreSQL; o ADMIN muda, e a mudança fica registrada.

Os nomes seguem o padrão em inglês já usado na proposta. Todo valor numérico abaixo é inicial, a ajustar no piloto (decisão 11).

### Portão, endereço e versão

| Parâmetro | Valor inicial | O que controla | Quando muda |
| --- | --- | --- | --- |
| `EDGE_MODE` | `proprio` | Quem atende a internet. O valor `apigee` fica reservado e, por ora, a partida o recusa | Na troca para o Apigee, depois das provas da troca |
| `EDGE_ORIGIN_AUTH` | `internet` | Quem o portão próprio aceita. A entrada aceita só o certificado do portão próprio, nos dois modos | Com o Apigee, `apigee_mtls`: o portão próprio passa a aceitar só o Apigee, com certificado |
| `EDGE_TOKEN_PRECHECK` | `desligado` | Primeira conferência do crachá no portão externo | Só com o Apigee, se for testada |
| `PUBLIC_BASE_URL` | Definir na instalação | Endereço público do gateway | Nunca, depois do piloto: mudar invalida crachás e autorizações |
| `MCP_RESOURCE_ID` | `PUBLIC_BASE_URL` + `/mcp` | Identificador do recurso, que o crachá precisa citar como audiência | Junto com o endereço público, o que se evita |
| `MCP_ACCEPTED_VERSIONS` | `2026-07-28` | Versões aceitas dos aplicativos. A partida recusa outro valor | Na revisão de compatibilidade da próxima versão do MCP, com mudança de código |
| `MCP_UPSTREAM_VERSION` | `2026-07-28` | Versão fixada no cliente que fala com os sistemas. A partida recusa outro valor | Na mesma revisão |
| `MCP_RESPONSE_MODE` | `inteira` | Forma da resposta: um corpo JSON só | Fluxo contínuo só depois de testado nos portões |
| `RESPONSE_MAX_WAIT_SECONDS` | 20 | Espera máxima antes de devolver o número de protocolo; nada fica pendurado | Se o piloto mostrar esperas maiores ou menores |
| `ALLOWED_HOSTS` | Nome de `PUBLIC_BASE_URL` | Lista de nomes aceitos, contra religação de DNS | Junto com o endereço público |
| `FORWARDED_ALLOW_IPS` | Endereço do portão na rede interna | De quem o gateway aceita cabeçalhos de origem | Se o portão mudar de endereço |
| `GATEWAY_INTERNAL_PORT` | 8443 | Porta interna da entrada, com mTLS | Raramente |
| `DISPATCHER_INTERNAL_PORT` | 9443 | Porta interna do despachante, para leituras e códigos de autorização vindos da entrada | Raramente |
| `ROUTINES_INTERNAL_PORT` | 9444 | Porta interna das rotinas, só para os alertas do Grafana | Raramente |
| `INTERNAL_CA_FILE` | Certificado da autoridade interna | Autoridade aceita só nas conexões internas, num contexto TLS próprio de cada destino. As conexões para fora usam o repositório do sistema, e `SSL_CERT_FILE` fica sem valor | Se a autoridade interna mudar |
| `GATEWAY_WORKERS` | 2 | Processos da entrada | Pelo teste de carga |

### Banco, login e cofre

| Parâmetro | Valor inicial | O que controla | Quando muda |
| --- | --- | --- | --- |
| `DATABASE_URL` | Sem senha; a senha vem de `DATABASE_PASSWORD_FILE` | Conexão de cada papel ao PostgreSQL | Na instalação |
| `DB_SSLMODE` | `verify-full` | Conexão ao banco com certificado conferido | Nunca abaixo disso |
| `IDP_KIND` | A escolher em "Provar a base": `keycloak` ou `logto` | Provedor de login | Uma vez, após o teste |
| `IDP_ISSUER_URL` | Definir na instalação | Emissor aceito nos crachás | Se o provedor mudar |
| `IDP_JWKS_CACHE_SECONDS` | 300 | Por quanto tempo guardar as chaves públicas | Se a troca de chaves do provedor pedir |
| `VAULT_ADDR` | Endereço interno do cofre | Onde está o OpenBao | Na instalação |
| `VAULT_SEAL_MODE` | `static` em teste; produção a decidir (`shamir`, `static`, `transit` ou `kms`) | Como o cofre é destravado depois de um reinício. `transit` pede outro OpenBao. `kms` usa plugin e, em geral, um serviço comercial de chaves, fora da regra do código aberto | Decisão 6 |
| `VAULT_AUTH_METHOD` | `approle` | Login de máquina do despachante e da entrada | Raramente |
| `REQUEST_STATE_KEY_ROTATION_DAYS` | 30 | Rodízio da chave comum do estado das confirmações | Pela revisão de segurança |

### Chaves de liga e desliga

| Parâmetro | Valor inicial | O que controla | Quando muda |
| --- | --- | --- | --- |
| `FEATURE_PHASE1_READ` | `desligado` | Leitura para pessoas reais | Liga quando a Fase 1 passa nas provas |
| `FEATURE_PHASE2_ACT` | `desligado` | Ações com ficha e limite | Liga quando a Fase 2 passa nas provas |
| `FEATURE_PHASE3_THIRD_PARTY` | `desligado` | Servidores de terceiros | Liga quando a Fase 3 passa nas provas |
| `FEATURE_PHASE4_BADGES` | `desligado` | Selos de nível 2 e 3 | Liga quando a Fase 4 passa nas provas |
| `FEATURE_TASKS` | `desligado` | Extensão Tasks (decisão 13) | Só com implementação própria sobre PostgreSQL e provas |
| `FEATURE_PASSKEY` | `ligado` com a Fase 2 | Passkey nas ações de maior risco | Desliga só pela ordem de corte |
| `FEATURE_TOOL_SEARCH` | `desligado` | Busca sob demanda na vitrine | Quando as ferramentas forem muitas |
| `PATROL_AUTOCLOSE_ENABLED` | `ligado` | Poder da ronda de fechar a entrada sozinha | Desliga só pela ordem de corte |
| `AUTH_ENTERPRISE_ASSERTION_ENABLED` | `falso` | Afirmação de identidade empresarial | Se houver provedor de login da organização |

### Valores de política (decisão 11)

Todos ficam na tabela de política, com registro de quem mudou. Todos são iniciais, a ajustar no piloto.

| Parâmetro | Valor inicial | O que controla | Quando muda |
| --- | --- | --- | --- |
| `WORK_ORDER_MAX_DAYS` | 30 | Validade máxima de uma ordem de serviço | Piloto |
| `WORK_ORDER_WARN_DAYS` | 7 | Aviso antes de a ordem vencer | Piloto |
| `WORK_ORDER_IDLE_DAYS` | 14 | Dias sem uso até a autorização ficar inativa | Piloto |
| `TRIAL_PERIOD_DAYS` | 14 | Período de experiência: tudo passa pela pessoa | Piloto |
| `OUTBOX_HOLD_SECONDS` | 60 | Espera antes de seguir, com relógio visível | Piloto, pelas telas |
| `CARD_TTL_MINUTES` | 30 | Prazo de uma ficha; sem resposta, nada segue | Piloto |
| `CONFIRM_CAP_PER_HOUR` | 10 | Teto de confirmações por aplicativo; acima dele, o aplicativo pausa | Piloto |
| `CONFIRM_TOO_FAST_SECONDS` | 3 | Confirmação mais rápida que isso entra na contagem de "rápida demais" | Piloto |
| `THREE_SIGNALS_WINDOW_MINUTES` | 240 | Janela em que os três sinais se somam | Fase 3 |
| `DUPLICATE_WINDOW_MINUTES` | 30 | Intervalo em que um pedido igual conta como repetição | Piloto |
| `BADGE_TTL_DAYS` | 90 | Validade de um selo de nível 2 ou 3 | Fase 4 |
| `LEDGER_RETENTION_DAYS` | 730 | Guarda do extrato | Piloto e revisão de LGPD |
| `READS_CAP_PER_DAY` | 200 | Catraca: leituras por ordem de serviço por dia | Piloto |
| `ALLOWANCE_MAX_PER_DAY` | 5 | Teto diário máximo de um limite por aproximação | Piloto |
| `ALLOWANCE_MAX_DAYS` | 30 | Prazo máximo de um limite por aproximação | Piloto |

### Programas que rodam sozinhos (decisão 9)

| Parâmetro | Valor inicial | O que controla | Quando muda |
| --- | --- | --- | --- |
| `PROGRAM_AUTH_METHODS` | `private_key_jwt` | Formas de login aceitas; segredo só por decisão do ADMIN | Raramente |
| `PROGRAM_TOKEN_TTL_SECONDS` | 300 | Validade do crachá de um programa | Piloto |
| `PROGRAM_KEY_MAX_AGE_DAYS` | 90 | Prazo para trocar a chave do programa | Revisão de segurança |
| `PROGRAM_KEY_EXPIRY_WARN_DAYS` | 14 | Aviso antes de a chave vencer | Piloto |
| `PROGRAM_ACTIONS_ENABLED` | `falso` | Se programas podem agir, dentro de limites | Liga só depois das provas da Fase 2 |
| `PROGRAM_PENDING_TIMEOUT_HOURS` | 24 | Prazo de uma pendência do responsável | Piloto |
| `PROGRAM_OWNER_RECERT_DAYS` | 90 | Revisão periódica pelo responsável | Piloto |
| `PROGRAM_IDLE_PAUSE_DAYS` | 30 | Dias sem uso até o programa pausar | Piloto |
| `PROGRAM_MAX_REQUESTS_PER_MINUTE` | 30 | Limite de pedidos de um programa | Teste de carga e piloto |

### Avisos, ronda, vigia e backup

| Parâmetro | Valor inicial | O que controla | Quando muda |
| --- | --- | --- | --- |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM` | Definir; porta 587 com STARTTLS | Servidor de e-mail dos avisos | Na instalação |
| `SMTP_PASSWORD_FILE` | Segredo do Docker | Senha do SMTP | No rodízio de senhas |
| `WEBHOOK_TARGETS` | Nenhum; o ADMIN cadastra | Destinos de webhook, assinados | Quando o ADMIN cadastrar |
| `NOTIFY_PENDING_AFTER_MINUTES` | 5 | Quando avisar de ficha pendente | Piloto |
| `NOTIFY_RETRY_MAX` | 5 | Novas tentativas de um aviso; aviso pode repetir, ação não | Piloto |
| `PATROL_INTERVAL_SECONDS` | 300 | Intervalo da ronda das portas | Piloto |
| `PATROL_FAILS_TO_CLOSE` | 2, como na proposta | Tentativas indevidas seguidas que fecham a entrada por etapas | Raramente |
| `CLOSING_INTERVAL_HOURS` | 6 | Intervalo entre fechamentos | Piloto |
| `WATCH_PUBLIC_URL` | Igual a `PUBLIC_BASE_URL` | Endereço que o vigia testa de fora | Junto com o endereço público |
| `WATCH_PROBE_INTERVAL_SECONDS` | 600 | Intervalo da ronda do vigia | Piloto |
| `WATCH_SILENCE_ALARM_SECONDS` | 900 | Silêncio que dispara o alarme do vigia | Piloto |
| `WATCH_SMTP_*` e `WATCH_WEBHOOK_TARGETS` | Próprios do vigia | Canais de aviso do vigia | Na instalação |
| `BACKUP_RESTORE_DRILL_DAYS` | 30 | Intervalo dos ensaios de restauração | Piloto |

### Limites de pedidos e medições

| Parâmetro | Valor inicial | O que controla | Quando muda |
| --- | --- | --- | --- |
| `EDGE_RATE_PER_IP_PER_MINUTE` | 120 | Limite grosso no portão, por endereço de origem | Teste de carga |
| `RATE_PER_APP_PER_MINUTE` | 60 | Limite fino por aplicativo, no gateway | Piloto |
| `RATE_PER_PERSON_PER_MINUTE` | 30 | Limite fino por pessoa | Piloto |
| `MAX_REQUEST_BYTES` | 4 MiB, padrão do kit | Tamanho máximo de um pedido | Se o piloto pedir |
| `METRICS_FLUSH_SECONDS` | 30 | De quanto em quanto tempo os contadores vão ao banco | Teste de carga |
| `METRICS_MINUTE_RETENTION_DAYS` | 31 | Guarda dos resumos por minuto | Piloto |
| `METRICS_HOUR_RETENTION_MONTHS` | 13 | Guarda dos resumos por hora | Piloto |
| `METRICS_DAY_RETENTION_YEARS` | 5 | Guarda dos resumos por dia | Piloto |
| `OBS_TRACE_ACCEPT_INBOUND` | `ligado` | Aceitar o `traceparent` do aplicativo, só para correlação | Raramente |
| `OBS_TRACE_PROPAGATE_DOWNSTREAM` | `sistemas_proprios` | Enviar `traceparent` novo aos sistemas próprios; nunca a terceiros por padrão | Raramente |
| `OBS_TRACES_EXPORT` | `desligado` | Exportar rastros por OTLP | Se a empresa tiver ferramenta de rastros |
| `OBS_EXPORT_CONTENT` | `falso` | Conteúdo de ferramenta em rastros | Não muda no começo |
| `LOG_DRIVER` | `local` | Rotação de logs do Docker; `json-file` só com o Collector. O [padrão do Docker](https://github.com/docker/docs/blob/main/content/manuals/engine/logging/configure.md) não faz rotação e pode encher o disco | Se o Collector ligar |

## Etapas de entrega

O plano não tem datas nem durações, porque o tamanho da equipe não foi definido. Cada etapa só termina quando cumpre a lista "Prova de saída". Toda fase repete o teste oficial e o ensaio do dublê; da Fase 2 em diante, também os testes de resposta perdida.

| Etapa | O que você ganha | O que você decide antes |
| --- | --- | --- |
| Etapa 0: fundação | Nada visível ainda: o terreno para construir e testar | Confirmar as premissas 1 e 2 |
| Provar a base | Uma leitura de ponta a ponta, num sistema de exemplo, pelo portão próprio | Nada antes; no fim, confirmar o provedor de login escolhido (decisão 3) |
| Fase 1: olhar | Pessoas reais lendo um sistema seu por dois aplicativos de IA, com extrato e bloqueio | Decisões 4, 5 e 6 |
| Fase 2: agir | Ações com ficha ou limite, sem repetir; programas que rodam sozinhos, só lendo | Que sistema e que ações entram primeiro |
| Fase 3: servidores de terceiros | Servidores comuns de terceiros, atendidos na sala de visitas | Quais servidores ligar primeiro |
| Fase 4: selos | Selo de nível 2 ou 3 para sistemas que provam mais | Quais equipes adaptam seus sistemas ao contrato |

> Desenho: etapas de entrega · seis etapas e o que fica para depois (versao com desenhos no documento compartilhado).

Leia da esquerda para a direita, linha por linha. Cada caixa termina na prova que libera a seguinte; a caixa tracejada não tem data.

### Etapa 0: fundação

Objetivo: deixar pronto o terreno onde todo o resto será construído e testado.

Entrega:

- Criar o repositório com o pacote `mcp_gateway`, o `uv.lock` e as regras de `ruff` e `mypy`.
- Montar o CI com testes, varredura de dependências, de imagens e de segredos, e lista de componentes.
- Montar a imagem do gateway sobre `python:3.14` e a do Caddy com o módulo de limite, pelo `xcaddy`.
- Escrever os arquivos de Docker Compose de desenvolvimento e de teste, com as imagens fixadas por digest.
- Criar as primeiras migrações e o esqueleto de parâmetros, com a recusa de combinações inválidas.
- Pôr a suíte oficial de conformidade, fixada na 0.2.0-alpha.12, rodando no CI.
- Escrever seis sistemas de exemplo:
  - leitura com chave por pessoa;
  - chave compartilhada;
  - ações numa caixa de teste;
  - um mal-comportado;
  - um só na geração antiga, com o kit `mcp` 1.x, em imagem de teste separada. Antes, conferir que ele não fala a 2026-07-28;
  - um dublê da Fase 2, que devolve número de protocolo, "estamos confirmando" e ficha pendente, para o simulado do dia ruim.
- Escrever um modelo de ameaças curto e o roteiro de segredos.

Prova de saída:

- CI verde a cada mudança, com varredura e lista de componentes.
- Suíte oficial verde contra um sistema de exemplo, na versão 2026-07-28.
- Sistema de exemplo da geração antiga conferido: ele não fala a 2026-07-28.
- Ambiente de teste sobe numa máquina limpa, só com parâmetros e segredos do Docker.
- Varredura de segredos limpa no repositório.

### Provar a base

Objetivo: escolher as peças por teste e provar o caminho de um pedido de leitura, de ponta a ponta.

Entrega:

- Testar Keycloak e Logto com a suíte, no modo autorização, se a versão publicada já o tiver. Testar também com os nossos testes de crachá. Escolher um pelo critério "funciona inteiro sem licença paga".
- Escrever o porteiro de versão, um filtro ASGI na frente do kit.
- Escrever o verificador de crachá: assinatura, emissor, audiência, validade e algoritmos aceitos.
- Validar os argumentos contra o esquema revisado, com `jsonschema` (Draft 2020-12) e sem buscar `$ref` de fora, antes das regras e da ficha.
- Montar o servidor MCP de baixo nível com uma vitrine vinda do cadastro.
- Ligar um sistema de exemplo com o cliente fixado em 2026-07-28 e a sondagem guardada no banco.
- Desligar a volta automática do cliente: sem respostas sozinhas a pedidos do sistema, sem segunda volta e sem reenvio depois de 401.
- Subir o OpenBao sobre PostgreSQL, com o motor Transit, e testar o cliente fino. Testar o `hvac` como reserva.
- Pôr o Caddy na frente, com mTLS na porta interna.
- Conferir os pontos em aberto: resposta inteira na via nova e sondagem que não cai no `initialize` antigo.
- Conferir também `get_access_token()` no servidor de baixo nível, a simulação do `httpx2` nos testes e o `private_key_jwt` no Logto.
- Rodar o simulado do dia ruim com os candidatos a piloto e o dublê da Fase 2. Ensaiar resposta perdida, pedido de confirmação e recusa de versão.
- Aplicar o critério do FastMCP, se faltar algo no kit para a Fase 1.

Prova de saída:

- Suíte oficial verde direto na entrada e através do Caddy. Atrás fica o servidor de exemplo do kit, com uma ordem de serviço de teste.
- O que o desenho deixa de fora entra na lista de falhas esperadas, com motivo escrito e revisado. Exemplos: recursos, prompts e sugestões de preenchimento, se a vitrine for só de ferramentas. Também pedidos do sistema à pessoa ou ao modelo, e fluxo contínuo e progresso, com resposta inteira.
- Aplicativo da geração antiga recebe, depois do login, uma recusa legível que cita as versões aceitas, e entra na contagem.
- Pedido na versão antiga, 2025-11-25, recebe a recusa que o padrão manda, com a lista das versões aceitas. Pedidos de um tipo que só a geração antiga usa também são recusados.
- Sistema de exemplo da geração antiga é recusado no cadastro, com mensagem legível, e contado.
- Crachá de outro condomínio, vencido ou de outro emissor é recusado.
- Ferramenta fora da vitrine, chamada pelo nome, é recusada. Argumento fora do esquema revisado também.
- Ação pedida pela porta de leitura é recusada pelo despachante, mesmo com a entrada alterada de propósito.
- Quando o sistema de teste pede dados no meio de uma ação ou recusa o crachá, ele conta uma chamada só.
- O crachá do aplicativo não chega ao sistema de exemplo.
- Chamada à porta interna sem o certificado do portão é recusada.
- Provedor de login escolhido, com o motivo registrado.

### Fase 1: olhar

Objetivo: ligar a leitura para pessoas reais, com ordem de serviço, extrato e bloqueio.

Entrega:

- Criar o cadastro único das ações, com revisão do ADMIN e registro de cada mudança.
- Criar as ordens de serviço: modelos do ADMIN, escolha da pessoa, prazo que vence sozinho, aviso antes e inatividade.
- Montar a vitrine sob medida a partir do cadastro, com listas que o aplicativo não guarda em cache.
- Ligar o cofre de chaves. A pessoa autoriza na página do sistema, e a volta da autorização chega às telas. As telas passam o código ao despachante pela porta interna.
- Fazer o despachante trocar o código pela chave, cifrá-la pelo Transit e gravá-la. A entrada nunca vê a chave.
- Renovar cada chave uma por vez, com trava por pessoa e sistema, gravando a chave nova antes do uso.
- Tratar sistemas de chave compartilhada: lista do ADMIN, só leitura e aviso no extrato.
- Fazer a leitura com medida: máscara de campos pessoais, catraca diária e texto de estranhos marcado.
- Fazer o extrato único com frases-padrão, o resumo semanal do ADMIN e o motivo para abrir extrato alheio.
- Fazer o bloqueio de um toque e a pausa. Desativar uma pessoa derruba as autorizações dela.
- Ligar o mudou-parou para sistemas próprios: ferramenta que passa a pedir mais perde a autorização.
- Criar no provedor de login as identidades da ronda e do vigia. São programas pré-cadastrados, com JWT assinado, presos a um sistema de exemplo da ronda e sem acesso a sistema real.
- Abrir o login de programas já aqui, só para essas identidades. O crachá de outro condomínio vem de um segundo recurso de teste; o falso, de uma chave própria da ronda.
- Pôr no ar a ronda das portas e o vigia de fora, com alarme de silêncio por e-mail e webhook.
- Montar medições no PostgreSQL e painéis no Grafana, incluindo as recusas por versão antiga.
- Montar o backup, o roteiro de restauração e a conferência de continuidade do banco na partida.

Prova de saída, para ligar no piloto:

- Dois aplicativos-piloto conectando de verdade, na versão 2026-07-28.
- Um sistema do dono conectado, com chave por pessoa no cofre (depende da decisão 5).
- Teste oficial do padrão passando pelo portão próprio.
- Crachá de outro condomínio recusado; crachá do aplicativo não repassado ao sistema.
- Uma pessoa não alcança o que é de outra.
- Ferramenta alterada de propósito para pedir mais perde a autorização até nova revisão.
- Backup restaurado e lido. O gateway percebe sozinho a restauração, e os aplicativos voltam pausados até cada pessoa reativar.
- Ronda das portas no ar, e o vigia avisa quando o gateway fica mudo.
- Recusas por versão antiga aparecem nas contagens, dos dois lados.
- Ensaio do dublê mal-intencionado sem nenhuma passagem indevida.
- Telas de ordem de serviço, extrato e bloqueio entendidas por pessoas sem formação técnica.

### Fase 2: agir

Objetivo: deixar uma ação seguir só com ficha confirmada ou dentro de um limite, sem repetir.

Entrega:

- Dar número de protocolo e código de conferência a toda ação.
- Pôr na vitrine a consulta pelo número de protocolo. Ela devolve estado e resultado só à mesma pessoa e ao mesmo aplicativo.
- Montar a ficha no gateway, com a caixa "o aplicativo diz" à parte e a caixa de pendências.
- Ligar a passkey para ações de maior risco e para criar ou ampliar limites.
- Avisar de ficha pendente por e-mail e webhook.
- Montar a porta única de saída: fila, despachante único, espera visível, cancelar antes de sair e "seguir agora".
- Fazer o "estamos confirmando": resultado não confirmado, decisão da pessoa registrada e pedido igual que devolve a operação original.
- Fazer o limite por aproximação, com ferramenta, destinatários, quantidade e prazo.
- Fazer o teto de confirmações e o período de experiência.
- Fazer o comprovante de bloqueio em três partes.
- Calcular o fechamento várias vezes ao dia e guardar o número no vigia.
- Abrir os programas que rodam sozinhos, só com leitura (decisão 9).
- Fazer a restauração com ações na fila: a porta de saída volta fechada.

Só um despachante envia por vez, e cada ação fica marcada "enviando" antes de sair. Bloquear, cancelar e enviar disputam a mesma ação, e só um deles acontece. O detalhe técnico está em "A porta de saída por dentro".

Prova de saída:

- Resposta perdida de propósito não duplica nada, nos nossos testes e no [teste público da discussão sobre repetição](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394).
- O que a ficha mostra é o que segue: o código de conferência bate com o pedido enviado.
- "Cancelado antes de sair" sem nenhuma chamada registrada no sistema.
- Um aplicativo-piloto acha o resultado de uma ação pela consulta do número de protocolo.
- Nos testes de disputa, bloquear e enviar a mesma ação ao mesmo tempo resulta em um só dos dois.
- Bloqueio no meio de uma fila cheia: nada segue depois dele, e o comprovante bate com o histórico.
- Fechamento calculado e guardado na máquina do vigia.
- Backup restaurado com ações na fila: a porta de saída volta fechada, e nada segue sem nova confirmação.
- Telas de ficha, limite e "estamos confirmando" entendidas por pessoas sem formação técnica.
- Em teste, com ações ligadas, um programa não age fora do limite. O que passa vai às pendências do responsável e vence.

### Fase 3: servidores de terceiros

Objetivo: receber servidores que não são do dono, cada um revisado e atendido na sala de visitas.

Entrega:

- Fazer a revisão servidor a servidor pelo ADMIN, com nomes e descrições escritos no cadastro.
- Montar a sala de visitas: endereço combinado, sem redirecionamento, chave separada e sem `traceparent`.
- Barrar pedidos de senha, link ou confirmação vindos do servidor; eles não chegam à pessoa.
- Marcar a resposta do servidor como texto escrito por estranhos.
- Endurecer o mudou-parou: qualquer mudança, mesmo só de texto, leva o servidor inteiro à quarentena.
- Fazer o alerta dos três sinais, somado por pessoa e por aplicativo na janela.
- Recusar no cadastro servidor que só fala a versão antiga, com contagem.

Prova de saída:

- Cada servidor de terceiros revisado e registrado.
- Sala de visitas isolada: o servidor não alcança chaves nem dados de outros sistemas.
- Provas de crachá não repassado e de chave por pessoa repetidas com um servidor de terceiros.
- Mudança só de texto num servidor de teste leva à quarentena.
- Três sinais juntos suspendem o limite por aproximação, e a ficha mostra o motivo.
- Pedido de senha vindo do servidor não chega à pessoa.

### Fase 4: selos

Objetivo: permitir que sistemas que provam mais ganhem selo de nível 2 ou 3.

Entrega:

- Publicar o contrato técnico, pequeno e igual para todos.
- Conferir na fonte os requisitos do rascunho MWS que o contrato aproveita.
- Escrever os testes por sistema: número de protocolo, consulta, relato do que fez e obediência ao bloqueio.
- Mostrar o selo com data e validade na ficha e no extrato. Ele volta ao nível 1 se o teste vencer ou falhar.
- Resolver o "estamos confirmando" pela consulta ao próprio sistema, nos níveis 2 e 3.

Prova de saída:

- Contrato publicado, com os requisitos do MWS conferidos na fonte.
- Testes rodando por sistema, com a data de cada um.
- Selo só sobe com teste aprovado e tem validade.
- Comprovante de bloqueio inclui o que o sistema de nível 3 informa, marcado como informado por ele.

### Depois: Apigee e ideias em estudo

- **Apigee.** A troca é mudar `EDGE_MODE` e `EDGE_ORIGIN_AUTH` e fazer o endereço público levar ao Apigee, sem mudar `PUBLIC_BASE_URL`. Antes, o modo é ensaiado com um dublê de portão externo, como previa a proposta.
- **Provas da troca.** Tipo de Apigee e limites conferidos e a mesma bateria nos dois portões. Também a porta dos fundos recusada e o pedido sem alteração nem repetição.
- **Tasks.** Se a chave ligar, a implementação será nossa, sobre PostgreSQL. As tarefas do FastMCP dependem de Redis.
- **Ideias em estudo, sem compromisso.** Avisos de conclusão, cartão de status no aplicativo, crachá preso a uma chave e dados-isca contra vazamento.
- **Próxima versão do MCP.** A revisão de compatibilidade decide quando migrar.

## Como vamos testar

Cada tipo de teste tem uma pergunta e um momento. "Antes de abrir fase" quer dizer antes de ligar a chave da fase no piloto.

| Tipo de teste | O que prova | Ferramenta | Quando roda |
| --- | --- | --- | --- |
| Unidade | Regras, código de conferência, montagem da ficha, máscara, frases do extrato | `pytest` com `anyio` | A cada mudança |
| Propriedades da máquina de estados da porta de saída | Em sequências sorteadas de confirmar, cancelar, bloquear, perder resposta e reiniciar, nada sai duas vezes e nada sai depois de bloqueio | `hypothesis`, em modo de estados, contra PostgreSQL real | A cada mudança (curto) e todo dia (longo) |
| Disputas na porta de saída | Bloquear, cancelar e enviar a mesma ação ao mesmo tempo: só um acontece. Dois pedidos pelo último uso de um limite: só um passa | Duas conexões reais ao PostgreSQL, com pausas forçadas entre os comandos | A cada mudança |
| Integração com Docker | As peças reais conversam: PostgreSQL, OpenBao, provedor de login, Caddy, Mailpit | `testcontainers` e Compose de teste | A cada mudança (parcial) e todo dia (completo) |
| Bateria própria de versão e crachá | Versão antiga recusada, dos dois lados. Crachá de outro condomínio, vencido ou de outro emissor recusado | `pytest`, MCP Inspector 2.16.0 e cliente FastMCP fixado | A cada mudança |
| Suíte oficial de conformidade | O gateway segue as regras de comunicação da 2026-07-28, como servidor, como cliente e no provedor de login | `@modelcontextprotocol/conformance` 0.2.0-alpha.12, Node 22 em contêiner de teste | A cada mudança e antes de abrir fase |
| Ensaio do dublê mal-intencionado | Aplicativo falso tenta pular a ficha, reusar confirmação, trocar o pedido e chamar fora da vitrine. Tenta também agir pela porta de leitura e por outra pessoa | Cliente e servidores de teste com FastMCP e kit oficial | Todo dia e antes de abrir fase |
| Resposta perdida de propósito | Repetir depois de resposta perdida não duplica efeito | Toxiproxy 2.13.0, cortando só o caminho da resposta; teste público da discussão sobre repetição | Todo dia, da Fase 2 em diante, e antes de abrir fase |
| Ronda das portas | A ronda acusa uma falha plantada e fecha a entrada por etapas | Módulo `ronda` contra ambiente com falha plantada; vigia de fora | A cada mudança no módulo; em produção, sempre |
| Carga | Limites de pedidos funcionam; fila sob pressão não perde estado; medições não seguram pedido | `locust` 2.47.0 em contêiner próprio | Antes de abrir fase |
| Restauração de backup | Restaurar numa máquina limpa. O gateway percebe a restauração, fecha a porta de saída e pausa os aplicativos; a fila vira "resultado não confirmado" | pgBackRest e `pg_dump`, com roteiro escrito | Antes de abrir fase e a cada `BACKUP_RESTORE_DRILL_DAYS` |
| Avisos | E-mail e webhook chegam, com assinatura, e novas tentativas funcionam | Mailpit, em modo de erro forçado, e webhook-tester | Todo dia |
| Varredura de segurança | Dependências, imagens, código e segredos sem falha conhecida aberta | `pip-audit`, Grype, Trivy, Syft, regras `S` do `ruff`, `bandit` | A cada mudança e todo dia |
| Telas com pessoas sem formação técnica | A pessoa explica, com as próprias palavras, o que vai acontecer ao confirmar, cancelar e bloquear | Sessões guiadas com roteiro, sem ajuda da equipe | Antes de abrir a Fase 1 e a Fase 2 |

Regras dos testes:

- A suíte oficial nunca roda sem versão fixada. A etiqueta `latest` aponta para a 0.1.16, anterior à 2026-07-28.
- A lista de falhas esperadas fica no repositório, uma entrada por checagem. Cada entrada traz o número do chamado ou, se ficou de fora por desenho, o motivo escrito e revisado.
- A suíte não testa crachá de outro destino do lado do servidor. Isso fica com a bateria própria e com a ronda.
- Para testar o gateway com login, um proxy só de teste injeta o crachá de uma pessoa de teste.
- A máquina de CI que tem acesso ao Docker fica isolada.

## Dados, segurança e operação

### Tabelas principais no PostgreSQL

| Tabela | O que guarda | Quem escreve |
| --- | --- | --- |
| `aplicativos` | Aplicativos habilitados, identificador no provedor, família, versão do MCP vista | ADMIN, pelas telas |
| `pessoas` | Emissor e identificador da pessoa, situação | Entrada, no primeiro login; ADMIN |
| `programas` | Programas que rodam sozinhos, responsável, substituto, data de fim | ADMIN; responsável aprova limites |
| `sistemas` | Endereço, tipo de chave, selo, versão, sondagem guardada, quarentena | ADMIN; rotinas marcam quarentena |
| `ferramentas_revisadas` | Definição revisada, classe (lê, muda, tem volta), risco, campos pessoais, impressão digital | ADMIN; cada mudança vira nova versão |
| `ferramentas_vistas` | Definição que o sistema declarou na última leitura | Despachante, a pedido das rotinas |
| `modelos_ordem` e `ordens_servico` | Ordens que existem e ordens concedidas, com prazo e situação | ADMIN; pessoa |
| `limites` | Limites por aproximação: ferramenta, destinatários, quantidade, prazo | Pessoa, com passkey |
| `operacoes` | Número de protocolo, código de conferência, estado | Entrada; despachante |
| `fichas` | Ficha montada, decisão, hora, confirmação por passkey | Entrada; pessoa |
| `fila_saida` | Uma linha por ação, com os estados de "A porta de saída por dentro". Cada mudança exige o estado anterior | Entrada cria; despachante envia; pessoa cancela |
| `bloqueios` e `comprovantes` | Bloqueios e as três partes do comprovante | Pessoa ou ADMIN; despachante |
| `chaves_cifradas` | Chaves das pessoas e dos programas, cifradas pelo Transit | Despachante |
| `extrato` | Cada passo, em linhas só de acrescentar | Todos os papéis, por uma função própria |
| `fechamentos` | Número de cada fechamento e o trecho que cobre | Rotinas; cópia no vigia |
| `politica` | Valores de política, versionados, com autor | ADMIN |
| `sinais` | Sinais do alerta dos três sinais, por pessoa e aplicativo | Entrada |
| `recusas_versao` | Resumos por minuto das recusas por versão antiga, por aplicativo e por sistema | Entrada e despachante, pelos contadores |
| `continuidade_banco` | Linha do tempo e identificador do banco vistos na última partida; marca "restaurado" | Despachante; roteiro de restauração |
| `ronda` | Resultado de cada ronda | Rotinas |
| `avisos` | Fila de e-mails e webhooks, com tentativas | Rotinas |
| `medicoes_minuto`, `_hora`, `_dia` | Contagens sem pessoa e sem conteúdo | Todos os papéis; rotinas resumem |
| `sessoes_telas` | Sessões de login das telas | Entrada |

### O que é só de acrescentar

- O `extrato` aceita só INSERT. O usuário de cada papel não tem permissão de UPDATE nem DELETE, e um gatilho recusa as duas.
- O extrato é particionado por mês. Uma partição só sai depois de `LEDGER_RETENTION_DAYS` e depois de o fechamento dela estar no vigia.
- As partições são criadas e apagadas por uma rotina própria, em Python; o `pg_partman` fica como opção. A tabela de medições por minuto também é particionada por mês.
- Cada fechamento encadeia o anterior e cobre só linhas de transações já terminadas. Cada linha guarda o número da sua transação (`pg_current_xact_id()`).
- O fechamento para antes da transação aberta mais antiga (`pg_snapshot_xmin`). Assim, uma linha que termina atrasada entra no fechamento seguinte, sem falso alarme.
- Um fechamento roda por vez, com trava consultiva própria e restrição única sobre o anterior. Alterar um trecho já fechado faz a conta refeita deixar de bater com o número guardado no vigia.
- `fechamentos` e `ronda` também são só de acrescentar.
- As medições são derivadas do extrato e nunca o contrário. Se gravar uma contagem falhar, perde-se a contagem, não o pedido.

### Cofre e chaves

- O OpenBao guarda só as chaves de cifra, no motor Transit. As chaves das pessoas ficam cifradas nas tabelas do gateway, na mesma transação da fila.
- Só o despachante tem permissão de decifrar. A entrada lê apenas a chave comum do estado das confirmações, num caminho separado.
- A chave comum do estado das confirmações tem pelo menos 32 bytes e troca em rodízio. Todas as réplicas da entrada usam a mesma.
- Os certificados internos vêm do motor PKI do OpenBao ou do `tls internal` do Caddy. A escolha fica para "Provar a base".
- Enquanto o cofre estiver trancado, a porta de saída fica fechada, e sai um aviso "cofre trancado".
- `VAULT_SEAL_MODE`: em teste, `static`, com a chave num segredo do Docker, fora do backup. No piloto, sugerimos `shamir` 2 de 3, com pessoas nomeadas. Shamir é dividir a chave em partes guardadas por pessoas diferentes.
- Com `static` na mesma máquina, quem tem acesso total à máquina tem a chave. Essa forma protege contra roubo de disco e de backup, não contra quem opera.

### Backup e restauração

- pgBackRest 2.60.1 faz cópia completa, diferencial e incremental, com recuperação para um ponto no tempo. A cópia cifrada vai para um repositório fora das duas máquinas.
- O pgBackRest fica nos dois lados: na imagem do banco e no contêiner de backup. Os dois se falam pelo servidor TLS do próprio pgBackRest, com certificados dos dois lados.
- Um `pg_dump` lógico periódico completa o backup. Os dois são restaurados em ensaio.
- O [Barman](https://pypi.org/pypi/barman/json) é GPL-3.0 e fica fora; o pgBackRest basta.
- Versões do pgBackRest anteriores à 2.59.3 podiam gerar subchaves fracas. O repositório de backup é criado já na 2.60.1.
- Os certificados do Caddy ficam em arquivos no volume `/data`, fora do PostgreSQL, como exceção a confirmar com você. O pgBackRest não copia esse volume: uma rotina própria manda uma cópia cifrada ao repositório de backup.
- Os painéis do Grafana vêm de arquivos do repositório.
- Restaurar o cofre exige também o meio de destravar. O roteiro diz onde ele fica, sem guardá-lo junto do backup.
- Na partida, o despachante confere a continuidade do banco. Compara a linha do tempo (`timeline_id`) e o identificador (`system_identifier`) do PostgreSQL com os últimos gravados.
- Confere também se o último fechamento guardado no vigia não é mais novo que o do banco. Uma recuperação para um ponto no tempo muda a linha do tempo.
- Se algo não bater, se o vigia não responder ou se o roteiro gravar a marca "restaurado", a porta de saída fica fechada. Os aplicativos também voltam pausados.
- O que estava na fila vira "resultado não confirmado". Reabrir é decisão humana registrada.
- O extrato registra o intervalo perdido entre o backup e a restauração.

### Vigia

- Fica em outro provedor ou outra rede, com acessos separados dos do gateway.
- Repete a ronda pelo endereço público, como um aplicativo de fora.
- Dispara o alarme de silêncio quando o gateway não responde por `WATCH_SILENCE_ALARM_SECONDS`.
- Busca cada fechamento com crachá próprio de programa e guarda só acrescentando. As identidades do vigia e da ronda nascem na Fase 1, sem acesso a sistema real.
- Avisa por SMTP e webhook próprios. Não depende do Grafana nem das rotinas do gateway.
- O [Gatus](https://raw.githubusercontent.com/TwiN/gatus/master/README.md), de código aberto e com dados em PostgreSQL, fica como opção para a parte de sondagem. A versão atual dele não foi conferida.
- Não cobre alteração feita antes do fechamento, nem por quem tiver acesso às duas máquinas.

### Atualizações

- Imagens fixadas por digest e dependências pelo `uv.lock`. Nada se atualiza sozinho, inclusive os plugins do Grafana.
- No Grafana, desligar a atualização automática de plugins, o envio de estatísticas e as checagens de versão.
- Varredura diária de falhas conhecidas. Alerta alto ou crítico numa peça do caminho do pedido vira correção prioritária.
- O kit oficial fica fixado. Subir de versão pede a suíte oficial e a bateria própria verdes.
- Uma revisão de compatibilidade por semestre cobre MCP, kit, Python, PostgreSQL, OpenBao e provedor de login.
- A próxima versão do MCP, prevista para 30/03/2027, abre uma revisão própria antes de mudar `MCP_ACCEPTED_VERSIONS`.

### Quem opera

- **ADMIN da organização.** Habilita aplicativos, sistemas e programas; revisa o cadastro; define ordens de serviço; bloqueia.
- **Equipe que opera o gateway.** Mantém o serviço no ar, roda as provas, ensaia a restauração e apura quando a ronda acusa algo.
- **Guardiões do cofre.** Pessoas nomeadas que guardam as partes da chave, se o modo for `shamir`.
- **Responsáveis por programas.** Aprovam limites e respondem às pendências dos seus programas.

## Pronto para o Apigee, sem usá-lo

Pela decisão 7, tudo é implementado e testado sem o Apigee. Mas o caminho fica pronto para o dia em que ele vier.

O que fica preparado agora:

- O parâmetro `EDGE_MODE`, com `apigee` reservado e recusado na partida, por ora.
- O certificado na porta interna: desde "Provar a base", o portão próprio chama a entrada com mTLS. Com o Apigee, o portão próprio fica no lugar e passa a aceitar só o certificado do Apigee, pelo `client_auth` do Caddy. A entrada segue aceitando só o certificado do portão próprio.
- O endereço público fixo, que é também o identificador do recurso nos crachás.
- A descoberta do login e as respostas 401 servidas pelo mesmo endereço público.
- Respostas inteiras por padrão, com `MCP_RESPONSE_MODE=inteira`.
- Nada pendurado: passado `RESPONSE_MAX_WAIT_SECONDS`, o gateway devolve o número de protocolo. Os prazos de espera do Apigee serão conferidos antes da troca.
- Nenhum portão repete pedidos. No Caddy, isso já é o padrão do proxy reverso.
- O gateway confere o crachá nos dois modos, e a identidade do aplicativo vem do crachá, nunca do endereço de origem.
- Nenhuma regra depende de cabeçalho MCP vindo do portão.
- A bateria de provas roda pelo portão próprio desde "Provar a base" e poderá rodar, igual, pelo Apigee.

O que não se faz agora:

- Nenhuma configuração, política ou código do Apigee.
- Nenhuma primeira conferência de crachá no portão externo.
- Nenhum recurso próprio de MCP do Apigee; a vitrine tem um dono só.
- Nenhuma resposta em fluxo contínuo.
- Nenhuma rede no Google Cloud nem revisão de LGPD da passagem pelo Google, que fica para antes da troca.
- Nenhum ensaio com dublê de portão externo por ora. A proposta o previa antes da chegada do Apigee. Pela decisão 7, ele entra quando a troca for marcada.

## Riscos do plano

| Risco | Como o plano trata |
| --- | --- |
| Equipe pequena e de tamanho indefinido | Núcleo pequeno, uma organização por instalação, fases com chave própria e a ordem de corte abaixo |
| Kit oficial mudando | Versão fixada; núcleo ligado ao kit por poucas interfaces estáveis; suíte e bateria própria a cada subida. Voltas automáticas do cliente desligadas e provadas por teste. A documentação anuncia que a versão 3.0 muda padrões de crachá e emissor |
| Poucos aplicativos na versão nova | Recusas contadas desde o primeiro dia; pilotos escolhidos entre os que têm registro público; MCP Inspector em modo `modern` para testes. Se o piloto travar por isso, a decisão volta a você |
| Sistemas na versão antiga | Recusa legível no cadastro, contagem e receita curta para equipes migrarem; sistemas de exemplo enquanto isso |
| Keycloak ou Logto com partes experimentais ou pagas | Os dois serão testados com a suíte de autorização; audiência por mapeador no Keycloak; pré-cadastro em vez de cadastro automático |
| Escopo crescendo | Nada entra sem prova de saída; ideias ficam na caixa "Depois"; proxy, Tasks e serviços hospedados ficam fora |
| Peça não oficial no portão | Módulo de limite do Caddy fixado por commit e revisto a cada atualização; Traefik 3.7.19 como plano B; limite fino no gateway |
| Cadeia de fornecimento | Imagens por digest, ações por commit, Grype como segunda opinião ao Trivy, imagens de teste separadas da do produto |
| Cofre trancado depois de um reinício | Porta de saída fechada enquanto isso, aviso imediato, guardiões nomeados e modo escolhido por você |
| Próxima versão do MCP em 30/03/2027 | Aceitar uma versão só pede uma decisão a cada versão nova. A revisão de compatibilidade começa antes da data e decide quando mudar `MCP_ACCEPTED_VERSIONS` e `MCP_UPSTREAM_VERSION` |

### Se o tempo apertar

Saem primeiro: período de experiência, passkey, busca sob demanda e parte automática do alerta dos três sinais. Sem a passkey, as ações de maior risco pedem um login refeito na hora. Em seguida, sai o poder da ronda de fechar a entrada sozinha.

Ficam sempre:

- a porta única de saída, com o "estamos confirmando";
- a confirmação no estilo do Pix;
- o bloqueio de um toque, com o comprovante;
- o mudou-parou;
- a guarda do número de fechamento em outra máquina.

## O que ainda depende de você

- **Decisão 1.** O produto é só para uso interno, com uma organização por instalação? Ou será oferecido a clientes ou publicado como código aberto?
- **Decisão 2.** Aprova o kit oficial em Python no núcleo, com o FastMCP só em exemplos e testes e gateways prontos só como referência?
- **Decisão 3.** Aprova escolher entre Keycloak e Logto por teste, com o critério "funciona inteiro sem licença paga"? Há uma conta da organização a ligar?
- **Decisão 4.** Aprova o Claude Code como primeiro piloto? Qual o segundo: Goose, aplicativo pronto, ou um programa feito com o OpenAI Agents SDK?
- **Decisão 5.** Qual sistema seu será o primeiro, só leitura e com chave por pessoa? Ele já fala a versão 2026-07-28?
- **Decisão 6.** Aprova Caddy e OpenBao? Em produção, o cofre será destravado por partes de chave, por chave em arquivo, por outro OpenBao ou por um serviço de chaves? Um serviço de chaves de fornecedor pede plugin e, em geral, é comercial.
- **Fases 2 a 4.** Que sistema e que ações entram primeiro na Fase 2? Quais servidores de terceiros ligar na Fase 3? Quais equipes adaptam seus sistemas na Fase 4?
- **Prazo e equipe.** Qual o prazo desejado e quantas pessoas trabalharão no produto? Com essas respostas, as etapas ganham datas.
- **Revisão de LGPD.** Quem cuida de proteção de dados deve revisar o texto antes de ele circular fora da equipe.
- **Detalhes de instalação.** Qual servidor de e-mail usar, quais destinos de webhook, onde fica a segunda máquina e onde fica o repositório de backup. Se o repositório for um armazenamento de provedor, ele é peça comercial fora do produto e recebe só cópia cifrada.
- **Arquivos fora do banco.** Aceita que os certificados do portão fiquem em arquivos, fora do PostgreSQL, com cópia cifrada no backup?

## Fontes

Páginas conferidas para este plano, em 18/11/2026, salvo outra data indicada.

### Padrão MCP

- [mudanças da versão 2026-07-28](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/changelog.mdx), [regras básicas e `_meta`](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/index.mdx), [versões e recusa](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/versioning.mdx), [transporte HTTP](https://raw.githubusercontent.com/modelcontextprotocol/modelcontextprotocol/main/docs/specification/2026-07-28/basic/transports/streamable-http.mdx).
- [calendário de versões](https://blog.modelcontextprotocol.io/posts/mcp-release-cycle/), [discussão sobre repetição](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394).

### Kit oficial em Python

- [versões no PyPI](https://pypi.org/pypi/mcp/json), [clientes antigos](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/docs/run/legacy-clients.md), [versões do protocolo](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/docs/protocol-versions.md), [autorização](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/docs/run/authorization.md).
- [clientes OAuth](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/docs/client/oauth-clients.md), [migração](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/docs/migration.md), [middleware](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/docs/advanced/middleware.md), [implantação](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/docs/run/deploy.md).
- [cache](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/docs/client/caching.md), [novidades da 2.x](https://github.com/modelcontextprotocol/python-sdk/blob/main/docs/whats-new.md), [alerta de audiência](https://github.com/advisories/GHSA-w4fh-qvv9-3v23).

### Conformidade e testes

- [suíte no npm](https://registry.npmjs.org/@modelcontextprotocol%2fconformance), [README da suíte](https://github.com/modelcontextprotocol/conformance/blob/main/README.md), [requisitos da 2026-07-28](https://raw.githubusercontent.com/modelcontextprotocol/conformance/main/requirements/2026-07-28.yaml), [integração em kits](https://raw.githubusercontent.com/modelcontextprotocol/conformance/main/SDK_INTEGRATION.md).
- [falhas esperadas do kit](https://raw.githubusercontent.com/modelcontextprotocol/python-sdk/main/.github/actions/conformance/expected-failures.2026-07-28.yml), [MCP Inspector e gerações](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/docs/2026-07-28/tools/inspector/protocol-eras.mdx), [Toxiproxy](https://raw.githubusercontent.com/Shopify/toxiproxy/main/README.md), [Mailpit](https://raw.githubusercontent.com/axllent/mailpit/develop/README.md), [webhook-tester](https://raw.githubusercontent.com/tarampampam/webhook-tester/master/README.md).

### Aplicativos e kits de outras linguagens

- [Claude Code](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md), [OpenAI Agents SDK](https://raw.githubusercontent.com/openai/openai-agents-js/main/packages/agents-core/CHANGELOG.md), [Goose](https://raw.githubusercontent.com/aaif-goose/goose/main/crates/goose/src/agents/mcp_client.rs), [Codex CLI](https://raw.githubusercontent.com/openai/codex/main/codex-rs/features/src/lib.rs).
- [Gemini CLI](https://raw.githubusercontent.com/google-gemini/gemini-cli/main/packages/core/package.json), [kit em TypeScript](https://raw.githubusercontent.com/modelcontextprotocol/typescript-sdk/main/docs/protocol-versions.md).

### Programas e identidade

- [OAuth Client Credentials](https://raw.githubusercontent.com/modelcontextprotocol/ext-auth/main/specification/stable/oauth-client-credentials/2026-07-28/oauth-client-credentials.mdx), [Enterprise-Managed Authorization](https://raw.githubusercontent.com/modelcontextprotocol/ext-auth/main/specification/stable/enterprise-managed-authorization/2026-07-28/enterprise-managed-authorization.mdx), [contas de serviço do Keycloak](https://raw.githubusercontent.com/keycloak/keycloak/main/docs/documentation/server_admin/topics/clients/oidc/proc-using-a-service-account.adoc), [Keycloak e MCP](https://github.com/keycloak/keycloak/blob/main/docs/guides/securing-apps/mcp-authz-server.adoc).
- [versão do Keycloak](https://raw.githubusercontent.com/keycloak/keycloak/main/pom.xml), [recursos de API do Logto](https://raw.githubusercontent.com/logto-io/docs/master/docs/authorization/global-api-resources.mdx), [licença paga do Logto](https://github.com/logto-io/docs/blob/master/docs/logto-oss/self-hosted-license.mdx), [OWASP NHI Top 10](https://raw.githubusercontent.com/OWASP/www-project-non-human-identities-top-10/main/index.md).

### FastMCP

- [PyPI](https://pypi.org/pypi/fastmcp/json), [licença](https://raw.githubusercontent.com/PrefectHQ/fastmcp/main/LICENSE), [FastMCP-CLARO](https://github.com/PrefectHQ/fastmcp/blob/main/docs/deployment/fastmcp-gateway.mdx), [novidades](https://github.com/PrefectHQ/fastmcp/blob/main/docs/getting-started/whats-new.mdx).
- [cliente](https://github.com/PrefectHQ/fastmcp/blob/main/docs/clients/client.mdx), [proxy](https://github.com/PrefectHQ/fastmcp/blob/main/docs/servers/providers/proxy.mdx), [verificador JWT](https://github.com/PrefectHQ/fastmcp/blob/main/fastmcp_slim/fastmcp/server/auth/providers/jwt.py), [tarefas](https://github.com/PrefectHQ/fastmcp/blob/main/docs/servers/tasks.mdx).
- [impressão digital de ferramentas](https://github.com/PrefectHQ/fastmcp/blob/main/docs/servers/tool-fingerprinting.mdx), [política de segurança](https://github.com/PrefectHQ/fastmcp/blob/main/SECURITY.md), [histórico](https://github.com/PrefectHQ/fastmcp/blob/main/docs/changelog.mdx), [alerta GHSA-7xgj-8392-5q4r](https://raw.githubusercontent.com/github/advisory-database/main/advisories/github-reviewed/2026/10/GHSA-7xgj-8392-5q4r/GHSA-7xgj-8392-5q4r.json).
- [fastmcp-slim](https://pypi.org/pypi/fastmcp-slim/json), [fastmcp-tasks](https://pypi.org/pypi/fastmcp-tasks/json).

### Langfuse

- [tag v4.82.0](https://github.com/langfuse/langfuse/tree/v4.82.0), [README](https://github.com/langfuse/langfuse/blob/v4.82.0/README.md), [Docker Compose](https://github.com/langfuse/langfuse/blob/v4.82.0/docker-compose.yml), [chave paga](https://github.com/langfuse/langfuse-docs/blob/main/content/self-hosting/license-key.mdx).
- [ciclo de versões](https://github.com/langfuse/langfuse-docs/blob/main/content/self-hosting/upgrade/versioning.mdx), [telemetria](https://github.com/langfuse/langfuse-docs/blob/main/content/self-hosting/security/telemetry.mdx), [OpenTelemetry](https://github.com/langfuse/langfuse-docs/blob/main/content/faq/all/opentelemetry-logs-metrics-span-links.mdx), [rastros MCP](https://github.com/langfuse/langfuse-docs/blob/main/content/docs/observability/features/mcp-tracing.mdx).

### Observabilidade

- [licença do Grafana 13.3.1](https://github.com/grafana/grafana/blob/v13.3.1/LICENSING.md), [configuração](https://github.com/grafana/grafana/blob/v13.3.1/docs/sources/setup-grafana/configure-grafana/_index.md), [fonte PostgreSQL](https://github.com/grafana/grafana/blob/v13.3.1/docs/sources/datasources/postgres/configure/_index.md), [alertas sobre SQL](https://github.com/grafana/grafana/blob/v13.3.1/docs/sources/datasources/postgres/alerting/index.md).
- [imagem do Grafana](https://hub.docker.com/v2/repositories/grafana/grafana/tags?name=13.3&ordering=last_updated), [partições no PostgreSQL](https://github.com/postgres/postgres/blob/REL_18_STABLE/doc/src/sgml/ddl.sgml), [Collector](https://github.com/open-telemetry/opentelemetry-collector-releases/blob/v0.165.0/distributions/otelcol/manifest.yaml), [logs do Docker](https://github.com/docker/docs/blob/main/content/manuals/engine/logging/configure.md).

### Peças de infraestrutura

- [OpenBao, mudanças](https://raw.githubusercontent.com/openbao/openbao/main/CHANGELOG.md), [OpenBao em PostgreSQL](https://github.com/openbao/openbao/blob/main/website/content/docs/configuration/storage/postgresql.mdx), [modos de destravar](https://github.com/openbao/openbao/blob/main/website/content/docs/concepts/seal.mdx), [chave estática](https://github.com/openbao/openbao/blob/main/website/content/docs/configuration/seal/static.mdx).
- [bibliotecas do OpenBao](https://raw.githubusercontent.com/openbao/openbao/main/website/content/docs/api/libraries.mdx), [Caddy](https://proxy.golang.org/github.com/caddyserver/caddy/v2/@latest), [proxy reverso do Caddy](https://raw.githubusercontent.com/caddyserver/website/master/src/docs/markdown/caddyfile/directives/reverse_proxy.md), [módulo de limite do Caddy](https://github.com/mholt/caddy-ratelimit).
- [Traefik](https://proxy.golang.org/github.com/traefik/traefik/v3/@latest), [opções do uvicorn](https://raw.githubusercontent.com/encode/uvicorn/main/docs/settings.md), [PostgreSQL](https://hub.docker.com/v2/repositories/library/postgres/tags?name=18.&ordering=last_updated), [pgBackRest](https://raw.githubusercontent.com/pgbackrest/pgbackrest/main/README.md).
- [Barman](https://pypi.org/pypi/barman/json), [pgqueuer](https://raw.githubusercontent.com/janbjorge/pgqueuer/main/README.md), [procrastinate](https://raw.githubusercontent.com/procrastinate-org/procrastinate/main/README.md), [Gatus](https://raw.githubusercontent.com/TwiN/gatus/master/README.md).
- [certificado entre o Apigee e o destino](https://github.com/GoogleCloudPlatform/apigee-samples/blob/main/mtls-southbound/README.md), [borda trocável por configuração](https://github.com/google/esmeralda/blob/main/docs/src/2-workloads-and-catalog/01-ingress-gateways.md).

### Bibliotecas Python, no PyPI e no npm

- [uv](https://pypi.org/pypi/uv/json), [hypothesis](https://pypi.org/pypi/hypothesis/json), [testcontainers](https://pypi.org/pypi/testcontainers/json), [httpx2](https://pypi.org/pypi/httpx2/json).
- [pytest-httpx2](https://pypi.org/pypi/pytest-httpx2/json), [starlette](https://pypi.org/pypi/starlette/json), [sqlalchemy](https://pypi.org/pypi/sqlalchemy/json), [psycopg](https://pypi.org/pypi/psycopg/json).
- [pydantic-settings](https://pypi.org/pypi/pydantic-settings/json), [pyjwt](https://pypi.org/pypi/pyjwt/2.14.0/json), [webauthn](https://pypi.org/pypi/webauthn/json), [htmx](https://registry.npmjs.org/htmx.org).
- [opentelemetry-sdk](https://pypi.org/pypi/opentelemetry-sdk/json), [locust](https://pypi.org/pypi/locust/json), [standardwebhooks](https://pypi.org/pypi/standardwebhooks/json), [certifi](https://pypi.org/pypi/certifi/json).

### Não conferido em página

- Páginas de alertas e de versões do GitHub, bloqueadas. Os alertas do FastMCP vieram do arquivo público do OSV; quatro foram conferidos no banco de alertas do GitHub.
- Sites oficiais bloqueados: langfuse.com, gofastmcp.com, grafana.com, openbao.org, caddyserver.com, doc.traefik.io, pgbackrest.org, postgresql.org e osv.dev. A documentação foi lida nos repositórios.
- O ataque de março de 2026 à cadeia de fornecimento do Trivy veio só de resumos de busca.
- A página de provisionamento de painéis do Grafana e o login do Grafana pelo Keycloak ou pelo Logto.
- Se `json_response` deixa a resposta inteira na via 2026-07-28; se a sondagem de versão dá para fazer sem cair no `initialize` antigo.
- Se a opção `--requirements` e o modo de autorização já estão na versão publicada da suíte.
- Se o Logto aceita `private_key_jwt`; em qual versão publicada do Keycloak estão os recursos de MCP.
- Se o `hvac` funciona com o OpenBao 2.7; se o `respx` intercepta o `httpx2`.
- Se a versão 1.60.0 do Goose foi publicada; como ligar a versão nova no Codex CLI.
- Se Cursor, conectores do ChatGPT e Microsoft 365 Copilot falam a 2026-07-28.
- A versão atual do Gatus e se o `pg_partman` vem na imagem oficial do PostgreSQL.
- A documentação oficial do Apigee e o rascunho MWS, como já dizia a proposta.
- Se `Client.call_tool()` refaz voltas sem `elicitation_callback`; se o `OAuthClientProvider` reenvia um pedido depois de um 401. Os dois ficam para "Provar a base".
- Se a linha 1.x do kit fala a 2026-07-28; a licença do `jsonschema`, que vem com o kit.
- A porta e o servidor TLS do pgBackRest e as funções do PostgreSQL citadas (`timeline_id`, `system_identifier`, `pg_current_xact_id`, `pg_snapshot_xmin`), por estarem bloqueados os sites oficiais.
