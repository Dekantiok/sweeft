"""Seller visibility of incoming purchase requests (dashboard + nav + listing page).

Regression for: a buyer requests a listing, but the seller only saw it buried on
/market/mine/ — the dashboard, the nav badge and the listing page now surface it.
"""
from unittest import mock

from django.test import TestCase

from apps.market.models import Listing, PurchaseRequest

SELLER = "seller.example.testnet"
BUYER = "buyer.example.testnet"


class SellerRequestVisibilityTests(TestCase):
    def setUp(self):
        self.listing = Listing.objects.create(
            seller_account=SELLER,
            title="Visible listing",
            price_amount="1000000000000000000000",
        )
        self.request = PurchaseRequest.objects.create(
            listing=self.listing,
            buyer_account=BUYER,
            status="pending",
            message="please escrow this",
        )

    def _login(self, wallet):
        session = self.client.session
        session["wallet"] = wallet
        session.save()

    def test_dashboard_shows_pending_request_to_seller(self):
        self._login(SELLER)
        with mock.patch("apps.escrow.views.near.escrows_for", return_value=[]):
            res = self.client.get("/dashboard/")
        self.assertEqual(res.status_code, 200)
        html = res.content.decode()
        self.assertIn(BUYER, html)
        self.assertIn("Visible listing", html)
        self.assertIn("Purchase requests", html)

    def test_dashboard_does_not_show_other_sellers_requests(self):
        self._login("stranger.example.testnet")
        with mock.patch("apps.escrow.views.near.escrows_for", return_value=[]):
            res = self.client.get("/dashboard/")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn(BUYER, res.content.decode())

    def test_nav_badge_counts_pending_requests(self):
        self._login(SELLER)
        with mock.patch("apps.escrow.views.near.escrows_for", return_value=[]):
            res = self.client.get("/dashboard/")
        html = res.content.decode()
        self.assertIn("1 request", html)

    def test_listing_page_hints_owner_about_requests(self):
        self._login(SELLER)
        with mock.patch("apps.market.views._reputation", return_value={}):
            res = self.client.get(f"/market/{self.listing.slug}/")
        self.assertEqual(res.status_code, 200)
        html = res.content.decode()
        self.assertIn("1 purchase request", html)
        # The owner can act directly from the listing page with the buyer prefilled.
        self.assertIn(f"/market/{self.listing.slug}/escrow/?buyer={BUYER}", html)
        self.assertIn("/market/mine/", html)

    def test_market_mine_still_lists_the_request(self):
        self._login(SELLER)
        with mock.patch("apps.market.views._reputations", return_value={}):
            res = self.client.get("/market/mine/")
        self.assertEqual(res.status_code, 200)
        self.assertIn(BUYER, res.content.decode())

    def test_context_processor_zero_without_wallet(self):
        with mock.patch("apps.escrow.views.near.escrows_for", return_value=[]):
            res = self.client.get("/dashboard/")
        self.assertEqual(res.status_code, 200)
