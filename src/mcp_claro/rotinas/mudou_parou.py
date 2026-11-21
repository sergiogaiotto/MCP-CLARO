"""Mudou-parou: o que cada sistema ativo declara hoje, comparado com o que o ADMIN revisou.

A cada rodada, as rotinas pedem ao despachante o que o sistema declara hoje (``FonteDeclaracoes``; o despachante le
com a chave de leitura das declaracoes guardada pelo ADMIN). As rotinas nunca falam com os sistemas nem abrem chaves.

Sistemas proprios (Fases 1 e 2), ferramenta por ferramenta. A base e o que o ADMIN revisou (impressao, esquema e
etiquetas guardados na revisao), nao a ultima leitura:

- igual: nada a fazer;
- diferente e pedindo mais (campo novo, obrigatorio novo, deixou de ser so leitura, ficou destrutiva; ver
  ``cadastro.impressao.pede_mais``): a revisada fica ``suspensa`` com motivo "mudou, parou", sai da vitrine, vai ao
  extrato e o ADMIN recebe aviso. So volta com nova revisao do ADMIN;
- diferente sem pedir mais (por exemplo, so o texto): so registra no extrato, uma vez por nova definicao;
- sem resposta: fica em ``sem_resposta`` e tenta de novo na proxima rodada (D28).

Servidores de terceiros (Fase 3), o servidor inteiro, so os que tem revisao do servidor vigente (sem ela, nada
dele aparece para as pessoas, e nada ha a conferir). A base e ``revisoes_servidor.declaradas`` da revisao vigente:

- a impressao do servidor (``cadastro.impressao.impressao_servidor``) igual a da revisao: nada, e a contagem de
  rodadas sem resposta volta a zero;
- diferente (ferramenta mudada, mesmo so no texto, nova ou sumida; D5): ``saida.quarentena.por_em_quarentena``
  poe o servidor inteiro em quarentena (suspende os limites, cancela o que espera na fila e grava o extrato) e,
  so quando ela devolve a quarentena, o ADMIN recebe aviso por e-mail e webhook, na mesma transacao. A quarentena
  so vale contra a revisao comparada (critica B1): se outra copia das rotinas ja pos em quarentena, ou se o ADMIN
  concluiu uma revisao nova no meio da rodada, nada muda. Impressao diferente com a lista da revisao igual, nome a
  nome, ao que o servidor declara (revisao gravada fora da receita; o banco nao confere uma contra a outra) tambem
  vai a quarentena, com zero ferramentas mudadas: sem saber o que o ADMIN aprovou, fica o lado seguro. O aviso ao
  ADMIN, nesse caso, e o proprio (``sistema_em_quarentena_revisao_inconsistente``);
- sem resposta a conferencia (critica A5): soma ``sistemas.conferencias_sem_resposta``; na segunda rodada seguida, o
  servidor vai a quarentena com o motivo "nao respondeu a conferencia" (``MOTIVO_SEM_RESPOSTA``) e o ADMIN recebe
  aviso. Uma resposta no meio zera a contagem. Uma rodada em que a conferencia nem aconteceu por um problema do
  MCP-CLARO (``ConferenciaNaoFeita``) nao conta nem zera: a culpa nao e do servidor. Isso vale para o despachante
  fora do ar (sem conexao, 503, 404) e para os motivos de ``rotinas.despachante.MOTIVOS_DO_MCP_CLARO``, como a sala
  de visitas fora (``sala_fora``) e a chave das declaracoes que o cofre nao abriu (``chave_indisponivel``). So o
  servidor que nao entregou as declaracoes (por exemplo, 502 ``declaracoes_indisponiveis``) conta.

Os nomes das ferramentas de um terceiro sao texto do fornecedor: vao so ao extrato (``dados.ferramentas``, com os
escondidos marcados, por ``por_em_quarentena``), nunca ao log nem ao aviso do ADMIN.

As rodadas seguidas sao as da tarefa ``mudou_parou``, que o agendador roda uma de cada vez na instalacao. Com duas
copias das rotinas no ar, cada uma no seu intervalo, duas rodadas podem cair a segundos uma da outra. Por isso a
soma guarda a hora (do banco) em ``sistemas.conferencia_sem_resposta_em``, e uma rodada sem resposta so soma quando
a ultima soma tem mais de metade do intervalo do mudou-parou (ou quando nao ha soma na contagem de agora): duas
rodadas perto uma da outra contam uma vez so.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.avisos import avisar_admin
from mcp_claro.cadastro import quarentena as cadastro_quarentena
from mcp_claro.cadastro.impressao import impressao, impressao_servidor, pede_mais
from mcp_claro.extrato import registrar
from mcp_claro.extrato.frases import aviso_admin
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.saida.quarentena import Quarentena, por_em_quarentena

log = logger("mcp_claro.rotinas.mudou_parou")

MOTIVO_SUSPENSAO = "mudou, parou: o sistema passou a pedir mais que o revisado; precisa de nova revisao do ADMIN"

RODADAS_SEM_RESPOSTA: Final = 2
"""Rodadas seguidas sem resposta a conferencia que levam um servidor de terceiros a quarentena (critica A5)."""


class ConferenciaNaoFeita(Exception):
    """A conferencia nao aconteceu por um problema do MCP-CLARO, nao do sistema (despachante fora do ar, sistema que
    deixou de estar ativo no meio, ou um motivo do MCP-CLARO na resposta do despachante). Nao conta como rodada sem
    resposta, nem zera a contagem."""


class FonteDeclaracoes(Protocol):
    async def declaracoes(self, sistema_id: uuid.UUID) -> list[dict[str, Any]]:
        """O que o sistema declara hoje (``tools/list``). Levanta ``ConferenciaNaoFeita`` quando a culpa nao e do
        sistema; qualquer outra excecao e o sistema que nao entregou as declaracoes."""
        ...


@dataclass(slots=True)
class ResultadoMudouParou:
    sistemas: int = 0
    suspensas: list[str] = field(default_factory=list)
    mudaram: list[str] = field(default_factory=list)
    sumiram: list[str] = field(default_factory=list)
    sem_resposta: list[str] = field(default_factory=list)
    quarentenas: list[str] = field(default_factory=list)
    """Servidores de terceiros que esta rodada pos em quarentena (nomes do cadastro)."""


async def conferir_sistema(
    engine: AsyncEngine,
    fonte: FonteDeclaracoes,
    sistema_id: uuid.UUID,
    sistema_nome: str,
    emails_admin: Sequence[str],
    resultado: ResultadoMudouParou,
) -> None:
    """Mudou-parou de um sistema proprio, ferramenta por ferramenta (Fases 1 e 2)."""
    async with engine.connect() as c:
        revisadas = (
            await c.execute(
                text(
                    "SELECT id, nome_original, nome_vitrine, esquema_entrada, esquema_saida, etiquetas, impressao"
                    " FROM ferramentas_revisadas WHERE sistema_id = :s AND situacao = 'ativa' ORDER BY nome_original"
                ),
                {"s": sistema_id},
            )
        ).all()
        vistas_antes = {
            str(nome): str(imp)
            for nome, imp in await c.execute(
                text("SELECT nome, impressao FROM ferramentas_vistas WHERE sistema_id = :s"), {"s": sistema_id}
            )
        }
    if not revisadas:
        return
    declaradas = await fonte.declaracoes(sistema_id)
    por_nome = {str(d["name"]): d for d in declaradas if isinstance(d.get("name"), str)}
    async with engine.begin() as c:
        for r in revisadas:
            declarada = por_nome.get(r.nome_original)
            if declarada is None:
                resultado.sumiram.append(r.nome_vitrine)
                log.info("ferramenta_sumiu", sistema=sistema_nome, ferramenta=r.nome_vitrine)
                continue
            nova = impressao(declarada)
            if nova == r.impressao:
                continue
            revisada = {
                "name": r.nome_original,
                "inputSchema": r.esquema_entrada,
                "outputSchema": r.esquema_saida,
                "annotations": r.etiquetas,
            }
            ferramenta_id = uuid.UUID(str(r.id))
            if pede_mais(revisada, declarada):
                mudou = await c.execute(
                    text(
                        "UPDATE ferramentas_revisadas SET situacao = 'suspensa', motivo = :m"
                        " WHERE id = :i AND situacao = 'ativa'"
                    ),
                    {"m": MOTIVO_SUSPENSAO, "i": ferramenta_id},
                )
                if not mudou.rowcount:
                    continue
                resultado.suspensas.append(r.nome_vitrine)
                CONTADORES.somar("mudou_parou_suspensas")
                await registrar(
                    c,
                    papel="rotinas",
                    tipo="ferramenta_suspensa",
                    campos={"ferramenta": r.nome_vitrine},
                    sistema_id=sistema_id,
                    ferramenta=r.nome_vitrine,
                    dados={
                        "ferramenta_id": str(ferramenta_id),
                        "impressao_revisada": r.impressao,
                        "impressao_nova": nova,
                        "motivo": "pede_mais",
                    },
                )
                await avisar_admin(
                    c,
                    "ferramenta_suspensa",
                    "MCP-CLARO: ferramenta suspensa (mudou, parou)",
                    {
                        "texto": (
                            f"A ferramenta {r.nome_vitrine}, do sistema {sistema_nome}, passou a pedir mais do que "
                            "foi revisado. Ela saiu da vitrine de todos ate uma nova revisao nas telas do ADMIN."
                        ),
                        "sistema": sistema_nome,
                        "ferramenta": r.nome_vitrine,
                        "ferramenta_id": str(ferramenta_id),
                        "impressao_revisada": r.impressao,
                        "impressao_nova": nova,
                    },
                    f"ferramenta_suspensa:{ferramenta_id}:{nova}",
                    emails_admin,
                )
                log.warning("ferramenta_suspensa", sistema=sistema_nome, ferramenta=r.nome_vitrine)
            else:
                if vistas_antes.get(r.nome_original) == nova:
                    continue  # esta mudanca ja foi vista e registrada numa rodada anterior
                resultado.mudaram.append(r.nome_vitrine)
                CONTADORES.somar("mudou_parou_mudou_sem_pedir_mais")
                await registrar(
                    c,
                    papel="rotinas",
                    tipo="ferramenta_mudou",
                    campos={"ferramenta": r.nome_vitrine},
                    sistema_id=sistema_id,
                    ferramenta=r.nome_vitrine,
                    dados={
                        "ferramenta_id": str(ferramenta_id),
                        "impressao_revisada": r.impressao,
                        "impressao_nova": nova,
                    },
                )


# --- Servidores de terceiros (Fase 3) ---------------------------------------------------------------------------


def _contar_quarentena(feita: Quarentena, motivo: str) -> None:
    """Depois do commit: a quarentena (``mudou`` ou ``sem_resposta``) e as acoes que ela cancelou. ``acoes_paradas``
    leva o mesmo rotulo que a porta de saida usa para a acao parada pela mesma causa (``MOTIVO_PARADA``): o painel
    "Acoes paradas antes de sair, por motivo" mostra uma serie so para a quarentena."""
    CONTADORES.somar("mudou_parou_quarentenas", motivo=motivo)
    if feita.paradas:
        CONTADORES.somar("acoes_paradas", valor=len(feita.paradas), motivo=cadastro_quarentena.MOTIVO_PARADA)


def _corpo_do_aviso(texto: str, feita: Quarentena, revisao_id: uuid.UUID, **mais: Any) -> dict[str, Any]:
    """Corpo do aviso ao ADMIN: o nome do cadastro, numeros e identificadores. Nenhum nome de ferramenta (texto do
    fornecedor), nenhum protocolo de pessoa."""
    return {
        "texto": texto,
        "sistema": feita.nome,
        "sistema_id": str(feita.sistema_id),
        "revisao": str(revisao_id),
        "paradas": len(feita.paradas),
        "limites_suspensos": feita.limites_suspensos,
        **mais,
    }


async def _zerar_sem_resposta(engine: AsyncEngine, sistema_id: uuid.UUID, revisao_id: uuid.UUID) -> None:
    """O servidor respondeu: a contagem de rodadas seguidas sem resposta volta a zero, e a hora da ultima soma
    tambem (so contra a revisao lida)."""
    async with engine.begin() as c:
        await c.execute(
            text(
                "UPDATE sistemas SET conferencias_sem_resposta = 0, conferencia_sem_resposta_em = NULL"
                " WHERE id = :i AND revisao_servidor_id = :b"
                " AND (conferencias_sem_resposta > 0 OR conferencia_sem_resposta_em IS NOT NULL)"
            ),
            {"i": sistema_id, "b": revisao_id},
        )


SQL_SOMAR_SEM_RESPOSTA: Final = (
    "UPDATE sistemas SET conferencias_sem_resposta = conferencias_sem_resposta + 1,"
    " conferencia_sem_resposta_em = now()"
    " WHERE id = :i AND situacao = 'ativo' AND revisao_servidor_id = :b"
    " AND (conferencia_sem_resposta_em IS NULL"
    " OR conferencia_sem_resposta_em < now() - make_interval(secs => :meio))"
    " RETURNING conferencias_sem_resposta"
)
"""Uma rodada sem resposta, so contra a revisao lida, com o servidor ``ativo`` e com a ultima soma a mais de metade
do intervalo (``:meio``, em segundos). A hora e a do banco: as copias das rotinas comparam o mesmo relogio. Duas
copias que somam ao mesmo tempo esperam uma pela outra na linha, e a segunda ja ve a hora da primeira."""


async def _sem_resposta_de_terceiro(
    engine: AsyncEngine,
    sistema_id: uuid.UUID,
    sistema_nome: str,
    revisao_id: uuid.UUID,
    emails_admin: Sequence[str],
    resultado: ResultadoMudouParou,
    *,
    intervalo: float,
) -> None:
    """Uma rodada sem resposta a conferencia; na ``RODADAS_SEM_RESPOSTA``-esima seguida, a quarentena (critica A5).

    A contagem so sobe contra a revisao lida e com o servidor ``ativo``: uma conclusao nova no meio zera a conta
    (a guarda do banco zera quando a revisao vigente muda) e a quarentena nao vale (``base``). E so sobe quando a
    ultima soma tem mais de metade de ``intervalo`` (o do mudou-parou, em segundos): a rodada de outra copia das
    rotinas, logo depois, nao conta de novo (``SQL_SOMAR_SEM_RESPOSTA``)."""
    resultado.sem_resposta.append(sistema_nome)
    CONTADORES.somar("mudou_parou_sem_resposta")
    async with engine.begin() as c:
        rodadas = (
            await c.execute(
                text(SQL_SOMAR_SEM_RESPOSTA), {"i": sistema_id, "b": revisao_id, "meio": max(0.0, intervalo) / 2}
            )
        ).scalar_one_or_none()
    if rodadas is None or int(rodadas) < RODADAS_SEM_RESPOSTA:
        return
    async with engine.begin() as c:
        feita = await por_em_quarentena(
            c, sistema_id=sistema_id, base=revisao_id, papel="rotinas", rodadas=int(rodadas)
        )
        if feita is None:
            return  # outra copia ja pos em quarentena, ou o ADMIN concluiu uma revisao nova no meio
        assunto, texto = aviso_admin("sistema_em_quarentena_sem_resposta", sistema=feita.nome, rodadas=int(rodadas))
        await avisar_admin(
            c,
            "sistema_em_quarentena_sem_resposta",
            assunto,
            _corpo_do_aviso(texto, feita, revisao_id, rodadas=int(rodadas)),
            f"sistema_em_quarentena_sem_resposta:{sistema_id}:{revisao_id}",
            emails_admin,
        )
    resultado.quarentenas.append(sistema_nome)
    _contar_quarentena(feita, "sem_resposta")
    log.warning("sistema_em_quarentena_sem_resposta", sistema=sistema_nome, rodadas=int(rodadas))


async def conferir_terceiro(
    engine: AsyncEngine,
    fonte: FonteDeclaracoes,
    sistema_id: uuid.UUID,
    sistema_nome: str,
    revisao_id: uuid.UUID,
    emails_admin: Sequence[str],
    resultado: ResultadoMudouParou,
    *,
    intervalo: float,
) -> None:
    """Mudou-parou de um servidor de terceiros revisado: o servidor inteiro contra a revisao vigente ``revisao_id``.
    ``intervalo``: o do mudou-parou, em segundos (ver ``_sem_resposta_de_terceiro``)."""
    async with engine.connect() as c:
        revisao = (
            await c.execute(
                text("SELECT id, impressao, declaradas FROM revisoes_servidor WHERE id = :r AND sistema_id = :s"),
                {"r": revisao_id, "s": sistema_id},
            )
        ).first()
    if revisao is None:
        return
    try:
        definicoes = await fonte.declaracoes(sistema_id)
    except ConferenciaNaoFeita as erro:
        resultado.sem_resposta.append(sistema_nome)
        CONTADORES.somar("mudou_parou_nao_conferido")
        log.warning("mudou_parou_nao_conferido", sistema=sistema_nome, erro=type(erro).__name__)
        return
    except Exception as erro:  # noqa: BLE001 - o servidor que nao entregou conta uma rodada sem resposta
        log.warning("mudou_parou_sem_resposta", sistema=sistema_nome, erro=type(erro).__name__)
        await _sem_resposta_de_terceiro(
            engine, sistema_id, sistema_nome, revisao_id, emails_admin, resultado, intervalo=intervalo
        )
        return
    agora = cadastro_quarentena.declaradas(definicoes)
    nova = impressao_servidor((d["nome"], d["impressao"]) for d in agora)
    if nova == revisao.impressao:
        await _zerar_sem_resposta(engine, sistema_id, revisao_id)
        return
    base: Mapping[str, str] = cadastro_quarentena.base_da_revisao(revisao.declaradas)
    achadas = cadastro_quarentena.diferencas(base, {d["nome"]: d["impressao"] for d in agora})
    evento = "sistema_em_quarentena"
    if not achadas:
        # A lista da revisao bate nome a nome com o que o servidor declara, mas a impressao gravada nao e a dela: a
        # revisao nao foi gravada pela receita, e nao se sabe o que o ADMIN aprovou. Quarentena do mesmo jeito (o
        # lado seguro), com zero ferramentas mudadas e aviso proprio; o log avisa a equipe.
        evento = "sistema_em_quarentena_revisao_inconsistente"
        log.warning("revisao_do_servidor_sem_a_impressao_da_lista", sistema=sistema_nome)
    async with engine.begin() as c:
        feita = await por_em_quarentena(c, sistema_id=sistema_id, base=revisao_id, papel="rotinas", diferencas=achadas)
        if feita is None:
            return  # outra copia ja pos em quarentena, ou o ADMIN concluiu uma revisao nova no meio (critica B1)
        if achadas:
            assunto, texto = aviso_admin(evento, sistema=feita.nome, quantas=len(achadas))
        else:
            assunto, texto = aviso_admin(evento, sistema=feita.nome)
        mudancas = {tipo: sum(1 for d in achadas if d.tipo == tipo) for tipo in ("mudou", "nova", "sumiu")}
        await avisar_admin(
            c,
            evento,
            assunto,
            _corpo_do_aviso(
                texto,
                feita,
                revisao_id,
                quantas=len(achadas),
                mudancas=mudancas,
                impressao_revisada=str(revisao.impressao),
                impressao_nova=nova,
            ),
            f"{evento}:{sistema_id}:{revisao_id}:{nova}",
            emails_admin,
        )
    resultado.quarentenas.append(sistema_nome)
    _contar_quarentena(feita, "mudou")
    log.warning("sistema_em_quarentena", sistema=sistema_nome, mudancas=len(achadas))


async def conferir_sistemas(
    engine: AsyncEngine, fonte: FonteDeclaracoes, emails_admin: Sequence[str], *, intervalo: float
) -> ResultadoMudouParou:
    """Uma rodada do mudou-parou: sistemas proprios ativos e servidores de terceiros ativos com revisao do servidor
    vigente. Um sistema que nao responde nao segura os outros. ``intervalo``: o da tarefa ``mudou_parou``, em
    segundos (``ConfigRotinas.intervalo_mudou_parou``); uma rodada sem resposta de um terceiro so soma quando a
    ultima soma tem mais de metade dele."""
    resultado = ResultadoMudouParou()
    async with engine.connect() as c:
        sistemas = (
            await c.execute(
                text(
                    "SELECT id, nome, origem, revisao_servidor_id FROM sistemas"
                    " WHERE situacao = 'ativo' AND (origem = 'proprio' OR revisao_servidor_id IS NOT NULL)"
                    " ORDER BY nome"
                )
            )
        ).all()
    for s in sistemas:
        resultado.sistemas += 1
        sistema_id, nome = uuid.UUID(str(s.id)), str(s.nome)
        try:
            if s.origem == "proprio":
                await conferir_sistema(engine, fonte, sistema_id, nome, emails_admin, resultado)
            else:
                revisao_id = uuid.UUID(str(s.revisao_servidor_id))
                await conferir_terceiro(
                    engine, fonte, sistema_id, nome, revisao_id, emails_admin, resultado, intervalo=intervalo
                )
        except Exception as erro:  # noqa: BLE001 - um sistema mudo nao para o mudou-parou dos outros
            if nome not in resultado.sem_resposta:
                resultado.sem_resposta.append(nome)
            CONTADORES.somar("mudou_parou_sem_resposta")
            log.warning("mudou_parou_sem_resposta", sistema=nome, erro=type(erro).__name__)
    return resultado


conferir_sistemas_proprios = conferir_sistemas
"""Apelido de antes da Fase 3: a rodada inteira (proprios e terceiros revisados)."""
