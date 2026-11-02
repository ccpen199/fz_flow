from __future__ import annotations

import re
from typing import Any

from packages.shared_contracts.python_models import SceneDTO


# Analysis entry points are derived from the scene's own field semantics instead
# of a fixed question list, so a new scene automatically yields new suggestions.
# Each theme is a keyword bundle over semantic_name / field_name; a theme only
# becomes a card when the scene really owns the fields it needs.
THEMES: list[dict[str, Any]] = [
    {
        "key": "fiber",
        "title": "面料成分与纤维混纺",
        "angle": "可持续成分与材质结构",
        "keywords": ["纤维", "材质", "成分", "织造", "肌理", "图案", "工艺", "定织", "fiber", "texture", "pattern", "fabric"],
        "prefer_metrics": ["纤维占比", "percent", "价格"],
        "prefer_dimensions": ["标准纤维名称", "纤维编码", "纤维标准编码", "织造方式", "肌理", "图案", "图案布局"],
        "chart_hint": "stacked_area",
        "reason": "统计再生聚酯纤维、棉麻等成分的应用比例走势",
    },
    {
        "key": "price",
        "title": "价格带与折扣节奏",
        "angle": "价格定位与促销深度",
        "keywords": ["价格", "标价", "折扣", "吊牌", "price", "amount"],
        "prefer_metrics": ["价格", "原始标价"],
        "prefer_dimensions": ["二级类目", "一级类目", "叶子类目", "品牌"],
        "chart_hint": "histogram",
        "reason": "各核心品类的吊牌均价分布及打折促销深度",
    },
    {
        "key": "color",
        "title": "流行色彩矩阵",
        "angle": "主推色系覆盖率",
        "keywords": ["颜色", "色彩", "主色", "色号", "rgb", "lab", "coloro", "pantone", "color"],
        "prefer_metrics": ["图片颜色占比", "percent"],
        "prefer_dimensions": ["颜色", "图片RGB", "图片LAB", "Coloro色号", "Pantone色号"],
        "chart_hint": "pie",
        "reason": "主推色系的货品覆盖率与色系配比",
    },
    {
        "key": "structure",
        "title": "品类结构分布",
        "angle": "货品结构与集中度",
        "keywords": ["类目", "品类", "结构", "叶子类目", "category"],
        "prefer_metrics": ["价格", "percent"],
        "prefer_dimensions": ["一级类目", "二级类目", "叶子类目"],
        "chart_hint": "bar",
        "reason": "各品类货品结构占比与集中度",
    },
    {
        "key": "brand",
        "title": "品牌竞争格局",
        "angle": "品牌间横向对比",
        "keywords": ["品牌", "竞品", "对比", "brand"],
        "prefer_metrics": ["价格"],
        "prefer_dimensions": ["品牌", "二级类目", "来源站点域名"],
        "chart_hint": "bar",
        "reason": "同类目下品牌的价格定位与 SKU 覆盖差异",
    },
    {
        "key": "trend",
        "title": "上新节奏与趋势",
        "angle": "时间维度的变化",
        "keywords": ["上新", "上架", "时间", "趋势", "节奏", "抓取", "批次", "createtime", "receivetime", "date"],
        "prefer_metrics": ["价格", "percent"],
        "prefer_dimensions": ["上架时间", "抓取日期", "品牌", "二级类目"],
        "chart_hint": "line",
        "reason": "上新量随时间的走势与高峰识别",
    },
]

CHART_HINT_TO_TYPE = {
    "stacked_area": "area",
    "histogram": "column",
    "pie": "pie",
    "bar": "bar",
    "line": "line",
}

MAX_CARDS = 4
MAX_FIELDS_PER_CARD = 6

# 一个方向在本套配置里找不到自己的度量字段时，用**如实声明的计数**做口径。
# 它与 `build_goal` 的空度量缺省、`canvas_orchestrator.plan_data_query` 的
# `["记录数"]` 缺省是同一个词，所以卡片上显示的、目标里写下的、下游取数时用的
# 是同一个口径——不会出现"卡片写 A、目标写 B"的分叉。
COUNT_METRIC = "记录数"


