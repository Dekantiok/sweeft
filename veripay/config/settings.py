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
# Secure by default: DEBUG only when explicitly enabled (DJANGO_DEBUG=1),
# and hosts are restricted unless DJANGO_ALLOWED_HOSTS says otherwise.
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = [
    h.strip()
    for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1,testserver").split(",")
    if h.strip()
]

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
    "apps.market",
    "apps.ai",
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
                "apps.escrow.views.config_context",
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

# ---------------------------------------------------------------------------
# AI infrastructure (provider-neutral — swap via AI_PROVIDER, no code changes)
#
#   echo          deterministic rules, no network (default; nothing breaks offline)
#   openai_compat any OpenAI-compatible /chat/completions endpoint
#                 (NEAR AI Cloud, OpenAI, Groq, Together, Ollama, LM Studio…)
#   anthropic     Anthropic Messages API
# ---------------------------------------------------------------------------
AI_PROVIDER = os.environ.get("AI_PROVIDER", "echo")
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://cloud-api.near.ai/v1")
AI_API_KEY = os.environ.get("AI_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "deepseek/deepseek-v4.1-flash")
AI_TIMEOUT_SECONDS = int(os.environ.get("AI_TIMEOUT_SECONDS", "30"))

# ---------------------------------------------------------------------------
# AI co-pilot payments (scoped, revocable on-chain allowance)
#
# The co-pilot agent holds NO user keys. A user grants it a tUSD allowance
# with one wallet signature (`approve` on the token); the agent can then pay
# within that budget without a popup per payment. The token contract enforces
# the allowance on-chain — that is the security boundary.
#
# Testnet disclosure: AI_AGENT_PRIVATE_KEY lives on the demo server because
# this is a testnet MVP; on mainnet the key would sit inside a TEE/KMS.
# ---------------------------------------------------------------------------
AI_AGENT_ENABLED = os.environ.get("AI_AGENT_ENABLED", "1") == "1"
AI_AGENT_ACCOUNT = os.environ.get("AI_AGENT_ACCOUNT", "")
AI_AGENT_PRIVATE_KEY = os.environ.get("AI_AGENT_PRIVATE_KEY", "")
AI_AGENT_MAX_PAYMENT = int(os.environ.get("AI_AGENT_MAX_PAYMENT", "100000000"))  # 100 tUSD per payment
AI_AGENT_GRANT = int(os.environ.get("AI_AGENT_GRANT", "250000000"))  # 250 tUSD suggested budget (UI)

# Platform owner hint for UI affordances (nav links). Authorization is always
# checked against the on-chain config — this is display only.
PLATFORM_OWNER = os.environ.get("PLATFORM_OWNER", "steepdata3968.testnet")

# ---------------------------------------------------------------------------
# Governance: Sputnik/Trezu DAO review board + treasury.
#
# The escrow contract's `arbiter`/`treasury` are set to this account on-chain
# (`get_gov_info`). Disputes resolve ONLY via council-approved proposals;
# platform fees accrue to this account's treasury.
# ---------------------------------------------------------------------------
GOVERNANCE_DAO = os.environ.get("GOVERNANCE_DAO", "veripay-board.testnet")
