"""Parametros do papel ``vigia``, na segunda maquina, com canais e identidade proprios.

Alguns parametros ainda nao existem em ``config/settings.py`` (ver ``docs/vigia.md``); ate entrarem,
valem os valores iniciais daqui. A partida do vigia recusa combinacoes invalidas (``problemas``).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import SecretStr

from mcp_claro.avisos.destinos import parece_email
from mcp_claro.avisos.fila import ConfigEnvio
from mcp_claro.config.settings import ConfiguracaoInvalida, Parametros
from mcp_claro.ronda.config import CLIENTE_VIGIA, IdentidadePrograma, identidade, ler

FERRAMENTA_ALHEIA_PADRAO = "mcp_claro_vigia_ferramenta_alheia"
BYTES_MINIMOS_SEGREDO = 32


def _lista(valor: Any) -> tuple[str, ...]:
    if isinstance(valor, str):
        return tuple(v.strip() for v in valor.split(",") if v.strip())
    if isinstance(valor, list | tuple):
        return tuple(str(v).strip() for v in valor if str(v).strip())
    return ()


def _liga(valor: Any) -> bool:
    if isinstance(valor, str):
        return valor.strip().lower() in {"ligado", "verdadeiro", "sim", "true", "1", "on", "yes"}
    return bool(valor)


def _segredo(caminho: str | None) -> SecretStr | None:
    if not caminho:
        return None
    conteudo = Path(caminho).read_text(encoding="utf-8").strip()
    return SecretStr(conteudo) if conteudo else None


@dataclass(frozen=True, slots=True)
class SmtpVigia:
    host: str | None = None
    porta: int = 587
    remetente: str | None = None
    usuario: str | None = None
    senha: SecretStr | None = None
    starttls: bool = True

    @property
    def configurado(self) -> bool:
        return bool(self.host and self.remetente)


@dataclass(frozen=True, slots=True)
class ConfigVigia:
    alvo: str
    """``WATCH_PUBLIC_URL``: endereco publico do gateway, testado de fora."""
    identidade: IdentidadePrograma
    """Programa ``mcp-claro-vigia`` no provedor de login (``WATCH_CLIENT_ID``, ``WATCH_KEY_FILE``...)."""
    intervalo_pulso: float = 60.0
    """``WATCH_PING_SECONDS``: de quanto em quanto tempo o vigia ve se o gateway responde."""
    intervalo_ronda: float = 600.0
    """``WATCH_PROBE_INTERVAL_SECONDS``: ronda de fora e busca dos fechamentos."""
    silencio: float = 900.0
    """``WATCH_SILENCE_ALARM_SECONDS``."""
    falhas_para_alarme: int = 2
    """``PATROL_FAILS_TO_CLOSE``: rondas seguidas com passagem que disparam o alarme."""
    emails: tuple[str, ...] = ()
    """``WATCH_ALARM_EMAILS``."""
    webhooks: tuple[str, ...] = ()
    """``WATCH_WEBHOOK_TARGETS``."""
    smtp: SmtpVigia = field(default_factory=SmtpVigia)
    segredo_webhook: SecretStr | None = None
    """``WATCH_WEBHOOK_SECRET_FILE``: Standard Webhooks, base64 de 24 a 64 bytes."""
    token_continuidade: SecretStr | None = None
    """``WATCH_CONTINUITY_TOKEN_FILE``: segredo comum com o despachante, para ``/ultimo-fechamento``."""
    ferramenta_alheia: str = FERRAMENTA_ALHEIA_PADRAO
    """``WATCH_FOREIGN_TOOL``: ferramenta de outra pessoa para o caso "outra pessoa"."""
    arquivo_ca: str | None = None
    """``WATCH_CA_FILE``: autoridade a mais para o endereco publico, so em desenvolvimento."""
    prazo_tentativa: float = 10.0
    primeira_espera: float = 5.0
    envio: ConfigEnvio = field(default_factory=lambda: ConfigEnvio(papel="vigia", registrar_no_extrato=False))
    em_producao: bool = False

    @classmethod
    def de_parametros(cls, p: Parametros) -> ConfigVigia:
        return cls(
            alvo=str(p.WATCH_PUBLIC_URL or p.PUBLIC_BASE_URL).rstrip("/"),
            identidade=identidade(p, "WATCH", CLIENTE_VIGIA),
            intervalo_pulso=float(ler(p, "WATCH_PING_SECONDS", 60)),
            intervalo_ronda=float(p.WATCH_PROBE_INTERVAL_SECONDS),
            silencio=float(p.WATCH_SILENCE_ALARM_SECONDS),
            falhas_para_alarme=int(p.PATROL_FAILS_TO_CLOSE),
            emails=_lista(ler(p, "WATCH_ALARM_EMAILS", ())),
            webhooks=_lista(ler(p, "WATCH_WEBHOOK_TARGETS", ())),
            smtp=SmtpVigia(
                host=ler(p, "WATCH_SMTP_HOST", None),
                porta=int(ler(p, "WATCH_SMTP_PORT", 587)),
                remetente=ler(p, "WATCH_SMTP_FROM", None),
                usuario=ler(p, "WATCH_SMTP_USERNAME", None),
                senha=_segredo(ler(p, "WATCH_SMTP_PASSWORD_FILE", None)),
                starttls=_liga(ler(p, "WATCH_SMTP_STARTTLS", True)),
            ),
            segredo_webhook=_segredo(ler(p, "WATCH_WEBHOOK_SECRET_FILE", None)),
            token_continuidade=_segredo(ler(p, "WATCH_CONTINUITY_TOKEN_FILE", None)),
            ferramenta_alheia=str(ler(p, "WATCH_FOREIGN_TOOL", FERRAMENTA_ALHEIA_PADRAO)),
            arquivo_ca=ler(p, "WATCH_CA_FILE", None),
            envio=ConfigEnvio(
                retry_max=int(p.NOTIFY_RETRY_MAX),
                espera_base=float(ler(p, "NOTIFY_RETRY_BASE_SECONDS", 30)),
                espera_teto=float(ler(p, "NOTIFY_RETRY_CAP_SECONDS", 3600)),
                intervalo=float(ler(p, "NOTIFY_POLL_SECONDS", 5)),
                papel="vigia",
                registrar_no_extrato=False,
            ),
            em_producao=p.em_producao,
        )

    def problemas(self) -> list[str]:
        """Combinacoes que a partida do vigia recusa. Vazia quando tudo esta certo."""
        erros: list[str] = []
        if self.em_producao and not self.alvo.startswith("https://"):
            erros.append("WATCH_PUBLIC_URL deve comecar com https://.")
        if self.em_producao and not self.identidade.configurada:
            erros.append("Em piloto ou producao, WATCH_KEY_FILE e obrigatorio: sem ele o vigia nao busca fechamentos.")
        if self.intervalo_pulso * 2 > self.silencio:
            erros.append("WATCH_PING_SECONDS precisa caber pelo menos duas vezes em WATCH_SILENCE_ALARM_SECONDS.")
        for email in self.emails:
            if not parece_email(email):
                erros.append("WATCH_ALARM_EMAILS tem um endereco de e-mail invalido.")
                break
        if self.emails and not self.smtp.configurado:
            erros.append("WATCH_ALARM_EMAILS precisa de WATCH_SMTP_HOST e WATCH_SMTP_FROM.")
        for url in self.webhooks:
            if not url.startswith("https://"):
                erros.append("WATCH_WEBHOOK_TARGETS aceita so enderecos https://.")
                break
        if self.webhooks and self.segredo_webhook is None:
            erros.append("WATCH_WEBHOOK_TARGETS precisa de WATCH_WEBHOOK_SECRET_FILE: webhook sem assinatura nao sai.")
        if self.em_producao and not (self.emails or self.webhooks):
            erros.append("Em piloto ou producao, o vigia precisa de WATCH_ALARM_EMAILS ou WATCH_WEBHOOK_TARGETS.")
        if self.token_continuidade is not None and len(self.token_continuidade.get_secret_value()) < (
            BYTES_MINIMOS_SEGREDO
        ):
            erros.append(f"WATCH_CONTINUITY_TOKEN_FILE precisa de pelo menos {BYTES_MINIMOS_SEGREDO} caracteres.")
        return erros

    def conferir(self) -> ConfigVigia:
        erros = self.problemas()
        if erros:
            raise ConfiguracaoInvalida(erros)
        return self
