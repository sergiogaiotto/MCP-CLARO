"""R6e-3 (contrato K7): a sala cheia responde "nada enviado" na hora, sem deixar a chamada de outro terceiro em duvida.

As vagas da sala (``MAX_CHAMADAS``) sao de todos os terceiros juntos. Antes, uma chamada esperava a vaga sem prazo
proprio, dentro do prazo do despachante: com um terceiro lento ocupando as vagas, a chamada a outro terceiro (que
nem saiu) ficava em duvida, e a acao dela virava "nao confirmada". Agora:

- a sala espera uma vaga no maximo ``PRAZO_VAGA`` (2 s). Sem vaga, responde 200 com o corpo de "nao enviado" da rota
  e a excecao ``sala_sem_vaga``, e nada vai a um filho;
- a acao de Y termina "nao saiu" ("a sala de visitas estava cheia; nada saiu"), contada em ``sala_sem_vaga``;
- uma chamada que espera menos que o prazo pega a vaga quando ela abre, e a vaga volta depois de cada chamada.

Prova da revisao: ``R/concorrencia/repo/tests/unidade/test_rev_conc_sala_vagas.py`` (aqui, invertida).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest

from mcp_claro.config.settings import Parametros
from mcp_claro.ligacao import sala_contrato
from mcp_claro.ligacao.sala_cliente import ClienteSala
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.saida import despachante as porta_de_saida
from mcp_claro.saida.despachante import PortaDeSaida
from mcp_claro.saida.estados import Estado
from mcp_claro.visitas import app as visitas_app
from tests import apoio_visitas
from tests.apoio_visitas import Certificados
from tests.unidade.test_despachante_classifica import FERRAMENTA_T, OP
from tests.unidade.test_visitas_app import _chave_de, _pedidos_do_espiao, _sala_no_ar

pytestmark = pytest.mark.anyio

Y = "https://outro-terceiro.exemplo.teste/mcp"


@pytest.fixture(scope="module")
def certificados(tmp_path_factory: pytest.TempPathFactory) -> Certificados:
    return apoio_visitas.gerar_certificados(tmp_path_factory.mktemp("certificados_r6e_vagas"))


@pytest.fixture
def espiao(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    pasta = tmp_path / "espiao"
    pasta.mkdir()
    monkeypatch.setenv(apoio_visitas.ESPIAO, str(pasta))
    return pasta


def _sala_sem_vaga(onde: str) -> int:
    medidas, recusas = CONTADORES.retirar()
    CONTADORES.devolver(medidas, recusas)
    return sum(n for (_, nome, rotulos), n in medidas.items() if nome == "sala_sem_vaga" and f'"{onde}"' in rotulos)


async def _ocupar(cs: ClienteSala, p: Parametros, espiao: Path, segundos: float) -> asyncio.Task[int]:
    """O terceiro X, lento, na unica vaga: a chamada fica no filho por ``segundos``."""
    tls = cs._tls  # o mesmo contexto mTLS do despachante
    base = str(p.VISITS_INTERNAL_URL).rstrip("/")

    async def lenta() -> int:
        async with httpx2.AsyncClient(verify=tls, timeout=60, trust_env=False) as http:
            r = await http.post(base + sala_contrato.ROTA_CHAMAR, content=b'{"dormir": %.1f}' % segundos)
            return r.status_code

    tarefa = asyncio.create_task(lenta())
    with anyio.fail_after(60):
        while not _dormindo(espiao):  # noqa: ASYNC110 - espera o filho receber a chamada lenta
            await anyio.sleep(0.05)
    return tarefa


def _dormindo(espiao: Path) -> bool:
    return bool(list(espiao.glob("*.dormindo")))


@contextlib.asynccontextmanager
async def _sala_de_uma_vaga(c: Certificados, **opcoes: Any) -> AsyncIterator[tuple[ClienteSala, Parametros, Any]]:
    async with _sala_no_ar(c, alvo=apoio_visitas.atende_devagar, max_chamadas=1, prontos=1, **opcoes) as sala:
        yield sala


async def test_sala_cheia_responde_nada_enviado_na_hora_e_nada_vai_a_um_filho(
    certificados: Certificados, espiao: Path
) -> None:
    """A prova da revisao, invertida: Y nao fica em duvida; nada dele chega a um filho."""
    antes = _sala_sem_vaga("acao")
    async with _sala_de_uma_vaga(certificados) as (cs, p, _sala):
        primeira = await _ocupar(cs, p, espiao, 8.0)
        # Prazo do despachante como no proprio teste da revisao: prazo da chamada (2 s) mais a folga (1 s).
        outra = ClienteSala(str(p.VISITS_INTERNAL_URL), tls=cs._tls, folga=1.0)
        inicio = anyio.current_time()
        chamada, listagem, sondagem = await asyncio.gather(
            outra.chamar_ferramenta(Y, _chave_de("chave-de-y"), None, "enviar", {"a": 1}, prazo=2.0),
            outra.listar(Y, _chave_de("chave-de-y"), None),
            outra.sondar(Y, _chave_de("chave-de-y"), prazo=2.0),
            return_exceptions=True,
        )
        demora = anyio.current_time() - inicio
        recebidos = _pedidos_do_espiao(espiao)
        # A acao de Y, pela porta de saida de verdade, com a resposta que a sala deu.
        porta = PortaDeSaida(engine=None, cifra=None, ligacao=lambda _s: outra)  # type: ignore[arg-type]

        async def chave(*_: Any) -> tuple[str | None, Any, str | None]:
            return "chave-de-y", None, None

        porta._chave = chave  # type: ignore[method-assign]
        acao = await porta._enviar(OP, FERRAMENTA_T, None, None)
        assert await primeira == 200  # a chamada lenta de X nao foi atrapalhada
    assert recebidos == {}, "um pedido de Y chegou a um filho"
    assert not isinstance(chamada, BaseException)
    assert (chamada.tipo, chamada.excecao) == ("nao_enviado", sala_contrato.SALA_SEM_VAGA)
    assert isinstance(listagem, sala_contrato.FalhaNaSala)
    assert (listagem.tipo, listagem.excecao) == ("nao_enviado", sala_contrato.SALA_SEM_VAGA)
    assert not isinstance(sondagem, BaseException)
    assert (sondagem.desfecho, sondagem.motivo, sondagem.excecao) == (
        "inalcancavel",
        sala_contrato.MOTIVO_SALA_SEM_VAGA,
        sala_contrato.SALA_SEM_VAGA,
    )
    # Na hora em que a sala desiste da vaga (2 s), e nao no prazo do despachante (que viraria duvida).
    assert visitas_app.PRAZO_VAGA == 2.0
    assert visitas_app.PRAZO_VAGA <= demora < visitas_app.PRAZO_VAGA + 2.5
    estado, corpo, desfecho, _ = acao
    assert (estado, corpo, desfecho) == (Estado.NAO_SAIU, None, "a sala de visitas estava cheia; nada saiu")
    assert porta_de_saida._NAO_ENVIADO[sala_contrato.SALA_SEM_VAGA] == desfecho
    assert _sala_sem_vaga("acao") == antes + 1


async def test_quem_espera_menos_que_o_prazo_pega_a_vaga_e_a_vaga_volta(
    certificados: Certificados, espiao: Path
) -> None:
    async with _sala_de_uma_vaga(certificados) as (cs, p, sala):
        primeira = await _ocupar(cs, p, espiao, 0.5)  # sai antes do prazo da vaga
        r = await cs.chamar_ferramenta(Y, _chave_de("chave-de-y"), None, "enviar", {"a": 1}, prazo=2.0)
        assert await primeira == 200
        # A vaga volta depois de cada chamada, com ou sem vaga achada: outra chamada ainda entra.
        depois = await cs.chamar_ferramenta(Y, _chave_de("chave-de-y"), None, "enviar", {"a": 2}, prazo=2.0)
        assert sala._vagas is not None
        assert sala._vagas.value == 1
    assert r.excecao != sala_contrato.SALA_SEM_VAGA
    assert depois.excecao != sala_contrato.SALA_SEM_VAGA
    assert len(_pedidos_do_espiao(espiao)) == 2  # as duas chamadas de Y chegaram a um filho


async def test_sem_vaga_a_sala_nao_espera_o_prazo_do_despachante(certificados: Certificados, espiao: Path) -> None:
    """``prazo_vaga`` menor (so neste teste): a resposta vem nele, e a vaga ocupada continua de quem a tem."""
    async with _sala_de_uma_vaga(certificados, prazo_vaga=0.2) as (cs, p, sala):
        primeira = await _ocupar(cs, p, espiao, 3.0)
        inicio = anyio.current_time()
        r = await cs.chamar_ferramenta(Y, _chave_de("chave-de-y"), None, "enviar", {"a": 1}, prazo=30.0)
        demora = anyio.current_time() - inicio
        assert sala._vagas is not None
        assert sala._vagas.value == 0  # a de X, ainda no filho
        assert await primeira == 200
    assert (r.tipo, r.excecao) == ("nao_enviado", sala_contrato.SALA_SEM_VAGA)
    assert demora < 2.0
    assert _pedidos_do_espiao(espiao) == {}


def test_o_corpo_sem_vaga_de_cada_rota_segue_o_contrato() -> None:
    corpos = visitas_app._SEM_VAGA
    assert set(corpos) == set(visitas_app.ROTAS)
    r = sala_contrato.ler_resposta_chamar(200, corpos[sala_contrato.ROTA_CHAMAR])
    assert (r.tipo, r.excecao) == ("nao_enviado", sala_contrato.SALA_SEM_VAGA)
    s = sala_contrato.ler_resposta_sondar(200, corpos[sala_contrato.ROTA_SONDAR])
    assert (s.desfecho, s.excecao) == ("inalcancavel", sala_contrato.SALA_SEM_VAGA)
    with pytest.raises(sala_contrato.FalhaNaSala) as falha:
        sala_contrato.ler_resposta_listar(200, corpos[sala_contrato.ROTA_LISTAR])
    assert (falha.value.tipo, falha.value.excecao) == ("nao_enviado", sala_contrato.SALA_SEM_VAGA)
    assert sala_contrato.SALA_SEM_VAGA in sala_contrato.EXCECOES_DA_SALA
    assert sala_contrato.SALA_SEM_VAGA not in sala_contrato.EXCECOES_DO_DESPACHANTE
