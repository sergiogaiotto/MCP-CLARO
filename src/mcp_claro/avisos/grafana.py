"""Entrada dos alertas do Grafana, por webhook interno, na fila de avisos.

O Grafana nao sai para a internet: ele manda os alertas as rotinas
(``POST /interno/alertas-grafana`` na porta ``ROUTINES_INTERNAL_PORT``, com mTLS) e
as rotinas os enviam pela fila, com assinatura e novas tentativas.

Autenticacao: senha propria, num segredo do Docker (``GRAFANA_WEBHOOK_PASSWORD_FILE``).
O ponto de contato do Grafana pode manda-la de dois jeitos, ambos aceitos:

- ``Authorization: Bearer <senha>`` (campo "Authorization header" do ponto de contato);
- ``Authorization: Basic <base64(usuario:senha)>`` (usuario qualquer, senha igual).

A comparacao e em tempo constante (``hmac.compare_digest``).

Formato esperado: o corpo padrao do ponto de contato "Webhook" do Grafana Alerting
(versao "1"), JSON de no maximo 256 KiB::

    {
      "receiver": "mcp-claro-rotinas",
      "status": "firing",                      # ou "resolved"
      "version": "1",
      "alerts": [                              # no maximo 50 por pedido; o resto e contado
        {
          "status": "firing",                  # ou "resolved"
          "labels": {"alertname": "Recusas por versao", "severity": "alta"},
          "annotations": {"summary": "Recusas por versao subiram", "description": "..."},
          "startsAt": "2026-11-18T10:00:00Z",
          "endsAt": "0001-01-01T00:00:00Z",
          "fingerprint": "c6eadffa33fcdf37"
        }
      ]
    }

Do corpo, so entram no aviso: ``alertname``, ``severity``, situacao, inicio,
``fingerprint`` e o resumo (``summary``, ou ``description``), cortados a tamanhos
curtos. Os demais campos (URLs, valores, rotulos) sao ignorados. Cada alerta vira um
aviso ao ADMIN com ``chave_unica`` ``grafana:<fingerprint>:<situacao>:<inicio>``: o
Grafana repete o mesmo alerta a cada ``repeat_interval``, e o ADMIN recebe um aviso
por disparo e um por resolucao.
"""

from __future__ import annotations

import base64
import binascii
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

TAMANHO_MAXIMO = 256 * 1024
ALERTAS_POR_PEDIDO = 50
SITUACOES = frozenset({"firing", "resolved"})


class AlertaInvalido(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class AlertaGrafana:
    nome: str
    situacao: str
    inicio: str
    impressao: str
    resumo: str
    severidade: str | None

    @property
    def chave_unica(self) -> str:
        return f"grafana:{self.impressao}:{self.situacao}:{self.inicio}"

    @property
    def situacao_legivel(self) -> str:
        return "disparou" if self.situacao == "firing" else "voltou ao normal"

    def assunto(self) -> str:
        return f"MCP-CLARO: alerta {self.nome} {self.situacao_legivel}"

    def corpo(self) -> dict[str, Any]:
        corpo: dict[str, Any] = {
            "texto": f"O alerta {self.nome} {self.situacao_legivel}. {self.resumo}".strip(),
            "alerta": self.nome,
            "situacao": self.situacao,
            "inicio": self.inicio,
            "impressao": self.impressao,
        }
        if self.severidade:
            corpo["severidade"] = self.severidade
        return corpo


def _texto(valor: Any, maximo: int) -> str:
    if not isinstance(valor, str):
        return ""
    return " ".join(valor.split())[:maximo]


def _mapa(valor: Any) -> Mapping[str, Any]:
    return valor if isinstance(valor, Mapping) else {}


def ler_alertas(carga: Any) -> tuple[list[AlertaGrafana], int]:
    """Le o corpo do Grafana. Devolve os alertas aceitos e quantos ficaram de fora pelo limite."""
    if not isinstance(carga, Mapping):
        raise AlertaInvalido("o corpo deve ser um objeto JSON")
    alertas = carga.get("alerts")
    if not isinstance(alertas, list):
        raise AlertaInvalido("falta a lista 'alerts'")
    situacao_geral = _texto(carga.get("status"), 20)
    lidos: list[AlertaGrafana] = []
    for bruto in alertas[:ALERTAS_POR_PEDIDO]:
        if not isinstance(bruto, Mapping):
            raise AlertaInvalido("cada alerta deve ser um objeto JSON")
        rotulos = _mapa(bruto.get("labels"))
        anotacoes = _mapa(bruto.get("annotations"))
        situacao = _texto(bruto.get("status"), 20) or situacao_geral
        if situacao not in SITUACOES:
            raise AlertaInvalido("situacao do alerta deve ser firing ou resolved")
        nome = _texto(rotulos.get("alertname"), 120) or "sem nome"
        impressao = _texto(bruto.get("fingerprint"), 64) or "sem-impressao"
        if not all(ch.isalnum() or ch in "-_" for ch in impressao):
            raise AlertaInvalido("fingerprint com caracteres inesperados")
        resumo = _texto(anotacoes.get("summary"), 500) or _texto(anotacoes.get("description"), 500)
        lidos.append(
            AlertaGrafana(
                nome=nome,
                situacao=situacao,
                inicio=_texto(bruto.get("startsAt"), 40),
                impressao=impressao,
                resumo=resumo,
                severidade=_texto(rotulos.get("severity"), 40) or None,
            )
        )
    return lidos, max(0, len(alertas) - ALERTAS_POR_PEDIDO)


def senha_confere(autorizacao: str | None, senha: str) -> bool:
    """Confere ``Authorization`` (Bearer ou Basic) contra a senha, em tempo constante."""
    if not autorizacao or not senha:
        return False
    esquema, _, valor = autorizacao.strip().partition(" ")
    valor = valor.strip()
    esperado = senha.encode("utf-8")
    if esquema.lower() == "bearer":
        recebido = valor.encode("utf-8")
    elif esquema.lower() == "basic":
        try:
            decodificado = base64.b64decode(valor, validate=True)
        except binascii.Error, ValueError:
            return False
        _, separador, segredo = decodificado.partition(b":")
        if not separador:
            return False
        recebido = segredo
    else:
        return False
    return hmac.compare_digest(recebido, esperado)
