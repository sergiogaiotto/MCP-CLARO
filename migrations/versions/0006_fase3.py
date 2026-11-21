"""Fase 3: servidores de terceiros (revisao do servidor, sondagem, texto de estranhos, tres sinais, quarentena).

Blocos, nesta ordem (cada um com comentario de abertura e "Fim do bloco"):

- REVISAO_SERVIDOR: ``revisoes_servidor``, so de acrescentar, e ``sistemas.revisao_servidor_id``;
- VERSAO: desfecho da sondagem e versoes anunciadas em ``sistemas``; visao ``medicoes.v_terceiros``. Sem visao
  propria de recusas de terceiros: o painel filtra ``motivo`` em ``medicoes.v_recusas_versao_sistema`` (critica B12);
- ESTRANHOS: marca "traz texto de fora" e definicao declarada nas ferramentas revisadas;
- TRES_SINAIS: tabela ``sinais`` e retrato do alerta na ficha;
- PEDIDOS: indice das linhas de pedido barrado, para a tela do ADMIN;
- CONFERENCIA: rodadas seguidas sem resposta ao mudou-parou (critica A5) e a hora da ultima soma;
- GUARDA_SISTEMAS: nova versao de ``sistemas_guarda`` (origem fixa, endereco e autorizacao de terceiro fixos,
  revisao vigente so com o servidor ativo e so na transacao que a grava, quarentena que so sai pela nova revisao do
  servidor);
- CHAVE_TERCEIRO: terceiro so com chave por pessoa ou compartilhada (D2);
- LIMITES: o despachante so suspende limites ativos (o UPDATE dele em limites vem da critica M2);
- CAMINHO: caminho de busca fixo nas funcoes de gatilho das migracoes anteriores;
- TEMPORARIA: o PUBLIC deixa de criar tabela temporaria no banco do gateway (bancos que ja existem);
- GRANTS, no fim.

Toda funcao desta migracao roda com ``search_path = pg_catalog, public, pg_temp``: um papel que crie uma tabela
temporaria com o nome de uma tabela do esquema (``sistemas``, ``pg_class``...) nao muda o que a guarda le.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-19
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _util import sql_em_bloco

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

SQL_REVISAO_SERVIDOR = r"""
-- Bloco REVISAO_SERVIDOR. A revisao do servidor inteiro, concluida pelo ADMIN e gravada pelo despachante depois de
-- ler o servidor de novo: a impressao de tudo o que ele declara, a impressao de cada ferramenta declarada e a lista
-- das ferramentas revisadas publicadas. So de acrescentar: cada conclusao e uma versao nova. A vitrine de um
-- terceiro so mostra as publicadas na revisao vigente (sistemas.revisao_servidor_id).
CREATE TABLE revisoes_servidor (
  id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  sistema_id   uuid        NOT NULL REFERENCES sistemas (id),
  versao       integer     NOT NULL CHECK (versao >= 1),
  impressao    text        NOT NULL CHECK (impressao ~ '^sha256:[0-9a-f]{64}$'),
  declaradas   jsonb       NOT NULL CHECK (jsonb_typeof(declaradas) = 'array'),
  publicadas   uuid[]      NOT NULL
               CHECK (cardinality(publicadas) <= 500 AND array_position(publicadas, NULL) IS NULL),
  fornecedor   text        NOT NULL CHECK (length(btrim(fornecedor)) BETWEEN 3 AND 200),
  revisado_por text        NOT NULL CHECK (btrim(revisado_por) <> ''),
  pessoa_id    uuid        REFERENCES pessoas (id),
  revisado_em  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (sistema_id, versao),
  -- Alvo da chave estrangeira de sistemas: a revisao vigente e sempre do proprio sistema.
  UNIQUE (id, sistema_id)
);

CREATE TRIGGER revisoes_servidor_sem_update_delete BEFORE UPDATE OR DELETE ON revisoes_servidor
  FOR EACH ROW EXECUTE FUNCTION extrato_recusa_mudanca();
CREATE TRIGGER revisoes_servidor_sem_truncate BEFORE TRUNCATE ON revisoes_servidor
  FOR EACH STATEMENT EXECUTE FUNCTION extrato_recusa_mudanca();

