"""Apoio dos testes do G4 da Fase 4 (fila, pedidos e entrada com o selo), sem despachante.

- A entrada sobe de verdade (uvicorn, cracha do ``IdpFalso``, usuario ``mcp_claro_entrada``), com um leitor falso
  no lugar do despachante (``LeitorFalso``). Nada do despachante e importado: os testes do G4 nao dependem do
  arquivo do G3a.
- O sistema e um proprio com o contrato do selo listado (``tests.apoio_selos.proprio_com_acoes``): a acao
  ``enviar_lembrete`` (classe ``muda``) e a leitura ``listar_lembretes``. O teste de selo e gravado pelo dono
  (``apoio_selos.gravar_teste``), como o despachante gravaria.
- A parte do outro grupo (G3a: a porta de saida e a consulta) e montada pelo banco, como dono, com
  ``tests/apoio_selos.py``: o envio com numero (``apoio_selos.enviar``, com a fila ``liberada``) e as respostas do
  sistema (``apoio_selos.informar``). Nenhuma porta de saida roda, entao nada sai sozinho, e nenhum duble e chamado:
  o caminho inteiro (porta, duble, consulta) e prova do G9.
- Cada ``MundoSelo`` tem os proprios aplicativo, pessoas e sistema (nomes com sufixo): os testes nao se misturam no
  banco da sessao. A producao do sistema e um nome ``.exemplo.teste`` unico, nunca o alvo de teste do apoio.

Pessoas: ``ana`` (de teste) e ``real`` (pessoa real). Com ``FEATURE_PHASE4_BADGES`` desligada (o padrao), o selo
efetivo da ``real`` e o nivel 1 (``fase_desligada``) e o da ``ana`` e o vigente.
"""

from __future__ import annotations

import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine, url_asyncpg
from mcp_claro.entrada.app import criar_app_entrada
from mcp_claro.entrada.servidor import PedidoLeitura
from mcp_claro.saida import fila
from mcp_claro.saida.pedidos import CONSULTA
from mcp_claro.versao import PREFIXO_META
from tests import apoio_selos
from tests.apoio import cliente_mcp, porta_livre, servidor
from tests.idp_falso import IdpFalso

PUBLICO = "https://mcp-claro.teste"
RECURSO = f"{PUBLICO}/mcp"
META_SELO = PREFIXO_META + "selo"
META_INFORMADO = PREFIXO_META + "informado"
PROTOCOLO = PREFIXO_META + "protocolo"
ESTADO = PREFIXO_META + "estado"
ARGUMENTOS = {"para": "caixa@exemplo.teste", "texto": "lembrete do G4"}
TEXTO_DO_SISTEMA = "Texto que o sistema escreveu."


def engine_do_papel(p: Parametros, papel: str) -> AsyncEngine:
    """Engine que conecta como o papel do banco (``SET ROLE`` na partida da conexao), como os testes da Fase 2."""
    return create_async_engine(
        url_asyncpg(p),
        connect_args={"ssl": False, "server_settings": {"application_name": f"teste-{papel}", "role": papel}},
        pool_size=5,
        max_overflow=5,
    )


@dataclass
class LeitorFalso:
    """Faz o papel do despachante nas leituras: devolve o texto do sistema com o ``_meta`` nosso."""

    sistemas: dict[uuid.UUID, str] = field(default_factory=dict)  # ferramenta_id -> nome do sistema
    chamadas: list[PedidoLeitura] = field(default_factory=list)

    async def __call__(self, pedido: PedidoLeitura) -> dict[str, Any]:
        self.chamadas.append(pedido)
        return {
            "content": [{"type": "text", "text": TEXTO_DO_SISTEMA}],
            "isError": False,
            "_meta": {PREFIXO_META + "sistema": self.sistemas.get(pedido.ferramenta_id, "?"), PREFIXO_META + "selo": 1},
        }


