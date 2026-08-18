#!/bin/sh
# Data retention for the OSS build: deletes trace data older than N days.
#
# Langfuse's built-in retention feature (project-level, deletes ClickHouse rows
# and blob payloads together) is Enterprise-only. On the MIT build data grows
# indefinitely, so this script applies the two equivalents by hand:
#   1. ClickHouse TTL on the tables holding traces/observations/scores
#   2. A MinIO lifecycle rule expiring the event blobs
# Both are needed -- a TTL alone leaves the blobs in MinIO untouched.
#
# Idempotent: re-running with a different value just overwrites the policy.
#
# Usage: ./setup_retention.sh [days]     (default: 180)

set -e

DAYS="${1:-180}"
BUCKET="${2:-langfuse}"

case "$DAYS" in
  ''|*[!0-9]*) echo "days must be a positive integer, got: $DAYS" >&2; exit 1 ;;
esac

echo "Applying a ${DAYS}-day retention policy."
echo

# --- 1. ClickHouse TTL -------------------------------------------------------
# Credentials are read inside the container, which already has CLICKHOUSE_USER /
# CLICKHOUSE_PASSWORD from compose -- avoids parsing .env and leaking the
# password into the host's process list.
#
# events_full / events_core hold the v4 data actually written today; traces /
# observations / scores are the v3 tables, still created by the migrations and
# covered here so an upgraded instance does not keep old rows forever.
echo "[1/2] ClickHouse TTL"
for pair in \
  "events_full:start_time" \
  "events_core:start_time" \
  "observations:start_time" \
  "traces:timestamp" \
  "scores:timestamp"
do
  table="${pair%%:*}"
  column="${pair##*:}"
  # --enable_full_text_index=1: events_full/events_core carry text() indexes,
  # and ALTER is rejected without the feature flag even though the TTL itself
  # does not touch them.
  docker compose exec -T clickhouse sh -c "
    clickhouse-client --user \"\$CLICKHOUSE_USER\" --password \"\$CLICKHOUSE_PASSWORD\" \
      --enable_full_text_index=1 \
      --query \"ALTER TABLE default.${table}
                MODIFY TTL toDateTime(${column}) + INTERVAL ${DAYS} DAY\"
  "
  echo "  default.${table} (${column}) -> ${DAYS}d"
done

# --- 2. MinIO lifecycle ------------------------------------------------------
# Clears any existing rules first so repeated runs do not stack up duplicates.
echo "[2/2] MinIO lifecycle rule on bucket '${BUCKET}'"
docker compose exec -T minio sh -c "
  mc alias set retention http://localhost:9000 \"\$MINIO_ROOT_USER\" \"\$MINIO_ROOT_PASSWORD\" >/dev/null
  mc ilm rule rm --all --force retention/${BUCKET} >/dev/null 2>&1 || true
  mc ilm rule add --expire-days ${DAYS} retention/${BUCKET} >/dev/null
  mc ilm rule ls retention/${BUCKET}
"

echo
echo "Done. TTL removal happens during ClickHouse's background merges, so disk"
echo "usage drops with a lag rather than at the exact cutoff. To reclaim space"
echo "for a specific month immediately:"
echo "  docker compose exec clickhouse clickhouse-client --user \$CLICKHOUSE_USER --password \$CLICKHOUSE_PASSWORD --query \"ALTER TABLE default.events_full DROP PARTITION '202601'\""
