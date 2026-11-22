"""R6a-1: mistura de servidores de autorizacao (OAuth mix-up, RFC 9700 4.4), pelo contrato K1 da revisao cruzada.

A cena: um servidor de terceiros mal-intencionado (A) recebe a pessoa na pagina de autorizacao dele e a manda, com o
mesmo estado e o mesmo desafio PKCE, para a pagina de autorizacao de outro sistema (H). O servidor de H e honesto: so
aceita o endereco de volta que tem cadastrado. Antes, o endereco de volta era um so para todos os sistemas; H voltava
ao MCP-CLARO com o codigo dele e o estado de A, e o despachante mandava o codigo de H, com o verificador de A, ao
``token_endpoint`` de A. Com H cliente publico, A trocava o codigo em H e ficava com a chave da pessoa em H.

Agora:

- o ``redirect_uri`` de um servidor de terceiros e ``/telas/autorizar/<id do servidor>/volta``; os sistemas proprios
  continuam em ``/telas/autorizar/volta``, o endereco ja cadastrado na Fase 2;
- a volta que chega pelo endereco de outro sistema, ou a de um terceiro que chega pela volta antiga, e recusada com
  ``estado_de_outro_sistema`` antes de abrir o segredo do cliente, decifrar o verificador ou trocar o codigo. Nada
  vai a ninguem, nada fica guardado e o extrato ganha ``autorizacao_recusada``;
- ``iniciar_autorizacao`` recusa um terceiro com ``redirect_uri`` fora do endereco de volta dele.

Tudo pelo caminho de verdade: as telas da entrada, a porta interna do despachante, o banco com o usuario de cada
papel. O arranjo e o de ``test_telas_autorizacao.subir``, com a liberacao dos testes para a troca de chave de um
terceiro em 127.0.0.1 (so vale em teste).
"""

from __future__ import annotations

import base64
import functools
import hashlib
import re
import secrets
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

import httpx2
import pytest
from sqlalchemy import text
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.despachante import autorizacao
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.telas import autorizar
from mcp_claro.versao import VERSAO_MCP
from tests import apoio_terceiros as t3
from tests.apoio import servidor
from tests.integracao import test_telas_autorizacao as ta
from tests.integracao.test_telas import Navegador

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

MOTIVO = "estado_de_outro_sistema"


def volta_de(sistema_id: uuid.UUID | None) -> str:
    """O endereco de volta que as telas mandam: o do proprio terceiro ou, sem ``sistema_id``, o da Fase 2."""
    return f"{ta.PUBLICO}/telas/autorizar/{sistema_id}/volta" if sistema_id else ta.VOLTA


def s256(v: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()


@dataclass
class ServidorH:
    """Servidor de autorizacao honesto de outro sistema, cliente publico (PKCE sem segredo).

    So aceita o endereco de volta cadastrado (``volta``), na pagina e na troca, como pede o RFC 6749."""

    client_id: str = "mcp-claro-em-h"
    volta: str = ""
    codigos: dict[str, dict[str, str]] = field(default_factory=dict)
    emitidas: list[str] = field(default_factory=list)

    def app(self) -> Starlette:
        async def autorizar_(pedido: Request) -> Response:
            q = pedido.query_params
            if q.get("client_id") != self.client_id or q.get("redirect_uri") != self.volta:
                return JSONResponse({"error": "invalid_request"}, status_code=400)
            codigo = secrets.token_urlsafe(16)
            # A pessoa ja tem sessao em H (login unico da organizacao): H emite o codigo sem perguntar de novo.
            self.codigos[codigo] = {"desafio": q["code_challenge"], "redirect_uri": q["redirect_uri"]}
            return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'code': codigo, 'state': q['state']})}", 302)

        async def token(pedido: Request) -> Response:
            form = dict(parse_qsl((await pedido.body()).decode()))
            dados = self.codigos.pop(form.get("code", ""), None)
            if (
                form.get("client_id") != self.client_id
                or dados is None
                or form.get("redirect_uri") != dados["redirect_uri"]
                or s256(form.get("code_verifier", "")) != dados["desafio"]
            ):
                return JSONResponse({"error": "invalid_grant"}, status_code=400)
            chave = f"chave-da-pessoa-em-H-{len(self.emitidas)}"
            self.emitidas.append(chave)
            return JSONResponse({"access_token": chave, "token_type": "Bearer", "expires_in": 3600})

        return Starlette(routes=[Route("/authorize", autorizar_), Route("/token", token, methods=["POST"])])


