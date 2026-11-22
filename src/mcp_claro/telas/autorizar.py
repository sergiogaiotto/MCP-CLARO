"""Tela da pessoa: "Autorizar um sistema".

1. A lista mostra os sistemas de chave por pessoa que entram nas ordens valendo da pessoa.
2. O botao e um link comum, nao um formulario: a CSP das telas tem ``form-action 'self'``, e o navegador
   aplica ``form-action`` tambem ao redirecionamento que segue um formulario. O link leva uma ficha
   amarrada a sessao e ao sistema (HMAC com o token CSRF da sessao): outro site nao consegue comecar
   uma autorizacao pela pessoa. A entrada pede a URL ao despachante (``/interno/autorizacao/iniciar``)
   e responde 303 com ``Location`` para a pagina do sistema.
3. A volta passa estado e codigo, com a pessoa da sessao, ao despachante (``/interno/autorizacao/concluir``).
   O endereco de volta, que precisa estar no ``redirect_uri`` cadastrado no servidor de autorizacao, e
   ``/telas/autorizar/volta`` para os sistemas proprios (o da Fase 2) e ``/telas/autorizar/<id>/volta`` para cada
   servidor de terceiros; por esse, a entrada manda tambem o ``sistema_id`` do caminho, e o despachante recusa a
   volta que nao e do sistema da pendencia (mistura de servidores de autorizacao).
   O estado so e conferido la (uso unico, prazo, mesma pessoa). A entrada nunca ve a chave e nao guarda
   o codigo. Deu certo: volta por GET a lista, com o aviso; recusa: a frase legivel, sem nada guardado.
4. A pessoa tambem pode retirar a propria autorizacao (``/interno/autorizacao/derrubar`` com o sistema).
"""

from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any, Final
from urllib.parse import urlsplit

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import BaseRoute, Route

from mcp_claro.despachante.autorizacao import (
    CAMINHO_VOLTA_PROPRIOS,
    ConfiguracaoAutorizacaoInvalida,
    caminho_da_volta,
    conferir_endereco,
)
from mcp_claro.logs import logger
from mcp_claro.telas import consultas
from mcp_claro.telas.nucleo import RespostaInterna, Telas, tela, ver_depois
from mcp_claro.telas.seguranca import iguais
from mcp_claro.telas.sessoes import Sessao

log = logger("mcp_claro.telas.autorizar")

CAMINHO_VOLTA: Final = CAMINHO_VOLTA_PROPRIOS
"""A volta dos sistemas proprios. Um servidor de terceiros volta pelo dele (``caminho_da_volta``)."""
MOTIVO_RETIRADA: Final = "a pessoa retirou a autorizacao"

# Recusas do despachante, em frases curtas para a pessoa. Fora daqui, vale a frase que ele mandou.
MENSAGENS: Final[dict[str, str]] = {
    "estado_ja_usado": "Este pedido de autorização já foi usado. Nada mais foi guardado. Comece de novo por aqui.",
    "estado_desconhecido": "Não achamos este pedido de autorização. Nada foi guardado. Comece de novo por aqui.",
    "estado_de_outra_pessoa": "Este pedido de autorização é de outra pessoa. Nada foi guardado.",
    "estado_de_outro_sistema": (
        "Este pedido de autorização não é deste sistema. Nada foi guardado. Comece de novo por aqui."
    ),
    "estado_vencido": "O prazo para autorizar acabou. Nada foi guardado. Comece de novo por aqui.",
    "pedido_invalido": "O pedido de autorização veio incompleto. Nada foi guardado. Comece de novo por aqui.",
    "sistema_nao_autorizou": "O sistema não autorizou. Nada foi guardado. Se quiser, tente de novo.",
    "troca_recusada": "O sistema recusou a autorização. Nada foi guardado. Tente autorizar de novo.",
    "sistema_indisponivel": "O sistema não está disponível agora. Nada foi guardado. Tente de novo mais tarde.",
    "pessoa_inativa": "Sua conta não está ativa no MCP-CLARO. Fale com o ADMIN.",
    "sem_segredo_cliente": "O ADMIN ainda não terminou de ligar este sistema. Fale com o ADMIN.",
    "configuracao_invalida": "A autorização deste sistema está mal configurada. Fale com o ADMIN.",
    "sem_autorizacao_por_pessoa": "Este sistema não pede autorização de cada pessoa.",
    "sistema_desconhecido": "Este sistema não está cadastrado.",
    "terceiros_desligados": (
        "Servidores de terceiros ainda estão desligados neste MCP-CLARO. Só pessoas de teste os autorizam. Nada foi"
        " guardado."
    ),
}
SEM_RESPOSTA: Final = "O MCP-CLARO não conseguiu falar com o despachante agora. Nada foi guardado. Tente de novo."
SEM_RESPOSTA_RETIRAR: Final = "O MCP-CLARO não conseguiu falar com o despachante agora. Nada mudou. Tente de novo."


