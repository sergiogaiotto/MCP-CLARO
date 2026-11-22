"""Regras do despachante: reconfere cada leitura, abre a chave e chama o sistema uma vez.

A porta interna de leituras so atende ferramenta da classe "le" do cadastro
revisado e da ordem de servico. Acao pedida por aqui e recusada e vai ao
extrato, mesmo que a entrada tenha sido alterada de proposito.

Regras da Fase 1 (``_conferir_regras`` e ``_chave_da_pessoa``): bloqueios ativos e
porta de saida antes de abrir a chave; chave por pessoa renovada antes do envio,
fora de qualquer conexao aberta; 401 no envio nao e reenviado.

Fase 3 (servidores de terceiros):

- toda conversa com um sistema sai por ``ligacao(sistema)`` (``ligacao.ponto.ligacao_para``): proprio pelo cliente
  fixado; terceiro pela sala de visitas. Sem sala, nenhum pedido MCP vai ao terceiro (``sala_fora`` no extrato);
  so a troca e a renovacao da chave (OAuth) saem do despachante, para o endereco de troca de chave do terceiro,
  conferido antes. O cliente local para terceiro so entra por injecao, em desenvolvimento e teste;
- a vitrine de terceiro so vale com a chave da Fase 3 ligada ou para pessoa de teste (``terceiros_ligados``);
- pedido do sistema no meio da leitura (formulario, link, IA, pastas, nova volta, fio antigo) nunca passa: a
  leitura devolve um erro nosso e descarta o que o sistema mandou depois (D12);
- a resposta de estranhos (terceiro ou ferramenta marcada "traz texto de fora") sai emoldurada
  (``regras.estranhos.marcar``), depois da medida, com a conta de partes retiradas feita antes dela;
- todo texto nosso leva ``PREFIXO_META + "motivo"`` no ``_meta``; texto do sistema nunca leva (contrato de
  ``regras.sinais.veio_do_sistema``). Nenhum ``_meta`` do sistema passa;
- resultado fundo demais (partes demais umas dentro das outras) vira erro nosso legivel, nunca erro interno;
- a revisao do servidor (``revisar_servidor``) le o servidor de novo, com a chave de leitura das declaracoes
  guardada, e so publica o que foi revisto e nao mudou;
- nas declaracoes, a falha que e do MCP-CLARO (sala de visitas fora, sala cheia dita pelo processo pai dela, cofre
  que nao abre a chave) e ``DeclaracoesNaoConferidas``, com motivo proprio: as rotinas nao a contam como "sem
  resposta" do servidor (a falha nossa no banco ou no grupo de conexoes sobe como veio, e a porta interna a responde
  com 503 ``conferencia_nao_feita``, contrato K5). O que o servidor declarou e o banco nao guarda (nome longo demais,
  texto que o banco nao escreve, erro de dado do banco ao gravar) conta contra o servidor
  (``DeclaracoesIndisponiveis``). ``ferramentas_vistas`` e gravada em ordem fixa (pelo nome): duas leituras ao mesmo
  tempo travam as linhas na mesma ordem e nao dao impasse no banco;
- pedido do sistema barrado fora de uma chamada, na leitura das declaracoes ou na sondagem (contrato K4), vai ao
  extrato sem pessoa (``declaracoes_sistema_pediu_dados``, ``sondagem_sistema_pediu_dados``; as categorias em
  ``dados.pedidos``) e ao contador ``pedidos_do_sistema_barrados``, com ``onde``. A lista lida continua a mesma;
- a sondagem separa o desfecho: so "versao" conta como versao antiga, e um terceiro ativo e revisado que deixa de
  falar a 2026-07-28 vai a quarentena (D29).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import mcp_types as types
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.cadastro import quarentena as cadastro_quarentena
from mcp_claro.cadastro import repositorio
from mcp_claro.cadastro.impressao import impressao, impressao_servidor
from mcp_claro.cadastro.modelos import FerramentaVitrine, Sistema
from mcp_claro.cadastro.quarentena import MOTIVO_QUARENTENA_VERSAO
from mcp_claro.cofre import ErroCofre
from mcp_claro.despachante import chaves, medida, renovacao
from mcp_claro.despachante.chaves import Cifra
from mcp_claro.despachante.cifra_origem import ContextoSemSistema
from mcp_claro.extrato import registrar
from mcp_claro.ligacao import cliente
from mcp_claro.ligacao.cliente import PEDIDO_FUNDO_DEMAIS, Listagem, Resposta, Segredo, Sondagem, TipoResultado
from mcp_claro.ligacao.ponto import (
    LINHA_SALA_FORA,
    Ligacao,
    ligacao_para,
    linha_da_sala,
    pedidos_da_resposta,
    pedidos_legiveis,
)
from mcp_claro.ligacao.sala_contrato import SALA_SEM_VAGA, FalhaNaSala, SalaSemVaga
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.regras import bloqueios, estado, estranhos, programas
from mcp_claro.regras.escondidos import marcar_escondidos
from mcp_claro.saida import travas
from mcp_claro.saida.quarentena import por_em_quarentena
from mcp_claro.versao import CAPACIDADES_DO_CLIENTE, MOTIVO_EXIGE_CAPACIDADE, PREFIXO_META, VERSAO_MCP

log = logger("mcp_claro.despachante")

Ligador = Callable[[Sistema], Ligacao]
"""Por onde falar com um sistema: ``ligacao.ponto.ligacao_para`` com o ambiente e a sala do despachante."""

MOTIVO: Final = PREFIXO_META + "motivo"
META_PEDIDOS: Final = PREFIXO_META + "pedidos"

TETO_NIVEIS_RESPOSTA: Final = 199
"""Niveis de listas e objetos, um dentro do outro, que um resultado pode ter ao voltar a entrada. E o que o leitor
de JSON do kit aceita numa mensagem (200 niveis, contando o envelope): mais fundo, nem o aplicativo leria."""

MENSAGEM_CHAVE_INDISPONIVEL: Final = (
    "Nao deu para abrir a chave deste sistema no cofre agora. Nada foi enviado. Tente de novo em instantes."
)
_COFRE_FALHOU: Final = (ErroCofre, ContextoSemSistema)
"""O cofre nao abriu a chave (fora do ar, trancado, acesso negado, chave Transit indisponivel) ou a cifra por origem
nao achou o sistema: a leitura e recusada com frase nossa, nunca erro interno."""

TETO_FORNECEDOR: Final = (3, 200)
TETO_PUBLICADAS: Final = 500
TETO_NOME_NO_EXTRATO: Final = 128
TETO_FERRAMENTAS_NO_EXTRATO: Final = 50
TETO_BYTES_NOME: Final = 1000
"""Maior nome de ferramenta, em bytes UTF-8, que ``ferramentas_vistas`` guarda: bem abaixo do teto de uma entrada do
indice da chave primaria ``(sistema_id, nome)`` (cerca de 2,7 kB, mesmo com letras que nao se comprimem). Nome de
mais de 128 letras que cabe aqui continua guardado: o ADMIN o ve, e a revisao de um terceiro o recusa
(``telas.admin.TETO_NOME_FERRAMENTA``)."""


class DeclaracoesIndisponiveis(Exception):
    """O sistema nao entregou as declaracoes (fora do ar, recusou, resposta invalida): 502 na porta interna."""


class DeclaracoesNaoConferidas(DeclaracoesIndisponiveis):
    """A falha e do MCP-CLARO, nao do sistema: a sala de visitas fora (``sala_fora``), a sala cheia dita pelo processo
    pai dela (``sala_sem_vaga``) ou o cofre que nao abriu a chave das declaracoes (``chave_indisponivel``). A porta
    interna responde 503 com ``motivo``, e as rotinas nao contam a rodada como "sem resposta" do servidor (critica
    A5)."""

    def __init__(self, motivo: str, mensagem: str) -> None:
        super().__init__(mensagem)
        self.motivo = motivo


class LeituraRecusada(Exception):
    def __init__(self, motivo: str, mensagem: str) -> None:
        self.motivo = motivo
        self.mensagem = mensagem
        super().__init__(mensagem)


@dataclass(frozen=True, slots=True)
class PedidoLeitura:
    pessoa_id: uuid.UUID
    aplicativo_id: uuid.UUID
    ordem_id: uuid.UUID
    ferramenta_id: uuid.UUID
    argumentos: dict[str, Any]
    trace_id: str | None = None

    @classmethod
    def de_json(cls, dados: dict[str, Any]) -> PedidoLeitura:
        return cls(
            pessoa_id=uuid.UUID(str(dados["pessoa_id"])),
            aplicativo_id=uuid.UUID(str(dados["aplicativo_id"])),
            ordem_id=uuid.UUID(str(dados["ordem_id"])),
            ferramenta_id=uuid.UUID(str(dados["ferramenta_id"])),
            argumentos=dict(dados.get("argumentos") or {}),
            trace_id=dados.get("trace_id"),
        )


def capacidades_exigidas(dados: Any) -> list[str]:
    """Nomes das capacidades que o sistema disse faltar, filtrados pela lista conhecida. Texto dele nao passa."""
    exigidas = dados.get("requiredCapabilities") if isinstance(dados, dict) else None
    if not isinstance(exigidas, dict):
        return []
    return [nome for nome in CAPACIDADES_DO_CLIENTE if nome in exigidas]


def _resultado_de_erro(texto: str, motivo: str) -> dict[str, Any]:
    """Texto nosso: sempre com o motivo no ``_meta`` (nunca vira sinal, nunca e emoldurado)."""
    return {"content": [{"type": "text", "text": texto}], "isError": True, "_meta": {MOTIVO: motivo}}


_TIPOS_DA_LISTAGEM: Final[dict[str, TipoResultado]] = {
    "nao_enviado": "nao_enviado",
    "duvida": "duvida",
    "recusado_http": "recusado_http",
}


def resposta_da_listagem(erro: Exception) -> Resposta:
    """A falha da listagem de um sistema como ``Resposta``, para achar a linha ``sala_*`` (``linha_da_sala``).

    A sala diz o que houve em ``FalhaNaSala`` (``tipo``, ``excecao`` e, num desvio ou numa chave recusada,
    ``status_http``). O cliente local levanta a propria excecao: ``EnderecoRecusado`` (ou o nome que nao resolve)
    antes de enviar, ``ListagemRecusada`` com o ``status_http`` quando o sistema recusou (3xx, 401, 403), e o resto
    depois de enviar (duvida). Com o ultimo status de recusa, o tipo e ``recusado_http`` (um 3xx e desvio)."""
    status = getattr(erro, "status_http", ())
    status_http = tuple(s for s in status if isinstance(s, int)) if isinstance(status, tuple) else ()
    tipo: TipoResultado
    if isinstance(erro, FalhaNaSala):
        tipo, excecao = _TIPOS_DA_LISTAGEM.get(erro.tipo, "duvida"), erro.excecao
    else:
        tipo = "nao_enviado" if isinstance(erro, cliente.EnderecoRecusado | OSError) else "duvida"
        excecao = type(erro).__name__
    if status_http and cliente.recusado_por_status(status_http[-1]):
        tipo = "recusado_http"
    # So a linha da sala le esta Resposta.
    return Resposta(tipo, excecao=excecao, status_http=status_http)


_FALHAS: Final[dict[str, str]] = {
    "nao_enviado": "nada foi enviado",
    "duvida": "a resposta nao chegou inteira ou nao veio num formato que o MCP-CLARO consiga ler",
    "erro_rpc": "ele devolveu um erro",
    "recusado_http": "ele recusou o pedido",
    "resultado": "a resposta veio vazia",
}


def _falha(resposta: Resposta, sistema: Sistema) -> dict[str, Any]:
    nome = sistema.nome
    sala = linha_da_sala(resposta, sistema)
    if sala == "sala_fora":
        return _resultado_de_erro(
            f"A sala de visitas nao esta disponivel; nada foi enviado ao servidor de terceiros {nome}.", sala
        )
    if sala == "sala_endereco_recusado":
        return _resultado_de_erro(
            f"O endereco do servidor de terceiros {nome} nao e publico ou nao usa https; nada foi enviado.", sala
        )
    if sala == "sala_desvio_recusado":
        return _resultado_de_erro(
            f"O servidor de terceiros {nome} pediu para mandar o pedido a outro endereco; o MCP-CLARO nao seguiu."
            " Nada foi lido.",
            sala,
        )
    if sistema.de_terceiro and resposta.tipo == "nao_enviado" and resposta.excecao == SALA_SEM_VAGA:
        return _resultado_de_erro(
            f"A sala de visitas estava cheia; nada foi enviado ao servidor de terceiros {nome}. Tente de novo em"
            " instantes.",
            SALA_SEM_VAGA,
        )
    if resposta.excecao == PEDIDO_FUNDO_DEMAIS:
        return _resultado_de_erro(
            f"O pedido tem partes demais umas dentro das outras; o MCP-CLARO nao consegue envia-lo ao sistema {nome}."
            " Nada foi enviado.",
            PEDIDO_FUNDO_DEMAIS,
        )
    legivel = _FALHAS.get(resposta.tipo, "a resposta nao chegou")
    return _resultado_de_erro(
        f"O sistema {nome} nao respondeu a leitura ({resposta.tipo}: {legivel}).", "falha_" + resposta.tipo
    )


def _resultado_fundo(sistema: Sistema) -> dict[str, Any]:
    return _resultado_de_erro(
        f"A resposta do sistema {sistema.nome} tem partes demais umas dentro das outras (mais de"
        f" {TETO_NIVEIS_RESPOSTA} niveis); o MCP-CLARO nao a repassa.",
        "resposta_funda_demais",
    )


def _resultado_do_sistema(
    modelo: types.CallToolResult | types.InputRequiredResult, ferramenta: FerramentaVitrine
) -> tuple[dict[str, Any], bool]:
    """O resultado que veio do sistema, pronto para o aplicativo: medida, sem ``_meta`` do sistema e, se for texto de
    estranhos, emoldurado. Devolve (resultado, emoldurado). Fundo demais vira erro nosso, nunca erro interno."""
    sistema = ferramenta.sistema
    try:
        bruto = modelo.model_dump(by_alias=True, exclude_none=True, mode="json")
        retiradas = estranhos.retiradas_pela_medida(bruto)
        resultado = medida.aplicar(bruto, ferramenta.campos_pessoais, anotacoes=not sistema.de_terceiro)
        resultado.pop("resultType", None)
        resultado.pop("_meta", None)  # o _meta do sistema nunca passa ao aplicativo
        emoldurado = ferramenta.de_estranhos
        if emoldurado:
            resultado = estranhos.marcar(
                resultado, sistema=sistema.nome, de_terceiro=sistema.de_terceiro, retiradas=retiradas
            )
    except ValueError, RecursionError:
        # medida.RespostaFundaDemais, "Maximum serialization depth exceeded" do pydantic e qualquer pilha estourada.
        return _resultado_fundo(sistema), False
    if medida.profundidade(resultado) > TETO_NIVEIS_RESPOSTA:
        return _resultado_fundo(sistema), False
    return resultado, emoldurado


@dataclass(frozen=True, slots=True)
class Leitura:
    """O que a leitura devolve e o que ela grava, decidido sem banco (``interpretar``)."""

    resultado: dict[str, Any]
    tipo: str
    campos: dict[str, object]
    dados: dict[str, Any]
    pedidos: tuple[str, ...] = ()
    sala: str | None = None
    marcar_vencida: bool = False
    emoldurado: bool = False
    contadores: tuple[tuple[str, dict[str, str]], ...] = field(default_factory=tuple)


def interpretar(resposta: Resposta, ferramenta: FerramentaVitrine, *, com_chave_da_pessoa: bool) -> Leitura:
    """Da resposta do sistema (ou da sala) ao resultado que volta a entrada e a linha do extrato. Puro."""
    sistema = ferramenta.sistema
    pedidos = pedidos_da_resposta(resposta)
    sala = linha_da_sala(resposta, sistema)
    marcar_vencida = False
    emoldurado = False
    if pedidos:
        # D12: pedido no meio (mesmo pelo fio antigo, seguido de resultado): o resultado e descartado.
        resultado = _resultado_de_erro(
            f"O sistema {sistema.nome} pediu {pedidos_legiveis(pedidos)} no meio da leitura. O MCP-CLARO recusou:"
            " nao repassa pedidos de dados, links ou confirmacoes de um sistema.",
            "sistema_pediu_dados",
        )
        resultado["_meta"][META_PEDIDOS] = list(pedidos)
    elif resposta.tipo == "resultado" and resposta.resultado is not None:
        resultado, emoldurado = _resultado_do_sistema(resposta.resultado, ferramenta)
    elif resposta.tipo == "recusado_http" and 401 in resposta.status_http and com_chave_da_pessoa:
        marcar_vencida = True
        resultado = _resultado_de_erro(
            f"O sistema {sistema.nome} recusou a sua chave. Nada foi reenviado. Tente de novo: o MCP-CLARO "
            "renova a chave antes; se nao der, autorize de novo nas telas.",
            "chave_recusada",
        )
    elif (
        resposta.tipo == "erro_rpc"
        and resposta.erro is not None
        and resposta.erro.code == types.MISSING_REQUIRED_CLIENT_CAPABILITY
    ):
        # O sistema so responde se puder pedir dados ao aplicativo (o MCP-CLARO, como cliente, nao declara
        # nenhuma capacidade). A entrada decide: -32021 se o aplicativo tambem nao declarou (so sistema proprio,
        # D13), ou este erro.
        capacidades = capacidades_exigidas(resposta.erro.data)
        resultado = _resultado_de_erro(
            f"O sistema {sistema.nome} so responde pedindo dados ao aplicativo"
            f" ({', '.join(capacidades) or 'capacidade nao informada'}). O MCP-CLARO nao repassa esses pedidos.",
            MOTIVO_EXIGE_CAPACIDADE,
        )
        resultado["_meta"][PREFIXO_META + "capacidades"] = capacidades
    else:
        resultado = _falha(resposta, sistema)
    # So o _meta nosso: o do sistema saiu em _resultado_do_sistema; o da moldura e o dos erros sao nossos.
    meta: dict[str, Any] = dict(resultado.get("_meta") or {})
    meta[PREFIXO_META + "sistema"] = sistema.nome
    meta[PREFIXO_META + "selo"] = sistema.selo
    if sistema.tipo_chave == "compartilhada":
        meta[PREFIXO_META + "aviso"] = "Sistema de chave compartilhada: ele nao distingue as pessoas."
    resultado["_meta"] = meta
    contadores: list[tuple[str, dict[str, str]]] = [
        ("pedidos_do_sistema_barrados", {"tipo": p, "onde": "leitura"}) for p in pedidos
    ]
    if sala is not None:
        contadores.append((sala, {"onde": "leitura"}))
    if _sala_sem_vaga(resposta, sistema):
        contadores.append((SALA_SEM_VAGA, {"onde": "leitura"}))
    if emoldurado:
        contadores.append(("respostas_de_estranhos", {"origem": "terceiro" if sistema.de_terceiro else "proprio"}))
    if pedidos:
        tipo = "leitura_sistema_pediu_dados"
        campos: dict[str, object] = {
            "sistema": sistema.nome,
            "pedidos": pedidos_legiveis(pedidos),
            "ferramenta": ferramenta.nome_vitrine,
        }
        dados: dict[str, Any] = {"desfecho": resposta.tipo, "pedidos": list(pedidos)}
    else:
        tipo = "leitura_feita"
        campos = {"ferramenta": ferramenta.nome_vitrine}
        dados = {"desfecho": resposta.tipo, "com_erro": bool(resultado.get("isError")), "texto_de_fora": emoldurado}
    return Leitura(
        resultado=resultado,
        tipo=tipo,
        campos=campos,
        dados=dados,
        pedidos=pedidos,
        sala=sala,
        marcar_vencida=marcar_vencida,
        emoldurado=emoldurado,
        contadores=tuple(contadores),
    )


def _sala_sem_vaga(resposta: Resposta | Sondagem, sistema: Sistema) -> bool:
    """A sala de visitas estava cheia e nada foi a um filho (contrato K7): conta ``sala_sem_vaga`` com ``onde``."""
    nada_saiu = (
        resposta.desfecho == "inalcancavel" if isinstance(resposta, Sondagem) else resposta.tipo == "nao_enviado"
    )
    return sistema.de_terceiro and nada_saiu and resposta.excecao == SALA_SEM_VAGA


async def _listagem(ligacao: Ligacao, endereco: str, segredo: Segredo, discover_json: str | None) -> Listagem:
    """``ligacao.listar``, com os pedidos do sistema. Uma ligacao que so tem ``listar_ferramentas`` (as trocadas por
    injecao nos testes; as de verdade, ``ClienteLocal``, ``SemSala`` e ``ClienteSala``, tem as duas) lista sem
    pedidos."""
    listar = getattr(ligacao, "listar", None)
    if listar is None:
        return Listagem(await ligacao.listar_ferramentas(endereco, segredo, discover_json))
    listagem: Listagem = await listar(endereco, segredo, discover_json)
    return listagem


def _pedidos_da_falha(erro: Exception) -> tuple[str, ...]:
    """O que o sistema pediu antes de a listagem falhar: a sala diz em ``FalhaNaSala.pedidos``; o cliente local, em
    ``ListagemRecusada.pedidos`` e ``ListagemFalhou.pedidos``. Qualquer outra falha nao traz pedido."""
    if isinstance(erro, FalhaNaSala | cliente.ListagemRecusada | cliente.ListagemFalhou):
        return erro.pedidos
    return ()


async def _guardar_vistas(c: AsyncConnection, sistema: Sistema, ferramentas: Sequence[dict[str, Any]]) -> None:
    """Grava ``ferramentas_vistas`` em ordem fixa, pelo nome (ja conferido por ``_declaracao_invalida``), e nao na
    ordem do servidor: duas leituras ao mesmo tempo travam as mesmas linhas na mesma ordem, sem impasse no banco."""
    for f in sorted(ferramentas, key=lambda f: str(f["name"])):
        await c.execute(
            text(
                "INSERT INTO ferramentas_vistas (sistema_id, nome, definicao, impressao) "
                "VALUES (:s, :n, CAST(:d AS jsonb), :i) ON CONFLICT (sistema_id, nome) DO UPDATE "
                "SET definicao = EXCLUDED.definicao, impressao = EXCLUDED.impressao, vista_em = now()"
            ),
            {"s": sistema.id, "n": f["name"], "d": json.dumps(f), "i": impressao(f)},
        )


def _nome_no_extrato(nome: str) -> str:
    """Nome de ferramenta declarado pelo fornecedor, como entra em ``dados`` do extrato: cortado e com os
    caracteres escondidos marcados."""
    return marcar_escondidos(nome[:TETO_NOME_NO_EXTRATO])


def _texto_que_o_banco_nao_guarda(texto: str) -> bool:
    """Texto com caractere nulo (U+0000), que o PostgreSQL recusa em texto e jsonb, ou que nao se escreve em UTF-8
    (um pedaco de par UTF-16 sozinho, como ``\\ud800``), a mesma regra das rotas internas (``app._texto_valido``)."""
    if "\x00" in texto:
        return True
    try:
        texto.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def _tem_texto_que_o_banco_nao_guarda(valor: Any) -> bool:
    """Algum texto (chave ou valor, em qualquer nivel) que o banco nao guarda. Sem recursao."""
    pilha = [valor]
    while pilha:
        atual = pilha.pop()
        if isinstance(atual, str):
            if _texto_que_o_banco_nao_guarda(atual):
                return True
        elif isinstance(atual, dict):
            pilha.extend(atual.keys())
            pilha.extend(atual.values())
        elif isinstance(atual, list):
            pilha.extend(atual)
    return False


def _declaracao_invalida(ferramentas: Sequence[dict[str, Any]]) -> bool:
    """O servidor declarou o que o banco nao guarda: ferramenta sem nome, com nome em branco, com nome de mais de
    ``TETO_BYTES_NOME`` bytes, ou com um texto que o banco nao guarda em qualquer lugar. Conta como o servidor que
    nao entregou (``DeclaracoesIndisponiveis``), e nunca como falha nossa no banco (critica A5)."""
    for f in ferramentas:
        nome = f.get("name")
        if not isinstance(nome, str) or not nome.strip() or _tem_texto_que_o_banco_nao_guarda(f):
            return True
        if len(nome.encode("utf-8")) > TETO_BYTES_NOME:
            return True
    return False


_CLASSES_DE_DADO: Final = frozenset({"22", "54"})
"""SQLSTATE do erro do banco causado pelo dado gravado: 22 (dado que o banco nao aceita: texto ou JSON que ele nao
le, valor longo demais) e 54 (passou de um limite do banco, como o tamanho de uma entrada de indice)."""


def _erro_de_dado(erro: BaseException) -> bool:
    """O banco recusou o que ia ser gravado por causa do dado (``_CLASSES_DE_DADO``), e nao por falha nossa (impasse,
    serializacao, conexao, grupo de conexoes tomado, permissao), que sobe como veio e vira 503 (contrato K5)."""
    if not isinstance(erro, DBAPIError):
        return False
    sqlstate = getattr(erro.orig, "sqlstate", None)
    return isinstance(sqlstate, str) and sqlstate[:2] in _CLASSES_DE_DADO


class RevisaoRecusada(Exception):
    """A conclusao da revisao do servidor nao foi feita; ``mensagem`` e para o ADMIN (ja esta no extrato)."""

    def __init__(self, motivo: str, mensagem: str, ferramentas: Iterable[tuple[str, str]] = ()) -> None:
        self.motivo = motivo
        self.mensagem = mensagem
        self.ferramentas = tuple(ferramentas)
        super().__init__(mensagem)

    def como_json(self) -> dict[str, Any]:
        return {
            "revisado": False,
            "motivo": self.motivo,
            "mensagem": self.mensagem,
            "ferramentas": [{"ferramenta": n, "problema": p} for n, p in self.ferramentas],
        }


_PROBLEMAS: Final[dict[str, str]] = {
    "sumiu": "o servidor nao declara mais a ferramenta {nome}",
    "sem_revisao": "a ferramenta {nome} nao tem revisao ativa",
    "mudou": "a ferramenta {nome} mudou desde a sua revisao",
    "rever": "revise a ferramenta {nome} de novo antes de concluir a revisao do servidor",
}


class Despachante:
    def __init__(
        self,
        engine: AsyncEngine,
        cifra: Cifra,
        *,
        prazo_leitura: float = 30.0,
        ambiente: str = "producao",
        sala: Ligacao | None = None,
        local_para_terceiro: Ligacao | None = None,
        liberar_locais: bool = False,
        terceiros_ligados: bool = False,
        ligacao: Ligador | None = None,
    ) -> None:
        """``ambiente`` (``APP_ENV``), ``sala`` (``ClienteSala`` montado na criacao do app) e
        ``local_para_terceiro`` (so testes; ignorado fora de desenvolvimento e teste) escolhem por onde falar com
        cada sistema. ``liberar_locais`` (so testes) aceita endereco local na troca de chave OAuth de um terceiro.
        ``terceiros_ligados``: a chave da Fase 3. ``ligacao`` troca a escolha inteira (so testes)."""
        self.engine = engine
        self.cifra = cifra
        self.prazo_leitura = prazo_leitura
        self.ambiente = str(ambiente)
        self.sala = sala
        self.local_para_terceiro = local_para_terceiro
        self.liberar_locais = liberar_locais
        self.terceiros_ligados = terceiros_ligados
        self._ligacao = ligacao

    def ligacao(self, sistema: Sistema) -> Ligacao:
        """Por onde falar com ``sistema``: o mesmo ponto para leituras, declaracoes, sondagem e porta de saida."""
        if self._ligacao is not None:
            return self._ligacao(sistema)
        return ligacao_para(
            sistema, ambiente=self.ambiente, sala=self.sala, local_para_terceiro=self.local_para_terceiro
        )

    async def _recusar(
        self, pedido: PedidoLeitura, motivo: str, mensagem: str, ferramenta: str | None
    ) -> LeituraRecusada:
        async with self.engine.begin() as c:
            linha = await programas.linha(
                c, pedido.aplicativo_id, "leitura_recusada", {"ferramenta": ferramenta or "?", "motivo": motivo}
            )
            await registrar(
                c,
                papel="despachante",
                tipo=linha.tipo,
                campos=linha.campos,
                aplicativo_id=pedido.aplicativo_id,
                pessoa_id=pedido.pessoa_id,
                ferramenta=ferramenta,
                trace_id=pedido.trace_id,
            )
        CONTADORES.somar("leituras_recusadas_despachante", motivo=motivo)
        return LeituraRecusada(motivo, mensagem)

    async def _conferir_regras(self, pedido: PedidoLeitura, sistema_id: uuid.UUID, nome: str | None) -> None:
        """Bloqueios ativos (pessoa, aplicativo, par, sistema ou tudo) e porta de saida, antes de abrir a chave."""
        async with self.engine.connect() as c:
            vigentes = await bloqueios.ativos(c, pessoa_id=pedido.pessoa_id, aplicativo_id=pedido.aplicativo_id)
            saida = await estado.ler_saida(c)
        bloqueio = vigentes.para(sistema_id)
        if bloqueio is not None:
            raise await self._recusar(pedido, bloqueio.motivo_recusa(), bloqueio.mensagem(), nome)
        if not saida.aberta:
            raise await self._recusar(pedido, "saida_fechada", estado.MENSAGEM_SAIDA_FECHADA, nome)

    async def _chave_da_pessoa(
        self, pedido: PedidoLeitura, sistema_id: uuid.UUID, sistema_nome: str, nome: str | None
    ) -> renovacao.ChaveAberta:
        """Chave por pessoa pronta para o envio: renovada antes, com trava, se venceu ou vai vencer."""
        try:
            return await renovacao.chave_pronta(
                self.engine,
                self.cifra,
                sistema_id=sistema_id,
                pessoa_id=pedido.pessoa_id,
                ambiente=self.ambiente,
                liberar_locais=self.liberar_locais,
            )
        except chaves.ChaveAusente:
            raise await self._recusar(pedido, "sem_chave", "Voce ainda nao autorizou este sistema.", nome) from None
        except renovacao.AutorizacaoVencida:
            raise await self._recusar(
                pedido,
                "autorizacao_vencida",
                f"Sua autorizacao no sistema {sistema_nome} venceu. Autorize de novo nas telas do MCP-CLARO. "
                "Nada foi enviado.",
                nome,
            ) from None
        except renovacao.RenovacaoIndisponivel:
            raise await self._recusar(
                pedido,
                "renovacao_indisponivel",
                f"Nao deu para renovar a sua chave no sistema {sistema_nome} agora. Nada foi enviado. "
                "Tente de novo em instantes.",
                nome,
            ) from None
        except _COFRE_FALHOU as erro:
            log.warning("chave_indisponivel", sistema=sistema_nome, erro=type(erro).__name__)
            raise await self._recusar(pedido, "chave_indisponivel", MENSAGEM_CHAVE_INDISPONIVEL, nome) from None

    async def ler(self, pedido: PedidoLeitura) -> dict[str, Any]:
        async with self.engine.connect() as c:
            pessoa = (
                await c.execute(text("SELECT situacao, teste FROM pessoas WHERE id = :p"), {"p": pedido.pessoa_id})
            ).first()
            # Terceiro so com a chave da Fase 3 ligada ou para pessoa de teste (a mesma regra da vitrine).
            terceiros = self.terceiros_ligados or bool(pessoa is not None and pessoa.teste)
            ferramenta = await repositorio.ferramenta_da_ordem(
                c,
                pessoa_id=pedido.pessoa_id,
                aplicativo_id=pedido.aplicativo_id,
                ordem_id=pedido.ordem_id,
                ferramenta_id=pedido.ferramenta_id,
                versao=VERSAO_MCP,
                terceiros=terceiros,
            )
            app = (
                await c.execute(text("SELECT situacao FROM aplicativos WHERE id = :a"), {"a": pedido.aplicativo_id})
            ).scalar_one_or_none()
        if ferramenta is None:
            # Vale so a versao revisada vigente ('ativa'), de modelo ativo, na ordem vigente da pessoa e do
            # aplicativo, de sistema ativo na 2026-07-28 (terceiro: publicada na revisao do servidor vigente e so
            # com ``terceiros``): a mesma regra da vitrine.
            raise await self._recusar(pedido, "fora_da_ordem", "Ferramenta fora da ordem de servico.", None)
        nome = ferramenta.nome_vitrine
        if (pessoa.situacao if pessoa is not None else None) != "ativa" or app != "ativo":
            raise await self._recusar(pedido, "bloqueado", "Pessoa ou aplicativo bloqueado ou pausado.", nome)
        # Programas que rodam sozinhos (decisao 9): cadastro valendo e so sistema de chave propria, antes da chave.
        async with self.engine.connect() as c:
            recusa_programa = await programas.reconferir_leitura(c, pedido.aplicativo_id, ferramenta.sistema.tipo_chave)
        if recusa_programa is not None:
            raise await self._recusar(pedido, recusa_programa.motivo, recusa_programa.mensagem, nome)
        if ferramenta.classe != "le":
            raise await self._recusar(
                pedido, "acao_pela_porta_de_leitura", "A porta de leitura so atende leituras.", nome
            )
        sistema = ferramenta.sistema
        if sistema.situacao != "ativo" or sistema.versao_mcp != VERSAO_MCP:
            raise await self._recusar(pedido, "sistema_indisponivel", "O sistema nao esta ativo na 2026-07-28.", nome)
        await self._conferir_regras(pedido, sistema.id, nome)
        aberta = (
            await self._chave_da_pessoa(pedido, sistema.id, sistema.nome, nome)
            if sistema.tipo_chave == "por_pessoa"
            else None
        )

        async with self.engine.connect() as c:
            discover_json = (
                await c.execute(text("SELECT sondagem FROM sistemas WHERE id = :s"), {"s": sistema.id})
            ).scalar_one_or_none()
            declarado = await repositorio.esquema_declarado(c, sistema.id, ferramenta.nome_original)
            try:
                if sistema.tipo_chave == "por_pessoa":
                    chave = aberta.segredo if aberta else None
                elif sistema.tipo_chave == "compartilhada":
                    chave = await chaves.abrir(c, self.cifra, sistema_id=sistema.id)
                else:
                    chave = await chaves.abrir(c, self.cifra, sistema_id=sistema.id, aplicativo_id=pedido.aplicativo_id)
            except chaves.ChaveAusente:
                raise await self._recusar(pedido, "sem_chave", "Voce ainda nao autorizou este sistema.", nome) from None
            except _COFRE_FALHOU as erro:
                log.warning("chave_indisponivel", sistema=sistema.nome, erro=type(erro).__name__)
                raise await self._recusar(pedido, "chave_indisponivel", MENSAGEM_CHAVE_INDISPONIVEL, nome) from None

        async def segredo() -> str | None:
            return chave

        sondagem = json.dumps(discover_json) if isinstance(discover_json, dict) else discover_json
        try:
            resposta = await self.ligacao(sistema).chamar_ferramenta(
                sistema.endereco,
                segredo,
                sondagem,
                ferramenta.nome_original,
                pedido.argumentos,
                traceparent=None,
                prazo=self.prazo_leitura,
                esquema_declarado=declarado,
            )
        except Exception as erro:  # noqa: BLE001 - a leitura nunca vira erro interno; nada e reenviado
            log.warning("leitura_falhou", sistema=sistema.nome, erro=type(erro).__name__)
            resposta = Resposta("duvida", excecao=type(erro).__name__)
        leitura = interpretar(resposta, ferramenta, com_chave_da_pessoa=aberta is not None)
        if leitura.marcar_vencida and aberta is not None:
            await renovacao.marcar_vencida(
                self.engine, sistema_id=sistema.id, pessoa_id=pedido.pessoa_id, cifrada=aberta.cifrada
            )
        async with self.engine.begin() as c:
            linha = await programas.linha(c, pedido.aplicativo_id, leitura.tipo, leitura.campos)
            await registrar(
                c,
                papel="despachante",
                tipo=linha.tipo,
                campos=linha.campos,
                aplicativo_id=pedido.aplicativo_id,
                pessoa_id=pedido.pessoa_id,
                sistema_id=sistema.id,
                ferramenta=ferramenta.nome_vitrine,
                dados=leitura.dados,
                trace_id=pedido.trace_id,
            )
            if leitura.sala is not None:
                await registrar(
                    c,
                    papel="despachante",
                    tipo=leitura.sala,
                    campos={"sistema": sistema.nome},
                    aplicativo_id=pedido.aplicativo_id,
                    pessoa_id=pedido.pessoa_id,
                    sistema_id=sistema.id,
                    ferramenta=ferramenta.nome_vitrine,
                    trace_id=pedido.trace_id,
                )
        for contador, rotulos in leitura.contadores:
            CONTADORES.somar(contador, 1, **rotulos)
        return leitura.resultado

    async def _registrar_sala(self, c: AsyncConnection, sistema: Sistema, linha: str | None) -> str | None:
        """Linha ``sala_*`` do extrato (sondagem e declaracoes, sem pessoa), na transacao de quem chama."""
        if linha is not None:
            await registrar(c, papel="despachante", tipo=linha, campos={"sistema": sistema.nome}, sistema_id=sistema.id)
        return linha

    @staticmethod
    async def _registrar_pedidos(c: AsyncConnection, sistema: Sistema, tipo: str, pedidos: Sequence[str]) -> None:
        """Pedido do sistema barrado fora de uma chamada (contrato K4): ``declaracoes_sistema_pediu_dados`` ou
        ``sondagem_sistema_pediu_dados``, sem pessoa, na transacao de quem chama. So as nossas palavras na frase e
        as nossas categorias em ``dados.pedidos``; nada do que o sistema mandou."""
        if pedidos:
            await registrar(
                c,
                papel="despachante",
                tipo=tipo,
                campos={"sistema": sistema.nome, "pedidos": pedidos_legiveis(pedidos)},
                sistema_id=sistema.id,
                dados={"pedidos": list(pedidos)},
            )

    @staticmethod
    def _contar_pedidos(pedidos: Sequence[str], *, onde: str) -> None:
        for categoria in pedidos:
            CONTADORES.somar("pedidos_do_sistema_barrados", tipo=categoria, onde=onde)

    async def sondar_sistema(self, sistema_id: uuid.UUID, segredo: str | None) -> Sondagem:
        """Sondagem de versao no cadastro. Grava sempre o desfecho e as versoes anunciadas (ja conferidas).

        - ``aceito``: pendente e recusado ficam ativos; quarentena e desativado continuam como estao (em quarentena,
          a versao volta a valer, mas so a conclusao da revisao do servidor tira de la);
        - ``versao`` (so este conta como versao antiga): recusado, salvo desativado e quarentena. Um terceiro ativo e
          revisado vai a quarentena antes (D29): extrato, fila e limites. Ativo e revisado e lido sob a trava
          exclusiva do sistema, na transacao da gravacao, e nao antes da sondagem. A contagem vai depois do commit,
          com ``cadastro_terceiro`` ou ``cadastro``;
        - ``desvio``, ``nao_mcp``, ``sem_acesso`` e ``inalcancavel``: a situacao nao muda, so o motivo (sem veredito);
          em quarentena, nem o motivo (continua o da quarentena).
        """
        async with self.engine.connect() as c:
            sistema = await repositorio.sistema_por_id(c, sistema_id)
        if sistema is None:
            raise LookupError("sistema desconhecido")

        async def obter() -> str | None:
            return segredo

        sondagem = await self.ligacao(sistema).sondar(sistema.endereco, obter)
        versoes = list(sondagem.versoes)
        recusa_versao = False
        quarentena = None
        async with self.engine.begin() as c:
            if sondagem.desfecho == "aceito":
                await c.execute(
                    text(
                        # So pendente ou recusado viram ativo: quarentena e desativado continuam como estao.
                        "UPDATE sistemas SET versao_mcp = :v, sondagem = CAST(:s AS jsonb), sondado_em = now(),"
                        " situacao = CASE WHEN situacao IN ('pendente', 'recusado') THEN 'ativo' ELSE situacao END,"
                        " motivo = CASE WHEN situacao IN ('pendente', 'recusado') THEN NULL ELSE motivo END,"
                        " sondagem_desfecho = 'aceito', versoes_anunciadas = CAST(:va AS text[]),"
                        " atualizado_em = now() WHERE id = :i"
                    ),
                    {"v": VERSAO_MCP, "s": sondagem.discover_json, "va": versoes, "i": sistema_id},
                )
            elif sondagem.desfecho == "versao":
                recusa_versao = True
                if sistema.de_terceiro:
                    # D29, antes de qualquer outro UPDATE: trava do sistema, linha, limites e fila, nesta ordem. A
                    # situacao e a revisao vigente sao lidas de novo sob a trava: o ADMIN pode ter concluido uma
                    # revisao (ou as rotinas posto em quarentena) enquanto a sondagem falava com o servidor.
                    await travas.exclusiva(c, travas.chaves_do_alcance("sistema", sistema_id=sistema_id))
                    agora = (
                        await c.execute(
                            text("SELECT situacao, revisao_servidor_id FROM sistemas WHERE id = :i FOR UPDATE"),
                            {"i": sistema_id},
                        )
                    ).first()
                    if agora is not None and agora.situacao == "ativo" and agora.revisao_servidor_id is not None:
                        quarentena = await por_em_quarentena(
                            c,
                            sistema_id=sistema_id,
                            base=uuid.UUID(str(agora.revisao_servidor_id)),
                            papel="despachante",
                            motivo=MOTIVO_QUARENTENA_VERSAO,
                        )
                await c.execute(
                    text(
                        "UPDATE sistemas SET situacao = CASE WHEN situacao IN ('desativado', 'quarentena') THEN"
                        " situacao ELSE 'recusado' END, versao_mcp = NULL,"
                        " motivo = CASE WHEN situacao = 'quarentena' THEN motivo ELSE :m END,"
                        " sondagem_desfecho = 'versao', versoes_anunciadas = CAST(:va AS text[]), sondado_em = now(),"
                        " atualizado_em = now() WHERE id = :i"
                    ),
                    {"m": sondagem.motivo, "va": versoes, "i": sistema_id},
                )
                await registrar(
                    c,
                    papel="despachante",
                    tipo="recusa_versao_sistema",
                    campos={"versao_pedida": ",".join(versoes) or "geracao antiga", "aceita": VERSAO_MCP},
                    sistema_id=sistema_id,
                    dados={"motivo": sondagem.motivo, "codigo": sondagem.codigo, "origem": sistema.origem},
                )
            else:
                await c.execute(
                    text(
                        # Em quarentena, o motivo continua o da quarentena (o ADMIN ve por que ela entrou).
                        "UPDATE sistemas SET motivo = CASE WHEN situacao = 'quarentena' THEN motivo ELSE :m END,"
                        " sondagem_desfecho = :d,"
                        " versoes_anunciadas = CAST(:va AS text[]), sondado_em = now(), atualizado_em = now()"
                        " WHERE id = :i"
                    ),
                    {"m": sondagem.motivo, "d": sondagem.desfecho, "va": versoes, "i": sistema_id},
                )
            sala = await self._registrar_sala(c, sistema, linha_da_sala(sondagem, sistema))
            await self._registrar_pedidos(c, sistema, "sondagem_sistema_pediu_dados", sondagem.pedidos)
        # Contagens so depois do commit: uma sondagem desfeita nao conta.
        self._contar_pedidos(sondagem.pedidos, onde="sondagem")
        if _sala_sem_vaga(sondagem, sistema):
            CONTADORES.somar(SALA_SEM_VAGA, onde="sondagem")
        if recusa_versao:
            CONTADORES.recusa_versao(
                "sistema",
                sistema.nome,
                ",".join(versoes)[:40] or "geracao-antiga",
                "cadastro_terceiro" if sistema.de_terceiro else "cadastro",
            )
        elif sondagem.desfecho != "aceito":
            CONTADORES.somar("sondagens_sem_veredito", desfecho=sondagem.desfecho)
        if sala is not None:
            CONTADORES.somar(sala, onde="sondagem")
        if quarentena is not None:
            # Como o mudou-parou (rotinas/mudou_parou.py): as acoes que a quarentena cancelou entram no painel "Acoes
            # paradas antes de sair, por motivo", com o rotulo da porta de saida para a mesma causa.
            CONTADORES.somar("quarentenas_pela_sondagem")
            if quarentena.paradas:
                CONTADORES.somar(
                    "acoes_paradas", valor=len(quarentena.paradas), motivo=cadastro_quarentena.MOTIVO_PARADA
                )
        return sondagem

    async def _chave_das_declaracoes(self, sistema: Sistema) -> str | None:
        """A chave propria de leitura das declaracoes, guardada pelo ADMIN; num sistema de chave compartilhada, a
        propria chave compartilhada. None quando nao ha nenhuma (as rotinas e a revisao do servidor usam esta).
        O cofre que nao abre a chave (ou a cifra que nao acha a origem do sistema) levanta
        ``DeclaracoesNaoConferidas`` com ``chave_indisponivel``: a falha e do MCP-CLARO, nao do servidor."""
        async with self.engine.connect() as c:
            for tipo in ("declaracoes", "acesso") if sistema.tipo_chave == "compartilhada" else ("declaracoes",):
                try:
                    return await chaves.abrir(c, self.cifra, sistema_id=sistema.id, tipo=tipo)
                except chaves.ChaveAusente:
                    continue
                except _COFRE_FALHOU as erro:
                    log.warning("chave_indisponivel", sistema=sistema.nome, erro=type(erro).__name__)
                    raise DeclaracoesNaoConferidas(
                        "chave_indisponivel", f"a chave das declaracoes de {sistema.nome} nao abriu"
                    ) from erro
        return None

    async def _listar(self, sistema: Sistema, segredo: str | None, discover_json: Any) -> list[dict[str, Any]]:
        """O que o sistema declara agora, pela ligacao dele, e guardado em ``ferramentas_vistas``.

        Falha do MCP-CLARO, que as rotinas nao contam contra o servidor: ``DeclaracoesNaoConferidas``, com
        ``sala_fora`` (a sala de visitas fora) ou ``sala_sem_vaga`` (a sala cheia, dita pelo processo pai com o
        cabecalho que so ele escreve: ``SalaSemVaga``). Nos dois, nada chegou ao servidor. Qualquer outra falha da
        listagem: ``DeclaracoesIndisponiveis`` (o servidor nao entregou), inclusive o corpo de sala cheia sem o
        cabecalho do pai, que um filho tomado pelo terceiro poderia escrever para escapar da regra das rodadas sem
        resposta (critica A5).

        O que o servidor declarou e o banco nao guarda (``_declaracao_invalida``, ou o banco que recusa a linha por
        causa do dado, ``_erro_de_dado``) tambem e ``DeclaracoesIndisponiveis``: conta contra o servidor. So a falha
        nossa no banco (impasse, conexao, grupo de conexoes tomado) sobe como veio, e a porta interna a responde com
        503 ``conferencia_nao_feita`` (contrato K5).

        As linhas vao em ordem fixa, pelo nome, e nao na ordem em que o servidor listou: duas leituras ao mesmo tempo
        (duas copias das rotinas, ou a rodada e a conclusao da revisao) travam as mesmas linhas na mesma ordem, e o
        banco nao desfaz nenhuma por impasse. O pedido do sistema barrado no meio da listagem vai ao extrato
        (``declaracoes_sistema_pediu_dados``) na mesma transacao, e ao contador depois dela, tambem quando a
        listagem falha depois do pedido."""

        async def obter() -> str | None:
            return segredo

        sondagem = json.dumps(discover_json) if isinstance(discover_json, dict) else discover_json
        try:
            listagem = await _listagem(self.ligacao(sistema), sistema.endereco, obter, sondagem)
        except Exception as erro:
            linha = linha_da_sala(resposta_da_listagem(erro), sistema)
            pedidos = _pedidos_da_falha(erro)
            if linha is not None or pedidos:
                async with self.engine.begin() as c:
                    await self._registrar_sala(c, sistema, linha)
                    await self._registrar_pedidos(c, sistema, "declaracoes_sistema_pediu_dados", pedidos)
                if linha is not None:
                    CONTADORES.somar(linha, onde="declaracoes")
                self._contar_pedidos(pedidos, onde="declaracoes")
            if linha == LINHA_SALA_FORA:
                raise DeclaracoesNaoConferidas(
                    "sala_fora", f"a sala de visitas nao atendeu; nada foi pedido ao sistema {sistema.nome}"
                ) from erro
            if isinstance(erro, SalaSemVaga) and sistema.de_terceiro:
                CONTADORES.somar(SALA_SEM_VAGA, onde="declaracoes")
                raise DeclaracoesNaoConferidas(
                    SALA_SEM_VAGA, f"a sala de visitas estava cheia; nada foi pedido ao sistema {sistema.nome}"
                ) from erro
            raise DeclaracoesIndisponiveis(f"o sistema {sistema.nome} nao entregou as declaracoes") from erro
        ferramentas, pedidos = listagem.ferramentas, listagem.pedidos
        invalida = _declaracao_invalida(ferramentas)
        if pedidos or not invalida:
            try:
                async with self.engine.begin() as c:
                    await self._registrar_pedidos(c, sistema, "declaracoes_sistema_pediu_dados", pedidos)
                    if not invalida:
                        await _guardar_vistas(c, sistema, ferramentas)
            except DBAPIError as erro:
                if not _erro_de_dado(erro):
                    raise  # falha nossa no banco: 503 conferencia_nao_feita na porta interna (K5)
                # Segunda defesa: o banco recusou o que o servidor declarou. Conta contra o servidor, como a
                # declaracao invalida. Nada foi gravado (nem a linha dos pedidos, na mesma transacao): sem pedidos,
                # vai de novo so o pedido barrado.
                log.warning(
                    "declaracoes_que_o_banco_nao_guarda",
                    sistema=sistema.nome,
                    erro=type(erro).__name__,
                    sqlstate=getattr(erro.orig, "sqlstate", None),
                )
                if pedidos:
                    async with self.engine.begin() as c:
                        await self._registrar_pedidos(c, sistema, "declaracoes_sistema_pediu_dados", pedidos)
                    self._contar_pedidos(pedidos, onde="declaracoes")
                raise DeclaracoesIndisponiveis(
                    f"o sistema {sistema.nome} declarou ferramentas que o MCP-CLARO nao consegue guardar"
                ) from erro
            self._contar_pedidos(pedidos, onde="declaracoes")
        if invalida:
            raise DeclaracoesIndisponiveis(
                f"o sistema {sistema.nome} declarou uma ferramenta sem nome valido ou que o MCP-CLARO nao consegue"
                " guardar"
            )
        return ferramentas

    async def ler_declaracoes(self, sistema_id: uuid.UUID, segredo: str | None) -> list[dict[str, Any]]:
        """Le o que o sistema declara hoje e guarda em ``ferramentas_vistas`` (base do mudou-parou)."""
        async with self.engine.connect() as c:
            sistema = await repositorio.sistema_por_id(c, sistema_id)
            discover_json = (
                await c.execute(text("SELECT sondagem FROM sistemas WHERE id = :s"), {"s": sistema_id})
            ).scalar_one_or_none()
        if sistema is None or sistema.situacao not in ("ativo", "quarentena"):
            raise LookupError("sistema inativo")
        if segredo is None:
            # Sem chave dada (pedido das rotinas): a chave propria de leitura das declaracoes.
            segredo = await self._chave_das_declaracoes(sistema)
        return await self._listar(sistema, segredo, discover_json)

    async def _recusar_revisao(
        self,
        sistema: Sistema,
        recusa: RevisaoRecusada,
        *,
        quem: str,
        pessoa_id: uuid.UUID | None,
        c: AsyncConnection | None = None,
    ) -> RevisaoRecusada:
        """``admin_servidor_revisao_recusada`` no extrato (nomes do fornecedor so em ``dados``, marcados)."""
        dados = {
            "motivo": recusa.motivo,
            "ferramentas": [
                {"ferramenta": _nome_no_extrato(n), "problema": p}
                for n, p in recusa.ferramentas[:TETO_FERRAMENTAS_NO_EXTRATO]
            ],
            "quem": quem[:80],
        }
        frase = recusa.mensagem if not recusa.ferramentas else f"{len(recusa.ferramentas)} ferramenta(s) fora de dia"

        async def gravar(conexao: AsyncConnection) -> None:
            await registrar(
                conexao,
                papel="despachante",
                tipo="admin_servidor_revisao_recusada",
                campos={"sistema": sistema.nome, "motivo": frase.rstrip(".")},
                pessoa_id=pessoa_id,
                sistema_id=sistema.id,
                dados=dados,
            )

        if c is not None:
            await gravar(c)
        else:
            async with self.engine.begin() as nova:
                await gravar(nova)
            CONTADORES.somar("revisoes_servidor", desfecho="recusada", motivo=recusa.motivo)
        return recusa

    async def revisar_servidor(
        self,
        sistema_id: uuid.UUID,
        *,
        publicar: Sequence[str],
        fornecedor: str,
        quem: str,
        pessoa_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        """Conclusao da revisao do servidor de terceiros pelo ADMIN (desenho 3.4; critica A5).

        ``publicar``: os nomes originais das ferramentas que vao a vitrine. Le o servidor de novo, com a chave de
        leitura das declaracoes guardada (a mesma das rotinas; sem ela, recusa), e, numa transacao so, com a trava
        exclusiva do sistema: confere que nada mudou de situacao desde o comeco (uma quarentena que entrou no meio
        recusa a conclusao), que cada publicada tem revisada ativa com a impressao da leitura fresca e a definicao
        declarada guardada, grava ``revisoes_servidor`` e a torna vigente com o servidor ``ativo``. Levanta
        ``LookupError`` (sistema desconhecido) ou ``RevisaoRecusada`` (com a frase para o ADMIN, ja no extrato).
        """
        async with self.engine.connect() as c:
            sistema = await repositorio.sistema_por_id(c, sistema_id)
            comeco = (
                await c.execute(
                    text("SELECT situacao, motivo, sondagem FROM sistemas WHERE id = :s"), {"s": sistema_id}
                )
            ).first()
        if sistema is None or comeco is None:
            raise LookupError("sistema desconhecido")
        quem = (quem or "").strip() or "admin"

        async def recusar(motivo: str, mensagem: str, ferramentas: Iterable[tuple[str, str]] = ()) -> RevisaoRecusada:
            return await self._recusar_revisao(
                sistema, RevisaoRecusada(motivo, mensagem, tuple(ferramentas)), quem=quem, pessoa_id=pessoa_id
            )

        fornecedor = (fornecedor or "").strip()
        nomes = list(dict.fromkeys(publicar))
        if not sistema.de_terceiro:
            raise await recusar("nao_e_terceiro", "So servidor de terceiros tem revisao do servidor.")
        if not TETO_FORNECEDOR[0] <= len(fornecedor) <= TETO_FORNECEDOR[1]:
            raise await recusar("fornecedor", "Escreva quem fornece este servidor, de 3 a 200 letras.")
        if len(nomes) > TETO_PUBLICADAS:
            raise await recusar("publicadas_demais", f"Ate {TETO_PUBLICADAS} ferramentas na vitrine por servidor.")
        if sistema.situacao not in ("ativo", "quarentena"):
            raise await recusar("situacao", "O servidor precisa estar ativo ou em quarentena para concluir a revisao.")
        if sistema.versao_mcp != VERSAO_MCP:
            raise await recusar(
                "versao", "O servidor precisa passar na sondagem de versao 2026-07-28 antes de concluir a revisao."
            )
        try:
            segredo = await self._chave_das_declaracoes(sistema)
        except DeclaracoesIndisponiveis:
            raise await recusar(
                "chave_indisponivel", "Nao deu para abrir no cofre a chave de leitura das declaracoes; tente de novo."
            ) from None
        if segredo is None:
            # Critica A5: sem a chave das rotinas, o mudou-parou nao leria este servidor; nunca sem Authorization.
            raise await recusar(
                "sem_chave_declaracoes",
                "Guarde a chave de leitura das declaracoes deste servidor antes de concluir a revisao.",
            )
        try:
            ferramentas = await self._listar(sistema, segredo, comeco.sondagem)
        except DeclaracoesIndisponiveis:
            raise await recusar(
                "sem_resposta", "O servidor nao entregou as declaracoes agora; nada mudou. Tente de novo."
            ) from None
        declaradas = cadastro_quarentena.declaradas(ferramentas)
        agora = {d["nome"]: d["impressao"] for d in declaradas}
        impressao_agora = impressao_servidor((d["nome"], d["impressao"]) for d in declaradas)
        recusa: RevisaoRecusada | None = None
        feito: dict[str, Any] | None = None
        async with self.engine.begin() as c:
            await travas.exclusiva(c, travas.chaves_do_alcance("sistema", sistema_id=sistema_id))
            linha = (
                await c.execute(
                    text("SELECT situacao, motivo, versao_mcp FROM sistemas WHERE id = :s FOR UPDATE"),
                    {"s": sistema_id},
                )
            ).one()
            if (linha.situacao, linha.motivo) != (comeco.situacao, comeco.motivo) or linha.versao_mcp != VERSAO_MCP:
                # A situacao mudou entre a leitura fresca e a trava (uma quarentena, por exemplo): a leitura pode
                # ser de antes da mudanca. Nada e publicado.
                recusa = RevisaoRecusada(
                    "situacao_mudou",
                    "O servidor mudou de situacao durante a conclusao; nada mudou. Leia as ferramentas e conclua de"
                    " novo.",
                )
            else:
                problemas, publicadas = await self._conferir_publicadas(c, sistema_id, nomes, agora)
                if problemas:
                    mensagem = "; ".join(_PROBLEMAS[p].format(nome=n) for n, p in problemas)
                    recusa = RevisaoRecusada("ferramentas", mensagem[:1].upper() + mensagem[1:] + ".", problemas)
                else:
                    feito = await self._gravar_revisao(
                        c,
                        sistema,
                        situacao_antes=str(linha.situacao),
                        declaradas=declaradas,
                        impressao_agora=impressao_agora,
                        publicadas=publicadas,
                        fornecedor=fornecedor,
                        quem=quem,
                        pessoa_id=pessoa_id,
                    )
            if recusa is not None:
                # Na mesma transacao, que confirma: a recusa fica no extrato e nada mais muda.
                await self._recusar_revisao(sistema, recusa, quem=quem, pessoa_id=pessoa_id, c=c)
        if recusa is not None or feito is None:
            recusa = recusa or RevisaoRecusada("sem_resposta", "A revisao do servidor nao foi gravada; tente de novo.")
            CONTADORES.somar("revisoes_servidor", desfecho="recusada", motivo=recusa.motivo)
            raise recusa
        CONTADORES.somar("revisoes_servidor", desfecho="concluida")
        log.info("servidor_revisado", sistema=sistema.nome, versao=feito["versao"], publicadas=feito["publicadas"])
        return feito

    async def _conferir_publicadas(
        self, c: AsyncConnection, sistema_id: uuid.UUID, nomes: Sequence[str], agora: dict[str, str]
    ) -> tuple[list[tuple[str, str]], list[uuid.UUID]]:
        """Cada nome publicado precisa de revisada ativa, com a impressao da leitura fresca e a definicao declarada
        guardada (so a revisao da Fase 3 grava; sem ela, a vitrine esconde a ferramenta). Devolve (problemas, ids)."""
        revisadas = {
            str(r.nome_original): r
            for r in (
                await c.execute(
                    text(
                        "SELECT id, nome_original, impressao, definicao_declarada IS NOT NULL AS com_definicao"
                        " FROM ferramentas_revisadas WHERE sistema_id = :s AND situacao = 'ativa'"
                        " AND nome_original = ANY (CAST(:n AS text[]))"
                    ),
                    {"s": sistema_id, "n": list(nomes)},
                )
            ).all()
        }
        problemas: list[tuple[str, str]] = []
        for nome in nomes:
            revisada = revisadas.get(nome)
            if nome not in agora:
                problemas.append((nome, "sumiu"))
            elif revisada is None:
                problemas.append((nome, "sem_revisao"))
            elif revisada.impressao != agora[nome]:
                problemas.append((nome, "mudou"))
            elif not revisada.com_definicao:
                problemas.append((nome, "rever"))
        return problemas, [uuid.UUID(str(revisadas[n].id)) for n in nomes if n in revisadas]

    async def _gravar_revisao(
        self,
        c: AsyncConnection,
        sistema: Sistema,
        *,
        situacao_antes: str,
        declaradas: list[dict[str, str]],
        impressao_agora: str,
        publicadas: list[uuid.UUID],
        fornecedor: str,
        quem: str,
        pessoa_id: uuid.UUID | None,
    ) -> dict[str, Any]:
        """INSERT em ``revisoes_servidor`` e UPDATE de ``sistemas`` na mesma transacao (a guarda so torna vigente a
        revisao gravada nela), com o extrato. Ja com a trava exclusiva do sistema e a linha travada."""
        revisao = (
            await c.execute(
                text(
                    "INSERT INTO revisoes_servidor (sistema_id, versao, impressao, declaradas, publicadas,"
                    " fornecedor, revisado_por, pessoa_id)"
                    " SELECT CAST(:s AS uuid), coalesce(max(versao), 0) + 1, :i, CAST(:d AS jsonb),"
                    " CAST(:p AS uuid[]), :f, :q, CAST(:pe AS uuid) FROM revisoes_servidor WHERE sistema_id = :s"
                    " RETURNING id, versao"
                ),
                {
                    "s": sistema.id,
                    "i": impressao_agora,
                    "d": json.dumps(declaradas, ensure_ascii=False),
                    "p": publicadas,
                    "f": fornecedor,
                    "q": quem,
                    "pe": pessoa_id,
                },
            )
        ).one()
        vigente = (
            await c.execute(
                text(
                    "UPDATE sistemas SET revisao_servidor_id = :r, situacao = 'ativo', motivo = NULL,"
                    " atualizado_em = now() WHERE id = :s AND situacao = :antes RETURNING id"
                ),
                {"r": revisao.id, "s": sistema.id, "antes": situacao_antes},
            )
        ).first()
        if vigente is None:  # a trava e o FOR UPDATE impedem; nunca fica uma revisao gravada e solta
            raise RuntimeError("a revisao do servidor nao ficou vigente")
        fora = len(declaradas) - len(publicadas)
        await registrar(
            c,
            papel="despachante",
            tipo="admin_servidor_revisado",
            campos={"sistema": sistema.nome, "quem": quem[:80], "publicadas": len(publicadas), "fora": fora},
            pessoa_id=pessoa_id,
            sistema_id=sistema.id,
            dados={
                "revisao": str(revisao.id),
                "versao": int(revisao.versao),
                "impressao": impressao_agora,
                "declaradas": len(declaradas),
                "publicadas": len(publicadas),
            },
        )
        saiu = situacao_antes == "quarentena"
        if saiu:
            await registrar(
                c,
                papel="despachante",
                tipo="sistema_saiu_da_quarentena",
                campos={"sistema": sistema.nome, "quem": quem[:80]},
                pessoa_id=pessoa_id,
                sistema_id=sistema.id,
                dados={"revisao": str(revisao.id)},
            )
        return {
            "revisado": True,
            "revisao_id": str(revisao.id),
            "versao": int(revisao.versao),
            "impressao": impressao_agora,
            "publicadas": len(publicadas),
            "fora": fora,
            "saiu_da_quarentena": saiu,
        }
