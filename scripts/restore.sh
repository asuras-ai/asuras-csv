#!/usr/bin/env bash
# Restore a dump made by backup.sh, replacing the current database contents.
# Usage: scripts/restore.sh <dumpfile> [--yes]
# Optional: PROJECT=<compose project name> to target a non-default compose project.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

file="" yes=0
for arg in "$@"; do
  case "$arg" in
    --yes) yes=1 ;;
    *) [[ -z "$file" ]] && file="$arg" || { echo "usage: $0 <dumpfile> [--yes]" >&2; exit 2; } ;;
  esac
done
if [[ -z "$file" ]]; then
  echo "usage: $0 <dumpfile> [--yes]" >&2
  exit 2
fi
[[ -f "$file" ]] || { echo "error: no such file: $file" >&2; exit 1; }

dc=(docker compose)
[[ -n "${PROJECT:-}" ]] && dc+=(-p "$PROJECT")

if [[ -z "$("${dc[@]}" ps --status running -q db 2>/dev/null)" ]]; then
  echo "error: the db container is not running (start it with: docker compose up -d db)" >&2
  exit 1
fi

if (( ! yes )); then
  echo "This REPLACES all data in the ohlcv database with the contents of $file."
  read -r -p 'Type "restore" to continue: ' answer
  [[ "$answer" == "restore" ]] || { echo "Aborted."; exit 1; }
fi

psql=("${dc[@]}" exec -T db psql -U ohlcv -d ohlcv -v ON_ERROR_STOP=1 -c)

echo "Stopping app..."
"${dc[@]}" stop app
echo "Restoring $file..."
"${psql[@]}" "SELECT timescaledb_pre_restore();"
# pg_restore may exit non-zero on harmless warnings (e.g. extension already exists); still run post_restore.
rc=0
"${dc[@]}" exec -T db pg_restore --clean --if-exists -U ohlcv -d ohlcv < "$file" || rc=$?
"${psql[@]}" "SELECT timescaledb_post_restore();"
echo "Starting app..."
"${dc[@]}" start app

if (( rc )); then
  echo "Restore finished with pg_restore exit code $rc (warnings are common; verify your data)."
else
  echo "Restore of $file complete; app restarted."
fi
