#!/usr/bin/env bash
# Free-plan testnet demo: run the optional automation beside the web process.
# Production deployments should use separately supervised worker services.
set -o errexit

if [[ "${RELAYER_ENABLED:-0}" == "1" && -n "${RELAYER_ACCOUNT:-}" && -n "${RELAYER_PRIVATE_KEY:-}" ]]; then
  python3 manage.py run_relayer &
fi

if [[ -n "${DEMO_CARRIER_API_KEY:-}" ]]; then
  export SWEEFT_BASE_URL="${SWEEFT_BASE_URL:-http://127.0.0.1:${PORT:-8000}}"
  python3 ../tools/demo-carrier/simulator.py &
fi

exec gunicorn config.wsgi:application --bind "0.0.0.0:${PORT:-8000}" --workers 2 --timeout 120
