"""R6c-2: o tempo de CPU de ``test_argumentos_orcamento.medir`` sai de uma chamada sem tracemalloc.

O tracemalloc multiplica a CPU medida de 3 a 12 vezes: com ele ligado na mesma chamada, o teto de 1,0 s media o
tracemalloc, e nao a conferencia (ja falhou com 1,196 s numa suite carregada). Aqui um relogio falso conta 1 s
por chamada sem tracemalloc e 10 s por chamada com ele: ``medir`` tem de devolver 1 s, e o pico da chamada com
tracemalloc.
"""

from __future__ import annotations

import tracemalloc
from typing import Any

import pytest

from mcp_claro.entrada.argumentos import ArgumentosInvalidos
from tests.unidade import test_argumentos_orcamento as orcamento


class _Relogio:
    def __init__(self) -> None:
        self.agora = 0.0

    def __call__(self) -> float:
        return self.agora


def test_tempo_medido_sem_tracemalloc_e_memoria_numa_segunda_chamada(monkeypatch: pytest.MonkeyPatch) -> None:
    relogio = _Relogio()
    chamadas: list[bool] = []

    def conferir(esquema: dict[str, Any], pedido: dict[str, Any], *, terceiro: bool) -> int:
        rastreando = tracemalloc.is_tracing()
        chamadas.append(rastreando)
        relogio.agora += 10.0 if rastreando else 1.0
        if rastreando:
            lixo = bytearray(4 * 1024 * 1024)  # o pico tem de vir da chamada com tracemalloc
            del lixo
        return 7

    monkeypatch.setattr(orcamento.time, "process_time", relogio)
    monkeypatch.setattr(orcamento, "conferir_argumentos", conferir)
    desfecho, segundos, pico = orcamento.medir({}, {})
    assert desfecho == 7
    assert segundos == 1.0, "o tempo saiu da chamada com tracemalloc"
    assert sorted(chamadas) == [False, True]  # uma chamada sem tracemalloc e uma com
    assert pico >= 4 * 1024 * 1024
    assert not tracemalloc.is_tracing()


def test_recusa_sai_da_chamada_que_mede_o_tempo(monkeypatch: pytest.MonkeyPatch) -> None:
    """O desfecho devolvido e o da chamada sem tracemalloc; a segunda chamada so mede a memoria."""
    recusas: list[ArgumentosInvalidos] = []

    def conferir(esquema: dict[str, Any], pedido: dict[str, Any], *, terceiro: bool) -> int:
        erro = ArgumentosInvalidos(["com" if tracemalloc.is_tracing() else "sem"])
        recusas.append(erro)
        raise erro

    monkeypatch.setattr(orcamento, "conferir_argumentos", conferir)
    desfecho, _, _ = orcamento.medir({}, {})
    assert isinstance(desfecho, ArgumentosInvalidos)
    assert desfecho.problemas == ["sem"]
    assert len(recusas) == 2
    assert not tracemalloc.is_tracing()
