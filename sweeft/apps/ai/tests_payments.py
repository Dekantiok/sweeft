"""Payment-intent authorization lifecycle: expiry, stale-claim recovery, intent id."""
import json
from datetime import timedelta
from unittest import mock

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.ai.models import PaymentIntent


def make_intent(wallet="buyer.testnet", **overrides):
    data = {
        "wallet": wallet,
        "to_account": "seller.testnet",
        "amount": "1000000000000000000000",  # 0.001 NEAR
        "expires_at": timezone.now() + timedelta(minutes=10),
    }
    data.update(overrides)
    return PaymentIntent.objects.create(**data)


@override_settings(
    AI_AGENT_ACCOUNT="agent.testnet",
    AI_AGENT_PRIVATE_KEY="ed25519:dummy",
    AI_AGENT_ENABLED=True,
    AI_AGENT_MAX_PAYMENT=100_000_000_000_000_000_000_000,
    AI_PAYMENT_PROCESSING_TIMEOUT_SECONDS=120,
)
class PaymentIntentLifecycleTests(TestCase):
    def setUp(self):
        self.session = self.client.session
        self.session["wallet"] = "buyer.testnet"
        self.session.save()

    def _post(self, intent_id):
        return self.client.post(f"/api/v1/ai/payments/{intent_id}/execute/")

    def _chain_mocks(self):
        return (
            mock.patch("apps.ai.tools._onchain_agent", return_value="agent.testnet"),
            mock.patch("apps.ai.tools._onchain_max_payment", return_value=10**24),
        )

    def test_expired_proposal_is_rejected(self):
        intent = make_intent(expires_at=timezone.now() - timedelta(seconds=1))
        agent, cap = self._chain_mocks()
        with agent, cap:
            res = self._post(intent.id)
        self.assertEqual(res.status_code, 410)
        self.assertIn("expired", res.json()["error"])
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.PROPOSED)

    def test_execution_passes_intent_id_to_contract(self):
        intent = make_intent()
        agent, cap = self._chain_mocks()
        with agent, cap, mock.patch(
            "apps.ai.views.transfer_from_allowance",
            return_value={"success": True, "tx_hash": "HASH", "error": None},
        ) as send:
            res = self._post(intent.id)
        self.assertEqual(res.status_code, 200, res.json())
        self.assertEqual(send.call_args.kwargs["intent_id"], str(intent.id))
        self.assertEqual(send.call_args.kwargs["receiver"], "seller.testnet")
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.EXECUTED)

    def test_stale_processing_claim_is_reclaimed(self):
        intent = make_intent(
            status=PaymentIntent.Status.PROCESSING,
            processing_started_at=timezone.now() - timedelta(seconds=600),
        )
        res = self._post(intent.id)
        self.assertEqual(res.status_code, 409)
        self.assertIn("timed out", res.json()["error"])
        intent.refresh_from_db()
        self.assertEqual(intent.status, PaymentIntent.Status.PROPOSED)
        self.assertIsNone(intent.processing_started_at)

    def test_fresh_processing_claim_conflicts(self):
        intent = make_intent(
            status=PaymentIntent.Status.PROCESSING,
            processing_started_at=timezone.now(),
        )
        res = self._post(intent.id)
        self.assertEqual(res.status_code, 409)
        self.assertIn("already being executed", res.json()["error"])

    def test_wrong_wallet_cannot_execute(self):
        intent = make_intent(wallet="stranger.testnet")
        res = self._post(intent.id)
        self.assertEqual(res.status_code, 404)

    def test_agent_info_exposes_owner_scope(self):
        with mock.patch("apps.ai.tools._onchain_agent", return_value="agent.testnet"), mock.patch(
            "apps.ai.tools._onchain_max_payment", return_value=10**24
        ), mock.patch(
            "apps.ai.views.near_mod.view_call",
            side_effect=lambda contract, method, args: (
                "agent.testnet" if method == "get_owner_agent" else False
            ),
        ):
            res = self.client.get("/api/v1/ai/agent/")
        body = res.json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(body["owner_agent"], "agent.testnet")
        self.assertFalse(body["revoked"])
