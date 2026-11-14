"""Demo Carrier + partner API tests — simulated data, real state machine.

The state machine guards what OutLayer validates: forward-only monotonic
timelines, failure statuses only at the end, no duplicate events.
"""
import json
from unittest import mock

from django.test import TestCase, override_settings

from apps.logistics import carriers
from apps.logistics.models import LogisticsPartner, PartnerApiCredential, Shipment, ShipmentEvent

HAPPY_FLOW_AFTER_CREATED = ["ACCEPTED", "PICKED_UP", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED"]


def make_partner(**overrides):
    data = {
        "company_name": "Demo Carrier Logistics",
        "email": "ops@demo-carrier.test",
        "settlement_wallet": "logistics.testnet",
        "owner_wallet": "owner.testnet",
        "status": "ACTIVE",
    }
    data.update(overrides)
    return LogisticsPartner.objects.create(**data)


def make_shipment(partner=None, **overrides):
    data = {
        "escrow_id": "SWFT-100001",
        "provider": partner,
        "tracking_number": "DEMO-TEST-1",
        "status": "CREATED",
    }
    data.update(overrides)
    shipment = Shipment.objects.create(**data)
    ShipmentEvent.objects.create(shipment=shipment, status="CREATED", note="Tracking created (simulated)")
    return shipment


class CarrierAdvanceTests(TestCase):
    def setUp(self):
        self.shipment = make_shipment()

    def test_full_happy_flow(self):
        carrier = carriers.get_carrier()
        for status in HAPPY_FLOW_AFTER_CREATED:
            carrier.advance(self.shipment, to_status=status)
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, "DELIVERED")
        self.assertEqual(self.shipment.events.count(), 6)

    def test_reposting_current_status_is_idempotent(self):
        carrier = carriers.get_carrier()
        first = carrier.advance(self.shipment, to_status="PICKED_UP")
        again = carrier.advance(self.shipment, to_status="PICKED_UP")
        self.assertEqual(first.id, again.id)
        self.assertEqual(self.shipment.events.filter(status="PICKED_UP").count(), 1)

    def test_backwards_rejected(self):
        carrier = carriers.get_carrier()
        carrier.advance(self.shipment, to_status="IN_TRANSIT")
        with self.assertRaises(ValueError):
            carrier.advance(self.shipment, to_status="PICKED_UP")

    def test_failure_is_terminal_and_idempotent(self):
        carrier = carriers.get_carrier()
        carrier.advance(self.shipment, to_status="LOST")
        with self.assertRaises(ValueError):
            carrier.advance(self.shipment, to_status="IN_TRANSIT")
        event = carrier.advance(self.shipment, to_status="LOST")  # re-post = no-op
        self.assertEqual(event.status, "LOST")
        self.assertEqual(self.shipment.events.filter(status="LOST").count(), 1)

    def test_unknown_status_rejected(self):
        with self.assertRaises(ValueError):
            carriers.get_carrier().advance(self.shipment, to_status="TELEPORTED")


