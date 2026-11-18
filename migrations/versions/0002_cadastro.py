"""Cadastro: aplicativos, pessoas, sistemas, ferramentas revisadas, ordens de servico e chaves cifradas.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-18
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _util import sql_em_bloco  # noqa: E402

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

SQL = r"""
-- Aplicativos habilitados pelo ADMIN. Programas que rodam sozinhos tambem sao aplicativos (decisao 9).
CREATE TABLE aplicativos (
  id               uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  client_id        text        NOT NULL UNIQUE,
  nome             text        NOT NULL,
  familia          text        NOT NULL,
  tipo             text        NOT NULL DEFAULT 'assistente' CHECK (tipo IN ('assistente', 'programa')),
  situacao         text        NOT NULL DEFAULT 'ativo' CHECK (situacao IN ('ativo', 'pausado', 'bloqueado')),
  versao_mcp_vista text,
  visto_em         timestamptz,
  criado_em        timestamptz NOT NULL DEFAULT now(),
  atualizado_em    timestamptz NOT NULL DEFAULT now()
);

-- Pessoas: nascem no primeiro login. Unicas por emissor e sujeito.
CREATE TABLE pessoas (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  emissor       text        NOT NULL,
  sujeito       text        NOT NULL,
  nome          text,
  papel         text        NOT NULL DEFAULT 'pessoa' CHECK (papel IN ('pessoa', 'admin')),
  situacao      text        NOT NULL DEFAULT 'ativa' CHECK (situacao IN ('ativa', 'desativada', 'bloqueada')),
  teste         boolean     NOT NULL DEFAULT false,
  criado_em     timestamptz NOT NULL DEFAULT now(),
  atualizado_em timestamptz NOT NULL DEFAULT now(),
  UNIQUE (emissor, sujeito)
);

-- Sistemas conectados. So entram se falarem a 2026-07-28 (sondagem server/discover no cadastro).
CREATE TABLE sistemas (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  nome          text        NOT NULL UNIQUE,
  endereco      text        NOT NULL CHECK (endereco ~ '^https?://'),
  origem        text        NOT NULL DEFAULT 'proprio' CHECK (origem IN ('proprio', 'terceiro')),
  tipo_chave    text        NOT NULL CHECK (tipo_chave IN ('por_pessoa', 'compartilhada', 'programa')),
  selo          smallint    NOT NULL DEFAULT 1 CHECK (selo BETWEEN 1 AND 3),
  versao_mcp    text,
  sondagem      jsonb,
  sondado_em    timestamptz,
  situacao      text        NOT NULL DEFAULT 'pendente'
                CHECK (situacao IN ('pendente', 'ativo', 'quarentena', 'recusado', 'desativado')),
  motivo        text,
  autorizacao   jsonb,
  criado_em     timestamptz NOT NULL DEFAULT now(),
  atualizado_em timestamptz NOT NULL DEFAULT now()
);

-- Ferramentas revisadas pelo ADMIN. Cada mudanca e uma nova versao; a vitrine sai daqui.
CREATE TABLE ferramentas_revisadas (
  id              uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  sistema_id      uuid        NOT NULL REFERENCES sistemas (id),
  nome_original   text        NOT NULL,
  versao          integer     NOT NULL,
  nome_vitrine    text        NOT NULL CHECK (nome_vitrine ~ '^[A-Za-z0-9_.-]{1,128}$'),
  titulo          text,
  descricao       text        NOT NULL,
  esquema_entrada jsonb       NOT NULL,
  esquema_saida   jsonb,
  etiquetas       jsonb       NOT NULL DEFAULT '{}'::jsonb,
  classe          text        NOT NULL CHECK (classe IN ('le', 'muda', 'tem_volta')),
  risco           text        NOT NULL DEFAULT 'medio' CHECK (risco IN ('baixo', 'medio', 'alto')),
  campos_pessoais text[]      NOT NULL DEFAULT '{}',
  impressao       text        NOT NULL,
  revisado_por    text        NOT NULL,
  revisado_em     timestamptz NOT NULL DEFAULT now(),
  situacao        text        NOT NULL DEFAULT 'ativa' CHECK (situacao IN ('ativa', 'suspensa', 'substituida')),
  motivo          text,
  UNIQUE (sistema_id, nome_original, versao)
);
CREATE UNIQUE INDEX ferramentas_vitrine_ativa ON ferramentas_revisadas (nome_vitrine) WHERE situacao = 'ativa';
CREATE UNIQUE INDEX ferramentas_original_ativa ON ferramentas_revisadas (sistema_id, nome_original)
  WHERE situacao = 'ativa';

