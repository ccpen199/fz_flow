"""「哪个月份有数据」要直接回答；「换个方向」要把上一个问题撤下。

报障（2026-10-19，用户截图三连问）：
1. 问「你看看优衣库在哪个月份有数据」→ 弹出的却是「优衣库 4 个候选，选一个」；
2. 接着问「你好」→ 回复仍然绕着那张待确认卡；
3. 说「换个方向」→ 还是那批东西，像在原地打转。

用户原话：「agent根本不理解我在问什么。没法理解任务」「我说了换个方向 它还是继续在
原来的方向去打转」。

这里锁两件**纯逻辑**（不碰数据库）：
- 覆盖度问句的识别（且不能把「哪个月份销量最高」这类真取数问句吞掉）；
- 换方向时，待确认的消歧卡必须从画布上撤下（品牌槽位本身不动，下次真取数还会问）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from packages.shared_contracts.python_models import (
    ArtifactCardDTO,
    ConversationDTO,
    WorkingMemoryDTO,
)
from app.services.conversation_service import ConversationService
from app.services.intent_router import is_coverage_question
from app.store import ARTIFACTS, CONVERSATIONS


def test_coverage_phrasings_are_recognised() -> None:
    for text in (
        "你看看优衣库在哪个月份有数据",
        "优衣库有数据的月份",
        "这批数据覆盖到什么时候",
        "优衣库有哪几个月",
    ):
        assert is_coverage_question(text), text


def test_real_query_about_a_month_is_not_swallowed() -> None:
    """「哪个月份销量最高」是要指标，不是问覆盖度 —— 不能被这条分支截走。"""
    for text in ("哪个月份销量最高", "看看最近一个月的价格带分布", "价格带与折扣节奏"):
        assert not is_coverage_question(text), text


def test_direction_switch_phrasings() -> None:
    for text in ("换个方向", "换一个角度", "先不选品牌了", "看点别的"):
        assert ConversationService._wants_direction_switch(text), text
    for text in ("你好", "看看价格带分布", "换成折线图"):
        assert not ConversationService._wants_direction_switch(text), text


def _seed_conversation() -> tuple[str, str, str]:
    """直接建会话与卡片（不经过 `create`，避免测试里连配置中心）。"""
    now = datetime.now(UTC)
    conversation = ConversationDTO(
        conversation_id="conv_test_switch",
        title="测试",
        working_memory=WorkingMemoryDTO(),
        created_at=now,
        updated_at=now,
    )
    CONVERSATIONS[conversation.conversation_id] = conversation
    pending = ArtifactCardDTO(
        artifact_id="art_pending",
        conversation_id=conversation.conversation_id,
        kind="resolution_card",
        title="品牌「优衣库」需要确认",
        payload={"status": "needs_confirmation"},
        created_at=now,
        updated_at=now,
    )
    chart = ArtifactCardDTO(
        artifact_id="art_chart",
        conversation_id=conversation.conversation_id,
        kind="chart_card",
        title="月度数据覆盖",
        payload={"status": "ready"},
        created_at=now,
        updated_at=now,
    )
    for card in (pending, chart):
        ARTIFACTS[card.artifact_id] = card
        conversation.working_memory.produced_artifact_ids.append(card.artifact_id)
    return conversation.conversation_id, pending.artifact_id, chart.artifact_id


def test_direction_switch_retires_the_pending_card_only() -> None:
    conversation_id, pending_id, chart_id = _seed_conversation()
    retired = ConversationService()._retire_pending_resolution(conversation_id)
    assert retired is True
    remaining = CONVERSATIONS[conversation_id].working_memory.produced_artifact_ids
    assert pending_id not in remaining
    assert chart_id in remaining


def test_retiring_twice_is_a_no_op() -> None:
    conversation_id, _, _ = _seed_conversation()
    service = ConversationService()
    assert service._retire_pending_resolution(conversation_id) is True
    assert service._retire_pending_resolution(conversation_id) is False


# ---------------------------------------------------------------- control 意图
# 「换个方向」现在由 agent 判成 intent=control（不再靠后端正则猜）。它必须：
# 撤下待确认卡、**不**重新渲染方向卡（否则用户看到同一个品牌同一批入口 = 原地打转）、
# 不产生任何产物。


def test_control_turn_retires_pending_and_adds_nothing() -> None:
    conversation_id, pending_id, chart_id = _seed_conversation()
    plan = ConversationService()._handle_control(
        conversation_id, {"reply": "行，这个方向先放下。", "control": {"action": "switch_direction"}}
    )

    assert plan["artifact_refs"] == []
    assert plan["actions"] == []
    assert "这个方向先放下" in plan["assistant_text"]
    assert "撤下" in plan["assistant_text"]
    remaining = CONVERSATIONS[conversation_id].working_memory.produced_artifact_ids
    assert pending_id not in remaining
    assert chart_id in remaining


def test_control_turn_without_pending_card_does_not_claim_it_retired() -> None:
    conversation_id, _, _ = _seed_conversation()
    service = ConversationService()
    service._retire_pending_resolution(conversation_id)  # 先撤一次

    plan = service._handle_control(conversation_id, {"reply": "好，先放下。"})

    assert "撤下" not in plan["assistant_text"]


# ------------------------------------------------------------ 覆盖度要统计谁


def test_coverage_subjects_prefer_the_agent_slot() -> None:
    # agent 槽位是它对这句话的理解（可能一句话点了两个品牌），优先级最高。
    plan = {
        "coverage": {"kind": "months", "brands": ["优衣库", "ZARA"]},
        "filters": [{"field": "品牌", "operator": "=", "value": "别家"}],
    }
    assert ConversationService._coverage_subjects(plan, {"brand": "优衣库"}) == ["优衣库", "ZARA"]


def test_coverage_subjects_fall_back_to_filters_then_memory() -> None:
    with_filters = {"filters": [{"field": "品牌", "operator": "=", "value": ["优衣库", "ZARA"]}]}
    assert ConversationService._coverage_subjects(with_filters, {"brand": "x"}) == ["优衣库", "ZARA"]
    # 什么都没给才用会话品牌：「这批数据覆盖到什么时候」在聊优衣库时指的就是优衣库。
    assert ConversationService._coverage_subjects({}, {"brand": "优衣库"}) == ["优衣库"]
    assert ConversationService._coverage_subjects({}, {}) == []


def test_named_brand_that_cannot_be_resolved_is_not_widened_to_all_brands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """点名的品牌在字典里对不上 → 如实说，**不许**退化成"全库统计"。

    这是最容易悄悄答错的一条：用户问「XX 哪个月有数据」，字典里没有 XX，
    拿全库的月份回给他，看起来像答了，其实答的是另一个问题。
    """
    conversation_id, _, _ = _seed_conversation()
    service = ConversationService()
    monkeypatch.setattr(
        service,
        "resolve_brand",
        lambda **kwargs: {"candidates": [], "needs_confirmation": False},
    )
    monkeypatch.setattr(
        service,
        "_monthly_coverage",
        lambda *args, **kwargs: pytest.fail("品牌对不上时不该去查库"),
    )

    plan = service._answer_coverage_question(
        conversation_id=conversation_id,
        scene=SimpleNamespace(scene_id="scene_x", version=1),
        routing={},
        subjects=["不存在的品牌"],
    )

    assert plan is not None
    assert "没能对上" in plan["assistant_text"]
    assert plan["artifact_refs"] == []


def test_global_coverage_sql_uses_single_percent_when_there_are_no_params(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全库覆盖度那条 SQL 必须用**单 `%`**（pymysql 无参数时不做 % 插值）。

    2026-10-19 实测踩过：无品牌的覆盖度问句走了带参数的 `%%` 版本，`%%Y-%%m` 被原样
    送到 MySQL，整列月份变成字面量 `"%Y-%m"` —— 页面显示"共 1 个月（97879 条记录）"。
    这里只断言**发出去的 SQL 文本**，不造任何业务行。
    """
    import sys
    from types import ModuleType

    executed: list[tuple[str, object]] = []

    class _Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, args=None):
            executed.append((sql, args))

        def fetchall(self):
            return []

    class _Connection:
        def cursor(self):
            return _Cursor()

        def close(self):
            pass

    fake = ModuleType("pymysql")
    fake.connect = lambda **kwargs: _Connection()
    monkeypatch.setitem(sys.modules, "pymysql", fake)

    field = SimpleNamespace(
        role="time", table_name="clothing_info", field_name="CreateTime", semantic_name="抓取日期"
    )
    scene = SimpleNamespace(scene_id="scene_x", version=1, fields=[field])

    result = ConversationService._monthly_coverage(scene, [])

    assert result is not None
    rows, display_sql = result
    assert rows == []
    sql, args = executed[0]
    assert args is None
    assert "'%Y-%m'" in sql and "%%" not in sql
    assert display_sql == sql