class PartnerApiTests(TestCase):
    def setUp(self):
        self.partner = make_partner()
        self.raw_key, _ = PartnerApiCredential.generate(self.partner)
        self.shipment = make_shipment(partner=self.partner)

    def _post(self, url, body):
        return self.client.post(
            url, data=json.dumps(body), content_type="application/json", HTTP_X_API_KEY=self.raw_key
        )

    def test_shipments_require_key(self):
        res = self.client.get("/api/v1/partners/shipments/")
        self.assertEqual(res.status_code, 401)

    def test_shipments_payload_carries_timeline_and_scenario(self):
        res = self.client.get("/api/v1/partners/shipments/", HTTP_X_API_KEY=self.raw_key)
        body = res.json()
        self.assertTrue(body["ok"])
        row = body["shipments"][0]
        self.assertEqual(row["simulation_scenario"], "delivered")
        self.assertEqual([e["status"] for e in row["events"]], ["CREATED"])
        self.assertNotIn("escrow_state", row)  # chain read is opt-in

    def test_chain_state_opt_in_uses_cached_read(self):
        with mock.patch("apps.logistics.views.near.get_escrow_cached", return_value={"state": "Shipped"}) as cached:
            res = self.client.get(
                "/api/v1/partners/shipments/?include_escrow_state=1", HTTP_X_API_KEY=self.raw_key
            )
        self.assertEqual(res.json()["shipments"][0]["escrow_state"], "Shipped")
        cached.assert_called_once_with("SWFT-100001")

    def test_chain_read_failure_does_not_break_polling(self):
        with mock.patch("apps.logistics.views.near.get_escrow_cached", side_effect=RuntimeError("rpc down")):
            res = self.client.get(
                "/api/v1/partners/shipments/?include_escrow_state=1", HTTP_X_API_KEY=self.raw_key
            )
        self.assertEqual(res.status_code, 200)
        self.assertIsNone(res.json()["shipments"][0]["escrow_state"])

    def test_partner_event_roundtrip(self):
        res = self._post(
            f"/api/v1/partners/shipments/{self.shipment.id}/events/",
            {"status": "PICKED_UP", "note": "Hub scan"},
        )
        self.assertEqual(res.status_code, 200, res.json())
        self.shipment.refresh_from_db()
        self.assertEqual(self.shipment.status, "PICKED_UP")

    def test_partner_event_backwards_rejected(self):
        carriers.get_carrier().advance(self.shipment, to_status="IN_TRANSIT")
        res = self._post(f"/api/v1/partners/shipments/{self.shipment.id}/events/", {"status": "ACCEPTED"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("backwards", res.json()["error"])

    def test_other_partners_shipment_not_reachable(self):
        other = make_partner(company_name="Other Logistics", owner_wallet="other.testnet")
        foreign = make_shipment(partner=other, tracking_number="DEMO-OTHER-1", escrow_id="SWFT-100002")
        res = self._post(f"/api/v1/partners/shipments/{foreign.id}/events/", {"status": "ACCEPTED"})
        self.assertEqual(res.status_code, 404)


class WalletShipmentApiTests(TestCase):
    def setUp(self):
        self.session = self.client.session
        self.session["wallet"] = "seller.testnet"
        self.session.save()

    def _post(self, url, body):
        return self.client.post(url, data=json.dumps(body), content_type="application/json")

    @staticmethod
    def chain(**overrides):
        data = {
            "id": "SWFT-100003",
            "state": "Funded",
            "seller": "seller.testnet",
            "buyer": "buyer.testnet",
            "logistics_provider": None,
        }
        data.update(overrides)
        return data

    def test_create_shipment_stores_scenario(self):
        with mock.patch("apps.logistics.views.near.get_escrow", return_value=self.chain()):
            res = self._post("/api/v1/shipments/", {"escrow_id": "SWFT-100003", "scenario": "failed"})
        body = res.json()
        self.assertEqual(res.status_code, 200, body)
        self.assertEqual(body["scenario"], "failed")
        self.assertEqual(Shipment.objects.get().simulation_scenario, "failed")

    def test_create_shipment_rejects_unknown_scenario(self):
        with mock.patch("apps.logistics.views.near.get_escrow", return_value=self.chain()):
            res = self._post("/api/v1/shipments/", {"escrow_id": "SWFT-100003", "scenario": "explode"})
        self.assertEqual(res.status_code, 400)

    def test_pre_registered_shipment_scenario_updated_at_dispatch(self):
        # Marketplace flow: the row exists from escrow creation; the scenario
        # chosen at dispatch must still take effect while the run hasn't started.
        make_shipment(escrow_id="SWFT-100003", tracking_number="DEMO-PRE-1", simulation_scenario="delivered")
        with mock.patch("apps.logistics.views.near.get_escrow", return_value=self.chain()):
            res = self._post(
                "/api/v1/shipments/",
                {"escrow_id": "SWFT-100003", "tracking_number": "DEMO-PRE-1", "scenario": "lost"},
            )
        self.assertEqual(res.status_code, 200, res.json())
        self.assertEqual(res.json()["scenario"], "lost")
        self.assertEqual(Shipment.objects.get(tracking_number="DEMO-PRE-1").simulation_scenario, "lost")

    def test_lookup_by_escrow_returns_timeline(self):
        make_shipment(escrow_id="SWFT-100003", tracking_number="DEMO-LOOKUP-1")
        res = self.client.get("/api/v1/shipments/by-escrow/SWFT-100003/")
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["shipment"]["tracking_number"], "DEMO-LOOKUP-1")
        self.assertEqual(body["shipment"]["events"][0]["status"], "CREATED")
        self.assertIn("simulation_scenario", body["shipment"])

    def test_lookup_missing_returns_null(self):
        res = self.client.get("/api/v1/shipments/by-escrow/SWFT-NONE/")
        self.assertEqual(res.json(), {"ok": True, "shipment": None})

    def test_verification_payload_matches_full_carrier_run(self):
        shipment = make_shipment(escrow_id="SWFT-000004", tracking_number="DEMO-VERIFY-1")
        carrier = carriers.get_carrier()
        for status in HAPPY_FLOW_AFTER_CREATED:
            carrier.advance(shipment, to_status=status)
        res = self._post("/api/v1/verification/payload/", {"escrow_id": "SWFT-000004"})
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertTrue(body["simulated"])
        payload = json.loads(body["carrier_payload"])
        statuses = [e["status"] for e in payload["events"]]
        # These are exactly the invariants the OutLayer module enforces.
        self.assertEqual(statuses[0], "CREATED")
        self.assertEqual(statuses[-1], "DELIVERED")
        self.assertIn("PICKED_UP", statuses)
        self.assertEqual(payload["status"], "DELIVERED")


class CarrierPayloadSigningTests(TestCase):
    """Carrier-authenticity: canonical message + ed25519 signature."""

    SEED_HEX = "07" * 32  # matches the verifier's test key

    def _payload(self):
        return {
            "provider_id": "LOG-DEMO-001",
            "tracking_number": "DEMO-1",
            "shipment_id": "SHIP-SWFT-100001",
            "status": "DELIVERED",
            "events": [{"status": "PICKED_UP"}, {"status": "DELIVERED"}],
        }

    def test_canonical_message_matches_verifier_format(self):
        self.assertEqual(
            carriers.canonical_carrier_message(self._payload()),
            "v1|LOG-DEMO-001|DEMO-1|SHIP-SWFT-100001|DELIVERED|0|PICKED_UP,DELIVERED",
        )

    @override_settings(DEMO_CARRIER_PRIVATE_KEY=SEED_HEX)
    def test_signed_payload_verifies_with_pubkey(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        payload = carriers.sign_carrier_payload(self._payload())
        self.assertIn("signature", payload)
        key = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(self.SEED_HEX))
        # Verifies over the canonical message (signature field itself excluded).
        key.public_key().verify(
            bytes.fromhex(payload["signature"]),
            carriers.canonical_carrier_message(payload).encode(),
        )

    @override_settings(DEMO_CARRIER_PRIVATE_KEY="")
    def test_signing_fails_closed_without_key_configuration(self):
        with self.assertRaisesMessage(ValueError, "Demo Carrier signing key is not configured"):
            carriers.sign_carrier_payload(self._payload())
