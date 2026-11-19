"""Estados da porta de saida e codigo de conferencia, sem banco: a tabela fecha, e o codigo muda com o pedido."""

from __future__ import annotations

import uuid
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_claro.saida import estados
from mcp_claro.saida.estados import ANTES_DE_SAIR, FINAIS, FRASE, TRANSICOES, Estado
from mcp_claro.saida.protocolo import canonico, chave_pedido_igual, codigo_conferencia, protocolo_valido

SISTEMA = uuid.UUID("00000000-0000-0000-0000-00000000000a")


def test_toda_acao_tem_frase_e_os_finais_nao_tem_saida() -> None:
    assert set(FRASE) == set(Estado)
    assert all(FRASE[e].strip() for e in Estado)
    assert {de for de, _ in TRANSICOES} == set(Estado) - FINAIS
    assert all(not estados.pode(f, outro) for f in FINAIS for outro in Estado)


def test_so_sai_quem_passou_por_enviando() -> None:
    saidos = {Estado.ENVIADA, Estado.NAO_SAIU, Estado.SISTEMA_PEDIU_DADOS, Estado.NAO_CONFIRMADA}
    for de, para in TRANSICOES:
        if para in saidos:
            assert de == Estado.ENVIANDO
        if de == Estado.ENVIANDO:
            assert para in saidos, "de enviando nao se cancela nem se para"
    assert {para for de, para in TRANSICOES if de in ANTES_DE_SAIR} >= {
        Estado.CANCELADA,
        Estado.PARADA_POR_BLOQUEIO,
        Estado.PARADA_POR_RESTAURACAO,
    }
    assert ANTES_DE_SAIR.isdisjoint(FINAIS)


def test_nao_confirmada_nao_volta_para_a_fila() -> None:
    assert Estado.NAO_CONFIRMADA in FINAIS
    assert not any(estados.pode(Estado.NAO_CONFIRMADA, e) for e in Estado)
    assert not any(estados.pode(e, Estado.LIBERADA) for e in Estado if e != Estado.AGUARDANDO_FICHA)


valores = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=20),
    lambda filhos: st.lists(filhos, max_size=4) | st.dictionaries(st.text(max_size=8), filhos, max_size=4),
    max_leaves=12,
)
argumentos = st.dictionaries(st.text(min_size=1, max_size=10), valores, max_size=5)


@settings(max_examples=200, deadline=None, database=None)
@given(argumentos, st.text(min_size=1, max_size=10), valores)
def test_codigo_muda_quando_qualquer_argumento_muda(args: dict[str, Any], campo: str, novo: Any) -> None:
    codigo = codigo_conferencia(SISTEMA, "criar_chamado", args)
    # A ordem das chaves nao muda o codigo: o pedido e o mesmo.
    assert codigo_conferencia(SISTEMA, "criar_chamado", dict(reversed(list(args.items())))) == codigo
    trocado = {**args, campo: novo}
    if canonico(trocado) != canonico(args):
        assert codigo_conferencia(SISTEMA, "criar_chamado", trocado) != codigo
    assert codigo_conferencia(SISTEMA, "outra_ferramenta", args) != codigo
    assert codigo_conferencia(uuid.uuid4(), "criar_chamado", args) != codigo


@settings(max_examples=100, deadline=None, database=None)
@given(argumentos)
def test_codigo_e_legivel(args: dict[str, Any]) -> None:
    codigo = codigo_conferencia(SISTEMA, "criar_chamado", args)
    partes = codigo.split("-")
    assert [len(p) for p in partes] == [4, 4, 4]
    assert not set("".join(partes)) & set("01ILOS5")


def test_pedido_igual_depende_de_pessoa_aplicativo_ferramenta_e_argumentos() -> None:
    p, a, f = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    base = chave_pedido_igual(p, a, f, {"titulo": "x"})
    assert chave_pedido_igual(p, a, f, {"titulo": "x"}) == base
    assert chave_pedido_igual(uuid.uuid4(), a, f, {"titulo": "x"}) != base
    assert chave_pedido_igual(p, uuid.uuid4(), f, {"titulo": "x"}) != base
    assert chave_pedido_igual(p, a, uuid.uuid4(), {"titulo": "x"}) != base
    assert chave_pedido_igual(p, a, f, {"titulo": "y"}) != base


def test_protocolo_valido() -> None:
    assert protocolo_valido("CLR-2026-00000012")
    for errado in ("CLR-26-00000012", "clr-2026-00000012", "CLR-2026-0000001", " CLR-2026-00000012", None, 12):
        assert not protocolo_valido(errado)
