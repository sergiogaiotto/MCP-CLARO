"""Banco do vigia: fechamentos guardados, rondas de fora, alarmes e a fila de avisos do vigia.

Tudo e so de acrescentar, menos a fila de avisos (o envio marca tentativas). Um gatilho recusa
UPDATE, DELETE e TRUNCATE; outro confere, em cada fechamento novo, o encadeamento no anterior
guardado. O usuario do papel (``mcp_claro_vigia``) so le e acrescenta.

Revision ID: v0001
Revises:
Create Date: 2026-11-18
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _util import sql_em_bloco

revision = "v0001"
down_revision = None
branch_labels = None
depends_on = None

SQL = r"""
-- Papel do vigia. Criado pelo script de iniciacao do banco do vigia; aqui so garantimos que exista.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_claro_vigia') THEN
    CREATE ROLE mcp_claro_vigia NOLOGIN;
  END IF;
END $$;

CREATE FUNCTION vigia_recusa_mudanca() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'O banco do vigia e so de acrescentar: % recusado em %', TG_OP, TG_TABLE_NAME
    USING ERRCODE = 'insufficient_privilege';
END $$;

-- Copia dos fechamentos do gateway, conferida pelo encadeamento.
CREATE TABLE fechamentos_guardados (
  numero        bigint      PRIMARY KEY CHECK (numero >= 1),
  desde_xid     xid8        NOT NULL,
  ate_xid       xid8        NOT NULL,
  linhas        bigint      NOT NULL CHECK (linhas >= 0),
  hash_anterior bytea,
  hash          bytea       NOT NULL CHECK (length(hash) = 32),
  calculado_em  timestamptz,
  guardado_em   timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (ate_xid >= desde_xid),
  CHECK ((numero = 1) = (hash_anterior IS NULL))
);

CREATE FUNCTION vigia_confere_encadeamento() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
  anterior fechamentos_guardados;
BEGIN
  IF NEW.numero = 1 THEN
    IF NEW.desde_xid <> '0'::xid8 THEN
      RAISE EXCEPTION 'O fechamento 1 precisa comecar do inicio do extrato' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;
  SELECT * INTO anterior FROM fechamentos_guardados WHERE numero = NEW.numero - 1;
  IF NOT FOUND THEN
    RAISE EXCEPTION 'O fechamento % chegou sem o anterior guardado', NEW.numero USING ERRCODE = 'check_violation';
  END IF;
  IF anterior.hash <> NEW.hash_anterior OR anterior.ate_xid <> NEW.desde_xid THEN
    RAISE EXCEPTION 'O fechamento % nao encadeia no % guardado', NEW.numero, anterior.numero
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER fechamentos_guardados_encadeamento BEFORE INSERT ON fechamentos_guardados
  FOR EACH ROW EXECUTE FUNCTION vigia_confere_encadeamento();
CREATE TRIGGER fechamentos_guardados_sem_update_delete BEFORE UPDATE OR DELETE ON fechamentos_guardados
  FOR EACH ROW EXECUTE FUNCTION vigia_recusa_mudanca();
CREATE TRIGGER fechamentos_guardados_sem_truncate BEFORE TRUNCATE ON fechamentos_guardados
  FOR EACH STATEMENT EXECUTE FUNCTION vigia_recusa_mudanca();

-- Ronda de fora: cada tentativa e o desfecho, como a tabela ronda do gateway.
CREATE TABLE rondas_vigia (
  id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  rodada      uuid        NOT NULL,
  caso        text        NOT NULL,
  esperado    text        NOT NULL,
  obtido      text        NOT NULL,
  defendeu    boolean     NOT NULL,
  detalhes    jsonb       NOT NULL DEFAULT '{}'::jsonb,
  feita_em    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX rondas_vigia_rodada ON rondas_vigia (rodada);
CREATE TRIGGER rondas_vigia_sem_update_delete BEFORE UPDATE OR DELETE ON rondas_vigia
  FOR EACH ROW EXECUTE FUNCTION vigia_recusa_mudanca();
CREATE TRIGGER rondas_vigia_sem_truncate BEFORE TRUNCATE ON rondas_vigia
  FOR EACH STATEMENT EXECUTE FUNCTION vigia_recusa_mudanca();

-- Alarmes: um por problema (chave unica), nunca apagado.
CREATE TABLE alarmes (
  id         bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  tipo       text        NOT NULL CHECK (tipo IN ('silencio', 'voltou', 'ronda', 'encadeamento_quebrado',
                                                  'fechamento_mudou', 'fechamento_sumiu',
                                                  'fechamentos_sem_resposta')),
  chave      text        NOT NULL UNIQUE,
  resumo     text        NOT NULL,
  dados      jsonb       NOT NULL DEFAULT '{}'::jsonb,
  criado_em  timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER alarmes_sem_update_delete BEFORE UPDATE OR DELETE ON alarmes
  FOR EACH ROW EXECUTE FUNCTION vigia_recusa_mudanca();
CREATE TRIGGER alarmes_sem_truncate BEFORE TRUNCATE ON alarmes
  FOR EACH STATEMENT EXECUTE FUNCTION vigia_recusa_mudanca();

-- Fila de avisos do vigia: mesma forma da fila do gateway (mcp_claro.avisos), canais proprios.
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

REVOKE ALL ON fechamentos_guardados, rondas_vigia, alarmes, avisos FROM PUBLIC;
"""

GRANTS = r"""
GRANT USAGE ON SCHEMA public TO mcp_claro_vigia;
GRANT SELECT, INSERT ON fechamentos_guardados, rondas_vigia, alarmes TO mcp_claro_vigia;
GRANT SELECT, INSERT ON avisos TO mcp_claro_vigia;
GRANT UPDATE (situacao, tentativas, proxima_em, ultimo_erro, enviado_em) ON avisos TO mcp_claro_vigia;
"""


def upgrade() -> None:
    sql_em_bloco(SQL)
    sql_em_bloco(GRANTS)


def downgrade() -> None:
    raise RuntimeError("O vigia nao desfaz migracoes; restaure o backup se precisar voltar.")
