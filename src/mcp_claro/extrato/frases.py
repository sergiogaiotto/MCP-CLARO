"""Frases-padrao do extrato, em linguagem simples.

Cada passo do gateway vira uma linha com um codigo (``tipo``) e uma frase
legivel. As frases nunca trazem argumento, resultado, chave nem dado pessoal:
so nomes de ferramenta, numeros de protocolo e motivos.
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Formatter
from typing import Final


@dataclass(frozen=True, slots=True)
class Frase:
    tipo: str
    modelo: str

    def campos(self) -> set[str]:
        return {nome for _, nome, _, _ in Formatter().parse(self.modelo) if nome}


FRASES: Final[dict[str, Frase]] = {
    f.tipo: f
    for f in (
        Frase("partida", "O papel {papel} subiu na versao {versao} do MCP-CLARO."),
        Frase(
            "recusa_versao_aplicativo",
            "Aplicativo recusado: falou a versao {versao_pedida}; so aceitamos {aceita}.",
        ),
        Frase(
            "recusa_versao_sistema",
            "Sistema recusado no cadastro: fala {versao_pedida}; so aceitamos {aceita}.",
        ),
        Frase("cracha_recusado", "Cracha recusado: {motivo}."),
        Frase("leitura_feita", "Leitura em {ferramenta} feita em nome da pessoa."),
        Frase("leitura_recusada", "Leitura em {ferramenta} recusada: {motivo}."),
        Frase("acao_pedida", "Acao {ferramenta} pedida; protocolo {protocolo}."),
        Frase("acao_enviada", "Acao {ferramenta} enviada uma vez ao sistema; protocolo {protocolo}."),
        Frase(
            "acao_nao_confirmada",
            "Resultado nao confirmado para o protocolo {protocolo}; nada sera reenviado.",
        ),
        Frase("acao_cancelada", "Acao do protocolo {protocolo} cancelada antes de sair."),
        Frase("bloqueio", "Bloqueio ligado por {quem}: {alcance}."),
        Frase("fechamento", "Fechamento {numero} calculado com {linhas} linhas."),
        Frase(
            "restauracao_detectada",
            "O banco foi restaurado; a porta de saida ficou fechada ate decisao humana.",
        ),
        Frase("ronda_falha", "A ronda das portas achou uma passagem indevida: {motivo}."),
        Frase("politica_mudou", "Politica {chave} mudou para {valor} por {autor}: {motivo}."),
    )
}


class FraseDesconhecida(KeyError):
    pass


def frase(tipo: str, **campos: object) -> str:
    """Monta a frase de um tipo, exigindo exatamente os campos do modelo."""
    try:
        f = FRASES[tipo]
    except KeyError as erro:
        raise FraseDesconhecida(tipo) from erro
    faltam = f.campos() - campos.keys()
    sobram = campos.keys() - f.campos()
    if faltam or sobram:
        raise ValueError(f"Frase {tipo}: faltam {sorted(faltam)}, sobram {sorted(sobram)}")
    return f.modelo.format(**campos)
