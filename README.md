# MCP-CLARO

MCP-CLARO, o MCP-Gateway da Claro.

É o ponto comum por onde aplicativos de IA usam ferramentas de outros sistemas. É um produto independente, com tela e
login próprios. Ele fala **só a versão 2026-07-28 do MCP**, a mais atual publicada pela fundação do MCP, ligada à Linux
Foundation, dos dois lados: com os aplicativos e com os sistemas (decisão 8).

- Tudo de código aberto, em Docker, e o único banco é o PostgreSQL.
- Núcleo em Python 3.14, sobre o kit oficial `mcp` 2.3.0.
- Portão próprio (Caddy) na frente. O Apigee fica para depois, por parâmetro (`EDGE_MODE`), e a partida o recusa por ora.

Leia primeiro: [proposta](docs/proposta.md), [plano](docs/plano.md), [decisões](docs/decisoes.md) e
[andamento](docs/andamento.md).

## Como roda

Uma imagem, quatro papéis, escolhidos pelo comando de partida:

| Papel | O que faz |
| --- | --- |
| `entrada` | Porteiro de versão, crachá, vitrine, regras e telas |
| `despachante` | Único que fala com os sistemas e abre as chaves |
| `rotinas` | Ronda das portas, vencimentos, avisos, fechamento e resumos |
| `vigia` | Na segunda máquina: ronda de fora, alarme de silêncio e guarda do fechamento |

## Começar em desenvolvimento

Requer Docker e [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

```bash
uv run pytest -m "not integracao"
```

```bash
uv run pytest -m integracao
```

Subir o ambiente de teste completo, com segredos só de teste:

```bash
bash scripts/gerar-segredos-dev.sh
```

```bash
docker compose -f docker/compose.yml up -d --build
```

```bash
curl -k --resolve mcp-claro.localhost:443:127.0.0.1 https://mcp-claro.localhost/saude
```

Atrás de um proxy com inspeção de TLS, passe a autoridade da empresa na construção:
`docker build --secret id=ca_corporativa,src=ca.pem ...`.

## Conferir os parâmetros

```bash
uv run mcp-claro conferir
```

A partida recusa combinações inválidas: versão do MCP diferente de 2026-07-28, `EDGE_MODE=apigee`, banco sem
`verify-full` em piloto ou produção, fases ligadas fora de ordem, entre outras. A lista completa está em
`src/mcp_claro/config/settings.py` e no [plano](docs/plano.md#parâmetros).

## Organização

```
src/mcp_claro/   pacote do produto (config, entrada, cadastro, regras, saida, ligacao, cofre, telas, extrato, ...)
migrations/      migrações Alembic; o esquema só muda por migração revisada
docker/          Dockerfiles, Compose e Caddyfile
exemplos/        sistemas de exemplo (fora da imagem do produto)
conformidade/    suíte oficial de conformidade fixada e lista de falhas esperadas
grafana/         painéis e alertas versionados
tests/           unidade, propriedades, integração e conformidade
docs/            proposta, plano, decisões, modelo de ameaças, roteiros
```

## Segurança

Nenhum segredo fica no repositório. Veja o [roteiro de segredos](docs/segredos.md) e o
[modelo de ameaças](docs/modelo-de-ameacas.md).
