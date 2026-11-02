"""测试全局兜底：**任何测试都不许把状态写进真实的持久化存储。**

背景（2026-10-11 真实踩到）：`test_checkpoint_idempotency.py` 会调用
`conversation_service.apply_artifact_action()`，它内部走 `store.persist_state()`。
而 `store.MYSQL_STATE_ENABLED` **默认就是 True**（`VIBE_MYSQL_PERSIST_STATE != "0"`），
于是跑一次测试就把测试造的会话与卡片写进了 MySQL 工作流状态表；后端下次启动从那里
载入，脏数据就出现在用户的会话下拉框里。

实测症状：用户被一张 **`chart_card` 形态、却没有数据** 的「品牌『THE NORTH FACE』需要确认」
卡挡住（`art_idem_resolution_card`）。它本该是带候选按钮的 `resolution_card`——
是测试 `test_chart_requery_is_not_blocked_by_the_gate` 把 `card.kind` 改成 `chart_card`
之后落盘的结果；紧接着用户的会话列表里就多出一条标题为「幂等用例」的会话，
消息里还留着测试桩的原文「（桩：未真实取数）」。

这里做**全局兜底**：不管谁在哪个测试里忘了打桩，都写不出去。
开关判定放在 `store` 的模块级变量上（`persist_state()` 调用时读全局），
所以即便某些模块用 `from ..store import persist_state` 直接引用，也同样被挡住。
"""

from __future__ import annotations

import pytest

from app import store


@pytest.fixture(autouse=True)
def _forbid_state_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    """关掉两条落盘通道：本地状态文件 + MySQL 工作流状态表。"""
    monkeypatch.setattr(store, "FILE_STATE_ENABLED", False)
    monkeypatch.setattr(store, "MYSQL_STATE_ENABLED", False)
