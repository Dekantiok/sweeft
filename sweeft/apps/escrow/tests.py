"""Dispute workflow tests — chain reads are mocked; the case file is real."""
import json
from unittest import mock

from django.test import TestCase, override_settings

from apps.escrow.models import Dispute, DisputeEvidence


def chain_state(state="ReviewRequired", buyer="buyer.testnet", seller="seller.testnet"):
    return {
        "id": "VP-000009",
        "seller": seller,
        "buyer": buyer,
        "state": state,
        "terms_hash": "demo-terms",
        "total_amount": "115000000",
    }


class DisputeApiTests(TestCase):
    def setUp(self):
        self.session = self.client.session
        self.session["wallet"] = "buyer.testnet"
        self.session.save()

    def _post(self, url, body):
        return self.client.post(url, data=json.dumps(body), content_type="application/json")

    def test_open_dispute_records_case_file(self):
        with mock.patch("apps.escrow.disputes.near.get_escrow", return_value=chain_state()):
            res = self._post("/api/v1/disputes/", {"escrow_id": "VP-000009", "reason": "goods arrived damaged"})
        body = res.json()
        self.assertEqual(res.status_code, 200, body)
        self.assertTrue(body["ok"])
        d = Dispute.objects.get()
        self.assertEqual(d.side, "buyer")
        self.assertEqual(d.status, "open")

    def test_open_dispute_requires_review_state(self):
        with mock.patch("apps.escrow.disputes.near.get_escrow", return_value=chain_state(state="Shipped")):
            res = self._post("/api/v1/disputes/", {"escrow_id": "VP-000009", "reason": "x"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("open the dispute on-chain first", res.json()["error"])

    def test_non_party_rejected(self):
        self.session["wallet"] = "stranger.testnet"
        self.session.save()
        with mock.patch("apps.escrow.disputes.near.get_escrow", return_value=chain_state()):
            res = self._post("/api/v1/disputes/", {"escrow_id": "VP-000009", "reason": "x"})
        self.assertEqual(res.status_code, 403)

    def test_evidence_from_both_sides(self):
        with mock.patch("apps.escrow.disputes.near.get_escrow", return_value=chain_state()):
            self._post("/api/v1/disputes/", {"escrow_id": "VP-000009", "reason": "damaged"})
            d = Dispute.objects.get()
            buyer_ev = self._post(f"/api/v1/disputes/{d.id}/evidence/", {"text": "photos attached"})
            self.assertTrue(buyer_ev.json()["ok"])

        self.session["wallet"] = "seller.testnet"
        self.session.save()
        with mock.patch("apps.escrow.disputes.near.get_escrow", return_value=chain_state()):
            seller_ev = self._post(f"/api/v1/disputes/{d.id}/evidence/", {"text": "packed with extra padding"})
        self.assertTrue(seller_ev.json()["ok"])
        self.assertEqual(DisputeEvidence.objects.count(), 2)
        self.assertEqual({e.side for e in DisputeEvidence.objects.all()}, {"buyer", "seller"})

    @override_settings(AI_PROVIDER="echo")
    def test_ai_review_is_advisory_and_labelled(self):
        with mock.patch("apps.escrow.disputes.near.get_escrow", return_value=chain_state()):
            self._post("/api/v1/disputes/", {"escrow_id": "VP-000009", "reason": "damaged"})
            d = Dispute.objects.get()
            res = self._post(f"/api/v1/disputes/{d.id}/ai-review/", {})
        body = res.json()
        self.assertEqual(res.status_code, 200, body)
        self.assertEqual(body["recommendation"]["source"], "rules")  # AI_PROVIDER=echo default
        d.refresh_from_db()
        self.assertEqual(d.ai_recommendation["pct"], 50)

    def test_owner_only_resolution_recorded(self):
        with mock.patch("apps.escrow.disputes.near.get_escrow", return_value=chain_state()):
            self._post("/api/v1/disputes/", {"escrow_id": "VP-000009", "reason": "damaged"})
            d = Dispute.objects.get()

        # a party cannot resolve
        with mock.patch("apps.escrow.disputes._owner_account", return_value="owner.testnet"):
            res = self._post(f"/api/v1/disputes/{d.id}/resolve/", {"release_pct": 70})
        self.assertEqual(res.status_code, 403)

        # the owner records the resolution after the on-chain tx
        self.session["wallet"] = "owner.testnet"
        self.session.save()
        with mock.patch("apps.escrow.disputes._owner_account", return_value="owner.testnet"), mock.patch(
            "apps.escrow.disputes.near.get_escrow", return_value=chain_state(state="Completed")
        ):
            res = self._post(f"/api/v1/disputes/{d.id}/resolve/", {"release_pct": 70, "note": "partial", "tx_hash": "0xabc"})
        self.assertEqual(res.status_code, 200, res.json())
        d.refresh_from_db()
        self.assertEqual(d.status, "resolved")
        self.assertEqual(d.resolution_pct, 70)

    def test_resolution_requires_onchain_first(self):
        # The dispute is opened by a party first…
        with mock.patch("apps.escrow.disputes.near.get_escrow", return_value=chain_state()):
            self._post("/api/v1/disputes/", {"escrow_id": "VP-000009", "reason": "damaged"})
            d = Dispute.objects.get()
        # …then the owner attempts to record a resolution while the chain
        # still shows ReviewRequired: rejected until resolve_review is sent.
        self.session["wallet"] = "owner.testnet"
        self.session.save()
        with mock.patch("apps.escrow.disputes._owner_account", return_value="owner.testnet"), mock.patch(
            "apps.escrow.disputes.near.get_escrow", return_value=chain_state()
        ):
            res = self._post(f"/api/v1/disputes/{d.id}/resolve/", {"release_pct": 70})
        self.assertEqual(res.status_code, 400)
        self.assertIn("resolve on-chain first", res.json()["error"])
