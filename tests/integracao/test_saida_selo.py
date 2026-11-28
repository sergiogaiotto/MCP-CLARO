"""Porta de saida e leitura com o selo de nivel 2 e 3 (desenho 5.1 e 12.1 G3; criticas A1, M3 e M4).

Com o duble de verdade (``exemplos.selo_nivel2`` e ``selo_nivel3``), o banco montado pelo dono
(``tests.apoio_selos``) e o despachante com o usuario do papel:

- quando o numero vai: com selo 2 ou 3 vigente e pessoa de teste (ou a chave da Fase 4 ligada), o ``_meta`` do
  pedido leva o numero de ``envios_selo``; com nivel 1, ou pessoa real com a chave desligada, nao;
- ordem: ``envios_selo`` nasce antes de "enviando" (o banco so aceita com a fila "liberada"); a queda entre gravar e
  responder deixa a acao "nao confirmada", com o envio, e a consulta e possivel;
- recibo do nivel 3: lido antes de o ``_meta`` sair, gravado em ``informacoes_sistema`` (origem ``recibo``), nunca em
  ``operacoes.resultado``; incoerente com o ``isError``, ``resposta_invalida``, e o estado da fila nao muda;
- o selo nunca para a porta (A1): um erro do banco no passo do selo faz a acao sair sem numero, com a linha
  ``acao_enviada_sem_numero`` quando um numero era devido (o selo lido pedia, ou a ficha prometia), e a acao seguinte
  da fila tambem sai; com a chave desligada e pessoa real, o selo nem e lido;
- pedido igual (D16): sai com o mesmo numero da original, o sistema nao faz de novo, e o extrato diz;
- leitura: o ``_meta`` leva o selo efetivo da pessoa, nunca a coluna antiga; uma falha ao ler o selo vira nivel 1,
  sem parar a leitura.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from exemplos import selo_mentiroso, selo_nivel2, selo_nivel3
from mcp_claro.config.settings import Parametros
from mcp_claro.despachante import servico
from mcp_claro.despachante.servico import Despachante, PedidoLeitura
from mcp_claro.saida import despachante as porta_de_saida
from mcp_claro.saida import fila
from mcp_claro.selo import contrato, vigente
from mcp_claro.versao import PREFIXO_META
from tests import apoio_selo_g3a as a
from tests import apoio_selos as s

pytestmark = [pytest.mark.integracao, pytest.mark.anyio]

META_SELO = PREFIXO_META + "selo"


def numero_no_meta(cn: a.Cena) -> list[Any]:
    """O numero que cada chamada de acao levou no ``_meta`` (None quando nao levou)."""
    return [m.get(contrato.CHAVE_OPERACAO) for m in cn.metas()]


@pytest.mark.parametrize(
    ("fabrica", "nivel"), [(selo_nivel2.criar, 2), (selo_nivel3.criar, 3)], ids=["nivel2", "nivel3"]
)
async def test_com_selo_vigente_o_numero_vai_so_no_meta(banco_migrado: Parametros, fabrica: Any, nivel: int) -> None:
    async with a.cena(banco_migrado, fabrica, nivel=nivel) as cn:
        op = await a.nova_acao(cn)
        await a.mandar(cn)
        assert await a.estado_da_fila(cn, op.id) == "enviada"
        envio = await a.envio(cn, op.id)
        assert envio is not None
        assert (envio.nivel, envio.teste_id, envio.repete_operacao_id) == (
            nivel,
            cn.teste.id if cn.teste else None,
            None,
        )
        assert numero_no_meta(cn) == [str(envio.chave)]
        tipos = [linha.tipo for linha in await a.extrato(cn, protocolo=op.protocolo)]
        assert "acao_enviada" in tipos
        assert "acao_enviada_sem_numero" not in tipos
        resultado = (await a.operacao(cn, op.id)).resultado
        assert str(envio.chave) not in json.dumps(resultado)


async def test_sem_selo_vigente_vai_sem_numero(banco_migrado: Parametros) -> None:
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=None) as cn:
        op = await a.nova_acao(cn)
        await a.mandar(cn)
        assert await a.estado_da_fila(cn, op.id) == "enviada"
        assert await a.envio(cn, op.id) is None
        assert numero_no_meta(cn) == [None]
        assert await a.informacoes(cn, op.id) == []


@pytest.mark.parametrize("ligada", [False, True], ids=["chave_desligada", "chave_ligada"])
async def test_pessoa_real_so_leva_o_numero_com_a_chave_da_fase_ligada(banco_migrado: Parametros, ligada: bool) -> None:
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        async with cn.dono.begin() as c:
            real = await s.nova_pessoa(c, teste=False)
            from mcp_claro.despachante import chaves

            await chaves.guardar(c, cn.cifra, sistema_id=cn.sistema.id, segredo=a.CHAVE_DA_PESSOA, pessoa_id=real)
        op = await a.nova_acao(cn, pessoa_id=real)
        await a.mandar(cn, cn.porta(selos_ligados=ligada))
        assert await a.estado_da_fila(cn, op.id) == "enviada"
        envio = await a.envio(cn, op.id)
        if ligada:
            assert envio is not None
            assert numero_no_meta(cn) == [str(envio.chave)]
        else:
            assert envio is None
            assert numero_no_meta(cn) == [None]


class Queda(Exception):
    pass


class PortaQueCai(porta_de_saida.PortaDeSaida):
    """A porta que cai depois de marcar "enviando" (com o envio gravado) e antes de o pedido sair."""

    async def _enviar(self, op: Any, *args: Any) -> Any:
        raise Queda


async def test_queda_entre_gravar_e_responder_deixa_nao_confirmada_com_envio(banco_migrado: Parametros) -> None:
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        op = await a.nova_acao(cn)
        with pytest.raises(Queda):
            await cn.porta(PortaQueCai).enviar_uma()
        assert await a.estado_da_fila(cn, op.id) == "enviando"
        envio = await a.envio(cn, op.id)
        assert envio is not None  # gravado antes de "enviando", na mesma transacao
        assert numero_no_meta(cn) == []  # nada saiu
        retomadas = await cn.porta().assumir()
        assert op.protocolo in retomadas
        assert await a.estado_da_fila(cn, op.id) == "nao_confirmada"
        # A consulta e possivel: o envio esta la. Passado o tempo da escada, o sistema diz que nao recebeu.
        await a.voltar_no_tempo(cn, [op.id])
        desfecho = await cn.perguntador().consultar(op.id)
        assert (desfecho.desfecho, desfecho.estado) == ("respondeu", "nao_recebida")


async def test_recibo_lido_antes_do_meta_sair_e_gravado_fora_do_resultado(banco_migrado: Parametros) -> None:
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        op = await a.nova_acao(cn)
        await a.mandar(cn)
        assert await a.estado_da_fila(cn, op.id) == "enviada"
        [info] = await a.informacoes(cn, op.id)
        assert (info.origem, info.desfecho, info.estado, info.repetida, info.objetos) == (
            "recibo",
            "respondeu",
            "concluida",
            False,
            1,
        )
        guardado = (await a.operacao(cn, op.id)).resultado
        assert "_meta" not in guardado
        assert contrato.CHAVE_RECIBO not in json.dumps(guardado)
        assert "lembrete-" not in a.textos(await a.informacoes(cn, op.id))  # os objetos do sistema nunca ficam


async def test_recibo_incoerente_vira_resposta_invalida_e_a_fila_nao_muda(banco_migrado: Parametros) -> None:
    async with a.cena(banco_migrado, selo_mentiroso.criar, nivel=3, modo="recibo_incoerente") as cn:
        op = await a.nova_acao(cn)
        await a.mandar(cn)
        # O isError do sistema diz que falhou: a fila segue a regra de hoje (respondeu com erro e "enviada").
        assert await a.estado_da_fila(cn, op.id) == "enviada"
        assert (await a.operacao(cn, op.id)).desfecho == "o sistema respondeu com erro"
        [info] = await a.informacoes(cn, op.id)
        assert (info.origem, info.desfecho, info.estado, info.objetos) == ("recibo", "resposta_invalida", None, None)


async def test_erro_do_banco_no_passo_do_selo_nao_para_a_porta(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Critica A1: o banco recusa o envio com numero (aqui, um nivel que nao e o vigente, o que o gatilho recusa).
    A acao sai sem numero, com a linha e o contador, e a acao seguinte da fila tambem sai."""
    original = vigente.efetivo

    def nivel_errado(lido: Any, *, ligada: bool, pessoa_teste: bool) -> Any:
        certo = original(lido, ligada=ligada, pessoa_teste=pessoa_teste)
        return vigente.SeloVigente(3, certo.teste_id, certo.testado_em, certo.vale_ate, certo.modo, "vigente")

    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        primeira = await a.nova_acao(cn)
        segunda = await a.nova_acao(cn, argumentos={"para": "caixa-de-teste@exemplo.teste", "texto": "outra"})
        monkeypatch.setattr(porta_de_saida.vigente, "efetivo", nivel_errado)
        await a.mandar(cn)
        for op in (primeira, segunda):
            assert await a.estado_da_fila(cn, op.id) == "enviada"
            assert await a.envio(cn, op.id) is None
            tipos = [linha.tipo for linha in await a.extrato(cn, protocolo=op.protocolo)]
            assert tipos.count("acao_enviada_sem_numero") == 1
            assert "acao_enviada" in tipos
        assert numero_no_meta(cn) == [None, None]
        assert cn.aplicadas() == 2


