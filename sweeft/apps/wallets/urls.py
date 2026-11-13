from django.urls import path

from . import views

urlpatterns = [
    path("api/v1/wallet/challenge/", views.challenge, name="wallet-challenge"),
    path("api/v1/wallet/verify/", views.verify, name="wallet-verify"),
    path("api/v1/wallet/me/", views.me, name="wallet-me"),
    path("api/v1/wallet/logout/", views.logout, name="wallet-logout"),
]
