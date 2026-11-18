"""Bloqueio de um toque, pausa, catraca e estado das portas, na entrada e no despachante.

Mesmo arranjo de ``test_autorizacao``: PostgreSQL 18, sistema de exemplo, OAuth falso, despachante e
entrada, cada papel com o proprio usuario do banco. Bloqueios que valem para todos (``tudo``), o
estado das portas e a politica sao sempre desfeitos no fim de cada teste.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import text

from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.regras import bloqueios, catraca, estado, revisao
from mcp_claro.regras.bloqueios import SemPermissao, quem_admin, quem_pessoa, quem_rotina
from tests.integracao.test_autorizacao import Ambiente, subir

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


@pytest.fixture
async def amb(banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Ambiente]:
    async with subir(banco_migrado, monkeypatch) as a:
        assert (await a.autorizar(a.ana_id, "ana")).status_code == 200
        assert (await a.autorizar(a.bia_id, "bia")).status_code == 200
        a.zerar_vistas()
        yield a


async def _ligar(amb: Ambiente, tipo: str, alcance: str, quem: str, **alvo: uuid.UUID) -> bloqueios.Bloqueio:
    engine = amb.rotinas_db if quem.startswith("rotina:") else amb.entrada_db
    papel = "rotinas" if quem.startswith("rotina:") else "entrada"
    async with engine.begin() as c:
        return await bloqueios.bloquear(c, tipo, alcance, quem, "teste do bloqueio", papel=papel, **alvo)


async def _encerrar(amb: Ambiente, b: bloqueios.Bloqueio, quem: str) -> bloqueios.Bloqueio:
    async with amb.entrada_db.begin() as c:
        return await bloqueios.encerrar(c, b.id, quem)


async def _ler_direto_no_despachante(amb: Ambiente, pessoa: str = "pessoa-ana") -> httpx2.Response:
    """Chama a porta interna sem passar pela entrada, como se a entrada tivesse sido alterada."""
    pessoa_id = amb.ana_id if pessoa == "pessoa-ana" else amb.bia_id
    async with amb.admin.connect() as c:
        ferramenta = (
            await c.execute(
                text("SELECT id FROM ferramentas_revisadas WHERE nome_vitrine = :n AND situacao = 'ativa'"),
                {"n": amb.fatura},
            )
        ).scalar_one()
    async with httpx2.AsyncClient() as http:
        return await http.post(
            f"{amb.despachante}/interno/ler",
            json={
                "pessoa_id": str(pessoa_id),
                "aplicativo_id": str(amb.aplicativo_id),
                "ordem_id": str(amb.ordens[pessoa]),
                "ferramenta_id": str(ferramenta),
                "argumentos": {"mes": "2026-11"},
            },
        )


async def _extrato(amb: Ambiente, tipo: str, pessoa_id: uuid.UUID | None = None) -> list[Any]:
    async with amb.admin.connect() as c:
        return list(
            (
                await c.execute(
                    text(
                        "SELECT frase, dados FROM extrato WHERE tipo = :t "
                        "AND (CAST(:p AS uuid) IS NULL OR pessoa_id = :p) ORDER BY id"
                    ),
                    {"t": tipo, "p": pessoa_id},
                )
            ).all()
        )


# --- Catraca ---------------------------------------------------------------------------------------


async def test_catraca_recusa_a_leitura_201_com_a_politica_inicial(amb: Ambiente) -> None:
    async with amb.admin.connect() as c:
        assert await catraca.teto_diario(c) == 200
    async with amb.admin.begin() as c:  # 200 leituras ja feitas hoje nesta ordem de servico
        await c.execute(
            text(
                "INSERT INTO leituras_diarias (ordem_id, dia, contagem) "
                "VALUES (:o, (now() AT TIME ZONE 'America/Sao_Paulo')::date, 200)"
            ),
            {"o": amb.ordens["pessoa-ana"]},
        )
    with pytest.raises(MCPError, match="limite de 200 leituras por dia"):
        await amb.ler_fatura("pessoa-ana", "2026-11")
    assert amb.sistema_app.state.contagem.chamadas == {}
    assert (await amb.ler_fatura("pessoa-bia", "2026-11")).is_error is False  # a ordem da bia e outra
    recusas = await _extrato(amb, "leitura_recusada", amb.ana_id)
    assert recusas[-1].frase.endswith("recusada: catraca.")


async def test_catraca_com_politica_baixa_recusa_a_quarta_leitura(amb: Ambiente) -> None:
    async with amb.admin.begin() as c:
        politica = (
            await c.execute(
                text(
                    "INSERT INTO politica (chave, valor, autor, motivo) "
                    "VALUES ('READS_CAP_PER_DAY', 3, 'teste', 'catraca baixa no teste') RETURNING id"
                )
            )
        ).scalar_one()
    try:
        for mes in ("2026-10", "2026-11", "2026-10"):
            assert (await amb.ler_fatura("pessoa-ana", mes)).is_error is False
        with pytest.raises(MCPError, match="limite de 3 leituras"):
            await amb.ler_fatura("pessoa-ana", "2026-11")
        assert amb.sistema_app.state.contagem.chamadas == {"consultar_fatura": 3}
        async with amb.admin.connect() as c:
            assert await catraca.usadas_hoje(c, amb.ordens["pessoa-ana"]) == 3
    finally:
        async with amb.admin.begin() as c:
            await c.execute(text("DELETE FROM politica WHERE id = :i"), {"i": politica})


async def test_catraca_nao_passa_do_teto_com_pedidos_ao_mesmo_tempo(amb: Ambiente) -> None:
    async def uma() -> bool:
        async with amb.entrada_db.begin() as c:
            return (await catraca.passar(c, amb.ordens["pessoa-bia"], teto=5)).passou

    passaram = await asyncio.gather(*(uma() for _ in range(20)))
    assert sum(passaram) == 5
    async with amb.admin.connect() as c:
        assert await catraca.usadas_hoje(c, amb.ordens["pessoa-bia"]) == 5


# --- Bloqueio e pausa ------------------------------------------------------------------------------


async def test_bloqueio_do_admin_recusa_na_entrada_e_so_o_admin_encerra(amb: Ambiente) -> None:
    b = await _ligar(amb, "bloqueio", "pessoa", quem_admin(amb.admin_id), pessoa_id=amb.ana_id)
    try:
        with pytest.raises(MCPError, match="Seu acesso ao MCP-CLARO esta bloqueado"):
            await amb.listar("pessoa-ana")
        with pytest.raises(MCPError, match="So o ADMIN pode desbloquear"):
            await amb.ler_fatura("pessoa-ana", "2026-11")
        assert amb.vistas() == []
        assert (await amb.ler_fatura("pessoa-bia", "2026-11")).is_error is False  # so a ana esta bloqueada
        with pytest.raises(SemPermissao, match="So o ADMIN"):
            await _encerrar(amb, b, quem_pessoa(amb.ana_id))
        with pytest.raises(SemPermissao, match="ADMIN ativo"):  # a ana se dizendo ADMIN
            await _encerrar(amb, b, quem_admin(amb.ana_id))
    finally:
        encerrado = await _encerrar(amb, b, quem_admin(amb.admin_id))
    assert encerrado.encerrado_por == quem_admin(amb.admin_id)
    assert (await amb.ler_fatura("pessoa-ana", "2026-11")).is_error is False
    assert [linha.frase for linha in await _extrato(amb, "bloqueio_ligado", amb.ana_id)] == [
        "O ADMIN ligou um bloqueio em uma pessoa."
    ]
    assert [linha.frase for linha in await _extrato(amb, "bloqueio_encerrado", amb.ana_id)] == [
        "O ADMIN encerrou um bloqueio em uma pessoa."
    ]
    assert any("bloqueio_pessoa" in linha.frase for linha in await _extrato(amb, "pedido_recusado", amb.ana_id))
    assert any("bloqueio_pessoa" in linha.frase for linha in await _extrato(amb, "leitura_recusada", amb.ana_id))


async def test_pausa_e_reversivel_pela_propria_pessoa(amb: Ambiente) -> None:
    pausa = await _ligar(
        amb,
        "pausa",
        "pessoa_aplicativo",
        quem_pessoa(amb.ana_id),
        pessoa_id=amb.ana_id,
        aplicativo_id=amb.aplicativo_id,
    )
    with pytest.raises(MCPError, match=r"pausado\. Voce pode retomar"):
        await amb.ler_fatura("pessoa-ana", "2026-11")
    assert (await amb.ler_fatura("pessoa-bia", "2026-11")).is_error is False
    with pytest.raises(SemPermissao):  # outra pessoa nao retoma a pausa da ana
        await _encerrar(amb, pausa, quem_pessoa(amb.bia_id))
    await _encerrar(amb, pausa, quem_pessoa(amb.ana_id))
    assert (await amb.ler_fatura("pessoa-ana", "2026-11")).is_error is False
    repetido = await _encerrar(amb, pausa, quem_pessoa(amb.ana_id))  # toque repetido nao muda nada
    assert repetido.encerrado_por == quem_pessoa(amb.ana_id)
    assert [linha.frase for linha in await _extrato(amb, "pausa_encerrada", amb.ana_id)] == [
        "A propria pessoa encerrou uma pausa em uma pessoa num aplicativo."
    ]


async def test_bloqueio_ligado_pela_propria_pessoa_so_o_admin_encerra(amb: Ambiente) -> None:
    b = await _ligar(
        amb,
        "bloqueio",
        "pessoa_aplicativo",
        quem_pessoa(amb.ana_id),
        pessoa_id=amb.ana_id,
        aplicativo_id=amb.aplicativo_id,
    )
    with pytest.raises(MCPError, match="Seu acesso por este aplicativo esta bloqueado"):
        await amb.ler_fatura("pessoa-ana", "2026-11")
    with pytest.raises(SemPermissao):
        await _encerrar(amb, b, quem_pessoa(amb.ana_id))
    await _encerrar(amb, b, quem_admin(amb.admin_id))
    assert (await amb.ler_fatura("pessoa-ana", "2026-11")).is_error is False


async def test_pessoa_nao_bloqueia_outra_nem_tudo(amb: Ambiente) -> None:
    with pytest.raises(SemPermissao):
        await _ligar(amb, "bloqueio", "pessoa", quem_pessoa(amb.ana_id), pessoa_id=amb.bia_id)
    with pytest.raises(SemPermissao):
        await _ligar(amb, "pausa", "tudo", quem_pessoa(amb.ana_id))
    with pytest.raises(SemPermissao):  # a ana nao e ADMIN no cadastro
        await _ligar(amb, "bloqueio", "tudo", quem_admin(amb.ana_id))
    with pytest.raises(bloqueios.BloqueioInvalido):
        await _ligar(amb, "bloqueio", "sistema", quem_admin(amb.admin_id))


async def test_bloqueio_de_sistema_esconde_da_vitrine_e_recusa_a_chamada(amb: Ambiente) -> None:
    b = await _ligar(amb, "bloqueio", "sistema", quem_rotina("ronda"), sistema_id=amb.sistema_id)
    try:
        assert await amb.listar("pessoa-ana") == []
        with pytest.raises(MCPError, match="O sistema desta ferramenta esta bloqueado"):
            await amb.ler_fatura("pessoa-ana", "2026-11")
        r = await _ler_direto_no_despachante(amb)
        assert r.status_code == 403
        assert r.json()["motivo"] == "bloqueio_sistema"
        assert amb.vistas() == []
        with pytest.raises(SemPermissao):  # rotina liga, mas nao encerra
            async with amb.rotinas_db.begin() as c:
                await bloqueios.encerrar(c, b.id, quem_rotina("ronda"), papel="rotinas")
    finally:
        await _encerrar(amb, b, quem_admin(amb.admin_id))
    assert await amb.listar("pessoa-ana") == sorted([amb.cadastro, amb.fatura])
    assert [linha.frase for linha in await _extrato(amb, "bloqueio_ligado") if "rotina" in linha.frase][-1] == (
        "A rotina ronda ligou um bloqueio em um sistema."
    )


@pytest.mark.parametrize(
    ("tipo", "alcance"),
    [
        ("bloqueio", "pessoa"),
        ("pausa", "pessoa"),
        ("bloqueio", "aplicativo"),
        ("pausa", "pessoa_aplicativo"),
        ("pausa", "sistema"),
        ("bloqueio", "tudo"),
    ],
)
async def test_despachante_recusa_mesmo_com_a_entrada_alterada(amb: Ambiente, tipo: str, alcance: str) -> None:
    alvo = {
        "pessoa": {"pessoa_id": amb.ana_id},
        "aplicativo": {"aplicativo_id": amb.aplicativo_id},
        "pessoa_aplicativo": {"pessoa_id": amb.ana_id, "aplicativo_id": amb.aplicativo_id},
        "sistema": {"sistema_id": amb.sistema_id},
        "tudo": {},
    }[alcance]
    b = await _ligar(amb, tipo, alcance, quem_admin(amb.admin_id), **alvo)
    try:
        r = await _ler_direto_no_despachante(amb)
        assert r.status_code == 403
        assert r.json()["motivo"] == f"{tipo}_{alcance}"
        assert "Nada foi enviado" in r.json()["mensagem"]
        assert amb.vistas() == []
    finally:
        await _encerrar(amb, b, quem_admin(amb.admin_id))
    assert (await _ler_direto_no_despachante(amb)).status_code == 200


async def test_bloqueio_de_tudo_recusa_todas_as_pessoas(amb: Ambiente) -> None:
    b = await _ligar(amb, "bloqueio", "tudo", quem_admin(amb.admin_id))
    try:
        for sujeito in ("pessoa-ana", "pessoa-bia"):
            with pytest.raises(MCPError, match="O MCP-CLARO esta bloqueado pelo ADMIN"):
                await amb.listar(sujeito)
            with pytest.raises(MCPError, match="bloqueado pelo ADMIN"):
                await amb.ler_fatura(sujeito, "2026-11")
        assert amb.vistas() == []
    finally:
        await _encerrar(amb, b, quem_admin(amb.admin_id))
    assert (await amb.ler_fatura("pessoa-bia", "2026-11")).is_error is False


async def test_iniciar_autorizacao_respeita_o_bloqueio(amb: Ambiente) -> None:
    from mcp_claro.despachante import autorizacao

    b = await _ligar(amb, "pausa", "pessoa", quem_pessoa(amb.ana_id), pessoa_id=amb.ana_id)
    try:
        with pytest.raises(autorizacao.AutorizacaoRecusada, match="pausado"):
            await amb.iniciar(amb.ana_id)
    finally:
        await _encerrar(amb, b, quem_pessoa(amb.ana_id))
    assert await amb.iniciar(amb.ana_id)


# --- Estado das portas -----------------------------------------------------------------------------


async def test_entrada_fechada_recusa_tudo_e_so_leitura_aceita_leitura(amb: Ambiente) -> None:
    try:
        async with amb.rotinas_db.begin() as c:
            await estado.mudar_entrada(
                c, "fechada", quem_rotina("ronda"), "teste: duas falhas seguidas", papel="rotinas"
            )
        with pytest.raises(MCPError, match="A entrada do MCP-CLARO esta fechada"):
            await amb.listar("pessoa-ana")
        with pytest.raises(MCPError, match="esta fechada"):
            await amb.ler_fatura("pessoa-bia", "2026-11")
        assert amb.vistas() == []
        async with amb.entrada_db.begin() as c:
            await estado.mudar_entrada(c, "so_leitura", quem_admin(amb.admin_id), "teste: reabrindo por etapas")
        assert await amb.listar("pessoa-ana") == sorted([amb.cadastro, amb.fatura])
        assert (await amb.ler_fatura("pessoa-ana", "2026-11")).is_error is False
    finally:
        async with amb.entrada_db.begin() as c:
            await estado.mudar_entrada(c, "aberta", quem_admin(amb.admin_id), "teste: fim")
    frases = [linha.frase for linha in await _extrato(amb, "estado_entrada_mudou")][-3:]
    assert frases == [
        "A rotina ronda deixou a entrada fechada.",
        "O ADMIN deixou a entrada so de leitura.",
        "O ADMIN deixou a entrada aberta.",
    ]


async def test_saida_fechada_o_despachante_nao_envia(amb: Ambiente) -> None:
    try:
        async with amb.despachante_db.begin() as c:
            await estado.mudar_saida(c, aberta=False, quem=quem_rotina("cofre"), motivo="teste: cofre trancado")
        with pytest.raises(MCPError, match="porta de saida do MCP-CLARO esta fechada"):
            await amb.ler_fatura("pessoa-ana", "2026-11")
        r = await _ler_direto_no_despachante(amb)
        assert r.json()["motivo"] == "saida_fechada"
        async with amb.despachante_db.connect() as c:
            with pytest.raises(estado.SaidaFechada):
                await estado.conferir_saida(c)
        assert amb.vistas() == []
    finally:
        async with amb.despachante_db.begin() as c:
            await estado.mudar_saida(c, aberta=True, quem=quem_admin(amb.admin_id), motivo="teste: fim")
    assert (await amb.ler_fatura("pessoa-ana", "2026-11")).is_error is False


# --- Revisao do ADMIN ------------------------------------------------------------------------------


async def test_revisao_recusa_acao_em_sistema_de_chave_compartilhada(amb: Ambiente) -> None:
    async with amb.entrada_db.connect() as c:  # a tela do ADMIN, com o usuario da entrada; sem COMMIT
        compartilhado = await repositorio.cadastrar_sistema(
            c,
            nome=f"compartilhado-{uuid.uuid4().hex[:6]}",
            endereco="https://s.exemplo/mcp",
            tipo_chave="compartilhada",
        )
        await revisao.conferir_revisao(c, compartilhado, "le")
        with pytest.raises(revisao.RevisaoRecusada, match="chave compartilhada"):
            await revisao.conferir_revisao(c, compartilhado, "muda")
        with pytest.raises(revisao.RevisaoRecusada, match="autorizacao por pessoa configurada"):
            await revisao.conferir_troca_tipo_chave(c, amb.sistema_id, "compartilhada")
    async with amb.admin.connect() as c:  # o dono do banco simula uma ferramenta que muda dados; sem COMMIT
        await c.execute(
            text("UPDATE ferramentas_revisadas SET classe = 'muda' WHERE nome_vitrine = :n"), {"n": amb.cadastro}
        )
        await c.execute(text("UPDATE sistemas SET autorizacao = NULL WHERE id = :s"), {"s": amb.sistema_id})
        with pytest.raises(revisao.RevisaoRecusada, match="1 ferramenta"):
            await revisao.conferir_troca_tipo_chave(c, amb.sistema_id, "compartilhada")
        await revisao.conferir_troca_tipo_chave(c, amb.sistema_id, "programa")
