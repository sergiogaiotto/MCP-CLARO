"""As tentativas da ronda das portas, pelo endereco publico, como um aplicativo de fora.

Cada rodada faz, em ordem, um pedido por caso:

- ``controle``: cracha de verdade da ronda, ``tools/list`` certo na 2026-07-28. So e conforme quando a
  entrada devolve a vitrine da ronda (um resultado). Qualquer recusa (aplicativo, conta ou cadastro de
  programa da ronda sem valer, limite por minuto, entrada fechada, bloqueio ou pausa geral) quer dizer que
  a vitrine nao foi testada: inconclusivo. Sem controle em dia, a rodada e inconclusiva e nao conta para
  fechar a entrada.
- ``cracha_falso``: copia do cracha de verdade, assinada por chave propria da ronda. Espera 401.
- ``cracha_vencido``: cracha de verdade guardado de rondas anteriores, ja vencido. Espera 401.
- ``outro_condominio``: cracha de verdade do mesmo provedor com audiencia de outro recurso. Espera 401.
- ``outra_pessoa``: cracha de verdade da ronda chamando uma ferramenta que nao esta na vitrine dela
  (de preferencia, uma que esta na vitrine de outra pessoa). Espera a recusa ``fora_da_vitrine``, e so
  ela. Recusa que a entrada da antes de olhar a vitrine nao prova nada (inconclusivo); qualquer outra
  recusa, ou um resultado, e passagem.
- ``versao_antiga_initialize``: ``initialize`` da geracao antiga, com cracha valido. Espera 400 e -32022.
- ``versao_antiga_cabecalho``: pedido com cabecalho ``2025-11-25``, com cracha valido. Espera 400 e -32022.

Desfechos (``Situacao``):

- ``conforme``: a porta recusou como devia;
- ``passou``: passagem indevida (a porta deixou entrar). E isso que conta para fechar a entrada;
- ``inconclusivo``: sem resposta, erro do servidor ou resposta que nao prova nada;
- ``pulado``: o caso nao pode ser feito agora (por exemplo, ainda nao ha cracha vencido guardado).

Nenhum cracha vai aos detalhes; so codigos HTTP e JSON-RPC, o motivo da recusa e o nome da ferramenta.
"""

from __future__ import annotations

import enum
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import httpx2
from mcp.shared.inbound import MCP_METHOD_HEADER, MCP_NAME_HEADER, MCP_PROTOCOL_VERSION_HEADER
from mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    HEADER_MISMATCH,
    INTERNAL_ERROR,
    PROTOCOL_VERSION_META_KEY,
    UNSUPPORTED_PROTOCOL_VERSION,
)

from mcp_claro import __version__
from mcp_claro.regras.bloqueios import ALCANCES, TIPOS
from mcp_claro.ronda.cracha import (
    MARGEM_VENCIDO_SEGUNDOS,
    ChavesProprias,
    FalhaCracha,
    FonteCrachas,
    audiencias,
    forjar_cracha,
    reivindicacoes_sem_conferir,
)
from mcp_claro.versao import PREFIXO_META, VERSAO_MCP

VERSAO_ANTIGA: Final = "2025-11-25"
FERRAMENTA_INVENTADA: Final = "mcp_claro_ronda_ferramenta_alheia"
CHAVE_MOTIVO: Final = PREFIXO_META + "motivo"

MOTIVO_DA_VITRINE: Final = "fora_da_vitrine"
"""A unica recusa que prova que a porta da vitrine segurou a ferramenta de outra pessoa."""

MOTIVOS_DO_CADASTRO_DO_PROGRAMA: Final = frozenset(
    {
        "programa_sem_cadastro",
        "programa_pausado",
        "programa_encerrado",
        "programa_vencido",
        "programa_sem_responsavel",
        "programa_limite_minuto",
    }
)
"""Recusas de ``regras/programas.conferir_pedido``: o cadastro de programa da ronda (ou do vigia) nao vale agora."""

_RECUSAS_DA_PROPRIA_RONDA: Final = frozenset({"sem_cracha", "fase1_desligada"}) | (
    MOTIVOS_DO_CADASTRO_DO_PROGRAMA - {"programa_limite_minuto"}
)
"""O cadastro da ronda nao vale. O limite por minuto fica de fora: passa sozinho e nao diz que o cadastro falhou
(``recusa_antes_da_vitrine`` o pega pelos motivos do cadastro)."""
_PREFIXOS_DA_PROPRIA_RONDA: Final = ("app_", "pessoa_")  # aplicativo ou conta da ronda sem valer (``quem``)

