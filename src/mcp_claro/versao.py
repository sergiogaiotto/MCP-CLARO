"""Versao unica do MCP aceita pelo MCP-CLARO (decisao 8, ratificada em 18/11/2026).

So a versao mais atual publicada pela fundacao do MCP, ligada a Linux
Foundation: 2026-07-28. Vale dos dois lados: aplicativos que se ligam ao
gateway e sistemas que o gateway chama. Mudar este valor pede revisao de
compatibilidade e mudanca de codigo revisada, nao so de configuracao.
"""

from typing import Final

VERSAO_MCP: Final = "2026-07-28"
"""Unica versao do protocolo aceita na entrada e fixada na saida."""

PROXIMA_VERSAO_PREVISTA: Final = "2027-03-30"
"""Data prevista da proxima versao; a revisao de compatibilidade vem antes."""

PREFIXO_META: Final = "br.com.claro.mcp-claro/"
"""Prefixo proprio das chaves que o gateway escreve em ``_meta``."""

CAPACIDADES_DO_CLIENTE: Final = ("elicitation", "roots", "sampling")
"""Capacidades do aplicativo que um sistema pode exigir (-32021). So estes nomes passam do despachante a entrada."""

MOTIVO_EXIGE_CAPACIDADE: Final = "sistema_exige_capacidade"
"""Motivo, no ``_meta`` do resultado interno, de um sistema que so responde pedindo dados ao aplicativo."""
