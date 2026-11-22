"""Autorizacao da pessoa no sistema (OAuth do sistema, com PKCE S256).

O gateway e cliente OAuth de cada sistema com ``tipo_chave = 'por_pessoa'``. O caminho:

1. A tela chama ``iniciar_autorizacao``: cria ``autorizacoes_pendentes`` com um estado aleatorio de
   uso unico e o verificador PKCE cifrado pelo cofre (junto com o ``redirect_uri``, que a troca
   precisa repetir), e devolve o endereco da pagina de autorizacao do sistema.
2. A pessoa autoriza na pagina do sistema; a volta chega as telas.
3. As telas passam estado, codigo e a pessoa da sessao ao despachante pela porta interna
   (``POST /interno/autorizacao/concluir``). O despachante (``concluir_autorizacao``) gasta o estado
   (uso unico, prazo, mesma pessoa), troca o codigo no ``token_endpoint`` e grava as chaves de acesso
   e de renovacao cifradas pelo Transit. A entrada nunca ve as chaves.

Endereco de volta (``caminho_da_volta``): os sistemas proprios voltam por ``/telas/autorizar/volta``, o endereco
cadastrado desde a Fase 2; cada servidor de terceiros volta por um endereco so dele,
``/telas/autorizar/<id do sistema>/volta``, e as telas dizem ao despachante por qual endereco a volta chegou
(``sistema_id``). Isso fecha a mistura de servidores de autorizacao (RFC 9700, 4.4): um terceiro que mande a pessoa a
pagina de autorizacao de outro sistema, com o estado dele, recebe a volta pelo endereco do outro sistema, e o
despachante recusa (``estado_de_outro_sistema``) antes de abrir o segredo do cliente, decifrar o verificador ou trocar
o codigo. Nada vai a ninguem. Supoe que o servidor de autorizacao de cada sistema so aceita o endereco de volta que
tem cadastrado (RFC 6749, 3.1.2). O ``iss`` na volta (RFC 9207) ainda nao e conferido.

Configuracao em ``sistemas.autorizacao`` (jsonb), conferida por ``ConfigAutorizacao.de_json``::

    {"authorization_endpoint": "https://...", "token_endpoint": "https://...",
     "client_id": "...", "escopos": ["..."],
     "autenticacao_cliente": "client_secret_basic" | "client_secret_post" | "none",
     "recurso": "https://..."}   # opcional; padrao: o endereco MCP do sistema (RFC 8707)

O segredo do cliente nao fica no jsonb: fica em ``chaves_cifradas`` do proprio sistema, sem pessoa e
sem aplicativo, tipo ``acesso``, cifrado com contexto proprio (``|cliente-oauth``). Assim, mesmo que
o sistema mude para chave compartilhada, essa linha nao decifra como chave compartilhada e nunca vai
ao sistema como ``Bearer``.

Todo HTTP de saida vai pelo ``httpx2``, sem seguir redirecionamento, sem proxy do ambiente e com resposta de
tamanho limitado. A troca inteira (abrir a conexao, o POST e ler o corpo) tem prazo total ``PRAZO_TROCA``; o
prazo de leitura do httpx sozinho nao basta, porque vale para cada pedaco lido. A renovacao espera
a trava de outra renovacao do mesmo par no maximo ``ESPERA_TRAVA_RENOVACAO`` (``travar_com_prazo``). Nada de chave,
codigo ou verificador vai ao log ou ao extrato.

Servidor de terceiros (Fase 3):

- a troca do codigo e a renovacao ficam no despachante (codigo nosso, pequeno, com teto de tamanho e o prazo
  total ``PRAZO_TROCA``), mas o ``token_endpoint``
  passa antes pela mesma conferencia do endereco combinado da sala (https, sem usuario nem fragmento, todo IP
  publico: ``ligacao.cliente.conferir_endereco_terceiro``). Endereco recusado: nada sai e o extrato ganha
  ``sala_endereco_recusado``;
- com a chave da Fase 3 desligada, so pessoa de teste autoriza um terceiro (``terceiros_desligados``);
- a configuracao de um terceiro so passa de vazia a preenchida: depois disso ``configurar`` recusa ("Para mudar,
  cadastre o servidor de novo."), como a guarda do banco (critica M1);
- o ``redirect_uri`` de um terceiro precisa ser o endereco de volta dele (``iniciar_autorizacao`` recusa outro), e a
  volta de um terceiro so e aceita por esse endereco.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import re
import secrets
import ssl
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal, get_args
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import anyio
import httpx2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from mcp_claro import __version__
from mcp_claro.despachante import chaves
from mcp_claro.despachante.chaves import Cifra
from mcp_claro.extrato import registrar
from mcp_claro.ligacao import cliente
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.regras import bloqueios

log = logger("mcp_claro.despachante.autorizacao")

AutenticacaoCliente = Literal["client_secret_basic", "client_secret_post", "none"]
AUTENTICACOES: Final[tuple[str, ...]] = get_args(AutenticacaoCliente)

PRAZO_PENDENTE: Final = timedelta(minutes=10)
"""Prazo para a pessoa voltar da pagina do sistema com o codigo."""

TAMANHO_MAXIMO_RESPOSTA: Final = 64 * 1024
PRAZO_TROCA: Final = 15.0
"""Segundos para a troca inteira no ``token_endpoint`` (abrir a conexao, o POST e ler o corpo). Num terceiro, a
conferencia do endereco vem antes, com o prazo dela (``cliente.PRAZO_CONEXAO``)."""
ESPERA_TRAVA_RENOVACAO: Final = 3.0
"""Segundos que uma renovacao espera a trava do par (pessoa, sistema) segura por outra renovacao."""
CAMINHO_VOLTA_PROPRIOS: Final = "/telas/autorizar/volta"
"""Endereco de volta dos sistemas proprios (o cadastrado nos servidores de autorizacao desde a Fase 2)."""
ESTADO_DE_OUTRO_SISTEMA: Final = "estado_de_outro_sistema"
MENSAGEM_ESTADO_DE_OUTRO_SISTEMA: Final = "Este pedido de autorizacao e de outro sistema. Nada foi guardado."
VALIDADE_MAXIMA: Final = timedelta(days=366)
_CAMPOS_CONFIG: Final = frozenset(
    {"authorization_endpoint", "token_endpoint", "client_id", "escopos", "autenticacao_cliente", "recurso"}
)
_PARAMETROS_RESERVADOS: Final = frozenset(
    {"response_type", "client_id", "redirect_uri", "state", "code_challenge", "code_challenge_method", "scope"}
)
_ESCOPO: Final = re.compile(r"^[\x21\x23-\x5b\x5d-\x7e]{1,128}$")  # RFC 6749 3.3
_VISIVEL: Final = re.compile(r"^[\x21-\x7e]{1,8192}$")  # vai num cabecalho: sem espaco nem quebra de linha
_ERROS_OAUTH: Final = frozenset(
    {
        "invalid_request",
        "invalid_client",
        "invalid_grant",
        "unauthorized_client",
        "unsupported_grant_type",
        "invalid_scope",
        "invalid_target",
        "access_denied",
        "unsupported_response_type",
        "server_error",
        "temporarily_unavailable",
    }
)


class ConfiguracaoAutorizacaoInvalida(ValueError):
    """A configuracao de autorizacao do sistema nao serve; a mensagem e legivel para o ADMIN."""


class AutorizacaoRecusada(Exception):
    """Recusa legivel de um passo da autorizacao. ``motivo`` e um codigo curto; ``mensagem``, para a pessoa."""

    def __init__(self, motivo: str, mensagem: str) -> None:
        self.motivo = motivo
        self.mensagem = mensagem
        super().__init__(mensagem)


class TrocaRecusada(Exception):
    """O servidor de autorizacao do sistema recusou a troca (resposta 4xx ou fora do padrao)."""

    def __init__(self, codigo: str) -> None:
        self.codigo = codigo
        super().__init__(f"troca recusada: {codigo}")


class TrocaIndisponivel(Exception):
    """O servidor de autorizacao nao respondeu (rede, prazo, 5xx, 429): vale tentar mais tarde."""


ENDERECO_RECUSADO: Final = "endereco_recusado"
"""``TrocaRecusada.codigo`` quando o ``token_endpoint`` de um terceiro nao e o endereco combinado: nada saiu."""

MENSAGEM_TERCEIRO_JA_CONFIGURADO: Final = "Para mudar, cadastre o servidor de novo."
MENSAGEM_TERCEIROS_DESLIGADOS: Final = (
    "Servidores de terceiros ainda estao desligados neste MCP-CLARO. So pessoas de teste os autorizam. Nada foi"
    " guardado."
)


# --- Configuracao ----------------------------------------------------------------------------------


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def conferir_endereco(valor: Any, campo: str) -> str:
    """https obrigatorio (http so em endereco local, para testes); sem usuario, senha nem fragmento."""
    if not isinstance(valor, str) or not valor:
        raise ConfiguracaoAutorizacaoInvalida(f"Falta o endereco {campo}.")
    partes = urlsplit(valor)
    if partes.scheme == "http" and partes.hostname and _loopback(partes.hostname):
        pass
    elif partes.scheme != "https":
        raise ConfiguracaoAutorizacaoInvalida(f"O endereco {campo} precisa comecar com https://.")
    if not partes.hostname or partes.username or partes.password or partes.fragment:
        raise ConfiguracaoAutorizacaoInvalida(f"O endereco {campo} nao pode ter usuario, senha nem fragmento (#).")
    return valor


@dataclass(frozen=True, slots=True)
class ConfigAutorizacao:
    authorization_endpoint: str
    token_endpoint: str
    client_id: str
    escopos: tuple[str, ...] = ()
    autenticacao_cliente: AutenticacaoCliente = "client_secret_basic"
    recurso: str | None = None

    @classmethod
    def de_json(cls, dados: Any) -> ConfigAutorizacao:
        if not isinstance(dados, dict):
            raise ConfiguracaoAutorizacaoInvalida("A configuracao de autorizacao precisa ser um objeto JSON.")
        sobram = set(dados) - _CAMPOS_CONFIG
        if sobram:
            raise ConfiguracaoAutorizacaoInvalida(f"Campos desconhecidos na configuracao: {', '.join(sorted(sobram))}.")
        autorizacao = conferir_endereco(dados.get("authorization_endpoint"), "authorization_endpoint")
        reservados = {k for k, _ in parse_qsl(urlsplit(autorizacao).query)} & _PARAMETROS_RESERVADOS
        if reservados:
            raise ConfiguracaoAutorizacaoInvalida(
                f"O authorization_endpoint nao pode trazer {', '.join(sorted(reservados))}: o MCP-CLARO preenche."
            )
        troca = conferir_endereco(dados.get("token_endpoint"), "token_endpoint")
        client_id = dados.get("client_id")
        if not isinstance(client_id, str) or not client_id.strip() or len(client_id) > 255:
            raise ConfiguracaoAutorizacaoInvalida("Falta o client_id do MCP-CLARO neste sistema.")
        escopos = dados.get("escopos") or []
        if not isinstance(escopos, list) or not all(isinstance(e, str) and _ESCOPO.match(e) for e in escopos):
            raise ConfiguracaoAutorizacaoInvalida("escopos deve ser uma lista de palavras sem espaco.")
        autenticacao = dados.get("autenticacao_cliente", "client_secret_basic")
        if autenticacao not in AUTENTICACOES:
            raise ConfiguracaoAutorizacaoInvalida(
                "autenticacao_cliente aceita client_secret_basic, client_secret_post ou none."
            )
        recurso = dados.get("recurso")
        if recurso is not None:
            recurso = conferir_endereco(recurso, "recurso")
        return cls(autorizacao, troca, client_id, tuple(escopos), autenticacao, recurso)

    def como_json(self) -> dict[str, Any]:
        dados: dict[str, Any] = {
            "authorization_endpoint": self.authorization_endpoint,
            "token_endpoint": self.token_endpoint,
            "client_id": self.client_id,
            "escopos": list(self.escopos),
            "autenticacao_cliente": self.autenticacao_cliente,
        }
        if self.recurso:
            dados["recurso"] = self.recurso
        return dados

    @property
    def usa_segredo(self) -> bool:
        return self.autenticacao_cliente != "none"


@dataclass(frozen=True, slots=True)
class SistemaAutorizavel:
    id: uuid.UUID
    nome: str
    endereco: str
    situacao: str
    tipo_chave: str
    config: ConfigAutorizacao
    origem: str = "proprio"

    @property
    def recurso(self) -> str:
        return self.config.recurso or self.endereco

    @property
    def de_terceiro(self) -> bool:
        """Toda origem que nao e ``proprio`` conta como terceiro (o lado que aperta)."""
        return self.origem != "proprio"


async def carregar_sistema(c: AsyncConnection, sistema_id: uuid.UUID) -> SistemaAutorizavel:
    """Le o sistema e a configuracao. Levanta ``AutorizacaoRecusada`` se ele nao aceita autorizacao por pessoa."""
    linha = (
        await c.execute(
            text("SELECT id, nome, endereco, situacao, tipo_chave, autorizacao, origem FROM sistemas WHERE id = :s"),
            {"s": sistema_id},
        )
    ).first()
    if linha is None:
        raise AutorizacaoRecusada("sistema_desconhecido", "Sistema desconhecido.")
    if linha.tipo_chave != "por_pessoa" or linha.autorizacao is None:
        raise AutorizacaoRecusada("sem_autorizacao_por_pessoa", "Este sistema nao usa autorizacao por pessoa.")
    try:
        config = ConfigAutorizacao.de_json(linha.autorizacao)
    except ConfiguracaoAutorizacaoInvalida:
        raise AutorizacaoRecusada(
            "configuracao_invalida", "A autorizacao deste sistema esta mal configurada. Fale com o ADMIN."
        ) from None
    return SistemaAutorizavel(
        linha.id, linha.nome, linha.endereco, linha.situacao, linha.tipo_chave, config, str(linha.origem)
    )


async def configurar(
    c: AsyncConnection, sistema_id: uuid.UUID, dados: dict[str, Any] | None, *, papel: str = "entrada"
) -> ConfigAutorizacao | None:
    """Para a tela do ADMIN: confere e grava ``sistemas.autorizacao`` (``None`` tira a configuracao).

    Num servidor de terceiros ja configurado, recusa qualquer mudanca (``MENSAGEM_TERCEIRO_JA_CONFIGURADO``): os
    enderecos de troca de chave nao mudam depois de preenchidos (critica M1; a guarda do banco faz o mesmo).
    """
    linha = (
        await c.execute(
            text("SELECT nome, tipo_chave, origem, autorizacao FROM sistemas WHERE id = :s"), {"s": sistema_id}
        )
    ).first()
    if linha is None:
        raise LookupError("Sistema desconhecido.")
    if linha.origem != "proprio" and linha.autorizacao is not None:
        raise ConfiguracaoAutorizacaoInvalida(MENSAGEM_TERCEIRO_JA_CONFIGURADO)
    config = None
    if dados is not None:
        if linha.tipo_chave != "por_pessoa":
            raise ConfiguracaoAutorizacaoInvalida("Autorizacao por pessoa so vale para sistemas com chave por pessoa.")
        config = ConfigAutorizacao.de_json(dados)
    await c.execute(
        text("UPDATE sistemas SET autorizacao = CAST(:a AS jsonb), atualizado_em = now() WHERE id = :s"),
        {"a": json.dumps(config.como_json()) if config else None, "s": sistema_id},
    )
    await registrar(
        c,
        papel=papel,
        tipo="autorizacao_configurada",
        campos={"sistema": linha.nome},
        sistema_id=sistema_id,
        dados={"configurada": config is not None},
    )
    return config


# --- Segredo do cliente ----------------------------------------------------------------------------


def contexto_segredo_cliente(sistema_id: uuid.UUID) -> bytes:
    return f"mcp-claro|{sistema_id}|cliente-oauth".encode()


async def guardar_segredo_cliente(c: AsyncConnection, cifra: Cifra, sistema_id: uuid.UUID, segredo: str) -> None:
    """Despachante: guarda o segredo do cliente OAuth do sistema, cifrado (sem pessoa, sem aplicativo)."""
    linha = (await c.execute(text("SELECT nome FROM sistemas WHERE id = :s"), {"s": sistema_id})).first()
    if linha is None:
        raise LookupError("Sistema desconhecido.")
    if not _VISIVEL.match(segredo):
        raise ConfiguracaoAutorizacaoInvalida("O segredo do cliente tem caracteres que nao servem.")
    cifrado = await cifra.cifrar(segredo.encode(), contexto_segredo_cliente(sistema_id))
    await c.execute(
        text(
            "INSERT INTO chaves_cifradas (sistema_id, pessoa_id, aplicativo_id, tipo, cifrada) "
            "VALUES (:s, NULL, NULL, 'acesso', :c) "
            "ON CONFLICT (sistema_id, coalesce(pessoa_id, '00000000-0000-0000-0000-000000000000'::uuid), "
            "coalesce(aplicativo_id, '00000000-0000-0000-0000-000000000000'::uuid), tipo) "
            "DO UPDATE SET cifrada = EXCLUDED.cifrada, expira_em = NULL, atualizada_em = now()"
        ),
        {"s": sistema_id, "c": cifrado},
    )
    await registrar(
        c,
        papel="despachante",
        tipo="segredo_cliente_guardado",
        campos={"sistema": linha.nome},
        sistema_id=sistema_id,
    )


async def abrir_segredo_cliente(c: AsyncConnection, cifra: Cifra, sistema_id: uuid.UUID) -> str:
    cifrada = (
        await c.execute(
            text(
                "SELECT cifrada FROM chaves_cifradas WHERE sistema_id = :s AND tipo = 'acesso' "
                "AND pessoa_id IS NULL AND aplicativo_id IS NULL"
            ),
            {"s": sistema_id},
        )
    ).scalar_one_or_none()
    if cifrada is None:
        raise AutorizacaoRecusada(
            "sem_segredo_cliente", "O ADMIN ainda nao guardou o segredo do MCP-CLARO neste sistema."
        )
    return (await cifra.decifrar(cifrada, contexto_segredo_cliente(sistema_id))).decode()


# --- PKCE, trava e troca ---------------------------------------------------------------------------


def _b64url(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).rstrip(b"=").decode()


def desafio_s256(verificador: str) -> str:
    """``code_challenge`` S256 (RFC 7636, 4.2)."""
    return _b64url(hashlib.sha256(verificador.encode("ascii")).digest())


def novo_verificador() -> str:
    """86 caracteres do alfabeto sem reserva (RFC 7636 pede de 43 a 128)."""
    return secrets.token_urlsafe(64)


def contexto_pendente(estado: str, pessoa_id: uuid.UUID, sistema_id: uuid.UUID) -> bytes:
    """Amarra o verificador cifrado a linha da autorizacao pendente: copiado para outra, nao decifra."""
    return f"mcp-claro|{sistema_id}|p:{pessoa_id}|autorizacao|{estado}".encode()


def chave_da_trava(pessoa_id: uuid.UUID, sistema_id: uuid.UUID) -> int:
    """Numero de 64 bits da trava consultiva do par (pessoa, sistema)."""
    resumo = hashlib.sha256(f"mcp-claro|chave|{pessoa_id}|{sistema_id}".encode()).digest()
    return int.from_bytes(resumo[:8], "big", signed=True)


async def travar(c: AsyncConnection, pessoa_id: uuid.UUID, sistema_id: uuid.UUID) -> None:
    """Trava consultiva do par (pessoa, sistema) ate o fim da transacao: uma troca ou renovacao por vez.

    Espera sem prazo: so para quem nao fala com ninguem de dentro da trava (concluir e derrubar)."""
    await c.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": chave_da_trava(pessoa_id, sistema_id)})


async def travar_com_prazo(
    c: AsyncConnection, pessoa_id: uuid.UUID, sistema_id: uuid.UUID, *, prazo: float = ESPERA_TRAVA_RENOVACAO
) -> bool:
    """A mesma trava de ``travar``, esperando no maximo ``prazo`` segundos (relogio monotonico).

    Para a renovacao, que troca a chave de dentro da trava: quem a segura pode estar esperando um servidor de
    autorizacao lento (ate ``PRAZO_TROCA``). Tenta com ``pg_try_advisory_xact_lock``, que nunca espera no banco, e
    entre uma tentativa e outra so dorme. ``False``: sem a trava; a transacao segue sem nada feito, e quem chamou
    desiste e a fecha, devolvendo a conexao ao pool."""
    chave = chave_da_trava(pessoa_id, sistema_id)
    fim = anyio.current_time() + prazo
    pausa = 0.02
    while True:
        if (await c.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": chave})).scalar_one():
            return True
        resta = fim - anyio.current_time()
        if resta <= 0:
            return False
        await anyio.sleep(min(pausa, resta))
        pausa = min(pausa * 2, 0.2)


@dataclass(frozen=True, slots=True)
class RespostaToken:
    acesso: str
    renovacao: str | None
    expira_em: datetime | None

    @classmethod
    def de_json(cls, dados: Any, *, agora: datetime | None = None) -> RespostaToken:
        if not isinstance(dados, dict):
            raise TrocaRecusada("resposta_fora_do_padrao")
        acesso = dados.get("access_token")
        tipo = dados.get("token_type")
        if not isinstance(acesso, str) or not _VISIVEL.match(acesso):
            raise TrocaRecusada("resposta_fora_do_padrao")
        if not isinstance(tipo, str) or tipo.lower() != "bearer":
            raise TrocaRecusada("tipo_nao_aceito")
        renovacao = dados.get("refresh_token")
        if renovacao is not None and (not isinstance(renovacao, str) or not _VISIVEL.match(renovacao)):
            raise TrocaRecusada("resposta_fora_do_padrao")
        expira_em = None
        validade = dados.get("expires_in")
        if isinstance(validade, str) and validade.isdigit():
            validade = int(validade)
        if isinstance(validade, int) and not isinstance(validade, bool) and validade > 0:
            prazo = min(timedelta(seconds=validade), VALIDADE_MAXIMA)
            expira_em = (agora or datetime.now(UTC)) + prazo
        return cls(acesso, renovacao, expira_em)


def http_para_autorizacao() -> httpx2.AsyncClient:
    """Cliente de saida para o servidor de autorizacao: um por troca, sem redirecionamento nem proxy."""
    return httpx2.AsyncClient(
        transport=httpx2.AsyncHTTPTransport(retries=0, verify=ssl.create_default_context()),
        headers={"User-Agent": f"mcp-claro/{__version__}", "Accept": "application/json"},
        timeout=httpx2.Timeout(connect=3.0, read=10.0, write=5.0, pool=3.0),
        follow_redirects=False,
        max_redirects=0,
        trust_env=False,
    )


def _codigo_erro(bruto: bytes) -> str:
    try:
        erro = json.loads(bruto).get("error")
    except ValueError, AttributeError:
        return "resposta_fora_do_padrao"
    return erro if isinstance(erro, str) and erro in _ERROS_OAUTH else "outro"


async def _conferir_troca_de_terceiro(endereco: str, ambiente: str, liberar_locais: bool) -> None:
    """O ``token_endpoint`` de um terceiro e conferido como o endereco da sala, antes de abrir conexao."""
    try:
        with anyio.fail_after(cliente.PRAZO_CONEXAO):
            await cliente.conferir_endereco_terceiro(endereco, ambiente, liberar_locais=liberar_locais)
    except ValueError:  # EnderecoRecusado e nome que o IDNA recusa
        raise TrocaRecusada(ENDERECO_RECUSADO) from None
    except OSError, TimeoutError:
        raise TrocaIndisponivel("nome_nao_resolvido") from None


async def trocar(
    config: ConfigAutorizacao,
    segredo_cliente: str | None,
    campos: dict[str, str],
    *,
    terceiro: bool = False,
    ambiente: str = "producao",
    liberar_locais: bool = False,
) -> RespostaToken:
    """Um POST no ``token_endpoint``. Sem reenvio: qualquer falha volta classificada a quem chamou.

    ``terceiro``: o endereco passa antes pela conferencia da sala (https e IP publico; ``liberar_locais`` so vale
    em desenvolvimento e teste). Recusado, levanta ``TrocaRecusada(ENDERECO_RECUSADO)`` sem nada enviado.

    A troca inteira (abrir a conexao, o POST e ler o corpo) tem ``PRAZO_TROCA`` segundos, para proprio e para
    terceiro: o prazo de leitura do httpx vale por pedaco lido, e um servidor que manda um byte por vez seguraria a
    troca por horas. Esgotado, levanta ``TrocaIndisponivel("prazo")`` (o pedido pode ter chegado ao servidor; nada e
    guardado). A conferencia do endereco de um terceiro vem antes, com o prazo dela (``cliente.PRAZO_CONEXAO``).
    """
    if terceiro:
        await _conferir_troca_de_terceiro(config.token_endpoint, ambiente, liberar_locais)
    corpo = dict(campos)
    cabecalhos: dict[str, str] = {}
    if config.autenticacao_cliente == "client_secret_basic":
        par = f"{quote(config.client_id, safe='')}:{quote(segredo_cliente or '', safe='')}"
        cabecalhos["Authorization"] = "Basic " + base64.b64encode(par.encode()).decode()
    elif config.autenticacao_cliente == "client_secret_post":
        corpo |= {"client_id": config.client_id, "client_secret": segredo_cliente or ""}
    else:
        corpo["client_id"] = config.client_id
    bruto = b""
    try:
        with anyio.fail_after(PRAZO_TROCA):
            async with (
                http_para_autorizacao() as http,
                http.stream("POST", config.token_endpoint, data=corpo, headers=cabecalhos) as resposta,
            ):
                async for pedaco in resposta.aiter_bytes():
                    bruto += pedaco
                    if len(bruto) > TAMANHO_MAXIMO_RESPOSTA:
                        raise TrocaRecusada("resposta_grande_demais")
                status = resposta.status_code
    except httpx2.HTTPError as erro:
        raise TrocaIndisponivel(type(erro).__name__) from None
    except TimeoutError:
        raise TrocaIndisponivel("prazo") from None
    if status >= 500 or status == 429:
        raise TrocaIndisponivel(f"http {status}")
    if status != 200:
        raise TrocaRecusada(_codigo_erro(bruto) if 400 <= status < 500 else "redirecionamento_ou_outro")
    try:
        dados = json.loads(bruto)
    except ValueError:
        raise TrocaRecusada("resposta_fora_do_padrao") from None
    return RespostaToken.de_json(dados)


async def guardar_resposta(
    c: AsyncConnection, cifra: Cifra, sistema_id: uuid.UUID, pessoa_id: uuid.UUID, resposta: RespostaToken
) -> str:
    """Grava a chave de acesso (e a de renovacao, se veio) cifradas. Devolve o texto cifrado do acesso.

    Sem chave de renovacao nova, a antiga continua (o servidor pode nao gira-la, RFC 6749, 6).
    """
    await chaves.guardar(
        c,
        cifra,
        sistema_id=sistema_id,
        segredo=resposta.acesso,
        pessoa_id=pessoa_id,
        tipo="acesso",
        expira_em=resposta.expira_em,
    )
    if resposta.renovacao:
        await chaves.guardar(
            c, cifra, sistema_id=sistema_id, segredo=resposta.renovacao, pessoa_id=pessoa_id, tipo="renovacao"
        )
    cifrada = (
        await c.execute(
            text(
                "SELECT cifrada FROM chaves_cifradas WHERE sistema_id = :s AND pessoa_id = :p AND tipo = 'acesso'"
                " AND aplicativo_id IS NULL"
            ),
            {"s": sistema_id, "p": pessoa_id},
        )
    ).scalar_one()
    return str(cifrada)


# --- Os tres passos --------------------------------------------------------------------------------


def caminho_da_volta(sistema_id: uuid.UUID | None) -> str:
    """Caminho da volta nas telas: o de um servidor de terceiros (``sistema_id``) ou, sem ele, o dos proprios."""
    return CAMINHO_VOLTA_PROPRIOS if sistema_id is None else f"/telas/autorizar/{sistema_id}/volta"


def _volta_do_terceiro(redirect_uri: str, sistema_id: uuid.UUID) -> bool:
    """O ``redirect_uri`` de um terceiro termina no endereco de volta dele, sem consulta."""
    partes = urlsplit(redirect_uri)
    return partes.path.endswith(caminho_da_volta(sistema_id)) and not partes.query


def montar_url(config: ConfigAutorizacao, *, estado: str, desafio: str, redirect_uri: str, recurso: str) -> str:
    partes = urlsplit(config.authorization_endpoint)
    parametros = parse_qsl(partes.query, keep_blank_values=True)
    parametros += [
        ("response_type", "code"),
        ("client_id", config.client_id),
        ("redirect_uri", redirect_uri),
        ("state", estado),
        ("code_challenge", desafio),
        ("code_challenge_method", "S256"),
    ]
    if config.escopos:
        parametros.append(("scope", " ".join(config.escopos)))
    parametros.append(("resource", recurso))
    return urlunsplit((partes.scheme, partes.netloc, partes.path, urlencode(parametros), ""))


async def _conferir_pessoa_e_sistema(
    c: AsyncConnection, pessoa_id: uuid.UUID, sistema: SistemaAutorizavel, *, terceiros_ligados: bool = False
) -> None:
    if sistema.situacao != "ativo":
        raise AutorizacaoRecusada("sistema_indisponivel", f"O sistema {sistema.nome} nao esta ativo agora.")
    pessoa = (await c.execute(text("SELECT situacao, teste FROM pessoas WHERE id = :p"), {"p": pessoa_id})).first()
    if pessoa is None or pessoa.situacao != "ativa":
        raise AutorizacaoRecusada("pessoa_inativa", "Sua conta nao esta ativa no MCP-CLARO.")
    if sistema.de_terceiro and not terceiros_ligados and not pessoa.teste:
        # Com a chave da Fase 3 desligada, so pessoa de teste usa terceiros (como a vitrine).
        raise AutorizacaoRecusada("terceiros_desligados", MENSAGEM_TERCEIROS_DESLIGADOS)
    bloqueio = await bloqueios.ativo_para(c, pessoa_id=pessoa_id, aplicativo_id=None, sistema_id=sistema.id)
    if bloqueio is not None:
        raise AutorizacaoRecusada(bloqueio.motivo_recusa(), bloqueio.mensagem())


async def iniciar_autorizacao(
    conexao: AsyncConnection,
    cifra: Cifra,
    pessoa_id: uuid.UUID,
    sistema_id: uuid.UUID,
    redirect_uri: str,
    *,
    prazo: timedelta = PRAZO_PENDENTE,
    papel: str = "entrada",
    terceiros_ligados: bool = False,
) -> str:
    """Cria a autorizacao pendente e devolve o endereco da pagina de autorizacao do sistema.

    Grava na transacao do chamador. ``cifra`` so precisa cifrar (a entrada nunca decifra o
    verificador; quem decifra e o despachante, na volta). Levanta ``AutorizacaoRecusada``.
    ``terceiros_ligados``: a chave da Fase 3; desligada, so pessoa de teste autoriza um terceiro.
    Num terceiro, ``redirect_uri`` precisa terminar no endereco de volta dele (``caminho_da_volta``).
    """
    try:
        conferir_endereco(redirect_uri, "redirect_uri")
    except ConfiguracaoAutorizacaoInvalida as erro:
        raise AutorizacaoRecusada("redirect_uri_invalido", str(erro)) from None
    sistema = await carregar_sistema(conexao, sistema_id)
    if sistema.de_terceiro and not _volta_do_terceiro(redirect_uri, sistema.id):
        raise AutorizacaoRecusada(
            "redirect_uri_invalido",
            "O endereco de volta de um servidor de terceiros precisa ser o dele"
            f" ({caminho_da_volta(sistema.id)}). Nada foi guardado.",
        )
    await _conferir_pessoa_e_sistema(conexao, pessoa_id, sistema, terceiros_ligados=terceiros_ligados)
    estado = secrets.token_urlsafe(32)
    verificador = novo_verificador()
    guardado = json.dumps({"verificador": verificador, "redirect_uri": redirect_uri}).encode()
    cifrado = await cifra.cifrar(guardado, contexto_pendente(estado, pessoa_id, sistema_id))
    await conexao.execute(
        text(
            "INSERT INTO autorizacoes_pendentes (estado, pessoa_id, sistema_id, verificador_cifrado, expira_em) "
            "VALUES (:e, :p, :s, :v, now() + make_interval(secs => :prazo))"
        ),
        {"e": estado, "p": pessoa_id, "s": sistema_id, "v": cifrado, "prazo": prazo.total_seconds()},
    )
    await registrar(
        conexao,
        papel=papel,
        tipo="autorizacao_iniciada",
        campos={"sistema": sistema.nome},
        pessoa_id=pessoa_id,
        sistema_id=sistema_id,
    )
    CONTADORES.somar("autorizacoes_iniciadas")
    return montar_url(
        sistema.config,
        estado=estado,
        desafio=desafio_s256(verificador),
        redirect_uri=redirect_uri,
        recurso=sistema.recurso,
    )


@dataclass(frozen=True, slots=True)
class Concluida:
    sistema_id: uuid.UUID
    sistema: str
    expira_em: datetime | None
    com_renovacao: bool


@dataclass(frozen=True, slots=True)
class _Pendente:
    pessoa_id: uuid.UUID
    sistema_id: uuid.UUID
    verificador_cifrado: str


async def _recusar(
    engine: AsyncEngine,
    motivo: str,
    mensagem: str,
    *,
    pessoa_id: uuid.UUID | None,
    sistema_id: uuid.UUID | None,
    sistema: str = "desconhecido",
) -> AutorizacaoRecusada:
    async with engine.begin() as c:
        await registrar(
            c,
            papel="despachante",
            tipo="autorizacao_recusada",
            campos={"sistema": sistema, "motivo": motivo},
            pessoa_id=pessoa_id,
            sistema_id=sistema_id,
        )
    CONTADORES.somar("autorizacoes_recusadas", motivo=motivo)
    return AutorizacaoRecusada(motivo, mensagem)


_MENSAGENS_ESTADO: Final[dict[str, str]] = {
    "estado_ja_usado": "Este pedido de autorizacao ja foi usado. Comece de novo pelas telas do MCP-CLARO.",
    "estado_desconhecido": "Pedido de autorizacao desconhecido. Comece de novo pelas telas do MCP-CLARO.",
    "estado_de_outra_pessoa": "Este pedido de autorizacao e de outra pessoa. Nada foi guardado.",
    "estado_vencido": "O prazo para autorizar acabou. Comece de novo pelas telas do MCP-CLARO.",
}


async def _gastar_estado(engine: AsyncEngine, estado: str, pessoa_id: uuid.UUID) -> _Pendente:
    """Uso unico: o UPDATE so passa uma vez. Recusado ou nao, o estado fica gasto (COMMIT antes da troca)."""
    async with engine.begin() as c:
        linha = (
            await c.execute(
                text(
                    "UPDATE autorizacoes_pendentes SET usada_em = now() WHERE estado = :e AND usada_em IS NULL "
                    "RETURNING pessoa_id, sistema_id, verificador_cifrado, expira_em > now() AS no_prazo"
                ),
                {"e": estado},
            )
        ).first()
        sistema_existente = None
        if linha is None:
            sistema_existente = (
                await c.execute(text("SELECT sistema_id FROM autorizacoes_pendentes WHERE estado = :e"), {"e": estado})
            ).scalar_one_or_none()
    if linha is None:
        motivo = "estado_ja_usado" if sistema_existente is not None else "estado_desconhecido"
        sistema_id = sistema_existente
    else:
        sistema_id = linha.sistema_id
        if linha.pessoa_id != pessoa_id:
            motivo = "estado_de_outra_pessoa"
        elif not linha.no_prazo:
            motivo = "estado_vencido"
        else:
            return _Pendente(linha.pessoa_id, linha.sistema_id, linha.verificador_cifrado)
    raise await _recusar(engine, motivo, _MENSAGENS_ESTADO[motivo], pessoa_id=pessoa_id, sistema_id=sistema_id)


def _conferir_volta(sistema: SistemaAutorizavel, sistema_da_volta: uuid.UUID | None) -> None:
    """A volta chegou pelo endereco do sistema da pendencia? Pelo de outro sistema, ou um terceiro pela volta dos
    proprios: o codigo pode ser de outro servidor de autorizacao (mistura), e nada pode ir ao deste."""
    pela_volta_dos_proprios = sistema_da_volta is None
    de_outro = sistema.de_terceiro if pela_volta_dos_proprios else sistema_da_volta != sistema.id
    if de_outro:
        log.warning(
            "autorizacao_de_outro_sistema", sistema=sistema.nome, pela_volta_dos_proprios=pela_volta_dos_proprios
        )
        raise AutorizacaoRecusada(ESTADO_DE_OUTRO_SISTEMA, MENSAGEM_ESTADO_DE_OUTRO_SISTEMA)


async def _preparar_troca(
    engine: AsyncEngine,
    cifra: Cifra,
    pendente: _Pendente,
    pessoa_id: uuid.UUID,
    *,
    terceiros_ligados: bool,
    sistema_da_volta: uuid.UUID | None,
) -> tuple[SistemaAutorizavel, str | None]:
    """Le o sistema da pendencia, confere a pessoa, o sistema e o endereco da volta, e so depois abre o segredo do
    cliente. Toda recusa daqui sai antes de abrir segredo, decifrar o verificador ou trocar o codigo."""
    recusa: AutorizacaoRecusada | None = None
    nome = "desconhecido"
    async with engine.connect() as c:
        try:
            sistema = await carregar_sistema(c, pendente.sistema_id)
            nome = sistema.nome
            await _conferir_pessoa_e_sistema(c, pessoa_id, sistema, terceiros_ligados=terceiros_ligados)
            _conferir_volta(sistema, sistema_da_volta)
            segredo = await abrir_segredo_cliente(c, cifra, sistema.id) if sistema.config.usa_segredo else None
        except AutorizacaoRecusada as erro:
            recusa = erro
    if recusa is not None:
        raise await _recusar(
            engine, recusa.motivo, recusa.mensagem, pessoa_id=pessoa_id, sistema_id=pendente.sistema_id, sistema=nome
        )
    return sistema, segredo


async def concluir_autorizacao(
    engine: AsyncEngine,
    cifra: Cifra,
    *,
    estado: str,
    pessoa_id: uuid.UUID,
    codigo: str | None = None,
    erro: str | None = None,
    ambiente: str = "producao",
    liberar_locais: bool = False,
    terceiros_ligados: bool = False,
    sistema_id: uuid.UUID | None = None,
) -> Concluida:
    """Despachante: confere o estado, troca o codigo pela chave e grava tudo cifrado.

    ``pessoa_id`` e a pessoa da sessao das telas; precisa ser a mesma que iniciou. ``erro`` e o
    ``error`` da volta (a pessoa desistiu ou o sistema recusou): o estado fica gasto e nada e
    guardado. Toda recusa vai ao extrato e levanta ``AutorizacaoRecusada``. Num terceiro, o
    ``token_endpoint`` passa antes pela conferencia do endereco combinado (``ambiente`` e
    ``liberar_locais``, este so em desenvolvimento e teste).

    ``sistema_id``: o sistema do endereco por onde a volta chegou (``/telas/autorizar/<id>/volta``); ``None`` e a
    volta dos proprios. Diferente do sistema da pendencia, ou ``None`` com a pendencia de um terceiro, a recusa e
    ``estado_de_outro_sistema``, depois de gastar o estado e antes de abrir o segredo, decifrar o verificador ou
    trocar o codigo: o codigo pode ser de outro servidor de autorizacao (mistura), e nada vai a ninguem.
    """
    if not estado or len(estado) > 256 or (codigo is not None and not _VISIVEL.match(codigo)):
        raise await _recusar(
            engine, "pedido_invalido", "Pedido de autorizacao invalido.", pessoa_id=pessoa_id, sistema_id=None
        )
    pendente = await _gastar_estado(engine, estado, pessoa_id)
    sistema, segredo = await _preparar_troca(
        engine, cifra, pendente, pessoa_id, terceiros_ligados=terceiros_ligados, sistema_da_volta=sistema_id
    )
    if erro is not None or not codigo:
        codigo_erro = erro if erro in _ERROS_OAUTH else "outro"
        raise await _recusar(
            engine,
            "sistema_nao_autorizou",
            f"O sistema {sistema.nome} nao autorizou o acesso ({codigo_erro}). Nada foi guardado.",
            pessoa_id=pessoa_id,
            sistema_id=sistema.id,
            sistema=sistema.nome,
        )
    guardado = json.loads(
        await cifra.decifrar(pendente.verificador_cifrado, contexto_pendente(estado, pessoa_id, sistema.id))
    )
    try:
        resposta = await trocar(
            sistema.config,
            segredo,
            {
                "grant_type": "authorization_code",
                "code": codigo,
                "redirect_uri": guardado["redirect_uri"],
                "code_verifier": guardado["verificador"],
                "resource": sistema.recurso,
            },
            terceiro=sistema.de_terceiro,
            ambiente=ambiente,
            liberar_locais=liberar_locais,
        )
    except TrocaRecusada as recusa:
        log.warning("troca_recusada", sistema=sistema.nome, erro_oauth=recusa.codigo)
        if recusa.codigo == ENDERECO_RECUSADO:
            await registrar_endereco_recusado(engine, sistema.id, sistema.nome, pessoa_id=pessoa_id)
            raise await _recusar(
                engine,
                ENDERECO_RECUSADO,
                f"O endereco de troca de chave do servidor de terceiros {sistema.nome} nao e publico ou nao usa"
                " https. Nada foi enviado nem guardado.",
                pessoa_id=pessoa_id,
                sistema_id=sistema.id,
                sistema=sistema.nome,
            ) from None
        raise await _recusar(
            engine,
            "troca_recusada",
            f"O sistema {sistema.nome} recusou a autorizacao. Nada foi guardado. Tente autorizar de novo.",
            pessoa_id=pessoa_id,
            sistema_id=sistema.id,
            sistema=sistema.nome,
        ) from None
    except TrocaIndisponivel:
        raise await _recusar(
            engine,
            "sistema_indisponivel",
            f"O sistema {sistema.nome} nao respondeu. Nada foi guardado. Tente autorizar de novo em instantes.",
            pessoa_id=pessoa_id,
            sistema_id=sistema.id,
            sistema=sistema.nome,
        ) from None
    async with engine.begin() as c:
        await travar(c, pessoa_id, sistema.id)
        await guardar_resposta(c, cifra, sistema.id, pessoa_id, resposta)
        if not resposta.renovacao:
            await c.execute(
                text("DELETE FROM chaves_cifradas WHERE sistema_id = :s AND pessoa_id = :p AND tipo = 'renovacao'"),
                {"s": sistema.id, "p": pessoa_id},
            )
        await registrar(
            c,
            papel="despachante",
            tipo="autorizacao_concluida",
            campos={"sistema": sistema.nome},
            pessoa_id=pessoa_id,
            sistema_id=sistema.id,
            dados={
                "com_renovacao": bool(resposta.renovacao),
                "vence_em": resposta.expira_em.isoformat() if resposta.expira_em else None,
            },
        )
    CONTADORES.somar("autorizacoes_concluidas")
    return Concluida(sistema.id, sistema.nome, resposta.expira_em, bool(resposta.renovacao))


async def derrubar_autorizacoes(
    conexao: AsyncConnection,
    pessoa_id: uuid.UUID,
    motivo: str,
    *,
    sistema_id: uuid.UUID | None = None,
    papel: str = "despachante",
) -> int:
    """Apaga as chaves cifradas da pessoa (em todos os sistemas, ou so em ``sistema_id``) e registra.

    Usada ao desativar uma pessoa e quando ela retira uma autorizacao. Segura a mesma trava das
    renovacoes, entao uma renovacao em curso nao recria a chave apagada. Pedidos de autorizacao
    pendentes da pessoa ficam gastos. ``motivo`` e uma frase curta, sem dado pessoal (vai ao extrato).
    Devolve quantos sistemas perderam a autorizacao.
    """
    # Sem sistema_id, ":s" e nulo e o filtro "(:s IS NULL OR ...)" deixa passar todos.
    parametros = {"p": pessoa_id, "s": sistema_id}
    sistemas = (
        await conexao.execute(
            text(
                "SELECT DISTINCT c.sistema_id, s.nome FROM chaves_cifradas c JOIN sistemas s ON s.id = c.sistema_id "
                "WHERE c.pessoa_id = :p AND (CAST(:s AS uuid) IS NULL OR c.sistema_id = :s) ORDER BY c.sistema_id"
            ),
            parametros,
        )
    ).all()
    for linha in sistemas:
        await travar(conexao, pessoa_id, linha.sistema_id)
    await conexao.execute(
        text("DELETE FROM chaves_cifradas WHERE pessoa_id = :p AND (CAST(:s AS uuid) IS NULL OR sistema_id = :s)"),
        parametros,
    )
    await conexao.execute(
        text(
            "UPDATE autorizacoes_pendentes SET usada_em = now() WHERE pessoa_id = :p AND usada_em IS NULL "
            "AND (CAST(:s AS uuid) IS NULL OR sistema_id = :s)"
        ),
        parametros,
    )
    for linha in sistemas:
        await registrar(
            conexao,
            papel=papel,
            tipo="autorizacao_derrubada",
            campos={"sistema": linha.nome, "motivo": motivo.strip()[:120] or "sem motivo"},
            pessoa_id=pessoa_id,
            sistema_id=linha.sistema_id,
        )
    return len(sistemas)


# --- Porta interna ---------------------------------------------------------------------------------


async def registrar_endereco_recusado(
    engine: AsyncEngine, sistema_id: uuid.UUID, nome: str, *, pessoa_id: uuid.UUID | None = None
) -> None:
    """``sala_endereco_recusado`` no extrato: o endereco de troca de chave de um terceiro nao passou."""
    async with engine.begin() as c:
        await registrar(
            c,
            papel="despachante",
            tipo="sala_endereco_recusado",
            campos={"sistema": nome},
            pessoa_id=pessoa_id,
            sistema_id=sistema_id,
        )
    CONTADORES.somar("sala_endereco_recusado", onde="autorizacao")


def _uuid(valor: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(valor)) if valor else None
    except ValueError:
        return None


def _texto(valor: Any) -> str | None:
    return valor if isinstance(valor, str) and valor else None


def _invalido(mensagem: str) -> JSONResponse:
    return JSONResponse({"motivo": "pedido_invalido", "mensagem": mensagem}, status_code=400)


async def _corpo(pedido: Request) -> dict[str, Any]:
    try:
        corpo = await pedido.json()
    except ValueError:
        return {}
    return corpo if isinstance(corpo, dict) else {}


class _PortaAutorizacao:
    """Atende as rotas internas. Sem ``engine`` e ``cifra`` dados, usa ``app.state.engine`` e ``app.state.cifra``.

    ``ambiente``, ``liberar_locais`` e ``terceiros_ligados`` valem para servidores de terceiros (ver o topo do
    modulo); o padrao e o lado que aperta."""

    def __init__(
        self,
        engine: AsyncEngine | None = None,
        cifra: Cifra | None = None,
        *,
        ambiente: str = "producao",
        liberar_locais: bool = False,
        terceiros_ligados: bool = False,
    ) -> None:
        self._engine = engine
        self._cifra = cifra
        self._ambiente = str(ambiente)
        self._liberar_locais = liberar_locais
        self._terceiros_ligados = terceiros_ligados

    def _ligacoes(self, pedido: Request) -> tuple[AsyncEngine, Cifra]:
        if self._engine is not None and self._cifra is not None:
            return self._engine, self._cifra
        return pedido.app.state.engine, pedido.app.state.cifra

    async def concluir(self, pedido: Request) -> JSONResponse:
        engine, cifra = self._ligacoes(pedido)
        corpo = await _corpo(pedido)
        pessoa_id, estado = _uuid(corpo.get("pessoa_id")), _texto(corpo.get("estado"))
        if pessoa_id is None or estado is None:
            return _invalido("estado e pessoa_id sao obrigatorios.")
        # O sistema do endereco por onde a volta chegou: opcional (sem ele, a volta dos proprios), mas, se veio,
        # precisa ser um uuid. Invalido, nada e conferido e o estado nao e gasto.
        bruto = corpo.get("sistema_id")
        sistema_id = _uuid(bruto) if isinstance(bruto, str) else None
        if bruto is not None and sistema_id is None:
            return _invalido("sistema_id invalido.")
        try:
            feito = await concluir_autorizacao(
                engine,
                cifra,
                estado=estado,
                pessoa_id=pessoa_id,
                codigo=_texto(corpo.get("codigo")),
                erro=_texto(corpo.get("erro")),
                ambiente=self._ambiente,
                liberar_locais=self._liberar_locais,
                terceiros_ligados=self._terceiros_ligados,
                sistema_id=sistema_id,
            )
        except AutorizacaoRecusada as recusa:
            return JSONResponse(
                {"concluida": False, "motivo": recusa.motivo, "mensagem": recusa.mensagem}, status_code=403
            )
        return JSONResponse(
            {
                "concluida": True,
                "sistema_id": str(feito.sistema_id),
                "sistema": feito.sistema,
                "mensagem": f"Pronto: a sua autorizacao no sistema {feito.sistema} foi guardada.",
            }
        )

    async def iniciar(self, pedido: Request) -> JSONResponse:
        engine, cifra = self._ligacoes(pedido)
        corpo = await _corpo(pedido)
        pessoa_id, sistema_id = _uuid(corpo.get("pessoa_id")), _uuid(corpo.get("sistema_id"))
        redirect_uri = _texto(corpo.get("redirect_uri"))
        if pessoa_id is None or sistema_id is None or redirect_uri is None:
            return _invalido("pessoa_id, sistema_id e redirect_uri sao obrigatorios.")
        try:
            async with engine.begin() as c:
                url = await iniciar_autorizacao(
                    c,
                    cifra,
                    pessoa_id,
                    sistema_id,
                    redirect_uri,
                    papel="despachante",
                    terceiros_ligados=self._terceiros_ligados,
                )
        except AutorizacaoRecusada as recusa:
            return JSONResponse({"motivo": recusa.motivo, "mensagem": recusa.mensagem}, status_code=403)
        return JSONResponse({"url": url})

    async def derrubar(self, pedido: Request) -> JSONResponse:
        engine, _ = self._ligacoes(pedido)
        corpo = await _corpo(pedido)
        pessoa_id, motivo = _uuid(corpo.get("pessoa_id")), _texto(corpo.get("motivo"))
        if pessoa_id is None or motivo is None:
            return _invalido("pessoa_id e motivo sao obrigatorios.")
        async with engine.begin() as c:
            n = await derrubar_autorizacoes(c, pessoa_id, motivo, sistema_id=_uuid(corpo.get("sistema_id")))
        return JSONResponse({"derrubadas": n})

    async def segredo(self, pedido: Request) -> JSONResponse:
        engine, cifra = self._ligacoes(pedido)
        corpo = await _corpo(pedido)
        sistema_id, valor = _uuid(corpo.get("sistema_id")), _texto(corpo.get("segredo"))
        if sistema_id is None or valor is None:
            return _invalido("sistema_id e segredo sao obrigatorios.")
        try:
            async with engine.begin() as c:
                await guardar_segredo_cliente(c, cifra, sistema_id, valor)
        except LookupError, ConfiguracaoAutorizacaoInvalida:
            return _invalido("Sistema desconhecido ou segredo que nao serve.")
        return JSONResponse({"guardado": True})

    def rotas(self) -> list[Route]:
        return [
            Route("/interno/autorizacao/concluir", self.concluir, methods=["POST"]),
            Route("/interno/autorizacao/iniciar", self.iniciar, methods=["POST"]),
            Route("/interno/autorizacao/derrubar", self.derrubar, methods=["POST"]),
            Route("/interno/autorizacao/segredo", self.segredo, methods=["POST"]),
        ]


def rotas_autorizacao(
    engine: AsyncEngine,
    cifra: Cifra,
    *,
    ambiente: str = "producao",
    liberar_locais: bool = False,
    terceiros_ligados: bool = False,
) -> list[Route]:
    """Rotas internas do despachante (so com mTLS da entrada), ja ligadas ao ``engine`` e a ``cifra``.

    - ``POST /interno/autorizacao/concluir`` {estado, pessoa_id, codigo | erro, sistema_id?}: a volta da pagina do
      sistema (``sistema_id``: o do endereco por onde ela chegou; sem ele, a volta dos proprios);
    - ``POST /interno/autorizacao/iniciar`` {pessoa_id, sistema_id, redirect_uri}: alternativa a chamar
      ``iniciar_autorizacao`` na entrada, para quando a entrada nao puder cifrar no cofre;
    - ``POST /interno/autorizacao/derrubar`` {pessoa_id, motivo, sistema_id?}: ao desativar a pessoa;
    - ``POST /interno/autorizacao/segredo`` {sistema_id, segredo}: o ADMIN guarda o segredo do cliente.
    """
    return _PortaAutorizacao(
        engine, cifra, ambiente=ambiente, liberar_locais=liberar_locais, terceiros_ligados=terceiros_ligados
    ).rotas()


ROTAS_AUTORIZACAO: Final[list[Route]] = _PortaAutorizacao().rotas()
"""As mesmas rotas, prontas para ``Starlette(routes=[..., *ROTAS_AUTORIZACAO])``. Pedem
``app.state.engine`` e ``app.state.cifra``; sem eles, use ``rotas_autorizacao(engine, cifra)``."""