class Espia:
    """Os contadores somados pela porta (``saida.despachante.CONTADORES``), sem deixar de somar."""

    def __init__(self, contadores: Any) -> None:
        self.contadores = contadores
        self.somados: list[tuple[str, dict[str, str]]] = []

    def somar(self, nome: str, valor: int = 1, **rotulos: str) -> None:
        self.somados.append((nome, rotulos))
        self.contadores.somar(nome, valor, **rotulos)


async def test_erro_do_codigo_no_passo_do_selo_nao_para_a_porta(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O selo nao foi lido, e a ficha prometia o nivel 2: a acao sai sem numero, com a linha da falha do MCP-CLARO.
    A linha de "nivel menor" nao sai junto: o selo do sistema nao mudou."""

    async def quebra(*_: Any, **__: Any) -> Any:
        raise RuntimeError("selo_vigente fora")

    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        op = await a.nova_acao(cn, ficha_selo=s.retrato(await a.selo_vigente(cn)))
        monkeypatch.setattr(porta_de_saida.vigente, "ler", quebra)
        await a.mandar(cn)
        assert await a.estado_da_fila(cn, op.id) == "enviada"
        tipos = [linha.tipo for linha in await a.extrato(cn, protocolo=op.protocolo)]
        assert "acao_enviada_sem_numero" in tipos
        assert "acao_enviada_selo_menor" not in tipos


async def test_selo_nao_lido_sem_promessa_na_ficha_nao_ganha_a_linha(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sem conseguir ler o selo, e sem ficha que prometesse numero, nao se sabe se um numero era devido: a acao sai
    como a pessoa esperava, sem a linha "sem o numero do selo", e so o contador diz que o selo nao foi lido."""

    async def quebra(*_: Any, **__: Any) -> Any:
        raise RuntimeError("selo_vigente fora")

    espia = Espia(porta_de_saida.CONTADORES)
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        op = await a.nova_acao(cn)
        monkeypatch.setattr(porta_de_saida.vigente, "ler", quebra)
        monkeypatch.setattr(porta_de_saida, "CONTADORES", espia)
        await a.mandar(cn)
        assert await a.estado_da_fila(cn, op.id) == "enviada"
        tipos = [linha.tipo for linha in await a.extrato(cn, protocolo=op.protocolo)]
        assert "acao_enviada_sem_numero" not in tipos
        assert ("selo_nao_lido", {"onde": "porta"}) in espia.somados
        assert ("selo_envios", {"desfecho": "sem_numero"}) not in espia.somados


@pytest.mark.parametrize("nivel", [None, 2], ids=["nunca_testado", "nivel2"])
async def test_chave_desligada_e_pessoa_real_nem_le_o_selo(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch, nivel: int | None
) -> None:
    """Com a chave da Fase 4 desligada e pessoa real, nenhum numero iria: o selo vigente nem e lido. Mesmo que a
    leitura falhasse, nada de "sem o numero do selo" no extrato; o retrato da linha da acao e o da fase desligada."""
    lidas: list[Any] = []

    async def quebra(*args: Any, **__: Any) -> Any:
        lidas.append(args)
        raise RuntimeError("selo_vigente fora")

    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=nivel) as cn:
        async with cn.dono.begin() as c:
            real = await s.nova_pessoa(c, teste=False)
            from mcp_claro.despachante import chaves

            await chaves.guardar(c, cn.cifra, sistema_id=cn.sistema.id, segredo=a.CHAVE_DA_PESSOA, pessoa_id=real)
        op = await a.nova_acao(cn, pessoa_id=real)
        monkeypatch.setattr(porta_de_saida.vigente, "ler", quebra)
        p = cn.porta(selos_ligados=False)
        guardados: list[Any] = []
        original = p._gravar_fim

        async def guardar(*args: Any, **kw: Any) -> None:
            guardados.append(kw["selo"])
            await original(*args, **kw)

        monkeypatch.setattr(p, "_gravar_fim", guardar)
        await a.mandar(cn, p)
        assert await a.estado_da_fila(cn, op.id) == "enviada"
        assert lidas == []
        assert numero_no_meta(cn) == [None]
        tipos = [linha.tipo for linha in await a.extrato(cn, protocolo=op.protocolo)]
        assert "acao_enviada_sem_numero" not in tipos
        [selo] = guardados
        assert (selo.nivel, selo.sem_numero) == (1, False)
        assert selo.retrato == vigente.FASE_DESLIGADA.retrato()


