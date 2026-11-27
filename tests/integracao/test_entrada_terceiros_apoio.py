"""Apoio dos testes do G4 (Fase 3): entrada real, servidor de terceiros revisado e sistema proprio, sem despachante.

- A entrada sobe de verdade (uvicorn, cracha do ``IdpFalso``, usuario ``mcp_claro_entrada``), com um leitor falso
  no lugar do despachante (``LeitorFalso``): cada leitura devolve o que o despachante devolveria (texto do sistema
  com o ``_meta`` nosso, ou texto nosso com o motivo).
- O "despachante" das acoes e feito a mao, com as funcoes da fila e o usuario ``mcp_claro_despachante``
  (``enviar_como_despachante``): nenhuma porta de saida roda, entao nada sai nem cancela sozinho.
- Cada ``Mundo`` tem os proprios aplicativos, pessoas e sistemas (nomes com sufixo): os testes nao se misturam no
  banco da sessao.

Ferramentas (chave curta -> nome original, classe, risco, campos pessoais, traz texto de fora):

- terceiro: ``t_ler`` (le), ``t_ver`` (le, com CPF), ``t_enviar`` (muda);
- proprio: ``p_cadastro`` (le, com CPF e e-mail), ``p_caixa`` (le, marcada "traz texto de fora"), ``p_simples``
  (le), ``p_mensagem`` (muda), ``p_cancelar`` (muda, risco alto).

Pessoas: ``ana`` e ``bia`` (de teste), ``real`` (pessoa real, fora do periodo de experiencia) e ``eva`` (de teste,
no periodo de experiencia). Aplicativos ``A`` (todas) e ``B`` (so a Ana).

``subir_mundo(..., extras=...)`` acrescenta ferramentas com esquema proprio (``Extra``), na mesma ordem de servico.
"""

from __future__ import annotations

import contextlib
import copy
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx2
import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro.cadastro import repositorio
from mcp_claro.cadastro.modelos import FerramentaVitrine
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.entrada.app import criar_app_entrada
from mcp_claro.entrada.servidor import PedidoLeitura
from mcp_claro.saida import fila
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.pedidos import CONSULTA, Pedido
from mcp_claro.versao import PREFIXO_META, VERSAO_MCP
from tests import apoio_terceiros as t3
from tests.apoio import cabecalhos_2026, cliente_mcp, pedido_2026, porta_livre, servidor
from tests.idp_falso import IdpFalso
from tests.integracao.test_autorizacao import engine_do_papel

PUBLICO = "https://mcp-claro.teste"
RECURSO = f"{PUBLICO}/mcp"

ESQUEMA = {
    "type": "object",
    "properties": {
        "texto": {"type": "string"},
        "destinatario": {"type": "string"},
        "lista": {"type": "array"},
    },
}

# chave curta: (nome original, classe, risco, campos pessoais, traz texto de fora)
TERCEIRO: dict[str, tuple[str, str, str, tuple[str, ...], bool]] = {
    "t_ler": ("ler_mensagens", "le", "medio", (), False),
    "t_ver": ("ver_cliente", "le", "medio", ("cpf",), False),
    "t_enviar": ("enviar_resposta", "muda", "medio", (), False),
}
PROPRIO: dict[str, tuple[str, str, str, tuple[str, ...], bool]] = {
    "p_cadastro": ("ver_cadastro", "le", "medio", ("cpf", "email"), False),
    "p_caixa": ("ler_caixa", "le", "medio", (), True),
    "p_simples": ("ver_horario", "le", "medio", (), False),
    "p_mensagem": ("enviar_mensagem", "muda", "medio", (), False),
    "p_cancelar": ("cancelar_servico", "muda", "alto", (), False),
}
TEXTO_DO_SISTEMA = "Texto que o sistema escreveu."


@dataclass(frozen=True)
class Extra:
    """Ferramenta a mais num ``Mundo``: no terceiro ou no proprio, com o esquema dado."""

    terceiro: bool
    spec: tuple[str, str, str, tuple[str, ...], bool]
    esquema: dict[str, Any]


