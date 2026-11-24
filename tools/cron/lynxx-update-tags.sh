#!/usr/bin/env sh
# Weekly tag lists update for Lynxx autocompletion. Called by cron (see crontab.example) or systemd timer.
# Env: LYNXX_TAG_MODULES (default rx,db), LYNXX_TAG_MIN_POSTS (default 2), LYNXX_API_KEY_RX, LYNXX_API_KEY_DB, LYNXX_TAGS_OUT (default <repo>/2tags)
set -eu
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PY="${LYNXX_PYTHON:-$REPO/.venv/bin/python}"
[ -x "$PY" ] || PY="$(command -v python3)"
LOG_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/lynxx"
mkdir -p "$LOG_DIR"
LOCK="$LOG_DIR/update-tags.lock"
OUT="${LYNXX_TAGS_OUT:-$REPO/2tags}"
# one run at a time
exec 9>"$LOCK"
if command -v flock >/dev/null 2>&1; then flock -n 9 || { echo "already running"; exit 0; }; fi
{
  for m in $(echo "${LYNXX_TAG_MODULES:-rx,db}" | tr ',' ' '); do
    key=""
    [ "$m" = "rx" ] && key="${LYNXX_API_KEY_RX:-}"
    [ "$m" = "db" ] && key="${LYNXX_API_KEY_DB:-}"
    "$PY" "$REPO/tools/update_tags.py" --modules "$m" --out "$OUT" --min-posts "${LYNXX_TAG_MIN_POSTS:-2}" ${key:+--api-key "$key"} || echo "module $m failed"
  done
} >>"$LOG_DIR/update-tags.log" 2>&1