@dataclass
class ServidorA:
    """Servidor de autorizacao do terceiro mal-intencionado: manda a pessoa para H, com o endereco de volta que H tem
    cadastrado (e publico), e tenta trocar em H o codigo que chegar ao ``token_endpoint`` dele."""

    url_h: str
    h: ServidorH
    roubadas: list[str] = field(default_factory=list)
    recebidos: list[dict[str, str]] = field(default_factory=list)

    def app(self) -> Starlette:
        async def autorizar_(pedido: Request) -> Response:
            q = pedido.query_params
            para_h = {
                "response_type": "code",
                "client_id": self.h.client_id,
                "redirect_uri": self.h.volta,
                "state": q["state"],
                "code_challenge": q["code_challenge"],
                "code_challenge_method": "S256",
            }
            return RedirectResponse(f"{self.url_h}/authorize?{urlencode(para_h)}", 302)

        async def token(pedido: Request) -> Response:
            form = dict(parse_qsl((await pedido.body()).decode()))
            self.recebidos.append(form)
            async with httpx2.AsyncClient(trust_env=False) as http:
                r = await http.post(
                    f"{self.url_h}/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": form.get("code", ""),
                        "redirect_uri": form.get("redirect_uri", ""),
                        "code_verifier": form.get("code_verifier", ""),
                        "client_id": self.h.client_id,
                    },
                )
            if r.status_code == 200:
                self.roubadas.append(r.json()["access_token"])
            return JSONResponse({"access_token": "chave-de-A", "token_type": "Bearer", "expires_in": 3600})

        return Starlette(routes=[Route("/authorize", autorizar_), Route("/token", token, methods=["POST"])])


@dataclass
class Espia:
    """O que o despachante decifrou e as somas de contador da autorizacao, sem mudar o que eles fazem."""

    contextos: list[bytes] = field(default_factory=list)
    somas: list[tuple[str, dict[str, str]]] = field(default_factory=list)

    def recusas(self, motivo: str) -> int:
        return sum(1 for nome, r in self.somas if nome == "autorizacoes_recusadas" and r.get("motivo") == motivo)