@dataclass(frozen=True)
class Ferramenta:
    id: uuid.UUID
    nome: str  # nome na vitrine
    sistema_id: uuid.UUID
    nome_original: str
    classe: str


def resultado_do_sistema(sistema: str, texto: str = TEXTO_DO_SISTEMA, *, erro: bool = False) -> dict[str, Any]:
    """O que o despachante devolve com o texto do sistema: so o ``_meta`` nosso, sem motivo."""
    return {
        "content": [{"type": "text", "text": texto}],
        "isError": erro,
        "_meta": {PREFIXO_META + "sistema": sistema, PREFIXO_META + "selo": 1},
    }


def resultado_nosso(sistema: str, motivo: str, texto: str = "Texto do MCP-CLARO.") -> dict[str, Any]:
    """O que o despachante devolve com texto nosso (falha, pedido barrado): o motivo no ``_meta``."""
    return {
        "content": [{"type": "text", "text": texto}],
        "isError": True,
        "_meta": {PREFIXO_META + "motivo": motivo, PREFIXO_META + "sistema": sistema, PREFIXO_META + "selo": 1},
    }


Resposta = dict[str, Any] | BaseException | Callable[[PedidoLeitura], Awaitable[dict[str, Any]]]


@dataclass
class LeitorFalso:
    """Faz o papel do despachante nas leituras: ``respostas[ferramenta_id]`` ou o texto do sistema."""

    sistemas: dict[uuid.UUID, str] = field(default_factory=dict)  # ferramenta_id -> nome do sistema
    respostas: dict[uuid.UUID, Resposta] = field(default_factory=dict)
    chamadas: list[PedidoLeitura] = field(default_factory=list)

    async def __call__(self, pedido: PedidoLeitura) -> dict[str, Any]:
        self.chamadas.append(pedido)
        r = self.respostas.get(pedido.ferramenta_id)
        if isinstance(r, BaseException):
            raise r
        if callable(r):
            return await r(pedido)
        if r is None:
            r = resultado_do_sistema(self.sistemas.get(pedido.ferramenta_id, "?"))
        return copy.deepcopy(r)


