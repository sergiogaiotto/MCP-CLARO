"""Apoio dos ensaios do G9b (Fase 3, etapa 6): o gateway inteiro, com a sala de visitas, antes de ligar a chave.

``subir`` monta, pelo caminho de fora, o que uma instalacao teria:

- a entrada de verdade (``criar_app_entrada``, usuario ``mcp_claro_entrada``), com o cracha do ``IdpFalso``;
- o despachante de verdade (``criar_app_despachante``, usuario ``mcp_claro_despachante``), com a propria porta de
  saida rodando (como na instalacao) e a sala de visitas montada de ``VISITS_INTERNAL_URL``: nenhum cliente local
  para terceiro e injetado, entao todo pedido MCP a um terceiro passa pela sala;
- a sala: ``processo`` e a ``sala_em_processo`` (o mesmo contrato, sem os filhos, por http); ``filhos`` e a sala de
  verdade (``visitas.app.Sala``, um processo filho por chamada, mTLS com o certificado do despachante);
- o intermediario ``tests/cortador.py`` (so usado) na frente da sala (so com ``processo``: por TLS ele nao ve
  o pedido) e na frente do terceiro, os dois em modo ``inteiro`` ate o teste mudar;
- tres sistemas de exemplo, todos com chave por pessoa: o terceiro ``exemplos.ensaio_mal_intencionado``
  (``ler_avisos`` e ``enviar_recado``) e dois proprios, ``exemplos.leitura_por_pessoa`` (``ver_cadastro``, com CPF
  e e-mail) e ``exemplos.acoes_caixa`` (``enviar_mensagem``).

O cadastro passa pelas portas do ADMIN: aplicativo habilitado e sistemas cadastrados pelo usuario da entrada,
sondagem, chaves (de acesso e, no terceiro, a de leitura das declaracoes) e declaracoes pelo despachante, revisao de
cada ferramenta pelo usuario da entrada e conclusao da revisao do servidor pela rota interna do despachante. So as
chaves por pessoa sao gravadas pelo dono do banco: elas nascem de uma autorizacao OAuth que o ensaio nao refaz
(``test_autorizacao`` a prova).

A chave da Fase 3 (``FEATURE_PHASE3_THIRD_PARTY``) liga pelo construtor de ``Parametros``. A partida recusa essa
chave (D6, que fica): ``conferir_sem_d6`` troca ``Parametros.conferir`` so durante o arranjo e continua recusando
qualquer outro problema. As pessoas do arranjo sao reais (``pessoas.teste`` falso), como no piloto com a chave ligada.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx2
import mcp_types as types
import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.applications import Starlette

from exemplos import acoes_caixa, ensaio_mal_intencionado, leitura_por_pessoa
from mcp_claro import __main__ as partida
from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import ConfiguracaoInvalida, Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.despachante import chaves
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.entrada.app import criar_app_entrada
from mcp_claro.entrada.despachante_cliente import ClienteDespachante
from mcp_claro.saida import fila
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.pedidos import CONSULTA
from mcp_claro.versao import PREFIXO_META
from mcp_claro.visitas import app as visitas_app
from tests import apoio_g3a as g
from tests.apoio import cliente_mcp, porta_livre, servidor
from tests.apoio_visitas import Certificados
from tests.cifra_falsa import CifraFalsa
from tests.cortador import Cortador, cortador
from tests.idp_falso import IdpFalso

PUBLICO = "https://mcp-claro.teste"
RECURSO = f"{PUBLICO}/mcp"
AUTOR = "teste-g9b"
PROTOCOLO = PREFIXO_META + "protocolo"
ESTADO = PREFIXO_META + "estado"
# Chaves estaticas dos sistemas de exemplo (``exemplos.comum``). Os tres sistemas usam chave por pessoa (sistema
# de chave compartilhada so aceita leitura): ``chave-<pessoa>`` nos proprios, ``terceiro-<pessoa>`` no terceiro. A
# do terceiro para ler as declaracoes (critica A5) e ``terceiro-declaracoes``.
CHAVES_DOS_EXEMPLOS = {
    "chave-ana": "ana",
    "chave-bia": "bia",
    "terceiro-ana": "ana",
    "terceiro-bia": "bia",
    "terceiro-declaracoes": "*",
}
CHAVE_DO_TERCEIRO = "terceiro-declaracoes"
D6 = "FEATURE_PHASE3_THIRD_PARTY ainda nao liga"
# O que ficou liberado na fila do banco da sessao (deste ou de outro teste) nao sai por esta porta de saida.
SOBRAS = "UPDATE fila_saida SET estado = 'cancelada', mudou_por = 'teste:sobra' WHERE estado = 'liberada'"

Sala = Literal["processo", "filhos"]


def conferir_sem_d6(self: Parametros) -> Parametros:
    """``Parametros.conferir`` sem a recusa D6 (a chave da Fase 3 ligada): qualquer outro problema continua
    recusando a partida. Usado so pelo arranjo dos ensaios, que ligam a chave pelo construtor."""
    erros = [e for e in self.problemas() if not e.startswith(D6)]
    if erros:
        raise ConfiguracaoInvalida(erros)
    return self


@dataclass
class Sistema:
    id: uuid.UUID
    nome: str
    base: str  # http://127.0.0.1:<porta> do proprio exemplo (sem o cortador)
    app: Starlette
    vitrine: dict[str, str] = field(default_factory=dict)  # nome original -> nome na vitrine
    revisadas: dict[str, uuid.UUID] = field(default_factory=dict)  # nome original -> id da revisada

    def chamadas(self, nome: str) -> int:
        return int(self.app.state.contagem.chamadas.get(nome, 0))


@dataclass
class Gateway:
    p: Parametros
    idp: IdpFalso
    dono: AsyncEngine
    entrada_db: AsyncEngine
    despachante_db: AsyncEngine
    cifra: CifraFalsa
    entrada: str
    despachante: str
    app_desp: Starlette
    sala: Sala
    corte_sala: Cortador | None
    corte_terceiro: Cortador
    aplicativo_id: uuid.UUID
    client_id: str
    pessoas: dict[str, uuid.UUID]
    terceiro: Sistema
    cadastro: Sistema
    caixa: Sistema
    sufixo: str

    def cracha(self, pessoa: str = "ana") -> str:
        return self.idp.cracha(audiencia=RECURSO, sujeito=f"g9b-{pessoa}-{self.sufixo}", client_id=self.client_id)

    async def chamar(self, sistema: Sistema, original: str, argumentos: dict[str, Any], pessoa: str = "ana") -> Any:
        """Uma chamada pelo aplicativo, com o cliente oficial e o cracha. Recusa sai como ``MCPError``."""
        recusa: MCPError | None = None
        async with cliente_mcp(f"{self.entrada}/mcp", self.cracha(pessoa)) as c:
            try:
                return await c.call_tool(sistema.vitrine[original], argumentos)
            except MCPError as erro:
                recusa = erro
        raise recusa

    async def listar(self, pessoa: str = "ana") -> list[types.Tool]:
        async with cliente_mcp(f"{self.entrada}/mcp", self.cracha(pessoa)) as c:
            return list((await c.list_tools()).tools)

    async def consultar(self, protocolo: str, pessoa: str = "ana") -> Any:
        async with cliente_mcp(f"{self.entrada}/mcp", self.cracha(pessoa)) as c:
            return await c.call_tool(CONSULTA, {"protocolo": protocolo})

    async def operacao(self, protocolo: str) -> fila.Operacao:
        async with self.dono.connect() as c:
            i = (await c.execute(text("SELECT id FROM operacoes WHERE protocolo = :p"), {"p": protocolo})).scalar_one()
            op = await fila.por_id(c, i)
        assert op is not None
        return op

    async def esperar_estado(self, protocolo: str, *estados: Estado, prazo: float = 30.0) -> fila.Operacao:
        """Le o estado da operacao no banco ate ele ser um dos ``estados`` (a porta de saida roda sozinha)."""
        fim = asyncio.get_running_loop().time() + prazo
        while True:
            op = await self.operacao(protocolo)
            if op.estado in estados:
                return op
            if asyncio.get_running_loop().time() > fim:
                raise AssertionError(f"{protocolo} ficou em {op.estado}, esperado {estados}")
            await asyncio.sleep(0.05)

    async def confirmar(self, op: fila.Operacao, pessoa: str = "ana") -> fila.Operacao:
        """A pessoa confirma a ficha na tela (com a passkey), sem espera visivel."""
        assert op.ficha_id is not None
        async with self.entrada_db.begin() as c:
            return await fila.confirmar_ficha(
                c, op.ficha_id, pessoa_id=self.pessoas[pessoa], com_passkey=True, espera_segundos=0, rapida_segundos=0
            )

    async def limite(
        self, sistema: Sistema, original: str, destinatarios: list[dict[str, Any]], *, pessoa: str = "ana"
    ) -> uuid.UUID:
        """Um limite por aproximacao ativo, feito com passkey (o que a tela de limites grava)."""
        async with self.entrada_db.begin() as c:
            return uuid.UUID(
                str(
                    (
                        await c.execute(
                            text(
                                "INSERT INTO limites (pessoa_id, aplicativo_id, sistema_id, nome_original,"
                                " destinatarios, por_dia, valido_ate, com_passkey) VALUES (:p, :a, :s, :n,"
                                " CAST(:d AS jsonb), 5, now() + interval '1 day', true) RETURNING id"
                            ),
                            {
                                "p": self.pessoas[pessoa],
                                "a": self.aplicativo_id,
                                "s": sistema.id,
                                "n": original,
                                "d": json.dumps(destinatarios),
                            },
                        )
                    ).scalar_one()
                )
            )

    async def usados(self, limite: uuid.UUID) -> int:
        async with self.dono.connect() as c:
            return int(
                (await c.execute(text("SELECT usados_hoje FROM limites WHERE id = :l"), {"l": limite})).scalar_one()
            )

    async def extrato(self, *, protocolo: str | None = None, sistema: Sistema | None = None) -> list[Any]:
        """Linhas do extrato deste arranjo (pelo aplicativo dele), na ordem."""
        async with self.dono.connect() as c:
            return list(
                (
                    await c.execute(
                        text(
                            "SELECT tipo, frase, dados, pessoa_id, protocolo, sistema_id FROM extrato"
                            " WHERE (aplicativo_id = :a OR sistema_id = ANY(:s))"
                            " AND (CAST(:p AS text) IS NULL OR protocolo = :p)"
                            " AND (CAST(:si AS uuid) IS NULL OR sistema_id = :si) ORDER BY id"
                        ),
                        {
                            "a": self.aplicativo_id,
                            "s": [self.terceiro.id, self.cadastro.id, self.caixa.id],
                            "p": protocolo,
                            "si": sistema.id if sistema else None,
                        },
                    )
                ).all()
            )

    async def ficha(self, protocolo: str) -> Any:
        async with self.dono.connect() as c:
            return (
                await c.execute(
                    text(
                        "SELECT fi.id, fi.sinais, fi.risco FROM fichas fi JOIN operacoes o ON o.id = fi.operacao_id"
                        " WHERE o.protocolo = :p"
                    ),
                    {"p": protocolo},
                )
            ).one_or_none()


def protocolo(resultado: Any) -> str:
    return str(dict(resultado.meta or {})[PROTOCOLO])


def estado(resultado: Any) -> str:
    return str(dict(resultado.meta or {})[ESTADO])


@contextlib.asynccontextmanager
async def _sala_em_processo() -> AsyncIterator[str]:
    async with servidor(g.sala_em_processo()) as base:
        yield base


@contextlib.asynccontextmanager
async def _sala_de_verdade(certificados: Certificados) -> AsyncIterator[tuple[str, dict[str, str]]]:
    """A sala de visitas de verdade (um filho por chamada), em desenvolvimento para aceitar o terceiro local.
    Devolve a URL https e os parametros do despachante para o mTLS (so a ``ca_visitas`` e o certificado dele)."""
    porta = porta_livre()
    p = Parametros(
        APP_ENV="desenvolvimento",
        VISITS_INTERNAL_PORT=porta,
        VISITS_INTERNAL_URL=f"https://localhost:{porta}",
        VISITS_CA_FILE=str(certificados.ca_visitas),
        VISITS_CLIENT_CERT_FILE=str(certificados.despachante_cert),
        VISITS_CLIENT_KEY_FILE=str(certificados.despachante_key),
    )
    tls = partida.opcoes_tls(
        Parametros(
            INTERNAL_CA_FILE=str(certificados.ca_visitas),
            INTERNAL_CERT_FILE=str(certificados.visitas_cert),
            INTERNAL_KEY_FILE=str(certificados.visitas_key),
            INTERNAL_CLIENT_CA_FILE=str(certificados.ca_clientes_visitas),
        )
    )
    sala = visitas_app.Sala(p)
    async with servidor(visitas_app.criar_app_visitas(p, sala=sala), porta, **tls):
        yield (
            f"https://localhost:{porta}",
            {
                "VISITS_CA_FILE": str(certificados.ca_visitas),
                "VISITS_CLIENT_CERT_FILE": str(certificados.despachante_cert),
                "VISITS_CLIENT_KEY_FILE": str(certificados.despachante_key),
            },
        )


async def _preparar_banco(dono: AsyncEngine) -> None:
    """O banco e o da sessao de testes: o que outro teste deixou liberado na fila nao sai por esta porta, a porta
    de saida esta aberta e nenhum bloqueio de tudo ficou aberto. Pelo dono, so para o arranjo."""
    await g.abrir_a_saida(dono)
    async with dono.begin() as c:
        await c.execute(text(SOBRAS))
        await c.execute(
            text(
                "UPDATE bloqueios SET encerrado_em = now(), encerrado_por = 'teste' WHERE encerrado_em IS NULL"
                " AND alcance = 'tudo'"
            )
        )


async def _cadastrar(
    gw_entrada: AsyncEngine,
    desp: ClienteDespachante,
    *,
    nome: str,
    endereco: str,
    tipo_chave: str,
    origem: str,
    chave_sondagem: str,
) -> tuple[uuid.UUID, dict[str, dict[str, Any]]]:
    """Cadastro pelo ADMIN (usuario da entrada), sondagem pelo despachante e as declaracoes de hoje."""
    async with gw_entrada.begin() as c:
        sistema_id = await repositorio.cadastrar_sistema(
            c, nome=nome, endereco=endereco, tipo_chave=tipo_chave, origem=origem
        )
    sondagem = await desp.post("/interno/sondar", {"sistema_id": str(sistema_id), "chave_sondagem": chave_sondagem})
    assert sondagem["aceito"], sondagem
    if origem == "terceiro":
        # Critica A5: a revisao do servidor le as declaracoes com a chave propria delas, guardada antes.
        guardada = await desp.post(
            "/interno/chaves", {"sistema_id": str(sistema_id), "segredo": chave_sondagem, "tipo": "declaracoes"}
        )
        assert guardada == {"guardada": True}, guardada
    declaradas = await desp.post(
        "/interno/declaracoes", {"sistema_id": str(sistema_id), "chave_sondagem": chave_sondagem}
    )
    return sistema_id, {str(f["name"]): f for f in declaradas["ferramentas"]}


@contextlib.asynccontextmanager
async def subir(
    banco: Parametros,
    monkeypatch: pytest.MonkeyPatch,
    *,
    sala: Sala = "processo",
    certificados: Certificados | None = None,
) -> AsyncIterator[Gateway]:
    """Sobe o gateway inteiro (ver o topo do modulo) e devolve o ``Gateway`` pronto, com a Fase 3 ligada."""
    monkeypatch.setenv("EXEMPLO_CHAVES", json.dumps(CHAVES_DOS_EXEMPLOS))
    monkeypatch.setattr(Parametros, "conferir", conferir_sem_d6)
    sufixo = uuid.uuid4().hex[:8]
    porta_idp, porta_desp = porta_livre(), porta_livre()
    idp = IdpFalso(emissor=f"http://127.0.0.1:{porta_idp}/realms/mcp-claro")
    apps = {
        "terceiro": ensaio_mal_intencionado.criar(),
        "cadastro": leitura_por_pessoa.criar(),
        "caixa": acoes_caixa.criar(),
    }
    async with contextlib.AsyncExitStack() as pilha:
        await pilha.enter_async_context(servidor(idp.app(), porta_idp))
        bases = {nome: await pilha.enter_async_context(servidor(app)) for nome, app in apps.items()}
        corte_terceiro = await pilha.enter_async_context(cortador(bases["terceiro"]))
        corte_sala: Cortador | None = None
        extra_sala: dict[str, str] = {}
        if sala == "processo":
            base_sala = await pilha.enter_async_context(_sala_em_processo())
            corte_sala = await pilha.enter_async_context(cortador(base_sala))
            url_sala = corte_sala.url
        else:
            assert certificados is not None, "a sala de verdade precisa dos certificados de teste"
            url_sala, extra_sala = await pilha.enter_async_context(_sala_de_verdade(certificados))
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
                    "DISPATCHER_INTERNAL_URL": f"http://127.0.0.1:{porta_desp}",
                    "FEATURE_PHASE1_READ": True,
                    "FEATURE_PHASE2_ACT": True,
                    "FEATURE_PHASE3_THIRD_PARTY": True,
                    # Folga para a sala de verdade sob pressao (um processo novo por chamada): nada aqui mede tempo.
                    "RESPONSE_MAX_WAIT_SECONDS": 15,
                    "OUTBOX_SWEEP_SECONDS": 1,
                    "VISITS_INTERNAL_URL": url_sala,
                }
                | extra_sala
            )
        )
        assert p.FEATURE_PHASE3_THIRD_PARTY is True
        assert [e for e in p.problemas() if e.startswith(D6)], "a recusa D6 continua na partida"
        dono = criar_engine(p, pool_size=3, max_overflow=3)
        entrada_db = g.engine_do_papel(p, "mcp_claro_entrada")
        despachante_db = g.engine_do_papel(p, "mcp_claro_despachante")
        for engine in (dono, entrada_db, despachante_db):
            pilha.push_async_callback(engine.dispose)
        await _preparar_banco(dono)
        cifra = CifraFalsa()
        app_desp = criar_app_despachante(p, despachante_db, cifra)
        url_desp = await pilha.enter_async_context(servidor(app_desp, porta_desp))
        url_entrada = await pilha.enter_async_context(servidor(criar_app_entrada(p, entrada_db)))
        client_id = f"g9b-app-{sufixo}"
        async with entrada_db.begin() as c:
            aplicativo_id = await repositorio.habilitar_aplicativo(
                c, client_id=client_id, nome=f"Aplicativo do ensaio {sufixo}", familia="g9b"
            )
            pessoas: dict[str, uuid.UUID] = {}
            for nome in ("ana", "bia"):
                pessoa = await repositorio.pessoa_no_login(c, idp.emissor, f"g9b-{nome}-{sufixo}", nome)
                pessoas[nome] = pessoa.id
        async with dono.begin() as c:
            # Pessoas reais (nao de teste): com a chave da Fase 3 ligada, elas veem e usam o terceiro.
            await c.execute(
                text("UPDATE pessoas SET teste = false WHERE id = ANY(:ids)"), {"ids": list(pessoas.values())}
            )
        desp = ClienteDespachante(p)
        pilha.push_async_callback(desp.fechar)
        terceiro_id, do_terceiro = await _cadastrar(
            entrada_db,
            desp,
            nome=f"fornecedor-g9b-{sufixo}",
            endereco=f"{corte_terceiro.url}/mcp",
            tipo_chave="por_pessoa",
            origem="terceiro",
            chave_sondagem=CHAVE_DO_TERCEIRO,
        )
        cadastro_id, do_cadastro = await _cadastrar(
            entrada_db,
            desp,
            nome=f"cadastro-g9b-{sufixo}",
            endereco=f"{bases['cadastro']}/mcp",
            tipo_chave="por_pessoa",
            origem="proprio",
            chave_sondagem="chave-ana",
        )
        caixa_id, da_caixa = await _cadastrar(
            entrada_db,
            desp,
            nome=f"caixa-g9b-{sufixo}",
            endereco=f"{bases['caixa']}/mcp",
            tipo_chave="por_pessoa",
            origem="proprio",
            chave_sondagem="chave-ana",
        )
        async with dono.begin() as c:
            # A chave de cada pessoa nasce da autorizacao dela na pagina do sistema (OAuth, ``test_autorizacao``);
            # aqui o dono grava o resultado, sem prazo.
            for sistema_id, prefixo in ((terceiro_id, "terceiro"), (cadastro_id, "chave"), (caixa_id, "chave")):
                for nome, pessoa_id in pessoas.items():
                    await chaves.guardar(
                        c, cifra, sistema_id=sistema_id, segredo=f"{prefixo}-{nome}", pessoa_id=pessoa_id
                    )
        terceiro = Sistema(terceiro_id, f"fornecedor-g9b-{sufixo}", bases["terceiro"], apps["terceiro"])
        cadastro = Sistema(cadastro_id, f"cadastro-g9b-{sufixo}", bases["cadastro"], apps["cadastro"])
        caixa = Sistema(caixa_id, f"caixa-g9b-{sufixo}", bases["caixa"], apps["caixa"])
        revisoes: list[tuple[Sistema, dict[str, Any], str, list[str], list[str]]] = [
            (terceiro, do_terceiro["ler_avisos"], "le", [], []),
            (terceiro, do_terceiro["enviar_recado"], "muda", [], []),
            (cadastro, do_cadastro["ver_cadastro"], "le", ["cpf", "email"], []),
            (caixa, da_caixa["enviar_mensagem"], "muda", [], ["destinatario"]),
        ]
        async with entrada_db.begin() as c:
            for sistema, definicao, classe, pessoais, destinatario in revisoes:
                original = str(definicao["name"])
                vitrine = f"{original}_{sufixo}"
                sistema.revisadas[original] = await repositorio.revisar_ferramenta(
                    c,
                    sistema_id=sistema.id,
                    definicao_declarada=definicao,
                    nome_vitrine=vitrine,
                    descricao="Leitura do ensaio." if classe == "le" else "Acao do ensaio.",
                    classe=classe,
                    revisado_por="admin-g9b",
                    campos_pessoais=pessoais,
                    campos_destinatario=destinatario,
                )
                sistema.vitrine[original] = vitrine
        revisado = await desp.post(
            "/interno/revisao_servidor",
            {
                "sistema_id": str(terceiro_id),
                "publicar": ["ler_avisos", "enviar_recado"],
                "fornecedor": "Fornecedor do ensaio",
                "quem": "admin-g9b",
            },
        )
        assert revisado["revisado"] is True, revisado
        async with entrada_db.begin() as c:
            modelo = await repositorio.criar_modelo_ordem(
                c,
                nome=f"g9b-{sufixo}",
                descricao="Ordem de servico do ensaio",
                ferramentas=[(s.id, original) for s, d, *_ in revisoes for original in (str(d["name"]),)],
                validade_dias=30,
                criado_por="admin-g9b",
            )
            for pessoa_id in pessoas.values():
                await repositorio.conceder_ordem(
                    c, pessoa_id=pessoa_id, aplicativo_id=aplicativo_id, modelo_id=modelo, dias=7, max_dias=30
                )
        autor = f"{AUTOR}-{sufixo}"
        async with dono.begin() as c:
            # Fora do periodo de experiencia, para os limites valerem.
            await c.execute(
                text("UPDATE ordens_servico SET criado_em = now() - interval '60 days' WHERE aplicativo_id = :a"),
                {"a": aplicativo_id},
            )
            for chave, valor in (("CONFIRM_CAP_PER_HOUR", 1000), ("OUTBOX_HOLD_SECONDS", 0)):
                await c.execute(
                    text("INSERT INTO politica (chave, valor, autor, motivo) VALUES (:k, :v, :a, 'ensaio do G9b')"),
                    {"k": chave, "v": valor, "a": autor},
                )

        async def apagar_politica() -> None:
            async with dono.begin() as c:
                await c.execute(text("DELETE FROM politica WHERE autor = :a"), {"a": autor})
                await c.execute(text(SOBRAS))

        pilha.push_async_callback(apagar_politica)
        for a in apps.values():
            a.state.contagem.chamadas.clear()
        corte_terceiro.pedidos.clear()
        corte_terceiro.conexoes = 0
        if corte_sala is not None:
            corte_sala.pedidos.clear()
            corte_sala.conexoes = 0
        yield Gateway(
            p=p,
            idp=idp,
            dono=dono,
            entrada_db=entrada_db,
            despachante_db=despachante_db,
            cifra=cifra,
            entrada=url_entrada,
            despachante=url_desp,
            app_desp=app_desp,
            sala=sala,
            corte_sala=corte_sala,
            corte_terceiro=corte_terceiro,
            aplicativo_id=aplicativo_id,
            client_id=client_id,
            pessoas=pessoas,
            terceiro=terceiro,
            cadastro=cadastro,
            caixa=caixa,
            sufixo=sufixo,
        )


async def contagem_http(base: str) -> dict[str, int]:
    """As contagens do exemplo pela rota dele (``GET /contagem``), como um teste de fora leria."""
    async with httpx2.AsyncClient() as http:
        r = await http.get(f"{base}/contagem")
    return dict(r.json()["chamadas"])
