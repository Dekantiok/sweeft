from django.urls import path

from . import views

urlpatterns = [
    path("market/", views.market_browse, name="market"),
    path("market/new/", views.listing_new, name="listing-new"),
    path("market/mine/", views.listing_mine, name="listing-mine"),
    path("market/<slug:slug>/", views.listing_detail, name="listing-detail"),
    path("market/<slug:slug>/status/", views.listing_status, name="listing-status"),
    path("market/<slug:slug>/request/", views.purchase_request, name="purchase-request"),
    path("market/<slug:slug>/escrow/", views.listing_escrow_prefill, name="listing-escrow"),
    path("api/v1/market/requests/<int:request_id>/decline/", views.purchase_request_decline, name="purchase-request-decline"),
    path("u/<str:account_id>/", views.profile, name="profile"),
]
