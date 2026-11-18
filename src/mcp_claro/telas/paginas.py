"""Modelos Jinja2 das telas, sempre com escape automatico, e os textos de aviso."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import Any, Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from jinja2 import Environment, FileSystemLoader, StrictUndefined

PASTA_MODELOS: Final = Path(__file__).resolve().parent / "modelos"
PASTA_ESTATICOS: Final = Path(__file__).resolve().parent / "estaticos"

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
    # Bloqueio de um toque e pausa (pessoa).
    "bloqueado": "Pronto. Está bloqueado a partir de agora. Só o ADMIN desfaz.",
    "pausado": "Pronto. Está pausado a partir de agora. Você retoma quando quiser.",
    "ja_valia": "Isto já estava valendo. Nada novo foi ligado. Este é o comprovante.",
    "pausa_retomada": "Pronto. A pausa terminou. O aplicativo volta a poder ler por você, dentro das suas ordens.",
    "ja_terminou": "Isto já tinha terminado. Nada mudou.",
    # Autorizar sistema (pessoa).
    "autorizacao_guardada": (
        "Pronto. Sua autorização foi guardada, cifrada no cofre. O aplicativo já pode ler nesse sistema por você,"
        " só dentro das suas ordens de serviço."
    ),
    "autorizacao_retirada": "Pronto. Sua autorização neste sistema foi retirada. Nada mais é lido lá por você.",
    "nada_a_retirar": "Não havia autorização sua guardada neste sistema. Nada mudou.",
    # Portas, pessoas, autorizacao e chaves (ADMIN).
    "bloqueio_ligado": "Pronto. O bloqueio foi ligado e vale a partir de agora.",
    "pausa_ligada": "Pronto. A pausa foi ligada e vale a partir de agora.",
    "ja_valia_admin": "Já havia um igual valendo. Nada novo foi ligado.",
    "parada_encerrada": "Pronto. Terminou. O que estava parado volta a funcionar, se nada mais o parar.",
    "entrada_mudou": "Pronto. A entrada mudou e vale a partir de agora.",
    "saida_mudou": "Pronto. A porta de saída mudou e vale a partir de agora.",
    "pessoa_desativada": "Pronto. A pessoa foi desativada e as autorizações dela nos sistemas foram derrubadas.",
    "autorizacao_configurada": "Pronto. A autorização por pessoa deste sistema foi configurada.",
    "segredo_guardado": "Pronto. O segredo foi entregue ao cofre. Ele não aparece mais nas telas.",
    "chave_guardada": "Pronto. A chave foi entregue ao cofre. Ela não aparece mais nas telas.",
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
    return ambiente
