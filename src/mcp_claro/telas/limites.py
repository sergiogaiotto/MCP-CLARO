"""Telas da pessoa: limites por aproximacao (Fase 2).

Um limite deixa um aplicativo fazer uma acao sem ficha, ate um numero de vezes por dia e ate uma data, so
para os destinatarios escolhidos (ou para qualquer destinatario, dito com clareza).

- So entram acoes (classe diferente de "le") das ordens de servico que valem agora para a pessoa, em
  sistemas de chave por pessoa. Risco alto nunca entra: sempre passa pela ficha.
- Quantidade por dia ate ``ALLOWANCE_MAX_PER_DAY`` e prazo ate ``ALLOWANCE_MAX_DAYS`` (politica vigente).
- Criar e ampliar pedem a passkey quando ``FEATURE_PASSKEY`` esta ligado. O desafio fica preso ao resumo
  exato do que foi pedido (finalidade "limite"): a passkey aprova este limite, nao outro. Encerrar nao pede.
  Com a passkey desligada, criar e ampliar pedem o login refeito na hora (``Telas.login_recente``).
- "So estes destinatarios": quem diz quais campos do pedido dizem o destinatario e o ADMIN, na revisao da
  ferramenta (``ferramentas_revisadas.campos_destinatario``). O campo escolhido aceita so os valores escritos,
  e cada outro campo marcado pelo ADMIN entra com a lista vazia: tem de vir vazio ou faltar. Um pedido com
  copia para outra pessoa, por exemplo, passa pela ficha. O resto do pedido (o texto da mensagem) fica livre.
  Sem campo marcado, o limite so pode valer para qualquer destinatario. Criar trava a versao revisada lida
  (``consultas.travar_revisao``): a revisao do ADMIN que marca um campo novo nao cruza com um limite sendo criado.
  Se a revisao termina antes, a escolha do formulario (a versao revisada antiga) sai da lista: a tela diz que o
  ADMIN acabou de revisar a acao e leva a "Criar limite" de novo, com a versao nova. Nada e gravado. O desafio
  da passkey (o primeiro passo do botao, com a passkey ligada) diz o mesmo e manda o caminho da versao nova; o
  passkey.js mostra o botao junto da frase.
- Ampliar so aumenta: mais vezes por dia ou mais prazo. Para mudar os destinatarios, encerre e crie outro.
  Ampliar segue o teto vigente: um limite acima do teto de agora so renova ate o teto, e o prazo nunca passa
  de ``ALLOWANCE_MAX_DAYS`` a contar de hoje.
- Com a passkey ligada, um limite criado ou ampliado sem ela nao vale (o nucleo nao o gasta): a tela marca
  "Nao vale com a passkey ligada" e nao o amplia; a pessoa cria outro, com a passkey (o que nao vale nao conta
  como "limite igual"), e pode encerrar o antigo.
- Encerrar cancela, na mesma transacao, o que o limite ja tinha liberado e ainda espera a hora de sair.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import urlencode

from sqlalchemy import Row
from sqlalchemy.ext.asyncio import AsyncConnection
from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import BaseRoute, Route

from mcp_claro.logs import logger
from mcp_claro.regras import politica
from mcp_claro.saida import fila
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.protocolo import FUSO, canonico
from mcp_claro.telas import consultas, passkeys
from mcp_claro.telas.acoes import marcar_escondidos
from mcp_claro.telas.nucleo import LoginRefeito, Telas, tela, ver_depois
from mcp_claro.telas.paginas import AVISOS
from mcp_claro.telas.seguranca import Formulario
from mcp_claro.telas.sessoes import Sessao
from mcp_claro.versao import VERSAO_MCP

log = logger("mcp_claro.telas.limites")

MAXIMO_VALORES: Final = 50
MAXIMO_VALOR: Final = 200
MOTIVO_ENCERRADO: Final = "encerrado pela pessoa nas telas"
MOTIVO_CANCELADA: Final = "cancelada porque a pessoa encerrou o limite que a liberou"
MAXIMO_CANCELADAS_NO_AVISO: Final = 10_000
LOGIN_REFEITO_LIMITE: Final = (
    "A passkey está desligada nesta instalação: criar ou ampliar um limite pede um login refeito na hora."
    " Entre de novo e tente outra vez. Nada mudou."
)
NAO_VALE_COM_PASSKEY: Final = (
    "Este limite foi criado ou ampliado sem a passkey e não vale com a passkey ligada: as ações dele passam pela"
    " ficha. Ele não pode ser ampliado. Encerre este limite e crie outro, com a passkey. Nada mudou."
)
REVISADA_AGORA: Final = "O ADMIN acabou de revisar esta ação. Abra Criar limite de novo."
REVISADA_RISCO_ALTO: Final = (
    "O ADMIN acabou de revisar esta ação e a marcou como de risco alto. Ações de risco alto nunca entram em limite:"
    " sempre passam pela ficha."
)
SEM_CAMPO_MARCADO: Final = (
    "O ADMIN ainda não marcou qual campo desta ação diz o destinatário. Por enquanto, o limite só pode valer para"
    " qualquer destinatário."
)
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


def aviso_de_encerrado(canceladas: int) -> str:
    """O aviso de encerrar, dizendo quantas acoes liberadas pelo limite foram canceladas antes de sair."""
    base = AVISOS_LIMITES["limite_encerrado"]
    if canceladas <= 0:
        return base + " Nenhuma ação liberada por ele esperava para sair."
    if canceladas == 1:
        return base + " 1 ação liberada por ele ainda esperava para sair e foi cancelada: nada dela chegou ao sistema."
    return (
        base + f" {canceladas} ações liberadas por ele ainda esperavam para sair e foram canceladas:"
        " nada delas chegou ao sistema."
    )


def _json(valor: Any) -> Any:
    return json.loads(valor) if isinstance(valor, str) else valor


def campos_de_destinatario(marcados: Any) -> tuple[str, ...]:
    """Os campos que o ADMIN marcou, na revisao da ferramenta, como os que dizem o destinatario.

    Le a coluna ``ferramentas_revisadas.campos_destinatario`` (o ADMIN so marca campos do esquema de entrada).
    Nao adivinha pelo tipo do campo: sem marca, nenhum campo (o limite so vale para qualquer destinatario)."""
    if not isinstance(marcados, list | tuple):
        return ()
    return tuple(dict.fromkeys(str(nome) for nome in marcados if isinstance(nome, str) and nome))


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
            campos=campos_de_destinatario(linha.campos_destinatario),
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


def ferramenta_da_escolha(chave: str) -> uuid.UUID | None:
    """A versao revisada (``ferramentas_revisadas.id``) da escolha "aplicativo:ferramenta" do formulario."""
    _, _, ferramenta = chave.partition(":")
    try:
        return uuid.UUID(ferramenta)
    except ValueError:
        return None


def aplicativo_da_escolha(chave: str) -> uuid.UUID | None:
    """O aplicativo da escolha "aplicativo:ferramenta" do formulario."""
    aplicativo, _, _ = chave.partition(":")
    try:
        return uuid.UUID(aplicativo)
    except ValueError:
        return None


def mesma_acao(
    escolhas: Iterable[Escolha], aplicativo_id: uuid.UUID, sistema_id: uuid.UUID, nome_original: str
) -> Escolha | None:
    """A escolha de agora para a mesma acao (mesmo aplicativo, mesmo sistema, mesmo nome no sistema)."""
    return next(
        (
            e
            for e in escolhas
            if e.aplicativo_id == aplicativo_id and e.sistema_id == sistema_id and e.nome_original == nome_original
        ),
        None,
    )


def link_revisada(revisada: Escolha) -> tuple[str, str]:
    """O passo seguinte quando o ADMIN acabou de revisar a acao: "Criar limite" com a versao revisada de agora."""
    return "/telas/limites/novo?" + urlencode({"escolha": revisada.chave}), "Criar limite para esta ação"


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
    if tipo == "estes" and not escolha.campos:
        erros.append(SEM_CAMPO_MARCADO)
    elif tipo == "estes":
        campo = f.texto("campo")
        valores = ler_valores(f.texto("valores"))
        if campo not in escolha.campos:
            erros.append("Escolha o campo do pedido que diz o destinatário.")
        if not valores:
            erros.append("Escreva pelo menos um destinatário aceito, um por linha.")
        elif len(valores) > MAXIMO_VALORES or any(len(v) > MAXIMO_VALOR for v in valores):
            erros.append(f"Use até {MAXIMO_VALORES} destinatários, cada um com até {MAXIMO_VALOR} letras.")
        # Os outros campos que o ADMIN marcou como destinatario entram vazios: a pessoa consentiu com estes
        # destinatarios, e uma copia para outra pessoa nao passa sem ficha. Os campos nao marcados ficam livres.
        destinatarios = [{"campo": campo, "valores": valores}] + [
            {"campo": outro, "valores": []} for outro in escolha.campos if outro != campo
        ]
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
    """Os destinatarios do limite numa frase, com os caracteres escondidos marcados (como na ficha)."""
    itens = _json(destinatarios) or []
    if not itens:
        return "Qualquer destinatário"
    partes: list[str] = []
    vazios: list[str] = []
    for item in itens:
        if isinstance(item, dict):
            campo = marcar_escondidos(str(item.get("campo")))
            valores = [marcar_escondidos(str(v)) for v in item.get("valores") or []]
            if valores:
                partes.append(f"{campo}: {', '.join(valores)}")
            else:
                vazios.append(campo)
    texto = "Só " + "; ".join(partes) if partes else "Nenhum destinatário"
    if vazios:
        texto += ". Precisam vir vazios: " + ", ".join(vazios)
    return texto


@dataclass(frozen=True, slots=True)
class Limite:
    """Um limite como a tela mostra. ``ligada``: a passkey esta ligada nesta instalacao agora."""

    linha: Row[Any]
    destinatarios: str
    ligada: bool = False

    @property
    def ativo(self) -> bool:
        return str(self.linha.situacao) == "ativo"

    @property
    def nao_vale_com_passkey(self) -> bool:
        """Criado ou ampliado sem a passkey, com ela ligada agora: nao e gasto (as acoes vao para a ficha)."""
        return nao_vale_com_passkey(self.linha, self.ligada)

    @property
    def situacao(self) -> str:
        if self.nao_vale_com_passkey:
            return "Não vale com a passkey ligada"
        return SITUACOES.get(str(self.linha.situacao), str(self.linha.situacao))


async def cancelar_liberadas(c: AsyncConnection, limite_id: uuid.UUID, motivo: str, *, quem: str) -> int:
    """Na transacao que encerra o limite: cancela o que ele liberou e ainda espera a hora de sair.

    O que o despachante ja marcou "enviando" nao muda (``fila.parar`` so tira de antes de sair)."""
    canceladas = 0
    for fila_id in await consultas.liberadas_pelo_limite(c, limite_id):
        op = await fila.por_fila(c, fila_id)
        if op is not None and await fila.parar(c, op, Estado.CANCELADA, motivo, quem=quem, papel="entrada"):
            canceladas += 1
    return canceladas


def nao_vale_com_passkey(linha: Any, ligada: bool) -> bool:
    """Limite ativo criado ou ampliado sem a passkey, com ela ligada agora: nao e gasto e nao e ampliado."""
    return ligada and str(linha.situacao) == "ativo" and not bool(linha.com_passkey)


def _limite(linha: Row[Any], ligada: bool = False) -> Limite:
    return Limite(linha, destinatarios_legiveis(linha.destinatarios), ligada)


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
        if codigo == "limite_encerrado" and "canceladas" in pedido.query_params:
            canceladas = _numero(pedido.query_params.get("canceladas", "")) or 0
            return aviso_de_encerrado(min(max(canceladas, 0), MAXIMO_CANCELADAS_NO_AVISO))
        return AVISOS_LIMITES.get(codigo) or AVISOS.get(codigo)

    def _refazer(self, pedido: Request, sessao: Sessao, voltar: str) -> LoginRefeito | None:
        """Sem a passkey, criar e ampliar pedem o login refeito na hora; com ela, nao (``None``)."""
        return None if self.ligada else self.t.login_refeito(pedido, sessao, voltar)

    def _falta_login(self, sessao: Sessao) -> bool:
        return not self.ligada and not self.t.login_recente(sessao)

    async def _escolhas(self, c: AsyncConnection, pessoa_id: uuid.UUID) -> list[Escolha]:
        linhas = await consultas.ferramentas_de_acao(c, pessoa_id, VERSAO_MCP)
        return sorted((Escolha.da_linha(linha) for linha in linhas), key=lambda e: (e.aplicativo, e.nome_vitrine))

    async def _revisada(self, c: AsyncConnection, chave: str, escolhas: dict[str, Escolha]) -> Escolha | None:
        """A escolha que veio no formulario saiu da lista porque o ADMIN revisou a acao (a versao revisada mudou)?
        Devolve a escolha de agora para a mesma acao. ``None``: a escolha vale, ou a acao saiu mesmo das ordens."""
        if chave in escolhas:
            return None
        aplicativo_id, ferramenta_id = aplicativo_da_escolha(chave), ferramenta_da_escolha(chave)
        if aplicativo_id is None or ferramenta_id is None:
            return None
        antiga = await consultas.revisao_por_id(c, ferramenta_id)
        if antiga is None:
            return None
        return mesma_acao(escolhas.values(), aplicativo_id, antiga.sistema_id, antiga.nome_original)

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
        limites = [_limite(linha, self.ligada) for linha in linhas]
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
            revisada = await self._revisada(c, chave, escolhas)
        escolha = escolhas.get(chave)
        if revisada is not None and revisada.risco == "alto":
            # Revisada para risco alto: criar de novo so levaria a outra recusa. A frase certa vem ja.
            return self.t.erro(pedido, 400, "Não dá para criar este limite", REVISADA_RISCO_ALTO)
        if revisada is not None:
            # A acao continua nas ordens; so a versao revisada mudou (o formulario era de antes da revisao).
            return self.t.erro(
                pedido, 409, "Não dá para criar este limite", REVISADA_AGORA, link=link_revisada(revisada)
            )
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
                "refazer": self._refazer(pedido, sessao, "/telas/limites/novo?" + urlencode({"escolha": chave})),
                "pede": "Criar um limite pede um login refeito na hora, porque a passkey está desligada"
                " nesta instalação.",
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
                revisada = await self._revisada(c, f.texto("escolha"), escolhas)
                if revisada is not None and revisada.risco == "alto":
                    return passkeys.resposta_desafio(None, REVISADA_RISCO_ALTO, status=400)
                if revisada is not None:
                    # O caso comum: o formulario foi aberto antes da revisao e o toque vem depois. O passkey.js
                    # mostra a frase com o botao para a versao nova.
                    return passkeys.resposta_desafio(None, REVISADA_AGORA, status=409, link=link_revisada(revisada))
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
            # Primeiro a trava da versao revisada escolhida: os campos de destinatario lidos aqui valem ate o
            # COMMIT, e uma revisao do ADMIN que os mude espera (ou ja terminou, e a escolha antiga sai da lista).
            ferramenta_id = ferramenta_da_escolha(f.texto("escolha"))
            if ferramenta_id is not None:
                await consultas.travar_revisao(c, ferramenta_id)
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
                ligada=self.ligada,
            ):
                erros = ["Você já tem um limite igual valendo para esta ação. Amplie o que existe."]
                status = 409
            if lido is not None and not erros and self._falta_login(sessao):
                erros, status = [LOGIN_REFEITO_LIMITE], 403
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
                    "limite_criado" if self.ligada else "limite_criado_login_refeito",
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
                "l": _limite(linha, self.ligada),
                "minimo_dia": min(int(linha.por_dia), maximo_dia),
                "maximo_dia": maximo_dia,
                "acima_do_teto": int(linha.por_dia) > maximo_dia,
                "maximo_dias": maximo_dias,
                "ligada": self.ligada,
                "tem_passkey": tem_passkey,
                "refazer": self._refazer(pedido, sessao, f"/telas/limites/{limite_id}"),
                "pede": "Ampliar um limite pede um login refeito na hora, porque a passkey está desligada"
                " nesta instalação.",
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
        # O teto e o de agora: um limite que ficou acima dele (a politica baixou) so renova ate o teto.
        teto = maximo_dia
        piso = min(atual, teto)
        por_dia = _numero(f.texto("por_dia")) or 0
        dias = _numero(f.texto("dias")) or 0
        if str(linha.situacao) != "ativo":
            return por_dia, dias, ["Só um limite que vale agora pode ser ampliado. Crie outro."]
        if not piso <= por_dia <= teto:
            if atual > teto:
                erros.append(
                    f"O teto mudou: agora vai até {teto} por dia. Para renovar, este limite passa a {teto} por dia."
                )
            else:
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
            if nao_vale_com_passkey(linha, self.ligada):
                return passkeys.resposta_desafio(None, NAO_VALE_COM_PASSKEY, status=409)
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
        feito: Row[Any] | None = None
        erros: list[str] = []
        status = 400
        async with self.t.engine.begin() as c:
            linha = await consultas.limite_da_pessoa(c, sessao.pessoa.id, limite_id, FUSO)
            if linha is not None:
                feito, erros, status = await self._ampliar(c, f, sessao, linha)
        # As paginas saem depois da transacao: cada pedido segura uma conexao do pool so.
        if linha is None:
            return self.t.erro(pedido, 404, "Limite não encontrado", "Este limite não é seu ou não existe.")
        if feito is None:
            return await self._pagina_limite(pedido, limite_id, erros=erros, status=status)
        log.info("telas_limite_ampliado", passkey=bool(feito.com_passkey))
        return ver_depois(f"/telas/limites/{limite_id}", "limite_ampliado")

    async def _ampliar(
        self, c: AsyncConnection, f: Formulario, sessao: Sessao, linha: Row[Any]
    ) -> tuple[Row[Any] | None, list[str], int]:
        """Na transacao: confere o pedido, o reforco (passkey ou login refeito) e amplia. (feito, erros, status)."""
        pessoa_id, limite_id = sessao.pessoa.id, uuid.UUID(str(linha.id))
        if nao_vale_com_passkey(linha, self.ligada):
            return None, [NAO_VALE_COM_PASSKEY], 409
        maximo_dia, maximo_dias = await self._maximos(c)
        por_dia, dias, erros = self._ler_ampliacao(f, linha, maximo_dia, maximo_dias)
        if erros:
            return None, erros, 400
        if self.ligada:
            negada = await passkeys.conferir(
                c, self.cer, pessoa_id, "limite", alvo_ampliar(limite_id, por_dia, dias), f.texto("credencial")
            )
            if negada is not None:
                return None, [negada], 403
        if self._falta_login(sessao):
            return None, [LOGIN_REFEITO_LIMITE], 403
        feito = await consultas.ampliar_limite(
            c,
            pessoa_id=pessoa_id,
            limite_id=limite_id,
            por_dia=por_dia,
            dias=dias,
            teto_dia=maximo_dia,
            teto_dias=maximo_dias,
            com_passkey=self.ligada,
        )
        if feito is None:
            return None, ["Este limite não vale mais. Nada mudou."], 409
        # O extrato diz como foi ampliado agora: com a passkey, ou (passkey desligada) com o login refeito.
        await self.t.extrato(
            c,
            "limite_ampliado" if feito.com_passkey else "limite_ampliado_login_refeito",
            {"ferramenta": linha.ferramenta, "por_dia": int(feito.por_dia)},
            pessoa_id=pessoa_id,
            aplicativo_id=linha.aplicativo_id,
            sistema_id=linha.sistema_id,
            ferramenta=linha.ferramenta,
            dados={"limite_id": str(limite_id), "dias": dias, "com_passkey": bool(feito.com_passkey)},
        )
        return feito, [], 200

    @tela
    async def encerrar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        await self.t.formulario(pedido, sessao)
        limite_id: uuid.UUID = pedido.path_params["limite_id"]
        feito: Row[Any] | None = None
        canceladas = 0
        async with self.t.engine.begin() as c:
            linha = await consultas.limite_da_pessoa(c, sessao.pessoa.id, limite_id, FUSO)
            if linha is not None:
                feito = await consultas.encerrar_limite(
                    c, pessoa_id=sessao.pessoa.id, limite_id=limite_id, motivo=MOTIVO_ENCERRADO
                )
            if feito is not None and linha is not None:
                canceladas = await cancelar_liberadas(c, limite_id, MOTIVO_CANCELADA, quem="pessoa")
                await self.t.extrato(
                    c,
                    "limite_encerrado",
                    {"ferramenta": linha.ferramenta, "motivo": MOTIVO_ENCERRADO},
                    pessoa_id=sessao.pessoa.id,
                    aplicativo_id=linha.aplicativo_id,
                    sistema_id=linha.sistema_id,
                    ferramenta=linha.ferramenta,
                    dados={"limite_id": str(limite_id), "canceladas": canceladas},
                )
        if linha is None:
            return self.t.erro(pedido, 404, "Limite não encontrado", "Este limite não é seu ou não existe.")
        if feito is None:
            return ver_depois(f"/telas/limites/{limite_id}", "ja_terminou")
        log.info("telas_limite_encerrado", canceladas=canceladas)
        return RedirectResponse(
            "/telas/limites?" + urlencode({"aviso": "limite_encerrado", "canceladas": canceladas}), status_code=303
        )
