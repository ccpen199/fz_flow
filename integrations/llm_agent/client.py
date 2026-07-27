from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib import error, request


CONFIG_CENTER_SYSTEM_PROMPT = """
You are the configuration-center recommendation LLM for a clothing data analysis
application.

Use INPUT.scene, INPUT.goal, INPUT.schema, INPUT.fallback_candidates, and any
provided context to recommend scene tables, semantic fields, relations, metric
templates, and regression questions. This is not a local rule path: infer
business meaning from the supplied schema and candidates.

Return only valid JSON, with no markdown and no extra text. The JSON shape must be:
{
  "candidates": {
    "tables": [],
    "fields": [],
    "relations": [],
    "metric_templates": [],
    "regression_questions": []
  },
  "field_type_list": [],
  "notes": []
}

Each field item must include table_name, field_name, semantic_name, description,
role, field_type, and enabled. Each relation item must include left_table,
left_field, right_table, right_field, join_type, and note.

When uncertain, prefer INPUT.fallback_candidates over inventing schema items.
""".strip()


@dataclass
class LlmAgentConfig:
    provider: str = "http"
    endpoint: str | None = None
    api_key: str | None = None
    model: str = "gpt-5.2"
    timeout_seconds: int = 90
    config_source: str = "SQL_RESULT_AGENT_*"


SUPPORTED_HTTP_PROVIDERS = {"http", "modelscope"}


class LlmAgentClient:
    """Configuration-center LLM connector.

    Runtime model selection is intentionally shared with the SQL result agent:
    SQL_RESULT_AGENT_HTTP_MODEL is the single model knob for this project.
    """

    def __init__(self) -> None:
        self.config = self._build_config()

    def _build_config(self) -> LlmAgentConfig:
        uses_llm_agent_env = any(
            os.getenv(name)
            for name in (
                "LLM_AGENT_PROVIDER",
                "LLM_AGENT_HTTP_ENDPOINT",
                "LLM_AGENT_API_KEY",
                "LLM_AGENT_HTTP_MODEL",
                "LLM_AGENT_HTTP_TIMEOUT",
            )
        )
        provider = os.getenv("LLM_AGENT_PROVIDER") or os.getenv("SQL_RESULT_AGENT_PROVIDER") or "http"
        endpoint = os.getenv("LLM_AGENT_HTTP_ENDPOINT") or os.getenv("SQL_RESULT_AGENT_HTTP_ENDPOINT") or ""
        api_key = os.getenv("LLM_AGENT_API_KEY") or os.getenv("SQL_RESULT_AGENT_API_KEY") or ""
        model = os.getenv("LLM_AGENT_HTTP_MODEL") or os.getenv("SQL_RESULT_AGENT_HTTP_MODEL") or "gpt-5.2"
        timeout = os.getenv("LLM_AGENT_HTTP_TIMEOUT") or os.getenv("SQL_RESULT_AGENT_HTTP_TIMEOUT") or "90"
        return LlmAgentConfig(
            provider=provider.strip().lower() or "http",
            endpoint=endpoint.strip() or None,
            api_key=api_key.strip() or None,
            model=model.strip() or "gpt-5.2",
            timeout_seconds=int(timeout),
            config_source="LLM_AGENT_*" if uses_llm_agent_env else "SQL_RESULT_AGENT_*",
        )

    def health(self) -> dict:
        return {
            "status": "ok",
            "service": "llm-agent",
            "provider": self.config.provider,
            "endpoint": self.config.endpoint,
            "model": self.config.model,
            "config_source": self.config.config_source,
            "ready": self.config.provider in SUPPORTED_HTTP_PROVIDERS and bool(self.config.endpoint),
        }

    def recommend(self, payload: dict) -> dict:
        if self.config.provider not in SUPPORTED_HTTP_PROVIDERS:
            raise RuntimeError(
                f"unsupported LLM agent provider={self.config.provider}; "
                "this project runtime supports provider=http or provider=modelscope"
            )
        if not self.config.endpoint:
            raise RuntimeError(f"{self.config.config_source.replace('*', 'HTTP_ENDPOINT')} is required for configuration recommendations")
        return self._recommend_via_http(payload)

    def _recommend_via_http(self, payload: dict) -> dict:
        request_payload = self._build_chat_payload(payload)
        body = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        req = request.Request(self.config.endpoint or "", data=body, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=self.config.timeout_seconds) as resp:  # noqa: S310
                raw_text = resp.read().decode("utf-8")
                parsed = json.loads(raw_text) if raw_text else {}
        except error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"llm-agent http provider failed: HTTP {exc.code} {detail}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"llm-agent http provider failed: {exc}") from exc

        return self._normalize_response(parsed, raw_text)

    def _build_chat_payload(self, payload: dict) -> dict:
        return {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": CONFIG_CENTER_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "temperature": 0.2,
            "stream": False,
        }

    def _normalize_response(self, parsed: dict | list | None, raw_text: str) -> dict:
        inner = parsed if isinstance(parsed, dict) else {}
        content = self._extract_message_content(parsed)
        if content:
            parsed_content = self._parse_json_loose(content)
            if isinstance(parsed_content, dict):
                inner = parsed_content

        candidates = inner.get("candidates", inner if any(k in inner for k in ("tables", "fields", "relations")) else {})
        notes = inner.get("notes", []) if isinstance(inner.get("notes"), list) else []
        return {
            "provider": self.config.provider,
            "mode": "remote",
            "model": self.config.model,
            "candidates": candidates if isinstance(candidates, dict) else {},
            "raw": content or raw_text,
            "notes": notes,
        }

    def _extract_message_content(self, parsed: dict | list | None) -> str:
        if not isinstance(parsed, dict):
            return ""

        choices = parsed.get("choices", [])
        if isinstance(choices, list) and choices:
            first = choices[0] if isinstance(choices[0], dict) else {}
            message = first.get("message", {}) if isinstance(first.get("message"), dict) else {}
            content = message.get("content")
            if isinstance(content, str):
                return content.strip()

        for key in ("output_text", "content"):
            value = parsed.get(key)
            if isinstance(value, str):
                return value.strip()

        output = parsed.get("output", [])
        if isinstance(output, list):
            parts: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                content_blocks = item.get("content", [])
                if not isinstance(content_blocks, list):
                    continue
                for block in content_blocks:
                    if not isinstance(block, dict):
                        continue
                    text = block.get("text") or block.get("content")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
            if parts:
                return "\n".join(parts)

        return ""

    def _parse_json_loose(self, raw_text: str) -> dict | list | None:
        text = str(raw_text or "").strip()
        if not text:
            return None
        for candidate in self._json_candidates(text):
            try:
                return json.loads(candidate)
            except Exception:
                continue
        return None

    def _json_candidates(self, text: str) -> list[str]:
        candidates = [text]
        if text.startswith("```"):
            fenced = text.strip("`")
            if fenced.lower().startswith("json"):
                fenced = fenced[4:]
            candidates.append(fenced.strip())

        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            candidates.append(text[start : end + 1].strip())

        seen: set[str] = set()
        uniq: list[str] = []
        for item in candidates:
            normalized = item.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            uniq.append(normalized)
        return uniq
