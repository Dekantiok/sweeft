"""Agent and tool tests (chain reads mocked, no network)."""
import json
from unittest import mock

from django.test import TestCase, override_settings

from apps.market.models import Listing, PurchaseRequest

from . import agent, tools


def rep(**kwargs):
    base = {
        "completed_as_seller": 0,
        "completed_as_buyer": 0,
        "disputes_opened": 0,
        "refunds_claimed": 0,
        "requests_rejected": 0,
        "volume": "0",
        "first_seen_ms": 0,
    }
    base.update(kwargs)
    return base


class ToolTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Listing.objects.create(
            seller_account="seller.testnet", title="Solar inverter batch", kind="goods", category="energy",
            description="5kVA units", price_amount="1200000000", location="Abuja",
        )
        Listing.objects.create(
            seller_account="cheap.testnet", title="Cotton fabric", kind="goods", category="textiles",
            description="200 units", price_amount="850000000", location="Lagos",
        )

    def test_search_ranks_by_reputation(self):
        # Listings are ordered newest-first, so mock reputation by account,
        # not by position.
        with mock.patch(
            "apps.ai.tools._reputation_of",
            side_effect=lambda acct: rep(completed_as_seller=2 if acct == "seller.testnet" else 9),
        ):
            result = tools.search_listings(query="")
        self.assertEqual(result["count"], 2)
        # the listing with the stronger seller record sorts first
        self.assertEqual(result["results"][0]["title"], "Cotton fabric")
        self.assertIn("completed sales", result["results"][0]["seller_score"])

    def test_plural_matches_singular_listing(self):
        with mock.patch("apps.ai.tools._reputation_of", return_value=rep()):
            result = tools.search_listings(query="solar inverters")
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["title"], "Solar inverter batch")

    def test_multi_token_and_semantics(self):
        with mock.patch("apps.ai.tools._reputation_of", return_value=rep()):
            result = tools.search_listings(query="inverter abuja")
        self.assertEqual(result["count"], 1)

    def test_price_filter(self):
        with mock.patch("apps.ai.tools._reputation_of", return_value=rep()):
            result = tools.search_listings(query="", max_price=900)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["title"], "Cotton fabric")

    def test_kind_filter(self):
        with mock.patch("apps.ai.tools._reputation_of", return_value=rep()):
            result = tools.search_listings(query="", kind="service")
        self.assertEqual(result["count"], 0)

    def test_unknown_tool_rejected(self):
        with self.assertRaises(tools.ToolError):
            tools.execute_tool("send_funds_to_anyone", {}, "buyer.testnet")

    def test_balance(self):
        with mock.patch("apps.ai.tools.near.token_balance", return_value="500000000"):
            result = tools.check_my_balance(wallet="me.testnet")
        self.assertEqual(result["balance"], "500000000")
        self.assertEqual(result["balance_display"], "500")

    def test_purchase_request_idempotent_and_owner_guard(self):
        listing = Listing.objects.get(title="Solar inverter batch")
        with mock.patch("apps.ai.tools._reputation_of", return_value=rep(completed_as_seller=3)):
            with mock.patch("apps.ai.tools.check_my_balance", return_value={"balance_display": "500"}):
                first = tools.send_purchase_request(listing_slug=listing.slug, wallet="buyer.testnet")
                second = tools.send_purchase_request(listing_slug=listing.slug, wallet="buyer.testnet")
        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(PurchaseRequest.objects.count(), 1)
        with self.assertRaises(tools.ToolError):
            tools.send_purchase_request(listing_slug=listing.slug, wallet="seller.testnet")

    def test_prepare_purchase_affordability(self):
        listing = Listing.objects.get(title="Solar inverter batch")
        with mock.patch("apps.ai.tools._reputation_of", return_value=rep(completed_as_seller=3)), mock.patch(
            "apps.ai.tools.near.token_balance", return_value="500000000000"
        ):
            card = tools.prepare_purchase(listing_slug=listing.slug, wallet="buyer.testnet")
        self.assertEqual(card["type"], "purchase_proposal")
        self.assertTrue(card["affordable"])
        self.assertIn("seller", card)

    def test_funding_requires_buyer(self):
        with mock.patch("apps.ai.tools.near.get_escrow", return_value={"id": "VP-1", "buyer": "other.testnet", "state": "Approved", "total_amount": "115000000"}):
            with self.assertRaises(tools.ToolError):
                tools.prepare_funding(escrow_id="VP-1", wallet="me.testnet")

    def test_dispute_requires_reason(self):
        with mock.patch("apps.ai.tools.near.get_escrow", return_value={"id": "VP-1", "buyer": "me.testnet", "seller": "s.testnet", "state": "AwaitingBuyerConfirmation"}):
            with self.assertRaises(tools.ToolError):
                tools.prepare_dispute(escrow_id="VP-1", wallet="me.testnet", reason="")


class KeywordRouteTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Listing.objects.create(
            seller_account="seller.testnet", title="Solar inverter batch", kind="goods", category="energy",
            description="5kVA units", price_amount="1200000000", location="Abuja",
        )

    @override_settings(AI_PROVIDER="echo")
    def test_buy_flow_routes_to_proposal(self):
        with mock.patch("apps.ai.tools._reputation_of", return_value=rep(completed_as_seller=4)), mock.patch(
            "apps.ai.tools.near.token_balance", return_value="500000000000"
        ):
            result = agent.keyword_route("buy solar inverter under 1500 tUSD", "buyer.testnet")
        self.assertTrue(result["cards"])
        self.assertEqual(result["cards"][0]["kind"], "purchase_proposal")
        self.assertTrue(any(t["tool"] == "search_listings" for t in result["tool_trace"]))
        self.assertTrue(any(t["tool"] == "prepare_purchase" for t in result["tool_trace"]))

    @override_settings(AI_PROVIDER="echo")
    def test_balance_route(self):
        with mock.patch("apps.ai.tools.near.token_balance", return_value="499680000"):
            result = agent.keyword_route("check my balance", "buyer.testnet")
        self.assertIn("499.68", result["reply"])

    @override_settings(AI_PROVIDER="echo")
    def test_dispute_route(self):
        with mock.patch("apps.ai.tools.near.get_escrow", return_value={"id": "VP-000006", "buyer": "me.testnet", "seller": "s.testnet", "state": "AwaitingBuyerConfirmation"}):
            result = agent.keyword_route("dispute VP-000006 goods arrived damaged", "me.testnet")
        self.assertEqual(result["cards"][0]["kind"], "dispute_proposal")

    @override_settings(AI_PROVIDER="echo")
    def test_score_route(self):
        with mock.patch("apps.ai.tools._reputation_of", return_value=rep(completed_as_seller=2)):
            result = agent.keyword_route("what is the score for seller.steepdata3968.testnet", "buyer.testnet")
        self.assertIn("completed sales", result["reply"])


class PaymentToolTests(TestCase):
    def test_prepare_payment_validates_inputs(self):
        with self.assertRaises(tools.ToolError):
            tools.prepare_payment(to="not-an-account", amount="5", wallet="buyer.testnet")
        with self.assertRaises(tools.ToolError):
            tools.prepare_payment(to="seller.testnet", amount="0", wallet="buyer.testnet")
        with self.assertRaises(tools.ToolError):
            tools.prepare_payment(to="buyer.testnet", amount="5", wallet="buyer.testnet")

    @override_settings(AI_AGENT_MAX_PAYMENT=100_000_000)
    def test_prepare_payment_caps_amount(self):
        with self.assertRaises(tools.ToolError):
            tools.prepare_payment(to="seller.testnet", amount="100.01", wallet="buyer.testnet")

    @override_settings(
        AI_AGENT_ACCOUNT="agent.testnet",
        AI_AGENT_PRIVATE_KEY="ed25519:dummy",
        AI_AGENT_ENABLED=True,
        AI_AGENT_MAX_PAYMENT=100_000_000,
        AI_AGENT_GRANT=250_000_000,
    )
    def test_prepare_payment_card_and_intent(self):
        from .models import PaymentIntent

        with mock.patch("apps.ai.tools.allowance_of", return_value=300_000_000):
            card = tools.prepare_payment(to="seller.testnet", amount="25", memo="invoice 12", wallet="buyer.testnet")
        self.assertEqual(card["type"], "payment_proposal")
        self.assertEqual(card["amount"], "25000000")
        self.assertEqual(card["amount_display"], "25")
        self.assertTrue(card["can_execute"])
        self.assertEqual(card["allowance_display"], "300")
        intent = PaymentIntent.objects.get(pk=card["intent_id"])
        self.assertEqual(intent.wallet, "buyer.testnet")
        self.assertEqual(intent.to_account, "seller.testnet")
        self.assertEqual(intent.status, "proposed")

        with mock.patch("apps.ai.tools.allowance_of", return_value=1_000_000):
            card2 = tools.prepare_payment(to="seller.testnet", amount="25", wallet="buyer.testnet")
        self.assertFalse(card2["can_execute"])  # allowance below amount

    def test_allowance_falls_back_to_zero(self):
        with override_settings(AI_AGENT_ACCOUNT="agent.testnet"):
            with mock.patch("apps.ai.tools.near.view_call", side_effect=RuntimeError("no method")):
                self.assertEqual(tools.allowance_of("buyer.testnet"), 0)
            with mock.patch("apps.ai.tools.near.view_call", return_value="42000000"):
                self.assertEqual(tools.allowance_of("buyer.testnet"), 42_000_000)


class PaymentRouteTests(TestCase):
    @override_settings(AI_PROVIDER="echo", AI_AGENT_ACCOUNT="agent.testnet")
    def test_pay_route_builds_payment_card(self):
        with mock.patch("apps.ai.tools.allowance_of", return_value=0):
            result = agent.keyword_route("pay 25 tUSD to seller.testnet for invoice 12", "buyer.testnet")
        self.assertTrue(result["cards"], result["reply"])
        card = result["cards"][0]
        self.assertEqual(card["kind"], "payment_proposal")
        self.assertEqual(card["to"], "seller.testnet")
        self.assertEqual(card["amount"], "25000000")
        self.assertEqual(card["memo"], "invoice 12")
        self.assertIn("seller.testnet", result["reply"])

    @override_settings(AI_PROVIDER="echo")
    def test_pay_route_without_amount_asks(self):
        result = agent.keyword_route("pay seller.testnet please", "buyer.testnet")
        self.assertFalse(result["cards"])
        self.assertIn("amount", result["reply"].lower())


