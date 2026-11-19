"""Passkeys (WebAuthn) da pessoa: cadastrar, listar e desativar, e a conferencia usada pela ficha e pelo limite.

- RP id e o host de ``PUBLIC_BASE_URL``; a origem e ``PUBLIC_BASE_URL`` (esquema e endereco).
- Toda cerimonia usa um desafio novo, de uso unico, guardado em ``desafios_passkey``: preso a pessoa, a
  finalidade ("cadastro", "ficha", "limite") e ao alvo (o id da ficha, ou o resumo do limite pedido), com
  prazo curto. O navegador pede o desafio na hora do toque, por POST com o token CSRF.
- A conferencia e sempre no servidor, no mesmo POST que faz a mudanca e na mesma transacao. O desafio e
  gasto antes da conferencia da assinatura: se ela nao bater, ele nao serve de novo.
- O autenticador precisa verificar a pessoa (``userVerification`` exigida) e o contador tem de subir.
- Com ``FEATURE_PASSKEY`` desligado, as telas dizem que a passkey esta desligada nesta instalacao.
- Cadastrar e desativar uma passkey pedem sempre o login refeito na hora (``Telas.login_recente``): quem so tem
  o cookie de uma sessao antiga nao troca a passkey da pessoa pela sua sem passar de novo pelo provedor.
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import BaseRoute, Route
from webauthn import (
    base64url_to_bytes,
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import options_to_json_dict, parse_authentication_credential_json, parse_client_data_json
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from mcp_claro.config.settings import Parametros
from mcp_claro.logs import logger
from mcp_claro.telas.nucleo import Telas, tela, ver_depois
from mcp_claro.telas.paginas import AVISOS
from mcp_claro.telas.sessoes import Sessao

log = logger("mcp_claro.telas.passkeys")

PRAZO_DESAFIO_SEGUNDOS: Final = 120
TEMPO_NAVEGADOR_MS: Final = 60_000
MAXIMO_CREDENCIAL: Final = 16 * 1024
MAXIMO_APELIDO: Final = 60

LOGIN_REFEITO_PASSKEY: Final = (
    "Para cadastrar ou desativar uma passkey, entre de novo agora (login refeito na hora) e tente outra vez."
    " Nada mudou."
)

AVISOS_PASSKEY: Final[dict[str, str]] = {
    "passkey_cadastrada": "Pronto. A passkey foi cadastrada. Use-a para confirmar ações de risco alto e limites.",
    "passkey_desativada": "Pronto. A passkey foi desativada. Ela não serve mais para confirmar nada.",
}


class PasskeyRecusada(Exception):
    """A passkey nao valeu. A mensagem ja vem pronta para a tela."""

    def __init__(self, mensagem: str) -> None:
        self.mensagem = mensagem
        super().__init__(mensagem)


@dataclass(frozen=True, slots=True)
class Cerimonia:
    rp_id: str
    origem: str
    nome: str = "MCP-CLARO"

    @classmethod
    def de_parametros(cls, p: Parametros) -> Cerimonia:
        partes = urlsplit(p.PUBLIC_BASE_URL)
        return cls(rp_id=partes.hostname or "", origem=f"{partes.scheme}://{partes.netloc}")


# --- Desafios de uso unico ---


async def novo_desafio(c: AsyncConnection, pessoa_id: uuid.UUID, finalidade: str, alvo: str) -> bytes:
    desafio = secrets.token_bytes(32)
    await c.execute(
        text(
            "INSERT INTO desafios_passkey (desafio, pessoa_id, finalidade, alvo, expira_em)"
            " VALUES (:d, :p, :f, :a, now() + make_interval(secs => :s))"
        ),
        {"d": desafio, "p": pessoa_id, "f": finalidade, "a": alvo, "s": PRAZO_DESAFIO_SEGUNDOS},
    )
    return desafio


async def gastar_desafio(c: AsyncConnection, desafio: bytes, pessoa_id: uuid.UUID, finalidade: str, alvo: str) -> bool:
    """Gasta o desafio de uma vez: outro uso, depois do prazo, de outra pessoa ou para outro alvo nao acha nada."""
    gasto = (
        await c.execute(
            text(
                "UPDATE desafios_passkey SET usado_em = now() WHERE desafio = :d AND pessoa_id = :p"
                " AND finalidade = :f AND alvo = :a AND usado_em IS NULL AND expira_em > now() RETURNING desafio"
            ),
            {"d": desafio, "p": pessoa_id, "f": finalidade, "a": alvo},
        )
    ).first()
    return gasto is not None


# --- Passkeys da pessoa ---


async def da_pessoa(c: AsyncConnection, pessoa_id: uuid.UUID, *, so_ativas: bool = False) -> Sequence[Row[Any]]:
    return (
        await c.execute(
            text(
                "SELECT id, credencial_id, apelido, criada_em, ultimo_uso, ativa FROM passkeys"
                " WHERE pessoa_id = :p AND (ativa OR NOT :so) ORDER BY ativa DESC, criada_em DESC"
            ),
            {"p": pessoa_id, "so": so_ativas},
        )
    ).all()


async def tem_ativa(c: AsyncConnection, pessoa_id: uuid.UUID) -> bool:
    linha = (
        await c.execute(text("SELECT 1 FROM passkeys WHERE pessoa_id = :p AND ativa LIMIT 1"), {"p": pessoa_id})
    ).first()
    return linha is not None


def _ler(credencial: str) -> dict[str, Any]:
    if not credencial or len(credencial) > MAXIMO_CREDENCIAL:
        raise PasskeyRecusada("A resposta da passkey não chegou. Tente de novo.")
    try:
        dados = json.loads(credencial)
    except ValueError as erro:
        raise PasskeyRecusada("A resposta da passkey veio incompleta. Tente de novo.") from erro
    if not isinstance(dados, dict):
        raise PasskeyRecusada("A resposta da passkey veio incompleta. Tente de novo.")
    return dados


def _desafio_da_resposta(dados: dict[str, Any]) -> bytes:
    resposta = dados.get("response")
    bruto = resposta.get("clientDataJSON") if isinstance(resposta, dict) else None
    if not isinstance(bruto, str):
        raise PasskeyRecusada("A resposta da passkey veio incompleta. Tente de novo.")
    try:
        return bytes(parse_client_data_json(base64url_to_bytes(bruto)).challenge)
    except (ValueError, WebAuthnException) as erro:
        raise PasskeyRecusada("A resposta da passkey veio incompleta. Tente de novo.") from erro


async def opcoes_cadastro(c: AsyncConnection, cer: Cerimonia, sessao: Sessao) -> dict[str, Any]:
    pessoa_id = sessao.pessoa.id
    ja = await da_pessoa(c, pessoa_id)
    desafio = await novo_desafio(c, pessoa_id, "cadastro", "cadastro")
    opcoes = generate_registration_options(
        rp_id=cer.rp_id,
        rp_name=cer.nome,
        user_id=pessoa_id.bytes,
        user_name=sessao.quem,
        user_display_name=sessao.quem,
        challenge=desafio,
        timeout=TEMPO_NAVEGADOR_MS,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED,
        ),
        exclude_credentials=[PublicKeyCredentialDescriptor(id=bytes(p.credencial_id)) for p in ja],
    )
    return dict(options_to_json_dict(opcoes))


async def cadastrar(
    c: AsyncConnection, cer: Cerimonia, pessoa_id: uuid.UUID, credencial: str, apelido: str
) -> uuid.UUID | str:
    """Confere a cerimonia de registro e guarda a passkey. Devolve o id, ou a frase da recusa (desafio ja gasto)."""
    try:
        dados = _ler(credencial)
        desafio = _desafio_da_resposta(dados)
    except PasskeyRecusada as erro:
        return erro.mensagem
    if not await gastar_desafio(c, desafio, pessoa_id, "cadastro", "cadastro"):
        return "Este pedido de passkey venceu ou já foi usado. Tente de novo."
    try:
        feito = verify_registration_response(
            credential=dados,
            expected_challenge=desafio,
            expected_rp_id=cer.rp_id,
            expected_origin=cer.origem,
            require_user_verification=True,
        )
    except WebAuthnException, ValueError, TypeError, KeyError:
        log.info("passkey_cadastro_recusado")
        return "A passkey não foi conferida. Nada foi cadastrado. Tente de novo."
    novo = (
        await c.execute(
            text(
                "INSERT INTO passkeys (pessoa_id, credencial_id, chave_publica, contador, apelido)"
                " VALUES (:p, :c, :k, :n, :a) ON CONFLICT (credencial_id) DO NOTHING RETURNING id"
            ),
            {
                "p": pessoa_id,
                "c": bytes(feito.credential_id),
                "k": bytes(feito.credential_public_key),
                "n": int(feito.sign_count),
                "a": apelido[:MAXIMO_APELIDO] or None,
            },
        )
    ).scalar_one_or_none()
    if novo is None:
        return "Esta passkey já estava cadastrada. Nada mudou."
    return uuid.UUID(str(novo))


async def opcoes_autenticacao(
    c: AsyncConnection, cer: Cerimonia, pessoa_id: uuid.UUID, finalidade: str, alvo: str
) -> dict[str, Any]:
    """Opcoes para ``navigator.credentials.get``, com desafio novo. Levanta ``PasskeyRecusada`` sem passkey ativa."""
    ativas = await da_pessoa(c, pessoa_id, so_ativas=True)
    if not ativas:
        raise PasskeyRecusada("Você ainda não tem passkey cadastrada. Cadastre uma em Minhas passkeys.")
    desafio = await novo_desafio(c, pessoa_id, finalidade, alvo)
    opcoes = generate_authentication_options(
        rp_id=cer.rp_id,
        challenge=desafio,
        timeout=TEMPO_NAVEGADOR_MS,
        allow_credentials=[PublicKeyCredentialDescriptor(id=bytes(p.credencial_id)) for p in ativas],
        user_verification=UserVerificationRequirement.REQUIRED,
    )
    return dict(options_to_json_dict(opcoes))


async def conferir(
    c: AsyncConnection, cer: Cerimonia, pessoa_id: uuid.UUID, finalidade: str, alvo: str, credencial: str
) -> str | None:
    """Gasta o desafio e confere a assinatura, na transacao de quem chama. ``None``: valeu; texto: a recusa.

    Quem chama segue a transacao ate o fim mesmo na recusa, para o desafio ficar gasto."""
    try:
        dados = _ler(credencial)
        desafio = _desafio_da_resposta(dados)
        lida = parse_authentication_credential_json(dados)
    except PasskeyRecusada as erro:
        return erro.mensagem
    except ValueError, WebAuthnException:
        return "A resposta da passkey veio incompleta. Tente de novo."
    if not await gastar_desafio(c, desafio, pessoa_id, finalidade, alvo):
        log.info("passkey_desafio_recusado", finalidade=finalidade)
        return "Este pedido de passkey venceu, já foi usado ou era para outra coisa. Nada seguiu. Tente de novo."
    chave = (
        await c.execute(
            text(
                "SELECT id, chave_publica, contador FROM passkeys WHERE credencial_id = :c AND pessoa_id = :p AND ativa"
                " FOR UPDATE"
            ),
            {"c": bytes(lida.raw_id), "p": pessoa_id},
        )
    ).first()
    if chave is None:
        log.info("passkey_desconhecida", finalidade=finalidade)
        return "Esta passkey não está cadastrada para você, ou foi desativada. Nada seguiu."
    try:
        conferida = verify_authentication_response(
            credential=lida,
            expected_challenge=desafio,
            expected_rp_id=cer.rp_id,
            expected_origin=cer.origem,
            credential_public_key=bytes(chave.chave_publica),
            credential_current_sign_count=int(chave.contador),
            require_user_verification=True,
        )
    except WebAuthnException, ValueError, TypeError, KeyError:
        log.info("passkey_assinatura_recusada", finalidade=finalidade)
        return "A passkey não foi conferida. Nada seguiu. Tente de novo."
    await c.execute(
        text("UPDATE passkeys SET contador = :n, ultimo_uso = now() WHERE id = :i"),
        {"n": int(conferida.new_sign_count), "i": chave.id},
    )
    return None


def resposta_desafio(opcoes: dict[str, Any] | None, erro: str | None = None, *, status: int = 400) -> JSONResponse:
    """O que o navegador recebe ao pedir um desafio: as opcoes, ou a frase da recusa."""
    if opcoes is None:
        return JSONResponse({"erro": erro or "Não deu para pedir a passkey agora. Tente de novo."}, status_code=status)
    return JSONResponse({"publicKey": opcoes})


# --- Telas: Minhas passkeys ---


class TelasDePasskeys:
    def __init__(self, t: Telas) -> None:
        self.t = t
        self.cer = Cerimonia.de_parametros(t.p)

    def rotas(self) -> list[BaseRoute]:
        return [
            Route("/passkeys", self.lista, methods=["GET"]),
            Route("/passkeys", self.cadastrar, methods=["POST"]),
            Route("/passkeys/desafio", self.desafio, methods=["POST"]),
            Route("/passkeys/{passkey_id:uuid}/desativar", self.desativar, methods=["POST"]),
        ]

    async def _pagina(self, pedido: Request, *, erros: list[str] | None = None, status: int = 200) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        async with self.t.engine.connect() as c:
            linhas = await da_pessoa(c, sessao.pessoa.id)
        codigo = pedido.query_params.get("aviso", "")
        return self.t.pagina(
            pedido,
            "acoes/passkeys.html",
            {
                "passkeys": linhas,
                "ligada": self.t.p.FEATURE_PASSKEY,
                "refazer": self.t.login_refeito(pedido, sessao, "/telas/passkeys"),
                "pede": "Cadastrar ou desativar uma passkey pede um login refeito na hora, como cuidado a mais.",
                "aviso": AVISOS_PASSKEY.get(codigo) or AVISOS.get(codigo),
                "erros": erros or [],
            },
            status=status,
        )

    @tela
    async def lista(self, pedido: Request) -> Response:
        return await self._pagina(pedido)

    @tela
    async def desafio(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        await self.t.formulario(pedido, sessao)
        if not self.t.p.FEATURE_PASSKEY:
            return resposta_desafio(None, "A passkey está desligada nesta instalação.", status=409)
        if not self.t.login_recente(sessao):
            log.info("passkey_pede_login_refeito")
            return resposta_desafio(None, LOGIN_REFEITO_PASSKEY, status=403)
        async with self.t.engine.begin() as c:
            opcoes = await opcoes_cadastro(c, self.cer, sessao)
        return resposta_desafio(opcoes)

    @tela
    async def cadastrar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        f = await self.t.formulario(pedido, sessao)
        if not self.t.p.FEATURE_PASSKEY:
            return await self._pagina(pedido, erros=["A passkey está desligada nesta instalação."], status=409)
        if not self.t.login_recente(sessao):
            log.info("passkey_pede_login_refeito")
            return await self._pagina(pedido, erros=[LOGIN_REFEITO_PASSKEY], status=403)
        async with self.t.engine.begin() as c:
            feito = await cadastrar(c, self.cer, sessao.pessoa.id, f.texto("credencial"), f.texto("apelido"))
            if isinstance(feito, uuid.UUID):
                await self.t.extrato(
                    c, "passkey_cadastrada", {}, pessoa_id=sessao.pessoa.id, dados={"passkey": str(feito)}
                )
        if isinstance(feito, str):
            return await self._pagina(pedido, erros=[feito], status=400)
        log.info("passkey_cadastrada")
        return ver_depois("/telas/passkeys", "passkey_cadastrada")

    @tela
    async def desativar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        await self.t.formulario(pedido, sessao)
        passkey_id: uuid.UUID = pedido.path_params["passkey_id"]
        if not self.t.login_recente(sessao):
            log.info("passkey_pede_login_refeito")
            return await self._pagina(pedido, erros=[LOGIN_REFEITO_PASSKEY], status=403)
        async with self.t.engine.begin() as c:
            mudou = (
                await c.execute(
                    text("UPDATE passkeys SET ativa = false WHERE id = :i AND pessoa_id = :p AND ativa RETURNING id"),
                    {"i": passkey_id, "p": sessao.pessoa.id},
                )
            ).first()
            if mudou is not None:
                await self.t.extrato(
                    c, "passkey_desativada", {}, pessoa_id=sessao.pessoa.id, dados={"passkey": str(passkey_id)}
                )
        if mudou is None:
            return self.t.erro(
                pedido, 404, "Passkey não encontrada", "Esta passkey não é sua ou já estava desativada. Nada mudou."
            )
        log.info("passkey_desativada")
        return ver_depois("/telas/passkeys", "passkey_desativada")
