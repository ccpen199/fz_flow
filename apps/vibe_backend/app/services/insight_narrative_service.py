from __future__ import annotations

import ast
from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from statistics import mean
from typing import Any

from packages.shared_contracts.python_models import QueryPlanDTO, QueryRunDTO, SessionStatus

SCHEME_PREFIX = {
    "presenton_ai": "Presenton AI",
}

BAR_FIRST_SCHEMES = set()


def _field_label(value: object) -> str:
    if isinstance(value, dict):
        for key in ("semantic_name", "field", "name", "label", "field_name"):
            text = str(value.get(key) or "").strip()
            if text:
                return text
        desc = str(value.get("desc") or value.get("description") or "").strip()
        return desc or "字段"
    text = str(value or "").strip()
    if text.startswith("{") and text.endswith("}"):
        try:
            parsed = ast.literal_eval(text)
        except (SyntaxError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            return _field_label(parsed)
    return text or "字段"


def _to_float(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _format_value(value: object) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    number = _to_float(value)
    if number is not None:
        if abs(number) >= 1000:
            return f"{number:,.0f}"
        if number.is_integer():
            return f"{number:.0f}"
        return f"{number:.2f}"
    return str(value)


def _detect_page_type(query_plan: QueryPlanDTO | None, query_run: QueryRunDTO | None) -> str:
    if query_run and len(query_run.result_preview) > 0:
        row = query_run.result_preview[0]
        if any("date" in key.lower() or "日期" in key for key in row):
            return "trend"
    if query_plan and len(query_plan.dimensions) >= 2:
        return "comparison"
    if query_plan and any("risk" in note.lower() for note in query_plan.risk_notes):
        return "risk"
    return "overview"


def profile_query_result(query_run: QueryRunDTO | None) -> dict[str, Any]:
    rows = query_run.result_preview if query_run else []
    columns = list(rows[0].keys()) if rows else []
    numeric_columns: list[str] = []
    column_stats: dict[str, dict[str, Any]] = {}
    for column in columns:
        values = [_to_float(row.get(column)) for row in rows]
        numbers = [value for value in values if value is not None]
        if not numbers:
            continue
        numeric_columns.append(column)
        column_stats[column] = {
            "min": min(numbers),
            "max": max(numbers),
            "avg": mean(numbers),
            "non_null": len(numbers),
        }

    primary_numeric = numeric_columns[0] if numeric_columns else None
    top_rows: list[dict[str, Any]] = []
    if primary_numeric:
        top_rows = sorted(rows, key=lambda row: _to_float(row.get(primary_numeric)) or float("-inf"), reverse=True)[:3]

    return {
        "rows_count": query_run.rows_count if query_run else 0,
        "preview_rows": len(rows),
        "columns": columns,
        "numeric_columns": numeric_columns,
        "primary_numeric": primary_numeric,
        "column_stats": column_stats,
        "top_rows": top_rows,
    }


def build_key_metrics(profile: dict[str, Any]) -> list[dict[str, str]]:
    metrics = [
        {"label": "返回行数", "value": _format_value(profile.get("rows_count", 0))},
        {"label": "预览行数", "value": _format_value(profile.get("preview_rows", 0))},
        {"label": "字段数", "value": _format_value(len(profile.get("columns", [])))},
    ]
    primary = profile.get("primary_numeric")
    stats = profile.get("column_stats", {}).get(primary, {}) if primary else {}
    if stats:
        metrics.extend(
            [
                {"label": f"{primary} 最大", "value": _format_value(stats.get("max"))},
                {"label": f"{primary} 均值", "value": _format_value(stats.get("avg"))},
            ]
        )
    return metrics[:5]


def build_chart_spec(page_type: str, query_plan: QueryPlanDTO | None, query_run: QueryRunDTO | None) -> dict[str, Any]:
    if query_run is None:
        return {}
    dimensions = query_plan.dimensions if query_plan else []
    metrics = query_plan.metrics if query_plan else []
    preview = query_run.result_preview[:10]
    x_field = _field_label(dimensions[0]) if dimensions else (next(iter(preview[0].keys())) if preview else None)
    series_field = _field_label(dimensions[1]) if len(dimensions) > 1 else None
    y_field = _field_label(metrics[0]) if metrics else None
    return {
        "chart_type": query_run.chart_suggestion or ("line" if page_type == "trend" else "bar"),
        "x_field": x_field,
        "series_field": series_field,
        "y_field": y_field,
        "rows": preview,
    }


def _build_title(global_goal: str, page_type: str, query_plan: QueryPlanDTO | None, scheme: str) -> tuple[str, str]:
    prefix = SCHEME_PREFIX.get(scheme, "汇报结论")
    if page_type == "comparison":
        dims = " vs ".join(_field_label(item) for item in query_plan.dimensions[:2]) if query_plan else "多维对比"
        return f"{dims}对比分析", f"{prefix} 导出 · {global_goal}"
    if page_type == "trend":
        metric = _field_label(query_plan.metrics[0]) if query_plan and query_plan.metrics else "核心指标"
        return f"{metric}趋势变化", f"{prefix} 导出 · {global_goal}"
    if page_type == "root_cause":
        return "差异驱动因素拆解", f"{prefix} 导出 · {global_goal}"
    if page_type == "risk":
        return "风险与异常提示", f"{prefix} 导出 · {global_goal}"
    if page_type == "summary":
        return "结论、来源与行动计划", f"{prefix} 导出 · {global_goal}"
    return "数据分析汇报页", f"{prefix} 导出 · {global_goal}"


def _fallback_findings(status: SessionStatus, query_plan: QueryPlanDTO | None) -> list[str]:
    return [
        "当前为规则型洞察服务生成的 slide draft",
        f"当前状态：{status}",
        f"当前意图：{query_plan.intent if query_plan else '未生成 QueryPlan'}",
    ]


def _findings(query_run: QueryRunDTO | None, fallback: Sequence[str]) -> list[str]:
    if query_run and query_run.insight_summary:
        return query_run.insight_summary[:3]
    return list(fallback)


def _scheme_narrative(
    *,
    scheme: str,
    global_goal: str,
    query_plan: QueryPlanDTO | None,
    query_run: QueryRunDTO | None,
    profile: dict[str, Any],
    fallback: Sequence[str],
) -> tuple[list[str], str, list[str]]:
    base_findings = _findings(query_run=query_run, fallback=fallback)
    intent = query_plan.intent if query_plan else global_goal
    key_metrics = build_key_metrics(profile)
    metric_line = "；".join(f"{item['label']}={item['value']}" for item in key_metrics[:4])
    primary = profile.get("primary_numeric")
    top_rows = profile.get("top_rows", [])
    columns_text = ", ".join(profile.get("columns", [])[:5]) or "暂无字段"
    top_text = ""
    if primary and top_rows:
        first = top_rows[0]
        label_col = next((col for col in profile.get("columns", []) if col != primary), primary)
        top_text = f"当前最高项：{first.get(label_col, '-')}, {primary}={_format_value(first.get(primary))}"

    if scheme == "presenton_ai":
        findings = [
            base_findings[0] if base_findings else "本轮分析已整理为 AI PPT 生成输入。",
            f"关键数据：{metric_line or '暂无可用统计'}",
            f"来源链路：query={query_run.query_id if query_run else '-'}",
        ]
        narrative = (
            f"本页围绕“{global_goal}”组织分析结果，作为 Presenton 大模型生成 PPT 的结构化输入。"
            f" 当前结果字段包括 {columns_text}，导出阶段会把结论、数据预览和生成指令发送给 Presenton。"
        )
        recommendations = ["核对标题、结论和口径", "批准入 Deck 后调用 Presenton 生成 PPTX", "下载后检查版式与可编辑内容"]
        return findings, narrative, recommendations

    findings = base_findings[:3]
    if not findings or findings == list(fallback):
        fields = ", ".join(profile.get("columns", [])[:4]) or "暂无字段"
        findings = [
            f"围绕“{global_goal}”完成一轮数据分析。",
            f"本次结果返回 {profile.get('rows_count', 0)} 行，主要字段包括 {fields}。",
            "建议结合业务目标确认是否继续做品牌、平台或时间维度下钻。",
        ]
    narrative = (
        f"本页面向管理汇报，围绕“{global_goal}”提炼核心结论。"
        f" 当前结果来自 query {query_run.query_id if query_run else '-'}，"
        f"建议以 {query_run.chart_suggestion if query_run else 'bar'} 图表达主结论。"
    )
    recommendations = ["确认主结论是否符合业务预期", "补充关键品牌/平台对比", "生成下一页原因分析或行动建议"]
    return findings, narrative, recommendations


def build_slide_content(
    *,
    scheme: str,
    scheme_name: str,
    global_goal: str,
    status: SessionStatus,
    query_plan: QueryPlanDTO | None,
    query_run: QueryRunDTO | None,
) -> dict[str, Any]:
    profile = profile_query_result(query_run)
    fallback = _fallback_findings(status=status, query_plan=query_plan)
    page_type = _detect_page_type(query_plan=query_plan, query_run=query_run)
    findings, narrative, recommendations = _scheme_narrative(
        scheme=scheme,
        global_goal=global_goal,
        query_plan=query_plan,
        query_run=query_run,
        profile=profile,
        fallback=fallback,
    )
    title, subtitle = _build_title(global_goal=global_goal, page_type=page_type, query_plan=query_plan, scheme=scheme)
    chart_spec = build_chart_spec(page_type=page_type, query_plan=query_plan, query_run=query_run)
    if scheme in BAR_FIRST_SCHEMES:
        chart_spec["chart_type"] = "bar"
    chart_spec["ppt_scheme"] = scheme
    chart_spec["ppt_scheme_name"] = scheme_name
    return {
        "insight_service": "rule_based_v1",
        "profile": profile,
        "key_metrics": build_key_metrics(profile),
        "page_type": page_type,
        "title": title,
        "subtitle": subtitle,
        "findings": findings,
        "narrative": narrative,
        "recommendations": recommendations,
        "chart_spec": chart_spec,
    }
