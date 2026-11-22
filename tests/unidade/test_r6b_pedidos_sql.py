"""R6b-4, sem banco: as constantes de ``telas/consultas_terceiros.py``, o texto das consultas da tela e os indices da
migracao 0006 dizem o mesmo. As consultas levam os tipos no texto do SQL, nunca por parametro: so assim o banco usa
o indice parcial, tambem no plano guardado (o EXPLAIN de verdade esta em ``test_migracao_0006``)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from mcp_claro.telas import consultas_terceiros as ct

RAIZ = Path(__file__).resolve().parents[2]


def _migracao() -> str:
    return (RAIZ / "migrations" / "versions" / "0006_fase3.py").read_text(encoding="utf-8")


def _indice(nome: str) -> str:
    """O ``CREATE INDEX`` de ``nome`` na 0006, ate o ponto e virgula, com os espacos juntados."""
    trecho = _migracao().split(f"CREATE INDEX {nome} ON extrato", 1)[1].split(";", 1)[0]
    return " ".join(f"CREATE INDEX {nome} ON extrato{trecho}".split())


def _tipos(texto: str) -> set[str]:
    return set(re.findall(r"'([a-z_]+)'", texto)) - {"pedidos", "array"}


def _predicado(sql: str) -> str:
    return " ".join(sql.split(" AND (tipo IN ", 1)[1].split())


def test_o_predicado_da_tela_e_o_do_indice_da_0006() -> None:
    indice = _indice("extrato_pedido_barrado_idx")
    assert indice.startswith("CREATE INDEX extrato_pedido_barrado_idx ON extrato (sistema_id, criado_em DESC) WHERE ")
    no_indice = indice.split(" WHERE ", 1)[1]
    na_tela = "tipo IN " + _predicado(ct._SQL_PEDIDOS)
    # O mesmo texto, so sem o parentese de fora que a consulta poe por causa do AND.
    assert na_tela == f"{no_indice})", (na_tela, no_indice)
    assert _tipos(no_indice) == _tipos(ct._SQL_PEDIDOS) == {*ct.TIPOS_PEDIDO_BARRADO, *ct.TIPOS_FIM_COM_PEDIDOS}
    assert "declaracoes_sistema_pediu_dados" in ct.TIPOS_PEDIDO_BARRADO  # contrato K4
    assert "sondagem_sistema_pediu_dados" in ct.TIPOS_PEDIDO_BARRADO
    assert ct.TIPOS_FIM_COM_PEDIDOS == ("acao_enviada", "acao_nao_confirmada", "acao_nao_saiu")  # contrato K9


def test_a_ultima_quarentena_e_o_indice_dela_dizem_o_mesmo() -> None:
    indice = _indice("extrato_quarentena_idx")
    assert indice == (
        "CREATE INDEX extrato_quarentena_idx ON extrato (sistema_id, criado_em DESC)"
        " WHERE tipo IN ('sistema_em_quarentena', 'sistema_em_quarentena_sem_resposta')"
    )
    assert "tipo IN ('sistema_em_quarentena', 'sistema_em_quarentena_sem_resposta')" in ct.SQL_ULTIMA_QUARENTENA
    assert _tipos(indice) == set(ct.TIPOS_QUARENTENA) == _tipos(ct.SQL_ULTIMA_QUARENTENA) - {"ferramentas"}


def test_as_consultas_da_tela_nao_levam_os_tipos_por_parametro() -> None:
    for sql in (ct._SQL_PEDIDOS, ct.SQL_ULTIMA_QUARENTENA):
        assert set(re.findall(r"(?<!:):([a-z_]+)", sql)) == {"s", "dias"}, sql
        assert "ANY" not in sql


def test_o_modulo_nao_guarda_outra_copia_do_predicado() -> None:
    # Um so lugar com o texto do predicado no codigo: a constante da consulta.
    fonte = Path(sys.modules[ct.__name__].__file__ or "").read_text(encoding="utf-8")
    assert fonte.count("'declaracoes_sistema_pediu_dados'") == 1
    assert fonte.count("'sistema_em_quarentena_sem_resposta'") == 1
