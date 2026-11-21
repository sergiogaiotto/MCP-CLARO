"""Lado da entrada: o pedido de acao vira protocolo, e a resposta ao aplicativo diz o estado.

Antes da rota: com argumentos de mais de ``NIVEIS_DA_ACAO`` niveis, um dentro do outro, com argumento que traz o
caractere nulo (U+0000, que o banco nao guarda), com numero NaN ou infinito (que o JSON do protocolo e o banco nao
guardam), com bloqueio que vale para o pedido, ou com a porta de saida fechada, a acao e recusada antes de virar
protocolo (nada e gasto, nenhuma ficha e montada). As voltas pelos
argumentos daqui sao sem recursao; as de depois (``json``, ``canonico``, o banco) sao em C e cabem nesses niveis.
Ferramenta de servidor de terceiros: sob a mesma trava, a ferramenta e lida de novo (``ferramenta_da_ordem``, com
``Pedido.terceiros``); servidor em quarentena recusa com ``sistema_em_quarentena``, e o resto que a tirou da
vitrine com ``fora_da_vitrine``.

Escolha da rota, nesta ordem:

1. pedido igual: a original que ainda pode sair (esperando a ficha, liberada ou enviando) e devolvida em
   qualquer idade; a que ja saiu ou ficou nao confirmada, por ``DUPLICATE_WINDOW_MINUTES``. Nada novo sai;
2. teto de fichas por aplicativo (``CONFIRM_CAP_PER_HOUR``): acima dele, o aplicativo pausa. As fichas do
   alerta dos tres sinais contam (D25);
3. risco alto: ficha com passkey (``FEATURE_PASSKEY``), nunca limite;
4. periodo de experiencia (``TRIAL_PERIOD_DAYS``): tudo passa pela pessoa, nunca limite;
5. alerta dos tres sinais (``regras.sinais``, janela ``THREE_SIGNALS_WINDOW_MINUTES``): o aplicativo leu texto
   de fora e viu dado privado da pessoa, e agora quer enviar algo. A acao vai para a ficha sem consultar nem
   gastar limite, e a ficha guarda o retrato (``fichas.sinais``). Risco alto e experiencia mantem a rota deles
   e tambem levam o retrato. O aplicativo nao fica sabendo do alerta (D24): recebe a resposta de qualquer ficha;
6. limite por aproximacao que cobre o pedido e tem saldo (com a passkey ligada, so limite feito com ela):
   liberada com a espera visivel;
7. senao, ficha.

Uma acao ja liberada antes do alerta (por um limite) sai: o alerta vale para o que for pedido depois.

A entrada nunca chama o sistema: quem envia e o despachante unico. Enquanto espera, a entrada so
olha o estado da fila, no maximo ``RESPONSE_MAX_WAIT_SECONDS``.

Resposta de uma acao enviada (``resposta``): o bloco "Protocolo X: enviada uma vez ao sistema." vem primeiro, fora
da moldura; o resultado de estranhos (servidor de terceiros ou ferramenta marcada "traz texto de fora") vem
depois, emoldurado por ``regras.estranhos.marcar``. O aviso de partes sem texto retiradas pela medida fica fora da
moldura so quando a porta de saida guardou a conta (``fila.META_RETIRADAS``); sem ela, vai dentro, como texto do
sistema. ``operacoes.resultado`` continua gravado so com a mascara, sem moldura (uma marca nova a cada resposta), e
o ``_meta`` guardado nunca vai ao aplicativo.
"""

from __future__ import annotations

import asyncio
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.cadastro import repositorio
from mcp_claro.cadastro.modelos import Aplicativo, FerramentaVitrine
from mcp_claro.config.settings import Parametros
from mcp_claro.extrato import registrar
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.regras import bloqueios, estado, estranhos, politica, programas, sinais
from mcp_claro.saida import fila, travas
from mcp_claro.saida.estados import FINAIS, FRASE, Estado
from mcp_claro.saida.fila import Operacao, Rota
from mcp_claro.saida.protocolo import FUSO, chave_pedido_igual, protocolo_valido
from mcp_claro.versao import PREFIXO_META, VERSAO_MCP

log = logger("mcp_claro.saida.pedidos")

CONSULTA: Final = "mcp_claro_consultar_protocolo"
CHAVE_APLICATIVO_DIZ: Final = PREFIXO_META + "aplicativo_diz"
TETO_APLICATIVO_DIZ: Final = 1000

