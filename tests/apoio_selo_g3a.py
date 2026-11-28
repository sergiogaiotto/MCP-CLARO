"""Apoio das provas do G3a da Fase 4 (porta com o numero, consulta e aviso de bloqueio), pelo banco e com os dubles.

Monta, como dono do banco (``tests.apoio_selos``), um sistema proprio na frente de um duble do selo (``exemplos``),
com o teste do selo aprovado gravado, uma pessoa de teste com a chave guardada e acoes ja confirmadas e liberadas na
fila. O despachante de verdade roda com o usuario ``mcp_claro_despachante`` (prova os GRANTs): a porta de saida
(``saida.despachante.PortaDeSaida``) e a consulta e o aviso (``selo.consulta.Perguntador``).

- Todo endereco de duble sai de ``tests.apoio_selos.endereco_de_duble``, com caminho unico.
- ``tests/cortador.py`` (so usado) fica, quando pedido, entre o despachante e o duble: corta a resposta da acao.
- O relogio da escada e o do banco: ``voltar_no_tempo`` muda, como dono e numa transacao curta, a hora do envio e das
  informacoes de uma operacao para tras (desliga e religa a guarda de "so de acrescentar" so nessa transacao), em vez
  de esperar os minutos da escada. Nada depende do relogio da maquina. O teste aprovado da cena fica um dia para tras
  (``teste_de_ontem``): o que as provas poem horas para tras ainda e depois dele.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import uuid
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from starlette.applications import Starlette

from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.despachante import chaves
from mcp_claro.saida.despachante import PortaDeSaida, ligacao_sem_sala
from mcp_claro.selo.consulta import Perguntador
from tests import apoio_g3a as g
from tests import apoio_selos as s
from tests.apoio import servidor
from tests.cifra_falsa import CifraFalsa
from tests.cortador import Cortador, cortador

CHAVE_DA_PESSOA = "chave-ana"
"""A chave de ``ana`` nos dubles (``exemplos.comum``)."""
EMAIL_ADMIN = "admin@exemplo.teste"
ACAO = "enviar_lembrete"
LEITURA = "listar_lembretes"
ARGUMENTOS = {"para": "caixa-de-teste@exemplo.teste", "texto": "teste do selo"}
PRAZO_ENVIO = 5.0
PRAZO_PERGUNTA = 1.5


@dataclass
class Cena:
    dono: AsyncEngine
    desp: AsyncEngine
    cifra: CifraFalsa
    duble: Starlette
    base: str
    sistema: s.SistemaDoSelo
    teste: s.TesteGravado | None
    pessoa_id: uuid.UUID
    aplicativo_id: uuid.UUID
    corte: Cortador | None = None

    @property
    def motor(self) -> Any:
        return self.duble.state.motor

    def chamadas(self, nome: str, alvo: str = "producao") -> int:
        """Quantas chamadas a ferramenta ``nome`` o duble recebeu no alvo (na producao, por padrao)."""
        return int(self.motor.ambientes[alvo].contagem.chamadas.get(nome, 0))

    def aplicadas(self, nome: str = ACAO, alvo: str = "producao") -> int:
        return int(self.motor.ambientes[alvo].aplicadas.get(nome, 0))

    def metas(self, alvo: str = "producao") -> list[dict[str, Any]]:
        return list(self.motor.ambientes[alvo].metas)

    def porta(self, classe: type[PortaDeSaida] = PortaDeSaida, **kw: Any) -> PortaDeSaida:
        opcoes: dict[str, Any] = {
            "prazo_envio": PRAZO_ENVIO,
            "varredura": 0.2,
            "ambiente": "teste",
            "liberar_locais": True,
            "emails_admin": (EMAIL_ADMIN,),
        }
        return classe(self.desp, self.cifra, **(opcoes | kw))

    def perguntador(self, **kw: Any) -> Perguntador:
        opcoes: dict[str, Any] = {
            "maximo": 8,
            "prazo": PRAZO_PERGUNTA,
            "prazo_envio": PRAZO_ENVIO,
            "falhas_para_retirar": 3,
            "tentativas_do_aviso": 3,
            "emails_admin": (EMAIL_ADMIN,),
            "ambiente": "teste",
            "liberar_locais": True,
        }
        return Perguntador(self.desp, self.cifra, ligacao_sem_sala, **(opcoes | kw))


async def teste_de_ontem(c: AsyncConnection, teste: s.TesteGravado) -> s.TesteGravado:
    """O teste aprovado da cena com as horas um dia para tras (como dono, desligando a guarda de "so de acrescentar"
    so nesta transacao): as provas andam no relogio do banco para tras ate algumas horas (``voltar_no_tempo``), e a
    conta da retirada so olha o que terminou depois do teste do selo de agora."""
    await c.execute(text("ALTER TABLE testes_selo DISABLE TRIGGER testes_selo_sem_update_delete"))
    linha = (
        await c.execute(
            text(
                "UPDATE testes_selo SET iniciado_em = iniciado_em - interval '1 day',"
                " testado_em = testado_em - interval '1 day', vale_ate = vale_ate - interval '1 day'"
                " WHERE id = :t RETURNING testado_em, vale_ate"
            ),
            {"t": teste.id},
        )
    ).one()
    await c.execute(text("ALTER TABLE testes_selo ENABLE TRIGGER testes_selo_sem_update_delete"))
    return dataclasses.replace(teste, testado_em=linha.testado_em, vale_ate=linha.vale_ate)


async def sem_sobras(dono: AsyncEngine) -> None:
    """A fila de saida e os avisos de bloqueio sao uns so no banco de teste: o que outro teste deixou liberado nao
    sai por esta porta, os avisos pendentes de outro teste ficam encerrados e os comprovantes de outro teste ficam
    derivados (com os avisos que ja tem)."""
    async with dono.begin() as c:
        await c.execute(
            text("UPDATE fila_saida SET estado = 'cancelada', mudou_por = 'teste:sobra' WHERE estado = 'liberada'")
        )
        await c.execute(text("UPDATE avisos_bloqueio SET estado = 'bloqueio_encerrado' WHERE estado = 'pendente'"))
        await c.execute(
            text(
                "INSERT INTO avisos_bloqueio_derivados (bloqueio_id, avisos) SELECT c.bloqueio_id,"
                " (SELECT count(*) FROM avisos_bloqueio x WHERE x.bloqueio_id = c.bloqueio_id) FROM comprovantes c"
                " WHERE NOT EXISTS (SELECT 1 FROM avisos_bloqueio_derivados d WHERE d.bloqueio_id = c.bloqueio_id)"
            )
        )


@contextlib.asynccontextmanager
async def cena(
    banco: Parametros,
    fabrica: Callable[..., Starlette],
    *,
    nivel: int | None = 3,
    nivel_do_contrato: int | None = None,
    cortar: bool = False,
    **do_duble: Any,
) -> AsyncIterator[Cena]:
    """Um duble no ar (``fabrica(producao=..., teste=..., **do_duble)``) e, no banco, o sistema proprio na frente dele
    (pelo cortador, com ``cortar``), com o teste aprovado de ``nivel`` (sem teste com ``None``), a pessoa de teste e a
    chave dela. ``nivel_do_contrato``: as ferramentas do contrato listadas (o ``nivel``, por padrao)."""
    dono = criar_engine(banco, pool_size=3, max_overflow=3)
    desp = g.engine_do_papel(banco, "mcp_claro_despachante")
    cifra = CifraFalsa()
    producao, alvo = s.caminho_unico("producao"), s.caminho_unico("teste")
    duble = fabrica(producao=producao, teste=alvo, **do_duble)
    try:
        await g.abrir_a_saida(dono)
        await sem_sobras(dono)
        async with servidor(duble) as base, contextlib.AsyncExitStack() as pilha:
            corte = await pilha.enter_async_context(cortador(base)) if cortar else None
            endereco = s.endereco_de_duble(corte.url if corte is not None else base, producao)
            async with dono.begin() as c:
                sistema = await s.proprio_com_acoes(
                    c,
                    acoes=(ACAO,),
                    leituras=(LEITURA,),
                    endereco=endereco,
                    nivel_do_contrato=nivel_do_contrato or nivel or 3,
                )
                # A sondagem que o apoio grava nao e um server/discover: o cliente fixado usa o padrao.
                await c.execute(text("UPDATE sistemas SET sondagem = NULL WHERE id = :s"), {"s": sistema.id})
                teste = await s.gravar_teste(c, sistema.id, nivel=nivel) if nivel is not None else None
                if teste is not None:
                    teste = await teste_de_ontem(c, teste)
                pessoa_id = await s.nova_pessoa(c, teste=True)
                aplicativo_id = await s.novo_aplicativo(c)
                await chaves.guardar(c, cifra, sistema_id=sistema.id, segredo=CHAVE_DA_PESSOA, pessoa_id=pessoa_id)
            yield Cena(dono, desp, cifra, duble, base, sistema, teste, pessoa_id, aplicativo_id, corte)
    finally:
        await sem_sobras(dono)
        async with dono.begin() as c:
            await c.execute(
                text(
                    "UPDATE bloqueios SET encerrado_em = now(), encerrado_por = 'teste' WHERE encerrado_em IS NULL"
                    " AND (alcance IN ('tudo', 'sistema') OR pessoa_id IS NOT NULL)"
                )
            )
        await g.abrir_a_saida(dono)
        await dono.dispose()
        await desp.dispose()


async def nova_acao(
    cn: Cena,
    *,
    argumentos: Mapping[str, Any] | None = None,
    repete: uuid.UUID | None = None,
    pessoa_id: uuid.UUID | None = None,
    ficha_selo: Mapping[str, Any] | None = None,
) -> s.OperacaoDeTeste:
    """Uma acao da pessoa na ferramenta de acao do sistema, com a ficha confirmada e a fila ``liberada``, como as
    telas e a entrada deixariam (pelo dono, com ``tests.apoio_selos.criar_operacao``)."""
    pessoa = pessoa_id or cn.pessoa_id
    async with cn.dono.begin() as c:
        op = await s.criar_operacao(
            c,
            cn.sistema.acoes[0],
            pessoa_id=pessoa,
            aplicativo_id=cn.aplicativo_id,
            argumentos=dict(argumentos or ARGUMENTOS),
            repete=repete,
            liberada=False,
            ficha_selo=ficha_selo,
        )
        await c.execute(
            text(
                "INSERT INTO modelos_ordem_ferramentas (modelo_id, sistema_id, nome_original)"
                " SELECT os.modelo_id, :s, :n FROM operacoes o JOIN ordens_servico os ON os.id = o.ordem_id"
                " WHERE o.id = :o ON CONFLICT DO NOTHING"
            ),
            {"s": cn.sistema.id, "n": ACAO, "o": op.id},
        )
        await c.execute(
            text(
                "UPDATE fichas SET decisao = 'confirmada', decidida_em = now(), decidida_por = :p,"
                " confirmada_com = 'sessao' WHERE id = :f"
            ),
            {"p": pessoa, "f": op.ficha_id},
        )
        await c.execute(
            text("UPDATE fila_saida SET estado = 'liberada', hora_saida = now() WHERE operacao_id = :o"),
            {"o": op.id},
        )
    return op


async def mandar(cn: Cena, porta: PortaDeSaida | None = None) -> None:
    """A porta de saida processa o que estiver liberado (a acao desta cena), uma volta de cada vez."""
    p = porta or cn.porta()
    for _ in range(10):
        if not await p.enviar_uma():
            return


async def estado_da_fila(cn: Cena, operacao_id: uuid.UUID) -> str:
    async with cn.dono.connect() as c:
        return str(
            (
                await c.execute(text("SELECT estado FROM fila_saida WHERE operacao_id = :o"), {"o": operacao_id})
            ).scalar_one()
        )


async def informacoes(cn: Cena, operacao_id: uuid.UUID) -> list[Any]:
    async with cn.dono.connect() as c:
        return list(
            (
                await c.execute(
                    text(
                        "SELECT origem, desfecho, estado, repetida, objetos, depois, contradiz, aviso_id"
                        " FROM informacoes_sistema WHERE operacao_id = :o ORDER BY gravado_em, id"
                    ),
                    {"o": operacao_id},
                )
            ).all()
        )


async def envio(cn: Cena, operacao_id: uuid.UUID) -> Any:
    async with cn.dono.connect() as c:
        return (
            await c.execute(
                text(
                    "SELECT chave, nivel, teste_id, repete_operacao_id, gravado_em FROM envios_selo"
                    " WHERE operacao_id = :o"
                ),
                {"o": operacao_id},
            )
        ).first()


async def extrato(cn: Cena, *, protocolo: str | None = None, sistema: bool = False) -> list[Any]:
    """As linhas do extrato do protocolo (ou, com ``sistema``, as do sistema da cena), em ordem."""
    filtro = "protocolo = :p" if protocolo is not None else "sistema_id = :s"
    async with cn.dono.connect() as c:
        return list(
            (
                await c.execute(
                    text(f"SELECT tipo, frase, dados, pessoa_id FROM extrato WHERE {filtro} ORDER BY id"),  # noqa: S608
                    {"p": protocolo, "s": cn.sistema.id},
                )
            ).all()
        )


async def operacao(cn: Cena, operacao_id: uuid.UUID) -> Any:
    async with cn.dono.connect() as c:
        return (
            await c.execute(text("SELECT resultado, desfecho FROM operacoes WHERE id = :o"), {"o": operacao_id})
        ).one()


async def voltar_no_tempo(cn: Cena, operacoes: Sequence[uuid.UUID], minutos: float = 300) -> None:
    """A hora do envio e de cada informacao destas operacoes ``minutos`` para tras, como se o tempo tivesse passado (a
    escada da consulta conta pelo relogio do banco). O dono desliga a guarda de "so de acrescentar" so nesta
    transacao curta."""
    deslocamento = timedelta(minutes=-minutos)
    async with cn.dono.begin() as c:
        await c.execute(text("ALTER TABLE envios_selo DISABLE TRIGGER envios_selo_sem_update_delete"))
        await c.execute(text("ALTER TABLE informacoes_sistema DISABLE TRIGGER informacoes_sistema_sem_update_delete"))
        await c.execute(
            text("UPDATE envios_selo SET gravado_em = gravado_em + :d WHERE operacao_id = ANY(:o)"),
            {"d": deslocamento, "o": list(operacoes)},
        )
        await c.execute(
            text("UPDATE informacoes_sistema SET gravado_em = gravado_em + :d WHERE operacao_id = ANY(:o)"),
            {"d": deslocamento, "o": list(operacoes)},
        )
        await c.execute(text("ALTER TABLE informacoes_sistema ENABLE TRIGGER informacoes_sistema_sem_update_delete"))
        await c.execute(text("ALTER TABLE envios_selo ENABLE TRIGGER envios_selo_sem_update_delete"))


async def selo_vigente(cn: Cena) -> Any:
    async with cn.dono.connect() as c:
        return await s.selo_vigente(c, cn.sistema.id)


async def avisos_ao_admin(cn: Cena, evento: str) -> list[Any]:
    async with cn.dono.connect() as c:
        return list(
            (
                await c.execute(
                    text("SELECT evento, assunto, corpo FROM avisos WHERE evento = :e AND corpo ->> 'sistema_id' = :s"),
                    {"e": evento, "s": str(cn.sistema.id)},
                )
            ).all()
        )


def textos(linhas: Sequence[Any]) -> str:
    """Tudo o que as linhas levam, em texto, para conferir que nada do sistema passou."""
    return json.dumps([[str(x) for x in linha] for linha in linhas], ensure_ascii=False)