MOTIVOS_ANTES_DA_VITRINE_GERAIS: Final = frozenset(
    {"entrada_fechada"} | {f"{tipo}_{alcance}" for tipo in TIPOS for alcance in ALCANCES if alcance != "sistema"}
)
"""Recusas da entrada fechada e dos bloqueios e pausas que valem para qualquer pedido (``Vigentes.geral``).

Vem antes de olhar a vitrine (ver ``entrada/servidor.py``, ``chamar``). Bloqueio e pausa de um sistema
(``bloqueio_sistema``, ``pausa_sistema``) vem depois da vitrine e nao entram aqui."""


def recusa_da_propria_ronda(motivo: str | None) -> bool:
    """A entrada recusou porque a propria ronda nao pode pedir agora: aplicativo, conta ou cadastro de programa."""
    if motivo is None:
        return False
    return motivo in _RECUSAS_DA_PROPRIA_RONDA or motivo.startswith(_PREFIXOS_DA_PROPRIA_RONDA)


def recusa_antes_da_vitrine(motivo: str | None) -> bool:
    """Recusa que a entrada da antes de procurar a ferramenta na vitrine: nao diz nada sobre a vitrine."""
    return (
        recusa_da_propria_ronda(motivo)
        or motivo in MOTIVOS_DO_CADASTRO_DO_PROGRAMA
        or motivo in MOTIVOS_ANTES_DA_VITRINE_GERAIS
    )


class Situacao(enum.StrEnum):
    CONFORME = "conforme"
    PASSOU = "passou"
    INCONCLUSIVO = "inconclusivo"
    PULADO = "pulado"


class Caso(enum.StrEnum):
    CONTROLE = "controle"
    CRACHA_FALSO = "cracha_falso"
    CRACHA_VENCIDO = "cracha_vencido"
    OUTRO_CONDOMINIO = "outro_condominio"
    OUTRA_PESSOA = "outra_pessoa"
    VERSAO_ANTIGA_INITIALIZE = "versao_antiga_initialize"
    VERSAO_ANTIGA_CABECALHO = "versao_antiga_cabecalho"


NOMES_LEGIVEIS: Final[dict[str, str]] = {
    Caso.CONTROLE: "teste de controle",
    Caso.CRACHA_FALSO: "cracha falso",
    Caso.CRACHA_VENCIDO: "cracha vencido",
    Caso.OUTRO_CONDOMINIO: "cracha de outro condominio",
    Caso.OUTRA_PESSOA: "ferramenta de outra pessoa",
    Caso.VERSAO_ANTIGA_INITIALIZE: "versao antiga (initialize)",
    Caso.VERSAO_ANTIGA_CABECALHO: f"versao antiga (cabecalho {VERSAO_ANTIGA})",
}

ESPERADO: Final[dict[str, str]] = {
    Caso.CONTROLE: "cracha aceito (resposta JSON-RPC)",
    Caso.CRACHA_FALSO: "401",
    Caso.CRACHA_VENCIDO: "401",
    Caso.OUTRO_CONDOMINIO: "401",
    Caso.OUTRA_PESSOA: "recusa fora_da_vitrine",
    Caso.VERSAO_ANTIGA_INITIALIZE: f"400 {UNSUPPORTED_PROTOCOL_VERSION}",
    Caso.VERSAO_ANTIGA_CABECALHO: f"400 {UNSUPPORTED_PROTOCOL_VERSION}",
}


@dataclass(frozen=True, slots=True)
class Tentativa:
    caso: str
    esperado: str
    obtido: str
    situacao: Situacao
    detalhes: dict[str, Any] = field(default_factory=dict)

    @property
    def defendeu(self) -> bool:
        """Nada passou. Pulado e inconclusivo tambem nao sao passagem."""
        return self.situacao is not Situacao.PASSOU

    def para_tabela(self) -> dict[str, Any]:
        return {"situacao": str(self.situacao)} | self.detalhes


