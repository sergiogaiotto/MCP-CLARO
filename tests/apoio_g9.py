"""Apoio das provas de saida da Fase 3 (G9a): o gateway inteiro, como no piloto, com servidores de teste.

Cada ``Gateway`` sobe de verdade, num teste:

- o PostgreSQL da sessao (``banco_migrado``), cada papel com o proprio usuario do banco (prova os GRANTs);
- o provedor OIDC falso (``tests/idp_oidc_falso.py``): o cracha do aplicativo e o login das telas;
- a sala de visitas de verdade (``visitas.app.Sala``: um processo filho por chamada, mTLS com os certificados de
  ``tests/apoio_visitas.gerar_certificados``), em desenvolvimento para aceitar o endereco local dos terceiros de
  teste;
- o despachante de verdade (``criar_app_despachante``), com ``VISITS_INTERNAL_URL`` apontando para a sala e sem o
  cliente local de teste: todo pedido a um terceiro passa pela sala, como no piloto;
- a entrada de verdade (``criar_app_entrada``), com as telas da pessoa e do ADMIN.

A chave da Fase 3 fica ligada pelo construtor de ``Parametros``, sem conferir: a partida ainda recusa a chave ligada
(D6), e as provas mostram o comportamento de depois da decisao do dono.

Os servidores de teste sobem com ``Gateway.servir``. Com ``gravar=True``, o endereco cadastrado e o de um
``Cortador`` em modo ``inteiro`` (``tests/cortador.py``, so usado), que guarda cada byte que chega ao servidor,
conexao por conexao; o ``/modo`` dos exemplos vai direto ao servidor, fora da gravacao.

O ADMIN (``Admin``) habilita os aplicativos, cadastra os programas e da a ordem deles, cadastra, sonda, le, revisa,
guarda chaves, conclui a revisao do servidor e cria modelos pelas telas, com CSRF. A pessoa da a ordem de servico
pela tela de ordens e cria os limites pela tela de limites, com a passkey do autenticador de software de teste
(``tests/autenticador_falso.py``, como em ``test_telas_acoes``).

Ficam pelo banco, gravados pelo dono das tabelas (ou pelo usuario da entrada ou do despachante), so o que nenhuma
tela faz, ou o que uma tela faria do mesmo jeito:

- a pessoa nasce por ``repositorio.pessoa_no_login``, pelo usuario da entrada (``Gateway.pessoa``): e a mesma
  funcao que o primeiro login das telas chama, so que antes de a pessoa entrar. Depois ela entra pelas telas
  (``Gateway.entrar``), com o mesmo emissor e o mesmo sujeito, e o login acha a linha;
- o papel de ADMIN da pessoa (nenhuma tela o da; a instalacao o grava no banco);
- a chave da pessoa num sistema de chave por pessoa: ela nasce da autorizacao da pessoa no sistema (OAuth), que pede
  um servidor de autorizacao para cada sistema de teste. A tela e a troca do codigo tem prova propria
  (``test_autorizacao``, ``test_telas_autorizacao``, ``test_g3a_autorizacao_terceiros``); aqui a chave e gravada
  pelo usuario do despachante, como ele grava depois da troca do codigo;
- as ordens recuadas 60 dias, para sair do periodo de experiencia (o tempo nao passa num teste, e nada de esperar);
- valores de politica so durante um bloco (``Gateway.politica``), apagados no fim (nenhuma tela muda a politica);
- na prova 5, um sinal com hora de 37 minutos atras (a guarda de ``sinais`` so deixa o dono gravar outra hora).
"""

from __future__ import annotations

import contextlib
import json
import re
import uuid
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from mcp_claro import __main__ as partida
from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.db.engine import criar_engine
from mcp_claro.despachante import chaves
from mcp_claro.despachante.app import criar_app_despachante
from mcp_claro.entrada.app import criar_app_entrada
from mcp_claro.rotinas.despachante import ClienteDespachante as RotinasDespachante
from mcp_claro.rotinas.mudou_parou import ConferenciaNaoFeita, FonteDeclaracoes, ResultadoMudouParou
from mcp_claro.rotinas.mudou_parou import conferir_sistemas as _conferir_sistemas
from mcp_claro.versao import PREFIXO_META, VERSAO_MCP
from mcp_claro.visitas import app as visitas_app
from tests import apoio_g3a as g
from tests import apoio_visitas
from tests.apoio import porta_livre, servidor
from tests.autenticador_falso import AutenticadorFalso
from tests.cifra_falsa import CifraFalsa
from tests.cortador import Cortador
from tests.idp_oidc_falso import IdpOidcFalso
from tests.integracao.test_telas import Navegador, token_csrf

