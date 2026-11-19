"""Parametros de implantacao do MCP-CLARO.

Vem do ambiente e de segredos do Docker (variaveis terminadas em ``_FILE``).
Mudam com um reinicio. Valores de politica ficam na tabela ``politica`` do
PostgreSQL (ver ``mcp_claro.config.politica``).

A partida recusa combinacoes invalidas: o gateway nao sobe com versao do MCP
diferente de 2026-07-28, com ``EDGE_MODE=apigee`` (reservado, decisao 7) nem,
em piloto ou producao, com banco sem ``verify-full``.
"""

from __future__ import annotations

import enum
from functools import cached_property
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import BeforeValidator, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from mcp_claro.versao import VERSAO_MCP

_VERDADE = {"ligado", "verdadeiro", "sim", "true", "1", "on", "yes"}
_FALSO = {"desligado", "falso", "nao", "não", "false", "0", "off", "no"}


def _liga_desliga(valor: Any) -> Any:
    """Aceita ``ligado``/``desligado`` e ``verdadeiro``/``falso``, como no plano."""
    if isinstance(valor, str):
        v = valor.strip().lower()
        if v in _VERDADE:
            return True
        if v in _FALSO:
            return False
        raise ValueError(f"valor '{valor}' nao e liga/desliga; use ligado ou desligado")
    return valor


Chave = Annotated[bool, BeforeValidator(_liga_desliga)]


def _lista(valor: Any) -> Any:
    if isinstance(valor, str):
        return [p.strip() for p in valor.split(",") if p.strip()]
    return valor


ListaTexto = Annotated[list[str], NoDecode, BeforeValidator(_lista)]


class Ambiente(enum.StrEnum):
    DESENVOLVIMENTO = "desenvolvimento"
    TESTE = "teste"
    PILOTO = "piloto"
    PRODUCAO = "producao"


class ModoPortao(enum.StrEnum):
    PROPRIO = "proprio"
    APIGEE = "apigee"  # reservado: a partida recusa (decisao 7)


class OrigemPortao(enum.StrEnum):
    INTERNET = "internet"
    APIGEE_MTLS = "apigee_mtls"


class ModoResposta(enum.StrEnum):
    INTEIRA = "inteira"
    FLUXO = "fluxo"  # so depois de testado nos portoes; a partida recusa por ora


class SslBanco(enum.StrEnum):
    DISABLE = "disable"
    REQUIRE = "require"
    VERIFY_CA = "verify-ca"
    VERIFY_FULL = "verify-full"


class TipoProvedor(enum.StrEnum):
    KEYCLOAK = "keycloak"
    LOGTO = "logto"
    OIDC = "oidc"  # provedor generico com JWKS; usado nos testes


class ModoCofre(enum.StrEnum):
    STATIC = "static"
    SHAMIR = "shamir"
    TRANSIT = "transit"
    KMS = "kms"


class PropagacaoRastro(enum.StrEnum):
    NUNCA = "nunca"
    SISTEMAS_PROPRIOS = "sistemas_proprios"


class ExportaRastros(enum.StrEnum):
    DESLIGADO = "desligado"
    OTLP = "otlp"


def _parece_email(valor: str) -> bool:
    if any(c.isspace() for c in valor) or valor.count("@") != 1:
        return False
    local, dominio = valor.split("@")
    return bool(local) and "." in dominio and not dominio.startswith(".") and not dominio.endswith(".")


class ConfiguracaoInvalida(Exception):
    """A partida recusa erro: uma ou mais combinacoes de parametros sao invalidas."""

    def __init__(self, problemas: list[str]) -> None:
        self.problemas = problemas
        texto = "\n".join(f"- {p}" for p in problemas)
        super().__init__(f"Configuracao recusada na partida:\n{texto}")


def _ler_segredo(caminho: str | None) -> SecretStr | None:
    if not caminho:
        return None
    conteudo = Path(caminho).read_text(encoding="utf-8").strip()
    return SecretStr(conteudo)


