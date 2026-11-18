"""Fase 1: visoes de medicoes para o Grafana, continuidade do banco na partida e usuario da copia logica.

- Visoes de leitura no esquema ``medicoes``: recusas por versao dos dois lados (por aplicativo e por
  sistema), crachas recusados por motivo, leituras por sistema e recusas de chamada por motivo.
  O ``grafana_leitor`` fica so com SELECT nessas visoes; perde o SELECT direto nas tabelas.
- Continuidade: ``continuidade_controle()`` devolve so o identificador e a linha do tempo do
  PostgreSQL (SECURITY DEFINER), ``continuidade_marcar_restaurado()`` grava a marca "restaurado"
  do roteiro, e ``continuidade_banco`` passa a ser so de acrescentar.
- O despachante pode pausar aplicativos (``aplicativos.situacao``) quando a continuidade nao confere.
- ``mcp_claro_backup``: usuario da copia logica (pg_dump), so leitura (``pg_read_all_data``).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-18
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _util import sql_em_bloco

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# Visoes que o Grafana le. So contagens: nenhuma tem pessoa nem conteudo.
VISOES_GRAFANA = (
    "medicoes.v_recusas_versao",
    "medicoes.v_recusas_versao_aplicativo",
    "medicoes.v_recusas_versao_sistema",
    "medicoes.v_recusas_versao_dia",
    "medicoes.v_crachas_recusados",
    "medicoes.v_leituras",
    "medicoes.v_recusas_chamada",
)

SQL_VISOES = r"""
-- Recusas por versao (decisao 8), dos dois lados. medicoes.v_recusas_versao ja existe (0001):
-- minuto, lado, origem, versao_pedida, motivo, contagem.
CREATE VIEW medicoes.v_recusas_versao_aplicativo AS
  SELECT minuto, origem AS aplicativo, versao_pedida, motivo, contagem
  FROM public.recusas_versao WHERE lado = 'aplicativo';
COMMENT ON VIEW medicoes.v_recusas_versao_aplicativo IS
  'Recusas por versao do lado dos aplicativos, por minuto. aplicativo = client_id do cracha, ou sem-cracha.';

CREATE VIEW medicoes.v_recusas_versao_sistema AS
  SELECT minuto, origem AS sistema, versao_pedida, motivo, contagem
  FROM public.recusas_versao WHERE lado = 'sistema';
COMMENT ON VIEW medicoes.v_recusas_versao_sistema IS
  'Recusas por versao do lado dos sistemas conectados, por minuto. sistema = nome do cadastro.';

-- Dias completos (resumo das rotinas), para tendencias longas (13 meses ou mais).
CREATE VIEW medicoes.v_recusas_versao_dia AS
  SELECT dia, coalesce(rotulos ->> 'lado', 'sem_lado') AS lado, valor AS contagem
  FROM medicoes.dia WHERE nome = 'recusas_versao';
COMMENT ON VIEW medicoes.v_recusas_versao_dia IS 'Recusas por versao por dia completo (UTC), por lado.';

CREATE VIEW medicoes.v_crachas_recusados AS
  SELECT minuto, coalesce(rotulos ->> 'motivo', 'sem_motivo') AS motivo, valor AS contagem
  FROM medicoes.minuto WHERE nome = 'crachas_recusados';
COMMENT ON VIEW medicoes.v_crachas_recusados IS 'Crachas recusados na entrada, por minuto e motivo.';

CREATE VIEW medicoes.v_leituras AS
  SELECT minuto, coalesce(rotulos ->> 'sistema', 'sem_sistema') AS sistema, valor AS contagem
  FROM medicoes.minuto WHERE nome = 'leituras';
COMMENT ON VIEW medicoes.v_leituras IS 'Leituras feitas, por minuto e sistema.';

-- Recusas de chamada: onde a chamada parou (entrada, lista de ferramentas ou despachante) e o motivo.
CREATE VIEW medicoes.v_recusas_chamada AS
  SELECT minuto,
         CASE nome
           WHEN 'chamadas_recusadas' THEN 'entrada'
           WHEN 'pedidos_recusados' THEN 'entrada_pedido'
           ELSE 'despachante'
         END AS etapa,
         coalesce(rotulos ->> 'motivo', 'sem_motivo') AS motivo,
         valor AS contagem
  FROM medicoes.minuto
  WHERE nome IN ('chamadas_recusadas', 'pedidos_recusados', 'leituras_recusadas_despachante');
COMMENT ON VIEW medicoes.v_recusas_chamada IS
  'Recusas de chamada por minuto, etapa (entrada, entrada_pedido, despachante) e motivo.';
"""

SQL_GRAFANA = r"""
-- O grafana_leitor perde o SELECT direto nas tabelas e particoes de medicoes (dado na 0001).
DO $$
DECLARE t regclass;
BEGIN
  FOR t IN SELECT c.oid::regclass FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
           WHERE n.nspname = 'medicoes' AND c.relkind IN ('r', 'p')
  LOOP
    EXECUTE format('REVOKE ALL ON %s FROM grafana_leitor', t);
  END LOOP;
  -- So leitura, caminho curto para as visoes e consulta que nao prende o banco.
  EXECUTE format('ALTER ROLE grafana_leitor IN DATABASE %I SET default_transaction_read_only = on', current_database());
  EXECUTE format('ALTER ROLE grafana_leitor IN DATABASE %I SET search_path = medicoes', current_database());
  EXECUTE format('ALTER ROLE grafana_leitor IN DATABASE %I SET statement_timeout = %L', current_database(), '30s');
