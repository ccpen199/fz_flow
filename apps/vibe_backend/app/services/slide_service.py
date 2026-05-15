from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from statistics import mean
from uuid import uuid4

from packages.shared_contracts.python_models import QueryPlanDTO, QueryRunDTO, SessionStatus, SlideDraftDTO
from .insight_narrative_service import build_slide_content

PPT_SCHEMES = {
    "presenton_ai": {
        "name": "Presenton AI PPT 生成",
        "category": "AI PPT 生成器",
        "description": "调用本地或配置的 presenton/presenton 服务，由大模型生成并导出 .pptx。",
        "reference": "presenton/presenton",
    },
}


def normalize_ppt_scheme(scheme: str | None) -> str:
    key = str(scheme or "presenton_ai").strip().lower()
    return key if key in PPT_SCHEMES else "presenton_ai"


def list_ppt_schemes() -> list[dict]:
    return [{"scheme": key, **value} for key, value in PPT_SCHEMES.items()]


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


def _build_chart_spec(page_type: str, query_plan: QueryPlanDTO | None, query_run: QueryRunDTO | None) -> dict:
    if query_run is None:
        return {}
    dimensions = query_plan.dimensions if query_plan else []
    metrics = query_plan.metrics if query_plan else []
    preview = query_run.result_preview[:10]
    x_field = dimensions[0] if dimensions else (next(iter(preview[0].keys())) if preview else None)
    series_field = dimensions[1] if len(dimensions) > 1 else None
    y_field = metrics[0] if metrics else None
    return {
        "chart_type": query_run.chart_suggestion or ("line" if page_type == "trend" else "bar"),
        "x_field": x_field,
        "series_field": series_field,
        "y_field": y_field,
        "rows": preview,
    }


def _build_title(global_goal: str, page_type: str, query_plan: QueryPlanDTO | None, scheme: str) -> tuple[str, str]:
    prefix = {
        "executive": "汇报结论",
        "evidence": "数据证据",
        "dashboard": "指标看板",
    }.get(scheme, "汇报结论")
    if page_type == "comparison":
        dims = " vs ".join(query_plan.dimensions[:2]) if query_plan else "多维对比"
        return f"{prefix}：{dims}对比分析", global_goal
    if page_type == "trend":
        metric = query_plan.metrics[0] if query_plan and query_plan.metrics else "核心指标"
        return f"{prefix}：{metric}趋势变化", global_goal
    if page_type == "risk":
        return f"{prefix}：风险与异常提示", global_goal
    return f"{prefix}：首轮分析页", global_goal


def _build_findings(query_run: QueryRunDTO | None, fallback: Sequence[str]) -> list[str]:
    if query_run and query_run.insight_summary:
        return query_run.insight_summary[:3]
    return list(fallback)


def _is_number(value: object) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float, Decimal)):
        return True
    try:
        float(str(value).replace(",", ""))
        return True
    except (TypeError, ValueError):
        return False


def _to_float(value: object) -> float | None:
    if not _is_number(value):
        return None
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


def _profile_result(query_run: QueryRunDTO | None) -> dict:
    rows = query_run.result_preview if query_run else []
    columns = list(rows[0].keys()) if rows else []
    numeric_columns: list[str] = []
    column_stats: dict[str, dict] = {}
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
    top_rows: list[dict] = []
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


def _key_metrics(profile: dict) -> list[dict]:
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


