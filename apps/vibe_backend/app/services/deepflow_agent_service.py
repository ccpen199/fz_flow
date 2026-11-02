"""DeepFlow（deer-flow）多轮对话 agent 的 HTTP 桥。

新链路（对话式画布）的多轮对话由 deer-flow 的 LangGraph agent 做**意图与槽位判定**：
本模块只负责「拼输入 → 调 agent → 解析严格 JSON 输出」，不取数、不造数、不猜意图。

设计约束（与项目硬约束一致）：
- **不兜底**：agent 不可用或判定不确定时返回 ``ok=False``（或 ``needs_clarification``），
  由对话层追问澄清，绝不替用户选一个方向。
- **数据真实性**：agent 只回意图、维度 key、时间窗、话术；任何数值仍来自真实 MySQL 取数。
- **可溯源**：每次调用的原始 JSON 一并回传（``raw``），便于排查误判。

环境变量（全部可选，带默认值；不影响老链路）：
- ``DEEPFLOW_AGENT_URL``     deer-flow LangGraph 服务地址，默认 ``http://127.0.0.1:2124``
- ``DEEPFLOW_ASSISTANT_ID``  图 id，默认 ``lead_agent``
- ``DEEPFLOW_AGENT_NAME``    自定义 agent 名（configurable.agent_name），默认 ``fz-canvas``
- ``DEEPFLOW_AGENT_MODEL``   **默认档**（gpt-5.2）对应的 deer-flow 模型条目名，默认 ``fz-gpt-5.2``
- ``DEEPFLOW_AGENT_TIMEOUT`` 单轮超时秒数，默认 ``120``

模型档位（2026-10-11 起）：
``configurable.model_name`` **不再在进程启动时定死**，而是每轮向
``integrations.llm_agent.model_registry`` 要一次当前档位对应的 deer-flow 模型条目名
（gpt-5.2 → ``fz-gpt-5.2``；deepseek → ``fz-deepseek``）。前端切一次档，
这里、配置中心推荐、SQL 结果生成三处同时生效。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
import uuid
from typing import Any

from integrations.llm_agent.model_registry import model_registry

from .canvas_action_service import canvas_action_service

DEFAULT_AGENT_URL = "http://127.0.0.1:2124"
DEFAULT_ASSISTANT_ID = "lead_agent"
DEFAULT_AGENT_NAME = "fz-canvas"
DEFAULT_AGENT_MODEL = "fz-gpt-5.2"
DEFAULT_TIMEOUT = 120.0

# 与 intent_router 的取值保持一致；agent 若回了别的词，按 unknown 处理。
VALID_INTENTS = {
    "chat",
    "explore",
    "data_query",
    # 「问数据本身」——哪个月份有数据 / 数据覆盖到什么时候。
    # 这类不是要指标，落不到任何 dimension_key 上（2026-10-19 用户："我是在问问题"）。
    "coverage",
    "report_build",
    "canvas_revision",
    # 「换个方向 / 先不选了」——只处置当前话题，不产生新需求。
    "control",
    # 「ZARA 和优衣库比一比」——多主体同口径对比（主体是槽位，不能只写文案）。
    "compare",
    # 「为什么这个月是 0 / 这结论怎么来的」——对画布上已有结果的追问与质询。
    "explain",
    "rollback",
    "unknown",
}

_THREAD_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]")
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

# 会话 → LangGraph thread 的命名空间（固定值，保证映射稳定）。
THREAD_NAMESPACE = uuid.UUID("6f1c5a52-2f1b-4d1e-9d5a-4c9b8f7a1c33")

# DeepFlow agent 跑在本机（默认 127.0.0.1:2124）。运行环境里 HTTP_PROXY /
# HTTPS_PROXY 是设着的（本机 curl 也需要 --noproxy），urllib 会把 localhost
# 也送去代理，所以这里显式用「不走代理」的 opener。
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class DeepflowAgentService:
    """Talks to deer-flow's multi-turn agent over the LangGraph HTTP API."""

    def __init__(self) -> None:
        self.base_url = os.getenv("DEEPFLOW_AGENT_URL", DEFAULT_AGENT_URL).rstrip("/")
        self.assistant_id = os.getenv("DEEPFLOW_ASSISTANT_ID", DEFAULT_ASSISTANT_ID).strip() or DEFAULT_ASSISTANT_ID
        self.agent_name = os.getenv("DEEPFLOW_AGENT_NAME", DEFAULT_AGENT_NAME).strip() or DEFAULT_AGENT_NAME
        try:
            self.timeout = float(os.getenv("DEEPFLOW_AGENT_TIMEOUT", str(DEFAULT_TIMEOUT)))
        except ValueError:
            self.timeout = DEFAULT_TIMEOUT

    @property
    def model_name(self) -> str:
        """本轮该用的 deer-flow 模型条目名 —— **每次读取都重新问注册表**。

        刻意不做成构造时快照：前端切档后，下一次对话就要走新模型。
        """
        try:
            return model_registry.resolve().agent_model_alias or DEFAULT_AGENT_MODEL
        except ValueError:
            # 注册表里出现未知档位（人为改坏了文件）时退回默认，不影响画布可用性。
            return DEFAULT_AGENT_MODEL

    # ------------------------------------------------------------------ config

    @property
    def thread_prefix(self) -> str:
        return "fz"

    def thread_id_for(self, conversation_id: str) -> str:
        """Derive a stable LangGraph thread id from our conversation id.

        LangGraph only accepts UUIDs as thread ids, so the mapping is a
        deterministic uuid5 — same conversation always maps to the same thread,
        with no extra bookkeeping to keep in sync (and it survives restarts
        because the checkpointer stores state by thread id).
        """
        safe = _THREAD_SAFE_RE.sub("-", str(conversation_id or "").strip())
        if not safe:
            return ""
        return str(uuid.uuid5(THREAD_NAMESPACE, f"{self.thread_prefix}-{safe}"))

    # ------------------------------------------------------------------ health

    def healthy(self) -> bool:
        try:
            with _NO_PROXY_OPENER.open(f"{self.base_url}/ok", timeout=3) as response:  # noqa: S310
                return response.status == 200
        except Exception:  # noqa: BLE001 - health probe never raises
            return False

    # ------------------------------------------------------------------- calls

    def plan_turn(
        self,
        *,
        conversation_id: str,
        text: str,
        scene_id: str = "",
        scene_name: str = "",
        brand: str = "",
        canvas_state: dict[str, Any] | None = None,
        last_turn: dict[str, Any] | None = None,
        dimension_options: list[dict[str, Any]] | None = None,
        filter_fields: list[dict[str, Any]] | None = None,
        config_catalog: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Ask the multi-turn agent to classify this turn.

        Returns ``{"ok": True, ...judgement...}`` or ``{"ok": False, "error": ...}``.
        Never raises: a dead agent must degrade to "ask the user", not to a guess.
        """
        thread_id = self.thread_id_for(conversation_id)
        if not thread_id:
            return {"ok": False, "error": "invalid_conversation_id"}

        payload = {
            "user_text": str(text or ""),
            "scene": {"scene_id": scene_id or "", "name": scene_name or ""},
            "brand_memory": brand or "",
            # 画布现状：一份**自描述**快照（画布上真实存在的产物 + 能否回退）。
            # 刻意不做成 has_charts / can_rollback / has_outline 这类"能力开关"——
            # 那种设计每加一种产物就要多一个字段、漏一个 agent 就只能猜。
            "canvas": canvas_state or {"artifacts": [], "rollback_available": False},
            # 上一轮系统**实际做了什么**。thread 里只有你自己的判定，
            # 没有执行结果，闭环补在这里。
            "last_turn": last_turn or {"intent": "", "system_result": ""},
            "dimension_options": list(dimension_options or []),
            # 可用于筛选的真实字段（semantic_name 必须原样使用）。每项还带 ``configs``，
            # 标明它属于哪几套配置——agent 选定本轮配置后，后端据此复核它写下的条件。
            "filterable_fields": list(filter_fields or []),
            # **配置目录**：配置中心里真实存在、且本轮可选的全部配置。
            # 给它是为了让 agent 能按问题判断"这一轮需要哪几套配置"（回 ``config_keys``），
            # 而不是由后端拿关键词去猜。目录内容全部来自配置中心，代码不硬编码业务词。
            "config_catalog": list(config_catalog or []),
            # 可用的**动作词表**：图表形态、页面操作、版式的合法取值。
            # 给词表不是让模型"匹配关键词"，而是让它把中文意图落到后端真正认得的
            # 规范值上——否则它只能瞎写一个字符串，后端还得猜。
            "action_vocabulary": {
                "chart_type": list(canvas_action_service.ALLOWED_CHART_TYPES),
                "chart_type_labels": dict(canvas_action_service.CHART_TYPE_LABELS),
                "page_ops": list(canvas_action_service.ALLOWED_PAGE_OPS),
                "page_layouts": list(canvas_action_service.ALLOWED_PAGE_LAYOUTS),
                "page_layout_labels": dict(canvas_action_service.PAGE_LAYOUT_LABELS),
                "filter_operators": [">", ">=", "<", "<=", "=", "between"],
            },
        }
        run_body = {
            "assistant_id": self.assistant_id,
            "input": {"messages": [{"type": "human", "content": json.dumps(payload, ensure_ascii=False)}]},
            "config": {
                "configurable": {
                    "agent_name": self.agent_name,
                    "model_name": self.model_name,
                    "thinking_enabled": False,
                    "subagent_enabled": False,
                }
            },
        }

        ensure = self._ensure_thread(thread_id)
        if ensure is not None:
            return {"ok": False, "error": ensure}

        body, error = self._post_json(f"/threads/{thread_id}/runs/wait", run_body)
        if error is not None:
            return {"ok": False, "error": error}

        content = self._last_ai_text(body)
        if not content:
            return {"ok": False, "error": "empty_reply"}
        judgement = self._parse_json(content)
        if not isinstance(judgement, dict):
            # 解析不出结构化判定：交给对话层追问，不猜。
            return {
                "ok": True,
                "intent": "unknown",
                "dimension_keys": [],
                "group_by": [],
                "config_keys": [],
                "time_window": "",
                "page_count": None,
                "report_topic": "",
                "chart_type": "",
                "filters": [],
                "remove_dimension_keys": [],
                "page_ops": [],
                "target": {},
                "coverage": {},
                "control": {},
                "compare": {},
                "explain": {},
                "reply": str(content).strip()[:400],
                "needs_clarification": True,
                "clarify_question": "我没太理解这一轮想做什么，可以再说具体一点吗？",
                "parse_failed": True,
                "raw": content,
            }

        intent = str(judgement.get("intent") or "").strip().lower()
        if intent not in VALID_INTENTS:
            intent = "unknown"

        return {
            "ok": True,
            "intent": intent,
            "dimension_keys": [str(key) for key in (judgement.get("dimension_keys") or []) if str(key).strip()],
            # 分组字段（"按颜色拆一下"）：字段**语义名**，原样回传；是否真实存在、是不是
            # 维度字段，由对话层用**本轮实际加载的配置**校验（这一层看不到配置）。
            "group_by": self._as_str_list(judgement.get("group_by")),
            # 本轮需要哪几套配置（配置目录里的 scene_id）。**空数组 = 本轮不需要配置**，
            # 是正常结果而不是失败。这里原样回传，白名单校验交给对话层
            # （turn_config_service.validate_keys），不在这一层做任何近义/模糊匹配。
            "config_keys": self._as_str_list(judgement.get("config_keys")),
            "time_window": str(judgement.get("time_window") or "").strip(),
            "page_count": self._as_page_count(judgement.get("page_count")),
            # 汇报主题：**用户原话里点名的那个主题**，原样回传。
            # 没有就空串——空的语义是"用户还没给主题"，后端会先问他，
            # 而不是自己编一个「品牌 + 场景复盘」当标题。
            "report_topic": str(judgement.get("report_topic") or "").strip(),
            # 结构化动作槽位：**原样回传**，一律由对话层用 canvas_action_service
            # 逐个校验（字段必须命中真实语义字段、页码必须真实存在…）。
            # 这里不做任何加工，避免"猜"在这里发生。
            "chart_type": str(judgement.get("chart_type") or "").strip(),
            "filters": self._as_dict_list(judgement.get("filters")),
            "remove_dimension_keys": self._as_str_list(judgement.get("remove_dimension_keys")),
            "page_ops": self._as_dict_list(judgement.get("page_ops")),
            "target": judgement.get("target") if isinstance(judgement.get("target"), dict) else {},
            # 「问数据本身」的槽位：`{"kind": "months", "brands": ["优衣库"]}`。
            # 原样回传——品牌原话要交给下游用真实字典解析，这里不许替它改成标准名。
            "coverage": self._as_slot(judgement.get("coverage")),
            # 「换个方向」的槽位：`{"action": "switch_direction"}`。
            "control": self._as_slot(judgement.get("control")),
            # 「对比」的槽位：`{"subjects": ["ZARA", "优衣库"]}`（原话，交给下游查字典）。
            "compare": self._as_slot(judgement.get("compare")),
            # 「解释/质询」的槽位：`{"about": "number|method|empty"}`。
            "explain": self._as_slot(judgement.get("explain")),
            "reply": str(judgement.get("reply") or "").strip(),
            "needs_clarification": bool(judgement.get("needs_clarification")),
            "clarify_question": str(judgement.get("clarify_question") or "").strip(),
            "raw": content,
        }

    # ------------------------------------------------------------------ internals

    def _ensure_thread(self, thread_id: str) -> str | None:
        body, error = self._post_json("/threads", {"thread_id": thread_id}, count_as_failure=False)
        if error is not None:
            return error
        del body
        return None

    def _post_json(
        self,
        path: str,
        body: dict[str, Any],
        *,
        count_as_failure: bool = True,
    ) -> tuple[Any, str | None]:
        """POST JSON with one retry.

        The upstream gateway occasionally answers 404/5xx on a healthy request;
        one retry removes that noise without hiding a genuinely dead service.
        """
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        last_error = "unknown_error"
        for attempt in range(2):
            request = urllib.request.Request(  # noqa: S310
                f"{self.base_url}{path}",
                data=data,
                headers={"Content-Type": "application/json", "Accept": "application/json"},
                method="POST",
            )
            try:
                with _NO_PROXY_OPENER.open(request, timeout=self.timeout) as response:  # noqa: S310
                    raw = response.read().decode("utf-8", errors="replace")
                return (json.loads(raw) if raw.strip() else {}), None
            except urllib.error.HTTPError as exc:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", errors="replace")[:200]
                except Exception:  # noqa: BLE001
                    detail = ""
                last_error = f"http_{exc.code}:{detail}" if count_as_failure else ""
                if exc.code == 409:
                    # Thread already exists — not a failure for our purposes.
                    return {}, None
                if exc.code < 500 and exc.code not in (404, 429) and count_as_failure:
                    return None, f"http_{exc.code}"
                if not count_as_failure:
                    return {}, None
            except Exception as exc:  # noqa: BLE001 - transport level
                last_error = f"{type(exc).__name__}:{exc}"
            if attempt == 0:
                continue
        return None, last_error or "unknown_error"

    @staticmethod
    def _last_ai_text(body: Any) -> str:
        if not isinstance(body, dict):
            return ""
        messages = body.get("messages")
        if not isinstance(messages, list):
            return ""
        for message in reversed(messages):
            if not isinstance(message, dict):
                continue
            if str(message.get("type") or message.get("role") or "").lower() not in ("ai", "assistant"):
                continue
            content = message.get("content")
            if isinstance(content, str):
                if content.strip():
                    return content
                continue
            if isinstance(content, list):
                texts = [
                    str(part.get("text") or "")
                    for part in content
                    if isinstance(part, dict) and part.get("type") in ("text", "output_text", None)
                ]
                joined = "".join(texts).strip()
                if joined:
                    return joined
        return ""

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any] | None:
        candidate = text.strip()
        fenced = _FENCE_RE.search(candidate)
        if fenced:
            candidate = fenced.group(1).strip()
        try:
            parsed = json.loads(candidate)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass
        decoder = json.JSONDecoder()
        for index, char in enumerate(candidate):
            if char != "{":
                continue
            try:
                parsed, _ = decoder.raw_decode(candidate[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
        return None

    @staticmethod
    def _as_page_count(value: Any) -> int | None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return min(12, max(2, number))

    @staticmethod
    def _as_dict_list(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, dict)]

    @staticmethod
    def _as_slot(value: Any) -> dict[str, Any]:
        """单个槽位对象（`coverage` / `control`）。

        不是 dict 就回空 dict —— 调用方据此判断"这一轮没声明这个槽位"，
        而不是拿到 None 再去 `.get()` 崩掉。
        """
        return dict(value) if isinstance(value, dict) else {}

    @staticmethod
    def _as_str_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item or "").strip()]


deepflow_agent_service = DeepflowAgentService()