-- "Publica so o que foi revisto e nao mudou", no banco: o sistema e de terceiros; a versao e a ultima mais um;
-- cada declarada e {nome, impressao}, sem nome repetido; cada publicada e uma revisada ATIVA deste sistema, com a
-- mesma impressao da declarada de mesmo nome_original na leitura fresca. A hora da revisao e a do banco (a da
-- transacao da conclusao): a guarda de sistemas so torna vigente a revisao gravada na mesma transacao.
CREATE FUNCTION revisoes_servidor_nasce() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  origem_do_sistema text;
  ultima integer;
  fora uuid;
BEGIN
  NEW.revisado_em := now();
  SELECT origem INTO origem_do_sistema FROM sistemas WHERE id = NEW.sistema_id;
  IF origem_do_sistema IS DISTINCT FROM 'terceiro' THEN
    RAISE EXCEPTION 'A revisao do servidor so vale para servidor de terceiros' USING ERRCODE = 'check_violation';
  END IF;
  SELECT coalesce(max(versao), 0) INTO ultima FROM revisoes_servidor WHERE sistema_id = NEW.sistema_id;
  IF NEW.versao <> ultima + 1 THEN
    RAISE EXCEPTION 'A revisao do servidor deveria ser a versao %, nao %', ultima + 1, NEW.versao
      USING ERRCODE = 'check_violation';
  END IF;
  IF jsonb_typeof(NEW.declaradas) IS DISTINCT FROM 'array' THEN
    RAISE EXCEPTION 'As ferramentas declaradas da revisao do servidor sao uma lista' USING ERRCODE = 'check_violation';
  END IF;
  IF EXISTS (
    SELECT 1 FROM jsonb_array_elements(NEW.declaradas) d
    WHERE jsonb_typeof(d) IS DISTINCT FROM 'object'
       OR jsonb_typeof(d -> 'nome') IS DISTINCT FROM 'string'
       OR jsonb_typeof(d -> 'impressao') IS DISTINCT FROM 'string'
       OR btrim(d ->> 'nome') = ''
       OR NOT ((d ->> 'impressao') ~ '^sha256:[0-9a-f]{64}$')
  ) THEN
    RAISE EXCEPTION 'Cada ferramenta declarada e {nome, impressao}, com a impressao sha256'
      USING ERRCODE = 'check_violation';
  END IF;
  IF (SELECT count(*) <> count(DISTINCT d ->> 'nome') FROM jsonb_array_elements(NEW.declaradas) d) THEN
    RAISE EXCEPTION 'Ferramenta repetida nas declaradas da revisao do servidor' USING ERRCODE = 'check_violation';
  END IF;
  SELECT p INTO fora FROM unnest(NEW.publicadas) AS p
  WHERE NOT EXISTS (
    SELECT 1 FROM ferramentas_revisadas f
    JOIN jsonb_array_elements(NEW.declaradas) d ON d ->> 'nome' = f.nome_original
    WHERE f.id = p AND f.sistema_id = NEW.sistema_id AND f.situacao = 'ativa' AND f.impressao = d ->> 'impressao'
  )
  LIMIT 1;
  IF fora IS NOT NULL THEN
    RAISE EXCEPTION 'A revisao do servidor so publica ferramenta revisada, ativa, deste servidor e igual a declarada (%)',
      fora USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER revisoes_servidor_nasce BEFORE INSERT ON revisoes_servidor
  FOR EACH ROW EXECUTE FUNCTION revisoes_servidor_nasce();

-- A revisao vigente de um terceiro. A chave estrangeira composta prende a revisao ao proprio sistema, na hora do
-- cadastro e em toda mudanca; a guarda (bloco GUARDA_SISTEMAS) so aceita a mais recente.
ALTER TABLE sistemas
  ADD COLUMN revisao_servidor_id uuid,
  ADD CONSTRAINT sistemas_revisao_do_proprio_sistema FOREIGN KEY (revisao_servidor_id, id)
    REFERENCES revisoes_servidor (id, sistema_id),
  ADD CONSTRAINT sistemas_revisao_so_de_terceiro CHECK (revisao_servidor_id IS NULL OR origem = 'terceiro');
-- Fim do bloco REVISAO_SERVIDOR.
"""

SQL_VERSAO = r"""
-- Bloco VERSAO (sondagem). A sondagem separa "versao" de "desvio", "nao e MCP", "sem acesso" e "inalcancavel"; so
-- "versao" conta como versao antiga. As versoes anunciadas sao so as que o despachante validou (AAAA-MM-DD, ou
-- "outra" no lugar de texto livre), ate 10: nada do texto do servidor fica no cadastro.
-- recusas_versao nao muda: a recusa de um terceiro no cadastro vai com motivo 'cadastro_terceiro', e o painel e o
-- alerta filtram esse motivo em medicoes.v_recusas_versao_sistema (0004), sem visao propria (critica B12).
CREATE FUNCTION versoes_anunciadas_validas(p_versoes text[]) RETURNS boolean LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT coalesce(bool_and(v ~ '^([0-9]{4}-[0-9]{2}-[0-9]{2}|outra)$'), true) FROM unnest(p_versoes) AS v
$$;

