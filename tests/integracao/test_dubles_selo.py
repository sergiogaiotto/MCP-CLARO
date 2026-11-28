"""Os dubles do selo contra uma bateria de referencia (desenho 3.3 e 12.1 G1; criticas A3, A4, M1, M2, M4, M6, B4,
B8 e B9). Esta e a referencia do G3:

- o duble de nivel 2 cumpre os casos do nivel 2 e, num teste de nivel 3, falha exatamente
  ``selo_nivel2.FALHA_NO_NIVEL_3``; o de nivel 3 cumpre todos (tambem no modo de ferramentas de teste e com os
  esquemas gerados pelo kit);
- cada modo de ``selo_mentiroso`` falha exatamente os casos de ``selo_mentiroso.CASOS_QUE_FALHAM`` (com as acoes de
  ``selo_mentiroso.ACOES_DO_MODO``), e todo caso de ``contrato.CASOS`` e falhado por pelo menos um modo (ou esta em
  ``selo_mentiroso.CASOS_SEM_MODO``, com a razao); o ``recusa_mas_aplica`` aplica em cada caminho em que o honesto
  recusa (``CAMINHOS_DA_RECUSA``);
- dentro de cada caso, a bateria julga por partes (``selo_mentiroso.PARTES``, todas avaliadas, sem curto-circuito), e
  cada modo de ``selo_mentiroso.PARTES_FORCADAS`` falha o caso so pela parte dita, em toda ferramenta em que falha.

A bateria daqui e pequena e so de caixa-preta, como a do desenho: chama o duble pelo cliente fixado do MCP-CLARO
(``cliente.chamar_ferramenta`` com ``chave_operacao``), le o rastro pela leitura declarada e julga cada caso pelo
contrato (``mcp_claro.selo.contrato``). So o C-2.6a sai por um socket cru, que manda o pedido inteiro e fecha a
conexao logo depois, sem esperar a resposta (critica A3; no produto, ``selo/alvo.py`` do G3). Ela nao e a bateria do
produto (G3, ``selo/bateria.py``): serve para provar que os dubles fazem o que a tabela diz. Por isso pode usar as
rotas de controle do duble (``Controle``): ``/contagem`` para esperar que ele recebeu a chamada antes de consultar
(C-2.6a e C-2.6b; sem isso, sob carga, a consulta pode chegar antes da chamada e mudar o que o caso mede) e para
esperar o fim dos efeitos adiados (C-3.6b), e ``/soltar`` para terminar a escrita lenta depois da consulta de
durante (C-2.6b). As duas ultimas tiram a espera do relogio sem mudar o que o caso mede: o efeito que o aviso de fato
parou termina na hora; o que ele nao parou (``ignora_bloqueio``), e o que o sistema poe mesmo dizendo ``parada``
(``escreve_escondido``), so termina no fim dos segundos declarados. A bateria le o rastro do C-3.6b so depois desse
fim: lido logo depois do aviso, ele ainda nao mostra o efeito do ``escreve_escondido``. Todo endereco de duble sai de
``tests.apoio_selos.endereco_de_duble``.

Em todo modo, a bateria de referencia nunca escreve na producao do duble (critica A4): no modo ambiente, a producao
so ve a consulta de controle e o ``selo_alvo``, com a chave das declaracoes, e nenhuma escrita aplicada; no de
ferramentas de teste, nenhuma acao real (``_producao_sem_acao``).
"""

from __future__ import annotations

import inspect
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, cast
from urllib.parse import urlsplit

import anyio
import httpx2
import mcp_types as types
import pytest
from mcp.shared.exceptions import MCPError
from starlette.applications import Starlette
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from exemplos import selo_mentiroso, selo_nivel2, selo_nivel3
from mcp_claro.ligacao import cliente
from mcp_claro.selo import contrato
from mcp_claro.selo import declaracao as declaracao_do_selo
from tests.apoio import cabecalhos_2026, pedido_2026, servidor
from tests.apoio_selos import caminho_unico, endereco_de_duble

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

CHAVE = "chave-teste-selo"
"""A chave da bateria (``teste_selo``), no alvo de teste."""
CHAVE_DA_PRODUCAO = "chave-declaracoes-selo"
"""Na producao, a bateria usa a chave de leitura das declaracoes (critica A4), nunca a de teste nem a de uma pessoa."""
PARES = 5
"""Pares simultaneos do C-2.2d: menos que ``contrato.PARES_MINIMOS``, entao o caso so informa (``poucos_pares``)."""
DEPOIS_DO_CORTE = 0.2
"""C-2.6a: a primeira consulta, este tanto depois de o duble contar a chamada cortada (``/contagem``). A bateria de
referencia espera a chegada antes (``esperar_a_chegada``): se a consulta chegasse antes da chamada, o honesto
queimaria o numero e recusaria a chamada, e os modos que so aparecem com a chamada ja recebida (``duplica``,
``consulta_diferente``, ``contradiz``, ``esquece_ao_desconectar``) passariam. A bateria do produto nao tem essa rota e
consulta depois de um prazo (``selo/alvo.py``, G3)."""
INTERVALO_DA_CONSULTA = 0.6
"""C-2.6a: a segunda consulta, um intervalo depois da primeira (mais que ``selo_mentiroso.ESPERA_DA_DESCONEXAO``)."""
SEGUNDOS_DECLARADOS = 2
"""O ``segundos`` da declaracao de teste (efeito adiado e escrita lenta): a folga para o aviso do C-3.6b chegar antes
do efeito, e a consulta do C-2.6b durante a escrita, numa maquina carregada."""
PRAZO_DA_CHEGADA = 10.0
"""Quanto a bateria de referencia espera o duble contar uma chamada (C-2.6a e C-2.6b); passou disso, o teste falha."""
ATRASO_DA_CHEGADA = 0.5
"""Na prova de que a bateria espera a chamada chegar: quanto ``ChamadaQueDemora`` segura cada chamada de acao (mais que
``DEPOIS_DO_CORTE`` e que a espera fixa que a bateria tinha no C-2.6b)."""
PRAZO_DO_CONTRATO = 1.0
"""Prazo de cada chamada a uma ferramenta do contrato: o duble esta na mesma maquina, e os mudos so terminam quando
quem chamou desiste."""


# --- O duble no ar ---------------------------------------------------------------------------------------------


@dataclass
class Lado:
    """Um endereco do duble (producao ou alvo de teste), chamado pelo cliente fixado do MCP-CLARO."""

    url: str
    chave: str = CHAVE

    async def _segredo(self) -> str:
        return self.chave

    async def chamar(
        self,
        nome: str,
        argumentos: Mapping[str, Any],
        numero: str | None = None,
        *,
        prazo: float = 5.0,
        teto: int = cliente.TETO_RESPOSTA_PADRAO,
    ) -> cliente.Resposta:
        return await cliente.chamar_ferramenta(
            self.url,
            self._segredo,
            None,
            nome,
            dict(argumentos),
            prazo=prazo,
            teto_resposta=teto,
            chave_operacao=numero,
        )

    async def listar(self) -> list[dict[str, Any]]:
        return (await cliente.listar(self.url, self._segredo, None)).ferramentas

    async def mandar_e_cortar(self, nome: str, argumentos: Mapping[str, Any], numero: str) -> None:
        """Manda o ``tools/call`` inteiro, com o numero no ``_meta``, e fecha a conexao logo depois, sem ler nada da
        resposta (C-2.6a, critica A3)."""
        pedido = pedido_2026("tools/call", {"name": nome, "arguments": dict(argumentos)})
        pedido["params"]["_meta"][contrato.CHAVE_OPERACAO] = numero
        corpo = json.dumps(pedido).encode()
        partes = urlsplit(self.url)
        assert partes.hostname is not None
        assert partes.port is not None
        linhas = [f"POST {partes.path} HTTP/1.1", f"host: {partes.netloc}", f"content-length: {len(corpo)}"]
        linhas += [f"{k}: {v}" for k, v in cabecalhos_2026("tools/call", nome, self.chave).items()]
        conexao = await anyio.connect_tcp(partes.hostname, partes.port)
        try:
            await conexao.send(("\r\n".join(linhas) + "\r\n\r\n").encode() + corpo)
        finally:
            await conexao.aclose()


@dataclass
class Duble:
    base: str
    producao: Lado
    teste: Lado | None
    motor: selo_nivel2.Motor

    async def controle(self, rota: str, corpo: Mapping[str, Any] | None = None) -> Any:
        async with httpx2.AsyncClient() as http:
            if corpo is None:
                return (await http.get(self.base + rota)).json()
            return (await http.post(self.base + rota, json=dict(corpo))).json()

    async def chamadas(self, alvo: str, nome: str) -> int:
        """Quantas chamadas de ``nome`` o duble recebeu em ``alvo`` (``producao`` ou ``teste``)."""
        return int((await self.controle("/contagem"))[alvo]["chamadas"].get(nome, 0))


async def esperar_a_chegada(contar: Callable[[], Awaitable[int]], antes: int) -> None:
    """Espera ``contar()`` passar de ``antes``: o duble recebeu a chamada (o honesto ja guardou o numero)."""
    with anyio.fail_after(PRAZO_DA_CHEGADA):
        while True:  # a contagem so se le pela rota de controle (HTTP): nao ha evento a esperar
            if await contar() > antes:
                return
            await anyio.sleep(0.02)


@dataclass
class Controle:
    """O que a bateria de referencia le e pede pelas rotas de controle do duble, no lado do alvo (``teste`` no modo
    ambiente, ``producao`` no de ferramentas de teste). A bateria do produto nao tem estas rotas: espera pelo relogio
    (``selo/alvo.py``, G3). Aqui elas tiram a espera do relogio sem mudar o que cada caso mede."""

    duble: Duble
    lado: str

    async def chegadas(self, nome: str) -> int:
        """Quantas chamadas de ``nome`` o alvo recebeu."""
        return await self.duble.chamadas(self.lado, nome)

    async def esperar_os_adiados(self, prazo: float) -> None:
        """Espera o alvo terminar os efeitos adiados: o que o aviso parou termina na hora; o que ele nao parou, e o
        que o sistema poe mesmo parado (``escreve_escondido``), no fim dos ``segundos`` declarados (e entao o efeito ja
        aconteceu)."""
        with anyio.fail_after(prazo):
            while True:  # como em esperar_a_chegada: a contagem so se le pela rota de controle
                if not (await self.duble.controle("/contagem"))[self.lado]["adiados"]:
                    return
                await anyio.sleep(0.02)

    async def soltar(self, numero: str) -> None:
        """Termina agora a espera da escrita lenta com ``numero`` (a escrita acontece do mesmo jeito)."""
        await self.duble.controle("/soltar", {"operacao": numero})


class ChamadaQueDemora:
    """Segura cada ``tools/call`` de uma ferramenta de acao ``segundos`` antes de entrega-lo ao duble, como numa
    maquina carregada: a chamada chega ao sistema depois de quem a mandou ja ter seguido em frente."""

    def __init__(self, app: ASGIApp, segundos: float) -> None:
        self.app, self.segundos = app, segundos

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        corpo = b""
        while True:
            mensagem = await receive()
            if mensagem["type"] != "http.request":
                break
            corpo += mensagem.get("body", b"")
            if not mensagem.get("more_body"):
                break
        try:
            pedido = json.loads(corpo)
        except ValueError:
            pedido = None
        nome = (pedido.get("params") or {}).get("name") if isinstance(pedido, dict) else None
        if isinstance(pedido, dict) and pedido.get("method") == "tools/call" and nome in selo_nivel2.ACOES:
            await anyio.sleep(self.segundos)
        entregue = False

        async def receber() -> Message:
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            return await receive()

        await self.app(scope, receber, send)


