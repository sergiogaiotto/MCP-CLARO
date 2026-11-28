"""O alvo fechado da bateria do selo (desenho 3.2, regras 1 e 2; criticas A3.1 e A4).

Toda chamada da bateria (``selo.casos``) passa por ``AlvoDoTeste``. Ele conhece os dois enderecos, a lista exata do
que pode chamar em cada um e os numeros que a propria bateria criou. Qualquer outra coisa levanta ``ForaDoAlvo``
ANTES de abrir conexao: nada sai. As regras:

- o endereco do alvo de teste (modo ``ambiente_de_teste``) e conferido na criacao, contra a producao de todo sistema
  do cadastro, na forma normalizada e na forma de conexao (``selo.declaracao.alvo_na_producao``; critica A4.1). As
  barreiras do banco (na declaracao, no pedido, em ``rodando`` e no teste) continuam valendo: esta e mais uma, antes
  da rede;
- na producao (lado ``producao``, com a chave de leitura das declaracoes; critica A4.4): so ``tools/list``,
  ``selo_alvo`` (a producao se declara de producao, C-0.4) e a consulta de controle do C-0.3, com um numero novo que a
  bateria criou e nunca mandou numa escrita. Nada de ``selo_bloqueio`` nem de acao na producao;
- no alvo (lado ``alvo``, com a chave de teste ``teste_selo``; no modo ``ferramentas_de_teste`` o endereco e o de
  producao): ``tools/list``, as ferramentas do contrato do nivel (com numeros da bateria), as leituras declaradas
  (com os argumentos declarados) e as acoes declaradas. No modo ``ambiente_de_teste``, as acoes sao as da declaracao;
  no ``ferramentas_de_teste``, so as ferramentas de teste declaradas, nunca a acao real;
- nenhuma leitura e uma acao (critica A4.2): uma leitura declarada (rastro, versao do conflito, conteudo) com o nome
  de uma acao da declaracao ou, com o ``cadastro``, de uma acao do sistema, e recusada na criacao (``acao_que_le``),
  nos dois modos; no ``ferramentas_de_teste`` (o alvo e a producao), nenhum nome de acao real e chamado
  (``acao_real``), a ferramenta de teste nao tem o nome de nenhuma revisada do sistema e, com o ``cadastro``, cada
  leitura e uma revisada ativa de classe ``le`` (A4.3). A declaracao ja confere tudo isso contra o cadastro
  (``selo.declaracao.conferir``): aqui e mais uma barreira, antes da rede, para um modelo que chegue sem ela;
- so argumentos declarados (regra 2): cada acao vai com um dos conjuntos de argumentos que a declaracao traz para
  ela (no conflito, com o argumento da versao posto pela bateria), e cada leitura com os argumentos declarados. Os
  numeros sao todos criados pela bateria (``numero_novo``);
- a marca positiva (critica A4.1, SELO-0.3): nenhuma escrita antes de ``conferir_marcas`` ver a producao dizer
  ``producao`` e, no modo ``ambiente_de_teste``, o alvo dizer ``teste``. No modo ``ferramentas_de_teste``, cada
  resposta de ferramenta de teste traz ``_meta[contrato.CHAVE_ALVO] = "teste"``; na primeira que nao trouxer, a
  bateria para (``SemMarca``) e nenhuma escrita sai depois;
- o teto de chamadas (``BADGE_TEST_MAX_CALLS``): toda conexao aberta conta (listagem, chamada e o corte). Passou dele,
  ``TetoDeChamadas`` antes de abrir conexao.

O corte do C-2.6a (critica A3.1) e codigo de produto: ``mandar_e_cortar`` abre a conexao, manda o ``tools/call``
inteiro, com o numero no ``_meta``, e fecha logo depois, sem ler nada da resposta. So para sistema proprio: a bateria
de um servidor de terceiros fica para o G7 (``selo.declaracao.recusar_terceiro``).

Nada daqui vai ao extrato nem ao log alem do tipo da falha: nem argumentos, nem o que o sistema respondeu.
"""

from __future__ import annotations

import json
import ssl
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from typing import Any, Final, Literal, Protocol

import anyio
import httpx2
import mcp_types as types

from mcp_claro import __version__
from mcp_claro.ligacao.cliente import Resposta
from mcp_claro.ligacao.ponto import Ligacao
from mcp_claro.logs import logger
from mcp_claro.selo import contrato, respostas
from mcp_claro.selo import declaracao as declaracao_do_selo
from mcp_claro.selo.declaracao import Cadastro, Declaracao, FerramentaDeclarada
from mcp_claro.versao import VERSAO_MCP

