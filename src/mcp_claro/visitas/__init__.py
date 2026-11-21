"""Papel ``visitas``: a sala de visitas, onde o MCP-CLARO fala com servidores de terceiros (desenho 3.8; ajustes D1).

- Conteiner proprio, sem banco, sem cofre e sem extrato, numa rede so dele (``saida_visitas``); so o despachante o
  alcanca, pela rede ``visitas``, com mTLS (``ca_clientes_visitas`` so emite o certificado do despachante).
- Cada chamada roda num processo filho novo, iniciado por ``spawn``, que recebe so os dados daquela chamada e
  morre no fim (ajustes, A1): um terceiro que tome o processo so ve a propria chamada. O processo pai so recebe o
  pedido do despachante, entrega ao filho e devolve o que o filho escreveu, sem ler os bytes do terceiro.
- O filho so recebe o pedido depois de avisar que se isolou (nao depuravel, dominio Landlock proprio), e fala com o
  pai por soquetes, que o ``/proc`` nao reabre: nem um filho recem-iniciado, ainda na partida, fica ao alcance de
  outro filho tomado.
- O filho roda ``ligacao.sala_contrato.atender`` com o ambiente da sala (nunca um campo do pedido); a liberacao de
  enderecos locais so vale em desenvolvimento.

Este pacote fica leve: ``tls`` (contexto do despachante) e importado pelo despachante.
"""
