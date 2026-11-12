from django.urls import path

from . import views

urlpatterns = [
    path("api/v1/intents/status/", views.intents_status_view, name="intents-status"),
    path("api/v1/intents/quote/", views.intents_quote, name="intents-quote"),
    path("api/v1/intents/quotes/<str:quote_id>/", views.intents_quote_status, name="intents-quote-status"),
]
