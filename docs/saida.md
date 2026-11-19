# Porta de saída: como uma ação sai

Este roteiro é para quem cuida do MCP-CLARO no dia a dia. Ele diz o que acontece com uma ação pedida por um
aplicativo, o que cada estado quer dizer e o que fazer em cada caso. O desenho completo está no
[plano](plano.md#a-porta-de-saída-por-dentro).

## O caminho de uma ação

1. O aplicativo pede uma ação (uma ferramenta que muda alguma coisa no sistema).
2. A entrada confere a vitrine, os argumentos e os bloqueios. Ela recusa o pedido **antes de dar número**
   quando:
   - um bloqueio ou uma pausa vale para aquela pessoa, aquele aplicativo ou aquele sistema;
   - a **porta de saída está fechada** (pelo ADMIN, na tela **Portas**, ou pela conferência do banco na
     partida). O aplicativo recebe: "A porta de saída do MCP-CLARO está fechada agora. Nada foi enviado ao
     sistema. Tente mais tarde.";
   - um argumento traz o caractere nulo (U+0000), que o MCP-CLARO não guarda. O aplicativo recebe a recusa e
     pode pedir de novo sem esse caractere.

   Nesses casos, nada entra na fila e nenhum limite é gasto. A recusa vai ao extrato.

   Se passar, a ação ganha um **número de protocolo** (`CLR-AAAA-NNNNNNNN`, com oito dígitos ou mais) e um
   **código de conferência** (três grupos de quatro letras e números). O código é o resumo do que vai seguir:
   mudou um argumento, mudou o código.
3. A ação passa por um de dois caminhos:
   - **ficha**: a pessoa confere nas telas do MCP-CLARO e confirma ou recusa. Ações de risco alto pedem a
     passkey; com a passkey desligada, pedem o login refeito na hora (veja abaixo). No período de experiência,
     tudo passa pela ficha;
   - **limite por aproximação**: a pessoa criou antes um limite para aquela ferramenta, aqueles destinatários,
     aquela quantidade por dia e aquele prazo. Dentro dele, a ação segue sem ficha. Ação de risco alto nunca
     passa por limite.
4. Liberada, a ação espera a **hora de sair** (`OUTBOX_HOLD_SECONDS`, com relógio na tela). Até lá, a pessoa pode
   cancelar ou mandar seguir agora.
5. Um **despachante só** envia, e só com a porta de saída aberta. Ele confere tudo de novo (pessoa, aplicativo,
   bloqueios, ficha ou limite, código de conferência), marca "enviando" e chama o sistema **uma vez**.
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
| Sistema pediu dados | O sistema quis mais dados no meio da ação | O MCP-CLARO não repassa esses pedidos; ajuste no sistema |
| Não confirmada | O pedido pode ter chegado ao sistema, mas a resposta não chegou inteira | A pessoa confere no sistema e registra: aconteceu, não aconteceu ou não sei. Nada é reenviado |
| Vencida | A ficha venceu sem resposta | Nada seguiu |
| Cancelada | A pessoa cancelou antes de sair, ou algo mudou antes do envio (cadastro, ordem de serviço, limite encerrado) | Nada seguiu; o detalhe da ação diz o motivo |
| Parada por bloqueio | Um bloqueio ou uma pausa valeu antes de sair, ou a pessoa ou o aplicativo não estava ativo na hora de sair | Nada seguiu; o detalhe diz o motivo (por exemplo, "o aplicativo está pausado"). Veja o comprovante do bloqueio |
| Parada por restauração | O banco voltou de um backup | Nada seguiu; a pessoa pede de novo, se ainda quiser |

"Não saiu" vale para a falha comprovada antes de executar: conexão recusada, recusa na porta (401, 403,
redirecionamento), erro do protocolo que o sistema devolve antes de executar (método ou argumentos
inválidos) ou chave que não ficou pronta antes do envio (a pessoa ainda não autorizou o sistema, a autorização
venceu, a renovação falhou ou o cofre não respondeu). Num 401, a chave da pessoa naquele sistema fica dada como
vencida: o próximo pedido a renova antes de enviar, e a pessoa só autoriza de novo se a renovação for recusada.
Qualquer outra falha, ou a dúvida, vira "não confirmada".

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
depois de conferir.

**"O despachante caiu."** Outra cópia assume a trava. Antes de enviar qualquer coisa, ela muda tudo o que estava
"enviando" para "não confirmada". Nada é reenviado sozinho.

**"A porta de saída está fechada."** Nenhuma ação nova ganha número: a entrada recusa antes. O que já estava na
fila espera, e o despachante não envia nada enquanto a saída estiver fechada. Quem reabre é o ADMIN, na tela
**Portas** da área do ADMIN, com motivo registrado.

**"Restauramos o banco."** Na partida, a conferência de continuidade percebe a volta. A porta de saída fica
fechada e os aplicativos voltam pausados. O que estava "enviando" fica "não confirmada", e o que esperava na
fila fica "parada por restauração". O número de protocolo salta 1.000.000 para frente (e mais 1.000.000 quando
o roteiro grava a marca "restaurado"), para que um número dado depois da cópia não seja dado de novo a outra
ação. Reabrir a saída é decisão do ADMIN, registrada ([restauração](restauracao.md)).

**"O vigia não respondeu na partida."** Em piloto e produção, a conferência exige o vigia
(`CONTINUITY_REQUIRE_WATCH`). Sem resposta dele, falta a testemunha de fora. A porta de saída fecha, os
aplicativos voltam pausados e o número de protocolo também salta. Mas a fila **não para**: a conferência não muda
o estado de nenhuma ação, e a saída fechada segura tudo. Reabrir a saída não reativa os aplicativos. Com a saída
reaberta, a ação de um aplicativo que já voltou sai na hora dela, depois de conferida de novo. A de um aplicativo
ainda pausado fica parada, sem sair ("parada por bloqueio", com o detalhe "o aplicativo está pausado"). Para o que
espera seguir, confira o vigia, reative os aplicativos e só depois reabra a saída ([restauração](restauracao.md)).

## O que nunca acontece

- A entrada nunca chama o sistema numa ação: quem envia é o despachante.
- O despachante não reenvia depois de dúvida, de 401 nem de queda.
- A ficha não vale para outra operação: o código de conferência da ficha precisa bater com o que vai seguir.
- Programas que rodam sozinhos não agem: com `PROGRAM_ACTIONS_ENABLED` ligado, a partida é recusada (decisão 9,
  [programas](programas.md)).
