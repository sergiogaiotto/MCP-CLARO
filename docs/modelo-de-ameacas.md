# Modelo de ameaças do MCP-CLARO

Versão curta, revista a cada fase. Diz o que protegemos, de quem e onde está a defesa no código.

## O que protegemos

1. **As chaves das pessoas e dos programas** para os sistemas conectados.
2. **As ações** que mudam algo num sistema: nenhuma sai sem ficha confirmada ou limite, e nenhuma sai duas vezes.
3. **O extrato**: o histórico do que passou, que precisa ser confiável depois de um incidente.
4. **Os dados lidos** dos sistemas, com campos pessoais mascarados.

## De quem

| Ameaça | Exemplo | Defesa principal |
| --- | --- | --- |
| Aplicativo de IA enganado ou mal-intencionado | Pede ferramenta fora da vitrine, troca argumentos, tenta pular a ficha | Vitrine sai do cadastro revisado; argumentos conferidos contra o esquema revisado; ficha montada pelo gateway; ensaio do dublê mal-intencionado |
| Crachá roubado ou de outro destino | Crachá emitido para outro serviço ("outro condomínio") | Verificador confere assinatura, emissor, audiência (recurso `PUBLIC_BASE_URL/mcp`), validade e algoritmos; ronda tenta entrar com crachá falso |
| Repasse de crachá | O crachá do aplicativo chegar ao sistema | O despachante usa só a chave do cofre; teste prova que o crachá não chega ao sistema de exemplo |
| Versão antiga do protocolo | Aplicativo ou sistema que só fala a geração antiga | Porteiro de versão recusa com mensagem legível; cadastro recusa sistema antigo; contagem dos dois lados (decisão 8) |
| Sistema conectado mal-comportado | Muda a definição da ferramenta, pede dados no meio da ação, redireciona | Mudou-parou por impressão digital; sem volta automática; sem seguir redirecionamento; só o endereço cadastrado |
| Servidor de terceiros | Pede senha à pessoa, escreve instruções na resposta | Sala de visitas; pedidos do servidor barrados; resposta marcada como texto de estranhos (Fase 3) |
| Resposta perdida e repetição | O aplicativo repete o pedido depois de perder a resposta | Número de protocolo; pedido igual devolve a operação original; "não confirmada" nunca é reenviada |
| Operador com acesso ao banco | Apagar ou mudar linhas do extrato | Extrato só de acrescentar (gatilho e permissões); fechamento encadeado guardado no vigia, em outra máquina |
| Restauração de backup | Voltar o banco e reenviar ações antigas, ou dar de novo um número de protocolo já usado | Conferência de continuidade na partida; porta de saída fechada e aplicativos pausados; "enviando" vira "não confirmada" e o que esperava fica parado por restauração; o número de protocolo salta para frente ([restauração](restauracao.md)) |
| Vazamento por logs e rastros | Crachá ou argumento num log | Logs sem conteúdo (`mcp_claro.logs`); extrato recusa chaves de conteúdo; rastros sem conteúdo |
| DNS rebinding e cabeçalhos forjados | Pedido com `Host` estranho ou `X-Forwarded-For` falso | `ALLOWED_HOSTS`; `FORWARDED_ALLOW_IPS` só o portão; identidade vem do crachá, nunca do endereço |
| Cadeia de fornecimento | Imagem ou pacote trocado | Imagens por digest, ações por commit, `uv.lock`, pip-audit, Trivy, Syft, gitleaks |

## O que não cobrimos

- O que o aplicativo faz por outros caminhos, fora do gateway.
- Erros da IA: o gateway limita o estrago, não impede o erro.
- Quem tem acesso total às duas máquinas ao mesmo tempo.
- Com `VAULT_SEAL_MODE=static` na mesma máquina, quem opera a máquina tem a chave do cofre.
