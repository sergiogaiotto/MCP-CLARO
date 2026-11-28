"""Regras puras da consulta (``selo.consulta``) do retoque da etapa 3 (G3ar): a conta da retirada e a duvida a tirar.

- ``terminou_sem_resposta``: so a operacao que chegou ao maximo de tentativas que gastam, ou que tem resposta que
  encerra, entra na conta da retirada (critica B8, decisao 3 do coordenador); a que ainda esta na escada, a que parou
  em ``sistema_fora`` e a que acabou pelo tempo sem o maximo nao contam (``None``). Mata a mutacao N2 do verificador;
- ``_vale_perguntar``: a resposta ao aviso nao conta para a "enviada" em aberto (decisao 1 do coordenador, D2);
- ``fora_de_ativo``: a porta fechada nao vira ``sistema_fora`` (decisao 2 do coordenador).
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from mcp_claro.selo import consulta, contrato
from mcp_claro.versao import VERSAO_MCP

ENVIO = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def _linha(
    minutos: float, origem: str = "consulta", desfecho: str = "sem_resposta", estado: str | None = None
) -> contrato.LinhaInformada:
    if estado is not None and desfecho == "sem_resposta":
        desfecho = "respondeu"
    return contrato.LinhaInformada(origem, desfecho, estado, ENVIO + timedelta(minutes=minutos))


def test_operacao_ainda_na_escada_nao_conta_na_retirada() -> None:
    """Mutacao N2 (``terminou_sem_resposta`` devolve ``True`` para quem ainda esta na escada): com menos tentativas
    que gastam do que o maximo, ``None``, com qualquer quantidade de linhas que nao gastam."""
    for feitas in range(8):
        assert consulta.terminou_sem_resposta([_linha(m) for m in range(feitas)], 8) is None
    # Acabada pelo tempo, sem o maximo: sete tentativas e muitas sem a chave da pessoa; o sistema nao foi perguntado o
    # bastante, e a escada nao entra na conta.
    pelo_tempo = [*[_linha(m) for m in range(7)], *[_linha(10 + m, desfecho="sem_chave") for m in range(40)]]
    assert contrato.tentativas_da_consulta(pelo_tempo) == 7
    assert (
        contrato.proxima_consulta(pelo_tempo, enviado_em=ENVIO - timedelta(days=7), maximo=8, prazo_envio_segundos=20)
        is None
    )
    assert consulta.terminou_sem_resposta(pelo_tempo, 8) is None


@pytest.mark.parametrize(
    ("ultima", "esperado"),
    [
        (("sem_resposta", None), True),
        (("resposta_invalida", None), True),
        (("respondeu", "desconhecido"), True),
        (("respondeu", "em_andamento"), False),
        (("respondeu", "nao_recebida"), False),
    ],
)
def test_no_maximo_conta_pela_ultima_consulta(ultima: tuple[str, str | None], esperado: bool) -> None:
    desfecho, estado = ultima
    linhas = [*[_linha(m) for m in range(7)], contrato.LinhaInformada("consulta", desfecho, estado, ENVIO)]
    assert consulta.terminou_sem_resposta(linhas, 8) is esperado


def test_resposta_que_encerra_interrompe_e_sistema_fora_no_fim_nao_conta() -> None:
    assert consulta.terminou_sem_resposta([_linha(1, estado="concluida")], 8) is False
    # O maximo baixou (BADGE_LOOKUP_MAX_ATTEMPTS e parametro): oito tentativas e depois sistema_fora.
    parada = [*[_linha(m) for m in range(8)], _linha(9, desfecho="sistema_fora")]
    assert consulta.terminou_sem_resposta(parada, 8) is None
    # Conferencia final da etapa 3r: uma resposta ao aviso depois do sistema_fora nao devolve a operacao a conta.
    for estado in ("desconhecido", "parada", "concluida"):
        assert consulta.terminou_sem_resposta([*parada, _linha(10, "aviso", estado=estado)], 8) is None
    # A resposta ao aviso nao encerra: o "parada" do aviso no meio nao tira a operacao da conta (decisao 1).
    com_aviso = [_linha(0, "aviso", estado="parada"), *[_linha(1 + m) for m in range(8)]]
    assert consulta.terminou_sem_resposta(com_aviso, 8) is True


def test_enviada_em_aberto_pelo_recibo_ou_pela_consulta_e_nao_pelo_aviso() -> None:
    """Decisao 1 do coordenador: o ``parada`` do aviso sobre uma escrita em andamento nao encerra a duvida; a consulta
    seguinte ainda pergunta (e pega o sistema que escreveu mesmo assim)."""
    adiada = [_linha(0, "recibo", estado="em_andamento")]
    assert consulta._vale_perguntar("enviada", adiada)
    for estado in contrato.ESTADOS_FINAIS:
        assert consulta._vale_perguntar("enviada", [*adiada, _linha(2, "aviso", estado=estado)])
        assert not consulta._vale_perguntar("enviada", [*adiada, _linha(2, estado=estado)])
    assert not consulta._vale_perguntar("enviada", [_linha(0, "recibo", estado="concluida")])
    # So o aviso, sem recibo nem consulta: nada em aberto pelo sistema (nivel 2 "enviada" nao se pergunta).
    assert not consulta._vale_perguntar("enviada", [_linha(2, "aviso", estado="em_andamento")])
    assert consulta._vale_perguntar("nao_confirmada", [_linha(2, "aviso", estado="parada")])
    for fila in ("liberada", "enviando", "nao_saiu", "sistema_pediu_dados"):
        assert not consulta._vale_perguntar(fila, adiada)


def test_porta_fechada_nao_e_sistema_fora() -> None:
    """Decisao 2 do coordenador: com a porta fechada a consulta nao grava nada (``PortaFechada``, 409
    ``porta_fechada``); ``fora_de_ativo`` so olha o sistema."""
    assert list(inspect.signature(consulta.fora_de_ativo).parameters) == ["linha"]
    ativo = SimpleNamespace(situacao="ativo", versao_mcp=VERSAO_MCP, origem="proprio", revisao_servidor_id=None)
    assert consulta.fora_de_ativo(SimpleNamespace(**vars(ativo), revisao_do_teste=None)) is None
    quarentena = SimpleNamespace(**(vars(ativo) | {"situacao": "quarentena"}), revisao_do_teste=None)
    assert consulta.fora_de_ativo(quarentena) == "o sistema esta em quarentena"
    assert not issubclass(consulta.PortaFechada, consulta.NadaAPerguntar)