@dataclass(frozen=True, slots=True)
class Resposta:
    """O que a ronda guarda de uma resposta: codigo HTTP e o JSON-RPC, se houver."""

    status: int
    corpo: dict[str, Any] | None

    @property
    def jsonrpc(self) -> bool:
        return isinstance(self.corpo, dict) and self.corpo.get("jsonrpc") == "2.0"

    @property
    def resultado(self) -> Any:
        return self.corpo.get("result") if self.jsonrpc and self.corpo else None

    @property
    def erro(self) -> dict[str, Any] | None:
        if not self.jsonrpc or not self.corpo:
            return None
        erro = self.corpo.get("error")
        return erro if isinstance(erro, dict) else None

    @property
    def tem_resultado(self) -> bool:
        return self.jsonrpc and self.corpo is not None and "result" in self.corpo

    @property
    def codigo(self) -> int | None:
        erro = self.erro
        codigo = erro.get("code") if erro else None
        return codigo if isinstance(codigo, int) else None

    @property
    def motivo(self) -> str | None:
        erro = self.erro
        dados = erro.get("data") if erro else None
        motivo = dados.get(CHAVE_MOTIVO) if isinstance(dados, dict) else None
        return motivo[:80] if isinstance(motivo, str) else None

    def descrever(self) -> str:
        if self.tem_resultado:
            return f"{self.status} resultado"
        if self.erro is not None:
            partes = [str(self.status), f"erro {self.codigo}"]
            if self.motivo:
                partes.append(self.motivo)
            return " ".join(partes)
        return str(self.status)

    def detalhes(self) -> dict[str, Any]:
        d: dict[str, Any] = {"http": self.status}
        if self.codigo is not None:
            d["codigo"] = self.codigo
        if self.motivo:
            d["motivo"] = self.motivo
        if self.tem_resultado:
            d["resultado"] = True
        return d


def pedido_2026(metodo: str, params: dict[str, Any] | None = None, id_: int = 1) -> dict[str, Any]:
    p = dict(params or {})
    p["_meta"] = {
        PROTOCOL_VERSION_META_KEY: VERSAO_MCP,
        CLIENT_CAPABILITIES_META_KEY: {},
        CLIENT_INFO_META_KEY: {"name": "mcp-claro-ronda", "version": __version__},
    }
    return {"jsonrpc": "2.0", "id": id_, "method": metodo, "params": p}


def cabecalhos_2026(metodo: str, cracha: str, nome: str | None = None) -> dict[str, str]:
    h = {
        "content-type": "application/json",
        "accept": "application/json",
        MCP_PROTOCOL_VERSION_HEADER: VERSAO_MCP,
        MCP_METHOD_HEADER: metodo,
        "authorization": f"Bearer {cracha}",
    }
    if nome:
        h[MCP_NAME_HEADER] = nome
    return h


async def _enviar(
    http: httpx2.AsyncClient, url: str, corpo: dict[str, Any], cabecalhos: dict[str, str]
) -> Resposta | str:
    """Manda um pedido. Devolve a resposta ou um texto curto dizendo por que nao houve resposta."""
    try:
        r = await http.post(url, json=corpo, headers=cabecalhos)
    except httpx2.HTTPError as erro:
        return f"sem resposta ({type(erro).__name__})"
    try:
        dados = r.json()
    except ValueError:
        dados = None
    return Resposta(r.status_code, dados if isinstance(dados, dict) else None)


def _sem_resposta(caso: Caso, motivo: str) -> Tentativa:
    return Tentativa(caso, ESPERADO[caso], motivo, Situacao.INCONCLUSIVO, {"por_que": motivo})


def _pulado(caso: Caso, por_que: str) -> Tentativa:
    return Tentativa(caso, ESPERADO[caso], f"pulado: {por_que}", Situacao.PULADO, {"por_que": por_que})


def _passou_das_portas(r: Resposta) -> bool:
    """Resposta JSON-RPC de dentro do servidor: o cracha foi aceito e a versao tambem.

    Recusas do servidor (``Recusa``, -32602) chegam com HTTP 400; as das portas, com 401, 403 ou -32022.
    """
    return (
        r.jsonrpc
        and r.status < 500
        and r.status not in (401, 403)
        and r.codigo not in (UNSUPPORTED_PROTOCOL_VERSION, HEADER_MISMATCH)
    )


def classificar_controle(r: Resposta) -> Situacao:
    """Conforme so quando o cracha de verdade passou pelas portas e a entrada devolveu a vitrine (um resultado).

    Qualquer recusa no controle vem antes da vitrine (``entrada/servidor.py``, ``quem`` e ``listar``): a propria
    ronda sem valer, limite por minuto, entrada fechada, bloqueio ou pausa geral, ou um motivo novo. Com ela, o
    caso ``outra_pessoa`` tambem nao testa a vitrine, e a rodada nao pode dizer "conforme": inconclusivo.
    """
    if _passou_das_portas(r) and r.tem_resultado:
        return Situacao.CONFORME
    return Situacao.INCONCLUSIVO


