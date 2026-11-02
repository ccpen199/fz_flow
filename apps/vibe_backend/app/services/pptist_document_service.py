"""把对话画布的大纲编译成 **PPTist / TrainPPTAgent 可编辑文档**。

为什么要这个模块（而不是继续用 `report_pptx_export_service`）：

- 产品要的是「真可编辑的 PPT」：版式、模板、元素级拖拽、导出都该由
  TrainPPTAgent（PPTist 内核）负责，我们只提供**元素级 JSON**（text / chart）。
- 之前对话画布这条线自己用 python-pptx 画死模板，结果是：版式不生效、
  图表只画前 12 行单系列、每页长一个样 —— 已经在 2026-10-25 复盘时确认是错的方向。
- 契约沿用 `docs/ppt_module_design_2026-09.md` §6：
  `editor_document = {title, width, height, theme, slides[]}`，每页 `elements[]`。

坐标系是 PPTist 的 1000 × 562.5（16:9）。**只产真实内容**：没有绑图就是纯文案页，
不编造图表；图表数据全部来自后端真实取数的 `chart_spec.rows`。
"""

from __future__ import annotations

import html
from typing import Any

SLIDE_WIDTH = 1000
SLIDE_HEIGHT = 562.5
FONT_NAME = "Microsoft YaHei"
TEXT_COLOR = "#1F2937"
MUTED_COLOR = "#5B6B80"
ACCENT_COLOR = "#2D8CFF"

# 一页最多画多少个类目。趋势图动辄上千个点，直接塞进元素会又慢又看不清；
# 超出的部分在页脚**如实写上**"已展示前 N 个类目"，不假装画全了。
MAX_CATEGORIES = 40
# 系列（图例）上限：超出合并成一条「其他（M 种）」，与画布上 ECharts 的口径一致。
MAX_SERIES = 12

# ECharts 的 bar 是"竖向柱"，PPTist 里竖向柱叫 column；这里做一次显式映射。
CHART_TYPE_MAP = {
    "bar": "column",
    "column": "column",
    "vertical": "column",
    "line": "line",
    "area": "area",
    "pie": "pie",
    "ring": "ring",
    "radar": "radar",
    "scatter": "scatter",
}


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _label(value: Any, index: int) -> str:
    text = str(value).strip() if value is not None else ""
    return (text or f"项 {index + 1}")[:24]


def _text_element(
    *,
    element_id: str,
    role: str,
    left: float,
    top: float,
    width: float,
    height: float,
    content: str,
    font_size: int,
    color: str = TEXT_COLOR,
    bold: bool = False,
    text_type: str = "content",
) -> dict[str, Any]:
    """一个 PPTist 文本元素。

    ``role`` 是**我们自己的语义标记**（title/subtitle/findings/narrative/source），
    PPTist 会忽略这个未知字段；回写时靠它把编辑器里的文字认回来，不用猜第几个文本框。
    """
    escaped = html.escape(str(content or "")).replace("\n", "<br>")
    style = f"font-size: {font_size}px; line-height: 1.45;"
    if bold:
        style += " font-weight: bold;"
    return {
        "type": "text",
        "id": element_id,
        "left": round(left, 2),
        "top": round(top, 2),
        "width": round(width, 2),
        "height": round(height, 2),
        "rotate": 0,
        "content": f'<p style="{style}">{escaped}</p>',
        "defaultFontName": FONT_NAME,
        "defaultColor": color,
        "vertical": False,
        "textType": text_type,
        "fzRole": role,
    }


