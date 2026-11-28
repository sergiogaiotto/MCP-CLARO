"""Telas da pessoa na Fase 2: caixa de pendencias, ficha, espera visivel, "Minhas acoes" e "estamos confirmando".

- A ficha mostra o que vai acontecer (ferramenta pelo nome da vitrine, sistema e os dados, escapados), o
  numero de protocolo, o codigo de conferencia em destaque e o prazo. O que o aplicativo diz fica numa caixa
  a parte, marcada como texto dele, nao conferido pelo MCP-CLARO.
- Confirmar e recusar sao POST com CSRF. Ficha de risco alto (``exige_passkey``) so confirma com passkey: a
  conferencia e no servidor, no mesmo POST e na mesma transacao que confirma (``telas/passkeys.py``).
- Ficha de risco alto que nao confirma pela passkey (``FEATURE_PASSKEY`` desligado, inclusive a ficha que
  nasceu pedindo passkey, ou ficha montada quando a passkey estava desligada) confirma so com o login refeito
  na hora (``Telas.login_recente``): a tela manda refazer o login no provedor e volta para a ficha.
- Os dados do pedido aparecem com os caracteres escondidos (invisiveis, de formato e de direcao, como U+202E)
  trocados por marcas visiveis como ``[U+202E]``, e a ficha avisa em destaque quando ha algum.
- Depois de confirmar, a pagina da acao mostra a hora de sair (hora de Brasilia) e quanto falta; sem
  JavaScript, a hora fica escrita; com o htmx, a parte do estado se atualiza sozinha. Da para cancelar
  antes de sair ou seguir agora.
- Resultado nao confirmado: a pessoa registra o que apurou (aconteceu, nao aconteceu, nao sei). Nada e
  reenviado.

Fase 3 (servidores de terceiros):

- Resultado de estranhos (servidor de terceiros, ou ferramenta propria marcada "traz texto de fora"): a tela da
  acao nao mostra o texto (critica A4). Diz que o servidor respondeu e que a resposta foi ao aplicativo marcada
  como texto de estranhos. So se a pessoa pedir (``?ver=resposta``, atras de um aviso), o texto aparece como foi
  ao aplicativo por dentro da moldura (``regras.estranhos.limpar_por_dentro``), escapado e sem link. Nem as
  pendencias, nem a lista, nem o extrato mostram texto de um sistema.
- A ficha de um terceiro avisa que o servidor e de terceiros; a ficha que nasceu com o alerta dos tres sinais
  (``Operacao.ficha_sinais``) mostra o cartao do alerta, so com nomes do cadastro e horas de Brasilia.
- O desfecho aparece com acento (``com_acento``), e os fins novos da porta (resposta funda demais, terceiros
  desligados, quarentena, pedido fundo demais, sala fora, pedido barrado) ganham uma explicacao para leigo. O
  desfecho e sempre texto nosso.
- A acao que a retomada deu como nao confirmada, cujo fim (que chegou depois) foi um "nao saiu" sem envio (sala fora,
  pedido fundo demais, chave que nao abriu...), nao diz que o pedido saiu uma vez: diz que foi dada como nao
  confirmada antes de o envio terminar e que, pelo que o MCP-CLARO viu depois, o pedido nao chegou ao sistema
  (``nao_chegou_depois_da_retomada``). A caixa de pendencias diz o mesmo dessa acao, no grupo "Estamos confirmando".

Fase 4 (selos de nivel 2 e 3, desenho 7.1, com as correcoes da critica):

- A ficha mostra o cartao "Selo do sistema" pelo retrato da hora (``fichas.selo``, ``Operacao.selo``), com o aviso de
  terceiro junto; a ficha que espera diz quando o selo de agora (lido do banco, efetivo para a pessoa) e outro (M3).
  Nenhuma tela do selo promete o que o sistema fara: "o selo diz como o sistema respondeu no teste; nao garante o que
  ele fara depois".
- A ficha do pedido igual diz qual protocolo ele repete e se vai com o mesmo numero (B12); com o numero queimado, que
  e um pedido novo para o sistema (A3.3); com ``desconhecido`` no fim da escada da original, o aviso e a caixa do
  numero novo (M4.2, ``Operacao.original_ficou_em_desconhecido``).
- A acao "nao confirmada" que saiu com numero mostra "O que o sistema informou" (``o_que_o_sistema_informou``): so o
  vocabulario do contrato (``paginas.ESTADOS_LEGIVEIS``) e a hora do gateway (``gravado_em``, nunca ``depois[].em``),
  marcado como dito pelo sistema (ou pelo servidor de terceiros, M9). O botao "Registrar o que o sistema informou"
  so aparece com estado final (``informado.estado_final``) que cabe num toque (``fila.DECISAO_PELO_SISTEMA``), sem
  outro protocolo com o mesmo numero, sem contradicao do sistema, nunca para ``nao_recebida`` de um terceiro e so
  antes da decisao (que e uma so). A resposta ao aviso de bloqueio aparece como o que o sistema disse, nunca como
  final. A escada acabada (``Operacao.consulta_acabou``) troca "continuamos perguntando" por "o sistema nao
  confirmou; a decisao e sua". Depois da decisao, nenhuma frase convida a decidir ou a registrar de novo: o bloco diz
  "vale a decisao que voce registrou".
- A ficha do pedido igual e as listas dizem quem informou: o sistema ou, num terceiro, o servidor de terceiros (M9).
- As listas ("Minhas acoes" e a caixa de pendencias) levam a etiqueta "O sistema informou: {estado}".

As mudancas de estado sao todas de ``saida/fila.py``; aqui so se mostra e se pede.
"""

from __future__ import annotations

import json
import re
import sys
import unicodedata
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

from markupsafe import Markup, escape
from sqlalchemy.exc import SQLAlchemyError
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from mcp_claro.logs import logger
from mcp_claro.regras import estado, estranhos, politica, sinais

# A marca dos caracteres escondidos vem de ``regras/escondidos.py`` (sem copia aqui). Os nomes ficam exportados
# daqui tambem, para quem ja os usava pelas telas (``paginas.criar_ambiente``, os limites).
from mcp_claro.regras.escondidos import CATEGORIAS_ESCONDIDAS as CATEGORIAS_ESCONDIDAS
from mcp_claro.regras.escondidos import ESCONDIDOS_DITOS as ESCONDIDOS_DITOS
from mcp_claro.regras.escondidos import escondido as escondido
from mcp_claro.regras.escondidos import marcar_escondidos as marcar_escondidos
from mcp_claro.saida import fila
from mcp_claro.saida.estados import FRASE, Estado
from mcp_claro.saida.fila import DecisaoRecusada, Operacao
from mcp_claro.saida.protocolo import FUSO, protocolo_valido
from mcp_claro.selo import contrato, vigente
from mcp_claro.telas import consultas, passkeys
from mcp_claro.telas.nucleo import Telas, tela, ver_depois
from mcp_claro.telas.paginas import AVISOS, estado_legivel

log = logger("mcp_claro.telas.acoes")

AVISOS_ACOES: Final[dict[str, str]] = {
    "confirmada": "Pronto. A ficha foi confirmada. A ação espera a hora de sair; ainda dá para cancelar.",
    "recusada": "Pronto. A ficha foi recusada. Nada foi para o sistema.",
    "cancelada": "Pronto. A ação foi cancelada antes de sair. Nada chegou ao sistema.",
    "seguir_agora": "Pronto. A ação sai agora, sem esperar o resto do tempo.",
    "seguir_agora_saida_fechada": "Pronto. A ação não espera mais o resto do tempo. A porta de saída está fechada"
    " agora: ela sai quando a porta reabrir, depois de conferida de novo.",
    "decisao_registrada": "Pronto. Sua decisão foi registrada. Nada será reenviado.",
}

# As frases de ``estados.FRASE``, com acento, para a tela. Se uma delas mudar la, a tela volta a mostrar a de la.
FRASE_TELA: Final[dict[Estado, str]] = {
    Estado.AGUARDANDO_FICHA: "A ficha espera a sua confirmação na tela do gateway.",
    Estado.LIBERADA: "Confirmada. A ação espera a hora de sair; ainda dá para cancelar.",
    Estado.ENVIANDO: "Estamos enviando ao sistema. Não repita o pedido.",
    Estado.ENVIADA: "Enviada uma vez ao sistema.",
    Estado.NAO_SAIU: "Não saiu: o sistema não executou o pedido (não recebeu ou recusou na porta). Nada aconteceu.",
    Estado.SISTEMA_PEDIU_DADOS: "O sistema pediu mais dados no meio da ação; o MCP-CLARO não repassa esses pedidos.",
    Estado.NAO_CONFIRMADA: "Resultado não confirmado: o pedido pode ter chegado ao sistema. Nada será reenviado; "
    "a pessoa decide na tela do gateway.",
    Estado.VENCIDA: "A ficha venceu sem resposta. Nada seguiu.",
    Estado.CANCELADA: "Cancelada antes de sair. Nada seguiu.",
    Estado.PARADA_POR_BLOQUEIO: "Parada por um bloqueio ou uma pausa. Nada seguiu.",
    Estado.PARADA_POR_RESTAURACAO: "Parada porque o banco foi restaurado. Nada seguiu sem nova confirmação.",
}

ROTULOS: Final[dict[Estado, str]] = {
    Estado.AGUARDANDO_FICHA: "Esperando a sua decisão",
    Estado.LIBERADA: "Na espera para sair",
    Estado.ENVIANDO: "Saindo agora",
    Estado.ENVIADA: "Enviada",
    Estado.NAO_SAIU: "Não saiu",
    Estado.SISTEMA_PEDIU_DADOS: "O sistema pediu mais dados",
    Estado.NAO_CONFIRMADA: "Estamos confirmando",
    Estado.VENCIDA: "Venceu sem resposta",
    Estado.CANCELADA: "Cancelada",
    Estado.PARADA_POR_BLOQUEIO: "Parada por bloqueio ou pausa",
    Estado.PARADA_POR_RESTAURACAO: "Parada por restauração",
}