FERRAMENTA_CONSULTA: Final[dict[str, Any]] = {
    "name": CONSULTA,
    "title": "Consultar protocolo do MCP-CLARO",
    "description": (
        "Consulta o estado e o resultado de uma acao pelo numero de protocolo (CLR-AAAA-NNNNNNNN). "
        "So mostra acoes pedidas pela mesma pessoa, por este mesmo aplicativo. Use esta consulta em vez "
        "de repetir o pedido: repetir nao reenvia nada."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "protocolo": {
                "type": "string",
                "pattern": "^CLR-[0-9]{4}-[0-9]{8,}$",
                "description": "Numero de protocolo, como CLR-2026-00000012.",
            }
        },
        "required": ["protocolo"],
        "additionalProperties": False,
    },
    "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
}


RECUSA_QUARENTENA: Final = "O servidor desta ferramenta esta em quarentena ate nova revisao do ADMIN. Nada foi enviado."


class AcaoRecusada(Exception):
    def __init__(self, motivo: str, mensagem: str) -> None:
        self.motivo = motivo
        self.mensagem = mensagem
        super().__init__(mensagem)


@dataclass(frozen=True, slots=True)
class Pedido:
    pessoa_id: uuid.UUID
    aplicativo: Aplicativo
    ferramenta: FerramentaVitrine
    argumentos: dict[str, Any]
    aplicativo_diz: str | None
    trace_id: str | None = None
    terceiros: bool = False
    """Servidores de terceiros valem para quem pede: chave da Fase 3 ligada ou pessoa de teste. Decide a
    releitura de uma ferramenta de terceiro e se o sinal "leu texto de fora" vindo de terceiro conta no alerta
    dos tres sinais (o de ferramenta propria marcada conta sempre)."""


RECUSA_NULO: Final = (
    "O pedido traz o caractere nulo (U+0000), que o MCP-CLARO nao guarda. Nada foi enviado. "
    "Peca de novo sem esse caractere."
)


NIVEIS_DA_ACAO: Final = 500
"""Niveis de listas e objetos, um dentro do outro, nos argumentos de uma acao (os argumentos sao o primeiro nivel).
Ate a Fase 3, ``tem_nulo`` descia com recursao, dois quadros da pilha do Python por nivel, num limite de 1000
quadros: todo pedido de acao com 500 niveis ou mais ja dava erro interno. A recusa legivel nao tira nada que
passava, e o que passa cabe no resto do caminho da entrada (``json``, banco, fila). O envio ao sistema e outra
conta: o kit do cliente fixado nao serializa mais de uns 250 niveis (isso fica com a porta de saida)."""

RECUSA_FUNDO: Final = (
    f"O pedido tem mais de {NIVEIS_DA_ACAO} niveis de listas e objetos, um dentro do outro, e o MCP-CLARO nao guarda "
    "pedido assim. Nada foi enviado."
)


def tem_nulo(valor: Any) -> bool:
    """Algum texto (chave ou valor, em qualquer profundidade) traz U+0000. Sem recursao (uma pilha): nem o
    aninhamento fundo derruba a conferencia."""
    pilha: list[Any] = [valor]
    while pilha:
        atual = pilha.pop()
        if isinstance(atual, str):
            if "\x00" in atual:
                return True
        elif isinstance(atual, dict):
            for chave, item in atual.items():
                pilha.append(chave)
                pilha.append(item)
        elif isinstance(atual, list | tuple):
            pilha.extend(atual)
    return False


RECUSA_NUMERO: Final = (
    "O pedido traz um numero que nao e um numero de verdade (NaN ou infinito), e o MCP-CLARO nao guarda pedido "
    "assim. Nada foi enviado."
)


def tem_numero_nao_finito(valor: Any) -> bool:
    """Algum numero (em qualquer profundidade) e NaN ou infinito. O leitor de JSON do kit aceita ``NaN``,
    ``Infinity`` e ``1e400``, mas o JSON do protocolo (``canonico``, com ``allow_nan=False``) e o banco nao: sem
    esta recusa, o pedido virava erro interno. Sem recursao (uma pilha)."""
    pilha: list[Any] = [valor]
    while pilha:
        atual = pilha.pop()
        if isinstance(atual, float):
            if not math.isfinite(atual):
                return True
        elif isinstance(atual, dict):
            pilha.extend(atual.values())
        elif isinstance(atual, list | tuple):
            pilha.extend(atual)
    return False


