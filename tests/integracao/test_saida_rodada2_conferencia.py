"""Segunda rodada, depois da conferencia (nucleo), contra o PostgreSQL real com o usuario de cada papel.

Cada teste falha sem a correcao: o vigia sem resposta (o motivo diz o que acontece ao reabrir com o aplicativo
pausado, o desfecho diz que o aplicativo esta pausado e o protocolo salta), a resposta tardia depois de uma
retomada que tenta de novo, a marca da retomada por padrao e o caractere nulo nos argumentos do aplicativo.

Mesmo arranjo de ``test_saida_disputas``: o app do despachante sobe sem a porta, e cada teste usa
``PortaDeSaida`` direto.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import text

from mcp_claro import continuidade
from mcp_claro.saida import despachante as despachante_mod
from mcp_claro.saida import fila
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.pedidos import RECUSA_NULO, AcaoRecusada, Pedido
from mcp_claro.saida.protocolo import novo_protocolo
from tests.integracao.test_autorizacao import Ambiente
from tests.integracao.test_saida import chamadas, chamar, operacao
from tests.integracao.test_saida_correcoes import extrato, pedidos_de
from tests.integracao.test_saida_disputas import amb, nova_acao, porta, reativar, vitrine
from tests.integracao.test_saida_rodada2 import falha_do_banco, situacao_da_saida, tipos_depois, ultimo_id_do_extrato

__all__ = ["amb"]

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

MOTIVO_VIGIA_MUDO = (
    "o vigia nao respondeu; sem sinal de banco restaurado, a fila de saida nao parou: com a saida reaberta, a acao"
    " de aplicativo reativado sai na hora dela, e a de aplicativo ainda pausado para sem sair"
)


def numero(protocolo: str) -> int:
    return int(protocolo.rsplit("-", 1)[1])


async def numero_da_entrada(amb: Ambiente) -> int:
    """Um numero de protocolo novo, com o usuario do banco da entrada (o mesmo que registra as acoes)."""
    async with amb.entrada_db.begin() as c:
        return numero(await novo_protocolo(c))


async def conferida(amb: Ambiente) -> None:
    """Partida anterior conferida: a ultima linha da continuidade e a do banco como esta, e a saida aberta."""
    base = await continuidade.conferir_continuidade(amb.despachante_db, 0, exigir_vigia=False)
    await reativar(amb, base.aplicativos_pausados)
    if not await situacao_da_saida(amb):
        assert await continuidade.reabrir_saida(amb.entrada_db, f"admin:{amb.admin_id}", "Arranjo do teste")


# --- N4: vigia sem resposta, reabrir com o aplicativo ainda pausado ---------------------------------


async def test_vigia_mudo_e_reabrir_com_o_aplicativo_pausado_para_com_o_desfecho_do_aplicativo(
    amb: Ambiente,
) -> None:
    pausados: tuple[uuid.UUID, ...] = ()
    try:
        await conferida(amb)
        liberada = await nova_acao(amb, f"Vigia mudo, aplicativo pausado {uuid.uuid4().hex[:6]}")
        antes_do_protocolo = await numero_da_entrada(amb)
        desde = await ultimo_id_do_extrato(amb)
        antes = chamadas(amb, "criar_chamado")

        r = await continuidade.conferir_continuidade(amb.despachante_db, None, exigir_vigia=True)
        pausados = r.aplicativos_pausados
        assert r.motivos == ("vigia_sem_resposta",)
        assert amb.aplicativo_id in pausados

        # O motivo diz o que acontece de verdade ao reabrir, no extrato e na saida fechada.
        async with amb.admin.connect() as c:
            linha = (
                await c.execute(
                    text("SELECT frase, dados FROM extrato WHERE tipo = 'continuidade_nao_confere' AND id > :i"),
                    {"i": desde},
                )
            ).one()
            saida_motivo = (await c.execute(text("SELECT motivo FROM estado_saida WHERE id = 1"))).scalar_one()
        assert saida_motivo == MOTIVO_VIGIA_MUDO
        assert MOTIVO_VIGIA_MUDO in linha.frase
        assert "segue quando a saida reabrir" not in linha.frase
        assert (linha.dados["fila_parada"], linha.dados["protocolo_saltou"]) == (False, True)
        tipos = await tipos_depois(amb, desde)
        assert "intervalo_perdido" in tipos
        assert "fila_parada_restauracao" not in tipos

        # Sem o vigia nao da para saber se o banco voltou de uma copia sem a marca: o protocolo salta.
        assert await numero_da_entrada(amb) >= antes_do_protocolo + fila.SALTO_NA_RESTAURACAO + 1

        # O ADMIN reabre sem reativar o aplicativo (reabrir nao reativa): a acao para, sem sair, e o desfecho
        # diz por que.
        assert (await operacao(amb, liberada.protocolo)).estado == Estado.LIBERADA
        assert await continuidade.reabrir_saida(amb.entrada_db, f"admin:{amb.admin_id}", "Vigia conferido")
        assert await porta(amb).enviar_uma() is True
        parada = await operacao(amb, liberada.protocolo)
        assert (parada.estado, parada.desfecho) == (Estado.PARADA_POR_BLOQUEIO, "o aplicativo esta pausado")
        linhas = await extrato(amb, "acao_parada", protocolo=liberada.protocolo)
        assert len(linhas) == 1
        assert "parada antes de sair: o aplicativo esta pausado." in linhas[0].frase
        assert chamadas(amb, "criar_chamado") == antes
    finally:
        if not await situacao_da_saida(amb):
            await continuidade.reabrir_saida(amb.entrada_db, f"admin:{amb.admin_id}", "Fim do teste")
        await reativar(amb, pausados)


async def test_pessoa_que_nao_esta_ativa_para_com_o_desfecho_da_pessoa(amb: Ambiente) -> None:
    acao = await nova_acao(amb, f"Pessoa desativada {uuid.uuid4().hex[:6]}", pessoa_id=amb.bia_id)
    antes = chamadas(amb, "criar_chamado")
    async with amb.admin.begin() as c:
        await c.execute(text("UPDATE pessoas SET situacao = 'desativada' WHERE id = :p"), {"p": amb.bia_id})
    try:
        assert await porta(amb).enviar_uma() is True
        parada = await operacao(amb, acao.protocolo)
        assert (parada.estado, parada.desfecho) == (Estado.PARADA_POR_BLOQUEIO, "a pessoa nao esta ativa")
        assert chamadas(amb, "criar_chamado") == antes
    finally:
        async with amb.admin.begin() as c:
            await c.execute(text("UPDATE pessoas SET situacao = 'ativa' WHERE id = :p"), {"p": amb.bia_id})


# --- Resto do N3: resposta tardia, depois de uma retomada, com o COMMIT sem confirmacao ----------------


async def test_resposta_tardia_que_tenta_de_novo_depois_da_retomada_grava_uma_vez(
    amb: Ambiente, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(despachante_mod, "ESPERA_DO_FIM", 0.01)
    op = await nova_acao(amb, f"Resposta tardia {uuid.uuid4().hex[:6]}")
    p = porta(amb)
    async with amb.despachante_db.begin() as c:
        assert await fila.marcar_enviando(c, op)
    assert op.protocolo in await p.assumir()  # retomada de verdade: a linha vira nao confirmada
    original = fila.finalizar
    tentativas: list[int] = []

    async def commit_sem_confirmacao(c: Any, *args: Any, **kwargs: Any) -> bool:
        tentativas.append(1)
        if len(tentativas) == 1:
            async with amb.despachante_db.begin() as outra:
                await original(outra, *args, **kwargs)
            raise falha_do_banco()
        return await original(c, *args, **kwargs)

    monkeypatch.setattr(fila, "finalizar", commit_sem_confirmacao)
    await p._gravar_fim(op, Estado.ENVIADA, {"content": []}, "o sistema respondeu")
    assert len(tentativas) == 2
    atual = await operacao(amb, op.protocolo)
    assert (atual.estado, atual.desfecho) == (
        Estado.NAO_CONFIRMADA,
        "o sistema respondeu (resposta depois da retomada)",
    )
    linhas = await extrato(amb, "acao_enviada", protocolo=op.protocolo)
    assert len(linhas) == 1
    assert linhas[0].dados["gravado"] is False
    assert len(await extrato(amb, "acao_nao_confirmada", protocolo=op.protocolo)) == 1


# --- Marca da retomada por padrao ---------------------------------------------------------------------


async def test_retomar_enviando_sem_dizer_quem_grava_a_marca_da_retomada(amb: Ambiente) -> None:
    op = await nova_acao(amb, f"Retomada padrao {uuid.uuid4().hex[:6]}")
    async with amb.despachante_db.begin() as c:
        assert await fila.marcar_enviando(c, op)
        assert op.protocolo in await fila.retomar_enviando(c)
    async with amb.admin.connect() as c:
        mudou_por = (
            await c.execute(text("SELECT mudou_por FROM fila_saida WHERE id = :i"), {"i": op.fila_id})
        ).scalar_one()
    # "despachante" sozinho e so o do fim do envio, que ``finalizar`` usa para reconhecer o proprio fim.
    assert mudou_por == "despachante:retomada"


# --- Caractere nulo nos argumentos que vem do aplicativo -----------------------------------------------


async def contar_operacoes(amb: Ambiente) -> int:
    async with amb.admin.connect() as c:
        return int((await c.execute(text("SELECT count(*) FROM operacoes"))).scalar_one())


@pytest.mark.parametrize(
    "argumentos",
    [
        {"titulo": "antes\x00depois"},
        {"titulo": "ok", "chave\x00": "valor"},
        {"titulo": "ok", "itens": [{"nome": "dentro\x00"}]},
    ],
    ids=["valor", "chave", "aninhado"],
)
async def test_argumento_com_caractere_nulo_e_recusado_antes_de_virar_protocolo(
    amb: Ambiente, argumentos: dict[str, Any]
) -> None:
    pedidos, aplicativo = await pedidos_de(amb)
    f = await vitrine(amb)
    antes = await contar_operacoes(amb)
    with pytest.raises(AcaoRecusada) as recusa:
        await pedidos.pedir(Pedido(amb.ana_id, aplicativo, f, argumentos, None))
    assert (recusa.value.motivo, recusa.value.mensagem) == ("caractere_nulo", RECUSA_NULO)
    assert "Nada foi enviado." in recusa.value.mensagem
    assert await contar_operacoes(amb) == antes


async def test_aplicativo_que_manda_caractere_nulo_recebe_a_recusa_legivel(amb: Ambiente) -> None:
    antes = await contar_operacoes(amb)
    desde = await ultimo_id_do_extrato(amb)
    with pytest.raises(MCPError) as erro:
        await chamar(amb, "criar_chamado", {"titulo": "com\x00nulo"})
    assert "caractere nulo" in str(erro.value)
    assert await contar_operacoes(amb) == antes
    async with amb.admin.connect() as c:
        frases = list(
            (
                await c.execute(
                    text("SELECT frase FROM extrato WHERE tipo = 'acao_recusada' AND id > :i"), {"i": desde}
                )
            ).scalars()
        )
    assert len(frases) == 1
    assert frases[0].endswith(": caractere_nulo.")
