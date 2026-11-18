"""Valores de politica (decisao 11).

Ficam na tabela versionada ``politica`` do PostgreSQL, com registro de quem
mudou. Todos comecam conservadores e sao ajustados no piloto. Aqui ficam os
valores iniciais, os limites aceitos e a regra de leitura: vale a linha mais
nova de cada chave; sem linha, vale o valor inicial.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class ValorPolitica:
    chave: str
    inicial: int
    minimo: int
    maximo: int
    descricao: str

    def conferir(self, valor: int) -> int:
        if not self.minimo <= valor <= self.maximo:
            raise ValueError(f"{self.chave} aceita de {self.minimo} a {self.maximo}; recebeu {valor}.")
        return valor


POLITICA: Final[dict[str, ValorPolitica]] = {
    v.chave: v
    for v in (
        ValorPolitica("WORK_ORDER_MAX_DAYS", 30, 1, 365, "Validade maxima de uma ordem de servico"),
        ValorPolitica("WORK_ORDER_WARN_DAYS", 7, 1, 60, "Aviso antes de a ordem vencer"),
        ValorPolitica("WORK_ORDER_IDLE_DAYS", 14, 1, 180, "Dias sem uso ate a autorizacao ficar inativa"),
        ValorPolitica("TRIAL_PERIOD_DAYS", 14, 0, 90, "Periodo de experiencia: tudo passa pela pessoa"),
        ValorPolitica("OUTBOX_HOLD_SECONDS", 60, 0, 3600, "Espera antes de seguir, com relogio visivel"),
        ValorPolitica("CARD_TTL_MINUTES", 30, 1, 1440, "Prazo de uma ficha; sem resposta, nada segue"),
        ValorPolitica("CONFIRM_CAP_PER_HOUR", 10, 1, 1000, "Teto de confirmacoes por aplicativo por hora"),
        ValorPolitica("CONFIRM_TOO_FAST_SECONDS", 3, 0, 60, "Confirmacao mais rapida que isso conta"),
        ValorPolitica("THREE_SIGNALS_WINDOW_MINUTES", 240, 5, 10080, "Janela dos tres sinais"),
        ValorPolitica("DUPLICATE_WINDOW_MINUTES", 30, 1, 1440, "Intervalo de pedido igual"),
        ValorPolitica("BADGE_TTL_DAYS", 90, 1, 365, "Validade de um selo de nivel 2 ou 3"),
        ValorPolitica("LEDGER_RETENTION_DAYS", 730, 30, 3650, "Guarda do extrato"),
        ValorPolitica("READS_CAP_PER_DAY", 200, 1, 100000, "Catraca: leituras por ordem por dia"),
        ValorPolitica("ALLOWANCE_MAX_PER_DAY", 5, 1, 1000, "Teto diario de um limite por aproximacao"),
        ValorPolitica("ALLOWANCE_MAX_DAYS", 30, 1, 365, "Prazo maximo de um limite por aproximacao"),
    )
}


def valor_inicial(chave: str) -> int:
    return POLITICA[chave].inicial


def nunca_mais_frouxo(chave: str, padrao: int, proposto: int) -> int:
    """Regra de um programa nunca afrouxa o padrao da instalacao (decisao 9).

    Para todos os valores desta tabela, "mais frouxo" significa maior
    (mais dias, mais leituras, mais confirmacoes), exceto as esperas, em que
    mais frouxo significa menor.
    """
    menores_sao_frouxos = {"OUTBOX_HOLD_SECONDS", "CONFIRM_TOO_FAST_SECONDS"}
    if chave in menores_sao_frouxos:
        return max(padrao, proposto)
    return min(padrao, proposto)
