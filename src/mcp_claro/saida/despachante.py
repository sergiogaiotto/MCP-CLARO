"""Despachante unico da porta de saida.

Uma copia do papel por vez segura a trava de sessao ``DESPACHANTE_UNICO`` numa conexao propria, fora do
pool (a conexao e descartada no fim, e a trava vai junto). Ao assumir, toda acao que ficou "enviando"
vira "nao confirmada": pode ter chegado ao sistema, e nada e reenviado.

Para cada acao liberada e na hora:

1. travas consultivas compartilhadas do alcance (tudo, sistema, aplicativo, pessoa) e a linha da fila;
2. reconferencia: saida aberta, pessoa e aplicativo ativos, sem bloqueio, cadastro do programa valendo
   (quando o aplicativo e um programa), ordem e ferramenta vigentes, ficha confirmada para esta operacao
   (a de risco alto, com passkey ou login refeito) ou limite gasto e nao encerrado, e codigo de conferencia
   igual ao que vai seguir. Servidor de terceiros: so com a chave da Fase 3 ligada (``terceiros_ligados``) ou
   pessoa de teste, e so com a ferramenta publicada na revisao vigente do servidor; senao a acao e cancelada
   (``MOTIVO_TERCEIROS``), e com o servidor em quarentena, cancelada com ``cadastro.quarentena.MOTIVO_PARADA``;
3. com o cliente do cofre, pergunta a saude dele: trancado ou sem resposta, a acao nao segue agora e continua
   liberada (nenhuma chave abriria);
4. marca "enviando" e grava, antes de qualquer byte sair (o que o envio le do banco e lido antes);
5. abre a chave (renovada antes, se for por pessoa) e faz exatamente uma chamada ao sistema, sempre por
   ``ligacao(sistema)``: proprio pelo cliente fixado, terceiro pela sala de visitas (sem sala, o pedido MCP nao
   sai; a renovacao da chave, antes, vai ao endereco de troca de chave do terceiro, conferido antes). Argumentos
   com niveis demais para o kit escrever (``TETO_NIVEIS_PEDIDO``) nao saem por nenhum caminho: "nao saiu", sem
   abrir a chave nem chamar a ligacao;
6. grava o fim: enviada, nao saiu, sistema pediu dados ou nao confirmada. Nunca reenvia. Num terceiro, o que a
   sala barrou (sem sala, endereco que nao e publico, desvio nao seguido) ganha antes a sua linha no extrato.
   O que o sistema pediu no meio da chamada so entra no desfecho com o nosso vocabulario (o mesmo da leitura:
   ``ligacao.ponto.pedidos_legiveis``), e as categorias vao a ``dados.pedidos`` da linha do fim. O caractere nulo
   (U+0000), que o banco nao guarda, vira U+FFFD no resultado. Se o banco falhar ao gravar o fim, tenta de
   novo algumas vezes; se nao der, encerra o turno: a conexao da trava cai, quem assumir a porta (esta mesma,
   na volta seguinte, ou outra copia) da a linha "enviando" como nao confirmada, e nada e reenviado.

O despachante acorda pelo ``LISTEN mcp_claro_saida`` e, de reserva, por varredura. Com a porta de saida
fechada, so olha de novo a cada varredura.

Com o cliente do cofre (``cofre``, que tem ``saude()``), o despachante que segura a trava confere tambem a
saude do cofre a cada ``conferir_cofre`` segundos (``VAULT_CHECK_SECONDS``), e de novo assim que a pergunta do
passo 3 ou uma chamada ao cofre responde que ele esta trancado: trancado, a saida fecha; destravado de novo,
reabre se foi o cofre que a fechou (``saida.cofre``, que tambem diz quando, e os avisos ao ADMIN). A acao
liberada espera a saida reabrir, sem mudar de estado. So o cofre que tranca ou para de responder entre a
pergunta do passo 3 e a chave do passo 5 (uma fracao de segundo) faz a acao daquele instante virar "nao saiu"
("o cofre de chaves esta trancado" ou "o cofre de chaves nao respondeu"). Sem esse cliente (testes, cifra
local), nada disso roda.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import datetime
from typing import Any, Final

import mcp_types as types
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from mcp_claro.cadastro import repositorio
from mcp_claro.cadastro.modelos import FerramentaVitrine, Sistema
from mcp_claro.cadastro.quarentena import MOTIVO_PARADA
from mcp_claro.cofre import CofreIndisponivel, CofreTrancado
from mcp_claro.despachante import chaves, medida, renovacao
from mcp_claro.despachante.chaves import Cifra
from mcp_claro.despachante.cifra_origem import ContextoSemSistema
from mcp_claro.extrato import registrar
from mcp_claro.ligacao import cliente, ponto
from mcp_claro.ligacao.ponto import Ligacao, linha_da_sala, pedidos_da_resposta, pedidos_legiveis
from mcp_claro.ligacao.sala_contrato import PEDIDO_FORA_DO_CONTRATO, SALA_FORA, SALA_SEM_VAGA
from mcp_claro.logs import logger
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.regras import bloqueios, estado, estranhos
from mcp_claro.saida import cofre as saida_cofre
from mcp_claro.saida import fila, travas
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.fila import Operacao
from mcp_claro.saida.protocolo import FUSO, codigo_conferencia
from mcp_claro.versao import VERSAO_MCP

log = logger("mcp_claro.saida.despachante")

CANAL = "mcp_claro_saida"

# Erros JSON-RPC que o sistema devolve antes de executar qualquer coisa: a acao nao aconteceu.
_ANTES_DE_EXECUTAR = {
    types.METHOD_NOT_FOUND,
    types.INVALID_PARAMS,
    types.INVALID_REQUEST,
    types.PARSE_ERROR,
    types.MISSING_REQUIRED_CLIENT_CAPABILITY,
}

Chamador = Callable[..., Awaitable[cliente.Resposta]]
Ligador = Callable[[Sistema], Ligacao]
"""Por onde falar com um sistema (``ligacao.ponto.ligacao_para`` com a sala montada no app do despachante)."""


def ligacao_sem_sala(sistema: Sistema) -> Ligacao:
    """Ligacao de reserva, quando o app nao da a sua: sistema proprio pelo cliente fixado; servidor de terceiros
    sem sala (nenhum pedido MCP sai: ``nao_enviado`` com ``SalaFora``). Nunca o cliente local para um terceiro."""
    return ponto.ligacao_para(sistema, ambiente="producao")


MOTIVO_TERCEIROS: Final = "servidores de terceiros desligados ou servidor sem revisao"
"""Desfecho da acao de terceiro cancelada na saida: a chave da Fase 3 desligada (e a pessoa nao e de teste) ou a
ferramenta fora da revisao vigente do servidor."""
_NAO_ENVIADO: Final[dict[str, str]] = {
    SALA_FORA: "a sala de visitas nao esta disponivel; o pedido nao chegou ao servidor de terceiros",
    cliente.ENDERECO_RECUSADO: (
        "o endereco do servidor de terceiros nao e publico ou nao usa https; o pedido nao chegou ao sistema"
    ),
    cliente.PEDIDO_FUNDO_DEMAIS: (
        "o pedido tem niveis demais, um dentro do outro, para o MCP-CLARO escrever; o pedido nao chegou ao sistema"
    ),
    PEDIDO_FORA_DO_CONTRATO: "o pedido nao cabe no contrato da sala de visitas; o pedido nao chegou ao sistema",
    # Contrato K7: as vagas da sala (de todos os terceiros juntos) ocupadas por mais que o prazo da vaga.
    SALA_SEM_VAGA: "a sala de visitas estava cheia; nada saiu",
}
"""Desfecho de "nao saiu" por ``Resposta.excecao``; as outras ficam com "o pedido nao chegou ao sistema"."""
RESULTADO_FUNDO_DEMAIS: Final = (
    "o sistema respondeu; a resposta tem partes demais umas dentro das outras e o MCP-CLARO nao a guardou"
)
"""Desfecho da acao enviada cuja resposta nao da para escrever, medir ou repassar (niveis demais): sem resultado."""
TETO_NIVEIS_RESULTADO: Final = 199
"""Niveis de listas e objetos, um dentro do outro, que o resultado guardado de uma acao pode ter: o mesmo teto da
leitura (``despachante.servico.TETO_NIVEIS_RESPOSTA``; o leitor de JSON do kit aceita 200 niveis numa mensagem,
contando o envelope). Mais fundo, nem o aplicativo leria a consulta pelo protocolo."""
TETO_NIVEIS_PEDIDO: Final = 255
"""Niveis de listas e objetos, um dentro do outro, que os argumentos de uma acao podem ter (o objeto dos argumentos
e o primeiro): o que o kit ainda escreve num pedido (``ligacao.cliente``: com um nivel a mais, o cliente fixado
devolve ``PEDIDO_FUNDO_DEMAIS``). Mais fundo, nada sai por caminho nenhum, e a porta nem abre a chave nem chama a
ligacao.

