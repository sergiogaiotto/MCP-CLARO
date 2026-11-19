"""Rotas das telas, montadas em ``/telas`` no app da entrada.

Telas da pessoa: inicio, entrar e sair (e entrar de novo na hora, o login refeito com ``prompt=login`` e
``max_age=0``, que as acoes de maior risco sem passkey e as passkeys pedem), "Minhas ordens de servico",
"Meu extrato", "Bloquear agora" e "Pausar" (``telas/bloqueio.py``) e "Autorizar um sistema"
(``telas/autorizar.py``). Fase 2: caixa de pendencias, ficha, "Minhas acoes" e "estamos confirmando"
(``telas/acoes.py``), passkeys (``telas/passkeys.py``) e limites por aproximacao (``telas/limites.py``).
Telas do ADMIN: em ``telas/admin.py`` (cadastro) e ``telas/admin_portas.py`` (bloqueios, portas,
pessoas, autorizacao e chaves dos sistemas).
"""

from __future__ import annotations

import httpx2
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import BaseRoute, Mount, Route, Router
from starlette.staticfiles import StaticFiles
from starlette.types import Receive, Scope, Send

from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.entrada.cracha import ChavesPublicas
from mcp_claro.logs import logger
from mcp_claro.telas import admin, admin_portas, admin_programas, consultas
from mcp_claro.telas.acoes import TelasDeAcoes
from mcp_claro.telas.autorizar import TelasDeAutorizacao
from mcp_claro.telas.bloqueio import TelasDeBloqueio
from mcp_claro.telas.config import ConfigTelas
from mcp_claro.telas.limites import TelasDeLimites
from mcp_claro.telas.login import LoginRecusado, ProvedorLogin
from mcp_claro.telas.nucleo import (
    PortaInterna,
    Telas,
    numero_pagina,
    tela,
    uuid_ou_none,
    ver_depois,
    voltar_refeito,
)
from mcp_claro.telas.paginas import PASTA_ESTATICOS, criar_ambiente
from mcp_claro.telas.passkeys import TelasDePasskeys
from mcp_claro.telas.seguranca import (
    COOKIE_LOGIN,
    COOKIE_SESSAO,
    MAXIMO_FORMULARIO,
    CabecalhosSeguranca,
    apagar_cookie,
    caminho_seguro,
    iguais,
    por_cookie,
)
from mcp_claro.telas.sessoes import abrir_sessao, criar_login, encerrar_sessao, hash_sessao, usar_login

log = logger("mcp_claro.telas")


def _url_jwks(p: Parametros) -> str:
    # A mesma regra da entrada (entrada/app.py: url_jwks), sem importar o app (evita ciclo).
    if p.IDP_JWKS_URL:
        return p.IDP_JWKS_URL
    if p.IDP_KIND.value == "keycloak":
        return f"{p.IDP_ISSUER_URL}/protocol/openid-connect/certs"
    return f"{p.IDP_ISSUER_URL}/.well-known/jwks.json"


