"""Fila de avisos na tabela ``avisos``: enfileirar, pegar, enviar, marcar.

Regras:

- ``enfileirar`` usa ``ON CONFLICT DO NOTHING`` pela ``chave_unica``: o mesmo aviso
  (por exemplo, "ordem X vence em breve") entra uma vez so. Sem alvo no ``ON
  CONFLICT`` para que a entrada e o despachante, que so tem INSERT, possam usar.
- O laco pega um aviso por vez com ``SELECT ... FOR UPDATE SKIP LOCKED``: varios
  processos dividem a fila sem mandar o mesmo aviso ao mesmo tempo.
- Na falha, soma uma tentativa e reagenda com espera crescente. Depois de
  ``NOTIFY_RETRY_MAX`` novas tentativas, o aviso vira ``desistiu`` e vai ao extrato.
- Aviso pode repetir (se o envio der certo e a gravacao falhar, ele sai de novo);
  acao nunca. Por isso o ``webhook-id`` e o ``Message-ID`` sao estaveis por aviso.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES

log = logger("mcp_claro.avisos")

CANAIS = frozenset({"email", "webhook"})
TAMANHO_MAXIMO_CORPO = 16 * 1024
"""Bytes do corpo em JSON. Aviso e curto: frase, numeros e identificadores."""

# Chaves que nunca entram no corpo de um aviso, em qualquer nivel.
_CHAVES_PROIBIDAS = frozenset(
    {
        "argumentos",
        "arguments",
        "resultado",
        "result",
        "conteudo",
        "content",
        "token",
        "cracha",
        "chave",
        "senha",
        "password",
        "secret",
        "segredo",
        "authorization",
        "cookie",
        "cpf",
        "email",
        "e-mail",
        "telefone",
        "endereco",
    }
)


class CorpoRecusado(ValueError):
    """O corpo do aviso tentou levar conteudo de ferramenta, segredo ou dado pessoal."""


class FalhaEnvio(Exception):
    """O canal nao entregou o aviso. ``permanente`` diz que tentar de novo nao adianta."""

    def __init__(self, motivo: str, *, permanente: bool = False) -> None:
        self.motivo = motivo[:200]
        self.permanente = permanente
        super().__init__(self.motivo)


def conferir_corpo(corpo: Mapping[str, Any]) -> dict[str, Any]:
    """Recusa chaves de conteudo, segredo ou dado pessoal em qualquer nivel, e corpos grandes."""

    def andar(valor: Any, caminho: str) -> None:
        if isinstance(valor, Mapping):
            for chave, filho in valor.items():
                if not isinstance(chave, str):
                    raise CorpoRecusado(f"chave nao textual em {caminho or 'corpo'}")
                if chave.lower() in _CHAVES_PROIBIDAS:
                    raise CorpoRecusado(f"o aviso nao leva '{chave}' ({caminho or 'corpo'})")
                andar(filho, f"{caminho}.{chave}" if caminho else chave)
        elif isinstance(valor, list | tuple):
            for i, filho in enumerate(valor):
                andar(filho, f"{caminho}[{i}]")
        elif not isinstance(valor, str | int | float | bool) and valor is not None:
            raise CorpoRecusado(f"valor de tipo {type(valor).__name__} em {caminho}")

    andar(corpo, "")
    copia = json.loads(json.dumps(dict(corpo), ensure_ascii=False))
    if len(json.dumps(copia, ensure_ascii=False).encode("utf-8")) > TAMANHO_MAXIMO_CORPO:
        raise CorpoRecusado("o corpo do aviso passou do tamanho maximo")
    return dict(copia)


def _linha_unica(valor: str, maximo: int) -> str:
    return " ".join(valor.split())[:maximo]


async def enfileirar(
    conexao: AsyncConnection,
    canal: str,
    destino: str,
    evento: str,
    assunto: str,
    corpo: Mapping[str, Any],
    chave_unica: str | None = None,
) -> bool:
    """Poe um aviso na fila, na transacao da conexao. Devolve ``False`` se a ``chave_unica`` ja existia."""
    if canal not in CANAIS:
        raise ValueError(f"canal desconhecido: {canal}")
    if not destino or not evento:
        raise ValueError("destino e evento sao obrigatorios")
    resultado = await conexao.execute(
        text(
            "INSERT INTO avisos (canal, destino, evento, assunto, corpo, chave_unica) "
            "VALUES (:canal, :destino, :evento, :assunto, CAST(:corpo AS jsonb), :chave) "
            "ON CONFLICT DO NOTHING"
        ),
        {
            "canal": canal,
            "destino": destino[:500],
            "evento": _linha_unica(evento, 80),
            "assunto": _linha_unica(assunto, 200),
            "corpo": json.dumps(conferir_corpo(corpo), ensure_ascii=False, sort_keys=True),
            "chave": chave_unica[:300] if chave_unica else None,
        },
    )
    novo = bool(resultado.rowcount)
    if novo:
        CONTADORES.somar("avisos_enfileirados", canal=canal, evento=_linha_unica(evento, 80))
    return novo


@dataclass(frozen=True, slots=True)
class Aviso:
    id: int
    canal: str
    destino: str
    evento: str
    assunto: str
    corpo: dict[str, Any]
    tentativas: int
    criado_em: datetime

    @property
    def identificador(self) -> str:
        """Identificador estavel do aviso, igual em todas as tentativas (webhook-id, Message-ID)."""
        return f"msg_{self.id}_{int(self.criado_em.timestamp())}"


class Envio(Protocol):
    """Um canal de envio. Levanta ``FalhaEnvio`` quando nao entrega."""

    async def enviar(self, conexao: AsyncConnection, aviso: Aviso) -> None: ...


Envios = Mapping[str, Envio]


class _CanalDesligado:
    """Canal sem configuracao neste papel: cada tentativa falha e a fila acaba desistindo."""

    def __init__(self, canal: str) -> None:
        self.canal = canal

    async def enviar(self, conexao: AsyncConnection, aviso: Aviso) -> None:
        raise FalhaEnvio(f"canal {self.canal} nao configurado neste papel")


def espera(tentativas: int, base: float = 30.0, teto: float = 3600.0) -> float:
    """Espera crescente antes da proxima tentativa: base, 2x base, 4x base... ate o teto.

    ``tentativas`` e quantas ja falharam (1 depois da primeira falha).
    """
    if tentativas < 1:
        return 0.0
    if base <= 0:
        return 0.0
    expoente = min(tentativas - 1, 30)
    return float(min(base * (2**expoente), teto))


@dataclass(frozen=True, slots=True)
class ConfigEnvio:
    retry_max: int = 5
    """``NOTIFY_RETRY_MAX``: novas tentativas depois da primeira."""
    espera_base: float = 30.0
    espera_teto: float = 3600.0
    lote: int = 50
    """Avisos por rodada do laco."""
    intervalo: float = 5.0
    """Segundos entre rodadas do laco quando a fila fica vazia."""
    papel: str = "rotinas"
    registrar_no_extrato: bool = True


@dataclass(slots=True)
class Resumo:
    enviados: int = 0
    reagendados: int = 0
    desistencias: int = 0
    ids: list[int] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.enviados + self.reagendados + self.desistencias


_SQL_PEGAR = """
SELECT id, canal, destino, evento, assunto, corpo, tentativas, criado_em
FROM avisos
WHERE situacao = 'pendente' AND proxima_em <= now()
ORDER BY proxima_em, id
LIMIT 1
FOR UPDATE SKIP LOCKED
"""


async def _pegar(conexao: AsyncConnection) -> Aviso | None:
    linha = (await conexao.execute(text(_SQL_PEGAR))).first()
    if linha is None:
        return None
    corpo = linha.corpo if isinstance(linha.corpo, dict) else json.loads(linha.corpo)
    return Aviso(
        id=int(linha.id),
        canal=str(linha.canal),
        destino=str(linha.destino),
        evento=str(linha.evento),
        assunto=str(linha.assunto),
        corpo=dict(corpo),
        tentativas=int(linha.tentativas),
        criado_em=linha.criado_em,
    )


async def _marcar_enviado(conexao: AsyncConnection, aviso: Aviso) -> None:
    await conexao.execute(
        text(
            "UPDATE avisos SET situacao = 'enviado', tentativas = tentativas + 1, enviado_em = now(),"
            " ultimo_erro = NULL WHERE id = :id"
        ),
        {"id": aviso.id},
    )


async def _marcar_falha(conexao: AsyncConnection, aviso: Aviso, falha: FalhaEnvio, config: ConfigEnvio) -> bool:
    """Soma a tentativa. Devolve ``True`` quando desiste."""
    tentativas = aviso.tentativas + 1
    desiste = falha.permanente or tentativas > config.retry_max
    if desiste:
        await conexao.execute(
            text("UPDATE avisos SET situacao = 'desistiu', tentativas = :t, ultimo_erro = :e WHERE id = :id"),
            {"t": tentativas, "e": falha.motivo, "id": aviso.id},
        )
        if config.registrar_no_extrato:
            from mcp_claro.extrato import registrar

            await registrar(
                conexao,
                papel=config.papel,
                tipo="aviso_desistiu",
                campos={"evento": aviso.evento, "tentativas": tentativas},
                dados={"aviso_id": aviso.id, "canal": aviso.canal, "erro": falha.motivo},
            )
        return True
    await conexao.execute(
        text(
            "UPDATE avisos SET tentativas = :t, ultimo_erro = :e,"
            " proxima_em = now() + make_interval(secs => :espera) WHERE id = :id"
        ),
        {
            "t": tentativas,
            "e": falha.motivo,
            "espera": espera(tentativas, config.espera_base, config.espera_teto),
            "id": aviso.id,
        },
    )
    return False


async def _enviar(c: AsyncConnection, envio: Envio, aviso: Aviso) -> FalhaEnvio | None:
    """Chama o canal num ponto de salvamento: erro inesperado vira falha comum, e a fila segue."""
    try:
        async with c.begin_nested():
            await envio.enviar(c, aviso)
    except FalhaEnvio as falha:
        return falha
    except Exception as erro:  # noqa: BLE001 - um aviso com defeito nao pode travar a fila inteira
        log.warning("aviso_erro_inesperado", aviso=aviso.id, canal=aviso.canal, erro=type(erro).__name__)
        return FalhaEnvio(f"erro inesperado no envio ({type(erro).__name__})")
    return None


async def processar_um(engine: AsyncEngine, envios: Envios, config: ConfigEnvio, resumo: Resumo) -> bool:
    """Pega um aviso pendente, envia e marca, numa transacao so. Devolve ``False`` se a fila estava vazia."""
    async with engine.begin() as c:
        aviso = await _pegar(c)
        if aviso is None:
            return False
        resumo.ids.append(aviso.id)
        falha = await _enviar(c, envios.get(aviso.canal) or _CanalDesligado(aviso.canal), aviso)
        if falha is None:
            await _marcar_enviado(c, aviso)
            resumo.enviados += 1
            CONTADORES.somar("avisos_enviados", canal=aviso.canal)
            log.info("aviso_enviado", aviso=aviso.id, canal=aviso.canal, evento=aviso.evento)
        elif await _marcar_falha(c, aviso, falha, config):
            resumo.desistencias += 1
            CONTADORES.somar("avisos_desistiu", canal=aviso.canal)
            log.warning("aviso_desistiu", aviso=aviso.id, canal=aviso.canal, motivo=falha.motivo)
        else:
            resumo.reagendados += 1
            CONTADORES.somar("avisos_falha", canal=aviso.canal)
            log.info("aviso_reagendado", aviso=aviso.id, canal=aviso.canal, motivo=falha.motivo)
        return True


async def processar_pendentes(engine: AsyncEngine, envios: Envios, config: ConfigEnvio | None = None) -> Resumo:
    """Uma rodada: ate ``config.lote`` avisos, cada um na sua transacao."""
    config = config or ConfigEnvio()
    resumo = Resumo()
    for _ in range(config.lote):
        if not await processar_um(engine, envios, config, resumo):
            break
    return resumo


@contextlib.asynccontextmanager
async def laco_de_envio(engine: AsyncEngine, envios: Envios, config: ConfigEnvio) -> AsyncIterator[None]:
    """Laco de fundo que esvazia a fila. Falha de banco ou de canal nunca derruba o papel."""

    async def laco() -> None:
        while True:
            try:
                resumo = await processar_pendentes(engine, envios, config)
            except Exception:  # noqa: BLE001 - a fila tenta de novo na proxima rodada
                log.warning("avisos_rodada_falhou")
                resumo = Resumo()
            if resumo.total < config.lote:
                await asyncio.sleep(config.intervalo)

    tarefa = asyncio.create_task(laco(), name="avisos")
    try:
        yield
    finally:
        tarefa.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await tarefa
