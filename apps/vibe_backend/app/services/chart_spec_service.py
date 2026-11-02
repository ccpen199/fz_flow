from __future__ import annotations

import re
from typing import Any


TIME_HINTS = ("日期", "时间", "月份", "年月", "date", "time", "month", "day")
SHARE_HINTS = ("占比", "比例", "percent", "rate", "share", "覆盖率")
ID_HINTS = ("id", "编码", "code", "编号")
NUMBER_PATTERN = re.compile(r"^-?[\d,]+(\.\d+)?%?$")

# 图表卡里最多带多少行。这是**载荷保险丝**，不是**展示上限**。
#
# 以前是 60，而 agent 写的 SQL 常按 `ORDER BY 时间, 值 DESC` 排 —— 一刀切下去
# 把 101 个月的纤维数据砍成最早的 6 个月，图上显示的"趋势"其实只是开头一小段
# （2026-10-12 真实事故：用户以为整条趋势画错了）。
DEFAULT_MAX_ROWS = 3000

# 截断时最多让「当前类目」超出上限的倍数 —— 防止某个类目自己就有几万行。
CATEGORY_GRACE = 2


def _limit_by_category(
    rows: list[dict[str, Any]], x_field: str, max_rows: int
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按**整类目**截断：绝不把同一个 x 值切成半截。

    保留的永远是原结果的**前缀**（只裁尾部，不重排、不改值），
    所以「画布 = MySQL 原样数据」这条守卫照旧成立。
    返回 (保留的行, 截断声明)；没有截断时声明为空 dict。
    """
    if not x_field or len(rows) <= max_rows:
        return list(rows), {}

    kept: list[dict[str, Any]] = []
    seen: set[Any] = set()
    cut_inside_category = False
    for row in rows:
        key = row.get(x_field)
        if key not in seen:
            if seen and len(kept) >= max_rows:
                break
            seen.add(key)
        elif len(kept) >= max_rows * CATEGORY_GRACE:
            # 极端情况：单个类目自己的行数就超上限若干倍，只能在类目内部裁断，
            # 如实声明，否则载荷无界。
            cut_inside_category = True
            break
        kept.append(row)

    return kept, {
        "kept_rows": len(kept),
        "kept_categories": len(seen),
        "total_categories": len({row.get(x_field) for row in rows}),
        "omitted_rows": len(rows) - len(kept),
        "cut_inside_category": cut_inside_category,
    }


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "").replace("%", "")
    if not text or not NUMBER_PATTERN.match(str(value).strip().replace(",", "")):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _label_text(value: Any) -> str:
    """类目名 / 序列名：`None` 与空串一律写成占位符。

    `str(row.get(field, "-"))` 在键存在但值为 `None` 时得到字符串 "None"，
    会直接漏进用户看的结论文案（实测：「None 在sku_cnt上累计占比最高」）。
    """
    text = "" if value is None else str(value).strip()
    return text or "-"


def _format(value: Any) -> str:
    number = _to_float(value)
    if number is None:
        return str(value if value is not None else "-")
    if abs(number) >= 1000:
        return f"{number:,.0f}"
    if float(number).is_integer():
        return f"{number:.0f}"
    return f"{number:.2f}"


def _is_numeric_column(rows: list[dict[str, Any]], column: str) -> bool:
    values = [row.get(column) for row in rows]
    numeric = [value for value in values if _to_float(value) is not None]
    return bool(numeric) and len(numeric) >= max(1, int(len(values) * 0.7))


def _looks_like(value: str, hints: tuple[str, ...]) -> bool:
    lowered = str(value or "").lower()
    return any(hint in lowered for hint in hints)


def is_percentage_column(rows: list[dict[str, Any]], column: str) -> bool:
    """只有当「列名像占比」**且**「值域真的落在 0–100」时，才把它当百分数。

    为什么不能只看列名：agent 写的 SQL 常把 `SUM(Percent)` 别名成 `total_percent`
    —— 名字里有 "percent"，但那是**累加值**（实测这个库里能到 27895.9），不是百分比。
    只看列名会给它贴上 "%"，画布上就出现 "20000%" 这种读不通的 y 轴刻轴。

    值域判定是**可核对的事实**而不是猜测：一个大于 100 的列不可能是百分比。
    约定：全链路 percent 值一律是 0–100 的数（前端 agent.js 只补 "%"，不再乘 100）。
    """
    if not _looks_like(column, SHARE_HINTS):
        return False
    numbers = [_to_float(row.get(column)) for row in rows]
    numbers = [number for number in numbers if number is not None]
    if not numbers:
        return False
    return max(abs(number) for number in numbers) <= 100.0


class ChartSpecService:
    """Turns a query result into one explicit, renderer-agnostic chart contract.

    The very same spec feeds the in-canvas ECharts renderer and the PPTX
    exporter, so a chart never has two divergent definitions.
    """

    def build(
        self,
        *,
        rows: list[dict[str, Any]],
        columns: list[str] | None = None,
        chart_hint: str = "",
        metric_hint: str = "",
        dimension_hint: str = "",
        title: str = "",
        max_rows: int = DEFAULT_MAX_ROWS,
    ) -> dict[str, Any]:
        clean_rows = [row for row in (rows or []) if isinstance(row, dict)]
        columns = columns or (list(clean_rows[0].keys()) if clean_rows else [])
        columns = [str(column) for column in columns]

        measures = [column for column in columns if _is_numeric_column(clean_rows, column)]
        categorical = [column for column in columns if column not in measures]

        # A measure that is really an identifier reads better as a label.
        named_measures = [column for column in measures if not _looks_like(column, ID_HINTS)]
        primary_measure = self._pick(measures, named_measures, metric_hint)

        time_columns = [column for column in categorical if _looks_like(column, TIME_HINTS)]
        other_dimensions = [column for column in categorical if column not in time_columns]

        x_field = ""
        if time_columns:
            x_field = time_columns[0]
        elif other_dimensions:
            x_field = other_dimensions[0]

        remaining = [column for column in other_dimensions if column != x_field]
        series_field = self._pick(remaining, remaining, dimension_hint) if remaining else ""
        if series_field == x_field:
            series_field = ""

        chart_type = self._decide_chart_type(
            hint=chart_hint,
            x_field=x_field,
            y_field=primary_measure,
            has_series=bool(series_field),
            row_count=len(clean_rows),
        )

        limited, truncation = _limit_by_category(clean_rows, x_field, max(1, max_rows))
        findings = self.findings(
            rows=limited,
            x_field=x_field,
            y_field=primary_measure,
            series_field=series_field,
            chart_type=chart_type,
        )
        return {
            "chart_type": chart_type,
            "x_field": x_field,
            "y_field": primary_measure,
            "series_field": series_field,
            "columns": columns,
            "rows": limited,
            "total_rows": len(clean_rows),
            "truncation": truncation,
            "table_header": " ｜ ".join(item for item in [x_field, primary_measure] if item),
            "title": title,
            "value_format": "percent" if is_percentage_column(limited, primary_measure) else "number",
            "stack": chart_type in {"area", "column"} and bool(series_field),
            "findings": findings,
        }

    @staticmethod
    def _pick(candidates: list[str], preferred: list[str], hint: str) -> str:
        if hint:
            for column in candidates:
                if str(column) == hint:
                    return column
            for column in candidates:
                if hint and hint in str(column):
                    return column
        return preferred[0] if preferred else (candidates[0] if candidates else "")

    @staticmethod
    def _decide_chart_type(
        *,
        hint: str,
        x_field: str,
        y_field: str,
        has_series: bool,
        row_count: int,
    ) -> str:
        """Match the most suitable chart for the data shape (flowchart: 匹配最合适图表)."""
        allowed = {"bar", "column", "line", "area", "pie", "ring", "radar", "scatter", "table"}
        hint = str(hint or "").lower()
        if x_field and _looks_like(x_field, TIME_HINTS):
            if has_series:
                return "area"
            return "line"
        if _looks_like(y_field, SHARE_HINTS) and not has_series and 2 <= row_count <= 8:
            return "pie"
        if hint == "stacked_area":
            return "area"
        if hint in allowed:
            if hint == "pie" and (has_series or row_count > 10):
                return "bar"
            if hint == "area" and not (x_field and _looks_like(x_field, TIME_HINTS)):
                return "column"
            return hint
        if not y_field and row_count:
            return "table"
        return "column"

    def findings(
        self,
        *,
        rows: list[dict[str, Any]],
        x_field: str,
        y_field: str,
        series_field: str,
        chart_type: str,
    ) -> list[str]:
        if not rows or not y_field:
            return []
        if series_field:
            return self._series_findings(rows=rows, y_field=y_field, series_field=series_field)

        pairs: list[tuple[str, float, Any]] = []
        for row in rows:
            value = _to_float(row.get(y_field))
            if value is None:
                continue
            label = _label_text(row.get(x_field)) if x_field else "-"
            pairs.append((label, value, row.get(y_field)))
        if not pairs:
            return []

        results: list[str] = []
        if chart_type == "line" and len(pairs) >= 2:
            first, last = pairs[0][1], pairs[-1][1]
            direction = "上升" if last > first else ("下降" if last < first else "持平")
            change = abs(last - first) / abs(first) * 100 if first else 0
            results.append(f"{pairs[0][0]} 至 {pairs[-1][0]}，{y_field}整体{direction} {change:.1f}%。")
            peak = max(pairs, key=lambda item: item[1])
            results.append(f"{peak[0]} 达到区间峰值 {_format(peak[2])}。")
            return results

        ranked = sorted(pairs, key=lambda item: item[1], reverse=True)
        total = sum(item[1] for item in ranked)
        top_label, top_value, top_raw = ranked[0]
        results.append(f"{top_label} 的{y_field}最高，为 {_format(top_raw)}。")
        if total and len(ranked) > 2:
            share = sum(item[1] for item in ranked[:3]) / total * 100
            results.append(f"前 {min(3, len(ranked))} 项合计占当前范围的 {share:.1f}%。")
        if len(ranked) > 1:
            results.append(
                f"最高项与最低项相差 {_format(top_value - ranked[-1][1])}，需结合业务口径判断差异原因。"
            )
        return results

    def _series_findings(
        self,
        *,
        rows: list[dict[str, Any]],
        y_field: str,
        series_field: str,
    ) -> list[str]:
        buckets: dict[str, list[float]] = {}
        for row in rows:
            name = _label_text(row.get(series_field))
            value = _to_float(row.get(y_field))
            if value is None:
                continue
            buckets.setdefault(name, []).append(value)
        if not buckets:
            return []
        totals = {name: sum(values) for name, values in buckets.items()}
        ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
        total = sum(totals.values())
        results = []
        if total:
            share = ranked[0][1] / total * 100
            results.append(f"{ranked[0][0]} 在{y_field}上累计占比最高，约 {share:.1f}%。")
        movers: list[tuple[str, float]] = []
        for name, values in buckets.items():
            if len(values) >= 2 and values[0]:
                movers.append((name, (values[-1] - values[0]) / abs(values[0]) * 100))
        if movers:
            movers.sort(key=lambda item: item[1], reverse=True)
            up_name, up_value = movers[0]
            down_name, down_value = movers[-1]
            if up_value > 0:
                results.append(f"{up_name} 首尾环比上升 {up_value:.1f}%，是当前趋势最明显的成分。")
            if down_value < 0:
                results.append(f"{down_name} 同期回落 {abs(down_value):.1f}%。")
        return results

    def narrative(self, *, title: str, spec: dict[str, Any]) -> str:
        findings = spec.get("findings") or []
        if not findings:
            return f"{title}：本次查询未返回可解读的数值结果，请确认字段绑定与筛选条件。"
        return f"{title}。{'；'.join(findings[:2])} 该结论基于 {spec.get('total_rows', 0)} 条查询结果。"


def normalize_value_format(spec: dict[str, Any]) -> dict[str, Any]:
    """按行数据校正 `value_format`（**只改标记，不动任何数值**）。

    存量卡片是旧判据（只看列名）写下的：`total_percent` 这种 `SUM(...)` 累加值
    也被标成了 "percent"，画布就给它贴 "%"，y 轴出现 "20000%" 这种读不通的刻轴。
    这里在**读取时**用新判据重算一次，老卡片不用重跑也能自愈。

    行数据缺失时**原样返回** —— 判据只能来自行数据，不猜。
    """
    rows = spec.get("rows")
    y_field = spec.get("y_field")
    if not isinstance(rows, list) or not rows or not y_field:
        return spec
    corrected = dict(spec)
    corrected["value_format"] = (
        "percent" if is_percentage_column(rows, str(y_field)) else "number"
    )
    return corrected


chart_spec_service = ChartSpecService()
