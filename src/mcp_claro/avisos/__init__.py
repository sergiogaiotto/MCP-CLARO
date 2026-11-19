"""Avisos: fila no PostgreSQL com novas tentativas, e-mail e webhook assinado.

Aviso pode repetir; acao nunca. O corpo dos avisos leva so o necessario: nunca
argumentos nem resultados de ferramentas, nem dado pessoal alem do destino.
Roda nas rotinas (e no vigia, com canais proprios).
"""

from mcp_claro.avisos.destinos import avisar_admin, avisar_pessoa, destino_pessoa, destino_webhook
from mcp_claro.avisos.fila import (
    Aviso,
    ConfigEnvio,
    Envios,
    FalhaEnvio,
    Resumo,
    enfileirar,
    espera,
    laco_de_envio,
    processar_pendentes,
)

__all__ = [
    "Aviso",
    "ConfigEnvio",
    "Envios",
    "FalhaEnvio",
    "Resumo",
    "avisar_admin",
    "avisar_pessoa",
    "destino_pessoa",
    "destino_webhook",
    "enfileirar",
    "espera",
    "laco_de_envio",
    "processar_pendentes",
]
