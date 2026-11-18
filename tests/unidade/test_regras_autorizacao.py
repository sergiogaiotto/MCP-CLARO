"""Autorizacao no sistema, sem banco: PKCE S256, configuracao, endereco montado e resposta da troca."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from mcp_claro.despachante import autorizacao
from mcp_claro.despachante.autorizacao import ConfigAutorizacao, ConfiguracaoAutorizacaoInvalida, RespostaToken

CONFIG = {
    "authorization_endpoint": "https://login.sistema.exemplo/authorize?tenant=claro",
    "token_endpoint": "https://login.sistema.exemplo/token",
    "client_id": "mcp-claro",
    "escopos": ["faturas.ler", "cadastro.ler"],
}


def test_desafio_s256_bate_com_o_exemplo_da_rfc_7636() -> None:
    assert autorizacao.desafio_s256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )
    verificador = autorizacao.novo_verificador()
    assert 43 <= len(verificador) <= 128
    assert verificador != autorizacao.novo_verificador()


def test_configuracao_valida_e_padroes() -> None:
    config = ConfigAutorizacao.de_json(CONFIG)
    assert config.autenticacao_cliente == "client_secret_basic"
    assert config.usa_segredo
    assert ConfigAutorizacao.de_json(config.como_json()) == config
    publico = ConfigAutorizacao.de_json(CONFIG | {"autenticacao_cliente": "none"})
    assert not publico.usa_segredo


@pytest.mark.parametrize(
    ("mudanca", "trecho"),
    [
        ({"token_endpoint": "http://login.sistema.exemplo/token"}, "https://"),
        ({"authorization_endpoint": None}, "Falta o endereco"),
        ({"authorization_endpoint": "https://x.exemplo/a?state=1"}, "nao pode trazer state"),
        ({"token_endpoint": "https://user:senha@x.exemplo/t"}, "usuario"),
        ({"client_id": ""}, "client_id"),
        ({"escopos": ["com espaco"]}, "escopos"),
        ({"autenticacao_cliente": "private_key_jwt"}, "autenticacao_cliente"),
        ({"segredo": "x"}, "Campos desconhecidos"),
    ],
)
def test_configuracao_recusada_com_frase_legivel(mudanca: dict[str, object], trecho: str) -> None:
    with pytest.raises(ConfiguracaoAutorizacaoInvalida, match=trecho):
        ConfigAutorizacao.de_json(CONFIG | mudanca)


def test_http_so_em_endereco_local() -> None:
    assert autorizacao.conferir_endereco("http://127.0.0.1:8080/token", "t")
    assert autorizacao.conferir_endereco("http://localhost/token", "t")
    with pytest.raises(ConfiguracaoAutorizacaoInvalida):
        autorizacao.conferir_endereco("http://10.0.0.1/token", "t")


def test_endereco_de_autorizacao_leva_pkce_estado_e_recurso() -> None:
    config = ConfigAutorizacao.de_json(CONFIG)
    url = autorizacao.montar_url(
        config,
        estado="estado-1",
        desafio="desafio-1",
        redirect_uri="https://mcp-claro.exemplo/telas/autorizacao/volta",
        recurso="https://sistema.exemplo/mcp",
    )
    partes = urlsplit(url)
    assert partes.netloc == "login.sistema.exemplo"
    q = {k: v[0] for k, v in parse_qs(partes.query).items()}
    assert q == {
        "tenant": "claro",
        "response_type": "code",
        "client_id": "mcp-claro",
        "redirect_uri": "https://mcp-claro.exemplo/telas/autorizacao/volta",
        "state": "estado-1",
        "code_challenge": "desafio-1",
        "code_challenge_method": "S256",
        "scope": "faturas.ler cadastro.ler",
        "resource": "https://sistema.exemplo/mcp",
    }


def test_resposta_da_troca() -> None:
    agora = datetime(2026, 11, 18, 12, tzinfo=UTC)
    r = RespostaToken.de_json(
        {"access_token": "abc", "token_type": "bearer", "expires_in": 3600, "refresh_token": "def"}, agora=agora
    )
    assert (r.acesso, r.renovacao) == ("abc", "def")
    assert r.expira_em is not None
    assert (r.expira_em - agora).total_seconds() == 3600
    sem_prazo = RespostaToken.de_json({"access_token": "abc", "token_type": "Bearer"})
    assert sem_prazo.expira_em is None
    assert sem_prazo.renovacao is None
    for ruim in (
        {"access_token": "abc\r\nX-Injetado: 1", "token_type": "Bearer"},
        {"access_token": "abc", "token_type": "mac"},
        {"access_token": "", "token_type": "Bearer"},
        {"token_type": "Bearer"},
        {"access_token": "abc", "token_type": "Bearer", "refresh_token": 12},
        ["nao", "e", "objeto"],
    ):
        with pytest.raises(autorizacao.TrocaRecusada):
            RespostaToken.de_json(ruim)


def test_contextos_e_trava_separam_pessoas_e_sistemas() -> None:
    ana, bia, s1, s2 = (uuid.uuid4() for _ in range(4))
    assert autorizacao.chave_da_trava(ana, s1) == autorizacao.chave_da_trava(ana, s1)
    assert len({autorizacao.chave_da_trava(p, s) for p in (ana, bia) for s in (s1, s2)}) == 4
    assert -(2**63) <= autorizacao.chave_da_trava(ana, s1) < 2**63
    assert autorizacao.contexto_pendente("e", ana, s1) != autorizacao.contexto_pendente("e", bia, s1)
    assert autorizacao.contexto_segredo_cliente(s1).endswith(b"|cliente-oauth")
