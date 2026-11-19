"""Telas da pessoa: limites por aproximacao (Fase 2).

Um limite deixa um aplicativo fazer uma acao sem ficha, ate um numero de vezes por dia e ate uma data, so
para os destinatarios escolhidos (ou para qualquer destinatario, dito com clareza).

- So entram acoes (classe diferente de "le") das ordens de servico que valem agora para a pessoa, em
  sistemas de chave por pessoa. Risco alto nunca entra: sempre passa pela ficha.
- Quantidade por dia ate ``ALLOWANCE_MAX_PER_DAY`` e prazo ate ``ALLOWANCE_MAX_DAYS`` (politica vigente).
- Criar e ampliar pedem a passkey quando ``FEATURE_PASSKEY`` esta ligado. O desafio fica preso ao resumo
  exato do que foi pedido (finalidade "limite"): a passkey aprova este limite, nao outro. Encerrar nao pede.
- Ampliar so aumenta: mais vezes por dia ou mais prazo. Para mudar os destinatarios, encerre e crie outro.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from mcp_claro.logs import logger
from mcp_claro.regras import politica
from mcp_claro.saida.protocolo import FUSO, canonico
from mcp_claro.telas import consultas, passkeys
from mcp_claro.telas.nucleo import Telas, tela, ver_depois
from mcp_claro.telas.paginas import AVISOS
from mcp_claro.telas.seguranca import Formulario
from mcp_claro.versao import VERSAO_MCP

log = logger("mcp_claro.telas.limites")

MAXIMO_VALORES: Final = 50
MAXIMO_VALOR: Final = 200
MOTIVO_ENCERRADO: Final = "encerrado pela pessoa nas telas"
SITUACOES: Final[dict[str, str]] = {
    "ativo": "Valendo",
    "suspenso": "Suspenso",
    "encerrado": "Encerrado",
    "vencido": "Venceu",
}

AVISOS_LIMITES: Final[dict[str, str]] = {
    "limite_criado": "Pronto. O limite foi criado e vale a partir de agora.",
    "limite_ampliado": "Pronto. O limite foi ampliado.",
    "limite_encerrado": "Pronto. O limite foi encerrado. A partir de agora, essa ação volta a pedir ficha.",
}


def _json(valor: Any) -> Any:
    return json.loads(valor) if isinstance(valor, str) else valor


def campos_de_destinatario(esquema: Any) -> tuple[str, ...]:
    """Campos do esquema de entrada que podem dizer o destinatario: texto, ou lista de textos."""
    propriedades = esquema.get("properties") if isinstance(esquema, dict) else None
    if not isinstance(propriedades, dict):
        return ()
    campos: list[str] = []
    for nome, definicao in propriedades.items():
        if not isinstance(definicao, dict):
            continue
        tipo = definicao.get("type")
        itens = definicao.get("items")
        if tipo == "string" or (tipo == "array" and isinstance(itens, dict) and itens.get("type") == "string"):
            campos.append(str(nome))
    return tuple(campos)


@dataclass(frozen=True, slots=True)
class Escolha:
    """Uma acao que pode entrar num limite: aplicativo e ferramenta de uma ordem que vale agora."""

    aplicativo_id: uuid.UUID
    aplicativo: str
    ferramenta_id: uuid.UUID
    sistema_id: uuid.UUID
    sistema: str
    nome_original: str
    nome_vitrine: str
    descricao: str
    risco: str
    campos: tuple[str, ...]

    @property
    def chave(self) -> str:
        return f"{self.aplicativo_id}:{self.ferramenta_id}"

    @classmethod
    def da_linha(cls, linha: Row[Any]) -> Escolha:
        return cls(
            aplicativo_id=linha.aplicativo_id,
            aplicativo=linha.aplicativo,
            ferramenta_id=linha.ferramenta_id,
            sistema_id=linha.sistema_id,
            sistema=linha.sistema,
            nome_original=linha.nome_original,
            nome_vitrine=linha.nome_vitrine,
            descricao=linha.descricao,
            risco=linha.risco,
            campos=campos_de_destinatario(_json(linha.esquema_entrada)),
        )


@dataclass(frozen=True, slots=True)
class PedidoLimite:
    escolha: Escolha
    destinatarios: list[dict[str, Any]]
    por_dia: int
    dias: int

    @property
    def destinatarios_json(self) -> str:
        return json.dumps(self.destinatarios, ensure_ascii=False)

    def alvo(self) -> str:
        """Resumo exato do pedido: o desafio da passkey so vale para este limite."""
        corpo = canonico(
            {
                "aplicativo": str(self.escolha.aplicativo_id),
                "ferramenta": str(self.escolha.ferramenta_id),
                "destinatarios": self.destinatarios,
                "por_dia": self.por_dia,
                "dias": self.dias,
            }
        )
        return "criar:" + hashlib.sha256(corpo.encode("utf-8")).hexdigest()


def alvo_ampliar(limite_id: uuid.UUID, por_dia: int, dias: int) -> str:
    corpo = canonico({"limite": str(limite_id), "por_dia": por_dia, "dias": dias})
    return f"ampliar:{limite_id}:" + hashlib.sha256(corpo.encode("utf-8")).hexdigest()


def _numero(texto: str) -> int | None:
    try:
        return int(texto)
    except ValueError:
        return None


def ler_valores(texto: str) -> list[str]:
    """Um valor por linha; linhas vazias e repetidas saem."""
    valores: list[str] = []
    for linha in texto.splitlines():
        valor = linha.strip()
        if valor and valor not in valores:
            valores.append(valor)
    return valores


def ler_pedido(
    f: Formulario, escolhas: dict[str, Escolha], *, maximo_dia: int, maximo_dias: int
) -> tuple[PedidoLimite | None, list[str]]:
    erros: list[str] = []
    escolha = escolhas.get(f.texto("escolha"))
    if escolha is None:
        return None, ["Escolha uma das ações da lista."]
    if escolha.risco == "alto":
        return None, ["Ações de risco alto nunca entram em limite: sempre passam pela ficha."]
    destinatarios: list[dict[str, Any]] = []
    tipo = f.texto("destinatarios")
    if tipo == "estes":
        campo = f.texto("campo")
        valores = ler_valores(f.texto("valores"))
        if campo not in escolha.campos:
            erros.append("Escolha o campo do pedido que diz o destinatário.")
        if not valores:
            erros.append("Escreva pelo menos um destinatário aceito, um por linha.")
        elif len(valores) > MAXIMO_VALORES or any(len(v) > MAXIMO_VALOR for v in valores):
            erros.append(f"Use até {MAXIMO_VALORES} destinatários, cada um com até {MAXIMO_VALOR} letras.")
        destinatarios = [{"campo": campo, "valores": valores}]
    elif tipo != "qualquer":
        erros.append("Diga para quais destinatários o limite vale.")
    por_dia = _numero(f.texto("por_dia"))
    if por_dia is None or not 1 <= por_dia <= maximo_dia:
        erros.append(f"A quantidade por dia vai de 1 a {maximo_dia}.")
    dias = _numero(f.texto("dias"))
    if dias is None or not 1 <= dias <= maximo_dias:
        erros.append(f"O prazo vai de 1 a {maximo_dias} dias.")
    if erros or por_dia is None or dias is None:
        return None, erros
    return PedidoLimite(escolha, destinatarios, por_dia, dias), []


def destinatarios_legiveis(destinatarios: Any) -> str:
    itens = _json(destinatarios) or []
    if not itens:
        return "Qualquer destinatário"
    partes = []
    for item in itens:
        if isinstance(item, dict):
            valores = ", ".join(str(v) for v in item.get("valores") or [])
            partes.append(f"{item.get('campo')}: {valores}")
    return "Só " + "; ".join(partes)


@dataclass(frozen=True, slots=True)
class Limite:
    """Um limite como a tela mostra."""

    linha: Row[Any]
    destinatarios: str

    @property
    def ativo(self) -> bool:
        return str(self.linha.situacao) == "ativo"

    @property
    def situacao(self) -> str:
        return SITUACOES.get(str(self.linha.situacao), str(self.linha.situacao))


def _limite(linha: Row[Any]) -> Limite:
    return Limite(linha, destinatarios_legiveis(linha.destinatarios))


class TelasDeLimites:
    def __init__(self, t: Telas) -> None:
        self.t = t
        self.cer = passkeys.Cerimonia.de_parametros(t.p)

    def rotas(self) -> list[BaseRoute]:
        return [
            Route("/limites", self.lista, methods=["GET"]),
            Route("/limites", self.criar, methods=["POST"]),
            Route("/limites/novo", self.novo, methods=["GET"]),
            Route("/limites/desafio", self.desafio_criar, methods=["POST"]),
            Route("/limites/{limite_id:uuid}", self.limite, methods=["GET"]),
            Route("/limites/{limite_id:uuid}/desafio", self.desafio_ampliar, methods=["POST"]),
            Route("/limites/{limite_id:uuid}/ampliar", self.ampliar, methods=["POST"]),
            Route("/limites/{limite_id:uuid}/encerrar", self.encerrar, methods=["POST"]),
        ]

    @property
    def ligada(self) -> bool:
        return bool(self.t.p.FEATURE_PASSKEY)

    def _aviso(self, pedido: Request) -> str | None:
        codigo = pedido.query_params.get("aviso", "")
        return AVISOS_LIMITES.get(codigo) or AVISOS.get(codigo)

    async def _escolhas(self, c: AsyncConnection, pessoa_id: uuid.UUID) -> list[Escolha]:
        linhas = await consultas.ferramentas_de_acao(c, pessoa_id, VERSAO_MCP)
        return sorted((Escolha.da_linha(linha) for linha in linhas), key=lambda e: (e.aplicativo, e.nome_vitrine))

    async def _maximos(self, c: AsyncConnection) -> tuple[int, int]:
        valores = await politica.varios(c, "ALLOWANCE_MAX_PER_DAY", "ALLOWANCE_MAX_DAYS")
        return valores["ALLOWANCE_MAX_PER_DAY"], valores["ALLOWANCE_MAX_DAYS"]

    # --- Lista ---

    @tela
    async def lista(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        async with self.t.engine.connect() as c:
            linhas = await consultas.limites_da_pessoa(c, sessao.pessoa.id, FUSO)
            escolhas = await self._escolhas(c, sessao.pessoa.id)
            maximo_dia, maximo_dias = await self._maximos(c)
        limites = [_limite(linha) for linha in linhas]
        return self.t.pagina(
            pedido,
            "acoes/limites.html",
            {
                "ativos": [x for x in limites if x.ativo],
                "terminados": [x for x in limites if not x.ativo][:20],
                "escolhas": [e for e in escolhas if e.risco != "alto"],
                "de_risco_alto": [e for e in escolhas if e.risco == "alto"],
                "maximo_dia": maximo_dia,
                "maximo_dias": maximo_dias,
                "ligada": self.ligada,
                "aviso": self._aviso(pedido),
            },
        )

    # --- Criar ---

    async def _pagina_novo(
        self,
        pedido: Request,
        chave: str,
        *,
        erros: list[str] | None = None,
        valores: dict[str, str] | None = None,
        status: int = 200,
    ) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        async with self.t.engine.connect() as c:
            escolhas = {e.chave: e for e in await self._escolhas(c, sessao.pessoa.id)}
            maximo_dia, maximo_dias = await self._maximos(c)
            tem_passkey = await passkeys.tem_ativa(c, sessao.pessoa.id)
        escolha = escolhas.get(chave)
        if escolha is None or escolha.risco == "alto":
            mensagem = (
                "Ações de risco alto nunca entram em limite: sempre passam pela ficha."
                if escolha is not None
                else "Esta ação não está nas suas ordens de serviço que valem agora. Volte para Meus limites."
            )
            return self.t.erro(pedido, 404 if escolha is None else 400, "Não dá para criar este limite", mensagem)
        return self.t.pagina(
            pedido,
            "acoes/limite_novo.html",
            {
                "e": escolha,
                "maximo_dia": maximo_dia,
                "maximo_dias": maximo_dias,
                "ligada": self.ligada,
                "tem_passkey": tem_passkey,
                "erros": erros or [],
                "valores": valores or {},
            },
            status=status,
        )

    @tela
    async def novo(self, pedido: Request) -> Response:
        return await self._pagina_novo(pedido, pedido.query_params.get("escolha", ""))

    @tela
    async def desafio_criar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        f = await self.t.formulario(pedido, sessao)
        if not self.ligada:
            return passkeys.resposta_desafio(None, "A passkey está desligada nesta instalação.", status=409)
        async with self.t.engine.begin() as c:
            escolhas = {e.chave: e for e in await self._escolhas(c, sessao.pessoa.id)}
            maximo_dia, maximo_dias = await self._maximos(c)
            lido, erros = ler_pedido(f, escolhas, maximo_dia=maximo_dia, maximo_dias=maximo_dias)
            if lido is None:
                return passkeys.resposta_desafio(None, " ".join(erros), status=400)
            try:
                opcoes = await passkeys.opcoes_autenticacao(c, self.cer, sessao.pessoa.id, "limite", lido.alvo())
            except passkeys.PasskeyRecusada as erro:
                return passkeys.resposta_desafio(None, erro.mensagem, status=409)
        return passkeys.resposta_desafio(opcoes)

    @tela
    async def criar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        f = await self.t.formulario(pedido, sessao)
        pessoa_id = sessao.pessoa.id
        erros: list[str] = []
        status = 400
        criado: uuid.UUID | None = None
        async with self.t.engine.begin() as c:
            escolhas = {e.chave: e for e in await self._escolhas(c, pessoa_id)}
            maximo_dia, maximo_dias = await self._maximos(c)
            lido, erros = ler_pedido(f, escolhas, maximo_dia=maximo_dia, maximo_dias=maximo_dias)
            if lido is not None and await consultas.limite_igual_ativo(
                c,
                pessoa_id=pessoa_id,
                aplicativo_id=lido.escolha.aplicativo_id,
                sistema_id=lido.escolha.sistema_id,
                nome_original=lido.escolha.nome_original,
                destinatarios=lido.destinatarios_json,
            ):
                erros = ["Você já tem um limite igual valendo para esta ação. Amplie o que existe."]
                status = 409
            if lido is not None and not erros and self.ligada:
                # A passkey e conferida aqui, na mesma transacao que cria. Recusada, o desafio fica gasto.
                negada = await passkeys.conferir(c, self.cer, pessoa_id, "limite", lido.alvo(), f.texto("credencial"))
                if negada is not None:
                    erros, status = [negada], 403
            if lido is not None and not erros:
                e = lido.escolha
                criado = await consultas.criar_limite(
                    c,
                    pessoa_id=pessoa_id,
                    aplicativo_id=e.aplicativo_id,
                    sistema_id=e.sistema_id,
                    nome_original=e.nome_original,
                    destinatarios=lido.destinatarios_json,
                    por_dia=lido.por_dia,
                    dias=lido.dias,
                    com_passkey=self.ligada,
                    fuso=FUSO,
                )
                await self.t.extrato(
                    c,
                    "limite_criado" if self.ligada else "limite_criado_sem_passkey",
                    {"por_dia": lido.por_dia, "ferramenta": e.nome_vitrine},
                    pessoa_id=pessoa_id,
                    aplicativo_id=e.aplicativo_id,
                    sistema_id=e.sistema_id,
                    ferramenta=e.nome_vitrine,
                    dados={
                        "limite_id": str(criado),
                        "dias": lido.dias,
                        "qualquer_destinatario": not lido.destinatarios,
                    },
                )
        if criado is None:
            log.info("telas_limite_recusado", status=status)
            return await self._pagina_novo(
                pedido,
                f.texto("escolha"),
                erros=erros,
                valores=dict(f.como_dict(("csrf", "credencial"))),
                status=status,
            )
        log.info("telas_limite_criado", passkey=self.ligada)
        return ver_depois(f"/telas/limites/{criado}", "limite_criado")

    # --- Ver, ampliar e encerrar ---

    async def _pagina_limite(
        self, pedido: Request, limite_id: uuid.UUID, *, erros: list[str] | None = None, status: int = 200
    ) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        async with self.t.engine.connect() as c:
            linha = await consultas.limite_da_pessoa(c, sessao.pessoa.id, limite_id, FUSO)
            maximo_dia, maximo_dias = await self._maximos(c)
            tem_passkey = await passkeys.tem_ativa(c, sessao.pessoa.id)
        if linha is None:
            return self.t.erro(pedido, 404, "Limite não encontrado", "Este limite não é seu ou não existe.")
        return self.t.pagina(
            pedido,
            "acoes/limite.html",
            {
                "l": _limite(linha),
                "maximo_dia": max(maximo_dia, int(linha.por_dia)),
                "maximo_dias": maximo_dias,
                "ligada": self.ligada,
                "tem_passkey": tem_passkey,
                "aviso": self._aviso(pedido),
                "erros": erros or [],
            },
            status=status,
        )

    @tela
    async def limite(self, pedido: Request) -> Response:
        return await self._pagina_limite(pedido, pedido.path_params["limite_id"])

    def _ler_ampliacao(
        self, f: Formulario, linha: Row[Any], maximo_dia: int, maximo_dias: int
    ) -> tuple[int, int, list[str]]:
        erros: list[str] = []
        atual = int(linha.por_dia)
        teto = max(maximo_dia, atual)
        por_dia = _numero(f.texto("por_dia")) or 0
        dias = _numero(f.texto("dias")) or 0
        if str(linha.situacao) != "ativo":
            return por_dia, dias, ["Só um limite que vale agora pode ser ampliado. Crie outro."]
        if not atual <= por_dia <= teto:
            erros.append(f"A quantidade por dia vai de {atual} a {teto}. Para diminuir, encerre e crie outro.")
        if not 1 <= dias <= maximo_dias:
            erros.append(f"O prazo vai de 1 a {maximo_dias} dias, contados de hoje.")
        cresce = por_dia > atual or datetime.now(UTC) + timedelta(days=dias) > linha.valido_ate
        if not erros and not cresce:
            erros.append("Nada a ampliar: escolha mais vezes por dia ou um prazo maior que o de agora.")
        return por_dia, dias, erros

    @tela
    async def desafio_ampliar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        f = await self.t.formulario(pedido, sessao)
        limite_id: uuid.UUID = pedido.path_params["limite_id"]
        if not self.ligada:
            return passkeys.resposta_desafio(None, "A passkey está desligada nesta instalação.", status=409)
        async with self.t.engine.begin() as c:
            linha = await consultas.limite_da_pessoa(c, sessao.pessoa.id, limite_id, FUSO)
            if linha is None:
                return passkeys.resposta_desafio(None, "Limite não encontrado.", status=404)
            maximo_dia, maximo_dias = await self._maximos(c)
            por_dia, dias, erros = self._ler_ampliacao(f, linha, maximo_dia, maximo_dias)
            if erros:
                return passkeys.resposta_desafio(None, " ".join(erros), status=400)
            try:
                opcoes = await passkeys.opcoes_autenticacao(
                    c, self.cer, sessao.pessoa.id, "limite", alvo_ampliar(limite_id, por_dia, dias)
                )
            except passkeys.PasskeyRecusada as erro:
                return passkeys.resposta_desafio(None, erro.mensagem, status=409)
        return passkeys.resposta_desafio(opcoes)

    @tela
    async def ampliar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        f = await self.t.formulario(pedido, sessao)
        limite_id: uuid.UUID = pedido.path_params["limite_id"]
        pessoa_id = sessao.pessoa.id
        erros: list[str] = []
        status = 400
        feito: Row[Any] | None = None
        async with self.t.engine.begin() as c:
            linha = await consultas.limite_da_pessoa(c, pessoa_id, limite_id, FUSO)
            if linha is None:
                return self.t.erro(pedido, 404, "Limite não encontrado", "Este limite não é seu ou não existe.")
            maximo_dia, maximo_dias = await self._maximos(c)
            por_dia, dias, erros = self._ler_ampliacao(f, linha, maximo_dia, maximo_dias)
            if not erros and self.ligada:
                negada = await passkeys.conferir(
                    c, self.cer, pessoa_id, "limite", alvo_ampliar(limite_id, por_dia, dias), f.texto("credencial")
                )
                if negada is not None:
                    erros, status = [negada], 403
            if not erros:
                feito = await consultas.ampliar_limite(
                    c, pessoa_id=pessoa_id, limite_id=limite_id, por_dia=por_dia, dias=dias, com_passkey=self.ligada
                )
                if feito is None:
                    erros, status = ["Este limite não vale mais. Nada mudou."], 409
                else:
                    await self.t.extrato(
                        c,
                        "limite_ampliado" if self.ligada else "limite_ampliado_sem_passkey",
                        {"ferramenta": linha.ferramenta, "por_dia": int(feito.por_dia)},
                        pessoa_id=pessoa_id,
                        aplicativo_id=linha.aplicativo_id,
                        sistema_id=linha.sistema_id,
                        ferramenta=linha.ferramenta,
                        dados={"limite_id": str(limite_id), "dias": dias},
                    )
        if feito is None:
            return await self._pagina_limite(pedido, limite_id, erros=erros, status=status)
        log.info("telas_limite_ampliado", passkey=self.ligada)
        return ver_depois(f"/telas/limites/{limite_id}", "limite_ampliado")

    @tela
    async def encerrar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        await self.t.formulario(pedido, sessao)
        limite_id: uuid.UUID = pedido.path_params["limite_id"]
        feito: Row[Any] | None = None
        async with self.t.engine.begin() as c:
            linha = await consultas.limite_da_pessoa(c, sessao.pessoa.id, limite_id, FUSO)
            if linha is not None:
                feito = await consultas.encerrar_limite(
                    c, pessoa_id=sessao.pessoa.id, limite_id=limite_id, motivo=MOTIVO_ENCERRADO
                )
            if feito is not None and linha is not None:
                await self.t.extrato(
                    c,
                    "limite_encerrado",
                    {"ferramenta": linha.ferramenta, "motivo": MOTIVO_ENCERRADO},
                    pessoa_id=sessao.pessoa.id,
                    aplicativo_id=linha.aplicativo_id,
                    sistema_id=linha.sistema_id,
                    ferramenta=linha.ferramenta,
                    dados={"limite_id": str(limite_id)},
                )
        if linha is None:
            return self.t.erro(pedido, 404, "Limite não encontrado", "Este limite não é seu ou não existe.")
        if feito is None:
            return ver_depois(f"/telas/limites/{limite_id}", "ja_terminou")
        log.info("telas_limite_encerrado")
        return ver_depois("/telas/limites", "limite_encerrado")
