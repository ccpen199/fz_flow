from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.chart import XL_CHART_TYPE
from pptx.enum.shapes import MSO_AUTO_SHAPE_TYPE
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

from packages.shared_contracts.python_models import DeckDTO, SlideDraftDTO


EXPORT_ROOT = Path("runtime_data/report_exports")
FONT_NAME = "Microsoft YaHei"
COLORS = {
    "background": RGBColor(13, 27, 42),
    "panel": RGBColor(23, 48, 71),
    "primary": RGBColor(41, 121, 226),
    "accent": RGBColor(49, 184, 166),
    "text": RGBColor(240, 246, 252),
    "muted": RGBColor(176, 197, 217),
}


def _inches(value: float):
    return Inches(value)


def _text(value: object, fallback: str = "") -> str:
    text = str(value if value is not None else fallback).strip()
    return text or fallback


def _number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _format_value(value: object) -> str:
    number = _number(value)
    if number is None:
        return _text(value, "-")
    if float(number).is_integer():
        return f"{number:,.0f}"
    return f"{number:,.2f}"


def _fill(shape, color: RGBColor) -> None:
    shape.fill.solid()
    shape.fill.fore_color.rgb = color


def _add_text(slide, x: float, y: float, w: float, h: float, text: str, *, size: float, color: RGBColor, bold: bool = False, align=None) -> None:
    shape = slide.shapes.add_textbox(_inches(x), _inches(y), _inches(w), _inches(h))
    frame = shape.text_frame
    frame.clear()
    frame.word_wrap = True
    frame.margin_left = _inches(0.04)
    frame.margin_right = _inches(0.04)
    frame.margin_top = _inches(0.03)
    for index, line in enumerate((text or "").splitlines() or [""]):
        paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
        if align is not None:
            paragraph.alignment = align
        run = paragraph.add_run()
        run.text = line
        run.font.name = FONT_NAME
        run.font.size = Pt(size)
        run.font.bold = bold
        run.font.color.rgb = color


def _add_panel(slide, x: float, y: float, w: float, h: float) -> None:
    shape = slide.shapes.add_shape(MSO_AUTO_SHAPE_TYPE.ROUNDED_RECTANGLE, _inches(x), _inches(y), _inches(w), _inches(h))
    _fill(shape, COLORS["panel"])
    shape.line.color.rgb = COLORS["primary"]
    shape.line.transparency = 55


def _chart_data(draft: SlideDraftDTO) -> tuple[list[str], str, list[float]]:
    spec = draft.chart_spec if isinstance(draft.chart_spec, dict) else {}
    rows = spec.get("rows") if isinstance(spec.get("rows"), list) else []
    rows = [row for row in rows if isinstance(row, dict)]
    if not rows:
        return [], "", []
    columns = list(rows[0].keys())
    preferred_x = _text(spec.get("x_field"))
    preferred_y = _text(spec.get("y_field"))
    y_field = preferred_y if preferred_y in columns and any(_number(row.get(preferred_y)) is not None for row in rows) else next((column for column in columns if any(_number(row.get(column)) is not None for row in rows)), "")
    if not y_field:
        return [], "", []
    x_field = preferred_x if preferred_x in columns and preferred_x != y_field else next((column for column in columns if column != y_field), "")
    labels: list[str] = []
    values: list[float] = []
    for index, row in enumerate(rows[:12], start=1):
        value = _number(row.get(y_field))
        if value is None:
            continue
        labels.append(_text(row.get(x_field), f"数据 {index}")[:30])
        values.append(value)
    return labels, y_field, values


def _add_chart(slide, draft: SlideDraftDTO) -> bool:
    labels, series_name, values = _chart_data(draft)
    if not labels or not values:
        return False
    spec = draft.chart_spec if isinstance(draft.chart_spec, dict) else {}
    chart_type = _text(spec.get("chart_type"), "bar").lower()
    chart_map = {
        "bar": XL_CHART_TYPE.BAR_CLUSTERED,
        "column": XL_CHART_TYPE.COLUMN_CLUSTERED,
        "line": XL_CHART_TYPE.LINE_MARKERS,
        "area": XL_CHART_TYPE.AREA,
        "pie": XL_CHART_TYPE.PIE,
        "ring": XL_CHART_TYPE.DOUGHNUT,
        "radar": XL_CHART_TYPE.RADAR,
        "scatter": XL_CHART_TYPE.LINE_MARKERS,
    }
    data = CategoryChartData()
    data.categories = labels
    data.add_series(series_name or "指标", values)
    chart_shape = slide.shapes.add_chart(chart_map.get(chart_type, XL_CHART_TYPE.COLUMN_CLUSTERED), _inches(6.2), _inches(1.65), _inches(6.55), _inches(3.55), data)
    chart = chart_shape.chart
    chart.has_legend = chart_type in {"pie", "ring"}
    chart.chart_title.has_text_frame = True
    chart.chart_title.text_frame.text = draft.subtitle or series_name
    if chart_type not in {"pie", "ring"}:
        chart.category_axis.tick_labels.font.size = Pt(8)
        chart.value_axis.tick_labels.font.size = Pt(8)
    return True


