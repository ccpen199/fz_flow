#!/usr/bin/env bash
set -euo pipefail

for agent in com.fzworkflow.local-demo com.fzworkflow.local-vibe; do
  plist="$HOME/Library/LaunchAgents/${agent}.plist"
  launchctl bootout "gui/$(id -u)/${agent}" 2>/dev/null || true
  rm -f "$plist"
  echo "LaunchAgent removed: $plist"
done

pkill -f "demo/run_demo.py" || true
