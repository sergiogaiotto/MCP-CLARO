"""Crachas da ronda: o de verdade, pedido ao provedor de login, e o falso, feito com chave propria.

Identidade de programa (decisao 9): a ronda e o vigia sao clientes pre-cadastrados no provedor
(``mcp-claro-ronda`` e ``mcp-claro-vigia`` no realm do Keycloak), com conta de servico e JWT
assinado (``private_key_jwt``, ES256). O provedor guarda so a chave publica. Para pedir um cracha:

1. monta a afirmacao do cliente (RFC 7523): ``iss`` e ``sub`` iguais ao ``client_id``, ``aud`` no
   token endpoint (no Keycloak, o emissor), ``jti`` novo, ``iat`` e ``exp`` com 60 segundos (o realm
   aceita no maximo 60);
2. faz ``POST`` no token endpoint com ``grant_type=client_credentials``, a afirmacao, o escopo e o
   ``resource`` (RFC 8707) do MCP-CLARO;
3. guarda o cracha ate 30 segundos antes de vencer. Os crachas que saem da guarda ficam numa
   lista curta: depois de vencidos, servem ao caso "cracha vencido" da ronda.

O cracha de outro condominio e um cracha de verdade do mesmo provedor, pedido com o escopo de um
segundo recurso de teste (``outro-servico``): a audiencia dele nao e a do MCP-CLARO.

O cracha falso copia as reivindicacoes e o cabecalho (``kid`` e ``alg``) de um cracha de verdade e e
assinado por uma chave gerada aqui, que o provedor nunca viu. Assim o gateway acha a chave publica
pelo ``kid`` e precisa recusar pela assinatura.

Nenhum cracha nem chave vai a log, extrato ou tabela.
"""

from __future__ import annotations

import ssl
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Protocol

import httpx2
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from mcp_claro import __version__
from mcp_claro.entrada.cracha import FOLGA_RELOGIO_SEGUNDOS
from mcp_claro.logs import logger
from mcp_claro.ronda.config import IdentidadePrograma

log = logger("mcp_claro.ronda.cracha")

TIPO_ASSERCAO: Final = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
VALIDADE_ASSERCAO_SEGUNDOS: Final = 60
"""O realm aceita afirmacao com no maximo 60 segundos (``token.endpoint.auth.signing.max.exp``)."""
RENOVAR_ANTES_SEGUNDOS: Final = 30
MARGEM_VENCIDO_SEGUNDOS: Final = FOLGA_RELOGIO_SEGUNDOS + 60
"""Um cracha guardado so serve de "vencido" quando passou do prazo com folga maior que a do verificador."""
GUARDA_ANTIGOS: Final = 6


class FalhaCracha(Exception):
    """O provedor nao deu o cracha. O motivo e curto e nunca traz o cracha nem a chave."""

    def __init__(self, motivo: str) -> None:
        self.motivo = motivo[:200]
        super().__init__(self.motivo)


class FonteCrachas(Protocol):
    """De onde a ronda tira os crachas de verdade. Nos testes, um provedor falso faz o mesmo papel."""

    async def valido(self) -> str:
        """Cracha de verdade da identidade da ronda, preso ao MCP-CLARO. Levanta ``FalhaCracha``."""
        ...

    async def outro_condominio(self) -> str | None:
        """Cracha de verdade do mesmo provedor, com audiencia de outro recurso. ``None``: caso desligado."""
        ...

    def vencido(self) -> str | None:
        """Cracha de verdade ja vencido (guardado de rondas anteriores). ``None``: ainda nao ha um."""
        ...


def reivindicacoes_sem_conferir(cracha: str) -> dict[str, Any]:
    """Le as reivindicacoes sem conferir a assinatura. So para decidir o que testar, nunca para aceitar."""
    try:
        dados = jwt.decode(cracha, options={"verify_signature": False})
    except jwt.PyJWTError:
        return {}
    return dados if isinstance(dados, dict) else {}


def audiencias(dados: dict[str, Any]) -> list[str]:
    aud = dados.get("aud")
    if isinstance(aud, str):
        return [aud]
    if isinstance(aud, list):
        return [a for a in aud if isinstance(a, str)]
    return []