@dataclass
class Mundo:
    p: Parametros
    idp: IdpFalso
    dono: AsyncEngine
    entrada_db: AsyncEngine
    despachante_db: AsyncEngine
    rotinas_db: AsyncEngine
    url: str
    leitor: LeitorFalso
    apps: dict[str, uuid.UUID]
    client_ids: dict[str, str]
    pessoas: dict[str, uuid.UUID]
    terceiro_id: uuid.UUID
    terceiro_nome: str
    revisao_id: uuid.UUID
    proprio_id: uuid.UUID
    proprio_nome: str
    f: dict[str, Ferramenta]
    ordens: dict[tuple[str, str], uuid.UUID]
    sufixo: str

    def cracha(self, pessoa: str = "ana", app: str = "A") -> str:
        return self.idp.cracha(audiencia=RECURSO, sujeito=f"g4-{pessoa}-{self.sufixo}", client_id=self.client_ids[app])

    async def chamar(self, pessoa: str, chave: str, argumentos: dict[str, Any] | None = None, app: str = "A") -> Any:
        """Uma chamada pelo aplicativo. Recusa sai como ``MCPError`` (fora do grupo de tarefas do cliente)."""
        recusa: MCPError | None = None
        async with cliente_mcp(f"{self.url}/mcp", self.cracha(pessoa, app)) as c:
            try:
                return await c.call_tool(self.f[chave].nome, argumentos or {})
            except MCPError as erro:
                recusa = erro
        raise recusa

    async def chamar_cru(
        self, pessoa: str, chave: str, argumentos: dict[str, Any], app: str = "A"
    ) -> tuple[int, dict[str, Any]]:
        """Uma chamada em JSON cru, sem o cliente do kit (que nao serializa mais de uns 250 niveis). Devolve o status
        HTTP e a resposta JSON-RPC inteira (``result`` ou ``error``)."""
        nome = self.f[chave].nome
        corpo = json.dumps(pedido_2026("tools/call", {"name": nome, "arguments": argumentos}))
        cabecalhos = cabecalhos_2026("tools/call", nome, self.cracha(pessoa, app))
        async with httpx2.AsyncClient(follow_redirects=False, timeout=30) as http:
            r = await http.post(f"{self.url}/mcp", content=corpo, headers=cabecalhos)
        return r.status_code, r.json()

    async def consultar(self, pessoa: str, protocolo: str, app: str = "A") -> Any:
        async with cliente_mcp(f"{self.url}/mcp", self.cracha(pessoa, app)) as c:
            return await c.call_tool(CONSULTA, {"protocolo": protocolo})

    async def listar(self, pessoa: str, app: str = "A") -> list[str]:
        async with cliente_mcp(f"{self.url}/mcp", self.cracha(pessoa, app)) as c:
            return sorted(t.name for t in (await c.list_tools()).tools)

    async def vitrine(self, pessoa: str, chave: str, *, terceiros: bool = True, app: str = "A") -> FerramentaVitrine:
        async with self.entrada_db.connect() as c:
            f = await repositorio.ferramenta_na_vitrine(
                c, self.pessoas[pessoa], self.apps[app], self.f[chave].nome, VERSAO_MCP, terceiros=terceiros
            )
        assert f is not None, chave
        return f

    async def pedido(
        self, pessoa: str, chave: str, argumentos: dict[str, Any], *, terceiros: bool = True, app: str = "A"
    ) -> Pedido:
        aplicativo = await self.aplicativo(app)
        f = await self.vitrine(pessoa, chave, terceiros=terceiros, app=app)
        return Pedido(self.pessoas[pessoa], aplicativo, f, argumentos, None, terceiros=terceiros)

    async def aplicativo(self, app: str = "A") -> Any:
        async with self.entrada_db.connect() as c:
            a = await repositorio.aplicativo_por_client_id(c, self.client_ids[app])
        assert a is not None
        return a

    async def sinais(self, pessoa: str, app: str = "A") -> list[Any]:
        async with self.dono.connect() as c:
            return list(
                (
                    await c.execute(
                        text(
                            "SELECT tipo, ferramenta_id, sistema_id, visto_em FROM sinais"
                            " WHERE pessoa_id = :p AND aplicativo_id = :a ORDER BY id"
                        ),
                        {"p": self.pessoas[pessoa], "a": self.apps[app]},
                    )
                ).all()
            )

    async def extrato(self, tipo: str, *, pessoa: str | None = None, protocolo: str | None = None) -> list[Any]:
        """Linhas do extrato deste mundo (pelos aplicativos e sistemas dele)."""
        async with self.dono.connect() as c:
            return list(
                (
                    await c.execute(
                        text(
                            "SELECT frase, dados, pessoa_id, protocolo, ferramenta, papel FROM extrato WHERE tipo = :t"
                            " AND (aplicativo_id = ANY(:a) OR sistema_id = ANY(:s))"
                            " AND (CAST(:p AS uuid) IS NULL OR pessoa_id = :p)"
                            " AND (CAST(:pr AS text) IS NULL OR protocolo = :pr) ORDER BY id"
                        ),
                        {
                            "t": tipo,
                            "a": list(self.apps.values()),
                            "s": [self.terceiro_id, self.proprio_id],
                            "p": self.pessoas[pessoa] if pessoa else None,
                            "pr": protocolo,
                        },
                    )
                ).all()
            )

    async def operacao(self, protocolo: str) -> fila.Operacao:
        async with self.dono.connect() as c:
            i = (await c.execute(text("SELECT id FROM operacoes WHERE protocolo = :p"), {"p": protocolo})).scalar_one()
            op = await fila.por_id(c, i)
        assert op is not None
        return op

    async def limite(
        self, pessoa: str, chave: str, *, por_dia: int = 5, app: str = "A", destinatarios: list[Any] | None = None
    ) -> uuid.UUID:
        """Um limite por aproximacao ativo, feito com passkey, gravado pelo dono."""
        f = self.f[chave]
        async with self.dono.begin() as c:
            return uuid.UUID(
                str(
                    (
                        await c.execute(
                            text(
                                "INSERT INTO limites (pessoa_id, aplicativo_id, sistema_id, nome_original,"
                                " destinatarios, por_dia, valido_ate, com_passkey) VALUES (:p, :a, :s, :n,"
                                " CAST(:d AS jsonb), :pd, now() + interval '10 days', true) RETURNING id"
                            ),
                            {
                                "p": self.pessoas[pessoa],
                                "a": self.apps[app],
                                "s": f.sistema_id,
                                "n": f.nome_original,
                                "d": json.dumps(destinatarios or []),
                                "pd": por_dia,
                            },
                        )
                    ).scalar_one()
                )
            )

    async def confirmar(self, op: fila.Operacao, pessoa: str = "ana") -> fila.Operacao:
        assert op.ficha_id is not None
        async with self.entrada_db.begin() as c:
            return await fila.confirmar_ficha(
                c,
                op.ficha_id,
                pessoa_id=self.pessoas[pessoa],
                com_passkey=True,
                espera_segundos=0,
                rapida_segundos=0,
            )

    async def enviar_como_despachante(self, op: fila.Operacao, resultado: dict[str, Any]) -> fila.Operacao:
        """O que o despachante faria com a acao liberada: "enviando" e o fim, com o resultado ja com a mascara."""
        async with self.despachante_db.begin() as c:
            atual = await fila.por_id(c, op.id)
            assert atual is not None
            assert atual.estado == Estado.LIBERADA, atual
            assert await fila.marcar_enviando(c, atual)
        async with self.despachante_db.begin() as c:
            atual = await fila.por_id(c, op.id)
            assert atual is not None
            assert await fila.finalizar(c, atual, Estado.ENVIADA, resultado, "o sistema respondeu")
        return await self.operacao(op.protocolo)


