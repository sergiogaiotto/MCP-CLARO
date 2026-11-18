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