LOGIN_REFEITO_FICHA: Final = (
    "Esta ação é de risco alto: para confirmar sem a passkey, entre de novo agora e confirme logo depois. Nada seguiu."
)

FICHA_ALHEIA: Final = (404, "Esta ficha não é sua ou não existe.")

# Recusas de ``fila`` (``DecisaoRecusada.motivo``) em linguagem simples, com o status da resposta.
RECUSAS: Final[dict[str, tuple[int, str]]] = {
    "ficha_desconhecida": (404, "Ficha não encontrada. Ela não é sua ou não existe."),
    "ficha_ja_decidida": (409, "Esta ficha já foi decidida antes. Nada mudou."),
    "ficha_vencida": (409, "O prazo desta ficha acabou. Nada seguiu para o sistema."),
    "falta_passkey": (403, "Esta ação é de risco alto: confirme com a sua passkey. Nada seguiu."),
    "falta_login_refeito": (403, LOGIN_REFEITO_FICHA),
    "codigo_nao_bate": (409, "O código de conferência não bate com o pedido. Por segurança, nada seguiu."),
    "nao_esta_aguardando": (409, "Esta ação não espera mais confirmação. Veja o andamento dela."),
    "operacao_desconhecida": (404, "Ação não encontrada. Ela não é sua ou não existe."),
    "nao_esta_esperando": (409, "Esta ação não está mais na espera. Nada mudou."),
    "decisao_invalida": (400, "Escolha uma das respostas: aconteceu, não aconteceu ou ainda não sei."),
    "nao_e_nao_confirmada": (409, "Só um resultado não confirmado pede esta decisão. Nada mudou."),
    "ja_decidida": (409, "Esta decisão já tinha sido registrada. Nada mudou."),
    # Fase 4: o botao "Registrar o que o sistema informou" e a escolha do numero novo na ficha.
    "sem_resposta_final": (
        409,
        "O sistema ainda não deu uma resposta final sobre este pedido. Nada foi registrado; decida pelo formulário.",
    ),
    "resultado_em_outro_protocolo": (
        409,
        "Outro protocolo saiu depois com o mesmo número; o que o sistema informar aparece nele. Nada foi registrado.",
    ),
    "terceiro_nao_recebida": (
        409,
        "Um servidor de terceiros disse que não recebeu o pedido. Confira no próprio servidor antes de decidir. Nada"
        " foi registrado.",
    ),
    "decida_pelo_formulario": (
        409,
        "O que o sistema informou não cabe num toque (feito só em parte, ou feito e depois desfeito). Nada foi"
        " registrado; decida pelo formulário.",
    ),
    "informado_mudou": (
        409,
        "O que o sistema informou mudou depois que a tela foi aberta. Nada foi registrado; veja a tela de novo.",
    ),
    "sistema_se_contradisse": (
        409,
        "O sistema deu respostas que não combinam sobre este pedido. Nada foi registrado; confira no sistema e decida"
        " pelo formulário.",
    ),
    "numero_novo_nao_cabe": (
        409,
        "O número novo só vale quando o sistema não confirmou o primeiro pedido e a consulta terminou sem saber."
        " Nada seguiu; veja a ficha de novo.",
    ),
}

DECISOES: Final[dict[str, str]] = {
    "aconteceu": "Aconteceu no sistema",
    "nao_aconteceu": "Não aconteceu no sistema",
    "nao_sei": "Ainda não sei",
}
RISCOS: Final[dict[str, str]] = {"baixo": "Risco baixo", "medio": "Risco médio", "alto": "Risco alto"}
MAXIMO_OBSERVACAO: Final = 500
NUMERO_NOVO_MARCADO: Final = "sim"
"""O valor da caixa "mandar com numero novo" da ficha do pedido igual (critica M4.2)."""
SEGUNDOS_ENTRE_ATUALIZACOES: Final = 2
VER_RESPOSTA: Final = "resposta"
"""``/telas/acoes/{id}?ver=resposta``: a pessoa pediu para ver o texto de estranhos (critica A4)."""


def frase_do_estado(estado: Estado) -> str:
    com_acento = FRASE_TELA.get(estado)
    if com_acento is not None and sem_acento(com_acento) == FRASE[estado]:
        return com_acento
    return FRASE[estado]


def recusa_legivel(erro: DecisaoRecusada) -> tuple[int, str]:
    """(status, frase) da recusa. A frase do ``ja_saiu`` traz o estado atual, que a propria recusa ja diz."""
    if erro.motivo == "ja_saiu":
        estado = next((e for e in Estado if FRASE[e] in erro.mensagem), None)
        atual = frase_do_estado(estado) if estado is not None else erro.mensagem
        return 409, f"Não deu para cancelar: a ação já tinha saído. {atual}"
    return RECUSAS.get(erro.motivo, (409, erro.mensagem))


# Palavras e trechos das frases nossas (desfechos das acoes, motivos dos limites), escritos sem acento como o
# extrato, com o acento que a tela poe. So troca a palavra inteira (ou o trecho inteiro), e so por ela mesma com
# acento: ``sem_acento(com_acento(t)) == sem_acento(t)`` sempre. "esta" e "e" so dentro de trechos, porque
# sozinhos tambem sao outras palavras ("esta acao", "e" de "e tambem").
_COM_ACENTO: Final = (
    "não",
    "ação",
    "ações",
    "revisão",
    "padrão",
    "formulário",
    "níveis",
    "endereço",
    "não é público",
    "código",
    "conferência",
    "serviço",
    "faça",
    "só",
    "já",
    "até",
    "também",
    "será",
    "você",
    "autorização",
    "definição",
    "instalação",
    "disponível",
    "inalcançável",
    "versão",
    "versões",
    "responsável",
    "período",
    "experiência",
    "destinatário",
    "destinatários",
    "resposta válida",
    "está trancado",
    "está bloqueado",
    "está pausado",
    "está ativo",
    "está ativa",
    "está valendo",
    "está disponível",
    "está fechada",
)


def sem_acento(texto: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(ch))


def _variantes(trecho: str) -> dict[str, str]:
    return {sem_acento(trecho): trecho, sem_acento(trecho[:1].upper() + trecho[1:]): trecho[:1].upper() + trecho[1:]}


_TROCAS: Final[dict[str, str]] = {cru: certo for trecho in _COM_ACENTO for cru, certo in _variantes(trecho).items()}
# Os trechos maiores primeiro: "nao esta valendo" vira "não está valendo", e nao "não esta valendo".
_ACHAR: Final = re.compile(r"\b(" + "|".join(re.escape(cru) for cru in sorted(_TROCAS, key=len, reverse=True)) + r")\b")


def com_acento(texto: str) -> str:
    """Uma frase nossa, escrita sem acento (como o extrato), com o acento das palavras que a tela conhece.

    So para texto do MCP-CLARO (desfecho de acao, motivo de limite): nunca para texto de um sistema."""
    return _ACHAR.sub(lambda m: _TROCAS[m.group(1)], texto)


def _cada_texto(valor: Any) -> Iterator[str]:
    """Cada texto de um valor, com as chaves de objetos e os itens de listas, na ordem em que aparecem.

    Sem recursao: uma pilha explicita. Um valor com milhares de niveis, um dentro do outro (os argumentos ou o
    resultado guardado de uma acao), nao estoura a pilha do Python na tela."""
    pilha: list[Any] = [valor]
    while pilha:
        atual = pilha.pop()
        if isinstance(atual, str):
            yield atual
        elif isinstance(atual, dict):
            itens: list[Any] = []
            for chave, item in atual.items():
                itens.append(str(chave))
                itens.append(item)
            pilha.extend(reversed(itens))
        elif isinstance(atual, list | tuple):
            pilha.extend(reversed(atual))


def _textos(valor: Any) -> list[str]:
    """Todos os textos de um valor, com as chaves de objetos e os itens de listas (sem recursao)."""
    return list(_cada_texto(valor))


def tem_escondidos(valor: Any) -> bool:
    """Algum texto do valor (chave ou item, em qualquer nivel) tem caractere escondido? Para no primeiro."""
    return any(escondido(ch) for texto in _cada_texto(valor) for ch in texto)


def valor_legivel(valor: Any) -> str:
    if isinstance(valor, str):
        return marcar_escondidos(valor)
    if isinstance(valor, bool):
        return "sim" if valor else "não"
    if valor is None:
        return "(vazio)"
    if isinstance(valor, int | float):
        return str(valor)
    return marcar_escondidos(json.dumps(valor, ensure_ascii=False, sort_keys=True))


def argumentos_legiveis(argumentos: dict[str, Any]) -> list[tuple[str, str]]:
    """Os dados do pedido em lista "chave: valor", com os escondidos marcados nas chaves e nos valores.

    O escape do HTML fica com o modelo (Jinja2 com escape automatico)."""
    return [(marcar_escondidos(str(chave)), valor_legivel(valor)) for chave, valor in argumentos.items()]


def linhas_do_aplicativo(diz: str | None) -> list[str]:
    """O que o aplicativo diz, linha a linha, com os escondidos marcados (a entrada ja tira os nao imprimiveis;
    aqui e a segunda barreira, para o que a tela mostra)."""
    return marcar_escondidos(diz).split("\n") if diz else []


def textos_do_resultado(resultado: dict[str, Any] | None) -> list[str]:
    """So o texto dos blocos de conteudo da resposta do sistema. Imagem, recurso e o resto nao aparecem."""
    if not isinstance(resultado, dict):
        return []
    blocos = resultado.get("content")
    if not isinstance(blocos, list):
        return []
    return [
        b["text"] for b in blocos if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    ]


