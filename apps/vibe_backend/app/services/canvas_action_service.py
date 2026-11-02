"""对话式画布的「动作槽位」——解析、校验与诚实回执。

这一层只做三件事，全部是**纯函数**（无 IO、无副作用、可单测）：

1. 把 DeepFlow agent 给出的结构化槽位归一化到后端真正认识的取值
   （图表形态、值筛选、页面操作、要移除的维度）。
2. **严格校验**：凡是没有命中真实字段/真实维度的槽位一律拒绝并给出原因，
   绝不"差不多就执行"。拒绝的原因会被上层拼进给用户的回复里。
3. 提供「条件到底有没有落到 SQL」的**确定性回执**：
   只判断"字面量完全不在 SQL 里"这一种情况（缺失是可证伪的），
   命中则不表态（存在可能只是巧合）。据此如实告知用户，而不是假装成功。

设计约束（与项目硬约束一致）：
- 不兜底：槽位认不出来就返回空，由上层追问，不猜一个近似值。
- 不造数：本模块不产生任何业务数值，只做字面量比对。
- 词表是**逻辑参数**：形态别名、页数上下限属于允许的硬编码规则，
  真正决定"用户想要什么"的始终是 agent，不是这里的关键词匹配。
"""

from __future__ import annotations

import re
from typing import Any

# --------------------------------------------------------------------- 图表形态

# ECharts 认得的取值 + 后端 chart_spec 可能产生的取值。
# `column` 保留在后端契约里（老配置页用 SVG 自绘，认它），但**不**在这里放行：
# 新画布统一用 bar，避免又出现"传 column 导致 series 被静默丢弃"。
ALLOWED_CHART_TYPES = ("bar", "line", "area", "pie", "ring")

# 形态词表：agent 应直接给规范值，这里是安全网，也为前端标签复用。
CHART_TYPE_VOCAB: dict[str, str] = {
    "bar": "bar", "column": "bar", "vertical": "bar", "histogram": "bar",
    "柱状图": "bar", "柱形图": "bar", "条形图": "bar", "柱图": "bar", "柱子": "bar",
    "line": "line", "trend": "line",
    "折线图": "line", "曲线图": "line", "线图": "line", "趋势图": "line", "走势图": "line",
    "area": "area", "堆叠面积图": "area", "面积图": "area",
    "pie": "pie", "饼图": "pie", "圆饼图": "pie",
    "ring": "ring", "donut": "ring", "doughnut": "ring",
    "圆环图": "ring", "环形图": "ring", "甜甜圈图": "ring",
}

CHART_TYPE_LABELS: dict[str, str] = {
    "bar": "柱状图",
    "line": "折线图",
    "area": "面积图",
    "pie": "饼图",
    "ring": "圆环图",
}

# --------------------------------------------------------------------- 页面操作

ALLOWED_PAGE_OPS = (
    "add_page",
    "remove_page",
    "merge_pages",
    "balance",
    "layout",
    "regenerate",
    "generate_deck",
)

# 版式：谁在左、谁在右、或只要一半。默认 chart_right（图表在右）。
ALLOWED_PAGE_LAYOUTS = ("chart_right", "chart_left", "chart_only", "text_only")

PAGE_LAYOUT_LABELS: dict[str, str] = {
    "chart_right": "左文右图",
    "chart_left": "左图右文",
    "chart_only": "仅图表",
    "text_only": "仅文案",
}

MIN_PAGE_COUNT = 2
MAX_PAGE_COUNT = 12

# 允许的比较运算符，与 field_value_resolver / price_band_filter_clauses 支持的一致。
ALLOWED_FILTER_OPERATORS = (">", ">=", "<", "<=", "=", "!=", "between", "in")

_NUMERIC_RE = re.compile(r"^-?\d+(?:\.\d+)?$")


def _text(value: Any) -> str:
    return str(value or "").strip()


def normalize_chart_type(raw: Any) -> str:
    """把 agent 给的图表形态归一化；认不出来返回空串（由上层追问）。"""
    token = _text(raw).lower().replace(" ", "")
    if not token:
        return ""
    return CHART_TYPE_VOCAB.get(token, "")


