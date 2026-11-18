"""Regras que a entrada e o despachante aplicam antes de cada pedido.

- ``bloqueios``: bloqueio de um toque e pausa, por pessoa, aplicativo, par, sistema ou tudo;
- ``catraca``: leituras por ordem de servico por dia (``READS_CAP_PER_DAY``);
- ``estado``: estado da entrada (aberta, so de leitura, fechada) e da porta de saida;
- ``revisao``: o que a revisao do ADMIN recusa (acao em sistema de chave compartilhada).
"""
