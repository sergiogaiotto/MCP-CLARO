# Ligar um servidor de terceiros

Roteiro do ADMIN, passo a passo, para quem nunca fez. Ele segue as telas do MCP-CLARO na ordem em que você as usa,
com os textos que elas mostram. Onde um passo depende da equipe que opera o gateway, de gente ou de ambiente real, o
roteiro diz isso.

## O que é um servidor de terceiros

É um servidor MCP que não é da organização: vem de um fornecedor de fora. O MCP-CLARO o trata como estranho, porque
tudo o que vem dele pode estar errado ou ser mentira:

- conversa com ele só pela **sala de visitas**: só no endereço combinado, sem seguir desvios, com a chave guardada
  à parte e sem dados de rastreio. Só a troca e a renovação da chave de cada pessoa (OAuth) saem do despachante,
  direto ao endereço de troca que o fornecedor deu, com a mesma conferência de endereço;
- só mostra às pessoas as ferramentas que você revisou e publicou na **revisão do servidor**;
- nada do texto escrito pelo fornecedor vai ao aplicativo de IA: o aplicativo vê só o que você escreveu;
- a resposta do servidor chega ao aplicativo dentro de uma moldura, marcada como **texto de estranhos**;
- qualquer mudança no que o servidor declara, mesmo só de texto, tira o servidor inteiro de cena: ele entra em
  **quarentena** até você revisar de novo.

Nada disso é garantia. Essas defesas reduzem o que um servidor mal-intencionado consegue fazer, mas não controlam o
que ele faz do lado de lá. O [modelo de ameaças](modelo-de-ameacas.md) diz o que fica fora.

## Antes de começar

Combine com o fornecedor e tenha em mãos:

1. O **endereço MCP** do servidor. Precisa ser `https://` e estar na internet. Endereço de rede interna, como os de
   escritório ou de nuvem privada, é recusado.
2. O **nome do fornecedor** (de 3 a 200 letras). Você escreve na conclusão da revisão.
3. **Como cada pessoa entra no servidor:**
   - **Uma chave por pessoa**: cada pessoa autoriza o MCP-CLARO na página do servidor (OAuth, o jeito comum de uma
     pessoa deixar um programa usar a conta dela, sem dar a senha a ele). O fornecedor precisa dar o endereço da
     página de autorização, o endereço de troca do código, o `client_id` do MCP-CLARO, os escopos e, se ele usar, o
     segredo do cliente. Ele também precisa cadastrar, do lado dele, o endereço de volta que a tela do MCP-CLARO
     mostra.
   - **Uma chave para todos**: uma chave só, para todas as pessoas. Nesse caso, o servidor só pode ler.
   - Chave de programa não serve para servidor de terceiros.
4. Uma **chave só para ler as declarações**, se a chave for por pessoa: uma chave do servidor que só lê a lista de
   ferramentas, sem dado de ninguém. Sem ela, você não consegue concluir a revisão do servidor (passo 6).
5. Se o servidor pedir chave já na conferência da versão, uma **chave de teste**. Ela é usada só naquela conferência
   e não fica guardada.

Enquanto a Fase 3 estiver desligada (hoje é assim; ligar é decisão do dono do produto, depois das provas que
dependem de gente e de ambiente real), as telas mostram:

> Servidores de terceiros ficam desligados até a Fase 3 passar nas provas. Você pode cadastrar, conferir e revisar; só pessoas de teste os veem.

Pessoa de teste é a pessoa marcada como de teste no cadastro de pessoas. Na tela "Pessoas", ela aparece com
"(teste)". Hoje não há botão nas telas para marcar uma pessoa de teste: peça à equipe que opera o gateway.

## Passo 1. Cadastrar o servidor

Na Área do ADMIN, toque em "Sistemas" e vá até "Cadastrar um sistema".

