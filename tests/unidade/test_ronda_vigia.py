"""Partes puras do vigia e das frases da ronda: conferencia do encadeamento, parametros e segredos."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from mcp_claro.config.settings import ConfiguracaoInvalida, Parametros
from mcp_claro.extrato.frases import frase
from mcp_claro.ronda.config import IdentidadePrograma
from mcp_claro.vigia.alarmes import ASSUNTOS, TIPOS
from mcp_claro.vigia.config import ConfigVigia, SmtpVigia
from mcp_claro.vigia.continuidade import UltimoNoVigia, segredo_confere, vigia_a_frente
from mcp_claro.vigia.fechamentos import Fechamento, RespostaInvalida, conferir

RAIZ = Path(__file__).resolve().parents[2]


def cadeia(n: int) -> list[Fechamento]:
    """``n`` fechamentos encadeados, como o gateway os calcula."""
    itens: list[Fechamento] = []
    anterior: Fechamento | None = None
    for numero in range(1, n + 1):
        desde = anterior.ate_xid if anterior else "0"
        hash_ = hashlib.sha256(f"f{numero}".encode()).digest()
        f = Fechamento(numero, desde, str(numero * 100), numero, anterior.hash if anterior else None, hash_)
        itens.append(f)
        anterior = f
    return itens


def tipos(problemas: list[Any]) -> set[tuple[str, int]]:
    return {(p.tipo, p.numero) for p in problemas}


def test_primeira_busca_guarda_a_cadeia_inteira() -> None:
    todos = cadeia(4)
    r = conferir({}, None, todos, 0)
    assert r.novos == todos
    assert r.problemas == []


def test_busca_com_sobreposicao_confere_e_guarda_so_os_novos() -> None:
    todos = cadeia(6)
    guardados = {f.numero: f for f in todos[:4]}
    r = conferir({n: f for n, f in guardados.items() if n > 2}, todos[3], todos[2:], 2)
    assert [f.numero for f in r.novos] == [5, 6]
    assert r.conferidos == 2
    assert r.problemas == []


def test_fechamento_ja_guardado_que_mudou_ou_sumiu() -> None:
    todos = cadeia(4)
    guardados = {f.numero: f for f in todos}
    mudado = Fechamento(2, todos[1].desde_xid, todos[1].ate_xid, 2, todos[1].hash_anterior, b"\xaa" * 32)
    r = conferir(guardados, todos[3], [todos[0], mudado, todos[2], todos[3]], 0)
    assert tipos(r.problemas) == {("fechamento_mudou", 2), ("encadeamento_quebrado", 3)}
    r = conferir(guardados, todos[3], [todos[0], todos[2], todos[3]], 0)
    assert ("fechamento_sumiu", 2) in tipos(r.problemas)


def test_novo_que_nao_encadeia_no_guardado_nao_e_guardado() -> None:
    todos = cadeia(3)
    guardados = {f.numero: f for f in todos[:2]}
    errado = Fechamento(3, todos[2].desde_xid, todos[2].ate_xid, 3, b"\xbb" * 32, todos[2].hash)
    r = conferir(guardados, todos[1], [todos[1], errado], 1)
    assert r.novos == []
    assert tipos(r.problemas) == {("encadeamento_quebrado", 3)}
    # Buraco na numeracao e intervalo de transacoes que nao emenda tambem quebram a cadeia.
    pulo = Fechamento(5, todos[2].ate_xid, "999", 1, todos[2].hash, b"\xcc" * 32)
    r = conferir({f.numero: f for f in todos}, todos[2], [pulo], 3)
    assert r.novos == []
    assert ("encadeamento_quebrado", 5) in tipos(r.problemas)
    torto = Fechamento(3, "1", todos[2].ate_xid, 3, todos[1].hash, todos[2].hash)
    assert ("encadeamento_quebrado", 3) in tipos(conferir(guardados, todos[1], [torto], 2).problemas)


def test_primeiro_fechamento_precisa_comecar_do_inicio() -> None:
    torto = Fechamento(1, "5", "100", 1, None, b"\x01" * 32)
    r = conferir({}, None, [torto], 0)
    assert r.novos == []
    assert ("encadeamento_quebrado", 1) in tipos(r.problemas)


def test_fechamento_do_json_confere_os_valores() -> None:
    bom = {
        "numero": 1,
        "desde_xid": "0",
        "ate_xid": "10",
        "linhas": 2,
        "hash_anterior": None,
        "hash": "ab" * 32,
        "calculado_em": "2026-11-18T00:00:00+00:00",
    }
    f = Fechamento.do_json(bom)
    assert f.hash == b"\xab" * 32
    for ruim in ({"hash": "zz"}, {"hash": "ab"}, {"numero": 0}, {"desde_xid": "-1"}, {"linhas": -1}):
        with pytest.raises(RespostaInvalida):
            Fechamento.do_json(bom | ruim)
    with pytest.raises(RespostaInvalida):
        Fechamento.do_json({"numero": 1})
    with pytest.raises(RespostaInvalida):
        Fechamento.do_json([1, 2])


def identidade(chave: str | None = "/run/secrets/vigia_chave") -> IdentidadePrograma:
    return IdentidadePrograma(
        "mcp-claro-vigia", chave, None, None, "mcp-claro", "outro-servico", "https://l", "https://g/mcp"
    )


def test_parametros_do_vigia() -> None:
    p = Parametros(APP_ENV="teste")
    cfg = ConfigVigia.de_parametros(p)
    assert cfg.alvo == p.PUBLIC_BASE_URL
    assert cfg.identidade.client_id == "mcp-claro-vigia"
    assert (cfg.intervalo_pulso, cfg.intervalo_ronda, cfg.silencio) == (60.0, 600.0, 900.0)
    assert cfg.envio.papel == "vigia"
    assert cfg.envio.registrar_no_extrato is False
    assert cfg.problemas() == []


def test_vigia_recusa_combinacoes_invalidas() -> None:
    base = ConfigVigia(alvo="https://g", identidade=identidade(), emails=("admin@exemplo.teste",))
    assert any("WATCH_SMTP_HOST" in e for e in base.problemas())
    with pytest.raises(ConfiguracaoInvalida):
        base.conferir()
    smtp = SmtpVigia(host="smtp.exemplo.teste", remetente="vigia@exemplo.teste")
    assert ConfigVigia(alvo="https://g", identidade=identidade(), emails=("a@b.teste",), smtp=smtp).problemas() == []
    erros = ConfigVigia(
        alvo="http://g",
        identidade=identidade(None),
        webhooks=("http://destino",),
        emails=("sem-arroba",),
        intervalo_pulso=600,
        silencio=900,
        token_continuidade=SecretStr("curto"),
        em_producao=True,
        smtp=smtp,
    ).problemas()
    texto = "\n".join(erros)
    for trecho in (
        "WATCH_PUBLIC_URL",
        "WATCH_KEY_FILE",
        "WATCH_PING_SECONDS",
        "e-mail invalido",
        "https://",
        "WATCH_WEBHOOK_SECRET_FILE",
        "WATCH_CONTINUITY_TOKEN_FILE",
    ):
        assert trecho in texto
    sem_canal = ConfigVigia(alvo="https://g", identidade=identidade(), em_producao=True)
    assert any("WATCH_ALARM_EMAILS ou WATCH_WEBHOOK_TARGETS" in e for e in sem_canal.problemas())


def test_segredo_da_continuidade() -> None:
    segredo = "s" * 40
    assert segredo_confere(f"Bearer {segredo}", segredo)
    assert segredo_confere(f"bearer {segredo}", segredo)
    assert not segredo_confere(f"Bearer {segredo}x", segredo)
    assert not segredo_confere(None, segredo)
    assert not segredo_confere(f"Basic {segredo}", segredo)
    assert not vigia_a_frente(5, UltimoNoVigia(None, None))
    assert not vigia_a_frente(5, UltimoNoVigia(5, b"x"))
    assert vigia_a_frente(4, UltimoNoVigia(5, b"x"))


def test_tipos_de_alarme_batem_com_o_banco_do_vigia() -> None:
    migracao = (RAIZ / "migrations_vigia" / "versions" / "0001_vigia.py").read_text(encoding="utf-8")
    trecho = migracao[migracao.index("tipo       text") : migracao.index("chave      text")]
    assert set(re.findall(r"'([a-z_]+)'", trecho)) == set(TIPOS)
    assert set(ASSUNTOS) == set(TIPOS)


def test_frases_da_ronda() -> None:
    assert frase("ronda_fechou_entrada", rondas=2, nivel="so de leitura") == (
        "A ronda das portas achou passagem indevida em 2 rondas seguidas e deixou a entrada so de leitura."
        " So o ADMIN reabre, depois de apurar."
    )
    assert "desligado" in frase("ronda_sem_fechamento", rondas=2, nivel="aberta")
    assert (
        frase("ronda_falha", motivo="cracha falso") == "A ronda das portas achou uma passagem indevida: cracha falso."
    )
