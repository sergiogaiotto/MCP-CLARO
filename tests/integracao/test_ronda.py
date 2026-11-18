"""Ronda das portas de ponta a ponta: PostgreSQL 18 real, provedor falso e entrada real.

A ronda roda com o usuario ``mcp_claro_rotinas`` de verdade e entra pelo endereco da entrada, como
um aplicativo de fora. Prova que toda tentativa indevida e recusada e que uma falha plantada (um
verificador de cracha alterado que aceita qualquer cracha) fecha a entrada por etapas.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from mcp_claro.config.settings import Parametros
from mcp_claro.ronda import Caso, ConfigRonda, Situacao, rodar_ronda
from tests.apoio import cabecalhos_2026, pedido_2026
from tests.integracao.test_ronda_apoio import ADMIN, Entrada, FonteFalsa, VerificadorQueAceitaTudo, subir_entrada

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


@pytest.fixture
async def entrada(banco_migrado: Parametros) -> AsyncIterator[Entrada]:
    async with subir_entrada(banco_migrado) as e:
        yield e


@pytest.fixture
async def entrada_com_falha(banco_migrado: Parametros) -> AsyncIterator[Entrada]:
    async with subir_entrada(banco_migrado, planta=VerificadorQueAceitaTudo) as e:
        yield e


def config(e: Entrada, **kw: Any) -> ConfigRonda:
    return ConfigRonda(alvo=e.url, falhas_para_fechar=2, prazo_tentativa=5.0, **kw)


async def linhas(e: Entrada, rodada: uuid.UUID) -> dict[str, Any]:
    async with e.dono.connect() as c:
        resultado = await c.execute(text("SELECT * FROM ronda WHERE rodada = :r ORDER BY id"), {"r": rodada})
        return {linha.caso: linha for linha in resultado}


async def nivel(e: Entrada) -> str:
    async with e.dono.connect() as c:
        return str((await c.execute(text("SELECT nivel FROM estado_entrada WHERE id = 1"))).scalar_one())


async def extrato(e: Entrada, tipo: str, rodada: uuid.UUID | None = None) -> list[Any]:
    async with e.dono.connect() as c:
        return list(
            await c.execute(
                text(
                    "SELECT frase, dados FROM extrato WHERE tipo = :t"
                    " AND (CAST(:r AS text) IS NULL OR dados ->> 'rodada' = :r) ORDER BY id"
                ),
                {"t": tipo, "r": str(rodada) if rodada else None},
            )
        )


async def avisos(e: Entrada, rodada: uuid.UUID) -> list[Any]:
    async with e.dono.connect() as c:
        return list(
            await c.execute(
                text("SELECT * FROM avisos WHERE chave_unica LIKE :k ORDER BY id"), {"k": f"ronda:{rodada}%"}
            )
        )


async def test_ronda_toda_tentativa_recusada(entrada: Entrada) -> None:
    fonte = FonteFalsa(entrada.idp)
    r = await rodar_ronda(entrada.p, entrada.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=config(entrada))
    por_caso = {t.caso: t for t in r.tentativas}
    assert set(por_caso) == {str(c) for c in Caso}
    assert all(t.situacao is Situacao.CONFORME for t in r.tentativas), [
        (str(t.caso), t.obtido, str(t.situacao)) for t in r.tentativas
    ]
    assert por_caso[Caso.CRACHA_FALSO].obtido == "401"
    assert por_caso[Caso.CRACHA_VENCIDO].obtido == "401"
    assert por_caso[Caso.OUTRO_CONDOMINIO].obtido == "401"
    assert por_caso[Caso.OUTRA_PESSOA].detalhes["motivo"] == "fora_da_vitrine"
    # A ronda escolhe uma ferramenta que esta na vitrine de outra pessoa.
    assert por_caso[Caso.OUTRA_PESSOA].detalhes["ferramenta"].startswith("fatura_ana_")
    assert por_caso[Caso.VERSAO_ANTIGA_INITIALIZE].obtido == "400 erro -32022"
    assert por_caso[Caso.VERSAO_ANTIGA_CABECALHO].obtido == "400 erro -32022"
    assert r.resumo.controle_ok
    assert not r.resumo.falhou
    assert r.nivel_antes == r.nivel_depois == "aberta"
    guardadas = await linhas(entrada, r.id)
    assert set(guardadas) == set(por_caso)
    assert all(linha.defendeu for linha in guardadas.values())
    assert guardadas["controle"].detalhes["situacao"] == "conforme"
    assert guardadas["cracha_falso"].esperado == "401"
    assert await extrato(entrada, "ronda_falha", r.id) == []
    # Nada de cracha na tabela.
    assert not any("eyJ" in str(dict(linha._mapping)) for linha in guardadas.values())


async def test_falha_plantada_fecha_a_entrada_por_etapas(entrada_com_falha: Entrada) -> None:
    e = entrada_com_falha
    fonte = FonteFalsa(e.idp)
    cfg = config(e)
    r1 = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
    passaram = {t.caso for t in r1.resumo.passagens}
    assert passaram == {Caso.CRACHA_FALSO, Caso.CRACHA_VENCIDO, Caso.OUTRO_CONDOMINIO}
    assert r1.resumo.controle_ok
    assert (r1.seguidas, r1.nivel_depois) == (1, "aberta")
    guardadas = await linhas(e, r1.id)
    assert not guardadas["cracha_falso"].defendeu
    assert guardadas["cracha_falso"].detalhes["situacao"] == "passou"
    (falha,) = await extrato(e, "ronda_falha", r1.id)
    assert "cracha falso" in falha.frase
    assert "cracha vencido" in falha.frase

    # Segunda rodada seguida: a entrada fica so de leitura, com frase no extrato e aviso ao ADMIN.
    r2 = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
    assert (r2.seguidas, r2.nivel_antes, r2.nivel_depois) == (2, "aberta", "so_leitura")
    assert await nivel(e) == "so_leitura"
    (fechou,) = await extrato(e, "ronda_fechou_entrada", r2.id)
    assert "2 rondas seguidas" in fechou.frase
    assert "so de leitura" in fechou.frase
    mudou = await extrato(e, "estado_entrada_mudou")
    assert mudou[-1].frase == "A rotina ronda deixou a entrada so de leitura."
    (aviso,) = await avisos(e, r2.id)
    assert (aviso.canal, aviso.destino, aviso.evento) == ("email", ADMIN, "ronda_fechou_entrada")
    assert aviso.corpo["nivel"] == "so_leitura"
    async with e.dono.connect() as c:
        motivo = (await c.execute(text("SELECT mudou_por, motivo FROM estado_entrada"))).one()
    assert motivo.mudou_por == "rotina:ronda"
    assert "cracha falso" in motivo.motivo

    # A contagem recomeca depois da mudanca: mais duas rodadas para o proximo degrau.
    r3 = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
    assert (r3.seguidas, r3.nivel_depois) == (1, "so_leitura")
    r4 = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
    assert (r4.seguidas, r4.nivel_antes, r4.nivel_depois) == (2, "so_leitura", "fechada")
    assert await nivel(e) == "fechada"
    assert len(await avisos(e, r4.id)) == 1

    # Fechada, a entrada recusa ate a lista de ferramentas; a ronda nao desce mais nada.
    async with httpx2.AsyncClient() as http:
        resposta = await http.post(
            f"{e.url}/mcp",
            json=pedido_2026("tools/list"),
            headers=cabecalhos_2026("tools/list", chave=e.cracha(sujeito="pessoa-ana")),
        )
    assert resposta.json()["error"]["data"]["br.com.claro.mcp-claro/motivo"] == "entrada_fechada"
    r5 = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
    assert r5.nivel_depois == "fechada"
    assert await avisos(e, r5.id) == []

    # O ADMIN reabre depois de apurar: a contagem zera.
    async with e.dono.begin() as c:
        await c.execute(text("UPDATE estado_entrada SET nivel = 'aberta', mudou_em = clock_timestamp()"))
    r6 = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
    assert (r6.seguidas, r6.nivel_depois) == (1, "aberta")


async def test_fechamento_sozinho_desligado_so_avisa(entrada_com_falha: Entrada) -> None:
    e = entrada_com_falha
    fonte = FonteFalsa(e.idp)
    cfg = config(e, fechar_sozinha=False)
    r1 = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
    r2 = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
    r3 = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
    assert [r.seguidas for r in (r1, r2, r3)] == [1, 2, 3]
    assert all(r.nivel_depois == "aberta" for r in (r1, r2, r3))
    assert await nivel(e) == "aberta"
    (frase,) = await extrato(e, "ronda_sem_fechamento", r2.id)
    assert "desligado" in frase.frase
    assert len(await avisos(e, r2.id)) == 1
    assert await avisos(e, r3.id) == []  # um aviso por sequencia


async def test_sem_controle_a_rodada_nao_conta(entrada_com_falha: Entrada) -> None:
    """Se o cracha da ronda nao passa (controle fora do ar), a rodada e inconclusiva e nao fecha nada."""
    e = entrada_com_falha
    cfg = config(e)

    class FonteQueFalha(FonteFalsa):
        async def valido(self) -> str:
            from mcp_claro.ronda.cracha import FalhaCracha

            raise FalhaCracha("provedor fora do ar")

    fonte = FonteQueFalha(e.idp)
    for _ in range(3):
        r = await rodar_ronda(e.p, e.rotinas_db, fonte=fonte, emails_admin=[ADMIN], config=cfg)
        assert not r.resumo.controle_ok
        assert r.seguidas == 0
    assert await nivel(e) == "aberta"
    por_caso = {t.caso: t for t in r.tentativas}
    assert por_caso[Caso.CONTROLE].situacao is Situacao.INCONCLUSIVO
    assert por_caso[Caso.VERSAO_ANTIGA_CABECALHO].situacao is Situacao.PULADO


async def test_ronda_sem_identidade_grava_controle_inconclusivo(entrada: Entrada) -> None:
    r = await rodar_ronda(entrada.p, entrada.rotinas_db, fonte=None, config=config(entrada))
    (t,) = r.tentativas
    assert t.caso == Caso.CONTROLE
    assert t.situacao is Situacao.INCONCLUSIVO
    assert set(await linhas(entrada, r.id)) == {"controle"}


async def test_tabela_ronda_so_de_acrescentar(entrada: Entrada) -> None:
    r = await rodar_ronda(entrada.p, entrada.rotinas_db, fonte=FonteFalsa(entrada.idp), config=config(entrada))
    for comando in ("UPDATE ronda SET obtido = 'x' WHERE rodada = :r", "DELETE FROM ronda WHERE rodada = :r"):
        for engine in (entrada.rotinas_db, entrada.dono):
            with pytest.raises(DBAPIError):
                async with engine.begin() as c:
                    await c.execute(text(comando), {"r": r.id})
