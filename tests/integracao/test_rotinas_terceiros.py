"""Mudou-parou de servidores de terceiros (G5, Fase 3): qualquer mudanca leva o servidor inteiro a quarentena.

Sobe de verdade: PostgreSQL, provedor de login falso, o duble ``exemplos/mal_comportado`` com duas ferramentas
(que exige chave: ``EXEMPLO_CHAVES`` fica com as chaves padrao), o despachante (usuario ``mcp_claro_despachante``,
com o cliente local de terceiro injetado, so valido em teste), a entrada (usuario ``mcp_claro_entrada``) e as
rotinas (usuario ``mcp_claro_rotinas``), que pedem as declaracoes ao despachante pela porta interna, como em
producao. O despachante le com a chave de leitura das declaracoes guardada pelo ADMIN (critica A5).

O banco da sessao tem servidores de outros testes: cada teste so confere os proprios (``FonteSo``); os outros
ficam como conferencia nao feita, sem efeito neles.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from exemplos import mal_comportado
from mcp_claro.cadastro import repositorio
from mcp_claro.cadastro.quarentena import MOTIVO_QUARENTENA
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.despachante import chaves
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.entrada.app import criar_app_entrada
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.rotinas import mudou_parou
from mcp_claro.rotinas.despachante import ClienteDespachante
from mcp_claro.rotinas.mudou_parou import (
    RODADAS_SEM_RESPOSTA,
    ConferenciaNaoFeita,
    FonteDeclaracoes,
    ResultadoMudouParou,
    conferir_sistemas,
)
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.quarentena import MOTIVO_LIMITE, MOTIVO_SEM_RESPOSTA
from mcp_claro.versao import PREFIXO_META
from tests import apoio_g3a as g
from tests import apoio_terceiros as t3
from tests.apoio import cliente_mcp, porta_livre, servidor
from tests.cifra_falsa import CifraFalsa
from tests.idp_falso import IdpFalso
from tests.integracao.test_entrada_terceiros_apoio import subir_mundo

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

ADMIN = "admin-g5@exemplo.teste"
CHAVE = "chave-compartilhada"  # nas chaves padrao do duble, vale para todos
PUBLICO = "https://mcp-claro.teste"
RECURSO = f"{PUBLICO}/mcp"
DUAS = ("consultar_entrega", "consultar_pedido")


@dataclass
class FonteSo:
    """So os sistemas deste teste; os outros do banco da sessao ficam como conferencia nao feita (sem efeito)."""

    fonte: FonteDeclaracoes
    ids: set[uuid.UUID]
    chamadas: list[uuid.UUID] = field(default_factory=list)

    async def declaracoes(self, sistema_id: uuid.UUID) -> list[dict[str, Any]]:
        if sistema_id not in self.ids:
            raise ConferenciaNaoFeita("fora deste teste")
        self.chamadas.append(sistema_id)
        return await self.fonte.declaracoes(sistema_id)


@dataclass
class FonteFalsa:
    """``FonteDeclaracoes`` sem despachante: devolve o que o teste deu para cada sistema (ou levanta)."""

    respostas: dict[uuid.UUID, list[dict[str, Any]] | BaseException | Callable[[], Awaitable[list[dict[str, Any]]]]]
    chamadas: list[uuid.UUID] = field(default_factory=list)

    async def declaracoes(self, sistema_id: uuid.UUID) -> list[dict[str, Any]]:
        r = self.respostas.get(sistema_id)
        if r is None:
            raise ConferenciaNaoFeita("fora deste teste")
        self.chamadas.append(sistema_id)
        if isinstance(r, BaseException):
            raise r
        if callable(r):
            return await r()
        return [dict(d) for d in r]


@dataclass
class Contados:
    """Espia dos contadores do mudou-parou (``mudou_parou.CONTADORES``): guarda cada soma e passa adiante. Os papeis
    que sobem nos testes descarregam ``CONTADORES`` no banco de tempos em tempos; a espia nao depende disso."""

    somas: list[tuple[str, int, dict[str, str]]] = field(default_factory=list)

    def somar(self, nome: str, valor: int = 1, **rotulos: str) -> None:
        self.somas.append((nome, valor, dict(rotulos)))
        CONTADORES.somar(nome, valor, **rotulos)

    def de(self, nome: str, **rotulos: str) -> int:
        return sum(v for n, v, r in self.somas if n == nome and all(r.get(k) == x for k, x in rotulos.items()))


@pytest.fixture
def contados(monkeypatch: pytest.MonkeyPatch) -> Contados:
    espia = Contados()
    monkeypatch.setattr(mudou_parou, "CONTADORES", espia)
    return espia


@dataclass
class Cena:
    p: Parametros
    idp: IdpFalso
    dono: AsyncEngine
    rotinas_db: AsyncEngine
    entrada_url: str
    desp_url: str
    base: str
    duble: g.ContaPosts
    fonte: FonteSo
    sistema_id: uuid.UUID
    nome: str
    pessoa_id: uuid.UUID
    aplicativo_id: uuid.UUID
    client_id: str
    vitrine: dict[str, str]
    sufixo: str

    def cracha(self) -> str:
        return self.idp.cracha(audiencia=RECURSO, sujeito=f"g5-ana-{self.sufixo}", client_id=self.client_id)

    async def modo(self, modo: str) -> None:
        async with httpx2.AsyncClient() as http:
            (await http.post(f"{self.base}/modo", json={"modo": modo})).raise_for_status()

    async def interno(self, rota: str, corpo: Mapping[str, Any]) -> httpx2.Response:
        async with httpx2.AsyncClient(timeout=30) as http:
            return await http.post(f"{self.desp_url}{rota}", json=dict(corpo))

    async def guardar_chave_declaracoes(self) -> None:
        r = await self.interno(
            "/interno/chaves",
            {"sistema_id": str(self.sistema_id), "segredo": CHAVE, "tipo": "declaracoes", "quem": "admin-teste"},
        )
        assert r.json() == {"guardada": True}, r.text

    async def concluir(self, *publicar: str) -> dict[str, Any]:
        r = await self.interno(
            "/interno/revisao_servidor",
            {
                "sistema_id": str(self.sistema_id),
                "publicar": list(publicar),
                "fornecedor": "Fornecedor de fora",
                "quem": "admin-teste",
            },
        )
        assert r.status_code == 200, r.text
        corpo: dict[str, Any] = r.json()
        return corpo

    async def conferir(self, fonte: FonteDeclaracoes | None = None) -> ResultadoMudouParou:
        return await conferir_sistemas(self.rotinas_db, fonte or self.fonte, [ADMIN])

    async def listar(self) -> list[str]:
        async with cliente_mcp(f"{self.entrada_url}/mcp", self.cracha()) as c:
            return sorted(t.name for t in (await c.list_tools()).tools)

    async def chamar(self, nome_original: str) -> Any:
        """Uma leitura pelo aplicativo. A recusa volta como ``MCPError`` (fora do grupo de tarefas do cliente)."""
        recusa: MCPError | None = None
        async with cliente_mcp(f"{self.entrada_url}/mcp", self.cracha()) as c:
            try:
                return await c.call_tool(self.vitrine[nome_original], {"pedido": "P-1"})
            except MCPError as erro:
                recusa = erro
        return recusa

    async def sistema(self) -> Any:
        async with self.dono.connect() as c:
            return (
                await c.execute(
                    text(
                        "SELECT situacao, motivo, revisao_servidor_id, conferencias_sem_resposta FROM sistemas"
                        " WHERE id = :s"
                    ),
                    {"s": self.sistema_id},
                )
            ).one()

    async def extrato(self, tipo: str) -> list[Any]:
        async with self.dono.connect() as c:
            return list(
                (
                    await c.execute(
                        text(
                            "SELECT frase, dados, papel, pessoa_id FROM extrato WHERE sistema_id = :s AND tipo = :t"
                            " ORDER BY id"
                        ),
                        {"s": self.sistema_id, "t": tipo},
                    )
                ).all()
            )

    async def avisos(self, evento: str) -> list[Any]:
        return await avisos_do_sistema(self.dono, self.sistema_id, evento)

    def chamadas_do_duble(self, nome: str) -> int:
        return int(self.duble.app.state.contagem.chamadas.get(nome, 0))


async def avisos_do_sistema(dono: AsyncEngine, sistema_id: uuid.UUID, evento: str) -> list[Any]:
    async with dono.connect() as c:
        return list(
            (
                await c.execute(
                    text(
                        "SELECT canal, destino, assunto, corpo, chave_unica FROM avisos"
                        " WHERE evento = :e AND corpo ->> 'sistema_id' = :s ORDER BY id"
                    ),
                    {"e": evento, "s": str(sistema_id)},
                )
            ).all()
        )


def _parametros(banco: Parametros, idp: IdpFalso, porta_idp: int, porta_desp: int) -> Parametros:
    return Parametros(
        **(
            banco.model_dump()
            | {
                "APP_ENV": "teste",
                "PUBLIC_BASE_URL": PUBLICO,
                "MCP_RESOURCE_ID": None,
                "WATCH_PUBLIC_URL": None,
                "ALLOWED_HOSTS": ["127.0.0.1:*"],
                "IDP_KIND": "oidc",
                "IDP_ISSUER_URL": idp.emissor,
                "IDP_JWKS_URL": f"http://127.0.0.1:{porta_idp}/jwks",
                "DISPATCHER_INTERNAL_URL": f"http://127.0.0.1:{porta_desp}",
                "FEATURE_PHASE1_READ": True,
                "RESPONSE_MAX_WAIT_SECONDS": 5,
            }
        )
    )


@contextlib.asynccontextmanager
async def montar(banco: Parametros, monkeypatch: pytest.MonkeyPatch, *, concluir: bool = True) -> AsyncIterator[Cena]:
    """Um terceiro com as duas ferramentas revisadas (Fase 3) numa ordem de uma pessoa de teste. Com ``concluir``,
    o ADMIN guarda a chave de leitura das declaracoes e conclui a revisao do servidor com as duas publicadas."""
    monkeypatch.delenv("EXEMPLO_CHAVES", raising=False)  # o duble exige chave
    duble = g.ContaPosts(mal_comportado.criar(segunda_ferramenta=True))
    porta_idp, porta_desp = porta_livre(), porta_livre()
    idp = IdpFalso(emissor=f"http://127.0.0.1:{porta_idp}/realms/mcp-claro")
    p = _parametros(banco, idp, porta_idp, porta_desp)
    dono = criar_engine(p, pool_size=3, max_overflow=3)
    desp_db = g.engine_do_papel(p, "mcp_claro_despachante")
    entrada_db = g.engine_do_papel(p, "mcp_claro_entrada")
    rotinas_db = g.engine_do_papel(p, "mcp_claro_rotinas")
    cifra = CifraFalsa()
    app_desp = criar_app_despachante(p, desp_db, cifra, local_para_terceiro=g.LOCAL_DE_TESTE, liberar_locais=True)
    rotinas = ClienteDespachante(p)
    try:
        async with (
            servidor(idp.app(), porta_idp),
            servidor(duble) as base,
            servidor(app_desp, porta_desp) as desp_url,
            servidor(criar_app_entrada(p, entrada_db)) as entrada_url,
        ):
            sufixo = uuid.uuid4().hex[:8]
            nome = f"g5-terceiro-{sufixo}"
            async with dono.begin() as c:
                sistema_id = await t3.cadastrar_terceiro(c, nome=nome, endereco=f"{base}/mcp", tipo_chave="por_pessoa")
                client_id = f"g5-app-{sufixo}"
                app_id = await repositorio.habilitar_aplicativo(c, client_id=client_id, nome="App G5", familia="g5")
                pessoa = await repositorio.pessoa_no_login(c, idp.emissor, f"g5-ana-{sufixo}", "Ana")
                await c.execute(text("UPDATE pessoas SET teste = true WHERE id = :p"), {"p": pessoa.id})
            async with dono.begin() as c:
                # A chave da pessoa nasce da autorizacao dela no sistema; aqui, direto, como o despachante faria.
                await chaves.guardar(c, cifra, sistema_id=sistema_id, pessoa_id=pessoa.id, segredo="chave-ana")
            cena = Cena(
                p,
                idp,
                dono,
                rotinas_db,
                entrada_url,
                desp_url,
                base,
                duble,
                FonteSo(rotinas, {sistema_id}),
                sistema_id,
                nome,
                pessoa.id,
                app_id,
                client_id,
                {},
                sufixo,
            )
            # O ADMIN le as ferramentas com a chave de teste digitada na tela e revisa as duas (Fase 3).
            lidas = await cena.interno("/interno/declaracoes", {"sistema_id": str(sistema_id), "chave_sondagem": CHAVE})
            declaradas = {f["name"]: f for f in lidas.json()["ferramentas"]}
            assert sorted(declaradas) == list(DUAS)
            async with dono.begin() as c:
                for original, d in declaradas.items():
                    cena.vitrine[original] = f"{original}_{sufixo}"
                    await repositorio.revisar_ferramenta(
                        c,
                        sistema_id=sistema_id,
                        definicao_declarada=d,
                        nome_vitrine=cena.vitrine[original],
                        descricao="Descricao escrita pelo ADMIN.",
                        classe="le",
                        revisado_por="admin-teste",
                    )
                modelo = await repositorio.criar_modelo_ordem(
                    c,
                    nome=f"g5-{sufixo}",
                    descricao="Ordem de teste do G5",
                    ferramentas=[(sistema_id, n) for n in DUAS],
                    validade_dias=30,
                    criado_por="admin-teste",
                )
                await repositorio.conceder_ordem(
                    c, pessoa_id=pessoa.id, aplicativo_id=app_id, modelo_id=modelo, dias=7, max_dias=30
                )
            if concluir:
                await cena.guardar_chave_declaracoes()
                feito = await cena.concluir(*DUAS)
                assert feito["revisado"] is True, feito
                assert feito["publicadas"] == 2
            yield cena
    finally:
        await rotinas.fechar()
        for engine in (dono, desp_db, entrada_db, rotinas_db):
            await engine.dispose()


@pytest.fixture
async def cena(banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Cena]:
    async with montar(banco_migrado, monkeypatch) as c:
        yield c


def _sem_texto_do_fornecedor(corpo: Any) -> None:
    """O aviso ao ADMIN leva nome do cadastro e numeros; nenhum nome de ferramenta (texto do fornecedor)."""
    bruto = json.dumps(corpo, ensure_ascii=False)
    for nome in (*DUAS, "Consulta a situacao"):
        assert nome not in bruto, nome


# --- Prova de saida ---------------------------------------------------------------------------------------------


async def test_mudanca_so_de_texto_em_terceiro_leva_o_servidor_inteiro_a_quarentena(
    cena: Cena, contados: Contados
) -> None:
    """Prova de saida da Fase 3 (plano): terceiro com duas ferramentas, revisado e publicado; muda so o texto de
    uma; o servidor inteiro sai de cena, pela entrada de verdade, e o ADMIN recebe um aviso."""
    nomes = sorted(cena.vitrine.values())
    assert await cena.listar() == nomes
    lida = await cena.chamar("consultar_pedido")
    assert not isinstance(lida, MCPError), lida
    assert any("Pedido em transito." in getattr(b, "text", "") for b in lida.content)
    chamadas_antes = cena.chamadas_do_duble("consultar_pedido")

    # Sem mudanca: nada. As rotinas leem pelo despachante, com a chave das declaracoes guardada (o duble exige).
    cena.duble.zerar()
    resultado = await cena.conferir()
    assert cena.nome not in resultado.quarentenas
    assert cena.nome not in resultado.sem_resposta
    assert cena.fonte.chamadas == [cena.sistema_id]
    assert cena.duble.posts >= 1
    assert {h.get("authorization") for h in cena.duble.cabecalhos} == {f"Bearer {CHAVE}"}
    antes = await cena.sistema()
    assert (antes.situacao, antes.conferencias_sem_resposta) == ("ativo", 0)
    assert contados.de("mudou_parou_quarentenas") == 0

    # So o texto de uma das duas muda: mudou, parou, o servidor inteiro.
    await cena.modo("muda_so_texto")
    resultado = await cena.conferir()
    assert resultado.quarentenas == [cena.nome]
    assert contados.de("mudou_parou_quarentenas", motivo="mudou") == 1
    assert contados.de("mudou_parou_quarentenas") == 1
    assert contados.de("acoes_paradas") == 0  # nada esperava na fila
    depois = await cena.sistema()
    assert depois.situacao == "quarentena"
    assert depois.motivo == MOTIVO_QUARENTENA
    assert depois.revisao_servidor_id is None

    # Pela entrada: nenhuma das duas na lista, e a leitura e recusada sem chegar ao servidor.
    assert await cena.listar() == []
    recusa = await cena.chamar("consultar_entrega")
    assert isinstance(recusa, MCPError), recusa
    assert recusa.error.code == -32602
    assert "nao esta na sua vitrine" in recusa.error.message
    assert cena.chamadas_do_duble("consultar_pedido") == chamadas_antes
    assert cena.chamadas_do_duble("consultar_entrega") == 0

    (linha,) = await cena.extrato("sistema_em_quarentena")
    assert linha.papel == "rotinas"
    assert cena.nome in linha.frase
    assert [(f["ferramenta"], f["mudanca"]) for f in linha.dados["ferramentas"]] == [("consultar_pedido", "mudou")]
    assert linha.dados["revisao"] == str(antes.revisao_servidor_id)
    (aviso,) = await cena.avisos("sistema_em_quarentena")
    assert (aviso.canal, aviso.destino) == ("email", ADMIN)
    assert "mudou, parou" in aviso.assunto
    assert aviso.corpo["sistema"] == cena.nome
    assert aviso.corpo["quantas"] == 1
    assert aviso.corpo["mudancas"] == {"mudou": 1, "nova": 0, "sumiu": 0}
    assert "limites das pessoas com ele foram suspensos" in aviso.corpo["texto"]
    _sem_texto_do_fornecedor(aviso.corpo)

    # Segunda rodada: nada se repete (o servidor nao esta mais ativo).
    resultado = await cena.conferir()
    assert cena.nome not in resultado.quarentenas
    assert len(await cena.extrato("sistema_em_quarentena")) == 1
    assert len(await cena.avisos("sistema_em_quarentena")) == 1
    assert contados.de("mudou_parou_quarentenas") == 1


async def test_rotinas_sem_a_chave_das_declaracoes_nao_leem_e_a_conclusao_e_recusada(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critica A5 com um duble que exige chave: sem a chave das declaracoes guardada, o despachante nao le o
    servidor (401) e a conclusao da revisao e recusada; guardada a chave, conclui e as rotinas conferem com ela."""
    async with montar(banco_migrado, monkeypatch, concluir=False) as cena:
        cena.duble.zerar()
        sem_chave = await cena.interno("/interno/declaracoes", {"sistema_id": str(cena.sistema_id)})
        assert sem_chave.status_code == 502
        assert {h.get("authorization") for h in cena.duble.cabecalhos} == {None}

        recusada = await cena.concluir(*DUAS)
        assert recusada["revisado"] is False
        assert recusada["motivo"] == "sem_chave_declaracoes"
        assert recusada["mensagem"] == (
            "Guarde a chave de leitura das declaracoes deste servidor antes de concluir a revisao."
        )
        # Sem revisao do servidor, as rotinas nem conferem (nada dele aparece para as pessoas).
        resultado = await cena.conferir()
        assert cena.fonte.chamadas == []
        assert cena.nome not in resultado.sem_resposta
        assert (await cena.sistema()).situacao == "ativo"

        await cena.guardar_chave_declaracoes()
        assert (await cena.concluir(*DUAS))["revisado"] is True
        cena.duble.zerar()
        resultado = await cena.conferir()
        assert cena.nome not in resultado.sem_resposta
        assert {h.get("authorization") for h in cena.duble.cabecalhos} == {f"Bearer {CHAVE}"}
        assert (await cena.sistema()).conferencias_sem_resposta == 0


