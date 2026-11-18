"""Cracha da ronda: afirmacao do cliente (private_key_jwt, ES256), pedido ao token endpoint e cracha falso.

O provedor de login e falso (``httpx2.MockTransport``) e confere a afirmacao como um provedor de
verdade: assinatura com a chave publica cadastrada, ``iss``/``sub`` iguais ao cliente, ``aud`` no
token endpoint e validade de no maximo 60 segundos.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from mcp_claro.config.settings import ConfiguracaoInvalida, Parametros
from mcp_claro.ronda.config import IdentidadePrograma, identidade_da_ronda, url_token
from mcp_claro.ronda.cracha import (
    TIPO_ASSERCAO,
    ChavesProprias,
    CrachasDoProvedor,
    FalhaCracha,
    ProvedorDeCrachas,
    assercao_cliente,
    carregar_chave,
    fonte_do_programa,
    forjar_cracha,
)
from tests.idp_falso import IdpFalso

EMISSOR = "https://login.teste/realms/mcp-claro"
TOKEN = f"{EMISSOR}/protocol/openid-connect/token"
RECURSO = "https://mcp-claro.teste/mcp"


def identidade(**kw: Any) -> IdentidadePrograma:
    valores: dict[str, Any] = {
        "client_id": "mcp-claro-ronda",
        "arquivo_chave": "/nao/usado",
        "kid": "ronda-1",
        "token_url": TOKEN,
        "escopo": "mcp-claro",
        "escopo_outro_condominio": "outro-servico",
        "emissor": EMISSOR,
        "recurso": RECURSO,
    }
    return IdentidadePrograma(**(valores | kw))


class ProvedorFalso:
    """Token endpoint falso: confere a afirmacao do cliente e devolve um cracha do ``IdpFalso``."""

    def __init__(self, chave_publica: ec.EllipticCurvePublicKey, *, recusar: bool = False) -> None:
        self.chave_publica = chave_publica
        self.recusar = recusar
        self.idp = IdpFalso(emissor=EMISSOR)
        self.pedidos: list[dict[str, str]] = []
        self.descobertas = 0
        self.validade = 300
        self.audiencia: str | None = None

    def __call__(self, pedido: httpx2.Request) -> httpx2.Response:
        if pedido.url.path.endswith("/.well-known/openid-configuration"):
            self.descobertas += 1
            return httpx2.Response(200, json={"issuer": EMISSOR, "token_endpoint": TOKEN})
        form = dict(parse_qsl(pedido.content.decode()))
        self.pedidos.append(form)
        if self.recusar:
            return httpx2.Response(401, json={"error": "invalid_client", "error_description": "nao"})
        assert form["grant_type"] == "client_credentials"
        assert form["client_assertion_type"] == TIPO_ASSERCAO
        cabecalho = jwt.get_unverified_header(form["client_assertion"])
        assert cabecalho["alg"] == "ES256"
        assert cabecalho["kid"] == "ronda-1"
        audiencia = self.audiencia or str(pedido.url)
        dados = jwt.decode(form["client_assertion"], self.chave_publica, algorithms=["ES256"], audience=audiencia)
        assert dados["iss"] == dados["sub"] == form["client_id"] == "mcp-claro-ronda"
        assert dados["exp"] - dados["iat"] <= 60
        assert dados["jti"]
        audiencia = "https://outro-servico.teste/api" if form.get("scope") == "outro-servico" else form["resource"]
        cracha = self.idp.cracha(
            audiencia=audiencia,
            sujeito="service-account-mcp-claro-ronda",
            client_id="mcp-claro-ronda",
            validade=self.validade,
        )
        return httpx2.Response(200, json={"access_token": cracha, "token_type": "Bearer", "expires_in": 300})


@pytest.fixture
def chave() -> ec.EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


@pytest.mark.anyio
async def test_pede_cracha_com_afirmacao_es256(chave: ec.EllipticCurvePrivateKey) -> None:
    falso = ProvedorFalso(chave.public_key())
    provedor = ProvedorDeCrachas(identidade(), chave=chave, transporte=httpx2.MockTransport(falso))
    obtido = await provedor.pedir("mcp-claro")
    (form,) = falso.pedidos
    assert form["scope"] == "mcp-claro"
    assert form["resource"] == RECURSO
    dados = jwt.decode(obtido.token, options={"verify_signature": False})
    assert dados["aud"] == RECURSO
    assert obtido.expira_em == dados["exp"]


@pytest.mark.anyio
async def test_no_keycloak_a_afirmacao_cita_o_emissor(chave: ec.EllipticCurvePrivateKey) -> None:
    falso = ProvedorFalso(chave.public_key())
    falso.audiencia = EMISSOR
    ident = identidade(audiencia_assercao=EMISSOR, token_url="http://login:8080/realms/mcp-claro/token")
    await ProvedorDeCrachas(ident, chave=chave, transporte=httpx2.MockTransport(falso)).pedir("mcp-claro")
    assert len(falso.pedidos) == 1
    p = Parametros(APP_ENV="teste")
    assert identidade_da_ronda(p).audiencia_assercao == p.IDP_ISSUER_URL
    assert identidade_da_ronda(Parametros(APP_ENV="teste", IDP_KIND="logto")).audiencia_assercao is None


@pytest.mark.anyio
async def test_descobre_o_token_endpoint_pelos_metadados(chave: ec.EllipticCurvePrivateKey) -> None:
    falso = ProvedorFalso(chave.public_key())
    provedor = ProvedorDeCrachas(identidade(token_url=None), chave=chave, transporte=httpx2.MockTransport(falso))
    await provedor.pedir("mcp-claro")
    await provedor.pedir("mcp-claro")
    assert falso.descobertas == 1
    assert len(falso.pedidos) == 2


@pytest.mark.anyio
async def test_recusa_do_provedor_vira_falha_sem_segredo(chave: ec.EllipticCurvePrivateKey) -> None:
    falso = ProvedorFalso(chave.public_key(), recusar=True)
    provedor = ProvedorDeCrachas(identidade(), chave=chave, transporte=httpx2.MockTransport(falso))
    with pytest.raises(FalhaCracha) as erro:
        await provedor.pedir("mcp-claro")
    assert "HTTP 401" in erro.value.motivo
    assert "invalid_client" in erro.value.motivo
    assert falso.pedidos[0]["client_assertion"] not in str(erro.value)


@pytest.mark.anyio
async def test_provedor_sem_resposta_vira_falha(chave: ec.EllipticCurvePrivateKey) -> None:
    def cair(_: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("sem rede")

    provedor = ProvedorDeCrachas(identidade(), chave=chave, transporte=httpx2.MockTransport(cair))
    with pytest.raises(FalhaCracha, match="sem resposta"):
        await provedor.pedir(None)


@pytest.mark.anyio
async def test_resposta_sem_bearer_e_recusada(chave: ec.EllipticCurvePrivateKey) -> None:
    def resposta(_: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"access_token": "x", "token_type": "DPoP"})

    provedor = ProvedorDeCrachas(identidade(), chave=chave, transporte=httpx2.MockTransport(resposta))
    with pytest.raises(FalhaCracha, match="Bearer"):
        await provedor.pedir(None)


@pytest.mark.anyio
async def test_guarda_o_cracha_e_os_antigos_para_o_caso_vencido(chave: ec.EllipticCurvePrivateKey) -> None:
    agora = [1_000_000.0]
    falso = ProvedorFalso(chave.public_key())
    provedor = ProvedorDeCrachas(identidade(), chave=chave, transporte=httpx2.MockTransport(falso))

    async def pedir(escopo: str | None) -> Any:
        from mcp_claro.ronda.cracha import CrachaObtido

        r = await ProvedorDeCrachas.pedir(provedor, escopo)
        return CrachaObtido(r.token, agora[0] + 300)

    provedor.pedir = pedir  # type: ignore[method-assign]
    fonte = CrachasDoProvedor(provedor, relogio=lambda: agora[0])
    primeiro = await fonte.valido()
    assert await fonte.valido() == primeiro
    assert len(falso.pedidos) == 1
    assert fonte.vencido() is None
    agora[0] += 280  # faltam 20 s: renova
    segundo = await fonte.valido()
    assert segundo != primeiro
    assert fonte.vencido() is None  # o antigo ainda nao venceu com folga
    agora[0] += 120  # o primeiro venceu ha 100 s
    assert fonte.vencido() == primeiro
    outro = await fonte.outro_condominio()
    assert outro is not None
    assert jwt.decode(outro, options={"verify_signature": False})["aud"] == "https://outro-servico.teste/api"


@pytest.mark.anyio
async def test_sem_escopo_de_outro_condominio_o_caso_fica_desligado(chave: ec.EllipticCurvePrivateKey) -> None:
    falso = ProvedorFalso(chave.public_key())
    provedor = ProvedorDeCrachas(
        identidade(escopo_outro_condominio=None), chave=chave, transporte=httpx2.MockTransport(falso)
    )
    assert await CrachasDoProvedor(provedor).outro_condominio() is None
    assert falso.pedidos == []


def test_afirmacao_do_cliente(chave: ec.EllipticCurvePrivateKey) -> None:
    afirmacao = assercao_cliente("mcp-claro-vigia", TOKEN, chave, kid="k1", agora=1_700_000_000)
    dados = jwt.decode(
        afirmacao, chave.public_key(), algorithms=["ES256"], audience=TOKEN, options={"verify_exp": False}
    )
    assert (dados["iss"], dados["sub"], dados["exp"] - dados["iat"]) == ("mcp-claro-vigia", "mcp-claro-vigia", 60)
    assert jwt.get_unverified_header(afirmacao) == {"alg": "ES256", "kid": "k1", "typ": "JWT"}
    assert assercao_cliente("x", TOKEN, chave) != assercao_cliente("x", TOKEN, chave)  # jti novo


def test_carregar_chave(tmp_path: Path, chave: ec.EllipticCurvePrivateKey) -> None:
    pem = tmp_path / "ronda.pem"
    pem.write_bytes(
        chave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    assert carregar_chave(str(pem)).public_key().public_numbers() == chave.public_key().public_numbers()
    rsa_pem = tmp_path / "rsa.pem"
    rsa_pem.write_bytes(
        rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
    )
    with pytest.raises(FalhaCracha, match="EC P-256"):
        carregar_chave(str(rsa_pem))
    lixo = tmp_path / "lixo.pem"
    lixo.write_text("nao e chave")
    with pytest.raises(FalhaCracha, match="ilegivel"):
        carregar_chave(str(lixo))
    with pytest.raises(FalhaCracha, match="ilegivel"):
        carregar_chave(str(tmp_path / "nao-existe.pem"))


def test_cracha_falso_copia_o_verdadeiro_mas_nao_tem_a_assinatura_do_provedor() -> None:
    idp = IdpFalso(emissor=EMISSOR)
    verdadeiro = idp.cracha(audiencia=RECURSO, sujeito="service-account-mcp-claro-ronda", client_id="mcp-claro-ronda")
    chaves = ChavesProprias()
    falso = forjar_cracha(verdadeiro, emissor=EMISSOR, recurso=RECURSO, chaves=chaves)
    assert jwt.get_unverified_header(falso)["kid"] == idp.kid
    assert jwt.get_unverified_header(falso)["alg"] == "RS256"
    claro = jwt.decode(falso, options={"verify_signature": False})
    original = jwt.decode(verdadeiro, options={"verify_signature": False})
    assert {k: claro[k] for k in ("iss", "sub", "aud", "azp")} == {k: original[k] for k in ("iss", "sub", "aud", "azp")}
    assert claro["jti"] != original["jti"]
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(falso, idp.chave.public_key(), algorithms=["RS256"], audience=RECURSO)
    # Assinado pela chave propria da ronda, que o provedor nunca viu.
    jwt.decode(falso, chaves.para("RS256").public_key(), algorithms=["RS256"], audience=RECURSO)


def test_cracha_falso_acompanha_o_algoritmo_e_funciona_sem_modelo() -> None:
    chave_es = ec.generate_private_key(ec.SECP256R1())
    verdadeiro = jwt.encode({"sub": "p", "aud": RECURSO}, chave_es, algorithm="ES256", headers={"kid": "es-1"})
    falso = forjar_cracha(verdadeiro, emissor=EMISSOR, recurso=RECURSO)
    assert jwt.get_unverified_header(falso)["alg"] == "ES256"
    sem_modelo = forjar_cracha(None, emissor=EMISSOR, recurso=RECURSO)
    dados = jwt.decode(sem_modelo, options={"verify_signature": False})
    assert (dados["iss"], dados["aud"]) == (EMISSOR, RECURSO)


def test_fonte_sem_chave_e_identidade_em_producao(tmp_path: Path) -> None:
    assert fonte_do_programa(identidade(arquivo_chave=None)) is None
    teste = Parametros(APP_ENV="teste")
    assert identidade_da_ronda(teste).client_id == "mcp-claro-ronda"
    assert url_token(teste, None) == f"{teste.IDP_ISSUER_URL}/protocol/openid-connect/token"
    assert url_token(Parametros(IDP_KIND="logto"), None) is None
    producao = Parametros(
        APP_ENV="producao", PUBLIC_BASE_URL="https://mcp.exemplo", INTERNAL_CA_FILE="ca", INTERNAL_CLIENT_CA_FILE="ca"
    )
    with pytest.raises(ConfiguracaoInvalida, match="PATROL_KEY_FILE"):
        identidade_da_ronda(producao)