PUBLICO = "https://mcp-claro.teste"
RECURSO = f"{PUBLICO}/mcp"
ADMIN_EMAIL = "admin-g9@exemplo.teste"
SEGUIDAS = 0.0
"""Intervalo do mudou-parou nas rodadas uma atras da outra (pedido do G5r ao G9): com zero, cada rodada sem resposta
soma; o resto da conferencia nao depende do intervalo."""


# --- Servidores de teste ----------------------------------------------------------------------------------------


@dataclass
class Servido:
    """Um servidor de teste no ar. ``base`` e o que vai ao cadastro (o do gravador, quando ha); ``direto`` e o
    servidor sem o gravador, para mudar o modo do exemplo sem sujar a gravacao."""

    app: Any
    direto: str
    base: str
    gravador: Cortador | None = None

    @property
    def endereco(self) -> str:
        return f"{self.base}/mcp"

    async def modo(self, modo: str, **extra: Any) -> None:
        async with httpx2.AsyncClient(trust_env=False, timeout=10) as http:
            (await http.post(f"{self.direto}/modo", json={"modo": modo, **extra})).raise_for_status()

    def conexoes(self) -> list[bytes]:
        """Cada conexao que chegou ao servidor, com todos os bytes do pedido (cabecalhos e corpo)."""
        assert self.gravador is not None, "servidor sem gravador"
        return list(self.gravador.pedidos)

    def recebido(self) -> bytes:
        return b"\n".join(self.conexoes())

    def zerar(self) -> None:
        assert self.gravador is not None
        self.gravador.pedidos.clear()

    def chamadas(self, nome: str) -> int:
        return int(self.app.state.contagem.chamadas.get(nome, 0))


# --- O gateway --------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Pessoa:
    apelido: str
    sujeito: str
    id: uuid.UUID


@dataclass(frozen=True)
class Aplicativo:
    id: uuid.UUID
    client_id: str
    nome: str


