"""Telas da pessoa na Fase 2: caixa de pendencias, ficha, espera visivel, "Minhas acoes" e "estamos confirmando".

- A ficha mostra o que vai acontecer (ferramenta pelo nome da vitrine, sistema e os dados, escapados), o
  numero de protocolo, o codigo de conferencia em destaque e o prazo. O que o aplicativo diz fica numa caixa
  a parte, marcada como texto dele, nao conferido pelo MCP-CLARO.
- Confirmar e recusar sao POST com CSRF. Ficha de risco alto (``exige_passkey``) so confirma com passkey: a
  conferencia e no servidor, no mesmo POST e na mesma transacao que confirma (``telas/passkeys.py``).
- Ficha de risco alto que nao confirma pela passkey (``FEATURE_PASSKEY`` desligado, inclusive a ficha que
  nasceu pedindo passkey, ou ficha montada quando a passkey estava desligada) confirma so com o login refeito
  na hora (``Telas.login_recente``): a tela manda refazer o login no provedor e volta para a ficha.
- Os dados do pedido aparecem com os caracteres escondidos (invisiveis, de formato e de direcao, como U+202E)
  trocados por marcas visiveis como ``[U+202E]``, e a ficha avisa em destaque quando ha algum.
- Depois de confirmar, a pagina da acao mostra a hora de sair (hora de Brasilia) e quanto falta; sem
  JavaScript, a hora fica escrita; com o htmx, a parte do estado se atualiza sozinha. Da para cancelar
  antes de sair ou seguir agora.
- Resultado nao confirmado: a pessoa registra o que apurou (aconteceu, nao aconteceu, nao sei). Nada e
  reenviado.

As mudancas de estado sao todas de ``saida/fila.py``; aqui so se mostra e se pede.
"""

from __future__ import annotations

import json
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final
from zoneinfo import ZoneInfo

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import BaseRoute, Route

from mcp_claro.logs import logger
from mcp_claro.regras import politica
from mcp_claro.saida import fila
from mcp_claro.saida.estados import FRASE, Estado
from mcp_claro.saida.fila import DecisaoRecusada, Operacao
from mcp_claro.saida.protocolo import FUSO, protocolo_valido
from mcp_claro.telas import consultas, passkeys
from mcp_claro.telas.nucleo import Telas, tela, ver_depois
from mcp_claro.telas.paginas import AVISOS

log = logger("mcp_claro.telas.acoes")

AVISOS_ACOES: Final[dict[str, str]] = {
    "confirmada": "Pronto. A ficha foi confirmada. A ação espera a hora de sair; ainda dá para cancelar.",
    "recusada": "Pronto. A ficha foi recusada. Nada foi para o sistema.",
    "cancelada": "Pronto. A ação foi cancelada antes de sair. Nada chegou ao sistema.",
    "seguir_agora": "Pronto. A ação sai agora, sem esperar o resto do tempo.",
    "decisao_registrada": "Pronto. Sua decisão foi registrada. Nada será reenviado.",
}

# As frases de ``estados.FRASE``, com acento, para a tela. Se uma delas mudar la, a tela volta a mostrar a de la.
FRASE_TELA: Final[dict[Estado, str]] = {
    Estado.AGUARDANDO_FICHA: "A ficha espera a sua confirmação na tela do gateway.",
    Estado.LIBERADA: "Confirmada. A ação espera a hora de sair; ainda dá para cancelar.",
    Estado.ENVIANDO: "Estamos enviando ao sistema. Não repita o pedido.",
    Estado.ENVIADA: "Enviada uma vez ao sistema.",
    Estado.NAO_SAIU: "Não saiu: o sistema não executou o pedido (não recebeu ou recusou na porta). Nada aconteceu.",
    Estado.SISTEMA_PEDIU_DADOS: "O sistema pediu mais dados no meio da ação; o MCP-CLARO não repassa esses pedidos.",
    Estado.NAO_CONFIRMADA: "Resultado não confirmado: o pedido pode ter chegado ao sistema. Nada será reenviado; "
    "a pessoa decide na tela do gateway.",
    Estado.VENCIDA: "A ficha venceu sem resposta. Nada seguiu.",
    Estado.CANCELADA: "Cancelada antes de sair. Nada seguiu.",
    Estado.PARADA_POR_BLOQUEIO: "Parada por um bloqueio ou uma pausa. Nada seguiu.",
    Estado.PARADA_POR_RESTAURACAO: "Parada porque o banco foi restaurado. Nada seguiu sem nova confirmação.",
}