def _add_data_table(slide, draft: SlideDraftDTO) -> None:
    spec = draft.chart_spec if isinstance(draft.chart_spec, dict) else {}
    rows = spec.get("rows") if isinstance(spec.get("rows"), list) else []
    rows = [row for row in rows if isinstance(row, dict)][:6]
    if not rows:
        return
    columns = list(rows[0].keys())[:5]
    if not columns:
        return
    shape = slide.shapes.add_table(len(rows) + 1, len(columns), _inches(6.2), _inches(5.4), _inches(6.55), _inches(1.35))
    table = shape.table
    for col_index, column in enumerate(columns):
        table.columns[col_index].width = int(_inches(6.55 / len(columns)))
        cell = table.cell(0, col_index)
        _fill(cell, COLORS["primary"])
        cell.text = column
        for paragraph in cell.text_frame.paragraphs:
            for run in paragraph.runs:
                run.font.name = FONT_NAME
                run.font.size = Pt(7)
                run.font.bold = True
                run.font.color.rgb = COLORS["text"]
    for row_index, row in enumerate(rows, start=1):
        for col_index, column in enumerate(columns):
            cell = table.cell(row_index, col_index)
            _fill(cell, COLORS["background"] if row_index % 2 else COLORS["panel"])
            cell.text = _format_value(row.get(column))
            for paragraph in cell.text_frame.paragraphs:
                for run in paragraph.runs:
                    run.font.name = FONT_NAME
                    run.font.size = Pt(6.7)
                    run.font.color.rgb = COLORS["text"]


def _add_slide(prs: Presentation, draft: SlideDraftDTO, index: int) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    background = slide.shapes.add_shape(MSO_AUTO_SHAPE_TYPE.RECTANGLE, 0, 0, prs.slide_width, prs.slide_height)
    _fill(background, COLORS["background"])
    background.line.fill.background()
    _add_text(slide, 0.55, 0.32, 11.9, 0.42, _text(draft.title, "数据分析"), size=23, color=COLORS["text"], bold=True)
    _add_text(slide, 0.56, 0.78, 11.8, 0.28, _text(draft.subtitle), size=10, color=COLORS["muted"])
    findings = [item for item in draft.findings if _text(item)]
    _add_panel(slide, 0.55, 1.55, 5.25, 2.25)
    _add_text(slide, 0.78, 1.75, 4.7, 0.25, "数据洞察", size=11, color=COLORS["accent"], bold=True)
    _add_text(slide, 0.8, 2.08, 4.55, 1.45, "\n".join(f"• {_text(item)}" for item in findings[:4]) or "请根据已确认数据补充本页结论。", size=11, color=COLORS["text"])
    _add_panel(slide, 0.55, 4.08, 5.25, 2.1)
    _add_text(slide, 0.78, 4.3, 4.7, 0.25, "业务解读", size=11, color=COLORS["accent"], bold=True)
    _add_text(slide, 0.8, 4.62, 4.55, 1.22, _text(draft.narrative, "暂无业务解读。"), size=10.5, color=COLORS["text"])
    if not _add_chart(slide, draft):
        _add_panel(slide, 6.2, 1.65, 6.55, 4.7)
        _add_text(slide, 6.5, 2.05, 5.95, 0.28, "本页说明", size=12, color=COLORS["accent"], bold=True)
        _add_text(slide, 6.5, 2.5, 5.8, 2.8, _text(draft.narrative, "该文字页不需要单独图表。"), size=16, color=COLORS["text"])
    else:
        _add_data_table(slide, draft)
    _add_text(slide, 0.55, 7.04, 12.2, 0.16, f"{index}  |  数据来源 query={draft.query_id or '-'}", size=7, color=COLORS["muted"], align=PP_ALIGN.RIGHT)


def export_report_pptx(*, deck: DeckDTO, slides: list[SlideDraftDTO]) -> Path:
    """Create a self-contained editable PPTX without starting a second web app."""
    EXPORT_ROOT.mkdir(parents=True, exist_ok=True)
    output = EXPORT_ROOT / f"{deck.deck_id}.pptx"
    prs = Presentation()
    prs.slide_width = _inches(13.333)
    prs.slide_height = _inches(7.5)
    for index, draft in enumerate(slides, start=1):
        _add_slide(prs, draft, index)
    if not slides:
        placeholder = SlideDraftDTO(slide_id="empty", session_id=deck.session_id, page_type="summary", title=deck.title, narrative="暂无已确认的报告页面。")
        _add_slide(prs, placeholder, 1)
    prs.save(output)
    return output