def _chart_element(
    *,
    element_id: str,
    spec: dict[str, Any],
    left: float,
    top: float,
    width: float,
    height: float,
    theme_colors: list[str],
) -> tuple[dict[str, Any] | None, str]:
    """把 `chart_spec`（长表 rows）折成 PPTist 的 labels/legends/series。

    返回 ``(元素, 截断说明)``；数据不足时元素为 ``None``。
    """
    rows = [row for row in (spec.get("rows") or []) if isinstance(row, dict)]
    x_field = str(spec.get("x_field") or "")
    y_field = str(spec.get("y_field") or "")
    series_field = str(spec.get("series_field") or "")
    if not rows or not x_field or not y_field:
        return None, ""

    labels: list[str] = []
    for index, row in enumerate(rows):
        label = _label(row.get(x_field), index)
        if label not in labels:
            labels.append(label)
    total_labels = len(labels)
    if total_labels > MAX_CATEGORIES:
        labels = labels[:MAX_CATEGORIES]

    def build_series(name: str, series_rows: list[dict[str, Any]]) -> list[float]:
        by_label: dict[str, float] = {}
        for index, row in enumerate(series_rows):
            label = _label(row.get(x_field), index)
            value = _number(row.get(y_field))
            if value is None:
                continue
            by_label[label] = by_label.get(label, 0.0) + value
        return [round(by_label.get(label, 0.0), 4) for label in labels]

    if series_field:
        names: list[str] = []
        rows_by_name: dict[str, list[dict[str, Any]]] = {}
        for index, row in enumerate(rows):
            name = _label(row.get(series_field), index)
            if name not in rows_by_name:
                rows_by_name[name] = []
                names.append(name)
            rows_by_name[name].append(row)
        kept = names[:MAX_SERIES]
        legends = list(kept)
        series = [build_series(name, rows_by_name[name]) for name in kept]
        rest = [name for name in names if name not in kept]
        if rest:
            rest_rows = [row for name in rest for row in rows_by_name[name]]
            legends.append(f"其他（{len(rest)} 种）")
            series.append(build_series("其他", rest_rows))
    else:
        legends = [y_field or "指标"]
        series = [build_series(y_field, rows)]

    if not any(any(value != 0 for value in item) for item in series):
        return None, ""

    chart_type = CHART_TYPE_MAP.get(str(spec.get("chart_type") or "bar").lower(), "column")
    element = {
        "type": "chart",
        "id": element_id,
        "left": round(left, 2),
        "top": round(top, 2),
        "width": round(width, 2),
        "height": round(height, 2),
        "rotate": 0,
        "chartType": chart_type,
        "themeColors": theme_colors,
        "textColor": TEXT_COLOR,
        "data": {"labels": labels, "legends": legends, "series": series},
        "options": {"stack": bool(spec.get("stack"))} if spec.get("stack") else {},
        "fzRole": "chart",
    }
    note = ""
    if total_labels > len(labels):
        note = f"图表已展示前 {len(labels)} 个类目（共 {total_labels} 个）"
    return element, note


def _layout_regions(layout: str) -> dict[str, tuple[float, float, float, float] | None]:
    """按版式给出 (text, chart) 两个区域，参数是 (left, top, width, height)。"""
    wide_text = (56, 150, 888, 300)
    wide_chart = (56, 150, 888, 340)
    if layout == "chart_only":
        return {"text": None, "chart": wide_chart}
    if layout == "text_only":
        return {"text": wide_text, "chart": None}
    if layout == "chart_left":
        return {"text": (532, 150, 412, 300), "chart": (56, 150, 452, 340)}
    return {"text": (56, 150, 412, 300), "chart": (500, 150, 452, 340)}


def build_slide(
    *,
    slide: dict[str, Any],
    layout: str,
    index: int,
    theme_colors: list[str],
    background: str = "#FFFFFF",
) -> dict[str, Any]:
    """一页 PPTist 幻灯片（元素级）。"""
    slide_id = str(slide.get("slide_id") or f"slide_{index}")
    regions = _layout_regions(layout)
    elements: list[dict[str, Any]] = []

    elements.append(
        _text_element(
            element_id=f"{slide_id}_title",
            role="title",
            left=56,
            top=44,
            width=888,
            height=62,
            content=str(slide.get("title") or "未命名页"),
            font_size=26,
            bold=True,
            text_type="title",
        )
    )
    subtitle = str(slide.get("subtitle") or "").strip()
    if subtitle:
        elements.append(
            _text_element(
                element_id=f"{slide_id}_subtitle",
                role="subtitle",
                left=56,
                top=106,
                width=888,
                height=32,
                content=subtitle,
                font_size=13,
                color=MUTED_COLOR,
                text_type="subtitle",
            )
        )

    findings = [str(item).strip() for item in (slide.get("findings") or []) if str(item).strip()]
    narrative = str(slide.get("narrative") or "").strip()
    text_region = regions["text"]
    if text_region is not None and (findings or narrative):
        left, top, width, height = text_region
        cursor = top
        if findings:
            elements.append(
                _text_element(
                    element_id=f"{slide_id}_findings_label",
                    role="findings_label",
                    left=left,
                    top=cursor,
                    width=width,
                    height=24,
                    content="数据洞察",
                    font_size=12,
                    color=ACCENT_COLOR,
                    bold=True,
                )
            )
            cursor += 28
            elements.append(
                _text_element(
                    element_id=f"{slide_id}_findings",
                    role="findings",
                    left=left,
                    top=cursor,
                    width=width,
                    height=max(60, min(150, 22 * len(findings[:5]) + 12)),
                    content="\n".join(f"· {item}" for item in findings[:5]),
                    font_size=12,
                )
            )
            cursor += max(60, min(150, 22 * len(findings[:5]) + 12)) + 14
        if narrative:
            elements.append(
                _text_element(
                    element_id=f"{slide_id}_narrative_label",
                    role="narrative_label",
                    left=left,
                    top=cursor,
                    width=width,
                    height=24,
                    content="业务解读",
                    font_size=12,
                    color=ACCENT_COLOR,
                    bold=True,
                )
            )
            elements.append(
                _text_element(
                    element_id=f"{slide_id}_narrative",
                    role="narrative",
                    left=left,
                    top=cursor + 28,
                    width=width,
                    height=max(56, height - (cursor - top) - 28),
                    content=narrative,
                    font_size=12,
                )
            )

    chart_region = regions["chart"]
    note = ""
    spec = slide.get("chart_spec") if isinstance(slide.get("chart_spec"), dict) else {}
    if chart_region is not None and spec:
        left, top, width, height = chart_region
        element, note = _chart_element(
            element_id=f"{slide_id}_chart",
            spec=spec,
            left=left,
            top=top,
            width=width,
            height=height,
            theme_colors=theme_colors,
        )
        if element is not None:
            elements.append(element)

    source = " · ".join(
        item
        for item in [
            f"query={slide.get('query_id')}" if slide.get("query_id") else "",
            note,
        ]
        if item
    )
    if source:
        elements.append(
            _text_element(
                element_id=f"{slide_id}_source",
                role="source",
                left=56,
                top=510,
                width=888,
                height=24,
                content=source,
                font_size=9,
                color="#98A6B8",
            )
        )

    return {
        "id": slide_id,
        "elements": elements,
        "background": {"type": "solid", "color": background},
        "type": str(slide.get("page_type") or "content"),
        "fz_lineage": {
            "slide_id": slide_id,
            "query_id": slide.get("query_id"),
            "index": index,
            "layout": layout,
        },
    }


