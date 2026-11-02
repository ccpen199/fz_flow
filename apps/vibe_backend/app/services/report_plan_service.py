from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from packages.shared_contracts.python_models import (
    ReportChartBindingDTO,
    ReportPageDTO,
    ReportPlanDTO,
    SceneDTO,
    SlideDraftDTO,
)


SUPPORTED_CHART_TYPES = {"bar", "column", "line", "area", "pie", "ring", "radar", "scatter", "table", "none"}


def _now() -> datetime:
    return datetime.now(UTC)


def _role(field: Any) -> str:
    value = getattr(field, "role", "")
    return str(getattr(value, "value", value) or "").strip().lower()


def _field_names(scene: SceneDTO, role: str) -> list[str]:
    return [
        str(field.semantic_name).strip()
        for field in scene.fields
        if getattr(field, "enabled", True) and _role(field) == role and str(field.semantic_name).strip()
    ]


def _topic(intent: str) -> str:
    text = re.sub(r"\s+", " ", str(intent or "").strip())
    return text[:42] or "业务数据分析"


def _scope_from_intent(intent: str, scene: SceneDTO) -> dict[str, Any]:
    text = str(intent or "")
    time_matches = re.findall(r"(?:20\d{2}年?|最近\s*\d+\s*(?:天|周|个月|月)|近\s*\d+\s*(?:天|周|个月|月))", text)
    return {
        "business_object": _topic(text),
        "time_range": time_matches[0] if time_matches else "",
        "available_metrics": _field_names(scene, "metric"),
        "available_dimensions": _field_names(scene, "dimension"),
        "available_time_fields": _field_names(scene, "time"),
    }


