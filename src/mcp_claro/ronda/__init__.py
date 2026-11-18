"""Ronda das portas: o gateway tenta entrar em si mesmo pelo endereco publico, como um aplicativo de fora.

Roda no papel ``rotinas`` (``rodar_ronda``, a cada ``PATROL_INTERVAL_SECONDS``) e, de fora, no vigia.
Tenta com cracha falso, vencido, de outro condominio e de outra pessoa, e com a versao antiga do MCP.
Toda tentativa precisa ser recusada. Passagem indevida repetida fecha a entrada por etapas.
"""

from mcp_claro.ronda.casos import Caso, Resumo, Situacao, Tentativa, fazer_tentativas
from mcp_claro.ronda.config import ConfigRonda, IdentidadePrograma, identidade, identidade_da_ronda
from mcp_claro.ronda.cracha import CrachasDoProvedor, FalhaCracha, FonteCrachas, ProvedorDeCrachas, forjar_cracha
from mcp_claro.ronda.rodada import Rodada, fonte_da_ronda, rodar_ronda, tarefa_da_ronda

__all__ = [
    "Caso",
    "ConfigRonda",
    "CrachasDoProvedor",
    "FalhaCracha",
    "FonteCrachas",
    "IdentidadePrograma",
    "ProvedorDeCrachas",
    "Resumo",
    "Rodada",
    "Situacao",
    "Tentativa",
    "fazer_tentativas",
    "fonte_da_ronda",
    "forjar_cracha",
    "identidade",
    "identidade_da_ronda",
    "rodar_ronda",
    "tarefa_da_ronda",
]
