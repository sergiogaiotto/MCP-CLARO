"""Telas do ADMIN para parar e soltar: bloqueios e pausas por alcance, estado da entrada e da porta de
saida, desativar uma pessoa, autorizacao por pessoa de um sistema e entrega de chaves ao cofre.

- Bloqueio, pausa e portas usam as regras prontas (``regras.bloqueios`` e ``regras.estado``), que poem
  a linha no extrato. Aqui vai tambem uma linha no extrato do ADMIN, com o nome de quem fez.
- Desativar a pessoa muda o cadastro e encerra as sessoes dela; depois o despachante derruba as
  autorizacoes dela nos sistemas (``/interno/autorizacao/derrubar``).
- Chave nenhuma passa pelas telas para ficar: o segredo do cliente OAuth (``/interno/autorizacao/segredo``)
  e as chaves compartilhada, de programa e de leitura das declaracoes (``/interno/chaves``) vao direto
  ao despachante, que cifra. O valor nunca volta a pagina, nem ao log, nem ao extrato.
"""

from __future__ import annotations

import re
import uuid
from typing import Any, Final

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from mcp_claro.despachante import autorizacao
from mcp_claro.despachante.autorizacao import (
    AUTENTICACOES,
    MENSAGEM_TERCEIRO_JA_CONFIGURADO,
    ConfiguracaoAutorizacaoInvalida,
    caminho_da_volta,
)
from mcp_claro.logs import logger
from mcp_claro.regras import bloqueios, estado
from mcp_claro.regras.bloqueios import ALCANCES, CAMPOS_POR_ALCANCE, TIPOS, BloqueioInvalido, SemPermissao
from mcp_claro.telas import consultas
from mcp_claro.telas.bloqueio import ALCANCES_LEGIVEIS, parada, quem_legivel
from mcp_claro.telas.nucleo import Interrompe, Telas, tela, uuid_ou_none, ver_depois
from mcp_claro.telas.seguranca import Formulario
from mcp_claro.telas.sessoes import Sessao

log = logger("mcp_claro.telas.admin")

CONTROLE: Final = re.compile(r"[\x00-\x1f\x7f]")
VISIVEL: Final = re.compile(r"^[\x21-\x7e]{1,8192}$")
NIVEIS_LEGIVEIS: Final[dict[str, str]] = {"aberta": "aberta", "so_leitura": "so de leitura", "fechada": "fechada"}
MOTIVO_DESATIVACAO: Final = "pessoa desativada"
TERCEIRO_JA_CONFIGURADO: Final = (
    "A autorização de um servidor de terceiros não muda depois de configurada: os endereços de troca de chave"
    " ficam presos ao servidor que você revisou. Nada mudou. Para mudar, cadastre o servidor de novo."
)
"""A recusa de ``autorizacao.configurar`` num terceiro ja configurado (critica M1), com acento."""
# Tipo de chave pedido na tela -> (tipo_chave do sistema que aceita, tipo na tabela, dono no extrato).
CHAVES: Final[dict[str, tuple[str | None, str, str]]] = {
    "compartilhada": ("compartilhada", "acesso", "compartilhada"),
    "programa": ("programa", "acesso", "de programa"),
    "declaracoes": (None, "declaracoes", "de leitura das declaracoes"),
}


def _motivo(f: Formulario, erros: list[str]) -> str:
    valor = " ".join(CONTROLE.sub(" ", f.texto("motivo")).split())
    if not 5 <= len(valor) <= 300:
        erros.append("Escreva o motivo (de 5 a 300 letras). Ele fica guardado com o pedido.")
    return valor


