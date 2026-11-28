"""Selo na entrada (Fase 4, grupo G4; desenho 7.3, criticas B1, M3 e A1), contra PostgreSQL 18.7 e a entrada real.

- ``tools/list`` leva o selo EFETIVO da pessoa (chave desligada: nivel 1 para a pessoa real, o vigente para a de
  teste; ligada pelo construtor: o vigente para todos), calculado sobre o selo lido na mesma linha da vitrine.
- A ficha nasce com o retrato do selo da hora (``fichas.selo``, com as datas em microssegundos), que vai tambem em
  ``dados.selo`` de ``acao_pedida``; o selo que cai depois nao muda a ficha. Se o banco recusar o retrato (o selo caiu
  entre a leitura e o INSERT), o selo e lido de novo e o pedido nao falha.
- ``agir`` e a consulta pelo protocolo levam o selo da operacao: o do envio com numero, nivel 1 para a que saiu sem
  numero, o do retrato da ficha antes de sair.
- ``tipo_da_recusa`` nao le o selo, e a leitura segue quando a leitura com o selo falha (A1).
- A1 na transacao do pedido e na resposta: um erro do banco ao ler o selo (``vigente.ler`` com ``SELECT 1 / 0``), ou
  o banco que recusa tambem o retrato relido, deixa a ficha sem retrato, sem erro; e a resposta sem selo lido vai com
  o nivel 1.
- Os textos ao aplicativo nao tem acento.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from mcp_claro.cadastro import repositorio
from mcp_claro.config.settings import Parametros
from mcp_claro.medicoes.contadores import CONTADORES
from mcp_claro.regras import bloqueios
from mcp_claro.saida import fila
from mcp_claro.saida.estados import Estado
from mcp_claro.saida.pedidos import CONSULTA
from mcp_claro.selo import vigente
from mcp_claro.versao import VERSAO_MCP
from tests import apoio_selos
from tests.integracao.test_selo_g4_apoio import (
    META_SELO,
    TEXTO_DO_SISTEMA,
    MundoSelo,
    meta,
    protocolo_de,
    retrato_json,
    subir_mundo_selo,
    textos,
)

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]


@pytest.fixture
async def mundo(banco_migrado: Parametros) -> AsyncIterator[MundoSelo]:
    async with subir_mundo_selo(banco_migrado) as m:
        yield m


def _selos_da_vitrine(ferramentas: dict[str, Any], m: MundoSelo) -> dict[str, Any]:
    return {nome: (t.meta or {}).get(META_SELO) for nome, t in ferramentas.items() if nome != CONSULTA}


async def test_tools_list_leva_o_selo_efetivo_da_pessoa(mundo: MundoSelo) -> None:
    m = mundo
    antes = await m.ferramentas("ana")
    assert _selos_da_vitrine(antes, m) == {m.acao: 1, m.leitura: 1}  # nunca testado
    await m.gravar_teste(nivel=3)
    assert _selos_da_vitrine(await m.ferramentas("ana"), m) == {m.acao: 3, m.leitura: 3}  # pessoa de teste
    assert _selos_da_vitrine(await m.ferramentas("real"), m) == {m.acao: 1, m.leitura: 1}  # chave desligada
    await m.retirar_selo()
    assert _selos_da_vitrine(await m.ferramentas("ana"), m) == {m.acao: 1, m.leitura: 1}  # o selo que cai, cai


async def test_tools_list_com_a_chave_ligada_leva_o_vigente_a_todos(banco_migrado: Parametros) -> None:
    async with subir_mundo_selo(banco_migrado, selo_ligado=True) as m:
        await m.gravar_teste(nivel=2)
        assert _selos_da_vitrine(await m.ferramentas("real"), m) == {m.acao: 2, m.leitura: 2}
        assert _selos_da_vitrine(await m.ferramentas("ana"), m) == {m.acao: 2, m.leitura: 2}
        r = await m.agir(pessoa="real")  # a ficha da pessoa real tambem leva o vigente
        assert meta(r)[META_SELO] == 2
        op = await m.operacao(protocolo_de(r))
        assert op.selo == apoio_selos.retrato(await m.selo_vigente())


async def test_ficha_guarda_o_retrato_da_hora_e_nao_muda_depois(mundo: MundoSelo) -> None:
    m = mundo
    teste = await m.gravar_teste(nivel=2)
    selo_de_agora = await m.selo_vigente()
    r = await m.agir()
    assert meta(r)[META_SELO] == 2
    op = await m.operacao(protocolo_de(r))
    assert op.estado == Estado.AGUARDANDO_FICHA
    esperado = apoio_selos.retrato(selo_de_agora)
    assert esperado["testado_em"] == teste.testado_em.isoformat()  # com os microssegundos do banco
    assert retrato_json(await m.ficha_selo(op)) == esperado
    assert op.selo == esperado
    [pedida] = await m.extrato("acao_pedida", op.protocolo)
    assert pedida.dados["selo"] == esperado  # o retrato da hora tambem vai ao extrato

    await m.retirar_selo()
    assert retrato_json(await m.ficha_selo(op)) == esperado  # o selo que cai nao muda a ficha
    assert (await m.operacao(op.protocolo)).selo == esperado
    outra = await m.operacao(protocolo_de(await m.agir({"para": "outra@exemplo.teste", "texto": "x"})))
    assert outra.selo is not None
    assert (outra.selo["nivel"], outra.selo["motivo"], outra.selo["teste_id"]) == (1, "retirado", str(teste.id))


async def test_ficha_da_pessoa_real_com_a_chave_desligada_fica_no_nivel_1(mundo: MundoSelo) -> None:
    m = mundo
    await m.gravar_teste(nivel=3)
    r = await m.agir(pessoa="real")
    assert meta(r)[META_SELO] == 1
    op = await m.operacao(protocolo_de(r))
    assert op.selo == {
        "nivel": 1,
        "teste_id": None,
        "testado_em": None,
        "vale_ate": None,
        "modo": None,
        "motivo": "fase_desligada",
    }


async def _ferramenta_com_selo(m: MundoSelo, *, com_selo: bool = True, pessoa: str = "ana") -> Any:
    async with m.entrada_db.connect() as c:
        f = await repositorio.ferramenta_na_vitrine(
            c, m.pessoas[pessoa], m.aplicativo_id, m.acao, VERSAO_MCP, com_selo=com_selo
        )
    assert f is not None
    assert f.ordem_id is not None
    return f


async def _registrar(m: MundoSelo, f: Any, argumentos: dict[str, Any], pessoa: str = "ana") -> fila.Operacao:
    async with m.entrada_db.begin() as c:
        return await fila.registrar_pedido(
            c,
            pessoa_id=m.pessoas[pessoa],
            aplicativo_id=m.aplicativo_id,
            ordem_id=f.ordem_id,
            ferramenta=f,
            argumentos=argumentos,
            aplicativo_diz=None,
            rota=fila.Rota(),
            prazo_ficha_minutos=30,
            espera_segundos=0,
        )


async def test_ficha_le_o_selo_de_novo_quando_o_banco_recusa_o_retrato(mundo: MundoSelo) -> None:
    """A retirada cai entre a leitura da vitrine (nivel 2) e o INSERT da ficha: o banco recusa o retrato de nivel 2
    (``fichas_guarda``), e a ficha nasce com o retrato de agora, sem falhar o pedido."""
    m = mundo
    teste = await m.gravar_teste(nivel=2)
    f = await _ferramenta_com_selo(m)
    assert f.sistema.selo_vigente.nivel == 2
    await m.retirar_selo()
    op = await _registrar(m, f, {"para": "corrida@exemplo.teste", "texto": "x"})
    assert op.estado == Estado.AGUARDANDO_FICHA
    assert op.selo is not None
    assert (op.selo["nivel"], op.selo["motivo"], op.selo["teste_id"]) == (1, "retirado", str(teste.id))
    [pedida] = await m.extrato("acao_pedida", op.protocolo)
    assert pedida.dados["selo"] == op.selo  # o extrato leva o retrato que ficou na ficha


async def test_ficha_sem_o_selo_da_vitrine_le_o_selo_na_transacao_do_pedido(mundo: MundoSelo) -> None:
    m = mundo
    await m.gravar_teste(nivel=2)
    f = await _ferramenta_com_selo(m, com_selo=False)
    op = await _registrar(m, f, {"para": "sem-selo@exemplo.teste", "texto": "x"})
    assert op.selo == apoio_selos.retrato(await m.selo_vigente())
    # Lido na transacao, o selo tambem passa pelo efetivo: a pessoa real, com a chave desligada, fica no nivel 1.
    f_real = await _ferramenta_com_selo(m, com_selo=False, pessoa="real")
    real = await _registrar(m, f_real, {"para": "sem-selo@exemplo.teste", "texto": "x"}, pessoa="real")
    assert real.selo is not None
    assert (real.selo["nivel"], real.selo["motivo"], real.selo["teste_id"]) == (1, "fase_desligada", None)


async def test_agir_e_consultar_levam_o_selo_da_operacao(mundo: MundoSelo) -> None:
    m = mundo
    await m.gravar_teste(nivel=3)
    r = await m.agir()
    assert meta(r)[META_SELO] == 3  # o retrato da ficha
    op = await m.operacao(protocolo_de(r))
    assert meta(await m.consultar(op.protocolo))[META_SELO] == 3
    op = await m.confirmar(op)
    envio = await m.sair_com_numero(op, "enviada")
    assert envio.nivel == 3
    assert meta(await m.consultar(op.protocolo))[META_SELO] == 3  # o do envio com numero
    repetida = await m.agir()  # pedido igual: devolve a original, com o selo dela
    assert protocolo_de(repetida) == op.protocolo
    assert meta(repetida)[META_SELO] == 3

    # A que passou por "enviando" sem numero (a porta nao conseguiu gravar o envio) saiu como nivel 1.
    outra = await m.operacao(protocolo_de(await m.agir({"para": "sem-numero@exemplo.teste", "texto": "y"})))
    await m.confirmar(outra)
    async with m.dono.begin() as c:
        await c.execute(
            text("UPDATE fila_saida SET estado = 'enviando', mudou_por = 'despachante' WHERE operacao_id = :o"),
            {"o": outra.id},
        )
        await c.execute(text("UPDATE fila_saida SET estado = 'nao_confirmada' WHERE operacao_id = :o"), {"o": outra.id})
    assert (await m.operacao(outra.protocolo)).selo is not None  # o retrato da ficha continua 3
    assert meta(await m.consultar(outra.protocolo))[META_SELO] == 1


async def test_o_fim_do_envio_leva_o_retrato_que_a_porta_passa(mundo: MundoSelo) -> None:
    """Desenho 7.2: ``acao_enviada`` ganha ``dados.selo`` (o da hora do passo) quando a porta (G3a) o passa a
    ``fila.finalizar``; sem ele, a linha fica como antes."""
    m = mundo
    await m.gravar_teste(nivel=2)
    retrato = apoio_selos.retrato(await m.selo_vigente())
    linhas = []
    for n, selo in enumerate((retrato, None)):
        op = await m.confirmar(await m.operacao(protocolo_de(await m.agir({"para": f"fim-{n}@exemplo.teste"}))))
        await m.sair_com_numero(op, None)  # o envio com numero e "enviando"; o fim e do despachante
        await m.terminar_como_despachante(op, Estado.ENVIADA, selo=selo)
        [linha] = await m.extrato("acao_enviada", op.protocolo)
        linhas.append(linha.dados)
    assert linhas[0]["selo"] == retrato
    assert "selo" not in linhas[1]


async def _limite_da_ana(m: MundoSelo) -> None:
    """Um limite da Ana para a acao (fora da experiencia): o pedido sai liberado, sem ficha."""
    acao = m.sistema.acoes[0]
    async with m.dono.begin() as c:
        await c.execute(
            text("UPDATE ordens_servico SET criado_em = now() - interval '60 days' WHERE pessoa_id = :p"),
            {"p": m.pessoas["ana"]},
        )
        await c.execute(
            text(
                "INSERT INTO limites (pessoa_id, aplicativo_id, sistema_id, nome_original, destinatarios, por_dia,"
                " valido_ate, com_passkey) VALUES (:p, :a, :s, :n, '[]'::jsonb, 5, now() + interval '10 days', true)"
            ),
            {"p": m.pessoas["ana"], "a": m.aplicativo_id, "s": m.sistema.id, "n": acao.nome_original},
        )


async def test_limite_sem_ficha_leva_o_selo_efetivo_de_agora(mundo: MundoSelo) -> None:
    m = mundo
    await m.gravar_teste(nivel=2)
    await _limite_da_ana(m)
    r = await m.agir()
    op = await m.operacao(protocolo_de(r))
    assert op.estado == Estado.LIBERADA
    assert op.ficha_id is None
    assert op.selo is None
    assert meta(r)[META_SELO] == 2
    assert meta(await m.consultar(op.protocolo))[META_SELO] == 2
    [pedida] = await m.extrato("acao_pedida", op.protocolo)
    assert pedida.dados["selo"] == apoio_selos.retrato(await m.selo_vigente())


async def test_tipo_da_recusa_nao_le_o_selo(mundo: MundoSelo, monkeypatch: pytest.MonkeyPatch) -> None:
    m = mundo
    chamadas: list[bool] = []
    original = repositorio.ferramenta_na_vitrine

    async def espiar(*args: Any, com_selo: bool = True, **kwargs: Any) -> Any:
        chamadas.append(com_selo)
        return await original(*args, com_selo=com_selo, **kwargs)

    monkeypatch.setattr(repositorio, "ferramenta_na_vitrine", espiar)
    async with m.entrada_db.begin() as c:
        b = await bloqueios.bloquear(
            c, "bloqueio", "pessoa", bloqueios.quem_pessoa(m.pessoas["ana"]), "teste", pessoa_id=m.pessoas["ana"]
        )
    try:
        with pytest.raises(MCPError):
            await m.agir()
        assert chamadas == [False]  # so tipo_da_recusa leu a ferramenta, e sem o selo
        [recusa] = await m.extrato_do_aplicativo("acao_recusada")
        assert recusa.ferramenta == m.acao  # o extrato diz que era uma acao
    finally:
        async with m.entrada_db.begin() as c:
            await bloqueios.encerrar(c, b.id, bloqueios.quem_admin(m.admin_id))


async def test_leitura_segue_quando_a_leitura_com_o_selo_falha(
    mundo: MundoSelo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1: um defeito ao ler o selo (``selo_vigente()`` na vitrine) nunca para a leitura nem o pedido de acao."""
    m = mundo
    await m.gravar_teste(nivel=2)
    original = repositorio.ferramenta_na_vitrine

    async def falha_com_selo(*args: Any, com_selo: bool = True, **kwargs: Any) -> Any:
        if com_selo:
            raise DBAPIError("SELECT selo_vigente(...)", {}, Exception("defeito de teste no selo"))
        return await original(*args, com_selo=com_selo, **kwargs)

    monkeypatch.setattr(repositorio, "ferramenta_na_vitrine", falha_com_selo)
    lido = await m.chamar(m.leitura, {})
    assert not lido.is_error
    assert textos(lido) == [TEXTO_DO_SISTEMA]
    r = await m.agir()
    op = await m.operacao(protocolo_de(r))
    assert op.selo == apoio_selos.retrato(await m.selo_vigente())  # a ficha leu o selo na transacao do pedido
    assert meta(r)[META_SELO] == 2


