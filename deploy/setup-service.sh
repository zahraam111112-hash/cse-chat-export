#!/usr/bin/env bash
# Provision the ChatGPT Share Exporter as unprivileged systemd user services.
# Run as the unprivileged dev user ($USER) — no sudo required.
# Tokens are read from the environment or from ~/.config/chatgpt-share-export.env
set -euo pipefail

# Default to whatever checkout this script lives in, so the clone can be
# anywhere (~/apps/cse, /opt/cse, …) without editing the script.
APP_DIR="${APP_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PORT="${PORT:-8098}"
ENV_FILE="$HOME/.config/chatgpt-share-export.env"
UNIT_DIR="$HOME/.config/systemd/user"
PY="$APP_DIR/.venv/bin/python"

echo "==> app dir: $APP_DIR"
mkdir -p "$APP_DIR" "$UNIT_DIR" "$(dirname "$ENV_FILE")"

if [ ! -d "$APP_DIR/.venv" ]; then
  echo "==> creating venv (first run only)"
  python3 -m venv "$APP_DIR/.venv"
  "$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip wheel
  "$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
fi

if [ ! -d "$HOME/.cache/ms-playwright" ]; then
  echo "==> installing chromium for playwright (one-time, ~150MB)"
  "$PY" -m playwright install chromium
fi

if [ ! -f "$ENV_FILE" ]; then
  echo "==> writing $ENV_FILE (edit it to add TELEGRAM_BOT_TOKEN)"
  cat > "$ENV_FILE" <<'EOF'
CSE_HOST=127.0.0.1
CSE_PORT=8099
# CSE_PROXY=http://127.0.0.1:10808
# TELEGRAM_BOT_TOKEN=
# TELEGRAM_CHAT_ID=
EOF
  chmod 600 "$ENV_FILE"
fi

if ! loginctl show-user "$USER" 2>/dev/null | grep -q 'Linger=yes'; then
  echo "==> enabling linger so user services survive logout"
  loginctl enable-linger "$USER" || echo "  (skipped: needs privilege; services stop at logout)"
fi

cat > "$UNIT_DIR/chatgpt-share-export.service" <<EOF
[Unit]
Description=ChatGPT Share Exporter (web + API)
After=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$PY -I run.py
Restart=always
RestartSec=5
MemoryMax=1200M

[Install]
WantedBy=default.target
EOF

cat > "$UNIT_DIR/chatgpt-share-export-bot.service" <<EOF
[Unit]
Description=ChatGPT Share Exporter (telegram bot)
After=network-online.target chatgpt-share-export.service
Requires=chatgpt-share-export.service

[Service]
Type=simple
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$PY -I run_bot.py
Restart=always
RestartSec=10
MemoryMax=800M

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now chatgpt-share-export.service

echo "==> status"
sleep 3
systemctl --user --no-pager status chatgpt-share-export.service | head -12
echo
echo "==> health"
curl -sS --max-time 10 "http://127.0.0.1:$PORT/healthz" || echo "not ready yet"
echo
echo "Next: set TELEGRAM_BOT_TOKEN in $ENV_FILE then:"
echo "  systemctl --user enable --now chatgpt-share-export-bot.service"
