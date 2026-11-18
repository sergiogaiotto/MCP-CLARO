"""Extrato so de acrescentar e fechamento encadeado, contra PostgreSQL real."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from mcp_claro.extrato import conferir_fechamento, fechar, registrar

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


async def test_extrato_aceita_insert_e_recusa_update_delete(conexao) -> None:  # type: ignore[no-untyped-def]
    n = await registrar(
        conexao, papel="entrada", tipo="partida", campos={"papel": "entrada", "versao": "0.1.0"}
    )
    await conexao.commit()
    assert n > 0
    with pytest.raises(DBAPIError, match="so de acrescentar"):
        await conexao.execute(text("UPDATE extrato SET frase = 'x' WHERE id = :id"), {"id": n})
    await conexao.rollback()
    with pytest.raises(DBAPIError, match="so de acrescentar"):
        await conexao.execute(text("DELETE FROM extrato WHERE id = :id"), {"id": n})
    await conexao.rollback()
    with pytest.raises(DBAPIError, match="so de acrescentar"):
        await conexao.execute(text("TRUNCATE extrato"))
    await conexao.rollback()


async def test_fechamento_encadeia_e_confere(conexao) -> None:  # type: ignore[no-untyped-def]
    for i in range(3):
        await registrar(conexao, papel="rotinas", tipo="fechamento", campos={"numero": i, "linhas": i})
    await conexao.commit()
    f1 = await fechar(conexao)
    await conexao.commit()
    assert f1.linhas >= 3
    assert await conferir_fechamento(conexao, f1.numero)
    await registrar(conexao, papel="rotinas", tipo="partida", campos={"papel": "rotinas", "versao": "x"})
    await conexao.commit()
    f2 = await fechar(conexao)
    await conexao.commit()
    assert f2.numero == f1.numero + 1
    assert f2.linhas == 1
    assert await conferir_fechamento(conexao, f2.numero)
    anterior = (
        await conexao.execute(
            text("SELECT hash_anterior FROM fechamentos WHERE numero = :n"), {"n": f2.numero}
        )
    ).scalar_one()
    assert bytes(anterior) == f1.hash


async def test_fechamento_nao_pode_ser_alterado(conexao) -> None:  # type: ignore[no-untyped-def]
    await registrar(conexao, papel="rotinas", tipo="partida", campos={"papel": "rotinas", "versao": "x"})
    await conexao.commit()
    f = await fechar(conexao)
    await conexao.commit()
    with pytest.raises(DBAPIError, match="so de acrescentar"):
        await conexao.execute(text("UPDATE fechamentos SET linhas = 0 WHERE numero = :n"), {"n": f.numero})
    await conexao.rollback()


async def test_papel_entrada_nao_insere_direto(conexao) -> None:  # type: ignore[no-untyped-def]
    privilegios = (
        await conexao.execute(
            text(
                "SELECT has_table_privilege('mcp_claro_entrada', 'extrato', 'INSERT'),"
                "       has_table_privilege('mcp_claro_entrada', 'extrato', 'UPDATE'),"
                "       has_table_privilege('mcp_claro_entrada', 'extrato', 'DELETE'),"
                "       has_function_privilege('mcp_claro_entrada',"
                "         'extrato_registrar(text,text,text,text,uuid,uuid,uuid,text,jsonb,text)', 'EXECUTE')"
            )
        )
    ).one()
    assert tuple(privilegios) == (False, False, False, True)