@asynccontextmanager
async def no_ar(
    fabrica: Callable[..., Starlette],
    *,
    ferramentas_de_teste: bool = False,
    envolver: Callable[[ASGIApp], ASGIApp] | None = None,
    **kw: Any,
) -> AsyncIterator[Duble]:
    producao, teste = caminho_unico("producao"), None if ferramentas_de_teste else caminho_unico("teste")
    app = fabrica(producao=producao, teste=teste, ferramentas_de_teste=ferramentas_de_teste, **kw)
    async with servidor(app if envolver is None else envolver(app)) as base:
        yield Duble(
            base,
            Lado(endereco_de_duble(base, producao), CHAVE_DA_PRODUCAO),
            None if teste is None else Lado(endereco_de_duble(base, teste)),
            app.state.motor,
        )


def _novo() -> str:
    return str(uuid.uuid4())


def _ponteiro(dados: Any, ponteiro: str) -> Any:
    for parte in ponteiro.split("/")[1:]:
        dados = dados[parte.replace("~1", "/").replace("~0", "~")]
    return dados


def _sucesso(r: cliente.Resposta) -> bool:
    return r.tipo == "resultado" and isinstance(r.resultado, types.CallToolResult) and not r.resultado.is_error


def _recusada(r: cliente.Resposta) -> bool:
    """Recusada sem aplicar: erro de execucao (``isError``) ou erro JSON-RPC (SELO-2.2)."""
    if r.tipo == "erro_rpc":
        return True
    return r.tipo == "resultado" and isinstance(r.resultado, types.CallToolResult) and bool(r.resultado.is_error)


def _recibo(r: cliente.Resposta, numero: str) -> dict[str, Any] | None:
    """O recibo do nivel 3, se tem a forma do contrato e combina com o ``isError`` (SELO-3.2); senao None."""
    if r.tipo != "resultado" or not isinstance(r.resultado, types.CallToolResult):
        return None
    recibo = (r.resultado.meta or {}).get(contrato.CHAVE_RECIBO)
    if not isinstance(recibo, dict) or recibo.get("contrato") != contrato.CONTRATO:
        return None
    if recibo.get("operacao") != numero or recibo.get("estado") not in contrato.ESTADOS_DO_RECIBO:
        return None
    objetos = recibo.get("objetos")
    if not isinstance(recibo.get("repetida"), bool) or not isinstance(objetos, list):
        return None
    if not all(isinstance(o, dict) and set(o) == {"id"} and isinstance(o["id"], str) for o in objetos):
        return None
    sucesso = recibo["estado"] in ("concluida", "em_andamento")
    if sucesso == bool(r.resultado.is_error):
        return None  # incoerente: concluida com erro, ou recusada com sucesso
    return recibo


# --- A bateria de referencia -----------------------------------------------------------------------------------


