"""Cliente fino do cofre (OpenBao 2.7).

O cofre guarda so as chaves de cifra, no motor Transit, e a chave comum do estado
das confirmacoes, num caminho separado do KV v2. As chaves das pessoas para os
sistemas ficam cifradas nas tabelas do gateway: o despachante cifra e decifra pelo
Transit, com uma chave ``derived`` e um contexto que amarra cada texto cifrado ao par
(sistema, dono). A entrada so le a chave do estado; as rotinas so a trocam.

Cada papel entra com o proprio AppRole (``VAULT_ROLE_ID_FILE`` e
``VAULT_SECRET_ID_FILE``) e recebe um token curto. O cliente renova o token antes de
vencer e entra de novo quando ele chega a validade maxima ou e revogado por fora.

Enquanto o cofre estiver trancado (sealed) ou fora do ar, todo pedido levanta
``CofreTrancado`` ou ``CofreIndisponivel``: quem chama fecha a porta de saida.
``saude()`` diz se o cofre esta destravado, sem precisar de login.

Todo HTTP sai por ``httpx2``, sem seguir redirecionamentos, com tempo limite, sem ler
variaveis de proxy e com contexto TLS proprio, que confia so em ``INTERNAL_CA_FILE``
(nunca em ``SSL_CERT_FILE``). Token, chave e conteudo nunca vao para os logs nem para
as mensagens de erro.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import math
import re
import secrets
import ssl
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Self
from urllib.parse import urlsplit

import anyio
import httpx2
from pydantic import SecretStr

from mcp_claro.config.settings import ConfiguracaoInvalida, Parametros
from mcp_claro.logs import logger

_log = logger("mcp_claro.cofre")

TAMANHO_CHAVE_ESTADO: Final = 32
"""Bytes da chave comum do estado das confirmacoes: minimo aceito e tamanho gerado no rodizio."""

CAMPO_CHAVE_ESTADO: Final = "chave"
"""Campo do segredo no KV v2 que guarda a chave do estado, em base64."""

_NOME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_SAUDE = {"standbyok": "true", "perfstandbyok": "true", "sealedcode": "200", "uninitcode": "200"}
_TEMPO_REVOGAR = 2.0


class ErroCofre(Exception):
    """Base dos erros do cofre. As mensagens nunca levam token, chave nem conteudo."""


class CofreIndisponivel(ErroCofre):
    """O cofre nao respondeu (rede, TLS, tempo esgotado) ou falhou do lado dele (5xx, 429)."""


class CofreTrancado(ErroCofre):
    """O cofre esta trancado (sealed) ou ainda nao foi iniciado. A porta de saida fica fechada."""


class PedidoRecusado(ErroCofre):
    """O cofre recusou o pedido (4xx)."""

    def __init__(self, mensagem: str, *, status: int) -> None:
        super().__init__(mensagem)
        self.status = status


class AcessoNegado(PedidoRecusado):
    """O token do papel nao tem permissao nesse caminho (403). Um papel nao usa o login do outro."""


class LoginRecusado(PedidoRecusado):
    """O AppRole recusou o login: role_id ou secret_id errado, vencido ou de outro papel."""


class CifradoInvalido(PedidoRecusado):
    """O texto cifrado nao abre com essa chave e esse contexto (contexto errado, adulterado ou mal formado)."""


class ChaveEstadoInvalida(ErroCofre):
    """A chave do estado das confirmacoes falta no cofre ou tem menos de 32 bytes."""


@dataclass(frozen=True, slots=True)
class EstadoCofre:
    """Resultado de ``Cofre.saude()``."""

    inicializado: bool
    trancado: bool
    versao: str

    @property
    def destravado(self) -> bool:
        """Pronto para atender: iniciado e nao trancado."""
        return self.inicializado and not self.trancado


@dataclass(frozen=True, slots=True)
class ChaveEstado:
    """Chave do estado das confirmacoes e a versao dela no KV v2. O valor nao aparece em ``repr``."""

    valor: bytes = field(repr=False)
    versao: int


def contexto_tls(p: Parametros) -> ssl.SSLContext:
    """Contexto TLS proprio da conexao interna com o cofre.

    Confia so na autoridade interna (``INTERNAL_CA_FILE``): com ``cafile`` dado, o Python nao
    carrega o repositorio do sistema nem ``SSL_CERT_FILE``. Confere o nome do servidor. Se o
    papel tiver certificado interno, apresenta-o (mTLS).
    """
    if not p.INTERNAL_CA_FILE:
        raise ConfiguracaoInvalida(["VAULT_ADDR com https exige INTERNAL_CA_FILE (autoridade interna)."])
    try:
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=p.INTERNAL_CA_FILE)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if p.INTERNAL_CERT_FILE and p.INTERNAL_KEY_FILE:
            ctx.load_cert_chain(p.INTERNAL_CERT_FILE, p.INTERNAL_KEY_FILE)
    except OSError as erro:  # inclui ssl.SSLError
        raise ConfiguracaoInvalida(
            ["Nao foi possivel carregar INTERNAL_CA_FILE ou o certificado interno do papel para o cofre."]
        ) from erro
    return ctx


def _nome(valor: str, campo: str) -> str:
    """Um trecho de caminho da API: sem barra, sem ``..``, so letras, numeros, ``-``, ``_`` e ``.``."""
    if not _NOME.fullmatch(valor) or ".." in valor:
        raise ValueError(f"{campo} invalido: use letras, numeros, '-', '_' ou '.', sem '..'.")
    return valor


def _caminho(valor: str, campo: str) -> str:
    return "/".join(_nome(parte, campo) for parte in valor.strip("/").split("/"))


def _b64(dados: bytes) -> str:
    return base64.b64encode(dados).decode("ascii")


def _inteiro(valor: object) -> int:
    return valor if isinstance(valor, int) and not isinstance(valor, bool) else 0


def _secao(corpo: Mapping[str, Any], nome: str) -> dict[str, Any]:
    valor = corpo.get(nome)
    return valor if isinstance(valor, dict) else {}


def _json(resposta: httpx2.Response) -> dict[str, Any]:
    if resposta.status_code == 204 or not resposta.content:
        return {}
    try:
        corpo = resposta.json()
    except ValueError as erro:
        raise ErroCofre("O cofre devolveu uma resposta que nao e JSON.") from erro
    if not isinstance(corpo, dict):
        raise ErroCofre("O cofre devolveu uma resposta fora do formato esperado.")
    return corpo


def _mensagem(resposta: httpx2.Response) -> str:
    """Texto curto dos erros do cofre (ex.: ``permission denied``). O OpenBao nao poe segredos ali."""
    try:
        corpo = resposta.json()
    except ValueError:
        return ""
    erros = corpo.get("errors") if isinstance(corpo, dict) else None
    if not isinstance(erros, list):
        return ""
    texto = re.sub(r"\d+ errors? occurred:", " ", " ".join(str(e) for e in erros))
    return " ".join(texto.replace("*", " ").split())[:200]


def _como_segredo(valor: str | SecretStr | None) -> SecretStr | None:
    if valor is None or isinstance(valor, SecretStr):
        return valor
    return SecretStr(valor)


def _ler_arquivo(caminho: str | None, campo: str) -> SecretStr:
    if not caminho:
        raise ConfiguracaoInvalida([f"{campo} e obrigatorio para entrar no cofre (AppRole)."])
    try:
        conteudo = Path(caminho).read_text(encoding="utf-8").strip()
    except OSError as erro:
        raise ConfiguracaoInvalida([f"Nao foi possivel ler o arquivo de {campo}."]) from erro
    if not conteudo:
        raise ConfiguracaoInvalida([f"O arquivo de {campo} esta vazio."])
    return SecretStr(conteudo)


class Cofre:
    """Cliente do cofre para um papel. Use como ``async with Cofre(parametros) as cofre:``.

    O login e preguicoso: o primeiro pedido que precisa de token chama ``entrar()``. Antes de
    cada pedido, se ja passou dois tercos da validade do token, o cliente renova; se o token
    nao renova mais (validade maxima do papel), entra de novo. ``saude()`` nao precisa de
    login. Ao sair do bloco, o token e revogado.

    ``role_id`` e ``secret_id`` dados aqui valem mais que os arquivos dos parametros.
    ``transporte`` e ``relogio`` existem para os testes.
    """

    def __init__(
        self,
        parametros: Parametros,
        *,
        role_id: str | SecretStr | None = None,
        secret_id: str | SecretStr | None = None,
        transporte: httpx2.AsyncBaseTransport | None = None,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        endereco = parametros.VAULT_ADDR.rstrip("/")
        partes = urlsplit(endereco)
        if (
            partes.scheme not in {"http", "https"}
            or not partes.hostname
            or partes.username
            or partes.path
            or partes.query
        ):
            raise ConfiguracaoInvalida(["VAULT_ADDR invalido: use https://<host>:<porta>, sem caminho."])
        try:
            self._transit = _caminho(parametros.VAULT_TRANSIT_MOUNT, "VAULT_TRANSIT_MOUNT")
            self._kv = _caminho(parametros.VAULT_KV_MOUNT, "VAULT_KV_MOUNT")
            self._estado = _caminho(parametros.VAULT_STATE_KEY_PATH, "VAULT_STATE_KEY_PATH")
            self._metodo = _caminho(parametros.VAULT_AUTH_METHOD, "VAULT_AUTH_METHOD")
            self._chave_padrao = _nome(parametros.VAULT_TRANSIT_KEY, "VAULT_TRANSIT_KEY")
        except ValueError as erro:
            raise ConfiguracaoInvalida([str(erro)]) from erro
        self._p = parametros
        self._endereco = endereco
        self._tls = contexto_tls(parametros) if partes.scheme == "https" else None
        self._role_id = _como_segredo(role_id)
        self._secret_id = _como_segredo(secret_id)
        self._transporte = transporte
        self._relogio = relogio
        self._http: httpx2.AsyncClient | None = None
        self._trava = anyio.Lock()
        self._token: SecretStr | None = None
        self._vence_em = 0.0
        self._validade = 0.0
        self._renovavel = False

    # Ciclo de vida

    async def __aenter__(self) -> Self:
        self._http = httpx2.AsyncClient(
            base_url=f"{self._endereco}/v1/",
            verify=self._tls if self._tls is not None else True,
            timeout=httpx2.Timeout(float(self._p.VAULT_TIMEOUT_SECONDS)),
            follow_redirects=False,
            trust_env=False,
            headers={"X-Vault-Request": "true"},
            transport=self._transporte,
        )
        return self

    async def __aexit__(
        self,
        tipo: type[BaseException] | None,
        erro: BaseException | None,
        rastro: TracebackType | None,
    ) -> None:
        with anyio.CancelScope(shield=True):
            try:
                if self._token is not None and self._http is not None:
                    with anyio.move_on_after(_TEMPO_REVOGAR):
                        await self._revogar(self._token)
            finally:
                self._token = None
                if self._http is not None:
                    await self._http.aclose()
                    self._http = None

    # Saude

    async def saude(self) -> EstadoCofre:
        """Diz se o cofre esta iniciado, trancado e qual a versao. Nao precisa de login.

        Levanta ``CofreIndisponivel`` se o cofre nao responder.
        """
        resposta = await self._bruto("GET", "sys/health", consulta=_SAUDE)
        if resposta.status_code != 200:
            raise CofreIndisponivel(f"A saude do cofre respondeu HTTP {resposta.status_code}.")
        corpo = _json(resposta)
        return EstadoCofre(
            inicializado=corpo.get("initialized") is True,
            trancado=corpo.get("sealed") is not False,
            versao=str(corpo.get("version") or ""),
        )

    # Login de maquina

    async def entrar(self) -> None:
        """Login de maquina pelo AppRole do papel. Troca o token atual (se houver) por um novo."""
        async with self._trava:
            await self._entrar_sem_trava()

    async def renovar(self) -> None:
        """Renova o token agora, sem esperar o proximo pedido. Sem token, entra."""
        async with self._trava:
            if self._token is None:
                await self._entrar_sem_trava()
            else:
                await self._renovar_sem_trava()

    # Transit: so o despachante tem permissao

    async def cifrar(self, chave: str, texto: bytes, *, contexto: bytes | None = None) -> str:
        """Cifra ``texto`` com a chave ``chave`` do Transit e devolve ``vault:vN:...``.

        Com chave ``derived`` (a do gateway), ``contexto`` e obrigatorio e amarra o texto
        cifrado: so o mesmo contexto decifra.
        """
        corpo = {"plaintext": _b64(texto)}
        if contexto is not None:
            corpo["context"] = _b64(contexto)
        resposta = await self._pedir("POST", f"{self._transit}/encrypt/{_nome(chave, 'chave')}", corpo=corpo)
        cifrado = _secao(resposta, "data").get("ciphertext")
        if not isinstance(cifrado, str) or not cifrado.startswith("vault:v"):
            raise ErroCofre("O cofre devolveu um texto cifrado fora do formato esperado.")
        return cifrado

    async def decifrar(self, chave: str, cifrado: str, *, contexto: bytes | None = None) -> bytes:
        """Decifra ``cifrado`` com a chave ``chave`` e o mesmo ``contexto`` usado para cifrar.

        Contexto errado, texto adulterado ou de outra chave levantam ``CifradoInvalido``.
        """
        corpo = {"ciphertext": cifrado}
        if contexto is not None:
            corpo["context"] = _b64(contexto)
        resposta = await self._pedir(
            "POST", f"{self._transit}/decrypt/{_nome(chave, 'chave')}", corpo=corpo, recusa=CifradoInvalido
        )
        texto = _secao(resposta, "data").get("plaintext", "")
        if not isinstance(texto, str):
            raise ErroCofre("O cofre devolveu um texto decifrado fora do formato esperado.")
        try:
            return base64.b64decode(texto, validate=True)
        except (binascii.Error, ValueError) as erro:
            raise ErroCofre("O cofre devolveu um texto decifrado fora do formato esperado.") from erro

    def cifra(self, chave: str | None = None) -> CifraTransit:
        """Cifra de chave fixa no formato que o despachante usa (padrao: ``VAULT_TRANSIT_KEY``)."""
        return CifraTransit(self, chave or self._chave_padrao)

    # Chave comum do estado das confirmacoes (KV v2, caminho separado)

    async def ler_chave_estado(self, *, versao: int | None = None) -> bytes:
        """Le a chave do estado das confirmacoes (a atual, ou a ``versao`` pedida). Tem >= 32 bytes."""
        return (await self.ler_chave_estado_versionada(versao=versao)).valor

    async def ler_chave_estado_versionada(self, *, versao: int | None = None) -> ChaveEstado:
        """Como ``ler_chave_estado``, com o numero da versao (para conferir estados de antes do rodizio)."""
        if versao is not None and versao < 1:
            raise ValueError("versao da chave do estado comeca em 1")
        consulta = {"version": str(versao)} if versao is not None else None
        try:
            corpo = await self._pedir("GET", f"{self._kv}/data/{self._estado}", consulta=consulta)
        except PedidoRecusado as erro:
            if erro.status == 404:
                raise ChaveEstadoInvalida(
                    "A chave do estado das confirmacoes nao existe no cofre (ou essa versao foi apagada)."
                ) from erro
            raise
        dados = _secao(corpo, "data")
        texto = _secao(dados, "data").get(CAMPO_CHAVE_ESTADO)
        if not isinstance(texto, str):
            raise ChaveEstadoInvalida("O segredo do estado das confirmacoes nao tem o campo da chave.")
        try:
            valor = base64.b64decode(texto, validate=True)
        except (binascii.Error, ValueError) as erro:
            raise ChaveEstadoInvalida("A chave do estado das confirmacoes nao esta em base64.") from erro
        if len(valor) < TAMANHO_CHAVE_ESTADO:
            raise ChaveEstadoInvalida(f"A chave do estado das confirmacoes tem menos de {TAMANHO_CHAVE_ESTADO} bytes.")
        return ChaveEstado(valor=valor, versao=_inteiro(_secao(dados, "metadata").get("version")))

    async def girar_chave_estado(self) -> int:
        """Grava uma chave nova e aleatoria (32 bytes) como nova versao no KV v2 e devolve a versao.

        As versoes anteriores ficam no cofre (``max_versions``), para conferir estados criados
        antes do rodizio. So o papel das rotinas tem permissao de escrever.
        """
        nova = secrets.token_bytes(TAMANHO_CHAVE_ESTADO)
        corpo = await self._pedir(
            "POST", f"{self._kv}/data/{self._estado}", corpo={"data": {CAMPO_CHAVE_ESTADO: _b64(nova)}}
        )
        versao = _secao(corpo, "data").get("version")
        if not isinstance(versao, int) or isinstance(versao, bool):
            raise ErroCofre("O cofre nao confirmou a versao nova da chave do estado.")
        _log.info("cofre_chave_estado_girada", versao=versao)
        return versao

    # Por dentro

    def _restante(self) -> float:
        return self._vence_em - self._relogio()

    def _margem(self) -> float:
        """Renova quando falta um terco da validade (pelo menos 1 s)."""
        return max(self._validade / 3, 1.0)

    def _exigir_token(self) -> SecretStr:
        if self._token is None:
            raise ErroCofre("Sem token do cofre depois do login.")
        return self._token

    def _credenciais(self) -> tuple[SecretStr, SecretStr]:
        role_id = self._role_id or _ler_arquivo(self._p.VAULT_ROLE_ID_FILE, "VAULT_ROLE_ID_FILE")
        secret_id = self._secret_id or _ler_arquivo(self._p.VAULT_SECRET_ID_FILE, "VAULT_SECRET_ID_FILE")
        return role_id, secret_id

    async def _entrar_sem_trava(self, *, revogar_anterior: bool = True) -> None:
        role_id, secret_id = self._credenciais()
        resposta = await self._bruto(
            "POST",
            f"auth/{self._metodo}/login",
            corpo={"role_id": role_id.get_secret_value(), "secret_id": secret_id.get_secret_value()},
        )
        if resposta.status_code in {400, 403}:
            self._token = None
            raise LoginRecusado(
                "O cofre recusou o login AppRole: role_id ou secret_id errado, vencido ou de outro papel.",
                status=resposta.status_code,
            )
        if not resposta.is_success:
            raise await self._falha(resposta)
        auth = _secao(_json(resposta), "auth")
        token = auth.get("client_token")
        if not isinstance(token, str) or not token:
            raise ErroCofre("O login no cofre nao devolveu token.")
        anterior = self._token
        validade = _inteiro(auth.get("lease_duration"))
        self._token = SecretStr(token)
        self._validade = float(validade)
        self._vence_em = self._relogio() + validade if validade > 0 else math.inf
        self._renovavel = auth.get("renewable") is True
        if anterior is not None and revogar_anterior:
            await self._revogar(anterior)
        papel = _secao(auth, "metadata").get("role_name")
        _log.info("cofre_entrou", papel=papel, validade_s=validade, renovavel=self._renovavel)

    async def _renovar_sem_trava(self) -> None:
        token = self._exigir_token()
        if not self._renovavel:
            await self._entrar_sem_trava()
            return
        resposta = await self._bruto("POST", "auth/token/renew-self", token=token, corpo={})
        if resposta.status_code in {400, 403}:
            # Venceu, foi revogado ou ja nao renova: um login novo resolve.
            await self._entrar_sem_trava(revogar_anterior=False)
            return
        if not resposta.is_success:
            raise await self._falha(resposta)
        validade = _inteiro(_secao(_json(resposta), "auth").get("lease_duration"))
        if validade <= self._margem():
            # Perto da validade maxima do papel: um token novo vale mais que o resto deste.
            await self._entrar_sem_trava()
            return
        self._vence_em = self._relogio() + validade
        _log.info("cofre_renovou", validade_s=validade)

    async def _token_em_dia(self) -> SecretStr:
        token = self._token
        if token is not None and self._restante() > self._margem():
            return token
        async with self._trava:
            if self._token is None:
                await self._entrar_sem_trava()
            elif self._restante() <= self._margem():
                await self._renovar_sem_trava()
            return self._exigir_token()

    async def _trocar_token(self, antigo: SecretStr) -> SecretStr:
        async with self._trava:
            if self._token is None or self._token is antigo:
                await self._entrar_sem_trava(revogar_anterior=False)
            return self._exigir_token()

    async def _token_vale(self, token: SecretStr) -> bool:
        resposta = await self._bruto("GET", "auth/token/lookup-self", token=token)
        return resposta.status_code != 403

    async def _revogar(self, token: SecretStr) -> None:
        with contextlib.suppress(ErroCofre):
            await self._bruto("POST", "auth/token/revoke-self", token=token)

    async def _pedir(
        self,
        metodo: str,
        caminho: str,
        *,
        corpo: Mapping[str, Any] | None = None,
        consulta: Mapping[str, str] | None = None,
        recusa: type[PedidoRecusado] = PedidoRecusado,
    ) -> dict[str, Any]:
        token = await self._token_em_dia()
        resposta = await self._bruto(metodo, caminho, token=token, corpo=corpo, consulta=consulta)
        if resposta.status_code == 403 and not await self._token_vale(token):
            # O token venceu ou foi revogado por fora: entra de novo e tenta uma vez so.
            token = await self._trocar_token(token)
            resposta = await self._bruto(metodo, caminho, token=token, corpo=corpo, consulta=consulta)
        if resposta.is_success:
            return _json(resposta)
        raise await self._falha(resposta, recusa)

    async def _bruto(
        self,
        metodo: str,
        caminho: str,
        *,
        token: SecretStr | None = None,
        corpo: Mapping[str, Any] | None = None,
        consulta: Mapping[str, str] | None = None,
    ) -> httpx2.Response:
        if self._http is None:
            raise RuntimeError("Use o cofre dentro de 'async with Cofre(...)'.")
        cabecalhos = {"X-Vault-Token": token.get_secret_value()} if token is not None else None
        try:
            return await self._http.request(
                metodo,
                caminho,
                json=dict(corpo) if corpo is not None else None,
                params=dict(consulta) if consulta is not None else None,
                headers=cabecalhos,
            )
        except httpx2.TimeoutException as erro:
            _log.warning("cofre_indisponivel", motivo="tempo esgotado")
            raise CofreIndisponivel("O cofre nao respondeu dentro do tempo limite.") from erro
        except (httpx2.TransportError, OSError) as erro:
            # OSError cobre o ssl.SSLError que o httpcore2 deixa passar quando, no TLS 1.3, o cofre recusa
            # o certificado de cliente so depois do aperto de maos (na primeira leitura).
            _log.warning("cofre_indisponivel", motivo=type(erro).__name__)
            raise CofreIndisponivel(f"Nao foi possivel falar com o cofre ({type(erro).__name__}).") from erro

    async def _falha(self, resposta: httpx2.Response, recusa: type[PedidoRecusado] = PedidoRecusado) -> ErroCofre:
        status = resposta.status_code
        texto = _mensagem(resposta)
        detalhe = f": {texto}" if texto else ""
        if status == 503:
            trancado = "sealed" in texto.lower()
            if not trancado:
                with contextlib.suppress(ErroCofre):
                    trancado = not (await self.saude()).destravado
            if trancado:
                _log.warning("cofre_trancado")
                return CofreTrancado("O cofre esta trancado (sealed); a porta de saida fica fechada.")
            return CofreIndisponivel(f"O cofre respondeu HTTP 503{detalhe}.")
        if status == 403:
            return AcessoNegado(f"O cofre negou o acesso a este papel (HTTP 403){detalhe}.", status=status)
        if status == 429 or status >= 500:
            return CofreIndisponivel(f"O cofre respondeu HTTP {status}{detalhe}.")
        return recusa(f"O cofre recusou o pedido (HTTP {status}){detalhe}.", status=status)


class CifraTransit:
    """Cifra pelo Transit com uma chave fixa, no formato que o despachante usa.

    ``contexto`` e obrigatorio: a chave do gateway e ``derived`` e o contexto amarra cada
    texto cifrado ao par (sistema, dono).
    """

    def __init__(self, cofre: Cofre, chave: str) -> None:
        self._cofre = cofre
        self._chave = _nome(chave, "chave")

    async def cifrar(self, texto: bytes, contexto: bytes) -> str:
        return await self._cofre.cifrar(self._chave, texto, contexto=contexto)

    async def decifrar(self, cifrado: str, contexto: bytes) -> bytes:
        return await self._cofre.decifrar(self._chave, cifrado, contexto=contexto)