class Parametros(BaseSettings):
    """Todos os parametros de implantacao, com os valores iniciais do plano."""

    model_config = SettingsConfigDict(
        env_file=None,
        case_sensitive=True,
        extra="ignore",
        frozen=True,
    )

    # Ambiente
    APP_ENV: Ambiente = Ambiente.DESENVOLVIMENTO

    # Portao, endereco e versao
    EDGE_MODE: ModoPortao = ModoPortao.PROPRIO
    EDGE_ORIGIN_AUTH: OrigemPortao = OrigemPortao.INTERNET
    EDGE_TOKEN_PRECHECK: Chave = False
    PUBLIC_BASE_URL: str = "https://mcp-claro.localhost"
    MCP_RESOURCE_ID: str | None = None
    MCP_ACCEPTED_VERSIONS: ListaTexto = Field(default_factory=lambda: [VERSAO_MCP])
    MCP_UPSTREAM_VERSION: str = VERSAO_MCP
    MCP_RESPONSE_MODE: ModoResposta = ModoResposta.INTEIRA
    MCP_SCOPES: ListaTexto = Field(default_factory=list)
    RESPONSE_MAX_WAIT_SECONDS: int = Field(default=20, ge=1, le=120)
    # Varredura de reserva da porta de saida (alem do LISTEN/NOTIFY), em segundos.
    OUTBOX_SWEEP_SECONDS: int = Field(default=5, ge=1, le=300)
    ALLOWED_HOSTS: ListaTexto = Field(default_factory=list)
    FORWARDED_ALLOW_IPS: ListaTexto = Field(default_factory=lambda: ["127.0.0.1"])
    GATEWAY_INTERNAL_PORT: int = 8443
    DISPATCHER_INTERNAL_PORT: int = 9443
    ROUTINES_INTERNAL_PORT: int = 9444
    INTERNAL_CA_FILE: str | None = None
    INTERNAL_CERT_FILE: str | None = None
    INTERNAL_KEY_FILE: str | None = None
    INTERNAL_CLIENT_CA_FILE: str | None = None
    INTERNAL_CLIENT_CERT_FILE: str | None = None
    INTERNAL_CLIENT_KEY_FILE: str | None = None
    INTERNAL_HEALTH_CERT_FILE: str | None = None
    INTERNAL_HEALTH_KEY_FILE: str | None = None
    DISPATCHER_INTERNAL_URL: str = "https://despachante:9443"
    GATEWAY_WORKERS: int = Field(default=2, ge=1, le=64)

    # Banco, login e cofre
    DATABASE_URL: str = "postgresql://mcp_claro_entrada@localhost:5432/mcp_claro"
    DATABASE_PASSWORD_FILE: str | None = None
    DB_SSLMODE: SslBanco = SslBanco.VERIFY_FULL
    DB_SSLROOTCERT: str | None = None
    IDP_KIND: TipoProvedor = TipoProvedor.KEYCLOAK
    IDP_ISSUER_URL: str = "https://mcp-claro.localhost/login/realms/mcp-claro"
    IDP_JWKS_URL: str | None = None
    IDP_JWKS_CACHE_SECONDS: int = Field(default=300, ge=30, le=86400)
    IDP_ALGORITHMS: ListaTexto = Field(default_factory=lambda: ["RS256", "ES256", "PS256", "EdDSA"])
    VAULT_ADDR: str = "https://cofre:8200"
    VAULT_SEAL_MODE: ModoCofre = ModoCofre.STATIC
    VAULT_AUTH_METHOD: str = "approle"
    VAULT_ROLE_ID_FILE: str | None = None
    VAULT_SECRET_ID_FILE: str | None = None
    VAULT_TRANSIT_MOUNT: str = "transit"
    VAULT_TRANSIT_KEY: str = "mcp-claro-chaves"
    VAULT_KV_MOUNT: str = "kv"
    VAULT_STATE_KEY_PATH: str = "mcp-claro/estado-confirmacoes"
    VAULT_TIMEOUT_SECONDS: int = Field(default=5, ge=1, le=60)
    # O despachante que segura a porta de saida confere a saude do cofre a cada tantos segundos: trancado,
    # a saida fecha na hora; sem resposta, fecha na segunda conferencia seguida (``saida.cofre``).
    VAULT_CHECK_SECONDS: int = Field(default=15, ge=5, le=300)
    REQUEST_STATE_KEY_ROTATION_DAYS: int = Field(default=30, ge=1, le=365)

    # Chaves de liga e desliga
    FEATURE_PHASE1_READ: Chave = False
    FEATURE_PHASE2_ACT: Chave = False
    FEATURE_PHASE3_THIRD_PARTY: Chave = False
    FEATURE_PHASE4_BADGES: Chave = False
    FEATURE_TASKS: Chave = False
    FEATURE_PASSKEY: Chave = True
    FEATURE_TOOL_SEARCH: Chave = False
    PATROL_AUTOCLOSE_ENABLED: Chave = True
    AUTH_ENTERPRISE_ASSERTION_ENABLED: Chave = False

    # Programas que rodam sozinhos (decisao 9, camada 1)
    PROGRAM_AUTH_METHODS: ListaTexto = Field(default_factory=lambda: ["private_key_jwt"])
    PROGRAM_TOKEN_TTL_SECONDS: int = Field(default=300, ge=60, le=3600)
    PROGRAM_KEY_MAX_AGE_DAYS: int = Field(default=90, ge=1, le=365)
    PROGRAM_KEY_EXPIRY_WARN_DAYS: int = Field(default=14, ge=1, le=90)
    PROGRAM_ACTIONS_ENABLED: Chave = False
    PROGRAM_PENDING_TIMEOUT_HOURS: int = Field(default=24, ge=1, le=168)
    PROGRAM_OWNER_RECERT_DAYS: int = Field(default=90, ge=7, le=365)
    PROGRAM_IDLE_PAUSE_DAYS: int = Field(default=30, ge=1, le=365)
    PROGRAM_MAX_REQUESTS_PER_MINUTE: int = Field(default=30, ge=1, le=10000)

    # Avisos, ronda, vigia e backup
    SMTP_HOST: str | None = None
    SMTP_PORT: int = 587
    SMTP_FROM: str | None = None
    SMTP_USERNAME: str | None = None
    SMTP_PASSWORD_FILE: str | None = None
    SMTP_STARTTLS: Chave = True
    WEBHOOK_SECRET_FILE: str | None = None
    NOTIFY_PENDING_AFTER_MINUTES: int = Field(default=5, ge=1, le=1440)
    NOTIFY_RETRY_MAX: int = Field(default=5, ge=0, le=50)
    PATROL_INTERVAL_SECONDS: int = Field(default=300, ge=30, le=86400)
    PATROL_FAILS_TO_CLOSE: int = Field(default=2, ge=1, le=10)
    CLOSING_INTERVAL_HOURS: int = Field(default=6, ge=1, le=24)
    WATCH_PUBLIC_URL: str | None = None
    WATCH_PROBE_INTERVAL_SECONDS: int = Field(default=600, ge=30, le=86400)
    WATCH_SILENCE_ALARM_SECONDS: int = Field(default=900, ge=60, le=86400)
    BACKUP_RESTORE_DRILL_DAYS: int = Field(default=30, ge=1, le=365)

    # Telas (Fase 1): login das pessoas pelo provedor (OIDC com PKCE)
    SCREENS_CLIENT_ID: str = "mcp-claro-telas"
    SCREENS_CLIENT_SECRET_FILE: str | None = None
    SCREENS_SESSION_TTL_MINUTES: int = Field(default=480, ge=15, le=1440)
    SCREENS_SESSION_IDLE_MINUTES: int = Field(default=30, ge=5, le=1440)
    SCREENS_LOGIN_TTL_SECONDS: int = Field(default=600, ge=60, le=1800)
    SCREENS_IDP_AUTHORIZATION_URL: str | None = None
    SCREENS_IDP_TOKEN_URL: str | None = None
    SCREENS_TIMEZONE: str = "America/Sao_Paulo"
    SCREENS_FOREIGN_LEDGER_WINDOW_MINUTES: int = Field(default=30, ge=5, le=240)
    # Idade maxima do login (auth_time do provedor) para contar como "login refeito na hora", em segundos.
    # A faixa (60 a 900) e conferida em ``problemas``, com frase legivel.
    SCREENS_RECENT_LOGIN_SECONDS: int = 300

    # Rotinas e avisos (Fase 1)
    NOTIFY_ADMIN_EMAILS: ListaTexto = Field(default_factory=list)
    GRAFANA_WEBHOOK_PASSWORD_FILE: str | None = None
    NOTIFY_RETRY_BASE_SECONDS: int = Field(default=30, ge=1, le=3600)
    NOTIFY_RETRY_CAP_SECONDS: int = Field(default=3600, ge=60, le=86400)
    NOTIFY_POLL_SECONDS: int = Field(default=5, ge=1, le=300)
    ROUTINES_INTERVAL_SECONDS: int = Field(default=300, ge=30, le=3600)
    TOOL_DRIFT_CHECK_SECONDS: int = Field(default=900, ge=60, le=86400)

    # Ronda das portas e vigia de fora (Fase 1); regras de recusa do papel vigia em vigia/config.py
    PATROL_CLIENT_ID: str = "mcp-claro-ronda"
    PATROL_KEY_FILE: str | None = None
    PATROL_KEY_ID: str | None = None
    PATROL_TOKEN_URL: str | None = None
    PATROL_TOKEN_SCOPE: str = "mcp-claro"  # noqa: S105 - nome de escopo OAuth, nao segredo
    PATROL_OTHER_AUDIENCE_SCOPE: str = "outro-servico"
    PATROL_CA_FILE: str | None = None
    WATCH_CLIENT_ID: str = "mcp-claro-vigia"
    WATCH_KEY_FILE: str | None = None
    WATCH_KEY_ID: str | None = None
    WATCH_TOKEN_URL: str | None = None
    WATCH_TOKEN_SCOPE: str = "mcp-claro"  # noqa: S105 - nome de escopo OAuth, nao segredo
    WATCH_OTHER_AUDIENCE_SCOPE: str = "outro-servico"
    WATCH_FOREIGN_TOOL: str = "mcp_claro_vigia_ferramenta_alheia"
    WATCH_CA_FILE: str | None = None
    WATCH_PING_SECONDS: int = Field(default=60, ge=10, le=3600)
    WATCH_ALARM_EMAILS: ListaTexto = Field(default_factory=list)
    WATCH_SMTP_HOST: str | None = None
    WATCH_SMTP_PORT: int = 587
    WATCH_SMTP_FROM: str | None = None
    WATCH_SMTP_USERNAME: str | None = None
    WATCH_SMTP_PASSWORD_FILE: str | None = None
    WATCH_SMTP_STARTTLS: Chave = True
    WATCH_WEBHOOK_TARGETS: ListaTexto = Field(default_factory=list)
    WATCH_WEBHOOK_SECRET_FILE: str | None = None
    WATCH_INTERNAL_PORT: int = 9445
    WATCH_CONTINUITY_TOKEN_FILE: str | None = None
    WATCH_CONTINUITY_URL: str | None = None
    WATCH_CONTINUITY_CA_FILE: str | None = None
    # Conferencia de continuidade na partida do despachante exige resposta do vigia. Sem valor: ligado em
    # piloto e producao, desligado em desenvolvimento e teste (onde nao ha segunda maquina).
    CONTINUITY_REQUIRE_WATCH: Annotated[bool | None, BeforeValidator(_liga_desliga)] = None

    # Limites de pedidos e medicoes
    EDGE_RATE_PER_IP_PER_MINUTE: int = Field(default=120, ge=1)
    RATE_PER_APP_PER_MINUTE: int = Field(default=60, ge=1)
    RATE_PER_PERSON_PER_MINUTE: int = Field(default=30, ge=1)
    MAX_REQUEST_BYTES: int = Field(default=4 * 1024 * 1024, ge=1024, le=64 * 1024 * 1024)
    METRICS_FLUSH_SECONDS: int = Field(default=30, ge=5, le=3600)
    METRICS_MINUTE_RETENTION_DAYS: int = Field(default=31, ge=1)
    METRICS_HOUR_RETENTION_MONTHS: int = Field(default=13, ge=1)
    METRICS_DAY_RETENTION_YEARS: int = Field(default=5, ge=1)
    OBS_TRACE_ACCEPT_INBOUND: Chave = True
    OBS_TRACE_PROPAGATE_DOWNSTREAM: PropagacaoRastro = PropagacaoRastro.SISTEMAS_PROPRIOS
    OBS_TRACES_EXPORT: ExportaRastros = ExportaRastros.DESLIGADO
    OBS_EXPORT_CONTENT: Chave = False
    LOG_LEVEL: str = "INFO"

    @field_validator("PUBLIC_BASE_URL", "IDP_ISSUER_URL")
    @classmethod
    def _sem_barra_final(cls, valor: str) -> str:
        return valor.rstrip("/")

    @model_validator(mode="after")
    def _derivados(self) -> Parametros:
        if self.MCP_RESOURCE_ID is None:
            object.__setattr__(self, "MCP_RESOURCE_ID", f"{self.PUBLIC_BASE_URL}/mcp")
        if not self.ALLOWED_HOSTS:
            host = urlsplit(self.PUBLIC_BASE_URL).hostname or ""
            object.__setattr__(self, "ALLOWED_HOSTS", [host] if host else [])
        if self.WATCH_PUBLIC_URL is None:
            object.__setattr__(self, "WATCH_PUBLIC_URL", self.PUBLIC_BASE_URL)
        if self.CONTINUITY_REQUIRE_WATCH is None:
            object.__setattr__(self, "CONTINUITY_REQUIRE_WATCH", self.em_producao)
        return self

    @property
    def recurso(self) -> str:
        """Identificador do recurso que o crachá precisa citar como audiencia."""
        if self.MCP_RESOURCE_ID is None:  # preenchido em _derivados; nunca deveria faltar
            raise ConfiguracaoInvalida(["MCP_RESOURCE_ID ficou vazio."])
        return self.MCP_RESOURCE_ID

    @property
    def em_producao(self) -> bool:
        return self.APP_ENV in (Ambiente.PILOTO, Ambiente.PRODUCAO)

    @cached_property
    def senha_banco(self) -> SecretStr | None:
        return _ler_segredo(self.DATABASE_PASSWORD_FILE)

    @cached_property
    def senha_smtp(self) -> SecretStr | None:
        return _ler_segredo(self.SMTP_PASSWORD_FILE)

    @cached_property
    def segredo_webhook(self) -> SecretStr | None:
        return _ler_segredo(self.WEBHOOK_SECRET_FILE)

    def problemas(self) -> list[str]:
        """Lista as combinacoes que a partida recusa. Vazia quando tudo esta certo."""
        erros: list[str] = []
        if self.MCP_ACCEPTED_VERSIONS != [VERSAO_MCP]:
            erros.append(
                f"MCP_ACCEPTED_VERSIONS deve ser exatamente {VERSAO_MCP} (decisao 8). "
                "Abrir outra versao pede mudanca de codigo revisada."
            )
        if self.MCP_UPSTREAM_VERSION != VERSAO_MCP:
            erros.append(f"MCP_UPSTREAM_VERSION deve ser {VERSAO_MCP} (decisao 8).")
        if self.EDGE_MODE is ModoPortao.APIGEE:
            erros.append("EDGE_MODE=apigee esta reservado e ainda nao e aceito (decisao 7).")
        if self.EDGE_ORIGIN_AUTH is OrigemPortao.APIGEE_MTLS and self.EDGE_MODE is not ModoPortao.APIGEE:
            erros.append("EDGE_ORIGIN_AUTH=apigee_mtls so vale com EDGE_MODE=apigee.")
        if self.EDGE_TOKEN_PRECHECK:
            erros.append("EDGE_TOKEN_PRECHECK so pode ligar com o Apigee, depois de testado.")
        if self.MCP_RESPONSE_MODE is ModoResposta.FLUXO:
            erros.append("MCP_RESPONSE_MODE=fluxo ainda nao foi testado nos portoes; use inteira.")
        if not self.PUBLIC_BASE_URL.startswith("https://"):
            erros.append("PUBLIC_BASE_URL deve comecar com https://.")
        if self.em_producao:
            if self.DB_SSLMODE is not SslBanco.VERIFY_FULL:
                erros.append("Em piloto ou producao, DB_SSLMODE deve ser verify-full.")
            if self.VAULT_SEAL_MODE is ModoCofre.KMS:
                erros.append("VAULT_SEAL_MODE=kms usa servico comercial; fora da regra do codigo aberto.")
            if not self.INTERNAL_CA_FILE:
                erros.append("Em piloto ou producao, INTERNAL_CA_FILE e obrigatorio (mTLS interno).")
            if not self.INTERNAL_CLIENT_CA_FILE:
                erros.append(
                    "Em piloto ou producao, INTERNAL_CLIENT_CA_FILE e obrigatorio: cada porta interna "
                    "aceita so o certificado de quem pode chama-la."
                )
            if "localhost" in self.PUBLIC_BASE_URL:
                erros.append("Em piloto ou producao, PUBLIC_BASE_URL nao pode ser localhost.")
            if not self.VAULT_ADDR.startswith("https://"):
                erros.append("Em piloto ou producao, VAULT_ADDR deve comecar com https:// (TLS interno).")
        if self.VAULT_AUTH_METHOD != "approle":
            erros.append("VAULT_AUTH_METHOD aceita so approle (login de maquina com token curto).")
        if self.INTERNAL_CERT_FILE and not self.INTERNAL_CLIENT_CA_FILE:
            erros.append(
                "Porta interna com TLS exige INTERNAL_CLIENT_CA_FILE: sem ela, qualquer certificado da autoridade "
                "interna entraria (mTLS falharia aberto)."
            )
        if self.em_producao:
            if not (self.INTERNAL_CERT_FILE and self.INTERNAL_KEY_FILE):
                erros.append("Em piloto ou producao, a porta interna exige INTERNAL_CERT_FILE e INTERNAL_KEY_FILE.")
            if not self.DISPATCHER_INTERNAL_URL.startswith("https://"):
                erros.append("Em piloto ou producao, DISPATCHER_INTERNAL_URL deve usar https (mTLS).")
        if self.SCREENS_SESSION_IDLE_MINUTES > self.SCREENS_SESSION_TTL_MINUTES:
            erros.append("SCREENS_SESSION_IDLE_MINUTES nao pode passar de SCREENS_SESSION_TTL_MINUTES.")
        if not 60 <= self.SCREENS_RECENT_LOGIN_SECONDS <= 900:
            erros.append("SCREENS_RECENT_LOGIN_SECONDS aceita de 60 a 900.")
        if self.NOTIFY_RETRY_CAP_SECONDS < self.NOTIFY_RETRY_BASE_SECONDS:
            erros.append("NOTIFY_RETRY_CAP_SECONDS deve ser maior ou igual a NOTIFY_RETRY_BASE_SECONDS.")
        email_ruim = [e for e in self.NOTIFY_ADMIN_EMAILS if not _parece_email(e)]
        if email_ruim:
            erros.append("NOTIFY_ADMIN_EMAILS tem endereco invalido.")
        if self.SMTP_USERNAME and not self.SMTP_PASSWORD_FILE:
            erros.append("SMTP_USERNAME exige SMTP_PASSWORD_FILE.")
        if self.SMTP_FROM and not _parece_email(self.SMTP_FROM):
            erros.append("SMTP_FROM deve ser um endereco de e-mail.")
        if self.em_producao:
            if not self.SCREENS_CLIENT_SECRET_FILE:
                erros.append("Em piloto ou producao, as telas usam cliente confidencial: SCREENS_CLIENT_SECRET_FILE.")
            if not self.NOTIFY_ADMIN_EMAILS:
                erros.append("Em piloto ou producao, NOTIFY_ADMIN_EMAILS precisa de pelo menos um endereco.")
            if not self.GRAFANA_WEBHOOK_PASSWORD_FILE:
                erros.append("Em piloto ou producao, GRAFANA_WEBHOOK_PASSWORD_FILE e obrigatorio.")
            if self.SMTP_HOST and not self.SMTP_STARTTLS and self.SMTP_PORT != 465:
                erros.append("Em piloto ou producao, o SMTP exige STARTTLS (ou a porta 465).")
        if not self.PATROL_CLIENT_ID.strip() or not self.WATCH_CLIENT_ID.strip():
            erros.append("PATROL_CLIENT_ID e WATCH_CLIENT_ID nao podem ficar vazios.")
        if 2 * self.WATCH_PING_SECONDS > self.WATCH_SILENCE_ALARM_SECONDS:
            erros.append("WATCH_PING_SECONDS precisa caber duas vezes em WATCH_SILENCE_ALARM_SECONDS.")
        if any(not u.startswith("https://") for u in self.WATCH_WEBHOOK_TARGETS):
            erros.append("WATCH_WEBHOOK_TARGETS aceita so enderecos https://.")
        if self.WATCH_WEBHOOK_TARGETS and not self.WATCH_WEBHOOK_SECRET_FILE:
            erros.append("WATCH_WEBHOOK_TARGETS exige WATCH_WEBHOOK_SECRET_FILE.")
        if any(not _parece_email(e) for e in self.WATCH_ALARM_EMAILS):
            erros.append("WATCH_ALARM_EMAILS tem endereco invalido.")
        if self.em_producao:
            for nome in ("PATROL_TOKEN_URL", "WATCH_TOKEN_URL", "WATCH_CONTINUITY_URL"):
                valor = getattr(self, nome)
                if valor and not str(valor).startswith("https://"):
                    erros.append(f"Em piloto ou producao, {nome} deve comecar com https://.")
            if not self.CONTINUITY_REQUIRE_WATCH:
                erros.append(
                    "Em piloto ou producao, a conferencia de continuidade exige o vigia (CONTINUITY_REQUIRE_WATCH)."
                )
            if not self.WATCH_CONTINUITY_URL or not self.WATCH_CONTINUITY_TOKEN_FILE:
                erros.append(
                    "Em piloto ou producao, WATCH_CONTINUITY_URL e WATCH_CONTINUITY_TOKEN_FILE sao obrigatorios."
                )
            if self.PATROL_CA_FILE or self.WATCH_CA_FILE:
                erros.append("PATROL_CA_FILE e WATCH_CA_FILE so valem em desenvolvimento e teste.")
        if self.FEATURE_TASKS:
            erros.append("FEATURE_TASKS so liga com implementacao propria sobre PostgreSQL (decisao 13).")
        if self.AUTH_ENTERPRISE_ASSERTION_ENABLED:
            erros.append("AUTH_ENTERPRISE_ASSERTION_ENABLED so liga com provedor da organizacao.")
        if self.FEATURE_PHASE2_ACT and not self.FEATURE_PHASE1_READ:
            erros.append("FEATURE_PHASE2_ACT exige FEATURE_PHASE1_READ.")
        if self.FEATURE_PHASE3_THIRD_PARTY and not self.FEATURE_PHASE2_ACT:
            erros.append("FEATURE_PHASE3_THIRD_PARTY exige FEATURE_PHASE2_ACT (decisao 10).")
        if self.FEATURE_PHASE4_BADGES and not self.FEATURE_PHASE3_THIRD_PARTY:
            erros.append("FEATURE_PHASE4_BADGES exige FEATURE_PHASE3_THIRD_PARTY (decisao 10).")
        if self.PROGRAM_ACTIONS_ENABLED:
            erros.append(
                "PROGRAM_ACTIONS_ENABLED ainda nao liga: as acoes de programas ainda nao tem as pendencias do"
                " responsavel nem a reconferencia completa. Ligam depois, por decisao registrada (decisao 9)."
            )
        if self.OBS_EXPORT_CONTENT:
            erros.append("OBS_EXPORT_CONTENT nao muda no comeco: rastros saem sem conteudo.")
        metodos_ok = {"private_key_jwt", "client_secret"}
        if not set(self.PROGRAM_AUTH_METHODS) <= metodos_ok:
            erros.append("PROGRAM_AUTH_METHODS aceita so private_key_jwt e client_secret.")
        if self.PROGRAM_KEY_EXPIRY_WARN_DAYS >= self.PROGRAM_KEY_MAX_AGE_DAYS:
            erros.append("PROGRAM_KEY_EXPIRY_WARN_DAYS deve ser menor que PROGRAM_KEY_MAX_AGE_DAYS.")
        portas = [self.GATEWAY_INTERNAL_PORT, self.DISPATCHER_INTERNAL_PORT, self.ROUTINES_INTERNAL_PORT]
        if len(set(portas)) != len(portas):
            erros.append("As portas internas da entrada, do despachante e das rotinas devem ser diferentes.")
        return erros

    def conferir(self) -> Parametros:
        """Levanta ``ConfiguracaoInvalida`` se houver qualquer problema."""
        erros = self.problemas()
        if erros:
            raise ConfiguracaoInvalida(erros)
        return self


def carregar(**valores: Any) -> Parametros:
    """Le os parametros do ambiente (ou dos valores dados) e confere tudo."""
    return Parametros(**valores).conferir()