def chart_type_label(chart_type: str) -> str:
    return CHART_TYPE_LABELS.get(str(chart_type or ""), str(chart_type or ""))


def normalize_page_layout(raw: Any) -> str:
    token = _text(raw).lower()
    return token if token in ALLOWED_PAGE_LAYOUTS else ""


# ---------------------------------------------------------------------- 值筛选


def normalize_page_count(raw: Any) -> int | None:
    try:
        number = int(raw)
    except (TypeError, ValueError):
        return None
    return min(MAX_PAGE_COUNT, max(MIN_PAGE_COUNT, number))


def _coerce_filter_value(raw: Any) -> Any:
    """数值型字面量转成数字，其余保持字符串（不猜、不改写用户给的值）。"""
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        return raw
    token = _text(raw)
    if not token:
        return None
    if _NUMERIC_RE.match(token):
        return float(token) if "." in token else int(token)
    return token


def normalize_filters(
    raw: Any,
    *,
    allowed_fields: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    """校验 agent 给的值筛选。

    ``allowed_fields`` 是「归一化字段名 -> 字段定义」的映射，来自场景里真实可查的
    语义字段。**只接受命中真实字段的条件**——认不出的字段会被拒绝并把原因回传，
    由上层如实告诉用户，而不是悄悄丢掉或硬塞给 SQL。

    返回 ``(接受的筛选条件, 拒绝原因列表)``。
    """
    if not isinstance(raw, list):
        return [], []
    accepted: list[dict[str, Any]] = []
    rejected: list[str] = []
    seen: set[tuple[str, str, str]] = set()

    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("field") or item.get("semantic_name") or item.get("name"))
        if not name:
            continue
        field = allowed_fields.get(normalize_key(name))
        if field is None:
            rejected.append(f"字段「{name}」不在当前场景的可查字段里")
            continue
        canonical_name = _text(field.get("semantic_name")) or name

        operator = _text(item.get("operator") or item.get("op") or "=").lower()
        if operator not in ALLOWED_FILTER_OPERATORS:
            rejected.append(f"「{canonical_name}」的比较方式 {operator or '（空）'} 不支持")
            continue

        if operator == "between":
            pair = item.get("value")
            if not isinstance(pair, list) or len(pair) != 2:
                rejected.append(f"「{canonical_name}」的区间写法不完整")
                continue
            left = _coerce_filter_value(pair[0])
            right = _coerce_filter_value(pair[1])
            if left is None or right is None:
                rejected.append(f"「{canonical_name}」的区间端点无法识别")
                continue
            value: Any = [left, right]
            shown = f"{left}~{right}"
        elif operator == "in":
            # 「这两个品牌对比」——多主体验证必须落到**一个**条件上，
            # 而不是让调用方偷偷只查第一个（那就是答非所问）。
            raw_values = item.get("value")
            if not isinstance(raw_values, list) or not raw_values:
                rejected.append(f"「{canonical_name}」的多值写法不完整")
                continue
            values = [
                _coerce_filter_value(entry)
                for entry in raw_values
                if _coerce_filter_value(entry) is not None
            ]
            if not values:
                rejected.append(f"「{canonical_name}」没给出可用的比较值")
                continue
            value = values
            shown = "∈ " + " / ".join(str(entry) for entry in values)
        else:
            single = _coerce_filter_value(item.get("value"))
            if single is None:
                rejected.append(f"「{canonical_name}」没给出可用的比较值")
                continue
            value = single
            shown = f"{operator}{single}"

        dedupe_key = (canonical_name, operator, str(value))
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        accepted.append(
            {
                "field": canonical_name,
                "operator": operator,
                "value": value,
                "table_name": _text(field.get("table_name")),
                "raw_field_name": _text(field.get("field_name")),
                "display": f"{canonical_name} {shown}",
            }
        )
    return accepted, rejected


def normalize_key(value: Any) -> str:
    return _text(value).lower().replace(" ", "").replace("_", "")


def describe_filters(filters: list[dict[str, Any]]) -> str:
    """把筛选条件写成人话，同时用于回复文本与给 SQL agent 的硬性要求。"""
    return "、".join(str(item.get("display") or "") for item in filters if item.get("display"))