class ReportPlanService:
    """Structured report planning adapted from TrainPPTAgent's outline/chart model.

    The original TrainPPTAgent outline is markdown-first.  FZ needs an auditable
    data workflow, so each outline node is represented as a page with explicit
    metric, dimension, chart and filter contracts before any SQL is generated.
    """

    def create_plan(self, *, session_id: str, scene: SceneDTO, intent: str) -> ReportPlanDTO:
        metric_fields = _field_names(scene, "metric")
        dimension_fields = _field_names(scene, "dimension")
        time_fields = _field_names(scene, "time")
        metric = metric_fields[0] if metric_fields else "记录数"
        primary_dimension = dimension_fields[0] if dimension_fields else "业务对象"
        secondary_dimension = dimension_fields[1] if len(dimension_fields) > 1 else primary_dimension
        time_field = time_fields[0] if time_fields else "时间"
        scope = _scope_from_intent(intent, scene)
        scope_label = scope.get("time_range") or "全量可用数据"
        now = _now()

        def page(
            *,
            order: int,
            page_kind: str,
            page_type: str,
            title: str,
            header: str,
            objective: str,
            metrics: list[str],
            dimensions: list[str],
            chart_type: str,
            label_field: str = "",
            value_fields: list[str] | None = None,
        ) -> ReportPageDTO:
            return ReportPageDTO(
                page_id=f"report_page_{uuid4().hex[:10]}",
                order=order,
                page_kind=page_kind,  # type: ignore[arg-type]
                page_type=page_type,  # type: ignore[arg-type]
                title=title,
                table_header=header,
                objective=objective,
                metrics=metrics,
                dimensions=dimensions,
                time_window=str(scope.get("time_range") or ""),
                chart_binding=ReportChartBindingDTO(
                    chart_type=chart_type,  # type: ignore[arg-type]
                    label_field=label_field,
                    value_fields=value_fields or [],
                    max_rows=12,
                ),
                updated_at=now,
            )

        topic = _topic(intent)
        pages = [
            page(
                order=1,
                page_kind="text",
                page_type="overview",
                title=topic,
                header="分析主题、范围与口径",
                objective=f"明确本报告围绕“{topic}”的分析范围和页面结构；数据范围为{scope_label}。",
                metrics=[],
                dimensions=[],
                chart_type="none",
            ),
            page(
                order=2,
                page_kind="chart",
                page_type="comparison",
                title=f"{primary_dimension}核心表现",
                header=f"{primary_dimension}｜{metric}",
                objective=f"比较不同{primary_dimension}的{metric}，识别主要贡献项与差异。",
                metrics=[metric],
                dimensions=[primary_dimension],
                chart_type="bar",
                label_field=primary_dimension,
                value_fields=[metric],
            ),
            page(
                order=3,
                page_kind="chart",
                page_type="trend",
                title=f"{metric}时间趋势",
                header=f"{time_field}｜{metric}",
                objective=f"观察{metric}随{time_field}的变化，定位趋势、拐点与异常波动。",
                metrics=[metric],
                dimensions=[time_field],
                chart_type="line",
                label_field=time_field,
                value_fields=[metric],
            ),
            page(
                order=4,
                page_kind="chart",
                page_type="comparison",
                title=f"{secondary_dimension}结构分布",
                header=f"{secondary_dimension}｜{metric}",
                objective=f"查看{secondary_dimension}的结构占比和集中度，为后续下钻确定重点。",
                metrics=[metric],
                dimensions=[secondary_dimension],
                chart_type="pie",
                label_field=secondary_dimension,
                value_fields=[metric],
            ),
            page(
                order=5,
                page_kind="text",
                page_type="summary",
                title="结论与下一步",
                header="数据洞察与业务建议",
                objective="汇总已确认页面中的真实数据结论，并形成可人工编辑的后续行动建议。",
                metrics=[],
                dimensions=[],
                chart_type="none",
            ),
        ]
        return ReportPlanDTO(
            report_plan_id=f"report_{uuid4().hex[:10]}",
            session_id=session_id,
            title=topic,
            intent=intent.strip(),
            analysis_scope=scope,
            pages=pages,
            active_page_id=pages[0].page_id,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def get_page(plan: ReportPlanDTO, page_id: str) -> ReportPageDTO:
        page = next((item for item in plan.pages if item.page_id == page_id), None)
        if page is None:
            raise ValueError("report page not found")
        return page

    def update_page(self, plan: ReportPlanDTO, page_id: str, patch: dict[str, Any]) -> ReportPageDTO:
        page = self.get_page(plan, page_id)
        for name in ("title", "table_header", "objective", "time_window"):
            if name in patch and patch[name] is not None:
                setattr(page, name, str(patch[name]).strip())
        for name in ("metrics", "dimensions", "filters"):
            if name in patch and patch[name] is not None:
                value = patch[name]
                setattr(page, name, value if isinstance(value, list) else [])
        if "page_kind" in patch and patch["page_kind"] in {"text", "chart", "table"}:
            page.page_kind = patch["page_kind"]
        if "page_type" in patch and patch["page_type"] in {"overview", "comparison", "trend", "root_cause", "risk", "summary"}:
            page.page_type = patch["page_type"]
        binding_patch = patch.get("chart_binding")
        if isinstance(binding_patch, dict):
            binding = page.chart_binding
            chart_type = str(binding_patch.get("chart_type") or binding.chart_type).strip().lower()
            if chart_type in SUPPORTED_CHART_TYPES:
                binding.chart_type = chart_type  # type: ignore[assignment]
            for name in ("label_field", "series_field"):
                if name in binding_patch and binding_patch[name] is not None:
                    setattr(binding, name, str(binding_patch[name]).strip())
            for name in ("value_fields", "filters"):
                if name in binding_patch and binding_patch[name] is not None:
                    value = binding_patch[name]
                    setattr(binding, name, value if isinstance(value, list) else [])
            if "max_rows" in binding_patch:
                try:
                    binding.max_rows = min(100, max(1, int(binding_patch["max_rows"])))
                except (TypeError, ValueError):
                    pass
        page.stage = "outline_draft" if page.stage == "outline_draft" else "sql_draft"
        page.updated_at = _now()
        plan.updated_at = page.updated_at
        plan.version += 1
        return page

    def add_page(self, plan: ReportPlanDTO) -> ReportPageDTO:
        now = _now()
        page = ReportPageDTO(
            page_id=f"report_page_{uuid4().hex[:10]}",
            order=len(plan.pages) + 1,
            page_kind="chart",
            page_type="comparison",
            title="新增分析页面",
            table_header="维度｜指标",
            objective="定义本页要回答的业务问题、指标、维度和图表。",
            chart_binding=ReportChartBindingDTO(chart_type="bar"),
            updated_at=now,
        )
        plan.pages.append(page)
        plan.active_page_id = page.page_id
        plan.updated_at = now
        plan.version += 1
        return page

    def delete_page(self, plan: ReportPlanDTO, page_id: str) -> None:
        if len(plan.pages) <= 1:
            raise ValueError("report plan must retain at least one page")
        page = self.get_page(plan, page_id)
        plan.pages = [item for item in plan.pages if item.page_id != page.page_id]
        for index, item in enumerate(plan.pages, start=1):
            item.order = index
        if plan.active_page_id == page_id:
            plan.active_page_id = plan.pages[0].page_id
        plan.updated_at = _now()
        plan.version += 1

    def build_page_intent(self, plan: ReportPlanDTO, page: ReportPageDTO) -> str:
        parts = [
            f"报告主题：{plan.intent}",
            f"PPT 第{page.order}页：{page.title}",
            f"页面目标：{page.objective}",
        ]
        if page.metrics:
            parts.append("指标：" + "、".join(page.metrics))
        if page.dimensions:
            parts.append("维度：" + "、".join(page.dimensions))
        if page.time_window:
            parts.append("时间范围：" + page.time_window)
        if page.filters:
            parts.append("筛选条件：" + "; ".join(str(item) for item in page.filters))
        parts.append("请只生成支持该页图表的数据查询，字段别名与页面表头保持一致。")
        return "\n".join(parts)

    def generate_insight(self, plan: ReportPlanDTO, page: ReportPageDTO) -> tuple[list[str], str]:
        rows = page.result_preview[: max(1, page.chart_binding.max_rows)]
        if page.page_kind == "text":
            completed = [item for item in plan.pages if item.stage in {"insight_ready", "approved"} and item.findings]
            findings = [finding for item in completed for finding in item.findings][:3]
            if not findings:
                return [], "请先完成图表页面的数据确认，再基于真实结果生成本页总结。"
            return findings, "本页汇总已确认页面中的真实数据结论；可在下方继续人工修改。"
        if not rows:
            return [], "本页尚未取得可确认的数据，不能生成数据洞察。"

        binding = page.chart_binding
        label_field = binding.label_field or (page.dimensions[0] if page.dimensions else "")
        value_field = binding.value_fields[0] if binding.value_fields else (page.metrics[0] if page.metrics else "")
        numeric: list[tuple[str, float, Any]] = []
        for row in rows:
            raw_value = row.get(value_field) if value_field else None
            try:
                value = float(str(raw_value).replace(",", "").replace("%", ""))
            except (TypeError, ValueError):
                continue
            numeric.append((str(row.get(label_field, "-")), value, raw_value))
        if not numeric:
            return [], f"已取得 {len(rows)} 条数据，但“{value_field or '数值字段'}”不是可计算数值；请先调整图表字段绑定。"
        numeric.sort(key=lambda item: item[1], reverse=True)
        top_label, top_value, top_raw = numeric[0]
        total = sum(item[1] for item in numeric)
        share = (top_value / total * 100) if total else 0
        findings = [f"{top_label}的{value_field}最高，为{top_raw}。"]
        if len(numeric) > 1 and total:
            findings.append(f"前{min(3, len(numeric))}项合计占当前图表范围的{sum(item[1] for item in numeric[:3]) / total * 100:.1f}%。")
        if len(numeric) > 1:
            findings.append(f"最高项与最低项相差{top_value - numeric[-1][1]:.2f}，需结合业务口径判断差异原因。")
        narrative = f"基于本页已确认的{len(rows)}条查询结果，{top_label}在{value_field}上居首，占当前展示范围约{share:.1f}%。"
        return findings, narrative

    def to_slide(self, *, session_id: str, page: ReportPageDTO) -> SlideDraftDTO:
        binding = page.chart_binding
        chart_spec = {
            "chart_type": binding.chart_type,
            "x_field": binding.label_field,
            "y_field": binding.value_fields[0] if binding.value_fields else "",
            "series_field": binding.series_field,
            "max_rows": binding.max_rows,
            "filters": binding.filters,
            "rows": page.result_preview[: binding.max_rows],
            "table_header": page.table_header,
        }
        return SlideDraftDTO(
            slide_id=page.slide_id or f"slide_{uuid4().hex[:10]}",
            session_id=session_id,
            task_id=page.page_id,
            query_id=page.query_id,
            page_type=page.page_type,
            title=page.title,
            subtitle=page.table_header,
            chart_spec=chart_spec,
            findings=page.findings,
            narrative=page.narrative,
            recommendations=[],
            lineage_summary={"report_page_id": page.page_id, "query_id": page.query_id, "rows_count": page.rows_count},
            status="approved" if page.stage == "approved" else "pending_review",
        )


report_plan_service = ReportPlanService()