def classificar_cracha(r: Resposta) -> Situacao:
    """Porta do cracha: so 401 e recusa certa. Resposta JSON-RPC ou 403 de escopo provam que o cracha foi aceito."""
    if r.status == 401:
        return Situacao.CONFORME
    if r.status == 403 or (r.status < 500 and r.jsonrpc):
        return Situacao.PASSOU
    return Situacao.INCONCLUSIVO


def classificar_versao(r: Resposta) -> Situacao:
    """Porta de versao: 400 com -32022. Qualquer outra resposta JSON-RPC prova que a versao antiga passou."""
    if r.status == 400 and r.codigo == UNSUPPORTED_PROTOCOL_VERSION:
        return Situacao.CONFORME
    if r.status in (401, 403):
        return Situacao.INCONCLUSIVO  # o cracha valido foi recusado: o controle mostra o porque
    if r.status < 500 and r.jsonrpc:
        return Situacao.PASSOU
    return Situacao.INCONCLUSIVO


def classificar_outra_pessoa(r: Resposta) -> Situacao:
    """Porta da vitrine: so a recusa ``fora_da_vitrine`` e conforme.

    Recusa de antes da vitrine (aplicativo, conta, cadastro de programa, limite por minuto, entrada fechada,
    bloqueio geral) e erro interno do servidor nao provam nada. Qualquer outra recusa (de depois de achar a
    ferramenta, como ``programa_sem_chave_propria`` ou ``argumento_fora_do_esquema``), ou um resultado, e
    passagem: a vitrine deixou a ferramenta de outra pessoa seguir.
    """
    if not _passou_das_portas(r):
        return Situacao.INCONCLUSIVO
    if r.tem_resultado:
        return Situacao.PASSOU
    if r.motivo == MOTIVO_DA_VITRINE:
        return Situacao.CONFORME
    if recusa_antes_da_vitrine(r.motivo):
        return Situacao.INCONCLUSIVO
    if r.motivo is None and r.codigo == INTERNAL_ERROR:
        return Situacao.INCONCLUSIVO
    return Situacao.PASSOU


Classificador = Callable[[Resposta], Situacao]


def _tentativa(caso: Caso, r: Resposta | str, classificar: Classificador, **extra: Any) -> Tentativa:
    if isinstance(r, str):
        return _sem_resposta(caso, r)
    return Tentativa(caso, ESPERADO[caso], r.descrever(), classificar(r), r.detalhes() | extra)


def nomes_da_vitrine(r: Resposta) -> list[str]:
    resultado = r.resultado
    ferramentas = resultado.get("tools") if isinstance(resultado, dict) else None
    if not isinstance(ferramentas, list):
        return []
    return [str(f["name"]) for f in ferramentas if isinstance(f, dict) and isinstance(f.get("name"), str)]