-- O que o sistema declarou na ultima leitura (base do mudou-parou).
CREATE TABLE ferramentas_vistas (
  sistema_id uuid        NOT NULL REFERENCES sistemas (id),
  nome       text        NOT NULL,
  definicao  jsonb       NOT NULL,
  impressao  text        NOT NULL,
  vista_em   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (sistema_id, nome)
);

-- Ordens de servico: modelos do ADMIN e ordens concedidas a uma pessoa para um aplicativo.
CREATE TABLE modelos_ordem (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  nome          text        NOT NULL UNIQUE,
  descricao     text        NOT NULL,
  validade_dias integer     NOT NULL CHECK (validade_dias BETWEEN 1 AND 365),
  criado_por    text        NOT NULL,
  ativo         boolean     NOT NULL DEFAULT true,
  criado_em     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE modelos_ordem_ferramentas (
  modelo_id      uuid NOT NULL REFERENCES modelos_ordem (id),
  sistema_id     uuid NOT NULL REFERENCES sistemas (id),
  nome_original  text NOT NULL,
  PRIMARY KEY (modelo_id, sistema_id, nome_original)
);
CREATE TABLE ordens_servico (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  pessoa_id     uuid        NOT NULL REFERENCES pessoas (id),
  aplicativo_id uuid        NOT NULL REFERENCES aplicativos (id),
  modelo_id     uuid        NOT NULL REFERENCES modelos_ordem (id),
  inicio        timestamptz NOT NULL DEFAULT now(),
  fim           timestamptz NOT NULL,
  situacao      text        NOT NULL DEFAULT 'ativa'
                CHECK (situacao IN ('ativa', 'vencida', 'revogada', 'inativa', 'pausada')),
  ultimo_uso    timestamptz,
  criado_em     timestamptz NOT NULL DEFAULT now(),
  CHECK (fim > inicio)
);
CREATE INDEX ordens_pessoa_app ON ordens_servico (pessoa_id, aplicativo_id) WHERE situacao = 'ativa';

-- Chaves das pessoas, dos programas e compartilhadas, cifradas pelo Transit do cofre.
CREATE TABLE chaves_cifradas (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  sistema_id    uuid        NOT NULL REFERENCES sistemas (id),
  pessoa_id     uuid        REFERENCES pessoas (id),
  aplicativo_id uuid        REFERENCES aplicativos (id),
  tipo          text        NOT NULL CHECK (tipo IN ('acesso', 'renovacao')),
  cifrada       text        NOT NULL CHECK (cifrada LIKE 'vault:v%'),
  expira_em     timestamptz,
  atualizada_em timestamptz NOT NULL DEFAULT now(),
  CHECK (pessoa_id IS NULL OR aplicativo_id IS NULL)
);
CREATE UNIQUE INDEX chaves_unicas ON chaves_cifradas
  (sistema_id, coalesce(pessoa_id, '00000000-0000-0000-0000-000000000000'::uuid),
   coalesce(aplicativo_id, '00000000-0000-0000-0000-000000000000'::uuid), tipo);
"""

GRANTS = r"""
-- Leitura do cadastro por todos os papeis.
GRANT SELECT ON aplicativos, pessoas, sistemas, ferramentas_revisadas, modelos_ordem, modelos_ordem_ferramentas,
  ordens_servico, ferramentas_vistas TO mcp_claro_entrada, mcp_claro_despachante, mcp_claro_rotinas;
-- A entrada atende as telas do ADMIN e o primeiro login das pessoas.
GRANT INSERT, UPDATE ON aplicativos, pessoas, sistemas, ferramentas_revisadas, modelos_ordem,
  ordens_servico TO mcp_claro_entrada;
GRANT INSERT, DELETE ON modelos_ordem_ferramentas TO mcp_claro_entrada;
-- O despachante grava a sondagem de versao, o que os sistemas declaram e as chaves cifradas.
GRANT UPDATE (versao_mcp, sondagem, sondado_em, situacao, motivo, atualizado_em) ON sistemas TO mcp_claro_despachante;
GRANT INSERT, UPDATE ON ferramentas_vistas TO mcp_claro_despachante;
GRANT SELECT, INSERT, UPDATE, DELETE ON chaves_cifradas TO mcp_claro_despachante;
-- As rotinas vencem ordens e poem ferramentas e sistemas em quarentena.
GRANT UPDATE (situacao) ON ordens_servico TO mcp_claro_rotinas;
GRANT UPDATE (situacao, motivo) ON ferramentas_revisadas TO mcp_claro_rotinas;
GRANT UPDATE (situacao, motivo, atualizado_em) ON sistemas TO mcp_claro_rotinas;
"""


def upgrade() -> None:
    sql_em_bloco(SQL)
    sql_em_bloco(GRANTS)


def downgrade() -> None:
    raise RuntimeError("O MCP-CLARO nao desfaz migracoes; restaure o backup se precisar voltar.")