def passa_dos_niveis(valor: Any, teto: int) -> bool:
    """Mais de ``teto`` listas e objetos, um dentro do outro (``valor`` e o primeiro nivel). Sem recursao, e para no
    primeiro que passa."""
    pilha: list[tuple[Any, int]] = [(valor, 1)]
    while pilha:
        atual, nivel = pilha.pop()
        if isinstance(atual, dict):
            filhos: Any = atual.values()
        elif isinstance(atual, list | tuple):
            filhos = atual
        else:
            continue
        if nivel > teto:
            return True
        pilha.extend((filho, nivel + 1) for filho in filhos if isinstance(filho, dict | list | tuple))
    return False


def aplicativo_diz(meta: Any) -> str | None:
    """O que o aplicativo diz sobre o pedido: texto dele, mostrado a parte na ficha, nunca como nosso."""
    if not isinstance(meta, dict):
        return None
    valor = meta.get(CHAVE_APLICATIVO_DIZ)
    if not isinstance(valor, str) or not valor.strip():
        return None
    return "".join(ch for ch in valor.strip() if ch.isprintable() or ch == "\n")[:TETO_APLICATIVO_DIZ]


class Pedidos:
    def __init__(self, p: Parametros, engine: AsyncEngine) -> None:
        self.p = p
        self.engine = engine

    async def pedir(self, pedido: Pedido) -> tuple[Operacao, bool]:
        """Registra a acao (ou acha a original) numa transacao so. Devolve (operacao, repetida).

        Levanta ``AcaoRecusada``.
        """
        f = pedido.ferramenta
        if f.ordem_id is None:
            raise AcaoRecusada("fora_da_ordem", "Ferramenta fora da sua ordem de servico. Nada foi enviado.")
        if passa_dos_niveis(pedido.argumentos, NIVEIS_DA_ACAO):
            # Antes de qualquer outra volta pelos argumentos (json, banco, ficha): recusa legivel, nunca erro interno.
            raise AcaoRecusada("aninhamento_fundo", RECUSA_FUNDO)
        if tem_nulo(pedido.argumentos):
            # O banco nao guarda U+0000, e trocar o caractere mudaria a acao que a pessoa confirma: recusa antes.
            raise AcaoRecusada("caractere_nulo", RECUSA_NULO)
        if tem_numero_nao_finito(pedido.argumentos):
            # Nem o codigo do protocolo (JSON canonico) nem o banco guardam NaN ou infinito, e trocar o numero mudaria
            # a acao que a pessoa confirma: recusa antes, nunca erro interno.
            raise AcaoRecusada("numero_invalido", RECUSA_NUMERO)
        chave = chave_pedido_igual(pedido.pessoa_id, pedido.aplicativo.id, f.id, pedido.argumentos)
        async with self.engine.begin() as c:
            # Mesma trava do envio: um bloqueio ligado agora ou entrou antes (e e visto aqui) ou espera este
            # pedido terminar (e o para, com ele no comprovante).
            await travas.compartilhar_envio(c, pedido.pessoa_id, pedido.aplicativo.id, f.sistema.id)
            await fila.travar_pedido_igual(c, chave)
            vigentes = await bloqueios.ativos(c, pessoa_id=pedido.pessoa_id, aplicativo_id=pedido.aplicativo.id)
            bloqueio = vigentes.para(f.sistema.id)
            if bloqueio is not None:
                raise AcaoRecusada(bloqueio.motivo_recusa(), bloqueio.mensagem())
            # Saida fechada (pelo ADMIN, pela conferencia do cofre trancado ou fora do ar, ou pela do banco na
            # partida): nada vira protocolo. A trava compartilhada de "tudo", tomada acima, e a mesma que fechar a
            # saida toma exclusiva: a leitura aqui vale.
            if not (await estado.ler_saida(c)).aberta:
                raise AcaoRecusada("saida_fechada", estado.MENSAGEM_SAIDA_FECHADA)
            if f.sistema.de_terceiro:
                # Sob a trava compartilhada do sistema, a mesma que a quarentena toma exclusiva: ou ela entrou antes
                # (e e vista aqui), ou espera este pedido terminar (e o cancela).
                await self._reler_terceiro(c, pedido)
            valores = await politica.varios(
                c,
                "DUPLICATE_WINDOW_MINUTES",
                "CONFIRM_CAP_PER_HOUR",
                "TRIAL_PERIOD_DAYS",
                "CARD_TTL_MINUTES",
                "OUTBOX_HOLD_SECONDS",
                sinais.CHAVE_JANELA,
            )
            original = await fila.pedido_igual(c, chave, valores["DUPLICATE_WINDOW_MINUTES"])
            if original is not None:
                # Para um programa, a linha diz "programa X, responsavel Y" (``programas.linha``).
                linha = await programas.linha(c, pedido.aplicativo, "acao_repetida", {"protocolo": original.protocolo})
                await registrar(
                    c,
                    papel="entrada",
                    tipo=linha.tipo,
                    campos=linha.campos,
                    protocolo=original.protocolo,
                    pessoa_id=pedido.pessoa_id,
                    aplicativo_id=pedido.aplicativo.id,
                    sistema_id=f.sistema.id,
                    ferramenta=f.nome_vitrine,
                    trace_id=pedido.trace_id,
                )
                CONTADORES.somar("acoes_repetidas")
                return original, True
            rota = await self._rota(c, pedido, valores)
            teto = valores["CONFIRM_CAP_PER_HOUR"]
            if rota is None:
                op = None
            else:
                op = await fila.registrar_pedido(
                    c,
                    pessoa_id=pedido.pessoa_id,
                    aplicativo_id=pedido.aplicativo.id,
                    ordem_id=f.ordem_id,
                    ferramenta=f,
                    argumentos=pedido.argumentos,
                    aplicativo_diz=pedido.aplicativo_diz,
                    rota=rota,
                    prazo_ficha_minutos=self._prazo_da_ficha(pedido, valores),
                    espera_segundos=valores["OUTBOX_HOLD_SECONDS"],
                    trace_id=pedido.trace_id,
                    aplicativo=pedido.aplicativo,
                )
        if op is None or rota is None:
            # A pausa vai numa transacao propria, depois de soltar a trava compartilhada deste pedido: dois
            # pedidos no teto ao mesmo tempo nao esperam um pelo outro. A pausa fica gravada antes da recusa.
            async with self.engine.begin() as c:
                await self._pausar_por_teto(c, pedido, teto)
            raise AcaoRecusada(
                "teto_confirmacoes",
                f"Este aplicativo pediu mais de {teto} confirmacoes na ultima hora e foi pausado. "
                "Nada foi enviado. O ADMIN reativa depois de conferir.",
            )
        CONTADORES.somar("acoes_pedidas", rota=rota.motivo)
        return op, False

    async def _reler_terceiro(self, c: Any, pedido: Pedido) -> None:
        """A ferramenta de terceiro ainda vale para esta pessoa, neste aplicativo, na mesma ordem e versao."""
        f = pedido.ferramenta
        if f.ordem_id is None:
            raise AcaoRecusada("fora_da_ordem", "Ferramenta fora da sua ordem de servico. Nada foi enviado.")
        atual = await repositorio.ferramenta_da_ordem(
            c,
            pessoa_id=pedido.pessoa_id,
            aplicativo_id=pedido.aplicativo.id,
            ordem_id=f.ordem_id,
            ferramenta_id=f.id,
            versao=VERSAO_MCP,
            terceiros=pedido.terceiros,
        )
        if atual is not None:
            return
        situacao = (
            await c.execute(text("SELECT situacao FROM sistemas WHERE id = :s"), {"s": f.sistema.id})
        ).scalar_one_or_none()
        if situacao == "quarentena":
            raise AcaoRecusada("sistema_em_quarentena", RECUSA_QUARENTENA)
        raise AcaoRecusada(
            "fora_da_vitrine", f"A ferramenta {f.nome_vitrine} nao esta na sua vitrine. Nada foi enviado."
        )

    def _prazo_da_ficha(self, pedido: Pedido, valores: dict[str, int]) -> int:
        """Programa que roda sozinho (decisao 9): a ficha vence no prazo ``PROGRAM_PENDING_TIMEOUT_HOURS``.
        Para assistentes, ``CARD_TTL_MINUTES``.

        As pendencias do responsavel ainda nao existem: por isso ``PROGRAM_ACTIONS_ENABLED`` nao liga
        (``Parametros.problemas``)."""
        if pedido.aplicativo.tipo == "programa":
            return int(self.p.PROGRAM_PENDING_TIMEOUT_HOURS) * 60
        return valores["CARD_TTL_MINUTES"]

    async def _rota(self, c: Any, pedido: Pedido, valores: dict[str, int]) -> Rota | None:
        f = pedido.ferramenta
        alerta = await sinais.alerta(
            c,
            pessoa_id=pedido.pessoa_id,
            aplicativo_id=pedido.aplicativo.id,
            janela_minutos=valores[sinais.CHAVE_JANELA],
            terceiros=pedido.terceiros,
        )
        retrato = alerta.retrato() if alerta is not None else None
        if f.risco == "alto":
            rota = Rota(exige_passkey=self.p.FEATURE_PASSKEY, motivo="risco_alto", sinais=retrato)
        elif await fila.em_experiencia(c, pedido.pessoa_id, pedido.aplicativo.id, valores["TRIAL_PERIOD_DAYS"]):
            rota = Rota(motivo="periodo_de_experiencia", sinais=retrato)
        elif retrato is not None:
            # Tres sinais: nenhum limite e consultado nem gasto.
            rota = Rota(motivo="tres_sinais", sinais=retrato)
        else:
            limite = await fila.gastar_limite(
                c,
                pessoa_id=pedido.pessoa_id,
                aplicativo_id=pedido.aplicativo.id,
                sistema_id=f.sistema.id,
                nome_original=f.nome_original,
                argumentos=pedido.argumentos,
                exige_passkey=self.p.FEATURE_PASSKEY,
            )
            if limite is not None:
                return Rota(limite_id=limite, motivo="limite")
            rota = Rota(motivo="ficha")
        # Contar e montar a ficha num passo so: pedidos ao mesmo tempo do mesmo aplicativo esperam aqui.
        await fila.travar_teto(c, pedido.aplicativo.id)
        if await fila.fichas_na_ultima_hora(c, pedido.aplicativo.id) >= valores["CONFIRM_CAP_PER_HOUR"]:
            return None
        return rota

    async def _pausar_por_teto(self, c: Any, pedido: Pedido, teto: int) -> None:
        # Trava exclusiva do aplicativo antes de olhar: dois pedidos no teto ligam uma pausa so.
        await travas.exclusiva(c, travas.chaves_do_alcance("aplicativo", aplicativo_id=pedido.aplicativo.id))
        vigentes = await bloqueios.ativos(c, pessoa_id=None, aplicativo_id=pedido.aplicativo.id)
        if vigentes.geral is None:
            # Pausa como qualquer outra: para a fila do aplicativo e guarda o comprovante.
            await bloqueios.bloquear(
                c,
                "pausa",
                "aplicativo",
                fila.QUEM_TETO,
                f"Mais de {teto} fichas na ultima hora.",
                aplicativo_id=pedido.aplicativo.id,
                papel="entrada",
            )
        await registrar(
            c,
            papel="entrada",
            tipo="teto_confirmacoes",
            campos={"aplicativo": pedido.aplicativo.nome, "teto": teto},
            aplicativo_id=pedido.aplicativo.id,
            pessoa_id=pedido.pessoa_id,
        )
        CONTADORES.somar("teto_confirmacoes")

    async def aguardar(self, op: Operacao, *, segundos: float | None = None) -> Operacao:
        """Olha a fila ate a acao terminar, pedir ficha ou passar do tempo de resposta."""
        limite = float(segundos if segundos is not None else self.p.RESPONSE_MAX_WAIT_SECONDS)
        inicio = time.monotonic()
        atual = op
        while True:
            if atual.estado in FINAIS or atual.estado == Estado.AGUARDANDO_FICHA:
                return atual
            resta = limite - (time.monotonic() - inicio)
            if resta <= 0:
                return atual
            if atual.estado == Estado.LIBERADA and atual.hora_saida is not None:
                async with self.engine.connect() as c:
                    falta = (
                        await c.execute(
                            text("SELECT extract(epoch FROM CAST(:h AS timestamptz) - now())"),
                            {"h": atual.hora_saida},
                        )
                    ).scalar_one()
                if float(falta) > resta:
                    return atual
            await asyncio.sleep(min(0.25, max(resta, 0.01)))
            async with self.engine.connect() as c:
                novo = await fila.por_id(c, op.id)
            if novo is None:
                return atual
            atual = novo

    async def consultar(self, protocolo: Any, *, pessoa_id: uuid.UUID, aplicativo_id: uuid.UUID) -> Operacao | None:
        if not protocolo_valido(protocolo):
            return None
        async with self.engine.connect() as c:
            return await fila.por_protocolo(c, protocolo, pessoa_id=pessoa_id, aplicativo_id=aplicativo_id)