@pytest.fixture
async def amb(banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> AsyncIterator[ta.Ambiente]:
    # Em teste, a troca de chave de um terceiro em 127.0.0.1 so sai com a liberacao dos testes (liberar_locais).
    monkeypatch.setattr(ta, "criar_app_despachante", functools.partial(criar_app_despachante, liberar_locais=True))
    async with ta.subir(banco_migrado, monkeypatch, tmp_path) as a:
        yield a


@pytest.fixture
def espia(amb: ta.Ambiente, monkeypatch: pytest.MonkeyPatch) -> Espia:
    e = Espia()
    decifrar = amb.cifra.decifrar
    somar = autorizacao.CONTADORES.somar

    async def decifrar_e_guardar(cifrado: str, contexto: bytes) -> bytes:
        e.contextos.append(contexto)
        return await decifrar(cifrado, contexto)

    def somar_e_guardar(nome: str, valor: int = 1, **rotulos: str) -> None:
        e.somas.append((nome, dict(rotulos)))
        somar(nome, valor, **rotulos)

    monkeypatch.setattr(amb.cifra, "decifrar", decifrar_e_guardar)
    monkeypatch.setattr(autorizacao.CONTADORES, "somar", somar_e_guardar)
    return e


async def _dar_ordem(amb: ta.Ambiente, pessoa_id: uuid.UUID, *sistemas: uuid.UUID) -> None:
    async with amb.entrada_db.begin() as c:
        modelo_id = await repositorio.criar_modelo_ordem(
            c,
            nome=f"R6a {ta.sufixo()}",
            descricao="Deixa o aplicativo ler nestes sistemas.",
            ferramentas=[(s, "ler") for s in sistemas],
            validade_dias=30,
            criado_por="admin-teste",
        )
        await repositorio.conceder_ordem(
            c, pessoa_id=pessoa_id, aplicativo_id=amb.aplicativo_id, modelo_id=modelo_id, dias=7, max_dias=30
        )


async def _link(nav: Navegador, sistema_id: uuid.UUID) -> str:
    r = await nav.get("/telas/autorizar")
    assert r.status_code == 200, r.text
    achado = re.search(rf'href="(/telas/autorizar/{sistema_id}/ir\?ficha=[0-9a-f]+)"', r.text)
    assert achado, "a lista nao trouxe o sistema"
    return achado.group(1)


async def _sistema_h(amb: ta.Ambiente, url_h: str, *, de_terceiro: bool) -> uuid.UUID:
    config = {
        "authorization_endpoint": f"{url_h}/authorize",
        "token_endpoint": f"{url_h}/token",
        "client_id": ServidorH.client_id,
        "autenticacao_cliente": "none",
    }
    async with amb.admin.begin() as c:
        if de_terceiro:
            return await t3.cadastrar_terceiro(c, nome=f"terceiro-h-{ta.sufixo()}", autorizacao=config)
        sistema_h = await repositorio.cadastrar_sistema(
            c, nome=f"proprio-h-{ta.sufixo()}", endereco=f"{url_h}/mcp", tipo_chave="por_pessoa"
        )
        await c.execute(
            text("UPDATE sistemas SET situacao = 'ativo', versao_mcp = :v WHERE id = :s"),
            {"s": sistema_h, "v": VERSAO_MCP},
        )
        await autorizacao.configurar(c, sistema_h, config)
    return sistema_h


def _abertos(contextos: list[bytes]) -> list[bytes]:
    """Contextos decifrados do segredo do cliente (``|cliente-oauth``) ou do verificador PKCE (``|autorizacao|``)."""
    return [ctx for ctx in contextos if b"|autorizacao|" in ctx or ctx.endswith(b"|cliente-oauth")]


async def _contar(amb: ta.Ambiente, sql: str, **parametros: Any) -> int:
    async with amb.admin.connect() as c:
        return int((await c.execute(text(sql), parametros)).scalar_one())


# --- A prova da triagem, invertida --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("h_de_terceiro", "a_confidencial"),
    [(False, False), (True, False), (False, True), (True, True)],
    ids=[
        "h_proprio_pela_volta_antiga",
        "h_terceiro_pela_dele",
        "a_confidencial_h_proprio",
        "a_confidencial_h_terceiro",
    ],
)
async def test_terceiro_nao_obtem_a_chave_da_pessoa_em_outro_sistema(
    amb: ta.Ambiente, espia: Espia, h_de_terceiro: bool, a_confidencial: bool
) -> None:
    """``a_confidencial``: A usa ``client_secret_basic``, com o segredo guardado; a recusa sai antes de abri-lo."""
    h = ServidorH()
    async with servidor(h.app()) as url_h:
        a = ServidorA(url_h, h)
        async with servidor(a.app()) as url_a:
            sistema_h = await _sistema_h(amb, url_h, de_terceiro=h_de_terceiro)
            # O endereco de volta que H tem cadastrado: o do proprio H (terceiro) ou o da Fase 2 (proprio).
            h.volta = volta_de(sistema_h if h_de_terceiro else None)
            async with amb.admin.begin() as c:
                sistema_a = await t3.cadastrar_terceiro(
                    c,
                    nome=f"terceiro-a-{ta.sufixo()}",
                    autorizacao={
                        "authorization_endpoint": f"{url_a}/authorize",
                        "token_endpoint": f"{url_a}/token",
                        "client_id": "mcp-claro-em-a",
                        "autenticacao_cliente": "client_secret_basic" if a_confidencial else "none",
                    },
                )
            if a_confidencial:
                async with amb.despachante_db.begin() as c:
                    await autorizacao.guardar_segredo_cliente(c, amb.cifra, sistema_a, "segredo-do-cliente-em-a")
            nav, _, pessoa_id = await amb.nova_pessoa("Ana")
            await _dar_ordem(amb, pessoa_id, sistema_a, sistema_h)

            # 1. A pessoa pede para autorizar o terceiro A, pelas telas. A volta dele e so dele.
            r = await nav.get(await _link(nav, sistema_a))
            assert r.status_code == 303, r.text
            pedido_a = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}
            assert r.headers["location"].startswith(f"{url_a}/authorize?")
            # 2. O navegador segue A -> H -> volta de H no MCP-CLARO, com o codigo de H e o estado de A.
            r = await amb.http.get(r.headers["location"])
            assert r.status_code == 302
            assert r.headers["location"].startswith(f"{url_h}/authorize?")
            r = await amb.http.get(r.headers["location"])
            assert r.status_code == 302, r.text
            volta = urlsplit(r.headers["location"])
            assert f"{volta.scheme}://{volta.netloc}{volta.path}" == h.volta
            assert parse_qs(volta.query)["state"] == [pedido_a["state"]]
            espia.contextos.clear()
            # 3. As telas mandam a volta ao despachante: recusada, com a frase da tela.
            r = await nav.get(f"{volta.path}?{volta.query}")

    prova = {"a_recebeu": a.recebidos, "chaves_de_h_com_a": a.roubadas, "codigos_de_h_por_trocar": len(h.codigos)}
    assert a.recebidos == [], f"o token_endpoint de A recebeu a volta de H: {prova}"
    assert a.roubadas == [], f"o terceiro A ficou com a chave da pessoa em H: {prova}"
    assert pedido_a["redirect_uri"] == volta_de(sistema_a)
    assert r.status_code == 403, r.text
    assert autorizar.MENSAGENS[MOTIVO] in r.text
    assert len(h.codigos) == 1  # o codigo de H nunca foi trocado
    # Nem o segredo do cliente (com A confidencial, ele existe) nem o verificador PKCE foram abertos.
    assert _abertos(espia.contextos) == []
    guardadas = "SELECT count(*) FROM chaves_cifradas WHERE pessoa_id = :p"
    assert await _contar(amb, guardadas, p=pessoa_id) == 0
    recusas = await amb.extrato("autorizacao_recusada", pessoa_id)
    assert [(ln.sistema_id, MOTIVO in ln.frase) for ln in recusas] == [(sistema_a, True)]
    assert await amb.extrato("autorizacao_concluida", pessoa_id) == []
    assert espia.recusas(MOTIVO) == 1
    # O estado de A ficou gasto: repetir a volta nao leva a nada.
    usada = "SELECT count(*) FROM autorizacoes_pendentes WHERE sistema_id = :s AND usada_em IS NOT NULL"
    assert await _contar(amb, usada, s=sistema_a) == 1


