# Andamento

Cada etapa só termina quando cumpre a "Prova de saída" do [plano](plano.md#etapas-de-entrega). Esta página diz o que
já foi entregue e o que falta, etapa por etapa.

## Etapa 0: fundação

| Entrega | Situação |
| --- | --- |
| Pacote `mcp_claro`, `uv.lock`, regras de `ruff` e `mypy` | Feito |
| CI com testes, varredura de dependências, de imagens e de segredos, e lista de componentes | Feito (`.github/workflows/ci.yml`) |
| Imagem do gateway sobre `python:3.14` e do Caddy com o módulo de limite | Feito (`docker/`) |
| Compose de desenvolvimento e teste, com imagens fixadas por digest | Feito (`docker/compose.yml`) |
| Primeiras migrações e esqueleto de parâmetros, com recusa de combinações inválidas | Feito (`migrations/`, `src/mcp_claro/config/`) |
| Suíte oficial de conformidade fixada na 0.2.0-alpha.12 rodando no CI | Feito (`conformidade/`) |
| Seis sistemas de exemplo | Feito (`exemplos/`) |
| Modelo de ameaças curto e roteiro de segredos | Feito (`docs/`) |

Prova de saída: CI verde a cada mudança; suíte oficial verde contra um sistema de exemplo; exemplo da geração antiga
conferido (não fala a 2026-07-28); ambiente de teste sobe numa máquina limpa; varredura de segredos limpa.

## Provar a base

| Entrega | Situação |
| --- | --- |
| Provedor de login escolhido por teste | Keycloak 26.8.2 (decisão 3): realm com PKCE S256, programas com JWT assinado, audiência do recurso e cadastro dinâmico fechado; teste contra Keycloak real |
| Porteiro de versão (filtro ASGI na frente do kit) | Feito: geração antiga recebe -32022 com `supported`, GET e DELETE recebem 405, recusa contada por aplicativo |
| Verificador de crachá | Feito: assinatura, emissor, audiência, validade, algoritmos; JWKS com cache e uso da chave guardada se o provedor cair |
| Argumentos contra o esquema revisado | Feito: Draft 2020-12, sem `$ref` de fora |
| Servidor de baixo nível com vitrine do cadastro | Feito: textos e nomes do cadastro, dicas conhecidas apenas, sem `x-mcp-header` |
| Sistema ligado com cliente fixado em 2026-07-28 e sondagem guardada | Feito: `server/discover` no cadastro; 401, 429 e 5xx não viram veredito de versão |
| Sem volta automática | Feito: uma chamada por pedido, sem reenvio depois de 401, sem GET de retomada. Até a Fase 3, o kit ainda respondia sozinho a um pedido do sistema pelo fio antigo (um segundo POST, com a chave); a barreira de pedidos da Fase 3 fechou isso para todos os sistemas ([decisões](decisoes.md), "Mudanças deliberadas da Fase 3") |
| OpenBao sobre PostgreSQL com Transit | Feito: cliente fino, AppRole por papel, selo `static` em teste |
| Caddy na frente, com mTLS na porta interna | Feito: cada porta interna aceita só quem pode chamá-la |
| Simulado do dia ruim | Roteiro em `docs/simulado-dia-ruim.md`; falta rodar com os pilotos |
| Suíte oficial através do Caddy, com o gateway na frente do exemplo | Feito (`conformidade/gateway.py`): 10 dos 37 cenários exigidos passam inteiros, direto na entrada e pelo portão; os outros 27 falham só no que o desenho deixa de fora, com motivo em `conformidade/falhas-esperadas-gateway.yml` |

Provas de saída automatizadas em `tests/integracao/test_provar_a_base.py`, `test_cofre.py`, `test_keycloak.py`,
`test_conformidade_gateway.py` e `tests/unidade/test_ligacao_classifica.py`. A suíte oficial através do gateway roda
no CI, no job "suite oficial atraves do gateway".

## Fase 1: olhar

| Entrega | Situação |
| --- | --- |
| Cadastro único das ações com revisão do ADMIN e registro de cada mudança | Feito: telas do ADMIN (`telas/admin.py`), cada mudança no extrato |
| Ordens de serviço: modelos, escolha da pessoa, prazo que vence sozinho, aviso antes e inatividade | Feito: telas da pessoa e rotinas de vencimento, aviso e inatividade |
| Vitrine sob medida, sem cache no aplicativo | Feito (`cacheScope` privado, `ttlMs` 0) |
| Cofre de chaves: autorização na página do sistema, troca do código no despachante, a entrada nunca vê a chave | Feito: OAuth com PKCE S256; o despachante cria o pedido e cifra o verificador |
| Renovação uma por vez, com trava, gravando a chave nova antes do uso | Feito (`despachante/renovacao.py`) |
| Sistemas de chave compartilhada: só leitura e aviso no extrato | Feito |
| Leitura com medida: máscara, catraca diária e texto de estranhos marcado | Feito: máscara e catraca na Fase 1; texto de estranhos marcado na Fase 3 |
| Extrato único, resumo semanal do ADMIN e motivo para abrir extrato alheio | Feito |
| Bloqueio de um toque e pausa; desativar uma pessoa derruba as autorizações | Feito |
| Mudou-parou para sistemas próprios | Feito: ferramenta que pede mais fica suspensa até nova revisão |
| Identidades da ronda e do vigia, login de programas só para elas | Feito no realm (JWT assinado ES256); a chave pública vai ao provedor na instalação (`docs/vigia.md`) |
| Ronda das portas e vigia de fora com alarme de silêncio | Feito: sete casos, entrada fechando por etapas, vigia com banco próprio só de acrescentar |
| Medições no PostgreSQL e painéis no Grafana, com as recusas por versão | Feito: visões só para o `grafana_leitor`, painel e alertas provisionados |
| Backup, roteiro de restauração e conferência de continuidade na partida | Feito: pgBackRest 2.60.1 e cópia lógica cifrada; fechamentos por época do servidor. O serviço `restaurar-logico`, que o roteiro usa no Caminho B e no ensaio da cópia lógica, voltou ao Compose único, no perfil `restauracao`. Prova: `tests/integracao/test_compose_restauracao.py` |

Provas que dependem de pessoas e do ambiente real, ainda a fazer: dois aplicativos-piloto conectando de verdade;
um sistema do dono com chave por pessoa (decisão 5); telas entendidas por pessoas sem formação técnica; ensaio de
restauração com o pgBackRest de verdade (o teste longo roda na rodada diária do CI).

## Fase 2: agir

Os arquivos de teste citados ficam em `tests/integracao/`, salvo onde se diz `tests/unidade/`.

| Entrega | Situação |
| --- | --- |
| Número de protocolo e código de conferência em toda ação | Feito (`saida/protocolo.py`): `CLR-AAAA-NNNNNNNN`, com oito dígitos ou mais, e um código de 12 letras e números do que vai seguir. Prova: `tests/unidade/test_saida_estados.py` e `test_saida_resposta_perdida.py::test_argumentos_e_codigo_trocados_no_banco_nao_batem_com_a_ficha` |
| Consulta pelo número de protocolo na vitrine | Feito: `mcp_claro_consultar_protocolo`, só a mesma pessoa pelo mesmo aplicativo. Prova: `test_saida.py::test_acao_com_ficha_sai_uma_vez_e_aparece_na_consulta` e `::test_consulta_pelo_protocolo_por_outro_aplicativo_da_mesma_pessoa_nao_acha` |
| Ficha no gateway, caixa "o aplicativo diz" à parte e caixa de pendências | Feito (`telas/acoes.py`), com os caracteres escondidos trocados por marcas visíveis. Prova: `test_telas_acoes.py::test_pendencias_e_ficha_aparecem_e_outra_pessoa_nao_abre` e `::test_ficha_mostra_caracteres_escondidos_marcados` |
| Passkey para risco alto e para criar ou ampliar limites | Feito (`telas/passkeys.py`). Com a passkey desligada, essas ações pedem o login refeito na hora. Prova: `test_telas_acoes.py::test_risco_alto_sem_passkey_recusa_e_com_passkey_valida_confirma`, `::test_passkey_desligada_risco_alto_pede_login_refeito`, `::test_cadastrar_e_desativar_passkey_pedem_login_refeito` e `test_telas_rodada2.py::test_passkey_cadastrada_ou_desativada_avisa_a_pessoa` |
| Aviso de ficha pendente por e-mail e webhook | Feito (`rotinas/acoes.py`): um aviso só, mesmo com duas cópias das rotinas. Prova: `test_rotinas_acoes.py::test_aviso_de_ficha_pendente_uma_vez_por_email_e_webhook` e `::test_duas_copias_das_rotinas_avisam_uma_vez` |
| Porta única de saída: fila, despachante único, espera visível, cancelar antes de sair e "seguir agora" | Feito (`saida/fila.py`, `saida/despachante.py`): trava de sessão, LISTEN/NOTIFY e varredura, "enviando" gravado na conexão da trava. Prova: `test_saida_disputas.py::test_duas_portas_so_uma_envia_e_a_que_herda_nao_reenvia`, `test_telas_acoes.py::test_espera_visivel_e_cancelar_antes_de_sair` e `::test_seguir_agora_sai_antes` |
| "Estamos confirmando": não confirmada, decisão da pessoa e pedido igual que devolve a original | Feito, com a tela de decisão. O pedido igual conta em qualquer idade enquanto a original pode sair. "Não saiu" e "não confirmada" seguem regra escrita. Prova: `test_saida_resposta_perdida.py`, `test_telas_acoes.py::test_estamos_confirmando_decisao_registrada_uma_vez`, `test_saida_correcoes.py::test_pedido_igual_pendente_conta_fora_da_janela_e_a_janela_vale_depois_de_sair` e `tests/unidade/test_despachante_classifica.py` |
| Limite por aproximação: ferramenta, destinatários, quantidade e prazo | Feito (`telas/limites.py`): gasto atômico, conferido de novo no despachante. "Só estes destinatários" usa os campos que o ADMIN marca na revisão. Com a passkey ligada, limite feito sem ela não vale. Prova: `test_saida.py::test_limite_libera_sem_ficha_e_nunca_passa_do_teto`, `test_saida_disputas.py::test_dois_pedidos_pelo_ultimo_uso_de_um_limite_so_um_passa`, `test_telas_rodada2.py` e `test_saida_rodada2.py::test_limite_so_estes_com_argumento_fora_do_esquema_vai_para_a_ficha` |
| Teto de confirmações e período de experiência | Feito: acima do teto, o aplicativo pausa e a fila dele para. Prova: `test_saida.py::test_teto_de_fichas_pausa_o_aplicativo_e_para_a_fila`, `::test_periodo_de_experiencia_manda_tudo_para_a_ficha_e_depois_o_limite_vale` e `test_saida_correcoes.py::test_reativar_depois_do_teto_nao_pausa_de_novo` |
| Comprovante de bloqueio em três partes | Feito (`fila.parar_por_bloqueio`): o que paramos a tempo, o que já tinha seguido e o que ainda está em conferência, na mesma transação do bloqueio. O que o sistema informou fica para a Fase 4. Prova: `test_saida_correcoes.py::test_comprovante_do_bloqueio_em_tres_partes` e `test_telas_acoes.py::test_comprovante_do_bloqueio_em_tres_partes` |
| Fechamento várias vezes ao dia, guardado no vigia | Feito desde a Fase 1 (`CLOSING_INTERVAL_HOURS`, 6 horas). Prova: `test_rotinas_tarefas.py::test_fechamento_encadeado_com_frase` e `test_vigia.py::test_vigia_guarda_os_fechamentos_e_percebe_adulteracao` |
| Programas que rodam sozinhos, só lendo (decisão 9) | Feito (`regras/programas.py`, `rotinas/programas.py`, `telas/admin_programas.py`). `PROGRAM_ACTIONS_ENABLED` ligado é recusado na partida. Prova: `test_programas.py`, `test_programas_rodada2.py` e `tests/unidade/test_settings_fase2.py` |
| Restauração com ações na fila: a porta de saída volta fechada | Feito: "enviando" vira não confirmada, o que esperava fica parado por restauração e o número de protocolo salta para frente. Só com o vigia sem resposta, a saída fecha e a fila não para. Prova: `test_saida.py::test_restauracao_para_a_fila_e_nada_sai_sem_nova_confirmacao`, `test_saida_disputas.py::test_restauracao_pela_conferencia_fecha_a_saida_e_nada_sai`, `test_continuidade.py::test_marca_restaurado_adianta_o_protocolo_antes_de_a_entrada_subir`, `test_continuidade_restauracao.py` e `test_saida_rodada2.py::test_vigia_sem_resposta_fecha_a_saida_e_pausa_sem_parar_a_fila` |
| Reativar um assistente pausado pela conferência do banco | Feito (`telas/admin.py`): botão **Reativar** no cartão "Aplicativos pausados" da tela Aplicativos do ADMIN, com o passo no extrato e o nome de quem fez. Vale só para assistente ainda pausado; o programa volta pela tela do programa. A tela Portas avisa quando há aplicativo pausado. Prova: `test_telas_rodada3.py::test_admin_reativa_o_assistente_pausado_e_so_ele` |
| Porta de saída fechada enquanto o cofre estiver trancado | Feito (`saida/cofre.py`): o despachante confere o cofre a cada `VAULT_CHECK_SECONDS` e logo antes de cada ação sair. Trancado, a saída fecha na primeira conferência; sem resposta, na segunda conferência seguida sem resposta. A ação liberada espera, sem virar "não saiu" (salvo no caso descrito em "O que se sabe e ficou como está", abaixo). A saída que o cofre fechou reabre sozinha quando ele volta; fechada pelo ADMIN ou pela conferência do banco, continua fechada. O ADMIN recebe aviso nos dois casos. Prova: `test_saida_cofre_rodada3.py`, `test_despachante_cofre_rodada3.py` (com o OpenBao de verdade) e `tests/unidade/test_saida_cofre_rodada3.py` |
| Medições da Fase 2 no Grafana | Feito: fila agora, fim dos envios, caminhos e paradas. Prova: `test_grafana.py::test_consultas_do_painel_rodam_como_grafana_leitor` |

Provas de saída automatizadas:

- resposta perdida de propósito não duplica nada: `test_saida_resposta_perdida.py`, entre o despachante e o
  sistema, e `test_saida_resposta_perdida_aplicativo.py`, entre o aplicativo e a entrada, com o cenário da
  discussão pública sobre repetição (3 rodadas a cada mudança, 20 na rodada diária);
- o que a ficha mostra é o que segue: `test_saida_resposta_perdida.py::test_argumentos_trocados_no_banco_depois_da_ficha_nao_saem`;
- "cancelado antes de sair" sem chamada ao sistema: `test_saida.py::test_cancelado_antes_de_sair_nao_chega_ao_sistema`;
- bloquear e enviar ao mesmo tempo: `test_saida_disputas.py::test_bloquear_e_enviar_ao_mesmo_tempo_so_um_acontece`;
- bloqueio no meio de uma fila cheia: `test_saida_disputas.py::test_bloqueio_no_meio_da_fila_cheia_para_o_resto_e_o_comprovante_bate`;
- sequências sorteadas de pedir, confirmar, cancelar, bloquear, perder resposta e reiniciar, com `hypothesis`:
  `test_saida_estados.py` e `test_saida_sequencias.py::test_sequencias_sorteadas_com_as_operacoes_reais`,
  curtas a cada mudança e longas na rodada diária;
- Fase 2 ligada para pessoas reais: `test_saida_fase2_real.py::test_chave_da_fase2_vale_para_pessoas_reais`.

Provas que dependem de gente ou de ambiente real:

- ensaio de restauração com o pgBackRest de verdade e a imagem do PGDG (`test_continuidade_pgbackrest.py`,
  longo, na rodada diária do CI; pula onde o repositório apt do PGDG não é alcançado);
- o teste opcional com o Toxiproxy (`test_saida_resposta_perdida_aplicativo.py::test_resposta_perdida_pelo_toxiproxy`),
  que pula sem a imagem; o `ghcr.io` fica bloqueado no ambiente de testes;
- passkey com autenticadores e navegadores reais; os testes usam um autenticador em software
  (`tests/autenticador_falso.py`);
- o provedor de login real pedindo a senha de novo (`prompt=login`, `max_age=0`) e assinando o `auth_time`; os
  testes usam um provedor falso (`tests/idp_oidc_falso.py`);
- sistemas reais (decisão 5); os testes usam o sistema de exemplo `exemplos/acoes_caixa.py`;
- a prova da issue 3394: o cenário roda refeito com o cortador, pelo gateway inteiro
  (`test_saida_resposta_perdida_aplicativo.py::test_resposta_perdida_ao_aplicativo_e_repeticao_nao_duplicam_o_efeito`).
  Os scripts da própria discussão ficam num repositório à parte e não rodaram contra o gateway;
- um aplicativo-piloto achando o resultado de uma ação pela consulta do número de protocolo;
- telas de ficha, limite e "estamos confirmando" entendidas por pessoas sem formação técnica.

Ainda falta no código:

- as pendências do responsável e a reconferência completa do cadastro do programa na porta de saída. Sem elas,
  `PROGRAM_ACTIONS_ENABLED` não liga, e a prova "com ações ligadas, um programa não age fora do limite" fica
  para depois ([programas](programas.md)).

O que se sabe e ficou como está:

- entre a última pergunta ao cofre e a abertura da chave há um intervalo curto: em geral, menos de um segundo;
  mais, quando a chave da pessoa precisa ser renovada. Se o cofre trancar ou parar de responder bem aí, a ação
  daquele instante vira "não saiu". Depois, a saída fecha pelas regras da conferência do cofre
  ([porta de saída](saida.md));
- a espera de 30 minutos fica na memória do despachante. Ela vale quando a saída fecha de novo porque o cofre
  não responde, menos de uma hora depois de a conferência do cofre a ter reaberto de um fechamento também por
  falta de resposta. Se o despachante reiniciar ou outra cópia assumir a porta, volta a regra das duas
  conferências seguidas (a cópia que assume recomeça as contagens e confere o cofre na hora).

Roteiro para quem cuida do dia a dia: [Porta de saída](saida.md).

## Fase 3: servidores de terceiros

A Fase 3 recebe servidores MCP que não são da organização (no cadastro, a origem "de terceiros"). O código das
etapas 1 a 6 está no repositório: banco, ligação, sala de visitas, cadastro, regras, entrada, fila, despachante,
porta de saída, rotinas, telas e documentos e, da etapa 6, as provas de saída pelo gateway inteiro e os ensaios antes
de ligar a chave. Falta terminar a revisão cruzada da etapa 6, com os achados corrigidos. As decisões e o que mudou de
propósito estão em [decisões](decisoes.md), nas seções "Decisões da Fase 3" e "Mudanças deliberadas da Fase 3".

**A chave da Fase 3 continua desligada na partida (D6).** Com `FEATURE_PHASE3_THIRD_PARTY` ligada, a partida é
recusada em todos os papéis, como com `PROGRAM_ACTIONS_ENABLED`. Ligar depende da decisão registrada do dono,
depois das provas de saída, das que dependem de gente e de ambiente real (listas abaixo) e da decisão sobre o teste
de carga. Enquanto isso, o ADMIN cadastra, sonda e revisa servidores de terceiros, e só pessoas de teste
(`pessoas.teste`) os veem e usam. Prova: `tests/unidade/test_settings_fase3.py`.

Os arquivos de teste citados ficam em `tests/integracao/`, salvo onde se diz `tests/unidade/`.

| Entrega | Situação |
| --- | --- |
| Revisão servidor a servidor pelo ADMIN, com nomes e descrições escritos no cadastro | Feito: revisão de cada ferramenta (esquema sem o texto do fornecedor, descrição de cada campo escrita pelo ADMIN, dicas só da classe revisada) e conclusão da revisão do servidor, registrada em `revisoes_servidor`, só de acrescentar, com quem revisou e quem fornece. A vitrine de um terceiro mostra só as ferramentas publicadas na revisão vigente. Prova: `test_revisao_servidor.py`, `test_vitrine_terceiros.py` e `test_telas_fase3_admin.py::test_conclusao_da_revisao_do_servidor_pela_tela` |
| Sala de visitas: endereço combinado, sem redirecionamento, chave separada e sem `traceparent` | Feito: contêiner `visitas`, sem banco e sem cofre, com um processo novo por chamada; só https e IP público; nenhum desvio seguido; chave do cofre só dos terceiros; nenhum dado de rastreio. Fora de desenvolvimento e teste, a sala é obrigatória. Prova: `tests/unidade/test_visitas_app.py`, `tests/unidade/test_ligacao_terceiro.py`, `test_chave_separada.py`, `test_compose_visitas.py` e `test_sala_isolada.py`, que sobe a sala de verdade e prova o isolamento a cada mudança |
| Barrar pedidos de senha, link ou confirmação vindos do servidor; eles não chegam à pessoa | Feito, para todos os sistemas: nenhum pedido do sistema passa ao aplicativo nem à pessoa. Ele vira uma categoria nossa no extrato e na tela do ADMIN ("Pedidos recusados deste sistema"), sem texto, link ou campo do servidor. Prova: `tests/unidade/test_ligacao_pedidos.py`, `test_pedidos_do_servidor.py` e `test_telas_fase3_admin.py::test_pedidos_recusados_com_contagem_e_linhas_sem_texto_do_servidor` |
| Marcar a resposta do servidor como texto escrito por estranhos | Feito: moldura com marca sorteada e aviso do MCP-CLARO, na leitura e na ação, também para ferramenta própria marcada "traz texto de fora". O texto do terceiro não aparece nas telas da pessoa. Prova: `test_estranhos_leitura.py`, `test_estranhos_acao.py` e `test_telas_fase3_pessoa.py::test_resposta_de_terceiro_com_pedido_de_senha_nao_chega_as_telas_da_pessoa` |
| Mudou-parou endurecido: qualquer mudança leva o servidor inteiro à quarentena | Feito: ferramenta mudada (mesmo só no texto), nova ou sumida, e também 2 rodadas seguidas sem resposta. A quarentena cancela o que esperava para sair, suspende os limites das pessoas com o servidor e avisa o ADMIN. Só uma nova conclusão da revisão do servidor o põe de volta em serviço; além dela, o banco só aceita desativá-lo. Prova: `test_rotinas_terceiros.py::test_mudanca_so_de_texto_em_terceiro_leva_o_servidor_inteiro_a_quarentena`, `test_saida_quarentena.py` e `test_telas_fase3_admin.py::test_quarentena_mostra_o_bloco_os_passos_e_o_antes_e_depois_e_a_revisao_tira_de_la` |
| Alerta dos três sinais, somado por pessoa e por aplicativo na janela | Feito: com "leu texto de fora" e "viu dado privado" na janela (`THREE_SIGNALS_WINDOW_MINUTES`, 240 minutos no início), toda ação vai para a ficha, sem consultar nem gastar limite, e a ficha mostra o motivo. Prova: `test_tres_sinais.py::test_tres_sinais_suspendem_o_limite_e_a_ficha_mostra_o_motivo` e `test_telas_fase3_pessoa.py::test_ficha_de_terceiro_avisa_e_mostra_o_cartao_do_alerta_dos_tres_sinais` |
| Recusar no cadastro servidor que só fala a versão antiga, com contagem | Feito: a sondagem separa seis desfechos, e só "versão" conta como versão antiga. A recusa de um terceiro conta à parte (`cadastro_terceiro`), no painel e no resumo semanal, fora do alerta alto. Prova: `test_sondagem_fase3.py` e `test_telas_fase3_admin.py::test_terceiro_da_geracao_antiga_e_recusado_e_contado_a_parte` |
| Porta de saída com terceiros | Feito: reconferência na saída, envio pela sala, quarentena, níveis e linhas da sala no extrato ([porta de saída](saida.md#ações-em-servidores-de-terceiros)). Prova: `test_saida_terceiros.py` |
| Telas do ADMIN e da pessoa | Feito na etapa 5: cadastro com a origem, sondagem por desfecho, revisão do servidor, quarentena e pedidos recusados, do lado do ADMIN; aviso de terceiro e cartão do alerta na ficha, limites e tela da ação, do lado da pessoa. Prova: `test_telas_fase3_admin.py` e `test_telas_fase3_pessoa.py` |
| Medições | Feito: bloco "Servidores de terceiros (Fase 3)" no Grafana (recusados por versão, em quarentena agora e por situação) e a linha de terceiros no resumo semanal. Prova: `test_grafana.py` |

Também ficou pronto:

- a barreira de pedidos e as outras mudanças que valem para todos os sistemas, inclusive os das Fases 1 e 2
  (decisões, "Mudanças deliberadas da Fase 3", itens marcados "vale para todos");
- o campo `Pedido.terceiros` é obrigatório e só por nome, sem valor padrão: um chamador que esquecesse o campo faria
  o sinal de terceiro não contar no alerta dos três sinais (`tests/unidade/test_f4_pedido_terceiros.py`);
- `OBS_TRACE_PROPAGATE_DOWNSTREAM` continua sem uso no código. Nenhum valor dele libera dados de rastreio a
  terceiros (D10; `tests/unidade/test_settings_fase3.py::test_nenhum_valor_da_propagacao_de_rastro_libera_terceiros`).
  A propagação de `traceparent` para sistemas próprios fica fora da Fase 3.

Provas de saída do plano, por partes e pelo gateway inteiro (etapa 6):

| Prova de saída | Situação |
| --- | --- |
| Cada servidor de terceiros revisado e registrado | Feito pelo gateway inteiro: `test_fase3_provas_de_saida.py::test_cada_terceiro_na_vitrine_tem_a_revisao_do_servidor_vigente_registrada_em_todo_o_percurso`. O invariante é conferido depois de cada passo do percurso do ADMIN pelas telas e da quarentena pelas rotinas, de três lados: na vitrine do aplicativo, no banco inteiro e no painel `medicoes.v_terceiros`. No percurso, uma conclusão que deixa de fora uma ferramenta já revisada a tira da vitrine, e a conclusão seguinte, com as duas, a traz de volta. Por partes: `test_revisao_servidor.py` e `test_migracao_0006.py` |
| Sala de visitas isolada: o servidor não alcança chaves nem dados de outros sistemas | Feito pelo gateway inteiro, com a sala de verdade: `test_fase3_provas_de_saida.py::test_sala_isolada_o_terceiro_recebe_so_o_pedido_sem_chave_alheia_cracha_rastro_nem_resultado_alheio`. O teste confere cada byte que dois terceiros recebem, com chaves por pessoa da Ana e da Bia, compartilhada e de programa em próprios. Com o Compose inteiro no ar, de dentro da sala, a entrada, as rotinas e o login não são alcançados pelo nome nem pelo IP: `test_fase3_ensaios.py::test_de_dentro_da_sala_entrada_rotinas_login_no_ar_nao_alcancaveis_por_nome_nem_por_ip`. Por partes: `test_sala_isolada.py` (o banco e o cofre, pelo nome e pelo IP, os metadados das nuvens e os IPs privados da rede `saida_visitas`, a cada mudança), `tests/unidade/test_visitas_app.py`, `test_chave_separada.py` e `tests/unidade/test_ligacao_terceiro.py` |
| Provas de crachá não repassado e de chave por pessoa repetidas com um servidor de terceiros | Feito pelo gateway inteiro: `test_fase3_provas_de_saida.py::test_cracha_do_aplicativo_nao_chega_ao_terceiro_e_uma_pessoa_nao_alcanca_o_que_e_de_outra` |
| Mudança só de texto num servidor de teste leva à quarentena | Feito pelas rotinas (`test_rotinas_terceiros.py`) e pelo gateway com a sala (`test_fase3_provas_de_saida.py::test_mudanca_so_de_texto_em_terceiro_leva_o_servidor_inteiro_a_quarentena_pela_sala`): um aviso só ao ADMIN, por e-mail, sem o texto do fornecedor nem o nome das ferramentas no servidor, e a rodada seguinte não repete nem a linha do extrato nem o aviso |
| Três sinais juntos suspendem o limite por aproximação, e a ficha mostra o motivo | Feito pela entrada e pela fila (`test_tres_sinais.py`), pela tela (`test_telas_fase3_pessoa.py`) e pelo gateway inteiro (`test_fase3_provas_de_saida.py::test_tres_sinais_suspendem_o_limite_e_a_ficha_mostra_o_motivo`, com o limite criado na tela de limites, com passkey) |
| Pedido de senha vindo do servidor não chega à pessoa | Feito pelo gateway inteiro, também para o texto comum com senha e link (crítica A4): `test_fase3_provas_de_saida.py::test_pedido_de_senha_vindo_do_terceiro_nao_chega_a_pessoa`. Nada do pedido (mensagem, link ou campos) fica guardado na operação. Por partes: `test_pedidos_do_servidor.py` e `test_telas_fase3_pessoa.py` |
| Nova (D31): a resposta de um terceiro chega inteira dentro da marca, e nada passa fora dela | Feito pelo gateway inteiro: `test_fase3_provas_de_saida.py::test_resposta_de_terceiro_chega_inteira_dentro_da_marca_com_erro_e_dados_estruturados`. O `_meta` que chega ao aplicativo tem só as chaves que o MCP-CLARO escreve: as chaves com o nosso prefixo que o terceiro forja não passam. Por partes: `test_estranhos_leitura.py` e `test_estranhos_acao.py` |

Ensaios antes de ligar a chave (etapa 6):

- ensaios em `test_fase3_ensaios.py`, com o dublê mal-intencionado `exemplos/ensaio_mal_intencionado.py`:
  - ler, ler o cadastro com CPF e tentar enviar dentro do limite vai para a ficha pelo alerta dos três sinais
    (`test_fase3_ensaios.py::test_duble_mal_intencionado_le_le_dado_pessoal_e_a_acao_dentro_do_limite_vai_para_a_ficha`,
    que confere também que nem as instruções nem o nome do servidor do fornecedor chegam ao aplicativo na
    descoberta), e o texto do terceiro chega ao aplicativo só dentro da marca, também pela sala de verdade
    (`test_fase3_ensaios.py::test_pela_sala_de_verdade_o_texto_do_terceiro_chega_so_dentro_da_marca`);
  - resposta perdida, com `tests/cortador.py` entre o despachante e a sala (a sala em processo e o processo pai de
    verdade, com um filho por chamada) e entre a sala e o terceiro: nada se repete, o desfecho é "não confirmada" e o
    pedido igual devolve o mesmo protocolo
    (`test_fase3_ensaios.py::test_resposta_perdida_entre_o_despachante_e_a_sala_nao_repete`,
    `test_fase3_ensaios.py::test_resposta_perdida_entre_o_despachante_e_a_sala_de_verdade_nao_repete` e
    `test_fase3_ensaios.py::test_resposta_perdida_entre_a_sala_e_o_terceiro_nao_repete`);
  - a sala sob pressão (não é teste de carga): várias chamadas ao mesmo tempo, e cada resposta volta a quem pediu,
    sem nada perdido nem repetido
    (`test_fase3_ensaios.py::test_sob_pressao_pela_sala_de_verdade_cada_resposta_volta_a_quem_pediu_e_nada_se_perde`);
  - a cópia lógica de um banco com servidores de terceiros volta com os dados, as guardas e as permissões da Fase 3;
  - o isolamento da sala com a entrada, as rotinas e o login no ar
    (`test_fase3_ensaios.py::test_de_dentro_da_sala_entrada_rotinas_login_no_ar_nao_alcancaveis_por_nome_nem_por_ip`);
- suíte oficial de conformidade pelo gateway, direto e pelo portão, com a chave da Fase 3 desligada e ligada: 10
  cenários exigidos passam, 27 falham só como previsto, nenhuma falha sem previsão; a Fase 3 não mudou a lista
  (`conformidade/falhas-esperadas-gateway.yml`). Ela roda fora do pytest, por `conformidade/gateway.py`, como no CI;
- a restauração com o pgBackRest e servidores de terceiros não foi conferida neste ambiente:
  `test_continuidade_pgbackrest.py` pula sem o repositório apt do PGDG. A cópia lógica com terceiros foi conferida
  (`test_fase3_ensaios.py::test_copia_logica_com_terceiros_volta_com_os_dados_as_guardas_e_os_grants_da_fase_3`);
- não há teste de carga no repositório: o locust da tabela de testes do plano ainda não entrou. Ligar a chave da
  Fase 3 (D6) depende da decisão do dono sobre ele ([decisões, "Ainda em aberto"](decisoes.md#ainda-em-aberto)).

Provas que dependem de gente ou de ambiente real:

- escolher os primeiros servidores de terceiros reais (decisões, "Ainda em aberto");
- ver se um assistente de verdade segue uma instrução escondida no texto de um terceiro (simulado do dia ruim): o
  teste automático só prova que a marca está lá;
- conferir como o Claude Code e o Goose mostram ao modelo o `_meta`, os dados estruturados e vários blocos: ainda
  não conferido;
- um servidor real feito com o kit TypeScript ou Rust da geração antiga (opcional);
- DNS, IP público e certificados públicos: só em ambiente real;
- o OAuth de um terceiro real e a página de autorização dele;
- o ajuste da janela dos três sinais no piloto (alarme falso), olhando `medicoes.v_acoes_pedidas`, e o efeito das
  fichas do alerta no teto por aplicativo;
- a revisão de LGPD: os sinais ficam 7 dias; o retrato do alerta fica na ficha pelo prazo do extrato;
- uma sessão guiada com um ADMIN seguindo o roteiro [Ligar um servidor de terceiros](terceiros.md), sem ajuda da
  equipe (recomendado);
- a decisão do dono para ligar a chave da Fase 3 (D6).

O que se sabe e ficou como está: os riscos aceitos estão em [decisões](decisoes.md), "Riscos aceitos da Fase 3".

Roteiro do ADMIN: [Ligar um servidor de terceiros](terceiros.md). Roteiro para quem cuida do dia a dia:
[Porta de saída](saida.md#ações-em-servidores-de-terceiros).