@dataclass
class Gateway:
    p: Parametros
    idp: IdpOidcFalso
    dono: AsyncEngine  # o usuario das migracoes: so para preparar o que as telas nao fazem e para conferir
    entrada_db: AsyncEngine
    desp_db: AsyncEngine
    rotinas_db: AsyncEngine
    grafana_db: AsyncEngine
    cifra: CifraFalsa
    entrada: str
    despachante: str
    sala: str
    pela_sala: list[str]  # cada pedido que chegou a sala (a rota), na ordem
    http: httpx2.AsyncClient
    pilha: contextlib.AsyncExitStack
    sufixo: str
    pessoas: dict[str, Pessoa] = field(default_factory=dict)
    passkeys: dict[uuid.UUID, AutenticadorFalso] = field(default_factory=dict)
    _admin: Admin | None = None

    # --- servidores de teste ---

    async def servir(self, app: Any, *, gravar: bool = False) -> Servido:
        direto = await self.pilha.enter_async_context(servidor(app))
        if not gravar:
            return Servido(app, direto, direto)
        partes = urlsplit(direto)
        gravador = Cortador(str(partes.hostname), int(partes.port or 80))
        await gravador.iniciar()
        self.pilha.push_async_callback(gravador.parar)
        return Servido(app, direto, gravador.url, gravador)

    # --- aplicativos e pessoas ---

    def client_id(self, nome: str) -> str:
        """O client_id do aplicativo ``nome`` deste teste (o programa de teste precisa dele antes do cadastro)."""
        return f"g9-{nome.lower().replace(' ', '-')}-{self.sufixo}"

    async def aplicativo(self, nome: str) -> Aplicativo:
        """Um aplicativo assistente, habilitado pelo ADMIN na tela de aplicativos (programa: ``Admin.programa``)."""
        admin = await self.admin()
        client_id = self.client_id(nome)
        r = await admin.post(
            "/telas/admin/aplicativos",
            {"client_id": client_id, "nome": f"{nome} {self.sufixo}", "familia": "g9", "tipo": "assistente"},
        )
        assert (r.status_code, r.headers.get("location")) == (
            303,
            "/telas/admin/aplicativos?aviso=aplicativo_habilitado",
        )
        return await self.aplicativo_habilitado(client_id)

    async def aplicativo_habilitado(self, client_id: str) -> Aplicativo:
        async with self.dono.connect() as c:
            linha = (
                await c.execute(text("SELECT id, nome FROM aplicativos WHERE client_id = :c"), {"c": client_id})
            ).one()
        return Aplicativo(uuid.UUID(str(linha.id)), client_id, str(linha.nome))

    async def pessoa(self, apelido: str, nome: str | None = None) -> Pessoa:
        """Uma pessoa real (nao de teste), criada como o primeiro login a cria (``repositorio.pessoa_no_login``,
        pelo usuario da entrada). A chave da Fase 3 fica ligada nestas provas: ela ve terceiros."""
        sujeito = f"g9-{apelido}-{self.sufixo}"
        async with self.entrada_db.begin() as c:
            linha = await repositorio.pessoa_no_login(c, self.idp.emissor, sujeito, nome or apelido.capitalize())
        pessoa = Pessoa(apelido, sujeito, linha.id)
        self.pessoas[apelido] = pessoa
        return pessoa

    async def chave_da_pessoa(self, sistema_id: uuid.UUID, pessoa: Pessoa, segredo: str) -> None:
        """A chave da pessoa num sistema de chave por pessoa, gravada pelo usuario do despachante, como depois da
        troca do codigo da autorizacao dela no sistema."""
        async with self.desp_db.begin() as c:
            await chaves.guardar(c, self.cifra, sistema_id=sistema_id, pessoa_id=pessoa.id, segredo=segredo)

    async def fora_da_experiencia(self, pessoa: Pessoa) -> None:
        """As ordens da pessoa como se fossem de 60 dias atras: fora do periodo de experiencia (so o dono muda)."""
        async with self.dono.begin() as c:
            await c.execute(
                text("UPDATE ordens_servico SET criado_em = now() - interval '60 days' WHERE pessoa_id = :p"),
                {"p": pessoa.id},
            )

    # --- o aplicativo ---

    def cracha(self, pessoa: Pessoa, app: Aplicativo) -> str:
        return self.idp.cracha(audiencia=RECURSO, sujeito=pessoa.sujeito, client_id=app.client_id)

    @contextlib.asynccontextmanager
    async def cliente(
        self, pessoa: Pessoa, app: Aplicativo, *, cabecalhos: Mapping[str, str] | None = None
    ) -> AsyncIterator[Client]:
        """O cliente oficial do kit, fixado na 2026-07-28, com o cracha da pessoa no aplicativo."""
        h = {"authorization": f"Bearer {self.cracha(pessoa, app)}", **dict(cabecalhos or {})}
        http = httpx2.AsyncClient(headers=h, follow_redirects=False, timeout=60, trust_env=False)
        async with http, Client(streamable_http_client(f"{self.entrada}/mcp", http_client=http), mode=VERSAO_MCP) as c:
            yield c

    async def listar(self, pessoa: Pessoa, app: Aplicativo) -> list[str]:
        async with self.cliente(pessoa, app) as c:
            return sorted(t.name for t in (await c.list_tools()).tools)

    async def chamar(
        self,
        pessoa: Pessoa,
        app: Aplicativo,
        nome: str,
        argumentos: Mapping[str, Any] | None = None,
        *,
        meta: Mapping[str, Any] | None = None,
        cabecalhos: Mapping[str, str] | None = None,
    ) -> Any:
        """Uma chamada pelo aplicativo. A recusa volta como ``MCPError`` (fora do grupo de tarefas do cliente)."""
        recusa: MCPError | None = None
        async with self.cliente(pessoa, app, cabecalhos=cabecalhos) as c:
            try:
                return await c.call_tool(nome, dict(argumentos or {}), meta=cast(Any, dict(meta)) if meta else None)
            except MCPError as erro:
                recusa = erro
        return recusa

    # --- telas ---

    def navegador(self) -> Navegador:
        return Navegador(self.entrada, self.http)

    async def entrar(self, pessoa: Pessoa) -> Navegador:
        """Login pelas telas, como a pessoa (o mesmo emissor e sujeito do cracha)."""
        nav = self.navegador()
        self.idp.proxima_pessoa = (pessoa.sujeito, pessoa.apelido.capitalize())
        r = await nav.get("/telas/login?voltar=/telas")
        assert r.status_code == 303, r.text
        r = await self.http.get(r.headers["location"])  # o provedor falso devolve direto o codigo
        assert r.status_code == 302, r.text
        volta = urlsplit(r.headers["location"])
        r = await nav.get(f"{volta.path}?{volta.query}")
        assert r.status_code == 303, r.text
        return nav

    async def admin(self) -> Admin:
        """O ADMIN deste gateway, logado nas telas (um so por teste)."""
        if self._admin is None:
            pessoa = await self.pessoa("admin", "Carla Admin")
            async with self.dono.begin() as c:  # nenhuma tela da o papel de ADMIN
                await c.execute(text("UPDATE pessoas SET papel = 'admin' WHERE id = :p"), {"p": pessoa.id})
            nav = await self.entrar(pessoa)
            csrf = token_csrf((await nav.get("/telas/admin")).text)
            self._admin = Admin(self, nav, csrf, pessoa)
        return self._admin

    async def dar_ordem(self, pessoa: Pessoa, app: Aplicativo, modelo_id: uuid.UUID, dias: int = 7) -> Navegador:
        """A pessoa da a ordem de servico ao aplicativo pela tela de ordens."""
        nav = await self.entrar(pessoa)
        csrf = token_csrf((await nav.get("/telas/ordens")).text)
        r = await nav.post(
            "/telas/ordens", {"aplicativo": str(app.id), "modelo": str(modelo_id), "dias": str(dias)}, csrf=csrf
        )
        assert r.status_code == 303, r.text
        return nav

    @contextlib.asynccontextmanager
    async def politica(self, **valores: int) -> AsyncIterator[None]:
        """Valores de politica so durante o bloco (a linha mais nova vale), gravados e apagados pelo dono."""
        autor = f"teste-g9-{uuid.uuid4().hex[:8]}"
        async with self.dono.begin() as c:
            for chave, valor in valores.items():
                await c.execute(
                    text(
                        "INSERT INTO politica (chave, valor, autor, motivo)"
                        " VALUES (:k, :v, :a, 'valor de politica das provas de saida')"
                    ),
                    {"k": chave, "v": valor, "a": autor},
                )
        try:
            yield
        finally:
            async with self.dono.begin() as c:
                await c.execute(text("DELETE FROM politica WHERE autor = :a"), {"a": autor})

    async def _passkey(self, pessoa: Pessoa, nav: Navegador) -> AutenticadorFalso:
        """A passkey da pessoa, cadastrada uma vez pela tela de passkeys (autenticador de software de teste)."""
        if pessoa.id not in self.passkeys:
            aut = AutenticadorFalso(PUBLICO)
            csrf = token_csrf((await nav.get("/telas/passkeys")).text)
            r = await nav.post("/telas/passkeys/desafio", {"credencial": ""}, csrf=csrf)
            assert r.status_code == 200, r.text
            credencial = json.dumps(aut.criar(r.json()))
            r = await nav.post("/telas/passkeys", {"apelido": "Celular", "credencial": credencial}, csrf=csrf)
            assert (r.status_code, r.headers.get("location")) == (303, "/telas/passkeys?aviso=passkey_cadastrada")
            self.passkeys[pessoa.id] = aut
        return self.passkeys[pessoa.id]

    async def limite(
        self,
        pessoa: Pessoa,
        app: Aplicativo,
        sistema_id: uuid.UUID,
        nome_original: str,
        *,
        campo: str | None = None,
        valores: Sequence[str] = (),
        por_dia: int = 5,
        dias: int = 10,
    ) -> uuid.UUID:
        """Um limite por aproximacao, criado pela pessoa na tela de limites, com a passkey: o desafio preso ao
        pedido exato e a assinatura do autenticador de teste, como o passkey.js faz. Com ``campo``, "so estes
        destinatarios" (``valores``); sem, qualquer destinatario. Devolve o id do limite."""
        ferramenta = await self.revisada(sistema_id, nome_original)
        nav = await self.entrar(pessoa)
        aut = await self._passkey(pessoa, nav)
        escolha = f"{app.id}:{ferramenta.id}"
        pagina = await nav.get(f"/telas/limites/novo?escolha={escolha}")
        assert pagina.status_code == 200, pagina.text
        csrf = token_csrf(pagina.text)
        form = {"escolha": escolha, "por_dia": str(por_dia), "dias": str(dias)}
        if campo is None:
            form["destinatarios"] = "qualquer"
        else:
            form |= {"destinatarios": "estes", "campo": campo, "valores": "\n".join(valores)}
        r = await nav.post("/telas/limites/desafio", {**form, "credencial": ""}, csrf=csrf)
        assert r.status_code == 200, r.text
        credencial = json.dumps(aut.assinar(r.json()))
        r = await nav.post("/telas/limites", {**form, "credencial": credencial}, csrf=csrf)
        assert r.status_code == 303, r.text
        caminho, _, aviso = r.headers["location"].partition("?")
        assert aviso == "aviso=limite_criado", r.headers["location"]
        return uuid.UUID(caminho.removeprefix("/telas/limites/"))

    # --- rotinas e despachante ---

    async def interno(self, rota: str, corpo: Mapping[str, Any]) -> httpx2.Response:
        async with httpx2.AsyncClient(timeout=60, trust_env=False) as http:
            return await http.post(f"{self.despachante}{rota}", json=dict(corpo))

    async def conferir(self, *sistemas: uuid.UUID) -> ResultadoMudouParou:
        """Uma rodada do mudou-parou das rotinas (usuario ``mcp_claro_rotinas``), que le as declaracoes pelo
        despachante de verdade, como em producao. So os sistemas dados: os de outros testes da sessao ficam como
        conferencia nao feita, sem efeito neles."""
        fonte = RotinasDespachante(self.p)
        try:
            return await _conferir_sistemas(
                self.rotinas_db, SoEstes(fonte, set(sistemas)), [ADMIN_EMAIL], intervalo=SEGUIDAS
            )
        finally:
            await fonte.fechar()

    # --- conferencias ---

    async def ultimo_extrato(self) -> int:
        async with self.dono.connect() as c:
            return int((await c.execute(text("SELECT coalesce(max(id), 0) FROM extrato"))).scalar_one())

    async def extrato(
        self,
        *,
        sistema_id: uuid.UUID | None = None,
        pessoa: Pessoa | None = None,
        tipo: str | None = None,
        depois_de: int = 0,
    ) -> list[Any]:
        async with self.dono.connect() as c:
            return list(
                (
                    await c.execute(
                        text(
                            "SELECT id, tipo, frase, dados, pessoa_id, sistema_id, aplicativo_id, protocolo,"
                            " ferramenta, papel, trace_id FROM extrato"
                            " WHERE (CAST(:s AS uuid) IS NULL OR sistema_id = :s)"
                            " AND (CAST(:p AS uuid) IS NULL OR pessoa_id = :p)"
                            " AND (CAST(:t AS text) IS NULL OR tipo = :t) AND id > :d ORDER BY id"
                        ),
                        {"s": sistema_id, "p": pessoa.id if pessoa else None, "t": tipo, "d": depois_de},
                    )
                ).all()
            )

    async def ultimo_aviso(self) -> int:
        async with self.dono.connect() as c:
            return int((await c.execute(text("SELECT coalesce(max(id), 0) FROM avisos"))).scalar_one())

    async def avisos_depois(self, ultimo: int) -> list[Any]:
        async with self.dono.connect() as c:
            return list((await c.execute(text("SELECT * FROM avisos WHERE id > :u ORDER BY id"), {"u": ultimo})).all())

    async def sistema(self, sistema_id: uuid.UUID) -> Any:
        async with self.dono.connect() as c:
            return (await c.execute(text("SELECT * FROM sistemas WHERE id = :s"), {"s": sistema_id})).one()

    async def revisada(self, sistema_id: uuid.UUID, nome_original: str) -> Any:
        async with self.dono.connect() as c:
            return (
                await c.execute(
                    text(
                        "SELECT * FROM ferramentas_revisadas WHERE sistema_id = :s AND nome_original = :n"
                        " AND situacao = 'ativa'"
                    ),
                    {"s": sistema_id, "n": nome_original},
                )
            ).one()


