from django.urls import path

from . import ops, views

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
    path("ops/", ops.ops_console, name="ops-console"),
]

# Dispute workflow (appended: keeps the original urlpatterns import path tidy)
from . import disputes as _disputes  # noqa: E402

urlpatterns += [
    path("api/v1/disputes/", _disputes.api_open_dispute, name="api-open-dispute"),
    path("api/v1/disputes/list/", _disputes.api_list_disputes, name="api-list-disputes"),
    path("api/v1/disputes/<int:dispute_id>/evidence/", _disputes.api_add_evidence, name="api-dispute-evidence"),
    path("api/v1/disputes/<int:dispute_id>/ai-review/", _disputes.api_ai_review, name="api-dispute-ai-review"),
    path("api/v1/disputes/<int:dispute_id>/resolve/", _disputes.api_resolve_dispute, name="api-dispute-resolve"),
    path("disputes/", _disputes.disputes_console, name="disputes-console"),
]