def carregar_chave(caminho: str) -> ec.EllipticCurvePrivateKey:
    """Chave privada EC P-256 em PEM (PKCS#8 ou SEC1), sem senha, de um segredo do Docker."""
    try:
        chave = serialization.load_pem_private_key(Path(caminho).read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as erro:
        raise FalhaCracha(f"chave do programa ilegivel ({type(erro).__name__})") from None
    if not isinstance(chave, ec.EllipticCurvePrivateKey) or not isinstance(chave.curve, ec.SECP256R1):
        raise FalhaCracha("a chave do programa precisa ser EC P-256 (ES256)")
    return chave


def assercao_cliente(
    client_id: str,
    audiencia: str,
    chave: ec.EllipticCurvePrivateKey,
    *,
    kid: str | None = None,
    agora: float | None = None,
) -> str:
    """Afirmacao do cliente (RFC 7523) assinada com ES256, valida por 60 segundos."""
    instante = int(agora if agora is not None else time.time())
    dados = {
        "iss": client_id,
        "sub": client_id,
        "aud": audiencia,
        "iat": instante,
        "nbf": instante,
        "exp": instante + VALIDADE_ASSERCAO_SEGUNDOS,
        "jti": str(uuid.uuid4()),
    }
    cabecalho: dict[str, Any] = {"typ": "JWT"}
    if kid:
        cabecalho["kid"] = kid
    return jwt.encode(dados, chave, algorithm="ES256", headers=cabecalho)


@dataclass(frozen=True, slots=True)
class CrachaObtido:
    token: str
    expira_em: float

    def vence_em(self, agora: float) -> float:
        return self.expira_em - agora


def _cliente_http(verify: ssl.SSLContext | None, transporte: httpx2.AsyncBaseTransport | None) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        transport=transporte,
        verify=verify or ssl.create_default_context(),
        timeout=httpx2.Timeout(10.0),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": f"mcp-claro/{__version__}"},
    )


class ProvedorDeCrachas:
    """Pede crachas de programa ao token endpoint, com ``client_credentials`` e ``private_key_jwt``."""

    def __init__(
        self,
        identidade: IdentidadePrograma,
        *,
        chave: ec.EllipticCurvePrivateKey | None = None,
        verify: ssl.SSLContext | None = None,
        transporte: httpx2.AsyncBaseTransport | None = None,
        relogio: Callable[[], float] = time.time,
    ) -> None:
        if chave is None:
            if not identidade.arquivo_chave:
                raise FalhaCracha("programa sem chave configurada")
            chave = carregar_chave(identidade.arquivo_chave)
        self.identidade = identidade
        self._chave = chave
        self._verify = verify
        self._transporte = transporte
        self._relogio = relogio
        self._token_url = identidade.token_url

    async def token_url(self, http: httpx2.AsyncClient) -> str:
        if self._token_url:
            return self._token_url
        url = f"{self.identidade.emissor}/.well-known/openid-configuration"
        try:
            resposta = await http.get(url)
        except httpx2.HTTPError as erro:
            raise FalhaCracha(f"metadados do provedor sem resposta ({type(erro).__name__})") from None
        if resposta.status_code != 200:
            raise FalhaCracha(f"metadados do provedor responderam HTTP {resposta.status_code}")
        try:
            endpoint = resposta.json().get("token_endpoint")
        except ValueError:
            endpoint = None
        if not isinstance(endpoint, str) or not endpoint.startswith(("https://", "http://")):
            raise FalhaCracha("metadados do provedor sem token_endpoint")
        self._token_url = endpoint
        return endpoint

    async def pedir(self, escopo: str | None) -> CrachaObtido:
        """Um cracha novo. Levanta ``FalhaCracha`` com motivo curto quando o provedor recusa."""
        async with _cliente_http(self._verify, self._transporte) as http:
            url = await self.token_url(http)
            formulario = {
                "grant_type": "client_credentials",
                "client_id": self.identidade.client_id,
                "client_assertion_type": TIPO_ASSERCAO,
                "client_assertion": assercao_cliente(
                    self.identidade.client_id,
                    self.identidade.audiencia_assercao or url,
                    self._chave,
                    kid=self.identidade.kid,
                    agora=self._relogio(),
                ),
                "resource": self.identidade.recurso,
            }
            if escopo:
                formulario["scope"] = escopo
            try:
                resposta = await http.post(url, data=formulario, headers={"Accept": "application/json"})
            except httpx2.HTTPError as erro:
                raise FalhaCracha(f"token endpoint sem resposta ({type(erro).__name__})") from None
        try:
            corpo = resposta.json()
        except ValueError:
            corpo = {}
        if not isinstance(corpo, dict):
            corpo = {}
        if resposta.status_code != 200:
            codigo_erro = corpo.get("error")
            codigo = codigo_erro[:40] if isinstance(codigo_erro, str) else "sem codigo"
            raise FalhaCracha(f"provedor recusou o pedido de cracha (HTTP {resposta.status_code}, {codigo})")
        token = corpo.get("access_token")
        tipo = corpo.get("token_type")
        if not isinstance(token, str) or not token or not isinstance(tipo, str) or tipo.lower() != "bearer":
            raise FalhaCracha("resposta do provedor sem cracha do tipo Bearer")
        agora = self._relogio()
        exp = reivindicacoes_sem_conferir(token).get("exp")
        if isinstance(exp, int | float):
            expira = float(exp)
        else:
            validade = corpo.get("expires_in")
            expira = agora + float(validade) if isinstance(validade, int | float) else agora + 60.0
        return CrachaObtido(token, expira)


