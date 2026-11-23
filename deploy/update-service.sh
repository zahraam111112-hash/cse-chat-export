#!/usr/bin/env bash
# Pull the latest main from GitHub and restart the services if it moved.
# Run as the app user (no sudo). Installed as a systemd user timer by
# install-update-timer.sh; safe to run by hand at any time.
#
# The checkout is the deployable artefact: data/ is gitignored local state and
# is never touched, so a hard reset to origin/main only ever changes code.
set -euo pipefail

APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
BRANCH="${BRANCH:-main}"
SERVICES="${SERVICES:-chatgpt-share-export.service chatgpt-share-export-bot.service}"
HEALTH="${HEALTH:-http://127.0.0.1:8098/healthz}"
PY="$APP_DIR/.venv/bin/python"
LOCK="${TMPDIR:-/tmp}/cse-update.lock"

# The dashboard needs auth; read it from the same env file the services use so
# the post-update check can fetch a real page instead of just the liveness ping.
[ -f "$HOME/.config/chatgpt-share-export.env" ] && . "$HOME/.config/chatgpt-share-export.env"
PORT="${CSE_PORT:-8098}"
CHECK_PATH="/healthz?deep=1"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

# A slow extraction or a manual run must not overlap with the timer. Without
# flock we still update — a concurrent run is a lesser risk than never syncing.
exec 9>"$LOCK"
if command -v flock >/dev/null 2>&1; then
  flock -n 9 || { log "another update is running, skipping"; exit 0; }
fi

cd "$APP_DIR"

# The app lives in a subdirectory of the repo, and `git rev-parse rev:path`
# resolves against the repo root, so build the path from the toplevel.
TOP="$(git rev-parse --show-toplevel)"
REQ="${APP_DIR#"$TOP"/}/requirements.txt"

before="$(git rev-parse HEAD)"

if ! git fetch --quiet origin "$BRANCH"; then
  log "fetch failed (offline or key revoked) — keeping $before"
  exit 0
fi

git reset --hard --quiet "origin/$BRANCH"
after="$(git rev-parse HEAD)"

if [ "$before" = "$after" ]; then
  log "up to date at $after"
  exit 0
fi

log "updating ${before:0:7} -> ${after:0:7}"
git --no-pager log --oneline "$before..$after"

if [ "$(git rev-parse "$before:$REQ" 2>/dev/null)" != "$(git rev-parse "$after:$REQ" 2>/dev/null)" ]; then
  log "requirements changed — reinstalling"
  if ! "$PY" -m pip install --quiet -r requirements.txt; then
    log "pip failed — rolling back to $before"
    git reset --hard --quiet "$before"
    exit 1
  fi
fi

systemctl --user restart $SERVICES

# /healthz is liveness only — it says the process is up, not that it serves.
# The deep check renders the dashboard template and touches SQLite, so a deploy
# that breaks either is caught. It must never depend on job data: an empty
# history (everything deleted, or nothing run yet) is a healthy state.
check() {
  curl -fsS --max-time 5 "$HEALTH" >/dev/null 2>&1 || return 1
  body="$(curl -fsS --max-time 10 "http://127.0.0.1:${PORT}${CHECK_PATH}" 2>/dev/null)" || return 1
  printf '%s' "$body" | grep -q '"template": *true' || return 1
  printf '%s' "$body" | grep -q '"db": *true'
}

for _ in $(seq 1 20); do
  sleep 1
  if check; then
    log "healthy at $after"
    exit 0
  fi
done

log "health check failed after update — rolling back to $before"
git reset --hard --quiet "$before"
systemctl --user restart $SERVICES
exit 1
