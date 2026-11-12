"""NEP-413 payload and signature verification tests.

The byte layout is pinned to @near-wallet-selector/core's `payloadSchema`
(tag = 2^31 + 413, then message, nonce, recipient, optional callback URL).
"""
import base64
import hashlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import SimpleTestCase

from apps.wallets import base58
from apps.wallets.nep413 import NEP413_TAG, borsh_payload, verify_nep413


class BorshPayloadTests(SimpleTestCase):
    def test_tag_is_first_u32(self):
        payload = borsh_payload("hello", b"\x00" * 32, "example.com")
        self.assertEqual(payload[:4], (NEP413_TAG).to_bytes(4, "little"))

    def test_layout_matches_wallet_selector_schema(self):
        message = "Sign in"
        nonce = bytes(range(32))
        recipient = "localhost:8000"
        payload = borsh_payload(message, nonce, recipient, None)
        expected = (
            (NEP413_TAG).to_bytes(4, "little")
            + len(message).to_bytes(4, "little")
            + message.encode()
            + nonce
            + len(recipient).to_bytes(4, "little")
            + recipient.encode()
            + b"\x00"
        )
        self.assertEqual(payload, expected)

    def test_callback_url_option(self):
        payload = borsh_payload("m", b"\x01" * 32, "r", "https://cb.example")
        self.assertTrue(payload.endswith(b"https://cb.example"))

    def test_nonce_length_enforced(self):
        with self.assertRaises(ValueError):
            borsh_payload("m", b"\x00" * 31, "r")


class VerifyTests(SimpleTestCase):
    def _keypair(self):
        priv = Ed25519PrivateKey.generate()
        raw = priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        return priv, f"ed25519:{base58.b58encode(raw)}"

    def test_roundtrip(self):
        priv, public_key = self._keypair()
        message = "Sign in to VeriPay\nDomain: localhost:8000"
        nonce = bytes(range(32))
        recipient = "localhost:8000"
        digest = hashlib.sha256(borsh_payload(message, nonce, recipient)).digest()
        signature = priv.sign(digest)
        ok = verify_nep413(
            public_key=public_key,
            signature_b64=base64.b64encode(signature).decode(),
            message=message,
            nonce_b64=base64.b64encode(nonce).decode(),
            recipient=recipient,
        )
        self.assertTrue(ok)

    def test_wrong_message_fails(self):
        priv, public_key = self._keypair()
        nonce = bytes(range(32))
        digest = hashlib.sha256(borsh_payload("original", nonce, "localhost:8000")).digest()
        signature = priv.sign(digest)
        ok = verify_nep413(
            public_key=public_key,
            signature_b64=base64.b64encode(signature).decode(),
            message="tampered",
            nonce_b64=base64.b64encode(nonce).decode(),
            recipient="localhost:8000",
        )
        self.assertFalse(ok)

    def test_wrong_recipient_fails(self):
        priv, public_key = self._keypair()
        nonce = bytes(range(32))
        digest = hashlib.sha256(borsh_payload("m", nonce, "evil.example")).digest()
        signature = priv.sign(digest)
        ok = verify_nep413(
            public_key=public_key,
            signature_b64=base64.b64encode(signature).decode(),
            message="m",
            nonce_b64=base64.b64encode(nonce).decode(),
            recipient="localhost:8000",
        )
        self.assertFalse(ok)
