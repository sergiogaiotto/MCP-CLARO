"""Telas do MCP-CLARO para pessoas sem formacao tecnica: ordens de servico, extrato e cadastro do ADMIN.

Sem aplicacao de pagina unica: paginas feitas no servidor (Jinja2 com escape automatico), formularios
comuns e htmx 2.0.11 servido pelo proprio gateway. Ligue com ``rotas_telas`` no app da entrada.
"""

from mcp_claro.telas.config import ConfigTelas
from mcp_claro.telas.rotas import rotas_telas

__all__ = ["ConfigTelas", "rotas_telas"]
