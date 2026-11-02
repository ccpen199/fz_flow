"""闸门：图表卡截断必须「整类目 + 如实声明」，不许把时间轴切成最早的一小段。

真事故（2026-10-12）：`build()` 里的 `limited = clean_rows[:60]` 在建 x 轴**之前**
就把行砍了，而 agent 的 SQL 是 `ORDER BY month, total_percent DESC` ——
101 个月的纤维数据被砍成最早的 6 个月，图上那条"趋势"只是开头一小段。
"""

from app.services.chart_spec_service import (
    CATEGORY_GRACE,
    DEFAULT_MAX_ROWS,
    ChartSpecService,
)

COLUMNS = ["month", "fiber_name", "total_percent"]


def _month_labels(count: int) -> list[str]:
    return ["%04d-%02d" % (2017 + index // 12, index % 12 + 1) for index in range(count)]


def _fiber_names(count: int) -> list[str]:
    return ["纤维%03d" % index for index in range(count)]


def _make(months: list[str], names: list[str], value: float = 1.0) -> list[dict]:
    return [
        {"month": month, "fiber_name": name, "total_percent": value}
        for month in months
        for name in names
    ]


def test_a_result_under_the_ceiling_is_not_touched() -> None:
    spec = ChartSpecService().build(
        rows=_make(_month_labels(3), _fiber_names(2)),
        columns=COLUMNS,
        metric_hint="total_percent",
    )

    assert spec["truncation"] == {}
    assert spec["total_rows"] == 6


def test_truncation_keeps_whole_categories_and_declares_the_rest() -> None:
    source = _make(_month_labels(40), _fiber_names(2))  # 40 个月 × 2 种 = 80 行
    spec = ChartSpecService().build(
        rows=source, columns=COLUMNS, metric_hint="total_percent", max_rows=10
    )

    info = spec["truncation"]
    assert info["total_categories"] == 40
    assert info["kept_categories"] == 5
    # 整类目：保留的月数 × 每月行数 == 保留行数（不存在"半个类目"）
    assert info["kept_rows"] == info["kept_categories"] * 2
    assert info["omitted_rows"] == len(source) - info["kept_rows"]
    # 保留的永远是原结果的**前缀**：只裁尾部，不重排、不改值
    assert spec["rows"] == source[: info["kept_rows"]]
    assert spec["total_rows"] == len(source)


def test_one_oversized_category_is_cut_inside_it_and_declared() -> None:
    """极端情况：单个类目自己就超上限若干倍 —— 只能在类目内部裁断，但要如实说明。"""
    source = _make(["2017-01"], _fiber_names(500))
    spec = ChartSpecService().build(
        rows=source, columns=COLUMNS, metric_hint="total_percent", max_rows=100
    )

    info = spec["truncation"]
    assert info["cut_inside_category"] is True
    assert info["kept_rows"] == 100 * CATEGORY_GRACE
    assert spec["rows"] == source[: info["kept_rows"]]


def test_a_real_sized_result_is_no_longer_squeezed_into_six_months() -> None:
    """回归本次事故：101 个月 × 16 种纤维不再被砍成最早的 6 个月。"""
    source = _make(_month_labels(101), _fiber_names(16))
    assert len(source) == 1616
    assert len(source) <= DEFAULT_MAX_ROWS

    spec = ChartSpecService().build(
        rows=source, columns=COLUMNS, metric_hint="total_percent"
    )

    assert spec["truncation"] == {}
    assert len({row["month"] for row in spec["rows"]}) == 101
    assert spec["rows"] == source