# --- Nao respondeu a conferencia (critica A5) -------------------------------------------------------------------


async def test_terceiro_que_nao_responde_em_duas_rodadas_seguidas_sai_da_vitrine(
    cena: Cena, contados: Contados
) -> None:
    nomes = sorted(cena.vitrine.values())
    await cena.modo("recusa_chave")  # 401 a tudo: nao entrega as declaracoes

    resultado = await cena.conferir()
    assert cena.nome in resultado.sem_resposta
    assert cena.nome not in resultado.quarentenas
    primeira = await cena.sistema()
    assert (primeira.situacao, primeira.conferencias_sem_resposta) == ("ativo", 1)
    assert contados.de("mudou_parou_quarentenas") == 0
    await cena.modo("normal")
    assert await cena.listar() == nomes  # uma rodada so nao tira de cena
    await cena.modo("recusa_chave")

    resultado = await cena.conferir()
    assert resultado.quarentenas == [cena.nome]
    assert contados.de("mudou_parou_quarentenas", motivo="sem_resposta") == 1
    assert contados.de("mudou_parou_quarentenas") == 1
    depois = await cena.sistema()
    assert (depois.situacao, depois.motivo, depois.revisao_servidor_id) == ("quarentena", MOTIVO_SEM_RESPOSTA, None)
    assert depois.conferencias_sem_resposta == 0  # a guarda zera quando a revisao vigente cai

    await cena.modo("normal")
    assert await cena.listar() == []
    (linha,) = await cena.extrato("sistema_em_quarentena_sem_resposta")
    assert "2 rodadas seguidas" in linha.frase
    assert linha.papel == "rotinas"
    assert await cena.extrato("sistema_em_quarentena") == []
    (aviso,) = await cena.avisos("sistema_em_quarentena_sem_resposta")
    assert (aviso.canal, aviso.destino, aviso.corpo["rodadas"]) == ("email", ADMIN, 2)
    _sem_texto_do_fornecedor(aviso.corpo)

    # Fora de 'ativo', o servidor nao e mais conferido: nada se repete.
    await cena.conferir()
    assert len(await cena.extrato("sistema_em_quarentena_sem_resposta")) == 1
    assert contados.de("mudou_parou_quarentenas") == 1


