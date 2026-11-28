"""Fase 4: selos de nivel 2 e 3 (contrato, declaracao, testes, selo vigente, envio com numero, aviso de bloqueio).

Blocos, nesta ordem (cada um com comentario de abertura e "Fim do bloco"):

- CONTRATO: as listas do contrato ``selo/1`` no banco (vocabulario, casos, esquemas, forma essencial dos esquemas
  com orcamento de nos, endereco normalizado, nome do alvo bem escrito, passagens e criterio de aprovacao), iguais
  as de ``mcp_claro.selo.contrato`` e ``mcp_claro.selo.declaracao`` (o teste confere);
- DECLARACOES: ``declaracoes_selo`` e a chave de teste (``chaves_cifradas.tipo = 'teste_selo'``, que nunca vira a
  chave de producao, criticas A4 e M11);
- TESTES: ``pedidos_teste_selo`` e ``testes_selo``, com a guarda que so deixa nascer teste aprovado com todos os
  casos obrigatorios, num sistema igual ao de agora;
- VIGENTE: ``selos_retirados``, ``selo_vigente()`` (calculado na leitura, D8), a visao ``selos_vigentes``, a marca
  de ferramenta sumida em ``ferramentas_vistas`` (critica M1), a conferencia das ferramentas do contrato calculada ao
  gravar a definicao (``ferramentas_vistas.contrato_confere``) e ``sistemas.selo`` preso em 1;
- RESERVADAS: os nomes das ferramentas do contrato nao sao revisados;
- ENVIO: ``operacoes.repete_operacao_id`` e ``envios_selo`` (o numero e do banco; a falta do envio original nunca
  e erro, critica A1);
- AVISOS_BLOQUEIO: ``avisos_bloqueio``, derivados pelo despachante depois do bloqueio, fora da transacao dele
  (critica A1, ajustes D22), e ``avisos_bloqueio_derivados``. Vem antes de INFORMACOES, que aponta para ele (no
  desenho a ordem era a inversa, e a chave estrangeira de ``informacoes_sistema.aviso_id`` nao teria alvo);
- INFORMACOES: ``informacoes_sistema`` e a contradicao pela coluna "conta como" (critica M2);
- FICHAS: ``fichas.selo`` com a guarda do banco (critica M3);
- MEDICOES: ``medicoes.v_selos`` (por uma funcao propria do Grafana, critica M10), ``medicoes.v_consultas_sistema``
  e a coluna nova de ``medicoes.v_fila_agora``;
- GRANTS, no fim.

Toda funcao desta migracao roda com ``search_path = pg_catalog, public, pg_temp``. As tabelas so de acrescentar
usam ``extrato_recusa_mudanca()`` em UPDATE e DELETE (por linha) e em TRUNCATE (por comando). O MCP-CLARO nao
desfaz migracoes: o downgrade levanta erro, como nas outras.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-22
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from _util import sql_em_bloco

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

SQL_CONTRATO = r"""
-- Bloco CONTRATO. As listas do contrato do selo (selo/1) no banco. Sao as mesmas de mcp_claro.selo.contrato; o
-- teste test_migracao_0007 confere uma por uma. Funcoes puras (IMMUTABLE), sem ler tabela: os CHECK e os gatilhos
-- dos blocos seguintes usam estas, e nao listas soltas.
CREATE FUNCTION selo_vocabulario(p_lista text) RETURNS text[] LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT CASE p_lista
    WHEN 'estados' THEN ARRAY['concluida', 'recusada', 'em_andamento', 'nao_recebida', 'parcial', 'desfeita',
                              'parada', 'desconhecido']
    WHEN 'estados_do_recibo' THEN ARRAY['concluida', 'recusada', 'em_andamento', 'nao_recebida', 'parcial', 'parada',
                                        'desconhecido']
    WHEN 'estados_em_aberto' THEN ARRAY['em_andamento', 'desconhecido']
    WHEN 'estados_que_queimam' THEN ARRAY['nao_recebida', 'recusada', 'parada', 'desfeita']
    WHEN 'eventos' THEN ARRAY['concluida', 'entregue', 'devolvida', 'desfeita', 'parada']
    WHEN 'origens' THEN ARRAY['recibo', 'consulta', 'aviso']
    WHEN 'desfechos' THEN ARRAY['respondeu', 'sem_resposta', 'resposta_invalida', 'sem_chave', 'sala_fora',
                                'sistema_fora']
    WHEN 'desfechos_que_gastam_tentativa' THEN ARRAY['respondeu', 'sem_resposta', 'resposta_invalida']
    WHEN 'desfechos_de_falha_do_sistema' THEN ARRAY['sem_resposta', 'resposta_invalida']
    WHEN 'resultados' THEN ARRAY['cumpriu', 'falhou', 'nao_se_aplica', 'nao_testado', 'nao_observado']
    WHEN 'razoes' THEN ARRAY['sem_condicao_declarada', 'condicao_nao_forcada', 'sem_resposta', 'tentativas_excluidas',
                             'interposicao', 'fora_do_alvo', 'teto', 'sem_segundo_efeito', 'poucos_pares',
                             'declarado']
    WHEN 'motivos_do_selo' THEN ARRAY['nunca_testado', 'reprovado', 'vencido', 'retirado', 'fora_de_ativo',
                                      'endereco_mudou', 'tipo_chave_mudou', 'revisao_nova', 'ferramenta_nova',
                                      'contrato_sumiu', 'vigente']
    WHEN 'motivos_do_retrato' THEN ARRAY['nunca_testado', 'reprovado', 'vencido', 'retirado', 'fora_de_ativo',
                                         'endereco_mudou', 'tipo_chave_mudou', 'revisao_nova', 'ferramenta_nova',
                                         'contrato_sumiu', 'vigente', 'fase_desligada']
    WHEN 'motivos_de_retirada' THEN ARRAY['admin', 'se_contradisse', 'nao_respondeu_consultas']
    WHEN 'estados_do_pedido' THEN ARRAY['pedido', 'rodando', 'terminado', 'interrompido']
    WHEN 'motivos_de_interrupcao' THEN ARRAY['despachante_caiu', 'sistema_mudou', 'prazo', 'porta_fechada',
                                             'cofre_trancado', 'sem_chave_de_teste', 'teto_de_chamadas',
                                             'erro_interno']
    WHEN 'estados_do_aviso' THEN ARRAY['pendente', 'respondido', 'sem_resposta', 'sistema_fora', 'bloqueio_encerrado']
    WHEN 'modos' THEN ARRAY['ambiente_de_teste', 'ferramentas_de_teste']
    WHEN 'alvos' THEN ARRAY['teste', 'producao']
    WHEN 'ferramentas' THEN ARRAY['selo_alvo', 'selo_consultar', 'selo_bloqueio']
  END
$$;

-- Os casos da bateria. nao_se_aplica_ate: o maior nivel de teste em que nao_se_aplica aprova o caso (0: nunca).
-- maximo: o melhor resultado que o metodo permite (nao_observado nos casos vistos so de fora, critica M5).
CREATE FUNCTION selo_casos(p_contrato text DEFAULT 'selo/1')
RETURNS TABLE (caso text, requisito text, nivel smallint, escopo text, obrigatorio boolean,
               nao_se_aplica_ate smallint, maximo text)
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT v.c, v.r, v.n::smallint, v.e, v.o, v.a::smallint, v.m
  FROM (VALUES
    ('C-0.1', 'SELO-0.1', 2, 'sistema', true, 0, 'cumpriu'),
    ('C-0.2', 'SELO-0.1', 2, 'sistema', true, 0, 'cumpriu'),
    ('C-0.3', 'SELO-2.4', 2, 'sistema', true, 0, 'cumpriu'),
    ('C-0.4', 'SELO-0.3', 2, 'sistema', true, 0, 'cumpriu'),
    ('C-2.1', 'SELO-2.1', 2, 'ferramenta', true, 0, 'cumpriu'),
    ('C-2.2a', 'SELO-2.2', 2, 'ferramenta', true, 0, 'cumpriu'),
    ('C-2.2b', 'SELO-2.2', 2, 'ferramenta', true, 3, 'cumpriu'),
    ('C-2.2c', 'SELO-2.2', 2, 'ferramenta', true, 0, 'cumpriu'),
    ('C-2.2d', 'SELO-2.2', 2, 'ferramenta', false, 0, 'nao_observado'),
    ('C-2.3', 'SELO-2.3', 2, 'ferramenta', true, 3, 'nao_observado'),
    ('C-2.4a', 'SELO-2.4', 2, 'ferramenta', true, 0, 'cumpriu'),
    ('C-2.4b', 'SELO-2.4', 2, 'ferramenta', true, 2, 'cumpriu'),
    ('C-2.5', 'SELO-2.5', 2, 'ferramenta', true, 0, 'cumpriu'),
    ('C-2.6a', 'SELO-2.4', 2, 'ferramenta', true, 0, 'cumpriu'),
    ('C-2.6b', 'SELO-2.4', 2, 'ferramenta', true, 3, 'cumpriu'),
    ('C-2.7', 'SELO-2.7', 2, 'ferramenta', true, 3, 'nao_observado'),
    ('C-3.1a', 'SELO-3.1', 3, 'ferramenta', true, 0, 'cumpriu'),
    ('C-3.1b', 'SELO-3.1', 3, 'ferramenta', true, 0, 'cumpriu'),
    ('C-3.2', 'SELO-3.2', 3, 'ferramenta', true, 0, 'cumpriu'),
    ('C-3.3', 'SELO-3.3', 3, 'ferramenta', true, 3, 'cumpriu'),
    ('C-3.4', 'SELO-3.4', 3, 'ferramenta', true, 3, 'cumpriu'),
    ('C-3.5', 'SELO-3.5', 3, 'ferramenta', true, 0, 'cumpriu'),
    ('C-3.6a', 'SELO-3.6', 3, 'ferramenta', true, 0, 'cumpriu'),
    ('C-3.6b', 'SELO-3.6', 3, 'ferramenta', true, 3, 'cumpriu')
  ) AS v(c, r, n, e, o, a, m)
  WHERE p_contrato = 'selo/1'
$$;

-- O que nunca e testado e sempre aparece no relatorio (sem IDs do rascunho, critica B6), em ordem de item.
CREATE FUNCTION selo_nao_testados() RETURNS jsonb LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT '[{"item": "guarda_dos_numeros", "razao": "declarado"},
           {"item": "interposicao_e_reinicio", "razao": "interposicao"}]'::jsonb
$$;

-- Os esquemas publicados das ferramentas do contrato: {"inputSchema", "outputSchema"}; NULL para outro nome.
CREATE FUNCTION selo_esquemas(p_nome text) RETURNS jsonb LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, public, pg_temp AS $esquemas$
  SELECT CASE p_nome
    WHEN 'selo_alvo' THEN '{"inputSchema": {"type": "object", "additionalProperties": false, "properties": {}}, "outputSchema": {"type": "object", "required": ["contrato", "alvo"], "properties": {"contrato": {"type": "string", "const": "selo/1"}, "alvo": {"type": "string", "enum": ["teste", "producao"]}}}}'::jsonb
    WHEN 'selo_consultar' THEN '{"inputSchema": {"type": "object", "additionalProperties": false, "required": ["operacao"], "properties": {"operacao": {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}}}, "outputSchema": {"type": "object", "required": ["contrato", "operacao", "estado"], "properties": {"contrato": {"type": "string", "const": "selo/1"}, "operacao": {"type": "string"}, "estado": {"type": "string", "enum": ["concluida", "recusada", "em_andamento", "nao_recebida", "parcial", "desfeita", "parada", "desconhecido"]}, "depois": {"type": "array", "maxItems": 20, "items": {"type": "object", "additionalProperties": false, "required": ["evento", "em"], "properties": {"evento": {"type": "string", "enum": ["concluida", "entregue", "devolvida", "desfeita", "parada"]}, "em": {"type": "string", "format": "date-time"}}}}}}}'::jsonb
    WHEN 'selo_bloqueio' THEN '{"inputSchema": {"type": "object", "additionalProperties": false, "required": ["bloqueio", "operacoes"], "properties": {"bloqueio": {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}, "operacoes": {"type": "array", "minItems": 1, "maxItems": 100, "items": {"type": "string", "pattern": "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"}}}}, "outputSchema": {"type": "object", "required": ["contrato", "bloqueio", "operacoes"], "properties": {"contrato": {"type": "string", "const": "selo/1"}, "bloqueio": {"type": "string"}, "operacoes": {"type": "array", "maxItems": 100, "items": {"type": "object", "additionalProperties": false, "required": ["operacao", "estado"], "properties": {"operacao": {"type": "string"}, "estado": {"type": "string", "enum": ["concluida", "recusada", "em_andamento", "nao_recebida", "parcial", "desfeita", "parada", "desconhecido"]}}}}}}}'::jsonb
  END
$esquemas$;

