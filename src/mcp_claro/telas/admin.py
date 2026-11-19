"""Telas do ADMIN: aplicativos, sistemas e sondagem, revisao de ferramentas, modelos de ordem, destinos de
webhook e abertura do extrato de outra pessoa. Os programas que rodam sozinhos estao em ``telas/admin_programas.py``.

Na revisao da ferramenta, o ADMIN marca os campos do pedido que dizem para quem a acao vai (so campos do esquema
de entrada). O limite "so estes destinatarios" da pessoa confere estes campos; o resto do pedido fica livre.

Toda mudanca vai ao extrato, na mesma transacao, com quem fez. A sondagem de versao e a leitura
das declaracoes sao pedidas ao despachante pela porta interna: so ele fala com os sistemas.
"""

from __future__ import annotations

import json
import re
import uuid
from typing import Any, Final
from urllib.parse import urlencode, urlsplit

import httpx2
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import BaseRoute, Route

from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Ambiente
from mcp_claro.entrada.argumentos import EsquemaInvalido
from mcp_claro.logs import logger
from mcp_claro.telas import consultas, limites
from mcp_claro.telas.nucleo import Interrompe, Telas, numero_pagina, tela, uuid_ou_none, ver_depois
from mcp_claro.telas.seguranca import Formulario
from mcp_claro.telas.sessoes import Sessao

log = logger("mcp_claro.telas.admin")

ID_SIMPLES: Final = re.compile(r"^[A-Za-z0-9._:-]{1,200}$")
NOME_VITRINE: Final = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
NOME_CAMPO: Final = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
NOME_EVENTO: Final = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
FAMILIA: Final = re.compile(r"^[a-z0-9][a-z0-9._-]{0,59}$")
CONTROLE: Final = re.compile(r"[\x00-\x1f\x7f]")
CLASSES: Final = ("le", "muda", "tem_volta")
RISCOS: Final = ("baixo", "medio", "alto")
TIPOS_CHAVE: Final = ("por_pessoa", "compartilhada", "programa")
# Os motivos aparecem para a pessoa (Meus limites, a pagina do limite e a da acao): texto de tela, com acento.
MOTIVO_CAMPOS_MUDARAM: Final = "o ADMIN marcou outro campo de destinatário nesta ação; crie outro limite se precisar"
MOTIVO_CANCELADA_CAMPOS: Final = (
    "cancelada porque o limite que a liberou terminou: o ADMIN mudou os campos de destinatário"
)


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


