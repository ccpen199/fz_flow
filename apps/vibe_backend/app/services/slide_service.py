from __future__ import annotations

from uuid import uuid4

from packages.shared_contracts.python_models import QueryPlanDTO, QueryRunDTO, SessionStatus, SlideDraftDTO

from .insight_narrative_service import build_slide_content


def build_slide_from_query(
    session_id: str,
    global_goal: str,
    status: SessionStatus,
    query_plan: QueryPlanDTO | None,
    query_run: QueryRunDTO | None,
) -> SlideDraftDTO:
    """Build the structured page that is handed to TrainPPTAgent for visual editing."""
    content = build_slide_content(
        global_goal=global_goal,
        status=status,
        query_plan=query_plan,
        query_run=query_run,
    )
    chart_spec = content["chart_spec"]
    lineage_summary = {
        "session_id": session_id,
        "insight_service": content["insight_service"],
        "result_profile": content["profile"],
        "key_metrics": content["key_metrics"],
    }
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
