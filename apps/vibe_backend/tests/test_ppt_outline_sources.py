"""大纲页的来源与优先级：**业务需求优先，会话上文只作参考**。

用户原话（2026-10-28）："ppt模块可不是一定完全以会话上文的数据为基准，只是作为参考。
且可以联动修改。" 所以：

1. 用户/主题点名的方向 → 排在最前面成页；已有图就绑图，没有图就标「待取数」
   （之后「补齐待取数」会真的去库里取）；
2. 会话里已经出过的图 → 作为参考证据跟在后面，且不重复；
3. 待取数页的标题**必须**与方向名一致，否则 `_page_dimension_key` 认不回来，
   那页就永远补不上数。

原则（项目硬约束）：不伪造业务数据。这里的图表卡只有标题/维度键，没有任何行数据。
"""

from __future__ import annotations

from app.services.conversation_service import conversation_service
from app.services.report_composer import report_composer

FIBER_CARD = {
    "artifact_id": "art_src_fiber",
    "title": "面料成分与纤维混纺",
    "subtitle": "fiber",
    "dimension_key": "fiber",
}


def _build(chart_cards, pending, requested, *, topic="优衣库 2026 春夏复盘", page_count=4):
    return report_composer.build_outline(
        brand="优衣库",
        scene_name="竞品企划分析",
        intent_text=topic,
        chart_cards=chart_cards,
        pending_dimensions=pending,
        page_count=page_count,
        topic=topic,
        requested_dimensions=requested,
    )


def test_requested_dimension_without_a_chart_becomes_a_pending_page() -> None:
    pages = _build(
        [FIBER_CARD],
        [],
        [{"dimension_key": "price", "title": "价格带与折扣节奏", "reason": "看价格结构"}],
        topic="优衣库 2026 春夏价格复盘",
    )

    assert pages[0]["title"] == "优衣库 2026 春夏价格复盘"
    # 业务需求排在会话里的图之前（上下文只是参考）。
    assert pages[1]["title"] == "价格带与折扣节奏"
    assert pages[1]["kind"] == "chart"
    assert pages[1]["status"] == "needs_data"
    assert pages[1].get("bound_artifact_id") in (None, "")
    assert any(page.get("bound_artifact_id") == "art_src_fiber" for page in pages)


def test_requested_dimension_with_a_chart_binds_it_instead_of_pending() -> None:
    pages = _build(
        [FIBER_CARD],
        [],
        [{"dimension_key": "fiber", "title": "面料成分与纤维混纺"}],
    )

    assert pages[1]["title"] == "面料成分与纤维混纺"
    assert pages[1]["bound_artifact_id"] == "art_src_fiber"
    assert pages[1]["status"] == "bound"
    # 不能因为"点名了这一页"就把同一张图再放一遍。
    assert sum(1 for page in pages if page.get("bound_artifact_id") == "art_src_fiber") == 1


def test_pending_page_keeps_the_dimension_title_so_it_can_be_filled() -> None:
    """标题必须就是方向名：`_page_dimension_key` 靠它认回维度去取数。"""
    pages = _build([], [{"dimension_key": "trend", "title": "上新节奏与趋势", "reason": "看节奏"}], [])

    pending = [page for page in pages if page["status"] == "needs_data"]
    assert pending, "有待取数方向时必须真的落成待取数页"
    assert pending[0]["title"] == "上新节奏与趋势"


def test_rank_dimensions_for_topic_uses_the_theme_terms_only() -> None:
    options = [
        {"dimension_key": "fiber", "match_terms": ["纤维", "fabric", "材质"]},
        {"dimension_key": "price", "match_terms": ["价格", "price", "折扣"]},
    ]

    ranked = conversation_service._rank_dimensions_for_topic(options, "看看价格带和折扣深度")
    assert [item["dimension_key"] for item in ranked] == ["price", "fiber"]

    # 一个词都没命中就按原顺序，不硬挑一个"最像"的。
    untouched = conversation_service._rank_dimensions_for_topic(options, "做个总览")
    assert [item["dimension_key"] for item in untouched] == ["fiber", "price"]
    # 空主题同样不动顺序。
    assert [
        item["dimension_key"]
        for item in conversation_service._rank_dimensions_for_topic(options, "")
    ] == ["fiber", "price"]
