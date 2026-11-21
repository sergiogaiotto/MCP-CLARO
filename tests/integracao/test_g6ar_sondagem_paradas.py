"""Quarentena pela sondagem (D29) no painel "Acoes paradas antes de sair" (retoque da etapa 5, G6ar, item 5).

A sondagem que acha outra versao num terceiro ativo e revisado poe o servidor em quarentena e cancela o que esperava
na fila. Como no mudou-parou (``rotinas/mudou_parou.py``), as acoes canceladas somam ``acoes_paradas`` com o rotulo
``cadastro.quarentena.MOTIVO_PARADA`` (a serie da quarentena no painel), depois do commit; ``quarentenas_pela_sondagem``
continua contando uma por quarentena. Contra PostgreSQL real, com o usuario ``mcp_claro_despachante`` e acoes de
verdade na fila (``test_saida_quarentena.preparar``).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest

from mcp_claro.cadastro.quarentena import MOTIVO_PARADA, MOTIVO_QUARENTENA_VERSAO
from mcp_claro.config.settings import Parametros
from mcp_claro.despachante import servico
from mcp_claro.despachante.servico import Despachante
from mcp_claro.ligacao.cliente import MOTIVO_NAO_FALA, Sondagem
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.saida.estados import Estado
from tests.cifra_falsa import CifraFalsa
from tests.integracao.test_entrada_terceiros_apoio import Mundo, subir_mundo
from tests.integracao.test_saida_quarentena import preparar, sistema

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


@dataclass
class Espia:
    """Os contadores do despachante (``servico.CONTADORES``): guarda cada soma e passa adiante."""

    somas: list[tuple[str, int, dict[str, str]]] = field(default_factory=list)

    def somar(self, nome: str, valor: int = 1, **rotulos: str) -> None:
        self.somas.append((nome, valor, dict(rotulos)))
        CONTADORES.somar(nome, valor, **rotulos)

    def recusa_versao(self, *args: Any) -> None:
        CONTADORES.recusa_versao(*args)

    def de(self, nome: str, **rotulos: str) -> int:
        return sum(v for n, v, r in self.somas if n == nome and all(r.get(k) == x for k, x in rotulos.items()))


@pytest.fixture
def espia(monkeypatch: pytest.MonkeyPatch) -> Espia:
    contados = Espia()
    monkeypatch.setattr(servico, "CONTADORES", contados)
    return contados


@pytest.fixture
async def mundo(banco_migrado: Parametros) -> AsyncIterator[Mundo]:
    async with subir_mundo(banco_migrado) as m:
        yield m


class VersaoAntiga:
    """O servidor de terceiros responde a sondagem com outra versao."""

    async def sondar(self, *_: Any, **__: Any) -> Sondagem:
        return Sondagem("versao", MOTIVO_NAO_FALA, versoes=("2025-06-18",), codigo=-32022)


def _despachante(m: Mundo) -> Despachante:
    return Despachante(m.despachante_db, CifraFalsa(), ambiente="teste", ligacao=lambda _s: VersaoAntiga())  # type: ignore[arg-type,return-value]


async def test_sondagem_que_poe_em_quarentena_conta_as_acoes_paradas(mundo: Mundo, espia: Espia) -> None:
    m = mundo
    ops = (await preparar(m))["ops"]  # no terceiro: uma esperando a ficha e uma liberada; no proprio: uma
    await _despachante(m).sondar_sistema(m.terceiro_id, None)
    s = await sistema(m)
    assert (s.situacao, s.motivo) == ("quarentena", MOTIVO_QUARENTENA_VERSAO)
    for chave in ("ficha", "liberada"):
        op = await m.operacao(ops[chave].protocolo)
        assert (op.estado, op.desfecho) == (Estado.CANCELADA, MOTIVO_PARADA)
    assert (await m.operacao(ops["proprio"].protocolo)).estado == Estado.AGUARDANDO_FICHA
    # As duas do terceiro, numa soma so, com o rotulo da porta de saida para a mesma causa.
    assert [(v, r) for n, v, r in espia.somas if n == "acoes_paradas"] == [(2, {"motivo": MOTIVO_PARADA})]
    assert espia.de("quarentenas_pela_sondagem") == 1


async def test_sondagem_que_poe_em_quarentena_sem_nada_na_fila_nao_soma_acoes_paradas(
    mundo: Mundo, espia: Espia
) -> None:
    m = mundo
    await _despachante(m).sondar_sistema(m.terceiro_id, None)
    assert (await sistema(m)).situacao == "quarentena"
    assert espia.de("quarentenas_pela_sondagem") == 1
    assert espia.de("acoes_paradas") == 0


async def test_sondagem_em_quarentena_nao_soma_de_novo(mundo: Mundo, espia: Espia) -> None:
    """Ja em quarentena, a sondagem nao poe de novo nem cancela nada: nenhuma das duas contas."""
    m = mundo
    await _despachante(m).sondar_sistema(m.terceiro_id, None)
    antes = list(espia.somas)
    await _despachante(m).sondar_sistema(m.terceiro_id, None)
    novas = espia.somas[len(antes) :]
    assert not [n for n, _, _ in novas if n in ("acoes_paradas", "quarentenas_pela_sondagem")]
