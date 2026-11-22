"""R6c, retoque: ``tests.apoio.sistema_geracao_antiga`` so entrega a URL com o exemplo respondendo.

Antes, depois das tentativas sem resposta, a ajuda seguia com a URL mesmo assim, e o teste seguinte falhava longe da
causa (o mesmo tipo de defeito que o R6c-1 fechou em ``servidor``). Aqui o exemplo e trocado por um processo que
escreve no stderr e fica vivo sem abrir a porta, ou que cai logo. A troca so devolve o processo quando ele ja
escreveu (ou ja caiu), para o teste nao depender de quanto tempo o Python leva para subir.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests import apoio

VIVO_CALADO = (
    "import pathlib, sys, time; sys.stderr.write('ultima linha do exemplo'); sys.stderr.flush(); "
    "pathlib.Path(sys.argv[1]).touch(); time.sleep(120)"
)
CAI_LOGO = "import sys; sys.stderr.write('o exemplo quebrou ao subir'); sys.exit(1)"


def _trocar_exemplo(
    monkeypatch: pytest.MonkeyPatch, codigo: str, marca: Path, pronto: Callable[[subprocess.Popen[bytes]], None]
) -> list[subprocess.Popen[bytes]]:
    """No lugar do ``uv run ... servidor.py``, roda ``codigo`` com os mesmos argumentos de ``Popen``."""
    real = subprocess.Popen
    criados: list[subprocess.Popen[bytes]] = []

    def falso(_comando: Any, **kw: Any) -> subprocess.Popen[bytes]:
        proc = real([sys.executable, "-c", codigo, str(marca)], **kw)
        criados.append(proc)
        pronto(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", falso)
    return criados


def _escreveu(marca: Path) -> Callable[[subprocess.Popen[bytes]], None]:
    def esperar(proc: subprocess.Popen[bytes]) -> None:
        limite = time.monotonic() + 30
        while not marca.exists():
            assert proc.poll() is None, "o processo de teste caiu antes de escrever"
            assert time.monotonic() < limite, "o processo de teste nao escreveu em 30 s"
            time.sleep(0.01)

    return esperar


def _caiu(proc: subprocess.Popen[bytes]) -> None:
    proc.wait(timeout=30)


def test_exemplo_vivo_que_nao_responde_falha_aqui_e_nao_entrega_a_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    marca = tmp_path / "escreveu"
    criados = _trocar_exemplo(monkeypatch, VIVO_CALADO, marca, _escreveu(marca))
    entregou = False
    with (
        pytest.raises(pytest.fail.Exception, match=r"nao respondeu em http://127\.0\.0\.1:\d+/ depois de 3 tentativas"),
        apoio.sistema_geracao_antiga(tentativas=3, pausa=0.0),
    ):
        entregou = True
    assert not entregou, "seguiu com a URL sem o exemplo responder"
    assert len(criados) == 1
    assert criados[0].poll() is not None  # o exemplo calado nao fica para tras


def test_a_falha_mostra_o_fim_do_stderr_do_exemplo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    marca = tmp_path / "escreveu"
    _trocar_exemplo(monkeypatch, VIVO_CALADO, marca, _escreveu(marca))
    with (
        pytest.raises(pytest.fail.Exception, match="ultima linha do exemplo"),
        apoio.sistema_geracao_antiga(tentativas=3, pausa=0.0),
    ):
        pass


def test_exemplo_que_cai_ao_subir_continua_falhando_com_o_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    criados = _trocar_exemplo(monkeypatch, CAI_LOGO, tmp_path / "nao_usada", _caiu)
    with (
        pytest.raises(pytest.fail.Exception, match="o exemplo antigo nao subiu: o exemplo quebrou ao subir"),
        apoio.sistema_geracao_antiga(tentativas=3, pausa=0.0),
    ):
        pass
    assert criados[0].returncode == 1