async def test_rota_de_concluir_compara_o_sistema_da_volta_com_o_da_pendencia(amb: ta.Ambiente, espia: Espia) -> None:
    """Direto na porta interna: ``sistema_id`` invalido e pedido invalido (o estado nao gasta); o de outro sistema e
    recusado; sem ``sistema_id``, um terceiro e recusado e um proprio segue (a volta da Fase 2)."""
    async with amb.admin.begin() as c:
        terceiro = await t3.cadastrar_terceiro(
            c,
            nome=f"terceiro-{ta.sufixo()}",
            autorizacao={
                "authorization_endpoint": f"{amb.url_oauth}/authorize",
                "token_endpoint": f"{amb.url_oauth}/token",
                "client_id": amb.oauth.client_id,
                "escopos": ["leitura"],
            },
        )
    async with amb.despachante_db.begin() as c:
        await autorizacao.guardar_segredo_cliente(c, amb.cifra, terceiro, amb.oauth.segredo)
    await amb.configurar_direto()  # o proprio do arranjo
    _, _, pessoa_id = await amb.nova_pessoa("Ana")
    rotas = autorizacao.rotas_autorizacao(amb.despachante_db, amb.cifra, ambiente="teste", liberar_locais=True)
    transporte = httpx2.ASGITransport(app=Starlette(routes=rotas))

    async def comecar(sistema_id: uuid.UUID, de_terceiro: bool) -> tuple[str, str]:
        async with amb.despachante_db.begin() as c:
            url = await autorizacao.iniciar_autorizacao(
                c, amb.cifra, pessoa_id, sistema_id, volta_de(sistema_id if de_terceiro else None), papel="despachante"
            )
        r = await amb.http.get(url + "&" + urlencode({"usuario": "ana"}))
        q = parse_qs(urlsplit(r.headers["location"]).query)
        return q["state"][0], q["code"][0]

    async with httpx2.AsyncClient(transport=transporte, base_url="http://despachante") as http:

        async def concluir(estado: str, codigo: str, **mais: Any) -> httpx2.Response:
            corpo = {"estado": estado, "pessoa_id": str(pessoa_id), "codigo": codigo} | mais
            return await http.post("/interno/autorizacao/concluir", json=corpo)

        async def recusada(estado: str, codigo: str, **mais: Any) -> httpx2.Response:
            """Recusada com ``estado_de_outro_sistema`` antes de abrir o segredo do cliente e o verificador."""
            antes_da_volta = len(espia.contextos)
            r = await concluir(estado, codigo, **mais)
            assert (r.status_code, r.json()["motivo"]) == (403, MOTIVO), r.text
            abertos = _abertos(espia.contextos[antes_da_volta:])
            assert abertos == [], f"a recusa abriu o segredo do cliente ou o verificador: {abertos}"
            return r

        estado, codigo = await comecar(terceiro, True)
        antes = amb.oauth.contagem["codigo"]
        for invalido in ("nao-e-um-uuid", "", 12, ["x"]):
            r = await concluir(estado, codigo, sistema_id=invalido)
            assert (r.status_code, r.json()["motivo"]) == (400, "pedido_invalido"), invalido
        # Pelo endereco de outro sistema (o proprio): recusado, nada vai ao OAuth. O terceiro e confidencial, com o
        # segredo guardado: a recusa sai antes de abri-lo.
        r = await recusada(estado, codigo, sistema_id=str(amb.sistema_id))
        assert r.json()["mensagem"] == "Este pedido de autorizacao e de outro sistema. Nada foi guardado."
        # Pela volta antiga (sem sistema_id), um terceiro tambem e recusado.
        estado, codigo = await comecar(terceiro, True)
        await recusada(estado, codigo)
        assert amb.oauth.contagem["codigo"] == antes
        # Pela volta dele, o terceiro conclui.
        estado, codigo = await comecar(terceiro, True)
        r = await concluir(estado, codigo, sistema_id=str(terceiro))
        assert (r.status_code, r.json()["concluida"]) == (200, True), r.text
        # O proprio: pela volta antiga, como na Fase 2, e pelo proprio endereco; pelo de outro, recusado.
        estado, codigo = await comecar(amb.sistema_id, False)
        assert (await concluir(estado, codigo)).status_code == 200
        estado, codigo = await comecar(amb.sistema_id, False)
        assert (await concluir(estado, codigo, sistema_id=str(amb.sistema_id))).status_code == 200
        estado, codigo = await comecar(amb.sistema_id, False)
        await recusada(estado, codigo, sistema_id=str(terceiro))
    assert amb.oauth.contagem["codigo"] == antes + 3
    assert espia.recusas(MOTIVO) == 3
    assert [ctx for ctx in espia.contextos if ctx.endswith(b"|cliente-oauth")] != []  # as que concluiram abriram