class TelasDaPessoa:
    def __init__(self, t: Telas) -> None:
        self.t = t

    # --- Inicio, entrar e sair ---

    async def inicio(self, pedido: Request) -> Response:
        await self.t.sessao(pedido)
        return self.t.pagina(pedido, "inicio.html")

    async def entrar(self, pedido: Request) -> Response:
        return await self._ao_provedor(pedido, caminho_seguro(pedido.query_params.get("voltar")))

    async def refazer(self, pedido: Request) -> Response:
        """Login refeito na hora: o provedor pede a senha de novo (``prompt=login``, ``max_age=0``) e a pessoa volta
        para a tela de onde saiu (so caminho local das telas). A sessao nova guarda o ``auth_time`` novo.

        Pedem isso as acoes de maior risco sem a passkey e mexer nas passkeys (``Telas.login_recente``)."""
        voltar = voltar_refeito(pedido.query_params.get("voltar") or "/telas")
        return await self._ao_provedor(pedido, voltar, refazer=True)

    async def _ao_provedor(self, pedido: Request, voltar: str, *, refazer: bool = False) -> Response:
        try:
            async with self.t.engine.begin() as c:
                login = await criar_login(c, voltar_para=voltar, prazo_segundos=self.t.cfg.login_segundos)
            destino = await self.t.provedor.url_login(login, refazer=refazer)
        except (httpx2.HTTPError, LoginRecusado) as erro:
            log.warning("telas_login_indisponivel", erro=type(erro).__name__)
            return self.t.erro(
                pedido, 503, "Login indisponível", "O provedor de login não respondeu. Tente de novo em instantes."
            )
        resposta = RedirectResponse(destino, status_code=303)
        # Prende o login a este navegador: a volta so vale com este cookie (contra login forcado).
        por_cookie(resposta, COOKIE_LOGIN, login.estado, max_age=self.t.cfg.login_segundos)
        return resposta

    async def volta(self, pedido: Request) -> Response:
        estado = pedido.query_params.get("state", "")
        if not iguais(estado, pedido.cookies.get(COOKIE_LOGIN)):
            log.info("telas_login_recusado", motivo="estado de outro navegador")
            return self._falha_login(pedido, "Este pedido de login não começou neste navegador.")
        async with self.t.engine.begin() as c:
            login = await usar_login(c, estado)
        if login is None:
            log.info("telas_login_recusado", motivo="estado vencido ou ja usado")
            return self._falha_login(pedido, "O pedido de login venceu ou já foi usado.")
        emissor_volta = pedido.query_params.get("iss")
        if emissor_volta is not None and emissor_volta != self.t.p.IDP_ISSUER_URL:
            log.info("telas_login_recusado", motivo="volta de outro emissor")
            return self._falha_login(pedido, "A resposta veio de outro provedor de login.")
        codigo = pedido.query_params.get("code", "")
        if pedido.query_params.get("error") or not codigo:
            log.info("telas_login_recusado", motivo="provedor nao deu o codigo")
            return self._falha_login(pedido, "O login não foi concluído no provedor.")
        try:
            identidade = await self.t.provedor.trocar_codigo(codigo, login)
        except LoginRecusado as erro:
            log.info("telas_login_recusado", motivo=erro.motivo)
            return self._falha_login(pedido, "Não conseguimos confirmar quem você é.")
        except httpx2.HTTPError as erro:
            log.warning("telas_login_indisponivel", erro=type(erro).__name__)
            return self.t.erro(
                pedido, 503, "Login indisponível", "O provedor de login não respondeu. Tente de novo em instantes."
            )
        async with self.t.engine.begin() as c:
            pessoa = await repositorio.pessoa_no_login(
                c, identidade.emissor, identidade.sujeito, identidade.nome, email=identidade.email
            )
            if pessoa.situacao != "ativa":
                log.info("telas_login_recusado", motivo="pessoa " + pessoa.situacao)
                return self._falha_login(pedido, "Seu acesso está suspenso. Fale com o ADMIN.", status=403)
            if anterior := pedido.cookies.get(COOKIE_SESSAO):
                await encerrar_sessao(c, hash_sessao(anterior))  # uma sessao por navegador
            identificador, _ = await abrir_sessao(
                c, pessoa.id, prazo_minutos=self.t.cfg.sessao_minutos, auth_time=identidade.auth_time
            )
            await self.t.extrato(c, "telas_entrada", {}, pessoa_id=pessoa.id)
        resposta = RedirectResponse(caminho_seguro(login.voltar_para), status_code=303)
        por_cookie(resposta, COOKIE_SESSAO, identificador, max_age=self.t.cfg.sessao_minutos * 60)
        apagar_cookie(resposta, COOKIE_LOGIN)
        return resposta

    def _falha_login(self, pedido: Request, mensagem: str, *, status: int = 400) -> Response:
        resposta = self.t.erro(pedido, status, "Não foi possível entrar", mensagem + " Tente entrar de novo.")
        apagar_cookie(resposta, COOKIE_LOGIN)
        return resposta

    @tela
    async def sair(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        await self.t.formulario(pedido, sessao)
        async with self.t.engine.begin() as c:
            await encerrar_sessao(c, sessao.hash_id)
            await self.t.extrato(c, "telas_saida", {}, pessoa_id=sessao.pessoa.id)
        resposta = ver_depois("/telas", "saiu")
        apagar_cookie(resposta, COOKIE_SESSAO)
        return resposta

    # --- Minhas ordens de servico ---

    async def _pagina_ordens(
        self,
        pedido: Request,
        *,
        erros: list[str] | None = None,
        valores: dict[str, str] | None = None,
        status: int = 200,
    ) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        async with self.t.engine.connect() as c:
            ativas, terminadas = await consultas.ordens_da_pessoa(c, sessao.pessoa.id)
            aplicativos = await consultas.aplicativos_habilitados(c)
            modelos = await consultas.modelos_ativos(c)
            maximo = await consultas.valor_politica(c, "WORK_ORDER_MAX_DAYS")
        for m in modelos:
            m.validade_dias = min(m.validade_dias, maximo)
        return self.t.pagina(
            pedido,
            "ordens.html",
            {
                "ativas": ativas,
                "terminadas": terminadas,
                "aplicativos": aplicativos,
                "modelos": modelos,
                "maximo_dias": maximo,
                "erros": erros or [],
                "valores": valores or {},
            },
            status=status,
        )

    @tela
    async def ordens(self, pedido: Request) -> Response:
        return await self._pagina_ordens(pedido)

    @tela
    async def dar_ordem(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        f = await self.t.formulario(pedido, sessao)
        aplicativo_id = uuid_ou_none(f.texto("aplicativo"))
        modelo_id = uuid_ou_none(f.texto("modelo"))
        erros: list[str] = []
        async with self.t.engine.begin() as c:
            aplicativos = {a.id: a for a in await consultas.aplicativos_habilitados(c)}
            modelos = {m.id: m for m in await consultas.modelos_ativos(c)}
            maximo = await consultas.valor_politica(c, "WORK_ORDER_MAX_DAYS")
            aplicativo = aplicativos.get(aplicativo_id) if aplicativo_id else None
            modelo = modelos.get(modelo_id) if modelo_id else None
            if aplicativo is None:
                erros.append("Escolha um dos aplicativos da lista.")
            if modelo is None:
                erros.append("Escolha um dos modelos de ordem de serviço.")
            limite = min(modelo.validade_dias, maximo) if modelo else maximo
            try:
                dias = int(f.texto("dias"))
            except ValueError:
                dias = 0
            if not 1 <= dias <= limite:
                erros.append(f"O prazo vai de 1 a {limite} dias.")
            repetida = (
                not erros
                and aplicativo is not None
                and modelo is not None
                and await consultas.ordem_ativa_igual(c, sessao.pessoa.id, aplicativo.id, modelo.id)
            )
            if repetida:
                erros.append("Você já tem esta ordem valendo para este aplicativo.")
            if not erros and aplicativo is not None and modelo is not None:
                ordem_id = await repositorio.conceder_ordem(
                    c,
                    pessoa_id=sessao.pessoa.id,
                    aplicativo_id=aplicativo.id,
                    modelo_id=modelo.id,
                    dias=dias,
                    max_dias=limite,
                )
                fim = await consultas.fim_da_ordem(c, ordem_id)
                await self.t.extrato(
                    c,
                    "ordem_concedida",
                    {"modelo": modelo.nome, "aplicativo": aplicativo.nome, "fim": self.t.data(fim)},
                    pessoa_id=sessao.pessoa.id,
                    aplicativo_id=aplicativo.id,
                    dados={"ordem_id": str(ordem_id), "modelo_id": str(modelo.id), "dias": dias},
                )
                return ver_depois("/telas/ordens", "ordem_dada")
        return await self._pagina_ordens(pedido, erros=erros, valores=dict(f.como_dict()), status=400)

    @tela
    async def revogar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        await self.t.formulario(pedido, sessao)
        ordem_id = pedido.path_params["ordem_id"]
        async with self.t.engine.begin() as c:
            ordem = await consultas.revogar_ordem(c, sessao.pessoa.id, ordem_id)
            if ordem is not None:
                await self.t.extrato(
                    c,
                    "ordem_revogada",
                    {"modelo": ordem.modelo, "aplicativo": ordem.aplicativo},
                    pessoa_id=sessao.pessoa.id,
                    aplicativo_id=ordem.aplicativo_id,
                    dados={"ordem_id": str(ordem.id)},
                )
        if ordem is None:
            return self.t.erro(
                pedido, 404, "Ordem não encontrada", "Esta ordem não é sua ou já tinha terminado. Nada mudou."
            )
        return ver_depois("/telas/ordens", "ordem_revogada")

    # --- Meu extrato ---

    @tela
    async def extrato(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        pagina = numero_pagina(pedido.query_params.get("pagina"))
        async with self.t.engine.connect() as c:
            linhas, ha_mais = await consultas.linhas_extrato(c, sessao.pessoa.id, pagina)
        return self.t.pagina(
            pedido,
            "extrato.html",
            {"linhas": linhas, "pagina": pagina, "ha_mais": ha_mais, "alheio": None, "base": "/telas/extrato"},
        )

    async def nao_achada(self, scope: Scope, receive: Receive, send: Send) -> None:
        pedido = Request(scope, receive)
        await self.t.sessao(pedido)
        resposta = self.t.erro(pedido, 404, "Página não encontrada", "Esta página não existe. Volte para o início.")
        await resposta(scope, receive, send)


def rotas_telas(
    p: Parametros,
    engine: AsyncEngine,
    despachante: PortaInterna,
    *,
    config: ConfigTelas | None = None,
    http_login: httpx2.AsyncClient | None = None,
) -> list[BaseRoute]:
    """Rotas das telas, para ligar no app da entrada (todas sob ``/telas``).

    ``http_login`` e o cliente HTTP que fala com o provedor de login (o mesmo da entrada serve:
    sem seguir redirecionamento). Quem o cria tambem o fecha.
    """
    cfg = config or ConfigTelas.de_parametros(p)
    problemas = cfg.problemas(p)
    if problemas:
        from mcp_claro.config.settings import ConfiguracaoInvalida

        raise ConfiguracaoInvalida(problemas)
    http = http_login or httpx2.AsyncClient(timeout=httpx2.Timeout(5.0), follow_redirects=False, trust_env=False)
    chaves = ChavesPublicas(_url_jwks(p), cache_segundos=p.IDP_JWKS_CACHE_SECONDS, http=http)
    t = Telas(p, cfg, engine, despachante, ProvedorLogin(p, cfg, http, chaves), criar_ambiente(cfg.fuso))
    pessoa = TelasDaPessoa(t)
    acoes = TelasDeAcoes(t)  # Fase 2; o inicio mostra a contagem da caixa de pendencias
    adm = admin.TelasDoAdmin(t)

    def g(caminho: str, funcao: object) -> Route:
        return Route(caminho, funcao, methods=["GET"])  # type: ignore[arg-type]

    def post(caminho: str, funcao: object) -> Route:
        return Route(caminho, funcao, methods=["POST"])  # type: ignore[arg-type]

    internas: list[BaseRoute] = [
        g("/", acoes.inicio),
        g("/login", pessoa.entrar),
        g("/login/volta", pessoa.volta),
        g("/login/refazer", pessoa.refazer),
        post("/sair", pessoa.sair),
        g("/ordens", pessoa.ordens),
        post("/ordens", pessoa.dar_ordem),
        post("/ordens/{ordem_id:uuid}/revogar", pessoa.revogar),
        g("/extrato", pessoa.extrato),
        *TelasDeBloqueio(t).rotas(),
        *TelasDeAutorizacao(t).rotas(),
        *acoes.rotas(),
        *TelasDePasskeys(t).rotas(),
        *TelasDeLimites(t).rotas(),
        *admin_programas.rotas_admin_programas(t),  # antes de adm.rotas(): /admin/programas e destas telas
        *adm.rotas(),
        *admin_portas.TelasDoAdminPortas(t).rotas(),
        Mount("/estaticos", app=StaticFiles(directory=PASTA_ESTATICOS), name="estaticos"),
    ]
    protecao = [Middleware(CabecalhosSeguranca, hsts=p.PUBLIC_BASE_URL.startswith("https://"))]
    roteador = Router(routes=internas, default=pessoa.nao_achada, redirect_slashes=False)
    return [
        Route("/telas", acoes.inicio, methods=["GET"], middleware=protecao),
        Mount("/telas", app=roteador, name="telas", middleware=protecao, max_body_size=MAXIMO_FORMULARIO),
    ]


__all__ = ["rotas_telas"]