@dataclass
class SoEstes:
    """``FonteDeclaracoes`` que so le os sistemas dados; os outros do banco da sessao ficam como nao conferidos."""

    fonte: FonteDeclaracoes
    ids: set[uuid.UUID]

    async def declaracoes(self, sistema_id: uuid.UUID) -> list[dict[str, Any]]:
        if sistema_id not in self.ids:
            raise ConferenciaNaoFeita("fora deste teste")
        return list(await self.fonte.declaracoes(sistema_id))


@dataclass
class Admin:
    """O ADMIN nas telas, com a sessao e o CSRF dele."""

    gw: Gateway
    nav: Navegador
    csrf: str
    pessoa: Pessoa

    async def post(self, caminho: str, dados: Mapping[str, Any] | Sequence[tuple[str, str]]) -> httpx2.Response:
        corpo: dict[str, Any] | list[tuple[str, str]] = dict(dados) if isinstance(dados, Mapping) else list(dados)
        return await self.nav.post(caminho, corpo, csrf=self.csrf)

    async def cadastrar(
        self, nome: str, endereco: str, *, origem: str, tipo_chave: str, chave_sondagem: str = ""
    ) -> uuid.UUID:
        """Cadastro com a sondagem logo depois (a tela pede ao despachante). Devolve o id do sistema."""
        r = await self.post(
            "/telas/admin/sistemas",
            {
                "nome": nome,
                "endereco": endereco,
                "origem": origem,
                "tipo_chave": tipo_chave,
                "chave_sondagem": chave_sondagem,
            },
        )
        assert r.status_code == 303, r.text
        return uuid.UUID(r.headers["location"].rsplit("/", 1)[-1].split("?", 1)[0])

    async def ler(self, sistema_id: uuid.UUID, chave: str = "") -> None:
        """ "Ler as ferramentas agora": o despachante lista o servidor (pela sala, num terceiro)."""
        r = await self.post(f"/telas/admin/sistemas/{sistema_id}/declaracoes", {"chave_sondagem": chave})
        assert r.status_code == 303, r.text

    async def revisar(
        self,
        sistema_id: uuid.UUID,
        nome: str,
        *,
        classe: str = "le",
        risco: str = "medio",
        campos: Iterable[str] = (),
        destinatario: Iterable[str] = (),
        nome_vitrine: str | None = None,
        texto_de_fora: bool = False,
    ) -> str:
        """Revisa uma ferramenta com a descricao do ADMIN. Devolve o nome na vitrine."""
        vitrine = nome_vitrine or f"{nome}_{uuid.uuid4().hex[:6]}"
        pares = [
            ("nome", nome),
            ("nome_vitrine", vitrine),
            ("descricao", f"Descricao escrita pelo ADMIN para {nome}."),
            ("classe", classe),
            ("risco", risco),
            *(("campos", c) for c in campos),
            *(("destinatario", d) for d in destinatario),
            *((("texto_de_fora", "sim"),) if texto_de_fora else ()),
        ]
        r = await self.post(f"/telas/admin/sistemas/{sistema_id}/ferramenta", pares)
        assert r.status_code == 303, r.text
        return vitrine

    async def guardar_chave(
        self, sistema_id: uuid.UUID, tipo: str, segredo: str, *, aplicativo: uuid.UUID | None = None
    ) -> None:
        """Pela tela de acesso: compartilhada, de programa ou de leitura das declaracoes, direto ao cofre."""
        dados = {"tipo": tipo, "segredo": segredo} | ({"aplicativo": str(aplicativo)} if aplicativo else {})
        r = await self.post(f"/telas/admin/sistemas/{sistema_id}/chave", dados)
        assert r.status_code == 303, r.text

    async def concluir(
        self, sistema_id: uuid.UUID, *publicar: str, fornecedor: str = "Fornecedor de Fora Ltda."
    ) -> httpx2.Response:
        pares = [("fornecedor", fornecedor), *(("publicar", n) for n in publicar)]
        return await self.post(f"/telas/admin/sistemas/{sistema_id}/revisao_servidor", pares)

    async def programa(self, client_id: str, nome: str, *, responsavel: Pessoa, substituto: Pessoa) -> Aplicativo:
        """Um programa que roda sozinho, cadastrado na tela de programas: o aplicativo do tipo programa e o
        cadastro com responsavel, substituto, finalidade e data de fim."""
        fim = datetime.now(ZoneInfo("America/Sao_Paulo")).date() + timedelta(days=30)
        r = await self.post(
            "/telas/admin/programas",
            {
                "client_id": client_id,
                "nome": nome,
                "familia": "programa",
                "finalidade": "Conta os chamados abertos.",
                "fim": fim.isoformat(),
                "responsavel": str(responsavel.id),
                "substituto": str(substituto.id),
            },
        )
        assert (r.status_code, r.headers.get("location")) == (303, "/telas/admin/programas?aviso=programa_cadastrado")
        return await self.gw.aplicativo_habilitado(client_id)

    async def ordem_do_programa(self, programa: Aplicativo, modelo_id: uuid.UUID, apelido: str) -> Pessoa:
        """A ordem de servico do programa, dada pelo ADMIN na tela do programa. A conta de servico (a pessoa das
        ordens dele) nasce ali, do sujeito dado, sem nome. Devolve a conta."""
        sujeito = f"g9-{apelido}-{self.gw.sufixo}"
        base = f"/telas/admin/programas/{programa.id}"
        r = await self.post(f"{base}/ordem", {"modelo": str(modelo_id), "dias": "7", "sujeito": sujeito})
        assert (r.status_code, r.headers.get("location")) == (303, f"{base}?feito=programa_ordem"), r.text
        async with self.gw.dono.connect() as c:
            conta = (
                await c.execute(
                    text("SELECT id, nome FROM pessoas WHERE emissor = :e AND sujeito = :s"),
                    {"e": self.gw.idp.emissor, "s": sujeito},
                )
            ).one()
        assert conta.nome is None
        pessoa = Pessoa(apelido, sujeito, uuid.UUID(str(conta.id)))
        self.gw.pessoas[apelido] = pessoa
        return pessoa

    async def modelo(self, ferramentas: Iterable[uuid.UUID], *, nome: str | None = None) -> uuid.UUID:
        nome = nome or f"g9-modelo-{uuid.uuid4().hex[:8]}"
        pares = [
            ("nome", nome),
            ("descricao", "Ordem de servico das provas de saida."),
            ("validade_dias", "30"),
            *(("ferramentas", str(f)) for f in ferramentas),
        ]
        r = await self.post("/telas/admin/modelos", pares)
        assert r.status_code == 303, r.text
        async with self.gw.dono.connect() as c:
            return uuid.UUID(
                str((await c.execute(text("SELECT id FROM modelos_ordem WHERE nome = :n"), {"n": nome})).scalar_one())
            )

    async def terceiro(
        self,
        servido: Servido,
        revisoes: Mapping[str, Mapping[str, Any]],
        *,
        tipo_chave: str = "por_pessoa",
        chave_declaracoes: str,
        publicar: Iterable[str] | None = None,
        nome: str | None = None,
    ) -> uuid.UUID:
        """O percurso do ADMIN num servidor de terceiros, todo pelas telas: cadastro (com a sondagem pela sala),
        leitura das ferramentas, revisao de cada uma, chave de leitura das declaracoes e conclusao da revisao do
        servidor com as publicadas (padrao: todas as revisadas)."""
        nome = nome or f"g9-terceiro-{uuid.uuid4().hex[:8]}"
        sistema_id = await self.cadastrar(
            nome, servido.endereco, origem="terceiro", tipo_chave=tipo_chave, chave_sondagem=chave_declaracoes
        )
        linha = await self.gw.sistema(sistema_id)
        assert (linha.situacao, linha.sondagem_desfecho) == ("ativo", "aceito"), linha
        await self.ler(sistema_id, chave_declaracoes)
        for original, opcoes in revisoes.items():
            await self.revisar(sistema_id, original, **dict(opcoes))
        await self.guardar_chave(sistema_id, "declaracoes", chave_declaracoes)
        r = await self.concluir(sistema_id, *(revisoes if publicar is None else publicar))
        assert r.status_code == 303, r.text
        return sistema_id

    async def proprio(
        self,
        servido: Servido,
        revisoes: Mapping[str, Mapping[str, Any]],
        *,
        tipo_chave: str,
        chave_sondagem: str,
        nome: str | None = None,
    ) -> uuid.UUID:
        """Um sistema proprio pelas telas: cadastro com a sondagem, leitura e revisao das ferramentas."""
        nome = nome or f"g9-proprio-{uuid.uuid4().hex[:8]}"
        sistema_id = await self.cadastrar(
            nome, servido.endereco, origem="proprio", tipo_chave=tipo_chave, chave_sondagem=chave_sondagem
        )
        assert (await self.gw.sistema(sistema_id)).situacao == "ativo"
        await self.ler(sistema_id, chave_sondagem)
        for original, opcoes in revisoes.items():
            await self.revisar(sistema_id, original, **dict(opcoes))
        return sistema_id


