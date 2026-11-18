"""Modelos Jinja2 das telas, sempre com escape automatico, e os textos de aviso."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jinja2 import Environment, FileSystemLoader, StrictUndefined

PASTA_MODELOS: Final = Path(__file__).resolve().parent / "modelos"
PASTA_ESTATICOS: Final = Path(__file__).resolve().parent / "estaticos"

# Lugares reservados para as telas que chegam depois (bloqueio, pausa e autorizar sistema).
RESERVADAS: Final[dict[str, dict[str, str]]] = {
    "bloqueio": {"caminho": "/telas/bloqueio", "titulo": "Bloquear agora"},
    "pausa": {"caminho": "/telas/pausa", "titulo": "Pausar um aplicativo"},
    "autorizar": {"caminho": "/telas/autorizar", "titulo": "Autorizar um sistema"},
}

# Avisos depois de um passo que deu certo. A URL leva so o codigo, nunca texto livre.
AVISOS: Final[dict[str, str]] = {
    "saiu": "Você saiu. Até logo.",
    "ordem_dada": "Pronto. A ordem de serviço foi dada ao aplicativo.",
    "ordem_revogada": "Pronto. A ordem foi revogada. O aplicativo não pode mais usá-la.",
    "aplicativo_habilitado": "Pronto. O aplicativo foi habilitado.",
    "declaracoes_lidas": "Pronto. Lemos as ferramentas que o sistema declara hoje.",
    "ferramenta_revisada": "Pronto. A revisão foi gravada. A ferramenta já pode entrar em modelos de ordem.",
    "modelo_criado": "Pronto. O modelo de ordem de serviço foi criado.",
    "programa_cadastrado": "Pronto. O programa foi cadastrado, com responsável e substituto.",
    "destino_cadastrado": "Pronto. O destino de webhook foi cadastrado.",
    "destino_mudou": "Pronto. O destino de webhook foi atualizado.",
    "despachante_mudo": (
        "O despachante não respondeu. O sistema ficou pendente. Peça a conferência de novo daqui a pouco."
    ),
    "motivo_exigido": "Para abrir o extrato de outra pessoa, diga antes o motivo.",
}

SITUACOES: Final[dict[str, str]] = {
    "ativa": "Valendo",
    "ativo": "Ativo",
    "vencida": "Venceu",
    "revogada": "Revogada",
    "inativa": "Parada por falta de uso",
    "pausada": "Pausada",
    "pausado": "Pausado",
    "bloqueado": "Bloqueado",
    "bloqueada": "Bloqueada",
    "desativada": "Desativada",
    "pendente": "Falta a sondagem",
    "recusado": "Recusado",
    "quarentena": "Em quarentena",
    "desativado": "Desativado",
    "encerrado": "Encerrado",
}

CLASSES: Final[dict[str, str]] = {
    "le": "Só lê",
    "muda": "Muda algo",
    "tem_volta": "Muda algo, mas tem volta",
}

TIPOS_CHAVE: Final[dict[str, str]] = {
    "por_pessoa": "Uma chave por pessoa",
    "compartilhada": "Uma chave para todos",
    "programa": "Chave do programa",
}


def criar_ambiente(fuso: str) -> Environment:
    try:
        zona: ZoneInfo | None = ZoneInfo(fuso)
    except ZoneInfoNotFoundError, ValueError:
        zona = None

    def data_hora(valor: Any) -> str:
        if not isinstance(valor, datetime):
            return ""
        local = valor.astimezone(zona) if zona else valor
        return local.strftime("%d/%m/%Y %H:%M")

    def so_data(valor: Any) -> str:
        if isinstance(valor, datetime):
            valor = valor.astimezone(zona) if zona else valor
        if isinstance(valor, date):
            return valor.strftime("%d/%m/%Y")
        return ""

    ambiente = Environment(
        loader=FileSystemLoader(PASTA_MODELOS),
        autoescape=True,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    ambiente.filters["data_hora"] = data_hora
    ambiente.filters["data"] = so_data
    ambiente.filters["situacao"] = lambda v: SITUACOES.get(str(v), str(v))
    ambiente.filters["classe"] = lambda v: CLASSES.get(str(v), str(v))
    ambiente.filters["tipo_chave"] = lambda v: TIPOS_CHAVE.get(str(v), str(v))
    ambiente.globals["reservadas"] = RESERVADAS
    return ambiente
