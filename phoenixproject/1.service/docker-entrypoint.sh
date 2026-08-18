#!/bin/sh
set -e

uv run python manage.py migrate --noinput
exec uv run gunicorn config.wsgi:application --bind 0.0.0.0:8000
