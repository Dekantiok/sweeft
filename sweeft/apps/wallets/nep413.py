"""NEP-413 (sign message) verification with no external dependencies.

Payload borsh layout (matches @near-wallet-selector/core's `payloadSchema`):

    u32     tag = 2147484061          # 2^31 + 413, per NEP-413
    string  message                   # u32 len + utf8
    [u8;32] nonce
    string  recipient                 # u32 len + utf8
    option  callback_url              # 1 byte flag, then string if Some

The wallet signs sha256(borsh(payload)); we verify with the wallet's public key.
Supports ed25519 and secp256k1 keys.
"""
from __future__ import annotations

import base64
import hashlib
import struct

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import base58

NEP413_TAG = 2147484061  # 2^31 + 413


def borsh_payload(message: str, nonce: bytes, recipient: str, callback_url: str | None = None) -> bytes:
    if len(nonce) != 32:
        raise ValueError("nonce must be 32 bytes")
    buf = bytearray()
    buf += struct.pack("<I", NEP413_TAG)
    m = message.encode()
    buf += struct.pack("<I", len(m)) + m
    buf += nonce
    r = recipient.encode()
    buf += struct.pack("<I", len(r)) + r
    if callback_url:
        c = callback_url.encode()
        buf += b"\x01" + struct.pack("<I", len(c)) + c
    else:
        buf += b"\x00"
    return bytes(buf)


def _decode_b58(text: str) -> bytes:
    return base58.b58decode(text)


def verify_nep413(
    public_key: str,
    signature_b64: str,
    message: str,
    nonce_b64: str,
    recipient: str,
    callback_url: str | None = None,
) -> bool:
    digest = hashlib.sha256(
        borsh_payload(message, base64.b64decode(nonce_b64), recipient, callback_url)
    ).digest()
    sig = base64.b64decode(signature_b64)
    key_type, _, key_b58 = public_key.partition(":")
    raw = _decode_b58(key_b58)
    try:
        if key_type == "ed25519":
            Ed25519PublicKey.from_public_bytes(raw).verify(sig, digest)
            return True
        if key_type == "secp256k1":
            pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256K1(), b"\x04" + raw)
            r = int.from_bytes(sig[:32], "big")
            s = int.from_bytes(sig[32:64], "big")
            from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

            pub.verify(encode_dss_signature(r, s), digest, ec.ECDSA(hashes.SHA256()))
            return True
    except (InvalidSignature, ValueError):
        return False
    return False