ALTER TABLE sistemas
  ADD COLUMN sondagem_desfecho text
    CONSTRAINT sistemas_sondagem_desfecho_check
    CHECK (sondagem_desfecho IN ('aceito', 'versao', 'desvio', 'nao_mcp', 'sem_acesso', 'inalcancavel')),
  ADD COLUMN versoes_anunciadas text[]
    CONSTRAINT sistemas_versoes_anunciadas_check
    CHECK (versoes_anunciadas IS NULL OR (cardinality(versoes_anunciadas) <= 10
           AND array_position(versoes_anunciadas, NULL) IS NULL
           AND versoes_anunciadas_validas(versoes_anunciadas)));

-- Prova "cada terceiro revisado e registrado", sem nome de pessoa nem de sistema.
CREATE VIEW medicoes.v_terceiros AS
  SELECT situacao, (revisao_servidor_id IS NOT NULL) AS revisado, count(*) AS sistemas
  FROM public.sistemas WHERE origem = 'terceiro' GROUP BY 1, 2;
COMMENT ON VIEW medicoes.v_terceiros IS
  'Servidores de terceiros por situacao e por revisao do servidor vigente (revisado), so contagens.';
-- Fim do bloco VERSAO.
"""

SQL_ESTRANHOS = r"""
-- Bloco ESTRANHOS (ferramentas revisadas). texto_de_fora: a resposta da ferramenta e texto de estranhos e vai ao
-- aplicativo dentro da moldura. Num terceiro e sempre verdadeira, e a dica do fornecedor (etiquetas) nao e guardada:
-- as dicas da vitrine de um terceiro saem so da classe revisada. Numa ferramenta propria, o ADMIN marca na revisao.
-- definicao_declarada: o que o fornecedor declarou na hora da revisao, para a tela mostrar "antes e depois".
-- Linhas antigas ficam NULL ("nao guardado").
ALTER TABLE ferramentas_revisadas
  ADD COLUMN texto_de_fora boolean NOT NULL DEFAULT false,
  ADD COLUMN definicao_declarada jsonb
    CONSTRAINT ferramentas_revisadas_definicao_declarada_check
    CHECK (definicao_declarada IS NULL OR jsonb_typeof(definicao_declarada) = 'object');

CREATE FUNCTION ferramentas_revisadas_nasce() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF EXISTS (SELECT 1 FROM sistemas WHERE id = NEW.sistema_id AND origem = 'terceiro') THEN
    NEW.texto_de_fora := true;
    NEW.etiquetas := '{}'::jsonb;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER ferramentas_revisadas_nasce BEFORE INSERT ON ferramentas_revisadas
  FOR EACH ROW EXECUTE FUNCTION ferramentas_revisadas_nasce();