1. Em "Nome do sistema", escreva um nome curto, que as pessoas vão ver.
2. Em "De quem é este servidor?", escolha:

   > De terceiros (de um fornecedor de fora)

   Atenção: esta escolha não muda depois. A tela avisa:

   > Esta escolha não muda depois. Um servidor de terceiros é atendido na sala de visitas: só no endereço combinado, sem seguir desvios, com a chave guardada à parte e sem dados de rastreio. Ele só aparece para as pessoas depois que você revisar o servidor inteiro.

3. Em "Endereço MCP do sistema", cole o endereço `https://` do servidor. A tela lembra: "Servidor de terceiros: só
   endereço https da internet."
4. Em "Tipo de chave", escolha "Uma chave por pessoa (cada pessoa autoriza)" ou "Uma chave para todos (só leitura)".
5. Se o servidor pedir chave já na conferência, escreva a chave de teste.
6. Toque em "Cadastrar e conferir a versão".

O cadastro pode ser recusado. Nada é gravado, e a tela diz o motivo:

| A tela diz | O que fazer |
| --- | --- |
| Escolha de quem é o servidor. | Marque "De terceiros (de um fornecedor de fora)". |
| Servidor de terceiros precisa de endereço https. | Peça ao fornecedor o endereço `https://`. |
| Este endereço não é público. O MCP-CLARO só chama servidores de terceiros em endereços da internet. | O endereço aponta para uma rede interna. Peça ao fornecedor o endereço da internet. |
| Endereço com nome de servidor inválido. | O nome do servidor no endereço tem letras que não valem num endereço. Confira o endereço com o fornecedor. |
| Servidor de terceiros usa uma chave por pessoa ou uma chave para todos (só leitura). Chave de programa fica para depois. | Escolha outro tipo de chave. |

Cadastrado, o servidor entra no extrato ("cadastrado por" você) e o MCP-CLARO já confere a versão dele.

## Passo 2. Ler o resultado da conferência da versão

A conferência pergunta ao servidor que versão do MCP ele fala. O MCP-CLARO só fala a 2026-07-28. A página do
sistema mostra o resultado:

| A tela diz | O que quer dizer | O que fazer |
| --- | --- | --- |
| **Aceito.** Este servidor fala a versão 2026-07-28 do MCP. | Tudo certo. | Siga para o passo 3. |
| Este servidor de terceiros foi recusado. Ele não fala a versão 2026-07-28 do MCP, a única que o MCP-CLARO aceita. Ele anunciou: (as versões). Peça ao fornecedor para atualizar o servidor e peça a conferência de novo. A recusa entrou na contagem de versões. | O servidor é de uma geração antiga do MCP. | Peça ao fornecedor para atualizar. Depois, toque em "Conferir a versão de novo". |
| O servidor pediu para irmos a outro endereço. O MCP-CLARO só chama o endereço combinado e não segue desvios. Confira o endereço certo com o fornecedor. | O servidor tentou mandar o pedido a outro lugar. | Confira o endereço com o fornecedor. Como o endereço não muda depois do cadastro, cadastre o servidor de novo, com outro nome, se o endereço estava errado. |
| Este endereço não respondeu como um servidor MCP. Confira o endereço. | No endereço há outra coisa (uma página, por exemplo). | Confira o endereço com o fornecedor. |
| O servidor pediu uma chave. Informe a chave de teste e confira de novo. | O servidor só responde com chave. | Escreva a chave de teste e toque em "Conferir a versão de novo". |
| Ainda não conseguimos falar com o servidor. Confira o endereço e peça a conferência de novo daqui a pouco. | O servidor não respondeu a tempo, ou deu erro. | Espere um pouco e confira de novo. |
| Nada foi enviado: o endereço deste servidor de terceiros não serve. Ou ele não é público (o MCP-CLARO só chama servidores de terceiros em endereços da internet), ou não usa https, ou o nome do servidor é inválido. Confira o endereço com o fornecedor. | Na hora de chamar, o nome do servidor levou a um endereço que não é da internet. | Confira o endereço com o fornecedor. |
| A sala de visitas, por onde o MCP-CLARO fala com servidores de terceiros, não está disponível agora. Nada foi enviado ao servidor. Peça a conferência de novo daqui a pouco. | O problema é do MCP-CLARO, não do servidor. | Espere um pouco. Se continuar, avise a equipe que opera o gateway. |

