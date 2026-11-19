"""cadastro/impressao.py: a impressao do servidor inteiro (Fase 3) e a receita de cada ferramenta, que nao muda."""

from __future__ import annotations

import hashlib
import json

from mcp_claro.cadastro.impressao import canonico, impressao, impressao_servidor
from tests.apoio_terceiros import definicao, impressao_do_servidor

# O vetor fixo que o G0 deixou (pedidos_etapa1.md e test_migracao_0006): o banco nao confere a impressao do servidor,
# entao a receita fica presa aqui e no apoio dos testes.
IMPRESSAO_DO_SERVIDOR_FIXA = "sha256:35160fa4ef315f4d28c6117e94ca214a5035ba6c4c6093ffbf0470e9a69aec44"
PARES = [("relatório", "sha256:" + "1" * 64), ("agenda", "sha256:" + "2" * 64)]


def test_bate_com_o_vetor_fixo_em_qualquer_ordem() -> None:
    assert impressao_servidor(PARES) == IMPRESSAO_DO_SERVIDOR_FIXA
    assert impressao_servidor(reversed(PARES)) == IMPRESSAO_DO_SERVIDOR_FIXA
    assert impressao_servidor(iter(PARES)) == IMPRESSAO_DO_SERVIDOR_FIXA  # aceita um iteravel de uma passada so
    # O apoio dos testes passa a usar a receita do cadastro (getattr), e da o mesmo.
    assert impressao_do_servidor(PARES) == IMPRESSAO_DO_SERVIDOR_FIXA


def test_receita_e_a_lista_canonica_ordenada() -> None:
    canonica = json.dumps(sorted([n, i] for n, i in PARES), ensure_ascii=False, separators=(",", ":"))
    assert canonica.startswith('[["agenda",')  # acento sem escape (ensure_ascii=False)
    assert "relatório" in canonica
    assert impressao_servidor(PARES) == "sha256:" + hashlib.sha256(canonica.encode("utf-8")).hexdigest()


def test_muda_com_texto_alterado_ferramenta_nova_e_ferramenta_sumida() -> None:
    a, b = definicao("a", "Texto A."), definicao("b", "Texto B.")
    base = impressao_servidor([("a", impressao(a)), ("b", impressao(b))])
    so_texto = impressao_servidor([("a", impressao(definicao("a", "Texto A!"))), ("b", impressao(b))])
    nova = impressao_servidor([("a", impressao(a)), ("b", impressao(b)), ("c", impressao(definicao("c")))])
    sumida = impressao_servidor([("a", impressao(a))])
    assert len({base, so_texto, nova, sumida}) == 4
    assert impressao_servidor([]) != base
    assert impressao_servidor([]).startswith("sha256:")


def test_receita_de_cada_ferramenta_nao_mudou() -> None:
    # Mudar a receita faria tudo o que ja foi revisado parecer mudado: fica presa a um valor conhecido.
    d = {"name": "x", "description": "d", "inputSchema": {"type": "object"}, "icons": [{"src": "i"}], "_meta": {}}
    assert canonico(d) == (
        '{"annotations":null,"description":"d","inputSchema":{"type":"object"},"name":"x",'
        '"outputSchema":null,"title":null}'
    )
    assert impressao(d) == "sha256:" + hashlib.sha256(canonico(d).encode("utf-8")).hexdigest()
