#!/usr/bin/env bash
# Dump the ohlcv database to backups/ohlcv-YYYYmmdd-HHMMSS.dump and keep the newest $KEEP (default 7).
# Optional: PROJECT=<compose project name> to target a non-default compose project.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

KEEP="${KEEP:-7}"
dc=(docker compose)
[[ -n "${PROJECT:-}" ]] && dc+=(-p "$PROJECT")

if [[ -z "$("${dc[@]}" ps --status running -q db 2>/dev/null)" ]]; then
  echo "error: the db container is not running (start it with: docker compose up -d)" >&2
  exit 1
fi

mkdir -p backups
file="backups/ohlcv-$(date +%Y%m%d-%H%M%S).dump"
if ! "${dc[@]}" exec -T db pg_dump -U ohlcv -d ohlcv -Fc > "$file"; then
  rm -f "$file"
  echo "error: pg_dump failed" >&2
  exit 1
fi
if [[ ! -s "$file" ]]; then
  rm -f "$file"
  echo "error: pg_dump produced an empty file" >&2
  exit 1
fi

echo "Backup written: $PWD/$file ($(du -h "$file" | cut -f1))"

# Retention: delete everything but the newest $KEEP dumps (names sort chronologically).
if [[ "$KEEP" =~ ^[0-9]+$ ]] && (( KEEP > 0 )); then
  ls -1 backups/ohlcv-*.dump | sort -r | tail -n +"$((KEEP + 1))" | while read -r old; do
    rm -f -- "$old"
    echo "Removed old backup: $old"
  done
fi
