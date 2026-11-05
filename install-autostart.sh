#!/usr/bin/env bash
# TradeVMx :: start the desk automatically at login.
#   Linux  -> systemd user unit (restarts on failure)
#   macOS  -> LaunchAgent (restarts on exit)
# The desk runs headless with --no-browser; open http://127.0.0.1:8765 yourself.
set -euo pipefail

cd "$(dirname "$0")"
DIR="$(pwd)"
PY="$DIR/.venv/bin/python"

if [ ! -x "$PY" ]; then
  echo "[TradeVMx] Run ./run_tradevmx.sh once first - it creates the environment." >&2
  exit 1
fi

mkdir -p "$DIR/logs"

if [ "$(uname -s)" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/com.tradevmx.desk.plist"
  mkdir -p "$(dirname "$PLIST")"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.tradevmx.desk</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DIR/desktop.py</string>
    <string>--no-browser</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$DIR/logs/autostart.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/autostart.log</string>
</dict>
</plist>
EOF
  launchctl unload "$PLIST" 2>/dev/null || true
  launchctl load "$PLIST"
  echo "[TradeVMx] Installed $PLIST"
  echo "[TradeVMx] It starts at login and restarts if it dies. Console: http://127.0.0.1:8765"
  exit 0
fi

UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat > "$UNIT_DIR/tradevmx.service" <<EOF
[Unit]
Description=TradeVMx NSE market desk
After=network-online.target

[Service]
WorkingDirectory=$DIR
ExecStart="$PY" "$DIR/desktop.py" --no-browser
Restart=always
RestartSec=20

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now tradevmx.service
# Keep the desk alive even when you are not logged in graphically.
loginctl enable-linger "$USER" 2>/dev/null || true

echo "[TradeVMx] Enabled tradevmx.service"
echo "[TradeVMx] Status:  systemctl --user status tradevmx"
echo "[TradeVMx] Logs:    journalctl --user -u tradevmx -f"
echo "[TradeVMx] Remove:  systemctl --user disable --now tradevmx"
