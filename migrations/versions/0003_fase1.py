"""Fase 1: programas, sessoes das telas, autorizacoes nos sistemas, catraca, bloqueios, avisos, ronda e entrada.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-18
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _util import sql_em_bloco

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

SQL = r"""
-- Programas que rodam sozinhos (decisao 9): cada um e um aplicativo, com responsavel e substituto com nome.
CREATE TABLE programas (
  aplicativo_id  uuid        PRIMARY KEY REFERENCES aplicativos (id),
  responsavel_id uuid        NOT NULL REFERENCES pessoas (id),
  substituto_id  uuid        NOT NULL REFERENCES pessoas (id),
  finalidade     text        NOT NULL,
  fim            date        NOT NULL,
  situacao       text        NOT NULL DEFAULT 'ativo' CHECK (situacao IN ('ativo', 'pausado', 'encerrado')),
  revisado_em    timestamptz NOT NULL DEFAULT now(),
  chave_trocada_em timestamptz NOT NULL DEFAULT now(),
  criado_em      timestamptz NOT NULL DEFAULT now(),
  CHECK (responsavel_id <> substituto_id)
);

-- Sessoes de login das telas. Guarda so o hash do identificador da sessao.
CREATE TABLE sessoes_telas (
  hash_id      bytea       PRIMARY KEY,
  pessoa_id    uuid        NOT NULL REFERENCES pessoas (id),
  csrf         text        NOT NULL,
  criada_em    timestamptz NOT NULL DEFAULT now(),
  expira_em    timestamptz NOT NULL,
  ultimo_uso   timestamptz NOT NULL DEFAULT now(),
  encerrada_em timestamptz,
  auth_time    timestamptz
);
CREATE INDEX sessoes_pessoa ON sessoes_telas (pessoa_id) WHERE encerrada_em IS NULL;

-- Pedidos de login das telas em andamento (OIDC com PKCE), de uso unico.
CREATE TABLE logins_pendentes (
  estado        text        PRIMARY KEY,
  nonce         text        NOT NULL,
  verificador   text        NOT NULL,
  voltar_para   text        NOT NULL DEFAULT '/telas',
  criado_em     timestamptz NOT NULL DEFAULT now(),
  expira_em     timestamptz NOT NULL,
  usado_em      timestamptz
);

-- Autorizacao da pessoa na pagina de um sistema (OAuth do sistema). O verificador PKCE vai cifrado.
CREATE TABLE autorizacoes_pendentes (
  estado               text        PRIMARY KEY,
  pessoa_id            uuid        NOT NULL REFERENCES pessoas (id),
  sistema_id           uuid        NOT NULL REFERENCES sistemas (id),
  verificador_cifrado  text        NOT NULL,
  criada_em            timestamptz NOT NULL DEFAULT now(),
  expira_em            timestamptz NOT NULL,
  usada_em             timestamptz
);

-- Catraca: leituras por ordem de servico por dia (READS_CAP_PER_DAY).
CREATE TABLE leituras_diarias (
  ordem_id uuid    NOT NULL REFERENCES ordens_servico (id),
  dia      date    NOT NULL,
  contagem integer NOT NULL DEFAULT 0,
  PRIMARY KEY (ordem_id, dia)
);

-- Bloqueio de um toque e pausa. Nenhuma linha e apagada; encerrar e registrar quem e quando.
CREATE TABLE bloqueios (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  tipo          text        NOT NULL CHECK (tipo IN ('bloqueio', 'pausa')),
  alcance       text        NOT NULL CHECK (alcance IN ('pessoa', 'aplicativo', 'pessoa_aplicativo', 'sistema', 'tudo')),
  pessoa_id     uuid        REFERENCES pessoas (id),
  aplicativo_id uuid        REFERENCES aplicativos (id),
  sistema_id    uuid        REFERENCES sistemas (id),
  quem          text        NOT NULL,
  motivo        text        NOT NULL,
  criado_em     timestamptz NOT NULL DEFAULT now(),
  encerrado_em  timestamptz,
  encerrado_por text
);
CREATE INDEX bloqueios_ativos ON bloqueios (alcance) WHERE encerrado_em IS NULL;

