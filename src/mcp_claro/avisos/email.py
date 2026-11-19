"""Envio de avisos por e-mail, com ``aiosmtplib``.

Porta 587 com STARTTLS por padrao; porta 465 usa TLS direto. O certificado do
servidor e sempre conferido pelo repositorio do sistema. A senha vem de
``SMTP_PASSWORD_FILE`` (``Parametros.senha_smtp``). O erro guardado na fila leva
so o tipo e o codigo SMTP, nunca o texto do servidor, que pode repetir o endereco.
"""

from __future__ import annotations

import ssl
import uuid
from email.message import EmailMessage
from email.utils import formatdate

import aiosmtplib
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncConnection

from mcp_claro.avisos.destinos import PREFIXO_PESSOA, email_da_pessoa, parece_email
from mcp_claro.avisos.fila import Aviso, FalhaEnvio
from mcp_claro.config.settings import Parametros

RODAPE = (
    "Este e um aviso automatico do MCP-CLARO. Ele nao traz dados das suas consultas.\n"
    "Para ver detalhes, entre nas telas do MCP-CLARO."
)


def _uma_linha(valor: str, maximo: int = 200) -> str:
    return " ".join(valor.split())[:maximo]


def montar_mensagem(aviso: Aviso, remetente: str, para: str) -> EmailMessage:
    """Mensagem de texto simples: frase do aviso, linhas de apoio e rodape. Sem HTML."""
    msg = EmailMessage()
    msg["From"] = remetente
    msg["To"] = para
    msg["Subject"] = _uma_linha(aviso.assunto)
    dominio = remetente.rsplit("@", 1)[-1] if "@" in remetente else "mcp-claro.local"
    msg["Message-ID"] = f"<{aviso.identificador}@{_uma_linha(dominio, 100)}>"
    msg["Date"] = formatdate(usegmt=True)
    msg["Auto-Submitted"] = "auto-generated"
    msg["X-MCP-CLARO-Evento"] = _uma_linha(aviso.evento, 80)
    partes = [str(aviso.corpo.get("texto") or aviso.assunto)]
    linhas = aviso.corpo.get("linhas")
    if isinstance(linhas, list) and linhas:
        partes.append("\n".join(f"- {_uma_linha(str(item), 300)}" for item in linhas[:50]))
    partes.append(RODAPE)
    msg.set_content("\n\n".join(partes))
    return msg


class EnvioEmail:
    """Canal ``email``. Um envio por aviso; a fila cuida das novas tentativas."""

    def __init__(
        self,
        *,
        host: str,
        porta: int,
        remetente: str,
        usuario: str | None = None,
        senha: SecretStr | None = None,
        starttls: bool = True,
        prazo: float = 20.0,
        contexto_tls: ssl.SSLContext | None = None,
    ) -> None:
        self.host = host
        self.porta = porta
        self.remetente = remetente
        self.usuario = usuario
        self.senha = senha
        self.tls_direto = porta == 465
        self.starttls = starttls and not self.tls_direto
        self.prazo = prazo
        self.contexto_tls = contexto_tls

    @classmethod
    def de_parametros(cls, p: Parametros) -> EnvioEmail | None:
        """``None`` quando ``SMTP_HOST`` ou ``SMTP_FROM`` faltam: o canal fica desligado."""
        if not p.SMTP_HOST or not p.SMTP_FROM:
            return None
        return cls(
            host=p.SMTP_HOST,
            porta=p.SMTP_PORT,
            remetente=p.SMTP_FROM,
            usuario=p.SMTP_USERNAME,
            senha=p.senha_smtp,
            starttls=p.SMTP_STARTTLS,
        )

    async def endereco(self, conexao: AsyncConnection, aviso: Aviso) -> str:
        if aviso.destino.startswith(PREFIXO_PESSOA):
            try:
                pessoa_id = uuid.UUID(aviso.destino[len(PREFIXO_PESSOA) :])
            except ValueError:
                raise FalhaEnvio("destino de pessoa invalido", permanente=True) from None
            email = await email_da_pessoa(conexao, pessoa_id)
            if email is None:
                raise FalhaEnvio("pessoa sem e-mail conhecido ou desativada", permanente=True)
            return email
        if not parece_email(aviso.destino):
            raise FalhaEnvio("destino de e-mail invalido", permanente=True)
        return aviso.destino

    async def enviar(self, conexao: AsyncConnection, aviso: Aviso) -> None:
        para = await self.endereco(conexao, aviso)
        mensagem = montar_mensagem(aviso, self.remetente, para)
        contexto = self.contexto_tls or ssl.create_default_context()
        try:
            await aiosmtplib.send(
                mensagem,
                hostname=self.host,
                port=self.porta,
                username=self.usuario,
                password=self.senha.get_secret_value() if self.senha else None,
                use_tls=self.tls_direto,
                start_tls=self.starttls,
                tls_context=contexto if (self.tls_direto or self.starttls) else None,
                timeout=self.prazo,
            )
        except aiosmtplib.SMTPResponseException as erro:
            raise FalhaEnvio(f"smtp {type(erro).__name__} {erro.code}") from None
        except aiosmtplib.SMTPRecipientsRefused as erro:
            codigos = sorted({r.code for r in erro.recipients})
            raise FalhaEnvio(f"smtp destinatario recusado {codigos}") from None
        except aiosmtplib.SMTPException as erro:
            raise FalhaEnvio(f"smtp {type(erro).__name__}") from None
        except (OSError, TimeoutError) as erro:
            raise FalhaEnvio(f"smtp sem conexao ({type(erro).__name__})") from None
