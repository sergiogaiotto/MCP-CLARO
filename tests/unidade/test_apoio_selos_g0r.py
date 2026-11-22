"""Ajudante de enderecos de duble de ``tests/apoio_selos.py`` (G0, etapa 1r): producao e alvo com caminho unico.

``selo_alvo_na_producao`` compara o alvo de teste com a producao de todo sistema do banco da sessao. Os dubles
escutam em ``127.0.0.1`` em portas que o sistema operacional reaproveita; com um caminho unico por endereco, dois
enderecos na mesma porta nunca sao o mesmo na forma normalizada."""

from __future__ import annotations

import pytest

from mcp_claro.selo import contrato
from tests import apoio_selos as apoio


def test_dois_enderecos_na_mesma_porta_nunca_colidem() -> None:
    base = "http://127.0.0.1:8123"
    producao, alvo = apoio.endereco_de_duble(base), apoio.endereco_de_duble(base)
    assert producao.startswith(base + "/selo-")
    assert producao.endswith("/mcp")
    assert not contrato.mesmo_endereco(producao, alvo)
    # A forma normalizada guarda o caminho como foi escrito: e ela que o banco compara.
    assert contrato.endereco_normalizado(producao) == producao
    assert contrato.mesmo_endereco(producao, producao.upper().replace("HTTP", "http") + "/")


def test_caminho_unico_e_o_endereco_com_caminho_dado() -> None:
    caminho = apoio.caminho_unico("teste")
    assert caminho.startswith("/selo-")
    assert caminho.endswith("/teste")
    assert caminho != apoio.caminho_unico("teste")
    assert apoio.endereco_de_duble("http://127.0.0.1:9", caminho) == "http://127.0.0.1:9" + caminho
    assert apoio.endereco_de_duble("http://127.0.0.1:9/", caminho) == "http://127.0.0.1:9" + caminho


@pytest.mark.parametrize("base", ["http://127.0.0.1:9/mcp", "127.0.0.1:9", "http://127.0.0.1:9?x=1", "http://"])
def test_base_do_duble_e_so_esquema_nome_e_porta(base: str) -> None:
    with pytest.raises(ValueError, match="sem caminho"):
        apoio.endereco_de_duble(base)


@pytest.mark.parametrize("papel", ["", "MCP", "a/b", "selo teste"])
def test_papel_do_caminho_e_um_nome_curto(papel: str) -> None:
    with pytest.raises(ValueError, match="nome curto"):
        apoio.caminho_unico(papel)
    with pytest.raises(ValueError, match="comeca com /"):
        apoio.endereco_de_duble("http://127.0.0.1:9", "mcp")