-- Destinos de webhook cadastrados pelo ADMIN e fila de avisos (aviso pode repetir; acao nao).
CREATE TABLE destinos_webhook (
  id         uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  nome       text        NOT NULL UNIQUE,
  url        text        NOT NULL CHECK (url ~ '^https://'),
  eventos    text[]      NOT NULL DEFAULT '{}',
  ativo      boolean     NOT NULL DEFAULT true,
  criado_por text        NOT NULL,
  criado_em  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE avisos (
  id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  canal       text        NOT NULL CHECK (canal IN ('email', 'webhook')),
  destino     text        NOT NULL,
  evento      text        NOT NULL,
  assunto     text        NOT NULL,
  corpo       jsonb       NOT NULL,
  chave_unica text        UNIQUE,
  situacao    text        NOT NULL DEFAULT 'pendente' CHECK (situacao IN ('pendente', 'enviado', 'desistiu')),
  tentativas  integer     NOT NULL DEFAULT 0,
  proxima_em  timestamptz NOT NULL DEFAULT now(),
  ultimo_erro text,
  criado_em   timestamptz NOT NULL DEFAULT now(),
  enviado_em  timestamptz
);
CREATE INDEX avisos_pendentes ON avisos (proxima_em) WHERE situacao = 'pendente';

-- Ronda das portas: cada tentativa indevida e o desfecho. So de acrescentar.
CREATE TABLE ronda (
  id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  rodada      uuid        NOT NULL,
  caso        text        NOT NULL,
  esperado    text        NOT NULL,
  obtido      text        NOT NULL,
  defendeu    boolean     NOT NULL,
  detalhes    jsonb       NOT NULL DEFAULT '{}'::jsonb,
  feita_em    timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER ronda_sem_update_delete BEFORE UPDATE OR DELETE ON ronda
  FOR EACH ROW EXECUTE FUNCTION extrato_recusa_mudanca();

-- Estado da entrada: a ronda pode fecha-la por etapas (PATROL_AUTOCLOSE_ENABLED). Uma linha so.
CREATE TABLE estado_entrada (
  id        smallint    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  nivel     text        NOT NULL DEFAULT 'aberta' CHECK (nivel IN ('aberta', 'so_leitura', 'fechada')),
  motivo    text,
  mudou_em  timestamptz NOT NULL DEFAULT now(),
  mudou_por text        NOT NULL DEFAULT 'instalacao'
);
INSERT INTO estado_entrada DEFAULT VALUES;

-- Porta de saida: fechada enquanto o cofre estiver trancado ou depois de restauracao (decisao humana reabre).
CREATE TABLE estado_saida (
  id        smallint    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
  aberta    boolean     NOT NULL DEFAULT true,
  motivo    text,
  mudou_em  timestamptz NOT NULL DEFAULT now(),
  mudou_por text        NOT NULL DEFAULT 'instalacao'
);
INSERT INTO estado_saida DEFAULT VALUES;

-- E-mail da pessoa, vindo do cracha quando verificado; so para os avisos a ela.
ALTER TABLE pessoas ADD COLUMN email text;

-- Chave so para ler as declaracoes de um sistema por pessoa (mudou-parou), guardada pelo ADMIN.
ALTER TABLE chaves_cifradas DROP CONSTRAINT chaves_cifradas_tipo_check;
ALTER TABLE chaves_cifradas ADD CONSTRAINT chaves_cifradas_tipo_check
  CHECK (tipo IN ('acesso', 'renovacao', 'declaracoes'));

-- Particoes: a funcao cria particao como dona da tabela-mae, so para o extrato e as medicoes por minuto.
CREATE OR REPLACE FUNCTION garantir_particao_mensal(p_tabela regclass, p_dia date) RETURNS text
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  inicio date := date_trunc('month', p_dia)::date;
  fim    date := (date_trunc('month', p_dia) + interval '1 month')::date;
  base   text := (SELECT relname FROM pg_class WHERE oid = p_tabela);
  esquema text := (SELECT nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = p_tabela);
  nome   text := base || '_' || to_char(inicio, 'YYYY_MM');
BEGIN
  IF p_tabela NOT IN ('public.extrato'::regclass, 'medicoes.minuto'::regclass) THEN
    RAISE EXCEPTION 'Particao mensal so para o extrato e as medicoes por minuto' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE c.relname = nome AND n.nspname = esquema) THEN
    EXECUTE format('CREATE TABLE %I.%I PARTITION OF %s FOR VALUES FROM (%L) TO (%L)',
                   esquema, nome, p_tabela, inicio, fim);
  END IF;
  RETURN esquema || '.' || nome;
END $$;
REVOKE ALL ON FUNCTION garantir_particao_mensal(regclass, date) FROM PUBLIC;
"""

GRANTS = r"""
GRANT SELECT ON programas, bloqueios, estado_entrada, estado_saida, destinos_webhook
  TO mcp_claro_entrada, mcp_claro_despachante, mcp_claro_rotinas;
GRANT INSERT, UPDATE ON programas, destinos_webhook TO mcp_claro_entrada;
GRANT SELECT, INSERT ON sessoes_telas, logins_pendentes TO mcp_claro_entrada;
GRANT UPDATE (ultimo_uso, encerrada_em) ON sessoes_telas TO mcp_claro_entrada;
GRANT UPDATE (usado_em) ON logins_pendentes TO mcp_claro_entrada;
-- Autorizacao nos sistemas: o despachante cria o pedido (cifra o verificador PKCE; a entrada nao cifra).
GRANT SELECT, INSERT, UPDATE (usada_em) ON autorizacoes_pendentes TO mcp_claro_despachante;
GRANT SELECT (estado, pessoa_id, sistema_id, expira_em, usada_em) ON autorizacoes_pendentes TO mcp_claro_entrada;
GRANT UPDATE (email) ON pessoas TO mcp_claro_entrada;
GRANT SELECT (expira_em, encerrada_em) ON sessoes_telas TO mcp_claro_rotinas;
GRANT SELECT (expira_em, usado_em) ON logins_pendentes TO mcp_claro_rotinas;
GRANT SELECT (expira_em, usada_em) ON autorizacoes_pendentes TO mcp_claro_rotinas;
GRANT DELETE ON autorizacoes_pendentes, recusas_versao TO mcp_claro_rotinas;
GRANT DELETE ON medicoes.hora, medicoes.dia TO mcp_claro_rotinas;
GRANT EXECUTE ON FUNCTION garantir_particao_mensal(regclass, date) TO mcp_claro_rotinas;
GRANT SELECT, INSERT, UPDATE ON leituras_diarias TO mcp_claro_entrada;
GRANT INSERT, UPDATE (encerrado_em, encerrado_por) ON bloqueios TO mcp_claro_entrada, mcp_claro_rotinas;
GRANT INSERT ON avisos TO mcp_claro_entrada, mcp_claro_despachante;
GRANT SELECT, INSERT, UPDATE ON avisos TO mcp_claro_rotinas;
GRANT SELECT, INSERT ON ronda TO mcp_claro_rotinas;
GRANT SELECT ON ronda TO mcp_claro_entrada;
GRANT UPDATE ON estado_entrada TO mcp_claro_rotinas, mcp_claro_entrada;
GRANT UPDATE ON estado_saida TO mcp_claro_despachante, mcp_claro_entrada;
GRANT UPDATE (situacao) ON programas TO mcp_claro_rotinas;
GRANT UPDATE (situacao) ON aplicativos TO mcp_claro_rotinas;
GRANT DELETE ON sessoes_telas, logins_pendentes TO mcp_claro_rotinas;
"""


def upgrade() -> None:
    sql_em_bloco(SQL)
    sql_em_bloco(GRANTS)


def downgrade() -> None:
    raise RuntimeError("O MCP-CLARO nao desfaz migracoes; restaure o backup se precisar voltar.")