class TelasDoAdminPortas:
    def __init__(self, t: Telas) -> None:
        self.t = t

    def rotas(self) -> list[BaseRoute]:
        def r(caminho: str, funcao: Any, *metodos: str) -> Route:
            return Route(caminho, funcao, methods=list(metodos or ("GET",)))

        return [
            r("/admin/bloqueios", self.bloqueios),
            r("/admin/bloqueios", self.ligar, "POST"),
            r("/admin/bloqueios/{bloqueio_id:uuid}/encerrar", self.encerrar, "POST"),
            r("/admin/portas", self.portas),
            r("/admin/portas/entrada", self.mudar_entrada, "POST"),
            r("/admin/portas/saida", self.mudar_saida, "POST"),
            r("/admin/pessoas", self.pessoas),
            r("/admin/pessoas/{pessoa_id:uuid}/desativar", self.desativar, "POST"),
            r("/admin/sistemas/{sistema_id:uuid}/acesso", self.acesso),
            r("/admin/sistemas/{sistema_id:uuid}/autorizacao", self.configurar_autorizacao, "POST"),
            r("/admin/sistemas/{sistema_id:uuid}/segredo", self.guardar_segredo, "POST"),
            r("/admin/sistemas/{sistema_id:uuid}/chave", self.guardar_chave, "POST"),
        ]

    # --- Bloqueios e pausas por alcance ---

    async def _pagina_bloqueios(
        self, pedido: Request, erros: list[str] | None = None, valores: dict[str, str] | None = None
    ) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        async with self.t.engine.connect() as c:
            linhas = await consultas.todos_bloqueios_ativos(c)
            pessoas = await consultas.pessoas(c)
            aplicativos = await consultas.todos_aplicativos(c)
            sistemas = await consultas.todos_sistemas(c)
        return self.t.pagina(
            pedido,
            "admin/bloqueios.html",
            {
                "paradas": [parada(linha, sessao.pessoa.id, da_pessoa=False) for linha in linhas],
                "pessoas": pessoas,
                "aplicativos": aplicativos,
                "sistemas": sistemas,
                "erros": erros or [],
                "valores": valores or {"tipo": "bloqueio", "alcance": "aplicativo"},
            },
            status=400 if erros else 200,
        )

    @tela
    async def bloqueios(self, pedido: Request) -> Response:
        return await self._pagina_bloqueios(pedido)

    @tela
    async def ligar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        erros: list[str] = []
        tipo, alcance = f.texto("tipo"), f.texto("alcance")
        if tipo not in TIPOS:
            erros.append("Escolha entre bloqueio e pausa.")
        if alcance not in ALCANCES:
            erros.append("Escolha o alcance: pessoa, aplicativo, sistema ou tudo.")
        motivo = _motivo(f, erros)
        exigidos = CAMPOS_POR_ALCANCE.get(alcance, frozenset())
        async with self.t.engine.connect() as c:
            alvos = await consultas.alvos_do_bloqueio(
                c,
                pessoa_id=uuid_ou_none(f.texto("pessoa")) if "pessoa_id" in exigidos else None,
                aplicativo_id=uuid_ou_none(f.texto("aplicativo")) if "aplicativo_id" in exigidos else None,
                sistema_id=uuid_ou_none(f.texto("sistema")) if "sistema_id" in exigidos else None,
            )
        pessoa_id, aplicativo_id, sistema_id = alvos["pessoa_id"], alvos["aplicativo_id"], alvos["sistema_id"]
        for campo, rotulo in (
            ("pessoa_id", "a pessoa"),
            ("aplicativo_id", "o aplicativo"),
            ("sistema_id", "o sistema"),
        ):
            if campo in exigidos and alvos[campo] is None:
                erros.append(f"Escolha {rotulo} da lista.")
        if erros:
            return await self._pagina_bloqueios(pedido, erros, dict(f.como_dict()))
        quem = bloqueios.quem_admin(sessao.pessoa.id)
        ids: dict[str, Any] = {"pessoa_id": pessoa_id, "aplicativo_id": aplicativo_id, "sistema_id": sistema_id}
        try:
            async with self.t.engine.begin() as c:
                if await consultas.bloqueio_igual_ativo(c, tipo, alcance, **ids) is not None:
                    return ver_depois("/telas/admin/bloqueios", "ja_valia_admin")
                feito = await bloqueios.bloquear(
                    c,
                    tipo,
                    alcance,
                    quem,
                    motivo,
                    pessoa_id=pessoa_id,
                    aplicativo_id=aplicativo_id,
                    sistema_id=sistema_id,
                )
                await self.t.extrato(
                    c,
                    "admin_bloqueio_ligado" if tipo == "bloqueio" else "admin_pausa_ligada",
                    {"quem": sessao.quem, "alcance": ALCANCES_LEGIVEIS[alcance]},
                    pessoa_id=sessao.pessoa.id,
                    aplicativo_id=aplicativo_id,
                    sistema_id=sistema_id,
                    dados={"bloqueio_id": str(feito.id), "alcance": alcance},
                )
        except (BloqueioInvalido, SemPermissao) as erro:
            return await self._pagina_bloqueios(pedido, [f"Pedido recusado: {erro}"], dict(f.como_dict()))
        log.info("telas_admin_parada_ligada", tipo=tipo, alcance=alcance)
        return ver_depois("/telas/admin/bloqueios", "bloqueio_ligado" if tipo == "bloqueio" else "pausa_ligada")

    @tela
    async def encerrar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        await self.t.formulario(pedido, sessao)
        bloqueio_id: uuid.UUID = pedido.path_params["bloqueio_id"]
        try:
            async with self.t.engine.begin() as c:
                antes = await bloqueios.por_id(c, bloqueio_id)
                if antes is None or not antes.ativo:
                    return ver_depois("/telas/admin/bloqueios", "ja_terminou")
                await bloqueios.encerrar(c, bloqueio_id, bloqueios.quem_admin(sessao.pessoa.id))
                await self.t.extrato(
                    c,
                    "admin_bloqueio_encerrado" if antes.tipo == "bloqueio" else "admin_pausa_encerrada",
                    {"quem": sessao.quem, "alcance": ALCANCES_LEGIVEIS[antes.alcance]},
                    pessoa_id=sessao.pessoa.id,
                    aplicativo_id=antes.aplicativo_id,
                    sistema_id=antes.sistema_id,
                    dados={"bloqueio_id": str(antes.id), "alcance": antes.alcance},
                )
        except (LookupError, SemPermissao) as erro:
            return self.t.erro(pedido, 403, "Pedido recusado", f"Nada mudou: {erro}")
        log.info("telas_admin_parada_encerrada", tipo=antes.tipo, alcance=antes.alcance)
        return ver_depois("/telas/admin/bloqueios", "parada_encerrada")

    # --- Estado da entrada e da porta de saida ---

    async def _pagina_portas(
        self, pedido: Request, erros: list[str] | None = None, valores: dict[str, str] | None = None
    ) -> Response:
        await self.t.exigir_admin(pedido)
        async with self.t.engine.connect() as c:
            entrada, saida = await consultas.estado_das_portas(c)
            pausados = await consultas.aplicativos_pausados(c)
        return self.t.pagina(
            pedido,
            "admin/portas.html",
            {
                "entrada": entrada,
                "saida": saida,
                "pausados": pausados,
                "entrada_por": quem_legivel(entrada.mudou_por) if entrada else "",
                "saida_por": quem_legivel(saida.mudou_por) if saida else "",
                "erros": erros or [],
                "valores": valores or {},
            },
            status=400 if erros else 200,
        )

    @tela
    async def portas(self, pedido: Request) -> Response:
        return await self._pagina_portas(pedido)

    @tela
    async def mudar_entrada(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        erros: list[str] = []
        nivel = f.texto("nivel")
        if nivel not in NIVEIS_LEGIVEIS:
            erros.append("Escolha como a entrada fica: aberta, só de leitura ou fechada.")
        motivo = _motivo(f, erros)
        if erros:
            return await self._pagina_portas(pedido, erros, dict(f.como_dict()))
        async with self.t.engine.begin() as c:
            await estado.mudar_entrada(c, nivel, bloqueios.quem_admin(sessao.pessoa.id), motivo)
            await self.t.extrato(
                c,
                "admin_entrada_mudou",
                {"nivel": NIVEIS_LEGIVEIS[nivel], "quem": sessao.quem},
                pessoa_id=sessao.pessoa.id,
                dados={"nivel": nivel},
            )
        log.info("telas_admin_entrada_mudou", nivel=nivel)
        return ver_depois("/telas/admin/portas", "entrada_mudou")

    @tela
    async def mudar_saida(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        erros: list[str] = []
        acao = f.texto("acao")
        if acao not in ("abrir", "fechar"):
            erros.append("Escolha abrir ou fechar a porta de saída.")
        motivo = _motivo(f, erros)
        if erros:
            return await self._pagina_portas(pedido, erros, dict(f.como_dict()))
        aberta = acao == "abrir"
        async with self.t.engine.begin() as c:
            await estado.mudar_saida(
                c, aberta=aberta, quem=bloqueios.quem_admin(sessao.pessoa.id), motivo=motivo, papel="entrada"
            )
            await self.t.extrato(
                c,
                "admin_saida_mudou",
                {"situacao": "aberta" if aberta else "fechada", "quem": sessao.quem},
                pessoa_id=sessao.pessoa.id,
                dados={"aberta": aberta},
            )
        log.info("telas_admin_saida_mudou", aberta=aberta)
        return ver_depois("/telas/admin/portas", "saida_mudou")

    # --- Pessoas ---

    async def _pagina_pessoas(
        self, pedido: Request, erros: list[str] | None = None, *, status: int | None = None
    ) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        async with self.t.engine.connect() as c:
            pessoas = await consultas.pessoas(c)
        return self.t.pagina(
            pedido,
            "admin/pessoas.html",
            {"pessoas": pessoas, "eu": sessao.pessoa.id, "erros": erros or []},
            status=status or (400 if erros else 200),
        )

    @tela
    async def pessoas(self, pedido: Request) -> Response:
        return await self._pagina_pessoas(pedido)

    @tela
    async def desativar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        alvo_id: uuid.UUID = pedido.path_params["pessoa_id"]
        if alvo_id == sessao.pessoa.id:
            return await self._pagina_pessoas(pedido, ["Você não pode desativar a sua própria conta por aqui."])
        if f.texto("confirmo") != "sim":
            return await self._pagina_pessoas(pedido, ["Marque a confirmação antes de desativar."])
        async with self.t.engine.begin() as c:
            alvo = await consultas.pessoa_por_id(c, alvo_id)
            if alvo is None:
                return self.t.erro(pedido, 404, "Pessoa não encontrada", "Esta pessoa não está no cadastro.")
            if await consultas.desativar_pessoa(c, alvo_id):
                await self.t.extrato(
                    c,
                    "pessoa_desativada",
                    {"quem": sessao.quem},
                    pessoa_id=alvo_id,
                    dados={"admin_id": str(sessao.pessoa.id)},
                )
                log.info("telas_admin_pessoa_desativada")
        resposta = await self.t.porta_interna(
            "/interno/autorizacao/derrubar", {"pessoa_id": str(alvo_id), "motivo": MOTIVO_DESATIVACAO}
        )
        if not resposta.ok:
            return await self._pagina_pessoas(
                pedido,
                [
                    "A pessoa foi desativada, mas o despachante não respondeu: as autorizações dela nos sistemas"
                    " ainda não foram derrubadas. Toque em “Derrubar as autorizações” de novo."
                ],
                status=502,
            )
        return ver_depois("/telas/admin/pessoas", "pessoa_desativada")

    # --- Autorizacao por pessoa e chaves de um sistema ---

    async def _sistema(self, pedido: Request) -> Any:
        async with self.t.engine.connect() as c:
            linha = await consultas.sistema(c, pedido.path_params["sistema_id"])
        if linha is None:
            raise Interrompe(self.t.erro(pedido, 404, "Sistema não encontrado", "Este sistema não está cadastrado."))
        return linha

    async def _pagina_acesso(
        self,
        pedido: Request,
        *,
        erros: list[str] | None = None,
        valores: dict[str, Any] | None = None,
        status: int | None = None,
    ) -> Response:
        await self.t.exigir_admin(pedido)
        sistema = await self._sistema(pedido)
        async with self.t.engine.connect() as c:
            config = await consultas.autorizacao_do_sistema(c, sistema.id)
            entregas = await consultas.ultimas_entregas(c, sistema.id)
            programas = await consultas.programas_cadastrados(c)
        if valores is None:
            valores = dict(config or {})
            valores["escopos"] = " ".join(valores.get("escopos") or [])
        return self.t.pagina(
            pedido,
            "admin/acesso.html",
            {
                "sistema": sistema,
                "configurada": config is not None,
                "travada": config is not None and sistema.origem != "proprio",
                "guardada": dict(config or {}),
                "terceiro_ja_configurado": TERCEIRO_JA_CONFIGURADO,
                "entregas": entregas,
                "programas": programas,
                "autenticacoes": AUTENTICACOES,
                # O endereco de volta que o ADMIN cadastra: o de um servidor de terceiros e so dele.
                "redirect_uri": self.t.p.PUBLIC_BASE_URL
                + caminho_da_volta(sistema.id if sistema.origem != "proprio" else None),
                "erros": erros or [],
                "valores": valores,
            },
            status=status or (400 if erros else 200),
        )

    @tela
    async def acesso(self, pedido: Request) -> Response:
        return await self._pagina_acesso(pedido)

    @tela
    async def configurar_autorizacao(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        sistema = await self._sistema(pedido)
        dados: dict[str, Any] = {
            "authorization_endpoint": f.texto("authorization_endpoint"),
            "token_endpoint": f.texto("token_endpoint"),
            "client_id": f.texto("client_id"),
            "escopos": [e for e in re.split(r"[\s,]+", f.texto("escopos")) if e],
            "autenticacao_cliente": f.texto("autenticacao_cliente", "client_secret_basic"),
        }
        if recurso := f.texto("recurso"):
            dados["recurso"] = recurso
        valores = dict(f.como_dict())
        try:
            async with self.t.engine.begin() as c:
                await autorizacao.configurar(c, sistema.id, dados)
                await self.t.extrato(
                    c,
                    "admin_autorizacao_configurada",
                    {"sistema": sistema.nome, "quem": sessao.quem},
                    pessoa_id=sessao.pessoa.id,
                    sistema_id=sistema.id,
                    dados={"autenticacao_cliente": dados["autenticacao_cliente"]},
                )
        except ConfiguracaoAutorizacaoInvalida as erro:
            mensagem = TERCEIRO_JA_CONFIGURADO if str(erro) == MENSAGEM_TERCEIRO_JA_CONFIGURADO else str(erro)
            return await self._pagina_acesso(pedido, erros=[mensagem], valores=valores)
        log.info("telas_admin_autorizacao_configurada")
        return ver_depois(f"/telas/admin/sistemas/{sistema.id}/acesso", "autorizacao_configurada")

    def _valor_secreto(self, f: Formulario, erros: list[str]) -> str:
        valor = f.texto("segredo")
        if not VISIVEL.match(valor):
            erros.append("Cole o valor inteiro, sem espaços nem quebras de linha (até 8192 letras).")
        return valor

    async def _entregue(
        self,
        sessao: Sessao,
        sistema: Any,
        tipo: str,
        campos: dict[str, object],
        entrega: str,
        aplicativo_id: uuid.UUID | None = None,
    ) -> None:
        """Linha no extrato do ADMIN: o que foi entregue ao cofre e quando (nunca o valor)."""
        async with self.t.engine.begin() as c:
            await self.t.extrato(
                c,
                tipo,
                {"sistema": sistema.nome, "quem": sessao.quem, **campos},
                pessoa_id=sessao.pessoa.id,
                sistema_id=sistema.id,
                aplicativo_id=aplicativo_id,
                dados={"entrega": entrega},
            )

    @tela
    async def guardar_segredo(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        sistema = await self._sistema(pedido)
        erros: list[str] = []
        if sistema.tipo_chave != "por_pessoa":
            erros.append("O segredo do MCP-CLARO só serve para sistema com uma chave por pessoa.")
        valor = self._valor_secreto(f, erros)
        if not erros:
            resposta = await self.t.porta_interna(
                "/interno/autorizacao/segredo", {"sistema_id": str(sistema.id), "segredo": valor}
            )
            if resposta.ok:
                await self._entregue(sessao, sistema, "admin_segredo_cliente_entregue", {}, "segredo_cliente")
                log.info("telas_admin_segredo_entregue")
                return ver_depois(f"/telas/admin/sistemas/{sistema.id}/acesso", "segredo_guardado")
            erros.append(
                "O despachante não respondeu. Nada foi guardado. Tente de novo."
                if resposta.status == 0
                else "O despachante recusou este segredo. Nada foi guardado. Confira o valor e tente de novo."
            )
        return await self._pagina_acesso(pedido, erros=erros)

    @tela
    async def guardar_chave(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        sistema = await self._sistema(pedido)
        erros: list[str] = []
        tipo = f.texto("tipo")
        regra = CHAVES.get(tipo)
        aplicativo_id: uuid.UUID | None = None
        if regra is None:
            erros.append("Escolha que chave você vai entregar.")
        elif regra[0] is not None and sistema.tipo_chave != regra[0]:
            erros.append("Este tipo de chave não combina com o tipo de chave do sistema.")
        elif tipo == "programa":
            aplicativo_id = uuid_ou_none(f.texto("aplicativo"))
            async with self.t.engine.connect() as c:
                programas = {p.id for p in await consultas.programas_cadastrados(c)}
            if aplicativo_id not in programas:
                erros.append("Escolha o programa da lista.")
        valor = self._valor_secreto(f, erros)
        if not erros and regra is not None:
            corpo: dict[str, Any] = {
                "sistema_id": str(sistema.id),
                "segredo": valor,
                "tipo": regra[1],
                "quem": sessao.quem,
            }
            if aplicativo_id is not None:
                corpo["aplicativo_id"] = str(aplicativo_id)
            resposta = await self.t.porta_interna("/interno/chaves", corpo)
            if resposta.ok:
                await self._entregue(
                    sessao, sistema, "admin_chave_entregue", {"dono": regra[2]}, tipo, aplicativo_id=aplicativo_id
                )
                log.info("telas_admin_chave_entregue", tipo=tipo)
                return ver_depois(f"/telas/admin/sistemas/{sistema.id}/acesso", "chave_guardada")
            erros.append(
                "O despachante não respondeu. Nada foi guardado. Tente de novo."
                if resposta.status == 0
                else "O despachante recusou esta chave. Nada foi guardado."
            )
        return await self._pagina_acesso(pedido, erros=erros)
