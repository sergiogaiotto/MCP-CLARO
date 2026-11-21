"""Alerta dos tres sinais (Fase 3, G4), contra PostgreSQL real e pela entrada.

- ``regras.sinais`` no banco: o que cada ferramenta grava, dois sinais do mesmo tipo nao bastam, janela, soma pelo
  par pessoa e aplicativo, sinal de terceiro so com ``terceiros`` e sinal de ferramenta propria marcada sempre
  (correcao B7), e a maior janela igual a do gatilho ``sinais_guarda``.
- Pela entrada (``test_entrada_terceiros_apoio``): o sinal esta gravado quando ``call_tool`` volta; texto nosso nao
  grava sinal; sem gravar o sinal, a leitura nao sai; com o alerta, a acao vai para a ficha sem consultar nem gastar
  limite, a ficha guarda o retrato e o extrato tem a linha; risco alto e experiencia mantem a rota; as fichas do
  alerta contam no teto por hora; resultado de acao enviada (e a consulta dela) grava sinal.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import text

from mcp_claro.config.politica import POLITICA
from mcp_claro.config.settings import Parametros
from mcp_claro.entrada.servidor import Recusa
from mcp_claro.regras import sinais
from mcp_claro.saida import fila
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.pedidos import AcaoRecusada, Pedidos
from mcp_claro.versao import PREFIXO_META
from tests import apoio_terceiros as t3
from tests.integracao.test_entrada_terceiros_apoio import Mundo, resultado_do_sistema, resultado_nosso, subir_mundo

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


@pytest.fixture
async def mundo(banco_migrado: Parametros) -> AsyncIterator[Mundo]:
    async with subir_mundo(banco_migrado) as m:
        yield m


async def sinal(m: Mundo, pessoa: str, tipo: str, chave: str, *, app: str = "A", ha_minutos: int = 0) -> None:
    async with m.dono.begin() as c:
        await t3.gravar_sinal(
            c,
            pessoa_id=m.pessoas[pessoa],
            aplicativo_id=m.apps[app],
            tipo=tipo,
            ferramenta_id=m.f[chave].id,
            visto_em=datetime.now(UTC) - timedelta(minutes=ha_minutos),
        )


async def alerta(
    m: Mundo, pessoa: str = "ana", *, app: str = "A", janela: int = 240, terceiros: bool = True
) -> sinais.Alerta | None:
    async with m.entrada_db.connect() as c:
        return await sinais.alerta(
            c, pessoa_id=m.pessoas[pessoa], aplicativo_id=m.apps[app], janela_minutos=janela, terceiros=terceiros
        )


# --- No banco ---------------------------------------------------------------------------------------------------


async def test_cada_ferramenta_grava_os_sinais_que_ela_da(mundo: Mundo) -> None:
    m = mundo
    esperado = {
        "t_ler": ("texto_de_fora",),
        "t_ver": ("dado_privado", "texto_de_fora"),
        "t_enviar": ("texto_de_fora",),
        "p_cadastro": ("dado_privado",),
        "p_caixa": ("texto_de_fora",),
        "p_simples": (),
    }
    for chave, tipos in esperado.items():
        async with m.entrada_db.begin() as c:
            gravados = await sinais.registrar(
                c, pessoa_id=m.pessoas["bia"], aplicativo_id=m.apps["A"], ferramenta_id=m.f[chave].id
            )
        assert gravados == tipos, chave
    linhas = await m.sinais("bia")
    assert len(linhas) == 6
    assert {(linha.tipo, linha.ferramenta_id) for linha in linhas} == {
        (tipo, m.f[chave].id) for chave, tipos in esperado.items() for tipo in tipos
    }
    assert all(
        linha.sistema_id == m.f[c].sistema_id for linha in linhas for c in esperado if m.f[c].id == linha.ferramenta_id
    )


async def test_dois_sinais_do_mesmo_tipo_nao_bastam(mundo: Mundo) -> None:
    m = mundo
    await sinal(m, "ana", "texto_de_fora", "t_ler")
    await sinal(m, "ana", "texto_de_fora", "p_caixa")
    assert await alerta(m) is None
    await sinal(m, "bia", "dado_privado", "p_cadastro")
    await sinal(m, "bia", "dado_privado", "t_ver")
    assert await alerta(m, "bia") is None
    await sinal(m, "bia", "texto_de_fora", "t_ler")
    a = await alerta(m, "bia")
    assert a is not None
    assert [v.sinal for v in a.vistos] == ["texto_de_fora", "dado_privado"]


async def test_fora_da_janela_nao_conta(mundo: Mundo) -> None:
    m = mundo
    await sinal(m, "ana", "texto_de_fora", "t_ler", ha_minutos=10)
    await sinal(m, "ana", "dado_privado", "p_cadastro")
    assert await alerta(m, janela=5) is None
    a = await alerta(m, janela=15)
    assert a is not None
    # O alerta dura ate o mais velho dos dois sair da janela.
    assert a.ate == a.de_fora.visto_em + timedelta(minutes=15)


async def test_os_sinais_somam_pelo_par_pessoa_e_aplicativo(mundo: Mundo) -> None:
    m = mundo
    await sinal(m, "ana", "texto_de_fora", "t_ler", app="A")
    await sinal(m, "ana", "dado_privado", "p_cadastro", app="B")
    await sinal(m, "bia", "dado_privado", "p_cadastro", app="A")
    assert await alerta(m, "ana", app="A") is None
    assert await alerta(m, "ana", app="B") is None
    assert await alerta(m, "bia", app="A") is None
    await sinal(m, "ana", "dado_privado", "t_ver", app="A")
    assert await alerta(m, "ana", app="A") is not None
    assert await alerta(m, "ana", app="B") is None
    assert await alerta(m, "bia", app="A") is None


async def test_texto_de_fora_de_terceiro_so_conta_com_terceiros_e_o_de_propria_marcada_sempre(mundo: Mundo) -> None:
    m = mundo
    await sinal(m, "ana", "texto_de_fora", "t_ler")
    await sinal(m, "ana", "dado_privado", "p_cadastro")
    assert await alerta(m, terceiros=False) is None
    assert await alerta(m, terceiros=True) is not None
    # Correcao B7: o texto de fora de ferramenta propria marcada liga o alerta com a chave desligada.
    await sinal(m, "ana", "texto_de_fora", "p_caixa", ha_minutos=1)
    a = await alerta(m, terceiros=False)
    assert a is not None
    assert a.de_fora.ferramenta == m.f["p_caixa"].nome
    assert a.de_fora.sistema == m.proprio_nome
    # Com terceiros, vale o mais recente (o do terceiro).
    b = await alerta(m, terceiros=True)
    assert b is not None
    assert b.de_fora.ferramenta == m.f["t_ler"].nome


async def test_retrato_tem_so_nomes_do_cadastro_e_horas(mundo: Mundo) -> None:
    m = mundo
    await sinal(m, "ana", "texto_de_fora", "t_ler")
    await sinal(m, "ana", "dado_privado", "p_cadastro")
    a = await alerta(m)
    assert a is not None
    retrato = a.retrato()
    assert set(retrato) == {"janela_minutos", "vistos"}
    assert retrato["janela_minutos"] == 240
    assert [set(v) for v in retrato["vistos"]] == [{"sinal", "ferramenta", "sistema", "visto_em"}] * 2
    assert [(v["sinal"], v["ferramenta"], v["sistema"]) for v in retrato["vistos"]] == [
        ("texto_de_fora", m.f["t_ler"].nome, m.terceiro_nome),
        ("dado_privado", m.f["p_cadastro"].nome, m.proprio_nome),
    ]
    for v in retrato["vistos"]:
        datetime.fromisoformat(v["visto_em"])


async def test_maior_janela_e_a_do_gatilho_e_a_da_politica(mundo: Mundo) -> None:
    async with mundo.dono.connect() as c:
        corpo = (await c.execute(text("SELECT pg_get_functiondef('sinais_guarda'::regproc)"))).scalar_one()
    assert re.findall(r"interval '(\d+) minutes'", corpo) == [str(sinais.MAIOR_JANELA_MINUTOS)]
    assert POLITICA["THREE_SIGNALS_WINDOW_MINUTES"].maximo == sinais.MAIOR_JANELA_MINUTOS


# --- Pela entrada: leitura ---------------------------------------------------------------------------------------


async def test_leitura_de_terceiro_com_campos_pessoais_da_os_dois_sinais_ja_gravados_quando_a_chamada_volta(
    mundo: Mundo,
) -> None:
    m = mundo
    assert await m.sinais("ana") == []
    r = await m.chamar("ana", "t_ver", {"texto": "oi"})
    assert not r.is_error
    # Logo que o call_tool volta, os dois sinais ja estao confirmados no banco.
    linhas = await m.sinais("ana")
    assert sorted(linha.tipo for linha in linhas) == ["dado_privado", "texto_de_fora"]
    assert {linha.ferramenta_id for linha in linhas} == {m.f["t_ver"].id}
    assert await alerta(m) is not None


async def test_texto_nosso_nao_grava_sinal(mundo: Mundo) -> None:
    m = mundo
    t = m.terceiro_nome
    m.leitor.respostas[m.f["t_ver"].id] = Recusa("Leitura recusada pelo despachante.", "fora_da_ordem")
    with pytest.raises(MCPError):
        await m.chamar("ana", "t_ver", {"texto": "oi"})
    for motivo in ("falha_nao_enviado", "sistema_pediu_dados", "chave_recusada"):
        m.leitor.respostas[m.f["t_ver"].id] = resultado_nosso(t, motivo)
        r = await m.chamar("ana", "t_ver", {"texto": "oi"})
        assert r.is_error
    assert await m.sinais("ana") == []
    # Erro do proprio sistema (isError sem motivo nosso) e texto do sistema: conta.
    m.leitor.respostas[m.f["t_ver"].id] = resultado_do_sistema(t, "Erro do sistema.", erro=True)
    await m.chamar("ana", "t_ver", {"texto": "oi"})
    assert len(await m.sinais("ana")) == 2


async def test_sem_gravar_o_sinal_a_leitura_nao_sai(mundo: Mundo, monkeypatch: pytest.MonkeyPatch) -> None:
    m = mundo

    async def falha(*_: Any, **__: Any) -> tuple[str, ...]:
        raise RuntimeError("banco fora do ar na hora do sinal")

    monkeypatch.setattr(sinais, "registrar", falha)
    m.leitor.respostas[m.f["t_ver"].id] = resultado_do_sistema(m.terceiro_nome, "SEGREDO-DO-TERCEIRO")
    with pytest.raises(MCPError) as erro:
        await m.chamar("ana", "t_ver", {"texto": "oi"})
    assert "SEGREDO-DO-TERCEIRO" not in str(erro.value.error)
    assert len(m.leitor.chamadas) == 1


# --- Pela entrada: acoes ---------------------------------------------------------------------------------------


def protocolo(r: Any) -> str:
    return str((r.meta or {})[PREFIXO_META + "protocolo"])


async def usados(m: Mundo, limite: uuid.UUID) -> int:
    async with m.dono.connect() as c:
        return int((await c.execute(text("SELECT usados_hoje FROM limites WHERE id = :l"), {"l": limite})).scalar_one())


async def ficha(m: Mundo, prot: str) -> Any:
    async with m.dono.connect() as c:
        return (
            await c.execute(
                text(
                    "SELECT fi.sinais, fi.exige_passkey, fi.risco FROM fichas fi"
                    " JOIN operacoes o ON o.id = fi.operacao_id WHERE o.protocolo = :p"
                ),
                {"p": prot},
            )
        ).one()


async def test_tres_sinais_suspendem_o_limite_e_a_ficha_mostra_o_motivo(mundo: Mundo) -> None:
    m = mundo
    limite = await m.limite("ana", "p_mensagem")
    antes = await m.chamar("ana", "p_mensagem", {"destinatario": "equipe", "texto": "antes do alerta"})
    op_antes = await m.operacao(protocolo(antes))
    assert op_antes.estado == Estado.LIBERADA
    assert op_antes.limite_id == limite
    assert await usados(m, limite) == 1

    await m.chamar("ana", "t_ler", {"texto": "oi"})
    await m.chamar("ana", "p_cadastro")
    depois = await m.chamar("ana", "p_mensagem", {"destinatario": "equipe", "texto": "SEGREDO-DO-PEDIDO"})
    prot = protocolo(depois)
    op = await m.operacao(prot)
    assert op.estado == Estado.AGUARDANDO_FICHA
    assert op.limite_id is None
    assert await usados(m, limite) == 1  # nada gasto
    # O aplicativo nao fica sabendo do alerta (D24): recebe a resposta de qualquer ficha.
    assert "Nada saiu ainda" in depois.content[0].text
    assert "sinais" not in json.dumps(depois.model_dump(by_alias=True)).lower()

    linha_ficha = await ficha(m, prot)
    retrato = linha_ficha.sinais
    assert retrato == op.ficha_sinais
    assert retrato["janela_minutos"] == 240
    assert [(v["sinal"], v["ferramenta"], v["sistema"]) for v in retrato["vistos"]] == [
        ("texto_de_fora", m.f["t_ler"].nome, m.terceiro_nome),
        ("dado_privado", m.f["p_cadastro"].nome, m.proprio_nome),
    ]
    assert "SEGREDO-DO-PEDIDO" not in json.dumps(retrato)
    assert "Texto que o sistema escreveu" not in json.dumps(retrato)

    pedida = await m.extrato("acao_pedida", protocolo=prot)
    assert [linha.dados["rota"] for linha in pedida] == ["tres_sinais"]
    assert pedida[0].dados["tres_sinais"] is True
    alerta_linha = await m.extrato("tres_sinais", protocolo=prot)
    assert len(alerta_linha) == 1
    assert alerta_linha[0].frase == (
        f"Alerta dos tres sinais no protocolo {prot}: o aplicativo leu texto de fora em {m.f['t_ler'].nome} e viu"
        f" dados pessoais em {m.f['p_cadastro'].nome}; nenhum limite valeu, e a acao foi para a ficha."
    )
    assert alerta_linha[0].pessoa_id == m.pessoas["ana"]
    # A acao liberada antes do alerta continua liberada: o alerta vale para o que for pedido depois.
    assert (await m.operacao(op_antes.protocolo)).estado == Estado.LIBERADA
    # Sem o alerta, o limite continua valendo para as outras pessoas e aplicativos.
    assert (await m.extrato("tres_sinais", protocolo=op_antes.protocolo)) == []


async def test_sem_alerta_a_acao_vai_pelo_limite(mundo: Mundo) -> None:
    m = mundo
    limite = await m.limite("ana", "p_mensagem")
    await m.chamar("ana", "t_ler", {"texto": "oi"})  # so um sinal
    r = await m.chamar("ana", "p_mensagem", {"destinatario": "equipe", "texto": "so um sinal"})
    assert (await m.operacao(protocolo(r))).estado == Estado.LIBERADA
    assert await usados(m, limite) == 1
    pedida = await m.extrato("acao_pedida", protocolo=protocolo(r))
    assert pedida[0].dados["tres_sinais"] is False
    assert (await ficha_ou_nada(m, protocolo(r))) is None


async def test_o_pedido_usa_a_janela_da_politica_vigente(mundo: Mundo) -> None:
    """A janela do alerta no pedido de acao e a da politica (``THREE_SIGNALS_WINDOW_MINUTES``), nao o valor
    inicial: com 5 minutos, um texto de fora de 10 minutos atras ja nao conta; com 15, conta e vai no retrato."""
    m = mundo
    autor = f"teste-g4-{m.sufixo}"

    async def janela(minutos: int) -> None:
        async with m.dono.begin() as c:
            await c.execute(
                text(
                    "INSERT INTO politica (chave, valor, autor, motivo)"
                    " VALUES ('THREE_SIGNALS_WINDOW_MINUTES', :v, :a, 'janela do alerta no teste')"
                ),
                {"v": minutos, "a": autor},
            )

    limite = await m.limite("ana", "p_mensagem")
    await sinal(m, "ana", "texto_de_fora", "t_ler", ha_minutos=10)
    await sinal(m, "ana", "dado_privado", "p_cadastro")
    try:
        await janela(5)
        curta = await m.chamar("ana", "p_mensagem", {"destinatario": "equipe", "texto": "janela de 5 minutos"})
        assert (await m.operacao(protocolo(curta))).estado == Estado.LIBERADA
        assert await usados(m, limite) == 1
        await janela(15)
        longa = await m.chamar("ana", "p_mensagem", {"destinatario": "equipe", "texto": "janela de 15 minutos"})
        op = await m.operacao(protocolo(longa))
        assert op.estado == Estado.AGUARDANDO_FICHA
        assert op.ficha_sinais is not None
        assert op.ficha_sinais["janela_minutos"] == 15
        assert await usados(m, limite) == 1
    finally:
        async with m.dono.begin() as c:
            await c.execute(text("DELETE FROM politica WHERE autor = :a"), {"a": autor})


async def ficha_ou_nada(m: Mundo, prot: str) -> Any:
    async with m.dono.connect() as c:
        return (
            await c.execute(
                text("SELECT fi.id FROM fichas fi JOIN operacoes o ON o.id = fi.operacao_id WHERE o.protocolo = :p"),
                {"p": prot},
            )
        ).first()


async def test_sinais_de_pedidos_diferentes_somam_e_a_propria_marcada_liga_o_alerta(mundo: Mundo) -> None:
    m = mundo
    await m.limite("ana", "p_mensagem")
    await m.chamar("ana", "p_caixa")
    await m.chamar("ana", "p_cadastro")
    r = await m.chamar("ana", "p_mensagem", {"destinatario": "equipe", "texto": "depois de ler a caixa"})
    op = await m.operacao(protocolo(r))
    assert op.estado == Estado.AGUARDANDO_FICHA
    assert op.ficha_sinais is not None
    assert op.ficha_sinais["vistos"][0]["ferramenta"] == m.f["p_caixa"].nome


async def test_risco_alto_e_experiencia_mantem_a_rota_e_levam_o_retrato(mundo: Mundo) -> None:
    m = mundo
    for pessoa in ("ana", "eva"):
        await sinal(m, pessoa, "texto_de_fora", "t_ler")
        await sinal(m, pessoa, "dado_privado", "p_cadastro")
    alto = protocolo(await m.chamar("ana", "p_cancelar", {"texto": "cancelar"}))
    f = await ficha(m, alto)
    assert f.risco == "alto"
    assert f.exige_passkey is True
    assert f.sinais is not None
    assert len(f.sinais["vistos"]) == 2
    assert [linha.dados["rota"] for linha in await m.extrato("acao_pedida", protocolo=alto)] == ["risco_alto"]
    assert len(await m.extrato("tres_sinais", protocolo=alto)) == 1

    experiencia = protocolo(await m.chamar("eva", "p_mensagem", {"destinatario": "equipe", "texto": "nova aqui"}))
    assert (await ficha(m, experiencia)).sinais is not None
    rotas = [linha.dados["rota"] for linha in await m.extrato("acao_pedida", protocolo=experiencia)]
    assert rotas == ["periodo_de_experiencia"]


async def test_chave_desligada_pessoa_real_sem_alerta_de_terceiro_e_de_teste_com(mundo: Mundo) -> None:
    m = mundo
    assert m.p.FEATURE_PHASE3_THIRD_PARTY is False
    limite_real = await m.limite("real", "p_mensagem")
    await m.limite("ana", "p_mensagem")
    for pessoa in ("real", "ana"):
        await sinal(m, pessoa, "texto_de_fora", "t_ler")  # gravado antes (quando podia), de terceiro
        await m.chamar(pessoa, "p_cadastro")
    real = await m.chamar("real", "p_mensagem", {"destinatario": "equipe", "texto": "real"})
    assert (await m.operacao(protocolo(real))).estado == Estado.LIBERADA
    assert await usados(m, limite_real) == 1
    ana = await m.chamar("ana", "p_mensagem", {"destinatario": "equipe", "texto": "teste"})
    assert (await m.operacao(protocolo(ana))).estado == Estado.AGUARDANDO_FICHA

    # Correcao B7: texto de fora de ferramenta propria marcada liga o alerta da pessoa real tambem.
    await m.chamar("real", "p_caixa")
    de_novo = await m.chamar("real", "p_mensagem", {"destinatario": "equipe", "texto": "real depois da caixa"})
    op = await m.operacao(protocolo(de_novo))
    assert op.estado == Estado.AGUARDANDO_FICHA
    assert op.ficha_sinais is not None
    assert await usados(m, limite_real) == 1


async def test_fichas_do_alerta_contam_no_teto_por_hora(mundo: Mundo) -> None:
    m = mundo
    teto = POLITICA["CONFIRM_CAP_PER_HOUR"].inicial
    await m.limite("ana", "p_mensagem", por_dia=1000)
    await sinal(m, "ana", "texto_de_fora", "t_ler")
    await sinal(m, "ana", "dado_privado", "p_cadastro")
    pedidos = Pedidos(m.p, m.entrada_db)
    for n in range(teto):
        op, _ = await pedidos.pedir(await m.pedido("ana", "p_mensagem", {"destinatario": "equipe", "texto": f"{n}"}))
        assert op.estado == Estado.AGUARDANDO_FICHA
    with pytest.raises(AcaoRecusada) as erro:
        await pedidos.pedir(await m.pedido("ana", "p_mensagem", {"destinatario": "equipe", "texto": "demais"}))
    assert erro.value.motivo == "teto_confirmacoes"
    assert len(await m.extrato("teto_confirmacoes")) == 1


# --- Resultado de acao ----------------------------------------------------------------------------------------


async def test_resultado_de_acao_de_terceiro_conta_enviada_e_consulta(mundo: Mundo) -> None:
    m = mundo
    r = await m.chamar("ana", "t_enviar", {"texto": "responder"})
    op = await m.operacao(protocolo(r))
    assert await m.sinais("ana") == []  # a ficha nao traz texto do sistema
    await m.confirmar(op)
    await m.enviar_como_despachante(op, resultado_do_sistema(m.terceiro_nome, "Resposta enviada."))
    assert await m.sinais("ana") == []  # o despachante nao grava sinal: so a entrada, quando entrega

    consulta = await m.consultar("ana", op.protocolo)
    assert not consulta.is_error
    assert [(s.tipo, s.ferramenta_id) for s in await m.sinais("ana")] == [("texto_de_fora", m.f["t_enviar"].id)]

    # Pedido igual: a entrada devolve a original enviada, com o resultado, e grava de novo.
    igual = await m.chamar("ana", "t_enviar", {"texto": "responder"})
    assert protocolo(igual) == op.protocolo
    assert len(await m.sinais("ana")) == 2

    # Acao que nao saiu nao traz texto do sistema: nada gravado.
    outra = await m.chamar("ana", "t_enviar", {"texto": "outra"})
    await m.consultar("ana", protocolo(outra))
    assert len(await m.sinais("ana")) == 2


async def test_alerta_do_pedido_ve_o_sinal_gravado_na_mesma_hora(mundo: Mundo) -> None:
    """A gravacao confirma antes de o texto sair: o pedido de acao logo depois ja enxerga os dois sinais."""
    m = mundo
    await m.limite("ana", "p_mensagem")
    await m.chamar("ana", "t_ver", {"texto": "oi"})
    pedidos = Pedidos(m.p, m.entrada_db)
    op, _ = await pedidos.pedir(await m.pedido("ana", "p_mensagem", {"destinatario": "equipe", "texto": "logo"}))
    assert op.estado == Estado.AGUARDANDO_FICHA
    assert op.ficha_sinais is not None


async def test_rota_tres_sinais_nunca_leva_limite(mundo: Mundo) -> None:
    m = mundo
    f = await m.vitrine("ana", "p_mensagem")
    async with m.entrada_db.begin() as c:
        with pytest.raises(ValueError, match="nenhum limite vale"):
            await fila.registrar_pedido(
                c,
                pessoa_id=m.pessoas["ana"],
                aplicativo_id=m.apps["A"],
                ordem_id=m.ordens[("ana", "A")],
                ferramenta=f,
                argumentos={"texto": "x"},
                aplicativo_diz=None,
                rota=fila.Rota(limite_id=uuid.uuid4(), motivo="limite", sinais={"janela_minutos": 5, "vistos": []}),
                prazo_ficha_minutos=30,
                espera_segundos=0,
            )
