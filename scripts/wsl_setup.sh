#!/usr/bin/env bash
set -euo pipefail

echo "[1/4] Update apt indexes..."
sudo apt update

echo "[2/4] Install core packages..."
sudo apt install -y git tmux curl unzip build-essential

echo "[3/4] Create workspace directory..."
mkdir -p "$HOME/workspace"

echo "[4/4] Ensure tmux basic config..."
if [[ ! -f "$HOME/.tmux.conf" ]]; then
  cat > "$HOME/.tmux.conf" <<'EOF'
set -g mouse on
set -g history-limit 50000
setw -g mode-keys vi
EOF
fi

echo "WSL setup completed. Next:"
echo "  cd ~/workspace"
echo "  git clone <repo-url>"
echo "  tmux new -s <project>"

