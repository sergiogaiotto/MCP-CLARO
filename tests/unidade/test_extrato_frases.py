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
