#!/usr/bin/env bash
# Render build step: install deps, collect static files, apply migrations.
set -o errexit

pip install --upgrade pip
pip install -r requirements.txt
python manage.py collectstatic --noinput
python manage.py migrate --noinput
# Shared rate-limit cache table (also created by migration wallets.0002;
# createcachetable is a no-op when the table already exists).
python manage.py createcachetable