async def fazer_tentativas(
    alvo: str,
    fonte: FonteCrachas,
    http: httpx2.AsyncClient,
    *,
    emissor: str,
    recurso: str,
    ferramenta_alheia: str | None = None,
    chaves: ChavesProprias | None = None,
) -> list[Tentativa]:
    """Uma rodada completa contra ``alvo`` (endereco publico, sem ``/mcp``). Nunca levanta por falha do alvo."""
    url = f"{alvo.rstrip('/')}/mcp"
    tentativas: list[Tentativa] = []
    forja = chaves or ChavesProprias()

    # Controle: o cracha de verdade precisa passar pela porta.
    valido: str | None
    try:
        valido = await fonte.valido()
    except FalhaCracha as erro:
        valido = None
        tentativas.append(_sem_resposta(Caso.CONTROLE, f"sem cracha da ronda: {erro.motivo}"))
    minhas: list[str] = []
    if valido is not None:
        aud = audiencias(reivindicacoes_sem_conferir(valido))
        if recurso not in aud:
            tentativas.append(_sem_resposta(Caso.CONTROLE, "o cracha da ronda nao cita o MCP-CLARO na audiencia"))
        else:
            r = await _enviar(http, url, pedido_2026("tools/list"), cabecalhos_2026("tools/list", valido))
            if isinstance(r, Resposta):
                minhas = nomes_da_vitrine(r)
                tentativas.append(_tentativa(Caso.CONTROLE, r, classificar_controle, vitrine=len(minhas)))
            else:
                tentativas.append(_sem_resposta(Caso.CONTROLE, r))

    # Cracha falso: mesmas reivindicacoes e mesmo kid, assinatura de chave que o provedor nao conhece.
    falso = forjar_cracha(valido, emissor=emissor, recurso=recurso, chaves=forja)
    r = await _enviar(http, url, pedido_2026("tools/list"), cabecalhos_2026("tools/list", falso))
    tentativas.append(_tentativa(Caso.CRACHA_FALSO, r, classificar_cracha))

    # Cracha vencido de verdade.
    vencido = fonte.vencido()
    if vencido is None:
        tentativas.append(_pulado(Caso.CRACHA_VENCIDO, "ainda nao ha cracha vencido guardado"))
    else:
        exp = reivindicacoes_sem_conferir(vencido).get("exp")
        if not isinstance(exp, int | float) or exp + MARGEM_VENCIDO_SEGUNDOS >= time.time():
            tentativas.append(_pulado(Caso.CRACHA_VENCIDO, "o cracha guardado ainda nao venceu com folga"))
        else:
            r = await _enviar(http, url, pedido_2026("tools/list"), cabecalhos_2026("tools/list", vencido))
            tentativas.append(_tentativa(Caso.CRACHA_VENCIDO, r, classificar_cracha))

    # Cracha de outro condominio: precisa ser de outro recurso, senao o caso nao prova nada.
    try:
        outro = await fonte.outro_condominio()
    except FalhaCracha as erro:
        tentativas.append(_sem_resposta(Caso.OUTRO_CONDOMINIO, f"sem cracha de outro condominio: {erro.motivo}"))
    else:
        if outro is None:
            tentativas.append(_pulado(Caso.OUTRO_CONDOMINIO, "sem fonte de cracha de outro condominio configurada"))
        elif recurso in audiencias(reivindicacoes_sem_conferir(outro)):
            tentativas.append(
                _pulado(Caso.OUTRO_CONDOMINIO, "o provedor pos o MCP-CLARO na audiencia do cracha de outro recurso")
            )
        else:
            r = await _enviar(http, url, pedido_2026("tools/list"), cabecalhos_2026("tools/list", outro))
            tentativas.append(_tentativa(Caso.OUTRO_CONDOMINIO, r, classificar_cracha))

    if valido is None:
        for caso in (Caso.OUTRA_PESSOA, Caso.VERSAO_ANTIGA_INITIALIZE, Caso.VERSAO_ANTIGA_CABECALHO):
            tentativas.append(_pulado(caso, "sem cracha da ronda"))
        return tentativas

    # Ferramenta de outra pessoa, chamada pelo nome.
    nome = ferramenta_alheia if ferramenta_alheia and ferramenta_alheia not in minhas else FERRAMENTA_INVENTADA
    r = await _enviar(
        http,
        url,
        pedido_2026("tools/call", {"name": nome, "arguments": {}}),
        cabecalhos_2026("tools/call", valido, nome=nome),
    )
    tentativas.append(_tentativa(Caso.OUTRA_PESSOA, r, classificar_outra_pessoa, ferramenta=nome))

    # Versao antiga: initialize sem cabecalho de versao, como o kit 1.x faz.
    inicio = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": VERSAO_ANTIGA,
            "capabilities": {},
            "clientInfo": {"name": "mcp-claro-ronda", "version": __version__},
        },
    }
    r = await _enviar(
        http,
        url,
        inicio,
        {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "authorization": f"Bearer {valido}",
        },
    )
    tentativas.append(_tentativa(Caso.VERSAO_ANTIGA_INITIALIZE, r, classificar_versao))

    # Versao antiga: pedido comum com o cabecalho 2025-11-25.
    r = await _enviar(
        http,
        url,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            MCP_PROTOCOL_VERSION_HEADER: VERSAO_ANTIGA,
            "authorization": f"Bearer {valido}",
        },
    )
    tentativas.append(_tentativa(Caso.VERSAO_ANTIGA_CABECALHO, r, classificar_versao))
    return tentativas


@dataclass(frozen=True, slots=True)
class Resumo:
    tentativas: Sequence[Tentativa]

    @property
    def controle_ok(self) -> bool:
        return any(t.caso == Caso.CONTROLE and t.situacao is Situacao.CONFORME for t in self.tentativas)

    @property
    def passagens(self) -> list[Tentativa]:
        return [t for t in self.tentativas if t.situacao is Situacao.PASSOU]

    @property
    def falhou(self) -> bool:
        return bool(self.passagens)

    def motivo_legivel(self) -> str:
        return ", ".join(NOMES_LEGIVEIS.get(t.caso, t.caso) for t in self.passagens)
