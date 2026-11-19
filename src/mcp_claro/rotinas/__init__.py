"""Papel ``rotinas``: vencimentos, avisos, fechamento, particoes, resumos, limpeza e mudou-parou.

Cada tarefa roda isolada e com trava consultiva do PostgreSQL, para rodar uma vez
por instalacao mesmo com mais de um processo. A aplicacao sai de
``mcp_claro.rotinas.app.criar_app_rotinas``.
"""
