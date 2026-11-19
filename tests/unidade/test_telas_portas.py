"""Partes sem banco das telas de bloqueio, pausa, autorizar sistema e portas do ADMIN."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx2
import pytest
from structlog.testing import capture_logs

from mcp_claro.extrato.frases import frase
from mcp_claro.telas import autorizar
from mcp_claro.telas.autorizar import ficha, mensagem_da_recusa
from mcp_claro.telas.bloqueio import o_que_parou, quem_legivel
from mcp_claro.telas.nucleo import RespostaInterna, Telas
from mcp_claro.telas.paginas import AVISOS, PASTA_MODELOS

PASTA_TELAS = Path(autorizar.__file__).resolve().parent


def test_ficha_do_link_depende_da_sessao_e_do_sistema() -> None:
    sistema, outro = uuid.uuid4(), uuid.uuid4()
    assert ficha("csrf-a", sistema) == ficha("csrf-a", sistema)
    assert ficha("csrf-a", sistema) != ficha("csrf-b", sistema)
    assert ficha("csrf-a", sistema) != ficha("csrf-a", outro)
    assert re.fullmatch(r"[0-9a-f]{64}", ficha("csrf-a", sistema))
    assert "csrf-a" not in ficha("csrf-a", sistema)


@pytest.mark.parametrize(
    ("url", "aceita"),
    [
        ("https://sistema.exemplo/authorize?state=x", True),
        ("http://127.0.0.1:8080/authorize?state=x", True),
        ("http://sistema.exemplo/authorize", False),
        ("javascript:alert(1)", False),
        ("https://usuario:senha@sistema.exemplo/a", False),
        ("https://sistema.exemplo/a#x", False),
        ("https://sistema.exemplo/a b", False),
        ("//sistema.exemplo/a", False),
        (None, False),
        (42, False),
    ],
)
def test_so_segue_para_pagina_de_sistema_segura(url: Any, aceita: bool) -> None:
    assert (autorizar._url_do_sistema(url) is not None) is aceita


def test_recusa_do_despachante_vira_frase_curta() -> None:
    outra = RespostaInterna(403, {"motivo": "estado_de_outra_pessoa", "mensagem": "texto do despachante"})
    assert mensagem_da_recusa(outra) == "Este pedido de autorização é de outra pessoa. Nada foi guardado."
    pausa = RespostaInterna(403, {"motivo": "pausa_pessoa", "mensagem": "Seu acesso esta pausado."})
    assert mensagem_da_recusa(pausa) == "Seu acesso esta pausado."
    assert "despachante" in mensagem_da_recusa(RespostaInterna(0))
    assert "Nada foi guardado" in mensagem_da_recusa(RespostaInterna(500))


def test_quem_ligou_em_palavras() -> None:
    eu, outro = uuid.uuid4(), uuid.uuid4()
    assert quem_legivel(f"pessoa:{eu}", eu) == "Você"
    assert quem_legivel(f"pessoa:{outro}", eu) == "A própria pessoa"
    assert quem_legivel(f"admin:{outro}", eu) == "O ADMIN"
    assert quem_legivel("rotina:ronda", eu) == "Uma rotina automática do MCP-CLARO"
    assert quem_legivel("instalacao") == "A instalação do MCP-CLARO"
    assert quem_legivel(None) == ""


def test_o_que_parou_para_a_pessoa_e_para_o_admin() -> None:
    linha = SimpleNamespace(alcance="pessoa_aplicativo", aplicativo="Goose", sistema=None, pessoa="Ana")
    assert o_que_parou(linha, da_pessoa=True) == "O aplicativo Goose, só para você"  # type: ignore[arg-type]
    assert o_que_parou(linha, da_pessoa=False) == "A pessoa Ana, no aplicativo Goose"  # type: ignore[arg-type]
    tudo = SimpleNamespace(alcance="tudo", aplicativo=None, sistema=None, pessoa=None)
    assert o_que_parou(tudo, da_pessoa=True) == "Todo o MCP-CLARO, para todas as pessoas"  # type: ignore[arg-type]


class _Despachante:
    def __init__(self, resposta: Any) -> None:
        self.resposta = resposta

    async def post(self, caminho: str, corpo: dict[str, Any]) -> dict[str, Any]:
        if isinstance(self.resposta, Exception):
            raise self.resposta
        return dict(self.resposta)


def _telas(despachante: _Despachante) -> Telas:
    return Telas(None, None, None, despachante, None, None)  # type: ignore[arg-type]


@pytest.mark.anyio
async def test_porta_interna_devolve_recusa_sem_excecao_e_nao_loga_o_corpo() -> None:
    pedido = httpx2.Request("POST", "http://despachante/interno/autorizacao/segredo")
    recusa = httpx2.Response(403, json={"motivo": "pedido_invalido", "mensagem": "nao"}, request=pedido)
    erro = httpx2.HTTPStatusError("403", request=pedido, response=recusa)
    corpo = {"sistema_id": "x", "segredo": "segredo-que-nao-pode-vazar"}
    with capture_logs() as logs:
        recusada = await _telas(_Despachante(erro)).porta_interna("/interno/autorizacao/segredo", corpo)
        muda = await _telas(_Despachante(httpx2.ConnectError("sem rede"))).porta_interna("/interno/chaves", corpo)
        certa = await _telas(_Despachante({"guardado": True})).porta_interna("/interno/autorizacao/segredo", corpo)
    assert (recusada.status, recusada.ok, recusada.motivo, recusada.mensagem) == (403, False, "pedido_invalido", "nao")
    assert (muda.status, muda.ok, muda.dados) == (0, False, {})
    assert (certa.status, certa.ok, certa.dados) == (200, True, {"guardado": True})
    assert "segredo-que-nao-pode-vazar" not in repr(logs)
    assert {linha["event"] for linha in logs} == {"telas_porta_interna_recusou", "telas_porta_interna_sem_resposta"}


def test_todo_aviso_usado_existe() -> None:
    usados: set[str] = set()
    for arquivo in PASTA_TELAS.glob("*.py"):
        texto = arquivo.read_text(encoding="utf-8")
        usados |= set(re.findall(r"ver_depois\([^)]*?,\s*\"([a-z_]+)\"", texto))
        usados |= set(re.findall(r"aviso=([a-z_]+)", texto))
        for linha in texto.splitlines():
            if "aviso" in linha or "ver_depois" in linha:
                for a, b in re.findall(r"\"([a-z_]+)\" if [^\n]+? else \"([a-z_]+)\"", linha):
                    usados |= {a, b}
    usados.discard("motivo_exigido")  # este vem por urlencode no admin
    faltam = {u for u in usados if u not in AVISOS}
    assert not faltam
    assert {"bloqueado", "pausado", "ja_valia", "pausa_retomada", "pessoa_desativada"} <= usados


def test_telas_em_breve_sumiram() -> None:
    for modelo in PASTA_MODELOS.rglob("*.html"):
        texto = modelo.read_text(encoding="utf-8")
        assert "em breve" not in texto.lower(), modelo
        assert "reservadas" not in texto, modelo
    assert not (PASTA_MODELOS / "em_breve.html").exists()


def test_frases_novas_nao_levam_segredo() -> None:
    texto = frase("admin_chave_entregue", dono="compartilhada", sistema="S", quem="Carla")
    assert texto == "Chave compartilhada do sistema S entregue ao cofre por Carla; as telas nao a guardam."
    assert frase("pessoa_desativada", quem="Carla") == "Conta desativada pelo ADMIN Carla."
