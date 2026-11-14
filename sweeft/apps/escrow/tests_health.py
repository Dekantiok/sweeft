"""Health endpoint contract: strict readiness vs platform liveness."""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from apps.escrow.models import RelayerHeartbeat


class HealthEndpointTests(TestCase):
    @override_settings(RELAYER_ENABLED=True)
    def test_liveness_never_depends_on_relayer(self):
        res = self.client.get("/health/live/")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])

    @override_settings(RELAYER_ENABLED=False)
    def test_strict_health_ok_without_relayer(self):
        res = self.client.get("/health/")
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["ok"])

    @override_settings(RELAYER_ENABLED=True, RELAYER_STALE_AFTER_SECONDS=300)
    def test_strict_health_503_when_relayer_never_ran(self):
        res = self.client.get("/health/")
        self.assertEqual(res.status_code, 503)
        self.assertFalse(res.json()["ok"])
        self.assertEqual(res.json()["relayer"]["status"], "never-ran")

    @override_settings(RELAYER_ENABLED=True, RELAYER_STALE_AFTER_SECONDS=300)
    def test_strict_health_503_when_stale_200_when_fresh(self):
        RelayerHeartbeat.record()
        RelayerHeartbeat.objects.filter(pk=1).update(
            last_tick_at=timezone.now() - timedelta(seconds=600)
        )
        res = self.client.get("/health/")
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["relayer"]["status"], "stale")

        RelayerHeartbeat.objects.filter(pk=1).update(last_tick_at=timezone.now())
        res = self.client.get("/health/")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["relayer"]["status"], "ok")