@dataclass
class MundoSelo:
    p: Parametros
    idp: IdpFalso
    dono: AsyncEngine
    entrada_db: AsyncEngine
    url: str
    leitor: LeitorFalso
    aplicativo_id: uuid.UUID
    client_id: str
    pessoas: dict[str, uuid.UUID]
    sistema: apoio_selos.SistemaDoSelo
    admin_id: uuid.UUID
    sufixo: str

    @property
    def acao(self) -> str:
        return self.sistema.acoes[0].nome_vitrine

    @property
    def leitura(self) -> str:
        return self.sistema.leituras[0].nome_vitrine

    def cracha(self, pessoa: str = "ana") -> str:
        return self.idp.cracha(audiencia=RECURSO, sujeito=f"g4s-{pessoa}-{self.sufixo}", client_id=self.client_id)

    async def chamar(self, nome: str, argumentos: dict[str, Any] | None = None, pessoa: str = "ana") -> Any:
        """Uma chamada pelo aplicativo. Recusa sai como ``MCPError`` (fora do grupo de tarefas do cliente)."""
        recusa: MCPError | None = None
        async with cliente_mcp(f"{self.url}/mcp", self.cracha(pessoa)) as c:
            try:
                return await c.call_tool(nome, argumentos or {})
            except MCPError as erro:
                recusa = erro
        raise recusa

    async def agir(self, argumentos: dict[str, Any] | None = None, pessoa: str = "ana") -> Any:
        return await self.chamar(self.acao, dict(argumentos or ARGUMENTOS), pessoa)

    async def consultar(self, protocolo: str, pessoa: str = "ana") -> Any:
        return await self.chamar(CONSULTA, {"protocolo": protocolo}, pessoa)

    async def ferramentas(self, pessoa: str = "ana") -> dict[str, Any]:
        """O ``tools/list`` da pessoa: nome na vitrine -> definicao (com o ``_meta``)."""
        async with cliente_mcp(f"{self.url}/mcp", self.cracha(pessoa)) as c:
            return {t.name: t for t in (await c.list_tools()).tools}

    async def operacao(self, protocolo: str) -> fila.Operacao:
        async with self.entrada_db.connect() as c:
            i = (await c.execute(text("SELECT id FROM operacoes WHERE protocolo = :p"), {"p": protocolo})).scalar_one()
            op = await fila.por_id(c, i)
        assert op is not None
        return op

    async def por_id(self, operacao_id: uuid.UUID) -> fila.Operacao:
        async with self.entrada_db.connect() as c:
            op = await fila.por_id(c, operacao_id)
        assert op is not None
        return op

    async def gravar_teste(self, nivel: int = 2) -> apoio_selos.TesteGravado:
        async with self.dono.begin() as c:
            return await apoio_selos.gravar_teste(c, self.sistema.id, nivel=nivel, admin_id=self.admin_id)

    async def selo_vigente(self) -> Any:
        async with self.dono.connect() as c:
            return await apoio_selos.selo_vigente(c, self.sistema.id)

    async def retirar_selo(self) -> None:
        """A retirada do ADMIN, gravada pelo dono (como em ``test_vitrine_selo``)."""
        async with self.dono.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO selos_retirados (sistema_id, teste_id, motivo, detalhe, retirado_por, pessoa_id)"
                    " SELECT :s, teste_id, 'admin', 'teste do G4', 'admin-teste', :a FROM selo_vigente(:s)"
                ),
                {"s": self.sistema.id, "a": self.admin_id},
            )

    async def confirmar(self, op: fila.Operacao, pessoa: str = "ana") -> fila.Operacao:
        """A pessoa confirma a ficha sem espera: a acao fica ``liberada`` (a porta nao roda aqui)."""
        assert op.ficha_id is not None
        async with self.entrada_db.begin() as c:
            return await fila.confirmar_ficha(
                c, op.ficha_id, pessoa_id=self.pessoas[pessoa], com_passkey=True, espera_segundos=0, rapida_segundos=0
            )

    async def sair_com_numero(self, op: fila.Operacao, fim: str = "nao_confirmada") -> apoio_selos.Envio:
        """O que a porta de saida (G3a) faria: o envio com numero na transacao de "enviando" e o fim, pelo dono."""
        async with self.dono.begin() as c:
            return await apoio_selos.enviar(c, op.id, fim)

    async def terminar_como_despachante(
        self, op: fila.Operacao, estado: Any, *, selo: dict[str, Any] | None = None
    ) -> fila.Operacao:
        """A acao ja em "enviando" (pelo dono) termina por ``fila.finalizar``, com o usuario do despachante."""
        despachante = engine_do_papel(self.p, "mcp_claro_despachante")
        try:
            async with despachante.begin() as c:
                atual = await fila.por_id(c, op.id)
                assert atual is not None
                await fila.finalizar(c, atual, estado, None, "o sistema respondeu", selo=selo)
        finally:
            await despachante.dispose()
        return await self.por_id(op.id)

    async def informar(self, op: fila.Operacao, estado: str | None, **mais: Any) -> apoio_selos.Informacao:
        """Uma resposta do sistema (``informacoes_sistema``), pelo dono, como a consulta ou o recibo gravariam."""
        async with self.dono.begin() as c:
            return await apoio_selos.informar(c, op.id, estado, **mais)

    async def recuar_informacao(self, informacao_id: int, minutos: int) -> None:
        """Poe uma resposta ``minutos`` antes de agora (o dono desliga a guarda de "so de acrescentar" so nesta
        transacao): duas ``nao_recebida`` com intervalo, sem esperar o relogio."""
        async with self.dono.begin() as c:
            await recuar_informacao(c, informacao_id, minutos)

    async def recuar_operacao(self, op: fila.Operacao, dias: int) -> None:
        """Poe a criacao da operacao ``dias`` antes de agora (operacoes nao tem guarda de UPDATE para o dono)."""
        async with self.dono.begin() as c:
            await c.execute(
                text("UPDATE operacoes SET criada_em = now() - make_interval(days => :d) WHERE id = :o"),
                {"d": dias, "o": op.id},
            )

    async def decidir(self, op: fila.Operacao, decisao: str, pessoa: str = "ana", **mais: Any) -> fila.Operacao:
        async with self.entrada_db.begin() as c:
            return await fila.decidir_nao_confirmada(
                c, op.id, pessoa_id=self.pessoas[pessoa], decisao=decisao, observacao=None, **mais
            )

    async def extrato(self, tipo: str, protocolo: str | None = None) -> list[Any]:
        async with self.dono.connect() as c:
            return list(
                (
                    await c.execute(
                        text(
                            "SELECT frase, dados, pessoa_id, protocolo, papel FROM extrato WHERE tipo = :t"
                            " AND sistema_id = :s AND (CAST(:pr AS text) IS NULL OR protocolo = :pr) ORDER BY id"
                        ),
                        {"t": tipo, "s": self.sistema.id, "pr": protocolo},
                    )
                ).all()
            )

    async def extrato_do_aplicativo(self, tipo: str) -> list[Any]:
        """Linhas do extrato do aplicativo deste mundo (as recusas antes da ferramenta nao levam o sistema)."""
        async with self.dono.connect() as c:
            return list(
                (
                    await c.execute(
                        text(
                            "SELECT frase, dados, pessoa_id, ferramenta, papel FROM extrato WHERE tipo = :t"
                            " AND aplicativo_id = :a ORDER BY id"
                        ),
                        {"t": tipo, "a": self.aplicativo_id},
                    )
                ).all()
            )

    async def ficha_selo(self, op: fila.Operacao) -> Any:
        async with self.dono.connect() as c:
            return (await c.execute(text("SELECT selo FROM fichas WHERE operacao_id = :o"), {"o": op.id})).scalar_one()


