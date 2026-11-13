"""Django settings for SWEEFT (testnet-first MVP).

Single settings module, environment-driven. SQLite by default; set
DATABASE_URL-style values later if needed. Django is the product coordinator
and keeps a searchable projection; the NEAR contract is the source of truth
for funds and authorization.
"""
import os
from pathlib import Path

import dj_database_url
import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

# Load sweeft/.env if present (testnet addresses live there).
environ.Env.read_env(BASE_DIR / ".env")

# Secure by default: DEBUG only when explicitly enabled (DJANGO_DEBUG=1),
# and hosts are restricted unless DJANGO_ALLOWED_HOSTS says otherwise.
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    if DEBUG:
        SECRET_KEY = "dev-only-not-secret-change-me"  # local development only
    else:
        raise ImproperlyConfigured(
            "DJANGO_SECRET_KEY must be set when DJANGO_DEBUG=0 — refusing to start with a known key"
        )
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
    "whitenoise.middleware.WhiteNoiseMiddleware",
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

# Django >=5.2 wraps the default template loaders in the cached loader in every
# environment. During local development (DJANGO_DEBUG=1) read templates from
# disk on every render, so edits show on refresh without restarting runserver.
if DEBUG:
    TEMPLATES[0]["APP_DIRS"] = False
    TEMPLATES[0]["OPTIONS"]["loaders"] = [
        "django.template.loaders.filesystem.Loader",
        "django.template.loaders.app_directories.Loader",
    ]

WSGI_APPLICATION = "config.wsgi.application"

# SQLite by default; set DATABASE_URL (Render Postgres) and it takes over.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("SQLITE_PATH", BASE_DIR / "db.sqlite3"),
    }
}
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if DATABASE_URL:
    DATABASES["default"] = dj_database_url.parse(
        DATABASE_URL, conn_max_age=600, conn_health_checks=True
    )

AUTH_PASSWORD_VALIDATORS = []

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
# WhiteNoise serves the collected static files in production (no CDN needed).
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
CSRF_TRUSTED_ORIGINS = os.environ.get("CSRF_TRUSTED_ORIGINS", "http://localhost:8000").split(",")

SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
# HTTPS-only cookies for deployments behind TLS (off for local http dev);
# on Render (TLS terminated by the platform) the secure default flips on.
SESSION_COOKIE_SECURE = os.environ.get(
    "SESSION_COOKIE_SECURE", "1" if os.environ.get("RENDER_EXTERNAL_HOSTNAME") else "0"
) == "1"
CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
CSRF_COOKIE_SAMESITE = "Lax"

# Render terminates TLS in front of the app and injects its public hostname.
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
RENDER_EXTERNAL_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME", "")
if RENDER_EXTERNAL_HOSTNAME:
    if RENDER_EXTERNAL_HOSTNAME not in ALLOWED_HOSTS:
        ALLOWED_HOSTS.append(RENDER_EXTERNAL_HOSTNAME)
    render_origin = f"https://{RENDER_EXTERNAL_HOSTNAME}"
    if render_origin not in CSRF_TRUSTED_ORIGINS:
        CSRF_TRUSTED_ORIGINS.append(render_origin)

# ---------------------------------------------------------------------------
# Rate limiting (cache-backed, per client IP) for RPC/DB-writing endpoints.
# ---------------------------------------------------------------------------
RATE_LIMIT_CHALLENGE_PER_MIN = int(os.environ.get("RATE_LIMIT_CHALLENGE_PER_MIN", "60"))
RATE_LIMIT_SYNC_PER_MIN = int(os.environ.get("RATE_LIMIT_SYNC_PER_MIN", "30"))
RATE_LIMIT_LOGIN_PER_MIN = int(os.environ.get("RATE_LIMIT_LOGIN_PER_MIN", "30"))

# ---------------------------------------------------------------------------
# SWEEFT configuration
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
SWEEFT_CONTRACT = os.environ.get("SWEEFT_CONTRACT", "")
OUTLAYER_CONTRACT = os.environ.get("OUTLAYER_CONTRACT", "outlayer.testnet")
OUTLAYER_PROJECT = os.environ.get("OUTLAYER_PROJECT", "")

# Demo Carrier: clearly-labelled simulated logistics provider
DEMO_CARRIER_PROVIDER_ID = "LOG-DEMO-001"
DEMO_CARRIER_NAME = "Demo Carrier (simulated)"
DEMO_CARRIER_WALLET = os.environ.get("DEMO_CARRIER_WALLET", "")
# Carrier payload signing (ed25519). The public key is registered on-chain via
# `set_carrier_pubkey`; the TEE verifier then requires a valid signature, so a
# caller cannot fabricate carrier data from public ids alone.
DEMO_CARRIER_PRIVATE_KEY = os.environ.get("DEMO_CARRIER_PRIVATE_KEY", "")
DEMO_CARRIER_PUBKEY = os.environ.get("DEMO_CARRIER_PUBKEY", "")

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
# The co-pilot agent holds NO user keys. A user deposits native NEAR into the
# on-chain budget with one wallet signature; the agent can then pay within that
# budget without a popup per payment. The budget contract enforces the limit
# on-chain — that is the security boundary.
#
# Testnet disclosure: AI_AGENT_PRIVATE_KEY lives on the demo server because
# this is a testnet MVP; on mainnet the key would sit inside a TEE/KMS.
# ---------------------------------------------------------------------------
AI_AGENT_ENABLED = os.environ.get("AI_AGENT_ENABLED", "1") == "1"
AI_AGENT_ACCOUNT = os.environ.get("AI_AGENT_ACCOUNT", "")
AI_AGENT_PRIVATE_KEY = os.environ.get("AI_AGENT_PRIVATE_KEY", "")
# Native-NEAR budget contract.
AI_BUDGET_CONTRACT = os.environ.get("AI_BUDGET_CONTRACT", "budget.steepdata3968.testnet")
AI_AGENT_MAX_PAYMENT = int(os.environ.get("AI_AGENT_MAX_PAYMENT", "100000000000000000000000"))  # 0.1 NEAR
AI_AGENT_GRANT = int(os.environ.get("AI_AGENT_GRANT", "250000000000000000000000"))  # 0.25 NEAR (UI)

# ---------------------------------------------------------------------------
# Verification relayer (platform-side automation; permissionless on-chain calls)
#
# The escrow contract exposes permissionless `request_delivery_verification`,
# `settle_logistics`, and `release_after_inspection`: destinations and amounts
# are fixed by the escrow, so a platform relayer can run the happy path
# (TEE-verify the carrier report → pay logistics → release after the inspection
# window) without user signatures. Testnet disclosure: this key lives on the
# demo server; on mainnet it would run inside a TEE/KMS. The key can only
# trigger pre-agreed, condition-gated transfers — it can never redirect funds.
# ---------------------------------------------------------------------------
RELAYER_ENABLED = os.environ.get("RELAYER_ENABLED", "0") == "1"
RELAYER_ACCOUNT = os.environ.get("RELAYER_ACCOUNT", "")
RELAYER_PRIVATE_KEY = os.environ.get("RELAYER_PRIVATE_KEY", "")
RELAYER_POLL_SECONDS = float(os.environ.get("RELAYER_POLL_SECONDS", "5"))
RELAYER_VERIFICATION_DEPOSIT_YOCTO = int(
    os.environ.get("RELAYER_VERIFICATION_DEPOSIT_YOCTO", "10000000000000000000000")  # 0.01 NEAR
)

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
GOVERNANCE_DAO = os.environ.get("GOVERNANCE_DAO", "sweeft-board.testnet")