def textos_de_estranhos(resultado: dict[str, Any] | None) -> list[str]:
    """O texto da resposta de estranhos (servidor de terceiros ou ferramenta marcada "traz texto de fora"), so para
    quando a pessoa pede para ver (critica A4): como foi ao aplicativo por dentro da moldura, com CPF e e-mail
    mascarados, os caracteres escondidos marcados e o comeco de moldura forjado (``[[estranhos``) neutralizado. O
    escape do HTML fica com o modelo, e nada vira link."""
    return [estranhos.limpar_por_dentro(t) for t in textos_do_resultado(resultado)]


# Explicacao para leigo, alem do desfecho, dos fins que a Fase 3 trouxe (as constantes vem de quem as grava; aqui
# so as chaves, para nao importar a porta de saida nas telas). O desfecho em si aparece com acento (``com_acento``).
_EXPLICACOES: Final[dict[str, str]] = {
    "o sistema respondeu; a resposta tem partes demais umas dentro das outras e o MCP-CLARO nao a guardou": (
        "A ação saiu uma vez e o sistema respondeu, mas a resposta tinha partes demais umas dentro das outras."
        " O MCP-CLARO não a guardou e não a mostra. Nada será reenviado: confira no próprio sistema."
    ),
    "servidores de terceiros desligados ou servidor sem revisao": (
        "Os servidores de terceiros estão desligados nesta instalação, ou esta ferramenta não está na revisão do"
        " servidor que vale agora. Nada chegou ao servidor."
    ),
    "o servidor entrou em quarentena (mudou, parou); nada seguiu": (
        "O servidor mudou depois da revisão do ADMIN e entrou em quarentena. Nada chegou a ele. Depois da nova"
        " revisão, peça de novo, se ainda precisar."
    ),
    "o pedido tem niveis demais, um dentro do outro, para o MCP-CLARO escrever; o pedido nao chegou ao sistema": (
        "O pedido tinha partes demais umas dentro das outras para o MCP-CLARO escrever. Nada saiu."
    ),
    "a sala de visitas nao esta disponivel; o pedido nao chegou ao servidor de terceiros": (
        "A sala de visitas, por onde o MCP-CLARO fala com servidores de terceiros, não estava disponível. Nada saiu."
    ),
    "a sala de visitas estava cheia; nada saiu": (
        "A sala de visitas, por onde o MCP-CLARO fala com servidores de terceiros, estava cheia: muitas chamadas ao"
        " mesmo tempo. Nada saiu. Peça de novo em instantes, se ainda precisar."
    ),
}
# O pedido barrado (``saida.despachante._classificar``): ou e o fim inteiro (estado "o sistema pediu mais dados"),
# ou vem depois do fim, como um rabo: "; antes pediu X e o MCP-CLARO recusou" numa acao enviada (com ou sem
# resultado guardado) e "; o sistema pediu X e o MCP-CLARO recusou" nos outros fins.
_PEDIU_NO_MEIO: Final = re.compile(r"^o sistema pediu .+ no meio da acao")
_PEDIU_NO_FIM: Final = re.compile(r"; (antes|o sistema) pediu .+ e o MCP-CLARO recusou$")
_EXPLICACAO_PEDIU: Final = (
    "O MCP-CLARO não repassa a você o que o sistema pede no meio de uma chamada (dados, links, confirmações) e não"
    " reenvia a ação."
)


def detalhe_do_desfecho(desfecho: str | None) -> tuple[str, str]:
    """(o desfecho com acento, a explicacao para leigo ou ""). O desfecho e sempre texto nosso: o que o sistema
    pediu no meio entra so com o nosso vocabulario (``ligacao.ponto.pedidos_legiveis``), nunca texto ou link dele.

    Com o pedido barrado no rabo, a explicacao do fim (por exemplo, a resposta funda demais) vem antes da do
    pedido: uma nao apaga a outra.

    A resposta que chega depois de a retomada dar a acao como nao confirmada leva a marca
    ``fila.DEPOIS_DA_RETOMADA`` no fim do desfecho. O rabo e o fim sao procurados sem a marca; o desfecho mostrado
    continua inteiro, com ela."""
    if not desfecho:
        return "", ""
    sem_marca = desfecho.removesuffix(fila.DEPOIS_DA_RETOMADA)
    fim, pediu = sem_marca, bool(_PEDIU_NO_MEIO.search(sem_marca))
    rabo = _PEDIU_NO_FIM.search(sem_marca)
    if rabo is not None:
        fim, pediu = sem_marca[: rabo.start()], True
    partes = [_EXPLICACOES.get(fim, ""), _EXPLICACAO_PEDIU if pediu else ""]
    return com_acento(desfecho), " ".join(p for p in partes if p)


# Os fins de "nao saiu" da porta de saida em que o pedido nem saiu do MCP-CLARO para o sistema: os de
# ``saida.despachante._NAO_ENVIADO``, o padrao dele (o "nao enviado" sem motivo conhecido) e as chaves que nao abriram
# (``PortaDeSaida._chave``). Ficam de fora os fins em que o pedido saiu e o sistema recusou (na porta ou antes de
# executar). Como em ``_EXPLICACOES``, so os textos, sem importar a porta de saida nas telas; o teste
# ``test_telas_f5a_pessoa`` le os fins do codigo da porta e confere esta lista.
_NAO_CHEGOU: Final[frozenset[str]] = frozenset(
    {
        "a sala de visitas nao esta disponivel; o pedido nao chegou ao servidor de terceiros",
        "a sala de visitas estava cheia; nada saiu",
        "o endereco do servidor de terceiros nao e publico ou nao usa https; o pedido nao chegou ao sistema",
        "o pedido tem niveis demais, um dentro do outro, para o MCP-CLARO escrever; o pedido nao chegou ao sistema",
        "o pedido nao cabe no contrato da sala de visitas; o pedido nao chegou ao sistema",
        "o pedido nao chegou ao sistema",
        "a pessoa ainda nao autorizou o sistema",
        "a autorizacao no sistema venceu",
        "nao deu para renovar a chave",
        "o cofre de chaves esta trancado",
        "o cofre de chaves nao respondeu",
        "nao deu para abrir a chave no cofre",
    }
)


def nao_chegou_depois_da_retomada(desfecho: str | None) -> bool:
    """A retomada deu a acao como nao confirmada antes de o envio terminar, e o fim que chegou depois (o desfecho
    com a marca ``fila.DEPOIS_DA_RETOMADA``) diz que o pedido nem saiu do MCP-CLARO (``_NAO_CHEGOU``). O rabo do
    pedido barrado, se houver, nao muda o fim.

    A tela da acao nao confirmada, nesse caso, nao diz "O pedido saiu uma vez": a explicacao do fim diz "Nada
    saiu.", e e ela que vale pelo que a porta viu."""
    if not desfecho or not desfecho.endswith(fila.DEPOIS_DA_RETOMADA):
        return False
    fim = desfecho.removesuffix(fila.DEPOIS_DA_RETOMADA)
    rabo = _PEDIU_NO_FIM.search(fim)
    if rabo is not None:
        fim = fim[: rabo.start()]
    return fim in _NAO_CHEGOU


def janela_legivel(minutos: int) -> str:
    """O comeco da frase do alerta: "Nas últimas 2 horas", "Nos últimos 30 minutos", "Na última hora"."""
    minutos = max(1, int(minutos))
    dias, resto_dia = divmod(minutos, 1440)
    horas, resto = divmod(minutos, 60)
    if not resto_dia:
        return "No último dia" if dias == 1 else f"Nos últimos {dias} dias"
    if not resto:
        return "Na última hora" if horas == 1 else f"Nas últimas {horas} horas"
    return "No último minuto" if minutos == 1 else f"Nos últimos {minutos} minutos"


def hora_e_dia(momento: datetime | None) -> str:
    """ "14:05 de 21/11/2026", na hora de Brasilia."""
    if momento is None:
        return "?"
    return momento.astimezone(ZoneInfo(FUSO)).strftime("%H:%M de %d/%m/%Y")


def hora_curta(momento: datetime, agora: datetime | None = None) -> str:
    """ "14:05", na hora de Brasilia; com o dia ("14:05 de 22/11/2026") se nao for hoje."""
    local = momento.astimezone(ZoneInfo(FUSO))
    hoje = (agora or datetime.now(UTC)).astimezone(ZoneInfo(FUSO)).date()
    return local.strftime("%H:%M") if local.date() == hoje else local.strftime("%H:%M de %d/%m/%Y")


@dataclass(frozen=True, slots=True)
class SinalVisto:
    """Um dos sinais do alerta, como a ficha mostra: nomes do cadastro e a hora de Brasilia."""

    ferramenta: str
    sistema: str
    hora: str


@dataclass(frozen=True, slots=True)
class AlertaDaFicha:
    """O cartao do alerta dos tres sinais na ficha (``fichas.sinais``, o retrato de quando a ficha nasceu)."""

    janela: str
    de_fora: SinalVisto
    privado: SinalVisto


def _visto(retrato: Mapping[str, Any], sinal: str) -> SinalVisto:
    vistos = retrato.get("vistos")
    achado: Mapping[str, Any] = {}
    for v in vistos if isinstance(vistos, list) else []:
        if isinstance(v, Mapping) and v.get("sinal") == sinal:
            achado = v
            break
    quando: datetime | None = None
    bruto = achado.get("visto_em")
    if isinstance(bruto, str):
        try:
            quando = datetime.fromisoformat(bruto)
        except ValueError:
            quando = None
    return SinalVisto(str(achado.get("ferramenta", "?")), str(achado.get("sistema", "?")), hora_e_dia(quando))


def alerta_da_ficha(retrato: Mapping[str, Any] | None) -> AlertaDaFicha | None:
    """O cartao do alerta, do retrato gravado na ficha; None sem alerta. So nomes do cadastro e horas: o retrato
    nunca leva argumento, resultado ou texto do sistema (``regras/sinais.py``)."""
    if not isinstance(retrato, Mapping):
        return None
    janela = retrato.get("janela_minutos")
    minutos = janela if isinstance(janela, int) and not isinstance(janela, bool) else 0
    return AlertaDaFicha(
        janela=janela_legivel(minutos) if minutos > 0 else "Há pouco",
        de_fora=_visto(retrato, sinais.TEXTO_DE_FORA),
        privado=_visto(retrato, sinais.DADO_PRIVADO),
    )


