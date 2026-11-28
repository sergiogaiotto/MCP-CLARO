"""Selo na fila e nos textos ao aplicativo, sem banco (Fase 4, grupo G4; desenho 5.3, 5.5 e 7.3, criticas A3.3, M4 e
M9).

- ``Operacao.nivel_do_selo``: o envio com numero, o nivel 1 de quem passou por "enviando" sem numero, o retrato da
  ficha antes de sair.
- ``Operacao.consulta_acabou``: a escada acaba pelas tentativas, por ``sistema_fora`` ou pelo pedido igual que levou
  o numero; nunca com estado final.
- ``contradiz_o_sistema``: conta pela coluna "Conta como" do vocabulario, com o estado final e a ultima resposta.
- Textos e ``_meta`` ao aplicativo: so o vocabulario do contrato, sem acento; num terceiro, "o servidor de terceiros" e
  ``de_terceiro`` no ``_meta`` (M9).
- "Registrar o que o sistema informou" recusa um terceiro que disse ``nao_recebida`` (A3.3), sem tocar o banco.
- Retoque da etapa 3 (G4r): a escada acaba pelo tempo (``contrato.escada_vencida``, decisao 3); a ficha do pedido
  igual sabe se a original terminou em ``desconhecido`` (M4.2, decisao 4); so a "enviada" diz que saiu com o numero da
  original (decisao 5); o botao registra so o que a pessoa viu, de um sistema que nao se contradisse (decisao 6).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from mcp_claro.saida import fila
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.fila import DECISAO_PELO_SISTEMA, InformadoDoSistema, Operacao, contradiz_o_sistema
from mcp_claro.saida.pedidos import META_INFORMADO, informado_no_meta, resposta, texto_do_estado
from mcp_claro.selo import contrato

AGORA = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)


def informado(**mudar: Any) -> InformadoDoSistema:
    base: dict[str, Any] = {
        "estado": "concluida",
        "desfecho": "respondeu",
        "origem": "consulta",
        "gravado_em": AGORA,
        "de_terceiro": False,
        "estado_final": "concluida",
        "tentativas": 1,
        "ultimo_desfecho": "respondeu",
    }
    base.update(mudar)
    return InformadoDoSistema(**base)


def operacao(**mudar: Any) -> Operacao:
    base: dict[str, Any] = {
        "id": uuid.uuid4(),
        "protocolo": "CLR-2026-00000088",
        "codigo_conferencia": "ABCD2346EFGH",
        "pessoa_id": uuid.uuid4(),
        "aplicativo_id": uuid.uuid4(),
        "ordem_id": uuid.uuid4(),
        "ferramenta_id": uuid.uuid4(),
        "sistema_id": uuid.uuid4(),
        "argumentos": {"texto": "oi"},
        "aplicativo_diz": None,
        "resultado": None,
        "desfecho": "a resposta do sistema nao chegou inteira",
        "criada_em": AGORA,
        "fila_id": 1,
        "estado": Estado.NAO_CONFIRMADA,
        "ficha_id": uuid.uuid4(),
        "limite_id": None,
        "hora_saida": None,
        "enviada_em": AGORA,
        "mudou_em": AGORA,
        "mudou_por": "despachante",
        "nome_vitrine": "enviar_lembrete",
        "sistema_nome": "cobranca",
        "envio_nivel": 2,
    }
    base.update(mudar)
    return Operacao(**base)


RETRATO_3 = {
    "nivel": 3,
    "teste_id": str(uuid.uuid4()),
    "testado_em": AGORA.isoformat(),
    "vale_ate": AGORA.isoformat(),
    "modo": "ambiente_de_teste",
    "motivo": "vigente",
}


@pytest.mark.parametrize(
    ("mudar", "nivel"),
    [
        ({"envio_nivel": 3}, 3),
        ({"envio_nivel": None, "estado": Estado.NAO_CONFIRMADA, "selo": RETRATO_3}, 1),
        ({"envio_nivel": None, "estado": Estado.ENVIADA, "selo": RETRATO_3}, 1),
        ({"envio_nivel": None, "estado": Estado.NAO_SAIU, "selo": RETRATO_3}, 1),
        ({"envio_nivel": None, "estado": Estado.ENVIANDO, "selo": RETRATO_3}, 1),
        ({"envio_nivel": None, "estado": Estado.SISTEMA_PEDIU_DADOS, "selo": RETRATO_3}, 1),
        ({"envio_nivel": None, "estado": Estado.AGUARDANDO_FICHA, "selo": RETRATO_3}, 3),
        ({"envio_nivel": None, "estado": Estado.CANCELADA, "selo": RETRATO_3}, 3),
        ({"envio_nivel": None, "estado": Estado.LIBERADA, "selo": None}, None),
        ({"envio_nivel": None, "estado": Estado.LIBERADA, "selo": {"nivel": True}}, None),
    ],
)
def test_nivel_do_selo_da_operacao(mudar: dict[str, Any], nivel: int | None) -> None:
    assert operacao(**mudar).nivel_do_selo == nivel


def test_consulta_acaba_pelas_tentativas_por_sistema_fora_ou_pelo_substituto() -> None:
    sem_final = informado(estado="desconhecido", estado_final=None, tentativas=8)
    assert operacao(informado=sem_final).consulta_acabou(8)
    assert not operacao(informado=sem_final).consulta_acabou(9)
    fora = informado(
        estado=None, desfecho="sistema_fora", estado_final=None, tentativas=0, ultimo_desfecho="sistema_fora"
    )
    assert operacao(informado=fora).consulta_acabou(8)
    assert operacao(informado=None, substituto="CLR-2026-00000099").consulta_acabou(8)
    assert not operacao(informado=None).consulta_acabou(8)
    assert not operacao(informado=informado(tentativas=8)).consulta_acabou(8)  # estado final: nada a perguntar
    assert not operacao(envio_nivel=None, informado=None, substituto="CLR-2026-00000099").consulta_acabou(8)


@pytest.mark.parametrize(
    ("decisao", "final", "ultimo", "contrariado"),
    [
        ("nao_aconteceu", "concluida", "concluida", "concluida"),
        ("nao_aconteceu", "parcial", "parcial", "parcial"),
        ("aconteceu", "recusada", "recusada", "recusada"),
        ("aconteceu", "parada", "parada", "parada"),
        ("aconteceu", None, "nao_recebida", "nao_recebida"),  # uma sozinha tambem: o ADMIN fica sabendo
        ("aconteceu", "concluida", "desconhecido", None),
        ("nao_aconteceu", "concluida", "desconhecido", "concluida"),
        ("aconteceu", "concluida", "concluida", None),
        ("nao_aconteceu", "recusada", "recusada", None),
        ("aconteceu", "desfeita", "desfeita", None),
        ("nao_aconteceu", "desfeita", "desfeita", None),
        ("nao_sei", "concluida", "concluida", None),
        ("aconteceu", None, "em_andamento", None),
    ],
)
def test_contradiz_o_sistema(decisao: str, final: str | None, ultimo: str, contrariado: str | None) -> None:
    assert contradiz_o_sistema(decisao, informado(estado=ultimo, estado_final=final)) == contrariado


def test_sem_resposta_nada_contraria() -> None:
    assert contradiz_o_sistema("aconteceu", None) is None
    sem = informado(estado=None, desfecho="sem_resposta", estado_final=None)
    assert contradiz_o_sistema("aconteceu", sem) is None


def test_o_botao_so_registra_o_que_cabe_num_toque() -> None:
    assert dict(DECISAO_PELO_SISTEMA) == {
        "concluida": "aconteceu",
        "recusada": "nao_aconteceu",
        "nao_recebida": "nao_aconteceu",
        "parada": "nao_aconteceu",
    }
    assert set(DECISAO_PELO_SISTEMA) <= set(contrato.ESTADOS_FINAIS)
    for estado, decisao in DECISAO_PELO_SISTEMA.items():
        assert contradiz_o_sistema(decisao, informado(estado=estado, estado_final=estado)) is None


@pytest.mark.parametrize("de_terceiro", [False, True])
def test_texto_e_meta_da_nao_confirmada_com_resposta(de_terceiro: bool) -> None:
    op = operacao(
        sistema_origem="terceiro" if de_terceiro else "proprio",
        informado=informado(de_terceiro=de_terceiro, estado="nao_recebida", estado_final="nao_recebida"),
    )
    texto = texto_do_estado(op, consultas_maximas=8)
    quem = "O servidor de terceiros cobranca" if de_terceiro else "O sistema cobranca"
    assert f"{quem} informou: nao_recebida." in texto
    assert texto.isascii()
    esperado: dict[str, Any] = {"estado": "nao_recebida", "final": True}
    if de_terceiro:
        esperado["de_terceiro"] = True
    assert informado_no_meta(op) == esperado
    r = resposta(op, selo=2, consultas_maximas=8)
    assert r["_meta"][META_INFORMADO] == esperado
    assert r["_meta"]["br.com.claro.mcp-claro/selo"] == 2


@pytest.mark.parametrize(
    ("decisao", "ainda"),
    [(None, True), ("aconteceu", False), ("nao_aconteceu", False), ("nao_sei", False)],
)
def test_depois_da_decisao_o_texto_nao_convida_a_registrar_de_novo(decisao: str | None, ainda: bool) -> None:
    """A decisao da pessoa e uma so (``decisoes_nao_confirmada.operacao_id`` e unica, e a entrada so insere): depois
    dela, o texto ao aplicativo nao diz que a pessoa ainda pode registrar o que viu."""
    op = operacao(informado=informado(), decisao_pessoa=decisao)
    texto = texto_do_estado(op, consultas_maximas=8)
    assert "O sistema cobranca informou: concluida." in texto
    assert ("A pessoa ainda pode registrar o que viu" in texto) is ainda
    assert ("A pessoa registrou que" in texto) is not ainda
    assert texto.isascii()


@pytest.mark.parametrize(
    "estado", [Estado.ENVIADA, Estado.ENVIANDO, Estado.SISTEMA_PEDIU_DADOS, Estado.NAO_SAIU, Estado.LIBERADA]
)
def test_o_meta_informado_so_vai_na_nao_confirmada(estado: Estado) -> None:
    """Desenho 7.3: so a "nao confirmada" com resposta leva ``_meta`` ``informado``. Uma enviada de nivel 3 com recibo
    (o sistema informou pelo recibo) nao leva."""
    recibo = informado(origem="recibo", estado="concluida", estado_final="concluida", tentativas=0)
    op = operacao(
        estado=estado,
        envio_nivel=3,
        informado=recibo,
        resultado={"content": [{"type": "text", "text": "ok"}], "isError": False},
    )
    assert informado_no_meta(op) is None
    assert META_INFORMADO not in resposta(op, selo=3, consultas_maximas=8)["_meta"]
    assert META_INFORMADO in resposta(operacao(envio_nivel=3, informado=recibo), selo=3)["_meta"]


def test_texto_da_nao_confirmada_sem_numero_e_o_da_fase_2() -> None:
    op = operacao(envio_nivel=None, informado=None)
    assert texto_do_estado(op) == (
        "Estamos confirmando: a acao CLR-2026-00000088 foi enviada uma vez, mas a resposta do sistema nao chegou"
        " inteira. Nada sera reenviado. Nao repita o pedido: confira no sistema."
    )
    assert informado_no_meta(op) is None


def test_texto_quando_a_escada_acabou_sem_resposta_final() -> None:
    sem = informado(
        estado=None, desfecho="sem_resposta", estado_final=None, tentativas=8, ultimo_desfecho="sem_resposta"
    )
    op = operacao(informado=sem)
    assert "ele nao confirmou; a decisao fica com a pessoa" in texto_do_estado(op, consultas_maximas=8)
    assert "esta perguntando" in texto_do_estado(op, consultas_maximas=9)
    assert informado_no_meta(op) is None  # sem resposta, nada do sistema no _meta


def test_o_resultado_em_outro_protocolo_nao_vai_no_meta() -> None:
    op = operacao(informado=informado(), substituto="CLR-2026-00000099")
    assert informado_no_meta(op) is None
    assert "O protocolo CLR-2026-00000099 saiu depois ao sistema com o mesmo numero" in texto_do_estado(op)


def test_enviada_com_o_mesmo_numero_diz_o_protocolo_original() -> None:
    op = operacao(
        estado=Estado.ENVIADA,
        resultado={"content": [{"type": "text", "text": "ok"}], "isError": False},
        mesmo_numero_de="CLR-2026-00000077",
    )
    r = resposta(op, selo=2)
    assert r["content"][0]["text"] == (
        "Protocolo CLR-2026-00000088: enviada uma vez ao sistema. Saiu com o mesmo numero do protocolo"
        " CLR-2026-00000077."
    )


@pytest.mark.anyio
async def test_terceiro_que_disse_nao_recebida_nao_tem_o_botao(monkeypatch: pytest.MonkeyPatch) -> None:
    op = operacao(
        sistema_origem="terceiro",
        informado=informado(de_terceiro=True, estado="nao_recebida", estado_final="nao_recebida"),
    )

    async def por_id(_c: Any, _i: uuid.UUID) -> Operacao:
        return op

    monkeypatch.setattr(fila, "por_id", por_id)
    with pytest.raises(fila.DecisaoRecusada) as recusa:
        await fila.registrar_o_que_o_sistema_informou(
            None,  # type: ignore[arg-type]
            op.id,
            pessoa_id=op.pessoa_id,
            estado_visto="nao_recebida",
        )
    assert recusa.value.motivo == "terceiro_nao_recebida"
    assert recusa.value.mensagem.isascii()


# --- Retoque da etapa 3 (G4r) -----------------------------------------------------------------------------------

SETE_DIAS = timedelta(days=contrato.REAPROVEITAR_NUMERO_DIAS)


def test_consulta_acaba_pelo_tempo_pela_hora_do_banco() -> None:
    """Decisao 3 do coordenador: ``escada_vencida`` com o envio e o ``now()`` do banco lidos na mesma linha; a escada
    acaba pelo tempo mesmo sem nenhuma linha e sem tentativas, e nunca com estado final."""
    enviado = AGORA - SETE_DIAS
    vencida = operacao(informado=None, envio_gravado_em=enviado, lido_em=AGORA)
    assert vencida.escada_vencida
    assert vencida.consulta_acabou(8)
    quase = operacao(informado=None, envio_gravado_em=enviado + timedelta(microseconds=1), lido_em=AGORA)
    assert not quase.escada_vencida
    assert not quase.consulta_acabou(8)
    sem_tentativas = informado(estado=None, desfecho="sem_chave", estado_final=None, tentativas=0)
    assert operacao(informado=sem_tentativas, envio_gravado_em=enviado, lido_em=AGORA).consulta_acabou(8)
    assert not operacao(informado=informado(), envio_gravado_em=enviado, lido_em=AGORA).consulta_acabou(8)
    assert not operacao(informado=None, envio_gravado_em=enviado, lido_em=None).escada_vencida  # sem a hora do banco
    sem_numero = operacao(envio_nivel=None, informado=None, envio_gravado_em=enviado, lido_em=AGORA)
    assert not sem_numero.consulta_acabou(8)
    texto = texto_do_estado(vencida, consultas_maximas=8)
    assert "ele nao confirmou; a decisao fica com a pessoa" in texto


def _original(**mudar: Any) -> fila.EscadaDaOriginal:
    base: dict[str, Any] = {
        "enviado_em": AGORA - timedelta(hours=9),
        "estado_final_do_numero": None,
        "ultima_consulta": "desconhecido",
        "tentativas": 8,
        "ultimo_desfecho": "respondeu",
    }
    base.update(mudar)
    return fila.EscadaDaOriginal(**base)


@pytest.mark.parametrize(
    ("mudar", "esperado"),
    [
        ({}, True),
        ({"tentativas": 7}, False),
        ({"tentativas": 2, "ultimo_desfecho": "sistema_fora"}, True),
        ({"tentativas": 2, "enviado_em": AGORA - SETE_DIAS}, True),
        ({"tentativas": 2, "enviado_em": AGORA - SETE_DIAS + timedelta(microseconds=1)}, False),
        ({"estado_final_do_numero": "concluida"}, False),  # na original ou em outra acao com o numero dela
        ({"ultima_consulta": "em_andamento"}, False),
        ({"ultima_consulta": None}, False),
    ],
)
def test_a_original_ficou_em_desconhecido(mudar: dict[str, Any], esperado: bool) -> None:
    """Critica M4.2: a ficha do pedido igual so oferece o numero novo quando a original terminou a escada sem resposta
    final, com a ultima resposta a consulta ``desconhecido``."""
    op = operacao(
        estado=Estado.AGUARDANDO_FICHA,
        envio_nivel=None,
        informado=None,
        repete_protocolo="CLR-2026-00000077",
        original=_original(**mudar),
        lido_em=AGORA,
    )
    assert op.original_ficou_em_desconhecido(8) is esperado


def test_sem_original_lida_nao_ha_numero_novo() -> None:
    assert not operacao(
        repete_protocolo="CLR-2026-00000077", original=None, lido_em=AGORA
    ).original_ficou_em_desconhecido(8)
    assert not operacao(repete_protocolo=None, original=_original(), lido_em=AGORA).original_ficou_em_desconhecido(8)


@pytest.mark.parametrize(
    ("estado", "diz"),
    [(Estado.NAO_SAIU, False), (Estado.SISTEMA_PEDIU_DADOS, False), (Estado.ENVIADA, True)],
)
def test_so_a_enviada_diz_que_saiu_com_o_numero_da_original(estado: Estado, diz: bool) -> None:
    """Decisao 5 do coordenador: o pedido igual que nao chegou ao fim no sistema nao diz que saiu com o numero da
    original (a frase se contradiria com "Nada aconteceu")."""
    op = operacao(estado=estado, resultado=None, mesmo_numero_de="CLR-2026-00000077")
    texto = texto_do_estado(op, consultas_maximas=8)
    assert ("Saiu com o mesmo numero do protocolo CLR-2026-00000077." in texto) is diz
    assert texto.isascii()


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("visto", "contradisse", "motivo"),
    [
        ("concluida", True, "sistema_se_contradisse"),
        ("recusada", True, "sistema_se_contradisse"),
        ("recusada", False, "informado_mudou"),
        (None, False, "informado_mudou"),
    ],
)
async def test_o_botao_registra_so_o_que_a_pessoa_viu(
    monkeypatch: pytest.MonkeyPatch, visto: str | None, contradisse: bool, motivo: str
) -> None:
    """Decisao 6 do coordenador, sem banco: a contradicao do sistema sobre o numero recusa antes de tudo (mesmo com o
    estado de agora), e o estado de agora diferente do que a pessoa viu recusa ``informado_mudou``."""
    op = operacao(informado=informado(estado="concluida", estado_final="concluida", se_contradisse=contradisse))

    async def por_id(_c: Any, _i: uuid.UUID) -> Operacao:
        return op

    monkeypatch.setattr(fila, "por_id", por_id)
    with pytest.raises(fila.DecisaoRecusada) as recusa:
        await fila.registrar_o_que_o_sistema_informou(
            None,  # type: ignore[arg-type]
            op.id,
            pessoa_id=op.pessoa_id,
            estado_visto=visto,  # type: ignore[arg-type]
        )
    assert recusa.value.motivo == motivo
    assert recusa.value.mensagem.isascii()