def filters_missing_from_sql(sql: str, filters: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """找出**确定没落到 SQL** 的筛选条件。

    只认一种判定：该条件的比较值（数字或字符串）在 SQL 里**完全找不到**。
    缺失是可证伪的（找不到就是没写进去），所以我们敢据此如实告知；
    反过来"找到了"不等于真的生效（可能只是巧合命中），因此不做正向断言。
    """
    text = str(sql or "")
    if not text:
        return list(filters or [])
    missing: list[dict[str, Any]] = []
    for item in filters or []:
        value = item.get("value")
        tokens = value if isinstance(value, list) else [value]
        if not any(_literal_present(text, token) for token in tokens):
            missing.append(item)
    return missing


_SQL_STRING_LITERAL = re.compile(r"'((?:[^']|'')*)'")


def _sql_string_literals(sql: str) -> set[str]:
    """SQL 里所有单引号字符串字面量（已还原 '' 转义）。"""
    return {match.group(1).replace("''", "'") for match in _SQL_STRING_LITERAL.finditer(sql)}


def _literal_present(sql: str, token: Any) -> bool:
    """该取值是否**真的作为字面量**出现在 SQL 里。

    ⚠️ 这里必须按「字面量整体相等」判定，不能做 `text in sql` 子串匹配：
    `优衣库` 是 `uniqlo/优衣库` 的子串、`T恤` 是 `T恤/Polo衫/背心` 的子串，
    子串命中会把「口径不同」误判成「条件已生效」，反过来静默出一个错口径的图。
    LIKE 模式（字面量里有 % 或 _）是真实存在的前缀/模糊匹配，按包含判定即可。
    """
    if token is None:
        return False
    if isinstance(token, (int, float)) and not isinstance(token, bool):
        body = f"{token:g}"
        return re.search(rf"(?<![\d.]){re.escape(body)}(?![\d.])", sql) is not None
    text = _text(token)
    if not text:
        return False
    # 日期型取值按「前缀 + 边界」认：筛选 `2024-07` 常被写成 `>= '2024-07-01'`，
    # 那是真的生效了，不能因为字面量不等就报"没落进 SQL"。
    date_like = re.fullmatch(r"\d{4}(?:-\d{1,2}){0,2}", text)
    for literal in _sql_string_literals(sql):
        if literal == text:
            return True
        if ("%" in literal or "_" in literal) and text in literal:
            return True
        if date_like and literal.startswith(text) and literal[len(text) : len(text) + 1] == "-":
            return True
    return False


# ---------------------------------------------------------------- 维度移除


def normalize_remove_keys(raw: Any, *, allowed_keys: set[str]) -> list[str]:
    """要移除的维度 key —— 只接受画布上真实存在、且确实是个维度的 key。"""
    if not isinstance(raw, list):
        return []
    result: list[str] = []
    for item in raw:
        key = _text(item)
        if key and key in allowed_keys and key not in result:
            result.append(key)
    return result


# ---------------------------------------------------------------- 页面操作


def normalize_page_ops(
    raw: Any,
    *,
    page_count: int,
) -> tuple[list[dict[str, Any]], list[str]]:
    """校验 agent 给的大纲页面操作。

    ``page_count`` 是当前大纲的真实页数，用来卡住越界的页码——
    指向不存在的页同样属于"画布上没有的东西不能改"。
    """
    if not isinstance(raw, list):
        return [], []
    ops: list[dict[str, Any]] = []
    rejected: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _text(item.get("op") or item.get("action")).lower()
        if name not in ALLOWED_PAGE_OPS:
            if name:
                rejected.append(f"不支持的页面操作「{name}」")
            continue

        if name == "add_page":
            ops.append(
                {
                    "op": "add_page",
                    "title": _text(item.get("title")),
                    "objective": _text(item.get("objective")),
                }
            )
            continue

        if name == "merge_pages":
            indexes = _merge_indexes(item)
            valid: list[int] = []
            for index in indexes:
                if index < 1 or index > page_count:
                    continue
                if index not in valid:
                    valid.append(index)
            if len(valid) < 2:
                rejected.append(
                    f"要合并的页码不清楚（当前共 {page_count} 页，至少要说两页）"
                )
                continue
            ops.append(
                {
                    "op": "merge_pages",
                    "page_indexes": sorted(valid),
                    # "突出混纺趋势"这类要求必须原话带下去，否则合并完
                    # 只是少了一页，用户要的重点根本没落在页面上。
                    "emphasis": _text(item.get("emphasis")),
                }
            )
            continue

        if name == "balance":
            target = normalize_page_count(item.get("page_count") or item.get("target"))
            if target is None:
                rejected.append("压缩页数不清楚（需要在 2~12 之间）")
                continue
            ops.append({"op": "balance", "page_count": target})
            continue

        if name == "generate_deck":
            ops.append({"op": "generate_deck"})
            continue

        index = _page_index(item)
        if index is None or index < 1 or index > page_count:
            rejected.append(f"第 {index if index else '?'} 页不存在（当前共 {page_count} 页）")
            continue
        if name == "remove_page":
            ops.append({"op": "remove_page", "page_index": index})
        elif name == "regenerate":
            ops.append({"op": "regenerate", "page_index": index})
        elif name == "layout":
            layout = normalize_page_layout(item.get("layout"))
            if not layout:
                rejected.append(f"第 {index} 页的版式不认识")
                continue
            ops.append({"op": "layout", "page_index": index, "layout": layout})
    return ops, rejected


def _merge_indexes(item: dict[str, Any]) -> list[int]:
    """把 agent 给的"要合并哪几页"归一化成整数列表。

    兼容三种写法：`page_indexes: [2, 3]`、"2,3"、以及 `pages: [2, 3]`。
    非法项直接丢弃——上层会用"不足两页"如实拒绝，不猜用户指的是哪页。
    """
    raw = item.get("page_indexes")
    if raw is None:
        raw = item.get("pages")
    if raw is None:
        raw = item.get("indexes")
    values = list(raw) if isinstance(raw, (list, tuple)) else [raw]
    if len(values) == 1 and isinstance(values[0], str):
        values = [part for part in re.split(r"[、,，\s]+", values[0].strip()) if part]
    indexes: list[int] = []
    for value in values:
        try:
            indexes.append(int(value))
        except (TypeError, ValueError):
            continue
    return indexes


def _page_index(item: dict[str, Any]) -> int | None:
    for key in ("page_index", "page", "index", "order"):
        if item.get(key) is None:
            continue
        try:
            return int(item[key])
        except (TypeError, ValueError):
            continue
    return None


# ------------------------------------------------------------------ 汇总


def merge_notes(*groups: list[str]) -> str:
    """把若干组拒绝原因合成一段给用户看的话（去重、保序）。"""
    merged: list[str] = []
    for group in groups:
        for item in group or []:
            text = _text(item)
            if text and text not in merged:
                merged.append(text)
    return "；".join(merged)


class CanvasActionService:
    """纯函数集合的类壳，便于测试与依赖注入。"""

    ALLOWED_CHART_TYPES = ALLOWED_CHART_TYPES
    CHART_TYPE_LABELS = CHART_TYPE_LABELS
    ALLOWED_PAGE_OPS = ALLOWED_PAGE_OPS
    ALLOWED_PAGE_LAYOUTS = ALLOWED_PAGE_LAYOUTS
    PAGE_LAYOUT_LABELS = PAGE_LAYOUT_LABELS

    normalize_chart_type = staticmethod(normalize_chart_type)
    chart_type_label = staticmethod(chart_type_label)
    normalize_page_layout = staticmethod(normalize_page_layout)
    normalize_key = staticmethod(normalize_key)
    normalize_page_count = staticmethod(normalize_page_count)
    normalize_filters = staticmethod(normalize_filters)
    describe_filters = staticmethod(describe_filters)
    filters_missing_from_sql = staticmethod(filters_missing_from_sql)
    normalize_remove_keys = staticmethod(normalize_remove_keys)
    normalize_page_ops = staticmethod(normalize_page_ops)
    merge_notes = staticmethod(merge_notes)


canvas_action_service = CanvasActionService()
