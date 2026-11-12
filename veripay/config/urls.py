from django.contrib import admin
from django.http import JsonResponse
from django.urls import include, path


def health(request):
    return JsonResponse({"ok": True, "service": "veripay"})


urlpatterns = [
    path("health/", health, name="health"),
    path("admin/", admin.site.urls),
    path("", include("apps.wallets.urls")),
    path("", include("apps.escrow.urls")),
    path("", include("apps.logistics.urls")),
    path("", include("apps.market.urls")),
    path("", include("apps.ai.urls")),
    path("", include("apps.intents.urls")),
]
