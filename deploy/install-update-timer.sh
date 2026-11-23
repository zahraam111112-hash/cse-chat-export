#!/usr/bin/env bash
# Install (or refresh) the systemd user timer that pulls main from GitHub.
# Run as the app user after the clone in setup-service.sh exists.
set -euo pipefail

APP_DIR="${APP_DIR:-$HOME/cse-repo/chatgpt-share-export}"
UNIT_DIR="$HOME/.config/systemd/user"

echo "==> app dir: $APP_DIR"
chmod +x "$APP_DIR/deploy/update-service.sh"

cat > "$UNIT_DIR/cse-update.service" <<EOF
[Unit]
Description=ChatGPT Share Exporter — pull main and restart

[Service]
Type=oneshot
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/deploy/update-service.sh
EOF

cat > "$UNIT_DIR/cse-update.timer" <<EOF
[Unit]
Description=ChatGPT Share Exporter — poll GitHub every 10 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=10min
AccuracySec=30s
Unit=cse-update.service

[Install]
WantedBy=timers.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now cse-update.timer

echo "==> first run"
systemctl --user start cse-update.service
systemctl --user --no-pager status cse-update.service | tail -8
echo
systemctl --user list-timers cse-update.timer --no-pager
