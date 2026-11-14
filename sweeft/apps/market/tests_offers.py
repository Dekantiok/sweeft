"""Marketplace offer negotiation API tests (session wallet, no chain calls)."""
from __future__ import annotations

import json

from django.test import TestCase

from .models import Listing, Offer, PurchaseRequest

SELLER = "seller.testnet"
BUYER = "buyer.testnet"
OTHER = "other.testnet"

YOCTO_1 = "1000000000000000000000000"  # 1 NEAR
YOCTO_1_2 = "1200000000000000000000000"  # 1.2 NEAR


def post_json(client, url: str, payload: dict):
    return client.post(url, data=json.dumps(payload), content_type="application/json")


class OfferApiTests(TestCase):
    def setUp(self):
        self.listing = Listing.objects.create(
            seller_account=SELLER,
            title="Solar inverter batch",
            kind="goods",
            category="energy",
            description="5kVA units",
            price_amount=YOCTO_1_2,
            location="Abuja",
        )
        self.as_wallet(BUYER)

    def as_wallet(self, wallet: str | None):
        session = self.client.session
        if wallet is None:
            session.flush()
        else:
            session["wallet"] = wallet
        session.save()

    def create_offer(self, **overrides):
        body = {
            "listing_slug": self.listing.slug,
            "amount": YOCTO_1,
            "max_amount": YOCTO_1_2,
            "message": "Can do today",
        }
        body.update(overrides)
        return post_json(self.client, "/api/v1/market/offers/", body)

    # -- create ---------------------------------------------------------

    def test_create_offer_success(self):
        res = self.create_offer()
        self.assertEqual(res.status_code, 201, res.content)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["offer"]["status"], "proposed")
        self.assertEqual(data["offer"]["amount_display"], "1")
        self.assertEqual(data["offer"]["max_display"], "1.2")
        self.assertEqual(data["offer"]["listing"], self.listing.title)
        offer = Offer.objects.get()
        self.assertEqual(offer.buyer_account, BUYER)
        self.assertEqual(offer.seller_account, SELLER)
        self.assertEqual(offer.amount, YOCTO_1)
        self.assertEqual(offer.max_amount, YOCTO_1_2)
        self.assertEqual(len(offer.history), 1)
        self.assertEqual(offer.history[0]["side"], "buyer")
        self.assertEqual(offer.history[0]["action"], "proposed")
        self.assertEqual(offer.history[0]["message"], "Can do today")
        self.assertIn("at", offer.history[0])

    def test_create_accepts_decimal_near(self):
        res = self.create_offer(amount="1.5", max_amount="2")
        self.assertEqual(res.status_code, 201, res.content)
        offer = Offer.objects.get()
        self.assertEqual(offer.amount, "1500000000000000000000000")
        self.assertEqual(offer.max_amount, "2000000000000000000000000")

    def test_create_requires_wallet(self):
        self.as_wallet(None)
        res = self.create_offer()
        self.assertEqual(res.status_code, 401)

    def test_create_rejects_own_listing(self):
        self.as_wallet(SELLER)
        res = self.create_offer()
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Offer.objects.count(), 0)

    def test_create_rejects_inactive_listing(self):
        self.listing.status = "paused"
        self.listing.save(update_fields=["status"])
        res = self.create_offer()
        self.assertEqual(res.status_code, 400)

    def test_create_rejects_amount_over_max(self):
        res = self.create_offer(amount=YOCTO_1_2, max_amount=YOCTO_1)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(Offer.objects.count(), 0)

    def test_create_rejects_zero_amount(self):
        res = self.create_offer(amount="0")
        self.assertEqual(res.status_code, 400)

    def test_create_rejects_duplicate_open_offer(self):
        first = self.create_offer()
        self.assertEqual(first.status_code, 201)
        second = self.create_offer()
        self.assertEqual(second.status_code, 409)
        data = second.json()
        self.assertFalse(data["ok"])
        self.assertEqual(data["offer_id"], first.json()["offer"]["offer_id"])
        self.assertEqual(Offer.objects.count(), 1)

    # -- transitions & party checks -------------------------------------

    def test_counter_seller_only_and_updates_amount(self):
        offer_id = self.create_offer().json()["offer"]["offer_id"]
        self.as_wallet(OTHER)
        self.assertEqual(post_json(self.client, f"/api/v1/market/offers/{offer_id}/counter/", {"amount": YOCTO_1}).status_code, 403)
        self.as_wallet(SELLER)
        res = post_json(self.client, f"/api/v1/market/offers/{offer_id}/counter/", {"amount": YOCTO_1, "message": "Meet in the middle"})
        self.assertEqual(res.status_code, 200, res.content)
        offer = Offer.objects.get(pk=offer_id)
        self.assertEqual(offer.status, "countered")
        self.assertEqual(offer.amount, YOCTO_1)
        self.assertEqual(len(offer.history), 2)
        self.assertEqual(offer.history[1]["side"], "seller")
        self.assertEqual(offer.history[1]["action"], "countered")
        self.assertEqual(res.json()["offer"]["amount_display"], "1")

    def test_counter_rejects_zero_and_closed_offer(self):
        offer_id = self.create_offer().json()["offer"]["offer_id"]
        self.as_wallet(SELLER)
        self.assertEqual(post_json(self.client, f"/api/v1/market/offers/{offer_id}/counter/", {"amount": "0"}).status_code, 400)
        Offer.objects.filter(pk=offer_id).update(status="withdrawn")
        res = post_json(self.client, f"/api/v1/market/offers/{offer_id}/counter/", {"amount": YOCTO_1})
        self.assertEqual(res.status_code, 409)

    def test_accept_counter_buyer_only_creates_purchase_request(self):
        offer_id = self.create_offer().json()["offer"]["offer_id"]
        self.as_wallet(SELLER)
        post_json(self.client, f"/api/v1/market/offers/{offer_id}/counter/", {"amount": YOCTO_1})
        self.as_wallet(OTHER)
        self.assertEqual(self.client.post(f"/api/v1/market/offers/{offer_id}/accept-counter/").status_code, 403)
        self.as_wallet(BUYER)
        res = self.client.post(f"/api/v1/market/offers/{offer_id}/accept-counter/")
        self.assertEqual(res.status_code, 200, res.content)
        offer = Offer.objects.get(pk=offer_id)
        self.assertEqual(offer.status, "accepted")
        self.assertEqual(offer.history[-1]["action"], "accepted")
        self.assertEqual(offer.history[-1]["side"], "buyer")
        req = PurchaseRequest.objects.get(listing=self.listing, buyer_account=BUYER)
        self.assertEqual(req.status, "pending")
        self.assertIn("1 NEAR", req.message)
        self.assertIn(f"Offer #{offer_id}", req.message)

    def test_accept_counter_rejects_proposed_offer(self):
        offer_id = self.create_offer().json()["offer"]["offer_id"]
        res = self.client.post(f"/api/v1/market/offers/{offer_id}/accept-counter/")
        self.assertEqual(res.status_code, 409)

    def test_seller_accept_creates_purchase_request(self):
        offer_id = self.create_offer().json()["offer"]["offer_id"]
        self.as_wallet(SELLER)
        res = self.client.post(f"/api/v1/market/offers/{offer_id}/accept/")
        self.assertEqual(res.status_code, 200, res.content)
        offer = Offer.objects.get(pk=offer_id)
        self.assertEqual(offer.status, "accepted")
        self.assertTrue(PurchaseRequest.objects.filter(listing=self.listing, buyer_account=BUYER, status="pending").exists())

    def test_accept_rejects_wrong_party_and_closed_state(self):
        offer_id = self.create_offer().json()["offer"]["offer_id"]
        self.as_wallet(BUYER)
        self.assertEqual(self.client.post(f"/api/v1/market/offers/{offer_id}/accept/").status_code, 403)
        self.as_wallet(SELLER)
        self.assertEqual(self.client.post(f"/api/v1/market/offers/{offer_id}/reject/").status_code, 200)
        self.assertEqual(self.client.post(f"/api/v1/market/offers/{offer_id}/accept/").status_code, 409)

    def test_withdraw_buyer_only(self):
        offer_id = self.create_offer().json()["offer"]["offer_id"]
        self.as_wallet(SELLER)
        self.assertEqual(self.client.post(f"/api/v1/market/offers/{offer_id}/withdraw/").status_code, 403)
        self.as_wallet(BUYER)
        res = self.client.post(f"/api/v1/market/offers/{offer_id}/withdraw/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(Offer.objects.get(pk=offer_id).status, "withdrawn")
        # withdrawing a closed offer is a conflict
        self.assertEqual(self.client.post(f"/api/v1/market/offers/{offer_id}/withdraw/").status_code, 409)

    def test_reject_requires_open_offer(self):
        offer_id = self.create_offer().json()["offer"]["offer_id"]
        self.as_wallet(SELLER)
        self.assertEqual(self.client.post(f"/api/v1/market/offers/{offer_id}/reject/").status_code, 200)
        self.assertEqual(self.client.post(f"/api/v1/market/offers/{offer_id}/reject/").status_code, 409)

    # -- mine -----------------------------------------------------------

    def test_mine_lists_buyer_and_seller_offers(self):
        offer_id = self.create_offer().json()["offer"]["offer_id"]
        res = self.client.get("/api/v1/market/offers/mine/")
        self.assertEqual(res.status_code, 200)
        rows = res.json()["offers"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["offer_id"], offer_id)
        self.assertEqual(rows[0]["listing_slug"], self.listing.slug)
        self.assertEqual(rows[0]["listing"], self.listing.title)
        self.assertEqual(rows[0]["amount_display"], "1")
        self.assertEqual(len(rows[0]["history"]), 1)
        self.as_wallet(SELLER)
        self.assertEqual(len(self.client.get("/api/v1/market/offers/mine/").json()["offers"]), 1)
        self.as_wallet(OTHER)
        self.assertEqual(len(self.client.get("/api/v1/market/offers/mine/").json()["offers"]), 0)

    def test_mine_requires_wallet(self):
        self.as_wallet(None)
        self.assertEqual(self.client.get("/api/v1/market/offers/mine/").status_code, 401)

    # -- escrow prefill --------------------------------------------------

    def test_prefill_uses_accepted_offer_amount(self):
        offer_id = self.create_offer().json()["offer"]["offer_id"]
        self.as_wallet(SELLER)
        self.client.post(f"/api/v1/market/offers/{offer_id}/accept/")
        res = self.client.get(f"/market/{self.listing.slug}/escrow/?buyer={BUYER}&offer={offer_id}")
        self.assertEqual(res.status_code, 302)
        self.assertEqual(
            res["Location"],
            f"/escrow/new/?listing={self.listing.slug}&buyer={BUYER}&offer={offer_id}",
        )
        page = self.client.get(
            f"/escrow/new/?listing={self.listing.slug}&buyer={BUYER}&offer={offer_id}"
        )
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["prefill"]["amount"], "1")
        self.assertIn("accepted offer", page.context["prefill"]["terms"])
        self.assertEqual(page.context["prefill"]["counterparty"], BUYER)

    def test_prefill_falls_back_for_unaccepted_offer(self):
        offer_id = self.create_offer().json()["offer"]["offer_id"]
        self.as_wallet(SELLER)
        page = self.client.get(
            f"/escrow/new/?listing={self.listing.slug}&buyer={BUYER}&offer={offer_id}"
        )
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["prefill"]["amount"], "1.2")
        self.assertIn("listed on the SWEEFT marketplace", page.context["prefill"]["terms"])

    def test_prefill_keeps_listing_price_without_offer(self):
        self.as_wallet(SELLER)
        page = self.client.get(f"/escrow/new/?listing={self.listing.slug}&buyer={BUYER}")
        self.assertEqual(page.status_code, 200)
        self.assertEqual(page.context["prefill"]["amount"], "1.2")

    def test_prefill_rejects_non_seller(self):
        res = self.client.get(f"/market/{self.listing.slug}/escrow/?buyer={BUYER}")
        self.assertEqual(res.status_code, 302)
        self.assertEqual(res["Location"], f"/market/{self.listing.slug}/")

    # -- pages -----------------------------------------------------------

    def test_listing_mine_page_renders_open_offers(self):
        self.create_offer()
        self.as_wallet(SELLER)
        res = self.client.get("/market/mine/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Open offers in negotiation")
        self.assertContains(res, BUYER)
        self.assertContains(res, "Counter")

    def test_listing_detail_page_shows_offer_form_and_status(self):
        res = self.client.get(f"/market/{self.listing.slug}/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Make an offer")
        self.create_offer()
        res = self.client.get(f"/market/{self.listing.slug}/")
        self.assertEqual(res.status_code, 200)
        self.assertContains(res, "Your offer #")