@dataclass
class Bateria:
    producao: Lado
    alvo: Lado
    declaracao: Mapping[str, Any]
    nivel: int
    controle: Controle
    """As rotas de controle do duble, no lado do alvo."""
    itens: list[dict[str, Any]] = field(default_factory=list)
    partes: dict[tuple[str, str | None], dict[str, bool]] = field(default_factory=dict)
    """O resultado de cada parte de cada caso julgado (caso, ferramenta): as de ``selo_mentiroso.PARTES``."""
    marcas: list[bool] = field(default_factory=list)
    recibos_ruins: dict[str, int] = field(default_factory=dict)
    producao_diz_producao: bool = False
    alvo_diz_teste: bool = False

    @property
    def de_teste(self) -> bool:
        return self.declaracao["modo"] == "ferramentas_de_teste"

    def _item(self, caso: str, ferramenta: str | None, resultado: str, razao: str | None = None) -> None:
        tentativas = 1 if resultado in ("cumpriu", "falhou", "nao_observado") else 0
        self.itens.append(
            {
                "caso": caso,
                "ferramenta": ferramenta,
                "resultado": resultado,
                "razao": razao,
                "tentativas": tentativas,
                "excluidas": 0,
            }
        )

    def _julgar(
        self,
        caso: str,
        ferramenta: str | None,
        partes: Mapping[str, bool],
        *,
        sem_falha: str = "cumpriu",
        razao: str | None = None,
    ) -> None:
        """Julga o caso pelas partes (``selo_mentiroso.PARTES[caso]``, todas, sem curto-circuito): ``falhou`` se uma
        falha; senao ``sem_falha`` (``cumpriu``, ou o maximo que o metodo permite, com a razao)."""
        assert set(partes) == set(selo_mentiroso.PARTES[caso]), (caso, sorted(partes))
        self.partes[(caso, ferramenta)] = dict(partes)
        if all(partes.values()):
            self._item(caso, ferramenta, sem_falha, razao)
        else:
            self._item(caso, ferramenta, "falhou")

    async def consultar(self, numero: str, lado: Lado | None = None) -> tuple[str | None, Any]:
        """O estado que a consulta diz, ou None se ela nao respondeu ou saiu da forma do contrato."""
        r = await (lado or self.alvo).chamar(
            contrato.FERRAMENTA_CONSULTAR,
            {"operacao": numero},
            prazo=PRAZO_DO_CONTRATO,
            teto=contrato.TETO_RESPOSTA_BYTES,
        )
        if not _sucesso(r) or r.resultado is None:
            return None, None
        dados = r.resultado.structured_content  # type: ignore[union-attr]
        if not isinstance(dados, dict) or dados.get("contrato") != contrato.CONTRATO:
            return None, None
        if dados.get("operacao") != numero or dados.get("estado") not in contrato.ESTADOS:
            return None, None
        if "depois" not in dados:
            # No nivel 3, depois e exigido, mesmo vazio (desenho 2.2), como em respostas.conferir_consulta.
            return (None, None) if self.nivel >= 3 else (str(dados["estado"]), None)
        depois = dados["depois"]
        if not isinstance(depois, list) or len(depois) > contrato.MAX_EVENTOS_DEPOIS:
            return None, None
        for evento in depois:
            if not isinstance(evento, dict) or set(evento) != {"evento", "em"}:
                return None, None
            if evento["evento"] not in contrato.EVENTOS_DEPOIS or not isinstance(evento["em"], str):
                return None, None
        return str(dados["estado"]), depois

    async def _acao(self, nome: str, argumentos: Mapping[str, Any], numero: str, **kw: Any) -> cliente.Resposta:
        r = await self.alvo.chamar(nome, argumentos, numero, **kw)
        if self.de_teste and r.tipo == "resultado" and isinstance(r.resultado, types.CallToolResult):
            self.marcas.append((r.resultado.meta or {}).get(contrato.CHAVE_ALVO) == "teste")
        com_resultado = r.tipo == "resultado" and isinstance(r.resultado, types.CallToolResult)
        if self.nivel == 3 and com_resultado and _recibo(r, numero) is None:
            self.recibos_ruins[nome] = self.recibos_ruins.get(nome, 0) + 1
        return r

    async def _ler(self, leitura: Mapping[str, Any]) -> Any:
        r = await self.alvo.chamar(leitura["ferramenta"], leitura.get("argumentos", {}))
        assert _sucesso(r), r
        return r.resultado.structured_content  # type: ignore[union-attr]

    async def rastro(self, leitura: Mapping[str, Any]) -> int:
        valor = _ponteiro(await self._ler(leitura), leitura["conta"])
        return len(valor) if isinstance(valor, list) else int(valor)

    async def lista_do_rastro(self, leitura: Mapping[str, Any]) -> list[Any] | None:
        """O que o rastro aponta, quando e uma lista (os objetos criados); senao None (uma contagem)."""
        valor = _ponteiro(await self._ler(leitura), leitura["conta"])
        return list(valor) if isinstance(valor, list) else None

    # --- casos de sistema ---------------------------------------------------------------------------------------

    async def sistema(self) -> None:
        do_nivel = contrato.ferramentas_do_nivel(self.nivel)
        listas = {"producao": await self.producao.listar(), "alvo": await self.alvo.listar()}
        por_nome = {lado: {f["name"]: f for f in lista} for lado, lista in listas.items()}
        self._julgar(
            "C-0.1",
            None,
            {
                "listadas": all(n in p for p in por_nome.values() for n in do_nivel),
                "esquemas": all(
                    contrato.definicao_confere(p[n]) for p in por_nome.values() for n in do_nivel if n in p
                ),
            },
        )
        declaradas = list(self.declaracao["ferramentas"])
        producao = por_nome["producao"]
        de_teste = (
            [(n, self.declaracao["ferramentas"][n]["ferramenta_de_teste"]) for n in declaradas] if self.de_teste else []
        )
        self._julgar(
            "C-0.2",
            None,
            {
                "mesmas_acoes": self.de_teste
                or all(producao.get(n) == por_nome["alvo"].get(n) is not None for n in declaradas),
                "teste_listada": all(t in producao for _, t in de_teste),
                "nome_proprio": all(t not in declaradas for _, t in de_teste),
                "mesmo_esquema": all(
                    t not in producao or producao[t]["inputSchema"] == (producao.get(n) or {}).get("inputSchema")
                    for n, t in de_teste
                ),
            },
        )
        estado, _ = await self.consultar(_novo(), self.producao)
        self._julgar("C-0.3", None, {"nao_recebida": estado == "nao_recebida"})

        # C-0.4, antes da primeira escrita: a producao se declara de producao e, no modo ambiente, o alvo de teste se
        # declara de teste. No modo de ferramentas de teste, a marca de cada resposta fecha o caso no fim.
        async def alvo_de(lado: Lado) -> Any:
            r = await lado.chamar(contrato.FERRAMENTA_ALVO, {}, prazo=PRAZO_DO_CONTRATO)
            return (r.resultado.structured_content or {}).get("alvo") if _sucesso(r) else None  # type: ignore[union-attr]

        self.producao_diz_producao = await alvo_de(self.producao) == "producao"
        self.alvo_diz_teste = self.de_teste or await alvo_de(self.alvo) == "teste"

    def alvos(self) -> None:
        marcas = not self.de_teste or (bool(self.marcas) and all(self.marcas))
        self._julgar(
            "C-0.4", None, {"producao": self.producao_diz_producao, "alvo": self.alvo_diz_teste, "marcas": marcas}
        )

    # --- casos por ferramenta -----------------------------------------------------------------------------------

    async def ferramenta(self, nome: str) -> None:  # um caso apos o outro, como a tabela
        e = self.declaracao["ferramentas"][nome]
        chamado = e.get("ferramenta_de_teste") or nome
        args, rastro = e["argumentos"], e["rastro"]
        idempotente = "idempotente" in e

        # C-2.1 e C-3.1a: aceita com o numero e o rastro sobe um. Num rastro que e lista, os novos sao os criados.
        antes, lista_antes = await self.rastro(rastro), await self.lista_do_rastro(rastro)
        k1 = _novo()
        r1 = await self._acao(chamado, args, k1)
        subiu = await self.rastro(rastro) - antes
        self._julgar("C-2.1", nome, {"aceita": _sucesso(r1), "sobe_um": subiu == 1})
        recibo1 = _recibo(r1, k1)
        lista_depois = await self.lista_do_rastro(rastro)
        criados1 = None
        if lista_antes is not None and lista_depois is not None:
            criados1 = {json.dumps(x, sort_keys=True) for x in lista_depois} - {
                json.dumps(x, sort_keys=True) for x in lista_antes
            }

        # C-2.2a: a repeticao nao aplica, nao e erro se a primeira nao foi, e a consulta diz concluida. No nivel 3, o
        # recibo da repeticao diz repetida e traz os mesmos objetos.
        antes = await self.rastro(rastro)
        r2 = await self._acao(chamado, args, k1)
        partes = {
            "rastro": await self.rastro(rastro) == antes,
            "sem_erro": not _sucesso(r1) or _sucesso(r2),
            "consulta": (await self.consultar(k1))[0] == "concluida",
            "recibos": True,
            "repetida": True,
            "objetos": True,
        }
        if self.nivel == 3:
            recibo2 = _recibo(r2, k1)
            partes["recibos"] = recibo1 is not None and recibo2 is not None
            partes["repetida"] = recibo2 is None or recibo2["repetida"] is True
            partes["objetos"] = recibo1 is None or recibo2 is None or recibo2["objetos"] == recibo1["objetos"]
        self._julgar("C-2.2a", nome, partes)

        # C-2.2b: numero novo e escrita nova (ou recusada pelo proprio merito).
        if idempotente:
            self._item("C-2.2b", nome, "nao_se_aplica", "sem_segundo_efeito")
        else:
            antes = await self.rastro(rastro)
            r = await self._acao(chamado, args, _novo())
            subiu = await self.rastro(rastro) - antes
            ok = (_sucesso(r) and subiu == 1) or (_recusada(r) and subiu == 0)
            self._julgar("C-2.2b", nome, {"aplicada_ou_recusada": ok})

        # C-2.2c: o mesmo numero com outros argumentos e recusado, sem aplicar.
        antes = await self.rastro(rastro)
        r = await self._acao(chamado, e["argumentos_outros"], k1)
        self._julgar("C-2.2c", nome, {"recusada": _recusada(r), "rastro": await self.rastro(rastro) == antes})

        # C-2.2d: pares simultaneos com o mesmo numero; aplicar duas vezes reprova.
        antes = await self.rastro(rastro)
        numeros = [_novo() for _ in range(PARES)]
        async with anyio.create_task_group() as tg:
            for numero in numeros:
                for _ in range(2):
                    tg.start_soon(self._acao, chamado, args, numero)
        dobrou = await self.rastro(rastro) - antes > PARES
        self._julgar("C-2.2d", nome, {"nao_dobrou": not dobrou}, sem_falha="nao_testado", razao="poucos_pares")

        # C-2.3: o pedido de dados nao pos nada em efeito.
        if "pede_dados" in e:
            antes = await self.rastro(rastro)
            r = await self._acao(chamado, e["pede_dados"]["argumentos"], _novo())
            partes = {
                "input_required": isinstance(r.resultado, types.InputRequiredResult),
                "rastro": await self.rastro(rastro) == antes,
            }
            self._julgar("C-2.3", nome, partes, sem_falha="nao_observado")
        else:
            self._item("C-2.3", nome, "nao_se_aplica", "sem_condicao_declarada")

        # C-2.4a: a consulta diz concluida do aplicado e nao_recebida do nunca mandado, sem mudar o rastro.
        antes = await self.rastro(rastro)
        concluida = (await self.consultar(k1))[0] == "concluida"
        nao_recebida = (await self.consultar(_novo()))[0] == "nao_recebida"
        partes = {"concluida": concluida, "nao_recebida": nao_recebida, "rastro": await self.rastro(rastro) == antes}
        self._julgar("C-2.4a", nome, partes)

        # C-2.4b: a chamada recusada e a consulta do numero dela.
        await self._acao(chamado, e["argumentos_recusados"], k3 := _novo())
        estado_k3 = (await self.consultar(k3))[0]
        self._julgar("C-2.4b", nome, {"recusada_ou_nao_recebida": estado_k3 in ("recusada", "nao_recebida")})

        # C-2.5: o numero dado como nao recebido fica queimado.
        k4 = _novo()
        primeiro = (await self.consultar(k4))[0]
        antes = await self.rastro(rastro)
        r = await self._acao(chamado, args, k4)
        partes = {
            "nao_recebida": primeiro == "nao_recebida",
            "recusada": _recusada(r),
            "rastro": await self.rastro(rastro) == antes,
        }
        self._julgar("C-2.5", nome, partes)

        # C-2.6a: a escrita com a conexao cortada logo depois de mandar (critica A3).
        antes = await self.rastro(rastro)
        k6 = _novo()
        chegaram = await self.controle.chegadas(chamado)
        await self.alvo.mandar_e_cortar(chamado, args, k6)
        await esperar_a_chegada(lambda: self.controle.chegadas(chamado), chegaram)
        await anyio.sleep(DEPOIS_DO_CORTE)
        na_hora = (await self.consultar(k6))[0]
        await anyio.sleep(INTERVALO_DA_CONSULTA)
        depois_do_intervalo = (await self.consultar(k6))[0]
        aplicada = await self.rastro(rastro) - antes
        respostas = (na_hora, depois_do_intervalo)
        antes_da_repeticao = await self.rastro(rastro)
        await self._acao(chamado, args, k6)
        partes = {
            "responderam": all(x is not None for x in respostas),
            "nunca_nao_recebida": not aplicada
            or all(x is None or x in ("concluida", "em_andamento", "desconhecido") for x in respostas),
            "repeticao_nao_aplica": await self.rastro(rastro) == antes_da_repeticao,
        }
        self._julgar("C-2.6a", nome, partes)

        # C-2.6b: a consulta durante a escrita lenta.
        if "escrita_lenta" in e:
            lenta = e["escrita_lenta"]
            k7 = _novo()
            durante: list[str | None] = []
            chegaram = await self.controle.chegadas(chamado)
            async with anyio.create_task_group() as tg:
                tg.start_soon(lambda: self._acao(chamado, lenta["argumentos"], k7, prazo=float(lenta["segundos"]) + 5))
                await esperar_a_chegada(lambda: self.controle.chegadas(chamado), chegaram)
                durante.append((await self.consultar(k7))[0])
                await self.controle.soltar(k7)  # medido o durante, a escrita termina sem esperar o relogio
            self._julgar("C-2.6b", nome, {"durante": durante[0] in ("em_andamento", "desconhecido")})
        else:
            self._item("C-2.6b", nome, "nao_se_aplica", "sem_condicao_declarada")

        # C-2.7: a ferramenta idempotente, duas vezes com numeros novos.
        dica = await self._dica_idempotente(chamado)
        if dica or idempotente:
            conteudo = (e.get("idempotente") or {}).get("rastro") or rastro
            await self._acao(chamado, args, _novo())
            antes = await self.rastro(conteudo)
            await self._acao(chamado, args, _novo())
            nao_muda = await self.rastro(conteudo) == antes
            self._julgar("C-2.7", nome, {"nao_muda": nao_muda}, sem_falha="nao_observado")
        else:
            self._item("C-2.7", nome, "nao_se_aplica", "sem_condicao_declarada")

        if self.nivel == 3:
            await self._nivel_3(nome, chamado, e, k1, recibo1, criados1)

    async def _dica_idempotente(self, chamado: str) -> bool:
        for f in await self.alvo.listar():
            if f["name"] == chamado:
                return bool((f.get("annotations") or {}).get("idempotentHint"))
        return False

    async def _nivel_3(  # um caso apos o outro, como a tabela
        self,
        nome: str,
        chamado: str,
        e: Mapping[str, Any],
        k1: str,
        recibo1: dict[str, Any] | None,
        criados1: set[str] | None,
    ) -> None:
        args, rastro = e["argumentos"], e["rastro"]
        # C-3.1a: o sucesso veio com o efeito lido de volta.
        antes = await self.rastro(rastro)
        r = await self._acao(chamado, args, _novo())
        self._julgar("C-3.1a", nome, {"aceita": _sucesso(r), "sobe_um": await self.rastro(rastro) - antes == 1})

        # C-3.1b: a recusa volta como erro, com o recibo recusada.
        antes = await self.rastro(rastro)
        k8 = _novo()
        r = await self._acao(chamado, e["argumentos_recusados"], k8)
        recibo = _recibo(r, k8)
        partes = {
            "recusada": _recusada(r),
            "recibo_recusada": recibo is not None and recibo["estado"] == "recusada",
            "rastro": await self.rastro(rastro) == antes,
        }
        self._julgar("C-3.1b", nome, partes)

        # C-3.3: varios itens com um ruim.
        if "varios_itens" in e:
            varios = e["varios_itens"]
            antes = await self.rastro(rastro)
            k9 = _novo()
            r = await self._acao(chamado, varios["argumentos"], k9)
            aplicados = await self.rastro(rastro) - antes
            recibo = _recibo(r, k9)
            esperado = {"aplicados": aplicados, "nao_aplicados": varios["itens"] - aplicados}
            partes = {"recusada": _recusada(r), "itens": recibo is not None and recibo.get("itens") == esperado}
            self._julgar("C-3.3", nome, partes)
        else:
            self._item("C-3.3", nome, "nao_se_aplica", "sem_condicao_declarada")

        # C-3.4: a escrita velha (stale write) e recusada como erro e nao e aplicada.
        if "conflito" in e:
            c = e["conflito"]
            lido = await self._ler(c["leitura"])
            versao = _ponteiro(lido, c["leitura"]["versao"])
            concorrente = _ponteiro(lido, c["leitura"]["campo_concorrente"])
            a = await self._acao(chamado, c["argumentos"] | {c["argumento_versao"]: versao}, _novo())
            depois_de_a = await self._ler(c["leitura"])
            independentes = _ponteiro(depois_de_a, c["leitura"]["campo_concorrente"]) == concorrente
            antes = await self.rastro(rastro)
            b = await self._acao(chamado, c["argumentos_concorrente"] | {c["argumento_versao"]: versao}, _novo())
            partes = {
                "a_aceita": _sucesso(a),
                "independentes": independentes,
                "b_recusada": _recusada(b),
                "rastro": await self.rastro(rastro) == antes,
            }
            self._julgar("C-3.4", nome, partes)
        else:
            self._item("C-3.4", nome, "nao_se_aplica", "sem_condicao_declarada")

        # C-3.5: a consulta do aplicado traz depois, coerente com o recibo.
        estado, depois = await self.consultar(k1)
        coerente = recibo1 is not None and estado == recibo1["estado"] == "concluida"
        self._julgar("C-3.5", nome, {"depois": isinstance(depois, list), "coerente": coerente})

        # C-3.6a: o aviso com um numero concluido e um nunca mandado; repetido, as mesmas respostas.
        k5, aviso = _novo(), _novo()
        pedido = {"bloqueio": aviso, "operacoes": [k1, k5]}
        primeira = await self.alvo.chamar(contrato.FERRAMENTA_BLOQUEIO, pedido, prazo=PRAZO_DO_CONTRATO)
        segunda = await self.alvo.chamar(contrato.FERRAMENTA_BLOQUEIO, pedido, prazo=PRAZO_DO_CONTRATO)
        esperado_aviso = [{"operacao": k1, "estado": "concluida"}, {"operacao": k5, "estado": "nao_recebida"}]
        dados1 = primeira.resultado.structured_content if _sucesso(primeira) else None  # type: ignore[union-attr]
        dados2 = segunda.resultado.structured_content if _sucesso(segunda) else None  # type: ignore[union-attr]
        antes = await self.rastro(rastro)
        r = await self._acao(chamado, args, k5)
        partes = {
            "estados": isinstance(dados1, dict) and dados1.get("operacoes") == esperado_aviso,
            "repetido": dados1 is not None and dados2 == dados1,
            "recusada": _recusada(r),
            "rastro": await self.rastro(rastro) == antes,
        }
        self._julgar("C-3.6a", nome, partes)

        # C-3.6b: o efeito adiado parado pelo aviso. O rastro e lido so depois de passados os segundos declarados (o
        # fim dos adiados): o escreve_escondido poe o efeito no fim do prazo, dizendo parada.
        if "efeito_adiado" in e:
            adiado = e["efeito_adiado"]
            antes = await self.rastro(rastro)
            k10 = _novo()
            r = await self._acao(chamado, adiado["argumentos"], k10)
            recibo = _recibo(r, k10)
            aviso_b = await self.alvo.chamar(
                contrato.FERRAMENTA_BLOQUEIO, {"bloqueio": _novo(), "operacoes": [k10]}, prazo=PRAZO_DO_CONTRATO
            )
            parou = _sucesso(aviso_b) and (aviso_b.resultado.structured_content or {}).get("operacoes") == [  # type: ignore[union-attr]
                {"operacao": k10, "estado": "parada"}
            ]
            await self.controle.esperar_os_adiados(float(adiado["segundos"]) + PRAZO_DA_CHEGADA)
            partes = {
                "recibo_em_andamento": recibo is not None and recibo["estado"] == "em_andamento",
                "aviso_parada": parou,
                "rastro": await self.rastro(rastro) == antes,
                "consulta_parada": (await self.consultar(k10))[0] == "parada",
            }
            self._julgar("C-3.6b", nome, partes)
        else:
            self._item("C-3.6b", nome, "nao_se_aplica", "sem_condicao_declarada")

        # C-3.2: todo recibo das acoes desta ferramenta tem a forma e combina com o erro; numa criacao (rastro que e
        # uma lista), o recibo da primeira chamada traz o identificador de cada objeto criado, e eles existem.
        objetos_ok = True
        if recibo1 is not None and criados1 is not None:
            ditos = {json.dumps(o["id"]) for o in recibo1["objetos"]}
            objetos_ok = bool(criados1) and ditos == criados1
        recibos_ok = self.recibos_ruins.get(chamado, 0) == 0 and recibo1 is not None
        self._julgar("C-3.2", nome, {"recibos": recibos_ok, "objetos": objetos_ok})

    async def rodar(self) -> list[dict[str, Any]]:
        await self.sistema()
        for nome in self.declaracao["ferramentas"]:
            await self.ferramenta(nome)
        self.alvos()
        return self.itens


