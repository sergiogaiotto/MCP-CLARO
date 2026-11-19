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
        # Fase 2: porta unica de saida, fichas, limites e "estamos confirmando".
        Frase(
            "acao_repetida",
            "Pedido igual ao do protocolo {protocolo}: devolvida a operacao original, nada novo saiu.",
        ),
        Frase("acao_recusada", "Acao em {ferramenta} recusada antes de virar protocolo: {motivo}."),
        Frase("ficha_confirmada", "Ficha do protocolo {protocolo} confirmada pela pessoa ({como})."),
        Frase("ficha_recusada", "Ficha do protocolo {protocolo} recusada pela pessoa; nada saiu."),
        Frase("ficha_vencida", "A ficha do protocolo {protocolo} venceu sem resposta; nada saiu."),
        Frase("ficha_avisada", "A pessoa foi avisada da ficha pendente do protocolo {protocolo}."),
        Frase("seguir_agora", "A pessoa pediu para a acao do protocolo {protocolo} seguir agora, sem esperar."),
        Frase("acao_parada", "Acao do protocolo {protocolo} parada antes de sair: {motivo}."),
        Frase("acao_nao_saiu", "Acao do protocolo {protocolo} nao saiu: {motivo}. Nada aconteceu no sistema."),
        Frase(
            "acao_sistema_pediu_dados",
            "O sistema pediu dados no meio da acao do protocolo {protocolo}; o MCP-CLARO nao repassa nem reenvia.",
        ),
        Frase("nao_confirmada_decidida", "A pessoa registrou sobre o protocolo {protocolo}: {decisao}."),
        Frase(
            "fila_parada_restauracao",
            "Banco restaurado: {paradas} acoes paradas na fila e {duvida} nao confirmadas; nada sai sem nova"
            " confirmacao.",
        ),
        Frase(
            "teto_confirmacoes",
            "O aplicativo {aplicativo} passou do teto de {teto} fichas por hora e foi pausado.",
        ),
        Frase(
            "despachante_assumiu",
            "Um despachante assumiu a porta de saida; {retomadas} acoes em envio ficaram nao confirmadas.",
        ),
        Frase("limite_criado", "A pessoa criou um limite de {por_dia} por dia para {ferramenta}, com passkey."),
        Frase("limite_ampliado", "A pessoa ampliou um limite de {ferramenta} para {por_dia} por dia, com passkey."),
        Frase("limite_encerrado", "O limite de {ferramenta} foi encerrado: {motivo}."),
        Frase("passkey_cadastrada", "A pessoa cadastrou uma passkey."),
        Frase("passkey_desativada", "Uma passkey da pessoa foi desativada."),
        # Fase 2: correcoes da revisao (programas).
        Frase(
            "programa_das_portas_mantido",
            "Programa {programa}, responsavel {responsavel}: a rotina nao o {acao} porque ele testa as portas do"
            " MCP-CLARO; {motivo}. O ADMIN precisa resolver na tela do programa.",
        ),
        Frase(
            "admin_programa_recadastrado",
            "Programa {programa}, responsavel {responsavel}, cadastrado de novo por {quem} depois de encerrado;"
            " substituto {substituto}, valido ate {fim}.",
        ),
        Frase(
            "admin_programa_fim_estendido",
            "Programa {programa}, responsavel {responsavel}: {quem} estendeu a data de fim de {antes} para {fim}.",
        ),
        Frase(
            "admin_programa_aplicativo_reativado",
            "Programa {programa}, responsavel {responsavel}: {quem} reativou o aplicativo do programa, que estava"
            " pausado.",
        ),
        # Acoes de um programa ("programa X, responsavel Y"), pelas mesmas regras de acao_*. Hoje so a recusa
        # acontece de verdade: com PROGRAM_ACTIONS_ENABLED desligado, nenhuma acao de programa vira protocolo.
        Frase(
            "programa_acao_recusada",
            "Acao em {ferramenta} pelo programa {programa}, responsavel {responsavel}, recusada antes de virar"
            " protocolo: {motivo}.",
        ),
        Frase(
            "programa_acao_pedida",
            "Acao {ferramenta} pedida pelo programa {programa}, responsavel {responsavel}; protocolo {protocolo}.",
        ),
        Frase(
            "programa_acao_repetida",
            "Pedido do programa {programa}, responsavel {responsavel}, igual ao do protocolo {protocolo}: devolvida"
            " a operacao original, nada novo saiu.",
        ),
        # Fase 2: correcoes da revisao (nucleo).
        Frase(
            "comprovante_bloqueio_partes",
            "Comprovante do bloqueio: {paradas} acoes paradas antes de sair; {ja_saidas} ja tinham saido;"
            " {em_conferencia} ainda em conferencia.",
        ),
        Frase(
            "ficha_avisada_webhooks",
            "O aviso da ficha pendente do protocolo {protocolo} foi so aos webhooks da instalacao; nao houve aviso"
            " por e-mail a pessoa.",
        ),
        Frase(
            "fila_restauracao_envio_com_dono",
            "Na conferencia do banco, {enviando} acoes estavam saindo por um despachante no ar; ele mesmo grava o"
            " fim delas.",
        ),
        # Fase 2: segunda rodada (telas).
        Frase(
            "limite_criado_login_refeito",
            "A pessoa criou um limite de {por_dia} por dia para {ferramenta}, com o login refeito na hora (passkey"
            " desligada nesta instalacao).",
        ),
        Frase(
            "limite_ampliado_login_refeito",
            "A pessoa ampliou um limite de {ferramenta} para {por_dia} por dia, com o login refeito na hora (passkey"
            " desligada nesta instalacao).",
        ),
        # Fase 2: terceira rodada (telas).
        Frase(
            "admin_aplicativo_reativado",
            "Aplicativo {aplicativo} reativado por {quem}; estava pausado pela conferencia do banco na partida.",
        ),
        # Fase 2: programas que rodam sozinhos (decisao 9).
        Frase(
            "programa_leitura_feita",
            "Leitura em {ferramenta} feita pelo programa {programa}, responsavel {responsavel}.",
        ),
        Frase(
            "programa_leitura_recusada",
            "Leitura em {ferramenta} pelo programa {programa}, responsavel {responsavel}, recusada: {motivo}.",
        ),
        Frase(
            "programa_pedido_recusado",
            "Pedido {metodo} do programa {programa}, responsavel {responsavel}, recusado: {motivo}.",
        ),
        Frase("programa_recusado", "Pedido do programa {programa}, responsavel {responsavel}, recusado: {motivo}."),
        Frase(
            "programa_sem_cadastro",
            "Pedido do aplicativo {aplicativo}, do tipo programa, recusado: falta o cadastro de programa, com"
            " responsavel.",
        ),
        Frase(
            "admin_programa_pausado",
            "Programa {programa}, responsavel {responsavel}, pausado por {quem}: {motivo}.",
        ),
        Frase("admin_programa_retomado", "Programa {programa}, responsavel {responsavel}, retomado por {quem}."),
        Frase(
            "admin_programa_encerrado",
            "Programa {programa}, responsavel {responsavel}, encerrado por {quem}: {motivo}.",
        ),
        Frase(
            "admin_programa_responsaveis",
            "Programa {programa}: {quem} passou a responsabilidade a {responsavel}, com substituto {substituto}.",
        ),
        Frase(
            "admin_programa_chave_trocada",
            "Programa {programa}, responsavel {responsavel}: {quem} registrou a troca da chave do programa.",
        ),
        Frase(
            "admin_programa_ordem",
            "Programa {programa}, responsavel {responsavel}: {quem} deu a ordem de servico {modelo}, valida ate {fim}.",
        ),
        Frase(
            "programa_recertificado",
            "Programa {programa}, responsavel {responsavel}: {quem} confirmou que ele segue valendo.",
        ),
        Frase(
            "programa_pausado_sem_uso",
            "Programa {programa}, responsavel {responsavel}, pausado: {dias} dias sem pedido.",
        ),
        Frase(
            "programa_pausado_revisao",
            "Programa {programa}, responsavel {responsavel}, pausado: a revisao de cada {dias} dias venceu sem"
            " confirmacao; o responsavel foi avisado.",
        ),
        Frase(
            "programa_pausado_chave",
            "Programa {programa}, responsavel {responsavel}, pausado: a chave passou de {dias} dias sem troca.",
        ),
        Frase(
            "programa_pausado_responsavel",
            "Programa {programa}, responsavel {responsavel}, pausado: o responsavel ou o substituto nao esta mais"
            " ativo.",
        ),
        Frase(
            "programa_chave_a_vencer",
            "A chave do programa {programa}, responsavel {responsavel}, vence em {fim}; o responsavel foi avisado.",
        ),
        Frase(
            "programa_encerrado_fim",
            "Programa {programa}, responsavel {responsavel}, encerrado: o cadastro valia ate {fim}.",
        ),
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
        # Fase 2: terceira rodada (nucleo).
        Frase(
            "cofre_fechou_saida",
            "A porta de saida fechou porque {motivo}. Nenhuma acao sai para os sistemas; as ja liberadas esperam.",
        ),
        Frase(
            "cofre_reabriu_saida",
            "O cofre voltou destravado. A porta de saida, que a conferencia do cofre tinha fechado, reabriu sozinha.",
        ),
        Frase(
            "cofre_trancado_saida_fechada",
            "O cofre esta trancado. A porta de saida ja estava fechada por outro motivo e continua fechada. Quando"
            " o cofre voltar, ela nao reabre sozinha.",
        ),
        Frase(
            "cofre_destravado_saida_fechada",
            "O cofre voltou destravado. A porta de saida continua fechada, porque foi fechada por outro motivo.",
        ),
        Frase(
            "cofre_passou_a_trancado",
            "A porta de saida continua fechada: o cofre voltou a responder, mas esta trancado. Ela reabre quando ele"
            " for destravado, se ninguem a mudar antes.",
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