def hora_de_brasilia(momento: datetime | None) -> str:
    if momento is None:
        return ""
    local = momento.astimezone(ZoneInfo(FUSO))
    return local.strftime("%H:%M:%S de %d/%m/%Y")


def tempo_legivel(segundos: int) -> str:
    if segundos <= 0:
        return "nada"
    horas, resto = divmod(segundos, 3600)
    minutos, seg = divmod(resto, 60)
    partes: list[str] = []
    if horas:
        partes.append(f"{horas} hora" + ("s" if horas > 1 else ""))
    if minutos:
        partes.append(f"{minutos} minuto" + ("s" if minutos > 1 else ""))
    if seg and not horas:
        partes.append(f"{seg} segundo" + ("s" if seg > 1 else ""))
    return " e ".join(partes)


def _segundos_ate(momento: datetime | None, agora: datetime) -> int | None:
    if momento is None:
        return None
    return max(0, int((momento - agora).total_seconds() + 0.999))


# --- Fase 4: o selo do sistema, o pedido igual e o que o sistema informou -------------------------------------

NAO_GARANTE: Final = "O selo diz como o sistema respondeu no teste; não garante o que ele fará depois."
ONDE_TESTADO: Final[dict[str, str]] = {
    "ambiente_de_teste": "no ambiente de teste do sistema",
    "ferramentas_de_teste": "pelas ferramentas de teste que o sistema declarou",
}
SELO_NIVEL_1: Final = (
    "Selo do sistema: nível 1 (conectado). Este sistema não informa o que faz depois de receber a ação."
)
SELO_NIVEL_2: Final = (
    "Selo do sistema: nível 2 (confere). Testado pela equipe do MCP-CLARO em {testado_em}, {onde}; vale até"
    " {vale_ate}. Se a resposta do sistema se perder, o MCP-CLARO pergunta a ele o que houve, pelo número que manda"
    " junto. " + NAO_GARANTE
)
SELO_NIVEL_3: Final = (
    "Selo do sistema: nível 3 (presta contas). Testado pela equipe do MCP-CLARO em {testado_em}, {onde}; vale até"
    " {vale_ate}. O sistema informa o que fez depois de receber a ação, e o MCP-CLARO mostra isso marcado como"
    " informado por ele. " + NAO_GARANTE
)
SELO_NAO_GUARDADO: Final = "Selo do sistema: esta ficha não guardou o selo."
SELO_MUDOU: Final = "O selo deste sistema mudou depois que a ficha foi montada: agora é nível {nivel}."
SELO_CAIU: Final = (
    " Se a resposta do sistema se perder, o MCP-CLARO não pergunta a ele: a decisão fica com você, como num sistema de"
    " nível 1."
)


@dataclass(frozen=True, slots=True)
class SeloDaFicha:
    """O cartao "Selo do sistema" da ficha, do retrato da hora (``fichas.selo``, ``contrato.RETRATO_DO_SELO``).
    ``nivel`` e None quando a ficha nao guardou o retrato (ficha de antes da 0007, ou o selo nao foi lido)."""

    nivel: int | None
    texto: str


def _dia(momento: datetime | None) -> str:
    return momento.astimezone(ZoneInfo(FUSO)).strftime("%d/%m/%Y") if momento is not None else "?"


def selo_da_ficha(retrato: Mapping[str, Any] | None) -> SeloDaFicha:
    """O texto do cartao pelo retrato gravado (desenho 7.1). O retrato fora da forma do contrato
    (``SeloVigente.de_retrato`` recusa) conta como nao guardado: nada e prometido."""
    if retrato is None:
        return SeloDaFicha(None, SELO_NAO_GUARDADO)
    try:
        selo = vigente.SeloVigente.de_retrato(retrato)
    except ValueError, TypeError:
        return SeloDaFicha(None, SELO_NAO_GUARDADO)
    if selo.nivel == 1:
        return SeloDaFicha(1, SELO_NIVEL_1)
    modelo = SELO_NIVEL_2 if selo.nivel == 2 else SELO_NIVEL_3
    texto = modelo.format(
        testado_em=_dia(selo.testado_em),
        onde=ONDE_TESTADO.get(str(selo.modo), ONDE_TESTADO["ambiente_de_teste"]),
        vale_ate=_dia(selo.vale_ate),
    )
    return SeloDaFicha(selo.nivel, texto)


def selo_mudou(retrato: SeloDaFicha, agora: int | None) -> str:
    """A nota da ficha que espera quando o selo efetivo de agora (``agora``, o nivel lido do banco) nao e o do
    retrato (critica M3); "" quando sao iguais ou quando um dos dois nao se sabe."""
    if agora is None or retrato.nivel is None or agora == retrato.nivel:
        return ""
    texto = SELO_MUDOU.format(nivel=agora)
    return texto + SELO_CAIU if agora == 1 else texto


REPETE: Final = "Este pedido repete o protocolo {original}, que ficou sem confirmação."
MESMO_NUMERO_PREVISTO: Final = (
    "O MCP-CLARO o manda ao sistema com o mesmo número do primeiro, quando pode: se o primeiro já tinha chegado, o"
    " sistema não faz de novo."
)
# Nas frases do que o sistema informou, ``{quem}`` e "O sistema" ou, num terceiro, "O servidor de terceiros" (critica
# M9, como em ``_quem``); ``quem_informa`` monta.
NUMERO_NOVO_PELO_SISTEMA: Final = (
    "{quem} informou sobre o protocolo {original}: {estado}. Por isso este pedido vai com número novo: para o"
    " sistema, é um pedido novo."
)
SE_ESTIVER_ERRADO: Final = "Se {quem} estiver errado, este pedido faz de novo."
NAO_CHEGOU_SE_ERRADO: Final = "{quem} disse que o primeiro não chegou; se ele estiver errado, este pedido faz de novo."
FEITO_PELO_SISTEMA: Final = (
    "{quem} informou sobre o protocolo {original}: {estado}. Este pedido vai com o mesmo número do primeiro: o"
    " sistema não faz de novo o que já foi feito."
)
NAO_SABE: Final = "{quem} não sabe se o primeiro foi feito; com número novo, ele pode fazer duas vezes."
NAO_SABE_SEM_CAIXA: Final = (
    "Sem marcar a caixa, este pedido vai com o mesmo número do primeiro: se o primeiro já tinha chegado, o sistema não"
    " faz de novo."
)
CAIXA_NUMERO_NOVO: Final = (
    "Mandar com número novo. Entendo que, se o primeiro já tinha sido feito, o sistema pode fazer duas vezes."
)
ESCOLHEU_NUMERO_NOVO: Final = (
    "Você escolheu mandar este pedido com número novo: para o sistema, é um pedido novo. Se o primeiro tinha sido"
    " feito, este pode fazer de novo."
)
SAIU_COM_O_MESMO_NUMERO: Final = (
    "Este pedido saiu com o mesmo número do protocolo {original}. Se o primeiro já tinha chegado, o sistema não faz de"
    " novo."
)
SAIU_COM_NUMERO_NOVO: Final = (
    "Este pedido repetia o protocolo {original}, mas saiu com número novo: para o sistema, é um pedido novo."
)
FOI_A_SUA_ESCOLHA: Final = "O número novo foi a sua escolha na ficha."


@dataclass(frozen=True, slots=True)
class PedidoIgual:
    """O que a ficha do pedido igual diz (critica B12, A3.3 e M4.2): qual protocolo ele repete e com que numero vai.
    ``oferece_numero_novo``: a ficha espera e a original terminou a escada em ``desconhecido``
    (``Operacao.original_ficou_em_desconhecido``): a caixa "mandar com numero novo" aparece."""

    textos: tuple[str, ...]
    oferece_numero_novo: bool = False
    caixa: str = CAIXA_NUMERO_NOVO
    """O texto da caixa, quando oferecida."""


def quem_informa(op: Operacao, *, maiuscula: bool = True) -> str:
    """Quem informou, sem o nome (o texto da ficha do pedido igual e das listas): o sistema ou, num terceiro, o
    servidor de terceiros (critica M9)."""
    quem = "o servidor de terceiros" if op.de_terceiro else "o sistema"
    return quem[0].upper() + quem[1:] if maiuscula else quem


def pedido_igual(op: Operacao, maximo: int | None, *, pendente: bool) -> PedidoIgual | None:
    """A previsao da ficha. Quem decide o numero e o banco, no envio (``envios_selo_nasce``): o mesmo da original,
    salvo quando o sistema ja o queimou (``contrato.ESTADOS_QUE_QUEIMAM``, com ``nao_recebida`` so confirmada, A3.3) ou
    a pessoa escolheu o numero novo. Por isso a ficha diz "quando pode". O que a original informou aparece dito pelo
    sistema ou, num terceiro, pelo servidor de terceiros (critica M9)."""
    if op.repete_protocolo is None:
        return None
    original = op.repete_protocolo
    textos = [REPETE.format(original=original)]
    final = op.original.estado_final_do_numero if op.original is not None else None
    quem = quem_informa(op)
    oferece = False
    if op.ficha_numero_novo:
        textos.append(ESCOLHEU_NUMERO_NOVO)
    elif final in contrato.ESTADOS_QUE_QUEIMAM:
        textos.append(NUMERO_NOVO_PELO_SISTEMA.format(quem=quem, original=original, estado=estado_legivel(final)))
        if final == "nao_recebida":
            textos.append(NAO_CHEGOU_SE_ERRADO.format(quem=quem))
        else:
            textos.append(SE_ESTIVER_ERRADO.format(quem=quem_informa(op, maiuscula=False)))
    elif final is not None:
        textos.append(FEITO_PELO_SISTEMA.format(quem=quem, original=original, estado=estado_legivel(final)))
    elif op.original_ficou_em_desconhecido(_maximo(maximo)):
        textos += [NAO_SABE.format(quem=quem), NAO_SABE_SEM_CAIXA]
        oferece = pendente
    else:
        textos.append(MESMO_NUMERO_PREVISTO)
    return PedidoIgual(tuple(textos), oferece)


