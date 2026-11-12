"""Django settings for VeriPay (testnet-first MVP).

Single settings module, environment-driven. SQLite by default; set
DATABASE_URL-style values later if needed. Django is the product coordinator
and keeps a searchable projection; the NEAR contract is the source of truth
for funds and authorization.
"""
import os
from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent

# Load veripay/.env if present (testnet addresses live there).
environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-not-secret-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "*").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "apps.wallets",
    "apps.escrow",
    "apps.logistics",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.wallets.context.wallet_context",
                "apps.escrow.context.config_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("SQLITE_PATH", BASE_DIR / "db.sqlite3"),
    }
}

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
CSRF_TRUSTED_ORIGINS = os.environ.get("CSRF_TRUSTED_ORIGINS", "http://localhost:8000").split(",")

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

# ---------------------------------------------------------------------------
# VeriPay configuration
# ---------------------------------------------------------------------------
NEAR_NETWORK = os.environ.get("NEAR_NETWORK", "testnet")
NEAR_RPC_URL = os.environ.get(
    "NEAR_RPC_URL",
    "https://rpc.testnet.near.org" if NEAR_NETWORK == "testnet" else "https://rpc.mainnet.near.org",
)
EXPLORER_URL = os.environ.get(
    "NEAR_EXPLORER_URL",
    "https://testnet.near.org" if NEAR_NETWORK == "testnet" else "https://near.org",
)

# Deployed contract accounts (filled by scripts/deploy_*.sh output)
VERIPAY_CONTRACT = os.environ.get("VERIPAY_CONTRACT", "")
TUSD_CONTRACT = os.environ.get("TUSD_CONTRACT", "")
OUTLAYER_CONTRACT = os.environ.get("OUTLAYER_CONTRACT", "outlayer.testnet")
OUTLAYER_PROJECT = os.environ.get("OUTLAYER_PROJECT", "")

# Demo Carrier: clearly-labelled simulated logistics provider
DEMO_CARRIER_PROVIDER_ID = "LOG-DEMO-001"
DEMO_CARRIER_NAME = "Demo Carrier (simulated)"
DEMO_CARRIER_WALLET = os.environ.get("DEMO_CARRIER_WALLET", "")