Só o primeiro caso de recusa (versão) entra na contagem de versões antigas. Os outros não contam e não mudam a
situação do servidor.

## Passo 3. Guardar a autorização e as chaves

Na página do sistema, toque em "Autorização e chaves deste sistema". As chaves vão direto ao despachante, que as
cifra no cofre. Depois de entregues, elas não aparecem mais em lugar nenhum.

**Se a chave é por pessoa:**

1. Em "Autorização de cada pessoa", preencha os dados que o fornecedor deu: endereço da página de autorização,
   endereço de troca do código, `client_id`, escopos e como o MCP-CLARO se apresenta na troca. Copie o endereço de
   volta que a tela mostra e peça ao fornecedor para cadastrá-lo no servidor de autorização dele. A tela mostra:

   > No servidor de autorização deste fornecedor, cadastre este endereço de volta. Ele é só deste servidor: (o endereço)

   Num servidor de terceiros, o endereço de volta é só dele: ele termina em
   `/telas/autorizar/<número do servidor>/volta`. Os sistemas próprios continuam com `/telas/autorizar/volta`. Não
   use o endereço de outro servidor nem o dos sistemas próprios: o MCP-CLARO recusa a volta que chega pelo endereço
   de outro sistema, e a pessoa vê "Este pedido de autorização não é deste sistema. Nada foi guardado. Comece de novo
   por aqui." Assim, fica mais difícil um servidor de terceiros usar a autorização que a pessoa deu a outro sistema.
   Isso conta com o servidor de autorização de cada sistema aceitar só o endereço de volta que tem cadastrado.

   A tela também avisa:

   > Servidor de terceiros: a troca do código e a renovação da chave só vão a endereços https da internet. Depois de guardada, esta configuração não muda; para mudar, cadastre o servidor de novo.

2. Toque em "Guardar a configuração". Confira tudo antes: se tentar mudar depois, a tela recusa e nada muda:

   > A autorização de um servidor de terceiros não muda depois de configurada: os endereços de troca de chave ficam presos ao servidor que você revisou. Nada mudou. Para mudar, cadastre o servidor de novo.

3. Se o fornecedor deu um segredo do cliente, entregue em "Segredo do MCP-CLARO no sistema".
4. Em "Chave só para ler as declarações", entregue a chave de leitura das declarações.

**Se a chave é uma para todos:** entregue a chave em "Chave compartilhada". Ela serve também para ler as
declarações.

## Passo 4. Ler as ferramentas

Volte à página do sistema. Em "Ferramentas que o sistema declara", toque em "Ler as ferramentas agora". A chave de
teste é opcional: sem ela, o MCP-CLARO usa a chave de leitura das declarações guardada. A tela mostra cada
ferramenta, com o nome e o texto que o fornecedor escreveu, e a situação da sua revisão: "Sem revisão", "Revisada"
ou "Mudou desde a revisão". O texto do fornecedor aparece num quadro à parte: ele não foi escrito por nós e não é
mostrado às pessoas nem aos aplicativos.

Se a leitura falhar, a tela diz o motivo. Estas quatro frases são problemas do MCP-CLARO, e não do servidor:

- "A sala de visitas não está disponível agora. Nada foi pedido ao servidor. Tente de novo daqui a pouco.";
- "Todas as vagas da sala de visitas estavam ocupadas. Nada foi pedido ao servidor. Tente de novo daqui a pouco.";
- "Não deu para abrir no cofre a chave de leitura das declarações. Tente de novo daqui a pouco.";
- "O MCP-CLARO não conseguiu usar o próprio banco de dados agora. A falha é do MCP-CLARO, não do servidor. Tente de
  novo daqui a pouco."

