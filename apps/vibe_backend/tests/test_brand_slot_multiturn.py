"""品牌槽位的多轮语义：**以最新说法为准**，且只换地区时沿用上下文品牌。

真实报障（2026-10-18，会话 `conv_8d11403823`）：

1. 第一轮问「优衣库」→ 弹 4 选 1 → 用户选了「优衣库（官网）」；
2. 再说「优衣库 日本 …T恤」——「日本」被当成普通修饰词丢掉，品牌还是裸「优衣库」；
3. 钉住值按词面 `优衣库` 直接命中 → **拿官网的数据回答日本的问题**；
4. 就算解析出了日本，旧实现里 `_merge_confirmations` 会把官网那条一起留着，
   `_canonicalize_filters` 按 (table, field) 建字典、后写覆盖先写 → 旧值又把新值盖回去。

这里锁住三条：
* 裸品牌沿用上轮选定的地区（上下文），但本轮自己点名地区时**绝不沿用**旧值；
* 一个字段只有一个确认值，新的覆盖旧的；
* 解析成功后把结果写回会话，下一轮裸品牌才有上下文可用。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.services.conversation_service import ConversationService
from app.services.intent_router import intent_router
from packages.shared_contracts.python_models import (
    ConversationDTO,
    FieldRole,
    SceneDTO,
    SceneFieldDTO,
    WorkingMemoryDTO,
)

BRAND = ("dict_brand_info", "Name")


def _scene() -> SceneDTO:
    return SceneDTO(
        scene_id="scene_test",
        name="测试场景",
        description="",
        fields=[
            SceneFieldDTO(
                field_id="dict_brand_info.Name",
                table_name="dict_brand_info",
                field_name="Name",
                semantic_name="品牌",
                description="",
                role=FieldRole.DIMENSION,
            )
        ],
    )


def _conversation(pins: list[dict[str, Any]] | None = None) -> ConversationDTO:
    now = datetime.now(UTC)
    return ConversationDTO(
        conversation_id="conv_test_brand_slot",
        title="品牌槽位用例",
        scene_id="scene_test",
        scene_name="测试场景",
        working_memory=WorkingMemoryDTO(
            brand="优衣库",
            confirmed_field_values=list(pins or []),
        ),
        created_at=now,
        updated_at=now,
    )


def _pin(term: str, canonical: str) -> dict[str, Any]:
    return {
        "term": term,
        "semantic_name": "品牌",
        "table_name": BRAND[0],
        "field_name": BRAND[1],
        "canonical_value": canonical,
        "score": None,
        "strategy": "user_confirmed",
    }


def _brand_term(message: str, memory_brand: str = "优衣库") -> str:
    return intent_router.route(message=message, dimensions=[], memory_brand=memory_brand)["brand"]


# ---------------------------------------------------------------- 钉子匹配


def test_bare_brand_reuses_the_region_chosen_last_turn() -> None:
    """上一轮选了官网，这一轮只说「优衣库」——接着上轮说，不该再问一次。"""
    entry = ConversationService._pinned_field_entry(
        [_pin("优衣库", "优衣库（官网）")],
        term="优衣库",
        field=_scene().fields[0],
    )

    assert entry is not None
    assert entry["canonical_value"] == "优衣库（官网）"


def test_explicit_new_region_is_never_overridden_by_the_old_pin() -> None:
    """这一轮点名了日本 → 旧官网钉子不许生效，必须重新解析。"""
    entry = ConversationService._pinned_field_entry(
        [_pin("优衣库", "优衣库（官网）")],
        term=_brand_term("优衣库 日本 品牌最近一个月上了多少件T恤"),
        field=_scene().fields[0],
    )

    assert entry is None


def test_different_brand_does_not_reuse_the_pin() -> None:
    entry = ConversationService._pinned_field_entry(
        [_pin("优衣库", "优衣库（官网）")],
        term="ZARA",
        field=_scene().fields[0],
    )

    assert entry is None


# ---------------------------------------------------------------- 槽位唯一


def test_one_value_per_field_and_the_latest_wins() -> None:
    stale = _pin("优衣库", "优衣库（官网）")
    fresh = _pin("优衣库", "优衣库（日本）")

    merged = ConversationService._merge_confirmations([stale], [fresh])

    assert [item["canonical_value"] for item in merged] == ["优衣库（日本）"]


def test_different_fields_are_kept_side_by_side() -> None:
    brand = _pin("优衣库", "优衣库（日本）")
    category = {
        "term": "T恤",
        "semantic_name": "二级类目",
        "table_name": "clothing_info",
        "field_name": "SubCategory",
        "canonical_value": "T恤/Polo衫/背心",
        "score": None,
        "strategy": "user_confirmed",
    }

    merged = ConversationService._merge_confirmations([brand], [category])

    assert {item["field_name"] for item in merged} == {"Name", "SubCategory"}


def test_filters_follow_the_latest_region_after_the_merge() -> None:
    """这条是报障的正解：合并之后的品牌值必须是日本，不是官网。"""
    stale = _pin("优衣库", "优衣库（官网）")
    fresh = _pin("优衣库", "优衣库（日本）")
    filters = [
        {
            "field": "品牌",
            "operator": "=",
            "value": "优衣库",
            "table_name": BRAND[0],
            "raw_field_name": BRAND[1],
        }
    ]

    merged = ConversationService._merge_confirmations([stale], [fresh])
    rewritten = ConversationService._canonicalize_filters(filters, merged)

    assert rewritten[0]["value"] == "优衣库（日本）"


def test_stale_brand_filter_from_the_previous_turn_is_stamped_to_the_new_region() -> None:
    """上一轮的 `品牌 =优衣库（官网）` 被 agent 带回 filters 时，必须改成本轮的日本。

    实测（2026-10-18）：改问日本后 SQL 已经按日本取数，但卡片条件栏还写着官网，
    并且额外报一句"品牌 =优衣库（官网）没能落到 SQL 里"——矛盾条件比没有条件更糟。
    """
    filters = [
        {
            "field": "品牌",
            "operator": "=",
            "value": "优衣库（官网）",
            "table_name": BRAND[0],
            "raw_field_name": BRAND[1],
        }
    ]

    rewritten = ConversationService._canonicalize_filters(filters, [_pin("优衣库", "优衣库（日本）")])

    assert rewritten[0]["value"] == "优衣库（日本）"
    assert rewritten[0]["canonicalized_from"] == "优衣库（官网）"


# ---------------------------------------------------------------- 写回上下文


def test_resolved_brand_is_remembered_as_the_new_context() -> None:
    conversation = _conversation([_pin("优衣库", "优衣库（官网）")])

    ConversationService()._remember_brand_resolution(
        conversation=conversation,
        scene=_scene(),
        term="优衣库（日本）",
        resolution={"canonical_value": "优衣库（日本）", "needs_confirmation": False},
    )

    pins = conversation.working_memory.confirmed_field_values
    assert [item["canonical_value"] for item in pins] == ["优衣库（日本）"]
    # 下一轮裸「优衣库」要能靠词干命中这条钉子
    assert ConversationService._brand_base(pins[0]["term"]) == "优衣库"


def test_unresolved_brand_is_not_written_back() -> None:
    """还没定下来的东西不许固化成语义上下文。"""
    conversation = _conversation([_pin("优衣库", "优衣库（官网）")])

    ConversationService()._remember_brand_resolution(
        conversation=conversation,
        scene=_scene(),
        term="优衣库",
        resolution={"canonical_value": "优衣库", "needs_confirmation": True},
    )

    assert [item["canonical_value"] for item in conversation.working_memory.confirmed_field_values] == [
        "优衣库（官网）"
    ]


# ---------------------------------------------------------------- 追问的时机


def test_agent_question_is_dropped_once_the_turn_produced_something() -> None:
    """已经出图/出卡 → 不再追问「日本是指站点吗」。"""
    trailing = ConversationService._data_query_trailing(
        notes="",
        clarify_question="「日本」是指只统计日本站点，还是只是说明品牌是日本的？",
        produced=True,
    )

    assert trailing == ""


def test_agent_question_survives_when_nothing_was_produced() -> None:
    """什么也没产出 → 该反问就反问。"""
    question = "「日本」是指只统计日本站点，还是只是说明品牌是日本的？"

    trailing = ConversationService._data_query_trailing(
        notes="", clarify_question=question, produced=False
    )

    assert question in trailing


def test_factual_notes_are_kept_even_when_something_was_produced() -> None:
    """事实性说明（被拒的筛选条件等）不随产出消失。"""
    trailing = ConversationService._data_query_trailing(
        notes="没能识别你给的筛选条件：颜色",
        clarify_question="还要看别的吗？",
        produced=True,
    )

    assert "没能识别你给的筛选条件" in trailing
    assert "还要看别的吗" not in trailing