def numero_do_envio(op: Operacao) -> tuple[str, ...]:
    """O que a acao que ja saiu diz do numero (critica B12): saiu com o mesmo numero de outro protocolo
    (``mesmo_numero_de``, que o banco gravou no envio), ou repetia um e saiu com numero novo. Antes de sair, a
    escolha do numero novo feita na ficha."""
    if op.estado in fila.PASSOU_POR_ENVIANDO:
        if op.mesmo_numero_de is not None:
            return (SAIU_COM_O_MESMO_NUMERO.format(original=op.mesmo_numero_de),)
        if op.repete_protocolo is not None and op.envio_nivel is not None:
            novo = SAIU_COM_NUMERO_NOVO.format(original=op.repete_protocolo)
            return (novo, FOI_A_SUA_ESCOLHA) if op.ficha_numero_novo else (novo,)
        return ()
    return (ESCOLHEU_NUMERO_NOVO,) if op.ficha_numero_novo else ()


_SEM_MAXIMO: Final = sys.maxsize
"""Sem o ``BADGE_LOOKUP_MAX_ATTEMPTS`` (quem monta so a lista), a escada nao acaba pelas tentativas: so pelas outras
regras de ``Operacao.consulta_acabou`` (tempo, ``sistema_fora``, outro protocolo)."""


def _maximo(maximo: int | None) -> int:
    return _SEM_MAXIMO if maximo is None else maximo


def _nome(op: Operacao) -> Markup:
    """O nome do sistema numa frase da tela: do cadastro, escapado, com os escondidos marcados e dentro de ``<bdi>``."""
    return Markup("<bdi>{}</bdi>").format(marcar_escondidos(op.sistema_nome))


def _quem(op: Operacao, *, maiuscula: bool = True) -> Markup:
    """Quem informou: o sistema ou, num terceiro, o servidor de terceiros (critica M9)."""
    if op.de_terceiro:
        return Markup("{} {}").format("O servidor de terceiros" if maiuscula else "o servidor de terceiros", _nome(op))
    return Markup("{} {}").format("O sistema" if maiuscula else "o sistema", _nome(op))


def _hora_do_gateway(momento: datetime, agora: datetime) -> str:
    """A hora em que o MCP-CLARO gravou a resposta (``gravado_em``), nunca uma hora escrita pelo sistema (B12)."""
    return f"{hora_curta(momento, agora)} (horário de Brasília)"


PERGUNTANDO: Final = (
    "Estamos confirmando com o sistema. O MCP-CLARO pergunta {quem} o que houve com este pedido, pelo número que"
    " mandou junto. Nada será reenviado."
)
PERGUNTOU_SEM_RESPOSTA: Final = (
    "Estamos confirmando com o sistema. O MCP-CLARO perguntou {quem} o que houve com este pedido, pelo número que"
    " mandou junto, e ainda não teve resposta. Continuamos perguntando. Nada será reenviado."
)
NAO_CONSEGUIU_PERGUNTAR: Final = (
    "O MCP-CLARO ainda não conseguiu perguntar {quem} o que houve: a falha é do MCP-CLARO, não do sistema. Continuamos"
    " tentando. Nada será reenviado."
)
NAO_RESPONDEU: Final = "{quem} não respondeu à consulta. Decida você, como num sistema de nível 1."
PAROU_SISTEMA_FORA: Final = "O MCP-CLARO parou de perguntar: {quem} não está ativo agora (por exemplo, em quarentena)."
NAO_CONFIRMOU: Final = "O sistema não confirmou; a decisão é sua."
# Depois da decisao (que e uma so), nenhuma frase convida a decidir ou a registrar de novo: as de cima ganham a forma
# sem o convite, e o bloco diz que vale a decisao registrada.
VALE_A_DECISAO: Final = "Vale a decisão que você registrou."
NAO_RESPONDEU_DEPOIS: Final = "{quem} não respondeu à consulta. " + VALE_A_DECISAO
NAO_CONFIRMOU_DEPOIS: Final = "O sistema não confirmou. " + VALE_A_DECISAO
INFORMOU_AS: Final = "{quem} informou às {hora}: {estado}."
INFORMOU: Final = "{quem} informou: {estado}."
DEPOIS_DISSE: Final = "A última resposta dele, às {hora}, foi: {estado}."
DO_AVISO: Final = "Foi a resposta ao aviso do bloqueio, que não é uma resposta final."
NAO_CONFERE: Final = "Isto é o que o sistema diz; o MCP-CLARO não confere do lado de lá."
NAO_CONFERE_TERCEIRO: Final = "Isto é o que o servidor de terceiros diz; o MCP-CLARO não confere do lado de lá."
VALE_A_SUA: Final = "Se você viu outra coisa no sistema, registre o que viu: vale a sua decisão."
CONTINUAMOS: Final = "Continuamos perguntando."
UMA_SO_NAO_BASTA: Final = "Uma resposta só não basta: continuamos perguntando."
CONTRADISSE: Final = (
    "O sistema deu respostas que não combinam sobre este pedido. Confira no próprio sistema e decida pelo formulário."
)
CONTRADISSE_DEPOIS: Final = "O sistema deu respostas que não combinam sobre este pedido. " + VALE_A_DECISAO
NAO_CABE_NUM_TOQUE: Final = "O que o sistema informou não cabe num toque: decida pelo formulário."
TERCEIRO_CONFIRA: Final = (
    "Num servidor de terceiros, a resposta “não recebeu” não vale um toque: confira no próprio servidor antes de"
    " decidir."
)
OUTRO_PROTOCOLO: Final = (
    "O resultado está no protocolo {protocolo}, que saiu depois com o mesmo número. O que o sistema informar aparece"
    " nele."
)


@dataclass(frozen=True, slots=True)
class OQueOSistemaInformou:
    """O bloco "O que o sistema informou" da acao que saiu com numero (desenho 7.1 e 5.3).

    ``situacao``: ``perguntando``, ``sem_resposta_ainda``, ``nao_perguntou``, ``nao_respondeu``, ``sistema_fora``,
    ``acabou``, ``respondeu`` (estado final), ``sem_final`` (o que o sistema disse ainda nao e final),
    ``outro_protocolo`` ou ``enviada`` (o recibo ou a consulta de uma acao enviada). ``frases`` e texto da tela, ja
    escapado (o nome do sistema vai marcado). ``botao``: o botao "Registrar o que o sistema informou", com o estado
    final mostrado (``estado_visto``), que o formulario manda (decisao 6 do coordenador); ``registra`` e a decisao
    que ele grava, em palavras. ``dica``: por que o toque nao vale, quando ha estado final sem botao."""

    situacao: str
    frases: tuple[Markup, ...]
    botao: bool = False
    estado_visto: str | None = None
    registra: str = ""
    dica: str = ""


def _dito(op: Operacao) -> str | None:
    """O que o sistema disse por ultimo, como o texto ao aplicativo (``pedidos._o_que_o_sistema_disse``): o estado
    final ou, sem ele, a ultima resposta (de qualquer origem, a do aviso tambem)."""
    inf = op.informado
    if inf is None:
        return None
    return inf.estado_final or (inf.estado if inf.desfecho == "respondeu" else None)


def o_que_o_sistema_informou(
    op: Operacao, maximo: int | None, agora: datetime | None = None
) -> OQueOSistemaInformou | None:
    """O bloco da acao que saiu com numero (``envio_nivel``); None nas outras (nivel 1: a decisao e da pessoa, como
    sempre). ``maximo`` e ``BADGE_LOOKUP_MAX_ATTEMPTS``. Depois da decisao da pessoa (que e uma so), nenhuma frase
    convida a decidir ou a registrar de novo: o bloco diz que vale a decisao registrada (``VALE_A_DECISAO``)."""
    if op.envio_nivel is None or op.estado not in (Estado.NAO_CONFIRMADA, Estado.ENVIADA):
        return None
    agora = agora or datetime.now(UTC)
    inf = op.informado
    dito = _dito(op)
    quem = _quem(op)
    nao_confere = NAO_CONFERE_TERCEIRO if op.de_terceiro else NAO_CONFERE
    if op.estado == Estado.ENVIADA:
        if inf is None or dito is None:
            return None
        frase = escape(INFORMOU_AS).format(
            quem=quem, hora=_hora_do_gateway(inf.gravado_em, agora), estado=estado_legivel(dito)
        )
        return OQueOSistemaInformou("enviada", (frase, escape(nao_confere)))
    if op.substituto is not None:
        return OQueOSistemaInformou("outro_protocolo", (escape(OUTRO_PROTOCOLO).format(protocolo=op.substituto),))
    acabou = op.consulta_acabou(_maximo(maximo))
    parou = inf is not None and inf.ultimo_desfecho == "sistema_fora"
    pergunta = _quem(op, maiuscula=False)
    decidido = op.decisao_pessoa is not None
    nao_confirmou = escape(NAO_CONFIRMOU_DEPOIS if decidido else NAO_CONFIRMOU)
    if inf is None or dito is None:
        if acabou and parou:
            return OQueOSistemaInformou(
                "sistema_fora", (escape(PAROU_SISTEMA_FORA).format(quem=pergunta), nao_confirmou)
            )
        if acabou and inf is not None and maximo is not None and inf.tentativas >= maximo:
            nao_respondeu = NAO_RESPONDEU_DEPOIS if decidido else NAO_RESPONDEU
            return OQueOSistemaInformou("nao_respondeu", (escape(nao_respondeu).format(quem=quem),))
        if acabou:
            return OQueOSistemaInformou("acabou", (nao_confirmou,))
        if inf is not None and inf.desfecho in ("sem_chave", "sala_fora", "sistema_fora"):
            return OQueOSistemaInformou("nao_perguntou", (escape(NAO_CONSEGUIU_PERGUNTAR).format(quem=_ao(pergunta)),))
        if inf is not None:
            return OQueOSistemaInformou(
                "sem_resposta_ainda", (escape(PERGUNTOU_SEM_RESPOSTA).format(quem=_ao(pergunta)),)
            )
        return OQueOSistemaInformou("perguntando", (escape(PERGUNTANDO).format(quem=_ao(pergunta)),))
    final = inf.estado_final
    hora = _hora_do_gateway(inf.gravado_em, agora)
    frases: list[Markup] = []
    if final is not None and inf.estado != final:
        frases.append(escape(INFORMOU).format(quem=quem, estado=estado_legivel(final)))
        if inf.desfecho == "respondeu" and inf.estado is not None:
            frases.append(escape(DEPOIS_DISSE).format(hora=hora, estado=estado_legivel(inf.estado)))
    else:
        frases.append(escape(INFORMOU_AS).format(quem=quem, hora=hora, estado=estado_legivel(dito)))
    if inf.origem == "aviso" and inf.estado is not None and (final is None or inf.estado != final):
        frases.append(escape(DO_AVISO))
    frases.append(escape(nao_confere))
    if final is None:
        if acabou:
            if parou:
                frases.append(escape(PAROU_SISTEMA_FORA).format(quem=pergunta))
            frases.append(nao_confirmou)
        else:
            frases.append(escape(UMA_SO_NAO_BASTA if dito == "nao_recebida" else CONTINUAMOS))
        return OQueOSistemaInformou("sem_final", tuple(frases))
    if inf.se_contradisse:
        frases.append(escape(CONTRADISSE_DEPOIS if decidido else CONTRADISSE))
    else:
        frases.append(escape(VALE_A_DECISAO if decidido else VALE_A_SUA))
    decisao = fila.DECISAO_PELO_SISTEMA.get(final)
    terceiro_nao_recebida = op.de_terceiro and final == "nao_recebida"
    botao = not decidido and decisao is not None and not inf.se_contradisse and not terceiro_nao_recebida
    dica = ""
    if not decidido and not inf.se_contradisse:
        if terceiro_nao_recebida:
            dica = TERCEIRO_CONFIRA
        elif decisao is None:
            dica = NAO_CABE_NUM_TOQUE
    return OQueOSistemaInformou(
        "respondeu",
        tuple(frases),
        botao=botao,
        estado_visto=final if botao else None,
        registra=DECISOES.get(decisao or "", "") if botao else "",
        dica=dica,
    )


