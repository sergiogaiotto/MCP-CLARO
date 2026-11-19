"""Telas dos programas que rodam sozinhos (decisao 9). Na Fase 2, programas so leem.

ADMIN (``/telas/admin/programas``): cadastrar um programa (client_id do provedor de login, nome, finalidade,
responsavel e substituto obrigatorios e diferentes, data de fim), listar, ver um programa, pausar, retomar,
encerrar, recertificar, registrar a troca da chave, trocar o responsavel e o substituto, e dar a ordem de
servico do programa (so com ferramentas que leem, de sistemas de chave propria do programa).

Responsavel (``/telas/programas``): ve os programas de que e responsavel ou substituto e, na revisao
periodica (``PROGRAM_OWNER_RECERT_DAYS``), confirma que cada um segue valendo (grava ``revisado_em``).

Sessao, CSRF e area do ADMIN como em ``telas/admin.py``. Toda mudanca vai ao extrato, na mesma transacao,
com quem fez. Retomar um programa pausado conta como revisao do ADMIN.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any, Final
from zoneinfo import ZoneInfo

from sqlalchemy import Row, text
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.logs import logger
from mcp_claro.regras import programas as regras
from mcp_claro.regras.programas import Programa
from mcp_claro.telas import consultas
from mcp_claro.telas.admin import CONTROLE, FAMILIA, ID_SIMPLES
from mcp_claro.telas.nucleo import Telas, tela, uuid_ou_none, ver_depois
from mcp_claro.telas.paginas import AVISOS
from mcp_claro.telas.seguranca import Formulario
from mcp_claro.telas.sessoes import Sessao

log = logger("mcp_claro.telas.admin_programas")

SUJEITO: Final = re.compile(r"^[A-Za-z0-9._:@-]{1,200}$")
FIM_MAXIMO_DIAS: Final = 366
BASE_ADMIN: Final = "/telas/admin/programas"
BASE_PESSOA: Final = "/telas/programas"

SEM_RESPONSAVEL: Final = "Sem responsável e substituto com nome, o programa não é salvo."
MESMA_PESSOA: Final = "O responsável e o substituto precisam ser pessoas diferentes."
NAO_SERVEM: Final = "Responsável e substituto precisam ser pessoas ativas, com nome no cadastro."

# Avisos depois de um passo que deu certo, nestas telas. A URL leva so o codigo.
FEITOS: Final[dict[str, str]] = {
    "programa_pausado": "Pronto. O programa foi pausado e não lê mais nada até ser retomado.",
    "programa_retomado": "Pronto. O programa foi retomado. A próxima revisão conta a partir de hoje.",
    "programa_encerrado": "Pronto. O programa foi encerrado. Nada mais passa por ele.",
    "programa_recertificado": (
        "Pronto. Ficou registrado que o programa segue valendo. A próxima revisão conta a partir de hoje."
    ),
    "programa_chave_trocada": "Pronto. A troca da chave foi registrada. O prazo da chave conta a partir de hoje.",
    "programa_responsaveis": "Pronto. O responsável e o substituto foram trocados.",
    "programa_ordem": "Pronto. A ordem de serviço foi dada ao programa. Ela só tem ferramentas que leem.",
    "programa_nada_mudou": "Nada mudou: o programa não estava na situação certa para isto.",
}


def hoje_em_brasilia() -> date:
    return datetime.now(ZoneInfo(regras.FUSO)).date()


def _limpo(valor: str) -> str:
    return " ".join(CONTROLE.sub(" ", valor).split())


def _texto(f: Formulario, nome: str, rotulo: str, erros: list[str], *, minimo: int = 1, maximo: int = 200) -> str:
    valor = _limpo(f.texto(nome))
    if not minimo <= len(valor) <= maximo:
        if minimo <= 1:
            erros.append(f"Preencha {rotulo} (até {maximo} letras).")
        else:
            erros.append(f"Preencha {rotulo} com {minimo} a {maximo} letras.")
    return valor


def conferir_responsaveis(responsavel_id: uuid.UUID | None, substituto_id: uuid.UUID | None) -> list[str]:
    """Regra fixa do plano (decisao 9): responsavel e substituto com nome, obrigatorios e diferentes."""
    if responsavel_id is None or substituto_id is None:
        return [SEM_RESPONSAVEL]
    if responsavel_id == substituto_id:
        return [MESMA_PESSOA]
    return []


def conferir_fim(valor: str, hoje: date) -> tuple[date | None, list[str]]:
    """Data de fim: depois de hoje (Brasilia) e em ate um ano."""
    try:
        fim = date.fromisoformat(valor)
    except ValueError:
        fim = None
    if fim is None or not hoje < fim <= hoje + timedelta(days=FIM_MAXIMO_DIAS):
        return None, ["A data de fim precisa ser depois de hoje e em até um ano."]
    return fim, []


@dataclass(frozen=True, slots=True)
class Prazos:
    """Prazos de um programa, para as telas. Datas em UTC; a tela mostra no fuso das telas."""

    revisao_vence: datetime
    chave_vence: datetime
    pausa_sem_uso: datetime
    ultimo_pedido: datetime | None
    revisao_vencida: bool
    chave_vencida: bool

    @classmethod
    def de(cls, programa: Programa, p: Parametros, agora: datetime | None = None) -> Prazos:
        agora = agora or datetime.now(UTC)
        revisao = programa.revisado_em + timedelta(days=int(p.PROGRAM_OWNER_RECERT_DAYS))
        chave = programa.chave_trocada_em + timedelta(days=int(p.PROGRAM_KEY_MAX_AGE_DAYS))
        base = max(x for x in (programa.visto_em, programa.revisado_em, programa.criado_em) if x is not None)
        return cls(
            revisao_vence=revisao,
            chave_vence=chave,
            pausa_sem_uso=base + timedelta(days=int(p.PROGRAM_IDLE_PAUSE_DAYS)),
            ultimo_pedido=programa.visto_em,
            revisao_vencida=revisao <= agora,
            chave_vencida=chave <= agora,
        )


def recusa_para_retomar(programa: Programa, prazos: Prazos) -> list[str]:
    """O que impede o ADMIN de retomar um programa pausado. Vazio: pode retomar."""
    if programa.situacao != "pausado":
        return ["Só um programa pausado pode ser retomado."]
    erros: list[str] = []
    if programa.vencido:
        erros.append("A data de fim do programa já passou. Encerre e cadastre de novo, se ainda for preciso.")
    if not programa.responsavel_ativo or not programa.substituto_ativo:
        erros.append("O responsável ou o substituto não está mais ativo. Troque-os antes de retomar.")
    if prazos.chave_vencida:
        erros.append("A chave do programa passou do prazo. Troque a chave no provedor de login e registre a troca.")
    return erros


async def candidatos(c: AsyncConnection) -> Sequence[Row[Any]]:
    """Quem pode ser responsavel ou substituto: pessoa ativa, com nome, que nao e conta de servico de programa."""
    return (
        await c.execute(
            text(
                "SELECT p.id, p.nome FROM pessoas p WHERE p.situacao = 'ativa' AND coalesce(btrim(p.nome), '') <> ''"
                " AND NOT EXISTS (SELECT 1 FROM ordens_servico o JOIN aplicativos a ON a.id = o.aplicativo_id"
                " WHERE o.pessoa_id = p.id AND a.tipo = 'programa') ORDER BY p.nome, p.criado_em LIMIT 2000"
            )
        )
    ).all()


async def _ordens_do_programa(c: AsyncConnection, aplicativo_id: uuid.UUID) -> Sequence[Row[Any]]:
    return (
        await c.execute(
            text(
                "SELECT o.id, m.nome AS modelo, o.inicio, o.fim, o.situacao, o.ultimo_uso FROM ordens_servico o"
                " JOIN modelos_ordem m ON m.id = o.modelo_id WHERE o.aplicativo_id = :a"
                " ORDER BY o.criado_em DESC LIMIT 50"
            ),
            {"a": aplicativo_id},
        )
    ).all()


async def _conta_do_programa(c: AsyncConnection, aplicativo_id: uuid.UUID) -> Row[Any] | None:
    """A conta de servico do programa (a pessoa das ordens dele), se ja houver ordem."""
    return (
        await c.execute(
            text(
                "SELECT p.id, p.sujeito, p.situacao FROM ordens_servico o JOIN pessoas p ON p.id = o.pessoa_id"
                " WHERE o.aplicativo_id = :a ORDER BY o.criado_em DESC LIMIT 1"
            ),
            {"a": aplicativo_id},
        )
    ).first()


async def _modelos_que_servem(c: AsyncConnection) -> Sequence[Row[Any]]:
    """Modelos ativos cujas ferramentas todas leem e sao de sistema com chave propria de programa."""
    return (
        await c.execute(
            text(
                "SELECT m.id, m.nome, m.descricao, m.validade_dias, count(*) AS quantas FROM modelos_ordem m"
                " JOIN modelos_ordem_ferramentas mf ON mf.modelo_id = m.id JOIN sistemas s ON s.id = mf.sistema_id"
                " LEFT JOIN ferramentas_revisadas f ON f.sistema_id = mf.sistema_id"
                " AND f.nome_original = mf.nome_original AND f.situacao = 'ativa'"
                " WHERE m.ativo GROUP BY m.id"
                " HAVING bool_and(coalesce(s.tipo_chave = 'programa' AND f.classe = 'le', false)) ORDER BY m.nome"
            )
        )
    ).all()


class TelasDosProgramas:
    def __init__(self, t: Telas) -> None:
        self.t = t

    def rotas(self) -> list[BaseRoute]:
        def r(caminho: str, funcao: Any, *metodos: str) -> Route:
            return Route(caminho, funcao, methods=list(metodos or ("GET",)))

        um = "/admin/programas/{aplicativo_id:uuid}"
        return [
            r("/admin/programas", self.lista),
            r("/admin/programas", self.cadastrar, "POST"),
            r(um, self.detalhe),
            r(um + "/pausar", self.pausar, "POST"),
            r(um + "/retomar", self.retomar, "POST"),
            r(um + "/encerrar", self.encerrar, "POST"),
            r(um + "/recertificar", self.recertificar_admin, "POST"),
            r(um + "/chave", self.chave_trocada, "POST"),
            r(um + "/responsaveis", self.trocar_responsaveis, "POST"),
            r(um + "/ordem", self.dar_ordem, "POST"),
            r("/programas", self.meus),
            r("/programas/{aplicativo_id:uuid}/recertificar", self.recertificar_responsavel, "POST"),
        ]

    def _aviso(self, pedido: Request) -> str | None:
        codigo = pedido.query_params.get("aviso", "")
        return FEITOS.get(codigo) or AVISOS.get(codigo)

    # --- Lista e cadastro ---

    async def _pagina_lista(
        self, pedido: Request, erros: list[str] | None = None, valores: dict[str, str] | None = None
    ) -> Response:
        async with self.t.engine.connect() as c:
            lista = await regras.todos(c)
            pessoas = await candidatos(c)
        return self.t.pagina(
            pedido,
            "admin/programas_lista.html",
            {
                "programas": [(pr, Prazos.de(pr, self.t.p)) for pr in lista],
                "pessoas": pessoas,
                "hoje": hoje_em_brasilia().isoformat(),
                "aviso": self._aviso(pedido),
                "erros": erros or [],
                "valores": valores or {},
            },
            status=400 if erros else 200,
        )

    @tela
    async def lista(self, pedido: Request) -> Response:
        await self.t.exigir_admin(pedido)
        return await self._pagina_lista(pedido)

    async def _dupla(
        self, c: AsyncConnection, responsavel_id: uuid.UUID | None, substituto_id: uuid.UUID | None, erros: list[str]
    ) -> tuple[Row[Any], Row[Any]] | None:
        if responsavel_id is None or substituto_id is None or responsavel_id == substituto_id:
            return None
        aceitas = {linha.id: linha for linha in await candidatos(c)}
        responsavel, substituto = aceitas.get(responsavel_id), aceitas.get(substituto_id)
        if responsavel is None or substituto is None:
            erros.append(NAO_SERVEM)
            return None
        return responsavel, substituto

    @tela
    async def cadastrar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        erros: list[str] = []
        client_id = f.texto("client_id")
        if not ID_SIMPLES.match(client_id):
            erros.append("O client_id usa só letras, números e . _ : - (até 200).")
        nome = _texto(f, "nome", "o nome do programa", erros, maximo=120)
        familia = f.texto("familia", "programa").lower() or "programa"
        if not FAMILIA.match(familia):
            erros.append("A família usa letras minúsculas, números e -.")
        finalidade = _texto(f, "finalidade", "para que serve o programa", erros, minimo=10, maximo=500)
        fim, erros_fim = conferir_fim(f.texto("fim"), hoje_em_brasilia())
        erros += erros_fim
        responsavel_id = uuid_ou_none(f.texto("responsavel"))
        substituto_id = uuid_ou_none(f.texto("substituto"))
        erros += conferir_responsaveis(responsavel_id, substituto_id)
        async with self.t.engine.begin() as c:
            dupla = await self._dupla(c, responsavel_id, substituto_id, erros)
            existente = await repositorio.aplicativo_por_client_id(c, client_id) if not erros else None
            # Um aplicativo do tipo programa ainda sem cadastro (de antes desta tela) ganha o cadastro aqui.
            if existente is not None and (existente.tipo != "programa" or await regras.ler(c, existente.id)):
                erros.append("Já existe um aplicativo com este client_id.")
            if not erros and dupla is not None and fim is not None:
                responsavel, substituto = dupla
                aplicativo_id = await repositorio.habilitar_aplicativo(
                    c, client_id=client_id, nome=nome, familia=familia, tipo="programa"
                )
                await c.execute(
                    text(
                        "INSERT INTO programas (aplicativo_id, responsavel_id, substituto_id, finalidade, fim)"
                        " VALUES (:a, :r, :s, :f, :d)"
                    ),
                    {"a": aplicativo_id, "r": responsavel.id, "s": substituto.id, "f": finalidade, "d": fim},
                )
                await self.t.extrato(
                    c,
                    "admin_programa_cadastrado",
                    {
                        "aplicativo": nome,
                        "quem": sessao.quem,
                        "responsavel": responsavel.nome,
                        "substituto": substituto.nome,
                    },
                    pessoa_id=sessao.pessoa.id,
                    aplicativo_id=aplicativo_id,
                    dados={
                        "client_id": client_id,
                        "responsavel_id": str(responsavel.id),
                        "substituto_id": str(substituto.id),
                        "fim": fim.isoformat(),
                    },
                )
                return ver_depois(BASE_ADMIN, "programa_cadastrado")
        return await self._pagina_lista(pedido, erros, dict(f.como_dict()))

    # --- Um programa ---

    async def _pagina_detalhe(
        self,
        pedido: Request,
        aplicativo_id: uuid.UUID,
        erros: list[str] | None = None,
        valores: dict[str, str] | None = None,
    ) -> Response:
        async with self.t.engine.connect() as c:
            programa = await regras.ler(c, aplicativo_id)
            if programa is None:
                return self.t.erro(pedido, 404, "Programa não encontrado", "Este programa não está cadastrado.")
            aplicativo = (
                await c.execute(text("SELECT situacao FROM aplicativos WHERE id = :a"), {"a": aplicativo_id})
            ).scalar_one()
            ordens = await _ordens_do_programa(c, aplicativo_id)
            conta = await _conta_do_programa(c, aplicativo_id)
            modelos = await _modelos_que_servem(c)
            pessoas = await candidatos(c)
            maximo = await consultas.valor_politica(c, "WORK_ORDER_MAX_DAYS")
        return self.t.pagina(
            pedido,
            "admin/programas_detalhe.html",
            {
                "programa": programa,
                "prazos": Prazos.de(programa, self.t.p),
                "situacao_aplicativo": aplicativo,
                "ordens": ordens,
                "conta": conta,
                "modelos": modelos,
                "pessoas": pessoas,
                "maximo_dias": maximo,
                "acoes_ligadas": bool(self.t.p.PROGRAM_ACTIONS_ENABLED),
                "aviso": self._aviso(pedido),
                "erros": erros or [],
                "valores": valores or {},
            },
            status=400 if erros else 200,
        )

    @tela
    async def detalhe(self, pedido: Request) -> Response:
        await self.t.exigir_admin(pedido)
        return await self._pagina_detalhe(pedido, pedido.path_params["aplicativo_id"])

    async def _mudar(
        self, c: AsyncConnection, aplicativo_id: uuid.UUID, conjunto: str, de: tuple[str, ...]
    ) -> Programa | None:
        """Muda o programa so se ele estiver numa das situacoes ``de``. ``conjunto`` e um SET fixo deste modulo."""
        mudou = (
            await c.execute(
                text(
                    f"UPDATE programas SET {conjunto} WHERE aplicativo_id = :a"  # noqa: S608
                    " AND situacao = ANY(CAST(:de AS text[])) RETURNING aplicativo_id"
                ),
                {"a": aplicativo_id, "de": list(de)},
            )
        ).first()
        return await regras.ler(c, aplicativo_id) if mudou else None

    async def _passo_do_admin(
        self,
        pedido: Request,
        *,
        conjunto: str,
        de: tuple[str, ...],
        tipo: str,
        feito: str,
        pede_motivo: bool = False,
    ) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        aplicativo_id: uuid.UUID = pedido.path_params["aplicativo_id"]
        erros: list[str] = []
        campos: dict[str, object] = {"quem": sessao.quem}
        if pede_motivo:
            campos["motivo"] = _texto(f, "motivo", "o motivo", erros, minimo=5, maximo=300).rstrip(" .")
        if erros:
            return await self._pagina_detalhe(pedido, aplicativo_id, erros, dict(f.como_dict()))
        async with self.t.engine.begin() as c:
            programa = await self._mudar(c, aplicativo_id, conjunto, de)
            if programa is not None:
                await self.t.extrato(
                    c,
                    tipo,
                    programa.campos() | campos,
                    pessoa_id=sessao.pessoa.id,
                    aplicativo_id=aplicativo_id,
                    dados={"responsavel_id": str(programa.responsavel_id), "situacao": programa.situacao},
                )
        return ver_depois(f"{BASE_ADMIN}/{aplicativo_id}", feito if programa else "programa_nada_mudou")

    @tela
    async def pausar(self, pedido: Request) -> Response:
        return await self._passo_do_admin(
            pedido,
            conjunto="situacao = 'pausado'",
            de=("ativo",),
            tipo="admin_programa_pausado",
            feito="programa_pausado",
            pede_motivo=True,
        )

    @tela
    async def encerrar(self, pedido: Request) -> Response:
        return await self._passo_do_admin(
            pedido,
            conjunto="situacao = 'encerrado'",
            de=("ativo", "pausado"),
            tipo="admin_programa_encerrado",
            feito="programa_encerrado",
            pede_motivo=True,
        )

    @tela
    async def recertificar_admin(self, pedido: Request) -> Response:
        return await self._passo_do_admin(
            pedido,
            conjunto="revisado_em = now()",
            de=("ativo", "pausado"),
            tipo="programa_recertificado",
            feito="programa_recertificado",
        )

    @tela
    async def chave_trocada(self, pedido: Request) -> Response:
        return await self._passo_do_admin(
            pedido,
            conjunto="chave_trocada_em = now()",
            de=("ativo", "pausado"),
            tipo="admin_programa_chave_trocada",
            feito="programa_chave_trocada",
        )

    @tela
    async def retomar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        await self.t.formulario(pedido, sessao)
        aplicativo_id: uuid.UUID = pedido.path_params["aplicativo_id"]
        async with self.t.engine.begin() as c:
            atual = await regras.ler(c, aplicativo_id)
            erros = (
                ["Programa não encontrado."]
                if atual is None
                else recusa_para_retomar(atual, Prazos.de(atual, self.t.p))
            )
            programa = (
                None
                if erros
                else await self._mudar(c, aplicativo_id, "situacao = 'ativo', revisado_em = now()", ("pausado",))
            )
            if programa is not None:
                await self.t.extrato(
                    c,
                    "admin_programa_retomado",
                    programa.campos() | {"quem": sessao.quem},
                    pessoa_id=sessao.pessoa.id,
                    aplicativo_id=aplicativo_id,
                    dados={"responsavel_id": str(programa.responsavel_id), "revisado": True},
                )
        if erros:
            return await self._pagina_detalhe(pedido, aplicativo_id, erros)
        return ver_depois(f"{BASE_ADMIN}/{aplicativo_id}", "programa_retomado" if programa else "programa_nada_mudou")

    @tela
    async def trocar_responsaveis(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        aplicativo_id: uuid.UUID = pedido.path_params["aplicativo_id"]
        responsavel_id = uuid_ou_none(f.texto("responsavel"))
        substituto_id = uuid_ou_none(f.texto("substituto"))
        erros = conferir_responsaveis(responsavel_id, substituto_id)
        async with self.t.engine.begin() as c:
            dupla = await self._dupla(c, responsavel_id, substituto_id, erros)
            if not erros and dupla is not None:
                responsavel, substituto = dupla
                mudou = (
                    await c.execute(
                        text(
                            "UPDATE programas SET responsavel_id = :r, substituto_id = :s"
                            " WHERE aplicativo_id = :a AND situacao <> 'encerrado' RETURNING aplicativo_id"
                        ),
                        {"a": aplicativo_id, "r": responsavel.id, "s": substituto.id},
                    )
                ).first()
                programa = await regras.ler(c, aplicativo_id) if mudou else None
                if programa is not None:
                    await self.t.extrato(
                        c,
                        "admin_programa_responsaveis",
                        {
                            "programa": programa.nome,
                            "quem": sessao.quem,
                            "responsavel": responsavel.nome,
                            "substituto": substituto.nome,
                        },
                        pessoa_id=sessao.pessoa.id,
                        aplicativo_id=aplicativo_id,
                        dados={"responsavel_id": str(responsavel.id), "substituto_id": str(substituto.id)},
                    )
                return ver_depois(
                    f"{BASE_ADMIN}/{aplicativo_id}", "programa_responsaveis" if programa else "programa_nada_mudou"
                )
        return await self._pagina_detalhe(pedido, aplicativo_id, erros, dict(f.como_dict()))

    async def _conta_para_ordem(
        self, c: AsyncConnection, aplicativo_id: uuid.UUID, sujeito: str, erros: list[str]
    ) -> uuid.UUID | None:
        """A pessoa da conta de servico do programa. Na primeira ordem, nasce do sujeito dado pelo ADMIN."""
        conta = await _conta_do_programa(c, aplicativo_id)
        if conta is not None:
            return uuid.UUID(str(conta.id))
        if not SUJEITO.match(sujeito):
            erros.append(
                "Informe o identificador da conta de serviço do programa no provedor de login (o sub do crachá)."
            )
            return None
        emissor = self.t.p.IDP_ISSUER_URL
        linha = (
            await c.execute(
                text(
                    "SELECT p.id, p.papel, p.situacao,"
                    " EXISTS (SELECT 1 FROM sessoes_telas s WHERE s.pessoa_id = p.id) AS entrou_nas_telas,"
                    " EXISTS (SELECT 1 FROM ordens_servico o WHERE o.pessoa_id = p.id) AS tem_ordens,"
                    " EXISTS (SELECT 1 FROM programas pr WHERE p.id IN (pr.responsavel_id, pr.substituto_id))"
                    " AS responde FROM pessoas p WHERE p.emissor = :e AND p.sujeito = :s"
                ),
                {"e": emissor, "s": sujeito},
            )
        ).first()
        if linha is not None and (
            linha.papel == "admin" or linha.entrou_nas_telas or linha.tem_ordens or linha.responde
        ):
            erros.append(
                "Este identificador é de uma pessoa ou de outro aplicativo, não da conta de serviço do programa."
            )
            return None
        if linha is not None and linha.situacao != "ativa":
            erros.append("A conta de serviço deste programa está desativada no MCP-CLARO.")
            return None
        # Conta de servico nasce sem nome: assim nunca aparece na lista de responsaveis.
        return (await repositorio.pessoa_no_login(c, emissor, sujeito, None)).id

    @tela
    async def dar_ordem(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        aplicativo_id: uuid.UUID = pedido.path_params["aplicativo_id"]
        modelo_id = uuid_ou_none(f.texto("modelo"))
        erros: list[str] = []
        async with self.t.engine.begin() as c:
            programa = await regras.ler(c, aplicativo_id)
            if programa is None or programa.situacao == "encerrado":
                erros.append("Programa encerrado ou não encontrado: nada foi dado.")
            modelos = {m.id: m for m in await _modelos_que_servem(c)}
            modelo = modelos.get(modelo_id) if modelo_id else None
            if modelo is None:
                erros.append("Escolha um modelo da lista: só servem os que leem em sistemas com chave do programa.")
            maximo = await consultas.valor_politica(c, "WORK_ORDER_MAX_DAYS")
            limite = min(int(modelo.validade_dias), maximo) if modelo is not None else maximo
            try:
                dias = int(f.texto("dias"))
            except ValueError:
                dias = 0
            if not 1 <= dias <= limite:
                erros.append(f"O prazo vai de 1 a {limite} dias.")
            pessoa_id = None if erros else await self._conta_para_ordem(c, aplicativo_id, f.texto("sujeito"), erros)
            if not erros and programa is not None and modelo is not None and pessoa_id is not None:
                ordem_id = await repositorio.conceder_ordem(
                    c, pessoa_id=pessoa_id, aplicativo_id=aplicativo_id, modelo_id=modelo.id, dias=dias, max_dias=limite
                )
                fim = await consultas.fim_da_ordem(c, ordem_id)
                await self.t.extrato(
                    c,
                    "admin_programa_ordem",
                    programa.campos() | {"quem": sessao.quem, "modelo": modelo.nome, "fim": self.t.data(fim)},
                    pessoa_id=sessao.pessoa.id,
                    aplicativo_id=aplicativo_id,
                    dados={"ordem_id": str(ordem_id), "modelo_id": str(modelo.id), "dias": dias},
                )
                return ver_depois(f"{BASE_ADMIN}/{aplicativo_id}", "programa_ordem")
        return await self._pagina_detalhe(pedido, aplicativo_id, erros, dict(f.como_dict()))

    # --- Tela do responsavel ---

    @tela
    async def meus(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        return await self._pagina_meus(pedido, sessao)

    async def _pagina_meus(self, pedido: Request, sessao: Sessao, status: int = 200) -> Response:
        async with self.t.engine.connect() as c:
            todos = await regras.todos(c)
        meus = [pr for pr in todos if sessao.pessoa.id in (pr.responsavel_id, pr.substituto_id)]
        return self.t.pagina(
            pedido,
            "admin/programas_meus.html",
            {
                "programas": [(pr, Prazos.de(pr, self.t.p)) for pr in meus],
                "eu": sessao.pessoa.id,
                "recert_dias": int(self.t.p.PROGRAM_OWNER_RECERT_DAYS),
                "aviso": self._aviso(pedido),
            },
            status=status,
        )

    @tela
    async def recertificar_responsavel(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        await self.t.formulario(pedido, sessao)
        aplicativo_id: uuid.UUID = pedido.path_params["aplicativo_id"]
        async with self.t.engine.begin() as c:
            atual = await regras.ler(c, aplicativo_id)
            if atual is None or atual.responsavel_id != sessao.pessoa.id:
                log.info("telas_recertificacao_recusada")
                return self.t.erro(
                    pedido,
                    404,
                    "Programa não encontrado",
                    "Só o responsável pelo programa confirma que ele segue valendo. Nada mudou.",
                )
            programa = await self._mudar(c, aplicativo_id, "revisado_em = now()", ("ativo", "pausado"))
            if programa is not None:
                await self.t.extrato(
                    c,
                    "programa_recertificado",
                    programa.campos() | {"quem": sessao.quem},
                    pessoa_id=sessao.pessoa.id,
                    aplicativo_id=aplicativo_id,
                    dados={"situacao": programa.situacao},
                )
        return ver_depois(BASE_PESSOA, "programa_recertificado" if programa else "programa_nada_mudou")


def rotas_admin_programas(t: Telas) -> list[BaseRoute]:
    """Rotas destas telas, para ligar em ``telas/rotas.py`` (todas sob ``/telas``)."""
    return TelasDosProgramas(t).rotas()


__all__ = ["TelasDosProgramas", "rotas_admin_programas"]