class CrachasDoProvedor:
    """``FonteCrachas`` de producao: guarda o cracha valido e os antigos, para o caso "vencido"."""

    def __init__(self, provedor: ProvedorDeCrachas, *, relogio: Callable[[], float] = time.time) -> None:
        self.provedor = provedor
        self._relogio = relogio
        self._atual: CrachaObtido | None = None
        self._antigos: deque[CrachaObtido] = deque(maxlen=GUARDA_ANTIGOS)

    async def valido(self) -> str:
        agora = self._relogio()
        if self._atual is None or self._atual.vence_em(agora) <= RENOVAR_ANTES_SEGUNDOS:
            novo = await self.provedor.pedir(self.provedor.identidade.escopo)
            if self._atual is not None:
                self._antigos.append(self._atual)
            self._atual = novo
            log.info("cracha_do_programa_renovado", programa=self.provedor.identidade.client_id)
        return self._atual.token

    async def outro_condominio(self) -> str | None:
        escopo = self.provedor.identidade.escopo_outro_condominio
        if not escopo:
            return None
        return (await self.provedor.pedir(escopo)).token

    def vencido(self) -> str | None:
        agora = self._relogio()
        for antigo in reversed(self._antigos):
            if antigo.expira_em + MARGEM_VENCIDO_SEGUNDOS < agora:
                return antigo.token
        return None


def fonte_do_programa(
    identidade: IdentidadePrograma,
    *,
    verify: ssl.SSLContext | None = None,
    transporte: httpx2.AsyncBaseTransport | None = None,
) -> CrachasDoProvedor | None:
    """Fonte de crachas a partir dos parametros. ``None`` sem chave configurada (a ronda fica sem identidade)."""
    if not identidade.configurada:
        log.warning("programa_sem_chave", programa=identidade.client_id)
        return None
    return CrachasDoProvedor(ProvedorDeCrachas(identidade, verify=verify, transporte=transporte))


# --- Cracha falso -------------------------------------------------------------------------------

_FAMILIA: Final[dict[str, str]] = {
    "RS256": "rsa",
    "RS384": "rsa",
    "RS512": "rsa",
    "PS256": "rsa",
    "PS384": "rsa",
    "PS512": "rsa",
    "ES256": "ec256",
    "ES384": "ec384",
    "ES512": "ec521",
    "EdDSA": "ed25519",
}


class ChavesProprias:
    """Chaves geradas pela ronda, uma por familia de algoritmo. O provedor nunca as viu."""

    def __init__(self) -> None:
        self._chaves: dict[str, Any] = {}

    def para(self, alg: str) -> Any:
        familia = _FAMILIA.get(alg, "rsa")
        if familia not in self._chaves:
            if familia == "rsa":
                self._chaves[familia] = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            elif familia == "ec256":
                self._chaves[familia] = ec.generate_private_key(ec.SECP256R1())
            elif familia == "ec384":
                self._chaves[familia] = ec.generate_private_key(ec.SECP384R1())
            elif familia == "ec521":
                self._chaves[familia] = ec.generate_private_key(ec.SECP521R1())
            else:
                self._chaves[familia] = ed25519.Ed25519PrivateKey.generate()
        return self._chaves[familia]


CHAVES_PROPRIAS: Final = ChavesProprias()


def forjar_cracha(modelo: str | None, *, emissor: str, recurso: str, chaves: ChavesProprias = CHAVES_PROPRIAS) -> str:
    """Cracha falso com as reivindicacoes e o ``kid`` de ``modelo``, assinado por chave que o provedor nao conhece.

    Sem modelo (a ronda nao conseguiu cracha de verdade), monta um parecido com o de um programa.
    """
    agora = int(time.time())
    dados: dict[str, Any] = {}
    cabecalho: dict[str, Any] = {}
    if modelo:
        dados = reivindicacoes_sem_conferir(modelo)
        try:
            cabecalho = dict(jwt.get_unverified_header(modelo))
        except jwt.PyJWTError:
            cabecalho = {}
    alg = cabecalho.get("alg") if cabecalho.get("alg") in _FAMILIA else "RS256"
    dados = dados or {"iss": emissor, "aud": recurso, "sub": "programa-falso", "azp": "programa-falso"}
    dados |= {"iat": agora, "nbf": agora, "exp": agora + 300, "jti": str(uuid.uuid4())}
    novo_cabecalho: dict[str, Any] = {"typ": "JWT"}
    if isinstance(cabecalho.get("kid"), str):
        novo_cabecalho["kid"] = cabecalho["kid"]
    else:
        novo_cabecalho["kid"] = "ronda-falsa"
    return jwt.encode(dados, chaves.para(str(alg)), algorithm=str(alg), headers=novo_cabecalho)
