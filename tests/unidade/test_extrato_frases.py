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


# --- Fase 4: selos de nivel 2 e 3 ---------------------------------------------------------------------------

FASE4 = {
    "admin_selo_declaracao_salva": {"sistema": "Cobranca", "quem": "Carla", "versao": 2},
    "admin_selo_teste_pedido": {"nivel": 2, "sistema": "Cobranca", "quem": "Carla"},
    "selo_testado": {"nivel": 3, "sistema": "Cobranca", "vale_ate": "20/02/2027"},
    "selo_nao_aprovado": {"nivel": 3, "sistema": "Cobranca", "falhas": 4},
    "selo_teste_interrompido": {"sistema": "Cobranca", "motivo": "o despachante caiu no meio"},
    "selo_retirado": {"sistema": "Cobranca", "motivo": "o sistema deu respostas que nao combinam"},
    "selo_a_vencer": {"nivel": 2, "sistema": "Cobranca", "vale_ate": "20/02/2027"},
    "sistema_informou": {"sistema": "Cobranca", "protocolo": "CLR-2026-00000007", "estado": "o pedido foi feito"},
    "sistema_nao_respondeu_consulta": {"sistema": "Cobranca", "protocolo": "CLR-2026-00000007"},
    "sistema_fora_consulta": {
        "sistema": "Cobranca",
        "protocolo": "CLR-2026-00000007",
        "motivo": "o sistema esta em quarentena",
    },
    "consulta_nao_feita": {"sistema": "Cobranca", "protocolo": "CLR-2026-00000007", "motivo": "o cofre esta trancado"},
    "sistema_informou_no_bloqueio": {"sistema": "Cobranca", "protocolo": "CLR-2026-00000007", "estado": "parada"},
    "aviso_bloqueio_sem_resposta": {"sistema": "Cobranca"},
    "aviso_bloqueio_sistema_fora": {"sistema": "Cobranca", "motivo": "o sistema esta em quarentena"},
    "aviso_bloqueio_dispensado": {"sistema": "Cobranca"},
    "acao_enviada_mesmo_numero": {
        "ferramenta": "enviar_lembrete",
        "original": "CLR-2026-00000007",
        "protocolo": "CLR-2026-00000009",
    },
    "acao_enviada_sem_numero": {"ferramenta": "enviar_lembrete", "protocolo": "CLR-2026-00000009"},
    "acao_enviada_selo_menor": {
        "ferramenta": "enviar_lembrete",
        "nivel": 1,
        "nivel_ficha": 2,
        "protocolo": "CLR-2026-00000009",
    },
    "sistema_se_contradisse": {"sistema": "Cobranca", "protocolo": "CLR-2026-00000007"},
    "sistema_contradito": {"sistema": "Cobranca", "protocolo": "CLR-2026-00000007"},
}

TEXTOS_FASE4 = {
    "admin_selo_declaracao_salva": (
        "Declaracao de teste do selo do sistema Cobranca salva por Carla (versao 2); a bateria so escreve no alvo"
        " declarado, e so se ele se declarar de teste."
    ),
    "admin_selo_teste_pedido": "Teste de selo nivel 2 do sistema Cobranca pedido por Carla.",
    "selo_testado": (
        "Teste de selo nivel 3 do sistema Cobranca: aprovado pelo criterio do MCP-CLARO; o sistema fica no nivel 3"
        " ate 20/02/2027."
    ),
    "selo_nao_aprovado": (
        "Teste de selo nivel 3 do sistema Cobranca: nao aprovado pelo criterio do MCP-CLARO, 4 casos falharam ou"
        " ficaram sem teste; o sistema fica no nivel 1."
    ),
    "selo_teste_interrompido": (
        "Teste de selo do sistema Cobranca interrompido: o despachante caiu no meio; nada mudou no selo."
    ),
    "selo_retirado": (
        "Selo do sistema Cobranca retirado: o sistema deu respostas que nao combinam; o sistema volta ao nivel 1."
    ),
    "selo_a_vencer": "O selo de nivel 2 do sistema Cobranca vence em 20/02/2027; o ADMIN foi avisado.",
    "sistema_informou": (
        "O sistema Cobranca informou sobre o protocolo CLR-2026-00000007: o pedido foi feito. E o que o sistema diz;"
        " o MCP-CLARO nao confere do lado de la."
    ),
    "sistema_nao_respondeu_consulta": (
        "O sistema Cobranca nao respondeu a consulta sobre o protocolo CLR-2026-00000007; a decisao fica com a pessoa."
    ),
    "sistema_fora_consulta": (
        "O MCP-CLARO nao perguntou ao sistema Cobranca sobre o protocolo CLR-2026-00000007: o sistema esta em"
        " quarentena; a decisao fica com a pessoa."
    ),
    "consulta_nao_feita": (
        "O MCP-CLARO nao conseguiu perguntar ao sistema Cobranca sobre o protocolo CLR-2026-00000007: o cofre esta"
        " trancado. A falha e do MCP-CLARO, nao do sistema."
    ),
    "sistema_informou_no_bloqueio": (
        "Depois do bloqueio, o sistema Cobranca informou sobre o protocolo CLR-2026-00000007: parada. E o que o"
        " sistema diz."
    ),
    "aviso_bloqueio_sem_resposta": (
        "O sistema Cobranca nao respondeu ao aviso do bloqueio; o comprovante fica sem o que ele informaria."
    ),
    "aviso_bloqueio_sistema_fora": (
        "O MCP-CLARO nao avisou o sistema Cobranca do bloqueio: o sistema esta em quarentena; o comprovante fica sem"
        " o que ele informaria."
    ),
    "aviso_bloqueio_dispensado": (
        "O bloqueio acabou antes de a porta de saida abrir; o MCP-CLARO nao avisou o sistema Cobranca."
    ),
    "acao_enviada_mesmo_numero": (
        "Acao enviar_lembrete enviada uma vez ao sistema com o mesmo numero do protocolo CLR-2026-00000007;"
        " protocolo CLR-2026-00000009."
    ),
    "acao_enviada_sem_numero": (
        "Acao enviar_lembrete enviada uma vez ao sistema sem o numero do selo, porque o MCP-CLARO nao conseguiu"
        " grava-lo; protocolo CLR-2026-00000009. Se a resposta se perder, a decisao fica com a pessoa."
    ),
    "acao_enviada_selo_menor": (
        "Acao enviar_lembrete enviada uma vez ao sistema no nivel 1, abaixo do nivel 2 da ficha, porque o selo do"
        " sistema mudou depois; protocolo CLR-2026-00000009."
    ),
    "sistema_se_contradisse": (
        "O sistema Cobranca deu respostas que nao combinam sobre o protocolo CLR-2026-00000007; o selo dele foi"
        " retirado."
    ),
    "sistema_contradito": (
        "O que a pessoa registrou sobre o protocolo CLR-2026-00000007 difere do que o sistema Cobranca informou; o"
        " ADMIN foi avisado."
    ),
}

