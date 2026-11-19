"""Porta unica de saida (Fase 2), de ponta a ponta: acao com ficha, espera, cancelar, pedido igual,
bloqueio no meio da fila, restauracao, "estamos confirmando", consulta pelo protocolo (so a mesma pessoa pelo
mesmo aplicativo) e periodo de experiencia.

Mesmo arranjo de ``test_autorizacao``, com o sistema de exemplo ``acoes_caixa`` (conta cada chamada).
Entrada, despachante e rotinas conectam cada um com o proprio usuario do banco, entao as permissoes da
migracao tambem sao provadas. As pessoas do arranjo sao de teste, entao as acoes valem com
``FEATURE_PHASE2_ACT`` desligado.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import text

from exemplos import acoes_caixa
from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.regras import bloqueios
from mcp_claro.saida import fila
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.pedidos import CONSULTA, Pedidos
from mcp_claro.versao import PREFIXO_META
from tests.apoio import cliente_mcp
from tests.integracao.test_autorizacao import RECURSO, Ambiente, subir

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

ACOES = (
    ("criar_chamado", "chamado", "muda", "medio"),
    ("enviar_mensagem", "mensagem", "muda", "medio"),
    ("cancelar_servico", "cancelar", "muda", "alto"),
)


@contextlib.asynccontextmanager
async def ambiente_de_acoes(
    banco: Parametros, monkeypatch: pytest.MonkeyPatch, *, extra: dict[str, Any] | None = None
) -> AsyncIterator[Ambiente]:
    """``extra`` muda parametros da instalacao (por exemplo, ``FEATURE_PHASE2_ACT``)."""
    async with subir(
        banco,
        monkeypatch,
        criar_sistema=acoes_caixa.criar,
        revisoes=ACOES,
        extra={"OUTBOX_SWEEP_SECONDS": 1} | (extra or {}),
    ) as a:
        assert (await a.autorizar(a.ana_id, "ana")).status_code == 200
        assert (await a.autorizar(a.bia_id, "bia")).status_code == 200
        async with a.admin.begin() as c:
            # Fora do periodo de experiencia, para os limites valerem nos testes que os usam.
            await c.execute(
                text("UPDATE ordens_servico SET criado_em = now() - interval '60 days' WHERE aplicativo_id = :a"),
                {"a": a.aplicativo_id},
            )
        a.zerar_vistas()
        yield a


@pytest.fixture
async def amb(banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Ambiente]:
    async with ambiente_de_acoes(banco_migrado, monkeypatch) as a:
        # O aplicativo do arranjo e o mesmo em todos os testes: o teto de fichas por hora fica alto aqui e
        # tem teste proprio.
        await politica(a, CONFIRM_CAP_PER_HOUR=1000)
        try:
            yield a
        finally:
            async with a.admin.begin() as c:
                await c.execute(text("DELETE FROM politica WHERE autor = 'teste-saida'"))
                await c.execute(
                    text(
                        "UPDATE bloqueios SET encerrado_em = now(), encerrado_por = 'teste'"
                        " WHERE encerrado_em IS NULL AND (alcance IN ('tudo', 'sistema') OR aplicativo_id = :a)"
                    ),
                    {"a": a.aplicativo_id},
                )


async def politica(amb: Ambiente, **valores: int) -> None:
    async with amb.admin.begin() as c:
        for chave, valor in valores.items():
            await c.execute(
                text("INSERT INTO politica (chave, valor, autor, motivo) VALUES (:k, :v, 'teste-saida', 'teste')"),
                {"k": chave, "v": valor},
            )


async def chamar(amb: Ambiente, original: str, argumentos: dict[str, Any], sujeito: str = "pessoa-ana") -> Any:
    """Uma acao pelo aplicativo. Recusa sai como ``MCPError`` (fora do grupo de tarefas do cliente)."""
    recusa: MCPError | None = None
    async with cliente_mcp(f"{amb.entrada}/mcp", amb.cracha(sujeito)) as c:
        try:
            return await c.call_tool(amb.vitrine[original], argumentos)
        except MCPError as erro:
            recusa = erro
    raise recusa


async def consultar(amb: Ambiente, protocolo: str, sujeito: str = "pessoa-ana") -> Any:
    async with cliente_mcp(f"{amb.entrada}/mcp", amb.cracha(sujeito)) as c:
        return await c.call_tool(CONSULTA, {"protocolo": protocolo})


def meta(resultado: Any) -> dict[str, Any]:
    return dict(resultado.meta or {})


async def operacao(amb: Ambiente, protocolo: str) -> fila.Operacao:
    async with amb.admin.connect() as c:
        op = (await c.execute(text("SELECT id FROM operacoes WHERE protocolo = :p"), {"p": protocolo})).scalar_one()
        achada = await fila.por_id(c, op)
    assert achada is not None
    return achada


async def esperar_estado(amb: Ambiente, protocolo: str, *estados: Estado, prazo: float = 15.0) -> fila.Operacao:
    fim = asyncio.get_running_loop().time() + prazo
    while True:
        op = await operacao(amb, protocolo)
        if op.estado in estados:
            return op
        if asyncio.get_running_loop().time() > fim:
            raise AssertionError(f"{protocolo} ficou em {op.estado}, esperado {estados}")
        await asyncio.sleep(0.1)


def chamadas(amb: Ambiente, nome: str) -> int:
    return int(amb.sistema_app.state.contagem.chamadas.get(nome, 0))


async def confirmar(amb: Ambiente, op: fila.Operacao, *, pessoa_id: uuid.UUID, com_passkey: bool = False) -> None:
    assert op.ficha_id is not None
    async with amb.entrada_db.begin() as c:
        await fila.confirmar_ficha(
            c, op.ficha_id, pessoa_id=pessoa_id, com_passkey=com_passkey, espera_segundos=0, rapida_segundos=3
        )


async def test_acao_com_ficha_sai_uma_vez_e_aparece_na_consulta(amb: Ambiente) -> None:
    r = await chamar(amb, "criar_chamado", {"titulo": "Internet caiu"})
    m = meta(r)
    protocolo = m[PREFIXO_META + "protocolo"]
    assert m[PREFIXO_META + "estado"] == "aguardando_ficha"
    assert not r.is_error
    assert "Nada saiu ainda" in r.content[0].text
    assert chamadas(amb, "criar_chamado") == 0

    op = await operacao(amb, protocolo)
    assert op.codigo_conferencia == m[PREFIXO_META + "codigo_conferencia"]
    await confirmar(amb, op, pessoa_id=amb.ana_id)
    enviada = await esperar_estado(amb, protocolo, Estado.ENVIADA)
    assert chamadas(amb, "criar_chamado") == 1
    assert enviada.resultado is not None

    c = await consultar(amb, protocolo)
    textos = " ".join(b.text for b in c.content if hasattr(b, "text"))
    assert "Feito: criar_chamado" in textos
    assert meta(c)[PREFIXO_META + "estado"] == "enviada"

    # Outra pessoa, mesmo aplicativo: o protocolo nao existe para ela.
    outra = await consultar(amb, protocolo, sujeito="pessoa-bia")
    assert outra.is_error
    assert "nao encontrado" in outra.content[0].text


async def test_pedido_igual_devolve_a_operacao_original(amb: Ambiente) -> None:
    args = {"titulo": "Pedido repetido"}
    primeiro = meta(await chamar(amb, "criar_chamado", args))[PREFIXO_META + "protocolo"]
    op = await operacao(amb, primeiro)
    await confirmar(amb, op, pessoa_id=amb.ana_id)
    await esperar_estado(amb, primeiro, Estado.ENVIADA)
    segundo = await chamar(amb, "criar_chamado", args)
    assert meta(segundo)[PREFIXO_META + "protocolo"] == primeiro
    assert meta(segundo)[PREFIXO_META + "repetido"] is True
    assert chamadas(amb, "criar_chamado") == 1


async def test_cancelado_antes_de_sair_nao_chega_ao_sistema(amb: Ambiente) -> None:
    await politica(amb, OUTBOX_HOLD_SECONDS=60)
    protocolo = meta(await chamar(amb, "criar_chamado", {"titulo": "Vou desistir"}))[PREFIXO_META + "protocolo"]
    op = await operacao(amb, protocolo)
    assert op.ficha_id is not None
    async with amb.entrada_db.begin() as c:
        await fila.confirmar_ficha(
            c, op.ficha_id, pessoa_id=amb.ana_id, com_passkey=False, espera_segundos=60, rapida_segundos=3
        )
    liberada = await operacao(amb, protocolo)
    assert liberada.estado == Estado.LIBERADA
    async with amb.entrada_db.begin() as c:
        cancelada = await fila.cancelar(c, op.id, pessoa_id=amb.ana_id)
    assert cancelada.estado == Estado.CANCELADA
    await asyncio.sleep(1.5)
    assert chamadas(amb, "criar_chamado") == 0
    with pytest.raises(fila.DecisaoRecusada):
        async with amb.entrada_db.begin() as c:
            await fila.cancelar(c, op.id, pessoa_id=amb.ana_id)


async def test_risco_alto_exige_passkey(amb: Ambiente) -> None:
    r = await chamar(amb, "cancelar_servico", {"servico": "tv"})
    protocolo = meta(r)[PREFIXO_META + "protocolo"]
    assert "com a passkey" in r.content[0].text
    op = await operacao(amb, protocolo)
    with pytest.raises(fila.DecisaoRecusada) as erro:
        await confirmar(amb, op, pessoa_id=amb.ana_id, com_passkey=False)
    assert erro.value.motivo == "falta_passkey"
    await confirmar(amb, op, pessoa_id=amb.ana_id, com_passkey=True)
    await esperar_estado(amb, protocolo, Estado.ENVIADA)
    assert chamadas(amb, "cancelar_servico") == 1


async def test_bloqueio_no_meio_da_fila_para_o_resto_e_guarda_comprovante(amb: Ambiente) -> None:
    await politica(amb, OUTBOX_HOLD_SECONDS=30)
    protocolos = []
    for n in range(4):
        protocolos.append(meta(await chamar(amb, "criar_chamado", {"titulo": f"Fila {n}"}))[PREFIXO_META + "protocolo"])
    ops = [await operacao(amb, p) for p in protocolos]
    # A primeira sai ja; as outras ficam na espera de 30 segundos.
    await confirmar(amb, ops[0], pessoa_id=amb.ana_id)
    await esperar_estado(amb, protocolos[0], Estado.ENVIADA)
    for op in ops[1:3]:
        assert op.ficha_id is not None
        async with amb.entrada_db.begin() as c:
            await fila.confirmar_ficha(
                c, op.ficha_id, pessoa_id=amb.ana_id, com_passkey=False, espera_segundos=30, rapida_segundos=3
            )
    async with amb.entrada_db.begin() as c:
        b = await bloqueios.bloquear(
            c, "bloqueio", "pessoa", bloqueios.quem_pessoa(amb.ana_id), "teste da fila", pessoa_id=amb.ana_id
        )
    async with amb.entrada_db.connect() as c:
        comprovante = await fila.comprovante_do_bloqueio(c, b.id)
    assert comprovante is not None
    assert {p["protocolo"] for p in comprovante.paradas} == set(protocolos[1:])
    assert {p["estava"] for p in comprovante.paradas} == {"liberada", "aguardando_ficha"}
    assert [s["protocolo"] for s in comprovante.ja_saidas] == [protocolos[0]]
    for p in protocolos[1:]:
        assert (await operacao(amb, p)).estado == Estado.PARADA_POR_BLOQUEIO
    assert chamadas(amb, "criar_chamado") == 1
    async with amb.entrada_db.begin() as c:
        await bloqueios.encerrar(c, b.id, bloqueios.quem_admin(amb.admin_id))


async def test_restauracao_para_a_fila_e_nada_sai_sem_nova_confirmacao(amb: Ambiente) -> None:
    await politica(amb, OUTBOX_HOLD_SECONDS=30)
    esperando = meta(await chamar(amb, "criar_chamado", {"titulo": "Esperando ficha"}))[PREFIXO_META + "protocolo"]
    liberada = meta(await chamar(amb, "criar_chamado", {"titulo": "Na espera"}))[PREFIXO_META + "protocolo"]
    op = await operacao(amb, liberada)
    assert op.ficha_id is not None
    async with amb.entrada_db.begin() as c:
        await fila.confirmar_ficha(
            c, op.ficha_id, pessoa_id=amb.ana_id, com_passkey=False, espera_segundos=30, rapida_segundos=3
        )
    async with amb.despachante_db.begin() as c:
        await fila.parar_por_restauracao(c)
    # As duas linhas deste teste param pela restauracao (o banco de teste e um so: outras linhas tambem
    # podem parar, por isso a conferencia e linha a linha).
    for p in (esperando, liberada):
        parada = await operacao(amb, p)
        assert parada.estado == Estado.PARADA_POR_RESTAURACAO
        assert parada.desfecho == "parada por restauracao"
    async with amb.admin.connect() as c:
        quem = set(
            (
                await c.execute(
                    text(
                        "SELECT f.mudou_por FROM fila_saida f JOIN operacoes o ON o.id = f.operacao_id"
                        " WHERE o.protocolo = ANY(:p)"
                    ),
                    {"p": [esperando, liberada]},
                )
            ).scalars()
        )
    assert quem == {"restauracao"}
    pendente = await operacao(amb, esperando)
    with pytest.raises(fila.DecisaoRecusada):
        await confirmar(amb, pendente, pessoa_id=amb.ana_id)
    assert chamadas(amb, "criar_chamado") == 0


async def test_limite_libera_sem_ficha_e_nunca_passa_do_teto(amb: Ambiente) -> None:
    await politica(amb, OUTBOX_HOLD_SECONDS=0)
    async with amb.entrada_db.begin() as c:
        await c.execute(
            text(
                "INSERT INTO limites (pessoa_id, aplicativo_id, sistema_id, nome_original, destinatarios, por_dia,"
                " valido_ate, com_passkey) VALUES (:p, :a, :s, 'enviar_mensagem',"
                " CAST(:d AS jsonb), 2, now() + interval '1 day', true)"
            ),
            {
                "p": amb.ana_id,
                "a": amb.aplicativo_id,
                "s": amb.sistema_id,
                "d": '[{"campo": "destinatario", "valores": ["equipe"]}]',
            },
        )
    fora = meta(await chamar(amb, "enviar_mensagem", {"destinatario": "estranho", "texto": "oi"}))
    assert fora[PREFIXO_META + "estado"] == "aguardando_ficha"
    estados = []
    for n in range(3):
        r = await chamar(amb, "enviar_mensagem", {"destinatario": "equipe", "texto": f"aviso {n}"})
        estados.append(meta(r)[PREFIXO_META + "estado"])
    assert estados[2] == "aguardando_ficha"
    assert all(e in ("liberada", "enviando", "enviada") for e in estados[:2])
    await asyncio.sleep(2)
    assert chamadas(amb, "enviar_mensagem") == 2


async def test_teto_de_fichas_pausa_o_aplicativo_e_para_a_fila(amb: Ambiente) -> None:
    async with amb.admin.connect() as c:
        ja = await fila.fichas_na_ultima_hora(c, amb.aplicativo_id)
    await politica(amb, CONFIRM_CAP_PER_HOUR=ja + 2)
    primeiros = [
        meta(await chamar(amb, "criar_chamado", {"titulo": f"Teto {n}"}))[PREFIXO_META + "protocolo"] for n in range(2)
    ]
    with pytest.raises(MCPError) as erro:
        await chamar(amb, "criar_chamado", {"titulo": "Teto demais"})
    assert "foi pausado" in str(erro.value)
    for p in primeiros:
        assert (await operacao(amb, p)).estado == Estado.PARADA_POR_BLOQUEIO
    async with amb.admin.connect() as c:
        pausa = (
            await c.execute(
                text(
                    "SELECT quem, tipo FROM bloqueios WHERE aplicativo_id = :a AND encerrado_em IS NULL"
                    " AND alcance = 'aplicativo'"
                ),
                {"a": amb.aplicativo_id},
            )
        ).one()
    assert (pausa.quem, pausa.tipo) == ("rotina:teto_confirmacoes", "pausa")
    assert chamadas(amb, "criar_chamado") == 0


# --- Consulta pelo protocolo por outro aplicativo ---------------------------------------------


async def outro_aplicativo_da_ana(amb: Ambiente) -> tuple[uuid.UUID, str]:
    """Um segundo aplicativo habilitado, com a mesma ordem de servico da Ana. Devolve (id, client_id)."""
    client_id = f"outro-app-{uuid.uuid4().hex[:8]}"
    async with amb.entrada_db.begin() as c:
        app_id = await repositorio.habilitar_aplicativo(c, client_id=client_id, nome="Outro app", familia="teste")
        modelo = (
            await c.execute(text("SELECT modelo_id FROM ordens_servico WHERE id = :o"), {"o": amb.ordens["pessoa-ana"]})
        ).scalar_one()
        await repositorio.conceder_ordem(
            c, pessoa_id=amb.ana_id, aplicativo_id=app_id, modelo_id=modelo, dias=7, max_dias=30
        )
    return app_id, client_id


async def test_consulta_pelo_protocolo_por_outro_aplicativo_da_mesma_pessoa_nao_acha(amb: Ambiente) -> None:
    r = await chamar(amb, "criar_chamado", {"titulo": "So pelo Claude Code"})
    protocolo = meta(r)[PREFIXO_META + "protocolo"]
    outro_id, outro_client = await outro_aplicativo_da_ana(amb)

    # A mesma Ana, pelo outro aplicativo: a ferramenta de consulta responde "nao encontrado", sem estado.
    cracha = amb.idp.cracha(audiencia=RECURSO, sujeito="pessoa-ana", client_id=outro_client)
    async with cliente_mcp(f"{amb.entrada}/mcp", cracha) as c:
        assert amb.vitrine["criar_chamado"] in {t.name for t in (await c.list_tools()).tools}
        pelo_outro = await c.call_tool(CONSULTA, {"protocolo": protocolo})
    assert pelo_outro.is_error
    assert "nao encontrado" in pelo_outro.content[0].text
    assert PREFIXO_META + "estado" not in meta(pelo_outro)

    # Pelo mesmo aplicativo, a consulta acha.
    pelo_mesmo = await consultar(amb, protocolo)
    assert not pelo_mesmo.is_error
    assert meta(pelo_mesmo)[PREFIXO_META + "protocolo"] == protocolo

    # Direto na entrada: o recorte e pessoa e aplicativo, os dois.
    pedidos = Pedidos(amb.p, amb.entrada_db)
    assert await pedidos.consultar(protocolo, pessoa_id=amb.ana_id, aplicativo_id=outro_id) is None
    assert await pedidos.consultar(protocolo, pessoa_id=amb.bia_id, aplicativo_id=amb.aplicativo_id) is None
    achada = await pedidos.consultar(protocolo, pessoa_id=amb.ana_id, aplicativo_id=amb.aplicativo_id)
    assert achada is not None
    assert achada.protocolo == protocolo


# --- Periodo de experiencia -------------------------------------------------------------------


async def ordem_criada_ha(amb: Ambiente, dias: int) -> None:
    async with amb.admin.begin() as c:
        await c.execute(
            text(
                "UPDATE ordens_servico SET criado_em = now() - make_interval(days => :d)"
                " WHERE pessoa_id = :p AND aplicativo_id = :a"
            ),
            {"d": dias, "p": amb.ana_id, "a": amb.aplicativo_id},
        )


async def rota_do_pedido(amb: Ambiente, protocolo: str) -> str:
    async with amb.admin.connect() as c:
        return str(
            (
                await c.execute(
                    text("SELECT dados ->> 'rota' FROM extrato WHERE tipo = 'acao_pedida' AND protocolo = :p"),
                    {"p": protocolo},
                )
            ).scalar_one()
        )


async def test_periodo_de_experiencia_manda_tudo_para_a_ficha_e_depois_o_limite_vale(amb: Ambiente) -> None:
    await politica(amb, TRIAL_PERIOD_DAYS=14, OUTBOX_HOLD_SECONDS=600)
    async with amb.entrada_db.begin() as c:
        limite = (
            await c.execute(
                text(
                    "INSERT INTO limites (pessoa_id, aplicativo_id, sistema_id, nome_original, destinatarios, por_dia,"
                    " valido_ate, com_passkey) VALUES (:p, :a, :s, 'enviar_mensagem',"
                    " CAST(:d AS jsonb), 5, now() + interval '1 day', true) RETURNING id"
                ),
                {
                    "p": amb.ana_id,
                    "a": amb.aplicativo_id,
                    "s": amb.sistema_id,
                    "d": '[{"campo": "destinatario", "valores": ["equipe"]}]',
                },
            )
        ).scalar_one()

    async def usados() -> int:
        async with amb.admin.connect() as c:
            return int(
                (await c.execute(text("SELECT usados_hoje FROM limites WHERE id = :i"), {"i": limite})).scalar_one()
            )

    # Ordem de 13 dias, dentro dos 14 de experiencia: o limite cobre, mas o pedido vai para a ficha.
    await ordem_criada_ha(amb, 13)
    dentro = meta(await chamar(amb, "enviar_mensagem", {"destinatario": "equipe", "texto": "na experiencia"}))
    assert dentro[PREFIXO_META + "estado"] == "aguardando_ficha"
    assert await rota_do_pedido(amb, dentro[PREFIXO_META + "protocolo"]) == "periodo_de_experiencia"
    assert (await operacao(amb, dentro[PREFIXO_META + "protocolo"])).limite_id is None
    assert await usados() == 0

    # Ordem de 15 dias: passou a experiencia, o limite vale (liberada, com a espera visivel).
    await ordem_criada_ha(amb, 15)
    depois = meta(await chamar(amb, "enviar_mensagem", {"destinatario": "equipe", "texto": "depois da experiencia"}))
    assert depois[PREFIXO_META + "estado"] == "liberada"
    assert await rota_do_pedido(amb, depois[PREFIXO_META + "protocolo"]) == "limite"
    assert (await operacao(amb, depois[PREFIXO_META + "protocolo"])).limite_id == limite
    assert await usados() == 1
    assert chamadas(amb, "enviar_mensagem") == 0

    # A acao liberada nao fica para tras (sai so daqui a dez minutos).
    async with amb.entrada_db.begin() as c:
        op = await operacao(amb, depois[PREFIXO_META + "protocolo"])
        await fila.cancelar(c, op.id, pessoa_id=amb.ana_id)
