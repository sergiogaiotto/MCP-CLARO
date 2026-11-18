"""Cracha da ronda num Keycloak 26.8.2 de verdade: private_key_jwt com ES256 e cracha de outro condominio.

Sobe o Keycloak em modo de desenvolvimento com um realm minimo, montado aqui com a mesma forma do
``docker/login/realm-mcp-claro.json`` para o cliente ``mcp-claro-ronda``: conta de servico, JWT
assinado (ES256) com a chave publica em ``jwks.string``, e os escopos ``mcp-claro`` (audiencia do
MCP-CLARO) e ``outro-servico`` (audiencia de um segundo recurso de teste) como escopos opcionais.
O verificador de cracha da entrada confere o cracha com as chaves publicas do proprio Keycloak.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from jwt.algorithms import ECAlgorithm

from mcp_claro.config.settings import Parametros
from mcp_claro.entrada.cracha import ChavesPublicas, CrachaRecusado, VerificadorCracha
from mcp_claro.ronda.config import IdentidadePrograma
from mcp_claro.ronda.cracha import CrachasDoProvedor, FalhaCracha, ProvedorDeCrachas

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

IMAGEM_KEYCLOAK = "keycloak/keycloak:26.8.2"
RECURSO = "https://mcp-claro.teste/mcp"
OUTRO = "https://outro-servico.teste/api"
KID = "mcp-claro-ronda-1"


def _escopo_audiencia(nome: str, audiencia: str) -> dict[str, Any]:
    return {
        "name": nome,
        "protocol": "openid-connect",
        "attributes": {"include.in.token.scope": "true", "display.on.consent.screen": "false"},
        "protocolMappers": [
            {
                "name": f"audiencia-{nome}",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-audience-mapper",
                "consentRequired": False,
                "config": {
                    "included.custom.audience": audiencia,
                    "access.token.claim": "true",
                    "id.token.claim": "false",
                    "introspection.token.claim": "true",
                },
            }
        ],
    }


def realm(chave: ec.EllipticCurvePrivateKey) -> dict[str, Any]:
    publica = ECAlgorithm.to_jwk(chave.public_key(), as_dict=True) | {"kid": KID, "use": "sig", "alg": "ES256"}
    basico = {
        "name": "basic",
        "protocol": "openid-connect",
        "attributes": {"include.in.token.scope": "false"},
        "protocolMappers": [
            {
                "name": "sub",
                "protocol": "openid-connect",
                "protocolMapper": "oidc-sub-mapper",
                "consentRequired": False,
                "config": {"access.token.claim": "true", "introspection.token.claim": "true"},
            }
        ],
    }
    return {
        "realm": "mcp-claro",
        "enabled": True,
        "accessTokenLifespan": 300,
        "defaultDefaultClientScopes": [],
        "defaultOptionalClientScopes": [],
        "clientScopes": [basico, _escopo_audiencia("mcp-claro", RECURSO), _escopo_audiencia("outro-servico", OUTRO)],
        "clients": [
            {
                "clientId": "mcp-claro-ronda",
                "enabled": True,
                "protocol": "openid-connect",
                "publicClient": False,
                "clientAuthenticatorType": "client-jwt",
                "standardFlowEnabled": False,
                "directAccessGrantsEnabled": False,
                "serviceAccountsEnabled": True,
                "fullScopeAllowed": False,
                "attributes": {
                    "access.token.lifespan": "300",
                    "use.jwks.url": "false",
                    "use.jwks.string": "true",
                    "jwks.string": json.dumps({"keys": [publica]}),
                    "token.endpoint.auth.signing.alg": "ES256",
                    "token.endpoint.auth.signing.max.exp": "60",
                    "use.refresh.tokens": "false",
                },
                "defaultClientScopes": ["basic"],
                "optionalClientScopes": ["mcp-claro", "outro-servico"],
            }
        ],
    }


@pytest.fixture(scope="module")
def chave() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@pytest.fixture(scope="module")
def keycloak(tmp_path_factory: pytest.TempPathFactory, chave: ec.EllipticCurvePrivateKey) -> Iterator[str]:
    container_mod = pytest.importorskip("testcontainers.core.container")
    pasta = tmp_path_factory.mktemp("realm")
    (pasta / "mcp-claro.json").write_text(json.dumps(realm(chave)), encoding="utf-8")
    pasta.chmod(0o755)
    (pasta / "mcp-claro.json").chmod(0o644)
    c = (
        container_mod.DockerContainer(IMAGEM_KEYCLOAK)
        .with_exposed_ports(8080)
        .with_volume_mapping(str(pasta), "/opt/keycloak/data/import", "ro")
        .with_command("start-dev --import-realm")
    )
    with c:
        base = f"http://{c.get_container_host_ip()}:{c.get_exposed_port(8080)}/realms/mcp-claro"
        fim = time.monotonic() + 180
        while time.monotonic() < fim:
            try:
                if httpx2.get(f"{base}/.well-known/openid-configuration", timeout=2).status_code == 200:
                    break
            except httpx2.TransportError:
                pass
            time.sleep(1)
        else:
            pytest.fail("o Keycloak nao subiu em 3 minutos")
        yield base


def identidade(emissor: str, **kw: Any) -> IdentidadePrograma:
    valores: dict[str, Any] = {
        "client_id": "mcp-claro-ronda",
        "arquivo_chave": None,
        "kid": KID,
        "token_url": f"{emissor}/protocol/openid-connect/token",
        "escopo": "mcp-claro",
        "escopo_outro_condominio": "outro-servico",
        "emissor": emissor,
        "recurso": RECURSO,
        "audiencia_assercao": emissor,  # como em producao com IDP_KIND=keycloak
    }
    return IdentidadePrograma(**(valores | kw))


def verificador(emissor: str, http: httpx2.AsyncClient) -> VerificadorCracha:
    p = Parametros(
        APP_ENV="teste", PUBLIC_BASE_URL="https://mcp-claro.teste", IDP_KIND="keycloak", IDP_ISSUER_URL=emissor
    )
    return VerificadorCracha(
        p, ChavesPublicas(f"{emissor}/protocol/openid-connect/certs", cache_segundos=60, http=http)
    )


async def test_ronda_pede_cracha_ao_keycloak_com_jwt_assinado(keycloak: str, chave: ec.EllipticCurvePrivateKey) -> None:
    fonte = CrachasDoProvedor(ProvedorDeCrachas(identidade(keycloak), chave=chave))
    cracha = await fonte.valido()
    dados = jwt.decode(cracha, options={"verify_signature": False})
    assert dados["azp"] == "mcp-claro-ronda"
    assert RECURSO in ([dados["aud"]] if isinstance(dados["aud"], str) else dados["aud"])
    async with httpx2.AsyncClient() as http:
        conferido = await verificador(keycloak, http).verificar(cracha)
        assert conferido.client_id == "mcp-claro-ronda"
        # Outro condominio: cracha de verdade do mesmo Keycloak, mas preso ao outro recurso.
        outro = await fonte.outro_condominio()
        assert outro is not None
        aud = jwt.decode(outro, options={"verify_signature": False})["aud"]
        assert RECURSO not in ([aud] if isinstance(aud, str) else aud)
        with pytest.raises(CrachaRecusado, match="audiencia"):
            await verificador(keycloak, http).verificar(outro)


async def test_keycloak_recusa_chave_desconhecida(keycloak: str) -> None:
    provedor = ProvedorDeCrachas(identidade(keycloak), chave=ec.generate_private_key(ec.SECP256R1()))
    with pytest.raises(FalhaCracha, match="HTTP 40"):
        await provedor.pedir("mcp-claro")


async def test_afirmacao_com_audiencia_no_token_endpoint(keycloak: str, chave: ec.EllipticCurvePrivateKey) -> None:
    """A outra forma do RFC 7523 (``aud`` no token endpoint), usada com provedores que nao sao o Keycloak."""
    provedor = ProvedorDeCrachas(identidade(keycloak, audiencia_assercao=None), chave=chave)
    assert (await provedor.pedir("mcp-claro")).token


async def test_descoberta_do_token_endpoint_no_keycloak(keycloak: str, chave: ec.EllipticCurvePrivateKey) -> None:
    provedor = ProvedorDeCrachas(identidade(keycloak, token_url=None), chave=chave)
    obtido = await provedor.pedir("mcp-claro")
    assert obtido.expira_em > time.time()


def test_realm_de_teste_tem_a_forma_do_realm_do_produto(chave: ec.EllipticCurvePrivateKey) -> None:
    """O cliente da ronda daqui segue o do realm do produto, com a mudanca proposta nos escopos."""
    produto = Path(__file__).resolve().parents[2] / "docker" / "login" / "realm-mcp-claro.json"
    if not produto.exists():
        pytest.skip("docker/login/realm-mcp-claro.json ainda nao esta nesta copia")
    clientes = {c["clientId"]: c for c in json.loads(produto.read_text(encoding="utf-8"))["clients"]}
    ronda = clientes["mcp-claro-ronda"]
    nosso = realm(chave)["clients"][0]
    for chave_atributo in ("token.endpoint.auth.signing.alg", "token.endpoint.auth.signing.max.exp", "use.jwks.string"):
        assert ronda["attributes"][chave_atributo] == nosso["attributes"][chave_atributo]
    assert ronda["clientAuthenticatorType"] == "client-jwt"
