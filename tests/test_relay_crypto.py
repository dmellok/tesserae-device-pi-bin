"""Relay crypto pinned to the contract's golden vectors.

These are load-bearing. A derivation that is merely plausible round-trips
against itself perfectly while never agreeing with the home instance, and the
only field symptom is that frames silently never appear. The vectors come from
docs/relay/contract.md ("Golden vectors"), generated from fixed private
scalars home_priv = 0x01*32, panel_priv = 0x02*32.
"""
from __future__ import annotations

import pytest

from tesserae_pi_bin_client.relay_crypto import (
    RelayCryptoError,
    b64url_decode,
    b64url_encode,
    derive_frame_key,
    generate_private_key,
    public_key_for,
    seal,
    unseal,
)

HOME_PRIV = bytes([0x01]) * 32
PANEL_PRIV = bytes([0x02]) * 32

HOME_PUB = bytes.fromhex(
    "a4e09292b651c278b9772c569f5fa9bb13d906b46ab68c9df9dc2b4409f8a209"
)
PANEL_PUB = bytes.fromhex(
    "ce8d3ad1ccb633ec7b70c17814a5c76ecd029685050d344745ba05870e587d59"
)
FRAME_KEY = bytes.fromhex(
    "613376ae6bc97931b6d33c17aaf561fb2ff2e2f12937249705e1e5b75dd98e83"
)
NONCE = bytes.fromhex("00112233445566778899aabb")
FRAME = bytes.fromhex("74657373657261652d6672616d652d0001027061796c6f6164")
SEALED = bytes.fromhex(
    "00112233445566778899aabb4fa3210211222b8aa47f010d2a30fc71c"
    "70e6de8d4345adf90c057a2b39262b5ab8cee9507d7839ce8"
)


# --- golden vectors ------------------------------------------------------------


def test_public_keys_match_the_contract() -> None:
    assert public_key_for(HOME_PRIV) == HOME_PUB
    assert public_key_for(PANEL_PRIV) == PANEL_PUB


def test_frame_key_matches_the_contract() -> None:
    assert derive_frame_key(PANEL_PRIV, HOME_PUB) == FRAME_KEY


def test_derivation_is_symmetric() -> None:
    """This is what lets the key never travel."""
    assert derive_frame_key(HOME_PRIV, PANEL_PUB) == derive_frame_key(
        PANEL_PRIV, HOME_PUB
    )


def test_seal_matches_the_contract_vector() -> None:
    assert seal(FRAME, FRAME_KEY, nonce=NONCE) == SEALED


def test_unseal_matches_the_contract_vector() -> None:
    assert unseal(SEALED, FRAME_KEY) == FRAME


def test_sealed_length_is_nonce_plus_frame_plus_tag() -> None:
    assert len(SEALED) == 12 + len(FRAME) + 16


# --- failure paths -------------------------------------------------------------


def test_wrong_key_raises_rather_than_returning_garbage() -> None:
    """A tag failure must never become paintable bytes."""
    wrong = derive_frame_key(generate_private_key(), HOME_PUB)
    with pytest.raises(RelayCryptoError, match="authentication tag failed"):
        unseal(SEALED, wrong)


def test_tampered_ciphertext_raises() -> None:
    tampered = bytearray(SEALED)
    tampered[20] ^= 0xFF
    with pytest.raises(RelayCryptoError, match="authentication tag failed"):
        unseal(bytes(tampered), FRAME_KEY)


def test_tampered_nonce_raises() -> None:
    tampered = bytearray(SEALED)
    tampered[0] ^= 0xFF
    with pytest.raises(RelayCryptoError, match="authentication tag failed"):
        unseal(bytes(tampered), FRAME_KEY)


def test_truncated_payload_raises() -> None:
    with pytest.raises(RelayCryptoError, match="minimum is 28"):
        unseal(SEALED[:20], FRAME_KEY)


def test_bad_key_length_raises() -> None:
    with pytest.raises(RelayCryptoError, match="key must be 32 bytes"):
        unseal(SEALED, b"short")


def test_bad_public_key_length_raises() -> None:
    with pytest.raises(RelayCryptoError, match="public key must be 32 bytes"):
        derive_frame_key(PANEL_PRIV, b"\x00" * 31)


# --- wire encoding -------------------------------------------------------------


def test_b64url_is_unpadded() -> None:
    encoded = b64url_encode(HOME_PUB)
    assert "=" not in encoded
    assert b64url_decode(encoded) == HOME_PUB


def test_b64url_decode_tolerates_padding() -> None:
    """Home or the Worker may pad; we must not care."""
    import base64

    padded = base64.urlsafe_b64encode(HOME_PUB).decode()
    assert b64url_decode(padded) == HOME_PUB


def test_b64url_decode_rejects_garbage() -> None:
    with pytest.raises(RelayCryptoError, match="not valid base64url"):
        b64url_decode("!!!not base64!!!")


def test_generated_keys_are_distinct_and_usable() -> None:
    a, b = generate_private_key(), generate_private_key()
    assert a != b
    assert derive_frame_key(a, public_key_for(b)) == derive_frame_key(
        b, public_key_for(a)
    )