class PaymentEndpointTests(TestCase):
    def setUp(self):
        self.session = self.client.session
        self.session["wallet"] = "buyer.testnet"
        self.session.save()

    def _intent(self, **kwargs):
        from .models import PaymentIntent

        defaults = dict(wallet="buyer.testnet", to_account="seller.testnet", amount="25000000", memo="invoice 12")
        defaults.update(kwargs)
        return PaymentIntent.objects.create(**defaults)

    def test_execute_requires_wallet(self):
        self.client.session.flush()
        intent = self._intent()
        res = self.client.post(f"/api/v1/ai/payments/{intent.id}/execute/")
        self.assertEqual(res.status_code, 401)

    def test_execute_wrong_wallet_404(self):
        intent = self._intent(wallet="someone-else.testnet")
        res = self.client.post(f"/api/v1/ai/payments/{intent.id}/execute/")
        self.assertEqual(res.status_code, 404)

    @override_settings(AI_AGENT_ACCOUNT="agent.testnet", AI_AGENT_PRIVATE_KEY="ed25519:dummy", AI_AGENT_ENABLED=True)
    def test_execute_success_records_tx(self):
        intent = self._intent()
        with mock.patch(
            "apps.ai.views.transfer_from_allowance",
            return_value={"success": True, "tx_hash": "HASH123", "error": None},
        ) as signer:
            res = self.client.post(f"/api/v1/ai/payments/{intent.id}/execute/")
        body = res.json()
        self.assertEqual(res.status_code, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["tx_hash"], "HASH123")
        intent.refresh_from_db()
        self.assertEqual(intent.status, "executed")
        self.assertEqual(intent.tx_hash, "HASH123")
        # idempotent: executing again returns the same hash without re-signing
        with mock.patch("apps.ai.views.transfer_from_allowance") as signer2:
            res2 = self.client.post(f"/api/v1/ai/payments/{intent.id}/execute/")
        self.assertEqual(res2.json()["tx_hash"], "HASH123")
        signer2.assert_not_called()

    @override_settings(AI_AGENT_ACCOUNT="agent.testnet", AI_AGENT_PRIVATE_KEY="ed25519:dummy", AI_AGENT_ENABLED=True)
    def test_execute_chain_failure_marks_failed(self):
        intent = self._intent()
        with mock.patch(
            "apps.ai.views.transfer_from_allowance",
            return_value={"success": False, "tx_hash": "HASHFAIL", "error": "allowance exceeded"},
        ):
            res = self.client.post(f"/api/v1/ai/payments/{intent.id}/execute/")
        self.assertEqual(res.status_code, 502)
        intent.refresh_from_db()
        self.assertEqual(intent.status, "failed")
        self.assertIn("allowance exceeded", intent.error)

    @override_settings(AI_AGENT_ACCOUNT="agent.testnet", AI_AGENT_PRIVATE_KEY="ed25519:dummy", AI_AGENT_ENABLED=True, AI_AGENT_MAX_PAYMENT=1_000_000)
    def test_execute_respects_payment_cap(self):
        intent = self._intent(amount="25000000")
        res = self.client.post(f"/api/v1/ai/payments/{intent.id}/execute/")
        self.assertEqual(res.status_code, 400)

    def test_agent_info_endpoint(self):
        with mock.patch("apps.ai.views.tools.allowance_of", return_value=42_000_000):
            res = self.client.get("/api/v1/ai/agent/")
        body = res.json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(body["wallet"], "buyer.testnet")
        self.assertEqual(body["allowance_display"], "42")


class EndpointTests(TestCase):
    def setUp(self):
        self.session = self.client.session
        self.session["wallet"] = "buyer.testnet"
        self.session.save()

    def test_chat_requires_wallet(self):
        self.client.session.flush()
        res = self.client.post("/api/v1/ai/chat/", data=json.dumps({"message": "hi"}), content_type="application/json")
        self.assertEqual(res.status_code, 401)

    @override_settings(AI_PROVIDER="echo")
    def test_chat_endpoint(self):
        with mock.patch("apps.ai.tools._reputation_of", return_value=rep()), mock.patch(
            "apps.ai.tools.check_my_balance", return_value={"balance_display": "500"}
        ):
            res = self.client.post(
                "/api/v1/ai/chat/",
                data=json.dumps({"message": "buy solar inverter under 1500"}),
                content_type="application/json",
            )
        body = res.json()
        self.assertEqual(res.status_code, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["wallet"], "buyer.testnet")

    def test_chat_requires_message(self):
        res = self.client.post("/api/v1/ai/chat/", data=json.dumps({}), content_type="application/json")
        self.assertEqual(res.status_code, 400)
