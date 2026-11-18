# MCP-CLARO — proposta

> Proposta original, de 18/11/2026, guardada como foi aprovada. As decisoes finais e o que mudou depois estao em [decisoes.md](decisoes.md).


Nov 18, 2026 · 

## Resumo em dois minutos

Propomos o MCP-CLARO como um produto independente: o ponto comum por onde aplicativos de IA passam para usar ferramentas de outros sistemas. É um serviço à parte, com tela e login próprios, e não depende de nenhum outro produto. Aplicativos que seguem a versão atual do padrão MCP podem se conectar, depois de habilitados. Sistemas que seguem essa mesma versão podem ser ligados a ele.

**Por que agora.** O padrão MCP, que liga assistentes de IA a outros sistemas, ganhou uma versão final nova em julho de 2026. Juntar vários sistemas atrás de um endereço virou coisa comum: muitos produtos já fazem. Nenhum dos doze produtos que a pesquisa examinou documenta, juntas, três proteções. A primeira é não repetir uma ação quando a conexão cai. A segunda é pedir a uma pessoa, fora do aplicativo, que confirme o pedido exato antes de ele seguir. A terceira é entregar comprovantes que dá para conferir depois. Vários já fazem partes disso.

**O que muda para quem usa.** A pessoa libera uma tarefa com prazo e limite, a "ordem de serviço", em vez de dar acesso a tudo. Antes de cada ação que muda algo, ela confirma na tela do gateway uma ficha com o pedido, como no Pix. Ou aprova de antemão um limite, e o aplicativo trabalha dentro dele. Ela lê um extrato único, em português claro, de todos os aplicativos e sistemas. Quando o gateway não sabe se algo chegou, diz "estamos confirmando" em vez de repetir. Ele confere sozinho nos sistemas que permitem; nos demais, pede que a pessoa confira e registre.

**Como é feito.** Tudo o que o produto usa é de código aberto, roda em Docker e guarda dados só em PostgreSQL. O núcleo é em Python, uma linguagem de programação que a equipe domina. Na frente do gateway fica um portão: no começo, um de código aberto; depois, o Apigee, por configuração. O Apigee é a única peça comercial e é opcional.

**O que é novo de verdade.** Nenhuma peça é inédita. A diferença está em pôr o comando com quem usa, não só com o administrador, e em dizer com franqueza o que cada sistema conectado comprova. A peça mais rara é o comprovante de bloqueio. Depois de um bloqueio, ele mostra o que foi parado, o que já tinha seguido e o que está em conferência.

**O que ele não faz.** O gateway controla só o que passa por ele. Não desfaz o que já seguiu. Não impede que a IA erre ou seja enganada: limita o estrago. No começo, muitos assistentes podem não conectar, porque ainda usam a versão antiga do padrão. Pelo mesmo motivo, parte dos sistemas pode ficar de fora.

**O que pede de você.** Nada foi implementado, e ainda não há prazo nem tamanho de equipe definidos. A proposta lista no fim catorze decisões de quem aprova o produto. A primeira é para quem ele se destina: uso interno, oferta a clientes ou publicação como código aberto. A decisão sobre a geração antiga do padrão já foi tomada: só vale a versão atual, para aplicativos e para sistemas.

## O que é um MCP-CLARO, sem jargão

Um MCP-CLARO é a portaria de um condomínio: decide quem entra, aonde pode ir e anota o que passou por ela.

Assistentes de IA já conseguem usar ferramentas de outros sistemas: ler um relatório, disparar uma rotina, enviar uma mensagem. O MCP (Model Context Protocol) é o padrão aberto que virou a tomada universal dessa conexão. Neste texto, "aplicativo" é o assistente ou outro programa que pede, e "sistema" é quem oferece as ferramentas. "Servidor MCP" é outro nome para sistema. "Portaria" é o próprio gateway: um programa, não uma pessoa. Os demais termos estão no glossário, no fim.

Sem portaria, cada aplicativo ganharia uma chave de cada prédio, e ninguém saberia quem entrou onde. Com uma portaria só, cada visitante recebe um crachá com nome e prazo de validade. O crachá só vale na portaria. As chaves dos prédios ficam com ela, que só abre os prédios combinados.

> Desenho: ponto comum · quem pede, o gateway e quem oferece ferramentas (versao com desenhos no documento compartilhado).

À esquerda está quem pede e à direita quem oferece. A pessoa acompanha tudo por uma tela do próprio gateway.

| Na portaria de um condomínio | No Gateway |
| --- | --- |
| O porteiro confere o documento | Confere qual é o aplicativo e em nome de qual pessoa ele age |
| O crachá só vale na portaria, e ela só abre os prédios combinados | O crachá do aplicativo só abre o gateway, e o aplicativo só enxerga as ferramentas que a pessoa autorizou |
| Há um livro de visitas único | Cada pedido fica registrado: quem, o quê, em qual sistema e com que resultado |
| Encomenda suspeita fica retida | Ação arriscada espera uma pessoa confirmar |
| O crachá pode ser cancelado | A pessoa bloqueia o aplicativo e os próximos pedidos param, em todos os sistemas |

O gateway é um serviço à parte. Tem banco de dados, login e telas próprios, e não é pedaço de nenhum dos sistemas que ficam atrás dele.

## O que o mundo está fazendo em 2026

O MCP mudou de forma em 2026 e "gateway MCP" virou uma categoria lotada, em que o básico é parecido em quase todos os produtos. Esta seção e a seguinte são contexto; quem preferir pode ir direto para "A proposta".

### O padrão ficou mais simples e mais rígido

