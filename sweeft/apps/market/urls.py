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
    path("api/v1/market/offers/", views.offer_create, name="offer-create"),
    path("api/v1/market/offers/mine/", views.offers_mine, name="offers-mine"),
    path("api/v1/market/offers/<int:offer_id>/counter/", views.offer_counter, name="offer-counter"),
    path("api/v1/market/offers/<int:offer_id>/accept-counter/", views.offer_accept_counter, name="offer-accept-counter"),
    path("api/v1/market/offers/<int:offer_id>/accept/", views.offer_accept, name="offer-accept"),
    path("api/v1/market/offers/<int:offer_id>/reject/", views.offer_reject, name="offer-reject"),
    path("api/v1/market/offers/<int:offer_id>/withdraw/", views.offer_withdraw, name="offer-withdraw"),
    path("u/<str:account_id>/", views.profile, name="profile"),
]