def _parametros(
    banco: Parametros,
    idp: IdpOidcFalso,
    porta_idp: int,
    porta_desp: int,
    sala: str,
    certs: Any,
    extra: Mapping[str, Any],
) -> Parametros:
    valores: dict[str, Any] = banco.model_dump() | {
        "APP_ENV": "teste",
        "PUBLIC_BASE_URL": PUBLICO,
        "MCP_RESOURCE_ID": None,
        "WATCH_PUBLIC_URL": None,
        "ALLOWED_HOSTS": ["127.0.0.1:*"],
        "IDP_KIND": "oidc",
        "IDP_ISSUER_URL": idp.emissor,
        "IDP_JWKS_URL": f"http://127.0.0.1:{porta_idp}/jwks",
        "DISPATCHER_INTERNAL_URL": f"http://127.0.0.1:{porta_desp}",
        "VISITS_INTERNAL_URL": sala,
        "FEATURE_PHASE1_READ": True,
        "FEATURE_PHASE2_ACT": True,
        "FEATURE_PHASE3_THIRD_PARTY": True,
        "RESPONSE_MAX_WAIT_SECONDS": 30,
        "OUTBOX_SWEEP_SECONDS": 1,
        "VISITS_CA_FILE": str(certs.ca_visitas),
        "VISITS_CLIENT_CERT_FILE": str(certs.despachante_cert),
        "VISITS_CLIENT_KEY_FILE": str(certs.despachante_key),
    }
    return Parametros(**(valores | dict(extra)))


