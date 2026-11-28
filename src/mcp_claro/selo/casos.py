"""Os casos da bateria do selo (desenho 3.3; criticas A3, A4, M5, M6 e B4; decisao 7 do coordenador na etapa 3r).

``Casos`` roda os casos de ``contrato.CASOS`` contra o alvo fechado (``selo.alvo.AlvoDoTeste``), com o que a
declaracao de teste traz, e ``Juiz`` os julga com os veredictos e as razoes fechadas do contrato. A ordem e a de
``tests/integracao/test_dubles_selo.py`` (a bateria de referencia do G1, cuja tabela ``CASOS_QUE_FALHAM`` a bateria do
produto segue): os casos de sistema, um caso apos o outro em cada ferramenta e, no fim, o C-0.4.

Regras de todos os casos:

- toda resposta do sistema passa por ``selo.respostas`` (o recibo, a consulta e a resposta ao aviso): so as categorias
  do contrato saem daqui. Os identificadores dos objetos de um recibo valido sao lidos so para conferir o C-3.2 (cada
  objeto do recibo existe e cada objeto criado esta no recibo) e nunca sao guardados;
- o rastro e lido antes e depois de toda escrita. Uma leitura so serve de "antes" da escrita seguinte quando nada foi
  chamado entre as duas (``_rastro``): a consulta de um numero novo, que um sistema com defeito pode fazer escrever, e
  sempre seguida de uma leitura nova;
- cada caso julgado tem uma tentativa. A tentativa com prazo esgotado (uma escrita sem certeza: ``duvida``) e
  excluida e contada a parte (``excluidas``), e o caso fica ``nao_testado`` com ``tentativas_excluidas``; os casos que
  dependem do numero K1 de uma tentativa excluida (C-2.2a, C-2.2c, C-2.4a, C-3.2, C-3.5, C-3.6a) ficam tambem assim, sem
  chamar o sistema. A excecao (``provadas`` em ``Juiz.julgar``): a parte que a duvida nao toca e que ja prova a falha
  julga o caso ``falhou`` mesmo assim. Sao as partes em que o rastro mostra mais aplicacoes que numeros (a repeticao
  que aplica de novo, os pares que dobram, o numero queimado que aplica) e as respostas do sistema lidas antes da
  escrita em duvida (a consulta do C-2.5 e do C-2.6a, o aviso do C-3.6a). Uma subida do rastro nao vem de uma escrita
  que ainda nao terminou; ja um rastro que nao subiu, ou uma resposta da escrita em duvida, nao provam nada;
- decisao 7 do coordenador: uma parte que o sistema nao cumpriu da ``falhou``; sem resposta do sistema (a chamada nao
  respondeu, ou uma leitura do rastro nao saiu), ``nao_testado`` com ``sem_resposta``; uma condicao que a bateria nao
  conseguiu provocar sozinha (a escrita lenta que terminou antes da resposta da consulta, o aviso que so voltou depois
  dos segundos do efeito adiado, o corte que nao saiu), ``nao_testado`` com ``condicao_nao_forcada``: a parte que
  dependia da condicao nao e julgada (None), e so uma resposta que seria errada de qualquer jeito (fora do contrato, ou
  ``nao_recebida`` de uma escrita que o rastro mostra aplicada) falha o caso. Os dois reprovam um caso obrigatorio
  (``contrato.resultado_aprova``). Uma resposta fora do contrato e uma resposta, e a parte falha;
- C-2.6a e C-2.6b esperam a chamada chegar ao sistema antes de consultar (``Chegada``): no produto, uma espera curta
  pelo relogio (``espera_fixa``); nos testes, o evento do duble. C-3.6b le o rastro so depois de passados os segundos
  declarados desde a chamada, com folga (``folga_do_efeito``): o sistema que poe o efeito no fim do prazo, mesmo dizendo
  ``parada``, e pego.

Nada daqui guarda argumento nem texto do sistema: os itens tem so caso, ferramenta (uuid da revisada), resultado,
razao e contagens (``contrato.CAMPOS_DO_ITEM``). ``Juiz.partes`` guarda, por caso, so o nome de cada parte e se ela
passou, para o relatorio dos testes.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

import anyio
import mcp_types as types

from mcp_claro.ligacao.cliente import Resposta
from mcp_claro.selo import contrato, respostas
from mcp_claro.selo.alvo import AlvoDoTeste, Lado
from mcp_claro.selo.declaracao import Declaracao, FerramentaDeclarada, Rastro

ESPERA_DA_CHEGADA: Final = 1.0
"""No produto, quanto a bateria espera, depois de mandar a escrita cortada (C-2.6a) ou a escrita lenta (C-2.6b), para
a chamada chegar ao sistema antes da consulta. Consultada antes, um sistema honesto queimaria o numero, e os defeitos
que so aparecem com a chamada ja recebida passariam."""
INTERVALO_DO_CORTE: Final = 1.0
"""C-2.6a: a segunda consulta, este tanto depois da primeira (critica A3.1: "na hora e depois de um intervalo")."""
FOLGA_DO_EFEITO: Final = 1.0
"""C-3.6b: o rastro e lido os segundos declarados mais esta folga depois da chamada."""
FOLGA_DA_ESCRITA_LENTA: Final = 10.0
"""C-2.6b: o prazo da escrita lenta e os segundos declarados mais esta folga."""

SEM_RESPOSTA: Final = frozenset({"duvida", "nao_enviado", "recusado_http"})
"""Os tipos de ``Resposta`` em que o sistema nao respondeu (decisao 7: ``sem_resposta``). ``duvida`` numa escrita e
tambem prazo esgotado: a tentativa e excluida."""

Chegada = Callable[[str, float | None], AbstractAsyncContextManager[None]]
"""``chegada(ferramenta, segundos)``: em volta do envio de uma escrita (a cortada, ou a lenta que leva ``segundos``),
espera, na saida do bloco, a chamada chegar ao sistema."""


def espera_fixa(segundos: float = ESPERA_DA_CHEGADA) -> Chegada:
    """A ``Chegada`` do produto: depois de mandar, espera ``segundos`` pelo relogio; numa escrita lenta, no maximo um
    terco dos segundos dela, para a consulta ir antes de a escrita terminar."""

    @asynccontextmanager
    async def chegada(_ferramenta: str, lenta: float | None) -> AsyncIterator[None]:
        yield
        await anyio.sleep(segundos if lenta is None else min(segundos, lenta / 3))

    return chegada


def ponteiro(dados: Any, caminho: str) -> tuple[bool, Any]:
    """O valor de um ponteiro JSON (RFC 6901) em ``dados``: (achou, valor)."""
    atual = dados
    for parte in caminho.split("/")[1:]:
        chave = parte.replace("~1", "/").replace("~0", "~")
        if isinstance(atual, dict) and chave in atual:
            atual = atual[chave]
        elif isinstance(atual, list) and chave.isdecimal() and chave.isascii() and int(chave) < len(atual):
            atual = atual[int(chave)]
        else:
            return False, None
    return True, atual


def contagem_do_rastro(valor: Any) -> int | None:
    """O que o rastro conta: o tamanho de uma lista ou um inteiro nao negativo; senao None."""
    if isinstance(valor, list):
        return len(valor)
    if isinstance(valor, int) and not isinstance(valor, bool) and valor >= 0:
        return valor
    return None


def sucesso(r: Resposta) -> bool | None:
    """A chamada deu certo (``isError`` falso); None se o sistema nao respondeu."""
    if r.tipo in SEM_RESPOSTA:
        return None
    return r.tipo == "resultado" and isinstance(r.resultado, types.CallToolResult) and not r.resultado.is_error


def recusada(r: Resposta) -> bool | None:
    """Recusada sem aplicar: erro de execucao (``isError``) ou erro JSON-RPC (SELO-2.2); None sem resposta."""
    if r.tipo in SEM_RESPOSTA:
        return None
    if r.tipo == "erro_rpc":
        return True
    return r.tipo == "resultado" and isinstance(r.resultado, types.CallToolResult) and bool(r.resultado.is_error)


def tudo(valores: Iterable[bool | None]) -> bool | None:
    """E logico de partes com tres valores: falso se algum e falso; None se algum e None; senao verdadeiro."""
    lista = list(valores)
    if any(v is False for v in lista):
        return False
    if any(v is None for v in lista):
        return None
    return True


@dataclass(frozen=True, slots=True)
class Recibido:
    """O recibo de uma acao do nivel 3: o lido por ``selo.respostas`` e os identificadores dos objetos (so para o
    C-3.2, nunca guardados)."""

    recibo: respostas.Recibo
    objetos: tuple[str, ...]


def recibo_de(r: Resposta, numero: str) -> Recibido | None:
    """O recibo valido da resposta (``selo.respostas.ler_recibo``), ou None (sem recibo, fora do contrato ou sem
    resposta)."""
    if r.tipo != "resultado" or not isinstance(r.resultado, types.CallToolResult):
        return None
    lido = respostas.ler_recibo(r.resultado, numero=numero)
    if isinstance(lido, respostas.Invalida):
        return None
    cru = respostas.recibo_do_meta(r.resultado.meta)
    objetos = tuple(str(o["id"]) for o in cru.get("objetos", ())) if isinstance(cru, dict) else ()
    return Recibido(lido, objetos)


@dataclass(frozen=True, slots=True)
class Consultado:
    """O que a consulta de um numero deu: se o sistema respondeu e, valida, a consulta (``selo.respostas``)."""

    respondeu: bool
    consulta: respostas.Consulta | None = None

    @property
    def estado(self) -> str | None:
        return None if self.consulta is None else self.consulta.estado

    def diz(self, *estados: str) -> bool | None:
        """A consulta valida diz um de ``estados``; falso se respondeu fora do contrato; None sem resposta."""
        if not self.respondeu:
            return None
        return self.consulta is not None and self.consulta.estado in estados


@dataclass(frozen=True, slots=True)
class Avisado:
    """O que o aviso de bloqueio deu: se respondeu e, valida, a resposta (``selo.respostas``)."""

    respondeu: bool
    resposta: respostas.RespostaAoAviso | None = None


# --- O juiz ----------------------------------------------------------------------------------------------------

Veredicto = Literal["cumpriu", "falhou", "nao_se_aplica", "nao_testado", "nao_observado"]


class Juiz:
    """Os itens de ``testes_selo.casos`` (``contrato.CAMPOS_DO_ITEM``), julgados pela decisao 7 do coordenador.

    ``ferramentas``: o nome de cada acao da declaracao e o uuid da revisada (o item guarda o uuid)."""

    def __init__(self, ferramentas: Mapping[str, str], nivel: int) -> None:
        self.ferramentas = dict(ferramentas)
        self.nivel = nivel
        self.itens: list[dict[str, Any]] = []
        self.partes: dict[tuple[str, str | None], dict[str, bool | None]] = {}
        self._feitos: set[tuple[str, str | None]] = set()

    def item(
        self,
        caso: str,
        nome: str | None,
        resultado: Veredicto,
        razao: str | None = None,
        *,
        tentativas: int = 0,
        excluidas: int = 0,
    ) -> None:
        chave = (caso, nome)
        if chave in self._feitos:
            raise ValueError(f"caso julgado duas vezes: {caso}")
        self._feitos.add(chave)
        self.itens.append(
            {
                "caso": caso,
                "ferramenta": None if nome is None else self.ferramentas[nome],
                "resultado": resultado,
                "razao": razao,
                "tentativas": tentativas,
                "excluidas": excluidas,
            }
        )

    def julgar(
        self,
        caso: str,
        nome: str | None,
        partes: Mapping[str, bool | None],
        *,
        sem_falha: Veredicto = "cumpriu",
        razao: str | None = None,
        excluida: bool = False,
        nao_forcada: bool = False,
        provadas: Iterable[str] = (),
    ) -> None:
        """Uma tentativa do caso, julgada por partes (todas avaliadas, sem curto-circuito): excluida (prazo esgotado
        numa escrita), ``nao_testado`` com ``tentativas_excluidas``, salvo se uma das ``provadas`` (as partes que a
        duvida nao toca) e falsa: ai ``falhou``, e a tentativa nao e excluida; uma parte falsa, ``falhou``; a condicao
        nao forcada, ``nao_testado`` com ``condicao_nao_forcada`` (as partes que dependem dela ficam None); uma sem
        resposta, ``nao_testado`` com ``sem_resposta``; senao ``sem_falha`` (``cumpriu``, ou o maximo do metodo, com a
        razao)."""
        self.partes[(caso, nome)] = dict(partes)
        if excluida:
            if any(partes.get(p) is False for p in provadas):
                self.item(caso, nome, "falhou", tentativas=1)
            else:
                self.item(caso, nome, "nao_testado", "tentativas_excluidas", tentativas=1, excluidas=1)
            return
        valor = tudo(partes.values())
        if valor is False:
            self.item(caso, nome, "falhou", tentativas=1)
        elif nao_forcada:
            self.item(caso, nome, "nao_testado", "condicao_nao_forcada", tentativas=1)
        elif valor is None:
            self.item(caso, nome, "nao_testado", "sem_resposta", tentativas=1)
        else:
            self.item(caso, nome, sem_falha, razao, tentativas=1)

    def feito(self, caso: str, nome: str | None) -> bool:
        return (caso, nome) in self._feitos

    def completar(self, razao: str) -> None:
        """O que nao foi julgado (a bateria parou no meio: teto, alvo sem a marca) fica ``nao_testado`` com
        ``razao``."""
        for caso in contrato.CASOS:
            if caso.nivel > self.nivel:
                continue
            nomes: list[str | None] = [None] if caso.escopo == "sistema" else list(self.ferramentas)
            for nome in nomes:
                if not self.feito(caso.caso, nome):
                    self.item(caso.caso, nome, "nao_testado", razao)


# --- Os casos --------------------------------------------------------------------------------------------------

DEPENDEM_DE_K1: Final = ("C-2.2a", "C-2.2c", "C-2.4a", "C-3.2", "C-3.5", "C-3.6a")
"""Os casos que usam o numero K1 do C-2.1: com a tentativa do C-2.1 excluida, eles nao rodam."""


def _canonico(valor: Any) -> str:
    return json.dumps(valor, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _unicos(lista: Sequence[Any]) -> set[str]:
    return {json.dumps(x, sort_keys=True) for x in lista}


class Casos:
    """Os casos de ``contrato.CASOS`` contra ``alvo``. ``pares``: ``BADGE_TEST_PAIRS``. ``revisadas``: o nome de toda
    revisada do sistema (a ferramenta de teste nao tem o nome de nenhuma, C-0.2). ``desde``: a hora do banco em que o
    teste comecou (a janela das horas de ``depois``)."""

    def __init__(
        self,
        alvo: AlvoDoTeste,
        declaracao: Declaracao,
        juiz: Juiz,
        *,
        pares: int,
        revisadas: Iterable[str] = (),
        desde: datetime | None = None,
        chegada: Chegada | None = None,
        intervalo_do_corte: float = INTERVALO_DO_CORTE,
        folga_do_efeito: float = FOLGA_DO_EFEITO,
    ) -> None:
        self.alvo = alvo
        self.declaracao = declaracao
        self.juiz = juiz
        self.nivel = juiz.nivel
        self.pares = max(0, pares)
        self.revisadas = frozenset(revisadas)
        self.desde = desde
        self.chegada = chegada or espera_fixa()
        self.intervalo_do_corte = intervalo_do_corte
        self.folga_do_efeito = folga_do_efeito
        self.listas: dict[Lado, dict[str, dict[str, Any]] | None] = {"alvo": None, "producao": None}
        self.marcas: tuple[bool | None, bool | None] | None = None
        self._lido: dict[tuple[str, str], Any] = {}
        self._duvidas = 0
        self._recibos_ruins: dict[str, int] = {}

    # --- Chamadas, com o que cada caso le delas ---------------------------------------------------------------

    def _esquecer(self) -> None:
        """Uma chamada que nao e leitura do rastro: a proxima leitura e nova."""
        self._lido.clear()

    async def _acao(
        self, chamado: str, argumentos: Mapping[str, Any], numero: str, *, prazo: float | None = None
    ) -> Resposta:
        self._esquecer()
        r = await self.alvo.chamar("alvo", chamado, argumentos, numero=numero, prazo=prazo)
        if r.tipo == "duvida":
            self._duvidas += 1
        com_resultado = r.tipo == "resultado" and isinstance(r.resultado, types.CallToolResult)
        if self.nivel == 3 and com_resultado and recibo_de(r, numero) is None:
            self._recibos_ruins[chamado] = self._recibos_ruins.get(chamado, 0) + 1
        return r

    async def _ler(self, ferramenta: str, argumentos: Mapping[str, Any]) -> Any:
        """O ``structuredContent`` de uma leitura declarada, ou None sem resposta. Uma leitura igual sem chamada
        nenhuma no meio e a mesma (o "depois" de uma escrita e o "antes" da seguinte)."""
        chave = (ferramenta, _canonico(dict(argumentos)))
        if chave in self._lido:
            return self._lido[chave]
        r = await self.alvo.chamar("alvo", ferramenta, argumentos)
        dados = None
        if sucesso(r) and r.resultado is not None and isinstance(r.resultado, types.CallToolResult):
            dados = r.resultado.structured_content
        self._lido[chave] = dados
        return dados

    async def _rastro(self, rastro: Rastro) -> tuple[int | None, list[Any] | None]:
        """O rastro (a contagem) e, quando ele aponta para uma lista, a lista (os objetos criados)."""
        dados = await self._ler(rastro.ferramenta, rastro.argumentos)
        if dados is None:
            return None, None
        achou, valor = ponteiro(dados, rastro.conta)
        if not achou:
            return None, None
        return contagem_do_rastro(valor), list(valor) if isinstance(valor, list) else None

    async def _contar(self, rastro: Rastro) -> int | None:
        return (await self._rastro(rastro))[0]

    async def _consultar(self, numero: str, lado: Lado = "alvo") -> Consultado:
        self._esquecer()
        r = await self.alvo.chamar(lado, contrato.FERRAMENTA_CONSULTAR, {"operacao": numero})
        if r.tipo in SEM_RESPOSTA:
            return Consultado(False)
        lido = respostas.ler_consulta(r.resultado, numero=numero, nivel=self.nivel, desde=self.desde)
        return Consultado(True, None if isinstance(lido, respostas.Invalida) else lido)

    async def _avisar(self, bloqueio: str, numeros: Sequence[str]) -> Avisado:
        self._esquecer()
        r = await self.alvo.chamar(
            "alvo", contrato.FERRAMENTA_BLOQUEIO, {"bloqueio": bloqueio, "operacoes": list(numeros)}
        )
        if r.tipo in SEM_RESPOSTA:
            return Avisado(False)
        lido = respostas.ler_bloqueio(r.resultado, bloqueio=bloqueio, numeros=numeros)
        return Avisado(True, None if isinstance(lido, respostas.Invalida) else lido)

    # --- Casos de sistema -------------------------------------------------------------------------------------

    async def sistema(self) -> None:
        """C-0.1, C-0.2 e C-0.3, e a marca do C-0.4 (antes da primeira escrita). O C-0.4 e julgado no fim
        (``alvos``)."""
        lados: tuple[Lado, Lado] = ("producao", "alvo")
        for lado in lados:
            lista = await self.alvo.listar(lado)
            self.listas[lado] = None if lista is None else {str(f.get("name")): f for f in lista}
        producao, no_alvo = self.listas["producao"], self.listas["alvo"]
        do_nivel = contrato.ferramentas_do_nivel(self.nivel)
        if producao is None or no_alvo is None:
            self.juiz.julgar("C-0.1", None, {"listadas": None, "esquemas": None})
            self.juiz.julgar(
                "C-0.2",
                None,
                {"mesmas_acoes": None, "teste_listada": None, "nome_proprio": None, "mesmo_esquema": None},
            )
        else:
            ambas = (producao, no_alvo)
            self.juiz.julgar(
                "C-0.1",
                None,
                {
                    "listadas": all(n in p for p in ambas for n in do_nivel),
                    "esquemas": all(contrato.definicao_confere(p[n]) for p in ambas for n in do_nivel if n in p),
                },
            )
            declaradas = list(self.declaracao.ferramentas)
            de_teste = [(n, self.alvo.acao_chamada(n)) for n in declaradas] if self.alvo.de_teste else []
            self.juiz.julgar(
                "C-0.2",
                None,
                {
                    "mesmas_acoes": self.alvo.de_teste
                    or all(producao.get(n) == no_alvo.get(n) is not None for n in declaradas),
                    "teste_listada": all(t in producao for _, t in de_teste),
                    "nome_proprio": all(t not in declaradas and t not in self.revisadas for _, t in de_teste),
                    "mesmo_esquema": all(
                        t not in producao
                        or producao[t].get("inputSchema") == (producao.get(n) or {}).get("inputSchema")
                        for n, t in de_teste
                    ),
                },
            )
        controle = await self._consultar(self.alvo.numero_novo(), "producao")
        self.juiz.julgar("C-0.3", None, {"nao_recebida": controle.diz("nao_recebida")})
        self.marcas = await self.alvo.conferir_marcas()

    def alvos(self, parou: str | None = None) -> None:
        """C-0.4: a producao se declarou de producao e o alvo de teste (no modo de ferramentas de teste, cada resposta
        de ferramenta de teste trouxe a marca). Sem as marcas conferidas, o caso fica para ``Juiz.completar``.

        No modo de ferramentas de teste, sem nenhuma resposta de ferramenta de teste (todas sem resposta, ou nenhuma
        saiu), a parte das marcas fica sem julgar (decisao 7: ``sem_resposta``, nunca ``falhou``); se a bateria parou
        (``parou``: o teto, por exemplo) antes de qualquer uma e as duas marcas do ``selo_alvo`` valem, o caso fica para
        ``Juiz.completar``, com a razao da parada."""
        if self.marcas is None:
            return
        producao, alvo = self.marcas
        marcas: bool | None = True
        if self.alvo.de_teste:
            if self.alvo.parou_sem_marca or not all(self.alvo.marcas):
                marcas = False
            elif not self.alvo.marcas:
                marcas = None
                if parou is not None and producao is True and alvo is True:
                    return
        self.juiz.julgar("C-0.4", None, {"producao": producao, "alvo": alvo, "marcas": marcas})

    # --- Casos por ferramenta ---------------------------------------------------------------------------------

    def _dica_idempotente(self, chamado: str) -> bool:
        definicao = (self.listas["alvo"] or {}).get(chamado) or {}
        anotacoes = definicao.get("annotations") or {}
        return bool(anotacoes.get("idempotentHint")) if isinstance(anotacoes, dict) else False

    def idempotente(self, nome: str) -> bool:
        """C-2.7 roda nesta acao: ela se declara idempotente na lista do alvo, ou a declaracao diz (critica M6)."""
        entrada = self.declaracao.ferramentas[nome]
        return entrada.idempotente is not None or self._dica_idempotente(self.alvo.acao_chamada(nome))

    async def ferramenta(self, nome: str) -> None:
        """Os casos de uma acao, na ordem da tabela (desenho 3.3)."""
        e = self.declaracao.ferramentas[nome]
        chamado = self.alvo.acao_chamada(nome)
        args, rastro = e.argumentos, e.rastro
        juiz, novo = self.juiz, self.alvo.numero_novo

        # C-2.1 e C-3.1a: aceita com o numero e o rastro sobe um. Num rastro que e lista, os novos sao os criados.
        inicio = self._duvidas
        antes, lista_antes = await self._rastro(rastro)
        k1 = novo()
        r1 = await self._acao(chamado, args, k1)
        depois, lista_depois = await self._rastro(rastro)
        k1_excluido = self._duvidas > inicio
        subiu = None if antes is None or depois is None else depois - antes == 1
        juiz.julgar("C-2.1", nome, {"aceita": sucesso(r1), "sobe_um": subiu}, excluida=k1_excluido)
        recibo1 = recibo_de(r1, k1)
        criados1 = None
        if lista_antes is not None and lista_depois is not None:
            criados1 = _unicos(lista_depois) - _unicos(lista_antes)

        # C-2.2a: a repeticao nao aplica, nao e erro se a primeira nao foi, e a consulta diz concluida. No nivel 3, o
        # recibo da repeticao diz repetida e traz os mesmos objetos.
        if k1_excluido:
            juiz.item("C-2.2a", nome, "nao_testado", "tentativas_excluidas")
        else:
            inicio = self._duvidas
            antes = await self._contar(rastro)
            r2 = await self._acao(chamado, args, k1)
            depois = await self._contar(rastro)
            ok1, ok2 = sucesso(r1), sucesso(r2)
            partes: dict[str, bool | None] = {
                "rastro": None if antes is None or depois is None else depois == antes,
                "sem_erro": None if ok1 is None or ok2 is None else (not ok1 or ok2),
                "consulta": (await self._consultar(k1)).diz("concluida"),
                "recibos": True,
                "repetida": True,
                "objetos": True,
            }
            if self.nivel == 3:
                recibo2 = recibo_de(r2, k1)
                partes["recibos"] = recibo1 is not None and recibo2 is not None
                partes["repetida"] = recibo2 is None or recibo2.recibo.repetida
                partes["objetos"] = recibo1 is None or recibo2 is None or set(recibo2.objetos) == set(recibo1.objetos)
            # O rastro que subiu prova a repeticao aplicada, mesmo com a repeticao sem resposta no prazo.
            juiz.julgar("C-2.2a", nome, partes, excluida=self._duvidas > inicio, provadas=("rastro",))

        # C-2.2b: numero novo e escrita nova (ou recusada pelo proprio merito). Numa ferramenta cuja repeticao nao
        # muda nada (critica M6), nao se aplica.
        if e.idempotente is not None or self._dica_idempotente(chamado):
            juiz.item("C-2.2b", nome, "nao_se_aplica", "sem_segundo_efeito")
        else:
            inicio = self._duvidas
            antes = await self._contar(rastro)
            r = await self._acao(chamado, args, novo())
            depois = await self._contar(rastro)
            ok, nao = sucesso(r), recusada(r)
            if antes is None or depois is None or ok is None:
                certo: bool | None = None
            else:
                certo = (bool(ok) and depois - antes == 1) or (bool(nao) and depois == antes)
            juiz.julgar("C-2.2b", nome, {"aplicada_ou_recusada": certo}, excluida=self._duvidas > inicio)

        # C-2.2c: o mesmo numero com outros argumentos e recusado, sem aplicar.
        if k1_excluido:
            juiz.item("C-2.2c", nome, "nao_testado", "tentativas_excluidas")
        else:
            inicio = self._duvidas
            antes = await self._contar(rastro)
            r = await self._acao(chamado, e.argumentos_outros, k1)
            depois = await self._contar(rastro)
            juiz.julgar(
                "C-2.2c",
                nome,
                {"recusada": recusada(r), "rastro": None if antes is None or depois is None else depois == antes},
                excluida=self._duvidas > inicio,
                provadas=("rastro",),
            )

        await self._pares(nome, chamado, args, rastro)

        # C-2.3: o pedido de dados nao pos nada em efeito.
        if e.pede_dados is not None:
            inicio = self._duvidas
            antes = await self._contar(rastro)
            r = await self._acao(chamado, e.pede_dados.argumentos, novo())
            depois = await self._contar(rastro)
            partes = {
                "input_required": None
                if r.tipo in SEM_RESPOSTA
                else isinstance(r.resultado, types.InputRequiredResult),
                "rastro": None if antes is None or depois is None else depois == antes,
            }
            juiz.julgar("C-2.3", nome, partes, sem_falha="nao_observado", excluida=self._duvidas > inicio)
        else:
            juiz.item("C-2.3", nome, "nao_se_aplica", "sem_condicao_declarada")

        # C-2.4a: a consulta diz concluida do aplicado e nao_recebida do nunca mandado, sem mudar o rastro.
        if k1_excluido:
            juiz.item("C-2.4a", nome, "nao_testado", "tentativas_excluidas")
        else:
            antes = await self._contar(rastro)
            concluida = (await self._consultar(k1)).diz("concluida")
            nao_recebida = (await self._consultar(novo())).diz("nao_recebida")
            depois = await self._contar(rastro)
            partes = {
                "concluida": concluida,
                "nao_recebida": nao_recebida,
                "rastro": None if antes is None or depois is None else depois == antes,
            }
            juiz.julgar("C-2.4a", nome, partes)

        # C-2.4b: a chamada recusada e a consulta do numero dela.
        inicio = self._duvidas
        await self._contar(rastro)
        k3 = novo()
        await self._acao(chamado, e.argumentos_recusados, k3)
        await self._contar(rastro)
        estado_k3 = await self._consultar(k3)
        juiz.julgar(
            "C-2.4b",
            nome,
            {"recusada_ou_nao_recebida": estado_k3.diz("recusada", "nao_recebida")},
            excluida=self._duvidas > inicio,
        )

        # C-2.5: o numero dado como nao recebido fica queimado. O "antes" e lido depois da consulta.
        inicio = self._duvidas
        k4 = novo()
        primeiro = await self._consultar(k4)
        antes = await self._contar(rastro)
        r = await self._acao(chamado, args, k4)
        depois = await self._contar(rastro)
        queimou = primeiro.diz("nao_recebida")
        partes = {
            "nao_recebida": queimou,
            # Sem resposta a consulta, o sistema nao disse que queimou o numero: a chamada depois nao e julgada.
            "recusada": None if queimou is None else recusada(r),
            "rastro": None if queimou is None or antes is None or depois is None else depois == antes,
        }
        # A consulta veio antes da escrita; o rastro que subiu prova o numero queimado aplicado.
        juiz.julgar("C-2.5", nome, partes, excluida=self._duvidas > inicio, provadas=("nao_recebida", "rastro"))

        await self._corte(nome, chamado, args, rastro)
        await self._lenta(nome, chamado, e, rastro)

        # C-2.7: a ferramenta idempotente, duas vezes com numeros novos; a segunda nao muda o rastro do conteudo.
        if self.idempotente(nome):
            conteudo = (e.idempotente.rastro if e.idempotente is not None else None) or rastro
            inicio = self._duvidas
            await self._contar(conteudo)
            await self._acao(chamado, args, novo())
            antes = await self._contar(conteudo)
            await self._acao(chamado, args, novo())
            depois = await self._contar(conteudo)
            juiz.julgar(
                "C-2.7",
                nome,
                {"nao_muda": None if antes is None or depois is None else depois == antes},
                sem_falha="nao_observado",
                excluida=self._duvidas > inicio,
            )
        else:
            juiz.item("C-2.7", nome, "nao_se_aplica", "sem_condicao_declarada")

        if self.nivel == 3:
            await self._nivel_3(nome, chamado, e, k1 if not k1_excluido else None, recibo1, criados1)

    async def _pares(self, nome: str, chamado: str, args: Mapping[str, Any], rastro: Rastro) -> None:
        """C-2.2d: ``pares`` pares ao mesmo tempo, cada par com um numero novo; aplicar duas vezes reprova. Com menos
        de ``contrato.PARES_MINIMOS`` pares, o caso so informa (``poucos_pares``, critica M5)."""
        if self.pares <= 0:
            self.juiz.item("C-2.2d", nome, "nao_testado", "poucos_pares")
            return
        inicio = self._duvidas
        antes = await self._contar(rastro)
        numeros = [self.alvo.numero_novo() for _ in range(self.pares)]
        async with anyio.create_task_group() as tg:
            for numero in numeros:
                for _ in range(2):
                    tg.start_soon(self._par, chamado, args, numero)
        depois = await self._contar(rastro)
        # Mais aplicacoes que numeros: algum numero foi aplicado duas vezes. A subida prova a dobra mesmo com uma das
        # chamadas sem resposta no prazo (critica: so falhou ou nao_observado); sem a subida, a duvida exclui.
        dobrou = None if antes is None or depois is None else depois - antes > self.pares
        poucos = self.pares < contrato.PARES_MINIMOS
        self.juiz.julgar(
            "C-2.2d",
            nome,
            {"nao_dobrou": None if dobrou is None else not dobrou},
            sem_falha="nao_testado" if poucos else "nao_observado",
            razao="poucos_pares" if poucos else None,
            excluida=self._duvidas > inicio,
            provadas=("nao_dobrou",),
        )

    async def _par(self, chamado: str, args: Mapping[str, Any], numero: str) -> None:
        await self._acao(chamado, args, numero)

    async def _corte(self, nome: str, chamado: str, args: Mapping[str, Any], rastro: Rastro) -> None:
        """C-2.6a (critica A3.1): a escrita com a conexao cortada logo depois de mandar; a consulta na hora e depois
        de um intervalo, o rastro e a repeticao do numero."""
        inicio = self._duvidas
        antes = await self._contar(rastro)
        k6 = self.alvo.numero_novo()
        self._esquecer()
        async with self.chegada(chamado, None):
            saiu = await self.alvo.mandar_e_cortar(chamado, args, k6)
        na_hora = await self._consultar(k6)
        await anyio.sleep(self.intervalo_do_corte)
        depois_do_intervalo = await self._consultar(k6)
        agora = await self._contar(rastro)
        aplicada = None if antes is None or agora is None else agora - antes
        await self._acao(chamado, args, k6)
        depois = await self._contar(rastro)
        respostas_do_corte = (na_hora, depois_do_intervalo)
        nunca: bool | None
        if aplicada is None:
            nunca = None
        elif not aplicada:
            nunca = True
        else:
            nunca = all(
                x.consulta is None or x.consulta.estado in ("concluida", "em_andamento", "desconhecido")
                for x in respostas_do_corte
            )
        partes = {
            "responderam": tudo(x.diz(*contrato.ESTADOS) for x in respostas_do_corte),
            "nunca_nao_recebida": nunca,
            "repeticao_nao_aplica": None if agora is None or depois is None else depois == agora,
        }
        # So a repeticao pode ficar em duvida: as consultas e o rastro do corte vieram antes dela.
        self.juiz.julgar(
            "C-2.6a",
            nome,
            partes,
            excluida=self._duvidas > inicio,
            nao_forcada=not saiu,
            provadas=("responderam", "nunca_nao_recebida", "repeticao_nao_aplica"),
        )

    async def _lenta(self, nome: str, chamado: str, e: FerramentaDeclarada, rastro: Rastro) -> None:
        """C-2.6b: a consulta durante a escrita lenta declarada da em_andamento ou desconhecido.

        Se a escrita terminou antes de a consulta responder, a condicao nao foi forcada: o que a consulta diz pode ser
        verdade (``concluida``, ``recusada``...), e o caso fica ``nao_testado`` com ``condicao_nao_forcada``. So falha
        a resposta errada de qualquer jeito: fora do contrato, ou ``nao_recebida`` de uma escrita que o rastro (ou o
        sucesso dela) mostra aplicada."""
        if e.escrita_lenta is None:
            self.juiz.item("C-2.6b", nome, "nao_se_aplica", "sem_condicao_declarada")
            return
        lenta = e.escrita_lenta
        inicio = self._duvidas
        antes = await self._contar(rastro)
        k7 = self.alvo.numero_novo()
        terminou = anyio.Event()
        respostas_da_escrita: list[Resposta] = []

        async def escrever() -> None:
            try:
                respostas_da_escrita.append(
                    await self._acao(
                        chamado, lenta.argumentos, k7, prazo=float(lenta.segundos) + FOLGA_DA_ESCRITA_LENTA
                    )
                )
            finally:
                terminou.set()

        async with anyio.create_task_group() as tg:
            async with self.chegada(chamado, float(lenta.segundos)):
                tg.start_soon(escrever)
            durante = await self._consultar(k7)
            forcada = not terminou.is_set()
        depois = await self._contar(rastro)
        certo = durante.diz("em_andamento", "desconhecido")
        nao_forcada = False
        if not forcada and certo is False:
            # A escrita terminou antes da resposta da consulta: o que a consulta disse pode ser verdade.
            nao_forcada = True
            subiu = None if antes is None or depois is None else depois > antes
            aplicada = subiu is True or (bool(respostas_da_escrita) and sucesso(respostas_da_escrita[0]) is True)
            fora_do_contrato = durante.respondeu and durante.consulta is None
            if not (fora_do_contrato or (aplicada and durante.estado == "nao_recebida")):
                certo = None
        self.juiz.julgar(
            "C-2.6b",
            nome,
            {"durante": certo},
            excluida=self._duvidas > inicio,
            nao_forcada=nao_forcada,
        )

    async def _nivel_3(
        self,
        nome: str,
        chamado: str,
        e: FerramentaDeclarada,
        k1: str | None,
        recibo1: Recibido | None,
        criados1: set[str] | None,
    ) -> None:
        args, rastro = e.argumentos, e.rastro
        juiz, novo = self.juiz, self.alvo.numero_novo

        # C-3.1a: o sucesso veio com o efeito lido de volta.
        inicio = self._duvidas
        antes = await self._contar(rastro)
        r = await self._acao(chamado, args, novo())
        depois = await self._contar(rastro)
        juiz.julgar(
            "C-3.1a",
            nome,
            {"aceita": sucesso(r), "sobe_um": None if antes is None or depois is None else depois - antes == 1},
            excluida=self._duvidas > inicio,
        )

        # C-3.1b: a recusa volta como erro, com o recibo recusada.
        inicio = self._duvidas
        antes = await self._contar(rastro)
        k8 = novo()
        r = await self._acao(chamado, e.argumentos_recusados, k8)
        depois = await self._contar(rastro)
        recibo = recibo_de(r, k8)
        partes: dict[str, bool | None] = {
            "recusada": recusada(r),
            "recibo_recusada": None
            if r.tipo in SEM_RESPOSTA
            else recibo is not None and recibo.recibo.estado == "recusada",
            "rastro": None if antes is None or depois is None else depois == antes,
        }
        juiz.julgar("C-3.1b", nome, partes, excluida=self._duvidas > inicio)

        # C-3.3: varios itens com um ruim.
        if e.varios_itens is not None:
            varios = e.varios_itens
            inicio = self._duvidas
            antes = await self._contar(rastro)
            k9 = novo()
            r = await self._acao(chamado, varios.argumentos, k9)
            depois = await self._contar(rastro)
            recibo = recibo_de(r, k9)
            itens: bool | None = None
            if antes is not None and depois is not None and r.tipo not in SEM_RESPOSTA:
                aplicados = depois - antes
                itens = recibo is not None and recibo.recibo.itens == (aplicados, varios.itens - aplicados)
            juiz.julgar("C-3.3", nome, {"recusada": recusada(r), "itens": itens}, excluida=self._duvidas > inicio)
        else:
            juiz.item("C-3.3", nome, "nao_se_aplica", "sem_condicao_declarada")

        # C-3.4: os dois campos sao independentes, e a escrita velha (stale write) e recusada como erro e nao aplicada.
        if e.conflito is not None:
            c = e.conflito
            inicio = self._duvidas
            lido = await self._ler(c.leitura.ferramenta, c.leitura.argumentos)
            achou_versao, versao = ponteiro(lido, c.leitura.versao) if lido is not None else (False, None)
            achou_campo, concorrente = (
                ponteiro(lido, c.leitura.campo_concorrente) if lido is not None else (False, None)
            )
            if not (achou_versao and achou_campo):
                juiz.julgar(
                    "C-3.4", nome, {"a_aceita": None, "independentes": None, "b_recusada": None, "rastro": None}
                )
            else:
                await self._contar(rastro)
                a = await self._acao(chamado, dict(c.argumentos) | {c.argumento_versao: versao}, novo())
                depois_de_a = await self._ler(c.leitura.ferramenta, c.leitura.argumentos)
                achou, valor = (
                    ponteiro(depois_de_a, c.leitura.campo_concorrente) if depois_de_a is not None else (False, None)
                )
                antes = await self._contar(rastro)
                b = await self._acao(chamado, dict(c.argumentos_concorrente) | {c.argumento_versao: versao}, novo())
                depois = await self._contar(rastro)
                partes = {
                    "a_aceita": sucesso(a),
                    "independentes": (valor == concorrente) if achou else None,
                    "b_recusada": recusada(b),
                    "rastro": None if antes is None or depois is None else depois == antes,
                }
                juiz.julgar("C-3.4", nome, partes, excluida=self._duvidas > inicio)
        else:
            juiz.item("C-3.4", nome, "nao_se_aplica", "sem_condicao_declarada")

        # C-3.5: a consulta do aplicado traz depois, coerente com o recibo.
        if k1 is None:
            juiz.item("C-3.5", nome, "nao_testado", "tentativas_excluidas")
        else:
            consulta = await self._consultar(k1)
            coerente = consulta.diz("concluida")
            if coerente is not None:
                coerente = coerente and recibo1 is not None and recibo1.recibo.estado == "concluida"
            juiz.julgar(
                "C-3.5",
                nome,
                {"depois": None if not consulta.respondeu else consulta.consulta is not None, "coerente": coerente},
            )

        # C-3.6a: o aviso com um numero concluido e um nunca mandado; repetido, as mesmas respostas; a chamada com o
        # numero nunca mandado e recusada, sem mudar o rastro.
        if k1 is None:
            juiz.item("C-3.6a", nome, "nao_testado", "tentativas_excluidas")
        else:
            inicio = self._duvidas
            k5, aviso = novo(), novo()
            primeira = await self._avisar(aviso, [k1, k5])
            segunda = await self._avisar(aviso, [k1, k5])
            antes = await self._contar(rastro)
            r = await self._acao(chamado, args, k5)
            depois = await self._contar(rastro)
            esperado = ((k1, "concluida"), (k5, "nao_recebida"))
            partes = {
                "estados": None
                if not primeira.respondeu
                else primeira.resposta is not None and primeira.resposta.estados == esperado,
                "repetido": None
                if not (primeira.respondeu and segunda.respondeu)
                else primeira.resposta is not None and segunda.resposta == primeira.resposta,
                # Sem resposta ao aviso, o sistema nao disse que queimou o numero: a chamada depois nao e julgada.
                "recusada": None if not primeira.respondeu else recusada(r),
                "rastro": None if not primeira.respondeu or antes is None or depois is None else depois == antes,
            }
            # Os avisos vieram antes da chamada com K5; o rastro que subiu prova o numero queimado aplicado.
            juiz.julgar(
                "C-3.6a", nome, partes, excluida=self._duvidas > inicio, provadas=("estados", "repetido", "rastro")
            )

        await self._adiado(nome, chamado, e, rastro)

        # C-3.2: todo recibo das acoes desta ferramenta tem a forma e combina com o erro; numa criacao (rastro que e
        # uma lista), o recibo da primeira chamada traz o identificador de cada objeto criado, e so deles.
        if k1 is None:
            juiz.item("C-3.2", nome, "nao_testado", "tentativas_excluidas")
        else:
            objetos_ok = True
            if recibo1 is not None and criados1 is not None:
                ditos = {json.dumps(o) for o in recibo1.objetos}
                objetos_ok = bool(criados1) and ditos == criados1
            recibos_ok = self._recibos_ruins.get(chamado, 0) == 0 and recibo1 is not None
            juiz.julgar("C-3.2", nome, {"recibos": recibos_ok, "objetos": objetos_ok})

    async def _adiado(self, nome: str, chamado: str, e: FerramentaDeclarada, rastro: Rastro) -> None:
        """C-3.6b: com o efeito adiado declarado, o aviso chega antes do efeito, da parada, e o efeito nao acontece. O
        rastro e lido so depois de passados os segundos declarados desde a chamada, com folga."""
        if e.efeito_adiado is None:
            self.juiz.item("C-3.6b", nome, "nao_se_aplica", "sem_condicao_declarada")
            return
        adiado = e.efeito_adiado
        inicio = self._duvidas
        antes = await self._contar(rastro)
        k10 = self.alvo.numero_novo()
        chamou_em = anyio.current_time()
        r = await self._acao(chamado, adiado.argumentos, k10)
        recibo = recibo_de(r, k10)
        aviso = await self._avisar(self.alvo.numero_novo(), [k10])
        # O aviso que so voltou depois dos segundos declarados pode ter chegado depois do efeito: a condicao nao foi
        # forcada (o relogio do efeito comeca quando a chamada chega, depois de ``chamou_em``).
        forcada = anyio.current_time() - chamou_em < adiado.segundos
        await anyio.sleep(max(0.0, chamou_em + adiado.segundos + self.folga_do_efeito - anyio.current_time()))
        self._esquecer()
        depois = await self._contar(rastro)
        consulta = await self._consultar(k10)
        em_andamento = None if r.tipo in SEM_RESPOSTA else recibo is not None and recibo.recibo.estado == "em_andamento"
        if not forcada:
            self.juiz.julgar(
                "C-3.6b",
                nome,
                {"recibo_em_andamento": em_andamento, "aviso_parada": None, "rastro": None, "consulta_parada": None},
                excluida=self._duvidas > inicio,
                nao_forcada=True,
            )
            return
        parada = None
        if aviso.respondeu:
            parada = aviso.resposta is not None and aviso.resposta.estado_de(k10) == "parada"
        # Sem resposta ao aviso, o sistema nao disse que parou: o efeito e a consulta depois nao sao julgados.
        partes = {
            "recibo_em_andamento": em_andamento,
            "aviso_parada": parada,
            "rastro": None if not aviso.respondeu or antes is None or depois is None else depois == antes,
            "consulta_parada": None if not aviso.respondeu else consulta.diz("parada"),
        }
        self.juiz.julgar("C-3.6b", nome, partes, excluida=self._duvidas > inicio)