def _hora_local(op: Operacao) -> str | None:
    if op.hora_saida is None:
        return None
    return op.hora_saida.astimezone(ZoneInfo(FUSO)).strftime("%H:%M:%S")


def texto_do_estado(op: Operacao) -> str:
    """Frase para o aplicativo mostrar a pessoa. Nunca traz argumento nem dado pessoal."""
    prot = op.protocolo
    if op.estado == Estado.AGUARDANDO_FICHA:
        passkey = " com a passkey" if op.ficha_exige_passkey else ""
        return (
            f"Pedido registrado com o protocolo {prot}. Nada saiu ainda: a pessoa precisa confirmar a ficha{passkey} "
            f"nas telas do MCP-CLARO, conferindo o codigo {op.codigo_conferencia}. Depois, consulte o andamento "
            f"com a ferramenta {CONSULTA}. Nao repita o pedido."
        )
    if op.estado == Estado.LIBERADA:
        hora = _hora_local(op)
        quando = f" as {hora} (horario de Brasilia)" if hora else ""
        return (
            f"Pedido {prot} liberado. Sai{quando}, se a pessoa nao cancelar antes nas telas do MCP-CLARO. "
            f"Consulte o resultado com a ferramenta {CONSULTA}. Nao repita o pedido."
        )
    if op.estado == Estado.ENVIANDO:
        return f"A acao {prot} esta saindo agora, uma vez so. Consulte o resultado com a ferramenta {CONSULTA}."
    if op.estado == Estado.NAO_CONFIRMADA:
        decidido = {
            "aconteceu": " A pessoa registrou que aconteceu no sistema.",
            "nao_aconteceu": " A pessoa registrou que nao aconteceu no sistema.",
            "nao_sei": " A pessoa registrou que ainda nao sabe.",
        }.get(op.decisao_pessoa or "", "")
        return (
            f"Estamos confirmando: a acao {prot} foi enviada uma vez, mas a resposta do sistema nao chegou inteira. "
            f"Nada sera reenviado. Nao repita o pedido: confira no sistema.{decidido}"
        )
    return f"Protocolo {prot}: {FRASE[op.estado]}"


