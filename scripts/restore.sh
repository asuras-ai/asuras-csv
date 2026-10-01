#!/usr/bin/env bash
# Restore a dump made by backup.sh, REPLACING the ohlcv database. Follows TimescaleDB's documented procedure
# (fresh database, timescaledb_pre_restore, pg_restore without --clean, timescaledb_post_restore) and fails safe:
#   1. the dump is validated first (pg_restore -l, then a full dry run with pg_restore -f /dev/null); nothing is touched if that fails
#   2. a safety backup of the current database is taken (scripts/backup.sh, KEEP=0)
#   3. on any failure the app stays STOPPED and the safety dump path plus the command to restore it are printed
# Usage: scripts/restore.sh <dumpfile> [--yes] [--force]
#   --yes    skip the typed confirmation
#   --force  restore even if the dump's TimescaleDB version differs from the running database
# Optional: PROJECT=<compose project name> to target a non-default compose project.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

usage() { echo "usage: $0 <dumpfile> [--yes] [--force]" >&2; exit 2; }
file="" yes=0 force=0
for arg in "$@"; do
  case "$arg" in
    --yes) yes=1 ;;
    --force) force=1 ;;
    -*) usage ;;
    *) [[ -z "$file" ]] && file="$arg" || usage ;;
  esac
done
[[ -n "$file" ]] || usage
[[ -f "$file" ]] || { echo "error: no such file: $file" >&2; exit 1; }

dc=(docker compose)
[[ -n "${PROJECT:-}" ]] && dc+=(-p "$PROJECT")
project_prefix="${PROJECT:+PROJECT=$PROJECT }"
project_flag="${PROJECT:+-p $PROJECT }"

if [[ -z "$("${dc[@]}" ps --status running -q db 2>/dev/null)" ]]; then
  echo "error: the db container is not running (start it with: docker compose up -d db)" >&2
  exit 1
fi

# psql against a given database; extra arguments are passed through (e.g. -c "...").
psql_in() { local db="$1"; shift; "${dc[@]}" exec -T db psql -U ohlcv -d "$db" -v ON_ERROR_STOP=1 -At "$@" </dev/null; }

# 1. Validate the dump before changing anything.
if ! "${dc[@]}" exec -T db pg_restore -l < "$file" > /dev/null; then
  echo "error: $file is not a valid pg_dump custom-format archive; nothing was changed" >&2
  exit 1
fi

# Full dry run: read and decompress the whole archive without touching any database (catches truncation).
if ! "${dc[@]}" exec -T db pg_restore -f /dev/null < "$file"; then
  echo "error: dump is damaged or truncated ($file); nothing was changed" >&2
  exit 1
fi

# 2. Version check against the sidecar written by backup.sh.
running_ts="$(psql_in ohlcv -c "SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'")"
if [[ -f "$file.version" ]]; then
  echo "Dump versions ($file.version):"
  sed 's/^/  /' "$file.version"
  dump_ts="$(sed -n 's/^timescaledb=//p' "$file.version")"
  if [[ "$dump_ts" != "$running_ts" ]]; then
    echo "WARNING: dump was made with TimescaleDB ${dump_ts:-unknown}, the running database has $running_ts." >&2
    if (( ! force )); then
      echo "Restoring across TimescaleDB versions can fail or corrupt data. Use the matching image tag, or pass --force." >&2
      exit 1
    fi
  fi
else
  echo "WARNING: no $file.version sidecar; cannot check the TimescaleDB version (running: $running_ts)." >&2
fi

if (( ! yes )); then
  echo "This REPLACES all data in the ohlcv database with the contents of $file."
  read -r -p 'Type "restore" to continue: ' answer
  [[ "$answer" == "restore" ]] || { echo "Aborted."; exit 1; }
fi

# 3. Safety backup of the current database (KEEP=0: never prunes).
echo "Taking a safety backup of the current database..."
safety_out="$(PROJECT="${PROJECT:-}" KEEP=0 scripts/backup.sh)" || { echo "error: safety backup failed; nothing was changed" >&2; exit 1; }
echo "$safety_out"
safety="$(sed -n 's/^Backup written: \(.*\) (.*)$/\1/p' <<< "$safety_out")"
[[ -s "$safety" ]] || { echo "error: could not locate the safety dump; nothing was changed" >&2; exit 1; }

# 4. Stop the app, remembering whether it was running.
was_running=0
[[ -n "$("${dc[@]}" ps --status running -q app 2>/dev/null)" ]] && was_running=1
app_stopped=0 pre_done=0

cleanup() {
  local code=$?
  trap - EXIT INT TERM
  if (( pre_done )); then
    echo "Running timescaledb_post_restore()..."
    if ! psql_in ohlcv -c "SELECT timescaledb_post_restore()" > /dev/null; then
      echo "ERROR: timescaledb_post_restore() failed" >&2
      (( code )) || code=1
    fi
  fi
  if (( code )); then
    {
      echo
      echo "RESTORE FAILED (exit $code). The app is left STOPPED."
      echo "Safety dump of the previous database: $safety"
      echo "Restore it with:  ${project_prefix}scripts/restore.sh $safety --yes"
      echo "Then start the app: docker compose ${project_flag}start app"
    } >&2
  elif (( app_stopped && was_running )); then
    echo "Starting app..."
    "${dc[@]}" start app
  fi
  exit "$code"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if (( was_running )); then
  echo "Stopping app..."
  "${dc[@]}" stop app
fi
app_stopped=1

# 5. Fresh database, extension, pre_restore. DROP/CREATE DATABASE must run connected to another database.
echo "Recreating the ohlcv database..."
dropped=0
for _ in 1 2 3 4 5; do
  psql_in postgres -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'ohlcv' AND pid <> pg_backend_pid()" > /dev/null
  if psql_in postgres -c "DROP DATABASE IF EXISTS ohlcv" > /dev/null 2>&1; then dropped=1; break; fi
  sleep 1
done
(( dropped )) || { echo "error: could not drop the ohlcv database (still in use?)" >&2; exit 1; }
psql_in postgres -c "CREATE DATABASE ohlcv" > /dev/null
psql_in ohlcv -c "CREATE EXTENSION IF NOT EXISTS timescaledb" > /dev/null
psql_in ohlcv -c "SELECT timescaledb_pre_restore()" > /dev/null
pre_done=1  # from here the trap always runs timescaledb_post_restore()

# 6. Restore into the fresh database (no --clean).
echo "Restoring $file..."
"${dc[@]}" exec -T db pg_restore -U ohlcv -d ohlcv < "$file"

echo "Restore of $file complete."
