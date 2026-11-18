"""Contadores em memoria, levados ao banco em resumos por minuto.

Gravar uma contagem nunca segura um pedido: se o banco falhar, perde-se a
contagem, nao o pedido. As recusas por versao antiga (decisao 8) tem tabela
propria, por lado (aplicativo ou sistema) e origem.
"""

from __future__ import annotations

import json
import threading
from collections import Counter
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

Lado = Literal["aplicativo", "sistema"]
ChaveMedida = tuple[datetime, str, str]
ChaveRecusa = tuple[datetime, str, str, str, str]


def _minuto(agora: datetime | None = None) -> datetime:
    agora = agora or datetime.now(UTC)
    return agora.replace(second=0, microsecond=0)


class Contadores:
    def __init__(self) -> None:
        self._trava = threading.Lock()
        self._medidas: Counter[ChaveMedida] = Counter()
        self._recusas: Counter[ChaveRecusa] = Counter()

    def somar(self, nome: str, valor: int = 1, **rotulos: str) -> None:
        chave = (_minuto(), nome, json.dumps(rotulos, sort_keys=True, ensure_ascii=False))
        with self._trava:
            self._medidas[chave] += valor

    def recusa_versao(self, lado: Lado, origem: str, versao_pedida: str, motivo: str) -> None:
        chave = (_minuto(), lado, origem[:200], versao_pedida[:40], motivo[:80])
        with self._trava:
            self._recusas[chave] += 1
        self.somar("recusas_versao", lado=lado)

    def retirar(self) -> tuple[Counter[ChaveMedida], Counter[ChaveRecusa]]:
        with self._trava:
            medidas, recusas = self._medidas, self._recusas
            self._medidas, self._recusas = Counter(), Counter()
        return medidas, recusas

    def devolver(self, medidas: Counter[ChaveMedida], recusas: Counter[ChaveRecusa]) -> None:
        """Se a gravacao falhar, as contagens voltam para a proxima tentativa."""
        with self._trava:
            self._medidas.update(medidas)
            self._recusas.update(recusas)

    def recusas_pendentes(self) -> dict[tuple[str, str, str, str], int]:
        """Recusas ainda nao gravadas, somadas sem o minuto (testes e tela do ADMIN)."""
        with self._trava:
            total: Counter[tuple[str, str, str, str]] = Counter()
            for (_, lado, origem, versao, motivo), n in self._recusas.items():
                total[(lado, origem, versao, motivo)] += n
            return dict(total)

    async def descarregar(self, conexao: AsyncConnection) -> int:
        """Grava os resumos por minuto na transacao da conexao. Devolve quantas linhas mudaram."""
        medidas, recusas = self.retirar()
        try:
            for (minuto, nome, rotulos), valor in medidas.items():
                await conexao.execute(
                    text(
                        "INSERT INTO medicoes.minuto (minuto, nome, rotulos, valor) "
                        "VALUES (:m, :n, CAST(:r AS jsonb), :v) ON CONFLICT (minuto, nome, rotulos) "
                        "DO UPDATE SET valor = medicoes.minuto.valor + EXCLUDED.valor"
                    ),
                    {"m": minuto, "n": nome, "r": rotulos, "v": valor},
                )
            for (minuto, lado, origem, versao, motivo), n in recusas.items():
                await conexao.execute(
                    text(
                        "INSERT INTO recusas_versao (minuto, lado, origem, versao_pedida, motivo, contagem) "
                        "VALUES (:m, :l, :o, :v, :mo, :n) "
                        "ON CONFLICT (minuto, lado, origem, versao_pedida, motivo) "
                        "DO UPDATE SET contagem = recusas_versao.contagem + EXCLUDED.contagem"
                    ),
                    {"m": minuto, "l": lado, "o": origem, "v": versao, "mo": motivo, "n": n},
                )
        except Exception:
            self.devolver(medidas, recusas)
            raise
        return len(medidas) + len(recusas)


CONTADORES = Contadores()
"""Contadores do processo. Cada papel descarrega a cada METRICS_FLUSH_SECONDS."""