Espere um pouco e, se continuar, avise a equipe que opera o gateway. "O despachante não conseguiu ler as ferramentas
deste sistema. Confira a chave e tente de novo." aparece quando o servidor não entregou a lista e nas outras falhas:
confira a chave e, se ela estiver certa, tente de novo daqui a pouco.

## Passo 5. Revisar cada ferramenta

Para cada ferramenta que você quer oferecer, toque em "Revisar". Você escreve o que as pessoas e o aplicativo vão
ver:

1. **Nome na vitrine**: o nome que o aplicativo de IA vê. Letras sem acento, números e `_ . -`. Num servidor de
   terceiros, a página sugere só o começo (o nome do sistema e `_`), e não o nome que o fornecedor deu, e lembra:
   "Escreva um nome seu: o nome declarado é texto do fornecedor."
2. **Descrição própria**: com suas palavras, o que a ferramenta faz. Não copie o texto do fornecedor.
3. **Descrição de cada campo (opcional)**: o aplicativo vê só estas descrições, escritas por você, até 300 letras
   cada.
4. **Classe**: "Só lê", "Muda algo e não tem volta" ou "Muda algo, mas dá para desfazer". Escolha pelo que a
   ferramenta faz de verdade, não pelo que o fornecedor diz.
5. **Risco**: baixo, médio ou alto.
6. **Campos pessoais da resposta**: marque os que trazem dado pessoal. Eles são mascarados antes de chegar ao
   aplicativo, e uma ferramenta com campo pessoal conta para o alerta dos três sinais.
7. **Campos do pedido que dizem o destinatário**, nas ferramentas que mandam algo para alguém.

Duas coisas são fixas num servidor de terceiros:

- a caixa "Texto de fora" vem marcada e travada:

  > Sempre, em servidor de terceiros: a resposta conta como texto de fora.

- o quadro "O que o aplicativo vai ver" mostra os campos como vão ao aplicativo, e avisa:

  > Nada do texto escrito pelo fornecedor vai para o aplicativo, nem as dicas como “só leitura”.

  Nele, as partes internas do esquema aparecem com nomes neutros (`d1`, `d2`...), no lugar dos nomes que o
  fornecedor deu. Os nomes de campo, as listas de escolhas e as regras de formato continuam como o fornecedor
  escreveu: o aplicativo precisa deles para chamar a ferramenta.

Toque em "Gravar a revisão". Cada gravação cria uma versão nova da revisão. Num servidor de terceiros, a tela diz:

> Pronto. A revisão foi gravada. Num servidor de terceiros, a ferramenta só aparece nas vitrines e nos modelos de ordem depois que você concluir a revisão do servidor.

Ou seja, a ferramenta só entra num modelo depois do passo 6. E, se você revisar de novo uma ferramenta já
publicada, ela sai da vitrine até você concluir a revisão do servidor outra vez.

O esquema do fornecedor pode ser recusado na revisão. A tela diz o que achou e pede um esquema mais simples. Por
exemplo, quando ele tem partes demais umas dentro das outras:

> O esquema de entrada desta ferramenta tem partes demais umas dentro das outras para o MCP-CLARO conferir. Peça ao fornecedor um esquema mais simples.

Os limites de um esquema de terceiro: até 2.000 partes, até 64 níveis, cada lista de escolhas com até 1.000 valores e
o esquema, já sem os textos do fornecedor, com até 256 KiB. Nesses casos, peça ao fornecedor para simplificar. O
MCP-CLARO não confere as regras de formato (`pattern`) nem a regra de itens sem repetição (`uniqueItems`) de um
servidor de terceiros: quem confere é o próprio servidor.

Uma ferramenta de terceiros com nome de mais de 128 letras também não pode ser revisada. A tela diz:

> O servidor declara esta ferramenta com um nome de mais de 128 letras. O MCP-CLARO não chama ferramenta de terceiros com nome tão longo, então ela não pode ser revisada nem ir para a vitrine.

