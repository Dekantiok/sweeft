"""AI layer tests: provider selection + deterministic fallbacks."""
import json
from unittest import mock

from django.test import SimpleTestCase, override_settings

from apps.ai import services
from apps.ai.providers.base import AIProviderError
from apps.ai.registry import get_provider, provider_status


class RegistryTests(SimpleTestCase):
    @override_settings(AI_PROVIDER="echo")
    def test_echo_default(self):
        self.assertEqual(get_provider().name, "echo")
        self.assertTrue(provider_status()["configured"])

    @override_settings(AI_PROVIDER="openai_compat", AI_API_KEY="")
    def test_openai_compat_without_key_reports_unconfigured(self):
        status = provider_status()
        self.assertEqual(status["provider"], "openai_compat")
        self.assertFalse(status["configured"])

    def test_unknown_provider_raises(self):
        with override_settings(AI_PROVIDER="nope"):
            with self.assertRaises(AIProviderError):
                get_provider()


class DeterministicServiceTests(SimpleTestCase):
    @override_settings(AI_PROVIDER="echo")
    def test_parse_extracts_fields(self):
        parsed = services.parse_deal_text("pay 0.05 NEAR to alice.testnet for logo in 3 days")
        self.assertEqual(parsed["beneficiary"], "alice.testnet")
        self.assertEqual(parsed["amount"], 0.05)
        self.assertEqual(parsed["deadlineDays"], 3)
        self.assertEqual(parsed["source"], "rules")

    @override_settings(AI_PROVIDER="echo")
    def test_parse_weeks(self):
        parsed = services.parse_deal_text("deliver in 2 weeks to bob.testnet")
        self.assertEqual(parsed["deadlineDays"], 14)

    @override_settings(AI_PROVIDER="echo")
    def test_recommend_defaults_even(self):
        rec = services.recommend_dispute_split("seller shipped, buyer says wrong colour")
        self.assertEqual(rec["pct"], 50)
        self.assertEqual(rec["source"], "rules")

    @override_settings(AI_PROVIDER="echo")
    def test_listing_draft(self):
        draft = services.draft_listing("Interstate delivery service, 48h, Lagos to Abuja")
        self.assertEqual(draft["kind"], "service")
        self.assertTrue(draft["title"])


class FallbackTests(SimpleTestCase):
    def test_provider_failure_falls_back_to_rules(self):
        class Boom:
            name = "openai_compat"

            def complete(self, **kwargs):
                raise AIProviderError("provider down")

        with mock.patch("apps.ai.services.get_provider", return_value=Boom()):
            parsed = services.parse_deal_text("pay 5 tUSD to carol.testnet tomorrow")
        self.assertEqual(parsed["source"], "rules")
        self.assertEqual(parsed["beneficiary"], "carol.testnet")

    def test_json_extraction_tolerates_prose(self):
        self.assertEqual(
            services.extract_json('Sure! Here you go: {"pct": 70, "reason": "x"} — hope that helps'),
            {"pct": 70, "reason": "x"},
        )


class EndpointTests(SimpleTestCase):
    def test_status_endpoint(self):
        res = self.client.get("/api/v1/ai/status/")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])
        self.assertIn("provider", res.json())

    def test_parse_endpoint(self):
        res = self.client.post(
            "/api/v1/ai/parse/",
            data=json.dumps({"text": "pay 10 tUSD to dave.testnet in 5 days"}),
            content_type="application/json",
        )
        body = res.json()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(body["parsed"]["beneficiary"], "dave.testnet")

    def test_recommend_endpoint_requires_evidence(self):
        res = self.client.post("/api/v1/ai/recommend/", data="{}", content_type="application/json")
        self.assertEqual(res.status_code, 400)

    def test_listing_endpoint(self):
        res = self.client.post(
            "/api/v1/ai/listing/",
            data=json.dumps({"description": "Solar inverter service"}),
            content_type="application/json",
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["draft"]["title"])