@dataclass
class _ContaSala:
    """Guarda a rota de cada pedido que chega a sala, antes de passar a ela."""

    app: Any
    rotas: list[str]

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            self.rotas.append(str(scope.get("path")))
        await self.app(scope, receive, send)


@contextlib.asynccontextmanager
async def _sala_de_verdade(certs: Any, porta: int, rotas: list[str]) -> AsyncIterator[str]:
    """A sala de visitas de verdade, um processo filho por chamada e mTLS, em desenvolvimento (aceita o endereco
    local dos terceiros de teste). Devolve a URL que o despachante usa (o nome do certificado e ``localhost``)."""
    url = f"https://localhost:{porta}"
    p = Parametros(
        APP_ENV="desenvolvimento",
        VISITS_INTERNAL_PORT=porta,
        VISITS_INTERNAL_URL=url,
        VISITS_CA_FILE=str(certs.ca_visitas),
        VISITS_CLIENT_CERT_FILE=str(certs.despachante_cert),
        VISITS_CLIENT_KEY_FILE=str(certs.despachante_key),
    )
    tls = partida.opcoes_tls(
        Parametros(
            INTERNAL_CA_FILE=str(certs.ca_visitas),
            INTERNAL_CERT_FILE=str(certs.visitas_cert),
            INTERNAL_KEY_FILE=str(certs.visitas_key),
            INTERNAL_CLIENT_CA_FILE=str(certs.ca_clientes_visitas),
        )
    )
    sala = visitas_app.Sala(p)
    async with servidor(_ContaSala(visitas_app.criar_app_visitas(p, sala=sala), rotas), porta, **tls):
        yield url


