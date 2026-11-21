"""Contrato do salto despachante -> sala de visitas (desenho 3.1 e 3.8; ajustes, D1).

O despachante manda um pedido JSON a sala; a sala chama o servidor de terceiros com o cliente fixado
(``ligacao.cliente`` com ``terceiro=True``) e devolve uma resposta JSON. Os dois lados usam estes modelos, para que
a sala (G7) e o despachante (G3) andem em paralelo. O contrato e versionado: ``contrato`` = ``VERSAO_CONTRATO`` nos
dois sentidos; versao diferente e corpo fora do contrato.

Classificacao do salto, do lado do despachante (critica B6):

- falha de conexao antes de enviar o pedido a sala: ``nao_enviado`` (nada chegou ao terceiro);
- qualquer outra coisa que nao seja HTTP 200 com corpo valido deste contrato (500, corpo cortado, versao trocada,
  campo a mais, queda ou prazo depois de enviar): ``duvida``, porque a sala pode ter chamado o terceiro;
- 200 com corpo valido: vale a classificacao da sala, que e a do cliente fixado (``resultado``, ``nao_enviado``,
  ``duvida``, ``erro_rpc``, ``recusado_http``), com ``status_http`` e ``pedidos`` do terceiro. Na listagem, a
  recusa pelo status (desvio 3xx, 401, 403) volta como ``recusado_http`` com o ``status_http``, e o despachante
  a recebe em ``FalhaNaSala.status_http`` (um desvio grava ``sala_desvio_recusado``).

A resposta da sala e conferida como dado nao confiavel: so categorias de pedido, nomes de excecao, versoes e
motivos do nosso vocabulario passam; o resultado precisa ser um ``CallToolResult`` ou ``InputRequiredResult``, cada
ferramenta da listagem um ``Tool``, e o tipo combina com o que vem junto (``resultado`` so com resultado,
``erro_rpc`` com erro, ``recusado_http`` com o status da recusa). Os nomes que so o despachante da (``SalaFora``,
``SalaRespostaInvalida`` e os motivos de sondagem deles, ``EXCECOES_DO_DESPACHANTE`` e ``MOTIVOS_DO_DESPACHANTE``)
ficam fora do contrato na resposta da sala: a sala que respondeu nunca esta fora.

Do lado da sala, ``atender`` sempre devolve (status, corpo) do contrato: o que o terceiro mandou e nao cabe no
contrato (status HTTP fora de 100 a 599, codigo fora de 32 bits, ferramentas demais, corpo regravado acima de
``TETO_SALTO``) vira ``duvida`` (ou sondagem sem veredito) com ``RespostaForaDoContrato``, nunca um erro do processo.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Annotated, Any, Final, Literal, Self

import mcp_types as types
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    ValidationError,
    field_serializer,
    model_validator,
)

from mcp_claro.ligacao import cliente
from mcp_claro.ligacao.cliente import Desfecho, Resposta, Sondagem, TipoResultado
from mcp_claro.versao import VERSAO_MCP

VERSAO_CONTRATO: Final = 1

ROTA_CHAMAR = "/interno/visitas/chamar"
ROTA_LISTAR = "/interno/visitas/listar"
ROTA_SONDAR = "/interno/visitas/sondar"

# Nomes de excecao do salto (em ``Resposta.excecao`` e ``Sondagem.excecao``).
SALA_FORA = "SalaFora"  # nao deu para conectar na sala: nada foi enviado
SALA_RESPOSTA_INVALIDA = "SalaRespostaInvalida"  # a sala respondeu fora do contrato: duvida
PEDIDO_FORA_DO_CONTRATO = "PedidoForaDoContrato"  # a sala recusou o pedido sem chamar o terceiro
RESPOSTA_FORA_DO_CONTRATO = "RespostaForaDoContrato"  # a resposta do terceiro nao cabe no contrato: duvida

MOTIVO_SALA_FORA = "sala de visitas fora do ar; nada foi enviado"
MOTIVO_SALA_INVALIDA = "a sala de visitas nao deu uma resposta valida"
MOTIVO_PEDIDO_FORA = "pedido fora do contrato da sala de visitas; nada foi enviado"
MOTIVO_RESPOSTA_FORA = "a resposta do servidor de terceiros nao cabe no contrato da sala de visitas"

EXCECOES_DO_DESPACHANTE: Final = frozenset({SALA_FORA, SALA_RESPOSTA_INVALIDA})
"""Nomes de excecao que so o lado do despachante da (``ClienteSala``, ``SemSala`` e os ``ler_resposta_*``): a
conexao com a sala nao se fez, ou a sala respondeu fora do contrato. Uma sala que respondeu nunca esta fora, e
quem responde nao julga a propria resposta. Na resposta da sala (de um filho que o terceiro pode ter tomado), esses
nomes ficam fora do contrato e viram a reserva (``duvida`` com ``SalaRespostaInvalida``): senao, um filho tomado
que dissesse ``SalaFora`` na listagem escaparia da regra das rodadas sem resposta (critica A5), e na leitura e na
acao viraria "nada foi enviado"."""
MOTIVOS_DO_DESPACHANTE: Final = frozenset({MOTIVO_SALA_FORA, MOTIVO_SALA_INVALIDA})
"""Os motivos de sondagem que so o lado do despachante da, pela mesma regra de ``EXCECOES_DO_DESPACHANTE``."""

TETO_ENDERECO = 2048
TETO_DISCOVER = 1024 * 1024
TETO_SALTO = 2 * cliente.TETO_RESPOSTA_PADRAO + 64 * 1024  # corpo da resposta da sala (JSON com escapes)

_EXCECAO = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
_PEDIDO = re.compile(
    "|".join(
        [
            *(
                re.escape(c)
                for c in (
                    cliente.PEDIDO_FORMULARIO,
                    cliente.PEDIDO_LINK,
                    cliente.PEDIDO_AMOSTRAGEM,
                    cliente.PEDIDO_RAIZES,
                    cliente.PEDIDO_SO_ESTADO,
                    cliente.PEDIDO_LINK_CODIGO_ANTIGO,
                )
            ),
            re.escape(cliente.PREFIXO_FIO_ANTIGO)
            + "(?:"
            + "|".join(re.escape(m) for m in sorted({*cliente.METODOS_DO_SISTEMA, cliente.DESCONHECIDO}))
            + ")",
        ]
    )
)
_MOTIVOS_FIXOS = frozenset(
    {*cliente.MOTIVOS_DA_SONDAGEM, MOTIVO_SALA_FORA, MOTIVO_SALA_INVALIDA, MOTIVO_PEDIDO_FORA, MOTIVO_RESPOSTA_FORA}
)
_MOTIVOS_COM_STATUS = tuple(
    re.compile(re.escape(m).replace(re.escape("{status}"), "[1-5][0-9]{2}")) for m in cliente.MOTIVOS_COM_STATUS
)
_RESULTADO: TypeAdapter[types.CallToolResult | types.InputRequiredResult] = TypeAdapter(
    types.CallToolResult | types.InputRequiredResult
)
_FERRAMENTAS: TypeAdapter[list[types.Tool]] = TypeAdapter(list[types.Tool])
TETO_FERRAMENTAS = 5000


def _excecao(v: str | None) -> str | None:
    if v is not None and not _EXCECAO.fullmatch(v):
        raise ValueError("excecao fora do vocabulario")
    if v in EXCECOES_DO_DESPACHANTE:
        raise ValueError("excecao que so o despachante da")
    return v


def _pedidos(v: tuple[str, ...]) -> tuple[str, ...]:
    if any(not _PEDIDO.fullmatch(p) for p in v) or len(set(v)) != len(v):
        raise ValueError("pedido fora do vocabulario")
    return tuple(sorted(v))


def _versoes(v: tuple[str, ...]) -> tuple[str, ...]:
    if cliente.versoes_anunciadas(list(v)) != v:
        raise ValueError("versoes fora do formato")
    return v


def _motivo(v: str) -> str:
    if v not in _MOTIVOS_FIXOS and not any(m.fullmatch(v) for m in _MOTIVOS_COM_STATUS):
        raise ValueError("motivo fora do vocabulario")
    if v in MOTIVOS_DO_DESPACHANTE:
        raise ValueError("motivo que so o despachante da")
    return v


Excecao = Annotated[str | None, AfterValidator(_excecao)]
Pedidos = Annotated[tuple[str, ...], Field(max_length=32), AfterValidator(_pedidos)]
StatusHttp = Annotated[tuple[Annotated[int, Field(ge=100, le=599)], ...], Field(max_length=64)]
Endereco = Annotated[str, Field(min_length=1, max_length=TETO_ENDERECO)]
Discover = Annotated[str | None, Field(max_length=TETO_DISCOVER)]
Prazo = Annotated[float, Field(gt=0, le=600)]


class _Modelo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contrato: Literal[1] = VERSAO_CONTRATO

    def para_json(self) -> bytes:
        return self.model_dump_json(by_alias=True).encode()


class _ComChave(_Modelo):
    endereco: Endereco
    chave: SecretStr | None = None

    @field_serializer("chave", when_used="json")
    def _chave_no_fio(self, chave: SecretStr | None) -> str | None:
        # A chave vai aberta so no corpo do salto (mTLS na rede da sala); fora do JSON, o repr a esconde.
        return chave.get_secret_value() if chave is not None else None

    async def segredo(self) -> str | None:
        return self.chave.get_secret_value() if self.chave is not None else None


class PedidoChamar(_ComChave):
    discover_json: Discover = None
    nome: Annotated[str, Field(min_length=1, max_length=128)]
    argumentos: dict[str, Any]
    prazo: Prazo = 30.0
    esquema_declarado: dict[str, Any] | None = None
    teto_resposta: Annotated[int, Field(gt=0, le=cliente.TETO_RESPOSTA_PADRAO)] = cliente.TETO_RESPOSTA_PADRAO


class PedidoListar(_ComChave):
    discover_json: Discover = None


class PedidoSondar(_ComChave):
    prazo: Prazo = 10.0


class RespostaChamar(_Modelo):
    tipo: TipoResultado
    resultado: dict[str, Any] | None = None
    erro: dict[str, Any] | None = None
    status_http: StatusHttp = ()
    excecao: Excecao = None
    pedidos: Pedidos = ()

    @model_validator(mode="after")
    def _tipo_combina(self) -> Self:
        # So "resultado" leva resultado; "erro_rpc" leva o erro; "recusado_http" leva o status da recusa.
        if (self.tipo == "resultado") != (self.resultado is not None):
            raise ValueError("resultado so com o tipo resultado")
        if self.tipo == "erro_rpc" and self.erro is None:
            raise ValueError("erro_rpc sem o erro")
        if self.tipo == "recusado_http" and not (
            self.status_http and cliente.recusado_por_status(self.status_http[-1])
        ):
            raise ValueError("recusado_http sem o status da recusa")
        return self

    @classmethod
    def de_resposta(cls, r: Resposta) -> RespostaChamar:
        return cls(
            tipo=r.tipo,
            resultado=r.resultado.model_dump(by_alias=True, exclude_none=True, mode="json") if r.resultado else None,
            erro=r.erro.model_dump(by_alias=True, exclude_none=True, mode="json") if r.erro else None,
            status_http=r.status_http,
            excecao=r.excecao,
            pedidos=r.pedidos,
        )

    def para_resposta(self) -> Resposta:
        """Levanta ``ValueError`` (``ValidationError``) se o resultado ou o erro nao seguem a 2026-07-28."""
        return Resposta(
            tipo=self.tipo,
            resultado=_RESULTADO.validate_python(self.resultado) if self.resultado is not None else None,
            erro=types.ErrorData.model_validate(self.erro) if self.erro is not None else None,
            status_http=self.status_http,
            excecao=self.excecao,
            pedidos=self.pedidos,
        )


class RespostaListar(_Modelo):
    tipo: Literal["resultado", "nao_enviado", "duvida", "recusado_http"]
    ferramentas: Annotated[list[dict[str, Any]], Field(max_length=TETO_FERRAMENTAS)] = Field(default_factory=list)
    excecao: Excecao = None
    status_http: StatusHttp = ()

    @model_validator(mode="after")
    def _tipo_combina(self) -> Self:
        # So "recusado_http" leva status, e sempre com o da recusa (3xx, 401 ou 403) no fim.
        if (self.tipo == "recusado_http") != bool(self.status_http):
            raise ValueError("status_http so com o tipo recusado_http")
        if self.status_http and not cliente.recusado_por_status(self.status_http[-1]):
            raise ValueError("recusado_http sem o status da recusa")
        return self


class RespostaSondar(_Modelo):
    desfecho: Desfecho
    motivo: Annotated[str, AfterValidator(_motivo)]
    versoes: Annotated[tuple[str, ...], Field(max_length=cliente.MAX_VERSOES), AfterValidator(_versoes)] = ()
    discover_json: Discover = None
    codigo: Annotated[int | None, Field(ge=-(2**31), le=2**31 - 1)] = None
    status_http: StatusHttp = ()
    excecao: Excecao = None
    pedidos: Pedidos = ()

    @classmethod
    def de_sondagem(cls, s: Sondagem) -> RespostaSondar:
        return cls(
            desfecho=s.desfecho,
            motivo=s.motivo,
            versoes=s.versoes,
            discover_json=s.discover_json,
            codigo=s.codigo,
            status_http=s.status_http,
            excecao=s.excecao,
            pedidos=s.pedidos,
        )

    def para_sondagem(self) -> Sondagem:
        """O ``discover_json`` volta conferido, sem ``instructions`` nem ``_meta`` (texto do fornecedor)."""
        guardado = None
        if self.discover_json is not None:
            if self.desfecho != "aceito":
                raise ValueError("discover so com desfecho aceito")
            discover = types.DiscoverResult.model_validate_json(self.discover_json)
            if VERSAO_MCP not in discover.supported_versions:
                raise ValueError("discover sem a 2026-07-28")
            guardado = cliente.discover_para_guardar(discover, terceiro=True)
        elif self.desfecho == "aceito":
            raise ValueError("aceito sem discover")
        return Sondagem(
            desfecho=self.desfecho,
            motivo=self.motivo,
            versoes=self.versoes,
            discover_json=guardado,
            codigo=self.codigo,
            status_http=self.status_http,
            excecao=self.excecao,
            pedidos=self.pedidos,
        )


class FalhaNaSala(RuntimeError):
    """A listagem pela sala nao trouxe as declaracoes (``tipo`` diz se algo chegou ao terceiro; ``status_http``,
    na recusa pelo status, o de cada POST, com o da recusa no fim)."""

    def __init__(self, tipo: str, excecao: str | None, *, status_http: tuple[int, ...] = ()) -> None:
        super().__init__(f"listagem pela sala de visitas: {tipo} ({excecao or 'sem detalhe'})")
        self.tipo = tipo
        self.excecao = excecao
        self.status_http = status_http


# Lado do despachante -------------------------------------------------------------------------------------------


def resposta_sala_fora() -> Resposta:
    """Conexao com a sala recusada antes de enviar: o terceiro nao recebeu nada."""
    return Resposta("nao_enviado", excecao=SALA_FORA)


def sondagem_sala_fora() -> Sondagem:
    return Sondagem("inalcancavel", MOTIVO_SALA_FORA, excecao=SALA_FORA)


def ler_resposta_chamar(status: int, corpo: bytes) -> Resposta:
    """Resposta da sala a ``/chamar``. Tudo o que nao e 200 com corpo do contrato e ``duvida``."""
    if status != 200:
        return Resposta("duvida", excecao=SALA_RESPOSTA_INVALIDA)
    try:
        return RespostaChamar.model_validate_json(corpo).para_resposta()
    except ValidationError, ValueError:
        return Resposta("duvida", excecao=SALA_RESPOSTA_INVALIDA)


def ler_resposta_sondar(status: int, corpo: bytes) -> Sondagem:
    """Resposta da sala a ``/sondar``. Fora do contrato: sem veredito (``inalcancavel``), nunca versao."""
    if status != 200:
        return Sondagem("inalcancavel", MOTIVO_SALA_INVALIDA, excecao=SALA_RESPOSTA_INVALIDA)
    try:
        return RespostaSondar.model_validate_json(corpo).para_sondagem()
    except ValidationError, ValueError:
        return Sondagem("inalcancavel", MOTIVO_SALA_INVALIDA, excecao=SALA_RESPOSTA_INVALIDA)


def ler_resposta_listar(status: int, corpo: bytes) -> list[dict[str, Any]]:
    """Resposta da sala a ``/listar``. Levanta ``FalhaNaSala`` se nao veio a lista.

    Cada ferramenta e conferida como ``Tool`` e volta no mesmo formato do cliente local (``model_dump`` por alias,
    sem nulos): a sala nao entrega ao cadastro nada que o caminho local nao entregaria.
    """
    if status != 200:
        raise FalhaNaSala("duvida", SALA_RESPOSTA_INVALIDA)
    try:
        resposta = RespostaListar.model_validate_json(corpo)
        if resposta.tipo != "resultado":
            raise FalhaNaSala(resposta.tipo, resposta.excecao, status_http=resposta.status_http)
        ferramentas = _FERRAMENTAS.validate_python(resposta.ferramentas)
    except ValidationError, ValueError:
        raise FalhaNaSala("duvida", SALA_RESPOSTA_INVALIDA) from None
    return [t.model_dump(by_alias=True, exclude_none=True, mode="json") for t in ferramentas]


# Lado da sala ---------------------------------------------------------------------------------------------------


def _no_contrato(montar: Callable[[], _Modelo], reserva: _Modelo) -> bytes:
    """O corpo da resposta da sala; se o que o terceiro mandou nao cabe no contrato, a ``reserva`` (sempre valida).

    Nao caber inclui passar de ``TETO_SALTO`` depois de regravado: o JSON regravado pode crescer (um ``1e15`` do
    terceiro volta como ``1000000000000000.0``), e o despachante nao le alem do teto. A reserva chega explicada.
    """
    try:
        corpo = montar().para_json()
    except ValueError:  # ValidationError e erro de serializacao
        return reserva.para_json()
    if len(corpo) > TETO_SALTO:
        return reserva.para_json()
    return corpo


async def atender(rota: str, corpo: bytes, *, ambiente: str, liberar_locais: bool = False) -> tuple[int, bytes]:
    """O que a sala faz com um pedido: chama o terceiro com ``terceiro=True`` e devolve (status, corpo).

    Pensado para rodar no processo filho de cada chamada (ajustes, A1). Pedido fora do contrato nao chama o
    terceiro e volta 200 com ``nao_enviado``; rota desconhecida volta 404. O corpo devolvido sempre segue o
    contrato e cabe em ``TETO_SALTO``: resposta do terceiro que nao cabe volta como ``duvida`` (sondagem:
    ``inalcancavel``, sem veredito) com ``RespostaForaDoContrato``.
    """
    regras: dict[str, Any] = {"terceiro": True, "ambiente": ambiente, "liberar_locais": liberar_locais}
    if rota == ROTA_CHAMAR:
        try:
            pc = PedidoChamar.model_validate_json(corpo)
        except ValidationError, ValueError:
            return 200, RespostaChamar(tipo="nao_enviado", excecao=PEDIDO_FORA_DO_CONTRATO).para_json()
        r = await cliente.chamar_ferramenta(
            pc.endereco,
            pc.segredo,
            pc.discover_json,
            pc.nome,
            pc.argumentos,
            prazo=pc.prazo,
            esquema_declarado=pc.esquema_declarado,
            teto_resposta=pc.teto_resposta,
            **regras,
        )
        reserva_chamar = RespostaChamar(tipo="duvida", excecao=RESPOSTA_FORA_DO_CONTRATO)
        return 200, _no_contrato(lambda: RespostaChamar.de_resposta(r), reserva_chamar)
    if rota == ROTA_LISTAR:
        try:
            pl = PedidoListar.model_validate_json(corpo)
        except ValidationError, ValueError:
            return 200, RespostaListar(tipo="nao_enviado", excecao=PEDIDO_FORA_DO_CONTRATO).para_json()
        reserva_listar = RespostaListar(tipo="duvida", excecao=RESPOSTA_FORA_DO_CONTRATO)
        try:
            ferramentas = await cliente.listar_ferramentas(pl.endereco, pl.segredo, pl.discover_json, **regras)
        except cliente.EnderecoRecusado:
            return 200, RespostaListar(tipo="nao_enviado", excecao=cliente.ENDERECO_RECUSADO).para_json()
        except cliente.ListagemRecusada as erro:
            status = erro.status_http  # do terceiro: o modelo confere (fora de 100 a 599, a reserva)
            return 200, _no_contrato(
                lambda: RespostaListar(tipo="recusado_http", excecao=cliente.LISTAGEM_RECUSADA, status_http=status),
                reserva_listar,
            )
        except OSError:
            return 200, RespostaListar(tipo="nao_enviado", excecao=cliente.NOME_NAO_RESOLVIDO).para_json()
        except Exception as erro:  # noqa: BLE001 - a listagem que falha volta como classificacao, nunca como 500
            nome = type(erro).__name__
            return 200, _no_contrato(lambda: RespostaListar(tipo="duvida", excecao=nome), reserva_listar)
        return 200, _no_contrato(lambda: RespostaListar(tipo="resultado", ferramentas=ferramentas), reserva_listar)
    if rota == ROTA_SONDAR:
        try:
            ps = PedidoSondar.model_validate_json(corpo)
        except ValidationError, ValueError:
            return 200, RespostaSondar(
                desfecho="inalcancavel", motivo=MOTIVO_PEDIDO_FORA, excecao=PEDIDO_FORA_DO_CONTRATO
            ).para_json()
        s = await cliente.sondar(ps.endereco, ps.segredo, prazo=ps.prazo, **regras)
        reserva_sondar = RespostaSondar(
            desfecho="inalcancavel", motivo=MOTIVO_RESPOSTA_FORA, excecao=RESPOSTA_FORA_DO_CONTRATO
        )
        return 200, _no_contrato(lambda: RespostaSondar.de_sondagem(s), reserva_sondar)
    return 404, b'{"erro":"rota desconhecida"}'
