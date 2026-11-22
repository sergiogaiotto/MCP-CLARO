import pytest

from mcp_claro.extrato.frases import FRASES, FraseDesconhecida, frase
from mcp_claro.extrato.registro import _sem_conteudo


def test_frase_exige_campos_exatos() -> None:
    assert frase("fechamento", numero=3, linhas=10) == "Fechamento 3 calculado com 10 linhas."
    with pytest.raises(ValueError, match="faltam"):
        frase("fechamento", numero=3)
    with pytest.raises(ValueError, match="sobram"):
        frase("fechamento", numero=3, linhas=1, extra="x")
    with pytest.raises(FraseDesconhecida):
        frase("nao_existe")


def test_frases_sem_jargao_de_segredo() -> None:
    for f in FRASES.values():
        assert "token" not in f.modelo.lower()
        assert "senha" not in f.modelo.lower()


def test_extrato_recusa_conteudo() -> None:
    with pytest.raises(ValueError, match="nao guarda conteudo"):
        _sem_conteudo({"argumentos": {"x": 1}})
    assert _sem_conteudo({"motivo": "audiencia"}) == {"motivo": "audiencia"}


def test_frase_do_comprovante_e_so_a_de_tres_partes() -> None:
    """A frase antiga do comprovante (duas partes) saiu: o comprovante do bloqueio grava a de tres partes."""
    assert "comprovante_bloqueio" not in FRASES
    assert FRASES["comprovante_bloqueio_partes"].campos() == {"paradas", "ja_saidas", "em_conferencia"}


# --- Fase 3: servidores de terceiros ------------------------------------------------------------------------

FASE3 = {
    "admin_terceiro_cadastrado": {"sistema": "Fornecedor CRM", "quem": "Carla"},
    "admin_servidor_revisado": {"sistema": "Fornecedor CRM", "quem": "Carla", "publicadas": 2, "fora": 1},
    "admin_servidor_revisao_recusada": {"sistema": "Fornecedor CRM", "motivo": "ler_a mudou desde a revisao"},
    "sistema_em_quarentena": {"sistema": "Fornecedor CRM"},
    "sistema_em_quarentena_sem_resposta": {"sistema": "Fornecedor CRM", "rodadas": 2},
    "sistema_saiu_da_quarentena": {"sistema": "Fornecedor CRM", "quem": "Carla"},
    "leitura_sistema_pediu_dados": {"sistema": "Fornecedor CRM", "pedidos": "dados por formulario", "ferramenta": "x"},
    "programa_leitura_sistema_pediu_dados": {
        "sistema": "Fornecedor CRM",
        "pedidos": "um link",
        "ferramenta": "x",
        "programa": "Robo",
        "responsavel": "Rui",
    },
    # Contrato K4 (R6e-2): pedido barrado fora de uma chamada, sem pessoa e sem ferramenta.
    "declaracoes_sistema_pediu_dados": {"sistema": "Fornecedor CRM", "pedidos": "algo fora do padrao 2026-07-28"},
    "sondagem_sistema_pediu_dados": {"sistema": "Fornecedor CRM", "pedidos": "dados por formulario"},
    "tres_sinais": {"protocolo": "CLR-2026-00000001", "de_fora": "ler_email", "privado": "ver_cadastro"},
    "limite_suspenso": {"ferramenta": "enviar_x", "motivo": "o servidor entrou em quarentena (mudou, parou)"},
    "sala_desvio_recusado": {"sistema": "Fornecedor CRM"},
    "sala_endereco_recusado": {"sistema": "Fornecedor CRM"},
    "sala_fora": {"sistema": "Fornecedor CRM"},
}


@pytest.mark.parametrize(("tipo", "campos"), FASE3.items())
def test_frases_da_fase_3_com_os_campos_exatos(tipo: str, campos: dict[str, object]) -> None:
    assert FRASES[tipo].campos() == set(campos)
    texto = frase(tipo, **campos)
    for valor in campos.values():
        assert str(valor) in texto
    assert texto.isascii(), "frase do extrato sem acento (D19)"


@pytest.mark.parametrize("tipo", FASE3)
def test_frases_da_fase_3_sem_segredo_nem_conteudo(tipo: str) -> None:
    modelo = FRASES[tipo].modelo.lower()
    for proibida in ("token", "senha", "argumento", "resultado", "://"):
        assert proibida not in modelo, (tipo, proibida)
    # Nenhum campo da frase e uma chave de conteudo que o extrato recusa.
    for campo in FRASES[tipo].campos():
        assert _sem_conteudo({campo: 1}) == {campo: 1}


