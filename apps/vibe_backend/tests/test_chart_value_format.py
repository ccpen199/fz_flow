from app.services.chart_spec_service import ChartSpecService, normalize_value_format


def _rows(values: list[float], key: str) -> list[dict]:
    return [
        {"month": "2017-10", "fiber_name": f"f{index}", key: value}
        for index, value in enumerate(values)
    ]


def test_sum_of_percent_column_is_not_labelled_percent() -> None:
    """真实事故：agent 把 SUM(Percent) 别名成 total_percent，被当成百分比贴上了 "%"。

    它是个**累加值**（实测能到 27895.9），不是百分比 —— 列名里带 "percent" 不算数。
    """
    spec = ChartSpecService().build(
        rows=_rows([27895.9, 24464.7, 9313.9], "total_percent"),
        columns=["month", "fiber_name", "total_percent"],
        metric_hint="total_percent",
    )

    assert spec["y_field"] == "total_percent"
    assert spec["value_format"] == "number"


def test_real_share_column_keeps_percent_format() -> None:
    spec = ChartSpecService().build(
        rows=_rows([35.22, 30.89, 11.76], "share_percent"),
        columns=["month", "fiber_name", "share_percent"],
        metric_hint="share_percent",
    )

    assert spec["value_format"] == "percent"


def test_share_column_topping_out_at_100_still_counts_as_percent() -> None:
    """100.0 是合法百分比（平均值恰好 100），不能因为「不严格小于 100」被排除。"""
    spec = ChartSpecService().build(
        rows=_rows([3.0, 100.0, 42.5], "avg_fiber_percent"),
        columns=["month", "fiber_name", "avg_fiber_percent"],
        metric_hint="avg_fiber_percent",
    )

    assert spec["value_format"] == "percent"


def test_column_without_share_hint_stays_plain_number() -> None:
    spec = ChartSpecService().build(
        rows=_rows([1200.0, 900.0], "sku_count"),
        columns=["month", "fiber_name", "sku_count"],
        metric_hint="sku_count",
    )

    assert spec["value_format"] == "number"


def test_share_column_whose_values_exceed_100_is_not_percent() -> None:
    """边界：占比列里混进了大于 100 的值，就不可能是百分比了（也不能只信列名）。"""
    spec = ChartSpecService().build(
        rows=_rows([35.0, 120.0, 8.5], "share_percent"),
        columns=["month", "fiber_name", "share_percent"],
        metric_hint="share_percent",
    )

    assert spec["value_format"] == "number"


def test_normalize_value_format_heals_an_old_sum_card() -> None:
    """存量卡片自愈：老判据把 SUM 累加值标成 percent，读取时要按行数据改回 number。"""
    stale = {
        "y_field": "total_percent",
        "value_format": "percent",
        "rows": _rows([27895.9, 24464.7], "total_percent"),
    }

    healed = normalize_value_format(stale)

    assert healed["value_format"] == "number"
    assert healed["rows"] == stale["rows"]  # 只改标记，一个数值都不许动
    assert stale["value_format"] == "percent"  # 不改动原对象


def test_normalize_value_format_keeps_a_real_share_card_percent() -> None:
    spec = {
        "y_field": "share_percent",
        "value_format": "percent",
        "rows": _rows([35.22, 30.89], "share_percent"),
    }

    assert normalize_value_format(spec)["value_format"] == "percent"


def test_normalize_value_format_leaves_a_specless_card_alone() -> None:
    """没有行数据就没法判定 —— 原样返回，不猜。"""
    assert normalize_value_format({}) == {}
    assert (
        normalize_value_format({"y_field": "x", "value_format": "percent"})["value_format"]
        == "percent"
    )