def ficha(csrf: str, sistema_id: uuid.UUID) -> str:
    """Ficha do link "Autorizar": so a sessao que mostrou a pagina sabe montar."""
    return hmac.new(csrf.encode(), f"mcp-claro|autorizar|{sistema_id}".encode(), hashlib.sha256).hexdigest()


def mensagem_da_recusa(resposta: RespostaInterna) -> str:
    if resposta.status == 0:
        return SEM_RESPOSTA
    return MENSAGENS.get(resposta.motivo) or resposta.mensagem or "A autorização foi recusada. Nada foi guardado."


def _url_do_sistema(valor: Any) -> str | None:
    """So segue para um endereco https (http so local, em teste) sem usuario, senha nem fragmento."""
    if not isinstance(valor, str) or len(valor) > 8192:
        return None
    try:
        conferir_endereco(valor, "da pagina do sistema")
    except ConfiguracaoAutorizacaoInvalida:
        return None
    if any(ord(c) < 0x21 for c in valor) or not urlsplit(valor).netloc:
        return None
    return valor


class TelasDeAutorizacao:
    def __init__(self, t: Telas) -> None:
        self.t = t

    def rotas(self) -> list[BaseRoute]:
        return [
            Route("/autorizar", self.lista, methods=["GET"]),
            Route("/autorizar/volta", self.volta, methods=["GET"]),
            Route("/autorizar/{sistema_id:uuid}/volta", self.volta_do_sistema, methods=["GET"]),
            Route("/autorizar/{sistema_id:uuid}/ir", self.ir, methods=["GET"]),
            Route("/autorizar/{sistema_id:uuid}/retirar", self.retirar, methods=["POST"]),
        ]

    def redirect_uri(self, sistema: Any) -> str:
        """O endereco de volta do sistema: o de cada servidor de terceiros e so dele; os proprios usam o da Fase 2."""
        return self.t.p.PUBLIC_BASE_URL + caminho_da_volta(sistema.id if sistema.origem != "proprio" else None)

    @tela
    async def lista(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        async with self.t.engine.connect() as c:
            sistemas = await consultas.sistemas_para_autorizar(c, sessao.pessoa.id)
        return self.t.pagina(
            pedido,
            "autorizar.html",
            {"sistemas": sistemas, "fichas": {s.id: ficha(sessao.csrf, s.id) for s in sistemas}},
        )

    async def _sistema_da_pessoa(self, sessao: Sessao, sistema_id: uuid.UUID) -> Any:
        async with self.t.engine.connect() as c:
            achados = await consultas.sistemas_para_autorizar(c, sessao.pessoa.id, sistema_id)
        return achados[0] if achados else None

    @tela
    async def ir(self, pedido: Request) -> Response:
        """Pede ao despachante a pagina de autorizacao do sistema e manda a pessoa para la (303)."""
        sessao = await self.t.sessao(pedido)
        if sessao is None:
            return RedirectResponse("/telas/login?voltar=%2Ftelas%2Fautorizar", status_code=303)
        sistema_id: uuid.UUID = pedido.path_params["sistema_id"]
        if not iguais(pedido.query_params.get("ficha"), ficha(sessao.csrf, sistema_id)):
            log.info("telas_csrf_recusado", motivo="ficha da autorizacao")
            return self.t.erro(
                pedido,
                403,
                "Pedido recusado",
                "Este pedido não veio desta tela. Por segurança, nada foi feito. Volte e tente de novo.",
            )
        sistema = await self._sistema_da_pessoa(sessao, sistema_id)
        if sistema is None:
            return self.t.erro(
                pedido,
                404,
                "Sistema fora das suas ordens",
                "Nenhuma ordem sua valendo usa este sistema. Dê uma ordem antes, em Minhas ordens de serviço.",
            )
        if not sistema.configurado:
            return self.t.erro(
                pedido, 409, "Ainda não dá para autorizar", "O ADMIN ainda não ligou a autorização deste sistema."
            )
        resposta = await self.t.porta_interna(
            "/interno/autorizacao/iniciar",
            {
                "pessoa_id": str(sessao.pessoa.id),
                "sistema_id": str(sistema_id),
                "redirect_uri": self.redirect_uri(sistema),
            },
        )
        destino = _url_do_sistema(resposta.dados.get("url")) if resposta.ok else None
        if destino is None:
            status = 403 if 400 <= resposta.status < 500 else 502
            return self.t.erro(pedido, status, "Não foi possível autorizar agora", mensagem_da_recusa(resposta))
        log.info("telas_autorizacao_iniciada")
        return RedirectResponse(destino, status_code=303)

    @tela
    async def volta(self, pedido: Request) -> Response:
        """A volta dos sistemas proprios (o endereco da Fase 2): sem ``sistema_id``."""
        return await self._volta(pedido, None)

    @tela
    async def volta_do_sistema(self, pedido: Request) -> Response:
        """A volta de um servidor de terceiros, pelo endereco so dele: o ``sistema_id`` do caminho vai junto."""
        sistema_id: uuid.UUID = pedido.path_params["sistema_id"]
        return await self._volta(pedido, sistema_id)

    async def _volta(self, pedido: Request, sistema_id: uuid.UUID | None) -> Response:
        """A volta da pagina do sistema: estado e codigo vao ao despachante com a pessoa desta sessao e, pelo endereco
        de um servidor de terceiros, o sistema desse endereco (o despachante confere com o da pendencia)."""
        sessao = await self.t.sessao(pedido)
        if sessao is None:
            # Sem sessao nao ha pessoa a conferir; o codigo nao vai a lugar nenhum (nem ao login).
            return self.t.erro(
                pedido,
                403,
                "Sua sessão terminou",
                "Entre de novo e comece a autorização outra vez. Nada foi guardado.",
            )
        q = pedido.query_params
        estado = q.get("state", "")
        codigo = q.get("code", "")
        erro_oauth = q.get("error", "")
        if not estado or len(estado) > 256:
            return self.t.erro(pedido, 400, "Não foi possível autorizar", MENSAGENS["pedido_invalido"])
        corpo: dict[str, Any] = {"estado": estado, "pessoa_id": str(sessao.pessoa.id)}
        if sistema_id is not None:
            corpo["sistema_id"] = str(sistema_id)
        if erro_oauth or not codigo:
            corpo["erro"] = erro_oauth[:64] or "sem_codigo"
        else:
            corpo["codigo"] = codigo
        resposta = await self.t.porta_interna("/interno/autorizacao/concluir", corpo)
        if resposta.ok and resposta.dados.get("concluida") is True:
            log.info("telas_autorizacao_concluida")
            # Volta por GET: recarregar a pagina nao reenvia o codigo, e ele sai da barra de endereco.
            return ver_depois("/telas/autorizar", "autorizacao_guardada")
        status = 502 if resposta.status == 0 else 403
        return self.t.pagina(pedido, "autorizar_volta.html", {"erro": mensagem_da_recusa(resposta)}, status=status)

    @tela
    async def retirar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        await self.t.formulario(pedido, sessao)
        sistema_id: uuid.UUID = pedido.path_params["sistema_id"]
        async with self.t.engine.connect() as c:
            sistema = await consultas.sistema(c, sistema_id)
        if sistema is None or sistema.tipo_chave != "por_pessoa":
            return self.t.erro(
                pedido, 404, "Sistema não encontrado", "Este sistema não pede autorização de cada pessoa."
            )
        resposta = await self.t.porta_interna(
            "/interno/autorizacao/derrubar",
            {"pessoa_id": str(sessao.pessoa.id), "sistema_id": str(sistema_id), "motivo": MOTIVO_RETIRADA},
        )
        if not resposta.ok:
            return self.t.erro(pedido, 502, "Não foi possível retirar agora", SEM_RESPOSTA_RETIRAR)
        retiradas = resposta.dados.get("derrubadas")
        return ver_depois("/telas/autorizar", "autorizacao_retirada" if retiradas else "nada_a_retirar")