def falhas(itens: Sequence[Mapping[str, Any]]) -> set[str]:
    return {str(i["caso"]) for i in itens if i["resultado"] == "falhou"}


def aprova(itens: Sequence[Mapping[str, Any]], nivel: int, ferramentas: Sequence[str]) -> bool:
    assert contrato.problema_nos_casos(list(itens), list(ferramentas)) is None
    return contrato.casos_aprovam(itens, list(ferramentas), nivel)


def _producao_sem_acao(contagem: Mapping[str, Any], *, de_teste: bool, acoes: Sequence[str]) -> None:
    """O que a producao do duble viu da bateria de referencia (critica A4), pela ``/contagem``. No modo ambiente: so a
    consulta de controle e o ``selo_alvo``, com a chave das declaracoes, e nenhuma escrita aplicada (o alvo de teste so
    ve a chave da bateria). No modo de ferramentas de teste: so as ferramentas de teste, as leituras do rastro e as do
    contrato, nenhuma acao real."""
    producao = contagem["producao"]["chamadas"]
    if de_teste:
        assert not set(producao) & set(selo_nivel2.ACOES), producao
        assert {a + selo_nivel2.SUFIXO_TESTE for a in acoes} <= set(producao), producao
        assert set(contagem["producao"]["pessoas"]) == {"bateria", "declaracoes"}
    else:
        assert set(producao) == {contrato.FERRAMENTA_CONSULTAR, contrato.FERRAMENTA_ALVO}, producao
        assert contagem["producao"]["aplicadas"] == {}
        assert set(contagem["producao"]["pessoas"]) == {"declaracoes"}
        assert set(contagem["teste"]["pessoas"]) == {"bateria"}


def partes_que_falham(bateria: Bateria, caso: str) -> dict[str | None, set[str]]:
    """Por ferramenta (``None`` num caso de sistema), as partes de ``caso`` que falharam, so onde o caso falhou."""
    return {
        f: falharam
        for (c, f), partes in bateria.partes.items()
        if c == caso and (falharam := {p for p, ok in partes.items() if not ok})
    }


async def _rodar_bateria(
    duble: Duble, nivel: int, *, modo: str = "ambiente_de_teste", acoes: Sequence[str] = selo_nivel2.ACOES
) -> Bateria:
    """A bateria de referencia no duble: no modo ambiente, o alvo e o endereco de teste; no modo de ferramentas de
    teste, o endereco de producao, com as ferramentas de teste. Nos dois, com a chave da bateria. ``acoes``: as do
    duble, que a declaracao cobre."""
    alvo = duble.teste if modo == "ambiente_de_teste" else Lado(duble.producao.url, CHAVE)
    assert alvo is not None
    endereco = alvo.url if modo == "ambiente_de_teste" else None
    lado = "teste" if modo == "ambiente_de_teste" else "producao"
    dados = selo_nivel2.declaracao(
        endereco_teste=endereco,
        modo=modo,  # type: ignore[arg-type]
        acoes=acoes,
        segundos=SEGUNDOS_DECLARADOS,
    )

    bateria = Bateria(duble.producao, alvo, dados, nivel, Controle(duble, lado))
    await bateria.rodar()
    return bateria


async def _testar(
    duble: Duble, nivel: int, *, modo: str = "ambiente_de_teste", acoes: Sequence[str] = selo_nivel2.ACOES
) -> list[dict[str, Any]]:
    """Os itens da bateria de referencia no duble (``_rodar_bateria``)."""
    return (await _rodar_bateria(duble, nivel, modo=modo, acoes=acoes)).itens


# --- O honesto -------------------------------------------------------------------------------------------------


async def test_duble_de_nivel_2_cumpre_o_nivel_2_e_falha_os_casos_do_3() -> None:
    async with no_ar(selo_nivel2.criar) as duble:
        no_2 = await _testar(duble, 2)
        no_3 = await _testar(duble, 3)
    assert falhas(no_2) == set()
    assert aprova(no_2, 2, selo_nivel2.ACOES)
    assert falhas(no_3) == set(selo_nivel2.FALHA_NO_NIVEL_3)
    assert not aprova(no_3, 3, selo_nivel2.ACOES)


@pytest.mark.parametrize(
    ("ferramentas_de_teste", "esquemas"),
    [(False, "contrato"), (True, "contrato"), (False, "kit")],
    ids=["ambiente", "ferramentas_de_teste", "esquemas_do_kit"],
)
async def test_duble_de_nivel_3_cumpre_todos_os_casos(ferramentas_de_teste: bool, esquemas: str) -> None:
    async with no_ar(selo_nivel3.criar, ferramentas_de_teste=ferramentas_de_teste, esquemas=esquemas) as duble:
        modo = "ferramentas_de_teste" if ferramentas_de_teste else "ambiente_de_teste"
        itens = await _testar(duble, 3, modo=modo)
        contagem = await duble.controle("/contagem")
    assert falhas(itens) == set()
    assert aprova(itens, 3, selo_nivel2.ACOES)
    por_caso = {(i["caso"], i["ferramenta"]): (i["resultado"], i["razao"]) for i in itens}
    # Critica M6: a ferramenta idempotente honesta e aprovada (C-2.2b nao se aplica; C-2.7 so nao_observado).
    assert por_caso[("C-2.2b", "marcar_lido")] == ("nao_se_aplica", "sem_segundo_efeito")
    assert por_caso[("C-2.7", "marcar_lido")] == ("nao_observado", None)
    assert por_caso[("C-2.2d", "enviar_lembrete")] == ("nao_testado", "poucos_pares")
    _producao_sem_acao(contagem, de_teste=ferramentas_de_teste, acoes=selo_nivel2.ACOES)


async def test_esquemas_do_kit_tem_titulos_e_conferem_na_forma_essencial() -> None:
    """Critica B4: um sistema feito pelo caminho comum do kit (funcao com tipos) lista os esquemas com ``title`` e
    ``$defs``, e a conferencia do contrato aceita."""
    async with no_ar(selo_nivel3.criar, esquemas="kit") as duble:
        lista = {f["name"]: f for f in await duble.producao.listar()}
    for nome in contrato.FERRAMENTAS_DO_CONTRATO:
        definicao = lista[nome]
        assert definicao != selo_nivel2.definicao_publicada(nome)
        assert "title" in json.dumps(definicao["inputSchema"]) + json.dumps(definicao["outputSchema"])
        assert contrato.definicao_confere(definicao)
    assert "$defs" in lista[contrato.FERRAMENTA_CONSULTAR]["outputSchema"]


@pytest.mark.parametrize("modo", ["ambiente_de_teste", "ferramentas_de_teste"])
def test_a_declaracao_do_duble_segue_o_modelo_da_declaracao(modo: str) -> None:
    endereco = "https://teste.duble.exemplo.teste/mcp" if modo == "ambiente_de_teste" else None
    dados = selo_nivel2.declaracao(endereco_teste=endereco, modo=modo)  # type: ignore[arg-type]
    lida = declaracao_do_selo.validar(dados)
    assert set(lida.ferramentas) == set(selo_nivel2.ACOES)
    assert lida.guardada() == dados


@pytest.mark.parametrize(
    ("fabrica", "kw", "falham"),
    [(selo_nivel2.criar, {}, set()), (selo_mentiroso.criar, {"modo": "duplica", "nivel": 2}, {"C-2.6a"})],
    ids=["honesto", "duplica"],
)
async def test_a_bateria_espera_a_chamada_chegar_antes_de_consultar(
    fabrica: Callable[..., Starlette], kw: dict[str, Any], falham: set[str]
) -> None:
    """Numa maquina carregada, a chamada pode chegar ao duble depois da hora em que a bateria consultaria. A bateria
    de referencia espera o duble contar a chamada antes de consultar (C-2.6a e C-2.6b). Sem isso, a consulta chegaria
    antes: o honesto queimaria o numero da escrita lenta e falharia o C-2.6b, e o ``duplica`` nao seria pego no
    C-2.6a (o numero queimado recusa a chamada e a repeticao)."""

    def devagar(app: ASGIApp) -> ASGIApp:
        return ChamadaQueDemora(app, ATRASO_DA_CHEGADA)

    async with no_ar(fabrica, envolver=devagar, **kw) as duble:
        alvo = duble.teste
        assert alvo is not None
        dados = selo_nivel2.declaracao(endereco_teste=alvo.url, segundos=SEGUNDOS_DECLARADOS)

        bateria = Bateria(duble.producao, alvo, dados, 2, Controle(duble, "teste"))
        await bateria.ferramenta("enviar_lembrete")
    por_caso = {i["caso"]: i["resultado"] for i in bateria.itens}
    assert {c for c in ("C-2.6a", "C-2.6b") if por_caso[c] == "falhou"} == falham
    assert por_caso["C-2.6b"] == "cumpriu"


# --- O mentiroso -----------------------------------------------------------------------------------------------


def test_a_tabela_do_mentiroso_cobre_todo_modo_com_casos_do_contrato() -> None:
    assert set(selo_mentiroso.CASOS_QUE_FALHAM) == set(selo_mentiroso.MODOS)
    for casos in selo_mentiroso.CASOS_QUE_FALHAM.values():
        assert set(casos) <= set(contrato.CASOS_POR_NOME)
        assert len(set(casos)) == len(casos)
    assert set(selo_nivel2.FALHA_NO_NIVEL_3) <= set(contrato.CASOS_POR_NOME)
    assert set(selo_mentiroso.ACOES_DO_MODO) == set(selo_mentiroso.MODOS)
    for acoes in selo_mentiroso.ACOES_DO_MODO.values():
        assert acoes
        assert set(acoes) <= set(selo_nivel2.ACOES)


