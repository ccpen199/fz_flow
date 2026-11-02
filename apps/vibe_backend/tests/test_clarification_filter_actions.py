"""澄清卡"点了等于没点"的回归测试（2026-10-17 会话 `conv_dbc7be9bf4` 死循环）。

那次报障的链路是：SQL 跑通但 0 行 → 画布弹澄清卡 → 卡片只给两个**时间口径**选项
→ 用户点完仍是 0 行（因为真正筛空的是 `二级类目 = 'T恤'`，该品牌这批数据写作
`T恤/Polo衫/背心`）。这里锁住三处修复：

1. 卡片必须记住"是哪些条件把结果筛空的"，并在诊断出堵点时给出**改值/去条件**的选项；
2. 点了这类选项后，结构化动作要真的落到送给取数引擎的 `filters` 上；
3. "条件没落进 SQL"的判定按**字面量整体相等**，不再被子串命中骗过。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.services import canvas_action_service
from app.services.conversation_service import ConversationService, conversation_service
from app.store import ARTIFACTS, CONVERSATIONS
from packages.shared_contracts.python_models import (
    ArtifactCardDTO,
    ConversationDTO,
    WorkingMemoryDTO,
)


# ---------------------------------------------------------------- 字面量缺失判定


def test_exact_literal_counts_as_present() -> None:
    sql = "SELECT COUNT(*) FROM clothing_info WHERE `SubCategory` = 'T恤/Polo衫/背心'"
    filters = [{"field": "二级类目", "value": "T恤/Polo衫/背心"}]

    assert canvas_action_service.filters_missing_from_sql(sql, filters) == []


def test_substring_of_a_different_literal_is_not_present() -> None:
    """`T恤` ⊂ `T恤/Polo衫/背心`、`优衣库` ⊂ `uniqlo/优衣库` —— 子串命中 ≠ 条件生效。"""
    sneaky = [
        ("SELECT * FROM clothing_info WHERE `SubCategory` = 'T恤/Polo衫/背心'", "T恤"),
        ("SELECT * FROM clothing_info WHERE `BrandName` = 'uniqlo/优衣库'", "优衣库"),
        ("SELECT * FROM clothing_info WHERE `BrandName` = '优衣库（官网）'", "优衣库"),
    ]

    for sql, value in sneaky:
        missing = canvas_action_service.filters_missing_from_sql(
            sql, [{"field": "条件", "value": value}]
        )
        assert missing and missing[0]["value"] == value, (sql, value)


def test_like_pattern_still_counts_as_referenced() -> None:
    """`LIKE '%优衣库%'` 是真实存在的模糊匹配，不能报成"完全没进 SQL"。"""
    sql = "SELECT * FROM clothing_info WHERE `BrandName` LIKE '%优衣库%'"
    assert canvas_action_service.filters_missing_from_sql(sql, [{"field": "品牌", "value": "优衣库"}]) == []


def test_numeric_boundaries_are_respected() -> None:
    sql = "SELECT * FROM clothing_info WHERE `Price` >= 199"
    assert canvas_action_service.filters_missing_from_sql(sql, [{"field": "价格", "value": 199}]) == []
    assert canvas_action_service.filters_missing_from_sql(sql, [{"field": "价格", "value": 19}]) != []


def test_month_prefix_counts_as_present() -> None:
    """筛选「2024-07」常被写成 `>= '2024-07-01'`，这是生效的，不许报缺失。"""
    sql = "SELECT * FROM clothing_info WHERE `CreateTime` >= '2024-07-01'"
    assert canvas_action_service.filters_missing_from_sql(sql, [{"field": "月份", "value": "2024-07"}]) == []


# ---------------------------------------------------------------- 口径规范化


def _confirmation(*, table: str, field: str, term: str, canonical: str, semantic: str) -> dict[str, Any]:
    return {
        "table_name": table,
        "field_name": field,
        "term": term,
        "canonical_value": canonical,
        "semantic_name": semantic,
    }


def test_confirmed_canonical_value_replaces_the_raw_word() -> None:
    filters = [
        {
            "field": "品牌",
            "operator": "=",
            "value": "优衣库",
            "table_name": "dict_brand_info",
            "raw_field_name": "Name",
        }
    ]
    confirmations = [
        _confirmation(
            table="dict_brand_info",
            field="Name",
            term="优衣库",
            canonical="优衣库（官网）",
            semantic="品牌",
        )
    ]

    rewritten = ConversationService._canonicalize_filters(filters, confirmations)

    assert rewritten[0]["value"] == "优衣库（官网）"
    assert rewritten[0]["canonicalized_from"] == "优衣库"


def test_subcategory_raw_word_is_rewritten_to_the_real_value() -> None:
    filters = [
        {
            "field": "二级类目",
            "operator": "=",
            "value": "T恤",
            "table_name": "clothing_info",
            "raw_field_name": "SubCategory",
        }
    ]
    confirmations = [
        _confirmation(
            table="clothing_info",
            field="SubCategory",
            term="T恤",
            canonical="T恤/Polo衫/背心",
            semantic="二级类目",
        )
    ]

    assert ConversationService._canonicalize_filters(filters, confirmations)[0]["value"] == "T恤/Polo衫/背心"


def test_explicitly_different_value_is_never_overwritten() -> None:
    """用户点名要另一个类目，那是改需求，不是校正口径。"""
    filters = [
        {
            "field": "二级类目",
            "operator": "=",
            "value": "外套",
            "table_name": "clothing_info",
            "raw_field_name": "SubCategory",
        }
    ]
    confirmations = [
        _confirmation(
            table="clothing_info",
            field="SubCategory",
            term="T恤",
            canonical="T恤/Polo衫/背心",
            semantic="二级类目",
        )
    ]

    assert ConversationService._canonicalize_filters(filters, confirmations)[0]["value"] == "外套"


# ---------------------------------------------------------------- 筛选条件动作


def _applied_filters() -> list[dict[str, Any]]:
    return [
        {
            "field": "品牌",
            "operator": "=",
            "value": "优衣库（官网）",
            "table_name": "dict_brand_info",
            "raw_field_name": "Name",
            "display": "品牌 =优衣库（官网）",
        },
        {
            "field": "二级类目",
            "operator": "=",
            "value": "T恤",
            "table_name": "clothing_info",
            "raw_field_name": "SubCategory",
            "display": "二级类目 =T恤",
        },
    ]


def test_set_filters_rewrites_the_target_and_keeps_the_rest() -> None:
    option = {"set_filters": [{"field": "二级类目", "value": "T恤/Polo衫/背心"}]}

    result = ConversationService._apply_filter_actions(_applied_filters(), option)

    assert [item["value"] for item in result] == ["优衣库（官网）", "T恤/Polo衫/背心"]
    assert result[1]["display"] == "二级类目 =T恤/Polo衫/背心"


def test_set_filters_matches_by_raw_column_name_too() -> None:
    option = {"set_filters": [{"field": "SubCategory", "value": "T恤/Polo衫/背心"}]}

    result = ConversationService._apply_filter_actions(_applied_filters(), option)
    values = {item["field"]: item["value"] for item in result}

    assert values["二级类目"] == "T恤/Polo衫/背心"


def test_set_filters_appends_when_field_is_absent() -> None:
    option = {"set_filters": [{"field": "性别", "value": "女"}]}

    result = ConversationService._apply_filter_actions(_applied_filters(), option)

    assert result[-1] == {"field": "性别", "operator": "=", "value": "女", "display": "性别 =女"}


def test_drop_fields_removes_the_condition() -> None:
    result = ConversationService._apply_filter_actions(_applied_filters(), {"drop_fields": ["二级类目"]})

    assert [item["field"] for item in result] == ["品牌"]


def test_no_actions_leaves_filters_untouched() -> None:
    before = _applied_filters()

    assert ConversationService._apply_filter_actions(before, {}) == before


# ---------------------------------------------------------------- 选项生成


def _diagnosis() -> dict[str, Any]:
    return {
        "checked": True,
        "blocker": {
            "field": "二级类目",
            "column": "SubCategory",
            "table": "clothing_info",
            "value": "T恤",
            "display": "二级类目 =T恤",
            "rows_if_dropped": 417,
        },
        "alternatives": [{"field": "二级类目", "value": "T恤/Polo衫/背心", "rows": 121}],
        "rows_without_filters": 417,
    }


def test_empty_result_options_offer_the_real_value_first() -> None:
    options = ConversationService._clarify_option_entries(
        reason="empty_result",
        question_text="查询执行成功，但该口径下没有数据",
        time_window="2026-09",
        diagnosis=_diagnosis(),
    )

    assert options[0]["set_filters"] == [{"field": "二级类目", "value": "T恤/Polo衫/背心"}]
    assert "T恤/Polo衫/背心" in options[0]["label"]
    assert "121" in options[0]["label"]
    # 至少有一条"去掉这个限制"的出口
    assert any(option.get("drop_fields") == ["二级类目"] for option in options)


def test_empty_result_options_fall_back_to_time_window_without_evidence() -> None:
    options = ConversationService._clarify_option_entries(
        reason="empty_result",
        question_text="查询执行成功，但该口径下没有数据",
        time_window="2026-09",
        diagnosis={"checked": False},
    )

    assert options[0]["time_window"] == ""
    assert [option["label"] for option in options] == [
        "不限时间（看全部可用数据）",
        "仍限定在「2026-09」",
    ]
    # 旧文案里那句"请换一个统计口径重新取数"曾被原样回显，造成自相矛盾
    assert all("请换一个统计口径" not in option["answer"] for option in options)


def test_needs_clarification_options_stay_prose_only() -> None:
    options = ConversationService._clarify_option_entries(
        reason="needs_clarification",
        question_text="缺少统计口径；需要确认是按 SKU 还是按店铺汇总",
        time_window="",
        diagnosis=_diagnosis(),
    )

    assert options and all(set(option) == {"label", "answer"} for option in options)


# ---------------------------------------------------------------- 端到端：点选项 → 条件真的变


def _conversation(conversation_id: str) -> ConversationDTO:
    now = datetime.now(UTC)
    return ConversationDTO(
        conversation_id=conversation_id,
        title="空结果澄清用例",
        scene_id="scene_prd_competitor",
        scene_name="竞品企划分析",
        working_memory=WorkingMemoryDTO(scene_locked_by="explicit"),
        created_at=now,
        updated_at=now,
    )


def test_choosing_the_value_option_changes_the_filters_sent_to_the_engine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conversation_id = "conv_test_empty_result"
    CONVERSATIONS[conversation_id] = _conversation(conversation_id)
    options = ConversationService._clarify_option_entries(
        reason="empty_result",
        question_text="",
        time_window="2026-09",
        diagnosis=_diagnosis(),
    )
    card = ArtifactCardDTO(
        artifact_id="art_test_empty_result",
        conversation_id=conversation_id,
        kind="clarification_card",
        title="需要确认口径",
        subtitle="",
        status="ready",
        payload={
            "dimension_key": "dim_fiber",
            "reason": "empty_result",
            "options": options,
            "applied_filters": _applied_filters(),
            "time_window": "2026-09",
            "status": "needs_clarification",
        },
        lineage={},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    ARTIFACTS[card.artifact_id] = card
    captured: dict[str, Any] = {}

    def fake_execute(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ANN001
        captured.update(kwargs)
        return {"assistant_text": "", "artifact_refs": [], "actions": []}

    # ⚠️ 必须打在**类**上：打在单例实例上时，monkeypatch 复原写回的是"绑定方法"，
    # 它此后作为实例属性常驻，会永久遮蔽后续对类的打桩（实测让 test_turn_config 的
    # 取数桩失效，断言拿到空调用）。
    monkeypatch.setattr(ConversationService, "_execute_goals", fake_execute)

    result = conversation_service._apply_clarification(
        conversation_id=conversation_id,
        scene=None,  # 桩不读 scene，传 None 以免依赖配置中心
        card=card,
        answer=str(options[0]["answer"]),
    )

    assert result["ok"] is True
    values = {item["field"]: item["value"] for item in captured["filters"]}
    assert values == {"品牌": "优衣库（官网）", "二级类目": "T恤/Polo衫/背心"}
    assert captured["time_window"] == "2026-09"


def test_empty_round_does_not_claim_a_chart_was_generated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一个图都没成功时，回复不能留着规划器那句"数据图表已在右侧生成"。"""
    from types import SimpleNamespace

    from app.services.canvas_orchestrator import canvas_orchestrator

    conversation_id = "conv_test_honesty"
    CONVERSATIONS[conversation_id] = _conversation(conversation_id)
    plan_text = (
        "好的，已筛选 优衣库 （本月） 的上新数据：\n"
        "· 上新节奏与趋势：已聚合 二级类目 与 价格 的明细口径。\n"
        "数据图表已在右侧生成，支持直接交互；点开卡片可查看底层 SQL 与数据明细。"
    )

    monkeypatch.setattr(
        type(canvas_orchestrator),
        "plan_data_query",
        lambda self, **kwargs: {
            "assistant_text": plan_text,
            "goals": [{"dimension_key": "trend", "title": "上新节奏与趋势", "goal": "g"}],
            "dimension_keys": ["trend"],
        },
    )
    monkeypatch.setattr(ConversationService, "_ensure_dimension_options", lambda self, cid, scene: [])
    monkeypatch.setattr(ConversationService, "_chart_cards", lambda self, cid: [])
    monkeypatch.setattr(
        ConversationService,
        "_run_goal",
        lambda self, **kwargs: {
            "dimension_key": "trend",
            "title": "上新节奏与趋势",
            "status": "failed",
            "empty_result": True,
            "error": "查询执行成功，但该口径下没有数据",
            "diagnosis": {"checked": False},
        },
    )
    monkeypatch.setattr(
        ConversationService,
        "_ask_clarification",
        lambda self, **kwargs: SimpleNamespace(artifact_id="art_test_honesty"),
    )

    out = conversation_service._execute_goals(
        conversation_id=conversation_id, scene=None, keys=["trend"]
    )

    assert "数据图表已在右侧生成" not in out["assistant_text"]
    assert "这次没有生成可展示的图表" in out["assistant_text"]


