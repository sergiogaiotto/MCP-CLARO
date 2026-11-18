"""Uma rodada da ronda das portas, no papel ``rotinas``: tentar, guardar, decidir e, se for o caso, fechar.

1. Faz as tentativas pelo endereco publico (``mcp_claro.ronda.casos``).
2. Guarda cada tentativa na tabela ``ronda`` (so de acrescentar), com o desfecho em ``detalhes``.
   ``defendeu`` e falso so quando houve passagem indevida.
3. Se houve passagem, registra ``ronda_falha`` no extrato.
4. Conta as rodadas seguidas com passagem e com o teste de controle em dia, desde a ultima mudanca
   do estado da entrada. A cada ``PATROL_FAILS_TO_CLOSE`` delas, a entrada desce um degrau:
   ``aberta`` -> ``so_leitura`` -> ``fechada``. Cada degrau vai ao extrato (``ronda_fechou_entrada``
   e ``estado_entrada_mudou``) e a um aviso ao ADMIN. Reabrir e decisao humana registrada: a
   reabertura muda o estado e zera a contagem.
5. Com ``PATROL_AUTOCLOSE_ENABLED`` desligado, a ronda nao fecha nada: registra
   ``ronda_sem_fechamento`` e avisa o ADMIN uma vez por sequencia.

A decisao trava a linha de ``estado_entrada`` (``FOR UPDATE``): duas rodadas ao mesmo tempo nao
descem dois degraus de uma vez.
"""

from __future__ import annotations

import json
import ssl
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import httpx2
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro import __version__
from mcp_claro.avisos import avisar_admin
from mcp_claro.config.settings import Parametros
from mcp_claro.extrato import frase, registrar
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.regras import estado
from mcp_claro.regras.bloqueios import quem_rotina
from mcp_claro.ronda.casos import Caso, Resumo, Situacao, Tentativa, fazer_tentativas
from mcp_claro.ronda.config import CLIENTE_RONDA, ConfigRonda, IdentidadePrograma, identidade_da_ronda, ler
from mcp_claro.ronda.cracha import ChavesProprias, FonteCrachas, fonte_do_programa
from mcp_claro.rotinas.agendador import Tarefa

log = logger("mcp_claro.ronda")

PROXIMO_NIVEL: Final[dict[str, str]] = {"aberta": "so_leitura", "so_leitura": "fechada"}
NIVEL_LEGIVEL: Final[dict[str, str]] = {"aberta": "aberta", "so_leitura": "so de leitura", "fechada": "fechada"}
_RODADAS_CONSULTADAS: Final = 200
_CHAVES_PROPRIAS: Final = ChavesProprias()


@dataclass(frozen=True, slots=True)
class Rodada:
    id: uuid.UUID
    tentativas: list[Tentativa]
    seguidas: int
    """Rodadas seguidas com passagem e controle em dia, desde a ultima mudanca da entrada (contando esta)."""
    nivel_antes: str
    nivel_depois: str

    @property
    def resumo(self) -> Resumo:
        return Resumo(self.tentativas)

    @property
    def fechou(self) -> bool:
        return self.nivel_antes != self.nivel_depois


def contexto_tls(arquivo_ca: str | None) -> ssl.SSLContext:
    """Repositorio do sistema, mais a autoridade de ``PATROL_CA_FILE`` / ``WATCH_CA_FILE`` se houver."""
    contexto = ssl.create_default_context()
    if arquivo_ca:
        contexto.load_verify_locations(cafile=arquivo_ca)
    return contexto


def cliente_publico(arquivo_ca: str | None, prazo: float, nome: str = "mcp-claro-ronda") -> httpx2.AsyncClient:
    """Cliente HTTP para o endereco publico: sem seguir redirecionamento, sem proxy do ambiente."""
    return httpx2.AsyncClient(
        verify=contexto_tls(arquivo_ca),
        timeout=httpx2.Timeout(prazo),
        follow_redirects=False,
        trust_env=False,
        headers={"User-Agent": f"{nome}/{__version__}"},
    )


def fonte_da_ronda(p: Parametros) -> FonteCrachas | None:
    """Fonte de crachas da identidade ``mcp-claro-ronda`` pelos parametros ``PATROL_*``.

    Em piloto ou producao, levanta ``ConfiguracaoInvalida`` sem ``PATROL_KEY_FILE``. A fonte guarda
    os crachas antigos (caso "vencido"): crie uma so e use-a em todas as rodadas.
    """
    ident: IdentidadePrograma = identidade_da_ronda(p)
    arquivo_ca = ConfigRonda.de_parametros(p).arquivo_ca
    return fonte_do_programa(ident, verify=contexto_tls(arquivo_ca) if arquivo_ca else None)