## Passo 6. Concluir a revisão do servidor

Na página do sistema, o cartão "Revisão do servidor" diz se ele já foi revisado:

> Nunca revisado. Nenhuma ferramenta dele aparece para as pessoas.

Para concluir:

1. Em "Quem fornece este servidor", escreva o nome do fornecedor, de 3 a 200 letras.
2. Em "Mostrar na vitrine", marque as ferramentas que as pessoas podem usar. Só aparecem para marcar as que têm a
   sua revisão em dia. As outras aparecem com o motivo: falta a sua revisão, mudou desde a revisão, ou foi revisada
   antes da Fase 3. Até 500 ferramentas por conclusão.
3. Toque em "Concluir a revisão do servidor".

Abaixo do botão, a tela lembra:

> Ao concluir, o MCP-CLARO lê o servidor de novo. Se algo mudou desde a sua revisão, a conclusão é recusada e mostra o quê.

Se você marcar mais de 500, a tela mostra a página "Pedido recusado", com o texto abaixo, e nada muda. Volte e
marque menos:

> Marque até 500 ferramentas para a vitrine. Nada mudou.

Se o nome do fornecedor ficar com menos de 3 letras ou mais de 200, nada vai ao despachante (o navegador costuma
avisar antes de enviar). A página do sistema volta e diz, em "Falta corrigir:":

> Preencha quem fornece este servidor com 3 a 200 letras.

Deu certo, a tela diz:

> Pronto. A revisão do servidor foi registrada. As ferramentas marcadas aparecem nas vitrines, dentro das ordens de serviço.

E o cartão passa a dizer quando, por quem, que versão da revisão e o fornecedor. A conclusão vai ao extrato. Se
algo não deu certo, a tela diz "A revisão do servidor não foi concluída. Veja abaixo o que mudou e revise de novo." e
mostra o motivo:

| A tela diz | O que fazer |
| --- | --- |
| Guarde a chave de leitura das declarações deste servidor antes de concluir a revisão. Ela fica em “Autorização e chaves deste sistema”. É a chave que o MCP-CLARO usa para conferir, de tempos em tempos, se o servidor mudou. | Volte ao passo 3 e entregue a chave. |
| Algumas ferramentas marcadas não estão em dia. Nada foi publicado. Veja a lista abaixo. | A lista diz o que houve com cada uma: "mudou desde a sua revisão: revise de novo", "ainda não tem revisão sua", "o servidor não declara mais esta ferramenta" ou "foi revisada antes da Fase 3: revise de novo para guardar o texto que o servidor declara". Revise as da lista (passo 5) e conclua de novo. |
| O servidor precisa estar ativo ou em quarentena para concluir a revisão. | Antes, a conferência da versão precisa dar "Aceito" (passo 2). |
| O servidor precisa passar na conferência da versão 2026-07-28 antes de concluir a revisão. Toque em “Conferir a versão de novo”. | Faça a conferência da versão de novo. |
| O servidor não entregou a lista de ferramentas agora, ou a sala de visitas, por onde o MCP-CLARO fala com ele, não estava disponível. Nada mudou. Tente de novo daqui a pouco. | Espere um pouco e conclua de novo. |
| O servidor mudou de situação enquanto a revisão era concluída. Nada mudou. Leia as ferramentas de novo e conclua outra vez. | Toque em "Ler as ferramentas agora" e conclua de novo. |
| Não deu para abrir no cofre a chave de leitura das declarações. Nada mudou. Tente de novo daqui a pouco. | Problema do MCP-CLARO. Espere um pouco; se continuar, avise a equipe que opera o gateway. |

Se o despachante não responder, esse quadro não aparece: a página do sistema volta com um aviso no alto.

| A tela diz | O que fazer |
| --- | --- |
| O despachante não respondeu. Nada mudou. Tente de novo daqui a pouco. | Espere um pouco e conclua de novo. |
| O despachante não respondeu a tempo. A revisão do servidor pode ter sido registrada ou não: confira o cartão “Revisão do servidor” abaixo antes de tentar de novo. | Olhe o cartão: se ele já mostra a revisão nova, está feito. Se não, conclua de novo. |