Pela sala de visitas, o limite e menor: o leitor do contrato da sala (``PedidoChamar``) para em cerca de 200
niveis. De uns 200 a 255 niveis, a acao de um terceiro tambem nao sai: "nao saiu", com "o pedido nao cabe no
contrato da sala de visitas" (a chave ja foi aberta, mas nada chegou ao terceiro). Acima de 255, mesmo que a porta
chamasse a ligacao, o ``ClienteSala`` devolveria "nao saiu" (``PEDIDO_FUNDO_DEMAIS``) sem abrir conexao."""

TENTATIVAS_DO_FIM = 4
"""Vezes que o despachante tenta gravar o fim de um envio antes de deixar a retomada decidir."""
ESPERA_DO_FIM = 0.25
"""Espera entre as tentativas (segundos, crescendo a cada uma)."""
APLICATIVO_PARADO: Final[dict[str, str]] = {
    "pausado": "o aplicativo esta pausado",
    "bloqueado": "o aplicativo esta bloqueado",
}
"""Desfecho da acao parada porque o aplicativo nao esta ativo (``aplicativos.situacao``)."""
NULO = "\x00"
TROCA_DO_NULO = "\ufffd"
"""O PostgreSQL nao guarda U+0000 em texto nem em jsonb: no resultado, ele vira U+FFFD (caractere de troca)."""


class FimNaoGravado(RuntimeError):
    """O fim do envio nao gravou depois de todas as tentativas. Encerra o turno da porta para a retomada decidir.

    A mensagem so diz a linha e o estado: o erro do banco pode trazer os valores do comando (o resultado)."""


def sem_nulo(valor: Any) -> tuple[Any, int]:
    """Troca U+0000 por U+FFFD em todo texto (chaves e valores, em qualquer profundidade). Devolve (valor, trocas)."""
    if isinstance(valor, str):
        trocas = valor.count(NULO)
        return (valor.replace(NULO, TROCA_DO_NULO), trocas) if trocas else (valor, 0)
    if isinstance(valor, dict):
        limpo: dict[Any, Any] = {}
        total = 0
        for chave, item in valor.items():
            nova_chave, n_chave = sem_nulo(chave)
            novo_item, n_item = sem_nulo(item)
            limpo[nova_chave] = novo_item
            total += n_chave + n_item
        return limpo, total
    if isinstance(valor, list):
        itens = [sem_nulo(item) for item in valor]
        return [item for item, _ in itens], sum(n for _, n in itens)
    return valor, 0


class PortaDeSaida:
    def __init__(
        self,
        engine: AsyncEngine,
        cifra: Cifra,
        *,
        prazo_envio: float = 30.0,
        varredura: float = 5.0,
        acoes_de_programas: bool = False,
        limite_exige_passkey: bool = True,
        terceiros_ligados: bool = False,
        ligacao: Ligador | None = None,
        ambiente: str = "producao",
        liberar_locais: bool = False,
        chamar: Chamador | None = None,
        cofre: saida_cofre.SaudeDoCofre | None = None,
        conferir_cofre: float = 15.0,
        emails_admin: Sequence[str] = (),
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        self.engine = engine
        self.cifra = cifra
        self.prazo_envio = prazo_envio
        self.varredura = varredura
        self.acoes_de_programas = acoes_de_programas
        self.limite_exige_passkey = limite_exige_passkey
        # Chave da Fase 3 (``FEATURE_PHASE3_THIRD_PARTY``): sem ela, so a acao de pessoa de teste sai para um
        # servidor de terceiros. A reconferencia da saida decide de novo, na hora do envio.
        self.terceiros_ligados = terceiros_ligados
        # Todo envio sai por ``ligacao(sistema)``: proprio pelo cliente fixado, terceiro pela sala de visitas.
        self.ligacao: Ligador = ligacao or ligacao_sem_sala
        # ``APP_ENV`` e, so em testes, endereco local na troca de chave OAuth de um terceiro: valem para a
        # renovacao da chave por pessoa antes do envio (``renovacao.chave_pronta``), como na leitura.
        self.ambiente = str(ambiente)
        self.liberar_locais = liberar_locais
        # So para testes: troca a chamada ao sistema (proprio ou terceiro) por uma funcao com a assinatura de
        # ``Ligacao.chamar_ferramenta``. O app do despachante nunca passa.
        self.chamar: Chamador | None = chamar
        # Cliente do cofre com saude(): o app do despachante o liga na partida, depois de entrar no cofre.
        self.cofre = cofre
        self.conferir_cofre = conferir_cofre
        self.emails_admin = tuple(emails_admin)
        self._relogio = relogio
        self.assumiu = asyncio.Event()
        self._acordar = asyncio.Event()
        # A porta de saida estava fechada na ultima volta: a espera e a varredura inteira.
        self._saida_fechada = False
        # Linhas puladas nesta volta porque outra transacao as segura (cancelar, confirmar, bloquear).
        self._puladas: set[int] = set()
        # Conferencia do cofre: quando foi a ultima e as leituras seguidas (sem resposta; destravado, desde quando).
        self._cofre_conferido_em: float | None = None
        self._cofre_sem_resposta = 0
        self._cofre_destravado = 0
        self._cofre_destravado_desde: float | None = None
        # Quando esta porta reabriu a saida fechada por "sem resposta", e se a saida fechada agora por "sem
        # resposta" tinha reaberto assim menos de uma hora antes (reabre so depois de
        # ``saida_cofre.ESPERA_SE_INSTAVEL``).
        self._cofre_reabriu_em: float | None = None
        self._cofre_instavel = False
        # Com a saida fechada por outro motivo: (hora do fechamento, o ultimo aviso foi o de cofre trancado). Evita
        # ler o extrato a cada conferencia.
        self._cofre_avisado: tuple[datetime | None, bool] | None = None
        # Uma chamada ao cofre (a pergunta antes de "enviando" ou a chave) acabou de responder "trancado": a
        # proxima volta fecha a saida sem esperar o intervalo. Sem resposta antes de "enviando": a porta espera.
        self._cofre_trancado_na_chamada = False
        self._cofre_mudo_no_envio = False

    # Ciclo

    async def rodar(self) -> None:
        """Roda ate ser cancelada. Sem a trava, espera a vez; com erro, recomeca depois da varredura."""
        while True:
            try:
                await self._turno()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("porta_de_saida_falhou")
            self.assumiu.clear()
            await asyncio.sleep(self.varredura)

    async def _turno(self) -> None:
        async with self.engine.connect() as dono:
            bruta = await dono.get_raw_connection()
            pg: Any = bruta.driver_connection
            try:
                if not await pg.fetchval("SELECT pg_try_advisory_lock($1)", travas.DESPACHANTE_UNICO):
                    return
                await pg.add_listener(CANAL, self._aviso)
                await self.assumir(dono)
                while True:
                    await pg.fetchval("SELECT 1")  # a trava segue nossa enquanto a conexao vive
                    if self._hora_de_conferir_o_cofre():
                        await self.conferir_o_cofre(dono)
                    if not await self.enviar_uma(dono):
                        await self._esperar()
            finally:
                # A conexao nao volta ao pool: com ela vai a trava de sessao e o LISTEN.
                await dono.invalidate()

    def _aviso(self, *_: object) -> None:
        self._acordar.set()

    async def _esperar(self) -> None:
        if self._saida_fechada or self._cofre_mudo_no_envio:
            # Nada sai com a porta fechada (ou com o cofre sem responder): as liberadas na hora esperam, sem
            # consultar o banco nem o cofre a cada instante.
            espera = self.varredura
            self._cofre_mudo_no_envio = False
        else:
            async with self.engine.connect() as c:
                falta = (
                    await c.execute(
                        text(
                            "SELECT extract(epoch FROM min(hora_saida) - now()) FROM fila_saida"
                            " WHERE estado = 'liberada'"
                        )
                    )
                ).scalar_one_or_none()
            espera = self.varredura if falta is None else max(0.0, min(self.varredura, float(falta)))
            if espera <= 0:
                espera = 0.05
        if self.cofre is not None and self._cofre_conferido_em is not None:
            # Acorda a tempo da proxima conferencia do cofre, mesmo com a saida fechada.
            falta_cofre = self._cofre_conferido_em + self.conferir_cofre - self._relogio()
            espera = max(0.05, min(espera, falta_cofre))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._acordar.wait(), timeout=espera)
        self._acordar.clear()

    @contextlib.asynccontextmanager
    async def _transacao(self, dono: AsyncConnection | None) -> AsyncIterator[AsyncConnection]:
        """Transacao na conexao que segura a trava (se houver): se ela cair, a trava some e a gravacao falha."""
        if dono is None:
            async with self.engine.begin() as c:
                yield c
        else:
            async with dono.begin():
                yield dono

    # Cofre

    def _hora_de_conferir_o_cofre(self) -> bool:
        if self.cofre is None:
            return False
        if self._cofre_trancado_na_chamada or self._cofre_conferido_em is None:
            return True
        return self._relogio() - self._cofre_conferido_em >= self.conferir_cofre

    def _contar(self, leitura: saida_cofre.Leitura, agora: float) -> None:
        self._cofre_sem_resposta = self._cofre_sem_resposta + 1 if leitura is saida_cofre.Leitura.FORA_DO_AR else 0
        if leitura is saida_cofre.Leitura.DESTRAVADO:
            if self._cofre_destravado == 0:
                self._cofre_destravado_desde = agora
            self._cofre_destravado += 1
        else:
            self._cofre_destravado = 0
            self._cofre_destravado_desde = None

    async def conferir_o_cofre(self, dono: AsyncConnection | None = None) -> saida_cofre.Decisao:
        """Confere a saude do cofre e fecha ou reabre a saida, ou avisa o ADMIN (``saida.cofre``). Sem cliente do
        cofre, nada faz.

        Depois de uma chamada ao cofre responder "trancado", vale essa resposta, sem perguntar de novo. A mudanca
        e gravada na conexao da trava (``dono``): se ela cair, nada muda."""
        if self.cofre is None:
            return saida_cofre.NADA
        if self._cofre_trancado_na_chamada:
            leitura = saida_cofre.Leitura.TRANCADO
        else:
            leitura = await saida_cofre.ler(self.cofre)
        agora = self._relogio()
        self._cofre_trancado_na_chamada = False
        self._cofre_conferido_em = agora
        self._contar(leitura, agora)
        pronta = saida_cofre.pronta_para_reabrir(
            self._cofre_destravado,
            agora - (self._cofre_destravado_desde if self._cofre_destravado_desde is not None else agora),
            saida_instavel=self._cofre_instavel,
        )
        async with self.engine.connect() as c:
            saida = await estado.ler_saida(c)
        avisado = self._cofre_avisado[1] if self._cofre_avisado and self._cofre_avisado[0] == saida.mudou_em else None
        # Sem mudanca nem aviso a fazer (o caso de quase todas as voltas), nada de trava exclusiva.
        previa = saida_cofre.decidir(leitura, self._cofre_sem_resposta, saida, pronta=pronta, avisado_trancado=avisado)
        if previa.passo is saida_cofre.Passo.NADA:
            return saida_cofre.NADA
        instavel = saida_cofre.instavel(None if self._cofre_reabriu_em is None else agora - self._cofre_reabriu_em)
        async with self._transacao(dono) as c:
            decisao, vista = await saida_cofre.aplicar(
                c,
                leitura,
                self._cofre_sem_resposta,
                emails_admin=self.emails_admin,
                papel="despachante",
                pronta=pronta,
                instavel_ao_fechar=instavel,
            )
        if saida_cofre.fechada_por_outro(vista) and leitura is not saida_cofre.Leitura.FORA_DO_AR:
            # O que o ADMIN ja sabe deste fechamento: as proximas conferencias nao leem o extrato de novo.
            self._cofre_avisado = (vista.mudou_em, leitura is saida_cofre.Leitura.TRANCADO)
        if decisao.passo is saida_cofre.Passo.FECHAR:
            self._saida_fechada = True
            if decisao.motivo == saida_cofre.MOTIVO_FORA_DO_AR:
                self._cofre_instavel = instavel
        elif decisao.passo is saida_cofre.Passo.REABRIR:
            self._saida_fechada = False
            if vista.motivo == saida_cofre.MOTIVO_FORA_DO_AR:
                # So conta a reabertura depois de "sem resposta": e ela que diz se a rede vai e volta.
                self._cofre_reabriu_em = agora
            self._cofre_instavel = False
        if decisao.passo is not saida_cofre.Passo.NADA:
            CONTADORES.somar("saida_pelo_cofre", passo=str(decisao.passo))
        return decisao

    async def _cofre_deixa_seguir(self) -> bool | None:
        """Pergunta ao cofre logo antes de "enviando". ``None``: pode seguir. ``True``: trancado (a proxima volta
        fecha a saida). ``False``: sem resposta (a porta espera; a conferencia conta as falhas e fecha na segunda
        seguida). Nos dois casos a acao continua liberada."""
        if self.cofre is None:
            return None
        leitura = await saida_cofre.ler(self.cofre)
        if leitura is saida_cofre.Leitura.TRANCADO:
            self._cofre_trancado_na_chamada = True
            return True
        if leitura is saida_cofre.Leitura.FORA_DO_AR:
            self._cofre_mudo_no_envio = True
            return False
        return None

    async def assumir(self, dono: AsyncConnection | None = None) -> list[str]:
        # Outra copia pode ter segurado a porta nesse meio tempo, conferido o cofre, fechado ou reaberto a saida e
        # avisado o ADMIN: o que esta porta lembrava (avisos, conferencias seguidas, chamada que respondeu trancado,
        # espera de 30 minutos) nao vale mais. Ela confere o cofre na hora, como uma porta nova, e rele o extrato.
        self._cofre_avisado = None
        self._cofre_conferido_em = None
        self._cofre_sem_resposta = 0
        self._cofre_destravado = 0
        self._cofre_destravado_desde = None
        self._cofre_trancado_na_chamada = False
        self._cofre_mudo_no_envio = False
        self._cofre_reabriu_em = None
        self._cofre_instavel = False
        async with self._transacao(dono) as c:
            retomadas = await fila.retomar_enviando(c, quem=fila.QUEM_RETOMA)
            await registrar(c, papel="despachante", tipo="despachante_assumiu", campos={"retomadas": len(retomadas)})
        self.assumiu.set()
        log.info("porta_de_saida_assumida", retomadas=len(retomadas))
        return retomadas

    # Uma acao

    async def enviar_uma(self, dono: AsyncConnection | None = None) -> bool:
        """Processa a proxima acao na hora. Devolve ``False`` quando nao ha o que fazer agora.

        ``dono`` e a conexao que segura a trava de despachante unico: a reconferencia e a marca "enviando"
        sao gravadas nela. O fim do envio vai por outra conexao, para a resposta ficar guardada mesmo se a
        trava cair no meio (ai ela so entra no extrato e na tela de decisao).
        """
        async with self.engine.connect() as c:
            self._saida_fechada = not (await estado.ler_saida(c)).aberta
            if self._saida_fechada:
                return False
            candidata = (
                await c.execute(
                    text(
                        "SELECT f.id, o.pessoa_id, o.aplicativo_id, o.sistema_id FROM fila_saida f"
                        " JOIN operacoes o ON o.id = f.operacao_id"
                        " WHERE f.estado = 'liberada' AND f.hora_saida <= now() AND NOT (f.id = ANY(:puladas))"
                        " ORDER BY f.hora_saida, f.id LIMIT 1"
                    ),
                    {"puladas": sorted(self._puladas)},
                )
            ).first()
        if candidata is None:
            # Volta completa: as puladas entram de novo na proxima, depois da espera.
            self._puladas.clear()
            return False
        # Com o cofre trancado ou sem resposta, nenhuma chave abriria: a acao nao vira "enviando" e espera.
        espera_o_cofre = await self._cofre_deixa_seguir()
        if espera_o_cofre is not None:
            return espera_o_cofre
        async with self._transacao(dono) as c:
            await travas.compartilhar_envio(c, candidata.pessoa_id, candidata.aplicativo_id, candidata.sistema_id)
            if not (await estado.ler_saida(c)).aberta:
                self._saida_fechada = True
                return False
            travada = (
                await c.execute(
                    text(
                        "SELECT id FROM fila_saida WHERE id = :i AND estado = 'liberada' AND hora_saida <= now()"
                        " FOR UPDATE SKIP LOCKED"
                    ),
                    {"i": candidata.id},
                )
            ).scalar_one_or_none()
            if travada is None:
                self._puladas.add(candidata.id)
                return True
            op = await fila.por_fila(c, candidata.id)
            if op is None:
                return True
            conferida = await self._conferir(c, op)
            if isinstance(conferida, tuple):
                estado_final, motivo = conferida
                await fila.parar(c, op, estado_final, motivo)
                CONTADORES.somar("acoes_paradas", motivo=motivo)
                return True
            ferramenta = conferida
            # O que o envio precisa do banco e lido aqui, antes de "enviando": uma falha nesta leitura deixa a
            # acao liberada, e nao em duvida.
            sondagem = (
                await c.execute(text("SELECT sondagem FROM sistemas WHERE id = :s"), {"s": ferramenta.sistema.id})
            ).scalar_one_or_none()
            declarado = await repositorio.esquema_declarado(c, ferramenta.sistema.id, ferramenta.nome_original)
            await fila.marcar_enviando(c, op)
        final, resultado, desfecho, pedidos = await self._enviar(op, ferramenta, sondagem, declarado)
        await self._gravar_fim(op, final, resultado, desfecho, pedidos=pedidos)
        CONTADORES.somar("acoes", estado=str(final), sistema=ferramenta.sistema.nome)
        return True

    async def _gravar_fim(
        self,
        op: Operacao,
        final: Estado,
        resultado: dict[str, Any] | None,
        desfecho: str,
        *,
        pedidos: Sequence[str] = (),
    ) -> None:
        """Grava o fim do envio, tentando de novo algumas vezes se o banco falhar. ``pedidos``: as categorias do que o
        sistema pediu no meio (``dados.pedidos`` da linha do fim).

        Antes, troca U+0000 por U+FFFD no resultado (o banco recusaria a gravacao todas as vezes). Sem conseguir
        gravar, levanta ``FimNaoGravado``: o turno da porta acaba, a conexao da trava cai, e quem assumir a porta
        da a linha "enviando" como nao confirmada (``retomar_enviando``). Nada e reenviado. Uma tentativa nova
        depois de um COMMIT que deu certo (e cuja confirmacao se perdeu) reconhece o proprio fim
        (``fila.finalizar``). O log nao leva o resultado nem o erro do banco (que pode trazer os valores do
        comando), so o tipo do erro."""
        if resultado is not None:
            resultado, trocas = sem_nulo(resultado)
            if trocas:
                log.warning("resultado_com_caractere_nulo", fila_id=op.fila_id, trocas=trocas)
        for tentativa in range(1, TENTATIVAS_DO_FIM + 1):
            try:
                async with self.engine.begin() as c:
                    await fila.finalizar(c, op, final, resultado, desfecho, pedidos=pedidos)
            except (SQLAlchemyError, OSError, TimeoutError) as erro:
                log.warning("fim_do_envio_falhou", fila_id=op.fila_id, tentativa=tentativa, erro=type(erro).__name__)
                if tentativa < TENTATIVAS_DO_FIM:
                    await asyncio.sleep(ESPERA_DO_FIM * tentativa)
            else:
                return
        log.error("fim_do_envio_nao_gravado", fila_id=op.fila_id, estado=str(final))
        raise FimNaoGravado(f"O fim do envio da linha {op.fila_id} ({final}) nao gravou; a retomada decide.") from None

    async def _conferir(self, c: Any, op: Operacao) -> FerramentaVitrine | tuple[Estado, str]:
        situacoes = (
            await c.execute(
                text(
                    "SELECT (SELECT situacao FROM pessoas WHERE id = :p) AS pessoa,"
                    " (SELECT teste FROM pessoas WHERE id = :p) AS teste,"
                    " (SELECT situacao FROM aplicativos WHERE id = :a) AS app,"
                    " (SELECT tipo FROM aplicativos WHERE id = :a) AS tipo_app"
                ),
                {"p": op.pessoa_id, "a": op.aplicativo_id},
            )
        ).one()
        if situacoes.pessoa != "ativa":
            return Estado.PARADA_POR_BLOQUEIO, "a pessoa nao esta ativa"
        if situacoes.app != "ativo":
            # Hoje so a conferencia do banco na partida (``continuidade``) pausa um aplicativo. O desfecho aparece
            # na tela da acao como detalhe.
            return Estado.PARADA_POR_BLOQUEIO, APLICATIVO_PARADO.get(situacoes.app, "o aplicativo nao esta ativo")
        vigentes = await bloqueios.ativos(c, pessoa_id=op.pessoa_id, aplicativo_id=op.aplicativo_id)
        bloqueio = vigentes.para(op.sistema_id)
        if bloqueio is not None:
            return Estado.PARADA_POR_BLOQUEIO, bloqueio.motivo_recusa()
        if situacoes.tipo_app == "programa":
            if not self.acoes_de_programas:
                return Estado.CANCELADA, "programas so leem nesta instalacao"
            cadastro = (
                await c.execute(
                    text(
                        "SELECT situacao, fim >= (now() AT TIME ZONE :fuso)::date AS no_prazo FROM programas"
                        " WHERE aplicativo_id = :a"
                    ),
                    {"a": op.aplicativo_id, "fuso": FUSO},
                )
            ).first()
            if cadastro is None or cadastro.situacao != "ativo" or not cadastro.no_prazo:
                return Estado.CANCELADA, "o cadastro do programa nao esta valendo (pausado, encerrado ou vencido)"
        # Servidor de terceiros so com a chave da Fase 3 ligada ou para pessoa de teste, e so com a ferramenta
        # publicada na revisao vigente do servidor (a mesma regra da vitrine, lida de novo aqui).
        terceiros = self.terceiros_ligados or bool(situacoes.teste)
        ferramenta = await repositorio.ferramenta_da_ordem(
            c,
            pessoa_id=op.pessoa_id,
            aplicativo_id=op.aplicativo_id,
            ordem_id=op.ordem_id,
            ferramenta_id=op.ferramenta_id,
            versao=VERSAO_MCP,
            terceiros=terceiros,
        )
        if ferramenta is None:
            return await self._sem_ferramenta(c, op, terceiros=terceiros)
        if ferramenta.classe == "le":
            return Estado.CANCELADA, "a ordem de servico ou a ferramenta mudou"
        sistema = ferramenta.sistema
        if (
            sistema.id != op.sistema_id
            or sistema.situacao != "ativo"
            or sistema.versao_mcp != VERSAO_MCP
            or sistema.tipo_chave == "compartilhada"
        ):
            return Estado.CANCELADA, "o sistema nao esta ativo para acoes"
        codigo = codigo_conferencia(sistema.id, ferramenta.nome_original, op.argumentos)
        if codigo != op.codigo_conferencia:
            return Estado.CANCELADA, "o codigo de conferencia nao bate"
        if op.ficha_id is not None:
            ficha = (
                await c.execute(
                    text(
                        "SELECT operacao_id, decisao, codigo_conferencia, exige_passkey, risco, confirmada_com"
                        " FROM fichas WHERE id = :f"
                    ),
                    {"f": op.ficha_id},
                )
            ).one()
            if (
                str(ficha.operacao_id) != str(op.id)
                or ficha.decisao != "confirmada"
                or ficha.codigo_conferencia != codigo
            ):
                return Estado.CANCELADA, "a ficha nao foi confirmada para este pedido"
            if (ficha.exige_passkey or ficha.risco == "alto") and ficha.confirmada_com not in (
                "passkey",
                "login_refeito",
            ):
                return Estado.CANCELADA, "a ficha de risco alto nao foi confirmada com passkey nem com login refeito"
        elif op.limite_id is not None:
            limite = (
                await c.execute(
                    text(
                        "SELECT pessoa_id, aplicativo_id, sistema_id, nome_original, destinatarios, com_passkey,"
                        " situacao FROM limites WHERE id = :l"
                    ),
                    {"l": op.limite_id},
                )
            ).first()
            if limite is not None and limite.situacao in ("encerrado", "suspenso"):
                # A pessoa encerrou (ou alguem suspendeu) o limite: o que ele liberou e ainda nao saiu nao sai.
                # Vencido pelo prazo depois de liberar ainda sai: o limite valia quando foi gasto.
                return Estado.CANCELADA, "o limite foi encerrado ou suspenso antes de a acao sair"
            if (
                limite is None
                or (limite.pessoa_id, limite.aplicativo_id, limite.sistema_id)
                != (op.pessoa_id, op.aplicativo_id, op.sistema_id)
                or limite.nome_original != ferramenta.nome_original
                or (self.limite_exige_passkey and not limite.com_passkey)
            ):
                return Estado.CANCELADA, "o limite nao cobre este pedido"
            # A mesma conferencia do gasto: "so estes destinatarios" com as chaves do esquema da ferramenta
            # revisada do limite, lidas nesta transacao (argumento fora do esquema nao sai pelo limite).
            propriedades = await fila.propriedades_da_ferramenta(c, limite.sistema_id, limite.nome_original)
            if not fila.cobre(limite.destinatarios, op.argumentos, propriedades=propriedades):
                return Estado.CANCELADA, "o limite nao cobre este pedido"
        else:
            return Estado.CANCELADA, "sem ficha nem limite"
        return ferramenta

    async def _sem_ferramenta(self, c: Any, op: Operacao, *, terceiros: bool) -> tuple[Estado, str]:
        """Por que a ferramenta da operacao saiu da vitrine: servidor em quarentena, terceiro sem a chave ou fora
        da revisao vigente, ou a ordem e a ferramenta que mudaram."""
        sistema = (
            await c.execute(
                text(
                    "SELECT s.origem, s.situacao, EXISTS (SELECT 1 FROM revisoes_servidor rs"
                    " JOIN ferramentas_revisadas f ON f.id = :f AND f.sistema_id = s.id"
                    " WHERE rs.id = s.revisao_servidor_id AND rs.sistema_id = s.id AND f.id = ANY (rs.publicadas)"
                    " AND f.definicao_declarada IS NOT NULL) AS publicada FROM sistemas s WHERE s.id = :s"
                ),
                {"s": op.sistema_id, "f": op.ferramenta_id},
            )
        ).first()
        if sistema is not None and sistema.situacao == "quarentena":
            return Estado.CANCELADA, MOTIVO_PARADA
        if sistema is not None and sistema.origem != "proprio" and not (terceiros and sistema.publicada):
            return Estado.CANCELADA, MOTIVO_TERCEIROS
        return Estado.CANCELADA, "a ordem de servico ou a ferramenta mudou"

    async def _chave(self, op: Operacao, ferramenta: FerramentaVitrine) -> tuple[str | None, Any, str | None]:
        """(segredo, chave aberta por pessoa, motivo de nao sair)."""
        sistema = ferramenta.sistema
        try:
            if sistema.tipo_chave == "por_pessoa":
                aberta = await renovacao.chave_pronta(
                    self.engine,
                    self.cifra,
                    sistema_id=sistema.id,
                    pessoa_id=op.pessoa_id,
                    ambiente=self.ambiente,
                    liberar_locais=self.liberar_locais,
                )
                return aberta.segredo, aberta, None
            async with self.engine.connect() as c:
                segredo = await chaves.abrir(c, self.cifra, sistema_id=sistema.id, aplicativo_id=op.aplicativo_id)
        except chaves.ChaveAusente:
            return None, None, "a pessoa ainda nao autorizou o sistema"
        except renovacao.AutorizacaoVencida:
            return None, None, "a autorizacao no sistema venceu"
        except renovacao.RenovacaoIndisponivel:
            return None, None, "nao deu para renovar a chave"
        except CofreTrancado:
            # O cofre trancou depois da pergunta de antes de "enviando" (uma fracao de segundo). A conferencia do
            # cofre fecha a saida na proxima volta, sem esperar o intervalo. Esta acao ja estava "enviando" e nao
            # sai: nenhuma chave abre com o cofre trancado.
            if self.cofre is not None:
                self._cofre_trancado_na_chamada = True
            log.warning("chave_com_cofre_trancado")
            return None, None, "o cofre de chaves esta trancado"
        except CofreIndisponivel:
            log.warning("chave_com_cofre_sem_resposta")
            return None, None, "o cofre de chaves nao respondeu"
        except ContextoSemSistema as erro:
            # A cifra por origem nao achou o sistema ou nao leu a origem agora (``OrigemIndisponivel``): nenhuma
            # chave abre, e nada foi guardado; a proxima acao busca de novo.
            log.warning("chave_indisponivel", erro=type(erro).__name__)
            return None, None, "nao deu para abrir a chave no cofre"
        except Exception:
            # O cofre respondeu e recusou (login vencido, acesso negado, cifrado invalido) ou outra falha.
            log.exception("chave_indisponivel")
            return None, None, "nao deu para abrir a chave no cofre"
        return segredo, None, None

    async def _enviar(
        self, op: Operacao, ferramenta: FerramentaVitrine, discover_json: Any, declarado: Any
    ) -> tuple[Estado, dict[str, Any] | None, str, tuple[str, ...]]:
        """Uma chamada so ao sistema. ``discover_json`` e ``declarado`` vem lidos antes de marcar "enviando".
        Devolve (estado, resultado, desfecho, pedidos do sistema no meio da chamada)."""
        sistema = ferramenta.sistema
        if medida.profundidade(op.argumentos) > TETO_NIVEIS_PEDIDO:
            # Nenhum caminho escreve este pedido (o cliente fixado e o da sala o devolveriam como "nao enviado").
            # Nada sai, nem a chave abre.
            log.warning("pedido_fundo_demais", fila_id=op.fila_id)
            return Estado.NAO_SAIU, None, _NAO_ENVIADO[cliente.PEDIDO_FUNDO_DEMAIS], ()
        segredo, aberta, sem_chave = await self._chave(op, ferramenta)
        if sem_chave is not None:
            return Estado.NAO_SAIU, None, sem_chave, ()

        async def obter() -> str | None:
            return segredo

        sondagem = json.dumps(discover_json) if isinstance(discover_json, dict) else discover_json
        chamar = self.chamar or self.ligacao(sistema).chamar_ferramenta
        try:
            resposta = await chamar(
                sistema.endereco,
                obter,
                sondagem,
                ferramenta.nome_original,
                op.argumentos,
                traceparent=None,
                prazo=self.prazo_envio,
                esquema_declarado=declarado,
            )
        except Exception:
            # Nao sabemos em que ponto parou: o pedido pode ter saido. Nunca reenvia.
            log.exception("envio_falhou")
            return Estado.NAO_CONFIRMADA, None, "falha inesperada durante o envio", ()
        fim = await self._classificar(op, ferramenta, resposta, aberta)
        await self._registrar_sala(op, sistema, resposta)
        return fim

    async def _registrar_sala(self, op: Operacao, sistema: Sistema, resposta: cliente.Resposta) -> None:
        """Linha do extrato (e contador) do que a sala barrou num terceiro, antes da linha do fim do envio. A acao
        nao saiu de qualquer jeito: se o banco falhar aqui, so a linha fica faltando (o log diz o tipo do erro).

        A sala cheia (``sala_sem_vaga``) so conta (``sala_sem_vaga{onde="acao"}``): ela nao tem linha propria no
        extrato, e o motivo vai na linha ``acao_nao_saiu`` do fim."""
        if sistema.de_terceiro and resposta.tipo == "nao_enviado" and resposta.excecao == SALA_SEM_VAGA:
            CONTADORES.somar(SALA_SEM_VAGA, onde="acao")
        tipo = linha_da_sala(resposta, sistema)
        if tipo is None:
            return
        try:
            async with self.engine.begin() as c:
                await registrar(
                    c,
                    papel="despachante",
                    tipo=tipo,
                    campos={"sistema": sistema.nome},
                    protocolo=op.protocolo,
                    pessoa_id=op.pessoa_id,
                    aplicativo_id=op.aplicativo_id,
                    sistema_id=sistema.id,
                    ferramenta=op.nome_vitrine,
                    dados={"onde": "acao", "status_http": list(resposta.status_http[-1:])},
                )
        except (SQLAlchemyError, OSError, TimeoutError) as erro:
            log.warning("linha_da_sala_nao_gravada", fila_id=op.fila_id, tipo=tipo, erro=type(erro).__name__)
        CONTADORES.somar(tipo, onde="acao")

    async def _classificar(
        self, op: Operacao, ferramenta: FerramentaVitrine, resposta: cliente.Resposta, aberta: Any
    ) -> tuple[Estado, dict[str, Any] | None, str, tuple[str, ...]]:
        """Estado final, resultado (com a medida, sem o ``_meta`` do sistema), desfecho e pedidos de uma chamada.

        O que o sistema pediu no meio (``Resposta.pedidos``) entra no desfecho so com o nosso vocabulario
        (``pedidos_legiveis``): nunca texto, link ou campo dele. Nada disso muda o estado. As categorias voltam
        tambem, para ``dados.pedidos`` da linha do fim (``fila.finalizar``)."""
        # O que o cliente (ou a sala) contou e o que o proprio resultado ou erro mostra: uma resposta sem a lista
        # nao esconde um ``InputRequiredResult`` nem um -32042.
        pedidos = pedidos_da_resposta(resposta)
        for categoria in pedidos:
            CONTADORES.somar("pedidos_do_sistema_barrados", tipo=categoria, onde="acao")
        estado_final, resultado, desfecho = await self._estado_final(op, ferramenta, resposta, aberta, pedidos)
        if pedidos and estado_final is not Estado.SISTEMA_PEDIU_DADOS:
            pediu = pedidos_legiveis(pedidos)
            if estado_final is Estado.ENVIADA:
                desfecho = f"{desfecho}; antes pediu {pediu} e o MCP-CLARO recusou"
            else:
                desfecho = f"{desfecho}; o sistema pediu {pediu} e o MCP-CLARO recusou"
        return estado_final, resultado, desfecho, pedidos

    async def _estado_final(
        self,
        op: Operacao,
        ferramenta: FerramentaVitrine,
        resposta: cliente.Resposta,
        aberta: Any,
        pedidos: tuple[str, ...],
    ) -> tuple[Estado, dict[str, Any] | None, str]:
        sistema = ferramenta.sistema
        if resposta.tipo == "resultado" and resposta.resultado is not None:
            pediu_dados = (
                Estado.SISTEMA_PEDIU_DADOS,
                None,
                f"o sistema pediu {pedidos_legiveis(pedidos)} no meio da acao",
            )
            if isinstance(resposta.resultado, types.InputRequiredResult):
                # Nada do pedido (mensagem, link, esquema) passa: so o que ele pediu, no nosso vocabulario.
                return pediu_dados
            try:
                bruto = resposta.resultado.model_dump(by_alias=True, exclude_none=True, mode="json")
                if bruto.get("resultType") == "input_required":
                    return pediu_dados  # defesa: o tipo do resultado e aberto no kit
                if medida.profundidade(bruto) > TETO_NIVEIS_RESULTADO:
                    raise medida.RespostaFundaDemais("resultado fundo demais para guardar e repassar")
                # A conta das partes sem texto e feita no resultado do sistema, antes da medida: o aviso de partes
                # retiradas sai da nossa conta (fora da moldura, em ``pedidos.resposta``), nunca de um bloco dele.
                retiradas = estranhos.retiradas_pela_medida(bruto)
                resultado = medida.aplicar(bruto, ferramenta.campos_pessoais, anotacoes=not sistema.de_terceiro)
            except (ValueError, RecursionError) as erro:
                # A resposta chegou inteira (a acao saiu), mas tem partes demais umas dentro das outras para
                # escrever (o kit nao escreve mais de uns 255 niveis), medir ou repassar (``TETO_NIVEIS_RESULTADO``).
                # Fica "enviada", sem o resultado: nunca erro interno, nunca "nao confirmada" pela retomada.
                log.warning("resultado_fundo_demais", fila_id=op.fila_id, erro=type(erro).__name__)
                return Estado.ENVIADA, None, RESULTADO_FUNDO_DEMAIS
            resultado.pop("resultType", None)
            resultado.pop("_meta", None)  # o _meta do sistema nunca passa adiante
            if retiradas:
                resultado["_meta"] = {fila.META_RETIRADAS: retiradas}
            desfecho = "o sistema respondeu com erro" if resultado.get("isError") else "o sistema respondeu"
            return Estado.ENVIADA, resultado, desfecho
        if resposta.tipo == "nao_enviado":
            return Estado.NAO_SAIU, None, _NAO_ENVIADO.get(resposta.excecao or "", "o pedido nao chegou ao sistema")
        if (
            resposta.tipo == "recusado_http"
            and resposta.status_http
            and cliente.recusado_por_status(resposta.status_http[-1])
        ):
            # So a recusa na porta (401, 403 ou desvio nao seguido, no ultimo status) e "nao saiu". O 401 marca a
            # chave por pessoa vencida; nenhum outro status vence a chave.
            if resposta.status_http[-1] == 401 and aberta is not None:
                try:
                    await renovacao.marcar_vencida(
                        self.engine, sistema_id=sistema.id, pessoa_id=op.pessoa_id, cifrada=aberta.cifrada
                    )
                except (SQLAlchemyError, OSError, TimeoutError) as erro:
                    # A acao nao saiu de qualquer jeito; so a proxima renovacao fica para depois.
                    log.warning("chave_vencida_nao_marcada", erro=type(erro).__name__)
            codigos = ",".join(str(s) for s in resposta.status_http) or "?"
            return Estado.NAO_SAIU, None, f"o sistema recusou na porta (HTTP {codigos})"
        if resposta.tipo == "erro_rpc" and resposta.erro is not None and resposta.erro.code in _ANTES_DE_EXECUTAR:
            return Estado.NAO_SAIU, None, f"o sistema recusou o pedido (codigo {resposta.erro.code})"
        if resposta.tipo == "erro_rpc":
            codigo = resposta.erro.code if resposta.erro is not None else "?"
            return Estado.NAO_CONFIRMADA, None, f"o sistema devolveu erro depois de receber (codigo {codigo})"
        return Estado.NAO_CONFIRMADA, None, "a resposta do sistema nao chegou inteira"