async def _revisar(
    c: Any,
    sistema_id: uuid.UUID,
    chave: str,
    spec: tuple[str, str, str, tuple[str, ...], bool],
    sufixo: str,
    esquema: dict[str, Any] = ESQUEMA,
) -> Ferramenta:
    nome, classe, risco, pessoais, de_fora = spec
    r = await t3.revisar_ferramenta(
        c,
        sistema_id,
        t3.definicao(nome, esquema=esquema),
        nome_vitrine=f"{chave}_{sufixo}",
        classe=classe,
        risco=risco,
        campos_pessoais=pessoais,
        texto_de_fora=de_fora,
    )
    return Ferramenta(r.id, r.nome_vitrine, sistema_id, nome, classe)


@contextlib.asynccontextmanager
async def subir_mundo(
    banco: Parametros,
    *,
    terceiros_ligados: bool = False,
    extra: dict[str, Any] | None = None,
    extras: dict[str, Extra] | None = None,
) -> AsyncIterator[Mundo]:
    """Sobe a entrada com o leitor falso e monta o cadastro. ``terceiros_ligados``: ``FEATURE_PHASE3_THIRD_PARTY``
    (pelo construtor, como nos testes de outras chaves que a partida recusa). ``extra``: parametros a mais.
    ``extras``: ferramentas a mais (chave curta -> ``Extra``), revisadas antes da revisao do servidor."""
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
                "FEATURE_PHASE3_THIRD_PARTY": terceiros_ligados,
                "RESPONSE_MAX_WAIT_SECONDS": 2,
            }
            | (extra or {})
        )
    )
    dono = criar_engine(p, pool_size=3, max_overflow=3)
    entrada_db = engine_do_papel(p, "mcp_claro_entrada")
    despachante_db = engine_do_papel(p, "mcp_claro_despachante")
    rotinas_db = engine_do_papel(p, "mcp_claro_rotinas")
    leitor = LeitorFalso()
    try:
        async with dono.begin() as c:
            client_ids = {"A": f"g4-app-a-{sufixo}", "B": f"g4-app-b-{sufixo}"}
            apps = {
                k: await repositorio.habilitar_aplicativo(c, client_id=v, nome=f"Aplicativo {k} {sufixo}", familia="g4")
                for k, v in client_ids.items()
            }
            pessoas: dict[str, uuid.UUID] = {}
            for nome in ("ana", "bia", "real", "eva"):
                pessoa = await repositorio.pessoa_no_login(c, idp.emissor, f"g4-{nome}-{sufixo}", nome)
                pessoas[nome] = pessoa.id
            await c.execute(
                text("UPDATE pessoas SET teste = (id <> :real) WHERE id = ANY(:ids)"),
                {"real": pessoas["real"], "ids": list(pessoas.values())},
            )
            terceiro_nome = f"terceiro-g4-{sufixo}"
            terceiro_id = await t3.cadastrar_terceiro(c, nome=terceiro_nome)
            proprio_nome = f"proprio-g4-{sufixo}"
            proprio_id = await repositorio.cadastrar_sistema(
                c, nome=proprio_nome, endereco=f"https://proprio-{sufixo}.exemplo.teste/mcp", tipo_chave="por_pessoa"
            )
            await c.execute(
                text("UPDATE sistemas SET situacao = 'ativo', versao_mcp = :v WHERE id = :s"),
                {"v": VERSAO_MCP, "s": proprio_id},
            )
            ferramentas: dict[str, Ferramenta] = {}
            for chave, spec in TERCEIRO.items():
                ferramentas[chave] = await _revisar(c, terceiro_id, chave, spec, sufixo)
            for chave, spec in PROPRIO.items():
                ferramentas[chave] = await _revisar(c, proprio_id, chave, spec, sufixo)
            for chave, mais in (extras or {}).items():
                sistema = terceiro_id if mais.terceiro else proprio_id
                ferramentas[chave] = await _revisar(c, sistema, chave, mais.spec, sufixo, mais.esquema)
            revisao_id = await t3.gravar_revisao_do_servidor(c, terceiro_id)
            modelo = await repositorio.criar_modelo_ordem(
                c,
                nome=f"g4-{sufixo}",
                descricao="Ordem de servico de teste do G4",
                ferramentas=[(f.sistema_id, f.nome_original) for f in ferramentas.values()],
                validade_dias=30,
                criado_por="admin-teste",
            )
            ordens: dict[tuple[str, str], uuid.UUID] = {}
            for pessoa, app in (("ana", "A"), ("bia", "A"), ("real", "A"), ("eva", "A"), ("ana", "B")):
                ordens[(pessoa, app)] = await repositorio.conceder_ordem(
                    c, pessoa_id=pessoas[pessoa], aplicativo_id=apps[app], modelo_id=modelo, dias=7, max_dias=30
                )
            # Fora do periodo de experiencia, menos a Eva.
            await c.execute(
                text("UPDATE ordens_servico SET criado_em = now() - interval '60 days' WHERE id = ANY(:o)"),
                {"o": [o for (pessoa, _), o in ordens.items() if pessoa != "eva"]},
            )
        for f in ferramentas.values():
            leitor.sistemas[f.id] = terceiro_nome if f.sistema_id == terceiro_id else proprio_nome
        with pytest.MonkeyPatch.context() as mp:
            # A partida recusa a chave da Fase 3 ligada (D6); os testes a ligam pelo construtor, sem conferir.
            mp.setattr(Parametros, "conferir", lambda self: self)
            app = criar_app_entrada(p, entrada_db, leitor=leitor)
        async with servidor(idp.app(), porta_idp), servidor(app) as url:
            yield Mundo(
                p=p,
                idp=idp,
                dono=dono,
                entrada_db=entrada_db,
                despachante_db=despachante_db,
                rotinas_db=rotinas_db,
                url=url,
                leitor=leitor,
                apps=apps,
                client_ids=client_ids,
                pessoas=pessoas,
                terceiro_id=terceiro_id,
                terceiro_nome=terceiro_nome,
                revisao_id=revisao_id,
                proprio_id=proprio_id,
                proprio_nome=proprio_nome,
                f=ferramentas,
                ordens=ordens,
                sufixo=sufixo,
            )
    finally:
        for engine in (dono, entrada_db, despachante_db, rotinas_db):
            await engine.dispose()


def textos(resultado: Any) -> list[str]:
    return [b.text for b in resultado.content if getattr(b, "type", None) == "text"]
