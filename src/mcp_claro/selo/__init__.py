"""Selos de nivel 2 e 3 (Fase 4): o contrato do selo, a bateria de testes e a consulta ao proprio sistema.

O selo e uma declaracao do MCP-CLARO, nao do rascunho de escrita segura nem do sistema: ele diz como um sistema
respondeu a uma bateria de testes do gateway, num alvo de teste declarado, numa data. Vale por ``BADGE_TTL_DAYS`` e
so enquanto o sistema nao muda. Nao garante o que o sistema fara depois.

Modulos, cada um com um dono (desenho, secao 11):

- ``contrato``: nomes, esquemas, chaves de ``_meta``, vocabulario, casos, passagens, versao da bateria e as
  assinaturas que os outros grupos usam (G0). O banco (migracao 0007) tem as mesmas listas; o teste confere;
- ``declaracao`` e ``vigente``: modelo da declaracao de teste, leitura de ``selos_vigentes``, selo efetivo e motivo
  legivel (G2);
- ``alvo``, ``respostas``, ``casos``, ``bateria`` e ``consulta``: alvo fechado, validacao estrita das respostas, os
  casos, a rodada, a consulta e o aviso de bloqueio (G3).
"""
