#!/usr/bin/env python3

from __future__ import annotations

from pathlib import Path


MODEL_BLOCK = """models:
  - name: gpt-5-codex
    display_name: GPT-5 (Codex CLI)
    use: deerflow.models.openai_codex_provider:CodexChatModel
    model: gpt-5
    supports_thinking: true
    supports_reasoning_effort: true
"""


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    config_path = root / "config.yaml"

    if not config_path.exists():
        raise SystemExit("config.yaml not found; run scripts/configure.py first")

    content = config_path.read_text()
    if "use: deerflow.models.openai_codex_provider:CodexChatModel" in content:
        return 0

    marker = "models:\n"
    if marker not in content:
        raise SystemExit("models section not found in config.yaml")

    updated = content.replace(marker, MODEL_BLOCK, 1)
    config_path.write_text(updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
