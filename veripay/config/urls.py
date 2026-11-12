from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", include("apps.wallets.urls")),
    path("", include("apps.escrow.urls")),
    path("", include("apps.logistics.urls")),
    path("", include("apps.market.urls")),
    path("", include("apps.ai.urls")),
    path("", include("apps.intents.urls")),
]
