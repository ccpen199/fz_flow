from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib import error, request

from .codex_cli_lock import codex_cli_lock


@dataclass
class LlmAgentConfig:
    provider: str = "heuristic"
    endpoint: str | None = None
    api_key: str | None = None
    model: str | None = None
    timeout_seconds: int = 30
    codex_bin: str = "codex"
    codex_model: str | None = None
    codex_home: str | None = None
    codex_home_fallbacks: tuple[str, ...] = ()
    codex_cwd: str | None = None
    codex_bypass_sandbox: bool = False


class LlmAgentClient:
    """Independent LLM-agent connector (not coupled with deepflow)."""

    def __init__(self) -> None:
        self.config = self._build_config()

    def _build_config(self) -> LlmAgentConfig:
        fallback_homes_raw = (os.getenv("LLM_AGENT_CODEX_HOME_FALLBACKS") or "").strip()
        fallback_homes = tuple(home.strip() for home in fallback_homes_raw.split(",") if home.strip())
        inferred_fallback_homes = self._discover_codex_home_fallbacks()
        if not fallback_homes and inferred_fallback_homes:
            fallback_homes = inferred_fallback_homes
        return LlmAgentConfig(
            provider=os.getenv("LLM_AGENT_PROVIDER", "heuristic").strip().lower() or "heuristic",
            endpoint=(os.getenv("LLM_AGENT_HTTP_ENDPOINT") or "").strip() or None,
            api_key=(os.getenv("LLM_AGENT_API_KEY") or "").strip() or None,
            model=(os.getenv("LLM_AGENT_HTTP_MODEL") or "").strip() or None,
            timeout_seconds=int(os.getenv("LLM_AGENT_HTTP_TIMEOUT", "30")),
            codex_bin=(os.getenv("LLM_AGENT_CODEX_BIN") or "codex").strip() or "codex",
            codex_model=(os.getenv("LLM_AGENT_CODEX_MODEL") or "").strip() or None,
            codex_home=(os.getenv("LLM_AGENT_CODEX_HOME") or "").strip() or None,
            codex_home_fallbacks=fallback_homes,
            codex_cwd=(os.getenv("LLM_AGENT_CODEX_CWD") or "").strip() or None,
            codex_bypass_sandbox=(os.getenv("LLM_AGENT_CODEX_BYPASS_SANDBOX", "0").strip() == "1"),
        )

    def _discover_codex_home_fallbacks(self) -> tuple[str, ...]:
        candidates: list[str] = []
        repo_root = Path(__file__).resolve().parents[2]
        configured_root = (os.getenv("CODEX_PROVIDER_HOMES_DIR") or "").strip()
        search_roots: list[Path] = []
        if configured_root:
            search_roots.append(Path(configured_root).expanduser())
        search_roots.append(repo_root / "data" / "codex-provider-homes")
        for ancestor in repo_root.parents:
            search_roots.append(ancestor / "Tools-project" / "data" / "codex-provider-homes")
        seen: set[str] = set()
        for root in search_roots:
            resolved_root = root.resolve()
            marker = str(resolved_root)
            if marker in seen:
                continue
            seen.add(marker)
            if not resolved_root.exists() or not resolved_root.is_dir():
                continue
            for home in sorted(resolved_root.iterdir()):
                if not home.is_dir():
                    continue
                if not (home / ".codex").exists():
                    continue
                normalized = str(home)
                if normalized in seen:
                    continue
                seen.add(normalized)
                candidates.append(normalized)
        return tuple(candidates)

    def health(self) -> dict:
        codex_ready = True
        if self.config.provider in {"codex", "codex_cli"}:
            codex_ready = bool(self.config.codex_bin)
        return {
            "status": "ok",
            "service": "llm-agent",
            "provider": self.config.provider,
            "endpoint": self.config.endpoint,
            "codex_bin": self.config.codex_bin,
            "codex_cwd": self.config.codex_cwd,
            "ready": (
                (self.config.provider != "http" or bool(self.config.endpoint))
                and (self.config.provider not in {"codex", "codex_cli"} or codex_ready)
            ),
        }

    def recommend(self, payload: dict) -> dict:
        provider = self.config.provider
        if provider in {"http", "modelscope"}:
            if not self.config.endpoint:
                raise RuntimeError(f"LLM_AGENT_HTTP_ENDPOINT is required when provider={provider}")
            return self._recommend_via_http(payload, provider=provider)
        if provider in {"codex", "codex_cli"}:
            return self._recommend_via_codex_cli(payload)
        return {
            "provider": "heuristic",
            "mode": "local",
            "candidates": {},
            "raw": "",
            "notes": ["heuristic provider does not call external LLM"],
        }

    def _recommend_via_http(self, payload: dict, *, provider: str = "http") -> dict:
        request_payload = payload
        if provider == "modelscope":
            request_payload = self._build_modelscope_payload(payload)
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

        if provider == "modelscope":
            return self._normalize_modelscope_response(parsed, raw_text)
        return {
            "provider": "http",
            "mode": "remote",
            "candidates": parsed.get("candidates", {}) if isinstance(parsed, dict) else {},
            "raw": raw_text,
            "notes": parsed.get("notes", []) if isinstance(parsed, dict) else [],
        }

    def _build_modelscope_payload(self, payload: dict) -> dict:
        model = self.config.model or "Qwen/Qwen3-235B-A22B-Instruct-2507"
        instruction = (
            "你是服装商品分析场景的推荐助手。"
            "请根据输入中的 scene、goal、schema 与 fallback_candidates 输出推荐结果。"
            "只能返回 JSON，不要 markdown，不要解释。"
            '格式必须为：{"candidates":{"tables":[],"fields":[],"relations":[],"metric_templates":[],"regression_questions":[]},"field_type_list":[],"notes":[]}。'
            "fields 每项必须包含 table_name、field_name、semantic_name、description、role、field_type、enabled。"
            "relations 每项必须包含 left_table、left_field、right_table、right_field、join_type、note。"
            "如果不确定，请优先参考 fallback_candidates。"
        )
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": instruction},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "temperature": 0.2,
            "stream": False,
        }

    def _normalize_modelscope_response(self, parsed: dict | list | None, raw_text: str) -> dict:
        content = ""
        if isinstance(parsed, dict):
            choices = parsed.get("choices", [])
            if isinstance(choices, list) and choices:
                message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
                content = str(message.get("content", "")).strip()
            elif isinstance(parsed.get("content"), str):
                content = str(parsed.get("content", "")).strip()
        inner = self._parse_json_loose(content) if content else None
        if not isinstance(inner, dict):
            inner = {}
        candidates = inner.get("candidates", inner if any(k in inner for k in ("tables", "fields", "relations")) else {})
        notes = inner.get("notes", []) if isinstance(inner.get("notes"), list) else []
        return {
            "provider": "modelscope",
            "mode": "remote",
            "candidates": candidates if isinstance(candidates, dict) else {},
            "raw": content or raw_text,
            "notes": notes,
        }

    def _recommend_via_codex_cli(self, payload: dict) -> dict:
        prompt_payload = json.dumps(payload, ensure_ascii=False)
        prompt = (
            "你是一个场景剧本（scene playbook）推荐助手。"
            "只返回合法 JSON，不要返回 markdown，也不要附加任何额外文本。"
            'JSON 必须为：{"candidates":{"tables":[],"fields":[],"relations":[],"metric_templates":[],"regression_questions":[]},"field_type_list":[],"notes":[]}。'
            "fields 数组中的每一项必须包含 table_name、field_name、semantic_name、description、role、field_type、enabled。"
            "field_type 必须是数据库列类型（如 varchar、int、decimal、datetime），优先来自 schema_column_types。"
            "field_type_list 需要汇总出现过的字段类型及数量，例如[{\"field_type\":\"varchar\",\"count\":12}]。"
            "relations 数组中的每一项必须包含 left_table、left_field、right_table、right_field、join_type、note。"
            "如果不确定，请优先使用 fallback_candidates。\n"
            f"INPUT={prompt_payload}"
        )

        args = ["exec", "--skip-git-repo-check", "--json"]
        if self.config.codex_model:
            args.extend(["--model", self.config.codex_model])
        if self.config.codex_bypass_sandbox:
            args.append("--dangerously-bypass-approvals-and-sandbox")
        else:
            args.extend(["--sandbox", "workspace-write"])
        args.append(prompt)

        home_candidates = []
        for candidate in (self.config.codex_home, *self.config.codex_home_fallbacks):
            normalized = (candidate or "").strip()
            if normalized and normalized not in home_candidates:
                home_candidates.append(normalized)
        if not home_candidates:
            home_candidates = [""]

        proc = None
        attempt_errors: list[str] = []
        selected_home = ""
        for home in home_candidates:
            child_env = os.environ.copy()
            if home:
                child_env["HOME"] = home
            try:
                with codex_cli_lock():
                    trial_proc = subprocess.run(
                        [self.config.codex_bin, *args],
                        capture_output=True,
                        text=True,
                        timeout=self.config.timeout_seconds,
                        check=False,
                        cwd=self.config.codex_cwd or None,
                        env=child_env,
                    )
            except FileNotFoundError as exc:
                raise RuntimeError(f"codex cli not found: {self.config.codex_bin}") from exc
            except subprocess.TimeoutExpired as exc:
                attempt_errors.append(f"home={home or '<default>'}: timeout after {self.config.timeout_seconds}s")
                continue
            except Exception as exc:  # noqa: BLE001
                attempt_errors.append(f"home={home or '<default>'}: execution failed: {exc}")
                continue

            if trial_proc.returncode == 0:
                proc = trial_proc
                selected_home = home
                break

            stdout_text = trial_proc.stdout or ""
            stderr_text = (trial_proc.stderr or "").strip()
            attempt_errors.append(
                f"home={home or '<default>'}: exit={trial_proc.returncode}, "
                f"error={stderr_text or stdout_text[-300:] or 'unknown error'}"
            )

        if proc is None:
            raise RuntimeError(f"codex cli failed on all configured homes: {' | '.join(attempt_errors)}")

        stdout_text = proc.stdout or ""
        response_text = self._extract_codex_text(stdout_text)
        parsed = self._parse_json_loose(response_text)
        if not isinstance(parsed, dict):
            parsed = {}

        candidates: dict = {}
        notes: list[str] = []
        if "candidates" in parsed and isinstance(parsed.get("candidates"), dict):
            candidates = parsed.get("candidates", {})
            notes = parsed.get("notes", []) if isinstance(parsed.get("notes"), list) else []
        elif any(k in parsed for k in ("tables", "fields", "relations")):
            candidates = parsed
            notes = ["codex output used as direct candidates object"]
        else:
            notes = ["codex output did not include candidates, fallback to heuristic will be used"]
        if attempt_errors:
            notes.append(f"codex home fallback activated; succeeded with home={selected_home or '<default>'}")
            notes.extend([f"codex home attempt failed: {msg}" for msg in attempt_errors])

        return {
            "provider": "codex_cli",
            "mode": "cli",
            "candidates": candidates,
            "raw": response_text or stdout_text,
            "notes": notes,
        }

    def _extract_codex_text(self, stdout_text: str) -> str:
        lines = [line.strip() for line in stdout_text.splitlines() if line.strip()]
        delta_parts: list[str] = []
        completed_parts: list[str] = []

        for line in lines:
            try:
                evt = json.loads(line)
            except Exception:
                continue
            if not isinstance(evt, dict):
                continue

            evt_type = str(evt.get("type", "")).strip()
            if evt_type == "response.output_text.delta":
                delta = str(evt.get("delta", ""))
                if delta:
                    delta_parts.append(delta)
                continue

            if evt_type == "item.completed":
                item = evt.get("item")
                if not isinstance(item, dict):
                    continue
                if str(item.get("type", "")).strip() not in {"agent_message", "assistant_message", "message"}:
                    continue
                direct = str(item.get("text", "")).strip()
                if direct:
                    completed_parts.append(direct)
                    continue
                for block in item.get("content", []) if isinstance(item.get("content"), list) else []:
                    text = str((block or {}).get("text", "")).strip()
                    if text:
                        completed_parts.append(text)
                continue

            if evt_type == "response.completed":
                output_list = evt.get("response", {}).get("output", []) if isinstance(evt.get("response"), dict) else []
                if not isinstance(output_list, list):
                    continue
                for item in output_list:
                    if not isinstance(item, dict):
                        continue
                    content = item.get("content", [])
                    if not isinstance(content, list):
                        continue
                    for block in content:
                        text = str((block or {}).get("text", "")).strip()
                        if text:
                            completed_parts.append(text)

        if delta_parts:
            return "".join(delta_parts).strip()
        if completed_parts:
            return "\n".join(part for part in completed_parts if part).strip()
        return stdout_text.strip()

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
