"""Vigia de fora: o mesmo pacote, no papel ``vigia``, em outra maquina e com banco proprio.

- repete a ronda das portas pelo endereco publico (``WATCH_PUBLIC_URL``);
- dispara o alarme de silencio quando o gateway nao responde por ``WATCH_SILENCE_ALARM_SECONDS``;
- busca cada fechamento novo com cracha proprio de programa e guarda so acrescentando, conferindo
  o encadeamento;
- diz ao despachante, numa rota pequena e autenticada, o ultimo fechamento guardado.

Avisa por SMTP e webhook proprios (``WATCH_SMTP_*``, ``WATCH_WEBHOOK_TARGETS``), sem depender do
Grafana nem das rotinas do gateway. Ver ``docs/vigia.md``.
"""

from mcp_claro.vigia.app import Vigia, criar_app_vigia
from mcp_claro.vigia.config import ConfigVigia
from mcp_claro.vigia.continuidade import (
    UltimoNoVigia,
    VigiaSemResposta,
    numero_no_vigia,
    ultimo_no_vigia,
    vigia_a_frente,
)
from mcp_claro.vigia.migrar import migrar_vigia

__all__ = [
    "ConfigVigia",
    "UltimoNoVigia",
    "Vigia",
    "VigiaSemResposta",
    "criar_app_vigia",
    "migrar_vigia",
    "numero_no_vigia",
    "ultimo_no_vigia",
    "vigia_a_frente",
]
