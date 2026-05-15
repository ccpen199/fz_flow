#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HARNESS_PATH="$ROOT_DIR/backend/packages/harness"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

run_loader() {
  local providers_path="$1"
  local auth_path="$2"
  local openai_key="${3:-}"

  CODEX_PROVIDERS_PATH="$providers_path" \
  CODEX_AUTH_PATH="$auth_path" \
  OPENAI_API_KEY="$openai_key" \
  HARNESS_PATH="$HARNESS_PATH" \
  python3 - <<'PY'
import importlib.util
import os
from pathlib import Path

module_path = Path(os.environ["HARNESS_PATH"]) / "deerflow" / "models" / "credential_loader.py"
spec = importlib.util.spec_from_file_location("credential_loader", module_path)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)
load_codex_cli_credential = module.load_codex_cli_credential

cred = load_codex_cli_credential()
if cred is None:
    print("NONE")
else:
    print(f"{cred.access_token}|{cred.source}")
PY
}

echo "Case 1: activeProvider 命中"
cat >"$TMP_DIR/providers-active.json" <<'JSON'
{
  "activeProvider": "OPENAI_API_KEY",
  "fallback": ["SECONDARY_KEY"],
  "providers": {
    "OPENAI_API_KEY": { "api_key": "sk-active" },
    "SECONDARY_KEY": { "api_key": "sk-secondary" }
  }
}
JSON
echo '{}' >"$TMP_DIR/auth-empty.json"

out="$(run_loader "$TMP_DIR/providers-active.json" "$TMP_DIR/auth-empty.json")"
[[ "$out" == "sk-active|codex-providers:OPENAI_API_KEY" ]] || {
  echo "FAILED: expected sk-active|codex-providers:OPENAI_API_KEY, got: $out" >&2
  exit 1
}

echo "Case 2: activeProvider 无 token，回退到 fallback"
cat >"$TMP_DIR/providers-fallback.json" <<'JSON'
{
  "activeProvider": "OPENAI_API_KEY",
  "fallback": ["SECONDARY_KEY"],
  "providers": {
    "OPENAI_API_KEY": {},
    "SECONDARY_KEY": { "api_key": "sk-secondary" }
  }
}
JSON

out="$(run_loader "$TMP_DIR/providers-fallback.json" "$TMP_DIR/auth-empty.json")"
[[ "$out" == "sk-secondary|codex-providers:SECONDARY_KEY" ]] || {
  echo "FAILED: expected sk-secondary|codex-providers:SECONDARY_KEY, got: $out" >&2
  exit 1
}

echo "Case 3: providers 无效，回退到 auth.json"
cat >"$TMP_DIR/providers-invalid.json" <<'JSON'
{
  "activeProvider": "OPENAI_API_KEY",
  "fallback": ["SECONDARY_KEY"],
  "providers": {
    "OPENAI_API_KEY": {},
    "SECONDARY_KEY": {}
  }
}
JSON
cat >"$TMP_DIR/auth-legacy.json" <<'JSON'
{
  "access_token": "sk-auth"
}
JSON

out="$(run_loader "$TMP_DIR/providers-invalid.json" "$TMP_DIR/auth-legacy.json")"
[[ "$out" == "sk-auth|codex-cli" ]] || {
  echo "FAILED: expected sk-auth|codex-cli, got: $out" >&2
  exit 1
}

echo "Smoke OK: codex provider resolution works."
