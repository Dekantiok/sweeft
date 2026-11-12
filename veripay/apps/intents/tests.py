"""Intents layer tests: dry provider behaviour + endpoint contracts."""
import json
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.intents.providers.base import IntentsError
from apps.intents.registry import get_intents_provider, intents_status


class RegistryTests(SimpleTestCase):
    @override_settings(INTENTS_PROVIDER="dry")
    def test_dry_default(self):
        provider = get_intents_provider()
        self.assertEqual(provider.name, "dry")
        self.assertTrue(intents_status()["dry_run"])

    def test_unknown_provider_raises(self):
        with override_settings(INTENTS_PROVIDER="nope"):
            with self.assertRaises(IntentsError):
                get_intents_provider()


class DryProviderTests(SimpleTestCase):
    def test_quote_is_deterministic_and_labelled(self):
        provider = get_intents_provider()
        q1 = provider.quote(asset_out="nep141:tusd", amount_out="115000000")
        q2 = provider.quote(asset_out="nep141:tusd", amount_out="115000000")
        self.assertEqual(q1.quote_id, q2.quote_id)
        self.assertTrue(q1.dry_run)
        self.assertIn("simulated", q1.deposit_address)
        self.assertEqual(q1.amount_in, "115000000")

    def test_status_is_simulated(self):
        status = get_intents_provider().status("dry-abc")
        self.assertTrue(status["simulated"])


class EndpointTests(SimpleTestCase):
    def test_status_endpoint(self):
        res = self.client.get("/api/v1/intents/status/")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])

    def test_quote_endpoint(self):
        res = self.client.post(
            "/api/v1/intents/quote/",
            data=json.dumps({"asset_out": "nep141:tusd", "amount_out": "100000000"}),
            content_type="application/json",
        )
        body = res.json()
        self.assertEqual(res.status_code, 200)
        self.assertTrue(body["quote"]["dry_run"])
        self.assertEqual(body["quote"]["amount_in"], "100000000")

    def test_quote_endpoint_validation(self):
        res = self.client.post("/api/v1/intents/quote/", data="{}", content_type="application/json")
        self.assertEqual(res.status_code, 400)