def _ao(quem: Markup) -> Markup:
    """ "o sistema X" vira "ao sistema X" ("pergunta ao sistema")."""
    return Markup("a{}").format(quem) if quem.startswith("o ") else quem


def etiqueta_informado(op: Operacao) -> str:
    """A etiqueta das listas ("Minhas ações" e a caixa de pendências): "O sistema informou: {estado}" na acao nao
    confirmada que saiu com numero e tem resposta do sistema (desenho 7.1). Sem resposta, ""."""
    if op.estado != Estado.NAO_CONFIRMADA or op.envio_nivel is None:
        return ""
    if op.substituto is not None:
        return f"O resultado está no protocolo {op.substituto}"
    dito = _dito(op)
    if dito is None:
        return ""
    return f"{quem_informa(op)} informou: {estado_legivel(dito)}"


@dataclass(frozen=True, slots=True)
class Acao:
    """Uma operacao como as telas mostram."""

    op: Operacao
    rotulo: str
    frase: str
    argumentos: list[tuple[str, str]]
    escondidos: bool
    diz: list[str]
    diz_escondidos: bool
    textos: list[str]
    hora_saida: str
    falta: int
    falta_texto: str
    prazo: int
    prazo_texto: str
    expira_em: str
    decisao: str
    como: str
    de_estranhos: bool = False
    """O resultado e texto de estranhos (``Operacao.de_estranhos``): a tela nao mostra o texto (critica A4)."""
    respondeu: bool = False
    """Ha resultado guardado (o sistema respondeu)."""
    pode_ver: bool = False
    """Resposta de estranhos com texto: a pessoa pode pedir para ver, atras do aviso."""
    estranhos: tuple[str, ...] = ()
    """O texto de estranhos, so quando a pessoa pediu para ver (``montar(..., ver_estranhos=True)``)."""
    detalhe: str = ""
    explicacao: str = ""
    alerta: AlertaDaFicha | None = None
    nao_chegou: bool = False
    """Nao confirmada pela retomada, e o fim que chegou depois diz que o pedido nao saiu do MCP-CLARO
    (``nao_chegou_depois_da_retomada``; a marca da retomada so vai no desfecho de uma acao nao confirmada): na acao
    nao confirmada, a tela nao diz que o pedido saiu uma vez, e a caixa de pendencias diz que ele nao chegou."""
    selo: SeloDaFicha = SeloDaFicha(None, SELO_NAO_GUARDADO)
    """O cartao "Selo do sistema" da ficha, do retrato da hora (``fichas.selo``)."""
    selo_agora: str = ""
    """Na ficha que espera: o selo de agora, lido do banco, quando difere do retrato (critica M3)."""
    pedido_igual: PedidoIgual | None = None
    """A ficha do pedido igual: qual protocolo ele repete e com que numero vai (B12, A3.3, M4.2)."""
    numero: tuple[str, ...] = ()
    """O que a acao diz do numero que levou ao sistema (o mesmo de outro protocolo, ou novo)."""
    informou: OQueOSistemaInformou | None = None
    """O bloco "O que o sistema informou" da acao que saiu com numero."""
    etiqueta_informado: str = ""
    """A etiqueta das listas: "O sistema informou: {estado}"."""

    @property
    def atualiza(self) -> bool:
        """A parte do estado se atualiza sozinha enquanto espera para sair ou esta saindo."""
        return self.op.estado in (Estado.LIBERADA, Estado.ENVIANDO)

    @property
    def pode_decidir_ficha(self) -> bool:
        return self.op.estado == Estado.AGUARDANDO_FICHA and self.op.ficha_decisao is None and self.prazo > 0

    @property
    def de_risco_alto(self) -> bool:
        """Ficha de risco alto: a que nasceu pedindo passkey ou a que foi montada com risco alto."""
        return self.op.ficha_exige_passkey or self.op.ficha_risco == "alto"


def como_passou(op: Operacao) -> str:
    """Por onde a acao passou. A ficha que nasceu com o retrato do alerta dos tres sinais diz isso (a rota
    ``tres_sinais``). A de risco alto mantem a rota dela (``saida.pedidos._rota`` olha o risco antes do alerta) e
    so diz que o alerta tambem valia. A operacao nao guarda a rota: a ficha do periodo de experiencia com o alerta
    valendo tambem diz "alerta dos tres sinais", o que e verdade, mas nao e o unico motivo."""
    if op.limite_id is not None:
        return "Dentro de um limite seu, sem ficha"
    if op.ficha_sinais is not None and op.ficha_exige_passkey:
        return "Pela ficha, com passkey (valia o alerta dos três sinais)"
    if op.ficha_sinais is not None and op.ficha_risco == "alto":
        return "Pela ficha: risco alto (valia o alerta dos três sinais)"
    if op.ficha_sinais is not None:
        return "Pela ficha: alerta dos três sinais"
    if op.ficha_exige_passkey:
        return "Pela ficha, com passkey"
    return "Pela ficha"


def montar(
    op: Operacao,
    agora: datetime | None = None,
    *,
    ver_estranhos: bool = False,
    maximo_consultas: int | None = None,
) -> Acao:
    """A operacao como as telas mostram. Resultado de estranhos (servidor de terceiros ou ferramenta marcada "traz
    texto de fora"): ``textos`` fica vazio sempre; o texto so vem em ``estranhos``, com ``ver_estranhos`` (a
    pessoa pediu para ver, atras do aviso). ``maximo_consultas`` e o ``BADGE_LOOKUP_MAX_ATTEMPTS`` (Fase 4: quando a
    escada da consulta acabou pelas tentativas); sem ele, so as outras regras do fim da escada valem."""
    agora = agora or datetime.now(UTC)
    falta = _segundos_ate(op.hora_saida, agora) or 0
    prazo = _segundos_ate(op.ficha_expira_em, agora) or 0
    pendente = op.estado == Estado.AGUARDANDO_FICHA and op.ficha_decisao is None and prazo > 0
    de_estranhos = op.de_estranhos
    textos = textos_do_resultado(op.resultado)
    detalhe, explicacao = detalhe_do_desfecho(op.desfecho)
    return Acao(
        op=op,
        rotulo=ROTULOS[op.estado],
        frase=frase_do_estado(op.estado),
        argumentos=argumentos_legiveis(op.argumentos),
        escondidos=tem_escondidos(op.argumentos),
        diz=linhas_do_aplicativo(op.aplicativo_diz),
        diz_escondidos=tem_escondidos(op.aplicativo_diz),
        textos=[] if de_estranhos else textos,
        hora_saida=hora_de_brasilia(op.hora_saida),
        falta=falta,
        falta_texto=tempo_legivel(falta),
        prazo=prazo,
        prazo_texto=tempo_legivel(prazo),
        expira_em=hora_de_brasilia(op.ficha_expira_em),
        decisao=DECISOES.get(op.decisao_pessoa or "", ""),
        como=como_passou(op),
        de_estranhos=de_estranhos,
        respondeu=isinstance(op.resultado, dict),
        pode_ver=de_estranhos and bool(textos),
        estranhos=tuple(textos_de_estranhos(op.resultado)) if de_estranhos and ver_estranhos else (),
        detalhe=detalhe,
        explicacao=explicacao,
        alerta=alerta_da_ficha(op.ficha_sinais),
        nao_chegou=nao_chegou_depois_da_retomada(op.desfecho),
        selo=selo_da_ficha(op.selo),
        pedido_igual=pedido_igual(op, maximo_consultas, pendente=pendente),
        numero=numero_do_envio(op),
        informou=o_que_o_sistema_informou(op, maximo_consultas, agora),
        etiqueta_informado=etiqueta_informado(op),
    )


