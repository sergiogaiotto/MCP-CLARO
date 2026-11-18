"""Base: politica, extrato so de acrescentar, fechamentos, continuidade, contagens.

Revision ID: 0001
Revises:
Create Date: 2026-11-18
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _util import sql_em_bloco

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


PAPEIS = ("mcp_claro_entrada", "mcp_claro_despachante", "mcp_claro_rotinas")

SQL = r"""
-- Papeis do banco. Criados pelo script de iniciacao do PostgreSQL; aqui so
-- garantimos que existam (sem login) quando o banco de teste nao os criou.
DO $$
DECLARE papel text;
BEGIN
  FOREACH papel IN ARRAY ARRAY['mcp_claro_entrada','mcp_claro_despachante','mcp_claro_rotinas','grafana_leitor']
  LOOP
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = papel) THEN
      EXECUTE format('CREATE ROLE %I NOLOGIN', papel);
    END IF;
  END LOOP;
END $$;

CREATE SCHEMA IF NOT EXISTS medicoes;

-- Valores de politica (decisao 11): versionados, com autor. Vale a linha mais nova.
CREATE TABLE politica (
  id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  chave       text        NOT NULL,
  valor       integer     NOT NULL,
  autor       text        NOT NULL,
  motivo      text        NOT NULL,
  criado_em   timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX politica_chave_idx ON politica (chave, id DESC);
CREATE VIEW politica_vigente AS
  SELECT DISTINCT ON (chave) chave, valor, autor, motivo, criado_em
  FROM politica ORDER BY chave, id DESC;

-- Extrato: cada passo em linhas so de acrescentar, particionado por mes.
CREATE TABLE extrato (
  id            bigint GENERATED ALWAYS AS IDENTITY,
  criado_em     timestamptz NOT NULL DEFAULT clock_timestamp(),
  xid           xid8        NOT NULL DEFAULT pg_current_xact_id(),
  papel         text        NOT NULL,
  tipo          text        NOT NULL,
  frase         text        NOT NULL,
  protocolo     text,
  aplicativo_id uuid,
  pessoa_id     uuid,
  sistema_id    uuid,
  ferramenta    text,
  dados         jsonb       NOT NULL DEFAULT '{}'::jsonb,
  trace_id      text,
  hash_linha    bytea       NOT NULL,
  PRIMARY KEY (criado_em, id)
) PARTITION BY RANGE (criado_em);
CREATE INDEX extrato_xid_idx ON extrato (xid, id);
CREATE INDEX extrato_protocolo_idx ON extrato (protocolo) WHERE protocolo IS NOT NULL;
CREATE INDEX extrato_pessoa_idx ON extrato (pessoa_id, criado_em DESC) WHERE pessoa_id IS NOT NULL;

CREATE FUNCTION extrato_recusa_mudanca() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'O extrato e so de acrescentar: % recusado', TG_OP
    USING ERRCODE = 'insufficient_privilege';
END $$;

CREATE TRIGGER extrato_sem_update_delete BEFORE UPDATE OR DELETE ON extrato
  FOR EACH ROW EXECUTE FUNCTION extrato_recusa_mudanca();
CREATE TRIGGER extrato_sem_truncate BEFORE TRUNCATE ON extrato
  FOR EACH STATEMENT EXECUTE FUNCTION extrato_recusa_mudanca();

-- Hash canonico de uma linha do extrato.
CREATE FUNCTION extrato_hash(
  p_criado_em timestamptz, p_papel text, p_tipo text, p_frase text, p_protocolo text,
  p_aplicativo uuid, p_pessoa uuid, p_sistema uuid, p_ferramenta text, p_dados jsonb, p_trace text
) RETURNS bytea LANGUAGE sql STABLE AS $$
  SELECT sha256(convert_to(jsonb_build_array(
    to_char(p_criado_em AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
    p_papel, p_tipo, p_frase, p_protocolo, p_aplicativo, p_pessoa, p_sistema, p_ferramenta, p_dados, p_trace
  )::text, 'UTF8'))
$$;

-- Unica porta de escrita do extrato, usada por todos os papeis.
CREATE FUNCTION extrato_registrar(
  p_papel text, p_tipo text, p_frase text, p_protocolo text DEFAULT NULL,
  p_aplicativo uuid DEFAULT NULL, p_pessoa uuid DEFAULT NULL, p_sistema uuid DEFAULT NULL,
  p_ferramenta text DEFAULT NULL, p_dados jsonb DEFAULT '{}'::jsonb, p_trace text DEFAULT NULL
) RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  agora timestamptz := clock_timestamp();
  novo bigint;
BEGIN
  INSERT INTO extrato (criado_em, papel, tipo, frase, protocolo, aplicativo_id, pessoa_id, sistema_id,
                       ferramenta, dados, trace_id, hash_linha)
  VALUES (agora, p_papel, p_tipo, p_frase, p_protocolo, p_aplicativo, p_pessoa, p_sistema,
          p_ferramenta, coalesce(p_dados, '{}'::jsonb), p_trace,
          extrato_hash(agora, p_papel, p_tipo, p_frase, p_protocolo, p_aplicativo, p_pessoa, p_sistema,
                       p_ferramenta, coalesce(p_dados, '{}'::jsonb), p_trace))
  RETURNING id INTO novo;
  RETURN novo;
END $$;

-- Particoes mensais, criadas por rotina propria (sem pg_partman).
CREATE FUNCTION garantir_particao_mensal(p_tabela regclass, p_dia date) RETURNS text
LANGUAGE plpgsql AS $$
DECLARE
  inicio date := date_trunc('month', p_dia)::date;
  fim    date := (date_trunc('month', p_dia) + interval '1 month')::date;
  base   text := (SELECT relname FROM pg_class WHERE oid = p_tabela);
  esquema text := (SELECT nspname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace WHERE c.oid = p_tabela);
  nome   text := base || '_' || to_char(inicio, 'YYYY_MM');
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE c.relname = nome AND n.nspname = esquema) THEN
    EXECUTE format('CREATE TABLE %I.%I PARTITION OF %s FOR VALUES FROM (%L) TO (%L)',
                   esquema, nome, p_tabela, inicio, fim);
  END IF;
  RETURN esquema || '.' || nome;
END $$;

SELECT garantir_particao_mensal('extrato', (current_date + make_interval(months => m))::date)
FROM generate_series(-1, 3) AS m;

-- Fechamentos: cada um encadeia o anterior e cobre so transacoes ja terminadas.
CREATE TABLE fechamentos (
  numero        bigint      PRIMARY KEY,
  anterior      bigint      UNIQUE REFERENCES fechamentos (numero),
  desde_xid     xid8        NOT NULL,
  ate_xid       xid8        NOT NULL,
  linhas        bigint      NOT NULL,
  hash_anterior bytea,
  hash          bytea       NOT NULL,
  calculado_em  timestamptz NOT NULL DEFAULT clock_timestamp(),
  CHECK (ate_xid >= desde_xid),
  CHECK ((numero = 1 AND anterior IS NULL) OR (numero > 1 AND anterior = numero - 1))
);
CREATE TRIGGER fechamentos_sem_update_delete BEFORE UPDATE OR DELETE ON fechamentos
  FOR EACH ROW EXECUTE FUNCTION extrato_recusa_mudanca();
CREATE TRIGGER fechamentos_sem_truncate BEFORE TRUNCATE ON fechamentos
  FOR EACH STATEMENT EXECUTE FUNCTION extrato_recusa_mudanca();

-- Conta o hash de um trecho do extrato: linhas com desde <= xid < ate, em ordem.
CREATE FUNCTION extrato_hash_trecho(p_desde xid8, p_ate xid8, p_hash_anterior bytea)
RETURNS TABLE (linhas bigint, hash bytea) LANGUAGE sql STABLE AS $$
  SELECT count(*)::bigint,
         sha256(coalesce(p_hash_anterior, '\x'::bytea) ||
                coalesce(string_agg(e.hash_linha, '\x'::bytea ORDER BY e.xid, e.id), '\x'::bytea))
  FROM extrato e
  WHERE e.xid >= p_desde AND e.xid < p_ate
$$;

-- Um fechamento por vez (trava consultiva propria). Para antes da transacao aberta mais antiga.
CREATE FUNCTION extrato_fechar() RETURNS fechamentos LANGUAGE plpgsql SECURITY DEFINER
SET search_path = public, pg_temp AS $$
DECLARE
  ultimo fechamentos;
  novo   fechamentos;
  limite xid8 := pg_snapshot_xmin(pg_current_snapshot());
  r      record;
BEGIN
  IF NOT pg_try_advisory_xact_lock(hashtext('mcp_claro.fechamento')) THEN
    RAISE EXCEPTION 'Outro fechamento esta em andamento' USING ERRCODE = 'lock_not_available';
  END IF;
  SELECT * INTO ultimo FROM fechamentos ORDER BY numero DESC LIMIT 1;
  IF ultimo.numero IS NULL THEN
    novo.numero := 1; novo.anterior := NULL; novo.desde_xid := '0'::xid8; novo.hash_anterior := NULL;
  ELSE
    novo.numero := ultimo.numero + 1; novo.anterior := ultimo.numero;
    novo.desde_xid := ultimo.ate_xid; novo.hash_anterior := ultimo.hash;
  END IF;
  IF limite < novo.desde_xid THEN
    limite := novo.desde_xid;
  END IF;
  novo.ate_xid := limite;
  SELECT * INTO r FROM extrato_hash_trecho(novo.desde_xid, novo.ate_xid, novo.hash_anterior);
  novo.linhas := r.linhas; novo.hash := r.hash; novo.calculado_em := clock_timestamp();
  INSERT INTO fechamentos SELECT (novo).*;
  RETURN novo;
END $$;

-- Refaz a conta de um fechamento guardado e diz se ainda bate.
CREATE FUNCTION extrato_conferir(p_numero bigint) RETURNS boolean LANGUAGE sql STABLE AS $$
  SELECT f.hash = t.hash AND f.linhas = t.linhas
  FROM fechamentos f, LATERAL extrato_hash_trecho(f.desde_xid, f.ate_xid, f.hash_anterior) t
  WHERE f.numero = p_numero
$$;

-- Continuidade do banco: linha do tempo e identificador vistos na partida.
CREATE TABLE continuidade_banco (
  id                bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  system_identifier text        NOT NULL,
  timeline_id       integer     NOT NULL,
  ultimo_fechamento bigint,
  restaurado        boolean     NOT NULL DEFAULT false,
  motivo            text,
  visto_em          timestamptz NOT NULL DEFAULT clock_timestamp()
);

-- Recusas por versao antiga, resumidas por minuto, dos dois lados (decisao 8).
CREATE TABLE recusas_versao (
  minuto        timestamptz NOT NULL,
  lado          text        NOT NULL CHECK (lado IN ('aplicativo', 'sistema')),
  origem        text        NOT NULL,
  versao_pedida text        NOT NULL,
  motivo        text        NOT NULL,
  contagem      bigint      NOT NULL CHECK (contagem > 0),
  PRIMARY KEY (minuto, lado, origem, versao_pedida, motivo)
);

-- Medicoes sem pessoa e sem conteudo.
CREATE TABLE medicoes.minuto (
  minuto   timestamptz NOT NULL,
  nome     text        NOT NULL,
  rotulos  jsonb       NOT NULL DEFAULT '{}'::jsonb,
  valor    bigint      NOT NULL,
  PRIMARY KEY (minuto, nome, rotulos)
) PARTITION BY RANGE (minuto);
SELECT garantir_particao_mensal('medicoes.minuto', (current_date + make_interval(months => m))::date)
FROM generate_series(-1, 3) AS m;

CREATE TABLE medicoes.hora (
  hora     timestamptz NOT NULL,
  nome     text        NOT NULL,
  rotulos  jsonb       NOT NULL DEFAULT '{}'::jsonb,
  valor    bigint      NOT NULL,
  PRIMARY KEY (hora, nome, rotulos)
);
CREATE TABLE medicoes.dia (
  dia      date        NOT NULL,
  nome     text        NOT NULL,
  rotulos  jsonb       NOT NULL DEFAULT '{}'::jsonb,
  valor    bigint      NOT NULL,
  PRIMARY KEY (dia, nome, rotulos)
);
CREATE VIEW medicoes.v_recusas_versao AS
  SELECT minuto, lado, origem, versao_pedida, motivo, contagem FROM public.recusas_versao;

-- Permissoes: o extrato so recebe INSERT pela funcao; ninguem muda nem apaga.
REVOKE ALL ON extrato, fechamentos FROM PUBLIC;
REVOKE ALL ON FUNCTION extrato_registrar(text, text, text, text, uuid, uuid, uuid, text, jsonb, text) FROM PUBLIC;
REVOKE ALL ON FUNCTION extrato_fechar() FROM PUBLIC;
"""

GRANTS = r"""
GRANT USAGE ON SCHEMA public TO {papel};
GRANT EXECUTE ON FUNCTION extrato_registrar(text, text, text, text, uuid, uuid, uuid, text, jsonb, text) TO {papel};
GRANT SELECT ON extrato, fechamentos, politica, politica_vigente TO {papel};
GRANT INSERT ON recusas_versao TO {papel};
GRANT UPDATE (contagem) ON recusas_versao TO {papel};
GRANT SELECT ON recusas_versao TO {papel};
GRANT USAGE ON SCHEMA medicoes TO {papel};
GRANT SELECT, INSERT ON medicoes.minuto TO {papel};
GRANT UPDATE (valor) ON medicoes.minuto TO {papel};
"""

GRANTS_ROTINAS = r"""
GRANT EXECUTE ON FUNCTION extrato_fechar() TO mcp_claro_rotinas;
GRANT EXECUTE ON FUNCTION garantir_particao_mensal(regclass, date) TO mcp_claro_rotinas;
GRANT SELECT, INSERT, UPDATE ON medicoes.hora, medicoes.dia TO mcp_claro_rotinas;
GRANT DELETE ON medicoes.minuto TO mcp_claro_rotinas;
GRANT SELECT, INSERT ON continuidade_banco TO mcp_claro_rotinas, mcp_claro_despachante;
GRANT INSERT ON politica TO mcp_claro_entrada;
"""

GRANTS_GRAFANA = r"""
GRANT USAGE ON SCHEMA medicoes TO grafana_leitor;
GRANT SELECT ON ALL TABLES IN SCHEMA medicoes TO grafana_leitor;
"""


def upgrade() -> None:
    sql_em_bloco(SQL)
    for papel in PAPEIS:
        sql_em_bloco(GRANTS.format(papel=papel))
    sql_em_bloco(GRANTS_ROTINAS)
    sql_em_bloco(GRANTS_GRAFANA)


def downgrade() -> None:
    raise RuntimeError("O MCP-CLARO nao desfaz migracoes; restaure o backup se precisar voltar.")