async def test_resposta_no_meio_zera_as_rodadas_sem_resposta(cena: Cena) -> None:
    await cena.modo("erro500")
    await cena.conferir()
    assert (await cena.sistema()).conferencias_sem_resposta == 1
    await cena.modo("normal")
    await cena.conferir()
    assert (await cena.sistema()).conferencias_sem_resposta == 0
    await cena.modo("erro500")
    resultado = await cena.conferir()
    assert cena.nome not in resultado.quarentenas
    situacao = await cena.sistema()
    assert (situacao.situacao, situacao.conferencias_sem_resposta) == ("ativo", 1)


async def test_conferencia_nao_feita_pelo_mcp_claro_nao_conta_rodada(cena: Cena) -> None:
    """Despachante fora do ar, sala fora ou chave que o cofre nao abriu: a culpa nao e do servidor."""
    fonte = FonteFalsa({cena.sistema_id: ConferenciaNaoFeita("o despachante nao atendeu")})
    for _ in range(3):
        resultado = await cena.conferir(fonte)
        assert cena.nome in resultado.sem_resposta
    situacao = await cena.sistema()
    assert (situacao.situacao, situacao.conferencias_sem_resposta) == ("ativo", 0)
    assert await cena.extrato("sistema_em_quarentena_sem_resposta") == []

    # O despachante fora do ar, de verdade: a conexao e recusada, nada conta.
    p = cena.p.model_copy(update={"DISPATCHER_INTERNAL_URL": f"http://127.0.0.1:{porta_livre()}"})
    fora = ClienteDespachante(p)
    try:
        for _ in range(2):
            await cena.conferir(FonteSo(fora, {cena.sistema_id}))
    finally:
        await fora.fechar()
    situacao = await cena.sistema()
    assert (situacao.situacao, situacao.conferencias_sem_resposta) == ("ativo", 0)