@contextlib.asynccontextmanager
async def subir_gateway(
    banco: Parametros, pasta: Path, *, extra: Mapping[str, Any] | None = None
) -> AsyncIterator[Gateway]:
    """O gateway inteiro (veja o comeco do modulo). ``pasta``: onde gravar os certificados da sala."""
    sufixo = uuid.uuid4().hex[:8]
    porta_idp, porta_desp, porta_sala = porta_livre(), porta_livre(), porta_livre()
    idp = IdpOidcFalso(emissor=f"http://127.0.0.1:{porta_idp}/realms/mcp-claro", segredo=None)
    certs = apoio_visitas.gerar_certificados(pasta)
    url_sala = f"https://localhost:{porta_sala}"
    p = _parametros(banco, idp, porta_idp, porta_desp, url_sala, certs, extra or {})
    dono = criar_engine(p, pool_size=3, max_overflow=3)
    entrada_db = g.engine_do_papel(p, "mcp_claro_entrada")
    desp_db = g.engine_do_papel(p, "mcp_claro_despachante")
    rotinas_db = g.engine_do_papel(p, "mcp_claro_rotinas")
    grafana_db = g.engine_do_papel(p, "grafana_leitor", tamanho=1)
    cifra = CifraFalsa()
    with pytest.MonkeyPatch.context() as mp:
        # A partida recusa a chave da Fase 3 ligada (D6): as provas a ligam pelo construtor, sem conferir.
        mp.setattr(Parametros, "conferir", lambda self: self)
        app_desp = criar_app_despachante(p, desp_db, cifra)
        app_entrada = criar_app_entrada(p, entrada_db)
    http = httpx2.AsyncClient(follow_redirects=False, timeout=60, trust_env=False)
    pela_sala: list[str] = []
    try:
        async with contextlib.AsyncExitStack() as pilha:
            await pilha.enter_async_context(servidor(idp.app(), porta_idp))
            await pilha.enter_async_context(_sala_de_verdade(certs, porta_sala, pela_sala))
            url_desp = await pilha.enter_async_context(servidor(app_desp, porta_desp))
            url_entrada = await pilha.enter_async_context(servidor(app_entrada))
            await g.abrir_a_saida(dono)
            yield Gateway(
                p=p,
                idp=idp,
                dono=dono,
                entrada_db=entrada_db,
                desp_db=desp_db,
                rotinas_db=rotinas_db,
                grafana_db=grafana_db,
                cifra=cifra,
                entrada=url_entrada,
                despachante=url_desp,
                sala=url_sala,
                pela_sala=pela_sala,
                http=http,
                pilha=pilha,
                sufixo=sufixo,
            )
    finally:
        await http.aclose()
        for engine in (dono, entrada_db, desp_db, rotinas_db, grafana_db):
            await engine.dispose()


# --- Ajudas das conferencias -----------------------------------------------------------------------------------


def textos(resultado: Any) -> list[str]:
    return [b.text for b in resultado.content if getattr(b, "type", None) == "text"]


def marca_de(resultado: Any) -> str:
    marca = str((resultado.meta or {})[PREFIXO_META + "marca"])
    assert re.fullmatch(r"[0-9a-f]{12}", marca), marca
    return marca


def dentro_e_fora(resultado: Any) -> tuple[list[str], list[str]]:
    """Os blocos de texto dentro da moldura de estranhos (com a marca desta resposta) e os de fora."""
    marca = marca_de(resultado)
    dentro, fora = [], []
    for t in textos(resultado):
        if t.startswith(f"[[estranhos {marca}]]\n") and t.endswith(f"\n[[/estranhos {marca}]]"):
            dentro.append(t)
        else:
            fora.append(t)
    return dentro, fora


def protocolo(resultado: Any) -> str:
    return str((resultado.meta or {})[PREFIXO_META + "protocolo"])
