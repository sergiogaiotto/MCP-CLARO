"""Conclusao da revisao do servidor de terceiros pelo despachante (G3a; desenho 3.4; critica A5).

O ADMIN conclui; o despachante le o servidor de novo com a chave de leitura das declaracoes guardada (a mesma das
rotinas), toma a trava do sistema, confere cada publicada (revisada ativa, mesma impressao da leitura fresca,
definicao declarada guardada) e grava ``revisoes_servidor`` e a revisao vigente numa transacao so. O despachante
roda com o usuario ``mcp_claro_despachante``: a guarda do banco so deixa ele tirar da quarentena.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from exemplos import mal_comportado
from mcp_claro.cadastro import quarentena, repositorio
from mcp_claro.cadastro.impressao import impressao, impressao_servidor
from mcp_claro.cofre import CofreIndisponivel
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.despachante import chaves
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.despachante.servico import (
    TETO_NOME_NO_EXTRATO,
    DeclaracoesIndisponiveis,
    DeclaracoesNaoConferidas,
    Despachante,
    RevisaoRecusada,
)
from mcp_claro.ligacao import sala_contrato
from mcp_claro.ligacao.sala_cliente import ClienteSala
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.versao import VERSAO_MCP
from tests import apoio_g3a as g
from tests import apoio_terceiros as t3
from tests.apoio import servidor
from tests.cifra_falsa import CifraFalsa

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

CHAVE = g.CHAVE_COMPARTILHADA


@dataclass
class Terceiro:
    dono: AsyncEngine
    desp_db: AsyncEngine
    cifra: CifraFalsa
    base: str
    app: g.ContaPosts
    sistema_id: uuid.UUID
    nome: str
    admin_id: uuid.UUID
    desp: Despachante

    async def modo(self, modo: str) -> None:
        async with httpx2.AsyncClient() as http:
            await http.post(f"{self.base}/modo", json={"modo": modo})

    async def revisar(self, *nomes: str) -> dict[str, uuid.UUID]:
        """Revisa (Fase 3) as ferramentas com o que o servidor declara agora, como a tela do ADMIN."""
        declaradas = {f["name"]: f for f in await self.desp.ler_declaracoes(self.sistema_id, None)}
        ids: dict[str, uuid.UUID] = {}
        async with self.dono.begin() as c:
            for nome in nomes:
                ids[nome] = await repositorio.revisar_ferramenta(
                    c,
                    sistema_id=self.sistema_id,
                    definicao_declarada=declaradas[nome],
                    nome_vitrine=f"{nome}_{uuid.uuid4().hex[:6]}",
                    descricao="Descricao do ADMIN.",
                    classe="le",
                    revisado_por="admin-teste",
                )
        return ids

    async def concluir(self, *publicar: str, fornecedor: str = "Fornecedor de fora") -> dict[str, Any]:
        return await self.desp.revisar_servidor(
            self.sistema_id, publicar=list(publicar), fornecedor=fornecedor, quem="admin-ana", pessoa_id=self.admin_id
        )

    async def sistema(self) -> Any:
        async with self.dono.connect() as c:
            return (
                await c.execute(
                    text("SELECT situacao, motivo, revisao_servidor_id FROM sistemas WHERE id = :s"),
                    {"s": self.sistema_id},
                )
            ).one()

    async def revisoes(self) -> list[Any]:
        async with self.dono.connect() as c:
            return list(
                (
                    await c.execute(
                        text("SELECT * FROM revisoes_servidor WHERE sistema_id = :s ORDER BY versao"),
                        {"s": self.sistema_id},
                    )
                ).all()
            )


@pytest.fixture
async def terceiro(banco_migrado: Parametros) -> AsyncIterator[Terceiro]:
    dono = criar_engine(banco_migrado, pool_size=3, max_overflow=3)
    desp_db = g.engine_do_papel(banco_migrado, "mcp_claro_despachante")
    cifra = CifraFalsa()
    app = g.ContaPosts(mal_comportado.criar(segunda_ferramenta=True))
    try:
        async with servidor(app) as base:
            sufixo = uuid.uuid4().hex[:8]
            async with dono.begin() as c:
                nome = f"g3a-revisao-{sufixo}"
                sistema_id = await t3.cadastrar_terceiro(c, nome=nome, endereco=f"{base}/mcp", tipo_chave="por_pessoa")
                await chaves.guardar(c, cifra, sistema_id=sistema_id, segredo=CHAVE, tipo="declaracoes")
                admin = await repositorio.pessoa_no_login(c, "https://idp.g3a.teste", f"admin-{sufixo}", "Admin")
                await c.execute(text("UPDATE pessoas SET papel = 'admin' WHERE id = :p"), {"p": admin.id})
            yield Terceiro(dono, desp_db, cifra, base, app, sistema_id, nome, admin.id, g.despachante(desp_db, cifra))
    finally:
        await dono.dispose()
        await desp_db.dispose()


def _medidas(nome: str, **rotulos: str) -> int:
    medidas, recusas = CONTADORES.retirar()
    CONTADORES.devolver(medidas, recusas)
    return sum(
        n
        for (_, k, r), n in medidas.items()
        if k == nome and all(json.loads(r).get(chave) == valor for chave, valor in rotulos.items())
    )


def _contados(desfecho: str) -> int:
    medidas, recusas = CONTADORES.retirar()
    CONTADORES.devolver(medidas, recusas)
    return sum(
        n
        for (_, nome, rotulos), n in medidas.items()
        if nome == "revisoes_servidor" and json.loads(rotulos).get("desfecho") == desfecho
    )


async def _vitrine(t: Terceiro) -> list[str]:
    """O que uma pessoa de teste com as duas ferramentas na ordem ve (com terceiros ligados para ela)."""
    sufixo = uuid.uuid4().hex[:8]
    async with t.dono.begin() as c:
        app_id = await repositorio.habilitar_aplicativo(c, client_id=f"g3a-v-{sufixo}", nome="App", familia="teste")
        pessoa = await repositorio.pessoa_no_login(c, "https://idp.g3a.teste", f"v-{sufixo}", "V")
        modelo = await repositorio.criar_modelo_ordem(
            c,
            nome=f"g3a-v-{sufixo}",
            descricao="Ordem",
            ferramentas=[(t.sistema_id, "consultar_pedido"), (t.sistema_id, "consultar_entrega")],
            validade_dias=30,
            criado_por="admin-teste",
        )
        await repositorio.conceder_ordem(
            c, pessoa_id=pessoa.id, aplicativo_id=app_id, modelo_id=modelo, dias=7, max_dias=30
        )
    async with t.dono.connect() as c:
        return [f.nome_original for f in await repositorio.vitrine(c, pessoa.id, app_id, VERSAO_MCP, terceiros=True)]


async def test_conclui_uma_de_duas_e_so_ela_aparece(terceiro: Terceiro) -> None:
    t = terceiro
    ids = await t.revisar("consultar_pedido", "consultar_entrega")
    assert await _vitrine(t) == []  # sem revisao do servidor, nada do terceiro aparece
    antes = _contados("concluida")
    t.app.zerar()
    feito = await t.concluir("consultar_pedido")
    # A conclusao leu o servidor de novo, com a chave de leitura das declaracoes guardada.
    assert [c["method"] for c in t.app.corpos] == ["tools/list"]
    assert t.app.cabecalhos[0]["authorization"] == f"Bearer {CHAVE}"
    assert feito["revisado"] is True
    assert (feito["versao"], feito["publicadas"], feito["fora"], feito["saiu_da_quarentena"]) == (1, 1, 1, False)
    (revisao,) = await t.revisoes()
    assert str(revisao.id) == feito["revisao_id"]
    assert list(revisao.publicadas) == [ids["consultar_pedido"]]
    assert revisao.fornecedor == "Fornecedor de fora"
    assert revisao.revisado_por == "admin-ana"
    assert revisao.pessoa_id == t.admin_id
    # A impressao do servidor sai da leitura fresca e bate com as declaradas guardadas.
    lidas = await t.desp.ler_declaracoes(t.sistema_id, None)
    assert revisao.declaradas == quarentena.declaradas(lidas)
    assert revisao.declaradas == sorted(
        ({"nome": f["name"], "impressao": impressao(f)} for f in lidas), key=lambda d: d["nome"]
    )
    assert revisao.impressao == impressao_servidor((d["nome"], d["impressao"]) for d in revisao.declaradas)
    sistema = await t.sistema()
    assert (sistema.situacao, sistema.revisao_servidor_id) == ("ativo", revisao.id)
    assert await _vitrine(t) == ["consultar_pedido"]
    linhas = await g.extrato(t.dono, t.sistema_id)
    feita = [ln for ln in linhas if ln.tipo == "admin_servidor_revisado"]
    assert len(feita) == 1
    assert feita[0].frase == (
        f"Servidor de terceiros {t.nome} revisado por admin-ana: 1 ferramentas na vitrine, 1 de fora."
    )
    assert feita[0].papel == "despachante"
    assert feita[0].pessoa_id == t.admin_id
    assert _contados("concluida") == antes + 1


async def test_servidor_que_mudou_depois_da_revisao_da_ferramenta_e_recusado_pela_leitura_nova(
    terceiro: Terceiro,
) -> None:
    t = terceiro
    await t.revisar("consultar_pedido", "consultar_entrega")
    await t.modo("muda_so_texto")  # muda so a descricao, a cada leitura; ferramentas_vistas fica com a antiga
    antes = _contados("recusada")
    with pytest.raises(RevisaoRecusada) as recusa:
        await t.concluir("consultar_pedido", "consultar_entrega")
    assert recusa.value.motivo == "ferramentas"
    assert recusa.value.ferramentas == (("consultar_pedido", "mudou"),)
    assert "A ferramenta consultar_pedido mudou desde a sua revisao" in recusa.value.mensagem
    assert await t.revisoes() == []
    assert (await t.sistema()).revisao_servidor_id is None
    assert await _vitrine(t) == []
    linhas = [ln for ln in await g.extrato(t.dono, t.sistema_id) if ln.tipo == "admin_servidor_revisao_recusada"]
    assert len(linhas) == 1
    assert linhas[0].frase == f"Revisao do servidor {t.nome} nao concluida: 1 ferramenta(s) fora de dia."
    assert linhas[0].dados["ferramentas"] == [{"ferramenta": "consultar_pedido", "problema": "mudou"}]
    assert _contados("recusada") == antes + 1


async def test_conclusao_em_quarentena_com_tudo_igual_volta_a_ativo(terceiro: Terceiro) -> None:
    t = terceiro
    await t.revisar("consultar_pedido", "consultar_entrega")
    primeira = await t.concluir("consultar_pedido", "consultar_entrega")
    async with t.dono.begin() as c:
        await t3.por_em_quarentena(c, t.sistema_id, "mudou, parou: teste")
    assert (await t.sistema()).revisao_servidor_id is None
    assert await _vitrine(t) == []
    segunda = await t.concluir("consultar_pedido", "consultar_entrega")
    assert segunda["saiu_da_quarentena"] is True
    assert segunda["versao"] == primeira["versao"] + 1
    sistema = await t.sistema()
    assert (sistema.situacao, sistema.motivo, str(sistema.revisao_servidor_id)) == (
        "ativo",
        None,
        segunda["revisao_id"],
    )
    assert sorted(await _vitrine(t)) == ["consultar_entrega", "consultar_pedido"]
    tipos = await g.tipos_no_extrato(t.dono, t.sistema_id)
    assert tipos.count("admin_servidor_revisado") == 2
    saiu = [ln for ln in await g.extrato(t.dono, t.sistema_id) if ln.tipo == "sistema_saiu_da_quarentena"]
    assert [ln.frase for ln in saiu] == [
        f"Servidor {t.nome} saiu da quarentena pela revisao de admin-ana; o que ele declara bate com o que foi"
        " revisado."
    ]


async def test_sem_a_chave_das_declaracoes_a_conclusao_e_recusada_sem_falar_com_o_servidor(
    terceiro: Terceiro,
) -> None:
    t = terceiro
    await t.revisar("consultar_pedido")
    async with t.dono.begin() as c:
        await c.execute(
            text("DELETE FROM chaves_cifradas WHERE sistema_id = :s AND tipo = 'declaracoes'"), {"s": t.sistema_id}
        )
    t.app.zerar()
    with pytest.raises(RevisaoRecusada) as recusa:
        await t.concluir("consultar_pedido")
    assert recusa.value.motivo == "sem_chave_declaracoes"
    assert recusa.value.mensagem == (
        "Guarde a chave de leitura das declaracoes deste servidor antes de concluir a revisao."
    )
    assert t.app.posts == 0  # nunca le sem Authorization
    assert await t.revisoes() == []
    tipos = await g.tipos_no_extrato(t.dono, t.sistema_id)
    assert tipos.count("admin_servidor_revisao_recusada") == 1


async def test_quarentena_que_entra_entre_a_leitura_e_a_trava_recusa_a_conclusao(terceiro: Terceiro) -> None:
    t = terceiro
    await t.revisar("consultar_pedido")
    primeira = await t.concluir("consultar_pedido")
    assert primeira["revisado"] is True

    class QuarentenaNoMeio:
        """Le o servidor e, antes de devolver, a quarentena entra (como o mudou-parou de outra copia faria)."""

        def __init__(self, sistema: Any) -> None:
            self.sistema = sistema

        async def listar_ferramentas(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            lidas = await g.LOCAL_DE_TESTE.listar_ferramentas(*a, **k)
            async with t.dono.begin() as c:
                await t3.por_em_quarentena(c, t.sistema_id, "mudou, parou: no meio da conclusao")
            return lidas

    t.desp._ligacao = QuarentenaNoMeio  # type: ignore[assignment]
    with pytest.raises(RevisaoRecusada) as recusa:
        await t.concluir("consultar_pedido")
    assert recusa.value.motivo == "situacao_mudou"
    sistema = await t.sistema()
    assert (sistema.situacao, sistema.revisao_servidor_id) == ("quarentena", None)
    assert len(await t.revisoes()) == 1  # nada novo gravado
    tipos = await g.tipos_no_extrato(t.dono, t.sistema_id)
    assert tipos.count("admin_servidor_revisao_recusada") == 1


async def test_revisada_sem_definicao_declarada_nao_e_publicada(terceiro: Terceiro) -> None:
    t = terceiro
    declarada = {f["name"]: f for f in await t.desp.ler_declaracoes(t.sistema_id, None)}["consultar_pedido"]
    async with t.dono.begin() as c:
        # Uma revisada de antes da Fase 3 (sem a definicao declarada guardada), com a impressao certa.
        await c.execute(
            text(
                "INSERT INTO ferramentas_revisadas (sistema_id, nome_original, versao, nome_vitrine, descricao,"
                " esquema_entrada, etiquetas, classe, risco, impressao, revisado_por)"
                " VALUES (:s, 'consultar_pedido', 1, :nv, 'Antiga.', CAST(:e AS jsonb), '{}'::jsonb, 'le', 'medio',"
                " :i, 'admin-teste')"
            ),
            {
                "s": t.sistema_id,
                "nv": f"antiga_{uuid.uuid4().hex[:6]}",
                "e": json.dumps(declarada["inputSchema"]),
                "i": impressao(declarada),
            },
        )
    with pytest.raises(RevisaoRecusada) as recusa:
        await t.concluir("consultar_pedido")
    assert recusa.value.ferramentas == (("consultar_pedido", "rever"),)
    assert recusa.value.mensagem == (
        "Revise a ferramenta consultar_pedido de novo antes de concluir a revisao do servidor."
    )
    assert await t.revisoes() == []


async def test_publicar_sem_revisao_ou_o_que_sumiu_e_recusado(terceiro: Terceiro) -> None:
    t = terceiro
    await t.revisar("consultar_pedido")
    with pytest.raises(RevisaoRecusada) as recusa:
        await t.concluir("consultar_pedido", "consultar_entrega", "nao_existe")
    assert recusa.value.ferramentas == (("consultar_entrega", "sem_revisao"), ("nao_existe", "sumiu"))
    assert await t.revisoes() == []
    with pytest.raises(RevisaoRecusada) as curto:
        await t.concluir("consultar_pedido", fornecedor="  x ")
    assert curto.value.motivo == "fornecedor"


async def test_sistema_proprio_nao_tem_revisao_do_servidor(terceiro: Terceiro) -> None:
    t = terceiro
    async with t.dono.begin() as c:
        proprio = await repositorio.cadastrar_sistema(
            c, nome=f"g3a-proprio-{uuid.uuid4().hex[:6]}", endereco=f"{t.base}/mcp", tipo_chave="compartilhada"
        )
    with pytest.raises(RevisaoRecusada) as recusa:
        await t.desp.revisar_servidor(proprio, publicar=[], fornecedor="Fornecedor", quem="admin", pessoa_id=None)
    assert recusa.value.motivo == "nao_e_terceiro"
    with pytest.raises(LookupError):
        await t.desp.revisar_servidor(uuid.uuid4(), publicar=[], fornecedor="Fornecedor", quem="admin")


async def test_rota_interna_da_revisao_do_servidor(terceiro: Terceiro, banco_migrado: Parametros) -> None:
    t = terceiro
    await t.revisar("consultar_pedido")
    p = banco_migrado.model_copy(update={"APP_ENV": "teste"})
    app = criar_app_despachante(p, t.desp_db, t.cifra, local_para_terceiro=g.LOCAL_DE_TESTE, liberar_locais=True)
    corpo = {
        "sistema_id": str(t.sistema_id),
        "publicar": ["consultar_pedido", "consultar_entrega"],
        "fornecedor": "Fornecedor de fora",
        "quem": "admin-ana",
        "pessoa_id": str(t.admin_id),
    }
    transporte = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transporte, base_url="http://despachante") as http:
        recusada = await http.post("/interno/revisao_servidor", json=corpo)
        feita = await http.post("/interno/revisao_servidor", json=corpo | {"publicar": ["consultar_pedido"]})
        desconhecido = await http.post("/interno/revisao_servidor", json=corpo | {"sistema_id": str(uuid.uuid4())})
        invalido = await http.post("/interno/revisao_servidor", json=corpo | {"publicar": "consultar_pedido"})
        sem_corpo = await http.post("/interno/revisao_servidor", content=b"nao e json")
    assert recusada.status_code == 200
    assert recusada.json() == {
        "revisado": False,
        "motivo": "ferramentas",
        "mensagem": "A ferramenta consultar_entrega nao tem revisao ativa.",
        "ferramentas": [{"ferramenta": "consultar_entrega", "problema": "sem_revisao"}],
    }
    assert feita.status_code == 200
    assert feita.json()["revisado"] is True
    assert feita.json()["publicadas"] == 1
    assert desconhecido.status_code == 404
    assert invalido.status_code == 400
    assert sem_corpo.status_code == 400


@pytest.mark.parametrize("situacao", ["pendente", "recusado", "desativado"])
async def test_so_conclui_servidor_ativo_ou_em_quarentena(terceiro: Terceiro, situacao: str) -> None:
    t = terceiro
    await t.revisar("consultar_pedido")
    async with t.dono.begin() as c:
        await c.execute(text("UPDATE sistemas SET situacao = :s WHERE id = :i"), {"s": situacao, "i": t.sistema_id})
    t.app.zerar()
    with pytest.raises(RevisaoRecusada) as recusa:
        await t.concluir("consultar_pedido")
    assert recusa.value.motivo == "situacao"
    assert t.app.posts == 0
    sistema = await t.sistema()
    assert (sistema.situacao, sistema.revisao_servidor_id) == (situacao, None)
    assert await t.revisoes() == []


async def test_so_conclui_servidor_que_fala_a_versao(terceiro: Terceiro) -> None:
    t = terceiro
    await t.revisar("consultar_pedido")
    async with t.dono.begin() as c:
        await c.execute(text("UPDATE sistemas SET versao_mcp = NULL WHERE id = :i"), {"i": t.sistema_id})
    t.app.zerar()
    with pytest.raises(RevisaoRecusada) as recusa:
        await t.concluir("consultar_pedido")
    assert recusa.value.motivo == "versao"
    assert t.app.posts == 0
    assert await t.revisoes() == []


async def test_publicadas_demais_e_recusado_na_funcao_e_na_rota(terceiro: Terceiro, banco_migrado: Parametros) -> None:
    t = terceiro
    nomes = [f"f{i}" for i in range(501)]
    t.app.zerar()
    with pytest.raises(RevisaoRecusada) as recusa:
        await t.concluir(*nomes)
    assert recusa.value.motivo == "publicadas_demais"
    assert t.app.posts == 0
    app = criar_app_despachante(
        banco_migrado.model_copy(update={"APP_ENV": "teste"}), t.desp_db, t.cifra, local_para_terceiro=g.LOCAL_DE_TESTE
    )
    corpo = {"sistema_id": str(t.sistema_id), "fornecedor": "Fornecedor de fora", "quem": "admin-ana"}
    transporte = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transporte, base_url="http://despachante") as http:
        respostas = [
            await http.post("/interno/revisao_servidor", json=corpo | {"publicar": publicar})
            for publicar in (nomes, ["consultar_pedido", ""], ["consultar_pedido", 1], [None])
        ]
        sem_quem = await http.post("/interno/revisao_servidor", json=corpo | {"publicar": [], "quem": 7})
    assert [r.status_code for r in respostas] == [400, 400, 400, 400]
    assert sem_quem.status_code == 400
    assert t.app.posts == 0
    assert await t.revisoes() == []


async def test_so_o_motivo_que_muda_no_meio_tambem_recusa(terceiro: Terceiro) -> None:
    t = terceiro
    await t.revisar("consultar_pedido")
    async with t.dono.begin() as c:
        await t3.por_em_quarentena(c, t.sistema_id, "mudou, parou: primeiro motivo")

    class MotivoNoMeio:
        """Le o servidor e, antes de devolver, o motivo da quarentena muda (outra quarentena por cima)."""

        def __init__(self, sistema: Any) -> None:
            self.sistema = sistema

        async def listar_ferramentas(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            lidas = await g.LOCAL_DE_TESTE.listar_ferramentas(*a, **k)
            async with t.dono.begin() as c:
                await c.execute(
                    text("UPDATE sistemas SET motivo = 'nao respondeu a conferencia' WHERE id = :i"),
                    {"i": t.sistema_id},
                )
            return lidas

    t.desp._ligacao = MotivoNoMeio  # type: ignore[assignment]
    with pytest.raises(RevisaoRecusada) as recusa:
        await t.concluir("consultar_pedido")
    assert recusa.value.motivo == "situacao_mudou"
    sistema = await t.sistema()
    assert (sistema.situacao, sistema.motivo) == ("quarentena", "nao respondeu a conferencia")
    assert await t.revisoes() == []


class _CifraQueNaoAbre(CifraFalsa):
    async def decifrar(self, cifrado: str, contexto: bytes) -> bytes:
        raise CofreIndisponivel("cofre fora")


async def test_cofre_que_nao_abre_a_chave_das_declaracoes(terceiro: Terceiro, banco_migrado: Parametros) -> None:
    t = terceiro
    await t.revisar("consultar_pedido")
    fora = g.despachante(t.desp_db, _CifraQueNaoAbre())
    t.app.zerar()
    with pytest.raises(RevisaoRecusada) as recusa:
        await fora.revisar_servidor(t.sistema_id, publicar=["consultar_pedido"], fornecedor="Fornecedor", quem="ana")
    assert recusa.value.motivo == "chave_indisponivel"
    with pytest.raises(DeclaracoesNaoConferidas) as nao_conferidas:
        await fora.ler_declaracoes(t.sistema_id, None)
    assert nao_conferidas.value.motivo == "chave_indisponivel"
    app = criar_app_despachante(
        banco_migrado.model_copy(update={"APP_ENV": "teste"}),
        t.desp_db,
        _CifraQueNaoAbre(),
        local_para_terceiro=g.LOCAL_DE_TESTE,
    )
    transporte = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transporte, base_url="http://despachante") as http:
        r = await http.post("/interno/declaracoes", json={"sistema_id": str(t.sistema_id)})
    # A falha e do MCP-CLARO, nao do servidor (critica A5): 503 com motivo proprio, que as rotinas nao contam.
    assert r.status_code == 503
    assert r.json()["motivo"] == "chave_indisponivel"
    assert t.app.posts == 0  # sem a chave aberta, nada vai ao servidor
    assert await t.revisoes() == []


async def test_nomes_do_fornecedor_entram_no_extrato_cortados_e_com_escondidos_marcados(terceiro: Terceiro) -> None:
    t = terceiro
    await t.revisar("consultar_pedido")
    escondido = "consultar\u202epedido"
    comprido = "x" * 300
    with pytest.raises(RevisaoRecusada) as recusa:
        await t.concluir("consultar_pedido", escondido, comprido)
    assert recusa.value.ferramentas == ((escondido, "sumiu"), (comprido, "sumiu"))
    (linha,) = [ln for ln in await g.extrato(t.dono, t.sistema_id) if ln.tipo == "admin_servidor_revisao_recusada"]
    assert linha.dados["ferramentas"] == [
        {"ferramenta": "consultar[U+202E]pedido", "problema": "sumiu"},
        {"ferramenta": "x" * TETO_NOME_NO_EXTRATO, "problema": "sumiu"},
    ]
    assert "\u202e" not in json.dumps(linha.dados, ensure_ascii=False) + linha.frase
    assert comprido not in linha.frase


@pytest.mark.parametrize(
    ("ferramenta", "valida"),
    [
        ({"name": "ler", "inputSchema": {"type": "object"}}, True),  # controle: a lista do duble esta no padrao
        ({"name": "  ", "inputSchema": {"type": "object"}}, False),
        ({"name": "ler", "description": "com \x00 nulo", "inputSchema": {"type": "object"}}, False),
        ({"name": "ler", "inputSchema": {"type": "object", "properties": {"a\x00": {"type": "string"}}}}, False),
    ],
    ids=["valida", "nome_em_branco", "nulo_na_descricao", "nulo_numa_chave"],
)
async def test_declaracao_que_o_banco_nao_guarda_e_recusada_sem_erro_interno(
    banco_migrado: Parametros, ferramenta: dict[str, Any], valida: bool
) -> None:
    def responder(pedido: dict[str, Any]) -> dict[str, Any]:
        assert pedido["method"] == "tools/list"
        lista = {"tools": [ferramenta], "ttlMs": 0, "cacheScope": "private", "resultType": "complete"}
        return {"result": lista}

    dono = criar_engine(banco_migrado, pool_size=2, max_overflow=0)
    desp_db = g.engine_do_papel(banco_migrado, "mcp_claro_despachante")
    cifra = CifraFalsa()
    try:
        async with servidor(g.sistema_cru(responder)) as base:
            async with dono.begin() as c:
                sistema_id = await t3.cadastrar_terceiro(
                    c, nome=f"g3a-decl-{uuid.uuid4().hex[:6]}", endereco=f"{base}/mcp"
                )
            if valida:
                assert [f["name"] for f in await g.despachante(desp_db, cifra).ler_declaracoes(sistema_id, "k")] == [
                    "ler"
                ]
                return
            with pytest.raises(DeclaracoesIndisponiveis):
                await g.despachante(desp_db, cifra).ler_declaracoes(sistema_id, "chave")
            app = criar_app_despachante(
                banco_migrado.model_copy(update={"APP_ENV": "teste"}),
                desp_db,
                cifra,
                local_para_terceiro=g.LOCAL_DE_TESTE,
            )
            transporte = httpx2.ASGITransport(app=app)
            async with httpx2.AsyncClient(transport=transporte, base_url="http://despachante") as http:
                r = await http.post("/interno/declaracoes", json={"sistema_id": str(sistema_id), "chave_sondagem": "k"})
        assert r.status_code == 502
        async with dono.connect() as c:
            vistas = (
                await c.execute(
                    text("SELECT count(*) FROM ferramentas_vistas WHERE sistema_id = :s"), {"s": sistema_id}
                )
            ).scalar_one()
        assert vistas == 0
    finally:
        await dono.dispose()
        await desp_db.dispose()


class _FalhaComStatus(sala_contrato.FalhaNaSala):
    """A listagem pela sala recusada com um 3xx (``FalhaNaSala`` com ``status_http``, do contrato)."""

    def __init__(self) -> None:
        super().__init__("recusado_http", "desvio", status_http=(307,))


class _ListagemQueFalha:
    def __init__(self, erro: Exception) -> None:
        self.erro = erro

    def __call__(self, _sistema: Any) -> _ListagemQueFalha:
        return self

    async def listar_ferramentas(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        raise self.erro


async def test_listagem_de_terceiro_sem_sala_ou_desviada_vai_ao_extrato_e_conta(terceiro: Terceiro) -> None:
    t = terceiro
    fora_antes = _medidas("sala_fora", onde="declaracoes")
    t.app.zerar()
    with pytest.raises(DeclaracoesNaoConferidas) as sem_sala:
        await g.despachante(t.desp_db, t.cifra, local=False).ler_declaracoes(t.sistema_id, None)
    assert sem_sala.value.motivo == "sala_fora"
    assert t.app.posts == 0
    assert await g.tipos_no_extrato(t.dono, t.sistema_id) == ["sala_fora"]
    assert _medidas("sala_fora", onde="declaracoes") == fora_antes + 1
    desvio_antes = _medidas("sala_desvio_recusado", onde="declaracoes")
    desviada = Despachante(t.desp_db, t.cifra, ambiente="teste", ligacao=_ListagemQueFalha(_FalhaComStatus()))
    with pytest.raises(DeclaracoesIndisponiveis):
        await desviada.ler_declaracoes(t.sistema_id, None)
    assert await g.tipos_no_extrato(t.dono, t.sistema_id) == ["sala_fora", "sala_desvio_recusado"]
    assert _medidas("sala_desvio_recusado", onde="declaracoes") == desvio_antes + 1
    # Uma falha sem status (o kit): sem linha da sala.
    kit = Despachante(t.desp_db, t.cifra, ambiente="teste", ligacao=_ListagemQueFalha(ExceptionGroup("k", [OSError()])))
    with pytest.raises(DeclaracoesIndisponiveis):
        await kit.ler_declaracoes(t.sistema_id, None)
    assert await g.tipos_no_extrato(t.dono, t.sistema_id) == ["sala_fora", "sala_desvio_recusado"]


# --- G3r (etapa 4r) ----------------------------------------------------------------------------------------------


async def test_desvio_na_listagem_de_terceiro_pelo_caminho_de_verdade_vai_ao_extrato_e_conta(
    terceiro: Terceiro,
) -> None:
    """O servidor responde 307 ao ``tools/list``: pelo cliente local (so em teste) e pela sala (o mesmo contrato que
    o filho da sala roda), a listagem e recusada com ``sala_desvio_recusado`` e o contador, sem seguir o desvio."""
    t = terceiro
    await t.modo("redireciona")
    antes = _medidas("sala_desvio_recusado", onde="declaracoes")
    with pytest.raises(DeclaracoesIndisponiveis) as local:
        await t.desp.ler_declaracoes(t.sistema_id, None)
    assert not isinstance(local.value, DeclaracoesNaoConferidas)  # foi o servidor: conta para as rotinas
    async with servidor(g.sala_em_processo()) as sala:
        pela_sala = g.despachante(t.desp_db, t.cifra, sala=ClienteSala(sala, tls=None), local=False)
        with pytest.raises(DeclaracoesIndisponiveis) as pela:
            await pela_sala.ler_declaracoes(t.sistema_id, None)
    assert not isinstance(pela.value, DeclaracoesNaoConferidas)
    assert await g.tipos_no_extrato(t.dono, t.sistema_id) == ["sala_desvio_recusado", "sala_desvio_recusado"]
    assert _medidas("sala_desvio_recusado", onde="declaracoes") == antes + 2
    assert t.app.app.state.contagem.chamadas.get("http307") == 2  # dois POSTs, nenhum seguido


async def test_listagem_que_pede_chave_nao_e_desvio(terceiro: Terceiro) -> None:
    t = terceiro
    await t.modo("recusa_chave")
    with pytest.raises(DeclaracoesIndisponiveis):
        await t.desp.ler_declaracoes(t.sistema_id, None)
    assert await g.tipos_no_extrato(t.dono, t.sistema_id) == []


@pytest.mark.parametrize("estranho", ["\x00", "\ud800"], ids=["nulo", "surrogate"])
@pytest.mark.parametrize("campo", ["fornecedor", "quem", "publicar"])
async def test_caractere_nulo_no_texto_do_admin_e_pedido_invalido_sem_erro_interno(
    terceiro: Terceiro, banco_migrado: Parametros, campo: str, estranho: str
) -> None:
    """Texto que o banco nao guarda: o caractere nulo (U+0000) e um pedaco de par UTF-16 sozinho (``\\ud800``, que o
    JSON aceita e o UTF-8 nao escreve). Os dois davam erro 500 depois de o servidor ja ter sido lido."""
    t = terceiro
    await t.revisar("consultar_pedido")
    p = banco_migrado.model_copy(update={"APP_ENV": "teste"})
    app = criar_app_despachante(p, t.desp_db, t.cifra, local_para_terceiro=g.LOCAL_DE_TESTE, liberar_locais=True)
    corpo: dict[str, Any] = {
        "sistema_id": str(t.sistema_id),
        "publicar": ["consultar_pedido"],
        "fornecedor": "Fornecedor de fora",
        "quem": "admin-ana",
    }
    corpo[campo] = ["consultar_pedido", f"con{estranho}sultar"] if campo == "publicar" else f"Forne{estranho}cedor"
    t.app.zerar()
    async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://despachante") as http:
        # json.dumps escreve o pedaco sozinho como o escape \ud800 (ASCII), como um cliente qualquer mandaria.
        r = await http.post(
            "/interno/revisao_servidor", content=json.dumps(corpo), headers={"content-type": "application/json"}
        )
    assert (r.status_code, r.json()["motivo"]) == (400, "pedido_invalido")
    assert t.app.posts == 0
    assert await t.revisoes() == []
    assert await g.tipos_no_extrato(t.dono, t.sistema_id) == []


async def test_versao_que_cai_entre_a_leitura_e_a_trava_recusa_sem_gravar(terceiro: Terceiro) -> None:
    """Em quarentena, a sondagem recusada por versao apaga ``versao_mcp`` e mantem a situacao e o motivo. Se isso
    acontece entre a leitura fresca e a trava, a conclusao e recusada (``situacao_mudou``), nunca erro interno."""
    t = terceiro
    await t.revisar("consultar_pedido")
    assert (await t.concluir("consultar_pedido"))["revisado"] is True
    async with t.dono.begin() as c:
        await t3.por_em_quarentena(c, t.sistema_id, "mudou, parou: teste")
    revisoes_antes = len(await t.revisoes())

    class VersaoCaiNoMeio:
        def __init__(self, sistema: Any) -> None:
            self.sistema = sistema

        async def listar_ferramentas(self, *a: Any, **k: Any) -> list[dict[str, Any]]:
            lidas = await g.LOCAL_DE_TESTE.listar_ferramentas(*a, **k)
            async with t.dono.begin() as c:
                await c.execute(text("UPDATE sistemas SET versao_mcp = NULL WHERE id = :s"), {"s": t.sistema_id})
            return lidas

    t.desp._ligacao = VersaoCaiNoMeio  # type: ignore[assignment]
    with pytest.raises(RevisaoRecusada) as recusa:
        await t.concluir("consultar_pedido")
    assert recusa.value.motivo == "situacao_mudou"
    sistema = await t.sistema()
    assert (sistema.situacao, sistema.motivo, sistema.revisao_servidor_id) == (
        "quarentena",
        "mudou, parou: teste",
        None,
    )
    assert len(await t.revisoes()) == revisoes_antes
    assert (await g.tipos_no_extrato(t.dono, t.sistema_id))[-1] == "admin_servidor_revisao_recusada"
