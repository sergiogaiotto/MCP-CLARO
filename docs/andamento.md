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
| Sem volta automática | Feito: uma chamada por pedido, sem respostas sozinhas, sem reenvio depois de 401, sem GET de retomada |
| OpenBao sobre PostgreSQL com Transit | Feito: cliente fino, AppRole por papel, selo `static` em teste |
| Caddy na frente, com mTLS na porta interna | Feito: cada porta interna aceita só quem pode chamá-la |
| Simulado do dia ruim | Roteiro em `docs/simulado-dia-ruim.md`; falta rodar com os pilotos |
| Suíte oficial através do Caddy, com o gateway na frente do exemplo | Feito (`conformidade/gateway.py`): 10 dos 37 cenários exigidos passam inteiros, direto na entrada e pelo portão; os outros 27 falham só no que o desenho deixa de fora, com motivo em `conformidade/falhas-esperadas-gateway.yml` |

Provas de saída automatizadas em `tests/integracao/test_provar_a_base.py`, `test_cofre.py`, `test_keycloak.py`,
`test_conformidade_gateway.py` e `tests/unidade/test_ligacao_classifica.py`. A suíte oficial através do gateway roda
no CI, no job "suite oficial atraves do gateway".
