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
