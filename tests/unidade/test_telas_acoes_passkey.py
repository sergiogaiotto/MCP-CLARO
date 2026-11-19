"""A passkey sem banco: o autenticador de teste contra a conferencia da biblioteca ``webauthn``, nos dois sentidos."""

from __future__ import annotations

import pytest
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import options_to_json_dict
from webauthn.helpers.exceptions import InvalidAuthenticationResponse, InvalidRegistrationResponse
from webauthn.helpers.structs import PublicKeyCredentialDescriptor, UserVerificationRequirement

from mcp_claro.config.settings import Parametros
from mcp_claro.telas.passkeys import Cerimonia
from tests.autenticador_falso import AutenticadorFalso, cbor

RP_ID = "mcp-claro.teste"
ORIGEM = "https://mcp-claro.teste"


def _cadastrar(aut: AutenticadorFalso) -> tuple[bytes, int]:
    desafio = b"d" * 32
    opcoes = options_to_json_dict(
        generate_registration_options(rp_id=RP_ID, rp_name="MCP-CLARO", user_name="Ana", challenge=desafio)
    )
    feito = verify_registration_response(
        credential=aut.criar({"publicKey": opcoes}),
        expected_challenge=desafio,
        expected_rp_id=RP_ID,
        expected_origin=ORIGEM,
        require_user_verification=True,
    )
    assert feito.credential_id == aut.credencial_id
    return bytes(feito.credential_public_key), int(feito.sign_count)


def _opcoes(aut: AutenticadorFalso, desafio: bytes) -> dict[str, object]:
    return {
        "publicKey": options_to_json_dict(
            generate_authentication_options(
                rp_id=RP_ID,
                challenge=desafio,
                allow_credentials=[PublicKeyCredentialDescriptor(id=aut.credencial_id)],
                user_verification=UserVerificationRequirement.REQUIRED,
            )
        )
    }


def test_cbor_minimo() -> None:
    assert cbor(1) == b"\x01"
    assert cbor(-7) == b"\x26"
    assert cbor(b"ab") == b"\x42ab"
    assert cbor("fmt") == b"\x63fmt"
    assert cbor({1: 2}) == b"\xa1\x01\x02"
    assert cbor(300) == b"\x19\x01\x2c"


def test_cadastro_e_entrada_conferem() -> None:
    aut = AutenticadorFalso(ORIGEM)
    chave, contador = _cadastrar(aut)
    desafio = b"e" * 32
    conferida = verify_authentication_response(
        credential=aut.assinar(_opcoes(aut, desafio)),
        expected_challenge=desafio,
        expected_rp_id=RP_ID,
        expected_origin=ORIGEM,
        credential_public_key=chave,
        credential_current_sign_count=contador,
        require_user_verification=True,
    )
    assert conferida.new_sign_count == 1
    assert conferida.user_verified


def test_outra_origem_outro_desafio_e_contador_repetido_sao_recusados() -> None:
    aut = AutenticadorFalso(ORIGEM)
    chave, _ = _cadastrar(aut)
    desafio = b"f" * 32

    def conferir(resposta: dict[str, object], contador: int = 0, esperado: bytes = desafio) -> None:
        verify_authentication_response(
            credential=resposta,
            expected_challenge=esperado,
            expected_rp_id=RP_ID,
            expected_origin=ORIGEM,
            credential_public_key=chave,
            credential_current_sign_count=contador,
            require_user_verification=True,
        )

    with pytest.raises(InvalidAuthenticationResponse):
        conferir(aut.assinar(_opcoes(aut, desafio), origem="https://outro.teste"))
    with pytest.raises(InvalidAuthenticationResponse):
        conferir(aut.assinar(_opcoes(aut, desafio)), esperado=b"g" * 32)
    with pytest.raises(InvalidAuthenticationResponse):
        conferir(aut.assinar(_opcoes(aut, desafio), rp_id="outro.teste"))
    resposta = aut.assinar(_opcoes(aut, desafio))
    with pytest.raises(InvalidAuthenticationResponse):
        conferir(resposta, contador=aut.contador)  # contador que nao subiu: copia do autenticador
    outra = AutenticadorFalso(ORIGEM, credencial_id=aut.credencial_id)
    with pytest.raises(InvalidAuthenticationResponse):
        conferir(outra.assinar(_opcoes(outra, desafio)))  # mesma credencial, chave errada


def test_sem_verificar_a_pessoa_nao_vale() -> None:
    aut = AutenticadorFalso(ORIGEM, verifica_pessoa=False)
    with pytest.raises(InvalidRegistrationResponse):
        _cadastrar(aut)


def test_rp_id_e_origem_saem_do_endereco_publico() -> None:
    p = Parametros(PUBLIC_BASE_URL="https://mcp-claro.exemplo:8443/")
    cer = Cerimonia.de_parametros(p)
    assert cer.rp_id == "mcp-claro.exemplo"
    assert cer.origem == "https://mcp-claro.exemplo:8443"
