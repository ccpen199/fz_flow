from __future__ import annotations

import json
import os
from dataclasses import dataclass
from urllib import error, request


@dataclass
class SqlResultAgentConfig:
    provider: str = "http"
    endpoint: str | None = None
    api_key: str | None = None
    model: str = "gpt-5.2"
    timeout_seconds: int = 90


SUPPORTED_HTTP_PROVIDERS = {"http", "modelscope"}


class SqlResultAgentClient:
    """LLM connector for SQL generation and optional plan metadata."""

    def __init__(self) -> None:
        self.config = self._build_config()

    def _build_config(self) -> SqlResultAgentConfig:
        return SqlResultAgentConfig(
            provider=(os.getenv("SQL_RESULT_AGENT_PROVIDER") or "http").strip().lower() or "http",
            endpoint=(os.getenv("SQL_RESULT_AGENT_HTTP_ENDPOINT") or "").strip() or None,
            api_key=(os.getenv("SQL_RESULT_AGENT_API_KEY") or "").strip() or None,
            model=(os.getenv("SQL_RESULT_AGENT_HTTP_MODEL") or "gpt-5.2").strip() or "gpt-5.2",
            timeout_seconds=int(os.getenv("SQL_RESULT_AGENT_HTTP_TIMEOUT", "90")),
        )

    def health(self) -> dict:
        return {
            "status": "ok",
            "service": "sql-result-agent",
            "provider": self.config.provider,
            "endpoint": self.config.endpoint,
            "model": self.config.model,
            "config_source": "SQL_RESULT_AGENT_*",
            "ready": self.config.provider in SUPPORTED_HTTP_PROVIDERS and bool(self.config.endpoint),
        }

    def generate_plan(self, payload: dict) -> dict:
        if self.config.provider not in SUPPORTED_HTTP_PROVIDERS:
            raise RuntimeError(
                f"unsupported SQL_RESULT_AGENT_PROVIDER={self.config.provider}; "
                "this project runtime supports provider=http or provider=modelscope"
            )
        if not self.config.endpoint:
            raise RuntimeError("SQL_RESULT_AGENT_HTTP_ENDPOINT is required for SQL generation")
        return self._via_http(payload)

    def _system_prompt(self) -> str:
        return (
            "You are the production SQL generation LLM for a clothing data analysis application. "
            "The backend will not programmatically write SQL for you; it only validates read-only SQL and executes it. "
            "Generate real executable MySQL 8.0 SQL from INPUT.scene, INPUT.semantic_fields, INPUT.relations, "
            "INPUT.scene_playbook, INPUT.selected_preset, INPUT.generation_rules, INPUT.intent, and INPUT.agent_prompt. "
            "Business terms in the user intent, including clothing product names, categories, styles, materials, "
            "functions, and descriptors in Chinese, Japanese, or English, must be interpreted by you and translated "
            "into filters over available real fields when the schema supports that. Use available category/name/"
            "description fields with equality or LIKE predicates as appropriate; do not expect backend code to add "
            "business filters. "
            "Use only table_name.field_name entries present in INPUT.semantic_fields and joins declared in INPUT.relations. "
            "Do not invent fields, tables, platforms, sales metrics, size structures, or relationships. "
            "If INPUT.selected_preset is not empty, follow its field_requirements, derived_metrics, group_by, sort, limit, "
            "and notes before making your own choices. "
            "SQL must be a single read-only statement: SELECT or WITH followed by SELECT. No write operations. "
            "The returned SQL must be directly executable by PyMySQL. Do not return placeholders or pseudo parameters, "
            "including :subcategory, :brand, ?, ${value}, <value>, to-be-confirmed, or values that require backend substitution. "
            "If a concrete filter value is missing, group by the relevant dimension or return empty sql with a clear risk_note. "
            "For recent/latest/last-30-days business windows, anchor on the maximum available data date in the relevant "
            "table, not CURRENT_DATE, CURDATE(), NOW(), or system time. Use MySQL syntax such as "
            "DATE_SUB(anchor_date, INTERVAL 30 DAY), not PostgreSQL interval syntax. "
            "When joining one-to-many extension tables, count SKUs with COUNT(DISTINCT clothing_info.Id) when SKU counts "
            "are required. "
            "If the question asks for product-level detail or candidate rows, do not aggregate unless the preset explicitly "
            "requires aggregation. "
            "metrics and dimensions in the plan must be arrays of semantic_name strings. filters.field must be a "
            "semantic_name string. "
            "Return only JSON, with no markdown and no explanation outside JSON. The JSON shape must be: "
            '{"sql":"","sql_explanation":"","plan":{"intent":"","metrics":[],"dimensions":[],"filters":[],"time_window":"",'
            '"chart_candidates":[],"risk_notes":[]},"notes":[]}. '
            "If the available fields or relations are insufficient to answer truthfully, return empty sql and explain the "
            "missing item in plan.risk_notes."
        )

    def _via_http(self, payload: dict) -> dict:
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
            raise RuntimeError(f"sql-result http provider failed: HTTP {exc.code} {detail}") from exc
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"sql-result http provider failed: {exc}") from exc

        return self._normalize_response(parsed, raw_text)

    def _build_chat_payload(self, payload: dict) -> dict:
        return {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
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

        if not isinstance(inner, dict):
            inner = {}
        plan = inner.get("plan", inner if isinstance(inner, dict) else {})
        return {
            "provider": self.config.provider,
            "mode": "remote",
            "model": self.config.model,
            "sql": str(inner.get("sql") or "").strip(),
            "sql_explanation": str(inner.get("sql_explanation") or "").strip(),
            "plan": plan if isinstance(plan, dict) else {},
            "notes": inner.get("notes", []) if isinstance(inner.get("notes"), list) else [],
            "raw": content or raw_text,
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
        items = [text]
        if text.startswith("```"):
            fenced = text.strip("`")
            if fenced.lower().startswith("json"):
                fenced = fenced[4:]
            items.append(fenced.strip())
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            items.append(text[start : end + 1].strip())
        uniq: list[str] = []
        seen: set[str] = set()
        for item in items:
            normalized = item.strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            uniq.append(normalized)
        return uniq
