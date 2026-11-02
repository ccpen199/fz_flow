"""对比（compare）/ 解释（explain）/ 下钻分组（group_by）的回归。

三条都是 2026-10-19 用户实测暴露出来的"嘴上答应了、结构化字段没填"的问题：

* 「ZARA 和优衣库对比一下价格带」被判成普通取数、`filters` 是空的 → 只查了会话里那一家；
* 「为什么优衣库中国这个月是 0」被判成 coverage → 去统计月份，答非所问；
* 「T恤里再按颜色拆一下」判对了 `二级类目=T恤`，但**分组列**没地方放 → 照旧跑了一遍
  「品类结构分布」，画出来还是 `一级类目`。

这里锁**纯逻辑**（不连库、不造任何业务 rows/spec）：
- `group_by` 只能落到本轮配置里真实存在、可分组（dimension / filter）的字段上；
- 对比把多个主体压成**一条** `品牌 ∈ …` 条件，且会话里钉着的单品牌不能覆盖它；
- 解释只读卡片上已经存下的口径，不重新取数。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services import canvas_action_service
from app.services.conversation_service import ConversationService
from app.store import ARTIFACTS, CONVERSATIONS
from packages.shared_contracts.python_models import (
    ArtifactCardDTO,
    ConversationDTO,
    FieldRole,
    SceneDTO,
    SceneFieldDTO,
    WorkingMemoryDTO,
)


def _scene() -> SceneDTO:
    """一套只有"真实字段名/角色"的配置骨架（不含任何业务取值）。"""

    def field(name: str, role: FieldRole, table: str = "clothing_info") -> SceneFieldDTO:
        return SceneFieldDTO(
            field_id=f"{table}.{name}",
            table_name=table,
            field_name=name,
            semantic_name=name,
            description="",
            role=role,
        )

    return SceneDTO(
        scene_id="scene_test",
        name="测试配置",
        description="",
        fields=[
            field("二级类目", FieldRole.DIMENSION),
            field("颜色", FieldRole.DIMENSION),
            field("适用性别", FieldRole.FILTER),
            field("价格", FieldRole.METRIC),
            field("上架时间", FieldRole.TIME),
        ],
    )


def _seed_conversation() -> str:
    now = datetime.now(UTC)
    conversation = ConversationDTO(
        conversation_id="conv_test_compare_explain",
        title="对比与解释用例",
        working_memory=WorkingMemoryDTO(),
        created_at=now,
        updated_at=now,
    )
    CONVERSATIONS[conversation.conversation_id] = conversation
    return conversation.conversation_id


@pytest.fixture(autouse=True)
def _cleanup() -> None:
    yield
    CONVERSATIONS.pop("conv_test_compare_explain", None)
    for artifact_id in list(ARTIFACTS):
        if artifact_id.startswith("art_test_"):
            ARTIFACTS.pop(artifact_id, None)


# ------------------------------------------------------------------ group_by
# 「按 X 拆一下」的分组列：agent 看到的是全部配置的字段并集，落不到本轮配置的字段上
# 就必须如实退回——不能挑一个近似的列去拆（那会让卡片标题和 SQL 各说一套）。


def test_group_by_accepts_dimension_and_filter_roles() -> None:
    accepted, rejected = ConversationService._resolve_group_by(["颜色", "适用性别"], _scene())
    assert accepted == ["颜色", "适用性别"]
    assert rejected == []


def test_group_by_rejects_metric_time_and_unknown_fields() -> None:
    """指标不能当分组列；时间要按日期粒度截断（那是趋势方向的口径），不在这里顺手做。"""
    accepted, rejected = ConversationService._resolve_group_by(
        ["价格", "上架时间", "不存在的列"], _scene()
    )
    assert accepted == []
    assert rejected == ["价格", "上架时间", "不存在的列"]


def test_group_by_without_turn_config_is_all_rejected() -> None:
    """这一轮没落到具体某套配置 → 没有字段资产可校验，只能如实退回（不猜）。"""
    accepted, rejected = ConversationService._resolve_group_by(["颜色"], None)
    assert accepted == []
    assert rejected == ["颜色"]


def test_group_by_ignores_non_list_and_blank_values() -> None:
    assert ConversationService._resolve_group_by(None, _scene()) == ([], [])
    assert ConversationService._resolve_group_by("颜色", _scene()) == ([], [])
    assert ConversationService._resolve_group_by(["", "  "], _scene()) == ([], [])


# --------------------------------------------------------------- compare 槽位


def test_compare_subjects_prefer_the_slot_and_dedupe() -> None:
    plan = {
        "compare": {"subjects": ["ZARA", "优衣库", "ZARA"]},
        "filters": [{"field": "品牌", "operator": "=", "value": "别家"}],
    }
    assert ConversationService._compare_subjects(plan, {"brand": "优衣库"}) == ["ZARA", "优衣库"]


def test_compare_subjects_fall_back_to_filters_then_memory() -> None:
    with_filters = {"filters": [{"field": "品牌", "operator": "=", "value": ["ZARA", "优衣库"]}]}
    assert ConversationService._compare_subjects(with_filters, {"brand": "x"}) == ["ZARA", "优衣库"]
    assert ConversationService._compare_subjects({}, {"brand": "优衣库"}) == ["优衣库"]


def test_compare_merges_subjects_into_one_in_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """多主体 = **一条** `品牌 ∈ A / B`，不是各查一遍再拼两张卡（拼卡无法逐值复核）。"""
    conversation_id = _seed_conversation()
    service = ConversationService()
    canonical = {"ZARA": "ZARA（中国）", "优衣库": "优衣库（中国）"}
    seen: dict[str, object] = {}

    monkeypatch.setattr(
        service,
        "resolve_brand",
        lambda **kwargs: {"needs_confirmation": False, "canonical_value": canonical[kwargs["term"]]},
    )

    def fake_data_query(cid, scene, routing, *, compare_values=None, group_by=None):
        seen["filters"] = routing["filters"]
        seen["compare_values"] = compare_values
        return {"assistant_text": "ok", "artifact_refs": [], "actions": []}

    monkeypatch.setattr(service, "_handle_data_query", fake_data_query)
    monkeypatch.setattr(service, "_compare_split_ok", lambda refs, values: True)

    service._handle_compare(
        conversation_id=conversation_id,
        scene=_scene(),
        routing={"filters": [{"field": "二级类目", "operator": "=", "value": "T恤"}]},
        agent_plan={"compare": {"subjects": ["ZARA", "优衣库"]}},
    )

    filters = seen["filters"]
    assert filters[0]["field"] == "品牌"
    assert filters[0]["operator"] == "in"
    assert filters[0]["value"] == ["ZARA（中国）", "优衣库（中国）"]
    # 用户另外提的条件不能被对比冲掉。
    assert any(item["field"] == "二级类目" for item in filters)
    assert seen["compare_values"] == ["ZARA（中国）", "优衣库（中国）"]


def test_compare_keeps_every_caliber_when_one_name_is_ambiguous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一个品牌名对应多个标准口径（优衣库 4 个）→ 全部放进对比并说明，不替用户挑一个。"""
    conversation_id = _seed_conversation()
    service = ConversationService()

    def fake_resolve(**kwargs):
        if kwargs["term"] == "优衣库":
            return {
                "needs_confirmation": True,
                "candidates": [{"value": "优衣库（官网）"}, {"value": "优衣库（日本）"}],
            }
        return {"needs_confirmation": False, "canonical_value": "ZARA（中国）"}

    seen: dict[str, object] = {}
    monkeypatch.setattr(service, "resolve_brand", fake_resolve)
    monkeypatch.setattr(
        service,
        "_handle_data_query",
        lambda cid, scene, routing, *, compare_values=None, group_by=None: (
            seen.update(values=compare_values)
            or {"assistant_text": "ok", "artifact_refs": [], "actions": []}
        ),
    )
    monkeypatch.setattr(service, "_compare_split_ok", lambda refs, values: True)

    plan = service._handle_compare(
        conversation_id=conversation_id,
        scene=_scene(),
        routing={"filters": []},
        agent_plan={"compare": {"subjects": ["ZARA", "优衣库"]}},
    )

    assert seen["values"] == ["ZARA（中国）", "优衣库（官网）", "优衣库（日本）"]
    assert "多个标准口径" in plan["assistant_text"]