def test_merged_bucket_option_says_it_is_a_merged_bucket() -> None:
    """合并标签（`T恤/Polo衫/背心`）不能写成"改用"。

    用户原话（2026-10-18）：「二级类目为何是 T恤/Polo衫/背心？这合理吗？」
    —— 该值是把三个子类并成一个，用它回答"多少件 T恤"会把 Polo 衫和背心算进来，
    所以卡片必须写明"只有合并标签"和"含什么"，而不是暗示两者等价。
    """
    diagnosis = {
        "checked": True,
        "blocker": {
            "field": "二级类目",
            "value": "T恤",
            "display": "二级类目 =T恤",
            "rows_if_dropped": 417,
        },
        "alternatives": [
            {
                "field": "二级类目",
                "value": "T恤/Polo衫/背心",
                "rows": 121,
                "merged_parts": ["T恤", "Polo衫", "背心"],
            }
        ],
    }

    options = ConversationService._clarify_option_entries(
        reason="empty_result",
        question_text="",
        time_window="2026-09",
        diagnosis=diagnosis,
    )

    label, answer = options[0]["label"], options[0]["answer"]
    assert "只有合并标签" in label
    assert "含Polo衫、背心" in label
    assert "改用" not in label
    assert "含Polo衫、背心" in answer
    assert options[0]["merged_parts"] == ["Polo衫", "背心"]
    # 仍然可以直接点：结构化动作不变
    assert options[0]["set_filters"] == [{"field": "二级类目", "value": "T恤/Polo衫/背心"}]


def test_plain_alias_option_keeps_the_plain_wording() -> None:
    """真正的同族写法（拼写差异）仍然说"改用"，不要一律套合并话术。"""
    diagnosis = {
        "checked": True,
        "blocker": {"field": "二级类目", "value": "T恤", "rows_if_dropped": 417},
        "alternatives": [{"field": "二级类目", "value": "T-shirt", "rows": 88}],
    }

    options = ConversationService._clarify_option_entries(
        reason="empty_result",
        question_text="",
        time_window="2026-09",
        diagnosis=diagnosis,
    )

    assert options[0]["label"] == "二级类目 改用「T-shirt」（88 条）"
    assert "只有合并标签" not in options[0]["label"]
