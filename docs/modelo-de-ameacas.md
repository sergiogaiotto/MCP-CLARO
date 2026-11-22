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
| Sistema conectado mal-comportado | Muda a definição da ferramenta, pede dados no meio da ação, redireciona | Mudou-parou por impressão digital; sem volta automática; barreira de pedidos: nenhum POST que não seja o pedido da chamada sai, e o pedido do sistema nunca chega à pessoa nem ao aplicativo; sem seguir redirecionamento; só o endereço cadastrado |
| Servidor de terceiros | Pede senha à pessoa, escreve instruções na resposta, muda o que declara, tenta alcançar chaves de outros | Revisão do servidor inteiro; sala de visitas; chave Transit separada; pedidos barrados; resposta marcada como texto de estranhos; alerta dos três sinais; quarentena (Fase 3, [abaixo](#servidores-de-terceiros-fase-3)) |
| Resposta perdida e repetição | O aplicativo repete o pedido depois de perder a resposta | Número de protocolo; pedido igual devolve a operação original; "não confirmada" nunca é reenviada |
| Operador com acesso ao banco | Apagar ou mudar linhas do extrato | Extrato só de acrescentar (gatilho e permissões); fechamento encadeado guardado no vigia, em outra máquina |
| Papel do banco que cria uma "sombra" | Uma tabela temporária com o nome de uma tabela do produto, para enganar uma guarda | Toda função de gatilho roda com caminho de busca fixo; o banco do gateway não dá a todo usuário o direito de criar tabela temporária (migração 0006 e `docker/postgres/iniciar/10-papeis.sh`) |
| Restauração de backup | Voltar o banco e reenviar ações antigas, ou dar de novo um número de protocolo já usado | Conferência de continuidade na partida; porta de saída fechada e aplicativos pausados; "enviando" vira "não confirmada" e o que esperava fica parado por restauração; o número de protocolo salta para frente ([restauração](restauracao.md)) |
| Vazamento por logs e rastros | Crachá, argumento ou texto do sistema num log | Logs sem conteúdo (`mcp_claro.logs`); nos registradores do kit no caminho do cliente fixado, só a mensagem fixa e o tipo da exceção; extrato recusa chaves de conteúdo; rastros sem conteúdo; nada de `traceparent`, `tracestate` e `baggage` a terceiros |
| DNS rebinding e cabeçalhos forjados | Pedido com `Host` estranho ou `X-Forwarded-For` falso | `ALLOWED_HOSTS`; `FORWARDED_ALLOW_IPS` só o portão; identidade vem do crachá, nunca do endereço |
| Cadeia de fornecimento | Imagem ou pacote trocado | Imagens por digest, ações por commit, `uv.lock`, pip-audit, Trivy, Syft, gitleaks |

## Servidores de terceiros (Fase 3)

Um servidor de terceiros é um servidor MCP que não é da organização (`sistemas.origem = 'terceiro'`). O MCP-CLARO
o trata como estranho: tudo o que vem dele pode ser mentira, e ele pode tentar enganar a IA, a pessoa ou o próprio
MCP-CLARO. A chave `FEATURE_PHASE3_THIRD_PARTY` só decide se pessoas reais veem e usam terceiros (com ela
desligada, só pessoas de teste). Todas as defesas abaixo valem sempre, pela origem do servidor, com a chave ligada
ou não. Nenhuma delas é garantia: cada uma reduz o que um servidor mal-intencionado consegue fazer.

| Ameaça | Defesa | Onde |
| --- | --- | --- |
| O texto do fornecedor engana o aplicativo ou a pessoa (descrição, dicas como "só leitura", textos dentro do esquema) | Revisão do servidor inteiro pelo ADMIN: só as ferramentas publicadas na revisão vigente aparecem. O aplicativo vê só o nome, a descrição e as descrições de campo escritas pelo ADMIN; as dicas vêm só da classe revisada; o esquema vai sem os textos do fornecedor | `cadastro/esquema.py`, `cadastro/modelos.py`, `revisoes_servidor` |
| O servidor muda depois da revisão, mesmo só no texto | Mudou-parou de servidor inteiro: qualquer ferramenta mudada, nova ou sumida leva à quarentena, que cancela o que esperava na fila e suspende os limites. Duas rodadas seguidas sem resposta também. Só uma nova conclusão da revisão do servidor o põe de volta em serviço; além dela, o banco só aceita desativá-lo. | `rotinas/mudou_parou.py`, `saida/quarentena.py`, migração 0006 |
| O servidor pede senha, link ou confirmação no meio da chamada | Barreira de pedidos: o MCP-CLARO não responde, não repassa e não faz segunda volta. O pedido vira uma categoria do nosso vocabulário no extrato e na tela do ADMIN, sem o texto, o link ou os campos do servidor | `ligacao/cliente.py`, `ligacao/ponto.py` |
| A resposta traz instruções escondidas para a IA | A resposta chega ao aplicativo inteira dentro de uma moldura com marca sorteada (12 letras, uma por resposta), depois de um aviso do MCP-CLARO; moldura forjada e caracteres escondidos são neutralizados; `_meta`, `annotations` e dados estruturados do servidor não passam como vieram (os dados estruturados viram texto emoldurado, com CPF e e-mail mascarados) | `regras/estranhos.py` |
| A resposta engana a pessoa na tela do MCP-CLARO, a tela em que ela confia | Nas telas da pessoa, a resposta de estranhos não aparece. Ela vê "O servidor de terceiros respondeu. A resposta foi entregue ao aplicativo, marcada como texto de estranhos."; o texto só aparece se ela pedir, com aviso, escapado e sem link clicável | `telas/acoes.py`, `telas/modelos/acoes/_estado.html` |
| A IA enganada lê um texto de fora e depois age com dados da pessoa | Alerta dos três sinais: com "leu texto de fora" e "viu dado pessoal" na janela (240 minutos de início), pelo par pessoa e aplicativo, toda ação vai para a ficha, sem consultar nem gastar limite, e a ficha mostra o motivo | `regras/sinais.py`, `saida/pedidos.py` |
| O servidor explora uma falha do kit e tenta alcançar chaves, dados ou outros sistemas | Sala de visitas: contêiner próprio, sem banco, sem cofre e sem extrato, numa rede só dela; cada chamada num processo novo, com só os dados daquela chamada ([isolamento](#isolamento-dos-processos-da-sala)) | `visitas/`, `docker/compose.yml` |
| O servidor desvia o pedido para dentro da rede (nuvem, banco, sistemas próprios) | Só o endereço combinado, só https e só endereço da internet, conferido no mesmo nome que o cliente conecta; nenhum desvio 3xx seguido (vira `sala_desvio_recusado` no extrato); o muro da sala tira as rotas para endereços que não são públicos | `ligacao/cliente.py`, `visitas-muro` |
| O servidor fica sabendo de quem chama ou cruza rastros | Sem `traceparent`, `tracestate` e `baggage`, nem nos cabeçalhos nem no corpo, com ou sem rastros ligados; na conversa MCP, nome e versão genéricos (`mcp-claro`); cookies recusados. A troca de chave (OAuth) ainda leva a versão do produto ([veja abaixo](#o-despachante-e-a-troca-de-chave-dos-terceiros)) | `ligacao/cliente.py` |
| A chave de um terceiro é usada para abrir chaves dos próprios, ou o contrário | Chave Transit própria dos terceiros (`mcp-claro-chaves-terceiros`, `derived`), escolhida pela origem do sistema e conferida na partida do despachante | `despachante/cifra_origem.py` |
| O ADMIN, ou alguém com acesso ao banco, troca o endereço depois de guardar as chaves | Num terceiro, o endereço não muda depois do cadastro, e a configuração de autorização só passa de vazia a preenchida; depois, só cadastrando o servidor de novo. Assim, as chaves já guardadas não seguem para um endereço cadastrado depois. O nome do servidor continua dependendo do DNS e do certificado do fornecedor (veja a janela de DNS em "O que fica, de propósito") | Migração 0006 (`sistemas_guarda`), `despachante/autorizacao.py` |
| Um terceiro manda a pessoa à página de autorização de outro sistema, com o pedido dele (mistura de servidores de autorização), para trocar o código dela no endereço de troca dele | Cada servidor de terceiros tem um endereço de volta só dele (`/telas/autorizar/<id>/volta`). A volta que chega pelo endereço de outro sistema, ou a de um terceiro pela volta dos próprios, é recusada (`estado_de_outro_sistema`) antes de abrir o segredo do cliente, decifrar o verificador ou trocar o código: nada vai a ninguém. A defesa conta com o servidor de autorização de cada sistema aceitar só o endereço de volta que tem cadastrado (RFC 6749, 3.1.2) e não cobre a mistura entre dois sistemas próprios, que dividem a mesma volta. O `iss` na volta (RFC 9207) ainda não é conferido | `despachante/autorizacao.py`, `telas/autorizar.py` |
| O esquema do fornecedor trava a CPU da entrada | Num terceiro, a entrada não roda as regras de formato (`pattern`) do fornecedor nem compara listas item a item; o esquema tem tetos de tamanho e de profundidade, e cada conferência tem um orçamento de passos ([decisões, "Mudanças deliberadas da Fase 3"](decisoes.md#mudanças-deliberadas-da-fase-3-21112026)) | `cadastro/esquema.py`, `entrada/argumentos.py` |

### O despachante e a troca de chave dos terceiros

A conversa MCP com o terceiro passa sempre pela sala. Mas a troca do código de autorização e a renovação da chave
(OAuth) de um terceiro ficam no despachante: é código nosso, pequeno e com teto de tamanho e de tempo. Por isso o
despachante fala com os sistemas próprios, com a sala e com o endereço de troca de chave (OAuth) de cada terceiro,
pela rede `saida`. Antes, esse endereço passa pela mesma conferência da sala (https e só endereço da internet); um
endereço recusado não recebe nada e vira `sala_endereco_recusado` no extrato. O que fica: o despachante, que abre
todas as chaves, recebe e lê a resposta desse endereço do terceiro. E, nessa troca, o despachante se apresenta com o
nome e a versão do MCP-CLARO (cabeçalho `User-Agent`, `mcp-claro/` e a versão): o endereço de troca do terceiro fica
sabendo qual versão do produto roda. A conversa MCP, pela sala, vai com nome e versão genéricos.

O teto de tempo da troca: a troca inteira tem um prazo total de 15 s (abrir a conexão, mandar o pedido e ler a
resposta inteira), e a renovação espera no máximo 3 s pela vez de outra renovação da mesma pessoa no mesmo sistema.
Antes, o prazo valia para cada pedaço da resposta, e um endereço de troca que mandasse um byte de cada vez prendia a
porta de saída por horas. O que fica: cada ação de um terceiro com o endereço de troca de chave lento ainda pode
ocupar a porta única de saída por até 15 s da troca, mais a espera da vez e a conferência do endereço (até 3 s
cada). Nesse tempo, nenhuma outra ação sai, de nenhum sistema.

A página de autorização do próprio terceiro abre no navegador da pessoa, fora do MCP-CLARO. Ela pode pedir senha:
isso é do terceiro, não do gateway.

### Isolamento dos processos da sala

A sala de visitas atende cada chamada num processo filho novo, iniciado por `spawn`, um por chamada, só com os
dados daquela chamada (a chave do terceiro já aberta, os argumentos e o endereço). O processo pai só recebe o pedido
do despachante, entrega ao filho e devolve o que o filho escreveu, sem interpretar bytes do terceiro. Assim, um
terceiro que tome um filho só vê a própria chamada. Para a chamada não esperar o filho carregar, a sala deixa 2
filhos já iniciados e sem dado nenhum, e atende até 32 chamadas ao mesmo tempo (`visitas/app.py`, `PRONTOS` e
`MAX_CHAMADAS`). As vagas são de todos os terceiros juntos. Com as 32 vagas ocupadas por mais de 2 s, a chamada
seguinte não sai ("não saiu"), sem ficar em dúvida: nada vai a um filho. Terceiros lentos que ocupem as vagas podem,
assim, fazer chamadas a outros terceiros não saírem (negação de serviço, não leitura); a pessoa pode pedir de novo.
Na conferência do mudou-parou, a sala cheia dita pelo processo pai (com um cabeçalho que o filho não consegue
escrever) é falha do MCP-CLARO e não conta contra o servidor; o mesmo corpo escrito por um filho tomado, sem o
cabeçalho, conta como o servidor que não entregou. Com a sala cheia em toda rodada, nenhuma conferência de terceiro é
feita (contador `sala_sem_vaga`, `onde=declaracoes`). Isso vale também para o terceiro lento que ocupa as vagas:
enquanto a sala estiver cheia, a mudança dele não é vista; a vitrine continua só com o que foi revisado.

Como cada filho se fecha antes de receber o pedido:

- não gera cópia de memória nem aceita ser depurado por outro processo do mesmo usuário (`PR_SET_DUMPABLE`);
- morre junto com a thread do pai que o criou (`PR_SET_PDEATHSIG`). Esse sinal vale pela thread, não pelo
  processo; por isso todos os filhos nascem numa thread própria da sala, que vive tanto quanto ela. Um teste prova
  que fechar a sala mata um filho já isolado que ainda vive;
- entra num domínio Landlock só dele. De lá, não alcança o `/proc` de outro processo (canos e memória), o
  `ptrace` nem, com Landlock 6 ou mais novo, os sinais de outro processo, nem do pai nem dos irmãos;
- troca a entrada e as saídas (0, 1 e 2) por `/dev/null` e fecha o cano herdado do rastreador de recursos do
  `multiprocessing`: um filho tomado não escreve no log da sala nem fala com o rastreador;
- só depois avisa o pai que se isolou, e só então recebe o pedido. Pai e filho falam por soquetes, que o `/proc`
  não reabre.

Entre o `spawn` e o isolamento, o filho ainda é um processo comum do usuário da sala. Essa janela durou cerca de 0,8 s
num filho novo, medida pela equipe da sala no ambiente de testes da Fase 3 (tempo de relógio, varia com a máquina; não é
garantia); os 2 filhos prontos escondem essa espera. A sala parte por `python -m mcp_claro visitas`, e não pelo script
`mcp-claro` da imagem: quando o pai é um script, o `spawn` roda esse script de novo em cada filho, antes do isolamento,
com o que ele importa. Medido na imagem do gateway, no mesmo ambiente: cada filho carrega 60 módulos e cerca de 5 MB a
menos; a partida de um filho fica parecida, com mediana entre 0,70 e 0,83 s de relógio nas séries de 20 e de 40
partidas.

A sala também não guarda segredo nenhum além do próprio certificado: a partida dela recusa variáveis `DATABASE_*`,
`DB_*`, `VAULT_*` e qualquer outro arquivo de segredo (`*_FILE`), e recusa subir sem `APP_ENV` escrito no ambiente
dela (sem ele, subiria como desenvolvimento e aceitaria endereços locais). Cada filho herda esse ambiente sem
segredos.

**Sala sem Landlock (decisão do dono).** A sala sobe mesmo num núcleo sem Landlock (núcleo antigo ou com o Landlock
desligado): ela não recusa a partida, e o log de partida diz a versão do Landlock (`landlock=0` quer dizer sem
Landlock). O ambiente de testes da Fase 3 (núcleo 6.18) tem Landlock 7, conferido nesta etapa. O runner
ubuntu-24.04 do CI depende do núcleo da vez, que muda com o tempo, e não foi conferido: o log de partida da sala
(`landlock=N`) diz a versão em cada máquina. Sem Landlock, ficam o aviso antes do pedido, o `PR_SET_DUMPABLE` e os
soquetes, e ficam estes riscos:

- um filho tomado ainda pode matar outros filhos, e essas chamadas viram "dúvida" (não confirmadas);
- se o núcleo também deixar `ptrace` entre processos irmãos (Yama desligado), um filho tomado pode, na janela de
  partida de um filho novo, prender-se a ele ou abrir a memória dele e ler o pedido depois;
- com Landlock 1 a 5, o sinal entre filhos ainda passa (negação de serviço, não leitura).

Se o dono quiser, a partida em piloto e produção pode passar a recusar núcleo sem Landlock. Hoje não recusa.

### O que fica, de propósito

- Injeção de prompt. A marca de estranhos e o alerta dos três sinais reduzem o que uma IA enganada causa; não
  impedem. A IA enganada ainda pode pôr dados da pessoa no texto de uma **consulta de leitura** a um terceiro: sai
  sem ficha, porque leituras seguem sem ficha e o alerta só age sobre ações.
- O aplicativo ainda recebe o texto do terceiro, emoldurado. Um link dentro de um texto normal de resposta continua
  passando, só que dentro da moldura e depois do aviso.
- Nomes de campo, valores de `enum` e `const` e regras de formato (`pattern`) do fornecedor continuam indo ao
  modelo: são necessários para chamar a ferramenta.
- O que o terceiro faz do lado de lá, e mudança de comportamento sem mudança no que ele declara. Um servidor pode
  esconder a mudança: falhar só na lista de ferramentas, mostrar definições diferentes conforme a chave, ou mudar
  de comportamento sem mudar a definição.
- Até a próxima rodada do mudou-parou (`TOOL_DRIFT_CHECK_SECONDS`, 900 segundos de início), um terceiro que mudou
  segue sendo chamado. O texto dele nunca chega ao aplicativo sem a moldura.
- A lista de ferramentas para em 50 páginas: num servidor com mais páginas, as ferramentas das páginas seguintes
  parecem sumidas, e um servidor revisado entra em quarentena (para o lado seguro). Pela sala, uma lista com mais
  de 5.000 ferramentas nem chega ao despachante: conta como sem resposta (quarentena depois de 2 rodadas seguidas,
  com o motivo "não respondeu à conferência"), e a revisão do servidor não conclui.
- A conferência do endereço pelo nome (DNS) tem uma janela até a conexão: o nome pode mudar entre uma e outra. Na
  sala, seguram o muro da rede e o https com certificado público. Na troca de chave (OAuth) de um terceiro, que sai
  do despachante pela rede `saida`, não há muro: só o https com certificado público segura essa janela. Fora dos
  testes automáticos, que usam um servidor de teste local, a troca só vai a endereço `https://`. Com o tempo, o
  nome do fornecedor também pode passar a apontar para outro lugar: o endereço guardado não muda, mas quem tiver um
  certificado válido para esse nome recebe as chaves.
- A chave do terceiro passa aberta do despachante à sala, só pelo mTLS da rede `visitas`, e vive só no processo
  daquela chamada.
- Um endereço de troca de chave lento ocupa a porta de saída: cada ação de um terceiro assim pode segurar a porta
  única por até 15 s da troca, mais a espera da vez e a conferência do endereço, e nesse tempo nenhuma outra ação sai
  ([decisões, "Riscos aceitos da Fase 3"](decisoes.md#riscos-aceitos-da-fase-3)).
- "Não saiu" forjável por um terceiro (risco aceito, em
  [decisões, "Riscos aceitos da Fase 3"](decisoes.md#riscos-aceitos-da-fase-3)). Todo status ou código que vira
  "não saiu" pode ser forjado por um terceiro que executou a ação e respondeu assim: 401, 403 e 3xx no último
  status, e os códigos -32601, -32602, -32600, -32700 e -32021. Nesse caso, um pedido igual sai de novo, só para
  esse mesmo terceiro, sem alcance a outros sistemas. Vale igual para sistema próprio desde a Fase 2. Na sala, um
  filho tomado pelo terceiro pode escrever, depois de chamar o terceiro, qualquer resposta "nada enviado" do
  contrato, também a de sala cheia, com o mesmo alcance.
- Sala cheia em toda conferência: enquanto durar, nenhuma conferência de terceiro é feita, nem a do terceiro lento
  que ocupa as vagas (risco aceito, em [decisões, "Riscos aceitos da Fase 3"](decisoes.md#riscos-aceitos-da-fase-3)).
- Alarme falso dos três sinais: quem lê e-mail o dia todo fica quase sempre sem limite. E o teto de confirmações é
  por aplicativo, somando todas as pessoas: fichas do alerta de poucas pessoas podem pausar o aplicativo para
  todos. Os dois se medem no piloto; o dono pode trocar para teto por pessoa e aplicativo.
- A conferência de saúde da sala só faz o aperto de mãos TLS 1.3, sem certificado de cliente.
- As redes da sala não têm IPv6. Se ligarem, o muro precisa das rotas de IPv6. Se o muro reiniciar, a sala fica
  sem rede até reiniciar também (falha fechada).
- Para sistemas próprios, com um provedor de rastros instalado, o kit pode pôr um `traceparent` novo no pedido
  (decisão do dono). Para terceiros, nunca.
- O registrador `jsonrpc_dispatcher` do kit fica sem filtro: ele só escreve valores do sistema no nível DEBUG, que
  o produto não usa por padrão (`LOG_LEVEL=INFO`), e o servidor da entrada usa o mesmo registrador. Não ligue o
  DEBUG em produção.

## O que não cobrimos

- O que o aplicativo faz por outros caminhos, fora do gateway.
- Erros da IA: o gateway limita o estrago, não impede o erro.
- O que um servidor de terceiros faz do lado de lá, com o que recebe.
- Quem tem acesso total às duas máquinas ao mesmo tempo.
- Com `VAULT_SEAL_MODE=static` na mesma máquina, quem opera a máquina tem a chave do cofre.