async def test_iniciar_recusa_terceiro_com_volta_que_nao_e_a_dele(amb: ta.Ambiente) -> None:
    async with amb.admin.begin() as c:
        terceiro = await t3.cadastrar_terceiro(
            c,
            nome=f"terceiro-{ta.sufixo()}",
            autorizacao={
                "authorization_endpoint": f"{amb.url_oauth}/authorize",
                "token_endpoint": f"{amb.url_oauth}/token",
                "client_id": amb.oauth.client_id,
            },
        )
        outro = await t3.cadastrar_terceiro(c, nome=f"terceiro-{ta.sufixo()}")
    await amb.configurar_direto()
    _, _, pessoa_id = await amb.nova_pessoa("Ana")
    for errada in (
        ta.VOLTA,
        volta_de(outro),
        volta_de(terceiro) + "?a=1",
        volta_de(terceiro) + "/mais",
        volta_de(terceiro).replace("/volta", "/ir"),
    ):
        async with amb.despachante_db.begin() as c:
            with pytest.raises(autorizacao.AutorizacaoRecusada) as recusa:
                await autorizacao.iniciar_autorizacao(c, amb.cifra, pessoa_id, terceiro, errada, papel="despachante")
        assert recusa.value.motivo == "redirect_uri_invalido", errada
    pendentes = "SELECT count(*) FROM autorizacoes_pendentes WHERE sistema_id = :s"
    assert await _contar(amb, pendentes, s=terceiro) == 0
    async with amb.despachante_db.begin() as c:
        assert await autorizacao.iniciar_autorizacao(
            c, amb.cifra, pessoa_id, terceiro, volta_de(terceiro), papel="despachante"
        )
        # O proprio segue com o endereco que tiver cadastrado (o da Fase 2, ou outro).
        for volta in (ta.VOLTA, volta_de(amb.sistema_id), "https://outro.exemplo/volta"):
            assert await autorizacao.iniciar_autorizacao(c, amb.cifra, pessoa_id, amb.sistema_id, volta)