async def recuar_informacao(c: AsyncConnection, informacao_id: int, minutos: int) -> None:
    await c.execute(text("ALTER TABLE informacoes_sistema DISABLE TRIGGER informacoes_sistema_sem_update_delete"))
    await c.execute(
        text("UPDATE informacoes_sistema SET gravado_em = now() - CAST(:d AS interval) WHERE id = :i"),
        {"d": timedelta(minutes=minutos), "i": informacao_id},
    )
    await c.execute(text("ALTER TABLE informacoes_sistema ENABLE TRIGGER informacoes_sistema_sem_update_delete"))


@contextlib.asynccontextmanager
async def subir_mundo_selo(
    banco: Parametros, *, selo_ligado: bool = False, extra: dict[str, Any] | None = None
) -> AsyncIterator[MundoSelo]:
    """Sobe a entrada com o leitor falso e monta o cadastro com um sistema proprio do contrato do selo.
    ``selo_ligado``: ``FEATURE_PHASE4_BADGES`` pelo construtor (a partida a recusa; como nos testes da Fase 3)."""
    sufixo = uuid.uuid4().hex[:8]
    porta_idp = porta_livre()
    idp = IdpFalso(emissor=f"http://127.0.0.1:{porta_idp}/realms/mcp-claro")
    p = Parametros(
        **(
            banco.model_dump()
            | {
                "APP_ENV": "teste",
                "PUBLIC_BASE_URL": PUBLICO,
                "MCP_RESOURCE_ID": None,
                "WATCH_PUBLIC_URL": None,
                "ALLOWED_HOSTS": ["127.0.0.1:*"],
                "IDP_KIND": "oidc",
                "IDP_ISSUER_URL": idp.emissor,
                "IDP_JWKS_URL": f"http://127.0.0.1:{porta_idp}/jwks",
                "FEATURE_PHASE1_READ": True,
                "FEATURE_PHASE2_ACT": True,
                "FEATURE_PHASE3_THIRD_PARTY": selo_ligado,
                "FEATURE_PHASE4_BADGES": selo_ligado,
                "RESPONSE_MAX_WAIT_SECONDS": 1,
            }
            | (extra or {})
        )
    )
    dono = criar_engine(p, pool_size=3, max_overflow=3)
    entrada_db = engine_do_papel(p, "mcp_claro_entrada")
    leitor = LeitorFalso()
    try:
        async with dono.begin() as c:
            client_id = f"g4s-app-{sufixo}"
            aplicativo_id = await repositorio.habilitar_aplicativo(
                c, client_id=client_id, nome=f"Aplicativo G4 {sufixo}", familia="g4s"
            )
            pessoas: dict[str, uuid.UUID] = {}
            for nome in ("ana", "real"):
                pessoa = await repositorio.pessoa_no_login(c, idp.emissor, f"g4s-{nome}-{sufixo}", nome)
                pessoas[nome] = pessoa.id
            await c.execute(text("UPDATE pessoas SET teste = true WHERE id = :p"), {"p": pessoas["ana"]})
            admin_id = await apoio_selos.nova_pessoa(c, admin=True)
            sistema = await apoio_selos.proprio_com_acoes(c)
            modelo = await repositorio.criar_modelo_ordem(
                c,
                nome=f"g4s-{sufixo}",
                descricao="Ordem de servico de teste do G4 (selos)",
                ferramentas=[(sistema.id, r.nome_original) for r in (*sistema.acoes, *sistema.leituras)],
                validade_dias=30,
                criado_por="admin-teste",
            )
            for pessoa_id in pessoas.values():
                await repositorio.conceder_ordem(
                    c, pessoa_id=pessoa_id, aplicativo_id=aplicativo_id, modelo_id=modelo, dias=7, max_dias=30
                )
        for r in sistema.leituras:
            leitor.sistemas[r.id] = sistema.nome
        with pytest.MonkeyPatch.context() as mp:
            # A partida recusa as chaves das Fases 3 e 4 ligadas (D6, D24); os testes as ligam pelo construtor.
            mp.setattr(Parametros, "conferir", lambda self: self)
            app = criar_app_entrada(p, entrada_db, leitor=leitor)
        async with servidor(idp.app(), porta_idp), servidor(app) as url:
            yield MundoSelo(
                p=p,
                idp=idp,
                dono=dono,
                entrada_db=entrada_db,
                url=url,
                leitor=leitor,
                aplicativo_id=aplicativo_id,
                client_id=client_id,
                pessoas=pessoas,
                sistema=sistema,
                admin_id=admin_id,
                sufixo=sufixo,
            )
    finally:
        for engine in (dono, entrada_db):
            await engine.dispose()


def meta(resultado: Any) -> dict[str, Any]:
    return dict(resultado.meta or {})


def textos(resultado: Any) -> list[str]:
    return [b.text for b in resultado.content if getattr(b, "type", None) == "text"]


def protocolo_de(resultado: Any) -> str:
    return str(meta(resultado)[PROTOCOLO])


def retrato_json(valor: Any) -> dict[str, Any]:
    return json.loads(valor) if isinstance(valor, str) else dict(valor)
