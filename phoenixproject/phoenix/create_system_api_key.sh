#!/bin/sh
# Non-interactive system API key creation for automated deployments.
# Requires PHOENIX_ADMIN_SECRET to be set on the phoenix server.
#
# Usage: PHOENIX_ADMIN_SECRET=<value> ./create_system_api_key.sh <key-name> [phoenix-url]

set -e

KEY_NAME="${1:?key name required, e.g. llmops-services}"
PHOENIX_URL="${2:-http://localhost:6006}"

if [ -z "$PHOENIX_ADMIN_SECRET" ]; then
  echo "PHOENIX_ADMIN_SECRET env var is required" >&2
  exit 1
fi

curl -sS -X POST "$PHOENIX_URL/v1/system/api_keys" \
  -H "Authorization: Bearer $PHOENIX_ADMIN_SECRET" \
  -H "Content-Type: application/json" \
  -d "{\"data\": {\"name\": \"$KEY_NAME\"}}"