def test_compare_with_one_resolved_subject_says_it_is_not_a_comparison(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只认出一个主体时按单品牌取数，但必须**明说**这不是对比，不能假装比了两家。"""
    conversation_id = _seed_conversation()
    service = ConversationService()

    monkeypatch.setattr(
        service,
        "resolve_brand",
        lambda **kwargs: (
            {"needs_confirmation": False, "canonical_value": "ZARA（中国）"}
            if kwargs["term"] == "ZARA"
            else {"needs_confirmation": False, "canonical_value": ""}
        ),
    )
    monkeypatch.setattr(
        service,
        "_handle_data_query",
        lambda cid, scene, routing, **kwargs: {"assistant_text": "取数结果", "artifact_refs": []},
    )

    plan = service._handle_compare(
        conversation_id=conversation_id,
        scene=_scene(),
        routing={"filters": []},
        agent_plan={"compare": {"subjects": ["ZARA", "查无此牌"]}},
    )

    assert "只落到了一个标准口径" in plan["assistant_text"]
    assert "不是对比" in plan["assistant_text"]


# ------------------------------------------------------------- filters: in


def test_in_operator_keeps_all_values() -> None:
    filters, rejected = canvas_action_service.normalize_filters(
        [{"field": "品牌", "operator": "in", "value": ["ZARA（中国）", "优衣库（中国）"]}],
        allowed_fields={"品牌": {"semantic_name": "品牌", "table_name": "dict_brand_info", "field_name": "Name"}},
    )
    assert not rejected
    assert filters[0]["operator"] == "in"
    assert filters[0]["value"] == ["ZARA（中国）", "优衣库（中国）"]
    assert "∈" in filters[0]["display"]


def test_in_operator_rejects_empty_or_scalar_values() -> None:
    allowed = {"品牌": {"semantic_name": "品牌", "table_name": "dict_brand_info", "field_name": "Name"}}
    for value in ([], "ZARA"):
        filters, rejected = canvas_action_service.normalize_filters(
            [{"field": "品牌", "operator": "in", "value": value}], allowed_fields=allowed
        )
        assert filters == []
        assert rejected, value


# ------------------------------------------------------------- explain 槽位


def test_explain_without_cards_does_not_invent_an_explanation() -> None:
    conversation_id = _seed_conversation()
    plan = ConversationService()._handle_explain(
        conversation_id, {"target": {}}, {"explain": {"about": "number"}}
    )
    assert "没有可以解释的对象" in plan["assistant_text"]
    assert plan["artifact_refs"] == []


def test_as_number_reads_decimal_from_the_driver() -> None:
    """pymysql 的 DECIMAL 是 `Decimal`，不是 float——漏掉它会让"有没有 0"永远判不出。"""
    assert ConversationService._as_number(Decimal("0")) == 0.0
    assert ConversationService._as_number(Decimal("12.50")) == 12.5
    assert ConversationService._as_number("3") == 3.0
    assert ConversationService._as_number(True) is None
    assert ConversationService._as_number(None) is None
    assert ConversationService._as_number("不是数字") is None


# ------------------------------------------------- 解释要按 about 裁剪答案
# 2026-10-19 实测：`explain.about` 判对了却没用上——"这个数怎么算出来的"和
# "为什么这个月是 0"回的是同一段话（把结论和"没有 0"一起堆上去），答非所问。


def _seed_chart(
    conversation_id: str,
    *,
    artifact_id: str = "art_test_ready",
    title: str = "旧图",
    payload: dict | None = None,
) -> str:
    """只建**元数据**卡（不造任何业务 rows）：状态、口径字段、条数。"""
    now = datetime.now(UTC)
    card = ArtifactCardDTO(
        artifact_id=artifact_id,
        conversation_id=conversation_id,
        kind="chart_card",
        title=title,
        payload=payload
        or {
            "status": "ready",
            "dimension_key": "structure",
            "rows_count": 9,
            "applied_filters": [{"field": "品牌", "operator": "=", "value": "X", "display": "品牌 =X"}],
            "spec": {"x_field": "一级类目", "y_field": "价格跨度"},
            "sql_explanation": "按一级类目分组的聚合口径。",
        },
        created_at=now,
        updated_at=now,
    )
    ARTIFACTS[card.artifact_id] = card
    conversation = CONVERSATIONS[conversation_id]
    conversation.working_memory.chart_artifact_ids.append(card.artifact_id)
    conversation.working_memory.produced_artifact_ids.append(card.artifact_id)
    return card.artifact_id


def _seed_pending(conversation_id: str) -> str:
    now = datetime.now(UTC)
    card = ArtifactCardDTO(
        artifact_id="art_test_pending",
        conversation_id=conversation_id,
        kind="clarification_card",
        title="口径待确认",
        subtitle="这个筛选条件没有记录",
        payload={"status": "needs_clarification", "error_detail": "去掉它可查到 6 条"},
        created_at=now,
        updated_at=now,
    )
    ARTIFACTS[card.artifact_id] = card
    CONVERSATIONS[conversation_id].working_memory.produced_artifact_ids.append(card.artifact_id)
    return card.artifact_id


def test_explain_method_does_not_dump_conclusions() -> None:
    """问"怎么算出来的"→ 给口径说明，不要把结论/"有没有 0"堆上去。"""
    conversation_id = _seed_conversation()
    _seed_chart(
        conversation_id,
        payload={
            "status": "ready",
            "dimension_key": "structure",
            "rows_count": 9,
            "applied_filters": [],
            "spec": {"x_field": "一级类目", "y_field": "价格跨度"},
            "findings": ["占位结论"],
            "sql_explanation": "按一级类目分组的聚合口径。",
        },
    )

    plan = ConversationService()._handle_explain(
        conversation_id, {"target": {}}, {"explain": {"about": "method"}}
    )

    text = plan["assistant_text"]
    assert "口径说明：按一级类目分组的聚合口径。" in text
    assert "这张图的结论就来自这些真实结果" not in text
    assert "这张卡里没有 0" not in text
    assert "计算方式以这条口径说明" in text
    # 口径里要带上"按什么分组、指标是什么"，用户才知道这个数是怎么来的。
    assert "按「一级类目」分组" in text
    assert "指标是「价格跨度」" in text


def test_explain_empty_prefers_the_pending_card_over_an_older_chart() -> None:
    """问"怎么一条都没有"时，别去解释一张更早、正常出数的旧图。"""
    conversation_id = _seed_conversation()
    _seed_chart(conversation_id, title="旧的价格带图")
    _seed_pending(conversation_id)

    plan = ConversationService()._handle_explain(
        conversation_id, {"target": {}}, {"explain": {"about": "empty"}}
    )

    text = plan["assistant_text"]
    assert "上一次取数没有直接出图" in text
    assert "旧的价格带图" not in text


# --------------------------------------------- 标准值没命中：说出真实取值
# 2026-10-19 实测：用户说「只看女装」，库里 SuitableGender 只有 女/通用/男，
# 取数闸门如实拦下，但回复是引擎术语（"无法命中的标准值过滤"），用户不知道下一句说什么。


def test_blocked_value_note_lists_the_real_values(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import conversation_service as module

    seen: dict[str, object] = {}

    def fake_diagnose(**kwargs):
        seen.update(kwargs)
        return {
            "checked": True,
            "blocker": {"field": "适用性别", "display": "适用性别 =女装", "value": "女装"},
            "alternatives": [
                {"field": "适用性别", "value": "女", "rows": 100},
                {"field": "适用性别", "value": "通用", "rows": 50},
            ],
        }

    monkeypatch.setattr(module.empty_result_diagnosis_service, "diagnose", fake_diagnose)

    note = ConversationService()._describe_blocked_values(
        scene=_scene(),
        filters=[
            {"field": "适用性别", "operator": "=", "value": "女装", "display": "适用性别 =女装"},
            {"field": "品牌", "operator": "=", "value": "优衣库（中国）"},
        ],
        time_range=None,
    )

    assert "「适用性别 =女装」在库里没有这个取值" in note
    assert "「女」" in note and "「通用」" in note
    # 品牌是用户明确选的，只作为固定条件参与计数，不进"可以换掉"的候选池。
    assert [item["field"] for item in seen["filters"]] == ["适用性别"]
    assert [item["field"] for item in seen["fixed_filters"]] == ["品牌"]


def test_blocked_value_note_is_silent_without_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import conversation_service as module

    monkeypatch.setattr(
        module.empty_result_diagnosis_service,
        "diagnose",
        lambda **kwargs: {"checked": False, "reason": "没有可诊断的结构化筛选条件"},
    )
    assert (
        ConversationService()._describe_blocked_values(
            scene=_scene(),
            filters=[{"field": "适用性别", "operator": "=", "value": "女装"}],
            time_range=None,
        )
        == ""
    )
