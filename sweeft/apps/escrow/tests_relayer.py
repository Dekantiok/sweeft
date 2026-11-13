"""Relayer tests — chain reads and signing are mocked; the automation logic is real.

The relayer submits permissionless on-chain calls; these tests pin that it only
acts on the right chain conditions and never double-settles.
"""
import json
from unittest import mock

from django.test import TestCase, override_settings

from apps.escrow import relayer
from apps.logistics.models import LogisticsPartner, Shipment, ShipmentEvent, VerificationRequest

OK = {"success": True, "tx_hash": "HASH", "error": None}


def make_shipment(**overrides):
    partner = LogisticsPartner.objects.create(
        company_name="Demo Carrier Logistics",
        email="ops@demo-carrier.test",
        settlement_wallet="logistics.testnet",
        status="ACTIVE",
    )
    data = {"escrow_id": "SWFT-000009", "provider": partner, "tracking_number": "DEMO-REL-1", "status": "DELIVERED"}
    data.update(overrides)
    shipment = Shipment.objects.create(**data)
    for status in ["CREATED", "ACCEPTED", "PICKED_UP", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED"]:
        ShipmentEvent.objects.create(shipment=shipment, status=status, note=f"scan {status}")
    return shipment


def chain(**overrides):
    data = {
        "id": "SWFT-000009",
        "state": "Shipped",
        "verification_pending": False,
        "pickup_verified": False,
        "delivered_verified": False,
        "logistics_paid": False,
        "seller_paid": False,
        "problem_reported": False,
        "inspection_period_ms": 60_000,
    }
    data.update(overrides)
    return data


@override_settings(
    RELAYER_ENABLED=True,
    RELAYER_ACCOUNT="relayer.testnet",
    RELAYER_PRIVATE_KEY="ed25519:dummy",
    SWEEFT_CONTRACT="sweeft-pay.testnet",
    RELAYER_VERIFICATION_DEPOSIT_YOCTO=10_000_000_000_000_000_000_000,
)
class RelayerTickTests(TestCase):
    def test_requests_verification_for_delivered_shipment(self):
        make_shipment()
        with mock.patch("apps.escrow.relayer.near.get_escrow", return_value=chain()), mock.patch(
            "apps.escrow.relayer.call_function_as", return_value=OK
        ) as send:
            report = relayer.relayer_tick()
        self.assertEqual(report.requested, ["SWFT-000009"])
        self.assertEqual(send.call_args.kwargs["method_name"], "request_delivery_verification")
        self.assertEqual(send.call_args.kwargs["deposit"], 10_000_000_000_000_000_000_000)
        payload = json.loads(send.call_args.kwargs["args"]["carrier_payload"])
        self.assertEqual(payload["status"], "DELIVERED")
        self.assertTrue(VerificationRequest.objects.filter(escrow_id="SWFT-000009", status="PENDING").exists())

    def test_no_request_while_verification_pending(self):
        make_shipment()
        with mock.patch(
            "apps.escrow.relayer.near.get_escrow", return_value=chain(verification_pending=True)
        ), mock.patch("apps.escrow.relayer.call_function_as", return_value=OK) as send:
            report = relayer.relayer_tick()
        self.assertEqual(report.requested, [])
        send.assert_not_called()

    def test_multiple_shipments_for_one_escrow_act_once(self):
        make_shipment(tracking_number="DEMO-REL-1")
        make_shipment(tracking_number="DEMO-REL-2")
        with mock.patch("apps.escrow.relayer.near.get_escrow", return_value=chain()), mock.patch(
            "apps.escrow.relayer.call_function_as", return_value=OK
        ) as send:
            report = relayer.relayer_tick()
        self.assertEqual(report.requested, ["SWFT-000009"])
        self.assertEqual(send.call_count, 1)

    def test_settles_logistics_after_verified_pickup(self):
        make_shipment()
        state = chain(
            state="AwaitingBuyerConfirmation",
            pickup_verified=True,
            delivered_verified=True,
            logistics_paid=False,
            last_verification={"verified": True},
        )
        with mock.patch("apps.escrow.relayer.near.get_escrow", return_value=state), mock.patch(
            "apps.escrow.relayer.near.view_call", return_value=None
        ), mock.patch("apps.escrow.relayer.call_function_as", return_value=OK) as send:
            report = relayer.relayer_tick()
        self.assertEqual(report.logistics_paid, ["SWFT-000009"])
        self.assertEqual(send.call_args.kwargs["method_name"], "settle_logistics")

    def test_releases_after_inspection_window(self):
        make_shipment()
        state = chain(
            state="AwaitingBuyerConfirmation",
            pickup_verified=True,
            delivered_verified=True,
            logistics_paid=True,
            last_verification={"verified": True},
        )
        # delivered at 1_000 ms, window 60_000 ms → deadline 61_000; now 62_000.
        with mock.patch("apps.escrow.relayer.near.get_escrow", return_value=state), mock.patch(
            "apps.escrow.relayer.near.view_call", return_value=1_000
        ), mock.patch("apps.escrow.relayer._now_ms", return_value=62_000), mock.patch(
            "apps.escrow.relayer.call_function_as", return_value=OK
        ) as send:
            report = relayer.relayer_tick()
        self.assertEqual(report.released, ["SWFT-000009"])
        self.assertEqual(send.call_args.kwargs["method_name"], "release_after_inspection")

    def test_waits_inside_the_inspection_window(self):
        make_shipment()
        state = chain(
            state="AwaitingBuyerConfirmation",
            pickup_verified=True,
            delivered_verified=True,
            logistics_paid=True,
            last_verification={"verified": True},
        )
        with mock.patch("apps.escrow.relayer.near.get_escrow", return_value=state), mock.patch(
            "apps.escrow.relayer.near.view_call", return_value=1_000
        ), mock.patch("apps.escrow.relayer._now_ms", return_value=30_000), mock.patch(
            "apps.escrow.relayer.call_function_as", return_value=OK
        ) as send:
            report = relayer.relayer_tick()
        self.assertEqual(report.released, [])
        send.assert_not_called()

    def test_problem_reported_blocks_release(self):
        make_shipment()
        state = chain(
            state="AwaitingBuyerConfirmation",
            pickup_verified=True,
            delivered_verified=True,
            logistics_paid=True,
            problem_reported=True,
            last_verification={"verified": True},
        )
        with mock.patch("apps.escrow.relayer.near.get_escrow", return_value=state), mock.patch(
            "apps.escrow.relayer.near.view_call", return_value=1_000
        ), mock.patch("apps.escrow.relayer._now_ms", return_value=999_999), mock.patch(
            "apps.escrow.relayer.call_function_as", return_value=OK
        ) as send:
            report = relayer.relayer_tick()
        self.assertEqual(report.released, [])
        send.assert_not_called()

    def test_syncs_pending_verification_record(self):
        shipment = make_shipment()
        VerificationRequest.objects.create(
            escrow_id="SWFT-000009", shipment=shipment, status="PENDING", requested_by="relayer.testnet"
        )
        state = chain(
            state="AwaitingBuyerConfirmation",
            pickup_verified=True,
            delivered_verified=True,
            logistics_paid=True,
            last_verification={"verified": True, "reported_status": "DELIVERED"},
        )
        with mock.patch("apps.escrow.relayer.near.get_escrow", return_value=state), mock.patch(
            "apps.escrow.relayer.near.view_call", return_value=None
        ), mock.patch("apps.escrow.relayer.call_function_as", return_value=OK):
            report = relayer.relayer_tick()
        self.assertEqual(report.synced, ["SWFT-000009"])
        self.assertEqual(VerificationRequest.objects.get(escrow_id="SWFT-000009").status, "VERIFIED")

    def test_reports_chain_failures_without_stopping(self):
        make_shipment()
        with mock.patch("apps.escrow.relayer.near.get_escrow", side_effect=RuntimeError("rpc down")), mock.patch(
            "apps.escrow.relayer.call_function_as", return_value=OK
        ) as send:
            report = relayer.relayer_tick()
        self.assertTrue(any("rpc down" in e for e in report.errors))
        send.assert_not_called()


class RelayerConfigTests(TestCase):
    @override_settings(RELAYER_ENABLED=False)
    def test_disabled_relayer_reports_not_configured(self):
        self.assertFalse(relayer.relayer_configured())
        report = relayer.relayer_tick()
        self.assertIn("not configured", report.errors[0])

    @override_settings(RELAYER_ENABLED=True, RELAYER_ACCOUNT="relayer.testnet", RELAYER_PRIVATE_KEY="")
    def test_missing_key_is_not_configured(self):
        self.assertFalse(relayer.relayer_configured())