@dataclass(frozen=True, slots=True)
class Pendencias:
    fichas: list[Acao]
    espera: list[Acao]
    confirmando: list[Acao]

    @property
    def total(self) -> int:
        return len(self.fichas) + len(self.espera) + len(self.confirmando)


def separar(ops: list[Operacao], agora: datetime | None = None, *, maximo_consultas: int | None = None) -> Pendencias:
    acoes = [montar(op, agora, maximo_consultas=maximo_consultas) for op in ops]
    return Pendencias(
        fichas=[a for a in acoes if a.op.estado == Estado.AGUARDANDO_FICHA],
        espera=[a for a in acoes if a.op.estado == Estado.LIBERADA],
        confirmando=[a for a in acoes if a.op.estado == Estado.NAO_CONFIRMADA],
    )


class TelasDeAcoes:
    def __init__(self, t: Telas) -> None:
        self.t = t
        self.cer = passkeys.Cerimonia.de_parametros(t.p)

    def rotas(self) -> list[BaseRoute]:
        return [
            Route("/pendencias", self.pendencias, methods=["GET"]),
            Route("/fichas/{ficha_id:uuid}", self.ficha, methods=["GET"]),
            Route("/fichas/{ficha_id:uuid}/desafio", self.desafio, methods=["POST"]),
            Route("/fichas/{ficha_id:uuid}/confirmar", self.confirmar, methods=["POST"]),
            Route("/fichas/{ficha_id:uuid}/recusar", self.recusar, methods=["POST"]),
            Route("/acoes", self.lista, methods=["GET"]),
            Route("/acoes/{operacao_id:uuid}", self.acao, methods=["GET"]),
            Route("/acoes/{operacao_id:uuid}/estado", self.estado, methods=["GET"]),
            Route("/acoes/{operacao_id:uuid}/cancelar", self.cancelar, methods=["POST"]),
            Route("/acoes/{operacao_id:uuid}/seguir", self.seguir, methods=["POST"]),
            Route("/acoes/{operacao_id:uuid}/decidir", self.decidir, methods=["POST"]),
            Route("/acoes/{operacao_id:uuid}/registrar_informado", self.registrar_informado, methods=["POST"]),
        ]

    @property
    def _maximo(self) -> int:
        """``BADGE_LOOKUP_MAX_ATTEMPTS``: ate onde vai a escada da consulta (Fase 4)."""
        return int(self.t.p.BADGE_LOOKUP_MAX_ATTEMPTS)

    def _montar(self, op: Operacao, agora: datetime | None = None, *, ver_estranhos: bool = False) -> Acao:
        return montar(op, agora, ver_estranhos=ver_estranhos, maximo_consultas=self._maximo)

    def _aviso(self, pedido: Request) -> str | None:
        codigo = pedido.query_params.get("aviso", "")
        return AVISOS_ACOES.get(codigo) or AVISOS.get(codigo)

    # --- Inicio, com a contagem da caixa de pendencias ---

    async def inicio(self, pedido: Request) -> Response:
        """O inicio das telas: a contagem da caixa de pendencias e, para quem e responsavel ou substituto de um
        programa que roda sozinho, o link "Meus programas"."""
        sessao = await self.t.sessao(pedido)
        contagem: Pendencias | None = None
        programas = False
        if sessao is not None:
            async with self.t.engine.connect() as c:
                contagem = separar(await fila.pendencias(c, sessao.pessoa.id), maximo_consultas=self._maximo)
                programas = await consultas.cuida_de_programa(c, sessao.pessoa.id)
        return self.t.pagina(pedido, "inicio.html", {"pendencias": contagem, "cuida_de_programa": programas})

    # --- Caixa de pendencias ---

    @tela
    async def pendencias(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        async with self.t.engine.connect() as c:
            caixa = separar(await fila.pendencias(c, sessao.pessoa.id), maximo_consultas=self._maximo)
        return self.t.pagina(
            pedido, "acoes/pendencias.html", {"caixa": caixa, "passkey_ligada": self.t.p.FEATURE_PASSKEY}
        )

    # --- Ficha ---

    async def _ficha_da_pessoa(self, c: Any, ficha_id: uuid.UUID, pessoa_id: uuid.UUID) -> Operacao | None:
        operacao_id = await consultas.operacao_da_ficha(c, ficha_id)
        if operacao_id is None:
            return None
        op = await fila.por_id(c, operacao_id)
        if op is None or op.pessoa_id != pessoa_id or op.ficha_id != ficha_id:
            return None
        return op

    async def _pagina_ficha(
        self, pedido: Request, ficha_id: uuid.UUID, *, erros: list[str] | None = None, status: int = 200
    ) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        selo_agora: int | None = None
        async with self.t.engine.connect() as c:
            op = await self._ficha_da_pessoa(c, ficha_id, sessao.pessoa.id)
            detalhes = await consultas.detalhes_da_operacao(c, op.id) if op else None
            tem_passkey = await passkeys.tem_ativa(c, sessao.pessoa.id)
            if op is not None and self._montar(op).pode_decidir_ficha:
                selo_agora = await self._nivel_de_agora(c, op, pessoa_teste=sessao.pessoa.teste)
        if op is None or detalhes is None:
            return self.t.erro(pedido, 404, "Ficha não encontrada", "Esta ficha não é sua ou não existe.")
        a = self._montar(op)
        a = replace(a, selo_agora=selo_mudou(a.selo, selo_agora))
        return self.t.pagina(
            pedido,
            "acoes/ficha.html",
            {
                "a": a,
                "d": detalhes,
                "risco": RISCOS.get(str(detalhes.risco), str(detalhes.risco)),
                "passkey_ligada": self.t.p.FEATURE_PASSKEY,
                "tem_passkey": tem_passkey,
                "pede_login": self._pede_login(op),
                "refazer": self.t.login_refeito(pedido, sessao, f"/telas/fichas/{ficha_id}"),
                "pede": "Confirmar esta ação de risco alto pede um login refeito na hora.",
                "aviso": self._aviso(pedido),
                "erros": erros or [],
            },
            status=status,
        )

    async def _nivel_de_agora(self, c: Any, op: Operacao, *, pessoa_teste: bool) -> int | None:
        """O nivel do selo efetivo de agora para a pessoa (critica M3: a ficha que espera diz quando ele mudou desde o
        retrato). Lido do banco (``vigente.ler``) num ponto de salvamento: uma falha vira None e a ficha segue sem a
        nota, nunca erro (A1). O efetivo segue ``FEATURE_PHASE4_BADGES`` e ``pessoas.teste``, como o retrato."""
        try:
            async with c.begin_nested():
                lido = await vigente.ler(c, op.sistema_id)
        except (SQLAlchemyError, ValueError) as erro:
            log.warning("telas_selo_nao_lido", erro=type(erro).__name__)
            return None
        efetivo = vigente.efetivo(lido, ligada=self.t.p.FEATURE_PHASE4_BADGES is True, pessoa_teste=pessoa_teste)
        return efetivo.nivel

    def _pede_login(self, op: Operacao) -> bool:
        """Ficha de risco alto que nao confirma pela passkey (passkey desligada nesta instalacao, ou ficha montada
        quando ela estava desligada): o reforco e o login refeito na hora."""
        return montar(op).de_risco_alto and not (self.t.p.FEATURE_PASSKEY and op.ficha_exige_passkey)

    @tela
    async def ficha(self, pedido: Request) -> Response:
        return await self._pagina_ficha(pedido, pedido.path_params["ficha_id"])

    @tela
    async def desafio(self, pedido: Request) -> Response:
        """Desafio novo para confirmar esta ficha com a passkey (o navegador pede na hora do toque)."""
        sessao = await self.t.exigir_sessao(pedido)
        await self.t.formulario(pedido, sessao)
        ficha_id: uuid.UUID = pedido.path_params["ficha_id"]
        if not self.t.p.FEATURE_PASSKEY:
            return passkeys.resposta_desafio(None, "A passkey está desligada nesta instalação.", status=409)
        async with self.t.engine.begin() as c:
            op = await self._ficha_da_pessoa(c, ficha_id, sessao.pessoa.id)
            if op is None:
                return passkeys.resposta_desafio(None, "Ficha não encontrada.", status=404)
            if not montar(op).pode_decidir_ficha:
                return passkeys.resposta_desafio(None, "Esta ficha não espera mais confirmação.", status=409)
            try:
                opcoes = await passkeys.opcoes_autenticacao(c, self.cer, sessao.pessoa.id, "ficha", str(ficha_id))
            except passkeys.PasskeyRecusada as erro:
                return passkeys.resposta_desafio(None, erro.mensagem, status=409)
        return passkeys.resposta_desafio(opcoes)

    @tela
    async def confirmar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        f = await self.t.formulario(pedido, sessao)
        ficha_id: uuid.UUID = pedido.path_params["ficha_id"]
        pessoa_id = sessao.pessoa.id
        ligada = bool(self.t.p.FEATURE_PASSKEY)
        recente = self.t.login_recente(sessao)
        login_refeito = False
        recusa: tuple[int, str] | None = None
        com_passkey = False
        confirmada: Operacao | None = None
        numero_novo = f.texto("numero_novo") == NUMERO_NOVO_MARCADO
        # Toda pagina de erro sai depois da transacao: cada pedido segura uma conexao do pool so (a pagina da
        # ficha abre a sua).
        async with self.t.engine.begin() as c:
            op = await self._ficha_da_pessoa(c, ficha_id, pessoa_id)
            if op is None:
                recusa = FICHA_ALHEIA
            elif self._pede_login(op) and not recente:
                # Sem a passkey, o reforco do risco alto e o login refeito na hora. Nada muda ate la.
                log.info("telas_ficha_pede_login_refeito")
                recusa = (403, LOGIN_REFEITO_FICHA)
            elif self._pede_login(op):
                login_refeito = True  # o reforco usado nesta ficha; o extrato diz "com o login refeito"
            if op is not None and recusa is None:
                credencial = f.texto("credencial")
                if ligada and op.ficha_exige_passkey and credencial:
                    # A passkey e conferida aqui, na mesma transacao que confirma. Recusada, o desafio fica gasto.
                    negada = await passkeys.conferir(c, self.cer, pessoa_id, "ficha", str(ficha_id), credencial)
                    if negada is not None:
                        recusa = (403, negada)
                    com_passkey = negada is None
            if op is not None and recusa is None:
                valores = await politica.varios(c, "OUTBOX_HOLD_SECONDS", "CONFIRM_TOO_FAST_SECONDS")
                # Fase 4, critica M4.2: a caixa "mandar com numero novo" da ficha do pedido igual. Sem ela, a chamada
                # de sempre (vale o mesmo numero); com ela, a fila confere de novo se cabe (numero_novo_nao_cabe).
                escolha: dict[str, Any] = {"numero_novo": True, "maximo_consultas": self._maximo} if numero_novo else {}
                try:
                    async with c.begin_nested():
                        confirmada = await fila.confirmar_ficha(
                            c,
                            ficha_id,
                            pessoa_id=pessoa_id,
                            com_passkey=com_passkey,
                            login_refeito=login_refeito,
                            passkey_ligada=ligada,
                            espera_segundos=valores["OUTBOX_HOLD_SECONDS"],
                            rapida_segundos=valores["CONFIRM_TOO_FAST_SECONDS"],
                            **escolha,
                        )
                except DecisaoRecusada as erro:
                    recusa = recusa_legivel(erro)
        if recusa is not None or confirmada is None:
            status, mensagem = recusa or (409, "Nada mudou.")
            log.info("telas_ficha_recusada", status=status)
            if status == 404:
                return self.t.erro(pedido, 404, "Ficha não encontrada", mensagem)
            return await self._pagina_ficha(pedido, ficha_id, erros=[mensagem], status=status)
        log.info("telas_ficha_confirmada", passkey=com_passkey, login_refeito=login_refeito, numero_novo=numero_novo)
        return ver_depois(f"/telas/acoes/{confirmada.id}", "confirmada_numero_novo" if numero_novo else "confirmada")

    @tela
    async def recusar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        await self.t.formulario(pedido, sessao)
        ficha_id: uuid.UUID = pedido.path_params["ficha_id"]
        recusa: tuple[int, str] | None = None
        recusada: Operacao | None = None
        async with self.t.engine.begin() as c:
            op = await self._ficha_da_pessoa(c, ficha_id, sessao.pessoa.id)
            if op is None:
                recusa = FICHA_ALHEIA
            else:
                try:
                    async with c.begin_nested():
                        recusada = await fila.recusar_ficha(c, ficha_id, pessoa_id=sessao.pessoa.id)
                except DecisaoRecusada as erro:
                    recusa = recusa_legivel(erro)
        # A pagina sai depois da transacao (uma conexao do pool por pedido).
        if recusa is not None or recusada is None:
            status, mensagem = recusa or (409, "Nada mudou.")
            if status == 404:
                return self.t.erro(pedido, 404, "Ficha não encontrada", mensagem)
            return await self._pagina_ficha(pedido, ficha_id, erros=[mensagem], status=status)
        log.info("telas_ficha_recusada_pela_pessoa")
        return ver_depois(f"/telas/acoes/{recusada.id}", "recusada")

    # --- Minhas acoes ---

    @tela
    async def lista(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        busca = pedido.query_params.get("protocolo", "").strip().upper()[:40]
        async with self.t.engine.connect() as c:
            if busca and protocolo_valido(busca):
                operacao_id = await consultas.operacao_do_protocolo(c, sessao.pessoa.id, busca)
                achada = await fila.por_id(c, operacao_id) if operacao_id else None
                ops = [achada] if achada is not None else []
            else:
                ops = await fila.da_pessoa(c, sessao.pessoa.id, limite=200)
                if busca:
                    ops = [op for op in ops if busca in op.protocolo]
        agora = datetime.now(UTC)
        return self.t.pagina(
            pedido, "acoes/lista.html", {"acoes": [self._montar(op, agora) for op in ops], "busca": busca}
        )

    async def _da_pessoa(self, pedido: Request, operacao_id: uuid.UUID) -> Operacao | None:
        sessao = await self.t.exigir_sessao(pedido)
        async with self.t.engine.connect() as c:
            op = await fila.por_id(c, operacao_id)
        if op is None or op.pessoa_id != sessao.pessoa.id:
            return None
        return op

    async def _saida_aberta(self, op: Operacao) -> bool:
        """A porta de saida esta aberta? Com ela fechada, a acao liberada espera, e a tela diz isso. So a liberada
        pergunta (o relogio se atualiza a cada poucos segundos): nas outras, a resposta nao muda nada na tela."""
        if op.estado != Estado.LIBERADA:
            return True
        async with self.t.engine.connect() as c:
            return (await estado.ler_saida(c)).aberta

    def _nao_achada(self, pedido: Request) -> Response:
        return self.t.erro(pedido, 404, "Ação não encontrada", "Esta ação não é sua ou não existe.")

    async def _pagina_acao(
        self, pedido: Request, operacao_id: uuid.UUID, *, erros: list[str] | None = None, status: int = 200
    ) -> Response:
        op = await self._da_pessoa(pedido, operacao_id)
        if op is None:
            return self._nao_achada(pedido)
        # A resposta de estranhos so aparece quando a pessoa pede ("Ver o texto mesmo assim", ``?ver=resposta``).
        ver = pedido.query_params.get("ver") == VER_RESPOSTA
        return self.t.pagina(
            pedido,
            "acoes/acao.html",
            {
                "a": self._montar(op, ver_estranhos=ver),
                "intervalo": SEGUNDOS_ENTRE_ATUALIZACOES,
                "saida_aberta": await self._saida_aberta(op),
                "aviso": self._aviso(pedido),
                "erros": erros or [],
            },
            status=status,
        )

    @tela
    async def acao(self, pedido: Request) -> Response:
        return await self._pagina_acao(pedido, pedido.path_params["operacao_id"])

    @tela
    async def estado(self, pedido: Request) -> Response:
        """So o relogio, para o htmx trocar sozinho enquanto a acao espera ou sai.

        Se o estado ja nao e o que a pagina mostra (``visto``), pede ao htmx que recarregue a pagina inteira:
        os botoes e o resultado mudam junto."""
        op = await self._da_pessoa(pedido, pedido.path_params["operacao_id"])
        if op is None:
            return self._nao_achada(pedido)
        visto = pedido.query_params.get("visto")
        if visto is not None and visto != str(op.estado) and pedido.headers.get("hx-request") == "true":
            return Response(status_code=200, headers={"HX-Refresh": "true"})
        return self.t.pagina(
            pedido,
            "acoes/_relogio.html",
            {
                "a": self._montar(op),
                "intervalo": SEGUNDOS_ENTRE_ATUALIZACOES,
                "saida_aberta": await self._saida_aberta(op),
            },
        )

    async def _mudar(self, pedido: Request, mudanca: str) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        f = await self.t.formulario(pedido, sessao)
        operacao_id: uuid.UUID = pedido.path_params["operacao_id"]
        if await self._da_pessoa(pedido, operacao_id) is None:
            return self._nao_achada(pedido)
        recusa: tuple[int, str] | None = None
        async with self.t.engine.begin() as c:
            try:
                async with c.begin_nested():
                    if mudanca == "cancelar":
                        await fila.cancelar(c, operacao_id, pessoa_id=sessao.pessoa.id)
                    elif mudanca == "seguir":
                        await fila.seguir_agora(c, operacao_id, pessoa_id=sessao.pessoa.id)
                    elif mudanca == "registrar":
                        # O botao "Registrar o que o sistema informou": o formulario manda o estado que a tela
                        # mostrou, e a fila recusa se ele mudou (decisao 6 do coordenador).
                        await fila.registrar_o_que_o_sistema_informou(
                            c,
                            operacao_id,
                            pessoa_id=sessao.pessoa.id,
                            estado_visto=f.texto("estado_visto"),
                            emails_admin=tuple(self.t.p.NOTIFY_ADMIN_EMAILS),
                        )
                    else:
                        await fila.decidir_nao_confirmada(
                            c,
                            operacao_id,
                            pessoa_id=sessao.pessoa.id,
                            decisao=f.texto("decisao"),
                            observacao=f.texto("observacao")[:MAXIMO_OBSERVACAO] or None,
                            emails_admin=tuple(self.t.p.NOTIFY_ADMIN_EMAILS),
                        )
            except DecisaoRecusada as erro:
                recusa = recusa_legivel(erro)
        if recusa is not None:
            status, mensagem = recusa
            log.info("telas_acao_recusada", mudanca=mudanca, status=status)
            if status == 404:
                return self._nao_achada(pedido)
            return await self._pagina_acao(pedido, operacao_id, erros=[mensagem], status=status)
        log.info("telas_acao_mudou", mudanca=mudanca)
        aviso = {"cancelar": "cancelada", "seguir": "seguir_agora", "registrar": "informado_registrado"}.get(
            mudanca, "decisao_registrada"
        )
        if aviso == "seguir_agora":
            async with self.t.engine.connect() as c:
                if not (await estado.ler_saida(c)).aberta:
                    aviso = "seguir_agora_saida_fechada"
        return ver_depois(f"/telas/acoes/{operacao_id}", aviso)

    @tela
    async def cancelar(self, pedido: Request) -> Response:
        return await self._mudar(pedido, "cancelar")

    @tela
    async def seguir(self, pedido: Request) -> Response:
        return await self._mudar(pedido, "seguir")

    @tela
    async def decidir(self, pedido: Request) -> Response:
        return await self._mudar(pedido, "decidir")

    @tela
    async def registrar_informado(self, pedido: Request) -> Response:
        return await self._mudar(pedido, "registrar")