def _build_scheme_content(
    *,
    scheme: str,
    global_goal: str,
    query_plan: QueryPlanDTO | None,
    query_run: QueryRunDTO | None,
    profile: dict,
    fallback_findings: Sequence[str],
) -> tuple[list[str], str, list[str]]:
    base_findings = _build_findings(query_run=query_run, fallback=fallback_findings)
    intent = query_plan.intent if query_plan else global_goal
    key_metrics = _key_metrics(profile)
    metric_line = "；".join(f"{item['label']}={item['value']}" for item in key_metrics[:4])
    primary = profile.get("primary_numeric")
    top_rows = profile.get("top_rows", [])

    if scheme == "evidence":
        findings = [
            f"查询口径：{intent}",
            f"数据规模：{metric_line or '暂无可用统计'}",
            f"来源链路：query={query_run.query_id if query_run else '-'}；plan={query_plan.query_plan_id if query_plan else '-'}",
        ]
        narrative = (
            "本页优先展示数据证据与可追溯信息。"
            f" SQL 结果返回 {profile.get('rows_count', 0)} 行，预览 {profile.get('preview_rows', 0)} 行，"
            "用于校验字段、聚合和排序是否符合业务口径。"
        )
        recommendations = ["先核对 SQL 与字段口径", "检查样本是否覆盖目标时间窗", "确认后再生成管理汇报页"]
        return findings, narrative, recommendations

    if scheme == "dashboard":
        top_text = ""
        if primary and top_rows:
            first = top_rows[0]
            label_col = next((col for col in profile.get("columns", []) if col != primary), primary)
            top_text = f"当前最高项：{first.get(label_col, '-')}, {primary}={_format_value(first.get(primary))}"
        findings = [
            f"核心指标卡：{metric_line or '暂无可用指标'}",
            top_text or "当前结果适合以表格或基础柱状图展示。",
            f"推荐图表：{query_run.chart_suggestion if query_run else 'bar'}",
        ]
        narrative = (
            "本页按看板方式组织信息，优先呈现指标卡、图表和Top项，"
            "适合快速扫读当前分析结果并决定下一步下钻方向。"
        )
        recommendations = ["按Top项继续下钻", "补充时间维度做趋势验证", "将异常项单独生成下一页"]
        return findings, narrative, recommendations

    findings = base_findings[:3]
    if not findings or findings == list(fallback_findings):
        findings = [
            f"围绕“{global_goal}”完成一轮数据分析。",
            f"本次结果返回 {profile.get('rows_count', 0)} 行，主要字段包括 {', '.join(profile.get('columns', [])[:4]) or '暂无字段'}。",
            "建议结合业务目标确认是否继续做品牌、平台或时间维度下钻。",
        ]
    narrative = (
        f"本页面向管理汇报，围绕“{global_goal}”提炼核心结论。"
        f" 当前结果来自 query {query_run.query_id if query_run else '-'}，"
        f"建议以 {query_run.chart_suggestion if query_run else 'bar'} 图表达主结论。"
    )
    recommendations = ["确认主结论是否符合业务预期", "补充关键品牌/平台对比", "生成下一页原因分析或行动建议"]
    return findings, narrative, recommendations


def build_slide_from_query(
    session_id: str,
    global_goal: str,
    status: SessionStatus,
    query_plan: QueryPlanDTO | None,
    query_run: QueryRunDTO | None,
    scheme: str | None = None,
) -> SlideDraftDTO:
    scheme_key = normalize_ppt_scheme(scheme)
    content = build_slide_content(
        scheme=scheme_key,
        scheme_name=PPT_SCHEMES[scheme_key]["name"],
        global_goal=global_goal,
        status=status,
        query_plan=query_plan,
        query_run=query_run,
    )
    lineage_summary = {
        "session_id": session_id,
        "ppt_scheme": scheme_key,
        "ppt_scheme_name": PPT_SCHEMES[scheme_key]["name"],
        "insight_service": content["insight_service"],
        "result_profile": content["profile"],
        "key_metrics": content["key_metrics"],
    }

    chart_spec = content["chart_spec"]

    if query_run is not None:
        lineage_summary.update(
            {
                "query_id": query_run.query_id,
                "rows_count": query_run.rows_count,
                "chart_type": chart_spec.get("chart_type"),
            }
        )
    return SlideDraftDTO(
        slide_id=f"slide_{uuid4().hex[:10]}",
        session_id=session_id,
        query_id=query_run.query_id if query_run else None,
        page_type=content["page_type"],
        title=content["title"],
        subtitle=content["subtitle"],
        chart_spec=chart_spec,
        findings=content["findings"],
        narrative=content["narrative"],
        recommendations=content["recommendations"],
        lineage_summary=lineage_summary,
    )
