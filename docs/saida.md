# Porta de saída: como uma ação sai

Este roteiro é para quem cuida do MCP-CLARO no dia a dia. Ele diz o que acontece com uma ação pedida por um
aplicativo, o que cada estado quer dizer e o que fazer em cada caso. O desenho completo está no
[plano](plano.md#a-porta-de-saída-por-dentro).

## O caminho de uma ação

1. O aplicativo pede uma ação (uma ferramenta que muda alguma coisa no sistema).
2. A entrada confere a vitrine, os argumentos e os bloqueios, e dá um **número de protocolo**
   (`CLR-AAAA-NNNNNNNN`) e um **código de conferência** (três grupos de quatro letras e números). O código é o
   resumo do que vai seguir: mudou um argumento, mudou o código.
3. A ação passa por um de dois caminhos:
   - **ficha**: a pessoa confere nas telas do MCP-CLARO e confirma ou recusa. Ações de risco alto pedem a passkey.
     No período de experiência, tudo passa pela ficha;
   - **limite por aproximação**: a pessoa criou antes, com passkey, um limite para aquela ferramenta, aqueles
     destinatários, aquela quantidade por dia e aquele prazo. Dentro dele, a ação segue sem ficha.
4. Liberada, a ação espera a **hora de sair** (`OUTBOX_HOLD_SECONDS`, com relógio na tela). Até lá, a pessoa pode
   cancelar ou mandar seguir agora.
5. Um **despachante só** envia. Ele confere tudo de novo, marca "enviando" e chama o sistema **uma vez**.
6. O resultado fica guardado. O aplicativo acompanha pela ferramenta `mcp_claro_consultar_protocolo`, que só
   mostra a mesma pessoa pelo mesmo aplicativo.

Um pedido igual (mesma pessoa, aplicativo, ferramenta e argumentos) dentro de `DUPLICATE_WINDOW_MINUTES` devolve
a operação original. Repetir o pedido não reenvia nada.

## O que cada estado quer dizer

| Estado | O que quer dizer | O que fazer |
| --- | --- | --- |
| Aguardando ficha | A pessoa ainda não confirmou | Nada; sem resposta no prazo (`CARD_TTL_MINUTES`), vence e nada segue. A pessoa recebe aviso depois de `NOTIFY_PENDING_AFTER_MINUTES` |
| Liberada | Confirmada, esperando a hora de sair | Dá para cancelar nas telas |
| Enviando | O despachante está chamando o sistema | Nada; não repita o pedido |
| Enviada | O sistema respondeu | O resultado está no protocolo |
| Não saiu | O sistema não recebeu, ou recusou na porta antes de executar | Pode pedir de novo |
| Sistema pediu dados | O sistema quis mais dados no meio da ação | O MCP-CLARO não repassa esses pedidos; ajuste no sistema |
| Não confirmada | O pedido pode ter chegado ao sistema, mas a resposta não chegou inteira | A pessoa confere no sistema e registra: aconteceu, não aconteceu ou não sei. Nada é reenviado |
| Vencida | A ficha venceu sem resposta | Nada seguiu |
| Cancelada | A pessoa cancelou antes de sair, ou o cadastro mudou | Nada seguiu |
| Parada por bloqueio | Um bloqueio ou uma pausa valeu antes de sair | Nada seguiu; veja o comprovante do bloqueio |
| Parada por restauração | O banco voltou de um backup | Nada seguiu; a pessoa pede de novo, se ainda quiser |

## Casos do dia a dia

**"O aplicativo diz que não sabe se deu certo."** Procure o protocolo nas telas. Se estiver "não confirmada",
a pessoa confere no próprio sistema e registra a decisão. "Não aconteceu" libera um novo pedido igual;
"aconteceu" e "não sei" fazem o pedido igual devolver a mesma operação.

**"Bloqueei no meio de uma fila cheia."** O bloqueio para, na mesma transação, tudo o que ainda não saiu daquela
pessoa, aplicativo ou sistema. O comprovante tem três partes: o que parou antes de sair, o que já tinha saído e o
que o sistema informou sobre o que saiu. Uma ação que já estava "enviando" quando o bloqueio chegou aparece como
já saída.

**"Um aplicativo pediu confirmações demais."** Acima de `CONFIRM_CAP_PER_HOUR` fichas por hora, o aplicativo é
pausado sozinho (rotina `teto_confirmacoes`), e o que esperava na fila dele para. Quem reativa é o ADMIN,
depois de conferir.

**"O despachante caiu."** Outra cópia assume a trava. Antes de enviar qualquer coisa, ela muda tudo o que estava
"enviando" para "não confirmada". Nada é reenviado sozinho.

**"Restauramos o banco."** Na partida, a conferência de continuidade percebe a volta. A porta de saída fica
fechada, os aplicativos voltam pausados, o que estava "enviando" fica "não confirmada" e o que esperava na fila
fica "parada por restauração". Reabrir a saída é decisão do ADMIN, registrada (`docs/restauracao.md`).

## O que nunca acontece

- A entrada nunca chama o sistema numa ação: quem envia é o despachante.
- O despachante não reenvia depois de dúvida, de 401 nem de queda.
- A ficha não vale para outra operação: o código de conferência da ficha precisa bater com o que vai seguir.
- Programas que rodam sozinhos não agem enquanto `PROGRAM_ACTIONS_ENABLED` estiver desligado (decisão 9).
