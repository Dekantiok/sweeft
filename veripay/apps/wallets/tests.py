"""NEP-413 payload and signature verification tests.

The byte layout is pinned to @near-wallet-selector/core's `payloadSchema`
(tag = 2^31 + 413, then message, nonce, recipient, optional callback URL).
"""
import base64
import hashlib

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from django.test import SimpleTestCase, TestCase

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


class VerifyEndpointTests(TestCase):
    """Full HTTP flow: challenge -> sign (as a wallet would) -> verify -> session."""

    def _sign(self, message: str, nonce_b64: str, recipient: str):
        priv = Ed25519PrivateKey.generate()
        raw = priv.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        public_key = f"ed25519:{base58.b58encode(raw)}"
        digest = hashlib.sha256(
            borsh_payload(message, base64.b64decode(nonce_b64), recipient)
        ).digest()
        return public_key, base64.b64encode(priv.sign(digest)).decode()

    def test_full_login_flow(self):
        ch = self.client.get("/api/v1/wallet/challenge/?account_id=alice.testnet").json()
        public_key, signature = self._sign(ch["message"], ch["nonce"], ch["recipient"])
        res = self.client.post(
            "/api/v1/wallet/verify/",
            data={
                "account_id": "alice.testnet",
                "public_key": public_key,
                "signature": signature,
                "nonce": ch["nonce"],
            },
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200, res.content)
        self.assertTrue(res.json()["ok"])
        self.assertEqual(self.client.session["wallet"], "alice.testnet")

    def test_replayed_challenge_rejected(self):
        ch = self.client.get("/api/v1/wallet/challenge/?account_id=alice.testnet").json()
        public_key, signature = self._sign(ch["message"], ch["nonce"], ch["recipient"])
        payload = {
            "account_id": "alice.testnet",
            "public_key": public_key,
            "signature": signature,
            "nonce": ch["nonce"],
        }
        first = self.client.post("/api/v1/wallet/verify/", data=payload, content_type="application/json")
        self.assertTrue(first.json()["ok"])
        second = self.client.post("/api/v1/wallet/verify/", data=payload, content_type="application/json")
        self.assertEqual(second.status_code, 400)
        self.assertIn("already used", second.json()["error"])

    def test_wrong_signature_rejected(self):
        ch = self.client.get("/api/v1/wallet/challenge/?account_id=alice.testnet").json()
        public_key, _ = self._sign(ch["message"], ch["nonce"], ch["recipient"])
        res = self.client.post(
            "/api/v1/wallet/verify/",
            data={
                "account_id": "alice.testnet",
                "public_key": public_key,
                "signature": base64.b64encode(b"\x00" * 64).decode(),
                "nonce": ch["nonce"],
            },
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("verification failed", res.json()["error"])