Você pode concluir sem marcar nenhuma ferramenta: o servidor fica revisado, mas nada dele aparece para as pessoas.

## Passo 7. Pôr as ferramentas nas ordens de serviço

Na Área do ADMIN, toque em "Modelos de ordem". As ferramentas de terceiros aparecem com "(de terceiros)". Uma
ferramenta que não foi publicada na revisão do servidor fica sem poder ser escolhida, com o aviso:

> Esta ferramenta é de um servidor de terceiros e não foi publicada na revisão do servidor.

Com a Fase 3 desligada, você monta o modelo assim mesmo, para as pessoas de teste. A tela lembra:

> Enquanto a Fase 3 estiver desligada, só pessoas de teste usam ferramentas de terceiros.

## Passo 8. Testar com uma pessoa de teste

Antes de oferecer a pessoas reais, peça a uma pessoa de teste para usar o servidor por um aplicativo de IA:

1. Se a chave é por pessoa, ela autoriza o MCP-CLARO na página do servidor. Essa página é do fornecedor e pode pedir
   senha: isso acontece fora do MCP-CLARO.
2. Ela pede uma ordem de serviço com o modelo do passo 7 e usa uma leitura. A resposta de uma leitura vai só ao
   aplicativo, dentro da moldura de texto de estranhos. No MCP-CLARO, a leitura não tem tela própria: no extrato,
   ela vê só a linha da leitura, sem o texto da resposta.
3. Se o modelo tiver uma ação (só com chave por pessoa: a chave para todos só lê), ela usa a ação. Quando a ação
   passa pela ficha (sem um limite que a cubra), a ficha avisa:

   > Este servidor é de terceiros. O MCP-CLARO não controla o que ele faz do lado de lá.

4. Depois que a ação sai, na tela da ação, em vez da resposta, ela vê:

   > O servidor de terceiros respondeu. A resposta foi entregue ao aplicativo, marcada como texto de estranhos.

   O texto só aparece se ela pedir, com um aviso para não fazer o que ele mandar.

Em "Minhas ordens de serviço", toda pessoa que vê uma ferramenta de terceiros lê:

> Uma ferramenta marcada “de terceiros” é de um servidor de fora da organização. O MCP-CLARO não controla o que ele faz do lado de lá, e o que ele responde chega ao aplicativo marcado como texto de estranhos.

Com a Fase 3 desligada, uma pessoa real (que não é de teste) não usa o servidor. Em "Minhas ordens de serviço", ela
lê também:

> Enquanto os servidores de terceiros estiverem desligados nesta instalação, as ferramentas marcadas “de terceiros” não funcionam para você: o aplicativo não as vê. Só pessoas de teste as usam.

E, se ela tentar autorizar o servidor, a tela diz:

> Servidores de terceiros ainda estão desligados neste MCP-CLARO. Só pessoas de teste os autorizam. Nada foi guardado.

O que a pessoa de teste e o ADMIN veem nos dias ruins está no [simulado do dia ruim](simulado-dia-ruim.md).

## Acompanhar o servidor