log = logger("mcp_claro.selo.alvo")

Lado = Literal["alvo", "producao"]
LADOS: Final[tuple[Lado, ...]] = ("alvo", "producao")

TETO_DO_CONTRATO: Final = 4 * contrato.TETO_RESPOSTA_BYTES
"""Teto, em bytes, do corpo HTTP da resposta de uma ferramenta do contrato: a resposta (``structuredContent``, ate
``contrato.TETO_RESPOSTA_BYTES``, conferida em ``selo.respostas``) e a copia em texto. Uma resposta maior que o teto do
contrato chega e e dada como invalida (``grande_demais``), nao como "sem resposta"."""

TETO_DA_BATERIA: Final = 1024 * 1024
"""Teto, em bytes, da resposta de uma acao ou de uma leitura do rastro na bateria."""

PRAZO_DO_CORTE: Final = 5.0
"""Prazo para abrir a conexao e mandar o pedido do corte (C-2.6a)."""


class ForaDoAlvo(Exception):
    """A chamada nao esta na lista fechada do alvo: nada saiu, nenhuma conexao foi aberta. ``motivo`` e um codigo
    curto (para o log e os testes), nunca texto do sistema nem argumento."""

    def __init__(self, motivo: str) -> None:
        super().__init__(motivo)
        self.motivo = motivo


class SemMarca(ForaDoAlvo):
    """O alvo nao se declarou de teste (critica A4.1): sem a marca positiva, a bateria nao escreve. No modo de
    ferramentas de teste, levantada logo depois da primeira resposta sem a marca: a bateria para ali."""


class TetoDeChamadas(Exception):
    """A bateria chegou ao teto de chamadas da rodada: nada mais sai."""


class Fluxo(Protocol):
    """O que o corte usa da conexao aberta (``anyio.abc.ByteStream``)."""

    async def send(self, item: bytes) -> None: ...
    async def aclose(self) -> None: ...


Conectar = Callable[[str, int, ssl.SSLContext | None, str], Awaitable[Fluxo]]
"""Abre a conexao do corte: (nome, porta, contexto TLS ou None, nome para o TLS). Os testes trocam para provar que nada
fora da lista abre conexao."""


async def conectar_tcp(nome: str, porta: int, tls: ssl.SSLContext | None, nome_tls: str) -> Fluxo:
    """A conexao do corte: TCP e, no https, TLS com a verificacao do sistema operacional."""
    if tls is None:
        return await anyio.connect_tcp(nome, porta)
    return await anyio.connect_tcp(nome, porta, ssl_context=tls, tls_hostname=nome_tls, tls_standard_compatible=False)


