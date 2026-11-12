from django.urls import path

from . import views

urlpatterns = [
    path("", views.landing, name="landing"),
    path("dashboard/", views.dashboard, name="dashboard"),
    path("escrow/new/", views.escrow_new, name="escrow-new"),
    path("escrow/<str:escrow_id>/", views.escrow_detail, name="escrow-detail"),
    path("invitations/<str:token>/", views.invitation_view, name="invitation"),
    # JSON API
    path("api/v1/config/", views.api_config, name="api-config"),
    path("api/v1/agreements/", views.api_register_agreement, name="api-agreements-create"),
    path("api/v1/agreements/mine/", views.api_my_agreements, name="api-agreements-mine"),
    path("api/v1/agreements/<str:escrow_id>/", views.api_agreement, name="api-agreement"),
    path("api/v1/agreements/<str:escrow_id>/sync/", views.api_sync_agreement, name="api-agreement-sync"),
    path("api/v1/invitations/<str:token>/", views.api_invitation, name="api-invitation"),
    path("api/v1/invitations/<str:token>/accept/", views.api_invitation_accept, name="api-invitation-accept"),
]
