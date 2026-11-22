"""Frases-padrao novas das rotinas e dos avisos."""

from __future__ import annotations

from mcp_claro.extrato.frases import FRASES, frase

NOVAS = {
    "ordem_vencida": {"modelo": "leitura-faturas"},
    "ordem_inativa": {"modelo": "leitura-faturas", "dias": 14},
    "ordem_a_vencer": {"modelo": "leitura-faturas", "fim": "25/11/2026"},
    "ferramenta_suspensa": {"ferramenta": "fatura_x"},
    "ferramenta_mudou": {"ferramenta": "fatura_x"},
    "aviso_desistiu": {"evento": "ordem_vencida", "tentativas": 6},
    "resumo_semanal": {"semana": "2026-W46"},
    "alerta_grafana": {"alerta": "Recusas por versao", "situacao": "disparou"},
}


def test_frases_novas_montam_com_os_campos_exatos() -> None:
    for tipo, campos in NOVAS.items():
        assert tipo in FRASES, tipo
        texto = frase(tipo, **campos)
        for valor in campos.values():
            assert str(valor) in texto


def test_mudou_parou_aparece_na_frase() -> None:
    assert "mudou, parou" in frase("ferramenta_suspensa", ferramenta="x")
    assert "so registramos" in frase("ferramenta_mudou", ferramenta="x")


def test_frases_novas_sem_segredo_nem_conteudo() -> None:
    for tipo in NOVAS:
        modelo = FRASES[tipo].modelo.lower()
        for proibida in ("token", "senha", "argumento", "resultado", "e-mail", "cpf"):
            assert proibida not in modelo, (tipo, proibida)


# --- Fase 3: avisos ao ADMIN (por e-mail e webhook) ----------------------------------------------------------

AVISOS_FASE3 = {
    "sistema_em_quarentena": (
        "MCP-CLARO: servidor de terceiros em quarentena (mudou, parou)",
        {"sistema": "Fornecedor CRM", "quantas": 2},
    ),
    "sistema_em_quarentena_sem_resposta": (
        "MCP-CLARO: servidor de terceiros em quarentena (nao respondeu a conferencia)",
        {"sistema": "Fornecedor CRM", "rodadas": 2},
    ),
    "sistema_em_quarentena_revisao_inconsistente": (
        "MCP-CLARO: servidor de terceiros em quarentena (revisao que nao confere)",
        {"sistema": "Fornecedor CRM"},
    ),
}

# O texto inteiro de cada aviso, com os campos de AVISOS_FASE3. A quarentena cancela o que esperava e suspende os
# limites das pessoas com o servidor (critica M2).
TEXTOS_AVISOS_FASE3 = {
    "sistema_em_quarentena": (
        "O servidor de terceiros Fornecedor CRM mudou 2 ferramentas (mudadas, novas ou sumidas) depois da revisao do"
        " servidor. Ele entrou em quarentena: nada dele chega as pessoas, o que esperava para sair foi cancelado e os"
        " limites das pessoas com ele foram suspensos. Para voltar, leia as ferramentas, revise as que mudaram e"
        " conclua a revisao do servidor nas telas do ADMIN."
    ),
    "sistema_em_quarentena_sem_resposta": (
        "O servidor de terceiros Fornecedor CRM nao respondeu a conferencia do mudou-parou em 2 rodadas seguidas. Ele"
        " entrou em quarentena: nada dele chega as pessoas, o que esperava para sair foi cancelado e os limites das"
        " pessoas com ele foram suspensos. Confira o servidor com o fornecedor; depois, leia as ferramentas e conclua"
        " a revisao do servidor nas telas do ADMIN."
    ),
    "sistema_em_quarentena_revisao_inconsistente": (
        "A revisao do servidor de terceiros Fornecedor CRM guardada no MCP-CLARO nao confere com a lista de"
        " ferramentas dela. Ele entrou em quarentena: nada dele chega as pessoas, o que esperava para sair foi"
        " cancelado e os limites das pessoas com ele foram suspensos. Leia as ferramentas e conclua a revisao do"
        " servidor de novo nas telas do ADMIN; se acontecer outra vez, fale com a equipe do MCP-CLARO."
    ),
}


def test_textos_dos_avisos_da_fase_3() -> None:
    from mcp_claro.extrato.frases import aviso_admin

    assert set(TEXTOS_AVISOS_FASE3) == set(AVISOS_FASE3)
    for evento, (assunto, campos) in AVISOS_FASE3.items():
        assert aviso_admin(evento, **campos) == (assunto, TEXTOS_AVISOS_FASE3[evento])


def test_avisos_da_fase_3_tem_assunto_fixo_e_campos_exatos() -> None:
    import pytest

    from mcp_claro.extrato.frases import AVISOS_ADMIN, FraseDesconhecida, aviso_admin

    assert set(AVISOS_ADMIN) == set(AVISOS_FASE3)
    for evento, (assunto, campos) in AVISOS_FASE3.items():
        assert AVISOS_ADMIN[evento].campos() == set(campos)
        recebido, texto = aviso_admin(evento, **campos)
        assert recebido == assunto
        for valor in campos.values():
            assert str(valor) in texto
        assert (assunto + texto).isascii()
        with pytest.raises(ValueError, match="faltam"):
            aviso_admin(evento)
        with pytest.raises(ValueError, match="sobram"):
            aviso_admin(evento, **campos, extra=1)
    with pytest.raises(FraseDesconhecida):
        aviso_admin("nao_existe")