ROTULOS: Final[dict[Estado, str]] = {
    Estado.AGUARDANDO_FICHA: "Esperando a sua decisão",
    Estado.LIBERADA: "Na espera para sair",
    Estado.ENVIANDO: "Saindo agora",
    Estado.ENVIADA: "Enviada",
    Estado.NAO_SAIU: "Não saiu",
    Estado.SISTEMA_PEDIU_DADOS: "O sistema pediu mais dados",
    Estado.NAO_CONFIRMADA: "Estamos confirmando",
    Estado.VENCIDA: "Venceu sem resposta",
    Estado.CANCELADA: "Cancelada",
    Estado.PARADA_POR_BLOQUEIO: "Parada por bloqueio ou pausa",
    Estado.PARADA_POR_RESTAURACAO: "Parada por restauração",
}

LOGIN_REFEITO_FICHA: Final = (
    "Esta ação é de risco alto: para confirmar sem a passkey, entre de novo agora e confirme logo depois. Nada seguiu."
)

FICHA_ALHEIA: Final = (404, "Esta ficha não é sua ou não existe.")

# Recusas de ``fila`` (``DecisaoRecusada.motivo``) em linguagem simples, com o status da resposta.
RECUSAS: Final[dict[str, tuple[int, str]]] = {
    "ficha_desconhecida": (404, "Ficha não encontrada. Ela não é sua ou não existe."),
    "ficha_ja_decidida": (409, "Esta ficha já foi decidida antes. Nada mudou."),
    "ficha_vencida": (409, "O prazo desta ficha acabou. Nada seguiu para o sistema."),
    "falta_passkey": (403, "Esta ação é de risco alto: confirme com a sua passkey. Nada seguiu."),
    "falta_login_refeito": (403, LOGIN_REFEITO_FICHA),
    "codigo_nao_bate": (409, "O código de conferência não bate com o pedido. Por segurança, nada seguiu."),
    "nao_esta_aguardando": (409, "Esta ação não espera mais confirmação. Veja o andamento dela."),
    "operacao_desconhecida": (404, "Ação não encontrada. Ela não é sua ou não existe."),
    "nao_esta_esperando": (409, "Esta ação não está mais na espera. Nada mudou."),
    "decisao_invalida": (400, "Escolha uma das respostas: aconteceu, não aconteceu ou ainda não sei."),
    "nao_e_nao_confirmada": (409, "Só um resultado não confirmado pede esta decisão. Nada mudou."),
    "ja_decidida": (409, "Esta decisão já tinha sido registrada. Nada mudou."),
}

DECISOES: Final[dict[str, str]] = {
    "aconteceu": "Aconteceu no sistema",
    "nao_aconteceu": "Não aconteceu no sistema",
    "nao_sei": "Ainda não sei",
}
RISCOS: Final[dict[str, str]] = {"baixo": "Risco baixo", "medio": "Risco médio", "alto": "Risco alto"}
MAXIMO_OBSERVACAO: Final = 500
SEGUNDOS_ENTRE_ATUALIZACOES: Final = 2