-- Forma essencial de um esquema (critica B4): sem anotacoes (title, description, $schema, exemplos, padroes) e sem
-- os limites que o MCP-CLARO confere sozinho na resposta; referencias locais (#/$defs/X, #/definitions/X)
-- resolvidas; required e enum de textos em ordem (por byte); type ao lado de const ou enum fora. O que sobra conta.
-- Uma referencia de fora, sem alvo, em ciclo ou fundo demais vira um texto que nunca confere com o contrato.
--
-- Orcamento de nos (mesma regra de contrato.TETO_NOS_DO_ESQUEMA, o teste confere): cada parte visitada gasta o seu
-- peso inteiro (selo_esquema_peso, com tudo o que esta dentro dela) e cada $ref seguido gasta de novo o peso inteiro
-- da definicao achada. Passou de selo_esquema_teto('nos'), a forma e o texto 'grande_demais', que nunca confere.
-- Sem o orcamento, 129 bytes ({"$defs": {"a": {"properties": {"x": a, "y": a}}}}, ou uma cadeia sem ciclo de 32
-- definicoes) davam uma forma de 2^33 partes, e selo_vigente() (a vitrine, a leitura e a porta de saida) nao
-- terminava. Cada copia que a funcao faz (a parte recebida, a definicao, a juncao com o $ref, os filhos, a forma de
-- saida) e de algo que o orcamento ja pagou: o trabalho de uma chamada fica em algumas vezes o teto, qualquer que seja
-- o tamanho do esquema guardado. Os numeros do orcamento (2000 nos, 64 bytes por no, 32 niveis) sao os de
-- selo_esquema_teto e os de contrato.py; o teste confere os tres.
CREATE FUNCTION selo_esquema_teto(p_nome text) RETURNS integer LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT CASE p_nome WHEN 'nos' THEN 2000 WHEN 'bytes_por_no' THEN 64 WHEN 'fundo' THEN 32 END
$$;

-- O peso de p_valor na conta do orcamento (contrato.peso_do_json): 1 por valor, em qualquer nivel, e 1 por nome de
-- campo; cada texto (valor ou nome) mais 1 a cada 64 bytes, e cada numero mais 1 a cada 64 letras de como o banco o
-- escreve. Para assim que passa de p_resta (devolve um numero acima dele), sem recursao. Antes de abrir uma lista ou
-- um objeto, o corte pelo tamanho guardado: cada no pesa ao menos 1 e ocupa no maximo 67 bytes no jsonb (um texto de
-- 63 bytes com a entrada de 4; um numero, um objeto e uma lista, menos), mais 8 na raiz; um valor maior que 68 bytes
-- por no que resta passa do que resta, e nao precisa ser aberto (o teste confere a conta contra pg_column_size).
CREATE FUNCTION selo_esquema_peso(p_valor jsonb, p_resta integer) RETURNS integer
LANGUAGE plpgsql IMMUTABLE STRICT SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  pilha jsonb[] := ARRAY[p_valor];
  topo integer := 1;
  total integer := 0;
  atual jsonb;
  filho record;
BEGIN
  WHILE topo > 0 LOOP
    atual := pilha[topo];
    pilha[topo] := NULL;
    topo := topo - 1;
    CASE jsonb_typeof(atual)
      WHEN 'string' THEN
        total := total + 1 + octet_length(atual #>> '{}') / 64;
      WHEN 'number' THEN
        total := total + 1 + length(atual::text) / 64;
      WHEN 'object' THEN
        total := total + 1;
        IF pg_column_size(atual) > 68::bigint * (p_resta - total + 1) + 8 THEN
          RETURN p_resta + 1;
        END IF;
        FOR filho IN SELECT e.key, e.value FROM jsonb_each(atual) e LOOP
          total := total + 1 + octet_length(filho.key) / 64;
          IF total > p_resta THEN
            RETURN total;
          END IF;
          topo := topo + 1;
          pilha[topo] := filho.value;
        END LOOP;
      WHEN 'array' THEN
        total := total + 1;
        IF pg_column_size(atual) > 68::bigint * (p_resta - total + 1) + 8 THEN
          RETURN p_resta + 1;
        END IF;
        IF total::bigint + jsonb_array_length(atual) > p_resta THEN
          RETURN p_resta + 1;
        END IF;
        FOR filho IN SELECT e.value FROM jsonb_array_elements(atual) e LOOP
          topo := topo + 1;
          pilha[topo] := filho.value;
        END LOOP;
      ELSE
        total := total + 1;
    END CASE;
    IF total > p_resta THEN
      RETURN total;
    END IF;
  END LOOP;
  RETURN total;
END $$;

-- Um passo da forma essencial: a forma de p_no (raiz p_raiz, no nivel p_fundo) e o gasto do orcamento ate aqui. Com
-- o gasto acima do teto, a forma volta nula e quem chamou para na hora.
CREATE FUNCTION selo_esquema_no(p_no jsonb, p_raiz jsonb, p_fundo integer, INOUT gasto integer, OUT forma jsonb)
LANGUAGE plpgsql IMMUTABLE STRICT SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  teto constant integer := selo_esquema_teto('nos');
  no jsonb := p_no;
  passos integer := 0;
  partes text[];
  alvo jsonb;
  fixo boolean;
  chave text;
  valor jsonb;
  filho record;
  nomes text[];
  formas jsonb[];
  chaves text[] := '{}';
  valores jsonb[] := '{}';
  dentro jsonb;
BEGIN
  gasto := gasto + selo_esquema_peso(no, teto - gasto);
  IF gasto > teto THEN
    RETURN;
  END IF;
  IF p_fundo > 32 THEN
    forma := to_jsonb('fundo_demais'::text);
    RETURN;
  END IF;
  IF jsonb_typeof(no) <> 'object' THEN
    forma := no;
    RETURN;
  END IF;
  WHILE no ? '$ref' LOOP
    passos := passos + 1;
    IF passos > 32 THEN
      forma := to_jsonb('ref_demais'::text);
      RETURN;
    END IF;
    IF jsonb_typeof(no -> '$ref') <> 'string' THEN
      forma := to_jsonb('ref_de_fora'::text);
      RETURN;
    END IF;
    partes := regexp_match(no ->> '$ref', '^#/(\$defs|definitions)/([^/]+)$');
    IF partes IS NULL THEN
      forma := to_jsonb('ref_de_fora'::text);
      RETURN;
    END IF;
    -- O grupo ($defs ou definitions) e conferido por caminho, sem copiar as definicoes; so a achada e copiada.
    alvo := CASE WHEN jsonb_typeof(p_raiz) = 'object'
                      AND CASE partes[1]
                            WHEN '$defs' THEN jsonb_path_exists(p_raiz, 'strict $ ? (@."$defs".type() == "object")')
                            ELSE jsonb_path_exists(p_raiz, 'strict $ ? (@."definitions".type() == "object")')
                          END
                 THEN p_raiz #> partes END;
    IF alvo IS NULL OR jsonb_typeof(alvo) <> 'object' THEN
      forma := to_jsonb('ref_sem_alvo'::text);
      RETURN;
    END IF;
    gasto := gasto + selo_esquema_peso(alvo, teto - gasto);
    IF gasto > teto THEN
      RETURN;
    END IF;
    no := (no - '$ref') || alvo;
  END LOOP;
  fixo := no ? 'const' OR no ? 'enum';
  FOR chave, valor IN SELECT e.key, e.value FROM jsonb_each(no) e LOOP
    IF chave = ANY (ARRAY['title', 'description', '$schema', '$id', '$comment', '$defs', 'definitions', 'examples',
                          'default', 'format', 'additionalProperties', 'maxItems', 'minItems', 'maxLength',
                          'minLength', 'readOnly', 'writeOnly', 'deprecated'])
       OR (chave = 'type' AND fixo) THEN
      CONTINUE;
    END IF;
    IF chave = 'properties' AND jsonb_typeof(valor) = 'object' THEN
      nomes := '{}';
      formas := '{}';
      FOR filho IN SELECT p.key, p.value FROM jsonb_each(valor) p LOOP
        SELECT r.gasto, r.forma INTO gasto, dentro FROM selo_esquema_no(filho.value, p_raiz, p_fundo + 1, gasto) r;
        IF gasto > teto THEN
          RETURN;
        END IF;
        nomes := nomes || filho.key;
        formas := formas || dentro;
      END LOOP;
      valor := coalesce((SELECT jsonb_object_agg(u.n, u.f) FROM unnest(nomes, formas) AS u(n, f)), '{}'::jsonb);
    ELSIF chave = 'items' AND jsonb_typeof(valor) = 'object' THEN
      SELECT r.gasto, r.forma INTO gasto, dentro FROM selo_esquema_no(valor, p_raiz, p_fundo + 1, gasto) r;
      IF gasto > teto THEN
        RETURN;
      END IF;
      valor := dentro;
    ELSIF chave IN ('required', 'enum') AND jsonb_typeof(valor) = 'array'
          AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(valor) x WHERE jsonb_typeof(x) <> 'string') THEN
      valor := (SELECT coalesce(jsonb_agg(x ORDER BY x #>> '{}' COLLATE "C"), '[]'::jsonb)
                FROM jsonb_array_elements(valor) x);
    END IF;
    chaves := chaves || chave;
    valores := valores || valor;
  END LOOP;
  forma := coalesce((SELECT jsonb_object_agg(u.c, u.v) FROM unnest(chaves, valores) AS u(c, v)), '{}'::jsonb);
END $$;

CREATE FUNCTION selo_esquema_essencial(p_esquema jsonb) RETURNS jsonb LANGUAGE sql IMMUTABLE STRICT
SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT CASE WHEN r.gasto > selo_esquema_teto('nos') THEN to_jsonb('grande_demais'::text) ELSE r.forma END
  FROM selo_esquema_no(p_esquema, p_esquema, 0, 0) r
$$;

-- Endereco na forma da comparacao entre o alvo de teste e a producao (critica A4): esquema e nome em minusculas
-- (so ASCII), sem usuario, sem porta padrao nem zeros a esquerda, sem ponto no fim do nome, caminho em minusculas
-- com %XX de letras sem reserva decodificados, sem . nem .., sem barras repetidas nem barra no fim, sem consulta nem
-- fragmento. De proposito mais largo que a igualdade de URL: dois enderecos que podem ser o mesmo servico ficam
-- iguais. Mesma regra de mcp_claro.selo.contrato.endereco_normalizado. Custo linear no tamanho do endereco: o %XX sai
-- de uma expressao so (antes, letra por letra, com substr, que no UTF-8 conta do comeco a cada letra: 32 mil letras
-- levavam 4 segundos), e o caminho e uma pilha com topo, sem copiar a lista a cada "..".
CREATE FUNCTION selo_endereco_normalizado(p_endereco text) RETURNS text
LANGUAGE plpgsql IMMUTABLE STRICT SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  maiusculas constant text := 'ABCDEFGHIJKLMNOPQRSTUVWXYZ';
  minusculas constant text := 'abcdefghijklmnopqrstuvwxyz';
  texto text := btrim(p_endereco, E' \t\n\r\f\v');
  url text[];
  esquema text;
  autoridade text;
  hospedeiro text;
  porta text := '';
  com_porta text[];
  caminho text;
  parte text;
  partes text[] := '{}';
  topo integer := 0;
BEGIN
  url := regexp_match(texto, '^([A-Za-z][A-Za-z0-9+.-]*)://([^/?#]*)([^?#]*)(?:\?[^#]*)?(?:#.*)?$');
  IF url IS NULL THEN
    RETURN translate(texto, maiusculas, minusculas);
  END IF;
  esquema := translate(url[1], maiusculas, minusculas);
  autoridade := translate(regexp_replace(url[2], '^.*@', ''), maiusculas, minusculas);
  hospedeiro := autoridade;
  com_porta := regexp_match(autoridade, '^(.*):([0-9]*)$');
  IF com_porta IS NOT NULL THEN
    hospedeiro := com_porta[1];
    porta := com_porta[2];
  END IF;
  IF porta <> '' THEN
    porta := CASE WHEN porta ~ '^0+$' THEN '0' ELSE ltrim(porta, '0') END;
  END IF;
  IF (esquema = 'http' AND porta = '80') OR (esquema = 'https' AND porta = '443') THEN
    porta := '';
  END IF;
  hospedeiro := rtrim(hospedeiro, '.');
  -- Cada pedaco e um %XX, um trecho sem % ou um % sozinho; o %XX de letra sem reserva vira a letra.
  caminho := coalesce((
    SELECT string_agg(CASE
             WHEN m[1] IS NULL THEN m[2]
             WHEN ('x' || substr(m[1], 2, 2))::bit(8)::integer BETWEEN 48 AND 57
                  OR ('x' || substr(m[1], 2, 2))::bit(8)::integer BETWEEN 65 AND 90
                  OR ('x' || substr(m[1], 2, 2))::bit(8)::integer BETWEEN 97 AND 122
                  OR ('x' || substr(m[1], 2, 2))::bit(8)::integer IN (45, 46, 95, 126)
               THEN chr(('x' || substr(m[1], 2, 2))::bit(8)::integer)
             ELSE m[1]
           END, '' ORDER BY n)
    FROM regexp_matches(url[3], '(%[0-9A-Fa-f]{2})|([^%]+|%)', 'g') WITH ORDINALITY AS t(m, n)), '');
  FOREACH parte IN ARRAY string_to_array(translate(caminho, maiusculas, minusculas), '/') LOOP
    IF parte IN ('', '.') THEN
      CONTINUE;
    END IF;
    IF parte = '..' THEN
      topo := greatest(topo - 1, 0);
      CONTINUE;
    END IF;
    topo := topo + 1;
    partes[topo] := parte;
  END LOOP;
  RETURN esquema || '://' || hospedeiro || CASE WHEN porta <> '' THEN ':' || porta ELSE '' END
         || CASE WHEN topo > 0 THEN '/' || array_to_string(partes[1:topo], '/') ELSE '' END;
END $$;

-- O IPv4 que o nome escreve, na forma que o resolvedor do sistema aceita (socket.inet_aton; mesma regra de
-- selo/declaracao.py, _ip_escrito): de uma a quatro partes separadas por ponto, cada uma decimal, octal (com 0 na
-- frente) ou hexadecimal (0x); cada parte antes da ultima vai ate 255, e a ultima enche os bytes que faltam. Assim
-- 127.1, 2130706433, 0x7f.0.0.1 e 0177.0.0.1 sao 127.0.0.1. Nulo para o nome que nao e um IPv4. p_nome ja vem em
-- minusculas e sem ponto no fim (selo_endereco_conexao); o regexp de entrada limita o texto a 4 partes curtas.
CREATE FUNCTION selo_ipv4_escrito(p_nome text) RETURNS inet LANGUAGE plpgsql IMMUTABLE STRICT
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  partes text[];
  digitos text;
  valor bigint;
  total bigint := 0;
  n integer;
BEGIN
  IF p_nome !~ '^(0x[0-9a-f]*|[0-9]+)(\.(0x[0-9a-f]*|[0-9]+)){0,3}$' THEN
    RETURN NULL;
  END IF;
  partes := string_to_array(p_nome, '.');
  n := cardinality(partes);
  FOR i IN 1 .. n LOOP
    IF partes[i] LIKE '0x%' THEN
      digitos := ltrim(substr(partes[i], 3), '0');
      IF partes[i] = '0x' OR length(digitos) > 8 THEN
        RETURN NULL;
      END IF;
      valor := ('x' || lpad(digitos, 8, '0'))::bit(32)::bigint;
    ELSIF partes[i] LIKE '0%' THEN
      digitos := ltrim(partes[i], '0');
      IF digitos ~ '[89]' OR length(digitos) > 11 THEN
        RETURN NULL;
      END IF;
      valor := 0;
      FOR j IN 1 .. length(digitos) LOOP
        valor := valor * 8 + (ascii(substr(digitos, j, 1)) - 48);
      END LOOP;
    ELSIF length(partes[i]) > 10 THEN
      RETURN NULL;
    ELSE
      valor := partes[i]::bigint;
    END IF;
    IF i < n THEN
      IF valor > 255 THEN
        RETURN NULL;
      END IF;
      total := total + (valor << (8 * (4 - i)));
    ELSIF valor >= (1::bigint << (8 * (5 - n))) THEN
      RETURN NULL;
    ELSE
      total := total + valor;
    END IF;
  END LOOP;
  RETURN '0.0.0.0'::inet + total;
END $$;

-- Onde o cliente conecta (critica A4; mesma regra de selo/declaracao.py, forma_de_conexao): o nome, a porta efetiva
-- (80 no http e 443 no https sem porta) e o caminho de selo_endereco_normalizado. O esquema nao entra: o mesmo nome
-- na mesma porta e o mesmo servico. O nome vai em minusculas e sem ponto no fim; os pontos de outros alfabetos
-- (U+3002, U+FF0E, U+FF61), que o leitor do cliente troca, viram pontos comuns; um IPv4 escrito de qualquer jeito
-- vai na forma comum (selo_ipv4_escrito), o IPv6 na forma do banco (host), e o IPv4 dentro de um IPv6 mapeado
-- (::ffff:a.b.c.d) ou do prefixo NAT64 (64:ff9b::a.b.c.d, 64:ff9b::/96, que numa rede so IPv6 chega ao proprio IPv4)
-- vale pelo IPv4. localhost, os nomes .localhost, todo IP de loopback (127.0.0.0/8, ::1) e o IP nao especificado
-- (0.0.0.0, ::) chegam a mesma maquina: viram o nome localhost. Nulo quando o cliente nao conecta no endereco ou o
-- banco nao le o que ele conecta: IPv4 de quatro numeros fora da forma comum, IPv6 que o cliente nao le, porta que nao
-- e so de digitos ASCII, nome vazio, caractere de controle. Ficam de fora:
-- * o nome fora de ASCII (depois de trocar os pontos): o leitor do cliente o escreve na forma xn-- (IDNA), que o banco
--   nao calcula. Esse nome nunca e igual ao de um alvo, que e so ASCII (selo_alvo_bem_escrito); a producao assim
--   escrita, com o alvo na forma xn--, fica so com a tela (declaracao.alvo_na_producao);
-- * a porta escrita como o leitor do cliente tambem a le, com int() (+443, 4_43, ' 443', digitos de outros
--   alfabetos): o alvo nunca a tem (selo_alvo_bem_escrito); a producao assim escrita fica so com a tela, que le a
--   porta pelo leitor do cliente, e o cadastro a recusa (urlsplit .port);
-- * outro prefixo NAT64 que nao o 64:ff9b::/96 (o de uso local 64:ff9b:1::/48, ou o da rede), que so a rede sabe;
-- * o IPv4 que o 6to4 (2002::/16) e o Teredo (2001::/32) levam dentro, que a Fase 3 le para saber se o IP e da
--   internet (telas/admin._ip_publico, ligacao.cliente._ip_publico): aqui eles valem como IPv6, no banco e na tela
--   (declaracao._ipv6_com_ipv4_dentro), e o alvo pode ser um deles. Um deles so chega a maquina desse IPv4 quando
--   ela e a propria ponta do tunel, e o 6to4 esta em desuso (RFC 7526).
-- Com os pontos trocados, o banco e de proposito mais largo que o leitor do cliente, que recusa alguns desses nomes
-- (IDNA).
CREATE FUNCTION selo_endereco_conexao(p_endereco text) RETURNS text[] LANGUAGE plpgsql IMMUTABLE STRICT
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  ipv4 constant text := '(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])){3}';
  autoridade text;
  partes text[];
  nome text;
  porta text;
  ip inet;
BEGIN
  IF p_endereco ~ E'[\\x01-\\x1f\\x7f]' THEN
    RETURN NULL;
  END IF;
  autoridade := regexp_replace(substring(p_endereco FROM '^[A-Za-z][A-Za-z0-9+.-]*://([^/?#]*)'), '^.*@', '');
  IF autoridade IS NULL THEN
    RETURN NULL;
  END IF;
  IF left(autoridade, 1) = '[' THEN
    partes := regexp_match(autoridade, '^\[([^\]]*)\](?::(.*))?$');
    IF partes IS NULL THEN
      RETURN NULL;
    END IF;
    nome := translate(partes[1], 'ABCDEF', 'abcdef');
    IF nome !~ '^[0-9a-f:.]+$' OR NOT pg_input_is_valid(nome, 'inet')
       OR (strpos(nome, '.') > 0 AND nome !~ (':' || ipv4 || '$')) THEN
      RETURN NULL;
    END IF;
    ip := nome::inet;
    IF family(ip) <> 6 THEN
      RETURN NULL;
    END IF;
    IF ip <<= '::ffff:0.0.0.0/96'::inet THEN
      ip := '0.0.0.0'::inet + (ip - '::ffff:0.0.0.0'::inet);
    ELSIF ip <<= '64:ff9b::/96'::inet THEN
      ip := '0.0.0.0'::inet + (ip - '64:ff9b::'::inet);
    END IF;
  ELSE
    partes := regexp_match(autoridade, '^([^:]*)(?::(.*))?$');
    IF partes IS NULL THEN
      RETURN NULL;
    END IF;
    nome := partes[1];
    -- O leitor do cliente recusa o nome de quatro numeros fora da forma comum (010.0.0.1, 256.0.0.1).
    IF nome ~ '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' AND nome !~ ('^' || ipv4 || '$') THEN
      RETURN NULL;
    END IF;
    nome := rtrim(translate(translate(nome, U&'\3002\FF0E\FF61', '...'), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ',
                            'abcdefghijklmnopqrstuvwxyz'), '.');
    IF nome = '' THEN
      RETURN NULL;
    END IF;
    ip := selo_ipv4_escrito(nome);
  END IF;
  porta := coalesce(partes[2], '');
  IF porta !~ '^[0-9]*$' THEN
    RETURN NULL;
  END IF;
  porta := CASE
    WHEN porta = '' THEN CASE WHEN p_endereco ~* '^https:' THEN '443' WHEN p_endereco ~* '^http:' THEN '80' END
    WHEN porta ~ '^0+$' THEN '0'
    ELSE ltrim(porta, '0')
  END;
  IF porta IS NULL THEN
    RETURN NULL;
  END IF;
  IF nome = 'localhost' OR nome LIKE '%.localhost'
     OR (family(ip) = 4 AND (ip <<= '127.0.0.0/8'::inet OR ip = '0.0.0.0'::inet))
     OR (family(ip) = 6 AND (ip = '::1'::inet OR ip = '::'::inet)) THEN
    nome := 'localhost';
  ELSIF ip IS NOT NULL THEN
    nome := host(ip);
  END IF;
  RETURN ARRAY[nome, porta, substring(selo_endereco_normalizado(p_endereco) FROM '^[^/]*//[^/]*(.*)$')];
END $$;

-- O nome e a porta do alvo de teste escritos de um jeito so (critica A4; mesma regra de selo/declaracao.py,
-- _nome_do_alvo, que o teste confere numa lista de formas): o nome e a porta que o cliente conecta sao o proprio texto.
-- Sem isso, um alvo que e a producao escrita de outro jeito passava pela comparacao de texto (selo_alvo_na_producao):
-- pontos de outros alfabetos (U+3002, U+FF0E, U+FF61), que o leitor do cliente troca por pontos comuns, um IPv4 curto,
-- decimal ou hexadecimal (127.1, 2130706433, 0x7f.0.0.1), que o resolvedor le como o IP inteiro, e a porta escrita de
-- outro jeito. Vale:
-- * um nome so de letras ASCII (minusculas depois de trocar as maiusculas ASCII), numeros, _ e -, em pedacos
--   separados por ponto, com um ponto no fim ou nao; um nome com acento vai na forma xn--;
-- * e, se o nome tem cara de IPv4 (numeros ou 0x... separados por ate tres pontos), so o IPv4 com os quatro numeros,
--   de 0 a 255 e sem zero a esquerda;
-- * ou um IPv6 entre colchetes, na forma curta que o banco escreve (host(inet)), sem zona nem mascara e sem um IPv4
--   dentro: nem o mapeado ::ffff:a.b.c.d, que o cliente conecta no proprio IPv4 (a producao 10.0.0.5, ou a maquina
--   local, escrita de outro jeito), nem o do prefixo NAT64 64:ff9b::a.b.c.d (64:ff9b::/96), que numa rede so IPv6
--   chega ao proprio IPv4 (a Fase 3 o trata assim, telas/admin._NAT64), nem a forma antiga ::a.b.c.d (compativel com
--   IPv4), que o banco e o Python escrevem de jeitos diferentes. O 6to4 e o Teredo, que tambem levam um IPv4, valem
--   como IPv6 (veja selo_endereco_conexao);
-- * e a porta vazia ou so com os digitos ASCII de 0 a 9, de 0 a 65535 (a regra de urlsplit .port, que a tela usa).
--   Os zeros a esquerda saem antes da conta, e mais de cinco digitos depois deles ja e demais: a conta nunca passa do
--   integer (porta com 11 ou 40 digitos da false, nunca erro).
--   O leitor do cliente (httpx2) le a porta com int(), que aceita +443, 4_43, ' 443' e digitos de outros alfabetos:
--   sem esta regra, o alvo https://p.exemplo:+443/mcp, que o cliente conecta na producao https://p.exemplo/mcp, era
--   gravado (as formas normalizada e de conexao do banco nao leem essa porta, e os textos ficavam diferentes).
-- O nome e o de urllib.parse.urlsplit: depois do ultimo @ e, sem colchete, antes do primeiro ':'; a porta e o que
-- vem depois do ':' (com colchete, depois do ']'). Endereco nulo (o modo ferramentas_de_teste nao tem alvo) vale.
CREATE FUNCTION selo_alvo_mal_escrito() RETURNS text LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT 'O nome do alvo de teste vai escrito de um jeito so: letras sem acento, numeros, ponto e hifen (a forma xn--'
         ' para nomes com acento), um IPv4 com os quatro numeros ou um IPv6 entre colchetes, na forma curta e sem um'
         ' IPv4 mapeado (::ffff:) ou NAT64 (64:ff9b::); e a porta, se houver, so com os digitos de 0 a 9,'
         ' ate 65535'::text
$$;

CREATE FUNCTION selo_alvo_bem_escrito(p_endereco text) RETURNS boolean LANGUAGE plpgsql IMMUTABLE
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  hospedeiro text;
  partes text[];
  nome text;
  porta text;
BEGIN
  IF p_endereco IS NULL THEN
    RETURN true;
  END IF;
  hospedeiro := regexp_replace(substring(p_endereco FROM '^[A-Za-z][A-Za-z0-9+.-]*://([^/?#]*)'), '^.*@', '');
  IF hospedeiro IS NULL THEN
    RETURN false;
  END IF;
  -- O nome e a porta: com colchete, o IPv6 e depois dele so ':' e digitos; sem colchete, o nome ate o primeiro ':'
  -- e depois dele so digitos (a regra de urlsplit .port, que a tela usa).
  IF strpos(hospedeiro, '[') > 0 THEN
    partes := regexp_match(hospedeiro, '^\[([^\]]*)\](?::([0-9]*))?$');
  ELSE
    partes := regexp_match(hospedeiro, '^([^:]*)(?::([0-9]*))?$');
  END IF;
  IF partes IS NULL THEN
    RETURN false;
  END IF;
  -- Sem os zeros a esquerda, mais de cinco digitos ja e demais: a conta so roda no que cabe no integer.
  porta := ltrim(coalesce(partes[2], ''), '0');
  IF length(porta) > 5 OR (porta <> '' AND porta::integer > 65535) THEN
    RETURN false;
  END IF;
  IF strpos(hospedeiro, '[') > 0 THEN
    nome := translate(partes[1], 'ABCDEF', 'abcdef');
    RETURN nome ~ '^[0-9a-f:.]+$' AND pg_input_is_valid(nome, 'inet')
       AND family(nome::inet) = 6 AND host(nome::inet) = nome
       AND NOT (host(nome::inet) ~ '^::[0-9]+\.' ) AND NOT (nome::inet <<= '::ffff:0.0.0.0/96'::inet)
       AND NOT (nome::inet <<= '64:ff9b::/96'::inet);
  END IF;
  nome := translate(partes[1], 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz');
  IF nome !~ '^[a-z0-9_-]+(\.[a-z0-9_-]+)*\.?$' THEN
    RETURN false;
  END IF;
  IF nome ~ '^(0x[0-9a-f]*|[0-9]+)(\.(0x[0-9a-f]*|[0-9]+)){0,3}\.?$' THEN
    RETURN nome ~ '^(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9][0-9]|[1-9]?[0-9])){3}$';
  END IF;
  RETURN true;
END $$;

-- A coluna "Conta como" do vocabulario, e as passagens permitidas entre duas respostas sobre o mesmo numero
-- (envios_selo.chave): a propria operacao e o pedido igual que saiu com o numero dela (critica M2). A contradicao sai
-- das categorias, nao do estado exato.
CREATE FUNCTION selo_conta_como(p_estado text) RETURNS text LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT CASE p_estado
    WHEN 'concluida' THEN 'aconteceu'
    WHEN 'recusada' THEN 'nao_aconteceu'
    WHEN 'em_andamento' THEN 'em_aberto'
    WHEN 'nao_recebida' THEN 'nao_aconteceu'
    WHEN 'parcial' THEN 'em_parte'
    WHEN 'desfeita' THEN 'nao_ficou'
    WHEN 'parada' THEN 'nao_aconteceu'
    WHEN 'desconhecido' THEN 'em_aberto'
  END
$$;

CREATE FUNCTION selo_passagem_permitida(p_antes text, p_novo text) RETURNS boolean LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT CASE
    WHEN p_novo IS NULL OR selo_conta_como(p_novo) IS NULL
         OR (p_antes IS NOT NULL AND selo_conta_como(p_antes) IS NULL) THEN false
    WHEN p_antes IS NULL OR p_antes = 'desconhecido' OR p_novo = 'desconhecido' THEN true
    WHEN p_antes = 'em_andamento' THEN p_novo <> 'nao_recebida'
    WHEN p_novo = 'em_andamento' THEN false
    WHEN selo_conta_como(p_antes) IN ('aconteceu', 'em_parte')
      THEN selo_conta_como(p_novo) IN ('aconteceu', 'em_parte', 'nao_ficou')
    WHEN selo_conta_como(p_antes) = 'nao_aconteceu' THEN selo_conta_como(p_novo) = 'nao_aconteceu'
    ELSE p_novo = 'desfeita'
  END
$$;

-- Critica A3.3: uma resposta nao_recebida so vale como final com duas respostas assim, com ao menos o primeiro degrau
-- da escada da consulta (1 minuto) entre a primeira e a ultima. p_horas: o gravado_em de cada resposta nao_recebida.
-- Um sistema que registra o numero so no fim da escrita diz nao_recebida enquanto ela roda; a segunda pergunta o pega
-- (contradicao). Vale na tela (selo_estado_final), na fila agora (medicoes.v_fila_agora) e na queima do numero do
-- pedido igual (envios_selo_nasce). Mesma regra de contrato.nao_recebida_confirmada.
CREATE FUNCTION selo_nao_recebida_confirmada(p_horas timestamptz[]) RETURNS boolean LANGUAGE sql IMMUTABLE
SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT coalesce(cardinality(p_horas) >= 2
                  AND (SELECT max(h) - min(h) FROM unnest(p_horas) h) >= interval '1 minute', false)
$$;

-- O primeiro problema na forma de testes_selo.casos, ou NULL (mesmos codigos de
-- mcp_claro.selo.contrato.problema_nos_casos). Cada item: {caso, ferramenta, resultado, razao, tentativas,
-- excluidas}, nada mais; ferramenta e uma das testadas (nula nos casos de escopo sistema).
CREATE FUNCTION selo_casos_problema(p_casos jsonb, p_ferramentas uuid[]) RETURNS text
LANGUAGE plpgsql IMMUTABLE SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  item jsonb;
  c record;
  vistos text[] := '{}';
  marca text;
  ferramenta jsonb;
  resultado jsonb;
  razao jsonb;
BEGIN
  IF p_casos IS NULL OR jsonb_typeof(p_casos) <> 'array' THEN
    RETURN 'nao_e_lista';
  END IF;
  FOR item IN SELECT e.valor FROM jsonb_array_elements(p_casos) WITH ORDINALITY AS e(valor, n) ORDER BY e.n LOOP
    IF jsonb_typeof(item) <> 'object' THEN
      RETURN 'item_nao_e_objeto';
    END IF;
    IF (SELECT array_agg(k ORDER BY k COLLATE "C") FROM jsonb_object_keys(item) k)
       IS DISTINCT FROM ARRAY['caso', 'excluidas', 'ferramenta', 'razao', 'resultado', 'tentativas'] THEN
      RETURN 'campos_do_item';
    END IF;
    SELECT * INTO c FROM selo_casos('selo/1') s
    WHERE jsonb_typeof(item -> 'caso') = 'string' AND s.caso = item ->> 'caso';
    IF NOT FOUND THEN
      RETURN 'caso_desconhecido';
    END IF;
    ferramenta := item -> 'ferramenta';
    IF c.escopo = 'sistema' THEN
      IF jsonb_typeof(ferramenta) <> 'null' THEN
        RETURN 'ferramenta_do_caso';
      END IF;
    ELSIF jsonb_typeof(ferramenta) <> 'string'
          OR NOT ((ferramenta #>> '{}') = ANY (coalesce(p_ferramentas, '{}'::uuid[])::text[])) THEN
      RETURN 'ferramenta_do_caso';
    END IF;
    resultado := item -> 'resultado';
    razao := item -> 'razao';
    IF jsonb_typeof(resultado) <> 'string' OR NOT ((resultado #>> '{}') = ANY (selo_vocabulario('resultados'))) THEN
      RETURN 'resultado_fora';
    END IF;
    IF jsonb_typeof(razao) <> 'null'
       AND (jsonb_typeof(razao) <> 'string' OR NOT ((razao #>> '{}') = ANY (selo_vocabulario('razoes')))) THEN
      RETURN 'razao_fora';
    END IF;
    IF jsonb_typeof(razao) = 'null' AND (resultado #>> '{}') IN ('nao_se_aplica', 'nao_testado') THEN
      RETURN 'razao_faltando';
    END IF;
    IF (resultado #>> '{}') = 'cumpriu' AND c.maximo = 'nao_observado' THEN
      RETURN 'acima_do_maximo';
    END IF;
    IF (resultado #>> '{}') = 'nao_se_aplica' AND c.nao_se_aplica_ate < c.nivel THEN
      RETURN 'nao_se_aplica_sem_lugar';
    END IF;
    IF NOT (jsonb_typeof(item -> 'tentativas') = 'number' AND (item ->> 'tentativas') ~ '^[0-9]+$'
            AND jsonb_typeof(item -> 'excluidas') = 'number' AND (item ->> 'excluidas') ~ '^[0-9]+$') THEN
      RETURN 'tentativas';
    END IF;
    IF (item ->> 'excluidas')::numeric > (item ->> 'tentativas')::numeric
       OR ((resultado #>> '{}') IN ('cumpriu', 'falhou', 'nao_observado') AND (item ->> 'tentativas')::numeric < 1) THEN
      RETURN 'tentativas';
    END IF;
    marca := (item ->> 'caso') || '|' || coalesce(ferramenta #>> '{}', '');
    IF marca = ANY (vistos) THEN
      RETURN 'caso_repetido';
    END IF;
    vistos := vistos || marca;
  END LOOP;
  RETURN NULL;
END $$;

-- O criterio do MCP-CLARO (desenho 2.4, criticas M5 e M6): nenhum item tem falhou; cada caso obrigatorio de nivel ate
-- p_nivel tem, em cada ferramenta (ou no sistema), um item com o resultado maximo do caso, ou nao_se_aplica onde o
-- caso admite nesse nivel. Supoe itens validos (selo_casos_problema). Mesma regra de contrato.casos_aprovam.
CREATE FUNCTION selo_casos_aprovam(p_casos jsonb, p_ferramentas uuid[], p_nivel smallint) RETURNS boolean
LANGUAGE sql IMMUTABLE SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT NOT EXISTS (SELECT 1 FROM jsonb_array_elements(p_casos) i WHERE i ->> 'resultado' = 'falhou')
     AND NOT EXISTS (
       SELECT 1
       FROM selo_casos('selo/1') c
       CROSS JOIN LATERAL (
         SELECT NULL::text AS ferramenta WHERE c.escopo = 'sistema'
         UNION ALL
         SELECT f::text FROM unnest(p_ferramentas) f WHERE c.escopo = 'ferramenta'
       ) a
       WHERE c.obrigatorio AND c.nivel <= p_nivel
         AND NOT EXISTS (
           SELECT 1 FROM jsonb_array_elements(p_casos) i
           WHERE i ->> 'caso' = c.caso AND (i ->> 'ferramenta') IS NOT DISTINCT FROM a.ferramenta
             AND (i ->> 'resultado' = c.maximo
                  OR (i ->> 'resultado' = 'nao_se_aplica' AND p_nivel <= c.nao_se_aplica_ate))))
$$;
-- Fim do bloco CONTRATO.
"""

SQL_DECLARACOES = r"""
-- Bloco DECLARACOES. A declaracao de teste que a equipe do sistema escreve e o ADMIN salva (com a caixa marcada e o
-- login refeito, pela tela). So de acrescentar e versionada: cada salvamento e uma versao nova. O banco confere o
-- que da para conferir sem o codigo: a versao seguinte, o ADMIN ativo, o JSON igual as colunas, o alvo de teste com o
-- nome escrito de um jeito so (selo_alvo_bem_escrito, ate 2048 letras) e diferente do endereco de producao do
-- sistema e de todo outro sistema do cadastro, comparados na forma normalizada e na forma de conexao
-- (selo_alvo_na_producao; critica A4; de novo no pedido, em 'rodando' e no teste). https fora de desenvolvimento
-- e teste e o resto da forma da declaracao ficam com o modelo (selo/declaracao.py). O teto do texto e o do modelo
-- (64 KiB); o banco guarda a forma jsonb, mais larga, e aceita ate o dobro.
CREATE TABLE declaracoes_selo (
  id             uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  sistema_id     uuid        NOT NULL REFERENCES sistemas (id),
  versao         integer     NOT NULL CHECK (versao >= 1),
  contrato       text        NOT NULL CHECK (contrato = 'selo/1'),
  modo           text        NOT NULL CHECK (modo = ANY (selo_vocabulario('modos'))),
  endereco_teste text        CHECK (endereco_teste IS NULL OR (endereco_teste ~ '^https?://'
                                                                AND length(endereco_teste) <= 2048)),
  declaracao     jsonb       NOT NULL CHECK (jsonb_typeof(declaracao) = 'object'
                                             AND octet_length(declaracao::text) <= 131072),
  responsavel    text        NOT NULL CHECK (length(btrim(responsavel)) BETWEEN 3 AND 200),
  salva_por      text        NOT NULL CHECK (btrim(salva_por) <> '' AND length(salva_por) <= 200),
  pessoa_id      uuid        NOT NULL REFERENCES pessoas (id),
  salva_em       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (sistema_id, versao),
  -- Alvo das chaves estrangeiras compostas: o pedido e o teste usam uma declaracao do proprio sistema.
  UNIQUE (id, sistema_id),
  CHECK ((modo = 'ambiente_de_teste') = (endereco_teste IS NOT NULL))
);

-- O alvo de teste e o endereco de producao de algum sistema do cadastro (critica A4): o proprio sistema ou outro, de
-- qualquer situacao. Duas comparacoes, como a tela (declaracao.alvo_na_producao): a forma normalizada
-- (selo_endereco_normalizado) e a forma de conexao (selo_endereco_conexao), que pega a producao escrita de outro
-- jeito, como 127.0.0.1 e localhost, [::1] ou 127.0.0.2, localhost e x.localhost, 127.1, ou 10.0.0.5 e
-- [::ffff:10.0.0.5] ou [64:ff9b::a00:5] (NAT64) na producao. O alvo escrito de outro jeito (o nome, o IP ou a porta)
-- ja saiu antes, por selo_alvo_bem_escrito. Ficam so com a tela a producao com o nome fora de ASCII e o alvo na forma
-- xn--, e a producao com a porta escrita como int() a le (+443), que o cadastro recusa (selo_endereco_conexao). O
-- 6to4 e o Teredo nao valem pelo IPv4 de dentro em nenhum dos dois lados (selo_endereco_conexao).
-- Conferido ao salvar a declaracao, ao pedir o teste, na passagem para 'rodando' (antes das escritas da bateria) e ao
-- gravar o teste, porque a producao pode mudar (ou outro sistema ser cadastrado) depois de a declaracao ser salva.
-- Cada forma do alvo e calculada uma vez (subconsulta), e cada producao uma vez por forma.
-- Roda como quem chama: a entrada e o despachante leem sistemas.
CREATE FUNCTION selo_alvo_na_producao(p_alvo text) RETURNS boolean LANGUAGE sql STABLE
SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT p_alvo IS NOT NULL AND EXISTS (
    SELECT 1 FROM sistemas s
    WHERE selo_endereco_normalizado(s.endereco) = (SELECT selo_endereco_normalizado(p_alvo))
       OR selo_endereco_conexao(s.endereco) = (SELECT selo_endereco_conexao(p_alvo)))
$$;

CREATE FUNCTION declaracoes_selo_nasce() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  ultima integer;
BEGIN
  NEW.salva_em := now();
  -- O teto do alvo antes das conferencias que leem o texto (o CHECK da tabela so roda depois deste gatilho).
  IF length(NEW.endereco_teste) > 2048 THEN
    RAISE EXCEPTION 'O alvo de teste tem ate 2048 letras' USING ERRCODE = 'check_violation';
  END IF;
  SELECT coalesce(max(versao), 0) INTO ultima FROM declaracoes_selo WHERE sistema_id = NEW.sistema_id;
  IF NEW.versao <> ultima + 1 THEN
    RAISE EXCEPTION 'A declaracao de teste deveria ser a versao %, nao %', ultima + 1, NEW.versao
      USING ERRCODE = 'check_violation';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pessoas WHERE id = NEW.pessoa_id AND papel = 'admin' AND situacao = 'ativa') THEN
    RAISE EXCEPTION 'So um ADMIN ativo salva a declaracao de teste' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF (NEW.declaracao ->> 'contrato') IS DISTINCT FROM NEW.contrato
     OR (NEW.declaracao ->> 'modo') IS DISTINCT FROM NEW.modo
     OR (NEW.declaracao ->> 'endereco_teste') IS DISTINCT FROM NEW.endereco_teste
     OR (NEW.declaracao ->> 'responsavel') IS DISTINCT FROM NEW.responsavel THEN
    RAISE EXCEPTION 'O contrato, o modo, o alvo e o responsavel da declaracao sao os do proprio JSON'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NOT selo_alvo_bem_escrito(NEW.endereco_teste) THEN
    RAISE EXCEPTION '%', selo_alvo_mal_escrito() USING ERRCODE = 'check_violation';
  END IF;
  IF selo_alvo_na_producao(NEW.endereco_teste) THEN
    RAISE EXCEPTION 'O alvo de teste declarado e o endereco de producao de um sistema do cadastro; a bateria nao escreve na producao'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER declaracoes_selo_nasce BEFORE INSERT ON declaracoes_selo
  FOR EACH ROW EXECUTE FUNCTION declaracoes_selo_nasce();
CREATE TRIGGER declaracoes_selo_sem_update_delete BEFORE UPDATE OR DELETE ON declaracoes_selo
  FOR EACH ROW EXECUTE FUNCTION extrato_recusa_mudanca();
CREATE TRIGGER declaracoes_selo_sem_truncate BEFORE TRUNCATE ON declaracoes_selo
  FOR EACH STATEMENT EXECUTE FUNCTION extrato_recusa_mudanca();

-- A chave de teste da bateria (A4, M11): guardada pelo ADMIN, uma por sistema, sem pessoa nem programa. O tipo faz
-- parte da chave unica (0002) e do contexto do cofre: guardar a chave de teste nunca toca a chave de producao
-- ('acesso'). E nenhuma linha muda de tipo nem de dono depois de gravada.
ALTER TABLE chaves_cifradas DROP CONSTRAINT chaves_cifradas_tipo_check;
ALTER TABLE chaves_cifradas ADD CONSTRAINT chaves_cifradas_tipo_check
  CHECK (tipo IN ('acesso', 'renovacao', 'declaracoes', 'teste_selo'));
ALTER TABLE chaves_cifradas ADD CONSTRAINT chaves_cifradas_teste_selo_do_sistema
  CHECK (tipo <> 'teste_selo' OR (pessoa_id IS NULL AND aplicativo_id IS NULL));

CREATE FUNCTION chaves_cifradas_guarda() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF ROW(NEW.sistema_id, NEW.pessoa_id, NEW.aplicativo_id, NEW.tipo)
     IS DISTINCT FROM ROW(OLD.sistema_id, OLD.pessoa_id, OLD.aplicativo_id, OLD.tipo) THEN
    RAISE EXCEPTION 'Uma chave guardada nao muda de sistema, de dono nem de tipo'
      USING ERRCODE = 'insufficient_privilege';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER chaves_cifradas_guarda BEFORE UPDATE ON chaves_cifradas
  FOR EACH ROW EXECUTE FUNCTION chaves_cifradas_guarda();
-- Fim do bloco DECLARACOES.
"""

SQL_TESTES = r"""
-- Bloco TESTES. O ADMIN pede o teste (entrada); o despachante pega um pedido por vez, roda a bateria e grava o
-- resultado na mesma transacao que fecha o pedido. Um pedido 'pedido' ou 'rodando' por sistema.
CREATE TABLE pedidos_teste_selo (
  id            uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  sistema_id    uuid        NOT NULL REFERENCES sistemas (id),
  nivel         smallint    NOT NULL CHECK (nivel IN (2, 3)),
  declaracao_id uuid        NOT NULL,
  pedido_por    uuid        NOT NULL REFERENCES pessoas (id),
  pedido_em     timestamptz NOT NULL DEFAULT now(),
  estado        text        NOT NULL DEFAULT 'pedido' CHECK (estado = ANY (selo_vocabulario('estados_do_pedido'))),
  motivo        text        CHECK (motivo IS NULL OR motivo = ANY (selo_vocabulario('motivos_de_interrupcao'))),
  mudou_em      timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (declaracao_id, sistema_id) REFERENCES declaracoes_selo (id, sistema_id),
  UNIQUE (id, sistema_id),
  CHECK ((estado = 'interrompido') = (motivo IS NOT NULL))
);
CREATE UNIQUE INDEX pedidos_teste_selo_um_por_vez ON pedidos_teste_selo (sistema_id)
  WHERE estado IN ('pedido', 'rodando');

-- O pedido nasce 'pedido', com a hora do banco, a declaracao mais recente do sistema e um ADMIN ativo. Selo de nivel
-- 2 ou 3 so para sistema ativo, na 2026-07-28, com chave por pessoa (critica B7), e com o alvo de teste da
-- declaracao bem escrito e ainda fora da producao de todo sistema do cadastro (critica A4). A recusa de terceiro ate
-- o G7 e da tela
-- (selo/declaracao.py), nao do banco.
CREATE FUNCTION pedidos_teste_selo_nasce() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  s record;
  alvo text;
BEGIN
  IF NEW.estado IS DISTINCT FROM 'pedido' OR NEW.motivo IS NOT NULL THEN
    RAISE EXCEPTION 'Um pedido de teste do selo nasce pedido' USING ERRCODE = 'check_violation';
  END IF;
  NEW.pedido_em := now();
  NEW.mudou_em := now();
  IF NOT EXISTS (SELECT 1 FROM pessoas WHERE id = NEW.pedido_por AND papel = 'admin' AND situacao = 'ativa') THEN
    RAISE EXCEPTION 'So um ADMIN ativo pede o teste do selo' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF NEW.declaracao_id IS DISTINCT FROM (SELECT d.id FROM declaracoes_selo d WHERE d.sistema_id = NEW.sistema_id
                                         ORDER BY d.versao DESC LIMIT 1) THEN
    RAISE EXCEPTION 'O teste do selo usa a declaracao de teste mais recente do sistema'
      USING ERRCODE = 'check_violation';
  END IF;
  SELECT situacao, versao_mcp, tipo_chave INTO s FROM sistemas WHERE id = NEW.sistema_id;
  IF s.situacao IS DISTINCT FROM 'ativo' OR s.versao_mcp IS DISTINCT FROM '2026-07-28' THEN
    RAISE EXCEPTION 'O teste do selo so vale para sistema ativo na versao 2026-07-28' USING ERRCODE = 'check_violation';
  END IF;
  IF s.tipo_chave IS DISTINCT FROM 'por_pessoa' THEN
    RAISE EXCEPTION 'O selo de nivel 2 ou 3 e so para sistema com chave por pessoa' USING ERRCODE = 'check_violation';
  END IF;
  alvo := (SELECT d.endereco_teste FROM declaracoes_selo d WHERE d.id = NEW.declaracao_id);
  IF NOT selo_alvo_bem_escrito(alvo) THEN
    RAISE EXCEPTION '%', selo_alvo_mal_escrito() USING ERRCODE = 'check_violation';
  END IF;
  IF selo_alvo_na_producao(alvo) THEN
    RAISE EXCEPTION 'O alvo de teste declarado e o endereco de producao de um sistema do cadastro; a bateria nao escreve na producao'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER pedidos_teste_selo_nasce BEFORE INSERT ON pedidos_teste_selo
  FOR EACH ROW EXECUTE FUNCTION pedidos_teste_selo_nasce();

-- O pedido so anda de 'pedido' para 'rodando' ou 'interrompido', e de 'rodando' para 'terminado' (com o teste
-- gravado) ou 'interrompido'. So o despachante (ou o dono) muda. 'interrompido' sempre com motivo (CHECK); a hora e
-- a do banco. O resto da linha nao muda. A bateria faz as escritas reais entre 'rodando' e o fim, entao 'rodando' so
-- com a porta de saida aberta e com o pedido ainda valendo agora: a declaracao do pedido ainda e a mais recente do
-- sistema (o ADMIN pode ter salvo uma correcao do alvo ou do modo depois do pedido), sistema ativo na 2026-07-28, com
-- chave por pessoa, e o alvo de teste da declaracao bem escrito e fora da producao de todo sistema do cadastro (A4: a
-- producao pode mudar, ou outro sistema ser cadastrado no alvo, entre o pedido e a vez do despachante). Recusado, o
-- despachante marca o pedido 'interrompido' com 'sistema_mudou'.
CREATE FUNCTION pedidos_teste_selo_guarda() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  dono text := (SELECT relowner::regrole::text FROM pg_class WHERE oid = TG_RELID);
  s record;
  alvo text;
BEGIN
  IF current_user NOT IN ('mcp_claro_despachante', dono) THEN
    RAISE EXCEPTION 'So o despachante muda um pedido de teste do selo' USING ERRCODE = 'insufficient_privilege';
  END IF;
  IF ROW(NEW.id, NEW.sistema_id, NEW.nivel, NEW.declaracao_id, NEW.pedido_por, NEW.pedido_em)
     IS DISTINCT FROM ROW(OLD.id, OLD.sistema_id, OLD.nivel, OLD.declaracao_id, OLD.pedido_por, OLD.pedido_em) THEN
    RAISE EXCEPTION 'Num pedido de teste do selo so o estado muda' USING ERRCODE = 'check_violation';
  END IF;
  IF ROW(NEW.estado, NEW.motivo) IS NOT DISTINCT FROM ROW(OLD.estado, OLD.motivo) THEN
    NEW.mudou_em := OLD.mudou_em;
    RETURN NEW;
  END IF;
  IF NOT ((OLD.estado = 'pedido' AND NEW.estado IN ('rodando', 'interrompido'))
          OR (OLD.estado = 'rodando' AND NEW.estado IN ('terminado', 'interrompido'))) THEN
    RAISE EXCEPTION 'Mudanca recusada no pedido de teste do selo: % -> %', OLD.estado, NEW.estado
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.estado = 'rodando' THEN
    IF NOT coalesce((SELECT aberta FROM estado_saida WHERE id = 1), false) THEN
      RAISE EXCEPTION 'Com a porta de saida fechada, o teste do selo nao comeca' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.declaracao_id IS DISTINCT FROM (SELECT d.id FROM declaracoes_selo d WHERE d.sistema_id = NEW.sistema_id
                                           ORDER BY d.versao DESC LIMIT 1) THEN
      RAISE EXCEPTION 'O teste do selo usa a declaracao de teste mais recente do sistema'
        USING ERRCODE = 'check_violation';
    END IF;
    SELECT situacao, versao_mcp, tipo_chave INTO s FROM sistemas WHERE id = NEW.sistema_id;
    IF s.situacao IS DISTINCT FROM 'ativo' OR s.versao_mcp IS DISTINCT FROM '2026-07-28' THEN
      RAISE EXCEPTION 'O teste do selo so vale para sistema ativo na versao 2026-07-28' USING ERRCODE = 'check_violation';
    END IF;
    IF s.tipo_chave IS DISTINCT FROM 'por_pessoa' THEN
      RAISE EXCEPTION 'O selo de nivel 2 ou 3 e so para sistema com chave por pessoa' USING ERRCODE = 'check_violation';
    END IF;
    alvo := (SELECT d.endereco_teste FROM declaracoes_selo d WHERE d.id = NEW.declaracao_id);
    IF NOT selo_alvo_bem_escrito(alvo) THEN
      RAISE EXCEPTION '%', selo_alvo_mal_escrito() USING ERRCODE = 'check_violation';
    END IF;
    IF selo_alvo_na_producao(alvo) THEN
      RAISE EXCEPTION 'O alvo de teste declarado e o endereco de producao de um sistema do cadastro; a bateria nao escreve na producao'
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  IF NEW.estado = 'terminado' AND NOT EXISTS (SELECT 1 FROM testes_selo WHERE pedido_id = NEW.id) THEN
    RAISE EXCEPTION 'O pedido de teste do selo so termina com o teste gravado' USING ERRCODE = 'check_violation';
  END IF;
  NEW.mudou_em := now();
  RETURN NEW;
END $$;
CREATE TRIGGER pedidos_teste_selo_guarda BEFORE UPDATE ON pedidos_teste_selo
  FOR EACH ROW EXECUTE FUNCTION pedidos_teste_selo_guarda();
CREATE TRIGGER pedidos_teste_selo_sem_delete BEFORE DELETE ON pedidos_teste_selo
  FOR EACH ROW EXECUTE FUNCTION extrato_recusa_mudanca();
CREATE TRIGGER pedidos_teste_selo_sem_truncate BEFORE TRUNCATE ON pedidos_teste_selo
  FOR EACH STATEMENT EXECUTE FUNCTION extrato_recusa_mudanca();

-- As ferramentas de acao que o selo cobre: as revisadas ativas de classe muda ou tem_volta; num terceiro, so as
-- publicadas na revisao do servidor vigente. Em ordem de id. Roda como o dono (o banco le o cadastro todo).
CREATE FUNCTION selo_acoes_do_sistema(p_sistema uuid) RETURNS uuid[] LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT coalesce(array_agg(f.id ORDER BY f.id), '{}'::uuid[])
  FROM ferramentas_revisadas f
  JOIN sistemas s ON s.id = f.sistema_id
  LEFT JOIN revisoes_servidor r ON r.id = s.revisao_servidor_id
  WHERE f.sistema_id = p_sistema AND f.situacao = 'ativa' AND f.classe IN ('muda', 'tem_volta')
    AND (s.origem = 'proprio' OR f.id = ANY (r.publicadas))
$$;

CREATE TABLE testes_selo (
  id                  uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  pedido_id           uuid        NOT NULL UNIQUE,
  sistema_id          uuid        NOT NULL REFERENCES sistemas (id),
  declaracao_id       uuid        NOT NULL,
  contrato            text        NOT NULL CHECK (contrato = 'selo/1'),
  bateria             text        NOT NULL CHECK (bateria ~ '^[0-9]+\.[0-9]+\.[0-9]+$'),
  nivel_pedido        smallint    NOT NULL CHECK (nivel_pedido IN (2, 3)),
  resultado           text        NOT NULL CHECK (resultado IN ('aprovado', 'reprovado')),
  nivel_obtido        smallint    NOT NULL CHECK (nivel_obtido IN (1, 2, 3)),
  sistema_endereco    text        NOT NULL,
  sistema_tipo_chave  text        NOT NULL,
  revisao_servidor_id uuid        REFERENCES revisoes_servidor (id),
  ferramentas         uuid[]      NOT NULL CHECK (cardinality(ferramentas) BETWEEN 1 AND 200
                                                  AND array_position(ferramentas, NULL) IS NULL),
  casos               jsonb       NOT NULL CHECK (jsonb_typeof(casos) = 'array'),
  nao_testado         jsonb       NOT NULL CHECK (jsonb_typeof(nao_testado) = 'array'),
  chamadas            integer     NOT NULL CHECK (chamadas >= 0),
  iniciado_em         timestamptz NOT NULL,
  testado_em          timestamptz NOT NULL DEFAULT now(),
  vale_ate            timestamptz NOT NULL,
  FOREIGN KEY (pedido_id, sistema_id) REFERENCES pedidos_teste_selo (id, sistema_id),
  FOREIGN KEY (declaracao_id, sistema_id) REFERENCES declaracoes_selo (id, sistema_id),
  UNIQUE (id, sistema_id),
  CHECK ((resultado = 'aprovado') = (nivel_obtido = nivel_pedido)),
  CHECK (resultado = 'aprovado' OR nivel_obtido = 1)
);
CREATE INDEX testes_selo_sistema ON testes_selo (sistema_id, testado_em DESC);

-- "So sobe com teste aprovado", no banco. A hora e a validade sao do banco: testado_em = now() e vale_ate = now() +
-- BADGE_TTL_DAYS da politica vigente (90 sem linha; fora da faixa de 1 a 365, o banco usa a borda). E o teste so
-- nasce de um pedido 'rodando' do mesmo sistema, nivel e declaracao, com a declaracao ainda a mais recente do sistema
-- (uma declaracao nova salva durante a bateria vence a antiga), num sistema ativo na 2026-07-28 com chave por pessoa,
-- com o retrato (endereco, tipo de chave, revisao do servidor e ferramentas de acao) igual ao de agora; o
-- alvo de teste da declaracao bem escrito e ainda fora da producao de todo sistema do cadastro (A4: a producao pode ter
-- mudado durante o teste); os casos na forma do contrato; o que nao foi testado igual a lista fixa; ao menos uma
-- chamada por tentativa (critica B3); e, aprovado, com todos os casos obrigatorios (selo_casos_aprovam). O banco
-- confere a forma dos casos, nao que a bateria chamou o sistema.
CREATE FUNCTION testes_selo_nasce() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  p record;
  s record;
  problema text;
  validade integer;
  alvo text;
BEGIN
  SELECT * INTO p FROM pedidos_teste_selo WHERE id = NEW.pedido_id;
  IF p.estado IS DISTINCT FROM 'rodando' OR p.sistema_id IS DISTINCT FROM NEW.sistema_id
     OR p.nivel IS DISTINCT FROM NEW.nivel_pedido OR p.declaracao_id IS DISTINCT FROM NEW.declaracao_id THEN
    RAISE EXCEPTION 'O teste do selo so nasce de um pedido rodando, do mesmo sistema, nivel e declaracao'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.iniciado_em < p.pedido_em OR NEW.iniciado_em > now() THEN
    RAISE EXCEPTION 'O teste do selo comecou depois do pedido e antes de agora' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.declaracao_id IS DISTINCT FROM (SELECT d.id FROM declaracoes_selo d WHERE d.sistema_id = NEW.sistema_id
                                         ORDER BY d.versao DESC LIMIT 1) THEN
    RAISE EXCEPTION 'O teste do selo usa a declaracao de teste mais recente do sistema'
      USING ERRCODE = 'check_violation';
  END IF;
  SELECT situacao, versao_mcp, endereco, tipo_chave, revisao_servidor_id INTO s FROM sistemas WHERE id = NEW.sistema_id;
  IF s.situacao IS DISTINCT FROM 'ativo' OR s.versao_mcp IS DISTINCT FROM '2026-07-28' THEN
    RAISE EXCEPTION 'O teste do selo so vale para sistema ativo na versao 2026-07-28' USING ERRCODE = 'check_violation';
  END IF;
  IF s.tipo_chave IS DISTINCT FROM 'por_pessoa' THEN
    RAISE EXCEPTION 'O selo de nivel 2 ou 3 e so para sistema com chave por pessoa' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.sistema_endereco IS DISTINCT FROM s.endereco OR NEW.sistema_tipo_chave IS DISTINCT FROM s.tipo_chave
     OR NEW.revisao_servidor_id IS DISTINCT FROM s.revisao_servidor_id THEN
    RAISE EXCEPTION 'O retrato do sistema no teste do selo nao bate com o sistema de agora (endereco, chave ou revisao)'
      USING ERRCODE = 'check_violation';
  END IF;
  alvo := (SELECT d.endereco_teste FROM declaracoes_selo d WHERE d.id = NEW.declaracao_id);
  IF NOT selo_alvo_bem_escrito(alvo) THEN
    RAISE EXCEPTION '%', selo_alvo_mal_escrito() USING ERRCODE = 'check_violation';
  END IF;
  IF selo_alvo_na_producao(alvo) THEN
    RAISE EXCEPTION 'O alvo de teste declarado e o endereco de producao de um sistema do cadastro; a bateria nao escreve na producao'
      USING ERRCODE = 'check_violation';
  END IF;
  IF cardinality(NEW.ferramentas) <> (SELECT count(DISTINCT x) FROM unnest(NEW.ferramentas) x)
     OR ARRAY(SELECT x FROM unnest(NEW.ferramentas) x ORDER BY x) <> selo_acoes_do_sistema(NEW.sistema_id) THEN
    RAISE EXCEPTION 'As ferramentas do teste do selo sao as ferramentas de acao ativas do sistema agora'
      USING ERRCODE = 'check_violation';
  END IF;
  problema := selo_casos_problema(NEW.casos, NEW.ferramentas);
  IF problema IS NOT NULL THEN
    RAISE EXCEPTION 'Os casos do teste do selo nao estao na forma do contrato (%)', problema
      USING ERRCODE = 'check_violation';
  END IF;
  IF (SELECT jsonb_agg(e ORDER BY e ->> 'item' COLLATE "C") FROM jsonb_array_elements(NEW.nao_testado) e)
     IS DISTINCT FROM selo_nao_testados() THEN
    RAISE EXCEPTION 'O que nao foi testado e sempre a lista do contrato' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.chamadas < (SELECT coalesce(sum((e ->> 'tentativas')::numeric), 0) FROM jsonb_array_elements(NEW.casos) e) THEN
    RAISE EXCEPTION 'O teste do selo tem menos chamadas que tentativas' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.resultado = 'aprovado' AND NOT selo_casos_aprovam(NEW.casos, NEW.ferramentas, NEW.nivel_pedido) THEN
    RAISE EXCEPTION 'Teste do selo aprovado sem todos os casos obrigatorios, ou com um caso que falhou'
      USING ERRCODE = 'check_violation';
  END IF;
  SELECT valor INTO validade FROM politica_vigente WHERE chave = 'BADGE_TTL_DAYS';
  NEW.testado_em := now();
  NEW.vale_ate := now() + make_interval(days => least(greatest(coalesce(validade, 90), 1), 365));
  RETURN NEW;
END $$;
CREATE TRIGGER testes_selo_nasce BEFORE INSERT ON testes_selo FOR EACH ROW EXECUTE FUNCTION testes_selo_nasce();
CREATE TRIGGER testes_selo_sem_update_delete BEFORE UPDATE OR DELETE ON testes_selo
  FOR EACH ROW EXECUTE FUNCTION extrato_recusa_mudanca();
CREATE TRIGGER testes_selo_sem_truncate BEFORE TRUNCATE ON testes_selo
  FOR EACH STATEMENT EXECUTE FUNCTION extrato_recusa_mudanca();
-- Fim do bloco TESTES.
"""

SQL_VIGENTE = r"""
-- Bloco VIGENTE. O selo e calculado na leitura (D8): nenhum papel grava "nivel 2", e o vencimento nao depende de
-- rotina.
-- Critica M1: o despachante marca, na mesma transacao da leitura das ferramentas (_listar), o que o sistema deixou
-- de listar (sumiu_em; nula quando a ferramenta volta). O UPDATE de tabela inteira do despachante em
-- ferramentas_vistas (0002) cobre a coluna nova. Linhas antigas ficam NULL.
ALTER TABLE ferramentas_vistas ADD COLUMN sumiu_em timestamptz;

-- As ferramentas do contrato conferem com os esquemas publicados (forma essencial, critica B4), calculado quando a
-- definicao e gravada, e nao a cada leitura do selo: a vitrine chama selo_vigente() por sistema, e a conferencia
-- copiava e lia a definicao guardada (ate o teto da resposta do tools/list, 8 MiB por pagina) a cada leitura. Coluna
-- gerada: ninguem a escreve, nem o despachante, que tem UPDATE na tabela inteira (0002); o banco a recalcula a cada
-- INSERT e a cada UPDATE de nome ou definicao, e calculou as linhas antigas ao criar a coluna. Nula fora das
-- ferramentas do contrato. Uma migracao que mude selo_esquema_essencial ou selo_esquemas recalcula a coluna
-- (UPDATE ferramentas_vistas SET definicao = definicao WHERE contrato_confere IS NOT NULL).
ALTER TABLE ferramentas_vistas ADD COLUMN contrato_confere boolean GENERATED ALWAYS AS (
  CASE WHEN nome = ANY (selo_vocabulario('ferramentas')) THEN
    coalesce(selo_esquema_essencial(definicao -> 'inputSchema')
               = selo_esquema_essencial(selo_esquemas(nome) -> 'inputSchema')
             AND selo_esquema_essencial(definicao -> 'outputSchema')
               = selo_esquema_essencial(selo_esquemas(nome) -> 'outputSchema'), false)
  END) STORED;

CREATE TABLE selos_retirados (
  id           uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  sistema_id   uuid        NOT NULL REFERENCES sistemas (id),
  teste_id     uuid        NOT NULL,
  motivo       text        NOT NULL CHECK (motivo = ANY (selo_vocabulario('motivos_de_retirada'))),
  detalhe      text        CHECK (detalhe IS NULL OR length(detalhe) BETWEEN 1 AND 500),  -- so o texto do ADMIN
  retirado_por text        NOT NULL CHECK (btrim(retirado_por) <> '' AND length(retirado_por) <= 200),
  pessoa_id    uuid        REFERENCES pessoas (id),
  retirado_em  timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (teste_id, sistema_id) REFERENCES testes_selo (id, sistema_id),
  CHECK ((motivo = 'admin') = (pessoa_id IS NOT NULL)),
  CHECK (motivo = 'admin' OR detalhe IS NULL)
);
CREATE INDEX selos_retirados_teste ON selos_retirados (teste_id);

-- Quem retira e por que (critica M10): a entrada so grava 'admin', de um ADMIN ativo, com o motivo dele e so do
-- selo vigente; quem retirou e o nome do ADMIN no cadastro (como sessao.quem), nunca o que o codigo manda; o
-- despachante nunca grava 'admin'. As retiradas automaticas tem a prova na mesma transacao, numa operacao do mesmo
-- sistema: 'se_contradisse', a resposta que contradiz (gravada pelo gatilho de informacoes_sistema);
-- 'nao_respondeu_consultas', uma consulta que acabou em falha do sistema (sem resposta, resposta invalida ou
-- desconhecido), nunca uma falha do MCP-CLARO (critica M7). O teste retirado e o do envio da prova ou o ultimo teste
-- do sistema (o que selo_vigente le): depois de um reteste, a prova num envio do teste antigo derruba o selo de agora
-- (D18, D19). Quantas operacoes seguidas e a conta do despachante, pelo banco.
CREATE FUNCTION selos_retirados_nasce() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v record;
  quem text;
  ultimo uuid;
BEGIN
  NEW.retirado_em := now();
  IF NEW.motivo = 'admin' THEN
    IF current_user = 'mcp_claro_despachante' THEN
      RAISE EXCEPTION 'O despachante nao retira o selo em nome do ADMIN' USING ERRCODE = 'insufficient_privilege';
    END IF;
    SELECT left(coalesce(nullif(btrim(p.nome), ''), p.sujeito), 200) INTO quem FROM pessoas p
    WHERE p.id = NEW.pessoa_id AND p.papel = 'admin' AND p.situacao = 'ativa';
    IF NOT FOUND THEN
      RAISE EXCEPTION 'So um ADMIN ativo retira o selo' USING ERRCODE = 'insufficient_privilege';
    END IF;
    NEW.retirado_por := quem;
    IF coalesce(btrim(NEW.detalhe), '') = '' THEN
      RAISE EXCEPTION 'Diga por que retirar o selo' USING ERRCODE = 'check_violation';
    END IF;
    SELECT * INTO v FROM selo_vigente(NEW.sistema_id);
    IF v.nivel < 2 OR v.teste_id IS DISTINCT FROM NEW.teste_id THEN
      RAISE EXCEPTION 'So se retira o selo vigente do sistema' USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
  END IF;
  IF current_user = 'mcp_claro_entrada' THEN
    RAISE EXCEPTION 'A entrada so retira o selo pelo ADMIN' USING ERRCODE = 'insufficient_privilege';
  END IF;
  NEW.retirado_por := 'mcp-claro';
  SELECT x.id INTO ultimo FROM testes_selo x WHERE x.sistema_id = NEW.sistema_id
  ORDER BY x.testado_em DESC, x.id DESC LIMIT 1;
  IF NEW.motivo = 'se_contradisse' THEN
    IF NOT EXISTS (SELECT 1 FROM informacoes_sistema i JOIN envios_selo e ON e.operacao_id = i.operacao_id
                   WHERE i.sistema_id = NEW.sistema_id AND (e.teste_id = NEW.teste_id OR NEW.teste_id = ultimo)
                     AND i.contradiz AND i.gravado_em = now()) THEN
      RAISE EXCEPTION 'A retirada por contradicao nasce da resposta que contradiz, na mesma transacao'
        USING ERRCODE = 'check_violation';
    END IF;
  ELSIF NOT EXISTS (SELECT 1 FROM informacoes_sistema i JOIN envios_selo e ON e.operacao_id = i.operacao_id
                    WHERE i.sistema_id = NEW.sistema_id AND (e.teste_id = NEW.teste_id OR NEW.teste_id = ultimo)
                      AND i.origem = 'consulta' AND i.gravado_em = now()
                      AND (i.desfecho = ANY (selo_vocabulario('desfechos_de_falha_do_sistema'))
                           OR (i.desfecho = 'respondeu' AND i.estado = 'desconhecido'))) THEN
    RAISE EXCEPTION 'A retirada por falta de resposta so nasce de uma consulta que o sistema nao respondeu, na mesma transacao'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER selos_retirados_nasce BEFORE INSERT ON selos_retirados
  FOR EACH ROW EXECUTE FUNCTION selos_retirados_nasce();
CREATE TRIGGER selos_retirados_sem_update_delete BEFORE UPDATE OR DELETE ON selos_retirados
  FOR EACH ROW EXECUTE FUNCTION extrato_recusa_mudanca();
CREATE TRIGGER selos_retirados_sem_truncate BEFORE TRUNCATE ON selos_retirados
  FOR EACH STATEMENT EXECUTE FUNCTION extrato_recusa_mudanca();

-- As ferramentas do contrato do nivel continuam listadas na producao: lidas ha menos de 48 horas (duas vezes o maior
-- TOOL_DRIFT_CHECK_SECONDS), sem sumir (critica M1), com a forma essencial dos esquemas igual a do contrato (critica
-- B4; ferramentas_vistas.contrato_confere, calculada ao gravar). A descricao e livre. Roda como o dono.
CREATE FUNCTION selo_contrato_listado(p_sistema uuid, p_nivel smallint, p_agora timestamptz) RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT p_nivel IN (2, 3) AND NOT EXISTS (
    SELECT 1
    FROM unnest(CASE WHEN p_nivel = 3 THEN ARRAY['selo_alvo', 'selo_consultar', 'selo_bloqueio']
                     ELSE ARRAY['selo_alvo', 'selo_consultar'] END) AS n(nome)
    WHERE NOT EXISTS (
      SELECT 1 FROM ferramentas_vistas v
      WHERE v.sistema_id = p_sistema AND v.nome = n.nome
        AND (v.sumiu_em IS NULL OR v.sumiu_em > p_agora)
        AND v.vista_em > p_agora - interval '48 hours'
        AND v.contrato_confere))
$$;

-- O selo de agora. Vale o ultimo teste do sistema (por testado_em e id). O motivo sai na primeira regra que valer,
-- nesta ordem; so 'vigente' tem o nivel do teste, todos os outros dao nivel 1. p_agora existe para o teste provar
-- o vencimento sem esperar 90 dias: o codigo de producao nunca o passa. Roda como o dono (critica M10): os papeis
-- so tem EXECUTE, e a visao do Grafana passa por medicoes.contagem_selos.
CREATE FUNCTION selo_vigente(p_sistema uuid, p_agora timestamptz DEFAULT now())
RETURNS TABLE (nivel smallint, teste_id uuid, testado_em timestamptz, vale_ate timestamptz, modo text, motivo text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  s record;
  t record;
  m text;
BEGIN
  SELECT x.situacao, x.versao_mcp, x.endereco, x.tipo_chave, x.revisao_servidor_id INTO s
  FROM sistemas x WHERE x.id = p_sistema;
  SELECT x.* INTO t FROM testes_selo x
  WHERE x.sistema_id = p_sistema AND x.testado_em <= p_agora ORDER BY x.testado_em DESC, x.id DESC LIMIT 1;
  IF NOT FOUND THEN
    RETURN QUERY SELECT 1::smallint, NULL::uuid, NULL::timestamptz, NULL::timestamptz, NULL::text, 'nunca_testado'::text;
    RETURN;
  END IF;
  m := CASE
    WHEN t.resultado <> 'aprovado' THEN 'reprovado'
    WHEN t.vale_ate <= p_agora THEN 'vencido'
    WHEN EXISTS (SELECT 1 FROM selos_retirados r WHERE r.teste_id = t.id AND r.retirado_em <= p_agora) THEN 'retirado'
    WHEN s.situacao IS DISTINCT FROM 'ativo' OR s.versao_mcp IS DISTINCT FROM '2026-07-28' THEN 'fora_de_ativo'
    WHEN s.endereco IS DISTINCT FROM t.sistema_endereco THEN 'endereco_mudou'
    WHEN s.tipo_chave IS DISTINCT FROM t.sistema_tipo_chave THEN 'tipo_chave_mudou'
    WHEN s.revisao_servidor_id IS DISTINCT FROM t.revisao_servidor_id THEN 'revisao_nova'
    WHEN NOT (selo_acoes_do_sistema(p_sistema) <@ t.ferramentas) THEN 'ferramenta_nova'
    WHEN NOT selo_contrato_listado(p_sistema, t.nivel_obtido, p_agora) THEN 'contrato_sumiu'
    ELSE 'vigente'
  END;
  RETURN QUERY SELECT CASE WHEN m = 'vigente' THEN t.nivel_obtido ELSE 1::smallint END, t.id, t.testado_em, t.vale_ate,
                      (SELECT d.modo FROM declaracoes_selo d WHERE d.id = t.declaracao_id), m;
END $$;

CREATE VIEW selos_vigentes AS
  SELECT s.id AS sistema_id, v.nivel, v.teste_id, v.testado_em, v.vale_ate, v.modo, v.motivo
  FROM sistemas s CROSS JOIN LATERAL selo_vigente(s.id) v;
COMMENT ON VIEW selos_vigentes IS 'O selo de agora de cada sistema, calculado por selo_vigente() (nivel 1 fora de vigente).';

-- O selo vigente sai de selo_vigente(); sistemas.selo fica em 1 para o codigo antigo e para ninguem criar uma
-- segunda verdade (D8). Nenhum papel tem UPDATE nela (0002); um valor que o dono tenha posto a mao volta a 1.
UPDATE sistemas SET selo = 1 WHERE selo <> 1;
ALTER TABLE sistemas ADD CONSTRAINT sistemas_selo_fixo CHECK (selo = 1);
COMMENT ON COLUMN sistemas.selo IS
  'O selo vigente sai de selo_vigente(); esta coluna fica em 1 para o codigo antigo e para ninguem criar uma segunda verdade.';
-- Fim do bloco VIGENTE.
"""

SQL_RESERVADAS = r"""
-- Bloco RESERVADAS. As ferramentas do contrato (selo_alvo, selo_consultar e selo_bloqueio) nunca sao revisadas nem
-- publicadas na vitrine (D6). Uma revisada ativa com um desses nomes, de antes desta migracao, para a migracao com uma
-- frase legivel: suspenda a ferramenta antes de migrar.
DO $$
DECLARE nomes text;
BEGIN
  SELECT string_agg(s.nome || ' (' || f.nome_original || ')', ', ' ORDER BY s.nome, f.nome_original) INTO nomes
  FROM ferramentas_revisadas f JOIN sistemas s ON s.id = f.sistema_id
  WHERE f.situacao = 'ativa' AND f.nome_original = ANY (selo_vocabulario('ferramentas'));
  IF nomes IS NOT NULL THEN
    RAISE EXCEPTION USING ERRCODE = 'check_violation', MESSAGE = format(
      'Ferramentas revisadas ativas com nome reservado ao contrato do selo: %s. Suspenda essas ferramentas %s', nomes,
      'antes de migrar; o MCP-CLARO nao poe essas ferramentas na vitrine.');
  END IF;
END $$;

-- Mesmo corpo da 0006, com a recusa dos nomes reservados antes.
CREATE OR REPLACE FUNCTION ferramentas_revisadas_nasce() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF NEW.nome_original = ANY (selo_vocabulario('ferramentas')) THEN
    RAISE EXCEPTION 'Este nome e reservado ao contrato do selo; o MCP-CLARO nao poe essa ferramenta na vitrine'
      USING ERRCODE = 'check_violation';
  END IF;
  IF EXISTS (SELECT 1 FROM sistemas WHERE id = NEW.sistema_id AND origem = 'terceiro') THEN
    NEW.texto_de_fora := true;
    NEW.etiquetas := '{}'::jsonb;
  END IF;
  RETURN NEW;
END $$;
-- Fim do bloco RESERVADAS.
"""

SQL_ENVIO = r"""
-- Bloco ENVIO. O pedido igual a uma "nao confirmada" leva o numero dela (D16): a entrada preenche
-- repete_operacao_id, e o banco confere. So para original "nao confirmada", sem a decisao 'aconteceu' da pessoa, com
-- os mesmos argumentos (codigo de conferencia) e a mesma chave do pedido igual, de ate 7 dias, e que saiu com
-- numero (envios_selo, critica A1). A coluna nova entra no INSERT de tabela inteira da entrada (0005). Critica M4: com
-- desconhecido no fim da escada, a entrada so faz o pedido igual depois da confirmacao explicita da pessoa na ficha
-- ("o sistema nao sabe se o primeiro foi feito; com numero novo, pode fazer duas vezes") e sem repete_operacao_id:
-- numero novo, por escolha dela.
ALTER TABLE operacoes
  ADD COLUMN repete_operacao_id uuid REFERENCES operacoes (id),
  ADD CONSTRAINT operacoes_repete_check CHECK (repete_operacao_id IS DISTINCT FROM id);
CREATE INDEX operacoes_repete ON operacoes (repete_operacao_id) WHERE repete_operacao_id IS NOT NULL;

CREATE FUNCTION operacoes_repete_confere() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  o record;
BEGIN
  IF NEW.repete_operacao_id IS NULL THEN
    RETURN NEW;
  END IF;
  SELECT x.chave_pedido_igual, x.codigo_conferencia, x.sistema_id, x.pessoa_id, x.criada_em INTO o
  FROM operacoes x WHERE x.id = NEW.repete_operacao_id;
  IF NOT FOUND OR o.chave_pedido_igual <> NEW.chave_pedido_igual OR o.codigo_conferencia <> NEW.codigo_conferencia
     OR o.sistema_id <> NEW.sistema_id OR o.pessoa_id <> NEW.pessoa_id OR o.criada_em < now() - interval '7 days'
     OR NOT EXISTS (SELECT 1 FROM fila_saida f WHERE f.operacao_id = NEW.repete_operacao_id AND f.estado = 'nao_confirmada')
     OR EXISTS (SELECT 1 FROM decisoes_nao_confirmada d
                WHERE d.operacao_id = NEW.repete_operacao_id AND d.decisao = 'aconteceu')
     OR NOT EXISTS (SELECT 1 FROM envios_selo e WHERE e.operacao_id = NEW.repete_operacao_id) THEN
    RAISE EXCEPTION 'O pedido igual so repete o numero de uma acao nao confirmada que saiu com numero, com os mesmos argumentos, de ate 7 dias e sem a decisao aconteceu'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER operacoes_repete_confere BEFORE INSERT ON operacoes
  FOR EACH ROW EXECUTE FUNCTION operacoes_repete_confere();

-- O numero que vai ao sistema (D4): um uuid aleatorio do banco, nunca o protocolo. So de acrescentar; o despachante
-- grava na transacao que passa a acao de 'liberada' para 'enviando'. repete_operacao_id nao tem chave estrangeira
-- para envios_selo (critica A1): uma original sem envio nunca e erro, vira numero novo.
CREATE TABLE envios_selo (
  operacao_id        uuid        PRIMARY KEY REFERENCES operacoes (id),
  sistema_id         uuid        NOT NULL REFERENCES sistemas (id),
  nivel              smallint    NOT NULL CHECK (nivel IN (2, 3)),
  teste_id           uuid        NOT NULL,
  chave              uuid        NOT NULL,
  repete_operacao_id uuid        REFERENCES operacoes (id),
  gravado_em         timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (teste_id, sistema_id) REFERENCES testes_selo (id, sistema_id),
  CHECK (repete_operacao_id IS DISTINCT FROM operacao_id)
);
CREATE INDEX envios_selo_chave ON envios_selo (chave);
CREATE INDEX envios_selo_repete ON envios_selo (repete_operacao_id) WHERE repete_operacao_id IS NOT NULL;

-- O envio nasce da fila 'liberada', do sistema da operacao, com o nivel e o teste do selo vigente agora (o codigo
-- nao sobe o nivel). A chave e do banco: o codigo manda NULL. Com o pedido igual (operacoes.repete_operacao_id), o
-- banco copia a chave do envio original, do mesmo sistema, de ate 7 dias, salvo se o sistema ja disse sobre esse
-- numero algo que o queima (recusada, parada ou desfeita, ou nao_recebida confirmada, criticas M4 e A3.3): ai, numero
-- novo. As respostas contam pelo numero (a original e todo pedido igual que saiu com ele). Uma nao_recebida sozinha
-- nao queima (selo_nao_recebida_confirmada): pode ser a de um sistema que registra o numero so no fim da escrita, e
-- um numero novo faria a acao duas vezes; com o mesmo numero, o sistema honesto devolve o desfecho da primeira (ou
-- nao_recebida de novo, e a segunda resposta confirma). Uma original sem envio tambem da numero novo, nunca erro
-- (critica A1). Na porta, este passo roda num SAVEPOINT (ajustes D22): um erro aqui faz a acao sair sem numero, com
-- acao_enviada_sem_numero no extrato.
CREATE FUNCTION envios_selo_nasce() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  o record;
  v record;
  original record;
  queimado boolean;
BEGIN
  NEW.gravado_em := now();
  IF NEW.chave IS NOT NULL THEN
    RAISE EXCEPTION 'O numero da operacao e do banco; o codigo nao o escolhe' USING ERRCODE = 'insufficient_privilege';
  END IF;
  SELECT x.sistema_id, x.repete_operacao_id INTO o FROM operacoes x WHERE x.id = NEW.operacao_id;
  IF NOT FOUND OR NOT EXISTS (SELECT 1 FROM fila_saida f WHERE f.operacao_id = NEW.operacao_id AND f.estado = 'liberada') THEN
    RAISE EXCEPTION 'O envio com numero nasce na transacao que passa a acao de liberada para enviando'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.sistema_id IS DISTINCT FROM o.sistema_id THEN
    RAISE EXCEPTION 'O envio com numero e do sistema da operacao' USING ERRCODE = 'check_violation';
  END IF;
  SELECT * INTO v FROM selo_vigente(o.sistema_id);
  IF v.nivel < 2 OR NEW.nivel IS DISTINCT FROM v.nivel OR NEW.teste_id IS DISTINCT FROM v.teste_id THEN
    RAISE EXCEPTION 'O nivel e o teste do envio sao os do selo vigente do sistema agora' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.repete_operacao_id IS NOT NULL AND NEW.repete_operacao_id IS DISTINCT FROM o.repete_operacao_id THEN
    RAISE EXCEPTION 'O envio repete a operacao que a propria operacao repete' USING ERRCODE = 'check_violation';
  END IF;
  NEW.repete_operacao_id := NULL;
  NEW.chave := gen_random_uuid();
  IF o.repete_operacao_id IS NOT NULL THEN
    SELECT e.sistema_id, e.chave, e.gravado_em INTO original FROM envios_selo e WHERE e.operacao_id = o.repete_operacao_id;
    IF FOUND AND original.sistema_id = NEW.sistema_id AND original.gravado_em >= now() - interval '7 days' THEN
      queimado := EXISTS (
        SELECT 1 FROM informacoes_sistema i JOIN envios_selo e ON e.operacao_id = i.operacao_id
        WHERE e.chave = original.chave AND i.desfecho = 'respondeu'
          AND i.estado = ANY (selo_vocabulario('estados_que_queimam')) AND i.estado <> 'nao_recebida')
        OR selo_nao_recebida_confirmada(ARRAY(
          SELECT i.gravado_em FROM informacoes_sistema i JOIN envios_selo e ON e.operacao_id = i.operacao_id
          WHERE e.chave = original.chave AND i.desfecho = 'respondeu' AND i.estado = 'nao_recebida'));
      IF NOT queimado THEN
        NEW.repete_operacao_id := o.repete_operacao_id;
        NEW.chave := original.chave;
      END IF;
    END IF;
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER envios_selo_nasce BEFORE INSERT ON envios_selo FOR EACH ROW EXECUTE FUNCTION envios_selo_nasce();
CREATE TRIGGER envios_selo_sem_update_delete BEFORE UPDATE OR DELETE ON envios_selo
  FOR EACH ROW EXECUTE FUNCTION extrato_recusa_mudanca();
CREATE TRIGGER envios_selo_sem_truncate BEFORE TRUNCATE ON envios_selo
  FOR EACH STATEMENT EXECUTE FUNCTION extrato_recusa_mudanca();

-- Critica M4: quando um pedido igual R saiu com o numero da original O, O para de ser consultado e o que o sistema
-- informar aparece em R. Esta funcao segue os envios que repetem p_operacao (repete_operacao_id so fica preenchido
-- quando o banco copiou o numero) e diz o ultimo da cadeia, ou NULL. Roda como o dono, como selo_vigente.
CREATE FUNCTION selo_substituto(p_operacao uuid) RETURNS uuid LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, public, pg_temp AS $$
  WITH RECURSIVE cadeia (operacao_id, passo) AS (
    SELECT p_operacao, 0
    UNION ALL
    SELECT e.operacao_id, c.passo + 1 FROM envios_selo e JOIN cadeia c ON e.repete_operacao_id = c.operacao_id
    WHERE c.passo < 100
  )
  SELECT operacao_id FROM cadeia WHERE passo > 0 ORDER BY passo DESC, operacao_id DESC LIMIT 1
$$;
-- Fim do bloco ENVIO.
"""

SQL_AVISOS_BLOQUEIO = r"""
-- Bloco AVISOS_BLOQUEIO (critica A1, ajustes D22). O aviso SAI da transacao do bloqueio: o bloqueio continua o da
-- Fase 2, sem gatilho novo. Uma tarefa do despachante deriva os avisos de cada comprovante novo (que ja nao muda) e
-- de envios_selo, e marca o bloqueio em avisos_bloqueio_derivados na mesma transacao, mesmo com nenhum aviso. Um
-- aviso por bloqueio, sistema, pessoa (so chave por pessoa, critica B7) e parte de ate 100 numeros. O id do aviso vai
-- ao sistema como o "bloqueio". Com a porta fechada o aviso espera ('pendente', sem gastar tentativa): nada sai com
-- a porta fechada (ajustes D30/A2).
CREATE TABLE avisos_bloqueio (
  id          uuid        PRIMARY KEY DEFAULT gen_random_uuid(),
  bloqueio_id uuid        NOT NULL REFERENCES bloqueios (id),
  sistema_id  uuid        NOT NULL REFERENCES sistemas (id),
  pessoa_id   uuid        NOT NULL REFERENCES pessoas (id),
  parte       smallint    NOT NULL DEFAULT 1 CHECK (parte >= 1),
  operacoes   uuid[]      NOT NULL CHECK (cardinality(operacoes) BETWEEN 1 AND 100
                                          AND array_position(operacoes, NULL) IS NULL),
  estado      text        NOT NULL DEFAULT 'pendente' CHECK (estado = ANY (selo_vocabulario('estados_do_aviso'))),
  tentativas  smallint    NOT NULL DEFAULT 0 CHECK (tentativas BETWEEN 0 AND 10),
  proxima_em  timestamptz NOT NULL DEFAULT now(),
  criado_em   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (bloqueio_id, sistema_id, pessoa_id, parte)
);
CREATE INDEX avisos_bloqueio_pendentes ON avisos_bloqueio (proxima_em) WHERE estado = 'pendente';

-- Cada operacao do aviso saiu com numero de nivel 3 por este sistema, e da pessoa do aviso, e esta no comprovante do
-- bloqueio (ja saidas ou em conferencia, pelo protocolo) ou, sendo do alcance do bloqueio, enviada ate 30 dias antes
-- dele, tem o ultimo estado informado em aberto (em_andamento ou desconhecido, critica B9). Cada numero vai uma vez so
-- em todos os avisos do bloqueio (o original e o pedido igual tem o mesmo, critica B12). So um bloqueio avisa: a
-- pausa nao pede ao sistema que pare nem queima numeros (decisao do coordenador na etapa 1; o despachante filtra pelo
-- tipo e marca a pausa em avisos_bloqueio_derivados com nenhum aviso).
CREATE FUNCTION avisos_bloqueio_nasce() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  b record;
  fora uuid;
BEGIN
  IF NEW.estado IS DISTINCT FROM 'pendente' OR NEW.tentativas <> 0 THEN
    RAISE EXCEPTION 'Um aviso de bloqueio nasce pendente, sem tentativa' USING ERRCODE = 'check_violation';
  END IF;
  NEW.criado_em := now();
  SELECT x.tipo, x.alcance, x.pessoa_id, x.aplicativo_id, x.sistema_id, x.criado_em INTO b FROM bloqueios x
  WHERE x.id = NEW.bloqueio_id;
  IF b.tipo IS DISTINCT FROM 'bloqueio' THEN
    RAISE EXCEPTION 'So um bloqueio avisa os sistemas; uma pausa nao' USING ERRCODE = 'check_violation';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM comprovantes c WHERE c.bloqueio_id = NEW.bloqueio_id) THEN
    RAISE EXCEPTION 'O aviso de bloqueio nasce do comprovante do bloqueio' USING ERRCODE = 'check_violation';
  END IF;
  IF cardinality(NEW.operacoes) <> (SELECT count(DISTINCT x) FROM unnest(NEW.operacoes) x) THEN
    RAISE EXCEPTION 'Operacao repetida no aviso de bloqueio' USING ERRCODE = 'check_violation';
  END IF;
  SELECT x INTO fora FROM unnest(NEW.operacoes) x
  WHERE NOT EXISTS (
    SELECT 1 FROM envios_selo e JOIN operacoes o ON o.id = e.operacao_id
    WHERE e.operacao_id = x AND e.nivel = 3 AND e.sistema_id = NEW.sistema_id AND o.pessoa_id = NEW.pessoa_id
      AND (EXISTS (SELECT 1 FROM comprovantes c, jsonb_array_elements(c.ja_saidas || c.em_conferencia) p
                   WHERE c.bloqueio_id = NEW.bloqueio_id AND p ->> 'protocolo' = o.protocolo)
           OR (e.gravado_em >= b.criado_em - interval '30 days' AND e.gravado_em <= b.criado_em
               AND CASE b.alcance
                     WHEN 'pessoa' THEN o.pessoa_id = b.pessoa_id
                     WHEN 'aplicativo' THEN o.aplicativo_id = b.aplicativo_id
                     WHEN 'pessoa_aplicativo' THEN o.pessoa_id = b.pessoa_id AND o.aplicativo_id = b.aplicativo_id
                     WHEN 'sistema' THEN o.sistema_id = b.sistema_id
                     ELSE true
                   END
               AND (SELECT i.estado FROM informacoes_sistema i
                    WHERE i.operacao_id = e.operacao_id AND i.desfecho = 'respondeu'
                    ORDER BY i.gravado_em DESC, i.id DESC LIMIT 1) = ANY (selo_vocabulario('estados_em_aberto')))))
  LIMIT 1;
  IF fora IS NOT NULL THEN
    RAISE EXCEPTION 'O aviso de bloqueio so leva operacao de nivel 3 deste sistema e desta pessoa, do comprovante do bloqueio ou em aberto no alcance dele (%)', fora
      USING ERRCODE = 'check_violation';
  END IF;
  IF (SELECT count(DISTINCT e.chave) FROM envios_selo e WHERE e.operacao_id = ANY (NEW.operacoes))
       <> cardinality(NEW.operacoes)
     OR EXISTS (SELECT 1 FROM avisos_bloqueio a CROSS JOIN LATERAL unnest(a.operacoes) y
                JOIN envios_selo antes ON antes.operacao_id = y
                JOIN envios_selo agora ON agora.chave = antes.chave
                WHERE a.bloqueio_id = NEW.bloqueio_id AND agora.operacao_id = ANY (NEW.operacoes)) THEN
    RAISE EXCEPTION 'Cada numero vai uma vez so nos avisos de um bloqueio' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER avisos_bloqueio_nasce BEFORE INSERT ON avisos_bloqueio
  FOR EACH ROW EXECUTE FUNCTION avisos_bloqueio_nasce();

-- Depois de nascer, so estado, tentativas e proxima_em mudam; tentativas so sobe, uma por vez (a tarefa reserva a vez
-- antes de chamar o sistema, critica M8), e 'respondido' ou 'sem_resposta' so depois de uma vez reservada, porque so
-- se chama o sistema depois de reservar; 'sistema_fora' e 'bloqueio_encerrado' nao chamam e nao pedem tentativa; de
-- um estado final nada muda.
CREATE FUNCTION avisos_bloqueio_guarda() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  IF ROW(NEW.id, NEW.bloqueio_id, NEW.sistema_id, NEW.pessoa_id, NEW.parte, NEW.operacoes, NEW.criado_em)
     IS DISTINCT FROM ROW(OLD.id, OLD.bloqueio_id, OLD.sistema_id, OLD.pessoa_id, OLD.parte, OLD.operacoes, OLD.criado_em)
  THEN
    RAISE EXCEPTION 'Num aviso de bloqueio so o estado, as tentativas e a proxima vez mudam'
      USING ERRCODE = 'check_violation';
  END IF;
  IF OLD.estado <> 'pendente' THEN
    RAISE EXCEPTION 'O aviso de bloqueio ja terminou (%); nada muda', OLD.estado USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.tentativas < OLD.tentativas OR NEW.tentativas > OLD.tentativas + 1 THEN
    RAISE EXCEPTION 'As tentativas do aviso de bloqueio so sobem, uma por vez' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.estado IN ('respondido', 'sem_resposta') AND NEW.tentativas < 1 THEN
    RAISE EXCEPTION 'O aviso de bloqueio so fica respondido ou sem resposta depois de uma tentativa'
      USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER avisos_bloqueio_guarda BEFORE UPDATE ON avisos_bloqueio
  FOR EACH ROW EXECUTE FUNCTION avisos_bloqueio_guarda();
CREATE TRIGGER avisos_bloqueio_sem_delete BEFORE DELETE ON avisos_bloqueio
  FOR EACH ROW EXECUTE FUNCTION extrato_recusa_mudanca();
CREATE TRIGGER avisos_bloqueio_sem_truncate BEFORE TRUNCATE ON avisos_bloqueio
  FOR EACH STATEMENT EXECUTE FUNCTION extrato_recusa_mudanca();

-- Os bloqueios cujos avisos ja nasceram (talvez nenhum): a tarefa do despachante so olha os comprovantes que nao estao
-- aqui. A linha nasce na mesma transacao dos avisos, com a contagem deles; duas copias do despachante nao derivam o
-- mesmo bloqueio duas vezes (chave primaria). So de acrescentar.
CREATE TABLE avisos_bloqueio_derivados (
  bloqueio_id uuid        PRIMARY KEY REFERENCES bloqueios (id),
  avisos      integer     NOT NULL CHECK (avisos >= 0),
  derivado_em timestamptz NOT NULL DEFAULT now()
);

CREATE FUNCTION avisos_bloqueio_derivados_nasce() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
BEGIN
  NEW.derivado_em := now();
  IF NOT EXISTS (SELECT 1 FROM comprovantes c WHERE c.bloqueio_id = NEW.bloqueio_id) THEN
    RAISE EXCEPTION 'Os avisos de bloqueio nascem do comprovante do bloqueio' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.avisos <> (SELECT count(*) FROM avisos_bloqueio a WHERE a.bloqueio_id = NEW.bloqueio_id) THEN
    RAISE EXCEPTION 'A contagem dos avisos derivados e a dos avisos gravados do bloqueio' USING ERRCODE = 'check_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER avisos_bloqueio_derivados_nasce BEFORE INSERT ON avisos_bloqueio_derivados
  FOR EACH ROW EXECUTE FUNCTION avisos_bloqueio_derivados_nasce();
CREATE TRIGGER avisos_bloqueio_derivados_sem_update_delete BEFORE UPDATE OR DELETE ON avisos_bloqueio_derivados
  FOR EACH ROW EXECUTE FUNCTION extrato_recusa_mudanca();
CREATE TRIGGER avisos_bloqueio_derivados_sem_truncate BEFORE TRUNCATE ON avisos_bloqueio_derivados
  FOR EACH STATEMENT EXECUTE FUNCTION extrato_recusa_mudanca();

-- Os comprovantes de antes desta migracao nao tem envio com numero: nenhum aviso.
INSERT INTO avisos_bloqueio_derivados (bloqueio_id, avisos) SELECT bloqueio_id, 0 FROM comprovantes;
-- Fim do bloco AVISOS_BLOQUEIO.
"""

SQL_INFORMACOES = r"""
-- Bloco INFORMACOES. O que o sistema informou, sempre em resposta a uma pergunta do gateway (D20): o recibo da
-- propria chamada, a consulta e a resposta ao aviso de bloqueio. So as categorias do contrato e a hora do gateway
-- (D21): nenhum texto do sistema. sistema_fora (critica A2): o sistema nao estava ativo, a porta estava fechada ou,
-- num terceiro, a revisao nao era a vigente; nada foi perguntado. So de acrescentar.
CREATE TABLE informacoes_sistema (
  id          bigint      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  operacao_id uuid        NOT NULL REFERENCES envios_selo (operacao_id),
  sistema_id  uuid        NOT NULL REFERENCES sistemas (id),
  origem      text        NOT NULL CHECK (origem = ANY (selo_vocabulario('origens'))),
  aviso_id    uuid        REFERENCES avisos_bloqueio (id),
  desfecho    text        NOT NULL CHECK (desfecho = ANY (selo_vocabulario('desfechos'))),
  estado      text        CHECK (estado IS NULL OR estado = ANY (selo_vocabulario('estados'))),
  repetida    boolean     NOT NULL DEFAULT false,
  objetos     smallint    CHECK (objetos IS NULL OR objetos BETWEEN 0 AND 1000),
  -- O que aconteceu depois, como o sistema informou na consulta: ate 20 eventos e ate o teto de uma resposta do
  -- contrato (16 KiB, contrato.TETO_RESPOSTA_BYTES), para o gatilho abaixo, que confere evento por evento, ter custo
  -- com teto mesmo diante de um defeito de quem grava.
  depois      jsonb       NOT NULL DEFAULT '[]'::jsonb
              CHECK (CASE WHEN jsonb_typeof(depois) = 'array'
                          THEN jsonb_array_length(depois) <= 20 AND pg_column_size(depois) <= 16384
                          ELSE false END),
  -- Forcada pelo gatilho: a resposta nao combina com uma anterior sobre o mesmo numero (envios_selo.chave): a propria
  -- operacao e o pedido igual que saiu com o numero dela (critica M2).
  contradiz   boolean     NOT NULL DEFAULT false,
  gravado_em  timestamptz NOT NULL DEFAULT now(),
  CHECK ((desfecho = 'respondeu') = (estado IS NOT NULL)),
  CHECK ((origem = 'aviso') = (aviso_id IS NOT NULL)),
  -- O recibo so existe quando a resposta chegou: valido ou nao.
  CHECK (origem <> 'recibo' OR desfecho IN ('respondeu', 'resposta_invalida')),
  CHECK (origem <> 'recibo' OR estado IS NULL OR estado = ANY (selo_vocabulario('estados_do_recibo'))),
  CHECK (NOT repetida OR (origem = 'recibo' AND desfecho = 'respondeu')),
  CHECK (objetos IS NULL OR (origem = 'recibo' AND desfecho = 'respondeu')),
  CHECK (depois = '[]'::jsonb OR (origem = 'consulta' AND desfecho = 'respondeu')),
  CHECK (desfecho = 'respondeu' OR NOT contradiz)
);
CREATE INDEX informacoes_sistema_operacao ON informacoes_sistema (operacao_id, gravado_em DESC);

-- A hora e a do banco; o sistema e o do envio; o aviso e deste sistema e leva a operacao, e ja chamou o sistema: uma
-- vez reservada (tentativas >= 1) e fora de 'sistema_fora' e 'bloqueio_encerrado', que por definicao nunca chamam.
-- depois: so {evento, em}, com o evento do vocabulario e a hora entre o envio menos 5 minutos e agora mais 5 minutos.
-- A contradicao e contra toda resposta anterior 'respondeu' sobre o mesmo NUMERO (envios_selo.chave), pelas passagens
-- permitidas (selo_passagem_permitida): a original e o pedido igual que saiu com o numero dela sao, para o sistema, a
-- mesma escrita (D16, critica M4), e uma resposta sobre um nao combina com a resposta sobre o outro como se fossem
-- sobre a mesma operacao. Duas respostas sobre o mesmo numero em transacoes ao mesmo tempo (o recibo e a resposta ao
-- aviso, critica M2, ou a consulta sobre a original e o recibo do pedido igual) nao se veriam, e o sistema que se
-- contradiz ficaria com o selo: antes da conferencia, a transacao toma a trava consultiva do sistema
-- (pg_advisory_xact_lock, ate o fim da transacao), e a segunda espera a primeira terminar. Em READ COMMITTED (o
-- padrao do MCP-CLARO), a consulta seguinte ja ve a linha confirmada da outra. A trava e por sistema, nao por numero:
-- uma transacao que grava varias linhas (a resposta ao aviso leva varios numeros, todos do mesmo sistema) toma uma
-- trava so. Duas transacoes que gravassem respostas de dois sistemas em ordens opostas poderiam se esperar em ciclo:
-- por isso o contrato (selo/contrato.py, ESCADA_DA_CONSULTA_MINUTOS) manda cada transacao gravar respostas de um
-- sistema so, e por ultimo. A retirada que segue (informacoes_sistema_contradicao) tambem fica em fila.
CREATE FUNCTION informacoes_sistema_nasce() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  e record;
  a record;
  item jsonb;
  quando timestamptz;
BEGIN
  NEW.gravado_em := now();
  SELECT x.sistema_id, x.chave, x.gravado_em INTO e FROM envios_selo x WHERE x.operacao_id = NEW.operacao_id;
  IF NEW.sistema_id IS DISTINCT FROM e.sistema_id THEN
    RAISE EXCEPTION 'A informacao e do sistema do envio' USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.aviso_id IS NOT NULL THEN
    SELECT x.estado, x.tentativas INTO a FROM avisos_bloqueio x
    WHERE x.id = NEW.aviso_id AND x.sistema_id = NEW.sistema_id AND NEW.operacao_id = ANY (x.operacoes);
    IF NOT FOUND THEN
      RAISE EXCEPTION 'A resposta ao aviso e de uma operacao do proprio aviso' USING ERRCODE = 'check_violation';
    END IF;
    IF a.tentativas < 1 OR a.estado IN ('sistema_fora', 'bloqueio_encerrado') THEN
      RAISE EXCEPTION 'A resposta ao aviso so vem depois de o aviso chamar o sistema (vez reservada, sem sistema_fora nem bloqueio_encerrado)'
        USING ERRCODE = 'check_violation';
    END IF;
  END IF;
  -- Os tetos de depois antes do laco (o CHECK da tabela so roda depois deste gatilho): ate 20 eventos e 16 KiB.
  IF jsonb_typeof(NEW.depois) IS DISTINCT FROM 'array' OR jsonb_array_length(NEW.depois) > 20
     OR pg_column_size(NEW.depois) > 16384 THEN
    RAISE EXCEPTION 'depois e uma lista de eventos (ate 20, com ate 16 KiB)' USING ERRCODE = 'check_violation';
  END IF;
  FOR item IN SELECT x FROM jsonb_array_elements(NEW.depois) x LOOP
    IF jsonb_typeof(item) <> 'object'
       OR (SELECT array_agg(k ORDER BY k COLLATE "C") FROM jsonb_object_keys(item) k) IS DISTINCT FROM ARRAY['em', 'evento']
       OR jsonb_typeof(item -> 'evento') <> 'string' OR jsonb_typeof(item -> 'em') <> 'string' THEN
      RAISE EXCEPTION 'Cada evento de depois e {evento, em}' USING ERRCODE = 'check_violation';
    END IF;
    IF NOT ((item ->> 'evento') = ANY (selo_vocabulario('eventos'))) THEN
      RAISE EXCEPTION 'Evento de depois fora do vocabulario do contrato' USING ERRCODE = 'check_violation';
    END IF;
    BEGIN
      quando := (item ->> 'em')::timestamptz;
    EXCEPTION WHEN data_exception THEN
      RAISE EXCEPTION 'A hora de um evento de depois nao e uma data e hora' USING ERRCODE = 'check_violation';
    END;
    IF quando < e.gravado_em - interval '5 minutes' OR quando > now() + interval '5 minutes' THEN
      RAISE EXCEPTION 'A hora de um evento de depois fica entre o envio e agora' USING ERRCODE = 'check_violation';
    END IF;
  END LOOP;
  PERFORM pg_advisory_xact_lock(hashtext('mcp_claro.selo.informacoes'), hashtext(NEW.sistema_id::text));
  NEW.contradiz := NEW.desfecho = 'respondeu' AND EXISTS (
    SELECT 1 FROM informacoes_sistema i JOIN envios_selo x ON x.operacao_id = i.operacao_id
    WHERE x.chave = e.chave AND i.desfecho = 'respondeu' AND NOT selo_passagem_permitida(i.estado, NEW.estado));
  RETURN NEW;
END $$;
CREATE TRIGGER informacoes_sistema_nasce BEFORE INSERT ON informacoes_sistema
  FOR EACH ROW EXECUTE FUNCTION informacoes_sistema_nasce();

-- A contradicao derruba o selo no banco (D19): com a linha ja gravada, uma retirada 'se_contradisse' do teste do envio
-- e, se for outro, do ultimo teste aprovado do sistema (o que selo_vigente le: depois de um reteste, contradizer-se
-- sobre um envio do teste antigo tambem tira o selo de agora), na mesma transacao (uma por teste). O codigo poe
-- sistema_se_contradisse no extrato e avisa o ADMIN (RETURNING contradiz diz quando).
CREATE FUNCTION informacoes_sistema_contradicao() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  do_envio uuid;
  ultimo record;
  teste uuid;
BEGIN
  IF NEW.contradiz THEN
    SELECT e.teste_id INTO do_envio FROM envios_selo e WHERE e.operacao_id = NEW.operacao_id;
    SELECT x.id, x.resultado INTO ultimo FROM testes_selo x WHERE x.sistema_id = NEW.sistema_id
    ORDER BY x.testado_em DESC, x.id DESC LIMIT 1;
    FOR teste IN
      SELECT DISTINCT t FROM unnest(ARRAY[do_envio, CASE WHEN ultimo.resultado = 'aprovado' THEN ultimo.id END]) t
      WHERE t IS NOT NULL
    LOOP
      IF NOT EXISTS (SELECT 1 FROM selos_retirados r WHERE r.teste_id = teste AND r.motivo = 'se_contradisse') THEN
        INSERT INTO selos_retirados (sistema_id, teste_id, motivo, retirado_por)
        VALUES (NEW.sistema_id, teste, 'se_contradisse', 'mcp-claro');
      END IF;
    END LOOP;
  END IF;
  RETURN NULL;
END $$;
CREATE TRIGGER informacoes_sistema_contradicao AFTER INSERT ON informacoes_sistema
  FOR EACH ROW EXECUTE FUNCTION informacoes_sistema_contradicao();
CREATE TRIGGER informacoes_sistema_sem_update_delete BEFORE UPDATE OR DELETE ON informacoes_sistema
  FOR EACH ROW EXECUTE FUNCTION extrato_recusa_mudanca();
CREATE TRIGGER informacoes_sistema_sem_truncate BEFORE TRUNCATE ON informacoes_sistema
  FOR EACH STATEMENT EXECUTE FUNCTION extrato_recusa_mudanca();

-- O estado final que o sistema informou sobre a operacao, ou NULL: resolve o "estamos confirmando" na tela, sem gravar
-- a decisao da pessoa, e e o que o botao "Registrar o que o sistema informou" registra. E o ultimo 'respondeu' fora
-- de em_andamento e desconhecido; nao_recebida so conta confirmada por duas respostas sobre a operacao, com ao menos
-- 1 minuto entre a primeira e a ultima (critica A3.3, selo_nao_recebida_confirmada). Mesma regra de
-- contrato.resposta_que_encerra, que encerra a escada da consulta.
CREATE FUNCTION selo_estado_final(p_operacao uuid) RETURNS text LANGUAGE sql STABLE
SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT i.estado FROM informacoes_sistema i
  WHERE i.operacao_id = p_operacao AND i.desfecho = 'respondeu'
    AND NOT (i.estado = ANY (selo_vocabulario('estados_em_aberto')))
    AND (i.estado <> 'nao_recebida' OR selo_nao_recebida_confirmada(ARRAY(
           SELECT n.gravado_em FROM informacoes_sistema n
           WHERE n.operacao_id = p_operacao AND n.desfecho = 'respondeu' AND n.estado = 'nao_recebida')))
  ORDER BY i.gravado_em DESC, i.id DESC LIMIT 1
$$;
-- Fim do bloco INFORMACOES.
"""

SQL_FICHAS = r"""
-- Bloco FICHAS. O retrato do selo efetivo para a pessoa quando a ficha nasceu: {nivel, teste_id, testado_em,
-- vale_ate, modo, motivo}. Fichas de antes da 0007 ficam NULL ("nao guardado"). Critica M3: a entrada grava o
-- retrato, mas o banco confere no INSERT a forma (exatamente esses campos; motivo do vocabulario, ou fase_desligada)
-- e o que ele diz. Nivel 2 ou 3 so com o nivel, o teste, as datas (com microssegundos, por igualdade) e o modo do selo
-- vigente do sistema da operacao agora, e motivo 'vigente'. Nivel 1 nunca diz 'vigente'; sem teste, sem datas nem
-- modo, e motivo 'nunca_testado' ou 'fase_desligada'; com teste, um teste do mesmo sistema, com as datas e o modo
-- dele, nunca 'nunca_testado', e 'reprovado' so (e sempre, fora 'fase_desligada') com um teste reprovado, como
-- selo_vigente diria. Depois, o selo da ficha nao muda.
ALTER TABLE fichas ADD COLUMN selo jsonb
  CONSTRAINT fichas_selo_check CHECK (selo IS NULL OR jsonb_typeof(selo) = 'object');

-- Mesmo corpo da 0006, com a conferencia do selo no INSERT e o selo fixo depois.
CREATE OR REPLACE FUNCTION fichas_guarda() RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, public, pg_temp AS $$
DECLARE
  v record;
  t record;
  confere boolean;
  combina boolean;
BEGIN
  IF TG_OP = 'INSERT' THEN
    IF NEW.decisao IS NOT NULL OR NEW.decidida_em IS NOT NULL OR NEW.decidida_por IS NOT NULL OR NEW.com_passkey
       OR NEW.confirmada_com IS NOT NULL OR NEW.segundos_ate_decidir IS NOT NULL THEN
      RAISE EXCEPTION 'Uma ficha nasce sem decisao' USING ERRCODE = 'check_violation';
    END IF;
    IF NEW.selo IS NOT NULL THEN
      IF (SELECT array_agg(k ORDER BY k COLLATE "C") FROM jsonb_object_keys(NEW.selo) k)
         IS DISTINCT FROM ARRAY['modo', 'motivo', 'nivel', 'testado_em', 'teste_id', 'vale_ate'] THEN
        RAISE EXCEPTION 'O retrato do selo na ficha tem so nivel, teste_id, testado_em, vale_ate, modo e motivo'
          USING ERRCODE = 'check_violation';
      END IF;
      IF jsonb_typeof(NEW.selo -> 'nivel') IS DISTINCT FROM 'number' OR (NEW.selo ->> 'nivel') NOT IN ('1', '2', '3') THEN
        RAISE EXCEPTION 'O retrato do selo na ficha traz o nivel 1, 2 ou 3' USING ERRCODE = 'check_violation';
      END IF;
      IF jsonb_typeof(NEW.selo -> 'motivo') IS DISTINCT FROM 'string'
         OR NOT ((NEW.selo ->> 'motivo') = ANY (selo_vocabulario('motivos_do_retrato'))) THEN
        RAISE EXCEPTION 'O motivo do retrato do selo na ficha e do vocabulario do selo' USING ERRCODE = 'check_violation';
      END IF;
      IF (NEW.selo ->> 'nivel') <> '1' THEN
        SELECT * INTO v FROM selo_vigente((SELECT o.sistema_id FROM operacoes o WHERE o.id = NEW.operacao_id));
        BEGIN
          confere := v.nivel::text = (NEW.selo ->> 'nivel') AND v.teste_id::text = (NEW.selo ->> 'teste_id')
                     AND (NEW.selo ->> 'motivo') = 'vigente' AND (NEW.selo ->> 'modo') IS NOT DISTINCT FROM v.modo
                     AND (NEW.selo ->> 'testado_em')::timestamptz = v.testado_em
                     AND (NEW.selo ->> 'vale_ate')::timestamptz = v.vale_ate;
        EXCEPTION WHEN data_exception THEN
          confere := false;
        END;
        IF NOT coalesce(confere, false) THEN
          RAISE EXCEPTION 'A ficha so mostra selo de nivel 2 ou 3 com o selo vigente do sistema agora'
            USING ERRCODE = 'check_violation';
        END IF;
      ELSE
        IF (NEW.selo ->> 'motivo') = 'vigente' THEN
          RAISE EXCEPTION 'O retrato de nivel 1 na ficha nunca diz vigente' USING ERRCODE = 'check_violation';
        END IF;
        IF jsonb_typeof(NEW.selo -> 'teste_id') = 'null' THEN
          confere := jsonb_typeof(NEW.selo -> 'testado_em') = 'null' AND jsonb_typeof(NEW.selo -> 'vale_ate') = 'null'
                     AND jsonb_typeof(NEW.selo -> 'modo') = 'null';
          combina := (NEW.selo ->> 'motivo') IN ('nunca_testado', 'fase_desligada');
        ELSE
          BEGIN
            SELECT x.id, x.resultado, x.testado_em, x.vale_ate, d.modo INTO t
            FROM testes_selo x JOIN declaracoes_selo d ON d.id = x.declaracao_id
            JOIN operacoes o ON o.id = NEW.operacao_id AND o.sistema_id = x.sistema_id
            WHERE jsonb_typeof(NEW.selo -> 'teste_id') = 'string' AND x.id = (NEW.selo ->> 'teste_id')::uuid;
            confere := FOUND AND jsonb_typeof(NEW.selo -> 'modo') = 'string' AND (NEW.selo ->> 'modo') = t.modo
                       AND jsonb_typeof(NEW.selo -> 'testado_em') = 'string'
                       AND (NEW.selo ->> 'testado_em')::timestamptz = t.testado_em
                       AND jsonb_typeof(NEW.selo -> 'vale_ate') = 'string'
                       AND (NEW.selo ->> 'vale_ate')::timestamptz = t.vale_ate;
          EXCEPTION WHEN data_exception THEN
            confere := false;
          END;
          IF coalesce(confere, false) THEN  -- t so existe com o teste achado
            combina := (NEW.selo ->> 'motivo') <> 'nunca_testado'
                       AND ((NEW.selo ->> 'motivo') = 'fase_desligada'
                            OR ((NEW.selo ->> 'motivo') = 'reprovado') = (t.resultado = 'reprovado'));
          END IF;
        END IF;
        IF NOT coalesce(confere, false) THEN
          RAISE EXCEPTION 'O retrato de nivel 1 na ficha traz um teste do proprio sistema, com as datas e o modo dele, ou nenhum'
            USING ERRCODE = 'check_violation';
        END IF;
        IF NOT coalesce(combina, false) THEN
          RAISE EXCEPTION 'O motivo do retrato de nivel 1 na ficha nao combina com o teste dele'
            USING ERRCODE = 'check_violation';
        END IF;
      END IF;
    END IF;
    RETURN NEW;
  END IF;
  IF ROW(NEW.operacao_id, NEW.codigo_conferencia, NEW.risco, NEW.exige_passkey, NEW.sinais)
     IS DISTINCT FROM ROW(OLD.operacao_id, OLD.codigo_conferencia, OLD.risco, OLD.exige_passkey, OLD.sinais) THEN
    RAISE EXCEPTION 'A operacao, o codigo, o risco, a passkey e os sinais de uma ficha nao mudam'
      USING ERRCODE = 'check_violation';
  END IF;
  IF NEW.selo IS DISTINCT FROM OLD.selo THEN
    RAISE EXCEPTION 'O selo de uma ficha nao muda' USING ERRCODE = 'check_violation';
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
-- Fim do bloco FICHAS.
"""

SQL_MEDICOES = r"""
-- Bloco MEDICOES. So contagens, nunca protocolo, numero nem pessoa.
-- Critica M10: uma visao le as tabelas com os direitos do dono, mas uma funcao chamada dentro dela roda como quem
-- consulta. selo_vigente le tabelas que o Grafana nao le; por isso v_selos passa por uma funcao propria, que roda
-- como o dono e devolve so nivel, motivo e contagem, com EXECUTE so ao Grafana.
CREATE FUNCTION medicoes.contagem_selos() RETURNS TABLE (nivel smallint, motivo text, sistemas bigint)
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, public, pg_temp AS $$
  SELECT v.nivel, v.motivo, count(*) FROM public.sistemas s CROSS JOIN LATERAL public.selo_vigente(s.id) v GROUP BY 1, 2
$$;
CREATE VIEW medicoes.v_selos AS SELECT nivel, motivo, sistemas FROM medicoes.contagem_selos();
COMMENT ON VIEW medicoes.v_selos IS 'Sistemas por nivel e motivo do selo de agora, so contagens.';

CREATE VIEW medicoes.v_consultas_sistema AS
  SELECT minuto, coalesce(rotulos ->> 'desfecho', 'sem_desfecho') AS desfecho,
         coalesce(rotulos ->> 'origem', 'sem_origem') AS origem, valor AS contagem
  FROM medicoes.minuto WHERE nome = 'selo_informacoes';
COMMENT ON VIEW medicoes.v_consultas_sistema IS
  'Perguntas aos sistemas (recibo, consulta, aviso) por minuto, desfecho e origem.';

-- Mesmo corpo da 0005, com uma coluna nova no fim: as "nao confirmadas" sem decisao da pessoa que ja tem resposta
-- final do sistema (a mesma regra de selo_estado_final, nao_recebida so confirmada, escrita aqui para a visao ler as
-- tabelas com os direitos do dono; selo_nao_recebida_confirmada e pura, com o EXECUTE padrao).
CREATE OR REPLACE VIEW medicoes.v_fila_agora AS
  SELECT f.estado, count(*) AS acoes, min(f.mudou_em) AS desde,
         count(*) FILTER (WHERE f.estado = 'nao_confirmada' AND EXISTS (
           SELECT 1 FROM public.informacoes_sistema i
           WHERE i.operacao_id = f.operacao_id AND i.desfecho = 'respondeu'
             AND i.estado NOT IN ('em_andamento', 'desconhecido')
             AND (i.estado <> 'nao_recebida' OR public.selo_nao_recebida_confirmada(ARRAY(
                    SELECT n.gravado_em FROM public.informacoes_sistema n
                    WHERE n.operacao_id = f.operacao_id AND n.desfecho = 'respondeu'
                      AND n.estado = 'nao_recebida'))))) AS com_resposta_do_sistema
  FROM public.fila_saida f
  WHERE f.estado IN ('aguardando_ficha', 'liberada', 'enviando')
     OR (f.estado = 'nao_confirmada'
         AND NOT EXISTS (SELECT 1 FROM public.decisoes_nao_confirmada d WHERE d.operacao_id = f.operacao_id))
  GROUP BY f.estado;
COMMENT ON VIEW medicoes.v_fila_agora IS
  'Fila de saida agora: aguardando ficha, liberada, enviando e nao confirmada ainda sem decisao da pessoa (e quantas destas ja tem resposta final do sistema).';
-- Fim do bloco MEDICOES.
"""

GRANTS = r"""
-- Bloco GRANTS (ultimo).
-- Leitura. Declaracoes e pedidos: as telas do ADMIN (entrada) e a bateria (despachante); as rotinas nao usam (critica
-- M10). Testes, retiradas, informacoes e avisos: as telas, o despachante e as rotinas (consulta, aviso de selo a
-- vencer, resumo semanal). O numero da operacao (envios_selo.chave) so o despachante le: a entrada e as rotinas veem
-- as outras colunas (critica M10).
GRANT SELECT ON declaracoes_selo, pedidos_teste_selo TO mcp_claro_entrada, mcp_claro_despachante;
GRANT SELECT ON testes_selo, selos_retirados, informacoes_sistema, avisos_bloqueio, selos_vigentes
  TO mcp_claro_entrada, mcp_claro_despachante, mcp_claro_rotinas;
GRANT SELECT ON envios_selo, avisos_bloqueio_derivados TO mcp_claro_despachante;
GRANT SELECT (operacao_id, sistema_id, nivel, teste_id, repete_operacao_id, gravado_em) ON envios_selo
  TO mcp_claro_entrada, mcp_claro_rotinas;
-- O despachante deriva os avisos dos comprovantes (critica A1).
GRANT SELECT ON comprovantes TO mcp_claro_despachante;
-- Escrita. As telas do ADMIN salvam a declaracao e pedem o teste; o despachante roda e grava (pedido, teste, envio,
-- informacao, aviso). Retirada: o ADMIN pela entrada ('admin') e o despachante (contradicao e falta de resposta); o
-- gatilho confere quem e por que. A hora do pedido e do banco: o despachante muda so estado e motivo.
GRANT INSERT ON declaracoes_selo, pedidos_teste_selo TO mcp_claro_entrada;
GRANT UPDATE (estado, motivo) ON pedidos_teste_selo TO mcp_claro_despachante;
GRANT INSERT ON testes_selo, envios_selo, informacoes_sistema, avisos_bloqueio, avisos_bloqueio_derivados
  TO mcp_claro_despachante;
GRANT INSERT ON selos_retirados TO mcp_claro_entrada, mcp_claro_despachante;
GRANT UPDATE (estado, tentativas, proxima_em) ON avisos_bloqueio TO mcp_claro_despachante;
-- Funcoes que leem tabelas: so os papeis que as usam. selo_vigente, selo_acoes_do_sistema, selo_contrato_listado e
-- selo_substituto rodam como o dono; selo_estado_final e selo_alvo_na_producao, como quem chama. As funcoes puras
-- do bloco CONTRATO nao leem tabela nenhuma e ficam com o EXECUTE padrao (selo_nao_recebida_confirmada tambem e
-- chamada por medicoes.v_fila_agora, que o Grafana le).
REVOKE ALL ON FUNCTION selo_vigente(uuid, timestamptz), selo_acoes_do_sistema(uuid),
  selo_contrato_listado(uuid, smallint, timestamptz), selo_substituto(uuid), selo_estado_final(uuid) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION selo_vigente(uuid, timestamptz), selo_acoes_do_sistema(uuid),
  selo_contrato_listado(uuid, smallint, timestamptz), selo_substituto(uuid), selo_estado_final(uuid)
  TO mcp_claro_entrada, mcp_claro_despachante, mcp_claro_rotinas;
-- O alvo contra a producao (critica A4) roda como quem chama, nos gatilhos da declaracao e do pedido (entrada) e da
-- passagem para 'rodando' e do teste (despachante): so esses dois.
REVOKE ALL ON FUNCTION selo_alvo_na_producao(text) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION selo_alvo_na_producao(text) TO mcp_claro_entrada, mcp_claro_despachante;
-- Grafana: so as visoes novas e a funcao de contagem que v_selos chama (critica M10). v_fila_agora mantem o GRANT da
-- 0005 (CREATE OR REPLACE VIEW nao o tira).
REVOKE ALL ON FUNCTION medicoes.contagem_selos() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION medicoes.contagem_selos() TO grafana_leitor;
GRANT SELECT ON medicoes.v_selos, medicoes.v_consultas_sistema TO grafana_leitor;
-- Sem GRANT novo, de proposito:
-- * ninguem tem UPDATE em testes_selo, envios_selo, informacoes_sistema, selos_retirados, declaracoes_selo e
--   avisos_bloqueio_derivados, e os gatilhos recusam ate o dono;
-- * sistemas.selo segue sem UPDATE para ninguem, e o CHECK a prende em 1;
-- * a entrada e as rotinas nao inserem em testes_selo nem em envios_selo: o selo nao sobe por elas. Nem em
--   avisos_bloqueio: o aviso sai da transacao do bloqueio (critica A1);
-- * operacoes.repete_operacao_id e fichas.selo: o INSERT da entrada e da tabela inteira (0005) e cobre as colunas
--   novas, e ninguem tem UPDATE nelas;
-- * ferramentas_vistas.sumiu_em: o UPDATE de tabela inteira do despachante (0002) a cobre;
-- * chaves_cifradas (a chave de teste): o despachante ja tem acesso inteiro (0002);
-- * a identidade de informacoes_sistema nao pede GRANT de sequencia;
-- * a sala de visitas continua sem usuario no banco.
-- Fim do bloco GRANTS.
"""


def upgrade() -> None:
    sql_em_bloco(SQL_CONTRATO)
    sql_em_bloco(SQL_DECLARACOES)
    sql_em_bloco(SQL_TESTES)
    sql_em_bloco(SQL_VIGENTE)
    sql_em_bloco(SQL_RESERVADAS)
    sql_em_bloco(SQL_ENVIO)
    sql_em_bloco(SQL_AVISOS_BLOQUEIO)
    sql_em_bloco(SQL_INFORMACOES)
    sql_em_bloco(SQL_FICHAS)
    sql_em_bloco(SQL_MEDICOES)
    sql_em_bloco(GRANTS)


def downgrade() -> None:
    raise RuntimeError("O MCP-CLARO nao desfaz migracoes; restaure o backup se precisar voltar.")
