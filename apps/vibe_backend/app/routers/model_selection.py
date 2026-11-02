"""模型档位切换 API（前端唯一配置入口）。

命名空间：``/api/v1/model-selection`` —— 与老链路（
``/api/v1/sql-result-agent``、``/api/v1/analysis/sessions``、``/api/v1/scenes/*``）
和新链路（``/api/v1/conversations/*``）都**零交集**，属于新增的独立面。

设计要点
--------
- ``GET`` 回当前档 + 可选档（**不含密钥**，只回 ``key_present`` 布尔）。
- ``PUT`` 切档并落盘；非法档位 **400**，绝不静默退回默认（避免"以为切了其实没切"）。
- ``POST /probe`` **真发一次模型请求**（不是假健康检查）：真报 HTTP 码、真报延迟、
  真报上游错误原文。失败就是 ``ok=false`` + 真实原因，不做任何粉饰。
- 处理函数一律写同步 ``def``：probe 最长等 30 秒，写成 ``async def`` 会把事件循环
  占死，整个后端（含 ``/health``）一起卡住——这条在 ``conversation.py`` 上踩过。
"""

from __future__ import annotations

import json
import time
from urllib import error, request

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from integrations.llm_agent.model_registry import ResolvedModel, model_registry

router = APIRouter(prefix="/api/v1/model-selection", tags=["model-selection"])

# 探针最长等 30 秒：只是为了确认"这个档能不能通"，不是跑业务。
PROBE_TIMEOUT_SECONDS = 30.0
PROBE_PROMPT = "只回复两个字：收到"


class UpdateModelSelectionRequest(BaseModel):
    model_id: str


class ProbeModelSelectionRequest(BaseModel):
    model_id: str | None = None


def _resolve_or_400(model_id: str | None) -> ResolvedModel:
    try:
        return model_registry.resolve(model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("")
def get_model_selection() -> dict:
    """当前档 + 可选档（给前端渲染下拉）。"""
    return model_registry.snapshot()


@router.put("")
def update_model_selection(body: UpdateModelSelectionRequest) -> dict:
    """切换档位。切换立即对**下一次**调用生效（三处消费方都在调用时读注册表）。"""
    try:
        model_registry.set_current(body.model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return model_registry.snapshot()


@router.post("/probe")
def probe_model_selection(body: ProbeModelSelectionRequest | None = None) -> dict:
    """真发一次最小请求，如实回报结果。

    不传 ``model_id`` 就探当前档。任何失败都原样回传（HTTP 码 / 上游错误串 /
    超时），**不**编一个"看起来正常"的结果。
    """
    resolved = _resolve_or_400(body.model_id if body else None)

    if not resolved.endpoint:
        return {
            "ok": False,
            "model_id": resolved.id,
            "model": resolved.model,
            "endpoint": "",
            "detail": "该档位没有配置 endpoint（检查部署 env）",
        }
    if not resolved.api_key:
        return {
            "ok": False,
            "model_id": resolved.id,
            "model": resolved.model,
            "endpoint": resolved.endpoint,
            "detail": "该档位没有配置 API key（检查部署 env）",
        }

    payload = {
        "model": resolved.model,
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "stream": False,
    }
    body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = request.Request(  # noqa: S310 - endpoint 来自本地配置，不是用户输入
        resolved.endpoint,
        data=body_bytes,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Bearer {resolved.api_key}",
        },
        method="POST",
    )

    started = time.monotonic()
    try:
        with request.urlopen(req, timeout=PROBE_TIMEOUT_SECONDS) as response:  # noqa: S310
            raw = response.read().decode("utf-8", errors="replace")
            status = response.status
    except error.HTTPError as exc:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        except Exception:  # noqa: BLE001
            detail = ""
        return {
            "ok": False,
            "model_id": resolved.id,
            "model": resolved.model,
            "endpoint": resolved.endpoint,
            "http_status": exc.code,
            "latency_ms": elapsed_ms,
            "detail": f"上游返回 HTTP {exc.code}：{detail}" if detail else f"上游返回 HTTP {exc.code}",
        }
    except Exception as exc:  # noqa: BLE001 - 探针本身不许抛，要把真实原因交出去
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return {
            "ok": False,
            "model_id": resolved.id,
            "model": resolved.model,
            "endpoint": resolved.endpoint,
            "latency_ms": elapsed_ms,
            "detail": f"{type(exc).__name__}: {exc}",
        }

    elapsed_ms = int((time.monotonic() - started) * 1000)
    sample = ""
    try:
        parsed = json.loads(raw) if raw.strip() else {}
        choices = parsed.get("choices") or [{}]
        message = (choices[0] or {}).get("message") or {}
        sample = str(message.get("content") or "").strip()
    except Exception:  # noqa: BLE001 - 能 200 就已经证明通了，解析失败不影响结论
        sample = ""

    return {
        "ok": status == 200,
        "model_id": resolved.id,
        "model": resolved.model,
        "endpoint": resolved.endpoint,
        "http_status": status,
        "latency_ms": elapsed_ms,
        "sample": sample[:120],
        "detail": "调用成功" if status == 200 else f"上游返回 HTTP {status}",
    }