def build_document(
    *,
    title: str,
    slides: list[dict[str, Any]],
    brand: str = "",
) -> dict[str, Any]:
    """整份 PPTist 文档。``slides`` 里每项要带 ``layout``（没有就按左文右图）。"""
    theme_colors = [ACCENT_COLOR, "#40D4C8", "#F0B429", "#EF4444", "#8B5CF6", "#22C55E"]
    theme = {
        "themeColors": theme_colors,
        "fontColor": TEXT_COLOR,
        "fontName": FONT_NAME,
        "backgroundColor": "#FFFFFF",
        "shadow": {"h": 3, "v": 3, "blur": 2, "color": "#808080"},
        "outline": {"width": 2, "color": "#525252", "style": "solid"},
    }
    return {
        "title": title or (f"{brand}汇报" if brand else "分析汇报"),
        "width": SLIDE_WIDTH,
        "height": SLIDE_HEIGHT,
        "theme": theme,
        "slides": [
            build_slide(
                slide=slide,
                layout=str(slide.get("layout") or "chart_right"),
                index=index,
                theme_colors=theme_colors,
            )
            for index, slide in enumerate(slides, start=1)
        ],
    }


def _plain(content: Any) -> str:
    """把 PPTist 富文本 `<p style>…</p>` 还原成纯文本（保留换行）。"""
    text = str(content or "")
    for token in ("<br>", "<br/>", "<br />", "</p>", "</div>"):
        text = text.replace(token, "\n")
    result: list[str] = []
    inside = False
    for char in text:
        if char == "<":
            inside = True
            continue
        if char == ">":
            inside = False
            continue
        if not inside:
            result.append(char)
    return html.unescape("".join(result)).strip()


def extract_edits(slides: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """从回写的 editor_document.slides 里，按 ``fzRole`` 抽回可落库的字段。

    返回 ``{slide_id: {title, subtitle, findings, narrative}}``。
    认不出角色的文本元素一律忽略——宁可少改，也不把版式里的杂字写进大纲。
    """
    edits: dict[str, dict[str, Any]] = {}
    for slide in slides:
        if not isinstance(slide, dict):
            continue
        slide_id = str(slide.get("id") or "")
        if not slide_id:
            continue
        found: dict[str, Any] = {}
        for element in slide.get("elements") or []:
            if not isinstance(element, dict) or element.get("type") != "text":
                continue
            role = str(element.get("fzRole") or "")
            if not role:
                if element.get("textType") == "title":
                    role = "title"
                elif element.get("textType") == "subtitle":
                    role = "subtitle"
            text = _plain(element.get("content"))
            if role == "title" and text:
                found["title"] = text
            elif role == "subtitle":
                found["subtitle"] = text
            elif role == "findings":
                lines = [line.strip("·• \t") for line in text.split("\n") if line.strip("·• \t")]
                found["findings"] = lines
            elif role == "narrative":
                found["narrative"] = text
        if found:
            edits[slide_id] = found
    return edits
