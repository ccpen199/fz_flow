"""运行期「模型档位」注册表 —— 前端可切换，消费方在**调用时**读取。

背景
----
本项目有三处调用大模型：
  1. 配置中心推荐（``integrations/llm_agent/client.py``）
  2. SQL 结果生成（``integrations/llm_agent/sql_result_client.py``）
  3. 新画布多轮对话的意图判定（``apps/vibe_backend/app/services/deepflow_agent_service.py``
     → deer-flow LangGraph agent @2124）
改造前这三处都在**进程启动时**从环境变量读死一个模型。本模块把它改成
「**每轮调用时**问一次当前档位」，于是前端改一次、三处同时生效。

硬约束（与项目既有约束一致）
--------------------------
- **默认档 ``gpt-5.2`` 的解析结果必须与改造前逐字一致**（同一条 env 链、同样的
  优先级），这样「不切档」时老链路的行为、错误文案、超时全部零变化。
- **密钥只从环境变量读**：不落盘、不进日志、不出现在 ``catalog()`` 的返回值里。
- **不兜底**：档位不存在就报错，不静默退回默认档（避免"以为切了其实没切"）。
- 这里只管"用哪个模型"，不碰任何取数 / SQL / 画布业务逻辑。

参考实现
--------
DeepSeek 的调用形态直接借鉴 ``~/Desktop/Phase2AgentTasks/web_app/main.py``
（``_call_deepseek``）：OpenAI 兼容的 ``POST {base}/chat/completions`` +
``Authorization: Bearer <key>``，模型取 DeepSeek 官方 ``/v1/models`` 实际返回的 id。
密钥取自既有项目 ``TrainPPTAgent`` 的 ``DEEPSEEK_API_KEY``（同一把 key）。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path

# ------------------------------------------------------------------ constants

GPT_PROFILE_ID = "gpt-5.2"
DEEPSEEK_PROFILE_ID = "deepseek"
DEFAULT_PROFILE_ID = GPT_PROFILE_ID
PROFILE_IDS = (GPT_PROFILE_ID, DEEPSEEK_PROFILE_ID)

# gpt-5.2：DeerFlow 里那份内网网关模型条目名（config.yaml 的 models[].name）。
DEFAULT_GPT_AGENT_MODEL = "fz-gpt-5.2"

# deepseek：官方 API（与 phase 项目一致，走 OpenAI 兼容的 /chat/completions）。
# 已实测 https://api.deepseek.com/v1/models 返回 deepseek-flash / deepseek-v4-pro，
# 这里默认用更快更省的 flash（DeepSeek-V4.1-Flash）。
DEFAULT_DEEPSEEK_ENDPOINT = "https://api.deepseek.com/v1/chat/completions"
DEFAULT_DEEPSEEK_MODEL = "deepseek-flash"
# deer-flow config.yaml 里给 deepseek 加的模型条目名。
DEFAULT_DEEPSEEK_AGENT_MODEL = "fz-deepseek"

# 档位切换的落盘位置（与既有后端状态文件分开，避免动到 store.py 的 state.json）。
_STATE_RELPATH = ("runtime_data", "model_registry.json")
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(*names: str, default: str = "") -> str:
    """按顺序取第一个非空环境变量。调用时求值（不是 import 时）。"""
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def _env_int(names: tuple[str, ...], default: int) -> int:
    raw = _env(*names, default=str(default))
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class ResolvedModel:
    """一次调用真正要用的模型参数（含密钥，**不要**回传给前端）。"""

    id: str
    label: str
    provider: str
    endpoint: str
    model: str
    api_key: str
    agent_model_alias: str
    timeout_seconds: int = 90

    @property
    def available(self) -> bool:
        return bool(self.endpoint) and bool(self.api_key)


class ModelRegistry:
    """两档模型的运行期开关。线程安全，读多写少。"""

    def __init__(self, state_path: Path | None = None) -> None:
        self._lock = threading.Lock()
        if state_path is not None:
            self._state_path = state_path
        else:
            override = _env("MODEL_SELECTION_STATE_PATH")
            self._state_path = Path(override) if override else _REPO_ROOT.joinpath(*_STATE_RELPATH)
        self._current_id = self._read_state() or DEFAULT_PROFILE_ID

    # ------------------------------------------------------------- 状态持久化

    def _read_state(self) -> str:
        try:
            raw = self._state_path.read_text(encoding="utf-8")
        except OSError:
            return ""
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return ""
        candidate = str((payload or {}).get("current") or "").strip()
        return candidate if candidate in PROFILE_IDS else ""

    def _write_state(self, profile_id: str) -> None:
        """写入当前档位。写失败不让业务崩——下次调用仍按内存里的档位走。"""
        parent = self._state_path.parent
        if not parent.exists():
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                return
        tmp = self._state_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps({"current": profile_id}, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._state_path)
        except OSError:
            return

    # ----------------------------------------------------------------- 公开面

    def current_id(self) -> str:
        with self._lock:
            return self._current_id

    def set_current(self, profile_id: str) -> str:
        """切换档位。非法档位**报错**，不静默退回默认档。"""
        candidate = str(profile_id or "").strip()
        if candidate not in PROFILE_IDS:
            raise ValueError(
                f"unknown model profile: {candidate!r}; supported: {', '.join(PROFILE_IDS)}"
            )
        with self._lock:
            self._current_id = candidate
        self._write_state(candidate)
        return candidate

    def resolve(self, profile_id: str | None = None) -> ResolvedModel:
        """解析某一档的调用参数；不传则用当前档。"""
        target = str(profile_id or "").strip() or self.current_id()
        if target == DEEPSEEK_PROFILE_ID:
            label = "DeepSeek-V4.1-Flash（DeepSeek 官方 API）"
            return ResolvedModel(
                id=DEEPSEEK_PROFILE_ID,
                label=label,
                provider="deepseek",
                endpoint=_env("DEEPSEEK_HTTP_ENDPOINT", default=DEFAULT_DEEPSEEK_ENDPOINT),
                model=_env("DEEPSEEK_HTTP_MODEL", default=DEFAULT_DEEPSEEK_MODEL),
                api_key=_env("DEEPSEEK_API_KEY"),
                agent_model_alias=_env("DEEPSEEK_AGENT_MODEL", default=DEFAULT_DEEPSEEK_AGENT_MODEL),
                timeout_seconds=_env_int(
                    ("DEEPSEEK_HTTP_TIMEOUT", "LLM_AGENT_HTTP_TIMEOUT", "SQL_RESULT_AGENT_HTTP_TIMEOUT"),
                    90,
                ),
            )
        if target == GPT_PROFILE_ID:
            # ⚠️ 这条链必须与改造前 client.py / sql_result_client.py 的取值**完全一致**，
            # 否则"没切档"的老链路会静默变行为。
            return ResolvedModel(
                id=GPT_PROFILE_ID,
                label="GPT-5.2（fz 内网网关）",
                provider=_env("LLM_AGENT_PROVIDER", "SQL_RESULT_AGENT_PROVIDER", default="http").lower(),
                endpoint=_env("LLM_AGENT_HTTP_ENDPOINT", "SQL_RESULT_AGENT_HTTP_ENDPOINT"),
                model=_env("LLM_AGENT_HTTP_MODEL", "SQL_RESULT_AGENT_HTTP_MODEL", default="gpt-5.2"),
                api_key=_env("LLM_AGENT_API_KEY", "SQL_RESULT_AGENT_API_KEY"),
                agent_model_alias=_env("DEEPFLOW_AGENT_MODEL", default=DEFAULT_GPT_AGENT_MODEL),
                timeout_seconds=_env_int(
                    ("LLM_AGENT_HTTP_TIMEOUT", "SQL_RESULT_AGENT_HTTP_TIMEOUT"), 90
                ),
            )
        raise ValueError(f"unknown model profile: {target!r}")

    def catalog(self) -> list[dict]:
        """给前端的可选档位清单（**不含密钥**）。"""
        options: list[dict] = []
        for profile_id in PROFILE_IDS:
            try:
                resolved = self.resolve(profile_id)
            except ValueError:
                continue
            options.append(
                {
                    "id": resolved.id,
                    "label": resolved.label,
                    "provider": resolved.provider,
                    "model": resolved.model,
                    "endpoint": resolved.endpoint,
                    "available": resolved.available,
                    "key_present": bool(resolved.api_key),
                }
            )
        return options

    def snapshot(self) -> dict:
        """当前档 + 可选档，供 ``GET /api/v1/model-selection`` 直接回给前端。"""
        return {"current": self.current_id(), "options": self.catalog()}


model_registry = ModelRegistry()
