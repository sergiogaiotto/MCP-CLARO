"""Regras sem banco: alcances, permissoes de bloqueio e pausa, estado das portas, revisao e frases."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from mcp_claro.extrato.frases import frase
from mcp_claro.regras import bloqueios, catraca, estado, revisao
from mcp_claro.regras.bloqueios import ALCANCES, TIPOS, Autor, Bloqueio, BloqueioInvalido

ANA = uuid.uuid4()
BIA = uuid.uuid4()
APP = uuid.uuid4()
SIS = uuid.uuid4()


def _b(tipo: str, alcance: str, pessoa: uuid.UUID | None = ANA) -> Bloqueio:
    return Bloqueio(
        id=uuid.uuid4(),
        tipo=tipo,  # type: ignore[arg-type]
        alcance=alcance,  # type: ignore[arg-type]
        pessoa_id=pessoa if alcance in ("pessoa", "pessoa_aplicativo") else None,
        aplicativo_id=APP if alcance in ("aplicativo", "pessoa_aplicativo") else None,
        sistema_id=SIS if alcance == "sistema" else None,
        quem="rotina:ronda",
        motivo="teste",
        criado_em=datetime.now(UTC),
    )


@pytest.mark.parametrize(
    ("alcance", "campos"),
    [
        ("pessoa", {"pessoa_id": ANA}),
        ("aplicativo", {"aplicativo_id": APP}),
        ("pessoa_aplicativo", {"pessoa_id": ANA, "aplicativo_id": APP}),
        ("sistema", {"sistema_id": SIS}),
        ("tudo", {}),
    ],
)
def test_cada_alcance_pede_exatamente_os_seus_campos(alcance: str, campos: dict[str, uuid.UUID]) -> None:
    bloqueios.conferir_pedido("bloqueio", alcance, **campos)
    with pytest.raises(BloqueioInvalido, match="pede"):
        bloqueios.conferir_pedido("bloqueio", alcance, **(campos | {"sistema_id": SIS, "pessoa_id": BIA}))
    if campos:
        with pytest.raises(BloqueioInvalido, match="pede"):
            bloqueios.conferir_pedido("pausa", alcance)


def test_tipo_e_alcance_desconhecidos() -> None:
    with pytest.raises(BloqueioInvalido, match="Tipo"):
        bloqueios.conferir_pedido("trava", "tudo")
    with pytest.raises(BloqueioInvalido, match="Alcance"):
        bloqueios.conferir_pedido("pausa", "mundo")


def test_quem_le_so_as_tres_formas() -> None:
    assert bloqueios.autor_de(bloqueios.quem_admin(ANA)) == Autor("admin", ANA)
    assert bloqueios.autor_de(bloqueios.quem_pessoa(ANA)) == Autor("pessoa", ANA)
    assert bloqueios.autor_de(bloqueios.quem_rotina("ronda")) == Autor("rotina", nome="ronda")
    for ruim in ("ana", "admin:nao-e-uuid", "rotina:Ronda Grande", "chefe:" + str(ANA)):
        with pytest.raises(BloqueioInvalido):
            bloqueios.autor_de(ruim)
    assert bloqueios.descrever_quem(bloqueios.quem_admin(ANA)) == "O ADMIN"
    assert bloqueios.descrever_quem(bloqueios.quem_pessoa(ANA)) == "A propria pessoa"
    assert bloqueios.descrever_quem("rotina:ronda") == "A rotina ronda"


def test_pessoa_liga_so_no_proprio_alcance() -> None:
    ana = Autor("pessoa", ANA)
    assert bloqueios.pode_ligar(ana, "pausa", "pessoa", ANA)
    assert bloqueios.pode_ligar(ana, "bloqueio", "pessoa_aplicativo", ANA)
    assert not bloqueios.pode_ligar(ana, "pausa", "pessoa", BIA)
    for alcance in ("aplicativo", "sistema", "tudo"):
        assert not bloqueios.pode_ligar(ana, "pausa", alcance, None)
    for papel in ("admin", "rotina"):
        autor = Autor(papel, ANA if papel == "admin" else None, "ronda" if papel == "rotina" else None)  # type: ignore[arg-type]
        assert all(bloqueios.pode_ligar(autor, t, a, None) for t in TIPOS for a in ALCANCES)


def test_bloqueio_so_o_admin_encerra_e_pausa_a_propria_pessoa_retoma() -> None:
    admin, ana, bia, ronda = (
        Autor("admin", uuid.uuid4()),
        Autor("pessoa", ANA),
        Autor("pessoa", BIA),
        Autor("rotina", nome="ronda"),
    )
    for alcance in ALCANCES:
        assert bloqueios.pode_encerrar(_b("bloqueio", alcance), admin)
        assert bloqueios.pode_encerrar(_b("pausa", alcance), admin)
        assert not bloqueios.pode_encerrar(_b("bloqueio", alcance), ana)  # nem o bloqueio que ela mesma ligou
        assert not bloqueios.pode_encerrar(_b("pausa", alcance), ronda)
        assert not bloqueios.pode_encerrar(_b("pausa", alcance), bia)
    assert bloqueios.pode_encerrar(_b("pausa", "pessoa"), ana)
    assert bloqueios.pode_encerrar(_b("pausa", "pessoa_aplicativo"), ana)
    for alcance in ("aplicativo", "sistema", "tudo"):
        assert not bloqueios.pode_encerrar(_b("pausa", alcance), ana)


def test_cada_bloqueio_tem_frase_legivel() -> None:
    for tipo in TIPOS:
        for alcance in ALCANCES:
            b = _b(tipo, alcance)
            assert "Nada foi enviado" in b.mensagem()
            assert b.motivo_recusa() == f"{tipo}_{alcance}"
    assert "So o ADMIN" in _b("bloqueio", "pessoa").mensagem()
    assert "retomar" in _b("pausa", "pessoa").mensagem()


def test_vigentes_geral_vale_para_todo_sistema() -> None:
    vazio = bloqueios.Vigentes()
    assert vazio.para(SIS) is None
    so_sistema = bloqueios.Vigentes(None, {SIS: _b("pausa", "sistema")})
    assert so_sistema.para(SIS) is not None
    assert so_sistema.para(uuid.uuid4()) is None
    geral = bloqueios.Vigentes(_b("bloqueio", "tudo"))
    assert geral.para(uuid.uuid4()) is geral.geral


@pytest.mark.parametrize(
    ("nivel", "le", "muda"),
    [("aberta", True, True), ("so_leitura", True, False), ("fechada", False, False), ("outro", False, False)],
)
def test_estado_da_entrada(nivel: str, le: bool, muda: bool) -> None:
    assert estado.permite(nivel, "le") is le
    assert estado.permite(nivel, "muda") is muda
    assert estado.permite(nivel, "tem_volta") is muda


def test_mensagens_das_portas() -> None:
    assert "fechada" in estado.EstadoEntrada("fechada").mensagem()
    assert "so leituras" in estado.EstadoEntrada("so_leitura").mensagem()
    assert estado.EstadoEntrada("so_leitura").motivo_recusa() == "entrada_so_leitura"
    assert "Nada foi enviado" in estado.MENSAGEM_SAIDA_FECHADA
    assert "Nada foi enviado" in str(estado.SaidaFechada("cofre trancado"))


def test_sistema_compartilhado_so_aceita_leitura_na_revisao() -> None:
    revisao.conferir_classe("compartilhada", "le")
    for classe in ("muda", "tem_volta"):
        with pytest.raises(revisao.RevisaoRecusada, match="chave compartilhada"):
            revisao.conferir_classe("compartilhada", classe)
        revisao.conferir_classe("por_pessoa", classe)
        revisao.conferir_classe("programa", classe)
    with pytest.raises(revisao.RevisaoRecusada, match="Classe desconhecida"):
        revisao.conferir_classe("por_pessoa", "apaga")
    assert not revisao.classe_permitida("compartilhada", "muda")


def test_catraca_fala_o_teto() -> None:
    texto = catraca.mensagem(200)
    assert "200 leituras por dia" in texto
    assert "Nada foi enviado" in texto
    assert catraca.Passagem(False, 200, 200).mensagem() == texto


def test_frases_novas_montam_com_os_campos_certos() -> None:
    assert frase("bloqueio_ligado", quem="O ADMIN", alcance="todo o MCP-CLARO") == (
        "O ADMIN ligou um bloqueio em todo o MCP-CLARO."
    )
    assert frase("pausa_encerrada", quem="A propria pessoa", alcance="uma pessoa num aplicativo").startswith(
        "A propria pessoa encerrou uma pausa"
    )
    assert "venda" in frase("autorizacao_concluida", sistema="venda")
    assert "derrubada" in frase("autorizacao_derrubada", sistema="s", motivo="pessoa desativada")
    assert frase("pedido_recusado", metodo="tools/list", motivo="entrada_fechada")
    assert (
        frase("estado_saida_mudou", quem="A rotina ronda", acao="fechou") == "A rotina ronda fechou a porta de saida."
    )
    for tipo, campos in {
        "autorizacao_configurada": {"sistema": "s"},
        "segredo_cliente_guardado": {"sistema": "s"},
        "autorizacao_iniciada": {"sistema": "s"},
        "autorizacao_recusada": {"sistema": "s", "motivo": "m"},
        "chave_renovada": {"sistema": "s"},
        "renovacao_falhou": {"sistema": "s", "motivo": "m"},
        "bloqueio_encerrado": {"quem": "q", "alcance": "a"},
        "pausa_ligada": {"quem": "q", "alcance": "a"},
        "estado_entrada_mudou": {"quem": "q", "nivel": "fechada"},
    }.items():
        assert frase(tipo, **campos)
