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
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
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
        await fila.registrar_o_que_o_sistema_informou(None, op.id, pessoa_id=op.pessoa_id)  # type: ignore[arg-type]
    assert recusa.value.motivo == "terceiro_nao_recebida"
    assert recusa.value.mensagem.isascii()