- **Cartão "Revisão do servidor".** Diz a revisão que vale. Depois, ele avisa de duas coisas diferentes.

  Se você revisou de novo uma ferramenta publicada, ele diz quantas ficaram fora da vitrine até você concluir de
  novo. Com uma:

  > 1 ferramenta foi revista depois desta revisão. Ela fica fora da vitrine até você concluir a revisão do servidor de novo.

  Se, na última leitura das ferramentas, uma ferramenta publicada estava diferente da sua revisão, ele avisa que ela
  ainda não saiu da vitrine. Com uma:

  > 1 ferramenta mudou no servidor desde a sua revisão. Na próxima conferência automática, se ele ainda declarar assim, o servidor entra em quarentena e nada dele chega às pessoas. Até lá, ela continua na vitrine, com o texto da sua revisão. Revise a ferramenta e conclua a revisão do servidor de novo.

  Se o servidor não respondeu à última conferência:

  > O servidor não respondeu à última conferência. Se não responder à próxima, entra em quarentena.

  Se o servidor saiu de "ativo" sem passar pela quarentena e voltou, a revisão anterior deixa de valer. Pelas
  telas, isso não acontece: um servidor revisado que muda, para de responder ou deixa de falar a versão vai à
  quarentena (veja abaixo). Só acontece quando a equipe que opera o gateway muda o cadastro dele direto no banco,
  por exemplo o tipo de chave, ou o desativa e depois o põe de volta. Com o servidor ativo de novo, o cartão diz:

  > A revisão anterior deixou de valer quando o servidor saiu do ar; conclua a revisão de novo. Até lá, nenhuma ferramenta dele aparece para as pessoas.

  Nesse caso, leia as ferramentas, revise as que mudaram e conclua a revisão do servidor de novo (passos 4 a 6).
  Enquanto ele ainda não está ativo, o cartão diz:

  > A revisão anterior deixou de valer quando o servidor saiu do ar. Quando ele voltar e passar na conferência da versão, conclua a revisão de novo. Até lá, nenhuma ferramenta dele aparece para as pessoas.

