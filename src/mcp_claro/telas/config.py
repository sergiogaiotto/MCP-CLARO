"""Parametros das telas.

Ficam aqui enquanto nao entram em ``Parametros`` (config/settings.py). Quando entrarem
la, com os mesmos nomes, ``ConfigTelas.de_parametros`` passa a le-los sozinho.

- ``SCREENS_CLIENT_ID``: identificador das telas no provedor de login (cliente OIDC proprio,
  diferente dos aplicativos de IA).
- ``SCREENS_CLIENT_SECRET_FILE``: segredo do cliente, em segredo do Docker. Com ele, o cliente e
  confidencial (``client_secret_basic``); sem ele, publico, so com PKCE. Obrigatorio em piloto e producao.
- ``SCREENS_SESSION_TTL_MINUTES`` e ``SCREENS_SESSION_IDLE_MINUTES``: validade da sessao e tempo parado.
- ``SCREENS_LOGIN_TTL_SECONDS``: prazo de um login em andamento (estado de uso unico).
- ``SCREENS_IDP_AUTHORIZATION_URL`` e ``SCREENS_IDP_TOKEN_URL``: enderecos de login (publico, do navegador)
  e de troca do codigo (interno). Sem eles, saem do tipo de provedor ou da descoberta OIDC.
- ``SCREENS_TIMEZONE``: fuso das datas nas telas.
- ``SCREENS_FOREIGN_LEDGER_WINDOW_MINUTES``: por quanto tempo o motivo dado pelo ADMIN vale para
  folhear o extrato de outra pessoa sem nova linha no extrato.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import SecretStr

from mcp_claro.config.settings import Parametros, TipoProvedor

CAMINHO_VOLTA = "/telas/login/volta"


def _ler_segredo(caminho: str | None) -> SecretStr | None:
    if not caminho:
        return None
    return SecretStr(Path(caminho).read_text(encoding="utf-8").strip())


@dataclass(frozen=True, slots=True)
class ConfigTelas:
    client_id: str = "mcp-claro-telas"
    segredo: SecretStr | None = field(default=None, repr=False)
    sessao_minutos: int = 480
    parada_minutos: int = 30
    login_segundos: int = 600
    url_autorizacao: str | None = None
    url_troca: str | None = None
    fuso: str = "America/Sao_Paulo"
    janela_extrato_alheio_minutos: int = 30
    escopos: tuple[str, ...] = ("openid", "profile")

    @classmethod
    def de_parametros(cls, p: Parametros) -> ConfigTelas:
        def valor(nome: str, padrao: Any) -> Any:
            return getattr(p, nome, padrao)

        return cls(
            client_id=valor("SCREENS_CLIENT_ID", _PADRAO.client_id),
            segredo=_ler_segredo(valor("SCREENS_CLIENT_SECRET_FILE", None)),
            sessao_minutos=int(valor("SCREENS_SESSION_TTL_MINUTES", _PADRAO.sessao_minutos)),
            parada_minutos=int(valor("SCREENS_SESSION_IDLE_MINUTES", _PADRAO.parada_minutos)),
            login_segundos=int(valor("SCREENS_LOGIN_TTL_SECONDS", _PADRAO.login_segundos)),
            url_autorizacao=valor("SCREENS_IDP_AUTHORIZATION_URL", None),
            url_troca=valor("SCREENS_IDP_TOKEN_URL", None),
            fuso=valor("SCREENS_TIMEZONE", _PADRAO.fuso),
            janela_extrato_alheio_minutos=int(
                valor("SCREENS_FOREIGN_LEDGER_WINDOW_MINUTES", _PADRAO.janela_extrato_alheio_minutos)
            ),
        )

    def problemas(self, p: Parametros) -> list[str]:
        """As mesmas regras de recusa que a partida deve aplicar quando estes parametros entrarem em settings."""
        erros: list[str] = []
        if not self.client_id.strip():
            erros.append("SCREENS_CLIENT_ID nao pode ficar vazio.")
        if not 15 <= self.sessao_minutos <= 1440:
            erros.append("SCREENS_SESSION_TTL_MINUTES aceita de 15 a 1440.")
        if not 5 <= self.parada_minutos <= self.sessao_minutos:
            erros.append("SCREENS_SESSION_IDLE_MINUTES aceita de 5 ate SCREENS_SESSION_TTL_MINUTES.")
        if not 60 <= self.login_segundos <= 1800:
            erros.append("SCREENS_LOGIN_TTL_SECONDS aceita de 60 a 1800.")
        if not 5 <= self.janela_extrato_alheio_minutos <= 240:
            erros.append("SCREENS_FOREIGN_LEDGER_WINDOW_MINUTES aceita de 5 a 240.")
        try:
            ZoneInfo(self.fuso)
        except ZoneInfoNotFoundError, ValueError:
            erros.append(f"SCREENS_TIMEZONE desconhecido: {self.fuso}.")
        if p.em_producao:
            if self.segredo is None:
                erros.append("Em piloto ou producao, as telas usam cliente confidencial: SCREENS_CLIENT_SECRET_FILE.")
            for nome, url in (
                ("SCREENS_IDP_AUTHORIZATION_URL", self.url_autorizacao),
                ("SCREENS_IDP_TOKEN_URL", self.url_troca),
            ):
                if url and not url.startswith("https://"):
                    erros.append(f"Em piloto ou producao, {nome} deve comecar com https://.")
        return erros

    def redirect_uri(self, p: Parametros) -> str:
        return p.PUBLIC_BASE_URL + CAMINHO_VOLTA

    def enderecos_por_tipo(self, p: Parametros) -> tuple[str | None, str | None]:
        """Enderecos de login e de troca conhecidos pelo tipo de provedor (sem descoberta)."""
        emissor = p.IDP_ISSUER_URL
        if p.IDP_KIND is TipoProvedor.KEYCLOAK:
            autorizacao, troca = f"{emissor}/protocol/openid-connect/auth", f"{emissor}/protocol/openid-connect/token"
        elif p.IDP_KIND is TipoProvedor.LOGTO:
            autorizacao, troca = f"{emissor}/auth", f"{emissor}/token"
        else:
            autorizacao, troca = None, None
        return self.url_autorizacao or autorizacao, self.url_troca or troca


_PADRAO = ConfigTelas()