def _text_of(field: Any) -> str:
    return " ".join(
        str(getattr(field, name, "") or "")
        for name in ("semantic_name", "field_name", "table_name", "description")
    ).lower()


def _field_role(field: Any) -> str:
    value = getattr(field, "role", "")
    return str(getattr(value, "value", value) or "").strip().lower()


def _enabled_fields(scene: SceneDTO) -> list[Any]:
    return [field for field in scene.fields if getattr(field, "enabled", True)]


def _match_fields(fields: list[Any], keywords: list[str]) -> list[Any]:
    hits: list[Any] = []
    for field in fields:
        text = _text_of(field)
        if any(keyword in text for keyword in keywords):
            hits.append(field)
    return hits


def _dedupe(fields: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for field in fields:
        key = str(getattr(field, "field_id", "") or getattr(field, "semantic_name", ""))
        if key in seen:
            continue
        seen.add(key)
        result.append(field)
    return result


def _preferred(fields: list[Any], preferred_names: list[str]) -> list[Any]:
    """Order fields by the theme's preference list, not by scene order."""
    fields = _dedupe(fields)
    ordered: list[Any] = []
    matched: set[str] = set()
    for name in preferred_names:
        lowered = name.lower()
        for field in fields:
            key = str(getattr(field, "field_id", "") or getattr(field, "semantic_name", ""))
            if key in matched:
                continue
            semantic = str(getattr(field, "semantic_name", "") or "")
            if semantic.lower() == lowered:
                ordered.append(field)
                matched.add(key)
    for field in fields:
        key = str(getattr(field, "field_id", "") or getattr(field, "semantic_name", ""))
        if key not in matched:
            ordered.append(field)
            matched.add(key)
    return ordered


CODE_HINTS = ("编码", "code", "id", "编号")


def _names(fields: list[Any], limit: int = MAX_FIELDS_PER_CARD) -> list[str]:
    """Readable business labels first; technical keys (编码/Code/ID) last."""
    names: list[str] = []
    for field in fields:
        name = str(getattr(field, "semantic_name", "") or "").strip()
        if name and name not in names:
            names.append(name)
    names.sort(key=lambda item: 1 if any(hint in item.lower() for hint in CODE_HINTS) else 0)
    return names[:limit]


class DimensionRecommender:
    """Derives "where to start" suggestions from the scene's real field semantics."""

    def recommend(
        self,
        *,
        scene: SceneDTO,
        brand: str = "",
        limit: int = MAX_CARDS,
    ) -> list[dict[str, Any]]:
        fields = _enabled_fields(scene)
        metrics = [field for field in fields if _field_role(field) == "metric"]
        dimensions = [field for field in fields if _field_role(field) == "dimension"]
        time_fields = [field for field in fields if _field_role(field) == "time"]

        cards: list[dict[str, Any]] = []
        for theme in THEMES:
            hit_fields = _match_fields(fields, theme["keywords"])
            hit_dimensions = [field for field in hit_fields if _field_role(field) == "dimension"]
            hit_metrics = [field for field in hit_fields if _field_role(field) == "metric"]

            fallback_dimensions = [field for field in dimensions if str(getattr(field, "semantic_name", "")) in theme["prefer_dimensions"]]
            chosen_dimensions = hit_dimensions or fallback_dimensions
            if not chosen_dimensions:
                # A theme with no dimension cannot be grouped; skip it entirely.
                continue

            chosen_metrics = hit_metrics or [
                field for field in metrics if str(getattr(field, "semantic_name", "")).lower() in [n.lower() for n in theme["prefer_metrics"]]
            ]
            # ⚠️ 这里**没有**兜底：本方向在这套配置里找不到自己的度量时，以前会拿
            # ``metrics[:1]``（配置里的第一个指标）顶上。实测后果（2026-10-13 报障）：
            # 纤维配置（9 个字段、唯一指标是「纤维占比」）里的「价格带与折扣节奏」
            # 被贴成 `各二级类目的纤维占比分布情况`、「品牌竞争格局」被贴成
            # `按品牌统计纤维占比`，卡片标题写着价格、口径却是纤维 —— 取数时字段对不上，
            # SQL agent 只能回"当前 semantic_fields 中没有价格/金额字段"，用户收到一张废卡
            # （另一个同类废卡的目标是 `各织造方式的价格构成与变化`，同样来自这次兜底）。
            # 找不到度量就**如实降级为计数**：它有明确名字「记录数」，会原样出现在卡片上，
            # 与 build_goal / plan_data_query 既有的缺省口径一致；这不是从别的方向借来的指标。
            ordered_dimensions = _preferred(chosen_dimensions, theme["prefer_dimensions"])
            ordered_metrics = _preferred(chosen_metrics, theme["prefer_metrics"]) if chosen_metrics else []
            metric_names = _names(ordered_metrics, 3) or [COUNT_METRIC]
            dimension_names = _names(ordered_dimensions)
            # coverage = 这张卡真正绑上的东西：维度 + 指标（计数也算一个指标）。
            coverage = len(ordered_dimensions) + len(metric_names)
            match_terms = list(
                dict.fromkeys(
                    [
                        theme["key"],
                        theme["title"],
                        theme["angle"],
                        *theme["keywords"],
                        *dimension_names,
                    ]
                )
            )
            cards.append(
                {
                    "dimension_key": theme["key"],
                    "title": theme["title"],
                    "angle": theme["angle"],
                    "reason": theme["reason"],
                    "metrics": metric_names,
                    "dimensions": dimension_names,
                    "match_terms": match_terms,
                    "time_field": str(getattr(time_fields[0], "semantic_name", "") or "") if time_fields else "",
                    "chart_hint": CHART_HINT_TO_TYPE.get(theme["chart_hint"], "bar"),
                    "coverage": coverage,
                    "sample_goal": self.build_goal(
                        theme=theme,
                        brand=brand,
                        metrics=metric_names,
                        dimensions=dimension_names,
                        time_field=str(getattr(time_fields[0], "semantic_name", "") or "") if time_fields else "",
                    ),
                }
            )

        cards.sort(key=lambda item: item["coverage"], reverse=True)
        return cards[:limit]

    @staticmethod
    def build_goal(
        *,
        theme: dict[str, Any],
        brand: str,
        metrics: list[str],
        dimensions: list[str],
        time_field: str,
    ) -> str:
        """Build a *brand-free* goal template.

        The brand deliberately stays out of the template. Dimension cards are
        cached on the canvas and reused across turns, so baking a brand in here
        would leak the old brand into every later query ("THE NORTH FACE ZARA
        各二级类目的价格分布") and make the SQL agent see two conflicting brands.
        The brand is prepended at query time instead, from current working
        memory.
        """
        del brand  # kept in the signature for callers; intentionally unused
        metric = metrics[0] if metrics else COUNT_METRIC
        dimension = dimensions[0] if dimensions else "类目"
        if theme["key"] == "trend":
            return f"按{time_field or '时间'}统计{metric}变化趋势"
        if theme["key"] == "price":
            return f"各{dimension}的{metric}分布情况"
        if theme["key"] == "fiber":
            return f"各{dimension}的{metric}构成与变化"
        if theme["key"] == "color":
            return f"各{dimension}的货品覆盖分布"
        return f"按{dimension}统计{metric}"


dimension_recommender = DimensionRecommender()


TIME_PATTERNS = [
    r"近\s*(\d+)\s*个月",
    r"最近\s*(\d+)\s*个月",
    r"近\s*(\d+)\s*周",
    r"最近\s*(\d+)\s*周",
    r"近\s*(\d+)\s*天",
    r"最近\s*(\d+)\s*天",
]


def extract_time_window(text: str) -> str:
    """Pull a human time window out of the question so the goal stays faithful."""
    raw = str(text or "")
    for pattern in TIME_PATTERNS:
        match = re.search(pattern, raw)
        if match:
            return match.group(0).replace(" ", "")
    for match in re.finditer(r"20\d{2}\s*年\s*(?:\d{1,2}\s*月)?", raw):
        return match.group(0).replace(" ", "")
    return ""
