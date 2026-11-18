"""Migracoes do banco do vigia (``migrations_vigia/``, ``alembic_vigia.ini``), proprias e separadas das do gateway.

Rodam com o usuario dono do banco do vigia (``DATABASE_URL`` + ``DATABASE_PASSWORD_FILE`` no ambiente
da segunda maquina). Para ligar no comando de partida: ``mcp-claro migrar-vigia``.
"""

from __future__ import annotations

from pathlib import Path

from mcp_claro.config.settings import Parametros

ARQUIVO = "alembic_vigia.ini"


def arquivo_alembic() -> Path:
    """``alembic_vigia.ini`` na pasta de trabalho (``/app`` na imagem) ou na raiz do repositorio."""
    aqui = Path(ARQUIVO)
    if aqui.exists():
        return aqui
    return Path(__file__).resolve().parents[3] / ARQUIVO


def migrar_vigia(p: Parametros, revisao: str = "head") -> None:
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(arquivo_alembic()))
    cfg.attributes["parametros"] = p
    command.upgrade(cfg, revisao)