def _canonico(valor: Any) -> str:
    return json.dumps(valor, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _argumentos_da_acao(entrada: FerramentaDeclarada) -> tuple[frozenset[str], tuple[tuple[str, str], ...]]:
    """Os conjuntos de argumentos que a declaracao traz para a acao (cada um em forma canonica) e, do conflito, os
    pares (argumento da versao, argumentos sem ele) que a bateria completa com a versao lida."""
    conjuntos = [entrada.argumentos, entrada.argumentos_outros, entrada.argumentos_recusados]
    for opcional in (entrada.efeito_adiado, entrada.escrita_lenta, entrada.varios_itens, entrada.pede_dados):
        if opcional is not None:
            conjuntos.append(opcional.argumentos)
    conflito: list[tuple[str, str]] = []
    if entrada.conflito is not None:
        c = entrada.conflito
        conflito = [
            (c.argumento_versao, _canonico(c.argumentos)),
            (c.argumento_versao, _canonico(c.argumentos_concorrente)),
        ]
    return frozenset(_canonico(a) for a in conjuntos), tuple(conflito)


def _leituras(entrada: FerramentaDeclarada) -> list[tuple[str, str]]:
    """(ferramenta, argumentos canonicos) de cada leitura declarada para a acao (rastro, conflito, conteudo)."""
    pares = [(entrada.rastro.ferramenta, _canonico(entrada.rastro.argumentos))]
    if entrada.conflito is not None:
        pares.append((entrada.conflito.leitura.ferramenta, _canonico(entrada.conflito.leitura.argumentos)))
    if entrada.idempotente is not None and entrada.idempotente.rastro is not None:
        rastro = entrada.idempotente.rastro
        pares.append((rastro.ferramenta, _canonico(rastro.argumentos)))
    return pares


class AlvoDoTeste:
    """O alvo fechado da bateria (veja o cabecalho do modulo).

    ``producao``: o endereco de producao do sistema testado. ``producoes``: a producao de todo sistema do cadastro
    (``selo.declaracao.Cadastro.producoes``). ``segredo_teste``: a chave ``teste_selo`` (lado ``alvo``).
    ``segredo_producao``: a chave de leitura das declaracoes (lado ``producao``; None sem ela). ``prazo``: o de cada
    acao e leitura; ``prazo_do_contrato``: o de cada ferramenta do contrato (``BADGE_LOOKUP_TIMEOUT_SECONDS``).
    ``teto``: ``BADGE_TEST_MAX_CALLS``. ``discover_producao``: a sondagem guardada do sistema, para o lado da
    producao (e o alvo no modo de ferramentas de teste). ``cadastro``: o cadastro de agora
    (``selo.declaracao.cadastro_do_sistema``), para recusar leitura que e acao do sistema e, no modo de ferramentas
    de teste, ferramenta de teste com nome de revisada e leitura que nao e uma revisada ``le`` (sem ele, so os nomes
    da declaracao)."""

    def __init__(
        self,
        declaracao: Declaracao,
        *,
        producao: str,
        producoes: Iterable[str],
        nivel: int,
        ligacao: Ligacao,
        segredo_teste: str,
        segredo_producao: str | None,
        teto: int,
        prazo: float = 20.0,
        prazo_do_contrato: float = 10.0,
        discover_producao: str | None = None,
        conectar: Conectar = conectar_tcp,
        cadastro: Cadastro | None = None,
    ) -> None:
        if nivel not in contrato.NIVEIS:
            raise ValueError(f"O contrato do selo so tem os niveis 2 e 3, nao {nivel}")
        self.declaracao = declaracao
        self.nivel = nivel
        self.de_teste = declaracao.modo == "ferramentas_de_teste"
        if self.de_teste:
            self.endereco_alvo = producao
        else:
            alvo = declaracao.endereco_teste
            if alvo is None:
                raise ForaDoAlvo("sem_alvo")
            # Critica A4.1: o alvo nunca e a producao de um sistema do cadastro, comparado como o MCP-CLARO conecta.
            if declaracao_do_selo.alvo_na_producao(alvo, [producao, *producoes]):
                raise ForaDoAlvo("alvo_na_producao")
            self.endereco_alvo = alvo
        self.endereco_producao = producao
        self._ligacao = ligacao
        self._segredos: dict[Lado, str | None] = {"alvo": segredo_teste, "producao": segredo_producao}
        self._discover: dict[Lado, str | None] = {
            "alvo": discover_producao if self.de_teste else None,
            "producao": discover_producao,
        }
        self.teto = teto
        self.prazo = prazo
        self.prazo_do_contrato = prazo_do_contrato
        self._conectar = conectar
        self.chamadas = 0
        """Conexoes abertas (listagens, chamadas e o corte): o ``testes_selo.chamadas``."""
        self.escritas = 0
        """Chamadas de acao (escritas reais no alvo), cortadas ou nao."""
        self.marcas: list[bool] = []
        """No modo de ferramentas de teste: se cada resposta de ferramenta de teste trouxe a marca."""
        self.producao_disse: str | None = None
        self.alvo_disse: str | None = None
        self.marcas_conferidas = False
        self._pode_escrever = False
        self._sem_marca = False
        self._numeros: set[str] = set()
        self._usados: set[str] = set()
        self._acoes: dict[str, tuple[frozenset[str], tuple[tuple[str, str], ...]]] = {}
        self._leituras: dict[str, set[str]] = {}
        self._reais = frozenset(declaracao.ferramentas) | (frozenset() if cadastro is None else cadastro.acoes)
        """Os nomes das acoes reais (as da declaracao e, com o cadastro, as do sistema): nunca uma leitura e, no modo
        de ferramentas de teste, nunca chamadas."""
        for nome, entrada in declaracao.ferramentas.items():
            chamado = entrada.ferramenta_de_teste if self.de_teste else nome
            if chamado is None:  # o modelo da declaracao ja exige; aqui so por defesa
                raise ForaDoAlvo("sem_ferramenta_de_teste")
            self._acoes[chamado] = _argumentos_da_acao(entrada)
            for leitura, argumentos in _leituras(entrada):
                self._leituras.setdefault(leitura, set()).add(argumentos)
        # Critica A4.2: nenhuma leitura e uma acao (nem a da declaracao, nem a real), nem uma ferramenta do contrato.
        if (set(self._acoes) | self._reais) & (set(self._leituras) | contrato.NOMES_RESERVADOS):
            raise ForaDoAlvo("acao_que_le")
        if self.de_teste:
            # Critica A4.3: a ferramenta de teste nao e uma acao real nem uma revisada do sistema.
            if set(self._acoes) & (self._reais | (frozenset() if cadastro is None else cadastro.revisadas)):
                raise ForaDoAlvo("ferramenta_de_teste_real")
            # Na producao, a leitura so pode ser uma revisada ativa de classe ``le``.
            if cadastro is not None and not set(self._leituras) <= cadastro.leituras:
                raise ForaDoAlvo("leitura_que_nao_le")

    # --- Numeros e marca ---------------------------------------------------------------------------------------

    def numero_novo(self) -> str:
        """Um numero de operacao novo, criado pela bateria (uuid em minusculas)."""
        numero = str(uuid.uuid4())
        self._numeros.add(numero)
        return numero

    @property
    def pode_escrever(self) -> bool:
        """A marca positiva foi conferida e nenhuma resposta de ferramenta de teste veio sem ela."""
        return self._pode_escrever and not self._sem_marca

    @property
    def parou_sem_marca(self) -> bool:
        return self._sem_marca

    def acao_chamada(self, nome: str) -> str:
        """O nome que a bateria chama para a acao ``nome`` da declaracao: ela mesma, ou a ferramenta de teste."""
        entrada = self.declaracao.ferramentas[nome]
        return (entrada.ferramenta_de_teste or nome) if self.de_teste else nome

    async def conferir_marcas(self) -> tuple[bool | None, bool | None]:
        """C-0.4, antes da primeira escrita: o que ``selo_alvo`` diz na producao e, no modo ambiente, no alvo.
        Devolve (a producao disse ``producao``, o alvo disse ``teste``), com None quando nao respondeu ou respondeu
        fora do contrato. So com os dois verdadeiros (no modo de ferramentas de teste, so a producao) a bateria
        escreve."""
        self.producao_disse = await self._quem_sou(await self.chamar("producao", contrato.FERRAMENTA_ALVO, {}))
        producao_ok = None if self.producao_disse is None else self.producao_disse == "producao"
        alvo_ok: bool | None = True
        if not self.de_teste:
            self.alvo_disse = await self._quem_sou(await self.chamar("alvo", contrato.FERRAMENTA_ALVO, {}))
            alvo_ok = None if self.alvo_disse is None else self.alvo_disse == "teste"
        self.marcas_conferidas = True
        self._pode_escrever = producao_ok is True and alvo_ok is True
        return producao_ok, alvo_ok

    @staticmethod
    async def _quem_sou(r: Resposta) -> str | None:
        if r.tipo != "resultado" or not isinstance(r.resultado, types.CallToolResult) or r.resultado.is_error:
            return None
        dados = r.resultado.structured_content
        if respostas.tamanho_json(dados) is None or not isinstance(dados, dict):
            return None
        if dados.get("contrato") != contrato.CONTRATO or dados.get("alvo") not in contrato.ALVOS:
            return None
        return str(dados["alvo"])

    # --- A lista fechada ---------------------------------------------------------------------------------------

    def _do_contrato(self, lado: Lado, nome: str, argumentos: Mapping[str, Any]) -> bool:
        if nome == contrato.FERRAMENTA_ALVO:
            return dict(argumentos) == {}
        if nome == contrato.FERRAMENTA_CONSULTAR:
            if set(argumentos) != {"operacao"} or argumentos["operacao"] not in self._numeros:
                return False
            # Na producao, so a consulta de controle: um numero que nunca foi numa escrita (critica A4.4).
            return lado == "alvo" or argumentos["operacao"] not in self._usados
        if nome == contrato.FERRAMENTA_BLOQUEIO:
            if lado != "alvo" or self.nivel != 3 or set(argumentos) != {"bloqueio", "operacoes"}:
                return False
            numeros = argumentos["operacoes"]
            return (
                argumentos["bloqueio"] in self._numeros
                and isinstance(numeros, list)
                and 1 <= len(numeros) <= contrato.MAX_OPERACOES_POR_AVISO
                and all(isinstance(n, str) and n in self._numeros for n in numeros)
            )
        return False

    def _argumentos_declarados(self, nome: str, argumentos: Mapping[str, Any]) -> bool:
        try:
            texto = _canonico(dict(argumentos))
        except ValueError, TypeError:
            return False
        if nome in self._leituras:
            return texto in self._leituras[nome]
        conjuntos, conflito = self._acoes[nome]
        if texto in conjuntos:
            return True
        for versao, sem_versao in conflito:
            if versao in argumentos:
                resto = {k: v for k, v in argumentos.items() if k != versao}
                if _canonico(resto) == sem_versao:
                    return True
        return False

    def conferir(self, lado: Lado, nome: str, argumentos: Mapping[str, Any], numero: str | None) -> bool:
        """Confere a chamada contra a lista fechada, sem rede, e devolve se e uma escrita. Levanta ``ForaDoAlvo``
        (``SemMarca`` numa escrita sem a marca positiva) e ``TetoDeChamadas``."""
        if lado not in LADOS:
            raise ForaDoAlvo("lado_fora")
        if not isinstance(nome, str) or not isinstance(argumentos, Mapping):
            raise ForaDoAlvo("forma")
        escrita = False
        if nome in contrato.NOMES_RESERVADOS:
            if numero is not None or not self._do_contrato(lado, nome, argumentos):
                raise ForaDoAlvo("contrato_fora")
        elif self.de_teste and nome in self._reais:
            raise ForaDoAlvo("acao_real")  # no modo de ferramentas de teste, o alvo e a producao
        elif lado == "producao":
            raise ForaDoAlvo("producao_so_le_o_contrato")
        elif nome in self._leituras:
            if numero is not None or not self._argumentos_declarados(nome, argumentos):
                raise ForaDoAlvo("leitura_fora")
        elif nome in self._acoes:
            if numero is None or numero not in self._numeros or not self._argumentos_declarados(nome, argumentos):
                raise ForaDoAlvo("acao_fora")
            if not self.pode_escrever:
                raise SemMarca("sem_marca")
            escrita = True
        else:
            raise ForaDoAlvo("nome_fora")
        if self.chamadas >= self.teto:
            raise TetoDeChamadas
        return escrita

    def _endereco(self, lado: Lado) -> str:
        return self.endereco_alvo if lado == "alvo" else self.endereco_producao

    def _segredo(self, lado: Lado) -> Callable[[], Awaitable[str | None]]:
        segredo = self._segredos[lado]

        async def obter() -> str | None:
            return segredo

        return obter

    # --- Chamadas ----------------------------------------------------------------------------------------------

    async def listar(self, lado: Lado) -> list[dict[str, Any]] | None:
        """``tools/list`` no lado, ou None sem resposta. Conta como uma chamada."""
        if lado not in LADOS:
            raise ForaDoAlvo("lado_fora")
        if self.chamadas >= self.teto:
            raise TetoDeChamadas
        self.chamadas += 1
        try:
            return await self._ligacao.listar_ferramentas(
                self._endereco(lado), self._segredo(lado), self._discover[lado]
            )
        except Exception as erro:  # noqa: BLE001 - a listagem que falha e "sem resposta"; nada e repetido
            log.warning("bateria_listagem_falhou", lado=lado, erro=type(erro).__name__)
            return None

    async def chamar(
        self,
        lado: Lado,
        nome: str,
        argumentos: Mapping[str, Any],
        *,
        numero: str | None = None,
        prazo: float | None = None,
    ) -> Resposta:
        """Um ``tools/call`` (exatamente um POST) no lado, depois da lista fechada (``conferir``). ``prazo`` troca o da
        acao (a escrita lenta do C-2.6b). No modo de ferramentas de teste, a resposta de uma ferramenta de teste sem a
        marca levanta ``SemMarca`` (a bateria para ali)."""
        escrita = self.conferir(lado, nome, argumentos, numero)
        do_contrato = nome in contrato.NOMES_RESERVADOS
        self.chamadas += 1
        if escrita:
            self.escritas += 1
            self._usados.add(str(numero))
        try:
            resposta = await self._ligacao.chamar_ferramenta(
                self._endereco(lado),
                self._segredo(lado),
                self._discover[lado],
                nome,
                dict(argumentos),
                traceparent=None,
                prazo=self.prazo_do_contrato if do_contrato else (prazo or self.prazo),
                teto_resposta=TETO_DO_CONTRATO if do_contrato else TETO_DA_BATERIA,
                chave_operacao=numero,
            )
        except Exception as erro:  # noqa: BLE001 - uma chamada nunca vira erro interno; nada e reenviado
            log.warning("bateria_chamada_falhou", lado=lado, erro=type(erro).__name__)
            resposta = Resposta("duvida", excecao=type(erro).__name__)
        if escrita and self.de_teste and isinstance(resposta.resultado, types.CallToolResult):
            marcada = (resposta.resultado.meta or {}).get(contrato.CHAVE_ALVO) == "teste"
            self.marcas.append(marcada)
            if not marcada:
                self._sem_marca = True
                raise SemMarca("marca_faltou")
        return resposta

    def pedido_cortado(self, nome: str, argumentos: Mapping[str, Any], numero: str) -> tuple[bytes, str]:
        """O corpo HTTP do ``tools/call`` do corte, como o cliente fixado o manda (com o envelope da 2026-07-28 e o
        numero no ``_meta``), e o alvo do pedido: o caminho e a consulta como o cliente (``httpx2``) os manda, com o
        que esta fora do ASCII escrito em ``%XX``."""
        meta = {
            "io.modelcontextprotocol/protocolVersion": VERSAO_MCP,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "mcp-claro", "version": __version__},
            contrato.CHAVE_OPERACAO: numero,
        }
        pedido = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": nome, "arguments": dict(argumentos), "_meta": meta},
        }
        caminho = httpx2.URL(self.endereco_alvo).raw_path.decode("ascii")
        return json.dumps(pedido, ensure_ascii=False).encode("utf-8"), caminho

    async def mandar_e_cortar(self, nome: str, argumentos: Mapping[str, Any], numero: str) -> bool:
        """C-2.6a (critica A3.1): manda a escrita no alvo e fecha a conexao logo depois de mandar, sem ler nada da
        resposta. Devolve se o pedido saiu inteiro (False: a conexao nao abriu ou caiu antes; a condicao nao foi
        forcada). Passa pela lista fechada como uma escrita."""
        self.conferir("alvo", nome, argumentos, numero)
        # O endereco lido como o cliente fixado o le (``httpx2``): o mesmo nome, porta, caminho e cabecalho Host.
        try:
            como_o_cliente = httpx2.Request("POST", self.endereco_alvo)
        except httpx2.InvalidURL, UnicodeError:
            raise ForaDoAlvo("endereco_fora") from None
        if como_o_cliente.url.scheme not in ("http", "https") or not como_o_cliente.url.raw_host:
            raise ForaDoAlvo("endereco_fora")
        self.chamadas += 1
        self.escritas += 1
        self._usados.add(numero)
        corpo, caminho = self.pedido_cortado(nome, argumentos, numero)
        segredo = self._segredos["alvo"]
        nome_do_alvo = como_o_cliente.url.raw_host.decode("ascii")
        cabecalhos = {
            "host": como_o_cliente.headers["host"],
            "content-type": "application/json",
            "accept": "application/json, text/event-stream",
            "content-length": str(len(corpo)),
            "mcp-protocol-version": VERSAO_MCP,
            "mcp-method": "tools/call",
            "mcp-name": nome,
            "user-agent": f"mcp-claro/{__version__}",
        }
        if segredo:
            cabecalhos["authorization"] = f"Bearer {segredo}"
        cabeca = f"POST {caminho} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in cabecalhos.items()) + "\r\n"
        porta = como_o_cliente.url.port or (443 if como_o_cliente.url.scheme == "https" else 80)
        tls = ssl.create_default_context() if como_o_cliente.url.scheme == "https" else None
        try:
            with anyio.fail_after(PRAZO_DO_CORTE):
                fluxo = await self._conectar(nome_do_alvo, porta, tls, nome_do_alvo)
                try:
                    await fluxo.send(cabeca.encode("latin-1") + corpo)
                finally:
                    await fluxo.aclose()
        except Exception as erro:  # noqa: BLE001 - o corte que nao saiu so deixa a condicao sem forcar
            log.warning("bateria_corte_falhou", erro=type(erro).__name__)
            return False
        return True


def acoes_e_leituras(declaracao: Declaracao) -> tuple[Sequence[str], Sequence[str]]:
    """As acoes que a bateria chama (as declaradas, ou as ferramentas de teste) e as leituras declaradas, para a tela
    e os testes (a lista fechada do ``AlvoDoTeste``)."""
    de_teste = declaracao.modo == "ferramentas_de_teste"
    acoes = [(e.ferramenta_de_teste or n) if de_teste else n for n, e in declaracao.ferramentas.items()]
    leituras = sorted({nome for e in declaracao.ferramentas.values() for nome, _ in _leituras(e)})
    return acoes, leituras
