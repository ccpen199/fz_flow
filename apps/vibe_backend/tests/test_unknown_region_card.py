"""字典里没有的地区 → **弹卡让用户选**，绝不把地区丢掉去取数。

用户 2026-10-19 的原话：如果没有「优衣库（韩国）」这个标准值，那就相当于意图不明，
需要弹卡片让用户选择。

这里锁住两件事：
1. 路由层把「优衣库 韩国」识别成 `unknown_region`，品牌退回可解析的裸名（不拼假值）；
2. 对话层据此产出 `resolution_card`，候选是**字典里真实存在的口径**，并且
   **一个 SQL 都不跑**（`_execute_goals` 被换成"一跑就失败"的桩）。

按项目硬约束：这里只打桩依赖（agent 规划、品牌解析），不伪造任何业务 rows/spec。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.services.conversation_service import ConversationService, conversation_service
from app.services.intent_router import intent_router
from app.store import ARTIFACTS, CONVERSATIONS, MESSAGES
from packages.shared_contracts.python_models import ConversationDTO, WorkingMemoryDTO

CONVERSATION_ID = "conv_test_unknown_region"
REAL_OPTIONS = ["优衣库（官网）", "优衣库（日本）", "优衣库（淘宝）", "优衣库（中国）"]


def _seed(brand: str = "优衣库") -> None:
    now = datetime.now(UTC)
    CONVERSATIONS[CONVERSATION_ID] = ConversationDTO(
        conversation_id=CONVERSATION_ID,
        title="未知地区用例",
        scene_id="scene_prd_price",
        scene_name="商品价格分析",
        working_memory=WorkingMemoryDTO(brand=brand),
        created_at=now,
        updated_at=now,
    )
    MESSAGES[CONVERSATION_ID] = []


@pytest.fixture()
def seeded() -> None:
    _seed()
    for artifact_id in [key for key in ARTIFACTS if key.startswith("art_unknown_")]:
        ARTIFACTS.pop(artifact_id, None)


def _resolution(term: str) -> dict[str, Any]:
    """品牌解析的桩：只给候选值，不碰数据库。"""
    return {
        "term": term,
        "needs_confirmation": True,
        "candidates": [{"value": value, "score": 1.0, "count": 0} for value in REAL_OPTIONS],
        "confirmed": [],
    }


def test_unknown_region_puts_a_card_with_real_options(seeded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ConversationService,
        "resolve_brand",
        lambda self, **kwargs: _resolution(kwargs.get("term") or ""),
    )

    card_calls: list[str] = []

    def _no_query(self, **kwargs):  # noqa: ANN001, ANN202
        card_calls.append("executed")
        raise AssertionError("字典里没有的地区不许直接取数")

    monkeypatch.setattr(ConversationService, "_execute_goals", _no_query)

    plan = conversation_service._ask_unknown_region(
        conversation_id=CONVERSATION_ID,
        scene=conversation_service.scene_for(CONVERSATION_ID) or _fallback_scene(),
        brand_term="优衣库",
        unknown_region="韩国",
        routing={"dimension_keys": ["price"], "time_window": "近一个月", "filters": []},
    )

    assert card_calls == []
    assert plan["artifact_refs"], "必须产出卡片"
    card = ARTIFACTS[plan["artifact_refs"][0]]
    assert card.kind == "resolution_card"
    assert card.status == "needs_confirmation"
    # 标题显示用户**真正说的**值，payload.term 留可解析的裸品牌。
    assert "优衣库（韩国）" in card.title
    assert card.payload.get("term") == "优衣库"
    assert [item["value"] for item in card.payload.get("options") or []] == REAL_OPTIONS
    # 必须说清"为什么问"，否则用户以为系统没听懂。
    assert "没有「优衣库（韩国）」" in plan["assistant_text"]


def test_unknown_region_without_any_option_asks_in_words(seeded: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """品牌字段里连候选都没有时不出空卡（点不了），但也不许去查库。"""

    monkeypatch.setattr(
        ConversationService,
        "resolve_brand",
        lambda self, **kwargs: {"term": kwargs.get("term"), "needs_confirmation": False, "candidates": []},
    )
    monkeypatch.setattr(
        ConversationService,
        "_execute_goals",
        lambda self, **kwargs: (_ for _ in ()).throw(AssertionError("不许取数")),
    )

    plan = conversation_service._ask_unknown_region(
        conversation_id=CONVERSATION_ID,
        scene=_fallback_scene(),
        brand_term="优衣库",
        unknown_region="韩国",
        routing={"dimension_keys": ["price"]},
    )

    assert plan["artifact_refs"] == []
    assert "没有「优衣库（韩国）」" in plan["assistant_text"]


def _fallback_scene():
    from app.services.scene_cache_service import scene_cache_service

    for scene_id in ("scene_prd_price", "scene_0004"):
        scene = scene_cache_service.get_scene(scene_id)
        if scene is not None:
            return scene
    raise AssertionError("没有可用的场景")


def test_settled_card_no_longer_says_it_needs_confirmation(
    seeded: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """点选之后标题要落地：不能还写着「优衣库（韩国）」需要确认。"""
    monkeypatch.setattr(
        ConversationService,
        "resolve_brand",
        lambda self, **kwargs: _resolution(kwargs.get("term") or ""),
    )
    monkeypatch.setattr(
        ConversationService,
        "_execute_goals",
        lambda self, **kwargs: {"assistant_text": "", "artifact_refs": [], "actions": []},
    )
    scene = _fallback_scene()
    plan = conversation_service._ask_unknown_region(
        conversation_id=CONVERSATION_ID,
        scene=scene,
        brand_term="优衣库",
        unknown_region="韩国",
        routing={"dimension_keys": ["price"]},
    )
    card = ARTIFACTS[plan["artifact_refs"][0]]

    conversation_service._apply_resolution(
        conversation_id=CONVERSATION_ID, scene=scene, card=card, value="优衣库（日本）"
    )

    assert card.status == "ready"
    assert "需要确认" not in card.title
    assert "优衣库（日本）" in card.title


def test_router_reports_the_unknown_region() -> None:
    plan = intent_router.route(message="优衣库 韩国 近一个月卖了多少件T恤", dimensions=[])
    assert plan["brand"] == "优衣库"
    assert plan["unknown_region"] == "韩国"
