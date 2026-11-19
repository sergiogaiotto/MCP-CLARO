"""Lado da entrada: o pedido de acao vira protocolo, e a resposta ao aplicativo diz o estado.

Antes da rota: com argumento que traz o caractere nulo (U+0000, que o banco nao guarda), com bloqueio que vale
para o pedido, ou com a porta de saida fechada, a acao e recusada antes de virar protocolo (nada e gasto,
nenhuma ficha e montada).

Escolha da rota, nesta ordem:

1. pedido igual: a original que ainda pode sair (esperando a ficha, liberada ou enviando) e devolvida em
   qualquer idade; a que ja saiu ou ficou nao confirmada, por ``DUPLICATE_WINDOW_MINUTES``. Nada novo sai;
2. teto de fichas por aplicativo (``CONFIRM_CAP_PER_HOUR``): acima dele, o aplicativo pausa;
3. risco alto: ficha com passkey (``FEATURE_PASSKEY``), nunca limite;
4. periodo de experiencia (``TRIAL_PERIOD_DAYS``): tudo passa pela pessoa, nunca limite;
5. limite por aproximacao que cobre o pedido e tem saldo (com a passkey ligada, so limite feito com ela):
   liberada com a espera visivel;
6. senao, ficha.

A entrada nunca chama o sistema: quem envia e o despachante unico. Enquanto espera, a entrada so
olha o estado da fila, no maximo ``RESPONSE_MAX_WAIT_SECONDS``.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.cadastro.modelos import Aplicativo, FerramentaVitrine
from mcp_claro.config.settings import Parametros
from mcp_claro.extrato import registrar
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.regras import bloqueios, estado, politica, programas
from mcp_claro.saida import fila, travas
from mcp_claro.saida.estados import FINAIS, FRASE, Estado
from mcp_claro.saida.fila import Operacao, Rota
from mcp_claro.saida.protocolo import FUSO, chave_pedido_igual, protocolo_valido
from mcp_claro.versao import PREFIXO_META

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


RECUSA_NULO: Final = (
    "O pedido traz o caractere nulo (U+0000), que o MCP-CLARO nao guarda. Nada foi enviado. "
    "Peca de novo sem esse caractere."
)


def tem_nulo(valor: Any) -> bool:
    """Algum texto (chave ou valor, em qualquer profundidade) traz U+0000."""
    if isinstance(valor, str):
        return "\x00" in valor
    if isinstance(valor, dict):
        return any(tem_nulo(chave) or tem_nulo(item) for chave, item in valor.items())
    if isinstance(valor, list | tuple):
        return any(tem_nulo(item) for item in valor)
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
        if tem_nulo(pedido.argumentos):
            # O banco nao guarda U+0000, e trocar o caractere mudaria a acao que a pessoa confirma: recusa antes.
            raise AcaoRecusada("caractere_nulo", RECUSA_NULO)
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
            valores = await politica.varios(
                c,
                "DUPLICATE_WINDOW_MINUTES",
                "CONFIRM_CAP_PER_HOUR",
                "TRIAL_PERIOD_DAYS",
                "CARD_TTL_MINUTES",
                "OUTBOX_HOLD_SECONDS",
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
        if f.risco == "alto":
            rota = Rota(exige_passkey=self.p.FEATURE_PASSKEY, motivo="risco_alto")
        elif await fila.em_experiencia(c, pedido.pessoa_id, pedido.aplicativo.id, valores["TRIAL_PERIOD_DAYS"]):
            rota = Rota(motivo="periodo_de_experiencia")
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
        corpo = {k: v for k, v in op.resultado.items() if k != "_meta"}
        aviso = {"type": "text", "text": f"Protocolo {op.protocolo}: enviada uma vez ao sistema."}
        corpo["content"] = [aviso, *list(corpo.get("content") or [])]
        corpo["_meta"] = meta
        return corpo
    return {
        "content": [{"type": "text", "text": texto_do_estado(op)}],
        "isError": op.estado in _COM_ERRO,
        "_meta": meta,
    }
