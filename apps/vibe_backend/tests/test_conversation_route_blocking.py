"""守卫「一轮对话不许把整个后端冻住」。

背景（实测）：路由处理函数原来是 `async def`，函数体里直接调同步的
`conversation_service.handle_message`。一轮要等 DeepFlow agent（LLM），
网关缓慢/不可达时**实测占用 227 秒**，这期间事件循环被占死：
`curl http://127.0.0.1:18900/health` **超时（000）而不是报错**，
肉眼看起来就是"后端挂了"（老配置页的接口也一起卡住）。

修法：处理函数写成同步 `def`（Starlette 自动丢进线程池）；并发进来时的一致性
由 `ConversationService._turn_lock` 保证。本文件把这两条都钉住。
"""

from __future__ import annotations

import inspect
import re
import threading
import time
from pathlib import Path

import pytest

from app.routers import conversation as conversation_router
from app.services import conversation_service as service_module
from app.services.conversation_service import ConversationService

ROUTER_SOURCE = Path(inspect.getsourcefile(conversation_router) or "").read_text(encoding="utf-8")

# 这些接口都可能等 LLM 或跑 SQL，是"长阻塞"的代表。
LONG_RUNNING_HANDLERS = (
    "send_message",
    "apply_artifact_action",
    "build_outline",
    "update_outline_page",
    "regenerate_outline_page",
    "fill_pending_pages",
    "compile_outline",
    "execute_chart_sql",
)


def test_no_async_handlers_in_conversation_router() -> None:
    """处理函数不许回到 `async def`——那等于把整个后端交给一次 LLM 调用的时长。"""
    assert not re.search(r"(?m)^async def ", ROUTER_SOURCE)
    assert not re.search(r"(?m)^\s+await ", ROUTER_SOURCE)


@pytest.mark.parametrize("name", LONG_RUNNING_HANDLERS)
def test_long_handlers_are_plain_functions(name: str) -> None:
    """FastAPI 正是按「处理函数是不是协程」来决定走线程池还是事件循环。"""
    handler = getattr(conversation_router, name)
    assert not inspect.iscoroutinefunction(handler), f"{name} 又变成协程了，会把事件循环占死"


def test_turn_lock_exists_and_is_reentrant() -> None:
    lock = ConversationService._turn_lock
    assert isinstance(lock, type(threading.RLock()))
    # 同一线程可重入：`_handle_turn` 内部若再调回入口不会自锁死。
    assert lock.acquire(blocking=False) is True
    lock.release()


def test_public_entry_delegates_to_locked_implementation() -> None:
    source = Path(inspect.getsourcefile(ConversationService) or "").read_text(encoding="utf-8")
    entry = source.split("def handle_message(", 1)[1].split("def _handle_turn(", 1)[0]
    assert "with self._turn_lock:" in entry
    assert "_handle_turn(" in entry


def test_concurrent_turns_are_serialised(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """并发打进来的两轮不能交错执行——一轮里要成串地读写 working_memory 与产物。

    把 LLM 这一**依赖**打桩掉（不伪造业务数据），只让它"慢一点"，
    然后数一数同一时刻有几个调用在里面。
    """
    monkeypatch.setattr(service_module, "persist_state", lambda: None)

    from app.services.deepflow_agent_service import deepflow_agent_service

    inside = 0
    max_inside = 0
    guard = threading.Lock()

    def fake_plan_turn(**_kwargs):
        nonlocal inside, max_inside
        with guard:
            inside += 1
            max_inside = max(max_inside, inside)
        time.sleep(0.15)
        with guard:
            inside -= 1
        return {"ok": True, "intent": "chat", "config_keys": [], "reply": "好", "dimension_keys": []}

    monkeypatch.setattr(deepflow_agent_service, "plan_turn", fake_plan_turn)

    service = service_module.conversation_service
    conversation = service.create(title="并发串行化验证")

    def one_turn(index: int) -> None:
        service.handle_message(conversation_id=conversation.conversation_id, text=f"你好{index}")

    threads = [threading.Thread(target=one_turn, args=(i,)) for i in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    capsys.readouterr()
    assert max_inside == 1, f"同一时刻有 {max_inside} 轮在跑，锁没起作用"
