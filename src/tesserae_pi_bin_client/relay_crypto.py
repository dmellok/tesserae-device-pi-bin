"""Zero-knowledge crypto for the cloud-relay transport.

The relay is a per-device mailbox both ends reach *outbound*: home seals each
rendered frame and PUTs it, the panel polls and decrypts. The relay holds
ciphertext, two public keys and a token hash — never the frame key — so it can
validate a poll but cannot read a dashboard.

    shared    = X25519(our_private, their_public)
    frame_key = HKDF-SHA256(ikm=shared, salt=none,
                            info="tesserae-relay-frame-key-v1", L=32)
    sealed    = nonce(12) || AES-256-GCM(frame, frame_key, nonce, aad="")

`docs/relay/contract.md` in the Tesserae repo is authoritative for all three
sides, and its golden vectors are checked in tests/test_relay_crypto.py. That
matters more here than the usual round-trip test: a derivation that is merely
*plausible* produces a wrong key, and the only symptom in the field is that
frames silently never appear. Round-tripping against ourselves would pass
happily while never agreeing with home.

Keys are raw 32-byte X25519, base64url without padding on the wire.
"""
from __future__ import annotations

import base64

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

# Domain separation string. Changing this changes every derived key, so it is
# part of the wire contract, not an implementation detail.
HKDF_INFO = b"tesserae-relay-frame-key-v1"

KEY_LEN = 32
NONCE_LEN = 12
TAG_LEN = 16


class RelayCryptoError(Exception):
    """Raised when a sealed payload cannot be authenticated or is malformed."""


def b64url_encode(raw: bytes) -> str:
    """Base64url without padding — the wire form for public keys."""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64url_decode(text: str) -> bytes:
    """Inverse of b64url_encode, tolerating present-or-absent padding."""
    pad = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + pad)
    except (ValueError, TypeError) as exc:
        raise RelayCryptoError(f"not valid base64url: {text!r}") from exc


def generate_private_key() -> bytes:
    """A fresh random X25519 private scalar, raw 32 bytes."""
    return X25519PrivateKey.generate().private_bytes_raw()


def public_key_for(private_raw: bytes) -> bytes:
    """Derive the raw 32-byte public key for a raw private scalar."""
    if len(private_raw) != KEY_LEN:
        raise RelayCryptoError(
            f"private key must be {KEY_LEN} bytes, got {len(private_raw)}"
        )
    private = X25519PrivateKey.from_private_bytes(private_raw)
    return private.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


def derive_frame_key(private_raw: bytes, their_public_raw: bytes) -> bytes:
    """The shared AES-256 frame key.

    Symmetric by construction: derive(panel_priv, home_pub) equals
    derive(home_priv, panel_pub), which is what lets the key never travel.
    """
    if len(private_raw) != KEY_LEN:
        raise RelayCryptoError(
            f"private key must be {KEY_LEN} bytes, got {len(private_raw)}"
        )
    if len(their_public_raw) != KEY_LEN:
        raise RelayCryptoError(
            f"public key must be {KEY_LEN} bytes, got {len(their_public_raw)}"
        )
    private = X25519PrivateKey.from_private_bytes(private_raw)
    public = X25519PublicKey.from_public_bytes(their_public_raw)
    shared = private.exchange(public)
    return HKDF(
        algorithm=hashes.SHA256(),
        length=KEY_LEN,
        salt=None,
        info=HKDF_INFO,
    ).derive(shared)


def seal(frame: bytes, key: bytes, nonce: bytes | None = None) -> bytes:
    """nonce || AES-256-GCM(frame). Only used to exercise the vectors in
    tests — the panel never publishes, it only reads."""
    if len(key) != KEY_LEN:
        raise RelayCryptoError(f"key must be {KEY_LEN} bytes, got {len(key)}")
    if nonce is None:
        import os

        nonce = os.urandom(NONCE_LEN)
    if len(nonce) != NONCE_LEN:
        raise RelayCryptoError(f"nonce must be {NONCE_LEN} bytes, got {len(nonce)}")
    return nonce + AESGCM(key).encrypt(nonce, frame, None)


def unseal(sealed: bytes, key: bytes) -> bytes:
    """Recover the frame, or raise RelayCryptoError.

    A failed authentication tag is NEVER a recoverable condition the caller
    should paint through: it means the mailbox does not match this panel's
    key, and painting it would render whatever an attacker or a corrupted
    store supplied. Raising is the whole point.
    """
    if len(key) != KEY_LEN:
        raise RelayCryptoError(f"key must be {KEY_LEN} bytes, got {len(key)}")
    if len(sealed) < NONCE_LEN + TAG_LEN:
        raise RelayCryptoError(
            f"sealed payload is {len(sealed)} bytes; "
            f"minimum is {NONCE_LEN + TAG_LEN} (nonce + tag)"
        )
    nonce, body = sealed[:NONCE_LEN], sealed[NONCE_LEN:]
    try:
        return AESGCM(key).decrypt(nonce, body, None)
    except InvalidTag as exc:
        raise RelayCryptoError(
            "authentication tag failed — the mailbox does not match this "
            "panel's frame key; refusing to paint"
        ) from exc


__all__ = [
    "HKDF_INFO",
    "KEY_LEN",
    "NONCE_LEN",
    "TAG_LEN",
    "RelayCryptoError",
    "b64url_decode",
    "b64url_encode",
    "derive_frame_key",
    "generate_private_key",
    "public_key_for",
    "seal",
    "unseal",
]