def test_todo_caso_do_contrato_tem_um_modo_que_o_falha() -> None:
    """Completude da tabela, por caso e por parte (este teste confere a forma das listas, sem rodar a bateria).

    Por caso: todo caso de ``contrato.CASOS`` (os obrigatorios dos niveis 2 e 3 e tambem o C-2.2d, que nao pode
    falhar) e falhado por pelo menos um modo do mentiroso, ou esta em ``CASOS_SEM_MODO`` com a razao (mutacoes Z1, Z2
    e Y14 do verificador).

    Por parte: todo caso tem as suas partes em ``selo_mentiroso.PARTES``, e cada parte esta em ``PARTES_FORCADAS``,
    com modos que tem o caso na tabela, ou em ``PARTES_SEM_MODO``, com a razao, nunca nas duas. Que a bateria confere
    exatamente essas partes, ``Bateria._julgar`` confere a cada caso julgado. Que cada modo de ``PARTES_FORCADAS``
    falha o caso so por aquela parte, em toda ferramenta em que o caso falha, quem confere e
    ``test_cada_modo_do_mentiroso_falha_os_casos_da_tabela``, rodando o modo: uma bateria que nao confira uma parte
    forcada deixa o caso passar nesse modo e nao casa com a tabela (mutacoes P1 a P6 e Q do verificador). Uma parte de
    ``PARTES_SEM_MODO`` nao e forcada: a razao diz por que nenhum modo a pega sozinha.

    Os modos sem caso sao exatamente os de ``MODOS_SEM_CASO``, cada um com a razao (o ``compara_token`` pela decisao 1
    do coordenador)."""
    falhados = {c for casos in selo_mentiroso.CASOS_QUE_FALHAM.values() for c in casos}
    obrigatorios = {c.caso for c in contrato.CASOS if c.obrigatorio and c.nivel in contrato.NIVEIS}
    assert obrigatorios
    sem_modo = set(selo_mentiroso.CASOS_SEM_MODO)
    assert obrigatorios - sem_modo <= falhados
    assert set(contrato.CASOS_POR_NOME) - sem_modo <= falhados
    assert not sem_modo & falhados  # uma excecao que um modo ja falha nao e excecao
    assert sem_modo <= set(contrato.CASOS_POR_NOME)
    assert all(r.strip() for r in selo_mentiroso.CASOS_SEM_MODO.values())
    vazios = {m for m, casos in selo_mentiroso.CASOS_QUE_FALHAM.items() if not casos}
    assert vazios == set(selo_mentiroso.MODOS_SEM_CASO) == {"membro_com_instrucao", "compara_token"}
    assert all(r.strip() for r in selo_mentiroso.MODOS_SEM_CASO.values())
    assert "Decisao 1 do coordenador" in selo_mentiroso.MODOS_SEM_CASO["compara_token"]

    assert set(selo_mentiroso.PARTES) == set(contrato.CASOS_POR_NOME)
    todas = {(caso, parte) for caso, partes in selo_mentiroso.PARTES.items() for parte in partes}
    assert all(selo_mentiroso.PARTES.values())
    assert all(d.strip() for partes in selo_mentiroso.PARTES.values() for d in partes.values())
    forcadas, sem = set(selo_mentiroso.PARTES_FORCADAS), set(selo_mentiroso.PARTES_SEM_MODO)
    assert not forcadas & sem
    assert forcadas | sem == todas
    for (caso, parte), modos in selo_mentiroso.PARTES_FORCADAS.items():
        assert modos, (caso, parte)
        assert len(set(modos)) == len(modos), (caso, parte)
        for m in modos:
            assert caso in selo_mentiroso.CASOS_QUE_FALHAM[m], (caso, parte, m)
    assert all(r.strip() for r in selo_mentiroso.PARTES_SEM_MODO.values())


@pytest.mark.parametrize("modo", selo_mentiroso.MODOS)
async def test_cada_modo_do_mentiroso_falha_os_casos_da_tabela(modo: str) -> None:
    de_teste = modo in selo_mentiroso.MODOS_COM_FERRAMENTAS_DE_TESTE
    acoes = selo_mentiroso.ACOES_DO_MODO[modo]
    async with no_ar(selo_mentiroso.criar, modo=modo, ferramentas_de_teste=de_teste, acoes=acoes) as duble:
        modo_da_declaracao = "ferramentas_de_teste" if de_teste else "ambiente_de_teste"
        bateria = await _rodar_bateria(duble, 3, modo=modo_da_declaracao, acoes=acoes)
        contagem = await duble.controle("/contagem")
    esperado = set(selo_mentiroso.CASOS_QUE_FALHAM[modo])
    assert falhas(bateria.itens) == esperado
    assert aprova(bateria.itens, 3, acoes) == (not esperado)
    # Cada parte que este modo forca (PARTES_FORCADAS): o caso falha, e em toda ferramenta em que falha, so por ela.
    for (caso, parte), modos in selo_mentiroso.PARTES_FORCADAS.items():
        if modo in modos:
            por_ferramenta = partes_que_falham(bateria, caso)
            assert por_ferramenta, (caso, parte)
            assert all(ps == {parte} for ps in por_ferramenta.values()), (caso, parte, por_ferramenta)
    # Em modo nenhum a bateria escreve na producao do duble, nem pelo defeito do modo: o consulta_escreve escreve so no
    # alvo (escrevendo em toda consulta de numero novo, ele escreveria na producao na consulta de controle do C-0.3).
    _producao_sem_acao(contagem, de_teste=de_teste, acoes=acoes)


def _contexto(pessoa: str = "bateria") -> Any:
    """Um contexto de chamada so com a pessoa da chave, para chamar o motor direto, sem servidor."""
    return cast(Any, SimpleNamespace(request=SimpleNamespace(state=SimpleNamespace(pessoa=pessoa))))


CAMINHOS_DA_RECUSA: dict[str, tuple[tuple[int, int, int], tuple[int, int, int]]] = {
    "argumentos_recusados/enviar_lembrete": ((0, 0, 0), (1, 0, 0)),
    "argumentos_recusados/enviar_lote": ((0, 0, 0), (1, 0, 0)),
    "argumentos_recusados/marcar_lido": ((0, 0, 0), (0, 1, 0)),
    "argumentos_recusados/trocar_perfil": ((0, 0, 0), (0, 0, 1)),
    "lote_com_item_ruim": ((2, 0, 0), (3, 0, 0)),
    "versao_velha": ((0, 0, 0), (0, 0, 1)),
    "outros_argumentos": ((0, 0, 0), (1, 0, 0)),
    "queimado_pela_consulta": ((0, 0, 0), (1, 0, 0)),
    "queimado_pelo_aviso": ((0, 0, 0), (1, 0, 0)),
}
"""Cada caminho em que o honesto recusa sem aplicar, com o quanto os dados mudam (lembretes, marcacoes de lido e
mudancas do perfil) no honesto e no ``recusa_mas_aplica``."""


async def _recusa(motor: selo_nivel2.Motor, caminho: str) -> tuple[Any, tuple[int, int, int]]:
    """Prepara o caminho num motor novo e faz a chamada recusada: o que o motor respondeu e quanto os dados mudaram."""
    amb, ctx = motor.ambientes["teste"], _contexto()
    dados = amb.espacos[selo_nivel2.ESPACO_REAL]
    entrada = {a: selo_nivel2.entrada_da_declaracao(a, de_teste=False) for a in selo_nivel2.ACOES}
    numero, outro = str(uuid.UUID(int=1)), str(uuid.UUID(int=2))

    async def acao(nome: str, args: Mapping[str, Any], k: str) -> Any:
        return await motor._acao(amb, ctx, nome, selo_nivel2.ESPACO_REAL, dict(args), k)

    pedido: tuple[str, Mapping[str, Any]] = ("enviar_lembrete", entrada["enviar_lembrete"]["argumentos"])
    if caminho == "outros_argumentos":
        await acao(*pedido, numero)
        pedido = ("enviar_lembrete", entrada["enviar_lembrete"]["argumentos_outros"])
    elif caminho == "queimado_pela_consulta":
        motor._consultar(amb, ctx, {"operacao": numero})
    elif caminho == "queimado_pelo_aviso":
        motor._bloqueio(amb, ctx, {"bloqueio": outro, "operacoes": [numero]})
    elif caminho == "versao_velha":
        await acao("trocar_perfil", {"nome": "Escritor A"}, outro)  # o perfil passa da versao 0 para a 1
        pedido = ("trocar_perfil", {"telefone": "0000-0000", "versao": 0})
    elif caminho == "lote_com_item_ruim":
        pedido = ("enviar_lote", entrada["enviar_lote"]["varios_itens"]["argumentos"])
    else:
        nome = caminho.removeprefix("argumentos_recusados/")
        pedido = (nome, entrada[nome]["argumentos_recusados"])

    def medida() -> tuple[int, int, int]:
        return (len(dados.lembretes), dados.aplicacoes_lidos, dados.mudancas)

    antes = medida()
    try:
        r = await acao(*pedido, numero)
    except MCPError as erro:
        resposta: Any = ("erro_rpc", erro.code, erro.message)
    else:
        assert isinstance(r, types.CallToolResult)
        assert r.is_error
        resposta = (r.content, r.is_error, r.meta)
    depois = medida()
    return resposta, (depois[0] - antes[0], depois[1] - antes[1], depois[2] - antes[2])


@pytest.mark.parametrize("caminho", list(CAMINHOS_DA_RECUSA))
async def test_recusa_mas_aplica_responde_a_recusa_do_honesto_e_aplica(caminho: str) -> None:
    """O ``recusa_mas_aplica``, caminho a caminho, contra o honesto (o motor chamado direto): a resposta e a mesma
    recusa (texto, ``isError`` e recibo, ou o mesmo erro JSON-RPC), e so ele muda os dados. A tabela do mentiroso ve os
    casos pelo nome, e varios caminhos levam ao mesmo caso (o C-3.1b, nas quatro acoes): esta prova pega o caminho que
    deixasse de aplicar."""
    honesto, mudou_no_honesto = await _recusa(selo_nivel2.Motor(nivel=3), caminho)
    mentiroso, mudou_no_mentiroso = await _recusa(selo_mentiroso.Mentiroso(modo="recusa_mas_aplica", nivel=3), caminho)
    assert mentiroso == honesto
    assert (mudou_no_honesto, mudou_no_mentiroso) == CAMINHOS_DA_RECUSA[caminho]


# --- Comportamentos que a bateria nao cobre sozinha ----------------------------------------------------------


async def test_aviso_que_chega_antes_da_chamada_para_o_efeito_sem_contradicao() -> None:
    """Critica M2: a chamada ainda a caminho (o duble a segura), o aviso chega antes e queima o numero; a chamada
    que chega depois e recusada sem aplicar, com recibo ``parada``. As respostas nao se contradizem pelo contrato."""
    async with no_ar(selo_nivel3.criar) as duble:
        alvo = duble.teste
        assert alvo is not None
        await duble.controle("/ajustes", {"segurar_segundos": 1.0})
        numero = _novo()
        args = selo_nivel2.entrada_da_declaracao("enviar_lembrete", de_teste=False)["argumentos"]
        respostas: dict[str, Any] = {}

        async def chamar() -> None:
            respostas["chamada"] = await alvo.chamar("enviar_lembrete", args, numero)

        async with anyio.create_task_group() as tg:
            tg.start_soon(chamar)
            await anyio.sleep(0.2)
            aviso = await alvo.chamar(contrato.FERRAMENTA_BLOQUEIO, {"bloqueio": _novo(), "operacoes": [numero]})
        await duble.controle("/ajustes", {"segurar_segundos": 0})
        consulta = await alvo.chamar(contrato.FERRAMENTA_CONSULTAR, {"operacao": numero})
        lembretes = await alvo.chamar("listar_lembretes", {})
    estado_do_aviso = aviso.resultado.structured_content["operacoes"][0]["estado"]  # type: ignore[union-attr]
    recibo = _recibo(respostas["chamada"], numero)
    assert estado_do_aviso == "nao_recebida"
    assert recibo is not None
    assert recibo["estado"] == "parada"
    assert consulta.resultado.structured_content["estado"] == "nao_recebida"  # type: ignore[union-attr]
    assert lembretes.resultado.structured_content["itens"] == []  # type: ignore[union-attr]
    ditas = [estado_do_aviso, recibo["estado"], "nao_recebida"]
    assert not any(contrato.contradiz(ditas[:i], ditas[i]) for i in range(1, len(ditas)))