_COM_ERRO: Final = {
    Estado.NAO_SAIU,
    Estado.SISTEMA_PEDIU_DADOS,
    Estado.VENCIDA,
    Estado.CANCELADA,
    Estado.PARADA_POR_BLOQUEIO,
    Estado.PARADA_POR_RESTAURACAO,
}


def resposta(op: Operacao, *, selo: int | None = None, repetida: bool = False) -> dict[str, Any]:
    """Resultado de ``tools/call`` no fio da 2026-07-28, com o protocolo no ``_meta`` do MCP-CLARO."""
    meta: dict[str, Any] = {
        PREFIXO_META + "protocolo": op.protocolo,
        PREFIXO_META + "estado": str(op.estado),
        PREFIXO_META + "codigo_conferencia": op.codigo_conferencia,
        PREFIXO_META + "sistema": op.sistema_nome,
    }
    if selo is not None:
        meta[PREFIXO_META + "selo"] = selo
    if op.hora_saida is not None and op.estado == Estado.LIBERADA:
        meta[PREFIXO_META + "hora_saida"] = op.hora_saida.isoformat()
    if repetida:
        meta[PREFIXO_META + "repetido"] = True
    if op.estado == Estado.ENVIADA and op.resultado is not None:
        aviso = {"type": "text", "text": f"Protocolo {op.protocolo}: enviada uma vez ao sistema."}
        if op.de_estranhos:
            # O aviso do protocolo e nosso: fica fora da moldura. O resto sai emoldurado, com marca nova. O aviso de
            # partes retiradas pela medida tambem sai fora, so com a conta guardada pela porta de saida.
            marcado = estranhos.marcar(
                op.resultado, sistema=op.sistema_nome, de_terceiro=op.de_terceiro, retiradas=op.retiradas
            )
            corpo = {k: v for k, v in marcado.items() if k not in ("content", "_meta")}
            corpo["content"] = [aviso, *marcado["content"]]
            corpo["_meta"] = meta | dict(marcado["_meta"])
            return corpo
        corpo = {k: v for k, v in op.resultado.items() if k != "_meta"}
        corpo["content"] = [aviso, *list(corpo.get("content") or [])]
        corpo["_meta"] = meta
        return corpo
    return {
        "content": [{"type": "text", "text": texto_do_estado(op)}],
        "isError": op.estado in _COM_ERRO,
        "_meta": meta,
    }
