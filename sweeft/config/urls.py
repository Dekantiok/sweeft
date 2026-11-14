import logging

from django.conf import settings
from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path
from django.utils import timezone

from apps.wallets.ratelimit import client_ip

logger = logging.getLogger("sweeft.proxy")


def _proxy_debug_log(request):
    """Temporary hop-count diagnostic (DJANGO_PROXY_DEBUG=1).

    Logs the raw proxy headers, the resolved client IP and the configured hop
    count so a deployment can confirm `TRUSTED_PROXY_COUNT` from its logs.
    """
    if getattr(settings, "DJANGO_PROXY_DEBUG", False):
        logger.info(
            "proxy-debug path=%s xff=%r remote_addr=%r client_ip=%r trusted_proxy_count=%s",
            request.path,
            request.META.get("HTTP_X_FORWARDED_FOR", ""),
            request.META.get("REMOTE_ADDR", ""),
            client_ip(request),
            getattr(settings, "TRUSTED_PROXY_COUNT", 1),
        )


def health(request):
    """Strict readiness probe — 503 while the relayer (if enabled) is stale.

    Uptime monitors should point here: a stale or never-ran relayer means
    settlements are paused, which must be alertable. Render's own platform
    health check uses `/health/live/`, which never depends on the relayer.
    """
    _proxy_debug_log(request)
    payload = {"ok": True, "service": "sweeft"}
    status_code = 200
    if getattr(settings, "RELAYER_ENABLED", False):
        stale_after = int(getattr(settings, "RELAYER_STALE_AFTER_SECONDS", 300))
        try:
            from apps.escrow.models import RelayerHeartbeat

            row = RelayerHeartbeat.objects.filter(pk=1).first()
            if row is None or row.last_tick_at is None:
                payload["relayer"] = {"status": "never-ran", "stale_after_seconds": stale_after}
                payload["ok"] = False
                status_code = 503
            else:
                age = int((timezone.now() - row.last_tick_at).total_seconds())
                relayer_status = "ok" if age <= stale_after else "stale"
                payload["relayer"] = {
                    "status": relayer_status,
                    "last_tick_seconds_ago": age,
                    "stale_after_seconds": stale_after,
                    "last_error": row.last_error,
                }
                if relayer_status != "ok":
                    payload["ok"] = False
                    status_code = 503
        except Exception as exc:  # noqa: BLE001 — health must not 500 on telemetry
            payload["relayer"] = {"status": "unknown", "error": str(exc)}
    return JsonResponse(payload, status=status_code)


def health_live(request):
    """Process liveness for platform health checks — relayer-independent."""
    _proxy_debug_log(request)
    return JsonResponse({"ok": True, "service": "sweeft"})


urlpatterns = [
    path("health/", health, name="health"),
    path("health/live/", health_live, name="health-live"),
    path("admin/", admin.site.urls),
    path("", include("apps.wallets.urls")),
    path("", include("apps.escrow.urls")),
    path("", include("apps.logistics.urls")),
    path("", include("apps.market.urls")),
    path("", include("apps.ai.urls")),
]