async def test_nao_saiu_nao_ganha_a_linha_de_enviada_sem_numero(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def quebra(*_: Any, **__: Any) -> Any:
        raise RuntimeError("selo_vigente fora")

    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        # A ficha prometia o nivel 2: so o "nao saiu" impede a linha.
        op = await a.nova_acao(cn, ficha_selo=s.retrato(await a.selo_vigente(cn)))
        async with cn.dono.begin() as c:
            await c.execute(text("DELETE FROM chaves_cifradas WHERE sistema_id = :s"), {"s": cn.sistema.id})
        monkeypatch.setattr(porta_de_saida.vigente, "ler", quebra)
        await a.mandar(cn)
        assert await a.estado_da_fila(cn, op.id) == "nao_saiu"
        tipos = [linha.tipo for linha in await a.extrato(cn, protocolo=op.protocolo)]
        assert "acao_enviada_sem_numero" not in tipos


async def test_pedido_igual_sai_com_o_mesmo_numero_e_nao_faz_de_novo(banco_migrado: Parametros) -> None:
    """D16 e M4: a original ficou "nao confirmada" (a resposta se perdeu no cortador), a pessoa pediu igual: o envio
    copia o numero, o sistema de nivel 2 reconhece e nao aplica de novo, e o extrato diz que saiu com o mesmo
    numero."""
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2, cortar=True) as cn:
        assert cn.corte is not None
        cn.corte.modo, cn.corte.so_se_tiver, cn.corte.maximo_de_cortes = "sem_resposta", b'"enviar_lembrete"', 1
        original = await a.nova_acao(cn)
        await a.mandar(cn)
        assert await a.estado_da_fila(cn, original.id) == "nao_confirmada"
        assert cn.aplicadas() == 1
        igual = await a.nova_acao(cn, repete=original.id)
        await a.mandar(cn)
        assert await a.estado_da_fila(cn, igual.id) == "enviada"
        envio_original, envio_igual = await a.envio(cn, original.id), await a.envio(cn, igual.id)
        assert envio_igual.chave == envio_original.chave
        assert envio_igual.repete_operacao_id == original.id
        assert numero_no_meta(cn) == [str(envio_original.chave)] * 2
        assert cn.aplicadas() == 1  # o sistema nao fez de novo
        linhas = await a.extrato(cn, protocolo=igual.protocolo)
        [linha] = [x for x in linhas if x.tipo == "acao_enviada_mesmo_numero"]
        assert original.protocolo in linha.frase
        assert str(envio_original.chave) not in a.textos(linhas)


