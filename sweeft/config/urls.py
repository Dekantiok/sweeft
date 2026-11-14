from django.conf import settings
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from django.utils import timezone


def health(request):
    """Liveness probe. When the relayer is enabled, its heartbeat is included
    and reported stale past ``RELAYER_STALE_AFTER_SECONDS``."""
    payload = {"ok": True, "service": "sweeft"}
    if getattr(settings, "RELAYER_ENABLED", False):
        stale_after = int(getattr(settings, "RELAYER_STALE_AFTER_SECONDS", 300))
        try:
            from apps.escrow.models import RelayerHeartbeat

            row = RelayerHeartbeat.objects.filter(pk=1).first()
            if row is None or row.last_tick_at is None:
                payload["relayer"] = {"status": "never-ran", "stale_after_seconds": stale_after}
            else:
                age = int((timezone.now() - row.last_tick_at).total_seconds())
                payload["relayer"] = {
                    "status": "ok" if age <= stale_after else "stale",
                    "last_tick_seconds_ago": age,
                    "stale_after_seconds": stale_after,
                    "last_error": row.last_error,
                }
        except Exception as exc:  # noqa: BLE001 — health must not 500 on telemetry
            payload["relayer"] = {"status": "unknown", "error": str(exc)}
    return JsonResponse(payload)


urlpatterns = [
    path("health/", health, name="health"),
    path("admin/", admin.site.urls),
    path("", include("apps.wallets.urls")),
    path("", include("apps.escrow.urls")),
    path("", include("apps.logistics.urls")),
    path("", include("apps.market.urls")),
    path("", include("apps.ai.urls")),
]
