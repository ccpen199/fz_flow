"""结论文案里不许漏出 `None`。

实测（2026-10-18，会话 `conv_406f167b72`）：单行结果的 x 值是 `None`，
`str(row.get(field, "-"))` 把它变成字符串 "None"，用户看到
「None 在sku_cnt上累计占比最高，约 100.0%」。
"""

from __future__ import annotations

from app.services.chart_spec_service import ChartSpecService


def _service() -> ChartSpecService:
    return ChartSpecService()


def test_single_series_label_never_prints_none() -> None:
    narrative = _service().narrative(
        title="上新节奏与趋势",
        spec={
            "chart_type": "bar",
            "x_field": "日期",
            "y_field": "sku_cnt",
            "series_field": "",
            "rows": [{"日期": None, "sku_cnt": 4}],
            "findings": _service().findings(
                rows=[{"日期": None, "sku_cnt": 4}],
                x_field="日期",
                y_field="sku_cnt",
                series_field="",
                chart_type="bar",
            ),
            "total_rows": 1,
        },
    )

    assert "None" not in narrative
    assert "- 的sku_cnt最高，为 4" in narrative


def test_series_name_never_prints_none() -> None:
    findings = _service()._series_findings(
        rows=[{"类目": None, "占比": 60.0}, {"类目": None, "占比": 40.0}],
        y_field="占比",
        series_field="类目",
    )

    assert findings
    assert all("None" not in item for item in findings)
