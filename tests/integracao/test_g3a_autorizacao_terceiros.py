"""Autorizacao por pessoa num servidor de terceiros (G3a, item 8; critica M1; desenho 3.4).

- a configuracao de um terceiro so passa de vazia a preenchida: depois disso, "Para mudar, cadastre o servidor de
  novo.";
- com a chave da Fase 3 desligada, so pessoa de teste autoriza um terceiro;
- a troca do codigo e a renovacao, que ficam no despachante, passam o ``token_endpoint`` pela mesma conferencia do
  endereco combinado da sala (https e IP publico): recusado, nada vai ao servidor de autorizacao e o extrato ganha
  ``sala_endereco_recusado``;
- o endereco de volta de um terceiro e so dele (``/telas/autorizar/<id>/volta``), e quem conclui manda o
  ``sistema_id`` desse endereco (R6a-1; a mistura de servidores de autorizacao fica em
  ``test_r6a_mistura_autorizacao.py``).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette

from exemplos import mal_comportado
from exemplos.comum import CHAVES_PADRAO
from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.despachante import autorizacao, chaves, renovacao
from mcp_claro.despachante.app import criar_app_despachante
from tests import apoio_g3a as g
from tests import apoio_terceiros as t3
from tests.apoio import servidor
from tests.cifra_falsa import CifraFalsa
from tests.integracao.test_autorizacao import OAuthFalso

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


def volta(sistema_id: uuid.UUID) -> str:
    """O endereco de volta de um servidor de terceiros, como as telas mandam: so dele."""
    return f"https://mcp-claro.teste{autorizacao.caminho_da_volta(sistema_id)}"


@dataclass
class Cena:
    p: Parametros
    dono: AsyncEngine
    desp_db: AsyncEngine
    cifra: CifraFalsa
    oauth: OAuthFalso
    url_oauth: str

    def config(self) -> dict[str, Any]:
        return {
            "authorization_endpoint": f"{self.url_oauth}/authorize",
            "token_endpoint": f"{self.url_oauth}/token",
            "client_id": self.oauth.client_id,
            "escopos": ["leitura"],
        }

    async def pessoa(self, *, teste: bool) -> uuid.UUID:
        async with self.dono.begin() as c:
            p = await repositorio.pessoa_no_login(c, "https://idp.g3a.teste", f"aut-{uuid.uuid4().hex[:8]}", "P")
            await c.execute(text("UPDATE pessoas SET teste = :t WHERE id = :p"), {"t": teste, "p": p.id})
        return p.id

    async def terceiro(self, *, configurar: bool = True, **config: Any) -> uuid.UUID:
        async with self.dono.begin() as c:
            sistema_id = await t3.cadastrar_terceiro(c, nome=f"g3a-aut-{uuid.uuid4().hex[:8]}")
        await self.configurar(sistema_id, configurar=configurar, **config)
        return sistema_id

    async def configurar(self, sistema_id: uuid.UUID, *, configurar: bool = True, **config: Any) -> None:
        async with self.desp_db.begin() as c:
            await autorizacao.guardar_segredo_cliente(c, self.cifra, sistema_id, self.oauth.segredo)
        if configurar:
            async with self.dono.begin() as c:  # a tela do ADMIN configura (a entrada), nao o despachante
                await autorizacao.configurar(c, sistema_id, self.config() | config)


@pytest.fixture
async def cena(banco_migrado: Parametros) -> AsyncIterator[Cena]:
    dono = criar_engine(banco_migrado, pool_size=3, max_overflow=3)
    desp_db = g.engine_do_papel(banco_migrado, "mcp_claro_despachante")
    oauth = OAuthFalso()
    try:
        async with servidor(oauth.app()) as url_oauth:
            yield Cena(banco_migrado, dono, desp_db, CifraFalsa(), oauth, url_oauth)
    finally:
        await dono.dispose()
        await desp_db.dispose()


async def test_configuracao_de_terceiro_so_passa_de_vazia_a_preenchida(cena: Cena) -> None:
    sistema_id = await cena.terceiro(configurar=False)
    async with cena.dono.begin() as c:
        assert await autorizacao.configurar(c, sistema_id, cena.config()) is not None
    for dados in (cena.config() | {"client_id": "outro"}, cena.config(), None):
        async with cena.dono.begin() as c:
            with pytest.raises(autorizacao.ConfiguracaoAutorizacaoInvalida, match=r"^Para mudar, cadastre o servidor"):
                await autorizacao.configurar(c, sistema_id, dados)
    async with cena.dono.connect() as c:
        guardada = (
            await c.execute(text("SELECT autorizacao FROM sistemas WHERE id = :s"), {"s": sistema_id})
        ).scalar_one()
    assert guardada["client_id"] == cena.oauth.client_id
    # Num sistema proprio, como nas Fases 1 e 2: reconfigurar segue valendo.
    async with cena.dono.begin() as c:
        proprio = await repositorio.cadastrar_sistema(
            c, nome=f"g3a-aut-p-{uuid.uuid4().hex[:6]}", endereco="https://p.exemplo/mcp", tipo_chave="por_pessoa"
        )
        await autorizacao.configurar(c, proprio, cena.config())
        await autorizacao.configurar(c, proprio, cena.config() | {"client_id": "outro"})


async def test_com_a_chave_desligada_so_pessoa_de_teste_autoriza_terceiro(cena: Cena) -> None:
    sistema_id = await cena.terceiro()
    real, de_teste = await cena.pessoa(teste=False), await cena.pessoa(teste=True)
    async with cena.desp_db.begin() as c:
        with pytest.raises(autorizacao.AutorizacaoRecusada) as recusa:
            await autorizacao.iniciar_autorizacao(
                c, cena.cifra, real, sistema_id, volta(sistema_id), papel="despachante"
            )
    assert recusa.value.motivo == "terceiros_desligados"
    async with cena.desp_db.begin() as c:
        assert await autorizacao.iniciar_autorizacao(
            c, cena.cifra, de_teste, sistema_id, volta(sistema_id), papel="despachante"
        )
        assert await autorizacao.iniciar_autorizacao(
            c, cena.cifra, real, sistema_id, volta(sistema_id), papel="despachante", terceiros_ligados=True
        )


async def test_rota_de_iniciar_aplica_a_chave_da_fase_3(cena: Cena) -> None:
    sistema_id = await cena.terceiro()
    real = await cena.pessoa(teste=False)
    corpo = {"pessoa_id": str(real), "sistema_id": str(sistema_id), "redirect_uri": volta(sistema_id)}
    for ligados, esperado in ((False, 403), (True, 200)):
        rotas = autorizacao.rotas_autorizacao(cena.desp_db, cena.cifra, terceiros_ligados=ligados)
        transporte = httpx2.ASGITransport(app=Starlette(routes=rotas))
        async with httpx2.AsyncClient(transport=transporte, base_url="http://despachante") as http:
            r = await http.post("/interno/autorizacao/iniciar", json=corpo)
        assert r.status_code == esperado, r.text


async def _codigo(
    cena: Cena, pessoa_id: uuid.UUID, sistema_id: uuid.UUID, *, terceiros_ligados: bool = False
) -> tuple[str, str]:
    async with cena.desp_db.begin() as c:
        url = await autorizacao.iniciar_autorizacao(
            c,
            cena.cifra,
            pessoa_id,
            sistema_id,
            volta(sistema_id),
            papel="despachante",
            terceiros_ligados=terceiros_ligados,
        )
    async with httpx2.AsyncClient(follow_redirects=False) as http:
        r = await http.get(url + "&" + urlencode({"usuario": "ana"}))
    q = parse_qs(urlsplit(r.headers["location"]).query)
    return q["state"][0], q["code"][0]


async def test_troca_do_codigo_de_terceiro_passa_pela_conferencia_do_endereco(cena: Cena) -> None:
    sistema_id = await cena.terceiro()
    pessoa = await cena.pessoa(teste=True)
    estado, codigo = await _codigo(cena, pessoa, sistema_id)
    antes = cena.oauth.contagem["codigo"]
    # Em teste, o token_endpoint em 127.0.0.1 nao e publico: sem a liberacao dos testes, nada sai.
    with pytest.raises(autorizacao.AutorizacaoRecusada) as recusa:
        await autorizacao.concluir_autorizacao(
            cena.desp_db,
            cena.cifra,
            estado=estado,
            pessoa_id=pessoa,
            codigo=codigo,
            ambiente="teste",
            sistema_id=sistema_id,
        )
    assert recusa.value.motivo == autorizacao.ENDERECO_RECUSADO
    assert "nao e publico ou nao usa https" in recusa.value.mensagem
    assert cena.oauth.contagem["codigo"] == antes
    tipos = await g.tipos_no_extrato(cena.dono, sistema_id)
    assert "sala_endereco_recusado" in tipos
    assert "autorizacao_recusada" in tipos
    # Com a liberacao dos testes (so em desenvolvimento e teste), a troca sai e guarda a chave.
    estado, codigo = await _codigo(cena, pessoa, sistema_id)
    feita = await autorizacao.concluir_autorizacao(
        cena.desp_db,
        cena.cifra,
        estado=estado,
        pessoa_id=pessoa,
        codigo=codigo,
        ambiente="teste",
        liberar_locais=True,
        sistema_id=sistema_id,
    )
    assert feita.sistema_id == sistema_id
    assert cena.oauth.contagem["codigo"] == antes + 1
    # Fora de desenvolvimento e teste, a liberacao nao vale (e o http tambem nao).
    estado, codigo = await _codigo(cena, pessoa, sistema_id)
    with pytest.raises(autorizacao.AutorizacaoRecusada):
        await autorizacao.concluir_autorizacao(
            cena.desp_db,
            cena.cifra,
            estado=estado,
            pessoa_id=pessoa,
            codigo=codigo,
            ambiente="producao",
            liberar_locais=True,
            sistema_id=sistema_id,
        )
    assert cena.oauth.contagem["codigo"] == antes + 1


async def test_renovacao_de_terceiro_passa_pela_conferencia_do_endereco(cena: Cena) -> None:
    sistema_id = await cena.terceiro()
    pessoa = await cena.pessoa(teste=True)
    vencida = datetime.now(UTC) - timedelta(minutes=5)
    async with cena.dono.begin() as c:
        await chaves.guardar(
            c, cena.cifra, sistema_id=sistema_id, segredo="acesso-velho", pessoa_id=pessoa, expira_em=vencida
        )
        cena.oauth.renovacoes["renova-ana-0"] = "ana"
        await chaves.guardar(
            c, cena.cifra, sistema_id=sistema_id, segredo="renova-ana-0", pessoa_id=pessoa, tipo="renovacao"
        )
    antes = cena.oauth.contagem["renovacao"]
    with pytest.raises(renovacao.RenovacaoIndisponivel):
        await renovacao.chave_pronta(
            cena.desp_db, cena.cifra, sistema_id=sistema_id, pessoa_id=pessoa, ambiente="teste"
        )
    assert cena.oauth.contagem["renovacao"] == antes  # a chave de renovacao nao foi a lugar nenhum
    linhas = await g.extrato(cena.dono, sistema_id)
    assert [ln.tipo for ln in linhas if ln.tipo in ("renovacao_falhou", "sala_endereco_recusado")] == [
        "renovacao_falhou",
        "sala_endereco_recusado",
    ]
    nova = await renovacao.chave_pronta(
        cena.desp_db, cena.cifra, sistema_id=sistema_id, pessoa_id=pessoa, ambiente="teste", liberar_locais=True
    )
    assert nova.renovada is True
    assert cena.oauth.contagem["renovacao"] == antes + 1


async def test_concluir_com_a_chave_desligada_recusa_pessoa_real(cena: Cena) -> None:
    """A pessoa real comecou com a chave ligada; na volta, a chave esta desligada: nada vai ao OAuth."""
    sistema_id = await cena.terceiro()
    real = await cena.pessoa(teste=False)
    antes = cena.oauth.contagem["codigo"]
    estado, codigo = await _codigo(cena, real, sistema_id, terceiros_ligados=True)
    with pytest.raises(autorizacao.AutorizacaoRecusada) as recusa:
        await autorizacao.concluir_autorizacao(
            cena.desp_db,
            cena.cifra,
            estado=estado,
            pessoa_id=real,
            codigo=codigo,
            ambiente="teste",
            liberar_locais=True,
            sistema_id=sistema_id,
        )
    assert recusa.value.motivo == "terceiros_desligados"
    assert cena.oauth.contagem["codigo"] == antes
    # Pela rota: desligada recusa (403); ligada conclui.
    for ligados, status, motivo in ((False, 403, "terceiros_desligados"), (True, 200, None)):
        estado, codigo = await _codigo(cena, real, sistema_id, terceiros_ligados=True)
        rotas = autorizacao.rotas_autorizacao(
            cena.desp_db, cena.cifra, ambiente="teste", liberar_locais=True, terceiros_ligados=ligados
        )
        transporte = httpx2.ASGITransport(app=Starlette(routes=rotas))
        async with httpx2.AsyncClient(transport=transporte, base_url="http://despachante") as http:
            r = await http.post(
                "/interno/autorizacao/concluir",
                json={"estado": estado, "pessoa_id": str(real), "codigo": codigo, "sistema_id": str(sistema_id)},
            )
        assert r.status_code == status, r.text
        assert r.json().get("motivo") == motivo
    assert cena.oauth.contagem["codigo"] == antes + 1


async def test_rota_de_concluir_confere_o_endereco_pelo_ambiente(cena: Cena) -> None:
    """A liberacao dos testes (``liberar_locais``) so vale em desenvolvimento e teste, tambem pela rota."""
    sistema_id = await cena.terceiro()
    pessoa = await cena.pessoa(teste=True)
    antes = cena.oauth.contagem["codigo"]
    for ambiente, status, motivo in (("producao", 403, autorizacao.ENDERECO_RECUSADO), ("teste", 200, None)):
        estado, codigo = await _codigo(cena, pessoa, sistema_id)
        rotas = autorizacao.rotas_autorizacao(cena.desp_db, cena.cifra, ambiente=ambiente, liberar_locais=True)
        transporte = httpx2.ASGITransport(app=Starlette(routes=rotas))
        async with httpx2.AsyncClient(transport=transporte, base_url="http://despachante") as http:
            r = await http.post(
                "/interno/autorizacao/concluir",
                json={"estado": estado, "pessoa_id": str(pessoa), "codigo": codigo, "sistema_id": str(sistema_id)},
            )
        assert r.status_code == status, r.text
        assert r.json().get("motivo") == motivo
    assert cena.oauth.contagem["codigo"] == antes + 1


def _app(cena: Cena, *, ambiente: str, ligados: bool = False) -> Starlette:
    return criar_app_despachante(
        cena.p.model_copy(update={"APP_ENV": ambiente, "FEATURE_PHASE3_THIRD_PARTY": ligados}),
        cena.desp_db,
        cena.cifra,
        local_para_terceiro=g.LOCAL_DE_TESTE,
        liberar_locais=True,
    )


async def _pelo_app(app: Starlette, rota: str, corpo: dict[str, Any]) -> httpx2.Response:
    transporte = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transporte, base_url="http://despachante") as http:
        return await http.post(rota, json=corpo)


async def test_app_liga_as_rotas_de_autorizacao_ao_ambiente_e_a_chave_da_fase_3(
    cena: Cena, monkeypatch: pytest.MonkeyPatch
) -> None:
    sistema_id = await cena.terceiro()
    real, de_teste = await cena.pessoa(teste=False), await cena.pessoa(teste=True)
    inicio = {"sistema_id": str(sistema_id), "redirect_uri": volta(sistema_id)}
    antes = cena.oauth.contagem["codigo"]
    # Em teste, com a liberacao dos testes: a troca sai; a pessoa real nao comeca (chave desligada).
    em_teste = _app(cena, ambiente="teste")
    r = await _pelo_app(em_teste, "/interno/autorizacao/iniciar", inicio | {"pessoa_id": str(real)})
    assert (r.status_code, r.json()["motivo"]) == (403, "terceiros_desligados")
    estado, codigo = await _codigo(cena, de_teste, sistema_id)
    corpo = {"estado": estado, "pessoa_id": str(de_teste), "codigo": codigo, "sistema_id": str(sistema_id)}
    r = await _pelo_app(em_teste, "/interno/autorizacao/concluir", corpo)
    assert r.status_code == 200, r.text
    assert cena.oauth.contagem["codigo"] == antes + 1
    # A partida ainda recusa a chave da Fase 3 (D6) e a producao sem a sala: so para ver a fiacao, sem a conferencia.
    monkeypatch.setattr(Parametros, "conferir", lambda self: None)
    ligada = _app(cena, ambiente="teste", ligados=True)
    r = await _pelo_app(ligada, "/interno/autorizacao/iniciar", inicio | {"pessoa_id": str(real)})
    assert r.status_code == 200, r.text
    em_producao = _app(cena, ambiente="producao")
    estado, codigo = await _codigo(cena, de_teste, sistema_id)
    r = await _pelo_app(
        em_producao,
        "/interno/autorizacao/concluir",
        {"estado": estado, "pessoa_id": str(de_teste), "codigo": codigo, "sistema_id": str(sistema_id)},
    )
    assert (r.status_code, r.json()["motivo"]) == (403, autorizacao.ENDERECO_RECUSADO)
    assert cena.oauth.contagem["codigo"] == antes + 1


async def test_endereco_de_troca_que_nao_resolve_e_indisponivel_nao_recusado(cena: Cena) -> None:
    sistema_id = await cena.terceiro(token_endpoint="https://nao-existe.invalid/token")
    pessoa = await cena.pessoa(teste=True)
    estado, codigo = await _codigo(cena, pessoa, sistema_id)
    with pytest.raises(autorizacao.AutorizacaoRecusada) as recusa:
        await autorizacao.concluir_autorizacao(
            cena.desp_db,
            cena.cifra,
            estado=estado,
            pessoa_id=pessoa,
            codigo=codigo,
            ambiente="teste",
            sistema_id=sistema_id,
        )
    assert recusa.value.motivo == "sistema_indisponivel"
    assert "sala_endereco_recusado" not in await g.tipos_no_extrato(cena.dono, sistema_id)


async def _guardar_chaves(cena: Cena, sistema_id: uuid.UUID, pessoa: uuid.UUID, expira_em: datetime) -> None:
    async with cena.dono.begin() as c:
        await chaves.guardar(
            c, cena.cifra, sistema_id=sistema_id, segredo="acesso-velho", pessoa_id=pessoa, expira_em=expira_em
        )
        cena.oauth.renovacoes["renova-ana-0"] = "ana"
        await chaves.guardar(
            c, cena.cifra, sistema_id=sistema_id, segredo="renova-ana-0", pessoa_id=pessoa, tipo="renovacao"
        )


async def test_renovacao_recusada_pelo_endereco_usa_a_chave_que_ainda_vale(cena: Cena) -> None:
    sistema_id = await cena.terceiro()
    pessoa = await cena.pessoa(teste=True)
    await _guardar_chaves(cena, sistema_id, pessoa, datetime.now(UTC) + timedelta(seconds=30))  # dentro da margem
    antes = cena.oauth.contagem["renovacao"]
    aberta = await renovacao.chave_pronta(
        cena.desp_db, cena.cifra, sistema_id=sistema_id, pessoa_id=pessoa, ambiente="teste"
    )
    assert (aberta.segredo, aberta.renovada) == ("acesso-velho", False)
    assert cena.oauth.contagem["renovacao"] == antes
    tipos = await g.tipos_no_extrato(cena.dono, sistema_id)
    assert [t for t in tipos if t in ("renovacao_falhou", "sala_endereco_recusado")] == [
        "renovacao_falhou",
        "sala_endereco_recusado",
    ]


async def test_leitura_de_terceiro_renova_a_chave_com_o_ambiente_do_despachante(
    cena: Cena, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A leitura renova a chave vencida pela conferencia do endereco no ambiente do despachante (em teste, com a
    liberacao dos testes, o OAuth em 127.0.0.1 passa; no padrao estrito, nao)."""
    # O sistema de exemplo aceita a chave que o OAuth de teste emite na renovacao.
    monkeypatch.setenv("EXEMPLO_CHAVES", json.dumps(CHAVES_PADRAO | {"acesso-ana-1": "ana"}))
    async with servidor(mal_comportado.criar()) as base:
        arr = await g.preparar(
            cena.dono,
            cena.cifra,
            endereco=f"{base}/mcp",
            definicao={
                "name": "consultar_pedido",
                "description": "Consulta a situacao de um pedido.",
                "inputSchema": {"type": "object", "properties": {"pedido": {"type": "string", "maxLength": 40}}},
            },
            origem="terceiro",
            tipo_chave="por_pessoa",
            chave=None,
        )
        await cena.configurar(arr.sistema_id)
        await _guardar_chaves(cena, arr.sistema_id, arr.pessoa_id, datetime.now(UTC) - timedelta(minutes=5))
        antes = cena.oauth.contagem["renovacao"]
        await g.abrir_a_saida(cena.dono)
        desp = g.despachante(cena.desp_db, cena.cifra)
        resultado = await desp.ler(arr.pedido({"pedido": "1"}))
    assert resultado.get("isError") is not True, resultado
    assert cena.oauth.contagem["renovacao"] == antes + 1
    assert "chave_renovada" in await g.tipos_no_extrato(cena.dono, arr.sistema_id)