async def test_acao_que_sai_num_nivel_menor_que_o_da_ficha(banco_migrado: Parametros) -> None:
    """Critica M3: a ficha prometeu o nivel 2; o selo caiu antes de a acao sair (o sistema deixou de listar o
    contrato): ela sai sem numero, e o extrato diz que saiu num nivel menor."""
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        retrato = s.retrato(await a.selo_vigente(cn))
        assert retrato["nivel"] == 2
        op = await a.nova_acao(cn, ficha_selo=retrato)
        async with cn.dono.begin() as c:
            await c.execute(
                text("UPDATE ferramentas_vistas SET sumiu_em = now() WHERE sistema_id = :s AND nome = :n"),
                {"s": cn.sistema.id, "n": contrato.FERRAMENTA_CONSULTAR},
            )
        assert (await a.selo_vigente(cn)).motivo == "contrato_sumiu"
        await a.mandar(cn)
        assert await a.estado_da_fila(cn, op.id) == "enviada"
        assert await a.envio(cn, op.id) is None
        [linha] = [x for x in await a.extrato(cn, protocolo=op.protocolo) if x.tipo == "acao_enviada_selo_menor"]
        assert "no nivel 1, abaixo do nivel 2 da ficha" in linha.frase
        assert linha.dados["selo"]["nivel"] == 1


