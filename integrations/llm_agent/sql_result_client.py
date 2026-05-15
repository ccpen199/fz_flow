from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .codex_cli_lock import codex_cli_lock


@dataclass
class SqlResultAgentConfig:
    provider: str = "codex"
    endpoint: str | None = None
    api_key: str | None = None
    model: str | None = None
    timeout_seconds: int = 90
    codex_bin: str = "codex"
    codex_model: str | None = None
    codex_home: str | None = None
    codex_home_fallbacks: tuple[str, ...] = ()
    codex_cwd: str | None = None
    codex_bypass_sandbox: bool = False


class SqlResultAgentClient:
    """LLM connector for SQL generation and optional plan metadata."""

    def __init__(self) -> None:
        self.config = self._build_config()

    def _build_config(self) -> SqlResultAgentConfig:
        fallback_homes_raw = (os.getenv("SQL_RESULT_AGENT_CODEX_HOME_FALLBACKS") or "").strip()
        fallback_homes = tuple(home.strip() for home in fallback_homes_raw.split(",") if home.strip())
        inferred_fallback_homes = self._discover_codex_home_fallbacks()
        if not fallback_homes and inferred_fallback_homes:
            fallback_homes = inferred_fallback_homes
        return SqlResultAgentConfig(
            provider=os.getenv("SQL_RESULT_AGENT_PROVIDER", "codex").strip().lower() or "codex",
            endpoint=(os.getenv("SQL_RESULT_AGENT_HTTP_ENDPOINT") or "").strip() or None,
            api_key=(os.getenv("SQL_RESULT_AGENT_API_KEY") or "").strip() or None,
            model=(os.getenv("SQL_RESULT_AGENT_HTTP_MODEL") or "").strip() or None,
            timeout_seconds=int(os.getenv("SQL_RESULT_AGENT_HTTP_TIMEOUT", "90")),
            codex_bin=(os.getenv("SQL_RESULT_AGENT_CODEX_BIN") or "codex").strip() or "codex",
            codex_model=(os.getenv("SQL_RESULT_AGENT_CODEX_MODEL") or "").strip() or None,
            codex_home=(os.getenv("SQL_RESULT_AGENT_CODEX_HOME") or "").strip() or None,
            codex_home_fallbacks=fallback_homes,
            codex_cwd=(os.getenv("SQL_RESULT_AGENT_CODEX_CWD") or "").strip() or None,
            codex_bypass_sandbox=(os.getenv("SQL_RESULT_AGENT_CODEX_BYPASS_SANDBOX", "0").strip() == "1"),
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
        return {
            "status": "ok",
            "service": "sql-result-agent",
            "provider": self.config.provider,
            "ready": self.config.provider in {"codex", "codex_cli", "http", "modelscope"},
        }

    def generate_plan(self, payload: dict) -> dict:
        provider = self.config.provider
        if provider in {"http", "modelscope"}:
            if not self.config.endpoint:
                raise RuntimeError(f"SQL_RESULT_AGENT_HTTP_ENDPOINT is required when provider={provider}")
            return self._via_http(payload, provider=provider)
        if provider in {"codex", "codex_cli"}:
            return self._via_codex(payload)
        raise RuntimeError(
            f"unsupported SQL_RESULT_AGENT_PROVIDER={provider}; allowed: codex/codex_cli/http/modelscope"
        )

    def _system_prompt(self) -> str:
        return (
            "你是生产级 SQL 生成与执行助手，只能生成真实可执行的 MySQL 8.0 查询。"
            "请基于 INPUT.scene 语义字段、relations、scene_playbook、selected_preset、generation_rules、intent 和 agent_prompt 生成 SQL。"
            "必须优先遵守 INPUT.generation_rules；如果 INPUT.selected_preset 非空，必须优先遵守它的 field_requirements、derived_metrics、group_by、sort、limit、notes。"
            "SQL 必须是单条只读语句（SELECT 或 WITH + SELECT），禁止写操作。"
            "只能使用 INPUT.semantic_fields 中已有的 table_name.field_name 和 INPUT.relations 中声明的关联，不能臆造字段、表、平台、销量或尺码结构。"
            "禁止任何未绑定占位符或伪参数，包括 :subcategory、:brand、?、${value}、<value>、待确认、待补充；没有具体过滤值时不要写等值过滤。"
            "如果 intent 出现“指定二级类目/指定品牌/某类目/某品牌”，但 INPUT.context 没有 provided_filters 或明确字段值，禁止写 WHERE 字段=:参数；必须按该字段分组，或返回空 sql 并在 risk_notes 说明缺少具体参数。"
            "时间窗口必须使用 MySQL 写法，例如 DATE_SUB(anchor_date, INTERVAL 30 DAY)，禁止 INTERVAL '30 day'。"
            "涉及最近/近30天/近期时，必须以数据最大日期为锚点，不得直接用系统 CURRENT_DATE/CURDATE()/NOW() 作为业务数据窗口；请用 CTE 或子查询取 MAX(DATE(ReceiveTime/CreateTime)) 作为 anchor_date。"
            "如果 selected_preset.notes 提到“没有具体二级类目参数时按二级类目分组”，SQL 必须 GROUP BY 二级类目，而不是生成参数占位符。"
            "如果 selected_preset.notes 要求返回商品级明细或候选清单，禁止使用 COUNT、GROUP BY 或聚合指标；每行直接返回一个商品和命中线索。"
            "metrics/dimensions 必须是 semantic_name 字符串数组，filters.field 必须是 semantic_name 字符串，禁止返回对象或字符串化 dict。"
            "返回的 sql 必须可以直接提交给 PyMySQL 执行，不能要求后端再替换变量、补参数、补表名或补字段。"
            "仅返回 JSON，不要 markdown，不要解释。"
            '格式必须为：{"sql":"","sql_explanation":"","plan":{"intent":"","metrics":[],"dimensions":[],"filters":[],"time_window":"","chart_candidates":[],"risk_notes":[]},"notes":[]}。'
            "如果字段或关系不足以真实回答问题，不要编造 SQL；返回空 sql，并在 risk_notes 写清缺失项。"
        )

    def _via_http(self, payload: dict, *, provider: str = "http") -> dict:
        request_payload = {"system_prompt": self._system_prompt(), **payload}
        if provider == "modelscope":
            request_payload = self._build_modelscope_payload(payload)

        body = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"

        from urllib import error, request

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

        if provider == "modelscope":
            return self._normalize_modelscope_response(parsed, raw_text)

        plan = parsed.get("plan", parsed if isinstance(parsed, dict) else {})
        return {
            "provider": "http",
            "mode": "remote",
            "sql": str(parsed.get("sql") or "").strip() if isinstance(parsed, dict) else "",
            "sql_explanation": str(parsed.get("sql_explanation") or "").strip() if isinstance(parsed, dict) else "",
            "plan": plan if isinstance(plan, dict) else {},
            "notes": (parsed.get("notes", []) if isinstance(parsed, dict) and isinstance(parsed.get("notes"), list) else []),
            "raw": raw_text,
        }

    def _build_modelscope_payload(self, payload: dict) -> dict:
        model = self.config.model or "Qwen/Qwen3-235B-A22B-Instruct-2507"
        prompt = self._system_prompt()
        return {
            "model": model,
            "messages": [
                {"role": "system", "content": prompt},
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
        plan = inner.get("plan", inner if isinstance(inner, dict) else {})
        return {
            "provider": "modelscope",
            "mode": "remote",
            "sql": str(inner.get("sql") or "").strip(),
            "sql_explanation": str(inner.get("sql_explanation") or "").strip(),
            "plan": plan if isinstance(plan, dict) else {},
            "notes": inner.get("notes", []) if isinstance(inner.get("notes"), list) else [],
            "raw": content or raw_text,
        }

    def _via_codex(self, payload: dict) -> dict:
        prompt_payload = json.dumps(payload, ensure_ascii=False)
        prompt = f"{self._system_prompt()} INPUT={prompt_payload}"

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
            env = os.environ.copy()
            if home:
                env["HOME"] = home
            try:
                with codex_cli_lock():
                    trial_proc = subprocess.run(
                        [self.config.codex_bin, *args],
                        capture_output=True,
                        text=True,
                        timeout=self.config.timeout_seconds,
                        check=False,
                        cwd=self.config.codex_cwd or None,
                        env=env,
                    )
            except FileNotFoundError as exc:
                raise RuntimeError(f"codex cli not found: {self.config.codex_bin}") from exc
            except subprocess.TimeoutExpired:
                attempt_errors.append(f"home={home or '<default>'}: timeout after {self.config.timeout_seconds}s")
                continue
            except Exception as exc:  # noqa: BLE001
                attempt_errors.append(f"home={home or '<default>'}: execution failed: {exc}")
                continue

            if trial_proc.returncode == 0:
                proc = trial_proc
                selected_home = home
                break

            stderr_text = (trial_proc.stderr or "").strip()
            stdout_tail = (trial_proc.stdout or "").strip()[-300:]
            attempt_errors.append(
                f"home={home or '<default>'}: exit={trial_proc.returncode}, error={stderr_text or stdout_tail or 'unknown error'}"
            )

        if proc is None:
            return {
                "provider": "codex_cli",
                "mode": "cli",
                "sql": "",
                "sql_explanation": "",
                "plan": {},
                "notes": [f"codex cli failed on all configured homes: {' | '.join(attempt_errors)}"],
                "raw": " | ".join(attempt_errors),
            }

        raw_text = self._extract_codex_text(proc.stdout or "")
        parsed = self._parse_json_loose(raw_text)
        if not isinstance(parsed, dict):
            parsed = {}
        plan = parsed.get("plan", parsed if isinstance(parsed, dict) else {})
        return {
            "provider": "codex_cli",
            "mode": "cli",
            "sql": str(parsed.get("sql") or "").strip(),
            "sql_explanation": str(parsed.get("sql_explanation") or "").strip(),
            "plan": plan if isinstance(plan, dict) else {},
            "notes": (
                (parsed.get("notes", []) if isinstance(parsed.get("notes"), list) else [])
                + ([f"codex home fallback activated; succeeded with home={selected_home or '<default>'}"] if attempt_errors else [])
                + ([f"codex home attempt failed: {msg}" for msg in attempt_errors] if attempt_errors else [])
            ),
            "raw": raw_text,
        }

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

    def _extract_codex_text(self, stdout_text: str) -> str:
        lines = [line.strip() for line in stdout_text.splitlines() if line.strip()]
        parts: list[str] = []
        for line in lines:
            try:
                evt = json.loads(line)
            except Exception:
                continue
            if not isinstance(evt, dict):
                continue
            if str(evt.get("type", "")).strip() == "response.output_text.delta":
                delta = str(evt.get("delta", "")).strip()
                if delta:
                    parts.append(delta)
            if str(evt.get("type", "")).strip() == "response.completed":
                output = evt.get("response", {}).get("output", []) if isinstance(evt.get("response"), dict) else []
                for item in output if isinstance(output, list) else []:
                    if not isinstance(item, dict):
                        continue
                    content = item.get("content", [])
                    for block in content if isinstance(content, list) else []:
                        text = str((block or {}).get("text", "")).strip()
                        if text:
                            parts.append(text)
            if str(evt.get("type", "")).strip() == "item.completed":
                item = evt.get("item", {})
                if not isinstance(item, dict):
                    continue
                if str(item.get("type", "")).strip() in {"agent_message", "assistant_message"}:
                    text = str(item.get("text", "")).strip()
                    if text:
                        parts.append(text)
        if parts:
            return "".join(parts).strip()
        return stdout_text.strip()
