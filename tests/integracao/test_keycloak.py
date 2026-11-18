"""Provedor de login real (Keycloak 26.8.2) contra o verificador de cracha do produto.

Sobe o Keycloak em Docker com o realm de ``docker/login/realm-mcp-claro.json`` e prova,
com o ``VerificadorCracha`` real: o cracha certo passa e diz qual aplicativo e (azp); o
cracha de outro destino (audiencia de outro recurso) e o cracha adulterado sao recusados.
Prova tambem o que o realm promete: a ronda entra com JWT assinado, aplicativos publicos
exigem PKCE S256 e retorno em loopback, e nao ha cadastro anonimo de clientes.

O emissor e fixo e igual ao do Compose: ``KC_HOSTNAME`` e a URL publica atras do Caddy
(https://mcp-claro.localhost/login), entao o ``iss`` nao depende da porta do conteiner.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from mcp_claro.config.settings import Parametros
from mcp_claro.entrada.cracha import ChavesPublicas, Cracha, CrachaRecusado, VerificadorCracha

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

IMAGEM_KEYCLOAK = os.environ.get(
    "MCP_CLARO_TESTE_KEYCLOAK",
    "keycloak/keycloak:26.8.2@sha256:c68e286cacd87b942ac1939a0a1481461109d6acd3a57dc496969a319a76bbf0",
)
REALM = Path(__file__).resolve().parents[2] / "docker" / "login" / "realm-mcp-claro.json"
PUBLICO = "https://mcp-claro.localhost"
RECURSO = f"{PUBLICO}/mcp"
EMISSOR = f"{PUBLICO}/login/realms/mcp-claro"
KID_RONDA = "ronda-teste-1"


@dataclass(frozen=True)
class Login:
    realm: str  # endereco do realm visto daqui (porta do conteiner)
    segredo_cc: str
    segredo_outro: str
    chave_ronda: ec.EllipticCurvePrivateKey

    @property
    def jwks(self) -> str:
        return f"{self.realm}/protocol/openid-connect/certs"

    @property
    def token(self) -> str:
        return f"{self.realm}/protocol/openid-connect/token"


def _realm_com_chave_da_ronda(publica: dict[str, Any]) -> bytes:
    """O realm do repositorio, so com a chave publica de teste da ronda cadastrada.

    No realm guardado a ronda vem sem chave (``{"keys":[]}``): sem chave, nao entra. Aqui o
    teste faz o papel do ADMIN e cadastra a chave publica que acabou de gerar.
    """
    realm = json.loads(REALM.read_text(encoding="utf-8"))
    ronda = next(c for c in realm["clients"] if c["clientId"] == "mcp-claro-ronda")
    ronda["attributes"]["jwks.string"] = json.dumps({"keys": [publica]})
    return json.dumps(realm).encode()


@pytest.fixture(scope="module")
def login() -> Iterator[Login]:
    pytest.importorskip("testcontainers")
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.wait_strategies import HttpWaitStrategy

    chave_ronda = ec.generate_private_key(ec.SECP256R1())
    publica = ECAlgorithm.to_jwk(chave_ronda.public_key(), as_dict=True)
    publica |= {"kid": KID_RONDA, "alg": "ES256", "use": "sig"}
    segredo_cc, segredo_outro = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    conteiner = (
        DockerContainer(IMAGEM_KEYCLOAK)
        .with_command(["start-dev", "--import-realm"])
        .with_envs(
            KC_HOSTNAME=f"{PUBLICO}/login",
            KC_HTTP_RELATIVE_PATH="/login",
            KC_HTTP_MANAGEMENT_RELATIVE_PATH="/",
            KC_HEALTH_ENABLED="true",
            KC_PROXY_HEADERS="xforwarded",
            MCP_CLARO_RECURSO=RECURSO,
            MCP_CLARO_CLIENTES_DE_TESTE="true",
            MCP_CLARO_TESTE_CC_SEGREDO=segredo_cc,
            MCP_CLARO_OUTRO_SERVICO_SEGREDO=segredo_outro,
        )
        .with_exposed_ports(8080)
        .with_copy_into_container(_realm_com_chave_da_ronda(publica), "/opt/keycloak/data/import/realm-mcp-claro.json")
        .waiting_for(
            HttpWaitStrategy(8080, "/login/realms/mcp-claro/.well-known/openid-configuration").with_startup_timeout(180)
        )
    )
    with conteiner:
        base = f"http://{conteiner.get_container_host_ip()}:{conteiner.get_exposed_port(8080)}"
        yield Login(f"{base}/login/realms/mcp-claro", segredo_cc, segredo_outro, chave_ronda)


@pytest.fixture
async def http() -> AsyncIterator[httpx2.AsyncClient]:
    async with httpx2.AsyncClient(timeout=httpx2.Timeout(10.0), follow_redirects=False) as cliente:
        yield cliente


def _parametros(login: Login) -> Parametros:
    return Parametros(
        APP_ENV="teste",
        PUBLIC_BASE_URL=PUBLICO,
        IDP_KIND="keycloak",
        IDP_ISSUER_URL=EMISSOR,
        IDP_JWKS_URL=login.jwks,
    )


async def _verificar(login: Login, http: httpx2.AsyncClient, token: str) -> Cracha:
    p = _parametros(login)
    assert p.recurso == RECURSO
    chaves = ChavesPublicas(p.IDP_JWKS_URL or "", cache_segundos=p.IDP_JWKS_CACHE_SECONDS, http=http)
    return await VerificadorCracha(p, chaves).verificar(token)


async def _cracha_por_segredo(http: httpx2.AsyncClient, login: Login, client_id: str, segredo: str) -> str:
    resposta = await http.post(login.token, data={"grant_type": "client_credentials"}, auth=(client_id, segredo))
    assert resposta.status_code == 200, resposta.text
    return str(resposta.json()["access_token"])


def _assercao(client_id: str, chave: ec.EllipticCurvePrivateKey, kid: str) -> str:
    """JWT assinado pelo programa (private_key_jwt). A audiencia e o emissor do realm."""
    agora = int(time.time())
    dados = {"iss": client_id, "sub": client_id, "aud": EMISSOR, "jti": str(uuid.uuid4()), "iat": agora}
    return jwt.encode(dados | {"exp": agora + 60}, chave, algorithm="ES256", headers={"kid": kid})


async def _cracha_por_jwt(http: httpx2.AsyncClient, login: Login, assercao: str) -> httpx2.Response:
    return await http.post(
        login.token,
        data={
            "grant_type": "client_credentials",
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assercao,
        },
    )


def _sem_conferir(token: str) -> dict[str, Any]:
    return dict(jwt.decode(token, options={"verify_signature": False}))


def _b64(dados: bytes) -> str:
    return base64.urlsafe_b64encode(dados).rstrip(b"=").decode()


def _de_b64(texto: str) -> bytes:
    return base64.urlsafe_b64decode(texto + "=" * (-len(texto) % 4))


async def test_cracha_certo_e_aceito_com_client_id_do_azp(login: Login, http: httpx2.AsyncClient) -> None:
    token = await _cracha_por_segredo(http, login, "mcp-claro-teste-cc", login.segredo_cc)
    dados = _sem_conferir(token)
    assert dados["iss"] == EMISSOR
    assert dados["azp"] == "mcp-claro-teste-cc"
    assert RECURSO in ([dados["aud"]] if isinstance(dados["aud"], str) else dados["aud"])

    cracha = await _verificar(login, http, token)

    assert cracha.client_id == dados["azp"] == "mcp-claro-teste-cc"
    assert cracha.emissor == EMISSOR
    assert cracha.sujeito == dados["sub"]
    assert "mcp-claro" in cracha.escopos
    assert cracha.emitido_em is not None
    assert cracha.expira_em - cracha.emitido_em == 300


async def test_cracha_de_outro_servico_e_recusado_pela_audiencia(login: Login, http: httpx2.AsyncClient) -> None:
    token = await _cracha_por_segredo(http, login, "outro-servico", login.segredo_outro)
    dados = _sem_conferir(token)
    # Mesmo emissor e mesma chave de assinatura: so o destino e outro.
    assert dados["iss"] == EMISSOR
    assert dados["aud"] == "https://outro-servico.localhost/api"

    with pytest.raises(CrachaRecusado) as recusa:
        await _verificar(login, http, token)

    assert recusa.value.motivo == "cracha de outro destino (audiencia)"


@pytest.mark.parametrize("onde", ["carga", "assinatura"])
async def test_cracha_alterado_e_recusado(login: Login, http: httpx2.AsyncClient, onde: str) -> None:
    token = await _cracha_por_segredo(http, login, "mcp-claro-teste-cc", login.segredo_cc)
    cabecalho, carga, assinatura = token.split(".")
    if onde == "carga":
        # Tenta se passar por outro aplicativo mantendo a assinatura original.
        dados = json.loads(_de_b64(carga)) | {"azp": "claude-code"}
        carga = _b64(json.dumps(dados, separators=(",", ":")).encode())
    else:
        bytes_assinatura = bytearray(_de_b64(assinatura))
        bytes_assinatura[len(bytes_assinatura) // 2] ^= 0x01
        assinatura = _b64(bytes(bytes_assinatura))
    adulterado = f"{cabecalho}.{carga}.{assinatura}"
    assert adulterado != token

    with pytest.raises(CrachaRecusado) as recusa:
        await _verificar(login, http, adulterado)

    assert recusa.value.motivo == "cracha invalido"


async def test_ronda_entra_com_jwt_assinado_e_vigia_sem_chave_nao(login: Login, http: httpx2.AsyncClient) -> None:
    resposta = await _cracha_por_jwt(http, login, _assercao("mcp-claro-ronda", login.chave_ronda, KID_RONDA))
    assert resposta.status_code == 200, resposta.text
    corpo = resposta.json()
    assert corpo["expires_in"] == 300
    assert "refresh_token" not in corpo

    cracha = await _verificar(login, http, corpo["access_token"])
    assert cracha.client_id == "mcp-claro-ronda"
    assert cracha.expira_em - (cracha.emitido_em or 0) == 300

    # O vigia esta pre-cadastrado, mas sem chave publica: nao entra, nem com uma chave qualquer.
    estranha = ec.generate_private_key(ec.SECP256R1())
    recusa = await _cracha_por_jwt(http, login, _assercao("mcp-claro-vigia", estranha, KID_RONDA))
    assert recusa.status_code in (400, 401)
    assert recusa.json()["error"] == "invalid_client"


@pytest.mark.parametrize("aplicativo", ["claude-code", "goose"])
async def test_aplicativo_publico_exige_pkce_s256_e_retorno_em_loopback(
    login: Login, http: httpx2.AsyncClient, aplicativo: str
) -> None:
    verificador = secrets.token_urlsafe(48)
    desafio = _b64(hashlib.sha256(verificador.encode()).digest())
    pedido = {
        "client_id": aplicativo,
        "response_type": "code",
        "scope": "openid",
        "state": secrets.token_urlsafe(8),
        "redirect_uri": "http://127.0.0.1:43123/callback",
        "code_challenge": desafio,
        "code_challenge_method": "S256",
    }
    autorizar = f"{login.realm}/protocol/openid-connect/auth"

    certo = await http.get(autorizar, params=pedido)
    assert certo.status_code == 200  # tela de login da pessoa

    sem_pkce = await http.get(autorizar, params={k: v for k, v in pedido.items() if not k.startswith("code_challenge")})
    assert sem_pkce.status_code == 302
    assert parse_qs(urlsplit(sem_pkce.headers["location"]).query)["error"] == ["invalid_request"]

    plain = await http.get(autorizar, params=pedido | {"code_challenge": verificador, "code_challenge_method": "plain"})
    assert plain.status_code == 302
    assert parse_qs(urlsplit(plain.headers["location"]).query)["error"] == ["invalid_request"]

    fora_do_loopback = await http.get(autorizar, params=pedido | {"redirect_uri": "https://atacante.exemplo/cb"})
    assert fora_do_loopback.status_code == 400


async def test_sem_cadastro_anonimo_de_clientes(login: Login, http: httpx2.AsyncClient) -> None:
    resposta = await http.post(
        f"{login.realm}/clients-registrations/openid-connect",
        json={"client_name": "intruso", "redirect_uris": ["http://127.0.0.1/cb"]},
    )
    assert resposta.status_code == 403