async def test_ignora_bloqueio_contradiz_pelo_contrato() -> None:
    """O efeito adiado acontece depois de o sistema dizer ``parada``: a consulta seguinte (``concluida``) contradiz,
    e o banco retiraria o selo (``contrato.contradiz``, a mesma regra de ``selo_passagem_permitida``)."""
    async with no_ar(selo_mentiroso.criar, modo="ignora_bloqueio") as duble:
        alvo = duble.teste
        assert alvo is not None
        entrada = selo_nivel2.entrada_da_declaracao("enviar_lembrete", de_teste=False)
        numero = _novo()
        r = await alvo.chamar("enviar_lembrete", entrada["efeito_adiado"]["argumentos"], numero)
        aviso = await alvo.chamar(contrato.FERRAMENTA_BLOQUEIO, {"bloqueio": _novo(), "operacoes": [numero]})
        await anyio.sleep(entrada["efeito_adiado"]["segundos"] + 0.5)
        consulta = await alvo.chamar(contrato.FERRAMENTA_CONSULTAR, {"operacao": numero})
    recibo = _recibo(r, numero)
    assert recibo is not None
    assert recibo["estado"] == "em_andamento"
    ditas = [
        recibo["estado"],
        aviso.resultado.structured_content["operacoes"][0]["estado"],  # type: ignore[union-attr]
        consulta.resultado.structured_content["estado"],  # type: ignore[union-attr]
    ]
    assert ditas == ["em_andamento", "parada", "concluida"]
    assert contrato.contradiz(ditas[:2], ditas[2])


async def test_numeros_pela_pessoa_e_nao_pelo_texto_da_chave() -> None:
    """Critica A3.3: o honesto guarda os numeros pela pessoa; com a chave renovada da mesma pessoa (``ana`` ou a
    ``bateria``), a consulta diz o que houve. O ``compara_token`` (o defeito) diz ``nao_recebida`` de uma escrita
    feita. Nenhum caso da bateria o reprova, pela decisao 1 do coordenador (``selo_mentiroso.MODOS_SEM_CASO``): esta
    prova fica aqui, contra o duble, e a do gateway e do G9."""
    for fabrica, esperado in ((selo_nivel3.criar, "concluida"), (selo_mentiroso.criar, "nao_recebida")):
        kw = {"modo": "compara_token"} if fabrica is selo_mentiroso.criar else {}
        async with no_ar(fabrica, **kw) as duble:
            assert duble.teste is not None
            args = selo_nivel2.entrada_da_declaracao("enviar_lembrete", de_teste=False)["argumentos"]
            bia = Lado(duble.teste.url, "chave-bia")
            vistos: list[tuple[Any, Any]] = []
            for chave, renovada in (("chave-ana", "chave-ana-renovada"), (CHAVE, "chave-teste-selo-renovada")):
                numero = _novo()
                assert _sucesso(await Lado(duble.teste.url, chave).chamar("enviar_lembrete", args, numero))
                pela_renovada = await Lado(duble.teste.url, renovada).chamar(
                    contrato.FERRAMENTA_CONSULTAR, {"operacao": numero}
                )
                pela_bia = await bia.chamar(contrato.FERRAMENTA_CONSULTAR, {"operacao": numero})
                vistos.append((_estado(pela_renovada), _estado(pela_bia)))
        # Outra pessoa nunca ve o numero de ana nem o da bateria (recomendacao do contrato: so a mesma identidade).
        assert vistos == [(esperado, "nao_recebida"), (esperado, "nao_recebida")]


async def test_desfeita_e_desconhecido_pelo_controle() -> None:
    """Critica M4: o sistema desfaz o que o numero fez (``desfeita``, com o evento) ou perde o registro
    (``desconhecido``); a repeticao com o numero desfeito e recusada (erro JSON-RPC), e a com o numero desconhecido
    nao aplica e diz ``desconhecido``."""
    async with no_ar(selo_nivel3.criar) as duble:
        alvo = duble.teste
        assert alvo is not None
        args = selo_nivel2.entrada_da_declaracao("enviar_lembrete", de_teste=False)["argumentos"]
        desfeito, perdido = _novo(), _novo()
        for numero in (desfeito, perdido):
            assert _sucesso(await alvo.chamar("enviar_lembrete", args, numero))
        assert (await duble.controle("/operacao", {"operacao": desfeito, "acao": "desfazer", "alvo": "teste"}))["ok"]
        assert (await duble.controle("/operacao", {"operacao": perdido, "acao": "desconhecer", "alvo": "teste"}))["ok"]
        c1 = (await alvo.chamar(contrato.FERRAMENTA_CONSULTAR, {"operacao": desfeito})).resultado
        c2 = (await alvo.chamar(contrato.FERRAMENTA_CONSULTAR, {"operacao": perdido})).resultado
        repetir_desfeito = await alvo.chamar("enviar_lembrete", args, desfeito)
        repetir_perdido = await alvo.chamar("enviar_lembrete", args, perdido)
        itens = (await alvo.chamar("listar_lembretes", {})).resultado.structured_content["itens"]  # type: ignore[union-attr]
    assert c1.structured_content["estado"] == "desfeita"  # type: ignore[union-attr]
    assert [e["evento"] for e in c1.structured_content["depois"]] == ["desfeita"]  # type: ignore[union-attr]
    assert c2.structured_content["estado"] == "desconhecido"  # type: ignore[union-attr]
    assert repetir_desfeito.tipo == "erro_rpc"
    recibo = _recibo(repetir_perdido, perdido)
    assert recibo is not None
    assert (recibo["estado"], recibo["repetida"]) == ("desconhecido", True)
    assert len(itens) == 1  # o desfeito saiu; o perdido nao foi aplicado de novo
    assert not contrato.contradiz(["concluida"], "desfeita")


async def test_sem_saber_e_o_desfecho_aberto_que_a_consulta_resolve() -> None:
    """Critica B9: a escrita aplicada com erro e recibo ``desconhecido``; a consulta depois diz ``concluida``, sem
    contradicao."""
    async with no_ar(selo_nivel3.criar) as duble:
        alvo = duble.teste
        assert alvo is not None
        args = selo_nivel2.entrada_da_declaracao("enviar_lembrete", de_teste=False)["argumentos"]
        numero = _novo()
        r = await alvo.chamar("enviar_lembrete", args | {"sem_saber": True}, numero)
        consulta = (await alvo.chamar(contrato.FERRAMENTA_CONSULTAR, {"operacao": numero})).resultado
        itens = (await alvo.chamar("listar_lembretes", {})).resultado.structured_content["itens"]  # type: ignore[union-attr]
    recibo = _recibo(r, numero)
    assert recibo is not None
    assert recibo["estado"] == "desconhecido"
    assert r.resultado.is_error  # type: ignore[union-attr]
    assert consulta.structured_content["estado"] == "concluida"  # type: ignore[union-attr]
    assert len(itens) == 1
    assert not contrato.contradiz(["desconhecido"], "concluida")


def _texto(r: cliente.Resposta) -> str:
    assert r.tipo == "resultado", r
    assert isinstance(r.resultado, types.CallToolResult), r
    (conteudo,) = r.resultado.content
    assert isinstance(conteudo, types.TextContent)
    return conteudo.text


@pytest.mark.parametrize("fabrica", [selo_nivel2.criar, selo_nivel3.criar], ids=["nivel_2", "nivel_3"])
async def test_a_repeticao_depois_de_sem_saber_diz_o_que_houve_com_a_primeira(
    fabrica: Callable[..., Starlette],
) -> None:
    """SELO-2.2: a repeticao responde o desfecho da primeira, isto e, o que de fato houve com ela. A primeira aplicou e
    respondeu erro sem saber (critica B9). A repeticao nao aplica, nao e erro (a escrita esta em efeito) e o texto diz
    que o pedido ja foi feito, nunca "nao se sabe" junto com sucesso. No nivel 3, o recibo diz ``concluida``, com
    ``repetida`` e o objeto criado; no 2, nao ha recibo."""
    async with no_ar(fabrica) as duble:
        alvo = duble.teste
        assert alvo is not None
        args = selo_nivel2.entrada_da_declaracao("enviar_lembrete", de_teste=False)["argumentos"] | {"sem_saber": True}
        numero = _novo()
        primeira = await alvo.chamar("enviar_lembrete", args, numero)
        repetida = await alvo.chamar("enviar_lembrete", args, numero)
        itens = (await alvo.chamar("listar_lembretes", {})).resultado.structured_content["itens"]  # type: ignore[union-attr]
    assert _recusada(primeira)
    assert "nao se sabe" in _texto(primeira)
    assert _sucesso(repetida)
    assert _texto(repetida) == "Este pedido ja foi feito (lembrete-1); nada foi feito de novo."
    assert itens == ["lembrete-1"]
    meta = repetida.resultado.meta or {}  # type: ignore[union-attr]
    if fabrica is selo_nivel3.criar:
        recibo = _recibo(repetida, numero)
        assert recibo is not None
        assert (recibo["estado"], recibo["repetida"], recibo["objetos"]) == ("concluida", True, [{"id": "lembrete-1"}])
    else:
        assert contrato.CHAVE_RECIBO not in meta


async def test_a_repeticao_usa_o_texto_do_estado_de_agora_quando_ele_mudou() -> None:
    """SELO-2.2 e SELO-3.2: enquanto o estado e o que a primeira resposta disse, a repeticao repete o texto dela; quando
    mudou depois (o efeito adiado terminou, ou o aviso de bloqueio parou), o texto, o ``isError`` e o recibo dizem o
    estado de agora, sem aplicar de novo."""
    async with no_ar(selo_nivel3.criar) as duble:
        alvo = duble.teste
        assert alvo is not None
        entrada = selo_nivel2.entrada_da_declaracao("enviar_lembrete", de_teste=False)
        args, adiado = entrada["argumentos"], entrada["efeito_adiado"]
        k_feito, k_adiado, k_parado = _novo(), _novo(), _novo()
        primeiras = {
            "feito": await alvo.chamar("enviar_lembrete", args, k_feito),
            "parado": await alvo.chamar("enviar_lembrete", adiado["argumentos"], k_parado),
        }
        aviso = await alvo.chamar(contrato.FERRAMENTA_BLOQUEIO, {"bloqueio": _novo(), "operacoes": [k_parado]})
        primeiras["adiado"] = await alvo.chamar("enviar_lembrete", adiado["argumentos"], k_adiado)
        await anyio.sleep(float(adiado["segundos"]) + 0.5)
        repetidas = {
            "feito": await alvo.chamar("enviar_lembrete", args, k_feito),
            "adiado": await alvo.chamar("enviar_lembrete", adiado["argumentos"], k_adiado),
            "parado": await alvo.chamar("enviar_lembrete", adiado["argumentos"], k_parado),
        }
        itens = (await alvo.chamar("listar_lembretes", {})).resultado.structured_content["itens"]  # type: ignore[union-attr]
    assert aviso.resultado.structured_content["operacoes"] == [{"operacao": k_parado, "estado": "parada"}]  # type: ignore[union-attr]
    assert _texto(primeiras["feito"]) == _texto(repetidas["feito"]) == "Lembrete enviado (lembrete-1)."
    assert _texto(primeiras["adiado"]) == "O lembrete sai em 1 segundos."
    assert _texto(repetidas["adiado"]) == "Este pedido ja foi feito (lembrete-2); nada foi feito de novo."
    assert _texto(repetidas["parado"]) == (
        "Este pedido foi parado pelo aviso de bloqueio antes de terminar; nada foi feito de novo."
    )
    assert [_sucesso(repetidas[k]) for k in ("feito", "adiado", "parado")] == [True, True, False]
    numeros = {"feito": k_feito, "adiado": k_adiado, "parado": k_parado}
    recibos = {k: _recibo(r, numeros[k]) for k, r in repetidas.items()}
    assert {k: (r or {}).get("estado") for k, r in recibos.items()} == {
        "feito": "concluida",
        "adiado": "concluida",
        "parado": "parada",
    }
    assert all(r is not None and r["repetida"] is True for r in recibos.values())
    assert itens == ["lembrete-1", "lembrete-2"]