- **"Pedidos recusados deste sistema".** O MCP-CLARO não repassa a ninguém pedidos de dados, links ou confirmações
  que um sistema faça no meio de uma chamada. Este quadro mostra quantos pedidos o servidor fez nos últimos 30 dias
  e os últimos, com a data, onde foi, a ferramenta e o que ele pediu, em palavras nossas (por exemplo, "dados por
  formulário" ou "que a pessoa abrisse um link"). Conta também os pedidos que o servidor fez fora de uma chamada, na
  leitura das ferramentas e na sondagem, e as ações que terminaram sem confirmação ou que não saíram depois de um
  pedido recusado. A coluna "Onde" diz em que ponto foi: por exemplo, "Na leitura das ferramentas", "Na sondagem",
  "Numa ação sem confirmação" ou "Numa ação, que não saiu". Na leitura das ferramentas e na sondagem, a linha vem sem
  o nome de ferramenta. O texto, o link e os campos que o servidor mandou não são guardados nem mostrados. Muitos
  pedidos são um sinal para conversar com o fornecedor.
- **Avisos por e-mail e webhook.** Quando a conferência de tempos em tempos põe o servidor em quarentena, o
  MCP-CLARO avisa os ADMIN por e-mail e pelos webhooks cadastrados. A quarentena pela conferência da versão, que você
  mesmo pede na tela, aparece ali na hora, sem aviso. Os eventos são `sistema_em_quarentena` (o servidor mudou),
  `sistema_em_quarentena_sem_resposta` (não respondeu à conferência) e `sistema_em_quarentena_revisao_inconsistente`
  (a revisão guardada não confere com a própria lista de ferramentas). Em "Avisos por webhook", ao cadastrar um
  destino, escreva os eventos que ele recebe no campo "Eventos (separados por vírgula)", por exemplo
  `sistema_em_quarentena,sistema_em_quarentena_sem_resposta`. Deixe o campo vazio para receber todos os avisos. As
  pessoas não recebem aviso por e-mail: elas veem no próprio extrato.
- **Conferência de tempos em tempos.** O MCP-CLARO lê o servidor de novo a cada `TOOL_DRIFT_CHECK_SECONDS` (900
  segundos, 15 minutos, no início), com a chave de leitura das declarações.

## Quarentena: o que é e como sair

O servidor entra em quarentena quando:

- mudou a definição de uma ferramenta depois da sua revisão, mesmo que só o texto, ou passou a declarar uma
  ferramenta nova, ou deixou de declarar uma;
- não respondeu à conferência em 2 rodadas seguidas (sem conferir, o MCP-CLARO não sabe se ele mudou);
- deixou de falar a versão 2026-07-28 numa nova conferência da versão;
- a revisão guardada não confere com a própria lista de ferramentas.

Na hora, nada do servidor chega às pessoas: as ferramentas dele saem das vitrines, o que esperava para sair é
cancelado, e os limites das pessoas com ele são suspensos. Cada pessoa vê isso no próprio extrato. Um limite
suspenso não volta sozinho: depois da nova revisão, a pessoa cria outro, se ainda precisar.

A página do sistema mostra um quadro em destaque, que começa por "Este servidor está em quarentena." e diz por quê:

| Por que entrou | O quadro diz |
| --- | --- |
| Mudou o que declara | Ele mudou a definição de uma ferramenta depois da sua revisão. Mesmo que tenha mudado só o texto, nada dele chega às pessoas até você revisar de novo. |
| Não respondeu | Ele não respondeu à conferência em rodadas seguidas. Sem conferir, o MCP-CLARO não sabe se ele mudou. |
| Versão | Numa conferência da versão, ele deixou de falar a versão 2026-07-28 do MCP. Depois que uma nova conferência da versão dá certo, o quadro diz também: "A versão já foi conferida de novo e está certa: falta ler as ferramentas, revisar as que mudaram e concluir a revisão do servidor." |
| A revisão guardada não confere | O que ele declara não bate com a revisão do servidor. |

Nos três últimos casos, o quadro segue com "Nada dele chega às pessoas até você revisar de novo." Em todos, ele
diz:

> O que estava esperando para sair foi cancelado, e os limites das pessoas com este servidor foram suspensos; cada pessoa vê isso no próprio extrato.

E o cartão "Revisão do servidor" diz:

> A revisão anterior deixou de valer com a quarentena. Nenhuma ferramenta dele aparece para as pessoas.

Quando o servidor mudou, o quadro mostra o que mudou, ferramenta por ferramenta ("ferramenta nova", "o servidor não
declara mais" ou "mudou a definição"), e, mais abaixo, o antes e o depois do texto do fornecedor. Para sair, siga
os passos do quadro:

1. Toque em “Ler as ferramentas agora”.
2. Revise as marcadas “Mudou desde a revisão”.
3. Conclua a revisão do servidor.

Se a versão do servidor não está certa (é o caso da quarentena pela versão), o quadro começa por outro passo,
"Toque em “Conferir a versão de novo”." Em quarentena, essa conferência não muda a situação do servidor: só a nova
revisão do servidor o põe de volta em serviço. Se a conclusão der certo, o extrato registra que o servidor saiu da quarentena pela
sua revisão.

Se você não quer o servidor de volta, deixe-o em quarentena: nada dele chega a ninguém. Para parar na hora tudo o
que vai a ele, use "Bloqueios e pausas" com "Um sistema: nada vai a ele". Hoje não há botão nas telas para
desativar um sistema; peça à equipe que opera o gateway.

## Mudar algo depois

- **De quem é, endereço e autorização** de um servidor de terceiros não mudam. Para mudar, cadastre o servidor de
  novo, com outro nome (o nome de um sistema não se repete), e refaça este roteiro.
- **Uma ferramenta**: revise de novo (passo 5) e conclua a revisão do servidor (passo 6). Até lá, ela fica fora da
  vitrine.

## O que depende de gente ou de ambiente real

- Escolher os primeiros servidores de terceiros reais: decisão do dono do produto.
- Ligar a Fase 3 para pessoas reais: decisão do dono, depois das provas que dependem de gente e de ambiente real.
  Hoje a partida do gateway recusa a chave ligada.
- A autorização (OAuth) e a página de autorização de um terceiro real ainda não foram conferidas com um servidor
  real.
- Marcar pessoas de teste e desativar um sistema: hoje só a equipe que opera o gateway faz.
- Recomendado: uma sessão guiada em que um ADMIN segue este roteiro sem ajuda da equipe, para achar o que ficou
  confuso.