_SQL_FERRAMENTA_ALHEIA = """
SELECT f.nome_vitrine
FROM ferramentas_revisadas f
JOIN modelos_ordem_ferramentas mf ON mf.sistema_id = f.sistema_id AND mf.nome_original = f.nome_original
JOIN ordens_servico o ON o.modelo_id = mf.modelo_id AND o.situacao = 'ativa' AND o.fim > now()
JOIN aplicativos a ON a.id = o.aplicativo_id AND a.client_id <> :ronda
WHERE f.situacao = 'ativa'
  AND NOT EXISTS (
    SELECT 1
    FROM ordens_servico o2
    JOIN aplicativos a2 ON a2.id = o2.aplicativo_id AND a2.client_id = :ronda
    JOIN modelos_ordem_ferramentas mf2 ON mf2.modelo_id = o2.modelo_id
    WHERE o2.situacao = 'ativa' AND mf2.sistema_id = f.sistema_id AND mf2.nome_original = f.nome_original
  )
ORDER BY f.revisado_em DESC, f.nome_vitrine
LIMIT 1
"""


async def ferramenta_alheia(engine: AsyncEngine, client_id_ronda: str) -> str | None:
    """Uma ferramenta que esta na vitrine de outra pessoa e nao na da ronda; ``None`` se nao houver."""
    async with engine.connect() as c:
        valor = (await c.execute(text(_SQL_FERRAMENTA_ALHEIA), {"ronda": client_id_ronda})).scalar_one_or_none()
    return str(valor) if valor else None


async def guardar(c: AsyncConnection, rodada: uuid.UUID, tentativas: Sequence[Tentativa]) -> None:
    for t in tentativas:
        await c.execute(
            text(
                "INSERT INTO ronda (rodada, caso, esperado, obtido, defendeu, detalhes)"
                " VALUES (:r, :c, :e, :o, :d, CAST(:x AS jsonb))"
            ),
            {
                "r": rodada,
                "c": t.caso,
                "e": t.esperado[:200],
                "o": t.obtido[:200],
                "d": t.defendeu,
                "x": json.dumps(t.para_tabela(), ensure_ascii=False, sort_keys=True),
            },
        )


_SQL_RODADAS = """
SELECT rodada,
       bool_or(detalhes ->> 'situacao' = 'passou') AS falhou,
       bool_or(caso = 'controle' AND detalhes ->> 'situacao' = 'conforme') AS controle_ok
FROM ronda
WHERE feita_em > :desde
GROUP BY rodada
ORDER BY max(id) DESC
LIMIT :limite
"""


SQL_RODADAS_VIGIA: Final = _SQL_RODADAS.replace("FROM ronda", "FROM rondas_vigia")
"""A mesma conta, no banco do vigia."""


async def rodadas_seguidas(c: AsyncConnection, desde: datetime | None, *, sql: str = _SQL_RODADAS) -> int:
    """Rodadas seguidas, da mais nova para tras, com passagem indevida e controle em dia, depois de ``desde``."""
    linhas = (
        await c.execute(text(sql), {"desde": desde or datetime(1970, 1, 1, tzinfo=UTC), "limite": _RODADAS_CONSULTADAS})
    ).all()
    seguidas = 0
    for linha in linhas:
        if not (linha.falhou and linha.controle_ok):
            break
        seguidas += 1
    return seguidas


async def _decidir(
    c: AsyncConnection,
    config: ConfigRonda,
    rodada_id: uuid.UUID,
    resumo: Resumo,
    emails_admin: Sequence[str],
) -> tuple[int, str, str]:
    linha = (await c.execute(text("SELECT nivel, mudou_em FROM estado_entrada WHERE id = 1 FOR UPDATE"))).first()
    if linha is None:
        return 0, "fechada", "fechada"  # sem linha, a entrada ja vale como fechada (regras.estado)
    nivel = str(linha.nivel)
    seguidas = await rodadas_seguidas(c, linha.mudou_em)
    n = config.falhas_para_fechar
    if seguidas < n or nivel not in PROXIMO_NIVEL:
        return seguidas, nivel, nivel
    linhas_aviso = [f"Passou: {resumo.motivo_legivel()}."] if resumo.falhou else []
    if not config.fechar_sozinha:
        if seguidas == n:
            texto = await _registrar_frase(
                c, "ronda_sem_fechamento", {"rondas": seguidas, "nivel": NIVEL_LEGIVEL[nivel]}, rodada_id, nivel
            )
            await avisar_admin(
                c,
                "ronda_sem_fechamento",
                "MCP-CLARO: a ronda achou passagem indevida e o fechamento sozinho esta desligado",
                {"texto": texto, "linhas": linhas_aviso, "nivel": nivel, "rondas": seguidas},
                f"ronda:{rodada_id}",
                emails_admin,
            )
        return seguidas, nivel, nivel
    novo = PROXIMO_NIVEL[nivel]
    motivo = f"Ronda das portas: passagem indevida em {seguidas} rondas seguidas ({resumo.motivo_legivel()})."
    await estado.mudar_entrada(c, novo, quem_rotina("ronda"), motivo, papel="rotinas")
    texto = await _registrar_frase(
        c, "ronda_fechou_entrada", {"rondas": seguidas, "nivel": NIVEL_LEGIVEL[novo]}, rodada_id, novo
    )
    await avisar_admin(
        c,
        "ronda_fechou_entrada",
        f"MCP-CLARO: a ronda deixou a entrada {NIVEL_LEGIVEL[novo]}",
        {"texto": texto, "linhas": linhas_aviso, "nivel": novo, "rondas": seguidas},
        f"ronda:{rodada_id}",
        emails_admin,
    )
    CONTADORES.somar("ronda_fechou_entrada", nivel=novo)
    log.warning("ronda_fechou_entrada", nivel=novo, rondas=seguidas)
    return seguidas, nivel, novo