async def test_o_mesmo_aviso_repetido_da_as_mesmas_respostas_mesmo_depois_de_o_estado_mudar() -> None:
    """SELO-3.6: o aviso repetido com o mesmo ``bloqueio`` da as mesmas respostas (guardadas), mesmo que o estado de um
    numero mude depois (aqui, o sistema desfaz o que o numero fez); um aviso novo diz o estado de agora. O modo
    ``aviso_sem_memoria`` do mentiroso e o defeito, e a bateria o reprova no C-3.6a. As respostas guardadas sao por
    pessoa: o mesmo pedido mandado por outra pessoa diz o que houve para ela."""
    async with no_ar(selo_nivel3.criar) as duble:
        alvo = duble.teste
        assert alvo is not None
        args = selo_nivel2.entrada_da_declaracao("enviar_lembrete", de_teste=False)["argumentos"]
        k1, k5 = _novo(), _novo()
        assert _sucesso(await alvo.chamar("enviar_lembrete", args, k1))
        pedido = {"bloqueio": _novo(), "operacoes": [k1, k5]}
        primeira = await alvo.chamar(contrato.FERRAMENTA_BLOQUEIO, pedido)
        assert (await duble.controle("/operacao", {"operacao": k1, "acao": "desfazer", "alvo": "teste"}))["ok"]
        segunda = await alvo.chamar(contrato.FERRAMENTA_BLOQUEIO, pedido)
        novo = await alvo.chamar(contrato.FERRAMENTA_BLOQUEIO, {"bloqueio": _novo(), "operacoes": [k1, k5]})
        # O aviso e guardado por pessoa (mutacao Y9 do verificador): o mesmo pedido, mandado por outra pessoa, nao
        # recebe as respostas guardadas para a bateria; k1 nunca chegou para bia. E a resposta a bia nao muda a da
        # bateria.
        pela_bia = await Lado(alvo.url, "chave-bia").chamar(contrato.FERRAMENTA_BLOQUEIO, pedido)
        terceira = await alvo.chamar(contrato.FERRAMENTA_BLOQUEIO, pedido)
    assert [_sucesso(r) for r in (primeira, segunda, novo, pela_bia, terceira)] == [True] * 5
    assert pela_bia.resultado.structured_content["operacoes"] == [  # type: ignore[union-attr]
        {"operacao": k1, "estado": "nao_recebida"},
        {"operacao": k5, "estado": "nao_recebida"},
    ]
    assert terceira.resultado.structured_content == primeira.resultado.structured_content  # type: ignore[union-attr]
    dados = [r.resultado.structured_content for r in (primeira, segunda, novo)]  # type: ignore[union-attr]
    assert dados[0]["operacoes"] == [  # type: ignore[index]
        {"operacao": k1, "estado": "concluida"},
        {"operacao": k5, "estado": "nao_recebida"},
    ]
    assert dados[1] == dados[0]
    assert dados[2]["operacoes"] == [  # type: ignore[index]
        {"operacao": k1, "estado": "desfeita"},
        {"operacao": k5, "estado": "nao_recebida"},
    ]


async def test_o_pedido_de_dados_nao_guarda_o_numero() -> None:
    """SELO-2.3: a resposta ``input_required`` nao pos nada em efeito, e o numero nao conta como recebido: ele nao fica
    guardado (``/numeros``), e a consulta depois diz ``nao_recebida``."""
    async with no_ar(selo_nivel2.criar) as duble:
        alvo = duble.teste
        assert alvo is not None
        pede = selo_nivel2.entrada_da_declaracao("enviar_lembrete", de_teste=False)["pede_dados"]["argumentos"]
        numero = _novo()
        r = await alvo.chamar("enviar_lembrete", pede, numero)
        guardados = await duble.controle("/numeros")
        consulta = await alvo.chamar(contrato.FERRAMENTA_CONSULTAR, {"operacao": numero})
        itens = (await alvo.chamar("listar_lembretes", {})).resultado.structured_content["itens"]  # type: ignore[union-attr]
    assert isinstance(r.resultado, types.InputRequiredResult), r
    assert guardados["teste"] == []
    assert _estado(consulta) == "nao_recebida"
    assert itens == []


async def test_a_escrita_lenta_termina_mesmo_se_a_chamada_for_cancelada() -> None:
    """A escrita termina mesmo se o servidor cancelar a chamada no meio, como faria um kit que cancela quando a conexao
    cai (critica A3: a resposta perdida nao vira ``nao_recebida``). O kit 2.3.0 nao cancela, e o teste da escrita
    lenta cortada mostra o caminho pelo fio; aqui o motor e chamado direto, com um prazo que vence no meio da
    escrita."""
    motor = selo_nivel2.Motor(nivel=3)
    amb, ctx = motor.ambientes["teste"], _contexto()
    lenta = selo_nivel2.entrada_da_declaracao("enviar_lembrete", de_teste=False)["escrita_lenta"]
    numero = _novo()
    with anyio.move_on_after(float(lenta["segundos"]) / 5) as prazo:
        await motor._acao(amb, ctx, "enviar_lembrete", selo_nivel2.ESPACO_REAL, dict(lenta["argumentos"]), numero)
    assert prazo.cancel_called
    reg = amb.registros[("bateria", numero)]
    assert (reg.estado, reg.objetos) == ("concluida", ["lembrete-1"])
    assert amb.espacos[selo_nivel2.ESPACO_REAL].lembretes == ["lembrete-1"]
    assert amb.aplicadas == {"enviar_lembrete": 1}


async def test_sem_numero_escreve_como_antes_e_nao_guarda_nada() -> None:
    """Pessoa de nivel 1 (sem numero): a escrita acontece, sem recibo, e nenhum numero fica guardado."""
    async with no_ar(selo_nivel3.criar) as duble:
        args = selo_nivel2.entrada_da_declaracao("enviar_lembrete", de_teste=False)["argumentos"]
        r = await Lado(duble.producao.url, "chave-ana").chamar("enviar_lembrete", args)
        numeros = await duble.controle("/numeros")
        metas = await duble.controle("/metas")
    assert _sucesso(r)
    assert contrato.CHAVE_RECIBO not in (r.resultado.meta or {})  # type: ignore[union-attr]
    assert numeros == {"producao": [], "teste": []}
    assert [contrato.CHAVE_OPERACAO in m for m in metas["producao"]] == [False]


async def test_o_duble_recebe_o_numero_so_no_meta() -> None:
    """O que o duble recebe do cliente fixado: o numero so em ``_meta``, com o nome exato, e nunca nos argumentos."""
    async with no_ar(selo_nivel2.criar) as duble:
        args = selo_nivel2.entrada_da_declaracao("enviar_lembrete", de_teste=False)["argumentos"]
        numero = _novo()
        await Lado(duble.producao.url, "chave-ana").chamar("enviar_lembrete", args, numero)
        metas = await duble.controle("/metas")
        numeros = await duble.controle("/numeros")
    (meta,) = metas["producao"]
    assert meta[contrato.CHAVE_OPERACAO] == numero
    assert {k for k in meta if not k.startswith("io.modelcontextprotocol/")} == {contrato.CHAVE_OPERACAO}
    assert [n["numero"] for n in numeros["producao"]] == [numero]


async def test_sem_as_ferramentas_do_contrato_na_lista() -> None:
    """Critica M1: o duble deixa de listar as ferramentas do contrato, todas ou so uma (para a prova de que o selo
    cai)."""
    async with no_ar(selo_nivel2.criar) as duble:
        antes = {f["name"] for f in await duble.producao.listar()}
        await duble.controle("/ajustes", {"listar_contrato": False})
        depois = {f["name"] for f in await duble.producao.listar()}
        await duble.controle("/ajustes", {"listar_contrato": True, "tirar": [contrato.FERRAMENTA_CONSULTAR]})
        sem_consultar = {f["name"] for f in await duble.producao.listar()}
        await duble.controle("/ajustes", {"tirar": []})
        de_volta = {f["name"] for f in await duble.producao.listar()}
    assert set(contrato.ferramentas_do_nivel(2)) <= antes
    assert not depois & set(contrato.FERRAMENTAS_DO_CONTRATO)
    assert depois == antes - set(contrato.FERRAMENTAS_DO_CONTRATO)
    assert sem_consultar == antes - {contrato.FERRAMENTA_CONSULTAR}
    assert de_volta == antes


def _estado(r: cliente.Resposta) -> Any:
    assert _sucesso(r), r
    return (r.resultado.structured_content or {}).get("estado")  # type: ignore[union-attr]


async def test_escrita_lenta_cortada_no_meio_termina_e_a_repeticao_nao_aplica() -> None:
    """Critica A3: quem chamou corta a conexao no meio de uma escrita lenta. O honesto termina a escrita mesmo assim;
    a consulta diz ``em_andamento`` durante e ``concluida`` depois, e a repeticao com o mesmo numero nao aplica de
    novo (recibo com ``repetida``)."""
    async with no_ar(selo_nivel3.criar) as duble:
        alvo = duble.teste
        assert alvo is not None
        lenta = selo_nivel2.entrada_da_declaracao("enviar_lembrete", de_teste=False, segundos=SEGUNDOS_DECLARADOS)[
            "escrita_lenta"
        ]
        numero = _novo()
        await alvo.mandar_e_cortar("enviar_lembrete", lenta["argumentos"], numero)
        await esperar_a_chegada(lambda: duble.chamadas("teste", "enviar_lembrete"), 0)
        await anyio.sleep(DEPOIS_DO_CORTE)
        durante = _estado(await alvo.chamar(contrato.FERRAMENTA_CONSULTAR, {"operacao": numero}))
        await anyio.sleep(float(lenta["segundos"]) + 0.5)
        depois = _estado(await alvo.chamar(contrato.FERRAMENTA_CONSULTAR, {"operacao": numero}))
        feitos = (await alvo.chamar("listar_lembretes", {})).resultado.structured_content["itens"]  # type: ignore[union-attr]
        repetida = await alvo.chamar("enviar_lembrete", lenta["argumentos"], numero)
        de_novo = (await alvo.chamar("listar_lembretes", {})).resultado.structured_content["itens"]  # type: ignore[union-attr]
        contagem = await duble.controle("/contagem")
    assert (durante, depois) == ("em_andamento", "concluida")
    assert len(feitos) == 1
    assert de_novo == feitos
    recibo = _recibo(repetida, numero)
    assert recibo is not None
    assert (recibo["estado"], recibo["repetida"]) == ("concluida", True)
    assert contagem["teste"]["aplicadas"] == {"enviar_lembrete": 1}
    assert contagem["teste"]["chamadas"]["enviar_lembrete"] == 2