# --- Leitura -----------------------------------------------------------------------------------------------------


async def _pedido_de_leitura(cn: a.Cena, pessoa_id: uuid.UUID) -> PedidoLeitura:
    leitura = cn.sistema.leituras[0]
    async with cn.dono.begin() as c:
        modelo = (
            await c.execute(
                text(
                    "INSERT INTO modelos_ordem (nome, descricao, validade_dias, criado_por)"
                    " VALUES (:n, 'M', 30, 'admin') RETURNING id"
                ),
                {"n": f"leitura-{uuid.uuid4().hex[:8]}"},
            )
        ).scalar_one()
        await c.execute(
            text("INSERT INTO modelos_ordem_ferramentas (modelo_id, sistema_id, nome_original) VALUES (:m, :s, :n)"),
            {"m": modelo, "s": cn.sistema.id, "n": leitura.nome_original},
        )
        ordem = (
            await c.execute(
                text(
                    "INSERT INTO ordens_servico (pessoa_id, aplicativo_id, modelo_id, fim)"
                    " VALUES (:p, :a, :m, now() + interval '7 days') RETURNING id"
                ),
                {"p": pessoa_id, "a": cn.aplicativo_id, "m": modelo},
            )
        ).scalar_one()
    return PedidoLeitura(pessoa_id, cn.aplicativo_id, uuid.UUID(str(ordem)), leitura.id, {})


def _despachante(cn: a.Cena, **kw: Any) -> Despachante:
    return Despachante(cn.desp, cn.cifra, prazo_leitura=5.0, ambiente="teste", liberar_locais=True, **kw)


@pytest.mark.parametrize(
    ("teste", "ligada", "esperado"),
    [(True, False, 2), (False, False, 1), (False, True, 2)],
    ids=["pessoa_de_teste", "pessoa_real", "pessoa_real_com_a_chave_ligada"],
)
async def test_a_leitura_leva_o_selo_efetivo_da_pessoa(
    banco_migrado: Parametros, teste: bool, ligada: bool, esperado: int
) -> None:
    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        pessoa = cn.pessoa_id
        if not teste:
            async with cn.dono.begin() as c:
                pessoa = await s.nova_pessoa(c, teste=False)
                from mcp_claro.despachante import chaves

                await chaves.guardar(c, cn.cifra, sistema_id=cn.sistema.id, segredo=a.CHAVE_DA_PESSOA, pessoa_id=pessoa)
        resultado = await _despachante(cn, selos_ligados=ligada).ler(await _pedido_de_leitura(cn, pessoa))
        assert resultado["_meta"][META_SELO] == esperado
        assert not resultado.get("isError")


