"""Parametros da ronda das portas e da identidade dela no provedor de login.

Alguns parametros ainda nao existem em ``config/settings.py`` (ver ``docs/vigia.md``);
ate entrarem, valem os valores iniciais daqui. A leitura fica so neste arquivo para
que a troca seja num lugar. O vigia usa a mesma forma, com os parametros ``WATCH_*``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from mcp_claro.config.settings import ConfiguracaoInvalida, Parametros

CLIENTE_RONDA = "mcp-claro-ronda"
CLIENTE_VIGIA = "mcp-claro-vigia"
ESCOPO_MCP = "mcp-claro"
ESCOPO_OUTRO_CONDOMINIO = "outro-servico"


def ler(p: Parametros, nome: str, padrao: Any) -> Any:
    """Valor do parametro, ou o valor inicial se ele ainda nao existir ou estiver vazio."""
    valor = getattr(p, nome, None)
    if valor is None or (isinstance(valor, str) and not valor.strip()):
        return padrao
    return valor


def url_token(p: Parametros, explicito: str | None) -> str | None:
    """Endereco do token endpoint. ``None`` quer dizer: descobrir pelos metadados OIDC do emissor."""
    if explicito:
        return explicito
    if p.IDP_KIND.value == "keycloak":
        return f"{p.IDP_ISSUER_URL}/protocol/openid-connect/token"
    return None


@dataclass(frozen=True, slots=True)
class IdentidadePrograma:
    """Programa pre-cadastrado no provedor de login, que entra com JWT assinado (private_key_jwt, ES256)."""

    client_id: str
    arquivo_chave: str | None
    kid: str | None
    token_url: str | None
    escopo: str | None
    escopo_outro_condominio: str | None
    emissor: str
    recurso: str
    audiencia_assercao: str | None = None
    """``aud`` da afirmacao do cliente. ``None``: o token endpoint (RFC 7523). No Keycloak, o emissor, que vale
    mesmo quando o pedido vai ao endereco interno do provedor (o Keycloak 26.8.2 aceita os dois)."""

    @property
    def configurada(self) -> bool:
        return bool(self.arquivo_chave)


def identidade(p: Parametros, prefixo: str, client_id_padrao: str) -> IdentidadePrograma:
    """Le ``<prefixo>_CLIENT_ID``, ``_KEY_FILE``, ``_KEY_ID``, ``_TOKEN_URL``, ``_TOKEN_SCOPE`` e
    ``_OTHER_AUDIENCE_SCOPE``."""
    return IdentidadePrograma(
        client_id=str(ler(p, f"{prefixo}_CLIENT_ID", client_id_padrao)),
        arquivo_chave=ler(p, f"{prefixo}_KEY_FILE", None),
        kid=ler(p, f"{prefixo}_KEY_ID", None),
        token_url=url_token(p, ler(p, f"{prefixo}_TOKEN_URL", None)),
        escopo=ler(p, f"{prefixo}_TOKEN_SCOPE", ESCOPO_MCP),
        escopo_outro_condominio=ler(p, f"{prefixo}_OTHER_AUDIENCE_SCOPE", ESCOPO_OUTRO_CONDOMINIO),
        emissor=p.IDP_ISSUER_URL,
        recurso=p.recurso,
        audiencia_assercao=p.IDP_ISSUER_URL if p.IDP_KIND.value == "keycloak" else None,
    )


@dataclass(frozen=True, slots=True)
class ConfigRonda:
    alvo: str
    """Endereco publico do gateway (``PUBLIC_BASE_URL``); a ronda entra por ele, como um aplicativo de fora."""
    falhas_para_fechar: int = 2
    """``PATROL_FAILS_TO_CLOSE``: rondas seguidas com passagem indevida que descem um degrau da entrada."""
    fechar_sozinha: bool = True
    """``PATROL_AUTOCLOSE_ENABLED``."""
    intervalo: float = 300.0
    """``PATROL_INTERVAL_SECONDS``."""
    prazo_tentativa: float = 10.0
    """Segundos de espera por resposta em cada tentativa."""
    arquivo_ca: str | None = None
    """``PATROL_CA_FILE``: autoridade a mais para o endereco publico, so em desenvolvimento."""

    @classmethod
    def de_parametros(cls, p: Parametros) -> ConfigRonda:
        return cls(
            alvo=p.PUBLIC_BASE_URL,
            falhas_para_fechar=int(p.PATROL_FAILS_TO_CLOSE),
            fechar_sozinha=bool(p.PATROL_AUTOCLOSE_ENABLED),
            intervalo=float(p.PATROL_INTERVAL_SECONDS),
            arquivo_ca=ler(p, "PATROL_CA_FILE", None),
        )


def identidade_da_ronda(p: Parametros) -> IdentidadePrograma:
    """Identidade da ronda. Em piloto ou producao, as rotinas nao sobem sem ``PATROL_KEY_FILE``."""
    ident = identidade(p, "PATROL", CLIENTE_RONDA)
    problemas: list[str] = []
    if not ident.client_id:
        problemas.append("PATROL_CLIENT_ID nao pode ficar vazio.")
    if p.em_producao and not ident.configurada:
        problemas.append(
            "Em piloto ou producao, PATROL_KEY_FILE e obrigatorio nas rotinas: sem a chave da ronda, "
            "ninguem testa as portas."
        )
    if p.em_producao and ident.token_url and not ident.token_url.startswith("https://"):
        problemas.append("PATROL_TOKEN_URL deve comecar com https://.")
    if problemas:
        raise ConfiguracaoInvalida(problemas)
    return ident