PRAZO_DOS_CONTROLES = 5.0
"""Na prova dos controles: o prazo da chamada com a escrita lenta e o da espera dos efeitos adiados."""
DEMORA_LONGA = 20
"""No honesto, os segundos da escrita lenta e do efeito adiado na prova dos controles: bem mais que
``PRAZO_DOS_CONTROLES``, entao so o controle (``/soltar``, o aviso) termina a espera dentro do prazo. E curto para o
teste nao ficar preso se o controle falhar: o duble so para depois de terminar a escrita blindada (no pior caso,
uns ``DEMORA_LONGA`` segundos, e nao os 10 minutos de antes)."""


@pytest.mark.parametrize(
    ("fabrica", "kw"),
    [
        (selo_nivel3.criar, {}),
        (selo_mentiroso.criar, {"modo": "ignora_bloqueio"}),
        (selo_mentiroso.criar, {"modo": "escreve_escondido"}),
    ],
    ids=["honesto", "ignora_bloqueio", "escreve_escondido"],
)
async def test_os_controles_da_bateria_tiram_a_espera_sem_mudar_o_que_o_caso_mede(
    fabrica: Callable[..., Starlette], kw: dict[str, Any]
) -> None:
    """``Controle``. O efeito adiado que o aviso parou termina na hora: ``adiados`` zera e o efeito nunca acontece. O
    que o aviso nao parou (``ignora_bloqueio``) e o que o sistema poe mesmo dizendo ``parada`` (``escreve_escondido``)
    so acontecem no fim dos segundos marcados: logo depois do aviso o rastro ainda nao mudou, e ``esperar_os_adiados``
    so volta depois do efeito, passados os segundos. Por isso a bateria espera antes de ler o rastro do C-3.6b: lido
    sem a espera, o rastro deixaria o ``escreve_escondido`` passar (mutacao VT1 do verificador).

    E ``/soltar`` termina a escrita lenta antes da hora, com a escrita aplicada como sempre. No honesto, os segundos da
    escrita lenta e do efeito adiado (``DEMORA_LONGA``) sao bem maiores que ``PRAZO_DOS_CONTROLES``: so o controle
    termina a espera dentro do prazo. Nos mentirosos, o efeito adiado vai com os segundos declarados da bateria."""
    honesto = fabrica is selo_nivel3.criar
    async with no_ar(fabrica, **kw) as duble:
        alvo = duble.teste
        assert alvo is not None
        controle = Controle(duble, "teste")
        entrada = selo_nivel2.entrada_da_declaracao("enviar_lembrete", de_teste=False)
        lembretes = {"ferramenta": "listar_lembretes", "argumentos": {}, "conta": "/itens"}
        bateria = Bateria(duble.producao, alvo, selo_nivel2.declaracao(endereco_teste=alvo.url), 3, controle)
        # O efeito adiado e o aviso que o para (ou que diz parada sem parar).
        segundos = DEMORA_LONGA if honesto else SEGUNDOS_DECLARADOS
        adiado, k_adiado = entrada["efeito_adiado"]["argumentos"] | {"daqui_a": segundos}, _novo()
        inicio = anyio.current_time()
        assert _sucesso(await alvo.chamar("enviar_lembrete", adiado, k_adiado))
        assert (await duble.controle("/contagem"))["teste"]["adiados"] == 1
        aviso = await alvo.chamar(contrato.FERRAMENTA_BLOQUEIO, {"bloqueio": _novo(), "operacoes": [k_adiado]})
        logo_depois_do_aviso = await bateria.rastro(lembretes)
        await controle.esperar_os_adiados(PRAZO_DOS_CONTROLES)
        esperou = anyio.current_time() - inicio
        depois_da_espera = await bateria.rastro(lembretes)
        # A escrita lenta, terminada por /soltar.
        lenta, k_lento = entrada["escrita_lenta"]["argumentos"] | {"demora": DEMORA_LONGA}, _novo()
        respostas: list[cliente.Resposta] = []
        chegaram = await controle.chegadas("enviar_lembrete")
        async with anyio.create_task_group() as tg:
            tg.start_soon(
                lambda: _guardar(respostas, alvo.chamar("enviar_lembrete", lenta, k_lento, prazo=PRAZO_DOS_CONTROLES))
            )
            await esperar_a_chegada(lambda: controle.chegadas("enviar_lembrete"), chegaram)
            durante = _estado(await alvo.chamar(contrato.FERRAMENTA_CONSULTAR, {"operacao": k_lento}))
            assert (await duble.controle("/soltar", {"operacao": k_lento}))["ok"]
        consulta = _estado(await alvo.chamar(contrato.FERRAMENTA_CONSULTAR, {"operacao": k_lento}))
        no_fim = await bateria.rastro(lembretes)
        sem_lenta = await duble.controle("/soltar", {"operacao": k_lento})
    assert aviso.resultado.structured_content["operacoes"] == [{"operacao": k_adiado, "estado": "parada"}]  # type: ignore[union-attr]
    assert logo_depois_do_aviso == 0  # nenhum efeito na hora do aviso, nem o que sai no fim dos segundos
    if honesto:
        assert depois_da_espera == 0
    else:
        assert depois_da_espera == 1
        assert esperou >= segundos - 0.05  # o efeito saiu no fim dos segundos marcados, e nao antes
    # A chamada lenta terminou dentro do prazo (bem antes da DEMORA_LONGA): foi o /soltar.
    assert _sucesso(respostas[0])
    assert (durante, consulta) == ("em_andamento", "concluida")
    assert no_fim == depois_da_espera + 1
    assert sem_lenta == {"ok": False}


async def _guardar(lista: list[cliente.Resposta], chamada: Awaitable[cliente.Resposta]) -> None:
    lista.append(await chamada)


@pytest.mark.parametrize(
    ("modo", "nome", "argumentos"),
    [
        ("consulta_muda", contrato.FERRAMENTA_CONSULTAR, {"operacao": "3f0c9a52-6d1e-4c41-9b7a-2f6c1e0d8a44"}),
        (
            "aviso_mudo",
            contrato.FERRAMENTA_BLOQUEIO,
            {"bloqueio": "0b9f5e8e-3c0a-4b8e-9a51-6f1c2d3e4a5b", "operacoes": ["3f0c9a52-6d1e-4c41-9b7a-2f6c1e0d8a44"]},
        ),
    ],
)
async def test_os_mudos_nao_respondem_e_soltam_o_servidor_quando_quem_chamou_desiste(
    modo: str, nome: str, argumentos: dict[str, Any]
) -> None:
    """O modo mudo nunca responde, mas nao prende o servidor: quando quem chamou desiste e fecha a conexao, a chamada
    termina do lado do duble, e o servidor para logo (sem isso, a parada do servidor esperaria a resposta que nunca
    vem)."""
    with anyio.fail_after(30):
        async with no_ar(selo_mentiroso.criar, modo=modo) as duble:
            assert duble.teste is not None
            r = await duble.teste.chamar(nome, argumentos, prazo=0.5)
            contagem = await duble.controle("/contagem")
    assert r.tipo == "duvida", r
    assert contagem["teste"]["chamadas"] == {nome: 1}


@pytest.mark.parametrize("modo", ["estado_com_instrucao", "membro_com_instrucao", "resposta_grande"])
async def test_as_respostas_ruins_saem_do_duble_como_o_modo_diz(modo: str) -> None:
    """O que a consulta (G3) tem de recusar ou ignorar chega mesmo pelo fio: a instrucao no ``estado`` (fora do
    vocabulario), a instrucao num membro a mais (com o resto certo) e a resposta maior que o teto do contrato."""
    async with no_ar(selo_mentiroso.criar, modo=modo) as duble:
        assert duble.teste is not None
        numero = _novo()
        r = await duble.teste.chamar(contrato.FERRAMENTA_CONSULTAR, {"operacao": numero}, teto=1024 * 1024)
    assert _sucesso(r), r
    dados = r.resultado.structured_content  # type: ignore[union-attr]
    assert isinstance(dados, dict)
    assert (dados["contrato"], dados["operacao"]) == (contrato.CONTRATO, numero)
    if modo == "estado_com_instrucao":
        assert selo_mentiroso.INSTRUCAO in dados["estado"]
        assert dados["estado"] not in contrato.ESTADOS
    elif modo == "membro_com_instrucao":
        assert dados["estado"] == "nao_recebida"
        assert dados["nota"] == selo_mentiroso.INSTRUCAO
    else:
        assert dados["estado"] == "nao_recebida"
        assert len(json.dumps(dados).encode()) > contrato.TETO_RESPOSTA_BYTES


async def test_modo_do_mentiroso_muda_pela_rota_e_o_honesto_nao_tem_modos() -> None:
    async with no_ar(selo_mentiroso.criar, modo="duplica") as duble:
        async with httpx2.AsyncClient() as http:
            assert (await http.post(duble.base + "/modo", json={"modo": "nao_queima"})).status_code == 200
            assert (await http.post(duble.base + "/modo", json={"modo": "inventado"})).status_code == 400
        assert getattr(duble.motor, "modo", None) == "nao_queima"
    async with no_ar(selo_nivel2.criar) as duble, httpx2.AsyncClient() as http:
        assert (await http.post(duble.base + "/modo", json={"modo": "duplica"})).status_code == 400


_FABRICANTES = [selo_nivel2.criar, selo_nivel3.criar, selo_mentiroso.criar]


def test_os_tres_fabricantes_tem_os_mesmos_parametros() -> None:
    """Os tres dubles se montam do mesmo jeito: os parametros de ``selo_nivel2.criar``, menos ``nivel`` (fixo no de
    nivel 3) e mais ``modo`` (so no mentiroso)."""

    def nomes(fabrica: Callable[..., Starlette]) -> set[str]:
        return set(inspect.signature(fabrica).parameters) - {"nivel", "modo"}

    assert nomes(selo_nivel3.criar) == nomes(selo_nivel2.criar) == nomes(selo_mentiroso.criar)
    assert "mapa_de_chaves" in nomes(selo_nivel2.criar)


@pytest.mark.parametrize("fabrica", _FABRICANTES, ids=["nivel_2", "nivel_3", "mentiroso"])
async def test_o_mapa_de_chaves_vale_nos_tres_dubles(fabrica: Callable[..., Starlette]) -> None:
    """``criar(mapa_de_chaves=...)`` troca o mapa inteiro em cada duble: a chave do mapa entra, com a pessoa dada, e
    as chaves de sempre (as de ``exemplos.comum`` e ``CHAVES_DO_SELO``) deixam de entrar."""
    async with no_ar(fabrica, mapa_de_chaves={"chave-so-deste-teste": "outra"}) as duble:
        assert duble.teste is not None
        nomes = {f["name"] for f in await Lado(duble.teste.url, "chave-so-deste-teste").listar()}
        with pytest.raises(cliente.ListagemRecusada):
            await Lado(duble.teste.url, CHAVE).listar()
        r = await Lado(duble.teste.url, "chave-so-deste-teste").chamar(
            contrato.FERRAMENTA_CONSULTAR, {"operacao": _novo()}
        )
        contagem = await duble.controle("/contagem")
    assert contrato.FERRAMENTA_CONSULTAR in nomes
    assert _estado(r) == "nao_recebida"
    assert contagem["teste"]["pessoas"] == {"outra": 1}
