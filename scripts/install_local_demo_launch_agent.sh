#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
AGENT_ID="com.fzworkflow.local-vibe"
PLIST_PATH="$HOME/Library/LaunchAgents/${AGENT_ID}.plist"
LOG_FILE="$ROOT_DIR/data/fz-local-service.log"

mkdir -p "$HOME/Library/LaunchAgents" "$ROOT_DIR/data"

cat >"$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key>
    <string>${AGENT_ID}</string>
    <key>ProgramArguments</key>
    <array>
      <string>/bin/bash</string>
      <string>-lc</string>
      <string>cd '${ROOT_DIR}' && bash scripts/start_vibe_stack.sh</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>${LOG_FILE}</string>
    <key>StandardErrorPath</key>
    <string>${LOG_FILE}</string>
  </dict>
</plist>
PLIST

# Remove legacy demo LaunchAgent if exists.
launchctl bootout "gui/$(id -u)/com.fzworkflow.local-demo" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/com.fzworkflow.local-demo.plist"

launchctl bootout "gui/$(id -u)/${AGENT_ID}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
launchctl enable "gui/$(id -u)/${AGENT_ID}"
launchctl kickstart -k "gui/$(id -u)/${AGENT_ID}"

echo "LaunchAgent installed: $PLIST_PATH"
echo "Log file: $LOG_FILE"