-- Revisadas de terceiros que ja existam ficam iguais as novas.
UPDATE ferramentas_revisadas f SET texto_de_fora = true, etiquetas = '{}'::jsonb
FROM sistemas s WHERE s.id = f.sistema_id AND s.origem = 'terceiro';
-- Fim do bloco ESTRANHOS.
"""

SQL_TRES_SINAIS = r"""
-- Bloco TRES_SINAIS. A entrada grava um sinal quando o aplicativo recebe texto de um sistema: "leu texto de fora"
-- (terceiro, ou propria marcada) e "viu dado privado" (ferramenta com campos pessoais). Com os dois na janela, pelo
-- par pessoa e aplicativo, toda acao vai para a ficha. So nomes do cadastro e hora: nenhum conteudo.
CREATE TABLE sinais (
  id            bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  pessoa_id     uuid        NOT NULL REFERENCES pessoas (id),
  aplicativo_id uuid        NOT NULL REFERENCES aplicativos (id),
  tipo          text        NOT NULL CHECK (tipo IN ('texto_de_fora', 'dado_privado')),
  sistema_id    uuid        NOT NULL REFERENCES sistemas (id),
  ferramenta_id uuid        NOT NULL REFERENCES ferramentas_revisadas (id),
  visto_em      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX sinais_janela ON sinais (pessoa_id, aplicativo_id, tipo, visto_em DESC);
CREATE INDEX sinais_visto  ON sinais (visto_em);

-- O sinal nasce com a hora do banco (so o dono das tabelas, nos testes, grava outra hora) e com a ferramenta do
-- proprio sistema.
CREATE FUNCTION sinais_nasce() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF current_user <> (SELECT relowner::regrole::text FROM pg_class WHERE oid = TG_RELID) THEN
    NEW.visto_em := now();
  END IF;
  IF NOT EXISTS (SELECT 1 FROM ferramentas_revisadas WHERE id = NEW.ferramenta_id AND sistema_id = NEW.sistema_id)
  THEN
    RAISE EXCEPTION 'O sinal precisa da ferramenta do proprio sistema' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER sinais_nasce BEFORE INSERT ON sinais FOR EACH ROW EXECUTE FUNCTION sinais_nasce();

-- Um sinal nao muda. So se apaga depois da maior janela possivel (THREE_SIGNALS_WINDOW_MINUTES vai de 5 a 10080
-- minutos, config/politica.py; regras/sinais.MAIOR_JANELA_MINUTOS; o teste confere): ninguem esconde um sinal
-- que ainda conta.
CREATE FUNCTION sinais_guarda() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    RAISE EXCEPTION 'Um sinal dos tres sinais nao muda' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF OLD.visto_em > now() - interval '10080 minutes' THEN
    RAISE EXCEPTION 'O sinal ainda cabe na maior janela dos tres sinais (7 dias) e nao se apaga'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN OLD;
END $$;
CREATE TRIGGER sinais_guarda BEFORE UPDATE OR DELETE ON sinais FOR EACH ROW EXECUTE FUNCTION sinais_guarda();
CREATE TRIGGER sinais_sem_truncate BEFORE TRUNCATE ON sinais
  FOR EACH STATEMENT EXECUTE FUNCTION extrato_recusa_mudanca();

-- Retrato do alerta na ficha: {"janela_minutos": 240, "vistos": [{"sinal", "ferramenta", "sistema", "visto_em"}]},
-- so nomes do cadastro e hora. NULL: a ficha nao nasceu com o alerta. Nasce com a ficha e nao muda.
ALTER TABLE fichas ADD COLUMN sinais jsonb
  CONSTRAINT fichas_sinais_check CHECK (sinais IS NULL OR jsonb_typeof(sinais) = 'object');

-- Mesmo corpo da 0005, com os sinais na linha que nao muda.
CREATE OR REPLACE FUNCTION fichas_guarda() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.decisao IS NOT NULL OR NEW.decidida_em IS NOT NULL OR NEW.decidida_por IS NOT NULL OR NEW.com_passkey
       OR NEW.confirmada_com IS NOT NULL OR NEW.segundos_ate_decidir IS NOT NULL THEN
      RAISE EXCEPTION 'Uma ficha nasce sem decisao' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;
  IF ROW(NEW.operacao_id, NEW.codigo_conferencia, NEW.risco, NEW.exige_passkey, NEW.sinais)
     IS DISTINCT FROM ROW(OLD.operacao_id, OLD.codigo_conferencia, OLD.risco, OLD.exige_passkey, OLD.sinais) THEN
    RAISE EXCEPTION 'A operacao, o codigo, o risco, a passkey e os sinais de uma ficha nao mudam'
      USING ERRCODE = 'check_violation';
  END IF;
  IF ROW(NEW.decisao, NEW.decidida_em, NEW.decidida_por, NEW.com_passkey, NEW.segundos_ate_decidir,
         NEW.confirmada_com)
     IS DISTINCT FROM ROW(OLD.decisao, OLD.decidida_em, OLD.decidida_por, OLD.com_passkey, OLD.segundos_ate_decidir,
         OLD.confirmada_com) THEN
    IF OLD.decisao IS NOT NULL THEN
      RAISE EXCEPTION 'A ficha ja foi %; a decisao nao muda', OLD.decisao USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.decisao IS NULL THEN
      RAISE EXCEPTION 'Os campos da decisao so mudam junto com a decisao' USING ERRCODE = 'check_violation';
    END IF;
    IF current_user = 'mcp_claro_rotinas' AND NEW.decisao <> 'vencida' THEN
      RAISE EXCEPTION 'As rotinas so vencem fichas' USING ERRCODE = 'insufficient_privilege';
    END IF;
    IF NEW.decisao = 'confirmada' AND (NEW.exige_passkey OR NEW.risco = 'alto')
       AND coalesce(NEW.confirmada_com, '') NOT IN ('passkey', 'login_refeito') THEN
      RAISE EXCEPTION 'Ficha de risco alto so confirma com a passkey ou com o login refeito'
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;
-- Fim do bloco TRES_SINAIS.
"""

SQL_PEDIDOS = r"""
-- Bloco PEDIDOS (tela do ADMIN). "Pedidos recusados deste sistema": contagem de 30 dias e as ultimas linhas de
-- pedido barrado, por sistema, sem passar pelo extrato inteiro. O indice do pai vale para as particoes de agora e
-- para as criadas depois.
CREATE INDEX extrato_pedido_barrado_idx ON extrato (sistema_id, criado_em DESC)
  WHERE tipo IN ('leitura_sistema_pediu_dados', 'programa_leitura_sistema_pediu_dados', 'acao_sistema_pediu_dados');
-- Fim do bloco PEDIDOS.
"""

SQL_CONFERENCIA = r"""
-- Bloco CONFERENCIA (critica A5). Rodadas seguidas em que o servidor nao respondeu a conferencia do mudou-parou.
-- As rotinas somam e zeram; com 2 seguidas, um terceiro vai a quarentena ("nao respondeu a conferencia"). A guarda
-- de sistemas zera a contagem quando a revisao vigente muda (nova conclusao, quarentena, endereco ou chave).
ALTER TABLE sistemas ADD COLUMN conferencias_sem_resposta integer NOT NULL DEFAULT 0
  CONSTRAINT sistemas_conferencias_sem_resposta_check CHECK (conferencias_sem_resposta >= 0);
-- Hora (do banco) da ultima rodada somada na contagem de agora; nula sem soma. Com duas copias das rotinas no ar,
-- cada uma no seu intervalo, as rotinas so somam outra rodada quando a ultima soma tem mais de metade do intervalo
-- do mudou-parou: duas rodadas a segundos uma da outra contam uma vez so. As rotinas a apagam junto com a contagem
-- quando o servidor responde; a guarda de sistemas, quando a revisao vigente muda.
ALTER TABLE sistemas ADD COLUMN conferencia_sem_resposta_em timestamptz;
-- Fim do bloco CONFERENCIA.
"""

SQL_GUARDA_SISTEMAS = r"""
-- Bloco GUARDA_SISTEMAS. Uma so versao nova da guarda da 0002, com as regras de todas as entregas, nesta ordem:
-- 1. origem nao muda, nem pelo dono das tabelas (D8);
-- 2. num terceiro, endereco e autorizacao so mudam de vazio para um valor (critica M1): mudar o destino mandaria as
--    chaves ja guardadas para outro lugar; para mudar, cadastre o servidor de novo;
-- 3. endereco ou tipo de chave mudou: apaga a versao, a sondagem e a revisao vigente; a situacao vira 'pendente',
--    salvo em quarentena (continua em quarentena, ou vai a 'desativado' se pedido);
-- 4. a revisao vigente so vale com o servidor 'ativo': sair de 'ativo' (quarentena, recusado, pendente,
--    desativado) apaga revisao_servidor_id. Fora de 'ativo' o mudou-parou nao confere o servidor; por isso a volta
--    pede uma conclusao nova, que le o servidor de novo (D27, D29);
-- 5. revisao_servidor_id novo so aponta para a revisao mais recente deste sistema, gravada na mesma transacao: a
--    conclusao grava a revisao e a torna vigente de uma vez, e uma revisao que deixou de valer nao volta
--    (revisado_em e sempre now(), a hora da transacao que a gravou). E so vale para um servidor que estava 'ativo'
--    ou em quarentena, fica 'ativo' e fala a versao 2026-07-28: um servidor recusado, pendente ou desativado nao
--    se ativa pela revisao, e uma revisao nova num servidor que nao fica 'ativo' e recusada, em vez de sumir;
-- 6. quarentena so sai para 'ativo', pelo despachante, com a versao 2026-07-28 e revisao nova no mesmo UPDATE (a
--    conclusao da revisao do servidor), ou para 'desativado', pela entrada ou pelo dono (D27);
-- 7. as regras da 0002: entrada e rotinas nao poem 'ativo'; a entrada so poe 'desativado' ou 'pendente';
-- 8. a contagem de rodadas sem resposta zera quando a revisao vigente muda, e a hora da ultima soma tambem.
-- Mudancas feitas pela guarda nao pedem GRANT de coluna (o mesmo padrao da 0002 com versao_mcp).
CREATE OR REPLACE FUNCTION sistemas_guarda() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  dono text := (SELECT relowner::regrole::text FROM pg_class WHERE oid = TG_RELID);
BEGIN
  IF NEW.origem IS DISTINCT FROM OLD.origem THEN
    RAISE EXCEPTION 'A origem de um sistema nao muda; para mudar, cadastre o servidor de novo'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF OLD.origem = 'terceiro'
     AND (NEW.endereco IS DISTINCT FROM OLD.endereco
          OR (NEW.autorizacao IS DISTINCT FROM OLD.autorizacao
              AND OLD.autorizacao IS NOT NULL AND jsonb_typeof(OLD.autorizacao) <> 'null')) THEN
    RAISE EXCEPTION USING ERRCODE = 'insufficient_privilege', MESSAGE =
      'Num servidor de terceiros, o endereco e a autorizacao nao mudam depois de preenchidos; '
      || 'para mudar, cadastre o servidor de novo';
  END IF;
  IF NEW.endereco IS DISTINCT FROM OLD.endereco OR NEW.tipo_chave IS DISTINCT FROM OLD.tipo_chave THEN
    NEW.versao_mcp := NULL;
    NEW.sondagem := NULL;
    NEW.sondagem_desfecho := NULL;
    NEW.versoes_anunciadas := NULL;
    NEW.revisao_servidor_id := NULL;
    IF OLD.situacao = 'quarentena' THEN
      NEW.situacao := CASE WHEN NEW.situacao = 'desativado' THEN 'desativado' ELSE 'quarentena' END;
    ELSE
      NEW.situacao := 'pendente';
    END IF;
  END IF;
  IF NEW.revisao_servidor_id IS NOT NULL AND NEW.revisao_servidor_id IS DISTINCT FROM OLD.revisao_servidor_id
     AND OLD.situacao IS DISTINCT FROM 'quarentena'
     AND NOT (OLD.situacao = 'ativo' AND NEW.situacao = 'ativo'
              AND NEW.versao_mcp IS NOT DISTINCT FROM '2026-07-28') THEN
    RAISE EXCEPTION 'A revisao do servidor so vale para servidor ativo ou em quarentena, que fica ativo e fala a versao 2026-07-28'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NEW.situacao <> 'ativo' THEN
    NEW.revisao_servidor_id := NULL;
  END IF;
  IF NEW.revisao_servidor_id IS NOT NULL AND NEW.revisao_servidor_id IS DISTINCT FROM OLD.revisao_servidor_id
     AND NOT EXISTS (SELECT 1 FROM revisoes_servidor r
                     WHERE r.id = NEW.revisao_servidor_id AND r.sistema_id = NEW.id AND r.revisado_em = now()
                       AND r.versao = (SELECT max(versao) FROM revisoes_servidor WHERE sistema_id = NEW.id)) THEN
    RAISE EXCEPTION 'A revisao vigente de um servidor e sempre a mais recente dele, gravada na mesma transacao'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF OLD.situacao = 'quarentena' AND NEW.situacao IS DISTINCT FROM 'quarentena'
     AND NOT (NEW.situacao = 'ativo' AND current_user = 'mcp_claro_despachante'
              AND NEW.versao_mcp IS NOT DISTINCT FROM '2026-07-28'
              AND NEW.revisao_servidor_id IS NOT NULL
              AND NEW.revisao_servidor_id IS DISTINCT FROM OLD.revisao_servidor_id)
     AND NOT (NEW.situacao = 'desativado' AND current_user IN ('mcp_claro_entrada', dono)) THEN
    RAISE EXCEPTION 'Servidor em quarentena so sai pela nova revisao do ADMIN'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF current_user IN ('mcp_claro_entrada', 'mcp_claro_rotinas')
     AND NEW.situacao = 'ativo' AND OLD.situacao IS DISTINCT FROM 'ativo' THEN
    RAISE EXCEPTION 'So a sondagem de versao do despachante ativa um sistema' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF current_user = 'mcp_claro_entrada' AND NEW.situacao NOT IN ('desativado', 'pendente')
     AND NEW.situacao IS DISTINCT FROM OLD.situacao THEN
    RAISE EXCEPTION 'A entrada so desativa um sistema' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NEW.revisao_servidor_id IS DISTINCT FROM OLD.revisao_servidor_id THEN
    NEW.conferencias_sem_resposta := 0;
    NEW.conferencia_sem_resposta_em := NULL;
  END IF;
  RETURN NEW;
END $$;
-- Fim do bloco GUARDA_SISTEMAS.
"""

SQL_CHAVE_TERCEIRO = r"""
-- Bloco CHAVE_TERCEIRO (D2). Um servidor de terceiros usa chave por pessoa ou chave compartilhada (que so le);
-- chave de programa fica para depois. Antes da regra, um terceiro antigo com chave de programa para a migracao com
-- uma frase legivel: troque o tipo de chave dele antes de migrar.
DO $$
DECLARE nomes text;
BEGIN
  SELECT string_agg(nome, ', ' ORDER BY nome) INTO nomes FROM sistemas
  WHERE origem = 'terceiro' AND tipo_chave = 'programa';
  IF nomes IS NOT NULL THEN
    RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = format(
      'Servidores de terceiros com chave de programa: %s. Na Fase 3 um terceiro usa chave por pessoa %s', nomes,
      'ou compartilhada; troque o tipo de chave deles antes de migrar.');
  END IF;
END $$;
ALTER TABLE sistemas ADD CONSTRAINT sistemas_chave_de_terceiro
  CHECK (origem = 'proprio' OR tipo_chave IN ('por_pessoa', 'compartilhada'));
-- Fim do bloco CHAVE_TERCEIRO.
"""

SQL_LIMITES = r"""
-- Bloco LIMITES (critica M2). A quarentena pela sondagem (D29) roda no despachante e suspende os limites ativos do
-- sistema: por isso ele ganha UPDATE (situacao, motivo) ON limites (bloco GRANTS). Com essa guarda, ele so faz
-- isso: de 'ativo' para 'suspenso'. Nao reativa, nao encerra e nao reescreve o motivo de um limite. A entrada e as
-- rotinas seguem como na 0005.
CREATE FUNCTION limites_guarda() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF current_user = 'mcp_claro_despachante'
     AND ROW(NEW.situacao, NEW.motivo) IS DISTINCT FROM ROW(OLD.situacao, OLD.motivo)
     AND NOT (OLD.situacao = 'ativo' AND NEW.situacao = 'suspenso') THEN
    RAISE EXCEPTION 'O despachante so suspende um limite ativo' USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER limites_guarda BEFORE UPDATE ON limites FOR EACH ROW EXECUTE FUNCTION limites_guarda();
-- Fim do bloco LIMITES.
"""

SQL_CAMINHO = r"""
-- Bloco CAMINHO. As funcoes de gatilho das migracoes 0001 a 0005 tambem passam a rodar com caminho de busca fixo,
-- como as desta. Sem isso, um papel que crie uma tabela temporaria transicoes_saida (ou pg_class) muda o que a
-- guarda da porta de saida le: a entrada levaria uma acao de 'aguardando_ficha' direto a 'enviada'. So o caminho de
-- busca muda; o corpo de cada funcao e o mesmo. O teste confere que toda funcao de gatilho do esquema tem o caminho.
ALTER FUNCTION extrato_recusa_mudanca() SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION ferramentas_guarda() SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION fila_saida_guarda() SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION fila_saida_nasce() SET search_path = pg_catalog, public, pg_temp;
ALTER FUNCTION fila_saida_avisa() SET search_path = pg_catalog, public, pg_temp;
-- Fim do bloco CAMINHO.
"""

SQL_TEMPORARIA = r"""
-- Bloco TEMPORARIA. O PUBLIC deixa de criar tabela temporaria neste banco: nenhum papel do app cria uma sombra em
-- pg_temp com o nome de uma tabela do esquema. E a defesa de cima; a de baixo e o caminho de busca fixo (bloco
-- CAMINHO), que vale mesmo se um papel ganhar o TEMPORARY por outro caminho. O docker/postgres/iniciar/10-papeis.sh
-- faz o mesmo, mas so na primeira partida do banco (volume vazio): esta linha alcanca os bancos que ja existem. O
-- CONNECT do PUBLIC fica como esta. O MCP-CLARO nao desfaz migracoes (o downgrade levanta erro, como nas outras);
-- quem voltar um banco a mao devolve o padrao do PostgreSQL com GRANT TEMPORARY ON DATABASE <banco> TO PUBLIC.
DO $$
BEGIN
  EXECUTE format('REVOKE TEMPORARY ON DATABASE %I FROM PUBLIC', current_database());
END $$;
-- Fim do bloco TEMPORARIA.
"""

GRANTS = r"""
-- Bloco GRANTS (ultimo).
-- Revisao do servidor: todos leem (vitrine da entrada, ordem do despachante, mudou-parou das rotinas); so o
-- despachante grava, na conclusao. Ninguem muda nem apaga (gatilhos).
GRANT SELECT ON revisoes_servidor TO mcp_claro_entrada, mcp_claro_despachante, mcp_claro_rotinas;
GRANT INSERT ON revisoes_servidor TO mcp_claro_despachante;
-- Conclusao da revisao e sondagem (despachante). A entrada e as rotinas nao tem UPDATE em revisao_servidor_id: so
-- a guarda o apaga (quarentena, endereco, tipo de chave).
GRANT UPDATE (revisao_servidor_id, sondagem_desfecho, versoes_anunciadas) ON sistemas TO mcp_claro_despachante;
-- Rodadas sem resposta ao mudou-parou (A5): so as rotinas contam, com a hora da ultima soma.
GRANT UPDATE (conferencias_sem_resposta, conferencia_sem_resposta_em) ON sistemas TO mcp_claro_rotinas;
-- Tres sinais: a entrada grava e le (leitura, acao, alerta no pedido); as rotinas so apagam os velhos (o DELETE
-- com WHERE le id e visto_em). O despachante nao tem acesso. A identidade de sinais nao pede GRANT de sequencia.
GRANT SELECT, INSERT ON sinais TO mcp_claro_entrada;
GRANT SELECT (id, visto_em), DELETE ON sinais TO mcp_claro_rotinas;
-- Quarentena pela sondagem (D29, despachante) suspende os limites do sistema (critica M2). As rotinas ja tem o
-- mesmo UPDATE (0005), e o despachante ja le limites (0005). A guarda do bloco LIMITES deixa o despachante so
-- suspender (de 'ativo' para 'suspenso').
GRANT UPDATE (situacao, motivo) ON limites TO mcp_claro_despachante;
-- Grafana: so a visao nova (a de recusas e a v_recusas_versao_sistema da 0004, filtrada por motivo).
GRANT SELECT ON medicoes.v_terceiros TO grafana_leitor;
-- Sem GRANT novo, de proposito:
-- * ferramentas_revisadas.texto_de_fora e definicao_declarada, e fichas.sinais: o INSERT da entrada e da tabela
--   inteira (0002, 0005) e cobre as colunas novas; ninguem tem UPDATE nelas. Mudar a marca e revisar de novo.
-- * origem segue sem UPDATE para ninguem (e a guarda recusa ate o dono).
-- * Quarentena: as rotinas ja tem UPDATE (situacao, motivo, atualizado_em) ON sistemas (0002), UPDATE (estado,
--   mudou_por) ON fila_saida, UPDATE (desfecho) ON operacoes e UPDATE (situacao, motivo) ON limites (0005); o
--   despachante tem UPDATE (situacao, motivo, atualizado_em) ON sistemas (0002), UPDATE (estado, mudou_por,
--   enviada_em) ON fila_saida e UPDATE (resultado, desfecho) ON operacoes (0005). SELECT ... FOR UPDATE OF da
--   fila vale com esses UPDATE de coluna. As travas consultivas nao pedem GRANT.
-- * A sala de visitas nao tem usuario no banco.
-- Fim do bloco GRANTS.
"""


def upgrade() -> None:
    sql_em_bloco(SQL_REVISAO_SERVIDOR)
    sql_em_bloco(SQL_VERSAO)
    sql_em_bloco(SQL_ESTRANHOS)
    sql_em_bloco(SQL_TRES_SINAIS)
    sql_em_bloco(SQL_PEDIDOS)
    sql_em_bloco(SQL_CONFERENCIA)
    sql_em_bloco(SQL_GUARDA_SISTEMAS)
    sql_em_bloco(SQL_CHAVE_TERCEIRO)
    sql_em_bloco(SQL_LIMITES)
    sql_em_bloco(SQL_CAMINHO)
    sql_em_bloco(SQL_TEMPORARIA)
    sql_em_bloco(GRANTS)


def downgrade() -> None:
    raise RuntimeError("O MCP-CLARO nao desfaz migracoes; restaure o backup se precisar voltar.")