# O que a Fase 4 nao pode dizer (desenho 7.2 e 12.1 G0, D26): segredo, promessa, certificacao, o nome do rascunho
# perto do selo, conteudo e enderecos.
PROIBIDAS_FASE4 = (
    "senha",
    "token",
    "garante",
    "garant",
    "certificado",
    "aprovado no",
    "mws",
    "write-safety",
    "argumento",
    "resultado",
    "://",
    "chave",
)


@pytest.mark.parametrize(("tipo", "campos"), FASE4.items())
def test_frases_da_fase_4_com_os_campos_exatos(tipo: str, campos: dict[str, object]) -> None:
    assert FRASES[tipo].campos() == set(campos)
    texto = frase(tipo, **campos)
    for valor in campos.values():
        assert str(valor) in texto
    assert texto.isascii(), "frase do extrato sem acento"


@pytest.mark.parametrize("tipo", FASE4)
def test_frases_da_fase_4_sem_segredo_promessa_nem_conteudo(tipo: str) -> None:
    modelo = FRASES[tipo].modelo.lower()
    for proibida in PROIBIDAS_FASE4:
        assert proibida not in modelo, (tipo, proibida)
    for campo in FRASES[tipo].campos():
        assert _sem_conteudo({campo: 1}) == {campo: 1}


def test_cada_frase_da_fase_4_tem_o_texto_conferido() -> None:
    assert set(TEXTOS_FASE4) == set(FASE4)


@pytest.mark.parametrize(("tipo", "esperado"), TEXTOS_FASE4.items())
def test_textos_das_frases_da_fase_4(tipo: str, esperado: str) -> None:
    assert frase(tipo, **FASE4[tipo]) == esperado


def test_as_frases_de_antes_da_fase_4_nao_mudaram() -> None:
    """D26: as frases novas entram sem mudar as de hoje. A impressao e a das 140 frases do commit 3c895a2 (Fase 3)."""
    import hashlib
    import json

    antigas = {t: f.modelo for t, f in FRASES.items() if t not in FASE4}
    assert len(antigas) == 140
    impressao = hashlib.sha256(json.dumps(sorted(antigas.items())).encode()).hexdigest()
    assert impressao == "5dc0934c0a10180e1bb89a86d13705abb691dda38562a6da6ad34b7b92fefcf5"


def test_frases_da_fase_4_dizem_o_que_o_desenho_e_a_critica_pedem() -> None:
    # A4: a bateria so escreve no alvo que se declara de teste.
    assert "so se ele se declarar de teste" in FRASES["admin_selo_declaracao_salva"].modelo
    # A2: fora de "ativo", nada e perguntado e a decisao fica com a pessoa; M7: a falha do MCP-CLARO tem frase propria.
    assert "a decisao fica com a pessoa" in FRASES["sistema_fora_consulta"].modelo
    assert "A falha e do MCP-CLARO, nao do sistema." in FRASES["consulta_nao_feita"].modelo
    # A1 e D22: a acao sai sem numero, nunca para a porta.
    assert "enviada uma vez ao sistema sem o numero do selo" in FRASES["acao_enviada_sem_numero"].modelo
    # Nenhuma linha leva o numero da operacao: nenhum campo chamado numero, operacao ou chave.
    for tipo in FASE4:
        assert not FRASES[tipo].campos() & {"numero", "operacao", "chave", "recibo"}