async def test_textos_ao_aplicativo_sem_acento(mundo: MundoSelo) -> None:
    m = mundo
    await m.gravar_teste(nivel=2)
    r = await m.agir()
    op = await m.confirmar(await m.operacao(protocolo_de(r)))
    await m.sair_com_numero(op)
    vistos = [*textos(r), *textos(await m.consultar(op.protocolo))]
    await m.informar(op, "concluida")
    vistos += textos(await m.consultar(op.protocolo))
    assert all(t.isascii() for t in vistos), vistos
    assert any("O MCP-CLARO esta perguntando ao sistema" in t for t in vistos)
    assert any(f"O sistema {m.sistema.nome} informou: concluida." in t for t in vistos)


# --- A1: o selo que nao se le (ou que o banco recusa duas vezes) nunca para o pedido nem a resposta ---------------


async def _ler_que_falha_no_banco(c: Any, _sistema_id: Any) -> Any:
    """``vigente.ler`` com um erro do proprio banco na conexao de quem chama: sem ponto de salvamento, a transacao do
    pedido ficaria abortada, e o INSERT seguinte falharia."""
    await c.execute(text("SELECT 1 / 0"))
    raise AssertionError("o banco ja recusou")


def _espiar_contadores(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    somados: list[str] = []
    somar = CONTADORES.somar

    def espiar(nome: str, valor: int = 1, **rotulos: str) -> None:
        somados.append(nome)
        somar(nome, valor, **rotulos)

    monkeypatch.setattr(CONTADORES, "somar", espiar)
    return somados


@pytest.mark.parametrize("caso", ["sem_o_selo_da_vitrine", "retrato_recusado_e_releitura_que_falha"])
async def test_selo_que_falha_no_banco_na_transacao_do_pedido_nao_para_o_pedido(
    mundo: MundoSelo, monkeypatch: pytest.MonkeyPatch, caso: str
) -> None:
    """A1 na ficha: a leitura do selo na transacao do pedido roda num ponto de salvamento. Um erro do banco nela vira
    "selo nao lido" (a ficha nasce sem retrato, "nao guardado"), nunca erro nem transacao abortada."""
    m = mundo
    await m.gravar_teste(nivel=2)
    if caso == "sem_o_selo_da_vitrine":
        f = await _ferramenta_com_selo(m, com_selo=False)
        esperados = ["selo_nao_lido_no_pedido"]
    else:
        f = await _ferramenta_com_selo(m)  # nivel 2 lido na vitrine
        await m.retirar_selo()  # o banco recusa o retrato de nivel 2; a releitura falha
        esperados = ["selo_ficha_retrato_relido", "selo_nao_lido_no_pedido", "selo_ficha_sem_retrato"]
    somados = _espiar_contadores(monkeypatch)
    monkeypatch.setattr(vigente, "ler", _ler_que_falha_no_banco)
    op = await _registrar(m, f, {"para": f"a1-{caso}@exemplo.teste", "texto": "x"})
    assert op.estado == Estado.AGUARDANDO_FICHA
    assert op.selo is None
    assert await m.ficha_selo(op) is None
    assert [n for n in somados if n.startswith("selo_")] == esperados
    [pedida] = await m.extrato("acao_pedida", op.protocolo)
    assert "selo" not in pedida.dados  # sem retrato, o extrato nao inventa um


async def test_ficha_sem_retrato_quando_o_banco_recusa_tambem_o_relido(
    mundo: MundoSelo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O selo cai entre a leitura da vitrine e o INSERT, e a releitura ainda traz o selo velho (outra corrida): o banco
    recusa os dois retratos, e a ficha nasce sem retrato, sem falhar o pedido (A1)."""
    m = mundo
    await m.gravar_teste(nivel=2)
    f = await _ferramenta_com_selo(m)
    velho = f.sistema.selo_vigente
    assert velho.nivel == 2
    await m.retirar_selo()

    async def ler_o_velho(_c: Any, _sistema_id: Any) -> Any:
        return velho

    somados = _espiar_contadores(monkeypatch)
    monkeypatch.setattr(vigente, "ler", ler_o_velho)
    op = await _registrar(m, f, {"para": "duas-vezes@exemplo.teste", "texto": "x"})
    assert op.estado == Estado.AGUARDANDO_FICHA
    assert op.selo is None
    assert [n for n in somados if n.startswith("selo_")] == ["selo_ficha_retrato_relido", "selo_ficha_sem_retrato"]


async def test_resposta_ao_aplicativo_com_o_selo_que_nao_se_le_vai_no_nivel_1(
    mundo: MundoSelo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A1 na resposta: sem envio, sem saida e sem retrato (o limite, que nao tem ficha), o selo da operacao e o efetivo
    de agora. Se ele nao se le, a resposta vai com o nivel 1 (que nao promete nada), sem erro, em ``agir`` e na
    consulta pelo protocolo."""
    m = mundo
    await m.gravar_teste(nivel=2)
    await _limite_da_ana(m)
    somados = _espiar_contadores(monkeypatch)
    monkeypatch.setattr(vigente, "ler", _ler_que_falha_no_banco)
    r = await m.agir()
    assert not r.is_error
    op = await m.operacao(protocolo_de(r))
    assert (op.estado, op.ficha_id, op.selo) == (Estado.LIBERADA, None, None)
    assert meta(r)[META_SELO] == 1
    consulta = await m.consultar(op.protocolo)
    assert not consulta.is_error
    assert meta(consulta)[META_SELO] == 1
    assert somados.count("selo_da_operacao_nao_lido") == 2
    monkeypatch.undo()
    assert meta(await m.consultar(op.protocolo))[META_SELO] == 2  # lido, o efetivo de agora e o 2
