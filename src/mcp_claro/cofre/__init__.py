"""Cofre (OpenBao): login de maquina por AppRole, cifra e decifra pelo Transit e a chave do estado."""

from mcp_claro.cofre.cliente import (
    CAMPO_CHAVE_ESTADO,
    TAMANHO_CHAVE_ESTADO,
    AcessoNegado,
    ChaveEstado,
    ChaveEstadoInvalida,
    CifradoInvalido,
    CifraTransit,
    Cofre,
    CofreIndisponivel,
    CofreTrancado,
    ErroCofre,
    EstadoCofre,
    LoginRecusado,
    PedidoRecusado,
    contexto_tls,
)

__all__ = [
    "CAMPO_CHAVE_ESTADO",
    "TAMANHO_CHAVE_ESTADO",
    "AcessoNegado",
    "ChaveEstado",
    "ChaveEstadoInvalida",
    "CifraTransit",
    "CifradoInvalido",
    "Cofre",
    "CofreIndisponivel",
    "CofreTrancado",
    "ErroCofre",
    "EstadoCofre",
    "LoginRecusado",
    "PedidoRecusado",
    "contexto_tls",
]
