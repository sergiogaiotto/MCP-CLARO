"""Extrato unico: linhas so de acrescentar, frases-padrao e fechamento."""

from mcp_claro.extrato.frases import FRASES, Frase, frase
from mcp_claro.extrato.registro import conferir_fechamento, fechar, registrar

__all__ = ["FRASES", "Frase", "conferir_fechamento", "fechar", "frase", "registrar"]
