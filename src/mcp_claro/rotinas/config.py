"""Intervalos e destinos das rotinas, tirados dos Parametros.

Alguns parametros ainda nao existem em ``config/settings.py`` (ver o relatorio da
Fase 1); ate entrarem, valem os valores iniciais daqui. A leitura fica so neste
arquivo para que a troca seja num lugar.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from mcp_claro.avisos.fila import ConfigEnvio
from mcp_claro.config.settings import Parametros


def _p(p: Parametros, nome: str, padrao: Any) -> Any:
    valor = getattr(p, nome, None)
    return padrao if valor is None else valor


def _segredo(caminho: str | None) -> SecretStr | None:
    if not caminho:
        return None
    conteudo = Path(caminho).read_text(encoding="utf-8").strip()
    return SecretStr(conteudo) if conteudo else None


@dataclass(frozen=True, slots=True)
class ConfigRotinas:
    # Intervalos, em segundos.
    intervalo_curto: float = 300.0
    """Vencimento de ordens, resumo das medicoes e conferencia do fechamento devido."""
    intervalo_aviso_vencer: float = 3600.0
    intervalo_particoes: float = 6 * 3600.0
    intervalo_limpeza: float = 3600.0
    intervalo_resumo_semanal: float = 3600.0
    intervalo_mudou_parou: float = 900.0
    fechamento_horas: int = 6
    """``CLOSING_INTERVAL_HOURS``."""
    primeira_espera: float = 5.0
    """Folga depois da partida antes da primeira rodada de cada tarefa."""
    prazo_tarefa: float = 600.0
    """Prazo maximo de uma rodada; passou disso, a rodada e cortada e conta como falha."""
    tarefas_ao_mesmo_tempo: int = 3
    # Medicoes.
    retencao_minuto_dias: int = 31
    retencao_hora_meses: int = 13
    retencao_dia_anos: int = 5
    meses_a_frente: int = 3
    # Avisos.
    emails_admin: tuple[str, ...] = ()
    envio: ConfigEnvio = field(default_factory=ConfigEnvio)
    senha_grafana: SecretStr | None = None

    @classmethod
    def de_parametros(cls, p: Parametros) -> ConfigRotinas:
        emails = _p(p, "NOTIFY_ADMIN_EMAILS", [])
        if isinstance(emails, str):
            emails = [e.strip() for e in emails.split(",") if e.strip()]
        return cls(
            intervalo_curto=float(_p(p, "ROUTINES_INTERVAL_SECONDS", 300)),
            intervalo_mudou_parou=float(_p(p, "TOOL_DRIFT_CHECK_SECONDS", 900)),
            fechamento_horas=int(p.CLOSING_INTERVAL_HOURS),
            retencao_minuto_dias=int(p.METRICS_MINUTE_RETENTION_DAYS),
            retencao_hora_meses=int(p.METRICS_HOUR_RETENTION_MONTHS),
            retencao_dia_anos=int(p.METRICS_DAY_RETENTION_YEARS),
            emails_admin=tuple(str(e) for e in emails),
            envio=ConfigEnvio(
                retry_max=int(p.NOTIFY_RETRY_MAX),
                espera_base=float(_p(p, "NOTIFY_RETRY_BASE_SECONDS", 30)),
                espera_teto=float(_p(p, "NOTIFY_RETRY_CAP_SECONDS", 3600)),
                intervalo=float(_p(p, "NOTIFY_POLL_SECONDS", 5)),
                papel="rotinas",
            ),
            senha_grafana=_segredo(_p(p, "GRAFANA_WEBHOOK_PASSWORD_FILE", None)),
        )
