from django.urls import path

from . import views

urlpatterns = [
    path("api/v1/ai/status/", views.ai_status, name="ai-status"),
    path("api/v1/ai/parse/", views.ai_parse, name="ai-parse"),
    path("api/v1/ai/recommend/", views.ai_recommend, name="ai-recommend"),
    path("api/v1/ai/listing/", views.ai_listing, name="ai-listing"),
    path("api/v1/ai/chat/", views.ai_chat, name="ai-chat"),
]