# --- O caminho certo -----------------------------------------------------------------------------------------------


async def test_terceiro_autoriza_pela_volta_dele_e_o_proprio_pela_antiga(amb: ta.Ambiente) -> None:
    async with amb.admin.begin() as c:
        terceiro = await t3.cadastrar_terceiro(
            c,
            nome=f"terceiro-{ta.sufixo()}",
            autorizacao={
                "authorization_endpoint": f"{amb.url_oauth}/authorize",
                "token_endpoint": f"{amb.url_oauth}/token",
                "client_id": amb.oauth.client_id,
                "escopos": ["leitura"],
            },
        )
    async with amb.despachante_db.begin() as c:
        await autorizacao.guardar_segredo_cliente(c, amb.cifra, terceiro, amb.oauth.segredo)
    await amb.configurar_direto()
    nav, _, pessoa_id = await amb.nova_pessoa("Ana")
    await amb.dar_ordem(pessoa_id)
    await _dar_ordem(amb, pessoa_id, terceiro)

    # O terceiro: o redirect_uri e o dele, a volta chega pela rota nova e conclui.
    r = await nav.get(await _link(nav, terceiro))
    assert r.status_code == 303, r.text
    destino = r.headers["location"]
    assert parse_qs(urlsplit(destino).query)["redirect_uri"] == [volta_de(terceiro)]
    r = await amb.http.get(destino + "&" + urlencode({"usuario": "ana"}))
    assert r.status_code == 302, r.text
    volta = urlsplit(r.headers["location"])
    assert f"{volta.scheme}://{volta.netloc}{volta.path}" == volta_de(terceiro)
    r = await nav.get(f"{volta.path}?{volta.query}")
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/telas/autorizar?aviso=autorizacao_guardada"
    troca = [p for p in amb.oauth.pedidos if p["grant_type"] == "authorization_code"][-1]
    assert troca["redirect_uri"] == volta_de(terceiro)  # a troca repete o endereco da pagina
    guardadas = "SELECT count(*) FROM chaves_cifradas WHERE pessoa_id = :p AND sistema_id = :s"
    assert await _contar(amb, guardadas, p=pessoa_id, s=terceiro) == 2  # acesso e renovacao

    # O proprio: tudo como na Fase 2, pela volta antiga.
    r = await amb.autorizar_pela_tela(nav, "ana")
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/telas/autorizar?aviso=autorizacao_guardada"
    assert set(await amb.chaves_de(pessoa_id)) == {"acesso", "renovacao"}
    concluidas = await amb.extrato("autorizacao_concluida", pessoa_id)
    assert sorted(str(ln.sistema_id) for ln in concluidas) == sorted([str(terceiro), str(amb.sistema_id)])


async def test_tela_do_admin_mostra_a_volta_de_cada_servidor(amb: ta.Ambiente) -> None:
    async with amb.admin.begin() as c:
        terceiro = await t3.cadastrar_terceiro(c, nome=f"terceiro-{ta.sufixo()}")
    adm, _, _ = await amb.nova_pessoa("Carla Admin", admin=True)
    r = await adm.get(f"/telas/admin/sistemas/{terceiro}/acesso")
    assert r.status_code == 200, r.text
    frase = (
        "No servidor de autorização deste fornecedor, cadastre este endereço de volta. Ele é só deste servidor:"
        f" <code>{volta_de(terceiro)}</code>"
    )
    assert frase in r.text
    assert f"<code>{ta.VOLTA}</code>" not in r.text
    r = await adm.get(f"/telas/admin/sistemas/{amb.sistema_id}/acesso")
    assert r.status_code == 200, r.text
    assert f"No sistema, cadastre este endereço de volta: <code>{ta.VOLTA}</code>" in r.text
    assert "Ele é só deste servidor" not in r.text
