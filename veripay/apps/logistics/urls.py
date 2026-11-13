from django.urls import path

from . import views

urlpatterns = [
    path("logistics/", views.directory, name="logistics-directory"),
    path("partners/register/", views.partner_register, name="partner-register"),
    path("partners/portal/", views.partner_portal, name="partner-portal"),
    # JSON API (wallet session)
    path("api/v1/shipments/", views.api_create_shipment, name="api-shipment-create"),
    path("api/v1/shipments/by-escrow/<str:escrow_id>/", views.api_shipment_for_escrow, name="api-shipment-for-escrow"),
    path("api/v1/shipments/<int:shipment_id>/", views.api_shipment, name="api-shipment"),
    path("api/v1/shipments/<int:shipment_id>/events/", views.api_shipment_event, name="api-shipment-event"),
    path("api/v1/verification/payload/", views.api_verification_payload, name="api-verification-payload"),
    path("api/v1/verification/sync/", views.api_verification_sync, name="api-verification-sync"),
    # Partner API (key-authenticated)
    path("api/v1/partners/shipments/", views.api_partner_shipments, name="api-partner-shipments"),
    path("api/v1/partners/shipments/<int:shipment_id>/events/", views.api_partner_event, name="api-partner-event"),
]
