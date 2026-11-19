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
        # Ronda das portas (Fase 1): fechamento da entrada por etapas.
        Frase(
            "ronda_fechou_entrada",
            "A ronda das portas achou passagem indevida em {rondas} rondas seguidas e deixou a entrada {nivel}."
            " So o ADMIN reabre, depois de apurar.",
        ),
        Frase(
            "ronda_sem_fechamento",
            "A ronda das portas achou passagem indevida em {rondas} rondas seguidas. O fechamento sozinho esta"
            " desligado: a entrada continua {nivel}.",
        ),
        Frase("chave_guardada", "Chave {dono} guardada cifrada no cofre por {quem}."),
        Frase("politica_mudou", "Politica {chave} mudou para {valor} por {autor}: {motivo}."),
        # Regras (Fase 1): autorizacao nos sistemas, chaves, bloqueio, pausa e estado das portas.
        Frase("autorizacao_configurada", "A autorizacao por pessoa do sistema {sistema} foi configurada."),
        Frase("segredo_cliente_guardado", "O segredo do MCP-CLARO no sistema {sistema} foi guardado cifrado."),
        Frase("autorizacao_iniciada", "A pessoa comecou a autorizar o sistema {sistema}."),
        Frase(
            "autorizacao_concluida",
            "A pessoa autorizou o sistema {sistema}; a chave foi guardada cifrada no cofre do gateway.",
        ),
        Frase("autorizacao_recusada", "Autorizacao no sistema {sistema} recusada: {motivo}."),
        Frase("autorizacao_derrubada", "Autorizacao da pessoa no sistema {sistema} derrubada: {motivo}."),
        Frase("chave_renovada", "A chave da pessoa no sistema {sistema} foi renovada antes do uso."),
        Frase("renovacao_falhou", "A chave da pessoa no sistema {sistema} nao foi renovada: {motivo}."),
        Frase("pedido_recusado", "Pedido {metodo} recusado: {motivo}."),
        Frase("bloqueio_ligado", "{quem} ligou um bloqueio em {alcance}."),
        Frase("bloqueio_encerrado", "{quem} encerrou um bloqueio em {alcance}."),
        Frase("pausa_ligada", "{quem} ligou uma pausa em {alcance}."),
        Frase("pausa_encerrada", "{quem} encerrou uma pausa em {alcance}."),
        Frase("estado_entrada_mudou", "{quem} deixou a entrada {nivel}."),
        Frase("estado_saida_mudou", "{quem} {acao} a porta de saida."),
        # Rotinas e avisos (Fase 1): vencimentos, mudou-parou, avisos e alertas.
        Frase("ordem_vencida", "A ordem de servico {modelo} chegou ao fim do prazo e deixou de valer."),
        Frase("ordem_inativa", "A ordem de servico {modelo} ficou inativa: {dias} dias sem uso."),
        Frase("ordem_a_vencer", "A ordem de servico {modelo} vence em {fim}; a pessoa foi avisada."),
        Frase(
            "ferramenta_suspensa",
            "Ferramenta {ferramenta} suspensa: mudou, parou. O sistema passou a pedir mais que o revisado.",
        ),
        Frase("ferramenta_mudou", "Ferramenta {ferramenta} mudou no sistema sem pedir mais; so registramos."),
        Frase("aviso_desistiu", "Aviso {evento} nao foi entregue depois de {tentativas} tentativas; desistimos."),
        Frase("resumo_semanal", "Resumo da semana {semana} posto na fila para o ADMIN."),
        Frase("alerta_grafana", "Alerta do Grafana {alerta} recebido: {situacao}."),
        # Telas (Fase 1): login, ordens de servico, extrato alheio e cadastro do ADMIN.
        Frase("telas_entrada", "Entrada nas telas do MCP-CLARO."),
        Frase("telas_saida", "Saida das telas do MCP-CLARO."),
        Frase("ordem_concedida", "Ordem de servico {modelo} dada ao aplicativo {aplicativo}, valida ate {fim}."),
        Frase("ordem_revogada", "Ordem de servico {modelo} do aplicativo {aplicativo} revogada pela pessoa."),
        Frase("extrato_alheio_aberto", "O ADMIN {quem} abriu este extrato. Motivo: {motivo}."),
        Frase("admin_aplicativo_habilitado", "Aplicativo {aplicativo} habilitado por {quem}."),
        Frase("admin_sistema_cadastrado", "Sistema {sistema} cadastrado por {quem}; falta a sondagem de versao."),
        Frase("admin_sondagem_pedida", "Sondagem de versao do sistema {sistema} pedida por {quem}: {desfecho}."),
        Frase("admin_declaracoes_lidas", "Ferramentas do sistema {sistema} lidas por {quem}: {quantas} declaradas."),
        Frase(
            "admin_ferramenta_revisada",
            "Ferramenta {ferramenta} do sistema {sistema} revisada por {quem}: classe {classe}, risco {risco}.",
        ),
        Frase("admin_modelo_criado", "Modelo de ordem {modelo} criado por {quem}, com {quantas} ferramentas."),
        Frase(
            "admin_programa_cadastrado",
            "Programa {aplicativo} cadastrado por {quem}; responsavel {responsavel}, substituto {substituto}.",
        ),
        Frase("admin_webhook_cadastrado", "Destino de webhook {destino} cadastrado por {quem}."),
        Frase("admin_webhook_mudou", "Destino de webhook {destino} {situacao} por {quem}."),
        # Telas (Fase 1): bloqueio, pausa, portas, pessoas, autorizacao e chaves pelo ADMIN.
        Frase("admin_bloqueio_ligado", "Bloqueio ligado por {quem} em {alcance}."),
        Frase("admin_bloqueio_encerrado", "Bloqueio encerrado por {quem} em {alcance}."),
        Frase("admin_pausa_ligada", "Pausa ligada por {quem} em {alcance}."),
        Frase("admin_pausa_encerrada", "Pausa encerrada por {quem} em {alcance}."),
        Frase("admin_entrada_mudou", "Entrada do MCP-CLARO deixada {nivel} por {quem}."),
        Frase("admin_saida_mudou", "Porta de saida {situacao} por {quem}."),
        Frase("pessoa_desativada", "Conta desativada pelo ADMIN {quem}."),
        Frase("admin_autorizacao_configurada", "Autorizacao por pessoa do sistema {sistema} configurada por {quem}."),
        Frase(
            "admin_segredo_cliente_entregue",
            "Segredo do MCP-CLARO no sistema {sistema} entregue ao cofre por {quem}; as telas nao o guardam.",
        ),
        Frase(
            "admin_chave_entregue",
            "Chave {dono} do sistema {sistema} entregue ao cofre por {quem}; as telas nao a guardam.",
        ),
        # Continuidade do banco (Fase 1): conferencia na partida do despachante e roteiro de restauracao.
        Frase(
            "continuidade_primeira",
            "Primeira partida neste banco: o identificador e a linha do tempo do PostgreSQL foram guardados.",
        ),
        Frase(
            "continuidade_nao_confere",
            "A conferencia do banco na partida nao bateu: {motivo}. A porta de saida ficou fechada ate decisao humana.",
        ),
        Frase("intervalo_perdido", "Pode faltar no extrato o que aconteceu entre {desde} e {ate}."),
        Frase(
            "aplicativo_pausado_continuidade",
            "O aplicativo {aplicativo} voltou pausado depois da conferencia do banco; reativar e decisao humana.",
        ),
        Frase(
            "restauracao_marcada",
            "O roteiro de restauracao marcou o banco como restaurado; a porta de saida fecha na proxima partida.",
        ),
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