def sem_acento(texto: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", texto) if not unicodedata.combining(ch))


def frase_do_estado(estado: Estado) -> str:
    com_acento = FRASE_TELA.get(estado)
    if com_acento is not None and sem_acento(com_acento) == FRASE[estado]:
        return com_acento
    return FRASE[estado]


def recusa_legivel(erro: DecisaoRecusada) -> tuple[int, str]:
    """(status, frase) da recusa. A frase do ``ja_saiu`` traz o estado atual, que a propria recusa ja diz."""
    if erro.motivo == "ja_saiu":
        estado = next((e for e in Estado if FRASE[e] in erro.mensagem), None)
        atual = frase_do_estado(estado) if estado is not None else erro.mensagem
        return 409, f"Não deu para cancelar: a ação já tinha saído. {atual}"
    return RECUSAS.get(erro.motivo, (409, erro.mensagem))


# Caracteres escondidos: os que nao aparecem na tela ou mudam a ordem do texto. O escape do HTML nao mexe neles,
# e o navegador os obedece (U+202E inverte o que vem depois). Na tela, viram uma marca visivel como [U+202E].
CATEGORIAS_ESCONDIDAS: Final = frozenset({"Cc", "Cf", "Zl", "Zp", "Co", "Cn"})
# Os de direcao (bidi) e de largura zero, ditos um a um (ja sao Cf, ficam aqui por clareza): U+061C, U+200B a
# U+200F, U+202A a U+202E, U+2066 a U+2069. E os que o Unicode chama de letra, marca ou simbolo, mas nao desenham
# nada ou desenham um branco: os "ignoraveis" do Unicode (preenchedores do hangul U+115F, U+1160, U+3164 e
# U+FFA0; U+034F, que junta letras; U+17B4 e U+17B5, vogais do khmer que nao aparecem; seletores de variacao,
# inclusive os do mongol, U+180B a U+180F) e U+2800, o braille em branco.
ESCONDIDOS_DITOS: Final = frozenset(
    {
        0x034F,
        0x061C,
        0x115F,
        0x1160,
        0x17B4,
        0x17B5,
        *range(0x180B, 0x1810),
        *range(0x200B, 0x2010),
        *range(0x202A, 0x202F),
        *range(0x2060, 0x2070),
        0x2800,
        0x3164,
        *range(0xFE00, 0xFE10),
        0xFEFF,
        0xFFA0,
        *range(0xE0000, 0xE1000),
    }
)


def escondido(ch: str) -> bool:
    """A quebra de linha fica: a tela a mostra como quebra (``white-space: pre-wrap``)."""
    return ch != "\n" and (ord(ch) in ESCONDIDOS_DITOS or unicodedata.category(ch) in CATEGORIAS_ESCONDIDAS)


def marcar_escondidos(texto: str) -> str:
    """Troca cada caractere escondido por uma marca visivel, no lugar exato onde ele esta: "12[U+202E]34"."""
    return "".join(f"[U+{ord(ch):04X}]" if escondido(ch) else ch for ch in texto)


def _textos(valor: Any) -> list[str]:
    """Todos os textos de um valor, com as chaves de objetos e os itens de listas."""
    if isinstance(valor, str):
        return [valor]
    if isinstance(valor, dict):
        return [t for chave, v in valor.items() for t in (*_textos(str(chave)), *_textos(v))]
    if isinstance(valor, list | tuple):
        return [t for v in valor for t in _textos(v)]
    return []


def tem_escondidos(valor: Any) -> bool:
    return any(escondido(ch) for texto in _textos(valor) for ch in texto)


def valor_legivel(valor: Any) -> str:
    if isinstance(valor, str):
        return marcar_escondidos(valor)
    if isinstance(valor, bool):
        return "sim" if valor else "não"
    if valor is None:
        return "(vazio)"
    if isinstance(valor, int | float):
        return str(valor)
    return marcar_escondidos(json.dumps(valor, ensure_ascii=False, sort_keys=True))


def argumentos_legiveis(argumentos: dict[str, Any]) -> list[tuple[str, str]]:
    """Os dados do pedido em lista "chave: valor", com os escondidos marcados nas chaves e nos valores.

    O escape do HTML fica com o modelo (Jinja2 com escape automatico)."""
    return [(marcar_escondidos(str(chave)), valor_legivel(valor)) for chave, valor in argumentos.items()]


def linhas_do_aplicativo(diz: str | None) -> list[str]:
    """O que o aplicativo diz, linha a linha, com os escondidos marcados (a entrada ja tira os nao imprimiveis;
    aqui e a segunda barreira, para o que a tela mostra)."""
    return marcar_escondidos(diz).split("\n") if diz else []


def textos_do_resultado(resultado: dict[str, Any] | None) -> list[str]:
    """So o texto dos blocos de conteudo da resposta do sistema. Imagem, recurso e o resto nao aparecem."""
    if not isinstance(resultado, dict):
        return []
    blocos = resultado.get("content")
    if not isinstance(blocos, list):
        return []
    return [
        b["text"] for b in blocos if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    ]


def hora_de_brasilia(momento: datetime | None) -> str:
    if momento is None:
        return ""
    local = momento.astimezone(ZoneInfo(FUSO))
    return local.strftime("%H:%M:%S de %d/%m/%Y")


def tempo_legivel(segundos: int) -> str:
    if segundos <= 0:
        return "nada"
    horas, resto = divmod(segundos, 3600)
    minutos, seg = divmod(resto, 60)
    partes: list[str] = []
    if horas:
        partes.append(f"{horas} hora" + ("s" if horas > 1 else ""))
    if minutos:
        partes.append(f"{minutos} minuto" + ("s" if minutos > 1 else ""))
    if seg and not horas:
        partes.append(f"{seg} segundo" + ("s" if seg > 1 else ""))
    return " e ".join(partes)


def _segundos_ate(momento: datetime | None, agora: datetime) -> int | None:
    if momento is None:
        return None
    return max(0, int((momento - agora).total_seconds() + 0.999))


@dataclass(frozen=True, slots=True)
class Acao:
    """Uma operacao como as telas mostram."""

    op: Operacao
    rotulo: str
    frase: str
    argumentos: list[tuple[str, str]]
    escondidos: bool
    diz: list[str]
    diz_escondidos: bool
    textos: list[str]
    hora_saida: str
    falta: int
    falta_texto: str
    prazo: int
    prazo_texto: str
    expira_em: str
    decisao: str
    como: str

    @property
    def atualiza(self) -> bool:
        """A parte do estado se atualiza sozinha enquanto espera para sair ou esta saindo."""
        return self.op.estado in (Estado.LIBERADA, Estado.ENVIANDO)

    @property
    def pode_decidir_ficha(self) -> bool:
        return self.op.estado == Estado.AGUARDANDO_FICHA and self.op.ficha_decisao is None and self.prazo > 0

    @property
    def de_risco_alto(self) -> bool:
        """Ficha de risco alto: a que nasceu pedindo passkey ou a que foi montada com risco alto."""
        return self.op.ficha_exige_passkey or self.op.ficha_risco == "alto"


def montar(op: Operacao, agora: datetime | None = None) -> Acao:
    agora = agora or datetime.now(UTC)
    falta = _segundos_ate(op.hora_saida, agora) or 0
    prazo = _segundos_ate(op.ficha_expira_em, agora) or 0
    if op.limite_id is not None:
        como = "Dentro de um limite seu, sem ficha"
    elif op.ficha_exige_passkey:
        como = "Pela ficha, com passkey"
    else:
        como = "Pela ficha"
    return Acao(
        op=op,
        rotulo=ROTULOS[op.estado],
        frase=frase_do_estado(op.estado),
        argumentos=argumentos_legiveis(op.argumentos),
        escondidos=tem_escondidos(op.argumentos),
        diz=linhas_do_aplicativo(op.aplicativo_diz),
        diz_escondidos=tem_escondidos(op.aplicativo_diz),
        textos=textos_do_resultado(op.resultado),
        hora_saida=hora_de_brasilia(op.hora_saida),
        falta=falta,
        falta_texto=tempo_legivel(falta),
        prazo=prazo,
        prazo_texto=tempo_legivel(prazo),
        expira_em=hora_de_brasilia(op.ficha_expira_em),
        decisao=DECISOES.get(op.decisao_pessoa or "", ""),
        como=como,
    )


@dataclass(frozen=True, slots=True)
class Pendencias:
    fichas: list[Acao]
    espera: list[Acao]
    confirmando: list[Acao]

    @property
    def total(self) -> int:
        return len(self.fichas) + len(self.espera) + len(self.confirmando)


def separar(ops: list[Operacao], agora: datetime | None = None) -> Pendencias:
    acoes = [montar(op, agora) for op in ops]
    return Pendencias(
        fichas=[a for a in acoes if a.op.estado == Estado.AGUARDANDO_FICHA],
        espera=[a for a in acoes if a.op.estado == Estado.LIBERADA],
        confirmando=[a for a in acoes if a.op.estado == Estado.NAO_CONFIRMADA],
    )


class TelasDeAcoes:
    def __init__(self, t: Telas) -> None:
        self.t = t
        self.cer = passkeys.Cerimonia.de_parametros(t.p)

    def rotas(self) -> list[BaseRoute]:
        return [
            Route("/pendencias", self.pendencias, methods=["GET"]),
            Route("/fichas/{ficha_id:uuid}", self.ficha, methods=["GET"]),
            Route("/fichas/{ficha_id:uuid}/desafio", self.desafio, methods=["POST"]),
            Route("/fichas/{ficha_id:uuid}/confirmar", self.confirmar, methods=["POST"]),
            Route("/fichas/{ficha_id:uuid}/recusar", self.recusar, methods=["POST"]),
            Route("/acoes", self.lista, methods=["GET"]),
            Route("/acoes/{operacao_id:uuid}", self.acao, methods=["GET"]),
            Route("/acoes/{operacao_id:uuid}/estado", self.estado, methods=["GET"]),
            Route("/acoes/{operacao_id:uuid}/cancelar", self.cancelar, methods=["POST"]),
            Route("/acoes/{operacao_id:uuid}/seguir", self.seguir, methods=["POST"]),
            Route("/acoes/{operacao_id:uuid}/decidir", self.decidir, methods=["POST"]),
        ]

    def _aviso(self, pedido: Request) -> str | None:
        codigo = pedido.query_params.get("aviso", "")
        return AVISOS_ACOES.get(codigo) or AVISOS.get(codigo)

    # --- Inicio, com a contagem da caixa de pendencias ---

    async def inicio(self, pedido: Request) -> Response:
        """O inicio das telas: a contagem da caixa de pendencias e, para quem e responsavel ou substituto de um
        programa que roda sozinho, o link "Meus programas"."""
        sessao = await self.t.sessao(pedido)
        contagem: Pendencias | None = None
        programas = False
        if sessao is not None:
            async with self.t.engine.connect() as c:
                contagem = separar(await fila.pendencias(c, sessao.pessoa.id))
                programas = await consultas.cuida_de_programa(c, sessao.pessoa.id)
        return self.t.pagina(pedido, "inicio.html", {"pendencias": contagem, "cuida_de_programa": programas})

    # --- Caixa de pendencias ---

    @tela
    async def pendencias(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        async with self.t.engine.connect() as c:
            caixa = separar(await fila.pendencias(c, sessao.pessoa.id))
        return self.t.pagina(
            pedido, "acoes/pendencias.html", {"caixa": caixa, "passkey_ligada": self.t.p.FEATURE_PASSKEY}
        )

    # --- Ficha ---

    async def _ficha_da_pessoa(self, c: Any, ficha_id: uuid.UUID, pessoa_id: uuid.UUID) -> Operacao | None:
        operacao_id = await consultas.operacao_da_ficha(c, ficha_id)
        if operacao_id is None:
            return None
        op = await fila.por_id(c, operacao_id)
        if op is None or op.pessoa_id != pessoa_id or op.ficha_id != ficha_id:
            return None
        return op

    async def _pagina_ficha(
        self, pedido: Request, ficha_id: uuid.UUID, *, erros: list[str] | None = None, status: int = 200
    ) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        async with self.t.engine.connect() as c:
            op = await self._ficha_da_pessoa(c, ficha_id, sessao.pessoa.id)
            detalhes = await consultas.detalhes_da_operacao(c, op.id) if op else None
            tem_passkey = await passkeys.tem_ativa(c, sessao.pessoa.id)
        if op is None or detalhes is None:
            return self.t.erro(pedido, 404, "Ficha não encontrada", "Esta ficha não é sua ou não existe.")
        a = montar(op)
        return self.t.pagina(
            pedido,
            "acoes/ficha.html",
            {
                "a": a,
                "d": detalhes,
                "risco": RISCOS.get(str(detalhes.risco), str(detalhes.risco)),
                "passkey_ligada": self.t.p.FEATURE_PASSKEY,
                "tem_passkey": tem_passkey,
                "pede_login": self._pede_login(op),
                "refazer": self.t.login_refeito(pedido, sessao, f"/telas/fichas/{ficha_id}"),
                "pede": "Confirmar esta ação de risco alto pede um login refeito na hora.",
                "aviso": self._aviso(pedido),
                "erros": erros or [],
            },
            status=status,
        )

    def _pede_login(self, op: Operacao) -> bool:
        """Ficha de risco alto que nao confirma pela passkey (passkey desligada nesta instalacao, ou ficha montada
        quando ela estava desligada): o reforco e o login refeito na hora."""
        return montar(op).de_risco_alto and not (self.t.p.FEATURE_PASSKEY and op.ficha_exige_passkey)

    @tela
    async def ficha(self, pedido: Request) -> Response:
        return await self._pagina_ficha(pedido, pedido.path_params["ficha_id"])

    @tela
    async def desafio(self, pedido: Request) -> Response:
        """Desafio novo para confirmar esta ficha com a passkey (o navegador pede na hora do toque)."""
        sessao = await self.t.exigir_sessao(pedido)
        await self.t.formulario(pedido, sessao)
        ficha_id: uuid.UUID = pedido.path_params["ficha_id"]
        if not self.t.p.FEATURE_PASSKEY:
            return passkeys.resposta_desafio(None, "A passkey está desligada nesta instalação.", status=409)
        async with self.t.engine.begin() as c:
            op = await self._ficha_da_pessoa(c, ficha_id, sessao.pessoa.id)
            if op is None:
                return passkeys.resposta_desafio(None, "Ficha não encontrada.", status=404)
            if not montar(op).pode_decidir_ficha:
                return passkeys.resposta_desafio(None, "Esta ficha não espera mais confirmação.", status=409)
            try:
                opcoes = await passkeys.opcoes_autenticacao(c, self.cer, sessao.pessoa.id, "ficha", str(ficha_id))
            except passkeys.PasskeyRecusada as erro:
                return passkeys.resposta_desafio(None, erro.mensagem, status=409)
        return passkeys.resposta_desafio(opcoes)

    @tela
    async def confirmar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        f = await self.t.formulario(pedido, sessao)
        ficha_id: uuid.UUID = pedido.path_params["ficha_id"]
        pessoa_id = sessao.pessoa.id
        ligada = bool(self.t.p.FEATURE_PASSKEY)
        recente = self.t.login_recente(sessao)
        login_refeito = False
        recusa: tuple[int, str] | None = None
        com_passkey = False
        confirmada: Operacao | None = None
        # Toda pagina de erro sai depois da transacao: cada pedido segura uma conexao do pool so (a pagina da
        # ficha abre a sua).
        async with self.t.engine.begin() as c:
            op = await self._ficha_da_pessoa(c, ficha_id, pessoa_id)
            if op is None:
                recusa = FICHA_ALHEIA
            elif self._pede_login(op) and not recente:
                # Sem a passkey, o reforco do risco alto e o login refeito na hora. Nada muda ate la.
                log.info("telas_ficha_pede_login_refeito")
                recusa = (403, LOGIN_REFEITO_FICHA)
            elif self._pede_login(op):
                login_refeito = True  # o reforco usado nesta ficha; o extrato diz "com o login refeito"
            if op is not None and recusa is None:
                credencial = f.texto("credencial")
                if ligada and op.ficha_exige_passkey and credencial:
                    # A passkey e conferida aqui, na mesma transacao que confirma. Recusada, o desafio fica gasto.
                    negada = await passkeys.conferir(c, self.cer, pessoa_id, "ficha", str(ficha_id), credencial)
                    if negada is not None:
                        recusa = (403, negada)
                    com_passkey = negada is None
            if op is not None and recusa is None:
                valores = await politica.varios(c, "OUTBOX_HOLD_SECONDS", "CONFIRM_TOO_FAST_SECONDS")
                try:
                    async with c.begin_nested():
                        confirmada = await fila.confirmar_ficha(
                            c,
                            ficha_id,
                            pessoa_id=pessoa_id,
                            com_passkey=com_passkey,
                            login_refeito=login_refeito,
                            passkey_ligada=ligada,
                            espera_segundos=valores["OUTBOX_HOLD_SECONDS"],
                            rapida_segundos=valores["CONFIRM_TOO_FAST_SECONDS"],
                        )
                except DecisaoRecusada as erro:
                    recusa = recusa_legivel(erro)
        if recusa is not None or confirmada is None:
            status, mensagem = recusa or (409, "Nada mudou.")
            log.info("telas_ficha_recusada", status=status)
            if status == 404:
                return self.t.erro(pedido, 404, "Ficha não encontrada", mensagem)
            return await self._pagina_ficha(pedido, ficha_id, erros=[mensagem], status=status)
        log.info("telas_ficha_confirmada", passkey=com_passkey, login_refeito=login_refeito)
        return ver_depois(f"/telas/acoes/{confirmada.id}", "confirmada")

    @tela
    async def recusar(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        await self.t.formulario(pedido, sessao)
        ficha_id: uuid.UUID = pedido.path_params["ficha_id"]
        recusa: tuple[int, str] | None = None
        recusada: Operacao | None = None
        async with self.t.engine.begin() as c:
            op = await self._ficha_da_pessoa(c, ficha_id, sessao.pessoa.id)
            if op is None:
                recusa = FICHA_ALHEIA
            else:
                try:
                    async with c.begin_nested():
                        recusada = await fila.recusar_ficha(c, ficha_id, pessoa_id=sessao.pessoa.id)
                except DecisaoRecusada as erro:
                    recusa = recusa_legivel(erro)
        # A pagina sai depois da transacao (uma conexao do pool por pedido).
        if recusa is not None or recusada is None:
            status, mensagem = recusa or (409, "Nada mudou.")
            if status == 404:
                return self.t.erro(pedido, 404, "Ficha não encontrada", mensagem)
            return await self._pagina_ficha(pedido, ficha_id, erros=[mensagem], status=status)
        log.info("telas_ficha_recusada_pela_pessoa")
        return ver_depois(f"/telas/acoes/{recusada.id}", "recusada")

    # --- Minhas acoes ---

    @tela
    async def lista(self, pedido: Request) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        busca = pedido.query_params.get("protocolo", "").strip().upper()[:40]
        async with self.t.engine.connect() as c:
            if busca and protocolo_valido(busca):
                operacao_id = await consultas.operacao_do_protocolo(c, sessao.pessoa.id, busca)
                achada = await fila.por_id(c, operacao_id) if operacao_id else None
                ops = [achada] if achada is not None else []
            else:
                ops = await fila.da_pessoa(c, sessao.pessoa.id, limite=200)
                if busca:
                    ops = [op for op in ops if busca in op.protocolo]
        agora = datetime.now(UTC)
        return self.t.pagina(pedido, "acoes/lista.html", {"acoes": [montar(op, agora) for op in ops], "busca": busca})

    async def _da_pessoa(self, pedido: Request, operacao_id: uuid.UUID) -> Operacao | None:
        sessao = await self.t.exigir_sessao(pedido)
        async with self.t.engine.connect() as c:
            op = await fila.por_id(c, operacao_id)
        if op is None or op.pessoa_id != sessao.pessoa.id:
            return None
        return op

    def _nao_achada(self, pedido: Request) -> Response:
        return self.t.erro(pedido, 404, "Ação não encontrada", "Esta ação não é sua ou não existe.")

    async def _pagina_acao(
        self, pedido: Request, operacao_id: uuid.UUID, *, erros: list[str] | None = None, status: int = 200
    ) -> Response:
        op = await self._da_pessoa(pedido, operacao_id)
        if op is None:
            return self._nao_achada(pedido)
        return self.t.pagina(
            pedido,
            "acoes/acao.html",
            {
                "a": montar(op),
                "intervalo": SEGUNDOS_ENTRE_ATUALIZACOES,
                "aviso": self._aviso(pedido),
                "erros": erros or [],
            },
            status=status,
        )

    @tela
    async def acao(self, pedido: Request) -> Response:
        return await self._pagina_acao(pedido, pedido.path_params["operacao_id"])

    @tela
    async def estado(self, pedido: Request) -> Response:
        """So o relogio, para o htmx trocar sozinho enquanto a acao espera ou sai.

        Se o estado ja nao e o que a pagina mostra (``visto``), pede ao htmx que recarregue a pagina inteira:
        os botoes e o resultado mudam junto."""
        op = await self._da_pessoa(pedido, pedido.path_params["operacao_id"])
        if op is None:
            return self._nao_achada(pedido)
        visto = pedido.query_params.get("visto")
        if visto is not None and visto != str(op.estado) and pedido.headers.get("hx-request") == "true":
            return Response(status_code=200, headers={"HX-Refresh": "true"})
        return self.t.pagina(pedido, "acoes/_relogio.html", {"a": montar(op), "intervalo": SEGUNDOS_ENTRE_ATUALIZACOES})

    async def _mudar(self, pedido: Request, mudanca: str) -> Response:
        sessao = await self.t.exigir_sessao(pedido)
        f = await self.t.formulario(pedido, sessao)
        operacao_id: uuid.UUID = pedido.path_params["operacao_id"]
        if await self._da_pessoa(pedido, operacao_id) is None:
            return self._nao_achada(pedido)
        recusa: tuple[int, str] | None = None
        async with self.t.engine.begin() as c:
            try:
                async with c.begin_nested():
                    if mudanca == "cancelar":
                        await fila.cancelar(c, operacao_id, pessoa_id=sessao.pessoa.id)
                    elif mudanca == "seguir":
                        await fila.seguir_agora(c, operacao_id, pessoa_id=sessao.pessoa.id)
                    else:
                        await fila.decidir_nao_confirmada(
                            c,
                            operacao_id,
                            pessoa_id=sessao.pessoa.id,
                            decisao=f.texto("decisao"),
                            observacao=f.texto("observacao")[:MAXIMO_OBSERVACAO] or None,
                        )
            except DecisaoRecusada as erro:
                recusa = recusa_legivel(erro)
        if recusa is not None:
            status, mensagem = recusa
            log.info("telas_acao_recusada", mudanca=mudanca, status=status)
            if status == 404:
                return self._nao_achada(pedido)
            return await self._pagina_acao(pedido, operacao_id, erros=[mensagem], status=status)
        log.info("telas_acao_mudou", mudanca=mudanca)
        aviso = {"cancelar": "cancelada", "seguir": "seguir_agora"}.get(mudanca, "decisao_registrada")
        return ver_depois(f"/telas/acoes/{operacao_id}", aviso)

    @tela
    async def cancelar(self, pedido: Request) -> Response:
        return await self._mudar(pedido, "cancelar")

    @tela
    async def seguir(self, pedido: Request) -> Response:
        return await self._mudar(pedido, "seguir")

    @tela
    async def decidir(self, pedido: Request) -> Response:
        return await self._mudar(pedido, "decidir")
