#!/usr/bin/env bash
# Dump the ohlcv database to backups/ohlcv-YYYYmmdd-HHMMSS-<pid>.dump (plus a .version sidecar with the TimescaleDB
# and PostgreSQL versions) and keep the newest $KEEP dumps (default 7; KEEP=0 means keep everything).
# Optional: PROJECT=<compose project name> to target a non-default compose project.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

KEEP="${KEEP:-7}"
if [[ ! "$KEEP" =~ ^[0-9]+$ ]]; then
  echo "error: KEEP must be a non-negative integer (got '$KEEP'); KEEP=0 disables pruning" >&2
  exit 2
fi
dc=(docker compose)
[[ -n "${PROJECT:-}" ]] && dc+=(-p "$PROJECT")

if [[ -z "$("${dc[@]}" ps --status running -q db 2>/dev/null)" ]]; then
  echo "error: the db container is not running (start it with: docker compose up -d)" >&2
  exit 1
fi

mkdir -p backups
# Seconds plus PID: two backups started in the same second never share a name.
file="backups/ohlcv-$(date +%Y%m%d-%H%M%S)-$$.dump"
if [[ -e "$file" ]]; then
  echo "error: $file already exists" >&2
  exit 1
fi
if ! "${dc[@]}" exec -T db pg_dump -U ohlcv -d ohlcv -Fc > "$file" </dev/null; then
  rm -f "$file"
  echo "error: pg_dump failed" >&2
  exit 1
fi
if [[ ! -s "$file" ]]; then
  rm -f "$file"
  echo "error: pg_dump produced an empty file" >&2
  exit 1
fi

# Sidecar: restore.sh compares the TimescaleDB version, because a dump must be restored into the same version.
if ! versions="$("${dc[@]}" exec -T db psql -U ohlcv -d ohlcv -At -F= -v ON_ERROR_STOP=1 -c \
  "SELECT 'timescaledb', extversion FROM pg_extension WHERE extname = 'timescaledb' UNION ALL SELECT 'server_version', current_setting('server_version')" </dev/null)"; then
  rm -f "$file"
  echo "error: could not read the database versions" >&2
  exit 1
fi
printf '%s\n' "$versions" > "$file.version"

echo "Backup written: $PWD/$file ($(du -h "$file" | cut -f1))"

# Retention: delete everything but the newest $KEEP dumps (names sort chronologically).
if (( KEEP > 0 )); then
  ls -1 backups/ohlcv-*.dump | sort -r | tail -n +"$((KEEP + 1))" | while read -r old; do
    rm -f -- "$old" "$old.version"
    echo "Removed old backup: $old"
  done
fi