END $$;
GRANT USAGE ON SCHEMA medicoes TO grafana_leitor;
"""

SQL_CONTINUIDADE = r"""
-- Continuidade do banco: quem gravou a linha (despachante ou roteiro) e o que a partida concluiu.
ALTER TABLE continuidade_banco
  ADD COLUMN origem text NOT NULL DEFAULT 'despachante' CHECK (origem IN ('despachante', 'roteiro')),
  ADD COLUMN desfecho text CHECK (desfecho IN ('primeira_partida', 'confere', 'nao_confere', 'marca_restaurado')),
  ADD COLUMN ultimo_fechamento_vigia bigint,
  ADD COLUMN quem text;

-- So de acrescentar, como o extrato: a historia das partidas nao muda.
CREATE TRIGGER continuidade_sem_update_delete BEFORE UPDATE OR DELETE ON continuidade_banco
  FOR EACH ROW EXECUTE FUNCTION extrato_recusa_mudanca();
CREATE TRIGGER continuidade_sem_truncate BEFORE TRUNCATE ON continuidade_banco
  FOR EACH STATEMENT EXECUTE FUNCTION extrato_recusa_mudanca();

-- Identificador e linha do tempo do PostgreSQL, e nada mais.
-- A linha do tempo vem do ultimo checkpoint (pg_control_checkpoint). Logo depois de uma recuperacao
-- para um ponto no tempo, o checkpoint do fim da recuperacao pode ainda nao ter terminado: por isso
-- vale a maior entre ela e a linha do tempo em que o WAL esta sendo escrito agora.
CREATE FUNCTION continuidade_controle() RETURNS TABLE (system_identifier text, timeline_id integer)
LANGUAGE sql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $$
  SELECT s.system_identifier::text,
         greatest(
           c.timeline_id,
           CASE WHEN pg_is_in_recovery() THEN 0
                ELSE ('x' || substr(pg_walfile_name(pg_current_wal_lsn()), 1, 8))::bit(32)::integer
           END
         )
  FROM pg_control_system() s, pg_control_checkpoint() c
$$;
REVOKE ALL ON FUNCTION continuidade_controle() FROM PUBLIC;

-- Marca "restaurado", gravada pelo roteiro de restauracao (docs/restauracao.md). A proxima partida do
-- despachante fecha a porta de saida e pausa os aplicativos. Quem e o motivo ficam so nesta tabela.
CREATE FUNCTION continuidade_marcar_restaurado(p_quem text, p_motivo text) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  atual record;
  novo  bigint;
BEGIN
  IF coalesce(btrim(p_quem), '') = '' OR coalesce(btrim(p_motivo), '') = '' THEN
    RAISE EXCEPTION 'Diga quem marcou e o motivo da marca restaurado' USING ERRCODE = 'invalid_parameter_value';
  END IF;
  SELECT * INTO atual FROM continuidade_controle();
  INSERT INTO continuidade_banco (system_identifier, timeline_id, ultimo_fechamento, restaurado, motivo,
                                  origem, desfecho, quem)
  VALUES (atual.system_identifier, atual.timeline_id, (SELECT max(numero) FROM fechamentos), true,
          left(btrim(p_motivo), 500), 'roteiro', 'marca_restaurado', left(btrim(p_quem), 200))
  RETURNING id INTO novo;
  -- Mesma frase de mcp_claro.extrato.frases ("restauracao_marcada"); o teste confere.
  PERFORM extrato_registrar('roteiro', 'restauracao_marcada',
    'O roteiro de restauracao marcou o banco como restaurado; a porta de saida fecha na proxima partida.',
    NULL, NULL, NULL, NULL, NULL, jsonb_build_object('continuidade_id', novo), NULL);
  RETURN novo;
END $$;
REVOKE ALL ON FUNCTION continuidade_marcar_restaurado(text, text) FROM PUBLIC;
"""

GRANTS = r"""
-- Despachante: confere a continuidade na partida e pausa os aplicativos quando ela nao confere.
GRANT EXECUTE ON FUNCTION continuidade_controle() TO mcp_claro_despachante;
GRANT EXECUTE ON FUNCTION continuidade_marcar_restaurado(text, text) TO mcp_claro_despachante;
GRANT UPDATE (situacao, atualizado_em) ON aplicativos TO mcp_claro_despachante;
-- Telas do ADMIN (entrada): mostram a ultima conferencia e reabrem a saida por decisao registrada.
GRANT SELECT ON continuidade_banco TO mcp_claro_entrada;
"""

SQL_BACKUP = r"""
-- Usuario da copia logica: le tudo, nao escreve nada. O script de iniciacao do banco cria com
-- login e senha (segredo db_backup); aqui so garantimos que exista e tenha a leitura.
DO $$
DECLARE banco text;
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'mcp_claro_backup') THEN
    CREATE ROLE mcp_claro_backup NOLOGIN;
  END IF;
  -- Este banco e, quando existirem, os bancos do cofre, do login e do Grafana.
  FOR banco IN SELECT datname FROM pg_database
               WHERE datname = current_database() OR datname IN ('cofre', 'login', 'grafana')
  LOOP
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO mcp_claro_backup', banco);
  END LOOP;
END $$;
GRANT pg_read_all_data TO mcp_claro_backup;
"""


def upgrade() -> None:
    sql_em_bloco(SQL_VISOES)
    sql_em_bloco(SQL_GRAFANA)
    sql_em_bloco("".join(f"GRANT SELECT ON {visao} TO grafana_leitor;\n" for visao in VISOES_GRAFANA))
    sql_em_bloco(SQL_CONTINUIDADE)
    sql_em_bloco(GRANTS)
    sql_em_bloco(SQL_BACKUP)


def downgrade() -> None:
    raise RuntimeError("O MCP-CLARO nao desfaz migracoes; restaure o backup se precisar voltar.")