async def _registrar_frase(
    c: AsyncConnection, tipo: str, campos: dict[str, object], rodada_id: uuid.UUID, nivel: str
) -> str:
    await registrar(c, papel="rotinas", tipo=tipo, campos=campos, dados={"rodada": str(rodada_id), "nivel": nivel})
    return frase(tipo, **campos)


async def rodar_ronda(
    p: Parametros,
    engine: AsyncEngine,
    *,
    fonte: FonteCrachas | None,
    emails_admin: Sequence[str] = (),
    config: ConfigRonda | None = None,
    http: httpx2.AsyncClient | None = None,
) -> Rodada:
    """Uma rodada da ronda, para o laco das rotinas chamar a cada ``PATROL_INTERVAL_SECONDS``.

    ``fonte`` sai de ``fonte_da_ronda(p)``, criada uma vez na partida. Sem fonte (sem chave da
    ronda), a rodada so grava um controle inconclusivo. ``http`` e ``config`` servem aos testes.
    """
    cfg = config or ConfigRonda.de_parametros(p)
    rodada_id = uuid.uuid4()
    if fonte is None:
        tentativas = [
            Tentativa(
                Caso.CONTROLE,
                "cracha aceito (resposta JSON-RPC)",
                "sem identidade da ronda (PATROL_KEY_FILE)",
                Situacao.INCONCLUSIVO,
                {"por_que": "sem identidade da ronda"},
            )
        ]
    else:
        alheia = await ferramenta_alheia(engine, str(ler(p, "PATROL_CLIENT_ID", CLIENTE_RONDA)))
        proprio = http is None
        cliente = http or cliente_publico(cfg.arquivo_ca, cfg.prazo_tentativa)
        try:
            tentativas = await fazer_tentativas(
                cfg.alvo,
                fonte,
                cliente,
                emissor=p.IDP_ISSUER_URL,
                recurso=p.recurso,
                ferramenta_alheia=alheia,
                chaves=_CHAVES_PROPRIAS,
            )
        finally:
            if proprio:
                await cliente.aclose()
    resumo = Resumo(tentativas)
    async with engine.begin() as c:
        await guardar(c, rodada_id, tentativas)
        if resumo.falhou:
            await registrar(
                c,
                papel="rotinas",
                tipo="ronda_falha",
                campos={"motivo": resumo.motivo_legivel()},
                dados={"rodada": str(rodada_id), "casos": [t.caso for t in resumo.passagens]},
            )
    async with engine.begin() as c:
        seguidas, antes, depois = await _decidir(c, cfg, rodada_id, resumo, emails_admin)
    desfecho = "passou" if resumo.falhou else ("conforme" if resumo.controle_ok else "inconclusiva")
    CONTADORES.somar("ronda_rodadas", desfecho=desfecho)
    if resumo.falhou:
        log.warning("ronda_passagem_indevida", casos=[t.caso for t in resumo.passagens], seguidas=seguidas)
    elif not resumo.controle_ok:
        log.warning("ronda_inconclusiva")
    return Rodada(rodada_id, tentativas, seguidas, antes, depois)


def tarefa_da_ronda(p: Parametros, engine: AsyncEngine, emails_admin: Sequence[str], *, prazo: float = 600.0) -> Tarefa:
    """Tarefa para o agendador das rotinas: uma fonte de crachas so, criada aqui, e uma rodada por intervalo.

    Levanta ``ConfiguracaoInvalida`` em piloto ou producao sem ``PATROL_KEY_FILE`` (a partida recusa).
    """
    fonte = fonte_da_ronda(p)
    cfg = ConfigRonda.de_parametros(p)
    emails = tuple(emails_admin)
    return Tarefa(
        "ronda",
        cfg.intervalo,
        lambda: rodar_ronda(p, engine, fonte=fonte, emails_admin=emails, config=cfg),
        prazo,
    )