Desde [dezembro de 2025](https://blog.modelcontextprotocol.io/posts/2025-12-09-mcp-joins-agentic-ai-foundation/) o MCP pertence a uma fundação neutra, a Agentic AI Foundation, ligada à Linux Foundation. A versão em vigor é a de 28 de julho de 2026, que o [blog oficial](https://blog.modelcontextprotocol.io/posts/2026-07-28/) apresenta, em um depoimento, como a mais importante em mais de um ano. A próxima está prevista para 30 de março de 2027 e, daí em diante, uma a cada seis meses.

| O que mudou | Em palavras simples | O que significa para o gateway |
| --- | --- | --- |
| Fim da "sessão" | Cada pedido se apresenta sozinho, com crachá e tudo. O servidor não guarda conversa aberta. | Menos peças para operar. O número de protocolo de cada operação vira o fio da meada. |
| Pergunta no meio do caminho | O servidor responde "preciso de mais uma coisa" e o aplicativo volta com ela. A "coisa" pode ser um link para uma tela do gateway. | É o encaixe padrão para a confirmação humana na tela do gateway. |
| Trabalho demorado | Virou uma extensão opcional, chamada Tasks, ainda em fase beta. | Usar quando o aplicativo suportar e manter a consulta comum para os demais. |
| Cadastro de aplicativos | O cadastro automático antigo foi desaconselhado: ainda funciona, mas tem remoção prevista. O novo usa um endereço web como identidade do aplicativo. | O pré-registro por um administrador é o caminho preferido do padrão. |
| Queda de conexão | Não existe mais "retomar de onde parou". O aplicativo reenvia o pedido. | Repetição vira rotina. Proteger contra efeito duplicado deixa de ser opcional. |

Fontes: [mudanças da versão 2026-07-28](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/changelog.mdx), [calendário de versões](https://blog.modelcontextprotocol.io/posts/mcp-release-cycle/), [lista de versões publicadas](https://github.com/modelcontextprotocol/modelcontextprotocol/releases).

### Os aplicativos ainda não acompanharam

Entre os aplicativos que a pesquisa conferiu, só o Claude Code tem registro público de falar a versão nova por padrão. A única tabela oficial de compatibilidade cobre extensões, é mantida pela comunidade e está defasada. Aplicativos e sistemas só entram no gateway se falarem a versão de julho, sem exceção. Quem só fala a geração antiga recebe uma recusa legível e fica de fora até se atualizar. Como a tabela oficial não basta, o gateway conta essas recusas desde o primeiro dia.

Fontes: [histórico de versões do Claude Code](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md), [tabela de extensões por aplicativo](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/extensions/client-matrix.mdx).

### O mercado já junta servidores

| Produto | Ponto forte | Onde para |
| --- | --- | --- |
| [Cloudflare MCP server portals](https://github.com/cloudflare/cloudflare-docs/blob/production/src/content/docs/cloudflare-one/access-controls/ai-controls/mcp-portals.mdx) | Portal único com regras, prevenção de vazamento e "Code Mode" | Serviço na nuvem do fornecedor; cada chamada tem limite de 60 segundos |
| [AWS Bedrock AgentCore](https://github.com/awslabs/amazon-bedrock-agentcore-samples/tree/main/01-features/07-centralize-and-govern-your-ai-infrastructure/02-policy) | Regras escritas em linguagem formal; tudo negado por padrão | Preso à nuvem da AWS |
| [agentgateway](https://github.com/agentgateway/agentgateway) | Código aberto, da Linux Foundation; roda fora de Kubernetes | Configurado por arquivo; a documentação não cita telas para a pessoa autorizar e conferir |
| [ToolHive](https://github.com/stacklok/toolhive/releases) | Já alinhado à versão de julho; busca de ferramentas embutida | A aprovação humana depende do aplicativo conectado |
| [Docker MCP-CLARO](https://github.com/docker/mcp-gateway/blob/main/docs/security.md) | Isola cada servidor em contêiner e confere assinatura | Um único crachá para todos; a documentação não cita identidade por usuário |
| [IBM ContextForge](https://github.com/IBM/mcp-context-forge) | Um dos códigos abertos com mais recursos, escrito em Python | Também [o que mais acumulou alertas de segurança](https://github.com/advisories?query=%22mcp+gateway%22) em 2026 |
| [Microsoft MCP-CLARO](https://github.com/microsoft/mcp-gateway) | Já exige a versão nova do padrão | Feito para Kubernetes; teve falha grave de autorização em novembro de 2026 |

Os limites da tabela vêm da documentação aberta de cada produto; o que ela não cita pode existir. Juntar servidores atrás de um endereço, com login e lista de ferramentas permitidas, virou mercadoria. É também o que o nosso gateway precisa fazer, e para essa parte existe código aberto a reaproveitar.

A pesquisa abriu as páginas de doze produtos: os sete da tabela acima e mais Obot, Kong, Azure API Management, LiteLLM e Bifrost. Nenhum dos doze documenta de ponta a ponta três coisas: repetição segura com o estado "não sei", confirmação humana do efeito exato fora do aplicativo e comprovantes conferíveis. É nesse espaço que a proposta se apoia. Ele é mais estreito do que parece: TrueFoundry, MintMCP e DigitalOcean, que a rede não deixou abrir, cobrem partes dele pelos resumos de busca, e dois projetos pequenos de código aberto também.

### Menos ferramentas, melhores respostas

Cada ferramenta ocupa espaço na memória de trabalho do assistente. A Anthropic [calculou, em um exemplo,](https://www.anthropic.com/engineering/advanced-tool-use) 58 ferramentas consumindo cerca de 55 mil unidades de texto antes da primeira pergunta. A [documentação da Claude API](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool) registra que a escolha piora acima de 30 a 50 ferramentas.

Um ponto comum junta muitas ferramentas. Mostrar a cada aplicativo só as da sua tarefa, e buscar o resto sob demanda, deixa de ser capricho.

## Onde os gateways de hoje falham

As falhas de 2026 foram, na maior parte, banais: porta sem tranca, crachá aceito sem conferir e botão de "aprovar" apertado sem ler.

| Falha | O que se viu | Resposta desta proposta |
| --- | --- | --- |
| Porta sem tranca | Em 17 de novembro de 2026, o banco de alertas do GitHub listava cerca de [526 alertas revisados com o termo "mcp"](https://github.com/advisories?query=mcp+type%3Areviewed+published%3A%3E2026-01-01) publicados no ano, 86 deles críticos. Em um gateway conhecido, [qualquer crachá inventado abria uma sessão autenticada](https://github.com/advisories/GHSA-7488-6r32-c95q). | Ronda das portas |
| Crachá de outro condomínio | O kit oficial em Python traz [desligada por padrão](https://github.com/advisories/GHSA-w4fh-qvv9-3v23) a conferência de "para quem este crachá foi emitido". | Ronda das portas |
| Um usuário vê dados de outro | Houve falhas em que um cliente [lia e apagava dados de outro](https://github.com/advisories/GHSA-j6r7-6fhx-77wx) no mesmo servidor. | Uma chave por pessoa em cada sistema e ronda das portas |
| Aprovar sem ler | No Claude Code, usuários aprovam [93% dos pedidos de permissão](https://www.anthropic.com/engineering/claude-code-auto-mode). Em um estudo com 50 analistas, as pessoas [seguiram a inclinação do agente em 86% das decisões](https://www.anthropic.com/research/agents-judgment-calls). | Ficha da ação montada pelo gateway e limite por aproximação |
| Repetir e duplicar | O padrão não tem proteção contra repetição. Um [teste público](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394) relatou efeito duplicado em 20 de 20 tentativas após uma resposta perdida. | "Estamos confirmando" |
| Etiqueta que mente | A etiqueta "só leitura" é declarada pelo próprio servidor, [sem fiscalização](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/). Mesmo assim, o Claude a usa para [dispensar a confirmação do usuário](https://claude.com/docs/connectors/building/review-criteria.md). | Cadastro único das ações |
| Ferramenta que muda depois de aprovada | A pesquisa não achou padrão aprovado para lacrar a definição de uma ferramenta. A [proposta ETDI, de 2025,](https://github.com/modelcontextprotocol/python-sdk/pull/845) foi fechada sem ser adotada. | "Mudou, parou" |
| Instrução escondida no conteúdo | A injeção de prompt segue sem solução dentro do modelo. As defesas que mudam o desenho do sistema, em vez de filtrar o texto, ainda são [código de pesquisa](https://github.com/google-research/camel-prompt-injection), não produto, e o próprio repositório avisa que pode ter falhas. | Alerta dos três sinais e sala de visitas |
| O próprio gateway vira alvo | Houve falhas graves em 2026 em pelo menos quatro gateways: LiteLLM, Bifrost, Microsoft MCP-CLARO e ContextForge. Um gateway concentra as chaves de tudo o que fica atrás dele. | Núcleo pequeno, cofre de chaves e vigia de fora |

A conclusão prática é que nenhuma garantia pode depender de a IA "se comportar". Um filtro que acerta 99% das vezes ainda falha várias vezes por dia quando há milhares de chamadas automáticas.

## A proposta: um ponto comum que presta contas

Propomos um serviço independente por onde os aplicativos de IA passam para usar ferramentas de outros sistemas. Ele controla só o que passa por ele. O aplicativo só entra para a tarefa que você liberou. Cada ação que muda algo espera a sua confirmação ou um limite que você aprovou. Você recebe um extrato único, inclusive um "ainda estamos confirmando" quando não há certeza. Nesta seção, "você" é a pessoa que usa o gateway.

Juntar servidores atrás de um endereço o mercado já faz. Aprovação humana e bloqueio de emergência vários gateways também já têm, quase sempre para o administrador. A proposta põe duas coisas em cima disso: o comando com quem usa e a franqueza sobre o que cada sistema comprova.

São três promessas: você decide, você confere e a proteção não depende da IA. Elas chegam em quatro fases.

> Desenho: três promessas · doze recursos · uma base (versao com desenhos no documento compartilhado).

Cada coluna é uma promessa e cada linha é um recurso descrito a seguir. A faixa de baixo sustenta as três.

### Fase 1: olhar

**Ordem de serviço, não chave da casa.** Você não libera "acesso a tudo". Libera uma tarefa com começo, fim e limite, por exemplo "acompanhar as cobranças no sistema financeiro, só olhando, por 30 dias". O aplicativo só recebe a lista de ferramentas dessa tarefa. O ADMIN define quais ordens de serviço existem, e cada pessoa escolhe dentro desse teto.

Quando o prazo acaba, o acesso cai sozinho, com aviso antes. Autorização sem uso fica inativa até a pessoa reativar. Na revisão, a tela já sugere o menor conjunto: "você liberou seis coisas, ele usou duas".

**Vitrine sob medida.** Cada aplicativo enxerga só as ferramentas da sua ordem de serviço. Assim o assistente não se perde nem gasta memória com o que não vai usar. Quando as ferramentas forem muitas, entra uma busca sob demanda.

**Cofre de chaves.** O crachá do aplicativo não é repassado aos sistemas. Quando o sistema tem login por pessoa, cada pessoa entra na página do próprio sistema e autoriza o gateway a usá-lo em nome dela. Dessa autorização sai uma chave, que o gateway guarda trancada no cofre. A autorização pode vencer ou ser cancelada no sistema; aí a tela do gateway pede para renovar. Autorizar um sistema não o libera para todos os aplicativos: cada aplicativo só chega a ele pela ordem de serviço escolhida para aquele aplicativo.

Há sistemas com uma chave única para todos, que não distinguem as pessoas. Esses entram marcados como "chave compartilhada". O ADMIN lista quem pode usar, e eles começam só com leitura. Neles, o gateway controla quem entra, mas não separa os dados: quem está na lista alcança tudo o que a chave única alcança nas ferramentas liberadas. A ficha e o extrato avisam isso.

**Leitura com medida.** Os campos que o cadastro marca como dado pessoal saem mascarados por padrão. O aplicativo vê "CLIENTE\_17" em vez do nome e do e-mail, a menos que a ordem de serviço libere. Quando uma ação usa um desses apelidos, o gateway põe o dado verdadeiro só na hora do envio, nos campos que o cadastro permite. A ficha mostra o dado verdadeiro antes de seguir. Em texto livre, o gateway não consegue achar todo dado pessoal. Cada ordem de serviço tem uma catraca, com um número máximo de leituras por dia. Texto escrito por estranhos sai marcado como tal, para o assistente não tomar aquilo como ordem.

**Extrato único.** Uma tela, como o extrato do banco, mostra em português claro o que cada aplicativo leu, fez ou deixou pendente em todos os sistemas conectados, e o que ficou sem confirmação. São frases-padrão preenchidas com fatos, não redigidas por IA. Leituras aparecem agrupadas. Quem administra recebe um resumo semanal com contagens, sem o conteúdo. Abrir o extrato de outra pessoa pede motivo registrado, e ela fica sabendo.

**Bloqueio de um toque.** Funciona como bloquear o cartão pelo celular: um toque e os pedidos novos do aplicativo de IA param no gateway, para todos os sistemas. Há também a opção de só pausar. O ADMIN pode bloquear qualquer aplicativo, e desativar uma pessoa derruba as autorizações dela.

A partir da fase 2, o bloqueio devolve um comprovante em três partes: o que paramos a tempo, o que já tinha seguido para o sistema e o que ainda está em conferência. O comprovante cobre só o que passou pela porta do gateway. Em sistemas de nível 1 e 2, "já tinha seguido" quer dizer entregue ao sistema, não concluído por ele. Bloquear não apaga o que o aplicativo já leu.

### Fase 2: agir

**Confirmação no estilo do Pix.** Antes de qualquer ação que muda algo, como enviar uma mensagem, criar um registro ou apagar um arquivo, você vê uma ficha montada pelo gateway, não pela IA. Ela diz qual aplicativo pede, em qual sistema, o que será feito e se tem volta. A ficha mostra o pedido que vai seguir, não o que o sistema fará com ele. A justificativa do aplicativo aparece em uma caixa separada, marcada "o aplicativo diz". Sua confirmação vale para aquela ficha e só para ela. Se o pedido mudar, o gateway pergunta de novo.

O aplicativo não tem botão de aprovar: a confirmação só vale na tela do gateway, com o login da pessoa. Um assistente que controle o navegador da pessoa ainda consegue clicar por ela. Por isso as ações de maior risco pedem a digital ou o PIN do aparelho naquele momento. Isso se chama passkey: a digital fica no aparelho, e o gateway recebe só a confirmação. As fichas ficam em uma caixa de pendências, então ninguém precisa clicar em link vindo de um aplicativo. Sem resposta no prazo, nada segue.

**Cancelar antes de sair.** Depois de confirmada, a ação espera um pouco na porta de saída, com um relógio visível. Se a tela disser "cancelado antes de sair", a ação não foi enviada ao sistema, e isso fica registrado. Para o que é urgente existe "seguir agora", que só a pessoa aciona.

**"Estamos confirmando", e não fazemos duas vezes.** Toda ação recebe um número de protocolo, criado pelo gateway quando o pedido chega. Se o aplicativo insistir com esse número, recebe de volta a operação original. Se a resposta se perdeu e ele insistir sem o número, o gateway compara pessoa, aplicativo, ferramenta e conteúdo. Um pedido igual feito pouco tempo depois não segue sozinho. Isso vale mesmo quando ele cabe em um limite por aproximação, explicado logo abaixo. O gateway devolve a operação original, como o banco que avisa "você fez um Pix igual há dois minutos". Para repetir de propósito, a pessoa confirma na tela.

Quando o sistema não responde, o estado é "resultado não confirmado", com o aviso "estamos confirmando; não repita". Se o sistema permite conferir, o gateway confere. Se não permite, quem confirmou a ação decide na tela do gateway, e a decisão fica no extrato. Até a fase 4, todo sistema é desse segundo tipo: o gateway só confere sozinho em sistemas com selo de nível 2 ou 3, explicado mais adiante. Vale a tela do gateway, não o que o assistente escreveu na conversa.

**Limite por aproximação.** Cartão por aproximação não pede senha para o café, mas pede para a compra grande. Você aprova uma regra com limites claros, por exemplo "pode enviar o relatório para mim e para a Ana, até cinco por dia, por 30 dias". Criar ou ampliar um limite pede a digital, como as ações de maior risco. Dentro do limite, o aplicativo trabalha sem interromper você. Fora do limite, volta a ficha. O limite prende a ferramenta, os destinatários, a quantidade e o prazo; não prende o texto, que segue sem ficha. O que seguiu aparece no extrato.

Ação para destinatário novo sai do limite e volta a pedir a sua confirmação. Se um aplicativo pedir confirmações demais, quem para é o aplicativo, não você.

**Período de experiência.** É como um funcionário novo. Nos primeiros dias, tudo o que o aplicativo quer fazer passa pela sua mesa. Depois a tela mostra o resumo real do que ele pediu, do que você aprovou e do que recusou, e você decide o que ele pode fazer sem perguntar.

**Programas que rodam sozinhos.** Um programa sem pessoa na tela tem um responsável com nome. Ele age só dentro de limites que esse responsável aprovou. O que passa do limite espera por ele e vence sem resposta.

### Fase 3: receber servidores de terceiros

**Sala de visitas.** Servidor de terceiros é revisado um a um pelo ADMIN e atendido em um cômodo separado. Como qualquer sistema, ele só entra se falar a versão de julho. Se só falar a geração antiga, o cadastro é recusado com mensagem legível, e a recusa entra na contagem. O gateway só o chama no endereço combinado e não aceita desvio para outro endereço. A chave dele fica separada das chaves dos seus sistemas. O gateway não controla o que esse servidor faz do lado de lá. O assistente lê nomes e descrições escritos no cadastro do gateway, não os do fornecedor. Pedido de senha, link ou confirmação vindo desse servidor não é repassado à pessoa. A resposta dele é tratada como texto escrito por estranhos. Se o servidor mudar a definição de uma ferramenta depois da revisão, mesmo que só o texto, o servidor inteiro fica em quarentena até nova revisão.

**Alerta dos três sinais.** Um e-mail ou uma página escrita por um estranho pode trazer ordens escondidas que a IA obedece. Como ponto comum, o gateway enxerga o caminho entre sistemas: leu aqui, quer enviar ali. Os sinais são somados por pessoa e por aplicativo, em uma janela de tempo, mesmo em pedidos diferentes. Quando a soma junta três sinais (leu texto de fora, viu dado privado seu e quer enviar algo para fora), o limite por aproximação é suspenso e a ficha mostra o motivo. Isso vale para o que passa pelo gateway. Não controla o que o aplicativo faz por outros caminhos.

### Fase 4: selos de nível 2 e 3

Até a fase 4, todo sistema entra com o selo de nível 1. Depois, sistemas que quiserem provar mais seguem um contrato técnico público e ganham o nível 2 ou 3. É uma lista de regras para programas, não um contrato jurídico. A seção "Como um sistema entra no gateway" descreve os três níveis.

### Como você sabe que isso é verdade

| Mecanismo | O que faz |
| --- | --- |
| Cadastro único das ações | Cada ferramenta de cada sistema tem um só cadastro, revisado pelo ADMIN: o que lê, o que muda e se tem volta. Ele gera os rótulos, as permissões e a ficha. Ferramenta que ainda não passou pela revisão não aparece para nenhum aplicativo. Ferramenta que cria algo que age depois, como um agendamento, é tratada como ação de risco alto. Toda mudança no cadastro fica registrada. |
| Mudou, parou | O gateway compara a definição de cada ferramenta com a que foi revisada. Se ela passa a declarar ou pedir mais do que você autorizou, a autorização deixa de valer para ela até alguém ver o "antes e depois". Nos seus sistemas, ajuste de texto não para nada. Em servidor de terceiros a regra é mais dura: qualquer mudança leva à quarentena. |
| Travas no banco de dados | As travas ficam no banco de dados do gateway. Uma ação só segue se existir uma confirmação com o mesmo código de conferência ou um limite aprovado em que ela caiba. Cada confirmação libera uma operação só. Um pedido igual feito pouco tempo depois não abre operação nova: recebe de volta a original, mesmo que caiba em um limite. |
| Ronda das portas | A cada poucos minutos o gateway tenta entrar em si mesmo com crachá falso, vencido, de outro condomínio e de outra pessoa. Se uma tentativa passar duas vezes seguidas, a entrada se fecha por etapas, primeiro para ações e depois para leituras, e a equipe apura. Um vigia em outra máquina repete a ronda e avisa também se o gateway ficar mudo. |
| Fechamento | Várias vezes ao dia o gateway confere, no histórico real, se tudo o que seguiu tinha confirmação idêntica ou cabia em um limite, se nenhum pedido igual seguiu duas vezes sem nova confirmação e se nada seguiu depois de um bloqueio. O resultado vira um número de fechamento, calculado a partir do histórico e guardado na máquina do vigia. Se alguém alterar um trecho do histórico já fechado, a conta deixa de bater com o número guardado no vigia. Isso aparece quando a conta é refeita, no fechamento seguinte ou em uma auditoria. Não cobre alteração feita antes do fechamento, nem por quem tiver acesso às duas máquinas. |
| Provas antes de ligar | Cada fase só liga depois de passar no teste oficial do padrão, que confere só as regras de comunicação. Da fase 2 em diante, cada fase precisa passar também em testes públicos, feitos por terceiros, em que a resposta é perdida de propósito. O primeiro é o teste citado na discussão pública sobre repetição no MCP. Há ainda um ensaio nosso, em que a IA é trocada por um dublê mal-intencionado, que tenta o que é proibido. |

### O dever de casa

- Tudo é de código aberto, roda em Docker e guarda dados só em PostgreSQL. A única exceção é o Apigee, opcional.
- Com os aplicativos e com os sistemas, falamos só a versão atual do padrão, a de julho. Aplicativo ou sistema que só fala a geração antiga recebe uma recusa legível e entra em uma contagem. Por isso, no começo, muitos assistentes podem não conectar: entre os que a pesquisa conferiu, só o Claude Code tem registro público de falar a versão nova por padrão. No caso dos sistemas, a recusa vem já no cadastro, e parte deles fica de fora até se atualizar. É o caso dos servidores feitos com a linha 1 dos kits oficiais nas linguagens TypeScript e Rust. Essa linha dos kits não fala a versão de julho.
- Cada aplicativo tem crachá próprio, que só abre o gateway e que o ADMIN habilita um a um.
- Onde o sistema tem chave por pessoa, cada pessoa só alcança o que é dela, porque o gateway chama o sistema com a chave daquela pessoa. Com chave compartilhada, essa separação não existe.
- Há limite de pedidos por aplicativo, para um aplicativo não travar os demais. São limites de contagem, não de dinheiro.
- Não depende de nenhum outro produto. Qualquer sistema que fale a versão de julho do MCP pode ser ligado, sem tratamento especial.

### Quanto disso é novo

Nenhuma peça é inédita: uma revisão em 18 de novembro de 2026 achou precedente, ao menos parcial, para cada uma. As letras A, B e C abaixo medem novidade. Não têm relação com os selos de nível 1, 2 e 3.

| Letra | Significado | Recursos |
| --- | --- | --- |
| A | Não achamos precedente | Nenhum |
| B | Existe fora do MCP ou em rascunho; não achamos em gateway MCP | Comprovante de bloqueio em três partes, a peça mais rara; ordem de serviço por tarefa, com prazo que vence sozinho; extrato único para a pessoa; ciclo completo do "estamos confirmando"; ficha montada pelo gateway, com o texto do aplicativo à parte; cancelar antes de sair; limite definido pela pessoa, com destinatários e prazo; ronda com poder de fechar a entrada; fechamento que confere se tudo o que seguiu tinha confirmação ou limite; selo sobre o que o sistema comprova |
| C | Já existe em algum gateway ou toolkit, às vezes em beta ou em projeto pequeno; a proposta é fazer bem-feito | Juntar servidores atrás de um endereço; a pessoa escolher o que o aplicativo enxerga; seguir sem perguntar abaixo de um teto; vitrine sob medida; confirmação presa ao conteúdo exato; "mudou, parou"; período de experiência; alerta dos três sinais; leitura com medida; sala de visitas; cofre de chaves; portão trocável por configuração |

O diferencial mais defensável para uma equipe pequena é a porta única de saída com estado franco. Ela funciona com qualquer sistema de nível 1 e pode ser conferida por testes públicos de resposta perdida, feitos por terceiros.

Os precedentes mais próximos são o [toolkit de governança de agentes da Microsoft](https://github.com/microsoft/agent-governance-toolkit), o [Tenuo](https://github.com/tenuo-ai/tenuo), o rascunho [AAuth](https://github.com/dickhardt/AAuth), o [warrantd-gateway](https://pypi.org/project/warrantd-gateway/), o [Tracekit](https://github.com/cygnux-labs/tracekit), um [rascunho comunitário de níveis de segurança](https://github.com/mcp-security-standard/mcp-server-security-standard) e a [discussão pública sobre repetição no MCP](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394).

## Como funciona na prática

Seis papéis participam, e a palavra final é sempre a tela do gateway, não a conversa com o assistente.

| Quem | O que decide ou faz |
| --- | --- |
| ADMIN da organização | Habilita cada aplicativo e cada sistema, revisa o cadastro das ações, define quais ordens de serviço existem e pode bloquear qualquer um |
| Pessoa | Escolhe a ordem de serviço, confirma ações, decide o que fazer com um resultado não confirmado, consulta o extrato e bloqueia os aplicativos dela |
| Aplicativo | Pede, em nome da pessoa. Precisa falar a versão de julho do padrão. Não aprova nada e só enxerga o que a ordem de serviço permite |
| Sistema conectado | Oferece as ferramentas. Pode ser seu ou de terceiros. Como o aplicativo, precisa falar a versão de julho do padrão. Entra com um selo de nível |
| Provedor de login | Confirma quem é a pessoa e emite o crachá do aplicativo |
| Equipe que opera o gateway | Mantém o serviço no ar, roda as provas e apura quando a ronda acusa algo |

Um exemplo inventado, do começo ao fim: Marina é gestora de operações e quer ajuda de um assistente de IA com as cobranças da empresa.

1. O ADMIN habilita o aplicativo do assistente e o sistema de cobrança, que é da própria empresa.
2. Marina entra no gateway com a conta da empresa, conecta o assistente e escolhe a ordem de serviço "acompanhar as cobranças, só olhando, por 30 dias". Na página do sistema de cobrança, ela autoriza o gateway a consultá-lo em nome dela.
3. Marina pergunta ao assistente o que venceu ontem. O assistente consulta o sistema de cobrança pelo gateway e vê as cobranças, com os clientes como "CLIENTE\_17". No extrato de Marina aparece que ele consultou as cobranças daquele dia.
4. Mais tarde, com as fases 2 e 3 já liberadas, o ADMIN revisa e habilita um serviço de e-mail, de terceiros. Marina escolhe uma nova ordem de serviço, que permite enviar lembretes por ele.
5. O assistente propõe enviar lembretes a três clientes. Marina encontra uma ficha na caixa de pendências: qual aplicativo pede, em qual sistema, para quem, o quê e o aviso "não tem volta depois de sair". A ficha mostra a ela os destinatários verdadeiros, que o assistente não vê. Ela confirma com a digital.
6. A ação espera na porta de saída. Marina nota um destinatário errado e cancela. A tela mostra "cancelado antes de sair".
7. Ela pede a correção ao assistente e confirma a nova ficha. Passado o período de experiência, cria um limite: lembretes só para esses três clientes, com teto diário e prazo.
8. A conexão cai no meio de um envio e o assistente insiste. O gateway devolve a mesma operação, com o mesmo número de protocolo, em vez de enviar de novo.
9. Um dos envios fica sem resposta do serviço de e-mail, que tem selo de nível 1. O extrato mostra "resultado não confirmado; este sistema não permite conferir". Marina confere por fora e registra que chegou.
10. No fim do projeto, Marina bloqueia o aplicativo. Recebe o comprovante com o que foi parado a tempo, o que já tinha seguido e o que ainda está em conferência.

## Como um sistema entra no gateway

Um sistema que já fala a versão de julho do MCP entra sem mudar nada nele. O trabalho fica do lado do gateway, e quem quiser provar mais ganha um selo mais alto.

Para ligar um sistema, o ADMIN primeiro o cadastra. Nesse passo, o gateway testa qual versão do padrão o sistema fala. Sistema que só fala a geração antiga tem o cadastro recusado e fica de fora até passar a falar a versão atual. O ADMIN recebe uma recusa legível, e a recusa entra em uma contagem. Depois que o sistema é aceito, o ADMIN cadastra e revisa cada ferramenta e registra o tipo de chave. Com chave por pessoa, cada um autoriza na página do sistema, e o gateway precisa estar cadastrado nesse sistema. Com chave compartilhada, o sistema não distingue as pessoas, e a ficha e o extrato dizem isso.

O gateway faz muito sozinho, mas não enxerga o que um sistema faz depois de receber a ação. Em vez de fingir que enxerga, ele mostra um selo por sistema.

| Selo | O que o sistema precisa fazer | O que passa a valer |
| --- | --- | --- |
| Nível 1: conectado | Nada além de falar a versão de julho do MCP | Tudo o que o gateway faz sozinho: crachá, ordem de serviço, leitura com medida, ficha, limite, cancelar antes de sair, bloqueio, extrato do que passou por ele e não repetir a mesma ação |
| Nível 2: confere | Aceitar o número de protocolo e responder a uma consulta sobre ele | "Estamos confirmando" se resolve com a resposta do próprio sistema, e repetir com o mesmo número não duplica nos casos testados |
| Nível 3: presta contas | Informar o que fez depois de receber a ação, obedecer ao bloqueio e passar nos testes de escrita segura que a equipe do gateway roda | O comprovante de bloqueio e o extrato passam a incluir o que o sistema informa ter feito |

O selo aparece na ficha e no extrato. Em um sistema de nível 1, a ficha avisa: "este sistema não informa o que faz depois de receber a ação".

O contrato é público, pequeno e igual para todos. O selo só sobe com teste que a equipe do gateway roda, e mostra a data desse teste. Ele tem validade e volta ao nível 1 se o teste vencer ou falhar. O selo diz como o sistema respondeu no teste; não garante o que ele fará depois.

Os níveis 2 e 3 aproveitam requisitos de um rascunho independente de escrita segura, o MWS versão 0.9, de 16 de novembro de 2026. "Escrita" é toda ação que muda algo. O rascunho é de uma empresa de auditoria, não do projeto MCP, e ainda pode mudar. Ele não define níveis nem selos, e não diz quando um sistema está aprovado. A ferramenta de teste dele ainda é uma prévia e cobre parte dos requisitos. O selo e o critério são do gateway, e os níveis 2 e 3 pedem mais do que o rascunho cobre.

### O que o gateway não delega

- O crachá do aplicativo não é repassado ao sistema.
- O rótulo que o servidor declara sobre si, como "só leitura" ou "pode repetir", não vale sem revisão.
- A descrição que o assistente lê é a do cadastro do gateway, não a do fornecedor.

## Por dentro, em linguagem simples

Propomos um serviço só, com núcleo em Python, cinco peças e um vigia em outra máquina.

> Desenho: caminho de um pedido · seis etapas e quem participa de cada uma (versao com desenhos no documento compartilhado).

O caminho desce pela esquerda; à direita está quem participa de cada etapa. Leituras terminam na etapa 4. Ações que mudam algo seguem até a etapa 6.

| Peça | O que é | O que depende dela |
| --- | --- | --- |
| Cadastro único das ações | Lista versionada das ferramentas de cada sistema, com o que cada uma lê, o que muda e se tem volta | Rótulos, permissões, ficha, vitrine e "mudou, parou" |
| Motor de regras | Decide cada pedido por regras fixas, sem IA | Ordem de serviço, limite por aproximação e alerta dos três sinais |
| Porta única de saída | Fila no PostgreSQL por onde passa toda ação antes de seguir para um sistema | Confirmação, cancelar antes de sair, comprovante de bloqueio e "estamos confirmando" |
| Cofre de chaves | Guarda trancada das chaves de cada pessoa para cada sistema | Chave por pessoa e sala de visitas |
| Telas da pessoa | Ordens de serviço, pendências e extrato, no próprio gateway | Tudo o que a pessoa decide e confere |
| Vigia de fora | Contêiner em uma segunda máquina, com PostgreSQL próprio | Ronda, número de fechamento e alarme de silêncio |

### O que sustenta a porta de saída

Um único programa, o despachante, fala com os sistemas e abre o cofre. Ele só envia uma ação a partir de uma linha da fila liberada por confirmação ou por limite, e grava "enviando" antes de chamar. Cancelar, bloquear e enviar disputam a mesma linha, então só um deles acontece. O despachante confere o bloqueio no mesmo passo em que grava "enviando". O que ainda não estava marcado entra em "paramos a tempo". O despachante tenta uma vez. Sem resposta, a ação vira "resultado não confirmado" e não é reenviada. Um programa simples em Python sobre o PostgreSQL basta; não é preciso um motor de execução à parte.

### Por que Python serve

A equipe domina Python, e o kit oficial em Python está no nível mais alto de suporte e fala a versão de julho desde o lançamento. Ele traz o que um gateway precisa nas duas pontas: um servidor e um cliente. A parte que atende os aplicativos, porém, também aceita a geração antiga e não oferece um jeito de recusá-la. O gateway põe na frente dela um filtro próprio e pequeno, o porteiro de versão, com testes, que faz essa recusa com mensagem legível. A pesquisa achou outros cuidados. Três pesam mais. O primeiro: a conferência de "para quem este crachá foi emitido" vem [desligada por padrão](https://github.com/advisories/GHSA-w4fh-qvv9-3v23) e precisa ser ligada e testada. O segundo: a extensão Tasks [constava como não implementada](https://github.com/modelcontextprotocol/python-sdk/releases) nas notas da versão 2.2, e as da 2.3 não tocam no assunto. Por isso o caminho principal é a consulta por número de protocolo. O terceiro cuidado é com a parte do kit que chama os sistemas. Ela teve duas falhas em 2026: um sistema conectado conseguia fazê-la buscar endereços ou reenviar cabeçalhos, os dados que acompanham cada pedido. Por isso a ligação com os sistemas usa versão atualizada do kit e só fala com o endereço combinado.

### Construir ou reaproveitar

Recomendamos fazer a ligação com os sistemas usando o kit oficial. Um gateway pronto no meio, como [agentgateway](https://github.com/agentgateway/agentgateway), [ToolHive](https://github.com/stacklok/toolhive) ou [IBM ContextForge](https://github.com/IBM/mcp-context-forge), tiraria da porta única o controle do envio. Traria também outro cofre e outro cadastro de pessoas para manter. Os três ficam como referência.

Vale avaliar o FastMCP, biblioteca de código aberto em Python que já junta servidores. Segundo a revisão de engenharia, do jeito que vem ele repassa o crachá do aplicativo adiante, o que o padrão proíbe; isso tem de ser desligado. A mesma revisão contou pelo menos seis alertas de segurança dele em 2026. Largura demais custa caro: o IBM ContextForge, um dos projetos com mais recursos, foi também o gateway que mais acumulou alertas de segurança em 2026.

## O portão da frente: próprio agora, Apigee depois

O gateway nasce com um portão próprio, de código aberto, e funciona inteiro com ele. O Apigee é opcional e entra depois, quando estiver disponível. Um ajuste de configuração, chamado parâmetro, escolhe o portão: o próprio, que vem ligado de início, ou o Apigee. Para trocar, a equipe muda os parâmetros listados em "O que muda por configuração" e faz o endereço público passar a levar ao Apigee. Nenhum código é reescrito.

> Desenho: dois portões · o começo sem Apigee e o depois com Apigee (versao com desenhos no documento compartilhado).

A linha de cima é o começo e a de baixo é o depois. Nos dois casos os aplicativos usam o mesmo endereço e o mesmo crachá.

| O que | Portão próprio | Portão Apigee |
| --- | --- | --- |
| Quem atende a internet | Caddy, de código aberto, em Docker | Apigee X, na nuvem do Google |
| Quem confere o crachá | O gateway | O gateway. O Apigee pode fazer uma primeira conferência, desligada por padrão |
| Limite de pedidos | Grosso no portão, fino no gateway | Grosso no Apigee, fino no gateway |
| Porta dos fundos | Só o portão próprio consegue chamar o gateway | O portão próprio continua no lugar e só aceita o Apigee, que se identifica com certificado |
| Quem emite o crachá | O provedor de login de código aberto | O mesmo. O Apigee não emite crachá |

### O que muda por configuração

Trocar de portão é mudar parâmetros, sem código novo.

| O que se escolhe | No começo | Com o Apigee | Nome sugerido |
| --- | --- | --- | --- |
| Qual portão atende a internet | Portão próprio | Apigee | `EDGE_MODE` |
| Quem o portão próprio aceita | Conexões vindas da internet; o crachá é conferido pelo gateway | Só o Apigee, com certificado | `EDGE_ORIGIN_AUTH` |
| Primeira conferência do crachá no Apigee | Não se aplica | Desligada; pode ser ligada | `EDGE_TOKEN_PRECHECK` |
| Forma da resposta | Inteira | Inteira; em fluxo só depois de testada | `MCP_RESPONSE_MODE` |
| Endereço público | Fixo na configuração | O mesmo | `PUBLIC_BASE_URL` |

Os nomes são sugestão para a equipe técnica. O parâmetro da troca é o da primeira linha. O da segunda acompanha o primeiro: ao escolher o Apigee, o portão próprio passa a aceitar só o Apigee, e o gateway não liga nesse modo sem o certificado. As outras três linhas não precisam mudar na troca. Nenhum parâmetro muda quem confere o crachá: nos dois modos, é o gateway.

### O que vale nos dois modos

- O endereço público fica escrito na configuração e não muda. Se mudasse, os crachás e as autorizações já dadas deixariam de valer.
- Ordens de serviço, confirmação, porta única de saída, cofre de chaves, extrato e selos ficam no gateway. Pelos resumos de busca, o Apigee tem recursos próprios para MCP. Eles ficam desligados: a lista de ferramentas tem um dono só.
- Os dois portões são configurados para não alterar o pedido nem repeti-lo por conta própria. O Caddy já vem sem repetir pedidos. O Traefik, plano B do portão próprio, também. No Apigee, a pesquisa não confirmou isso. Por isso os dois cuidados, não alterar e não repetir, entram na prova da troca.
- Nenhuma chamada fica pendurada. Passou do tempo, o gateway devolve o número de protocolo, para que o relógio do portão não corte uma ação no meio.
- As respostas saem inteiras por padrão. Resposta em fluxo contínuo só liga depois de testada nos dois portões.

### Como a troca é provada

Antes de trocar, a mesma bateria roda nos dois portões: teste oficial do padrão, ronda com crachás falsos, tentativa de entrar pela porta dos fundos, comparação das respostas e conferência de que o pedido chega sem alteração e sem repetição. Enquanto o Apigee não chega, o modo é ensaiado com um dublê: um segundo proxy de código aberto no papel de portão externo. Voltar atrás é desfazer os mesmos parâmetros e apontar o endereço público de volta para o portão próprio.

### Avisos sobre o Apigee

- O Apigee é um serviço comercial do Google. É a única peça fora da regra do código aberto, e por isso é opcional.
- "Portão Apigee" quer dizer Apigee X, na nuvem do Google. Pelos resumos de busca, a variante instalada na casa do cliente exige Kubernetes; por isso a recomendação é deixá-la de fora. Falta conferir na documentação oficial.
- No modo Apigee, crachás e conteúdo passam pela infraestrutura do Google. Isso entra na revisão de proteção de dados.
- Parte do que se sabe do Apigee veio de resumos de busca, porque a documentação oficial estava bloqueada. Tempos de espera, tamanhos, repetição de pedidos e fluxo contínuo serão conferidos com o Apigee real antes da troca.

Fontes: [Apigee na frente de um servidor MCP](https://github.com/GoogleCloudPlatform/apigee-samples/issues/465), [certificado entre o Apigee e o destino](https://github.com/GoogleCloudPlatform/apigee-samples/blob/main/mtls-southbound/README.md), [conferência de crachá de provedor externo](https://github.com/GoogleCloudPlatform/apigee-samples/blob/main/authorize-idp-access-tokens/README.md), [borda trocável por configuração](https://github.com/google/esmeralda/blob/main/docs/src/2-workloads-and-catalog/01-ingress-gateways.md).

## O que o produto usa

Tudo o que o produto usa é de código aberto, roda em Docker e guarda dados só em PostgreSQL. A única exceção é o Apigee, que é opcional.

| Papel | Peça | Licença |
| --- | --- | --- |
| Núcleo do gateway | Código próprio em Python, sobre o kit oficial do MCP | MIT (kit oficial) |
| Banco de dados | PostgreSQL, o único do produto | PostgreSQL License |
| Provedor de login | Keycloak ou Logto | Apache-2.0; MPL-2.0 |
| Portão próprio | Caddy, com um módulo não oficial de limite de pedidos; Traefik como plano B | Apache-2.0 (Caddy e módulo); MIT (Traefik) |
| Cofre de chaves | OpenBao, guardando em PostgreSQL | MPL-2.0 |
| Porta única de saída | Fila no PostgreSQL; Temporal como opção | MIT (Temporal) |
| Motor de regras | Regras em tabela no PostgreSQL; Cedar ou OPA como opção | Apache-2.0 |
| Medições | Contagens em tabelas no PostgreSQL, com painéis no Grafana; OpenTelemetry Collector como opção, para enviar medições a uma ferramenta que a empresa já tenha | AGPL-3.0 (Grafana); Apache-2.0 (OpenTelemetry Collector) |
| Vigia de fora | Contêiner próprio e PostgreSQL em uma segunda máquina | Código próprio |
| Ligação com os sistemas | Kit oficial do MCP, com o FastMCP em avaliação | MIT (kit oficial); Apache-2.0 (FastMCP) |
| Portão Apigee, opcional | Apigee X | Proprietário |

As licenças foram conferidas no arquivo de licença de cada repositório em 18 de novembro de 2026, com uma exceção: a do FastMCP veio do arquivo de configuração do projeto e falta conferir no arquivo de licença. Há duas ressalvas. O Logto reserva alguns recursos a uma licença paga. O Grafana usa a licença AGPL-3.0, com obrigações mais fortes que as demais. A proposta o usa sem alterações, com os painéis guardados no PostgreSQL. Se o produto for oferecido a clientes ou distribuído, essa licença pede parecer jurídico antes. Três peças conhecidas ficam de fora: o HashiCorp Vault, porque a licença dele não é de código aberto, e o Redis e o Prometheus, porque guardam dados fora do PostgreSQL.

O produto roda em uma máquina, com Docker, mais uma máquina pequena para o vigia. Nenhuma das duas exige Kubernetes.

## Roteiro de entrega

A primeira entrega é só leitura, e cada fase só abre depois de passar nas próprias provas.

> Desenho: roteiro · cinco etapas com prova e um horizonte (versao com desenhos no documento compartilhado).

Leia da esquerda para a direita, linha por linha. Nenhuma etapa tem data, e a última caixa reúne ideias em estudo, que esta proposta não detalha. Além das condições abaixo, toda fase repete o teste oficial e o ensaio do dublê mal-intencionado. Da fase 2 em diante, repete também os testes públicos de resposta perdida.

| Passo | O que precisa estar comprovado antes |
| --- | --- |
| Abrir a fase 1 | Dois aplicativos-piloto conectando de verdade, na versão de julho do padrão; um sistema seu conectado, com chave por pessoa no cofre; teste oficial do padrão passando pelo portão próprio; crachá de outro condomínio recusado; crachá do aplicativo não repassado ao sistema; uma pessoa não alcança o que é de outra; ferramenta alterada de propósito para pedir mais perde a autorização até nova revisão; backup restaurado e lido, com os aplicativos voltando pausados até cada pessoa reativar; ronda das portas no ar; aplicativo e sistema que só falam a geração antiga são recusados com mensagem legível e entram na contagem |
| Abrir a fase 2 | Resposta perdida de propósito não duplica nada; o que a ficha mostra é o que segue; "cancelado antes de sair" sem nenhuma chamada registrada; bloqueio no meio de uma fila cheia: nada segue depois dele, e o comprovante bate com o histórico; fechamento calculado e guardado na máquina do vigia; backup restaurado com ações na fila: a porta de saída volta fechada e nada segue sem nova confirmação; telas entendidas por pessoas sem formação técnica |
| Abrir a fase 3 | Cada servidor de terceiros revisado; sala de visitas isolada; provas de crachá não repassado e de chave por pessoa repetidas com um servidor de terceiros |
| Abrir a fase 4 | Contrato publicado, com os requisitos do rascunho MWS conferidos na fonte; testes rodando por sistema; selo só sobe com teste aprovado e tem validade |
| Trocar para o portão Apigee, em qualquer fase | Tipo de Apigee e limites de tempo e de tamanho conferidos na documentação oficial; a mesma bateria passa pelos dois portões; chamada direta, sem passar pelo Apigee, é recusada; pedido que passa pelo Apigee chega sem alteração e sem repetição; resposta em fluxo e tempos de espera conferidos com o Apigee real |

Antes da fase 1, os candidatos a piloto passam por um "simulado do dia ruim". O gateway provoca de propósito os momentos difíceis, como resposta perdida e pedido de confirmação, e observa se o aplicativo se comporta. Candidato que só fala a geração antiga já é recusado nesse simulado, com mensagem legível, e entra na contagem.

O roteiro não tem datas porque ainda não há prazo nem tamanho de equipe definidos. A ordem entre as fases 3 e 4 pode se inverter, conforme os sistemas que você quiser ligar primeiro.

## Riscos, limites e o que não prometemos

A proposta limita o estrago; não elimina o risco.

### O que isto não faz

- Não recolhe o que um aplicativo autorizado já leu. Bloquear vale daqui para a frente.
- Não vê o que o aplicativo faz por outros caminhos, fora do gateway. Também não liga aplicativo nem sistema que só fale a geração antiga do padrão: eles recebem uma recusa legível e ficam de fora até se atualizar.
- Não vê o que um sistema faz depois de receber a ação. Nos níveis 2 e 3, mostra o que o próprio sistema informa, e ele pode informar errado.
- Não percebe se um sistema muda de comportamento sem mudar o que declara.
- Não desfaz o que já seguiu. Depois disso só existe conserto, quando existe.
- Não resolve a injeção de prompt. Limita o que uma IA enganada consegue causar. Um caminho continua aberto: a IA enganada pode pôr dados seus no texto de uma consulta a um servidor de terceiros, e eles saem por ali. O gateway não impede isso, porque leituras seguem sem ficha e o alerta dos três sinais só age sobre ações.
- Não impede que alguém com controle total da máquina do gateway use as chaves do cofre, leia o que passa ou deixe de registrar. Isso vale para um invasor e para quem opera o gateway. Se acontecer, o remédio é cancelar as chaves em cada sistema.
- Não distingue você de quem usa a sua conta e o seu aparelho. Isso inclui um assistente de IA que controle o seu navegador. Também não distingue um aplicativo instalado no computador de outro programa, na mesma máquina, que copie a identidade dele. O Claude Code, piloto recomendado, é um aplicativo instalado no computador e, por isso, está exposto a esse risco.
- Não examina, no portão, o conteúdo do que passa. Máscara, selos e extrato são trabalho do gateway.
- Não é parecer jurídico nem selo de conformidade com a LGPD. O texto precisa de revisão de quem cuida de proteção de dados.

### Se o gateway cair

Os aplicativos perdem as ferramentas até ele voltar, e os sistemas seguem funcionando por conta própria. As ações já confirmadas que esperavam na porta de saída ficam guardadas no banco de dados. Na volta, o que estava "enviando" sem resposta vira "resultado não confirmado", em vez de ser reenviado.

Restaurar um backup é outro caso, porque o banco de dados volta a um ponto anterior. Aí a porta de saída volta fechada e os aplicativos voltam pausados. Reabrir a porta de saída é decisão humana registrada. O que estava na fila vira "resultado não confirmado", e nada segue sem nova confirmação. Cada pessoa reativa os seus aplicativos, porque um bloqueio feito depois do backup pode ter se perdido. O extrato registra o intervalo perdido.

### Riscos da própria proposta

| Risco | Como a proposta trata |
| --- | --- |
| Ponto único de falha | Backup ensaiado, com a porta de saída fechada na volta; vigia em outra máquina; sistemas que seguem funcionando por conta própria |
| Alvo preferido | O gateway guarda as chaves de todos os sistemas. Cofre separado, uma chave por pessoa e por sistema, ronda e núcleo pequeno |
| Mercado lotado | Juntar servidores, aprovação humana e bloqueio já não diferenciam. O esforço próprio vai para a linha B da tabela "Quanto disso é novo", a começar pela porta única de saída |
| Dois portões para manter | A mesma bateria de provas roda nos dois; o portão Apigee só liga depois dela |
| Dados passando pelo Google no modo Apigee | Entra na revisão de proteção de dados antes da troca |
| Confirmação vira carimbo | Limite por aproximação, teto de interrupções e contagem de confirmações dadas rápido demais |
| Limite usado por uma IA enganada | O limite não prende o texto, que segue sem ficha. Destinatários nomeados, teto diário baixo e extrato do que seguiu; a partir da fase 3, também o alerta dos três sinais |
| Alarme falso fecha a entrada | A ronda só fecha com falha repetida e com o teste de controle em dia; reabrir é decisão humana registrada |
| Uma ação classificada errado | Cadastro revisado e com mudanças registradas; mudar a classe exige nova revisão; dublê mal-intencionado antes de cada fase |
| Escopo grande para a equipe | Núcleo pequeno, uma organização por instalação e a ordem de corte abaixo |
| Padrão em movimento e versão única | Tasks está em beta; eventos e crachá preso a uma chave ainda não são padrão; uma revisão de compatibilidade por semestre. Como o gateway aceita uma versão só, parte dos aplicativos e dos sistemas deve ficar de fora no começo. Quem fica de fora recebe uma recusa legível e entra na contagem desde o primeiro dia. A próxima versão do padrão está prevista para 30 de março de 2027. A revisão de compatibilidade feita antes dessa data decide quando o gateway passa para ela |
| Fatos que a pesquisa não conferiu em página | Estão separados na lista de fontes. Três pontos se apoiam neles. Dois serão conferidos antes de valer: a escolha e o comportamento do Apigee, antes da troca de portão; e os requisitos do rascunho MWS, antes da fase 4. O terceiro é a comparação com os produtos comerciais que não abriram, que o texto já usa e marca como vinda de resumos de busca |

Se o tempo apertar, saem primeiro o período de experiência, a passkey, a busca sob demanda da vitrine e a parte automática do alerta dos três sinais. Sem a passkey, as ações de maior risco pedem um login refeito na hora. Essa troca protege menos contra um assistente que controle o navegador da pessoa. Em seguida, sai o poder da ronda de fechar a entrada sozinha.

Ficam sempre a porta única de saída com o "estamos confirmando", a confirmação no estilo do Pix, o bloqueio de um toque com o comprovante, o "mudou, parou" e a guarda do número de fechamento em outra máquina.

## Decisões que precisam de você

Aqui, "você" é quem aprova o produto, não a pessoa que vai usá-lo. Catorze decisões destravam a proposta; as seis primeiras vêm antes de qualquer código. A decisão "Geração antiga do padrão" já foi tomada e não pede resposta. Onde a coluna "Recomendação" traz uma sugestão pronta, basta aprovar ou recusar. Onde ela diz "decidir", "definir" ou "escolher", a resposta tem de vir de você.

| Decisão | Recomendação | Por quê |
| --- | --- | --- |
| Para quem é o produto | Decidir entre uso interno, oferta a clientes ou publicação como código aberto; em qualquer caso, começar com uma organização por instalação | Define telas, suporte e modelo de contas. Se o gateway for publicado, falta escolher a licença |
| Ligação com os sistemas | Usar o kit oficial em Python e avaliar o FastMCP na etapa "Provar a base", antes da fase 1; gateways prontos ficam só como referência | Um gateway pronto no meio tiraria da porta única o controle do envio |
| Provedor de login | Keycloak ou Logto. A equipe testa os dois e fica com um que funcione inteiro sem licença paga. O login é próprio por padrão, com ligação opcional, por configuração, à conta que a organização já usa | O Logto é leve e já traz o cadastro por endereço web, mas reserva recursos a uma licença paga. O Keycloak é mais completo, com partes experimentais |
| Aplicativos-piloto | Claude Code e um aplicativo de outra família que fale a versão de julho, a escolher | Entre os conferidos, o Claude Code é o único com registro público da versão nova. Se faltar um segundo aplicativo nessa versão, a decisão volta a você |
| Primeiro sistema conectado | Escolher um sistema seu, só com leitura e com chave por pessoa | A fase 1 precisa de um caso real |
| Peças que vêm com o gateway | Aprovar o Caddy no portão próprio e o OpenBao no cofre. Decidir como o cofre é destravado depois de um reinício: à mão, por pessoas que digitam partes de uma chave, ou sozinho, com uma chave guardada em arquivo | Depois de um reinício, o cofre volta trancado até ser destravado. À mão é mais seguro e exige gente; sozinho é automático e pede cautela. O limite de pedidos do Caddy vem de um módulo não oficial, que exige montar uma versão própria; o Traefik já traz o limite e é o plano B |
| Qual Apigee e quando | Apigee X. Definir região, quem opera e como ele alcança a máquina do gateway | Pelos resumos de busca, a variante instalada na casa do cliente exige Kubernetes. Falta conferir na documentação oficial |
| Geração antiga do padrão | Decidido: só a versão de julho, tanto para aplicativos quanto para sistemas. Quem só fala a geração antiga recebe uma recusa legível e entra na contagem, sem exceção. No caso de um sistema, a recusa vem já no cadastro | Simplifica o produto, porque o gateway fala uma versão só do padrão, com aplicativos e com sistemas. Em troca, deve deixar de fora, no começo, parte deles. Cada versão nova pede uma decisão. A próxima está prevista para 30 de março de 2027. Antes dessa data, uma revisão de compatibilidade decide quando mudar de versão |
| Programas que rodam sozinhos | Definir o responsável e com que chave eles agem em cada sistema | Sem regra, a saída natural é uma conta com poder demais |
| Ordem das fases 3 e 4 | Escolher: terceiros antes dos selos, ou o inverso | Depende de quais sistemas você quer ligar primeiro |
| Valores iniciais | Definir a validade das ordens de serviço, a duração da experiência, a espera antes de seguir, o teto de confirmações, a janela dos três sinais, o intervalo em que um pedido igual conta como repetição, a validade do selo e o tempo de guarda do extrato | Pedem teste com usuários reais |
| Avisos e segunda máquina | Escolher o canal de aviso de ficha pendente e de alarme, e pôr o vigia em outro provedor ou outra rede, com acessos separados dos do gateway | Sem aviso, a ficha vence sem ser vista; na mesma rede, uma queda cala os dois |
| Extensão Tasks | Deixar atrás de uma chave de liga e desliga própria; a consulta comum segue como caminho principal | Poucos aplicativos suportam, e o kit em Python ainda não a traz |
| Revisão jurídica | Submeter o texto a quem cuida da LGPD antes de circular fora da equipe, incluindo a passagem de dados pelo Google no modo Apigee. Pedir também parecer jurídico sobre a licença do Grafana, se o produto for oferecido a clientes ou distribuído | O documento evita promessa legal, mas não é parecer |

## Glossário para leigos

| Termo | Em palavras simples |
| --- | --- |
| MCP (Model Context Protocol) | Padrão aberto que permite a assistentes de IA usar ferramentas e dados de outros sistemas |
| Gateway | Ponto único de entrada que confere, filtra e registra cada pedido |
| Portaria | Imagem usada neste texto para o gateway: um programa, não uma pessoa |
| Portão | A peça que atende a internet na frente do gateway: um proxy de código aberto ou o Apigee |
| Apigee | Serviço comercial do Google para gerir o tráfego de APIs; aqui, o portão opcional |
| Aplicativo (cliente MCP) | O assistente de IA ou programa que faz pedidos em nome de uma pessoa |
| Sistema conectado (servidor MCP) | Quem oferece as ferramentas; pode ser seu ou de terceiros |
| Ferramenta (tool) | Algo que o assistente pode pedir a um sistema, como "listar cobranças" |
| Ação | Uso de uma ferramenta que muda algo, como enviar, criar ou apagar |
| ADMIN | Quem administra o gateway na organização: habilita aplicativos e sistemas, revisa o cadastro e define o teto das autorizações |
| Crachá (token de acesso) | Credencial digital do aplicativo, com prazo de validade |
| Provedor de login (IdP) | Serviço que confirma quem é a pessoa e emite o crachá |
| Ordem de serviço | Tarefa liberada, com limite e prazo; substitui o "acesso a tudo" |
| Vitrine | A lista de ferramentas que um aplicativo enxerga |
| Ficha da ação | Resumo de uma ação, montado pelo gateway, que a pessoa confirma |
| Cadastro único das ações | Lista, mantida no gateway, do que cada ferramenta lê, do que muda e se tem volta |
| Código de conferência | Número calculado a partir do conteúdo; se o conteúdo muda, o número muda |
| Pendências | Caixa, na tela do gateway, com as fichas que aguardam decisão |
| Porta única de saída | Fila por onde toda ação passa antes de seguir para um sistema |
| Cofre de chaves | Guarda trancada das chaves que cada pessoa autorizou para cada sistema |
| Chave compartilhada | Chave única de um sistema que não distingue as pessoas |
| Extrato | Tela com o que cada aplicativo leu, fez ou deixou pendente |
| Comprovante de bloqueio | Lista do que foi parado, do que já tinha seguido e do que está em conferência |
| Selo de nível | Indica o quanto um sistema conectado comprova sobre o que faz |
| Número de fechamento | Número calculado a partir do histórico e guardado no vigia; se um trecho já fechado for alterado, a conta refeita deixa de bater |
| Número de protocolo | Identificador de um trabalho, para consultar depois |
| Resultado não confirmado | A ação pode ter seguido, mas não chegou confirmação; repetir pode duplicar o efeito |
| Quarentena | Suspensão de um servidor até nova revisão |
| Injeção de prompt | Instrução maliciosa escondida em um conteúdo que a IA lê e pode acabar obedecendo |
| Chave de liga e desliga | Configuração que liga ou desliga uma função inteira do gateway |
| Geração do padrão | Versão do MCP que o aplicativo ou o sistema fala: a de julho de 2026 ou as anteriores; o gateway só aceita a de julho |
| Tasks | Extensão do MCP para trabalhos demorados, ainda em fase beta |
| Passkey | Confirmação pela digital ou pelo PIN do aparelho; a digital não sai do aparelho |
| Código aberto | Programa cujo código é público e pode ser usado sem pagar licença |
| Docker | Forma de empacotar e rodar programas em caixas isoladas, os contêineres |
| PostgreSQL | O banco de dados de código aberto usado pelo produto |
| Python | A linguagem de programação do núcleo do gateway |
| Kit oficial | Biblioteca do próprio projeto MCP para escrever servidores e clientes |
| Kubernetes | Sistema para operar muitos servidores; pede uma equipe dedicada |
| LGPD | Lei Geral de Proteção de Dados |
| Sala de visitas | Parte separada do gateway onde são atendidos os servidores de terceiros |
| Limite por aproximação | Regra aprovada pela pessoa que deixa o aplicativo agir sem ficha dentro de limites combinados |
| Alerta dos três sinais | Regra que suspende o limite por aproximação quando o aplicativo leu texto de fora, viu dado privado e quer enviar algo para fora |
| Mudou, parou | Regra que suspende a autorização de uma ferramenta quando ela passa a pedir mais do que foi revisado |
| Ronda das portas | Teste que o gateway repete em si mesmo, a cada poucos minutos, tentando entrar com crachá inválido |
| Vigia de fora | Programa em outra máquina que repete a ronda, guarda o número de fechamento e avisa se o gateway ficar mudo |
| Parâmetro | Ajuste de configuração que muda o comportamento do gateway sem código novo |
| Certificado | Documento digital com que um programa ou uma máquina prova quem é |

## Fontes da pesquisa

A primeira checagem reabriu 68 afirmações em 17 de novembro de 2026: 63 confirmadas, 4 corrigidas e 1 sem acesso. Em 18 de novembro, três rodadas a mais conferiram os precedentes de novidade, o Apigee e as licenças. Uma revisão final, no mesmo dia, comparou o texto inteiro com esse material.

### Páginas abertas

- Padrão MCP: [mudanças da versão 2026-07-28](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/changelog.mdx), [anúncio da versão](https://blog.modelcontextprotocol.io/posts/2026-07-28/), [versões publicadas](https://github.com/modelcontextprotocol/modelcontextprotocol/releases), [calendário de versões](https://blog.modelcontextprotocol.io/posts/mcp-release-cycle/), [entrada na fundação](https://blog.modelcontextprotocol.io/posts/2025-12-09-mcp-joins-agentic-ai-foundation/), [extensões e seus estágios](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/extensions/overview.mdx), [extensões por aplicativo](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/extensions/client-matrix.mdx), [roteiro oficial](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/development/roadmap.mdx), [boas práticas de segurança](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/docs/2026-07-28/tutorials/security/security_best_practices.mdx), [etiquetas de ferramenta](https://blog.modelcontextprotocol.io/posts/2026-03-16-tool-annotations/), [extensão Tasks](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/seps/2663-tasks-extension.md), [discussão sobre repetição](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394), [suíte de conformidade](https://github.com/modelcontextprotocol/conformance), [kit em Python](https://github.com/modelcontextprotocol/python-sdk/releases), [níveis dos kits](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/community/sdk-tiers.mdx).
- Aplicativos: [histórico do Claude Code](https://github.com/anthropics/claude-code/blob/main/CHANGELOG.md), [critérios para conectores do Claude](https://claude.com/docs/connectors/building/review-criteria.md), [busca de ferramentas na Claude API](https://platform.claude.com/docs/en/agents-and-tools/tool-use/tool-search-tool).
- Mercado: os doze produtos abertos. Os sete da tabela de mercado têm o link em cada nome. Os outros cinco são [Obot](https://github.com/obot-platform/obot), [Kong](https://github.com/Kong/developer.konghq.com/blob/main/app/_kong_plugins/ai-mcp-proxy/index.md), [Azure API Management](https://github.com/MicrosoftDocs/azure-docs/blob/main/articles/api-management/mcp-server-overview.md), LiteLLM e Bifrost; os links dos dois últimos estão em Incidentes.
- Fator humano e contexto: [aprovação de 93% dos pedidos](https://www.anthropic.com/engineering/claude-code-auto-mode), [decisões que seguem o agente](https://www.anthropic.com/research/agents-judgment-calls), [custo de muitas ferramentas](https://www.anthropic.com/engineering/advanced-tool-use).
- Incidentes: [alertas com o termo "mcp" em 2026](https://github.com/advisories?query=mcp+type%3Areviewed+published%3A%3E2026-01-01), [LiteLLM](https://github.com/advisories/GHSA-7488-6r32-c95q), [Bifrost](https://github.com/advisories/GHSA-gqjq-cgxr-8c7c), [Microsoft MCP-CLARO](https://github.com/advisories/GHSA-xgj3-w2cc-qgrq), [ContextForge](https://github.com/advisories?query=%22mcp+gateway%22), [kit Python do MCP](https://github.com/advisories/GHSA-w4fh-qvv9-3v23), [n8n-mcp](https://github.com/advisories/GHSA-j6r7-6fhx-77wx), [proposta ETDI](https://github.com/modelcontextprotocol/python-sdk/pull/845), [CaMeL](https://github.com/google-research/camel-prompt-injection).
- Identidade: [Keycloak e MCP](https://github.com/keycloak/keycloak/blob/main/docs/guides/securing-apps/mcp-authz-server.adoc), [Logto](https://github.com/logto-io/docs/blob/master/docs/integrate-logto/third-party-applications/dynamic-apps.mdx), [Tenuo](https://github.com/tenuo-ai/tenuo), [AAuth](https://github.com/dickhardt/AAuth).
- Precedentes de novidade: [toolkit de governança da Microsoft](https://github.com/microsoft/agent-governance-toolkit), [warrantd-gateway](https://pypi.org/project/warrantd-gateway/), [Tracekit](https://github.com/cygnux-labs/tracekit), [níveis de segurança para servidores MCP](https://github.com/mcp-security-standard/mcp-server-security-standard).
- Portão e licenças: [Apigee na frente de um servidor MCP](https://github.com/GoogleCloudPlatform/apigee-samples/issues/465), [certificado entre o Apigee e o destino](https://github.com/GoogleCloudPlatform/apigee-samples/blob/main/mtls-southbound/README.md), [borda trocável por configuração](https://github.com/google/esmeralda/blob/main/docs/src/2-workloads-and-catalog/01-ingress-gateways.md), [módulo de limite de pedidos do Caddy](https://github.com/mholt/caddy-ratelimit), e o arquivo de licença de cada repositório da tabela "O que o produto usa".

### Não conferido em página

A rede da sessão de pesquisa bloqueou vários sites, e os itens abaixo não foram conferidos na página de origem. O texto usa três deles: a documentação do Apigee, no portão Apigee; o rascunho MWS, nos selos de nível 2 e 3; e os produtos comerciais, na comparação de mercado. Os dois primeiros serão conferidos antes de valer: o Apigee antes da troca de portão, e o MWS antes da fase 4. A comparação de mercado já traz a ressalva no texto. A proposta não depende dos demais.

- A documentação oficial do Apigee: variantes, tempos de espera, tamanhos, repetição de pedidos, fluxo contínuo e os recursos próprios de MCP.
- Produtos comerciais que cobrem partes da proposta: MintMCP, DigitalOcean Action Gateway, TrueFoundry e Permit.
- O rascunho MWS, lido em cópia local, sem o endereço de origem à mão.
- Regulação: o regulamento europeu 2026/1744, a situação do PL 2338/2023 e o estudo da ANPD sobre agentes de IA.
- Números de fornecedores e artigos acadêmicos hospedados no arXiv.
- O site oficial modelcontextprotocol.io. A especificação foi lida no repositório oficial no GitHub, que é a origem do site.