async def test_falha_ao_ler_o_selo_vira_nivel_1_e_a_leitura_segue(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def quebra(*_: Any, **__: Any) -> Any:
        raise RuntimeError("selo_vigente fora")

    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        pedido = await _pedido_de_leitura(cn, cn.pessoa_id)
        monkeypatch.setattr(servico.vigente, "ler", quebra)
        resultado = await _despachante(cn).ler(pedido)
        assert resultado["_meta"][META_SELO] == 1
        assert not resultado.get("isError")
        assert cn.chamadas(a.LEITURA) == 1


async def test_erro_do_banco_ao_ler_o_selo_tambem_vira_nivel_1(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O SAVEPOINT da leitura do selo: um erro do banco ali nao estraga a conexao da leitura (a chave e lida depois,
    na mesma conexao)."""

    async def erro_do_banco(c: Any, sistema_id: uuid.UUID) -> Any:
        await c.execute(text("SELECT 1/0"))

    async with a.cena(banco_migrado, selo_nivel2.criar, nivel=2) as cn:
        pedido = await _pedido_de_leitura(cn, cn.pessoa_id)
        monkeypatch.setattr(servico.vigente, "ler", erro_do_banco)
        resultado = await _despachante(cn).ler(pedido)
        assert resultado["_meta"][META_SELO] == 1
        assert not resultado.get("isError")


async def test_retomada_continua_como_antes(banco_migrado: Parametros) -> None:
    """``retomar_enviando`` segue como hoje: a acao em "enviando" com numero vira "nao confirmada", sem nada novo."""
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        op = await a.nova_acao(cn)
        with pytest.raises(Queda):
            await cn.porta(PortaQueCai).enviar_uma()
        async with cn.dono.begin() as c:
            retomadas = await fila.retomar_enviando(c)
        assert retomadas == [op.protocolo]
        assert await a.informacoes(cn, op.id) == []


async def test_o_fim_gravado_de_novo_nao_repete_as_linhas_do_selo(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uma tentativa de novo do fim, depois de um COMMIT cuja confirmacao se perdeu: o fim reconhece o proprio fim, e
    as linhas do selo e o recibo nao saem duas vezes."""
    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        op = await a.nova_acao(cn)
        p = cn.porta()
        guardados: list[Any] = []
        original = p._gravar_fim

        async def guardar(*args: Any, **kw: Any) -> None:
            guardados.append((args, kw))
            await original(*args, **kw)

        monkeypatch.setattr(p, "_gravar_fim", guardar)
        await a.mandar(cn, p)
        [(args, kw)] = guardados
        sem_numero = porta_de_saida.SeloDoEnvio(
            nivel=1, sem_numero=True, chave=kw["selo"].chave, teste_id=kw["selo"].teste_id
        )
        for _ in range(2):
            await original(*args, **(kw | {"selo": sem_numero}))
        tipos = [x.tipo for x in await a.extrato(cn, protocolo=op.protocolo)]
        assert tipos.count("acao_enviada") == 1
        assert tipos.count("acao_enviada_sem_numero") == 1
        assert [x.origem for x in await a.informacoes(cn, op.id)] == ["recibo"]


async def test_erro_ao_gravar_o_recibo_nao_impede_o_fim(
    banco_migrado: Parametros, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O passo do selo no fim fica num SAVEPOINT: um erro do banco ao gravar o recibo nao desfaz o fim do envio."""

    async def recibo_que_falha(c: Any, info: Any) -> bool:
        await c.execute(text("SELECT 1/0"))
        return False

    async with a.cena(banco_migrado, selo_nivel3.criar, nivel=3) as cn:
        op = await a.nova_acao(cn)
        monkeypatch.setattr(porta_de_saida.selo_consulta, "gravar_informacao", recibo_que_falha)
        await a.mandar(cn)
        assert await a.estado_da_fila(cn, op.id) == "enviada"
        assert await a.informacoes(cn, op.id) == []
        assert "acao_enviada" in [x.tipo for x in await a.extrato(cn, protocolo=op.protocolo)]
