"""时间窗必须**看得见**：条件栏、结论、回复里都要说清"这是哪个月的数据"。

报障（2026-10-19，用户截图）：问「优衣库品牌最近一个月上了多少件T恤」，
图上是 2025-07-01 一个点（折线还因为 `showSymbol:false` 画不出东西），
条件栏只有「品牌 =优衣库（淘宝）」「二级类目 =T恤」，**完全没有时间**。
用户的原话："不然我怎么知道这些数据是哪个月份的？"

这里锁住三件纯逻辑（不碰数据库）：
1. 从结果行里取真实日期区间；
2. 时间窗渲染成一条可见条件（单天要说"仅一天"）；
3. 已经有"时间"条件时不重复追加。

⚠️ 下面的 `_spec()` 只造**结构占位**（日期格子 + 一个类目占位），用来验证
"能不能从行里认出日期列"，不断言任何业务数值；真实数据来自 MySQL。
"""

from __future__ import annotations

from typing import Any

from app.services.conversation_service import ConversationService


def _spec(rows: list[dict[str, Any]], x_field: str = "listing_date") -> dict[str, Any]:
    """结构占位：给日期区间提取器喂最小行结构，非业务结果。"""
    return {"x_field": x_field, "rows": rows}


def test_spec_date_range_reads_the_real_rows() -> None:
    spec = _spec(
        [
            {"listing_date": "2024-06-12"},
            {"listing_date": "2024-07-03"},
            {"listing_date": "2024-07-03"},
        ]
    )
    assert ConversationService._spec_date_range(spec) == ("2024-06-12", "2024-07-03", 2)


def test_spec_date_range_ignores_non_date_columns() -> None:
    spec = _spec([{"label": "A", "value": 1}], x_field="label")
    assert ConversationService._spec_date_range(spec) is None
    assert ConversationService._spec_date_range(_spec([])) is None


def test_single_day_window_says_it_out_loud() -> None:
    """只有一天有数据时，必须说出来 —— 否则用户以为"图坏了"。"""
    entry = ConversationService._time_filter_entry("近1个月", ("2025-07-01", "2025-07-01", 1))
    assert entry is not None
    assert entry["field"] == "时间"
    assert entry["value"] == "近1个月"
    assert entry["display"] == "时间 =近1个月（数据仅 2025-07-01 一天）"


def test_multi_day_window_shows_the_span() -> None:
    entry = ConversationService._time_filter_entry("近3个月", ("2024-05-01", "2024-07-31", 12))
    assert entry is not None
    assert entry["display"] == "时间 =近3个月（数据 2024-05-01 ~ 2024-07-31，12 天有数据）"


def test_no_time_window_adds_nothing() -> None:
    assert ConversationService._time_filter_entry("", None) is None


def test_existing_time_filter_is_not_duplicated() -> None:
    filters = [{"field": "时间", "operator": "=", "value": "2024-07", "display": "时间 =2024-07"}]
    merged = ConversationService._with_time_filter(filters, "近1个月", ("2025-07-01", "2025-07-01", 1))
    assert merged == filters


def test_time_filter_is_appended_after_the_other_conditions() -> None:
    filters = [{"field": "品牌", "operator": "=", "value": "优衣库（淘宝）", "display": "品牌 =优衣库（淘宝）"}]
    merged = ConversationService._with_time_filter(filters, "近1个月", ("2025-07-01", "2025-07-01", 1))
    assert [item["field"] for item in merged] == ["品牌", "时间"]
    assert merged[0] == filters[0]
