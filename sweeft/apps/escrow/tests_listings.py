"""Marketplace ↔ escrow reconciliation tests (chain state is plain dicts)."""
from django.test import TestCase

from apps.escrow.views import reconcile_listing
from apps.market.models import Listing, PurchaseRequest

SELLER = "seller.testnet"
BUYER = "buyer.testnet"


def chain(state, seller_paid=False, **overrides):
    data = {
        "id": "SWFT-000001",
        "seller": SELLER,
        "buyer": BUYER,
        "state": state,
        "seller_paid": seller_paid,
    }
    data.update(overrides)
    return data


class ReconcileListingTests(TestCase):
    def setUp(self):
        self.listing = Listing.objects.create(
            seller_account=SELLER,
            title="Reconcile test listing",
            price_amount="1000000000000000000",
        )
        self.request = PurchaseRequest.objects.create(
            listing=self.listing,
            buyer_account=BUYER,
            status="escrowed",
            escrow_id="SWFT-000001",
        )

    def test_approved_reserves_and_funded_sells(self):
        reconcile_listing(chain("Approved"), "SWFT-000001")
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, "reserved")
        self.assertEqual(self.listing.sold_escrow_id, "SWFT-000001")

        reconcile_listing(chain("Funded"), "SWFT-000001")
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, "sold")

    def test_pending_acceptance_leaves_listing_active(self):
        reconcile_listing(chain("PendingAcceptance"), "SWFT-000001")
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, "active")
        self.assertEqual(self.listing.sold_escrow_id, "")

    def test_rejected_reopens_listing_and_request(self):
        reconcile_listing(chain("Funded"), "SWFT-000001")
        reconcile_listing(chain("Rejected"), "SWFT-000001")
        self.listing.refresh_from_db()
        self.request.refresh_from_db()
        self.assertEqual(self.listing.status, "active")
        self.assertEqual(self.listing.sold_escrow_id, "")
        self.assertEqual(self.request.status, "pending")
        self.assertEqual(self.request.escrow_id, "")

    def test_cancelled_reopens_listing(self):
        reconcile_listing(chain("Approved"), "SWFT-000001")
        reconcile_listing(chain("Cancelled"), "SWFT-000001")
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, "active")

    def test_full_refund_without_seller_payout_reopens_listing(self):
        reconcile_listing(chain("Funded"), "SWFT-000001")
        reconcile_listing(chain("Refunded", seller_paid=False), "SWFT-000001")
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, "active")
        self.assertEqual(self.listing.sold_escrow_id, "")

    def test_partial_split_that_paid_the_seller_keeps_listing_sold(self):
        reconcile_listing(chain("Funded"), "SWFT-000001")
        # Dispute split: buyer refunded but the seller was paid a share.
        reconcile_listing(chain("Refunded", seller_paid=True), "SWFT-000001")
        self.listing.refresh_from_db()
        self.assertEqual(self.listing.status, "sold")
        self.assertEqual(self.listing.sold_escrow_id, "SWFT-000001")
