"""Terceira rodada (nucleo), sem banco: a decisao da saida diante do cofre, frases e parametro novo.

- N1: trancado fecha na hora; sem resposta fecha na segunda conferencia seguida; destravado reabre so o que a
  conferencia do cofre fechou; fechada pelo ADMIN ou pela conferencia do banco, continua fechada. Sem cliente
  do cofre com ``saude()``, a porta nao confere nada.
- Rodada 3b: fechada porque o cofre nao respondia, reabre so com duas conferencias destravadas seguidas, e
  depois de 30 minutos destravado se ja abriu e fechou assim na ultima hora; fechada por outro motivo, o cofre
  so avisa (trancado e de volta, uma vez cada); antes de "enviando", a porta pergunta ao cofre.
- N2: a frase de "parada por bloqueio" tambem diz "pausa".
- N3: o motivo da saida fechada com o vigia mudo, em frases curtas e sem promessa.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from pydantic import ValidationError

from mcp_claro import continuidade
from mcp_claro.cofre import CofreIndisponivel, CofreTrancado, EstadoCofre
from mcp_claro.config.settings import Parametros
from mcp_claro.extrato.frases import frase
from mcp_claro.regras.bloqueios import quem_admin
from mcp_claro.regras.estado import EstadoSaida
from mcp_claro.saida import cofre
from mcp_claro.saida.cofre import (
    MOTIVO_FORA_DO_AR,
    MOTIVO_TRANCADO,
    QUEM,
    Decisao,
    Leitura,
    Passo,
    decidir,
)
from mcp_claro.saida.despachante import PortaDeSaida
from mcp_claro.saida.estados import FRASE, Estado
from mcp_claro.telas.acoes import FRASE_TELA, frase_do_estado

pytestmark = pytest.mark.anyio

ABERTA = EstadoSaida(True, "reaberta pelo ADMIN", quem_admin(uuid.uuid4()))
PELO_COFRE = EstadoSaida(False, MOTIVO_TRANCADO, QUEM)
PELO_COFRE_SEM_RESPOSTA = EstadoSaida(False, MOTIVO_FORA_DO_AR, QUEM)
PELO_ADMIN = EstadoSaida(False, "manutencao do sistema de chamados", quem_admin(uuid.uuid4()))
# O ADMIN fechou com o mesmo texto do cofre: quem conta e o autor.
PELO_ADMIN_COM_O_TEXTO_DO_COFRE = EstadoSaida(False, MOTIVO_TRANCADO, quem_admin(uuid.uuid4()))
PELA_CONTINUIDADE = EstadoSaida(False, "o vigia nao respondeu e nao houve sinal", continuidade.QUEM)


# --- N1: decisao --------------------------------------------------------------------------------------


def test_trancado_fecha_na_hora() -> None:
    assert QUEM == "rotina:cofre"
    assert decidir(Leitura.TRANCADO, 0, ABERTA) == Decisao(Passo.FECHAR, "o cofre esta trancado")


def test_sem_resposta_fecha_so_na_segunda_conferencia_seguida() -> None:
    assert decidir(Leitura.FORA_DO_AR, 1, ABERTA).passo is Passo.NADA
    assert decidir(Leitura.FORA_DO_AR, 2, ABERTA) == Decisao(Passo.FECHAR, "o cofre nao responde")
    assert decidir(Leitura.FORA_DO_AR, 5, ABERTA).passo is Passo.FECHAR


def test_destravado_reabre_so_o_que_o_cofre_fechou() -> None:
    assert decidir(Leitura.DESTRAVADO, 0, PELO_COFRE) == Decisao(Passo.REABRIR)
    assert decidir(Leitura.DESTRAVADO, 0, PELO_COFRE_SEM_RESPOSTA) == Decisao(Passo.REABRIR)
    assert decidir(Leitura.DESTRAVADO, 0, ABERTA).passo is Passo.NADA


def test_fechada_sem_resposta_reabre_so_quando_esta_pronta() -> None:
    """Fechada porque o cofre nao respondia: so reabre ``pronta``. Fechada porque estava trancado: na hora."""
    assert decidir(Leitura.DESTRAVADO, 0, PELO_COFRE_SEM_RESPOSTA, pronta=False).passo is Passo.NADA
    assert decidir(Leitura.DESTRAVADO, 0, PELO_COFRE, pronta=False) == Decisao(Passo.REABRIR)


def test_pronta_para_reabrir_e_instavel() -> None:
    assert cofre.DESTRAVADO_PARA_REABRIR == 2
    assert (cofre.JANELA_INSTAVEL, cofre.ESPERA_SE_INSTAVEL) == (3600.0, 1800.0)
    # Uma conferencia destravada so nao basta; duas seguidas bastam.
    assert not cofre.pronta_para_reabrir(1, 0.0, saida_instavel=False)
    assert cofre.pronta_para_reabrir(2, 15.0, saida_instavel=False)
    # Ja abriu e fechou assim na ultima hora: 30 minutos seguidos de cofre destravado.
    assert not cofre.pronta_para_reabrir(2, 15.0, saida_instavel=True)
    assert not cofre.pronta_para_reabrir(119, 1799.0, saida_instavel=True)
    assert cofre.pronta_para_reabrir(121, 1800.0, saida_instavel=True)
    assert not cofre.instavel(None)
    assert cofre.instavel(3599.0)
    assert not cofre.instavel(3600.0)


@pytest.mark.parametrize(
    "saida", [PELO_ADMIN, PELO_ADMIN_COM_O_TEXTO_DO_COFRE, PELA_CONTINUIDADE, EstadoSaida(False, "outro", QUEM)]
)
def test_fechada_por_outro_nao_reabre_nem_muda(saida: EstadoSaida) -> None:
    """Fechada pelo ADMIN, pela conferencia do banco ou com outro motivo: o cofre nao reabre nem refecha."""
    for leitura in Leitura:
        for avisado in (True, False, None):
            passo = decidir(leitura, 3, saida, avisado_trancado=avisado).passo
            assert passo not in (Passo.FECHAR, Passo.REABRIR), (leitura, avisado)


@pytest.mark.parametrize("saida", [PELO_ADMIN, PELA_CONTINUIDADE])
def test_fechada_por_outro_avisa_o_cofre_trancado_e_a_volta_uma_vez_cada(saida: EstadoSaida) -> None:
    """``avisado_trancado``: o ultimo aviso deste fechamento foi o de trancado (True), outro ou nenhum (False), ou
    a porta ainda nao sabe (None, decide de novo com a trava e o extrato)."""
    trancado = Decisao(Passo.AVISAR_TRANCADO, MOTIVO_TRANCADO)
    voltou = Decisao(Passo.AVISAR_DESTRAVADO, "o cofre voltou destravado")
    assert decidir(Leitura.TRANCADO, 0, saida, avisado_trancado=False) == trancado
    assert decidir(Leitura.TRANCADO, 0, saida, avisado_trancado=None) == trancado
    assert decidir(Leitura.TRANCADO, 0, saida, avisado_trancado=True) == cofre.NADA
    assert decidir(Leitura.DESTRAVADO, 0, saida, avisado_trancado=True) == voltou
    assert decidir(Leitura.DESTRAVADO, 0, saida, avisado_trancado=None) == voltou
    assert decidir(Leitura.DESTRAVADO, 0, saida, avisado_trancado=False) == cofre.NADA
    # Sem resposta, com a saida ja fechada por outro motivo: nada a dizer.
    for avisado in (True, False, None):
        assert decidir(Leitura.FORA_DO_AR, 5, saida, avisado_trancado=avisado) == cofre.NADA


def test_fechada_pelo_cofre_sem_resposta_passa_a_trancado_uma_vez() -> None:
    assert decidir(Leitura.TRANCADO, 0, PELO_COFRE_SEM_RESPOSTA) == Decisao(Passo.FECHAR, MOTIVO_TRANCADO)
    # Ja fechada como trancado: nada de novo (um aviso por mudanca).
    assert decidir(Leitura.TRANCADO, 0, PELO_COFRE).passo is Passo.NADA
    assert decidir(Leitura.FORA_DO_AR, 4, PELO_COFRE).passo is Passo.NADA


class Saude:
    def __init__(self, resposta: EstadoCofre | Exception) -> None:
        self.resposta = resposta

    async def saude(self) -> EstadoCofre:
        if isinstance(self.resposta, Exception):
            raise self.resposta
        return self.resposta


@pytest.mark.parametrize(
    ("resposta", "leitura"),
    [
        (EstadoCofre(inicializado=True, trancado=False, versao="2.7.0"), Leitura.DESTRAVADO),
        (EstadoCofre(inicializado=True, trancado=True, versao="2.7.0"), Leitura.TRANCADO),
        # Ainda nao iniciado conta como trancado.
        (EstadoCofre(inicializado=False, trancado=False, versao="2.7.0"), Leitura.TRANCADO),
        (CofreTrancado("trancado"), Leitura.TRANCADO),
        (CofreIndisponivel("sem rede"), Leitura.FORA_DO_AR),
        (RuntimeError("cliente fechado"), Leitura.FORA_DO_AR),
    ],
)
async def test_leitura_da_saude(resposta: EstadoCofre | Exception, leitura: Leitura) -> None:
    assert await cofre.ler(Saude(resposta)) is leitura


class CifraTrancada:
    async def cifrar(self, texto: bytes, contexto: bytes) -> str:
        raise CofreTrancado("trancado")

    async def decifrar(self, cifrado: str, contexto: bytes) -> bytes:
        raise CofreTrancado("trancado")


class Linha:
    cifrada = "vault:v1:abc"


class Resultado:
    def first(self) -> Linha:
        return Linha()


class Conexao:
    async def __aenter__(self) -> Conexao:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def execute(self, *_: object, **__: object) -> Resultado:
        return Resultado()


class Engine:
    """So o que ``_chave`` usa de um engine: a leitura da chave cifrada (que a cifra trancada nao abre)."""

    def connect(self) -> Conexao:
        return Conexao()


def _op_e_ferramenta() -> tuple[Any, Any]:
    sistema = type("Sistema", (), {"id": uuid.uuid4(), "tipo_chave": "por_aplicativo"})()
    ferramenta = type("Ferramenta", (), {"sistema": sistema})()
    op = type("Op", (), {"aplicativo_id": uuid.uuid4(), "pessoa_id": uuid.uuid4()})()
    return op, ferramenta


@pytest.mark.parametrize("com_cliente_do_cofre", [True, False])
async def test_chave_com_cofre_trancado_pede_conferencia_na_hora(com_cliente_do_cofre: bool) -> None:
    saude = Saude(EstadoCofre(inicializado=True, trancado=True, versao="2.7.0")) if com_cliente_do_cofre else None
    porta = PortaDeSaida(Engine(), CifraTrancada(), cofre=saude)  # type: ignore[arg-type]
    porta._cofre_conferido_em = 1e18  # conferido agora mesmo: so a chamada trancada antecipa a proxima
    op, ferramenta = _op_e_ferramenta()
    segredo, aberta, motivo = await porta._chave(op, ferramenta)
    assert (segredo, aberta, motivo) == (None, None, "o cofre de chaves esta trancado")
    # Com o cliente do cofre, a proxima volta fecha a saida sem esperar o intervalo; sem ele, nada muda.
    assert porta._hora_de_conferir_o_cofre() is com_cliente_do_cofre


async def test_sem_cliente_do_cofre_a_porta_nao_confere() -> None:
    porta = PortaDeSaida(Engine(), CifraTrancada())  # type: ignore[arg-type]
    assert porta._hora_de_conferir_o_cofre() is False
    assert await porta.conferir_o_cofre() == cofre.NADA
    assert await porta._cofre_deixa_seguir() is None


@pytest.mark.parametrize(
    ("resposta", "esperado", "fecha_na_proxima", "espera_a_varredura"),
    [
        (EstadoCofre(inicializado=True, trancado=False, versao="2.7.0"), None, False, False),
        (EstadoCofre(inicializado=True, trancado=True, versao="2.7.0"), True, True, False),
        (CofreIndisponivel("sem rede"), False, False, True),
    ],
)
async def test_pergunta_ao_cofre_antes_de_enviando(
    resposta: EstadoCofre | Exception, esperado: bool | None, fecha_na_proxima: bool, espera_a_varredura: bool
) -> None:
    """Destravado: segue. Trancado: a acao nao segue e a proxima volta fecha a saida. Sem resposta: a porta espera."""
    porta = PortaDeSaida(Engine(), CifraTrancada(), cofre=Saude(resposta))  # type: ignore[arg-type]
    porta._cofre_conferido_em = 1e18
    assert await porta._cofre_deixa_seguir() is esperado
    assert porta._hora_de_conferir_o_cofre() is fecha_na_proxima
    assert porta._cofre_mudo_no_envio is espera_a_varredura


def test_intervalo_da_conferencia_do_cofre() -> None:
    assert Parametros().VAULT_CHECK_SECONDS == 15
    assert Parametros(VAULT_CHECK_SECONDS=5).VAULT_CHECK_SECONDS == 5
    assert Parametros(VAULT_CHECK_SECONDS=300).VAULT_CHECK_SECONDS == 300
    for valor in (4, 301, 0):
        with pytest.raises(ValidationError):
            Parametros(VAULT_CHECK_SECONDS=valor)


def test_intervalo_vem_do_ambiente(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VAULT_CHECK_SECONDS", "40")
    assert Parametros().VAULT_CHECK_SECONDS == 40


def test_frases_do_cofre_no_extrato() -> None:
    assert frase("cofre_fechou_saida", motivo=MOTIVO_TRANCADO) == (
        "A porta de saida fechou porque o cofre esta trancado. Nenhuma acao sai para os sistemas; as ja liberadas"
        " esperam."
    )
    assert "reabriu sozinha" in frase("cofre_reabriu_saida")
    assert frase("cofre_trancado_saida_fechada") == (
        "O cofre esta trancado. A porta de saida ja estava fechada por outro motivo e continua fechada. Quando o"
        " cofre voltar, ela nao reabre sozinha."
    )
    assert frase("cofre_destravado_saida_fechada") == (
        "O cofre voltou destravado. A porta de saida continua fechada, porque foi fechada por outro motivo."
    )


# --- N2: parada por bloqueio ou pausa -------------------------------------------------------------------


def test_parada_por_bloqueio_tambem_diz_pausa() -> None:
    assert FRASE[Estado.PARADA_POR_BLOQUEIO] == "Parada por um bloqueio ou uma pausa. Nada seguiu."
    assert FRASE_TELA[Estado.PARADA_POR_BLOQUEIO] == "Parada por um bloqueio ou uma pausa. Nada seguiu."
    assert frase_do_estado(Estado.PARADA_POR_BLOQUEIO) == FRASE_TELA[Estado.PARADA_POR_BLOQUEIO]


# --- N3: motivo com o vigia mudo ------------------------------------------------------------------------


def test_motivo_do_vigia_mudo_em_frases_curtas_sem_promessa() -> None:
    texto = continuidade.SEM_SINAL_DE_RESTAURACAO
    assert texto == (
        "nao houve sinal de banco restaurado. A fila de saida nao parou. Quando a saida reabrir, o que espera volta"
        " a ser conferido antes de sair. A acao de um aplicativo que continua pausado fica parada e nao sai"
    )
    for trecho in (":", ";", "na hora dela", "para sem sair"):
        assert trecho not in texto
    linha = frase("continuidade_nao_confere", motivo=f"o vigia nao respondeu e {texto}")
    assert ".." not in linha
    assert linha.startswith("A conferencia do banco na partida nao bateu: o vigia nao respondeu e nao houve sinal")