def test_avisos_da_fase_3_sem_segredo_nem_promessa() -> None:
    from mcp_claro.extrato.frases import AVISOS_ADMIN

    for evento, a in AVISOS_ADMIN.items():
        texto = (a.assunto + " " + a.texto).lower()
        for proibida in ("token", "senha", "argumento", "resultado", "garant", "://"):
            assert proibida not in texto, (evento, proibida)
    # O aviso de quarentena diz "mudou, parou" e o caminho de volta, como o extrato.
    assert "mudou, parou" in AVISOS_ADMIN["sistema_em_quarentena"].assunto
    assert "conclua a revisao do servidor" in AVISOS_ADMIN["sistema_em_quarentena"].texto


def test_frases_do_extrato_dos_avisos_da_fase_3() -> None:
    assert "mudou, parou" in frase("sistema_em_quarentena", sistema="CRM")
    assert "2 rodadas seguidas" in frase("sistema_em_quarentena_sem_resposta", sistema="CRM", rodadas=2)
    for tipo in ("sistema_em_quarentena", "sistema_em_quarentena_sem_resposta", "sistema_saiu_da_quarentena"):
        modelo = FRASES[tipo].modelo.lower()
        for proibida in ("token", "senha", "argumento", "resultado", "e-mail", "cpf"):
            assert proibida not in modelo, (tipo, proibida)


# --- Fase 4: avisos ao ADMIN sobre os selos (desenho 7.4) ----------------------------------------------------

AVISOS_FASE4 = {
    "selo_a_vencer": (
        "MCP-CLARO: selo de sistema perto de vencer",
        {"nivel": 2, "sistema": "Cobranca", "dias": 14, "vale_ate": "20/02/2027"},
    ),
    "selo_nao_aprovado": ("MCP-CLARO: teste de selo nao aprovado", {"nivel": 3, "sistema": "Cobranca", "falhas": 4}),
    "selo_retirado": (
        "MCP-CLARO: selo de sistema retirado",
        {"sistema": "Cobranca", "motivo": "o sistema nao respondeu a 3 consultas seguidas"},
    ),
    "sistema_contradito": (
        "MCP-CLARO: uma pessoa registrou o contrario do que um sistema informou",
        {"sistema": "Cobranca", "protocolo": "CLR-2026-00000007"},
    ),
}

TEXTOS_AVISOS_FASE4 = {
    "selo_a_vencer": (
        "O selo de nivel 2 do sistema Cobranca vence em 14 dias (20/02/2027). Sem um teste novo aprovado, o sistema"
        " volta ao nivel 1 quando o selo vencer. Para pedir o teste, abra o sistema nas telas do ADMIN."
    ),
    "selo_nao_aprovado": (
        "O teste de selo nivel 3 do sistema Cobranca nao foi aprovado pelo criterio do MCP-CLARO: 4 casos falharam ou"
        " ficaram sem teste. O sistema fica no nivel 1. O relatorio do teste esta na pagina do sistema, nas telas do"
        " ADMIN."
    ),
    "selo_retirado": (
        "O selo do sistema Cobranca foi retirado: o sistema nao respondeu a 3 consultas seguidas. O sistema volta ao"
        " nivel 1: o MCP-CLARO deixa de mandar o numero da operacao e de perguntar a ele o que houve. Para voltar a"
        " um nivel maior, peca um teste novo nas telas do ADMIN."
    ),
    "sistema_contradito": (
        "Uma pessoa registrou sobre o protocolo CLR-2026-00000007 o contrario do que o sistema Cobranca informou. O"
        " selo continua, porque a pessoa pode ter se enganado. Veja o extrato do protocolo e decida, nas telas do"
        " ADMIN, se retira o selo."
    ),
}


def test_avisos_da_fase_4_com_assunto_fixo_texto_e_campos_exatos() -> None:
    import pytest

    from mcp_claro.extrato.frases import AVISOS_ADMIN, AVISOS_ADMIN_SELO, aviso_admin

    assert set(AVISOS_ADMIN_SELO) == set(AVISOS_FASE4) == set(TEXTOS_AVISOS_FASE4)
    assert not set(AVISOS_ADMIN) & set(AVISOS_ADMIN_SELO)
    for evento, (assunto, campos) in AVISOS_FASE4.items():
        assert AVISOS_ADMIN_SELO[evento].campos() == set(campos)
        assert aviso_admin(evento, **campos) == (assunto, TEXTOS_AVISOS_FASE4[evento])
        assert (assunto + TEXTOS_AVISOS_FASE4[evento]).isascii()
        with pytest.raises(ValueError, match="faltam"):
            aviso_admin(evento)
        with pytest.raises(ValueError, match="sobram"):
            aviso_admin(evento, **campos, extra=1)


def test_avisos_da_fase_4_sem_segredo_promessa_nem_o_nome_do_rascunho() -> None:
    from mcp_claro.extrato.frases import AVISOS_ADMIN_SELO

    for evento, a in AVISOS_ADMIN_SELO.items():
        texto = (a.assunto + " " + a.texto).lower()
        for proibida in ("token", "senha", "garant", "certificado", "aprovado no", "mws", "write-safety", "://"):
            assert proibida not in texto, (evento, proibida)