# O texto inteiro de cada frase, com os campos de FASE3. Os da secao 4.2 do desenho estao letra por letra; os outros
# vem dos ajustes (sala_fora, A2; sistema_em_quarentena_sem_resposta, A5; limite_suspenso, M2).
TEXTOS_FASE3 = {
    "admin_terceiro_cadastrado": (
        "Servidor de terceiros Fornecedor CRM cadastrado por Carla; sera atendido na sala de visitas; falta a sondagem"
        " de versao."
    ),
    "admin_servidor_revisado": (
        "Servidor de terceiros Fornecedor CRM revisado por Carla: 2 ferramentas na vitrine, 1 de fora."
    ),
    "admin_servidor_revisao_recusada": "Revisao do servidor Fornecedor CRM nao concluida: ler_a mudou desde a revisao.",
    "sistema_em_quarentena": (
        "Servidor Fornecedor CRM em quarentena: mudou, parou. Ele mudou depois da revisao; nada dele segue ate nova"
        " revisao do ADMIN."
    ),
    "sistema_em_quarentena_sem_resposta": (
        "Servidor Fornecedor CRM em quarentena: nao respondeu a conferencia em 2 rodadas seguidas; nada dele segue ate"
        " nova revisao do ADMIN."
    ),
    "sistema_saiu_da_quarentena": (
        "Servidor Fornecedor CRM saiu da quarentena pela revisao de Carla; o que ele declara bate com o que foi"
        " revisado."
    ),
    "leitura_sistema_pediu_dados": (
        "O sistema Fornecedor CRM pediu dados por formulario no meio da leitura em x; o MCP-CLARO recusou e nao"
        " repassou o pedido."
    ),
    "programa_leitura_sistema_pediu_dados": (
        "O sistema Fornecedor CRM pediu um link no meio da leitura em x, pelo programa Robo, responsavel Rui; o"
        " MCP-CLARO recusou e nao repassou o pedido."
    ),
    "declaracoes_sistema_pediu_dados": (
        "O sistema Fornecedor CRM pediu algo fora do padrao 2026-07-28 no meio da leitura das ferramentas; o MCP-CLARO"
        " recusou e nao repassou o pedido."
    ),
    "sondagem_sistema_pediu_dados": (
        "O sistema Fornecedor CRM pediu dados por formulario no meio da sondagem de versao; o MCP-CLARO recusou e nao"
        " repassou o pedido."
    ),
    "tres_sinais": (
        "Alerta dos tres sinais no protocolo CLR-2026-00000001: o aplicativo leu texto de fora em ler_email e viu"
        " dados pessoais em ver_cadastro; nenhum limite valeu, e a acao foi para a ficha."
    ),
    "limite_suspenso": "O limite de enviar_x foi suspenso: o servidor entrou em quarentena (mudou, parou).",
    "sala_desvio_recusado": (
        "O servidor de terceiros Fornecedor CRM pediu para mandar o pedido a outro endereco; o MCP-CLARO nao seguiu."
    ),
    "sala_endereco_recusado": (
        "O endereco do servidor de terceiros Fornecedor CRM nao e publico ou nao usa https; nada foi enviado."
    ),
    "sala_fora": "A sala de visitas nao esta disponivel; nada foi enviado ao servidor de terceiros Fornecedor CRM.",
}


def test_cada_frase_da_fase_3_tem_o_texto_conferido() -> None:
    assert set(TEXTOS_FASE3) == set(FASE3)


@pytest.mark.parametrize(("tipo", "esperado"), TEXTOS_FASE3.items())
def test_textos_das_frases_da_fase_3(tipo: str, esperado: str) -> None:
    assert frase(tipo, **FASE3[tipo]) == esperado


def test_pedido_barrado_na_leitura_tem_a_versao_de_programa() -> None:
    from mcp_claro.regras.programas import PARA_PROGRAMA

    assert PARA_PROGRAMA["leitura_sistema_pediu_dados"] == "programa_leitura_sistema_pediu_dados"
    assert FRASES["programa_leitura_sistema_pediu_dados"].campos() == (
        FRASES["leitura_sistema_pediu_dados"].campos() | {"programa", "responsavel"}
    )
