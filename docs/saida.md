# Porta de saída: como uma ação sai

Este roteiro é para quem cuida do MCP-CLARO no dia a dia. Ele diz o que acontece com uma ação pedida por um
aplicativo, o que cada estado quer dizer e o que fazer em cada caso. O desenho completo está no
[plano](plano.md#a-porta-de-saída-por-dentro).

## O caminho de uma ação

1. O aplicativo pede uma ação (uma ferramenta que muda alguma coisa no sistema).
2. A entrada confere a vitrine, os argumentos e os bloqueios. Ela recusa o pedido **antes de dar número**
   quando:
   - um bloqueio ou uma pausa vale para aquela pessoa, aquele aplicativo ou aquele sistema;
   - a **porta de saída está fechada** (pelo ADMIN, na tela **Portas**, pela conferência do banco na
     partida ou pela conferência do cofre, quando ele está trancado ou não responde). O aplicativo recebe:
     "A porta de saída do MCP-CLARO está fechada agora. Nada foi enviado ao sistema. Tente mais tarde.";
   - um argumento traz o caractere nulo (U+0000), que o MCP-CLARO não guarda. O aplicativo recebe a recusa e
     pode pedir de novo sem esse caractere;
   - os argumentos têm mais de 500 níveis de listas e objetos, um dentro do outro (motivo `aninhamento_fundo`), ou
     um número que não é um número de verdade, NaN ou infinito (motivo `numero_invalido`);
   - a ferramenta não está na vitrine da pessoa (motivo `fora_da_vitrine`). Num servidor de terceiros, isso
     acontece também com a chave da Fase 3 desligada (salvo para pessoa de teste), sem a revisão do servidor ou
     com o servidor em quarentena. Se a quarentena entra no meio do pedido, a recusa diz isso (motivo
     `sistema_em_quarentena`). Num terceiro, a entrada também recusa argumentos grandes demais ou caros demais
     para conferir (veja "Ações em servidores de terceiros", abaixo).

   Nesses casos, nada entra na fila e nenhum limite é gasto. A recusa vai ao extrato.

   Se passar, a ação ganha um **número de protocolo** (`CLR-AAAA-NNNNNNNN`, com oito dígitos ou mais) e um
   **código de conferência** (três grupos de quatro letras e números). O código é o resumo do que vai seguir:
   mudou um argumento, mudou o código.
3. A ação passa por um de dois caminhos:
   - **ficha**: a pessoa confere nas telas do MCP-CLARO e confirma ou recusa. Ações de risco alto pedem a
     passkey; com a passkey desligada, pedem o login refeito na hora (veja abaixo). No período de experiência,
     tudo passa pela ficha. Com o **alerta dos três sinais** ligado para aquela pessoa e aquele aplicativo, também
     (veja "Limites por aproximação");
   - **limite por aproximação**: a pessoa criou antes um limite para aquela ferramenta, aqueles destinatários,
     aquela quantidade por dia e aquele prazo. Dentro dele, a ação segue sem ficha. Ação de risco alto nunca
     passa por limite.
4. Liberada, a ação espera a **hora de sair** (`OUTBOX_HOLD_SECONDS`, com relógio na tela). Até lá, a pessoa pode
   cancelar ou mandar seguir agora.
5. Um **despachante só** envia, e só com a porta de saída aberta. Antes, ele pergunta ao cofre se está
   destravado. Se não estiver, a ação continua liberada e espera (veja "O cofre está trancado"). Depois, ele
   confere tudo de novo (pessoa, aplicativo, bloqueios, ficha ou limite, código de conferência), marca
   "enviando" e chama o sistema **uma vez**, pelo caminho daquele sistema: um sistema próprio, direto; um
   servidor de terceiros, pela sala de visitas.
6. O resultado fica guardado. O aplicativo acompanha pela ferramenta `mcp_claro_consultar_protocolo`, que só
   mostra a mesma pessoa pelo mesmo aplicativo.

## Pedido igual

Um pedido igual tem a mesma pessoa, o mesmo aplicativo, a mesma ferramenta e os mesmos argumentos. Ele não abre
outra ação: devolve a original, e nada novo sai.

- A original que **ainda pode sair** (esperando a ficha, liberada ou enviando) conta em qualquer idade, mesmo
  que o pedido seja de dias atrás.
- A original que **já saiu** (enviada) ou ficou **não confirmada** conta por `DUPLICATE_WINDOW_MINUTES` (30
  minutos no início). A conta começa na última mudança dela: para a que saiu, na saída, e não no pedido.
- Uma não confirmada que a pessoa registrou como "não aconteceu" não conta: o pedido igual vira uma ação nova.
- Também não contam: não saiu, sistema pediu dados, vencida, cancelada, parada por bloqueio ou por
  restauração. Nesses casos, o pedido igual vira uma ação nova.

## O que cada estado quer dizer

| Estado | O que quer dizer | O que fazer |
| --- | --- | --- |
| Aguardando ficha | A pessoa ainda não confirmou | Nada; sem resposta no prazo (`CARD_TTL_MINUTES`), vence e nada segue. A pessoa recebe aviso depois de `NOTIFY_PENDING_AFTER_MINUTES` |
| Liberada | Confirmada, esperando a hora de sair | Dá para cancelar nas telas |
| Enviando | O despachante está chamando o sistema | Nada; não repita o pedido |
| Enviada | O sistema respondeu | O resultado está no protocolo |
| Não saiu | O sistema não executou o pedido: não recebeu, ou recusou na porta antes de executar | Pode pedir de novo. A tela diz: "Não saiu: o sistema não executou o pedido (não recebeu ou recusou na porta). Nada aconteceu." |
| Sistema pediu dados | O sistema quis mais no meio da ação: dados por formulário, que a pessoa abrisse um link, uma resposta da IA do aplicativo, a lista de pastas do aplicativo ou uma nova volta | O MCP-CLARO não repassa esses pedidos; ajuste no sistema. O detalhe diz o que ele pediu, só com essas palavras, nunca com o texto ou o link do sistema |
| Não confirmada | O pedido pode ter chegado ao sistema, mas a resposta não chegou inteira | A pessoa confere no sistema e registra: aconteceu, não aconteceu ou não sei. Nada é reenviado |
| Vencida | A ficha venceu sem resposta | Nada seguiu |
| Cancelada | A pessoa cancelou antes de sair, ou algo mudou antes do envio (cadastro, ordem de serviço, limite encerrado). Num servidor de terceiros, também quando ele entrou em quarentena ou quando a ferramenta deixou de valer para a pessoa (veja "Ações em servidores de terceiros") | Nada seguiu; o detalhe da ação diz o motivo |
| Parada por bloqueio | Um bloqueio ou uma pausa valeu antes de sair, ou a pessoa ou o aplicativo não estava ativo na hora de sair | Nada seguiu. A tela diz: "Parada por um bloqueio ou uma pausa. Nada seguiu." O detalhe diz o motivo (por exemplo, "o aplicativo está pausado"). Veja o comprovante do bloqueio |
| Parada por restauração | O banco voltou de um backup | Nada seguiu; a pessoa pede de novo, se ainda quiser |

"Não saiu" vale para a falha comprovada antes de executar: conexão recusada, recusa na porta (401, 403 ou
redirecionamento, no último status que o sistema deu), erro do protocolo que o sistema devolve antes de executar
(método ou argumentos inválidos, pedido ilegível, ou uma capacidade que o MCP-CLARO não tem: os códigos -32601,
-32602, -32600, -32700 e -32021), chave que não ficou pronta antes do envio (a pessoa ainda não autorizou o
sistema, a autorização venceu, a renovação falhou ou o cofre recusou abrir a chave: o detalhe diz "não deu para
abrir a chave no cofre") ou um pedido que nem chegou a ser escrito (argumentos com mais de 255 níveis, um dentro
do outro). Só um 401 no último status deixa a chave da pessoa naquele sistema dada como vencida: o próximo
pedido a renova antes de enviar, e a pessoa só autoriza de novo se a renovação for recusada. Um "recusado" sem um
desses status vira "não confirmada". Qualquer outra falha, ou a dúvida, vira "não confirmada". Num servidor de
terceiros, a sala de visitas tem regras a mais (veja "Ações em servidores de terceiros").

Com o cofre trancado ou sem resposta, a ação em geral não vira "não saiu": o despachante pergunta ao cofre antes
de marcar "enviando", e a ação espera. Ela só vira "não saiu" se o cofre trancar ou parar de responder no curto
intervalo entre essa pergunta e a abertura da chave (em geral, menos de um segundo; mais, quando a chave da
pessoa precisa ser renovada). O detalhe diz "o cofre de chaves está trancado" ou "o cofre de chaves não
respondeu". O cofre que responde mas recusa não fecha a porta: cada ação cuja chave ele recusa vira "não saiu",
com o detalhe "não deu para abrir a chave no cofre". Com o acesso do próprio despachante ao cofre vencido, isso
vale para a ação de qualquer sistema, até alguém corrigir o acesso.

## Limites por aproximação

- **Passkey.** Criar e ampliar um limite pedem a passkey. Com `FEATURE_PASSKEY` desligado, pedem o login
  refeito na hora.
- **Limite sem passkey não vale com a passkey ligada.** Um limite criado ou ampliado sem a passkey (por exemplo,
  quando ela estava desligada) não é gasto, e as ações passam pela ficha. A tela marca "Não vale com a passkey
  ligada" e não deixa ampliar. A pessoa cria outro limite, com a passkey: o antigo não conta como "limite
  igual". Depois, pode encerrar o antigo.
- **Só estes destinatários.** Quem diz quais campos do pedido são de destinatário é o ADMIN, na revisão da
  ferramenta (por exemplo, "para" e "cópia"). Num limite "só estes destinatários", o campo escolhido aceita só
  os valores escritos, e cada outro campo marcado pelo ADMIN tem de vir vazio ou faltar. O resto do pedido, como
  o texto da mensagem, fica livre. Um pedido com um campo que não está no esquema revisado da ferramenta vai
  para a ficha. Sem campo marcado pelo ADMIN, o limite só pode valer para qualquer destinatário.
- **O ADMIN marcou um campo novo.** Os limites "só estes" daquela ação que não conferem o campo novo são
  encerrados, e o que eles tinham liberado e ainda esperava a hora de sair é cancelado. A pessoa cria outro
  limite, se precisar. Criar um limite e essa revisão do ADMIN não se cruzam: um espera o outro.
- **Ampliar e encerrar.** Ampliar só aumenta (vezes por dia ou prazo) e segue o teto de agora. Para mudar os
  destinatários, encerre e crie outro. Encerrar cancela o que o limite liberou e ainda espera a hora de sair.
- O despachante confere o limite de novo antes de enviar. Se o limite foi encerrado ou suspenso, ou não cobre
  mais o pedido, a ação não sai.
- **Alerta dos três sinais.** Quando o aplicativo leu texto escrito por gente de fora (de um servidor de terceiros
  ou de uma ferramenta própria que o ADMIN marcou "traz texto de fora") e viu dados pessoais da pessoa, na janela
  de `THREE_SIGNALS_WINDOW_MINUTES` (240 minutos no início), os limites dessa pessoa com esse aplicativo não valem:
  toda ação vai para a ficha, sem consultar nem gastar limite, e a ficha mostra o motivo. O alerta acaba sozinho,
  quando a janela passa; a pessoa não o apaga. O aplicativo não fica sabendo do alerta. Uma ação já liberada antes
  do alerta sai. O texto de fora vindo de um servidor de terceiros só conta com a chave da Fase 3 ligada ou para
  pessoa de teste; o de ferramenta própria marcada conta sempre. As fichas do alerta contam no teto de fichas por
  hora do aplicativo.
- **Quarentena.** Quando um servidor de terceiros entra em quarentena, os limites ativos das pessoas com ele são
  suspensos, com uma linha no extrato de cada pessoa. O motivo do limite suspenso é "o servidor entrou em
  quarentena (mudou, parou); faça o limite de novo depois da nova revisão": o limite suspenso não volta sozinho,
  nem depois da nova revisão do ADMIN.

## Login refeito na hora

O login refeito é o reforço quando a passkey não entra: o MCP-CLARO manda a pessoa ao provedor de login, que
pede a senha de novo (`prompt=login` e `max_age=0`). Vale o login feito há no máximo
`SCREENS_RECENT_LOGIN_SECONDS` (300 segundos no início), pela hora que o provedor assina (`auth_time`). Pedem o
login refeito:

- confirmar uma ficha de risco alto que não confirma pela passkey (por exemplo, com a passkey desligada em
  `FEATURE_PASSKEY`);
- criar ou ampliar um limite, com a passkey desligada;
- cadastrar ou desativar uma passkey, mesmo com a passkey ligada. Com ela desligada, não dá para cadastrar;
  desativar continua possível e também pede o login refeito.

A cada passkey cadastrada ou desativada, o MCP-CLARO manda um aviso por e-mail à pessoa.

## Ações em servidores de terceiros

Um servidor de terceiros é um servidor MCP que não é da organização. A ação dele passa pelo mesmo caminho das
outras, com regras a mais. Hoje a chave da Fase 3 (`FEATURE_PHASE3_THIRD_PARTY`) continua desligada, e a partida
recusa ligá-la ([decisões](decisoes.md), D6): só pessoas de teste usam servidores de terceiros.

**Antes de virar protocolo.** Além das recusas que valem para todo pedido, a entrada mede os argumentos e recusa,
antes de conferir o esquema: texto de mais de 10.000 letras (também nome de campo), lista de mais de 1.000 itens,
número inteiro de mais de 100 dígitos, mais de 20.000 letras somando tudo, mais de 5.000 partes ou mais de 64
níveis de listas e objetos, um dentro do outro (motivo `argumento_grande_demais`). Depois, ela confere o esquema
revisado com um orçamento de 50.000 passos; passou dele, recusa (motivo `esquema_caro_demais`). Nos dois casos,
nada é guardado e nada sai. O detalhe de cada número está em [decisões](decisoes.md), "Mudanças deliberadas da Fase
3".

**Reconferência na hora de sair.** O despachante confere tudo de novo, como em toda ação, e mais duas coisas: a
chave da Fase 3 ligada, ou a pessoa é de teste; e a ferramenta publicada na revisão vigente do servidor (a última
revisão do servidor que o ADMIN concluiu, com o servidor ativo). Se uma das duas falta, a ação é cancelada com o
detalhe "servidores de terceiros desligados ou servidor sem revisão". Com o servidor em quarentena, é cancelada com
"o servidor entrou em quarentena (mudou, parou); nada seguiu".

**Quarentena.** Um servidor de terceiros entra em quarentena quando o que ele declara muda depois da revisão do
servidor (mesmo só o texto de uma ferramenta, uma ferramenta nova ou uma que sumiu), quando ele não responde à
conferência do mudou-parou em 2 rodadas seguidas, ou quando uma nova sondagem diz que ele não fala mais a
2026-07-28. Na mesma transação, a quarentena suspende os limites ativos das pessoas com ele e cancela o que
esperava a ficha ou a hora de sair, com uma linha no extrato de cada pessoa. Nessa ordem: primeiro a trava do
servidor, depois os limites, depois a fila, como a tela que encerra um limite; assim, as duas não se travam uma à
outra. O que já estava "enviando" segue: a quarentena e o envio tomam a mesma trava, e só um dos dois vem primeiro.
Quando a quarentena vem do mudou-parou, o ADMIN recebe um aviso por e-mail e pelos webhooks. As pessoas não recebem
aviso por e-mail: elas veem no extrato. Um pedido novo para um servidor em quarentena é recusado antes de virar
protocolo. Só uma nova conclusão da revisão do servidor, pelo ADMIN, o põe de volta em serviço; além dela, o banco só aceita desativá-lo.

**Como a ação sai.** Sempre pela sala de visitas, num processo novo para cada chamada. O despachante abre a chave
e manda o pedido à sala, que chama o servidor uma vez, só no endereço combinado (https e endereço público), sem
seguir desvios e sem dados de rastreio. Sem a sala, a ação não vai ao servidor. O despachante espera a sala pelo
prazo da chamada mais 10 segundos. Só a renovação da chave da pessoa, quando é preciso, não passa pela sala: ela sai
do despachante direto para o endereço de troca de chave do servidor (OAuth), conferido antes (https e IP público).

**"Não saiu" e "não confirmada" pela sala.**

- **Não saiu**, além dos casos de todo sistema (veja "O que cada estado quer dizer"):
  - a conexão com a sala falhou antes de o pedido sair: "a sala de visitas não está disponível; o pedido não chegou
    ao servidor de terceiros";
  - o endereço do servidor não é público ou não usa https: "o endereço do servidor de terceiros não é público ou
    não usa https; o pedido não chegou ao sistema";
  - argumentos com mais de 255 níveis, um dentro do outro: não saem por nenhum caminho, e a chave nem é aberta.
    Pela sala, argumentos com mais de uns 200 níveis também não chegam ao servidor: "o pedido não cabe no contrato
    da sala de visitas" (aqui a chave já foi aberta). Na prática, a entrada já recusa um terceiro acima de 64
    níveis.
- **Não confirmada**: qualquer outra resposta da sala que não seja a resposta do contrato (um erro 500, um corpo
  cortado, uma queda ou o prazo esgotado depois de enviar), e a resposta do servidor que não cabe no contrato da
  sala (por exemplo, funda demais). A sala pode ter chamado o servidor: nada é reenviado, e a pessoa confere no
  sistema. Uma resposta "dúvida" nunca vira "não saiu".

**Pedidos do servidor no meio da ação.** Se o servidor pede, no meio da ação, dados por formulário, que a pessoa
abra um link, uma resposta da IA do aplicativo, a lista de pastas do aplicativo ou uma nova volta, o MCP-CLARO não
repassa. Se o pedido é a própria resposta, a ação fica "sistema pediu dados". Se o servidor pediu e depois
respondeu normalmente, a ação fica "enviada", e o detalhe diz "antes pediu ... e o MCP-CLARO recusou". O detalhe
usa só essas palavras (para um pedido pelo fio antigo, "algo fora do padrão 2026-07-28"), nunca o texto, o link ou
os campos que o servidor mandou. A linha do fim no extrato guarda as categorias (`dados.pedidos`), para a tela do
ADMIN mostrar os pedidos recusados. Um link pedido pelo código antigo do protocolo (-32042) deixa a ação "não
confirmada". Isso vale igual para sistemas próprios.

**O que fica guardado e o que volta ao aplicativo.** O resultado fica guardado com a máscara dos campos pessoais e
sem o `_meta` do servidor: o único `_meta` guardado é a conta de partes sem texto que a leitura com medida tirou.
Ao aplicativo, o bloco "Protocolo ...: enviada uma vez ao sistema." vem primeiro, fora da moldura. Depois vêm o
aviso do MCP-CLARO, o aviso de partes retiradas (fora da moldura) e o texto do servidor, dentro da moldura de
estranhos, com uma marca nova a cada resposta. Uma resposta com mais de 199 níveis, ou que o kit não escreve, tem
um desfecho em cada caminho. Pela sala de visitas, o caminho de um servidor de terceiros, ela volta como dúvida, e a
ação fica "não confirmada". Pelo cliente local, o dos sistemas próprios, ela deixa a ação "enviada" sem resultado,
com o detalhe "o sistema respondeu; a resposta tem partes demais umas dentro das outras e o MCP-CLARO não a
guardou". Quando o aplicativo recebe o resultado de uma ação de terceiro, pela resposta, pela consulta do protocolo
ou por um pedido igual, a entrada grava o sinal "leu texto de fora" do alerta dos três sinais.

**Nas telas da pessoa.** A tela da ação não mostra o texto de um servidor de terceiros: ela diz "O servidor de
terceiros respondeu. A resposta foi entregue ao aplicativo, marcada como texto de estranhos." E lembra: "O MCP-CLARO
não repassa a você pedidos de dados, links ou confirmações que um sistema faça no meio de uma chamada. Um texto de
resposta ainda pode trazer um link: confirme ações só na tela do MCP-CLARO."

**Linhas da sala no extrato.** O que a sala barrou numa ação vai ao extrato da pessoa, com o protocolo:
`sala_fora` (a sala não estava disponível), `sala_endereco_recusado` (o endereço não é público ou não usa https)
e `sala_desvio_recusado` (o servidor pediu para ir a outro endereço, e o MCP-CLARO não seguiu). Cada uma soma o
contador do mesmo nome, com `onde=acao`. A linha da sala é gravada numa transação própria, logo antes da linha do
fim: se o banco falhar bem ali, só essa linha fica faltando (o log diz o tipo do erro, e o contador soma assim
mesmo); o estado da ação não muda.

**O que fica (riscos aceitos).** Todo status ou código que vira "não saiu" (401, 403 e desvio no último status, e
os códigos -32601, -32602, -32600, -32700 e -32021) pode ser forjado por um servidor que executou a ação e
respondeu assim. Nesse caso, um pedido igual sai de novo, só para esse mesmo servidor, sem alcance a outros
sistemas. Vale igual para sistema próprio desde a Fase 2. A lista completa está em [decisões](decisoes.md), "Riscos
aceitos da Fase 3".

## Casos do dia a dia

**"O aplicativo diz que não sabe se deu certo."** Procure o protocolo nas telas. Se estiver "não confirmada",
a pessoa confere no próprio sistema e registra a decisão. "Não aconteceu" libera um novo pedido igual;
"aconteceu" e "não sei" fazem o pedido igual devolver a mesma operação, dentro de `DUPLICATE_WINDOW_MINUTES`
(veja "Pedido igual").

**"Bloqueei no meio de uma fila cheia."** O bloqueio para, na mesma transação, tudo o que ainda não saiu daquela
pessoa, aplicativo ou sistema. O comprovante tem três partes:

1. **o que paramos a tempo**: o que esperava a ficha ou a hora de sair. Não saiu e não vai sair;
2. **o que já tinha seguido**, nas últimas 24 horas: enviada, sistema pediu dados, e não confirmada que a pessoa
   registrou como "aconteceu". O bloqueio não desfaz o que já seguiu;
3. **o que ainda está em conferência**: o que estava "enviando" quando o bloqueio chegou, e a não confirmada sem
   decisão ou com "não sei". Nada disso é reenviado; a pessoa confere no sistema e registra.

O comprovante não diz o que o sistema fez com o pedido. Isso fica para os sistemas de nível 3, na Fase 4.

**"Um aplicativo pediu confirmações demais."** Acima de `CONFIRM_CAP_PER_HOUR` fichas por hora, o aplicativo é
pausado sozinho (rotina `teto_confirmacoes`), e o que esperava na fila dele para. Quem reativa é o ADMIN,
depois de conferir: na tela **Bloqueios e pausas**, ele encerra essa pausa. O botão **Reativar** da tela
**Aplicativos** é outra coisa: vale só para a pausa da conferência do banco.

**"O despachante caiu."** Outra cópia assume a trava. Antes de enviar qualquer coisa, ela muda tudo o que estava
"enviando" para "não confirmada". Nada é reenviado sozinho.

**"A porta de saída está fechada."** Nenhuma ação nova ganha número: a entrada recusa antes. O que já estava na
fila espera, e o despachante não envia nada enquanto a saída estiver fechada. Quem reabre é o ADMIN, na tela
**Portas** da área do ADMIN, com motivo registrado. A exceção é a saída que a conferência do cofre fechou: ela
reabre sozinha quando o cofre volta (veja o caso abaixo).

**"O cofre está trancado."** Sem o cofre destravado, nenhuma chave de sistema abre. O despachante confere o
cofre a cada `VAULT_CHECK_SECONDS` (15 segundos no início) e logo antes de cada ação sair. Com a porta de saída
aberta:

- cofre trancado: a porta fecha na primeira conferência que o encontra assim;
- cofre sem resposta: a porta fecha na segunda conferência seguida sem resposta. Assim, uma falha de rede que
  passa antes da conferência seguinte não fecha a porta.

Com a porta fechada, nenhuma ação nova ganha número. O que já estava na fila espera, sem virar "não saiu".
Na tela da ação liberada, a pessoa lê que a porta de saída está fechada e que a ação sai quando ela reabrir (e
não antes da hora marcada).
Quando a porta reabre, cada ação é conferida de novo antes de sair. O extrato registra o fechamento. O ADMIN
recebe um aviso por e-mail (`NOTIFY_ADMIN_EMAILS`) e pelos webhooks que assinam o evento `cofre_trancado` ou
todos os eventos.

Quando o cofre volta destravado, depende de quem fechou a porta:

- **a conferência do cofre**: a porta reabre sozinha, se ninguém a mudou antes, e o ADMIN recebe o aviso
  `cofre_destravado`. Se ela fechou porque o cofre estava trancado, reabre na primeira conferência com ele
  destravado. Se fechou porque o cofre não respondia, reabre depois de duas conferências seguidas com ele
  destravado. Mas, se ela já tinha reaberto assim menos de uma hora antes de fechar de novo pela falta de
  resposta, espera o cofre ficar 30 minutos seguidos destravado. Assim, uma rede que vai e volta não abre e
  fecha a porta a cada pouco. Se, com a porta fechada porque o cofre não respondia, ele passa a responder
  trancado, a porta continua fechada desde a mesma hora: só o motivo muda para "trancado", com um aviso próprio
  ("o cofre voltou a responder, mas está trancado"), e vale a regra do cofre trancado. O ADMIN pode reabrir
  antes, na tela **Portas**;
- **o ADMIN ou a conferência do banco**: a porta continua fechada. Se a conferência encontra o cofre trancado,
  o ADMIN recebe um aviso. Quando depois o encontra destravado, recebe outro. Os avisos não se repetem a cada
  conferência, nem quando alguém fecha de novo a porta que já estava fechada: se o ADMIN fecha de novo a porta
  que o cofre fechou por estar trancado, ele não recebe outro aviso de cofre trancado, e recebe o de cofre de
  volta quando o cofre voltar. Se a porta tinha fechado porque o cofre não respondia, o aviso de cofre trancado
  ainda sai quando o cofre aparecer trancado. Os eventos são os mesmos (`cofre_trancado` e `cofre_destravado`), e o texto do aviso diz se a porta
  fechou ou se já estava fechada. Reabrir é decisão do ADMIN.

**"Restauramos o banco."** Na partida, a conferência de continuidade percebe a volta. A porta de saída fica
fechada e os aplicativos voltam pausados. O que estava "enviando" fica "não confirmada", e o que esperava na
fila fica "parada por restauração". O número de protocolo salta 1.000.000 para frente (e mais 1.000.000 quando
o roteiro grava a marca "restaurado"), para que um número dado depois da cópia não seja dado de novo a outra
ação. Reabrir a saída é decisão do ADMIN, registrada. Reativar cada aplicativo também: um assistente volta na
tela **Aplicativos** da área do ADMIN, botão **Reativar**, e um programa, na tela do programa
([restauração](restauracao.md)).

**"O vigia não respondeu na partida."** Em piloto e produção, a conferência exige o vigia
(`CONTINUITY_REQUIRE_WATCH`). Sem resposta dele, falta a testemunha de fora. A porta de saída fecha, os
aplicativos voltam pausados e o número de protocolo também salta. Mas a fila **não para**: a conferência não muda
o estado de nenhuma ação, e a saída fechada segura tudo. Reabrir a saída não reativa os aplicativos. Com a saída
reaberta, a ação de um aplicativo que já voltou só sai depois de conferida de novo. A de um aplicativo ainda pausado
fica parada, sem sair: a tela diz "Parada por um bloqueio ou uma pausa", com o detalhe "o aplicativo está
pausado". Para o que espera seguir, confira o vigia, reative os aplicativos e só depois reabra a saída. Um
assistente volta na área do ADMIN, tela **Aplicativos**, botão **Reativar**; um programa, na tela do programa
([restauração](restauracao.md)).

**"Um servidor de terceiros entrou em quarentena."** O ADMIN recebe o aviso (quando a quarentena vem do
mudou-parou) e vê, na tela do sistema, o bloco da quarentena com o que mudou. Nada dele chega às pessoas: o que
esperava para sair foi cancelado, e os limites das pessoas com ele foram suspensos; cada pessoa vê isso no próprio
extrato. Para voltar, o ADMIN lê as ferramentas de novo, revisa as que mudaram e conclui a revisão do servidor. Depois
disso, as pessoas fazem os limites de novo, se precisarem; até lá, as ações passam pela ficha. Se o servidor não
respondia, confira com o fornecedor antes.

**"A sala de visitas está fora."** Nenhum pedido MCP vai a servidor de terceiros: a ação vira "não saiu" e a
leitura volta com a recusa, as duas com a linha `sala_fora` no extrato. Os sistemas próprios seguem como sempre. A
conferência do mudou-parou não conta essa rodada contra os servidores de terceiros. Quando a sala volta, a pessoa
pode pedir de novo.

## O que nunca acontece

- A entrada nunca chama o sistema numa ação: quem envia é o despachante.
- Fora de desenvolvimento e teste, o despachante nunca chama as ferramentas de um servidor de terceiros direto:
  só pela sala de visitas. A troca e a renovação da autorização (OAuth) saem do despachante direto para o endereço
  de troca de chave do terceiro, conferido antes (https e IP público).
- O MCP-CLARO não repassa à pessoa nem ao aplicativo um pedido que o sistema faça no meio de uma chamada (dados,
  link, confirmação): ele vira uma recusa com as nossas palavras.
- O despachante não reenvia depois de dúvida, de 401 nem de queda.
- A ficha não vale para outra operação: o código de conferência da ficha precisa bater com o que vai seguir.
- Programas que rodam sozinhos não agem: com `PROGRAM_ACTIONS_ENABLED` ligado, a partida é recusada (decisão 9,
  [programas](programas.md)).