class TelasDoAdmin:
    def __init__(self, t: Telas) -> None:
        self.t = t

    def rotas(self) -> list[BaseRoute]:
        def r(caminho: str, funcao: Any, *metodos: str) -> Route:
            return Route(caminho, funcao, methods=list(metodos or ("GET",)))

        return [
            r("/admin", self.inicio),
            r("/admin/aplicativos", self.aplicativos),
            r("/admin/aplicativos", self.habilitar_aplicativo, "POST"),
            r("/admin/sistemas", self.sistemas),
            r("/admin/sistemas", self.cadastrar_sistema, "POST"),
            r("/admin/sistemas/{sistema_id:uuid}", self.sistema),
            r("/admin/sistemas/{sistema_id:uuid}/sondar", self.sondar, "POST"),
            r("/admin/sistemas/{sistema_id:uuid}/declaracoes", self.ler_declaracoes, "POST"),
            r("/admin/sistemas/{sistema_id:uuid}/ferramenta", self.ferramenta),
            r("/admin/sistemas/{sistema_id:uuid}/ferramenta", self.revisar, "POST"),
            r("/admin/modelos", self.modelos),
            r("/admin/modelos", self.criar_modelo, "POST"),
            r("/admin/webhooks", self.webhooks),
            r("/admin/webhooks", self.cadastrar_webhook, "POST"),
            r("/admin/webhooks/{destino_id:uuid}/situacao", self.mudar_webhook, "POST"),
            r("/admin/extrato", self.extrato_alheio_pedido),
            r("/admin/extrato", self.abrir_extrato_alheio, "POST"),
            r("/admin/extrato/{pessoa_id:uuid}", self.extrato_alheio),
        ]

    @tela
    async def inicio(self, pedido: Request) -> Response:
        await self.t.exigir_admin(pedido)
        return self.t.pagina(pedido, "admin/inicio.html")

    # --- Aplicativos ---

    async def _pagina_aplicativos(
        self, pedido: Request, erros: list[str] | None = None, valores: dict[str, str] | None = None
    ) -> Response:
        async with self.t.engine.connect() as c:
            aplicativos = await consultas.todos_aplicativos(c)
        return self.t.pagina(
            pedido,
            "admin/aplicativos.html",
            {"aplicativos": aplicativos, "erros": erros or [], "valores": valores or {}},
            status=400 if erros else 200,
        )

    @tela
    async def aplicativos(self, pedido: Request) -> Response:
        await self.t.exigir_admin(pedido)
        return await self._pagina_aplicativos(pedido)

    @tela
    async def habilitar_aplicativo(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        erros: list[str] = []
        client_id = f.texto("client_id")
        if not ID_SIMPLES.match(client_id):
            erros.append("O client_id usa só letras, números e . _ : - (até 200).")
        nome = _texto(f, "nome", "o nome", erros, maximo=120)
        familia = f.texto("familia").lower()
        if not FAMILIA.match(familia):
            erros.append("A família usa letras minúsculas, números e - (por exemplo: claude-code, goose).")
        tipo = f.texto("tipo", "assistente")
        if tipo == "programa":
            erros.append(
                "Programa que roda sozinho tem cadastro próprio, com responsável e substituto. Use a tela Programas."
            )
        elif tipo != "assistente":
            erros.append("Escolha o tipo do aplicativo.")
        if not erros:
            async with self.t.engine.begin() as c:
                if await consultas.aplicativo_existe(c, client_id):
                    erros.append("Já existe um aplicativo com este client_id.")
                else:
                    aplicativo_id = await repositorio.habilitar_aplicativo(
                        c, client_id=client_id, nome=nome, familia=familia, tipo="assistente"
                    )
                    await self.t.extrato(
                        c,
                        "admin_aplicativo_habilitado",
                        {"aplicativo": nome, "quem": sessao.quem},
                        pessoa_id=sessao.pessoa.id,
                        aplicativo_id=aplicativo_id,
                        dados={"client_id": client_id, "familia": familia},
                    )
                    return ver_depois("/telas/admin/aplicativos", "aplicativo_habilitado")
        return await self._pagina_aplicativos(pedido, erros, dict(f.como_dict()))

    # --- Sistemas e sondagem ---

    async def _pagina_sistemas(
        self, pedido: Request, erros: list[str] | None = None, valores: dict[str, str] | None = None
    ) -> Response:
        async with self.t.engine.connect() as c:
            sistemas = await consultas.todos_sistemas(c)
        return self.t.pagina(
            pedido,
            "admin/sistemas.html",
            {"sistemas": sistemas, "erros": erros or [], "valores": valores or {}},
            status=400 if erros else 200,
        )

    @tela
    async def sistemas(self, pedido: Request) -> Response:
        await self.t.exigir_admin(pedido)
        return await self._pagina_sistemas(pedido)

    def _endereco_aceito(self, endereco: str) -> bool:
        partes = urlsplit(endereco)
        esquemas = {"https"}
        if self.t.p.APP_ENV in (Ambiente.DESENVOLVIMENTO, Ambiente.TESTE):
            esquemas.add("http")
        return (
            partes.scheme in esquemas
            and bool(partes.hostname)
            and partes.username is None
            and partes.password is None
            and not partes.fragment
            and len(endereco) <= 500
            and not CONTROLE.search(endereco)
        )

    @tela
    async def cadastrar_sistema(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        erros: list[str] = []
        nome = _texto(f, "nome", "o nome do sistema", erros, maximo=80)
        endereco = f.texto("endereco")
        if not self._endereco_aceito(endereco):
            erros.append("O endereço precisa ser completo e começar com https:// (sem usuário e senha).")
        tipo_chave = f.texto("tipo_chave")
        if tipo_chave not in TIPOS_CHAVE:
            erros.append("Escolha o tipo de chave do sistema.")
        if erros:
            return await self._pagina_sistemas(pedido, erros, dict(f.como_dict(sem=("csrf", "chave_sondagem"))))
        try:
            async with self.t.engine.begin() as c:
                sistema_id = await repositorio.cadastrar_sistema(c, nome=nome, endereco=endereco, tipo_chave=tipo_chave)
                await self.t.extrato(
                    c,
                    "admin_sistema_cadastrado",
                    {"sistema": nome, "quem": sessao.quem},
                    pessoa_id=sessao.pessoa.id,
                    sistema_id=sistema_id,
                    dados={"tipo_chave": tipo_chave},
                )
        except IntegrityError:
            erros.append("Já existe um sistema com este nome.")
            return await self._pagina_sistemas(pedido, erros, dict(f.como_dict(sem=("csrf", "chave_sondagem"))))
        return await self._sondar(sessao, sistema_id, nome, f.texto("chave_sondagem"))

    async def _sondar(self, sessao: Sessao, sistema_id: uuid.UUID, nome: str, chave: str) -> Response:
        """Pede a sondagem ao despachante e volta para a ficha do sistema, que explica o resultado."""
        corpo: dict[str, Any] = {"sistema_id": str(sistema_id)}
        if chave:
            corpo["chave_sondagem"] = chave
        aviso: str | None = None
        try:
            resposta = await self.t.despachante.post("/interno/sondar", corpo)
        except (httpx2.HTTPError, ValueError) as erro:
            log.warning("telas_sondagem_sem_resposta", erro=type(erro).__name__)
            desfecho, aviso = "o despachante nao respondeu", "despachante_mudo"
        else:
            if resposta.get("aceito"):
                desfecho = "aceito, fala a 2026-07-28"
            elif resposta.get("inalcancavel"):
                desfecho = "sistema inalcancavel"
            else:
                desfecho = "recusado, nao fala a 2026-07-28"
        async with self.t.engine.begin() as c:
            await self.t.extrato(
                c,
                "admin_sondagem_pedida",
                {"sistema": nome, "quem": sessao.quem, "desfecho": desfecho},
                pessoa_id=sessao.pessoa.id,
                sistema_id=sistema_id,
            )
        return ver_depois(f"/telas/admin/sistemas/{sistema_id}", aviso)

    async def _sistema_ou_404(self, pedido: Request) -> Any:
        async with self.t.engine.connect() as c:
            linha = await consultas.sistema(c, pedido.path_params["sistema_id"])
        if linha is None:
            raise Interrompe(self.t.erro(pedido, 404, "Sistema não encontrado", "Este sistema não está cadastrado."))
        return linha

    @tela
    async def sistema(self, pedido: Request) -> Response:
        await self.t.exigir_admin(pedido)
        linha = await self._sistema_ou_404(pedido)
        async with self.t.engine.connect() as c:
            vistas = await consultas.ferramentas_vistas(c, linha.id)
        return self.t.pagina(pedido, "admin/sistema.html", {"sistema": linha, "vistas": vistas})

    @tela
    async def sondar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        linha = await self._sistema_ou_404(pedido)
        return await self._sondar(sessao, linha.id, linha.nome, f.texto("chave_sondagem"))

    @tela
    async def ler_declaracoes(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        linha = await self._sistema_ou_404(pedido)
        if linha.situacao not in ("ativo", "quarentena"):
            return self.t.erro(
                pedido,
                409,
                "Falta a sondagem",
                "Só lemos as ferramentas de um sistema aceito na sondagem de versão.",
            )
        corpo: dict[str, Any] = {"sistema_id": str(linha.id)}
        if chave := f.texto("chave_sondagem"):
            corpo["chave_sondagem"] = chave
        try:
            resposta = await self.t.despachante.post("/interno/declaracoes", corpo)
        except (httpx2.HTTPError, ValueError) as erro:
            log.warning("telas_declaracoes_sem_resposta", erro=type(erro).__name__)
            return self.t.erro(
                pedido,
                502,
                "Não conseguimos ler",
                "O despachante não conseguiu ler as ferramentas deste sistema. Confira a chave e tente de novo.",
            )
        quantas = len(resposta.get("ferramentas") or [])
        async with self.t.engine.begin() as c:
            await self.t.extrato(
                c,
                "admin_declaracoes_lidas",
                {"sistema": linha.nome, "quem": sessao.quem, "quantas": quantas},
                pessoa_id=sessao.pessoa.id,
                sistema_id=linha.id,
            )
        return ver_depois(f"/telas/admin/sistemas/{linha.id}", "declaracoes_lidas")

    # --- Revisao de cada ferramenta ---

    async def _pagina_ferramenta(
        self,
        pedido: Request,
        sistema: Any,
        nome: str,
        *,
        erros: list[str] | None = None,
        valores: dict[str, Any] | None = None,
    ) -> Response:
        async with self.t.engine.connect() as c:
            vista = await consultas.ferramenta_vista(c, sistema.id, nome)
            revisao = await consultas.revisao_ativa(c, sistema.id, nome)
        if vista is None:
            raise Interrompe(
                self.t.erro(
                    pedido,
                    404,
                    "Ferramenta não encontrada",
                    "O sistema não declarou esta ferramenta na última leitura.",
                )
            )
        definicao = dict(vista.definicao)
        saida = definicao.get("outputSchema") or {}
        campos_saida = sorted((saida.get("properties") or {}).keys()) if isinstance(saida, dict) else []
        if valores is None:
            if revisao is not None:
                valores = {
                    "nome_vitrine": revisao.nome_vitrine,
                    "titulo": revisao.titulo or "",
                    "descricao": revisao.descricao,
                    "classe": revisao.classe,
                    "risco": revisao.risco,
                    "campos": list(revisao.campos_pessoais or []),
                    "destinatario": list(revisao.campos_destinatario or []),
                }
            else:
                so_le = bool((definicao.get("annotations") or {}).get("readOnlyHint"))
                valores = {
                    "nome_vitrine": re.sub(r"[^A-Za-z0-9_.-]", "_", f"{sistema.nome}_{nome}")[:128],
                    "titulo": "",
                    "descricao": "",
                    "classe": "le" if so_le else "muda",
                    "risco": "medio",
                    "campos": [],
                    "destinatario": [],
                }
        outros = [c for c in valores.get("campos", []) if c not in campos_saida]
        return self.t.pagina(
            pedido,
            "admin/ferramenta.html",
            {
                "sistema": sistema,
                "nome": nome,
                "definicao": definicao,
                "esquema_entrada": json.dumps(definicao.get("inputSchema") or {}, indent=2, ensure_ascii=False),
                "campos_saida": campos_saida,
                "campos_entrada": repositorio.campos_de_entrada(definicao.get("inputSchema") or {}),
                "outros_campos": ", ".join(outros),
                "revisao": revisao,
                "mudou": revisao is not None and revisao.impressao != vista.impressao,
                "valores": valores,
                "erros": erros or [],
            },
            status=400 if erros else 200,
        )

    @tela
    async def ferramenta(self, pedido: Request) -> Response:
        await self.t.exigir_admin(pedido)
        sistema = await self._sistema_ou_404(pedido)
        return await self._pagina_ferramenta(pedido, sistema, pedido.query_params.get("nome", ""))

    @tela
    async def revisar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        sistema = await self._sistema_ou_404(pedido)
        nome = f.texto("nome")
        erros: list[str] = []
        nome_vitrine = f.texto("nome_vitrine")
        if not NOME_VITRINE.match(nome_vitrine):
            erros.append("O nome na vitrine usa só letras sem acento, números e _ . - (até 128).")
        titulo = _texto(f, "titulo", "o título", erros, minimo=0, maximo=120)
        descricao = _texto(f, "descricao", "a descrição própria", erros, minimo=10, maximo=1000)
        classe = f.texto("classe")
        if classe not in CLASSES:
            erros.append("Escolha a classe: só lê, muda algo ou muda algo com volta.")
        risco = f.texto("risco")
        if risco not in RISCOS:
            erros.append("Escolha o risco: baixo, médio ou alto.")
        campos = f.lista("campos") + [c.strip() for c in f.texto("outros_campos").split(",") if c.strip()]
        campos = list(dict.fromkeys(campos))
        if len(campos) > 30 or not all(NOME_CAMPO.match(c) for c in campos):
            erros.append("Os campos pessoais usam só letras sem acento, números e _ . - (até 30 campos).")
        async with self.t.engine.connect() as c:
            vista = await consultas.ferramenta_vista(c, sistema.id, nome)
        if vista is None:
            return await self._pagina_ferramenta(pedido, sistema, nome)
        destinatario = list(dict.fromkeys(f.lista("destinatario")))
        de_entrada = repositorio.campos_de_entrada(dict(vista.definicao).get("inputSchema") or {})
        if len(destinatario) > 30 or any(d not in de_entrada for d in destinatario):
            erros.append("Marque como destinatário só campos que a ferramenta pede (a lista desta página).")
            destinatario = [d for d in destinatario if d in de_entrada][:30]
        valores = {
            "nome_vitrine": nome_vitrine,
            "titulo": titulo,
            "descricao": descricao,
            "classe": classe,
            "risco": risco,
            "campos": campos,
            "destinatario": destinatario,
        }
        if erros:
            return await self._pagina_ferramenta(pedido, sistema, nome, erros=erros, valores=valores)
        try:
            async with self.t.engine.begin() as c:
                ferramenta_id = await repositorio.revisar_ferramenta(
                    c,
                    sistema_id=sistema.id,
                    definicao_declarada=dict(vista.definicao),
                    nome_vitrine=nome_vitrine,
                    descricao=descricao,
                    classe=classe,
                    revisado_por=sessao.quem,
                    risco=risco,
                    titulo=titulo or None,
                    campos_pessoais=campos,
                    campos_destinatario=destinatario,
                )
                await self.t.extrato(
                    c,
                    "admin_ferramenta_revisada",
                    {
                        "ferramenta": nome_vitrine,
                        "sistema": sistema.nome,
                        "quem": sessao.quem,
                        "classe": classe,
                        "risco": risco,
                    },
                    pessoa_id=sessao.pessoa.id,
                    sistema_id=sistema.id,
                    ferramenta=nome_vitrine,
                    dados={
                        "ferramenta_id": str(ferramenta_id),
                        "nome_original": nome,
                        "campos_pessoais": campos,
                        "campos_destinatario": destinatario,
                    },
                )
                await self._encerrar_limites_sem_os_campos(c, sistema.id, nome, nome_vitrine, destinatario)
        except EsquemaInvalido as erro:
            erros.append(f"O esquema de entrada desta ferramenta não é aceito: {erro}.")
        except repositorio.CampoForaDoEsquema:
            erros.append("Marque como destinatário só campos que a ferramenta pede (a lista desta página).")
        except IntegrityError:
            erros.append("Já existe outra ferramenta valendo com este nome na vitrine. Escolha outro nome.")
        if erros:
            return await self._pagina_ferramenta(pedido, sistema, nome, erros=erros, valores=valores)
        return ver_depois(f"/telas/admin/sistemas/{sistema.id}", "ferramenta_revisada")

    async def _encerrar_limites_sem_os_campos(
        self, c: AsyncConnection, sistema_id: uuid.UUID, nome: str, nome_vitrine: str, campos: list[str]
    ) -> int:
        """Na transacao da revisao: os limites "so estes" que nao conferem um campo marcado agora terminam, e o
        que eles liberaram e ainda espera a hora de sair e cancelado. Cada pessoa ve no proprio extrato."""
        encerrados = await consultas.encerrar_limites_sem_os_campos(
            c, sistema_id=sistema_id, nome_original=nome, campos=campos, motivo=MOTIVO_CAMPOS_MUDARAM
        )
        for limite in encerrados:
            canceladas = await limites.cancelar_liberadas(c, limite.id, MOTIVO_CANCELADA_CAMPOS, quem="admin")
            await self.t.extrato(
                c,
                "limite_encerrado",
                {"ferramenta": nome_vitrine, "motivo": MOTIVO_CAMPOS_MUDARAM},
                pessoa_id=limite.pessoa_id,
                aplicativo_id=limite.aplicativo_id,
                sistema_id=limite.sistema_id,
                ferramenta=nome_vitrine,
                dados={"limite_id": str(limite.id), "canceladas": canceladas, "por": "admin"},
            )
        if encerrados:
            log.info("telas_limites_encerrados_pela_revisao", limites=len(encerrados))
        return len(encerrados)

    # --- Modelos de ordem de servico ---

    async def _pagina_modelos(
        self, pedido: Request, erros: list[str] | None = None, valores: dict[str, Any] | None = None
    ) -> Response:
        async with self.t.engine.connect() as c:
            modelos = await consultas.todos_modelos(c)
            ferramentas = await consultas.ferramentas_revisadas_ativas(c)
            maximo = await consultas.valor_politica(c, "WORK_ORDER_MAX_DAYS")
        return self.t.pagina(
            pedido,
            "admin/modelos.html",
            {
                "modelos": modelos,
                "ferramentas": ferramentas,
                "maximo_dias": maximo,
                "so_leitura": not self.t.p.FEATURE_PHASE2_ACT,
                "erros": erros or [],
                "valores": valores or {"ferramentas": []},
            },
            status=400 if erros else 200,
        )

    @tela
    async def modelos(self, pedido: Request) -> Response:
        await self.t.exigir_admin(pedido)
        return await self._pagina_modelos(pedido)

    @tela
    async def criar_modelo(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        erros: list[str] = []
        nome = _texto(f, "nome", "o nome do modelo", erros, maximo=80)
        descricao = _texto(f, "descricao", "a explicação para a pessoa", erros, minimo=10, maximo=500)
        escolhidas = [i for i in (uuid_ou_none(v) for v in f.lista("ferramentas")) if i is not None]
        valores: dict[str, Any] = dict(f.como_dict()) | {"ferramentas": [str(i) for i in escolhidas]}
        async with self.t.engine.begin() as c:
            maximo = await consultas.valor_politica(c, "WORK_ORDER_MAX_DAYS")
            try:
                dias = int(f.texto("validade_dias"))
            except ValueError:
                dias = 0
            if not 1 <= dias <= maximo:
                erros.append(f"O prazo máximo do modelo vai de 1 a {maximo} dias (regra da política).")
            ativas = {linha.id: linha for linha in await consultas.ferramentas_revisadas_ativas(c)}
            ferramentas = [ativas[i] for i in escolhidas if i in ativas]
            if not ferramentas or len(ferramentas) != len(escolhidas):
                erros.append("Escolha ao menos uma ferramenta revisada da lista.")
            if not self.t.p.FEATURE_PHASE2_ACT and any(x.classe != "le" for x in ferramentas):
                erros.append("Nesta fase, a ordem de serviço só pode ter ferramentas que só leem.")
            if not erros and await consultas.modelo_existe(c, nome):
                erros.append("Já existe um modelo com este nome.")
            if not erros:
                modelo_id = await repositorio.criar_modelo_ordem(
                    c,
                    nome=nome,
                    descricao=descricao,
                    ferramentas=[(x.sistema_id, x.nome_original) for x in ferramentas],
                    validade_dias=dias,
                    criado_por=sessao.quem,
                )
                await self.t.extrato(
                    c,
                    "admin_modelo_criado",
                    {"modelo": nome, "quem": sessao.quem, "quantas": len(ferramentas)},
                    pessoa_id=sessao.pessoa.id,
                    dados={"modelo_id": str(modelo_id), "validade_dias": dias},
                )
                return ver_depois("/telas/admin/modelos", "modelo_criado")
        return await self._pagina_modelos(pedido, erros, valores)

    # --- Destinos de webhook ---

    async def _pagina_webhooks(
        self, pedido: Request, erros: list[str] | None = None, valores: dict[str, str] | None = None
    ) -> Response:
        async with self.t.engine.connect() as c:
            destinos = await consultas.destinos_webhook(c)
        return self.t.pagina(
            pedido,
            "admin/webhooks.html",
            {"destinos": destinos, "erros": erros or [], "valores": valores or {}},
            status=400 if erros else 200,
        )

    @tela
    async def webhooks(self, pedido: Request) -> Response:
        await self.t.exigir_admin(pedido)
        return await self._pagina_webhooks(pedido)

    @tela
    async def cadastrar_webhook(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        erros: list[str] = []
        nome = _texto(f, "nome", "o nome do destino", erros, maximo=80)
        url = f.texto("url")
        partes = urlsplit(url)
        if (
            partes.scheme != "https"
            or not partes.hostname
            or partes.username is not None
            or partes.password is not None
            or len(url) > 500
            or CONTROLE.search(url)
        ):
            erros.append("O endereço do webhook precisa começar com https:// (sem usuário e senha).")
        eventos = [e.strip().lower() for e in f.texto("eventos").split(",") if e.strip()]
        if len(eventos) > 20 or not all(NOME_EVENTO.match(e) for e in eventos):
            erros.append("Os eventos usam letras minúsculas, números e _ (separados por vírgula).")
        if not erros:
            try:
                async with self.t.engine.begin() as c:
                    destino_id = await consultas.cadastrar_destino(
                        c, nome=nome, url=url, eventos=eventos, criado_por=sessao.quem
                    )
                    await self.t.extrato(
                        c,
                        "admin_webhook_cadastrado",
                        {"destino": nome, "quem": sessao.quem},
                        pessoa_id=sessao.pessoa.id,
                        dados={"destino_id": str(destino_id), "eventos": eventos},
                    )
            except IntegrityError:
                erros.append("Já existe um destino com este nome.")
            else:
                return ver_depois("/telas/admin/webhooks", "destino_cadastrado")
        return await self._pagina_webhooks(pedido, erros, dict(f.como_dict()))

    @tela
    async def mudar_webhook(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        ativo = f.texto("ativo") == "sim"
        async with self.t.engine.begin() as c:
            nome = await consultas.mudar_destino(c, pedido.path_params["destino_id"], ativo)
            if nome is not None:
                await self.t.extrato(
                    c,
                    "admin_webhook_mudou",
                    {"destino": nome, "situacao": "ligado" if ativo else "desligado", "quem": sessao.quem},
                    pessoa_id=sessao.pessoa.id,
                    dados={"destino_id": str(pedido.path_params["destino_id"])},
                )
        return ver_depois("/telas/admin/webhooks", "destino_mudou" if nome else None)

    # --- Extrato de outra pessoa: so com motivo, e o acesso vira linha no extrato dela ---

    async def _pagina_pedido_extrato(
        self, pedido: Request, erros: list[str] | None = None, valores: dict[str, str] | None = None
    ) -> Response:
        async with self.t.engine.connect() as c:
            pessoas = await consultas.pessoas(c)
        valores = valores or {}
        if "pessoa" not in valores and pedido.query_params.get("pessoa"):
            valores["pessoa"] = pedido.query_params["pessoa"]
        return self.t.pagina(
            pedido,
            "admin/extrato_pedido.html",
            {"pessoas": pessoas, "erros": erros or [], "valores": valores},
            status=400 if erros else 200,
        )

    @tela
    async def extrato_alheio_pedido(self, pedido: Request) -> Response:
        await self.t.exigir_admin(pedido)
        return await self._pagina_pedido_extrato(pedido)

    @tela
    async def abrir_extrato_alheio(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        f = await self.t.formulario(pedido, sessao)
        erros: list[str] = []
        alvo_id = uuid_ou_none(f.texto("pessoa"))
        motivo = _texto(f, "motivo", "o motivo", erros, minimo=10, maximo=300).rstrip(" .")
        async with self.t.engine.begin() as c:
            alvo = await consultas.pessoa_por_id(c, alvo_id) if alvo_id else None
            if alvo is None:
                erros.append("Escolha a pessoa.")
            if not erros and alvo is not None:
                await self.t.extrato(
                    c,
                    "extrato_alheio_aberto",
                    {"quem": sessao.quem, "motivo": motivo},
                    pessoa_id=alvo.id,
                    dados={"admin_id": str(sessao.pessoa.id)},
                )
                log.info("telas_extrato_alheio_aberto")
                return RedirectResponse(f"/telas/admin/extrato/{alvo.id}", status_code=303)
        return await self._pagina_pedido_extrato(pedido, erros, dict(f.como_dict()))

    @tela
    async def extrato_alheio(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_admin(pedido)
        alvo_id: uuid.UUID = pedido.path_params["pessoa_id"]
        pagina = numero_pagina(pedido.query_params.get("pagina"))
        async with self.t.engine.connect() as c:
            alvo = await consultas.pessoa_por_id(c, alvo_id)
            liberado = alvo is not None and await consultas.acesso_alheio_recente(
                c, alvo_id, sessao.pessoa.id, self.t.cfg.janela_extrato_alheio_minutos
            )
            if alvo is None or not liberado:
                destino = "/telas/admin/extrato?" + urlencode({"pessoa": str(alvo_id), "aviso": "motivo_exigido"})
                return RedirectResponse(destino, status_code=303)
            linhas, ha_mais = await consultas.linhas_extrato(c, alvo_id, pagina)
        return self.t.pagina(
            pedido,
            "extrato.html",
            {
                "linhas": linhas,
                "pagina": pagina,
                "ha_mais": ha_mais,
                "alheio": alvo,
                "base": f"/telas/admin/extrato/{alvo_id}",
            },
        )
