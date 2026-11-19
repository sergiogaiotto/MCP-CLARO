"""Programas que rodam sozinhos (Fase 2, decisao 9): so lendo, com chave propria, de ponta a ponta.

Mesmo arranjo de ``test_autorizacao`` (``subir``): PostgreSQL 18, provedor de login falso, despachante e
entrada reais, cada papel com o proprio usuario do banco (as permissoes tambem sao provadas). Alem do sistema
de chave por pessoa do arranjo, sobe o sistema de exemplo ``leitura_de_programa`` (chave propria de programa)
e cadastra o programa ``robo-<sufixo>``, com responsavel ``pessoa-ana`` e substituto ``pessoa-bia``. O
cracha do programa e o do ``tests/idp_falso.py``, com ``azp`` do programa e o sujeito da conta de servico.

Tambem aqui: as rotinas dos programas (com o usuario das rotinas) e as telas do ADMIN e do responsavel
(com o usuario da entrada), montadas so com as rotas de ``telas/admin_programas.py``.

A tabela de contagem por minuto e as permissoes dela ainda nao estao na migracao: ``aplicar_sql_ate_a_0005``
as cria com o dono do banco.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette
from starlette.routing import Mount

from exemplos import leitura_de_programa
from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.entrada.cracha import ChavesPublicas
from mcp_claro.entrada.despachante_cliente import ClienteDespachante
from mcp_claro.rotinas.programas import cuidar_dos_programas
from mcp_claro.telas.admin_programas import MESMA_PESSOA, SEM_RESPONSAVEL, rotas_admin_programas
from mcp_claro.telas.config import ConfigTelas
from mcp_claro.telas.login import ProvedorLogin
from mcp_claro.telas.nucleo import Telas
from mcp_claro.telas.paginas import criar_ambiente
from mcp_claro.telas.seguranca import COOKIE_SESSAO
from mcp_claro.telas.sessoes import abrir_sessao
from mcp_claro.versao import PREFIXO_META
from tests.apoio import cliente_mcp, servidor
from tests.integracao.test_autorizacao import RECURSO, Ambiente, engine_do_papel, subir
from tests.integracao.test_telas import Navegador

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

LIMITE_POR_MINUTO = 5

# Ate entrar na 0005: contagem de pedidos por programa por minuto (PROGRAM_MAX_REQUESTS_PER_MINUTE).
SQL_ATE_A_0005 = (
    """CREATE TABLE IF NOT EXISTS programas_pedidos_minuto (
  aplicativo_id uuid        NOT NULL REFERENCES aplicativos (id),
  minuto        timestamptz NOT NULL,
  contagem      integer     NOT NULL DEFAULT 0 CHECK (contagem >= 0),
  PRIMARY KEY (aplicativo_id, minuto)
)""",
    "GRANT SELECT, INSERT, UPDATE (contagem) ON programas_pedidos_minuto TO mcp_claro_entrada",
    "GRANT SELECT, DELETE ON programas_pedidos_minuto TO mcp_claro_rotinas",
)


async def aplicar_sql_ate_a_0005(dono: AsyncEngine) -> None:
    """Ate entrar na 0005: o dono do banco cria a tabela de contagem por minuto e as permissoes dela."""
    async with dono.begin() as c:
        await c.execute(text("SELECT pg_advisory_xact_lock(hashtext('teste_programas_0005'))"))
        for comando in SQL_ATE_A_0005:
            await c.execute(text(comando))


def motivo(erro: MCPError) -> str:
    dados = erro.error.data or {}
    return str(dados.get(PREFIXO_META + "motivo"))


@dataclass
class Programas:
    amb: Ambiente
    sistema_app: Any
    sistema_id: uuid.UUID
    robo_id: uuid.UUID
    client_id: str
    nome: str
    conta: str
    conta_id: uuid.UUID
    ordem_id: uuid.UUID
    sufixo: str
    vitrine: dict[str, str]

    def cracha(self, client_id: str | None = None) -> str:
        return self.amb.idp.cracha(audiencia=RECURSO, sujeito=self.conta, client_id=client_id or self.client_id)

    async def listar(self, client_id: str | None = None) -> list[str]:
        recusa: MCPError | None = None
        async with cliente_mcp(f"{self.amb.entrada}/mcp", self.cracha(client_id)) as c:
            try:
                return sorted(t.name for t in (await c.list_tools()).tools)
            except MCPError as erro:
                recusa = erro
        raise recusa

    async def chamar(self, nome: str, argumentos: dict[str, Any]) -> Any:
        recusa: MCPError | None = None
        async with cliente_mcp(f"{self.amb.entrada}/mcp", self.cracha()) as c:
            try:
                return await c.call_tool(nome, argumentos)
            except MCPError as erro:
                recusa = erro
        raise recusa

    async def ler_pelo_despachante(self, ferramenta: str, argumentos: dict[str, Any]) -> httpx2.Response:
        """A porta interna do despachante, como se a entrada tivesse sido alterada de proposito."""
        async with self.amb.admin.connect() as c:
            ferramenta_id = (
                await c.execute(
                    text("SELECT id FROM ferramentas_revisadas WHERE nome_vitrine = :n AND situacao = 'ativa'"),
                    {"n": ferramenta},
                )
            ).scalar_one()
        corpo = {
            "pessoa_id": str(self.conta_id),
            "aplicativo_id": str(self.robo_id),
            "ordem_id": str(self.ordem_id),
            "ferramenta_id": str(ferramenta_id),
            "argumentos": argumentos,
        }
        async with httpx2.AsyncClient(timeout=10) as http:
            return await http.post(f"{self.amb.despachante}/interno/ler", json=corpo)

    async def mudar(self, sql: str, **valores: Any) -> None:
        async with self.amb.admin.begin() as c:
            await c.execute(text(sql), {"a": self.robo_id} | valores)

    async def extrato(self, tipo: str) -> list[Any]:
        async with self.amb.admin.connect() as c:
            return list(
                (
                    await c.execute(
                        text("SELECT frase, dados, pessoa_id FROM extrato WHERE tipo = :t AND aplicativo_id = :a"),
                        {"t": tipo, "a": self.robo_id},
                    )
                ).all()
            )


@contextlib.asynccontextmanager
async def ambiente_de_programas(banco: Parametros, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Programas]:
    async with subir(banco, monkeypatch, extra={"PROGRAM_MAX_REQUESTS_PER_MINUTE": LIMITE_POR_MINUTO}) as a:
        await aplicar_sql_ate_a_0005(a.admin)
        s = uuid.uuid4().hex[:8]
        client_id, conta, chave = f"robo-{s}", f"service-account-robo-{s}", f"chave-robo-{s}"
        monkeypatch.setenv(
            "EXEMPLO_CHAVES", json.dumps({"chave-sondagem-programa": "sondagem", chave: f"programa:{client_id}"})
        )
        sistema_app = leitura_de_programa.criar()
        async with servidor(sistema_app) as url:
            async with a.entrada_db.begin() as c:  # o que a tela do ADMIN faria, com o usuario da entrada
                sistema_id = await repositorio.cadastrar_sistema(
                    c, nome=f"programa-{s}", endereco=f"{url}/mcp", tipo_chave="programa"
                )
            despachante = ClienteDespachante(a.p)
            try:
                sondagem = await despachante.post(
                    "/interno/sondar", {"sistema_id": str(sistema_id), "chave_sondagem": "chave-sondagem-programa"}
                )
                assert sondagem["aceito"], sondagem
                declaradas = (
                    await despachante.post(
                        "/interno/declaracoes",
                        {"sistema_id": str(sistema_id), "chave_sondagem": "chave-sondagem-programa"},
                    )
                )["ferramentas"]
                por_nome = {f["name"]: f for f in declaradas}
                vitrine = {original: f"{original}_{s}" for original in por_nome}
                async with a.entrada_db.begin() as c:
                    robo_id = await repositorio.habilitar_aplicativo(
                        c, client_id=client_id, nome=f"Robo {s}", familia="programa", tipo="programa"
                    )
                    await c.execute(
                        text(
                            "INSERT INTO programas (aplicativo_id, responsavel_id, substituto_id, finalidade, fim)"
                            " VALUES (:a, :r, :s, 'Conta os chamados abertos toda manha.', current_date + 30)"
                        ),
                        {"a": robo_id, "r": a.ana_id, "s": a.bia_id},
                    )
                    # A conta de servico do programa: pessoa de teste, sem nome (a leitura vale com a Fase 1).
                    conta_id = (await repositorio.pessoa_no_login(c, a.idp.emissor, conta, None)).id
                    await c.execute(text("UPDATE pessoas SET teste = true WHERE id = :p"), {"p": conta_id})
                    for original, classe in (
                        ("consultar_fila", "le"),
                        ("status_filas", "le"),
                        ("fechar_chamado", "muda"),
                    ):
                        await repositorio.revisar_ferramenta(
                            c,
                            sistema_id=sistema_id,
                            definicao_declarada=por_nome[original],
                            nome_vitrine=vitrine[original],
                            descricao="Ferramenta de teste do programa.",
                            classe=classe,
                            revisado_por="admin-teste",
                        )
                    modelo = await repositorio.criar_modelo_ordem(
                        c,
                        nome=f"robo-{s}",
                        descricao="Ordem do programa, com uma ferramenta de sistema de chave por pessoa",
                        ferramentas=[
                            (sistema_id, "consultar_fila"),
                            (sistema_id, "status_filas"),
                            (sistema_id, "fechar_chamado"),
                            (a.sistema_id, "consultar_fatura"),
                        ],
                        validade_dias=30,
                        criado_por="admin-teste",
                    )
                    ordem_id = await repositorio.conceder_ordem(
                        c, pessoa_id=conta_id, aplicativo_id=robo_id, modelo_id=modelo, dias=7, max_dias=30
                    )
                # A chave propria do programa vai cifrada ao cofre pelo despachante (o que a tela do ADMIN faz).
                guardada = await despachante.post(
                    "/interno/chaves",
                    {"sistema_id": str(sistema_id), "segredo": chave, "aplicativo_id": str(robo_id), "quem": "teste"},
                )
                assert guardada == {"guardada": True}
            finally:
                await despachante.fechar()
            sistema_app.state.contagem.autorizacoes_vistas.clear()
            yield Programas(
                amb=a,
                sistema_app=sistema_app,
                sistema_id=sistema_id,
                robo_id=robo_id,
                client_id=client_id,
                nome=f"Robo {s}",
                conta=conta,
                conta_id=conta_id,
                ordem_id=ordem_id,
                sufixo=s,
                vitrine=vitrine,
            )


@pytest.fixture
async def prog(banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Programas]:
    async with ambiente_de_programas(banco_migrado, monkeypatch) as pr:
        yield pr


# --- Leitura com a chave propria ---------------------------------------------------------------------


async def test_programa_le_sistema_de_chave_programa_com_a_chave_propria(prog: Programas) -> None:
    assert await prog.listar() == sorted([prog.vitrine["consultar_fila"], prog.vitrine["status_filas"]])
    r = await prog.chamar(prog.vitrine["consultar_fila"], {"fila": "suporte"})
    assert not r.is_error
    assert r.structured_content == {"fila": "suporte", "abertos": 2, "programa": prog.client_id}
    assert prog.sistema_app.state.contagem.chamadas == {"consultar_fila": 1}
    vistas = prog.sistema_app.state.contagem.autorizacoes_vistas
    assert vistas
    assert set(vistas) == {f"Bearer chave-robo-{prog.sufixo}"}
    feitas = await prog.extrato("programa_leitura_feita")
    assert [linha.frase for linha in feitas] == [
        f"Leitura em {prog.vitrine['consultar_fila']} feita pelo programa {prog.nome}, responsavel pessoa-ana."
    ]
    assert feitas[0].pessoa_id == prog.conta_id


async def test_sistema_de_chave_por_pessoa_fora_da_vitrine_e_a_chamada_recusada(prog: Programas) -> None:
    assert prog.amb.fatura not in await prog.listar()
    with pytest.raises(MCPError) as erro:
        await prog.chamar(prog.amb.fatura, {"mes": "2026-10"})
    assert motivo(erro.value) == "programa_sem_chave_propria"
    assert "chave propria do programa" in str(erro.value)
    assert prog.amb.sistema_app.state.contagem.chamadas == {}
    recusas = await prog.extrato("programa_leitura_recusada")
    assert [linha.frase for linha in recusas] == [
        f"Leitura em {prog.amb.fatura} pelo programa {prog.nome}, responsavel pessoa-ana, recusada:"
        " programa_sem_chave_propria."
    ]
    # O despachante reconfere antes de abrir qualquer chave, mesmo que a entrada fosse alterada.
    r = await prog.ler_pelo_despachante(prog.amb.fatura, {"mes": "2026-10"})
    assert r.status_code == 403
    assert r.json()["motivo"] == "programa_sem_chave_propria"
    assert prog.amb.sistema_app.state.contagem.chamadas == {}


async def test_assistente_nao_usa_sistema_de_chave_de_programa(prog: Programas) -> None:
    async with prog.amb.entrada_db.begin() as c:
        modelo = await repositorio.criar_modelo_ordem(
            c,
            nome=f"ana-fila-{prog.sufixo}",
            descricao="Ordem de pessoa com ferramenta de sistema de chave de programa",
            ferramentas=[(prog.sistema_id, "consultar_fila")],
            validade_dias=30,
            criado_por="admin-teste",
        )
        await repositorio.conceder_ordem(
            c, pessoa_id=prog.amb.ana_id, aplicativo_id=prog.amb.aplicativo_id, modelo_id=modelo, dias=7, max_dias=30
        )
    assert prog.vitrine["consultar_fila"] not in await prog.amb.listar("pessoa-ana")
    recusa: MCPError | None = None
    async with cliente_mcp(f"{prog.amb.entrada}/mcp", prog.amb.cracha("pessoa-ana")) as c:
        try:
            await c.call_tool(prog.vitrine["consultar_fila"], {"fila": "suporte"})
        except MCPError as erro:
            recusa = erro
    assert recusa is not None
    assert motivo(recusa) == "chave_de_programa"
    assert prog.sistema_app.state.contagem.chamadas == {}


# --- Cadastro que nao vale ---------------------------------------------------------------------------


async def test_programa_sem_cadastro_nao_passa(prog: Programas) -> None:
    outro = f"robo-sem-cadastro-{prog.sufixo}"
    async with prog.amb.entrada_db.begin() as c:
        outro_id = await repositorio.habilitar_aplicativo(
            c, client_id=outro, nome="Robo sem cadastro", familia="programa", tipo="programa"
        )
    with pytest.raises(MCPError) as erro:
        await prog.listar(client_id=outro)
    assert motivo(erro.value) == "programa_sem_cadastro"
    async with prog.amb.admin.connect() as c:
        frases = (
            await c.execute(
                text("SELECT frase FROM extrato WHERE tipo = 'programa_sem_cadastro' AND aplicativo_id = :a"),
                {"a": outro_id},
            )
        ).scalars()
        assert list(frases) == [
            "Pedido do aplicativo Robo sem cadastro, do tipo programa, recusado: falta o cadastro de programa, com"
            " responsavel."
        ]


@pytest.mark.parametrize(
    ("mudanca", "esperado"),
    [
        ("UPDATE programas SET situacao = 'pausado' WHERE aplicativo_id = :a", "programa_pausado"),
        ("UPDATE programas SET situacao = 'encerrado' WHERE aplicativo_id = :a", "programa_encerrado"),
        (
            "UPDATE programas SET fim = (now() AT TIME ZONE 'America/Sao_Paulo')::date - 1 WHERE aplicativo_id = :a",
            "programa_vencido",
        ),
        (
            "UPDATE pessoas SET situacao = 'desativada'"
            " WHERE id = (SELECT substituto_id FROM programas WHERE aplicativo_id = :a)",
            "programa_sem_responsavel",
        ),
    ],
)
async def test_programa_pausado_encerrado_vencido_ou_sem_responsavel_nao_passa(
    prog: Programas, mudanca: str, esperado: str
) -> None:
    assert await prog.listar()  # antes da mudanca, passa
    await prog.mudar(mudanca)
    with pytest.raises(MCPError) as erro:
        await prog.listar()
    assert motivo(erro.value) == esperado
    with pytest.raises(MCPError) as erro:
        await prog.chamar(prog.vitrine["consultar_fila"], {"fila": "suporte"})
    assert motivo(erro.value) == esperado
    # O despachante reconfere o cadastro antes de abrir a chave.
    r = await prog.ler_pelo_despachante(prog.vitrine["consultar_fila"], {"fila": "suporte"})
    assert r.status_code == 403
    assert r.json()["motivo"] == esperado
    assert prog.sistema_app.state.contagem.chamadas == {}
    recusas = await prog.extrato("programa_recusado")
    assert len(recusas) == 2
    assert all(linha.dados["motivo"] == esperado for linha in recusas)
    assert recusas[0].frase == f"Pedido do programa {prog.nome}, responsavel pessoa-ana, recusado: {esperado}."


# --- Acoes e limite por minuto -----------------------------------------------------------------------


async def test_acao_de_programa_recusada_com_acoes_de_programas_desligadas(prog: Programas) -> None:
    assert prog.amb.p.PROGRAM_ACTIONS_ENABLED is False
    assert prog.vitrine["fechar_chamado"] not in await prog.listar()
    with pytest.raises(MCPError) as erro:
        await prog.chamar(prog.vitrine["fechar_chamado"], {"id": "CH-7"})
    assert motivo(erro.value) == "programa_so_le"
    assert "fechar_chamado" not in prog.sistema_app.state.contagem.chamadas
    async with prog.amb.admin.connect() as c:
        operacoes = (
            await c.execute(text("SELECT count(*) FROM operacoes WHERE aplicativo_id = :a"), {"a": prog.robo_id})
        ).scalar_one()
    assert operacoes == 0


async def _longe_da_virada_do_minuto(dono: AsyncEngine) -> None:
    """O limite e por minuto do relogio: o teste nao comeca nos ultimos segundos de um minuto."""
    async with dono.connect() as c:
        segundo = float((await c.execute(text("SELECT extract(second FROM clock_timestamp())"))).scalar_one())
    if segundo > 45:
        await asyncio.sleep(61 - segundo)


async def test_limite_por_minuto_recusa_o_pedido_seguinte(prog: Programas) -> None:
    await _longe_da_virada_do_minuto(prog.amb.admin)
    passaram = 0
    recusa: MCPError | None = None
    async with cliente_mcp(f"{prog.amb.entrada}/mcp", prog.cracha()) as c:
        try:
            for _ in range(LIMITE_POR_MINUTO + 1):
                await c.list_tools(cache_mode="refresh")
                passaram += 1
        except MCPError as erro:
            recusa = erro
    assert passaram == LIMITE_POR_MINUTO
    assert recusa is not None
    assert motivo(recusa) == "programa_limite_minuto"
    assert f"limite de {LIMITE_POR_MINUTO} pedidos por minuto" in str(recusa)
    async with prog.amb.admin.connect() as c:
        contagem = (
            await c.execute(
                text(
                    "SELECT contagem FROM programas_pedidos_minuto"
                    " WHERE aplicativo_id = :a AND minuto = date_trunc('minute', now())"
                ),
                {"a": prog.robo_id},
            )
        ).scalar_one()
    assert contagem == LIMITE_POR_MINUTO
    recusas = await prog.extrato("programa_recusado")
    assert [linha.dados["motivo"] for linha in recusas] == ["programa_limite_minuto"]


async def test_limite_por_minuto_aguenta_pedidos_ao_mesmo_tempo(prog: Programas) -> None:
    await _longe_da_virada_do_minuto(prog.amb.admin)

    async def um() -> str:
        try:
            await prog.listar()
        except MCPError as erro:
            return motivo(erro)
        return "passou"

    desfechos = await asyncio.gather(*(um() for _ in range(LIMITE_POR_MINUTO + 4)))
    assert sorted(desfechos) == ["passou"] * LIMITE_POR_MINUTO + ["programa_limite_minuto"] * 4


# --- Rotinas -----------------------------------------------------------------------------------------


async def _situacao(prog: Programas) -> str:
    async with prog.amb.admin.connect() as c:
        return str(
            (
                await c.execute(text("SELECT situacao FROM programas WHERE aplicativo_id = :a"), {"a": prog.robo_id})
            ).scalar_one()
        )


async def _avisos(prog: Programas, evento: str, *, so_pessoas: bool = True) -> list[Any]:
    """Destinos dos avisos deste programa (por padrao, so os das pessoas: webhooks de outros testes ficam de fora)."""
    async with prog.amb.admin.connect() as c:
        linhas = (
            await c.execute(
                text(
                    "SELECT destino, corpo FROM avisos WHERE evento = :e AND corpo ->> 'aplicativo_id' = :a"
                    " ORDER BY destino = :r DESC, id"
                ),
                {"e": evento, "a": str(prog.robo_id), "r": f"pessoa:{prog.amb.ana_id}"},
            )
        ).all()
    if so_pessoas:
        return [linha.destino for linha in linhas if linha.destino.startswith("pessoa:")]
    return list(linhas)


async def test_rotina_pausa_o_programa_sem_uso_e_ele_nao_passa(prog: Programas) -> None:
    assert await prog.listar()
    await prog.mudar("UPDATE aplicativos SET visto_em = now() - interval '40 days' WHERE id = :a")
    await prog.mudar(
        "UPDATE programas SET revisado_em = now() - interval '40 days', criado_em = now() - interval '40 days'"
        " WHERE aplicativo_id = :a"
    )
    contagens = await cuidar_dos_programas(prog.amb.rotinas_db, prog.amb.p)
    assert contagens["sem_uso"] >= 1
    assert await _situacao(prog) == "pausado"
    linhas = await prog.extrato("programa_pausado_sem_uso")
    assert [linha.frase for linha in linhas] == [
        f"Programa {prog.nome}, responsavel pessoa-ana, pausado: 30 dias sem pedido."
    ]
    assert linhas[0].pessoa_id == prog.amb.ana_id
    assert await _avisos(prog, "programa_sem_uso") == [f"pessoa:{prog.amb.ana_id}", f"pessoa:{prog.amb.bia_id}"]
    with pytest.raises(MCPError) as erro:
        await prog.listar()
    assert motivo(erro.value) == "programa_pausado"
    # Outra rodada nao repete nada.
    await cuidar_dos_programas(prog.amb.rotinas_db, prog.amb.p)
    assert len(await prog.extrato("programa_pausado_sem_uso")) == 1
    assert len(await _avisos(prog, "programa_sem_uso")) == 2  # um ao responsavel, um ao substituto


async def test_revisao_vencida_pausa_e_avisa_o_responsavel(prog: Programas) -> None:
    await prog.mudar("UPDATE programas SET revisado_em = now() - interval '91 days' WHERE aplicativo_id = :a")
    await cuidar_dos_programas(prog.amb.rotinas_db, prog.amb.p, ["admin@exemplo.test"])
    assert await _situacao(prog) == "pausado"
    linhas = await prog.extrato("programa_pausado_revisao")
    assert len(linhas) == 1
    assert "a revisao de cada 90 dias venceu sem confirmacao; o responsavel foi avisado" in linhas[0].frase
    assert await _avisos(prog, "programa_revisao_vencida") == [f"pessoa:{prog.amb.ana_id}", f"pessoa:{prog.amb.bia_id}"]
    avisos = await _avisos(prog, "programa_revisao_vencida", so_pessoas=False)
    assert "admin@exemplo.test" in {a.destino for a in avisos}
    assert all("foi pausado" in a.corpo["texto"] for a in avisos)
    assert all("/telas/programas" in a.corpo["texto"] for a in avisos)
    with pytest.raises(MCPError) as erro:
        await prog.listar()
    assert motivo(erro.value) == "programa_pausado"


async def test_chave_a_vencer_avisa_uma_vez_e_chave_vencida_pausa(prog: Programas) -> None:
    await prog.mudar("UPDATE programas SET chave_trocada_em = now() - interval '80 days' WHERE aplicativo_id = :a")
    await cuidar_dos_programas(prog.amb.rotinas_db, prog.amb.p)
    await cuidar_dos_programas(prog.amb.rotinas_db, prog.amb.p)
    assert await _situacao(prog) == "ativo"
    assert len(await _avisos(prog, "programa_chave_a_vencer")) == 2  # responsavel e substituto, uma vez
    assert len(await prog.extrato("programa_chave_a_vencer")) == 1
    await prog.mudar("UPDATE programas SET chave_trocada_em = now() - interval '91 days' WHERE aplicativo_id = :a")
    await cuidar_dos_programas(prog.amb.rotinas_db, prog.amb.p)
    assert await _situacao(prog) == "pausado"
    assert len(await prog.extrato("programa_pausado_chave")) == 1


async def test_rotina_encerra_no_fim_e_pausa_sem_responsavel(prog: Programas) -> None:
    await prog.mudar("UPDATE pessoas SET situacao = 'desativada' WHERE id = :p", p=prog.amb.bia_id)
    try:
        await cuidar_dos_programas(prog.amb.rotinas_db, prog.amb.p)
        assert await _situacao(prog) == "pausado"
        assert len(await prog.extrato("programa_pausado_responsavel")) == 1
    finally:
        await prog.mudar("UPDATE pessoas SET situacao = 'ativa' WHERE id = :p", p=prog.amb.bia_id)
    await prog.mudar(
        "UPDATE programas SET fim = (now() AT TIME ZONE 'America/Sao_Paulo')::date - 1 WHERE aplicativo_id = :a"
    )
    await cuidar_dos_programas(prog.amb.rotinas_db, prog.amb.p)
    assert await _situacao(prog) == "encerrado"
    linhas = await prog.extrato("programa_encerrado_fim")
    assert len(linhas) == 1
    assert linhas[0].frase.startswith(f"Programa {prog.nome}, responsavel pessoa-ana, encerrado: o cadastro valia ate")


# --- Telas do ADMIN e do responsavel -----------------------------------------------------------------


PUBLICO = "https://mcp-claro.teste"


class _SemDespachante:
    async def post(self, caminho: str, corpo: dict[str, Any]) -> dict[str, Any]:
        raise AssertionError("estas telas nao falam com o despachante")


@dataclass
class Telinhas:
    base: str
    dono: AsyncEngine
    entrada: AsyncEngine
    http: httpx2.AsyncClient
    admin_id: uuid.UUID
    rui_id: uuid.UUID
    sara_id: uuid.UUID
    sufixo: str

    async def navegador(self, pessoa_id: uuid.UUID) -> tuple[Navegador, str]:
        async with self.entrada.begin() as c:
            identificador, csrf = await abrir_sessao(c, pessoa_id, prazo_minutos=60, auth_time=None)
        nav = Navegador(self.base, self.http)
        nav.cookies[COOKIE_SESSAO] = identificador
        return nav, csrf

    async def programa(self, client_id: str) -> Any:
        async with self.dono.connect() as c:
            return (
                await c.execute(
                    text(
                        "SELECT pr.*, a.nome FROM programas pr JOIN aplicativos a ON a.id = pr.aplicativo_id"
                        " WHERE a.client_id = :c"
                    ),
                    {"c": client_id},
                )
            ).first()

    async def extrato(self, tipo: str, aplicativo_id: uuid.UUID) -> list[Any]:
        async with self.dono.connect() as c:
            return list(
                (
                    await c.execute(
                        text("SELECT frase, pessoa_id FROM extrato WHERE tipo = :t AND aplicativo_id = :a ORDER BY id"),
                        {"t": tipo, "a": aplicativo_id},
                    )
                ).all()
            )


@pytest.fixture
async def telas(banco_migrado: Parametros) -> AsyncIterator[Telinhas]:
    p = Parametros(
        **(
            banco_migrado.model_dump()
            | {
                "APP_ENV": "teste",
                "PUBLIC_BASE_URL": PUBLICO,
                "MCP_RESOURCE_ID": None,
                "WATCH_PUBLIC_URL": None,
                "IDP_KIND": "oidc",
                "IDP_ISSUER_URL": "http://127.0.0.1:9/realms/mcp-claro",
                "IDP_JWKS_URL": "http://127.0.0.1:9/jwks",
            }
        )
    )
    dono = criar_engine(p, pool_size=2, max_overflow=2)
    entrada = engine_do_papel(p, "mcp_claro_entrada")
    http = httpx2.AsyncClient(follow_redirects=False, timeout=10, trust_env=False)
    cfg = ConfigTelas(segredo=SecretStr("segredo-das-telas"))
    chaves = ChavesPublicas(str(p.IDP_JWKS_URL), cache_segundos=60, http=http)
    t = Telas(p, cfg, entrada, _SemDespachante(), ProvedorLogin(p, cfg, http, chaves), criar_ambiente(cfg.fuso))
    app = Starlette(routes=[Mount("/telas", routes=rotas_admin_programas(t))])
    s = uuid.uuid4().hex[:8]
    try:
        async with dono.begin() as c:
            ids = {}
            for sujeito, nome in (("admin", "Carla Admin"), ("rui", f"Rui {s}"), ("sara", f"Sara {s}")):
                ids[sujeito] = (await repositorio.pessoa_no_login(c, p.IDP_ISSUER_URL, f"{sujeito}-{s}", nome)).id
            await c.execute(text("UPDATE pessoas SET papel = 'admin' WHERE id = :p"), {"p": ids["admin"]})
        async with servidor(app) as base:
            yield Telinhas(base, dono, entrada, http, ids["admin"], ids["rui"], ids["sara"], s)
    finally:
        await http.aclose()
        await dono.dispose()
        await entrada.dispose()


def _cadastro(t: Telinhas, **extra: str) -> dict[str, str]:
    return {
        "client_id": f"robo-tela-{t.sufixo}",
        "nome": f"Robo da tela {t.sufixo}",
        "familia": "programa",
        "finalidade": "Le os chamados abertos toda manha.",
        "fim": (date.today() + timedelta(days=200)).isoformat(),
    } | extra


async def test_tela_do_admin_recusa_programa_sem_responsavel_ou_com_responsavel_igual_ao_substituto(
    telas: Telinhas,
) -> None:
    nav, csrf = await telas.navegador(telas.admin_id)
    for faltando, mensagem in (
        ({}, SEM_RESPONSAVEL),
        ({"responsavel": str(telas.rui_id)}, SEM_RESPONSAVEL),
        ({"substituto": str(telas.sara_id)}, SEM_RESPONSAVEL),
        ({"responsavel": str(telas.rui_id), "substituto": str(telas.rui_id)}, MESMA_PESSOA),
    ):
        r = await nav.post("/telas/admin/programas", _cadastro(telas, **faltando), csrf=csrf)
        assert r.status_code == 400, faltando
        assert mensagem in r.text
    assert await telas.programa(f"robo-tela-{telas.sufixo}") is None
    async with telas.dono.connect() as c:
        assert (
            await c.execute(text("SELECT 1 FROM aplicativos WHERE client_id = :c"), {"c": f"robo-tela-{telas.sufixo}"})
        ).first() is None

    r = await nav.post(
        "/telas/admin/programas",
        _cadastro(telas, responsavel=str(telas.rui_id), substituto=str(telas.sara_id)),
        csrf=csrf,
    )
    assert r.status_code == 303
    linha = await telas.programa(f"robo-tela-{telas.sufixo}")
    assert (linha.responsavel_id, linha.substituto_id, linha.situacao) == (telas.rui_id, telas.sara_id, "ativo")
    cadastrado = await telas.extrato("admin_programa_cadastrado", linha.aplicativo_id)
    assert [x.frase for x in cadastrado] == [
        f"Programa Robo da tela {telas.sufixo} cadastrado por Carla Admin; responsavel Rui {telas.sufixo},"
        f" substituto Sara {telas.sufixo}."
    ]
    lista = await nav.get("/telas/admin/programas")
    assert lista.status_code == 200
    assert f"Rui {telas.sufixo}" in lista.text
    # Quem nao e ADMIN nao cadastra.
    rui, csrf_rui = await telas.navegador(telas.rui_id)
    r = await rui.post(
        "/telas/admin/programas",
        _cadastro(
            telas, client_id=f"outro-{telas.sufixo}", responsavel=str(telas.rui_id), substituto=str(telas.sara_id)
        ),
        csrf=csrf_rui,
    )
    assert r.status_code == 403


async def test_admin_pausa_retoma_recertifica_registra_chave_e_encerra(telas: Telinhas) -> None:
    nav, csrf = await telas.navegador(telas.admin_id)
    r = await nav.post(
        "/telas/admin/programas",
        _cadastro(telas, responsavel=str(telas.rui_id), substituto=str(telas.sara_id)),
        csrf=csrf,
    )
    assert r.status_code == 303
    antes = await telas.programa(f"robo-tela-{telas.sufixo}")
    base = f"/telas/admin/programas/{antes.aplicativo_id}"
    assert (await nav.get(base)).status_code == 200

    r = await nav.post(f"{base}/pausar", {"motivo": "x"}, csrf=csrf)
    assert r.status_code == 400  # motivo curto demais
    r = await nav.post(f"{base}/pausar", {"motivo": "Conferir um alerta da equipe"}, csrf=csrf)
    assert (r.status_code, r.headers["location"]) == (303, f"{base}?aviso=programa_pausado")
    assert (await telas.programa(f"robo-tela-{telas.sufixo}")).situacao == "pausado"

    r = await nav.post(f"{base}/retomar", {}, csrf=csrf)
    assert r.status_code == 303
    depois = await telas.programa(f"robo-tela-{telas.sufixo}")
    assert depois.situacao == "ativo"
    assert depois.revisado_em > antes.revisado_em  # retomar conta como revisao do ADMIN

    r = await nav.post(f"{base}/chave", {}, csrf=csrf)
    assert r.status_code == 303
    assert (await telas.programa(f"robo-tela-{telas.sufixo}")).chave_trocada_em > antes.chave_trocada_em
    r = await nav.post(f"{base}/recertificar", {}, csrf=csrf)
    assert r.status_code == 303

    r = await nav.post(f"{base}/encerrar", {"motivo": "Programa substituido por outro"}, csrf=csrf)
    assert r.status_code == 303
    assert (await telas.programa(f"robo-tela-{telas.sufixo}")).situacao == "encerrado"
    r = await nav.post(f"{base}/retomar", {}, csrf=csrf)
    assert r.status_code == 400  # encerrado nao volta
    r = await nav.post(f"{base}/pausar", {"motivo": "Depois de encerrado"}, csrf=csrf)
    assert r.headers["location"] == f"{base}?aviso=programa_nada_mudou"

    tipos = [
        ("admin_programa_pausado", "pausado por Carla Admin: Conferir um alerta da equipe."),
        ("admin_programa_retomado", "retomado por Carla Admin."),
        ("admin_programa_chave_trocada", "Carla Admin registrou a troca da chave do programa."),
        ("programa_recertificado", "Carla Admin confirmou que ele segue valendo."),
        ("admin_programa_encerrado", "encerrado por Carla Admin: Programa substituido por outro."),
    ]
    for tipo, final in tipos:
        linhas = await telas.extrato(tipo, antes.aplicativo_id)
        assert len(linhas) == 1, tipo
        assert linhas[0].frase.startswith(f"Programa Robo da tela {telas.sufixo}, responsavel Rui {telas.sufixo}")
        assert linhas[0].frase.endswith(final), linhas[0].frase
        assert linhas[0].pessoa_id == telas.admin_id


async def test_responsavel_recertifica_na_tela_dele_e_o_substituto_nao(telas: Telinhas) -> None:
    nav, csrf = await telas.navegador(telas.admin_id)
    r = await nav.post(
        "/telas/admin/programas",
        _cadastro(telas, responsavel=str(telas.rui_id), substituto=str(telas.sara_id)),
        csrf=csrf,
    )
    assert r.status_code == 303
    antes = await telas.programa(f"robo-tela-{telas.sufixo}")

    sara, csrf_sara = await telas.navegador(telas.sara_id)
    pagina = await sara.get("/telas/programas")
    assert f"Robo da tela {telas.sufixo}" in pagina.text
    assert "Confirmo que este programa segue valendo" not in pagina.text
    r = await sara.post(f"/telas/programas/{antes.aplicativo_id}/recertificar", {}, csrf=csrf_sara)
    assert r.status_code == 404
    assert (await telas.programa(f"robo-tela-{telas.sufixo}")).revisado_em == antes.revisado_em

    rui, csrf_rui = await telas.navegador(telas.rui_id)
    pagina = await rui.get("/telas/programas")
    assert "Confirmo que este programa segue valendo" in pagina.text
    r = await rui.post(f"/telas/programas/{antes.aplicativo_id}/recertificar", {}, csrf=csrf_rui)
    assert (r.status_code, r.headers["location"]) == (303, "/telas/programas?aviso=programa_recertificado")
    assert (await telas.programa(f"robo-tela-{telas.sufixo}")).revisado_em > antes.revisado_em
    linhas = await telas.extrato("programa_recertificado", antes.aplicativo_id)
    assert [(x.frase, x.pessoa_id) for x in linhas] == [
        (
            f"Programa Robo da tela {telas.sufixo}, responsavel Rui {telas.sufixo}: Rui {telas.sufixo} confirmou que"
            " ele segue valendo.",
            telas.rui_id,
        )
    ]
    # Sem o token CSRF da sessao, nada muda.
    r = await rui.post(f"/telas/programas/{antes.aplicativo_id}/recertificar", {}, csrf="outro")
    assert r.status_code == 403


async def test_admin_da_ordem_ao_programa_so_com_leitura_de_sistema_de_chave_propria(telas: Telinhas) -> None:
    nav, csrf = await telas.navegador(telas.admin_id)
    r = await nav.post(
        "/telas/admin/programas",
        _cadastro(telas, responsavel=str(telas.rui_id), substituto=str(telas.sara_id)),
        csrf=csrf,
    )
    assert r.status_code == 303
    programa = await telas.programa(f"robo-tela-{telas.sufixo}")
    s = telas.sufixo
    definicao = {"name": "ler", "description": "Le.", "inputSchema": {"type": "object", "properties": {}}}
    modelos = {}
    async with telas.dono.begin() as c:
        for tipo_chave in ("programa", "por_pessoa"):
            sistema_id = await repositorio.cadastrar_sistema(
                c, nome=f"ordem-{tipo_chave}-{s}", endereco="https://sistema.exemplo/mcp", tipo_chave=tipo_chave
            )
            await c.execute(
                text("UPDATE sistemas SET situacao = 'ativo', versao_mcp = '2026-07-28' WHERE id = :s"),
                {"s": sistema_id},
            )
            await repositorio.revisar_ferramenta(
                c,
                sistema_id=sistema_id,
                definicao_declarada=definicao,
                nome_vitrine=f"ler_{tipo_chave}_{s}",
                descricao="Leitura de teste.",
                classe="le",
                revisado_por="admin-teste",
            )
            modelos[tipo_chave] = await repositorio.criar_modelo_ordem(
                c,
                nome=f"ordem-{tipo_chave}-{s}",
                descricao="Modelo de teste",
                ferramentas=[(sistema_id, "ler")],
                validade_dias=30,
                criado_por="admin-teste",
            )
    base = f"/telas/admin/programas/{programa.aplicativo_id}"
    pagina = await nav.get(base)
    assert f"ordem-programa-{s}" in pagina.text
    assert f"ordem-por_pessoa-{s}" not in pagina.text
    conta = f"service-account-robo-tela-{s}"
    r = await nav.post(
        f"{base}/ordem", {"modelo": str(modelos["por_pessoa"]), "dias": "7", "sujeito": conta}, csrf=csrf
    )
    assert r.status_code == 400
    # O sujeito de uma pessoa (que responde por programa) nao vira conta de servico.
    r = await nav.post(
        f"{base}/ordem", {"modelo": str(modelos["programa"]), "dias": "7", "sujeito": f"rui-{s}"}, csrf=csrf
    )
    assert r.status_code == 400
    r = await nav.post(f"{base}/ordem", {"modelo": str(modelos["programa"]), "dias": "7", "sujeito": conta}, csrf=csrf)
    assert (r.status_code, r.headers["location"]) == (303, f"{base}?aviso=programa_ordem")
    async with telas.dono.connect() as c:
        ordem = (
            await c.execute(
                text(
                    "SELECT p.sujeito, p.nome, o.modelo_id FROM ordens_servico o JOIN pessoas p ON p.id = o.pessoa_id"
                    " WHERE o.aplicativo_id = :a"
                ),
                {"a": programa.aplicativo_id},
            )
        ).one()
    assert (ordem.sujeito, ordem.nome, ordem.modelo_id) == (conta, None, modelos["programa"])
    linhas = await telas.extrato("admin_programa_ordem", programa.aplicativo_id)
    assert len(linhas) == 1
    assert f"deu a ordem de servico ordem-programa-{s}" in linhas[0].frase