# --- Duas copias das rotinas ------------------------------------------------------------------------------------


async def test_duas_copias_das_rotinas_uma_linha_um_aviso_um_update(
    cena: Cena, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with cena.dono.begin() as c:
        limite = (
            await c.execute(
                text(
                    "INSERT INTO limites (pessoa_id, aplicativo_id, sistema_id, nome_original, destinatarios, por_dia,"
                    " valido_ate, com_passkey) VALUES (:p, :a, :s, 'consultar_pedido', '[]', 3,"
                    " now() + interval '5 days', true) RETURNING id"
                ),
                {"p": cena.pessoa_id, "a": cena.aplicativo_id, "s": cena.sistema_id},
            )
        ).scalar_one()
    await cena.modo("muda_so_texto")
    juntas = asyncio.Barrier(2)

    @dataclass
    class FonteJunta:
        async def declaracoes(self, sistema_id: uuid.UUID) -> list[dict[str, Any]]:
            lidas = await cena.fonte.declaracoes(sistema_id)
            if sistema_id == cena.sistema_id:
                await juntas.wait()  # as duas copias comparam ao mesmo tempo
            return lidas

    feitas: list[object] = []
    original = mudou_parou.por_em_quarentena

    async def contar(*args: Any, **kwargs: Any) -> Any:
        feita = await original(*args, **kwargs)
        if kwargs.get("sistema_id") == cena.sistema_id:
            feitas.append(feita)
        return feita

    monkeypatch.setattr(mudou_parou, "por_em_quarentena", contar)
    outra = g.engine_do_papel(cena.p, "mcp_claro_rotinas")
    try:
        a, b = await asyncio.gather(
            conferir_sistemas(cena.rotinas_db, FonteJunta(), [ADMIN]),
            conferir_sistemas(outra, FonteJunta(), [ADMIN]),
        )
    finally:
        await outra.dispose()
    assert sorted(a.quarentenas + b.quarentenas) == [cena.nome]
    assert len(feitas) == 2
    assert sum(1 for f in feitas if f is not None) == 1  # um UPDATE so passou; o outro achou o servidor fora
    assert len(await cena.extrato("sistema_em_quarentena")) == 1
    assert len(await cena.avisos("sistema_em_quarentena")) == 1
    async with cena.dono.connect() as c:
        situacao, motivo = (
            await c.execute(text("SELECT situacao, motivo FROM limites WHERE id = :i"), {"i": limite})
        ).one()
        suspensos = (
            await c.execute(
                text("SELECT count(*) FROM extrato WHERE tipo = 'limite_suspenso' AND dados ->> 'limite_id' = :i"),
                {"i": str(limite)},
            )
        ).scalar_one()
    assert (situacao, motivo, suspensos) == ("suspenso", MOTIVO_LIMITE, 1)


# --- Fonte falsa: sumida, nova, igual, sem revisao e a corrida com a conclusao (B1) ----------------------------


@dataclass
class Revisado:
    sistema_id: uuid.UUID
    nome: str
    revisao_id: uuid.UUID
    definicoes: list[dict[str, Any]]


async def _terceiro_revisado(dono: AsyncEngine, *, revisar_servidor: bool = True) -> Revisado:
    """Terceiro ativo com duas ferramentas revisadas e, com ``revisar_servidor``, a revisao do servidor vigente."""
    sufixo = uuid.uuid4().hex[:8]
    nome = f"g5-falso-{sufixo}"
    definicoes = [
        t3.definicao("ler_caixa", "Le a caixa de mensagens."),
        t3.definicao("ver_agenda", "Mostra a agenda.", esquema={"type": "object", "properties": {"dia": {}}}),
    ]
    async with dono.begin() as c:
        sistema_id = await t3.cadastrar_terceiro(c, nome=nome)
        for d in definicoes:
            await t3.revisar_ferramenta(c, sistema_id, d)
        revisao = await t3.gravar_revisao_do_servidor(c, sistema_id) if revisar_servidor else uuid.UUID(int=0)
    return Revisado(sistema_id, nome, revisao, definicoes)


async def _situacao(dono: AsyncEngine, sistema_id: uuid.UUID) -> Any:
    async with dono.connect() as c:
        return (
            await c.execute(
                text(
                    "SELECT situacao, motivo, revisao_servidor_id, conferencias_sem_resposta FROM sistemas"
                    " WHERE id = :s"
                ),
                {"s": sistema_id},
            )
        ).one()


async def _emails(dono: AsyncEngine, sistema_id: uuid.UUID, evento: str) -> list[Any]:
    """Os avisos por e-mail (os webhooks que outros testes da sessao deixaram ativos ficam de fora)."""
    return [a for a in await avisos_do_sistema(dono, sistema_id, evento) if a.canal == "email"]


async def _linhas(dono: AsyncEngine, sistema_id: uuid.UUID, tipo: str) -> list[Any]:
    async with dono.connect() as c:
        return list(
            (
                await c.execute(
                    text("SELECT dados FROM extrato WHERE sistema_id = :s AND tipo = :t ORDER BY id"),
                    {"s": sistema_id, "t": tipo},
                )
            ).all()
        )


@pytest.fixture
async def dono(banco_migrado: Parametros) -> AsyncIterator[AsyncEngine]:
    engine = criar_engine(banco_migrado, pool_size=3, max_overflow=3)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def rotinas_db(banco_migrado: Parametros) -> AsyncIterator[AsyncEngine]:
    engine = g.engine_do_papel(banco_migrado, "mcp_claro_rotinas")
    try:
        yield engine
    finally:
        await engine.dispose()


async def test_ferramenta_sumida_e_ferramenta_nova_levam_a_quarentena(
    dono: AsyncEngine, rotinas_db: AsyncEngine
) -> None:
    sumiu = await _terceiro_revisado(dono)
    nova = await _terceiro_revisado(dono)
    igual = await _terceiro_revisado(dono)
    extra = t3.definicao("apagar_tudo", "Ferramenta que o fornecedor acrescentou.")
    fonte = FonteFalsa(
        {
            sumiu.sistema_id: sumiu.definicoes[:1],
            nova.sistema_id: [*nova.definicoes, extra],
            igual.sistema_id: list(reversed(igual.definicoes)),  # mesma definicao em outra ordem: nada
        }
    )
    resultado = await conferir_sistemas(rotinas_db, fonte, [ADMIN])
    assert sorted(resultado.quarentenas) == sorted([sumiu.nome, nova.nome])
    assert (await _situacao(dono, sumiu.sistema_id)).situacao == "quarentena"
    assert (await _situacao(dono, nova.sistema_id)).situacao == "quarentena"
    assert (await _situacao(dono, igual.sistema_id)).situacao == "ativo"

    ((dados_sumiu,),) = await _linhas(dono, sumiu.sistema_id, "sistema_em_quarentena")
    assert [(f["ferramenta"], f["mudanca"], f["impressao_depois"]) for f in dados_sumiu["ferramentas"]] == [
        ("ver_agenda", "sumiu", None)
    ]
    ((dados_nova,),) = await _linhas(dono, nova.sistema_id, "sistema_em_quarentena")
    assert [(f["ferramenta"], f["mudanca"], f["impressao_antes"]) for f in dados_nova["ferramentas"]] == [
        ("apagar_tudo", "nova", None)
    ]
    (aviso,) = await avisos_do_sistema(dono, nova.sistema_id, "sistema_em_quarentena")
    assert aviso.corpo["mudancas"] == {"mudou": 0, "nova": 1, "sumiu": 0}
    assert "apagar_tudo" not in json.dumps(aviso.corpo)
    assert await _linhas(dono, igual.sistema_id, "sistema_em_quarentena") == []


async def test_terceiro_sem_revisao_do_servidor_nao_e_conferido(dono: AsyncEngine, rotinas_db: AsyncEngine) -> None:
    sem = await _terceiro_revisado(dono, revisar_servidor=False)
    fonte = FonteFalsa({sem.sistema_id: []})  # se fosse conferido, "todas sumiram" daria quarentena
    resultado = await conferir_sistemas(rotinas_db, fonte, [ADMIN])
    assert fonte.chamadas == []
    assert sem.nome not in resultado.quarentenas + resultado.sem_resposta
    assert (await _situacao(dono, sem.sistema_id)).situacao == "ativo"


async def test_conclusao_nova_no_meio_da_rodada_nao_vira_quarentena(dono: AsyncEngine, rotinas_db: AsyncEngine) -> None:
    """Critica B1: as rotinas leem a revisao N; o ADMIN conclui N+1 (com a mudanca revisada) enquanto elas
    comparam com N; a comparacao velha nao poe o servidor em quarentena."""
    t = await _terceiro_revisado(dono)
    mudada = dict(t.definicoes[0], description="Le a caixa de mensagens (texto novo).")
    nova_revisao: list[uuid.UUID] = []

    async def concluir_no_meio() -> list[dict[str, Any]]:
        async with dono.begin() as c:
            await t3.revisar_ferramenta(c, t.sistema_id, mudada)
            nova_revisao.append(await t3.gravar_revisao_do_servidor(c, t.sistema_id))
        return [mudada, t.definicoes[1]]

    resultado = await conferir_sistemas(rotinas_db, FonteFalsa({t.sistema_id: concluir_no_meio}), [ADMIN])
    assert t.nome not in resultado.quarentenas
    situacao = await _situacao(dono, t.sistema_id)
    assert (situacao.situacao, situacao.revisao_servidor_id) == ("ativo", nova_revisao[0])
    assert await _linhas(dono, t.sistema_id, "sistema_em_quarentena") == []
    assert await avisos_do_sistema(dono, t.sistema_id, "sistema_em_quarentena") == []

    # Na rodada seguinte, contra N+1, nada mudou.
    resultado = await conferir_sistemas(rotinas_db, FonteFalsa({t.sistema_id: [mudada, t.definicoes[1]]}), [ADMIN])
    assert t.nome not in resultado.quarentenas
    assert (await _situacao(dono, t.sistema_id)).situacao == "ativo"


async def test_rodada_sem_resposta_contra_revisao_velha_nao_conta(dono: AsyncEngine, rotinas_db: AsyncEngine) -> None:
    """A mesma corrida na contagem sem resposta: a conclusao nova no meio zera a conta, e a rodada que leu a
    revisao velha nao soma nem poe em quarentena."""
    t = await _terceiro_revisado(dono)
    async with dono.begin() as c:
        await c.execute(text("UPDATE sistemas SET conferencias_sem_resposta = 1 WHERE id = :s"), {"s": t.sistema_id})

    async def concluir_e_falhar() -> list[dict[str, Any]]:
        async with dono.begin() as c:
            await t3.gravar_revisao_do_servidor(c, t.sistema_id)
        raise RuntimeError("o servidor nao entregou")

    resultado = await conferir_sistemas(rotinas_db, FonteFalsa({t.sistema_id: concluir_e_falhar}), [ADMIN])
    assert t.nome not in resultado.quarentenas
    async with dono.connect() as c:
        situacao, rodadas = (
            await c.execute(
                text("SELECT situacao, conferencias_sem_resposta FROM sistemas WHERE id = :s"), {"s": t.sistema_id}
            )
        ).one()
    assert (situacao, rodadas) == ("ativo", 0)


async def test_servidor_que_vai_e_volta_avisa_o_admin_a_cada_quarentena(
    dono: AsyncEngine, rotinas_db: AsyncEngine
) -> None:
    """O aviso e um por revisao e por conjunto novo de impressoes: um servidor que alterna entre duas definicoes, com
    o ADMIN revisando no meio, avisa a cada quarentena (a chave nao repete a de uma quarentena antiga)."""
    t = await _terceiro_revisado(dono)
    a, b = t.definicoes[0], dict(t.definicoes[0], description="Le a caixa (texto B).")
    for atual, proxima in ((a, b), (b, a), (a, b)):
        resultado = await conferir_sistemas(rotinas_db, FonteFalsa({t.sistema_id: [proxima, t.definicoes[1]]}), [ADMIN])
        assert resultado.quarentenas == [t.nome], atual
        async with dono.begin() as c:  # o ADMIN revisa a mudanca e conclui de novo
            await t3.revisar_ferramenta(c, t.sistema_id, proxima)
            await t3.gravar_revisao_do_servidor(c, t.sistema_id)
    avisos = await avisos_do_sistema(dono, t.sistema_id, "sistema_em_quarentena")
    assert len(avisos) == 3
    assert len({x.chave_unica for x in avisos}) == 3
    assert len(await _linhas(dono, t.sistema_id, "sistema_em_quarentena")) == 3


async def test_revisao_com_impressao_fora_da_receita_vai_a_quarentena(
    dono: AsyncEngine, rotinas_db: AsyncEngine
) -> None:
    """Uma revisao gravada fora da receita (a impressao do servidor gravada nao e a da lista dela; o banco nao confere
    uma contra a outra) nao serve de base: mesmo com a lista igual, nome a nome, ao que o servidor declara, ele vai a
    quarentena (o lado seguro), com zero ferramentas mudadas, e o ADMIN recebe o aviso."""
    sufixo = uuid.uuid4().hex[:8]
    nome = f"g5-receita-{sufixo}"
    definicoes = [t3.definicao("ler_caixa", "Le a caixa.")]
    errada = "sha256:" + "0" * 64
    async with dono.begin() as c:
        sistema_id = await t3.cadastrar_terceiro(c, nome=nome)
        await t3.revisar_ferramenta(c, sistema_id, definicoes[0])
        lista = await t3.declaradas_revisadas(c, sistema_id)
        revisao = (
            await c.execute(
                text(
                    "INSERT INTO revisoes_servidor (sistema_id, versao, impressao, declaradas, publicadas, fornecedor,"
                    " revisado_por) VALUES (:s, 1, :i, CAST(:d AS jsonb),"
                    " ARRAY(SELECT id FROM ferramentas_revisadas WHERE sistema_id = :s AND situacao = 'ativa'),"
                    " 'Fornecedor', 'admin-teste') RETURNING id"
                ),
                {"s": sistema_id, "i": errada, "d": json.dumps(lista)},
            )
        ).scalar_one()
        await c.execute(
            text("UPDATE sistemas SET revisao_servidor_id = :r WHERE id = :s"), {"r": revisao, "s": sistema_id}
        )
    resultado = await conferir_sistemas(rotinas_db, FonteFalsa({sistema_id: definicoes}), [ADMIN])
    assert resultado.quarentenas == [nome]
    situacao = await _situacao(dono, sistema_id)
    assert (situacao.situacao, situacao.motivo, situacao.revisao_servidor_id) == ("quarentena", MOTIVO_QUARENTENA, None)
    ((dados,),) = await _linhas(dono, sistema_id, "sistema_em_quarentena")
    assert (dados["revisao"], dados["mudancas"], dados["ferramentas"]) == (str(revisao), 0, [])
    (aviso,) = await _emails(dono, sistema_id, "sistema_em_quarentena")
    assert (aviso.corpo["quantas"], aviso.corpo["impressao_revisada"]) == (0, errada)
    assert aviso.corpo["mudancas"] == {"mudou": 0, "nova": 0, "sumiu": 0}


# --- Contagem das rodadas sem resposta, contadores e transacao ----------------------------------------------------


async def test_rodada_nao_feita_no_meio_nao_conta_nem_zera(dono: AsyncEngine, rotinas_db: AsyncEngine) -> None:
    """O servidor falha, a rodada seguinte nao acontece por um problema do MCP-CLARO, e o servidor falha de novo:
    as duas falhas sao seguidas (a rodada nao feita nao e rodada) e levam a quarentena."""
    t = await _terceiro_revisado(dono)
    respostas = iter(
        [
            RuntimeError("o servidor nao entregou"),
            ConferenciaNaoFeita("o despachante nao atendeu"),
            RuntimeError("o servidor nao entregou"),
        ]
    )

    async def proxima() -> list[dict[str, Any]]:
        raise next(respostas)

    fonte = FonteFalsa({t.sistema_id: proxima})
    vistas = []
    for _ in range(3):
        resultado = await conferir_sistemas(rotinas_db, fonte, [ADMIN])
        situacao = await _situacao(dono, t.sistema_id)
        vistas.append((situacao.situacao, situacao.conferencias_sem_resposta, t.nome in resultado.quarentenas))
    assert vistas == [("ativo", 1, False), ("ativo", 1, False), ("quarentena", 0, True)]
    assert (await _situacao(dono, t.sistema_id)).motivo == MOTIVO_SEM_RESPOSTA
    ((dados,),) = await _linhas(dono, t.sistema_id, "sistema_em_quarentena_sem_resposta")
    assert dados["revisao"] == str(t.revisao_id)


async def test_mudo_de_novo_depois_de_nova_revisao_avisa_o_admin_de_novo(
    dono: AsyncEngine, rotinas_db: AsyncEngine
) -> None:
    """Mudo por duas rodadas, quarentena; o ADMIN conclui uma revisao nova; mudo de novo por duas rodadas: outra
    quarentena e outro aviso (a chave do aviso leva a revisao comparada)."""
    t = await _terceiro_revisado(dono)
    fonte = FonteFalsa({t.sistema_id: RuntimeError("o servidor nao entregou")})
    revisoes = [t.revisao_id]
    for vez in range(2):
        for _ in range(RODADAS_SEM_RESPOSTA):
            resultado = await conferir_sistemas(rotinas_db, fonte, [ADMIN])
        assert resultado.quarentenas == [t.nome], vez
        async with dono.begin() as c:  # o ADMIN confere com o fornecedor e conclui de novo
            revisoes.append(await t3.gravar_revisao_do_servidor(c, t.sistema_id))
    avisos = await _emails(dono, t.sistema_id, "sistema_em_quarentena_sem_resposta")
    assert [a.corpo["revisao"] for a in avisos] == [str(r) for r in revisoes[:2]]
    assert len({a.chave_unica for a in avisos}) == 2
    assert len(await _linhas(dono, t.sistema_id, "sistema_em_quarentena_sem_resposta")) == 2


async def test_aviso_que_falha_desfaz_a_quarentena_e_nada_e_contado(
    dono: AsyncEngine, rotinas_db: AsyncEngine, contados: Contados, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Quarentena e aviso ao ADMIN numa transacao so, e os contadores depois dela: se o aviso falha, o servidor
    segue ativo, sem linha no extrato e sem contagem; a rodada seguinte faz tudo."""
    t = await _terceiro_revisado(dono)
    fonte = FonteFalsa({t.sistema_id: t.definicoes[:1]})  # uma das duas sumiu
    original = mudou_parou.avisar_admin

    async def aviso_que_falha(*args: Any, **kwargs: Any) -> int:
        raise RuntimeError("a fila de avisos nao gravou")

    monkeypatch.setattr(mudou_parou, "avisar_admin", aviso_que_falha)
    resultado = await conferir_sistemas(rotinas_db, fonte, [ADMIN])
    assert t.nome not in resultado.quarentenas
    assert (await _situacao(dono, t.sistema_id)).situacao == "ativo"
    assert await _linhas(dono, t.sistema_id, "sistema_em_quarentena") == []
    assert contados.de("mudou_parou_quarentenas") == 0

    monkeypatch.setattr(mudou_parou, "avisar_admin", original)
    resultado = await conferir_sistemas(rotinas_db, fonte, [ADMIN])
    assert resultado.quarentenas == [t.nome]
    assert (await _situacao(dono, t.sistema_id)).situacao == "quarentena"
    assert len(await _linhas(dono, t.sistema_id, "sistema_em_quarentena")) == 1
    assert len(await _emails(dono, t.sistema_id, "sistema_em_quarentena")) == 1
    assert contados.de("mudou_parou_quarentenas", motivo="mudou") == 1


async def test_quarentena_do_mudou_parou_cancela_o_que_espera_e_conta(
    banco_migrado: Parametros, contados: Contados
) -> None:
    """Com uma acao esperando a ficha, a quarentena do mudou-parou a cancela e o painel conta: ``acoes_paradas``
    com o motivo ``quarentena`` e ``mudou_parou_quarentenas`` com o motivo ``mudou``."""
    async with subir_mundo(banco_migrado) as m:
        espera = await m.chamar("ana", "t_enviar", {"destinatario": "fora", "texto": "espera a ficha"})
        op = await m.operacao(str((espera.meta or {})[PREFIXO_META + "protocolo"]))
        assert op.estado == Estado.AGUARDANDO_FICHA
        resultado = await conferir_sistemas(m.rotinas_db, FonteFalsa({m.terceiro_id: []}), [ADMIN])
        assert resultado.quarentenas == [m.terceiro_nome]
        assert (await m.operacao(op.protocolo)).estado == Estado.CANCELADA
        assert contados.de("acoes_paradas", motivo="quarentena") == 1
        assert contados.de("acoes_paradas") == 1
        assert contados.de("mudou_parou_quarentenas", motivo="mudou") == 1
        (aviso,) = await _emails(m.dono, m.terceiro_id, "sistema_em_quarentena")
        assert aviso.corpo["paradas"] == 1
