"""PPT 交接（TrainPPTAgent / PPTist）单测。

只验三件事：

1. **结构映射**：真实 `chart_spec`（长表）→ PPTist 的 `labels/legends/series`；
   版式决定元素落位；系列超上限合并成「其他（N 种）」而不是丢掉。
2. **交接口径**：`ppt_handoff()` 给的文档带 `fzRole`，回写时能按角色认出标题/要点/结论。
3. **回写**：编辑器改的字要落到 slide 草稿**和大纲页**，且图表数据不被改。

数据说明：下面 `_fixture_*` 是**结构占位**（只用来验映射关系），数值不被当作业务结论断言，
也不会进任何存储；真实数据始终来自 MySQL。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.conversation_service import conversation_service
from app.services.pptist_document_service import (
    MAX_CATEGORIES,
    MAX_SERIES,
    build_document,
    extract_edits,
)
from app.store import ARTIFACTS, CONVERSATIONS, MESSAGES
from packages.shared_contracts.python_models import (
    ArtifactCardDTO,
    ConversationDTO,
    WorkingMemoryDTO,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _fixture_rows():
    return [
        {"fiber": "棉", "share": 40.0},
        {"fiber": "涤纶", "share": 35.0},
        {"fiber": "锦纶", "share": 25.0},
    ]


def _fixture_multi_series_rows():
    rows = []
    for index in range(MAX_SERIES + 3):
        rows.append({"month": "2026-01", "fiber": f"S{index:02d}", "share": float(index + 1)})
    return rows


def _fixture_chart_spec(rows=None, *, series_field: str = ""):
    return {
        "chart_type": "bar",
        "x_field": "month" if series_field else "fiber",
        "y_field": "share",
        "series_field": series_field,
        "stack": False,
        "rows": rows if rows is not None else _fixture_rows(),
    }


def _slide(slide_id: str, *, title: str, layout: str = "chart_right", chart_spec=None):
    return {
        "slide_id": slide_id,
        "title": title,
        "subtitle": "fiber ｜ share",
        "chart_spec": chart_spec if chart_spec is not None else _fixture_chart_spec(),
        "findings": ["棉占比最高", "三种成分合计 100%"],
        "narrative": "混纺以棉为主。",
        "page_type": "comparison",
        "query_id": "q_demo",
        "layout": layout,
        "lineage_summary": {"report_page_id": f"page_{slide_id}"},
    }


def test_document_maps_real_chart_rows_and_layout():
    document = build_document(title="纤维结构", slides=[_slide("s1", title="纤维结构")])

    assert document["width"] == 1000 and document["height"] == 562.5
    slide = document["slides"][0]
    chart = next(item for item in slide["elements"] if item["type"] == "chart")
    assert chart["chartType"] == "column"  # ECharts 的 bar = PPTist 的 column
    assert chart["data"]["labels"] == ["棉", "涤纶", "锦纶"]
    assert chart["data"]["series"] == [[40.0, 35.0, 25.0]]
    assert chart["data"]["legends"] == ["share"]

    roles = {item.get("fzRole"): item for item in slide["elements"] if item["type"] == "text"}
    assert roles["title"]["content"].startswith("<p")
    assert "数据洞察" in roles["findings_label"]["content"]
    assert "业务解读" in roles["narrative_label"]["content"]
    chart_left = chart["left"]
    findings_left = roles["findings"]["left"]
    assert findings_left < chart_left  # 左文右图

    only_chart = build_document(title="只图表", slides=[_slide("s2", title="只图表", layout="chart_only")])
    elements = only_chart["slides"][0]["elements"]
    assert not any(item.get("fzRole") in {"findings", "narrative"} for item in elements)
    assert next(item for item in elements if item["type"] == "chart")["width"] > 800


def test_series_over_limit_is_merged_not_dropped():
    spec = _fixture_chart_spec(_fixture_multi_series_rows(), series_field="fiber")
    document = build_document(title="多系列", slides=[_slide("s1", title="多系列", chart_spec=spec)])
    chart = next(item for item in document["slides"][0]["elements"] if item["type"] == "chart")

    assert len(chart["data"]["legends"]) == MAX_SERIES + 1
    assert chart["data"]["legends"][-1] == "其他（3 种）"
    real_total = sum(float(index + 1) for index in range(MAX_SERIES + 3))
    merged_total = sum(series[0] for series in chart["data"]["series"])
    assert merged_total == real_total  # 合并不能丢值


def test_categories_are_capped_and_declared():
    spec = _fixture_chart_spec(
        [{"fiber": f"F{index:02d}", "share": 1.0} for index in range(MAX_CATEGORIES + 5)]
    )
    document = build_document(title="多类目", slides=[_slide("s1", title="多类目", chart_spec=spec)])
    elements = document["slides"][0]["elements"]
    chart = next(item for item in elements if item["type"] == "chart")
    assert len(chart["data"]["labels"]) == MAX_CATEGORIES
    source = next(item for item in elements if item.get("fzRole") == "source")
    assert f"共 {MAX_CATEGORIES + 5} 个" in source["content"]


def test_extract_edits_reads_roles_not_positions():
    slides = [
        {
            "id": "s1",
            "elements": [
                {"type": "text", "fzRole": "title", "content": "<p>新标题</p>"},
                {"type": "text", "fzRole": "findings", "content": "<p>· 要点一<br>· 要点二</p>"},
                {"type": "text", "content": "<p>版式里的杂字</p>"},
            ],
        }
    ]
    edits = extract_edits(slides)
    assert edits["s1"]["title"] == "新标题"
    assert edits["s1"]["findings"] == ["要点一", "要点二"]
    assert "杂字" not in str(edits["s1"])


@pytest.fixture()
def conversation_with_deck():
    conversation_id = "conv_ppt_handoff_test"
    conversation = ConversationDTO(
        conversation_id=conversation_id,
        title="PPT 交接用例",
        scene_id="scene_prd_competitor",
        scene_name="竞品企划分析",
        working_memory=WorkingMemoryDTO(scene_locked_by="explicit"),
        created_at=_now(),
        updated_at=_now(),
    )
    CONVERSATIONS[conversation_id] = conversation

    pages = [
        {
            "page_id": "page_s1",
            "order": 1,
            "kind": "chart",
            "title": "纤维结构",
            "objective": "看成分",
            "layout": "chart_left",
            "findings": ["棉占比最高"],
            "narrative": "以棉为主。",
            "bound_artifact_id": "art_chart_demo",
        },
        {
            "page_id": "page_s2",
            "order": 2,
            "kind": "text",
            "title": "落地建议",
            "objective": "给建议",
            "layout": "text_only",
            "findings": [],
            "narrative": "建议继续下探中端价格带。",
        },
    ]
    outline = ArtifactCardDTO(
        artifact_id="art_ppt_outline",
        conversation_id=conversation_id,
        kind="outline_card",
        title="PPT 交接用例汇报大纲",
        subtitle="2 页",
        status="ready",
        payload={"dedupe_key": "main", "status": "ready", "pages": pages},
        created_at=_now(),
        updated_at=_now(),
    )
    slides = [
        {
            "slide_id": "s1",
            "title": "纤维结构",
            "subtitle": "fiber ｜ share",
            "chart_spec": _fixture_chart_spec(),
            "findings": ["棉占比最高"],
            "narrative": "以棉为主。",
            "page_type": "comparison",
            "query_id": "q1",
            "lineage_summary": {"report_page_id": "page_s1"},
        },
        {
            "slide_id": "s2",
            "title": "落地建议",
            "subtitle": "",
            "chart_spec": {},
            "findings": [],
            "narrative": "建议继续下探中端价格带。",
            "page_type": "summary",
            "query_id": None,
            "lineage_summary": {"report_page_id": "page_s2"},
        },
    ]
    deck = ArtifactCardDTO(
        artifact_id="art_ppt_deck",
        conversation_id=conversation_id,
        kind="deck_card",
        title="PPT 交接用例汇报大纲",
        subtitle="2 页 · 16:9",
        status="ready",
        payload={
            "dedupe_key": "main",
            "status": "ready",
            "deck_id": "deck_demo",
            "slides": slides,
            "export_url": "/api/v1/conversations/x/decks/deck_demo/export.pptx",
        },
        created_at=_now(),
        updated_at=_now(),
    )
    ARTIFACTS[outline.artifact_id] = outline
    ARTIFACTS[deck.artifact_id] = deck
    conversation.working_memory.produced_artifact_ids = [outline.artifact_id, deck.artifact_id]
    try:
        yield conversation_id
    finally:
        CONVERSATIONS.pop(conversation_id, None)
        ARTIFACTS.pop(outline.artifact_id, None)
        ARTIFACTS.pop(deck.artifact_id, None)
        for artifact_id in list(MESSAGES.keys()):
            if artifact_id == conversation_id:
                MESSAGES.pop(artifact_id, None)


def test_handoff_then_callback_writes_back_title_and_outline(conversation_with_deck):
    handoff = conversation_service.ppt_handoff(conversation_with_deck)
    assert handoff["ok"] is True
    document = handoff["editor_document"]
    assert len(document["slides"]) == 2
    # 版式来自大纲页：第 1 页 chart_left，第 2 页 text_only
    assert {item["type"] for item in document["slides"][0]["elements"]} >= {"text", "chart"}
    assert not any(item["type"] == "chart" for item in document["slides"][1]["elements"])

    title_element = next(
        item for item in document["slides"][0]["elements"] if item.get("fzRole") == "title"
    )
    title_element["content"] = "<p>纤维结构（已改标题）</p>"

    result = conversation_service.apply_ppt_edits(
        conversation_id=conversation_with_deck,
        editor_document=document,
        message="编辑器同步",
    )
    assert result["ok"] is True and result["changed"] is True

    deck = ARTIFACTS["art_ppt_deck"]
    assert deck.payload["slides"][0]["title"] == "纤维结构（已改标题）"
    # 图表数据没被编辑器改掉
    assert deck.payload["slides"][0]["chart_spec"]["rows"] == _fixture_rows()

    pages = ARTIFACTS["art_ppt_outline"].payload["pages"]
    assert pages[0]["title"] == "纤维结构（已改标题）"

    # 再次打开（非 fresh）应拿到编辑器那一份，不能把用户改的冲掉
    again = conversation_service.ppt_handoff(conversation_with_deck)
    assert again["editor_document"]["slides"][0]["elements"] == document["slides"][0]["elements"]

    # fresh=True 才按大纲重编
    rebuilt = conversation_service.ppt_handoff(conversation_with_deck, fresh=True)
    rebuilt_title = next(
        item for item in rebuilt["editor_document"]["slides"][0]["elements"] if item.get("fzRole") == "title"
    )
    assert "已改标题" in rebuilt_title["content"]


def test_callback_without_changes_is_a_noop(conversation_with_deck):
    handoff = conversation_service.ppt_handoff(conversation_with_deck)
    result = conversation_service.apply_ppt_edits(
        conversation_id=conversation_with_deck,
        editor_document=handoff["editor_document"],
    )
    assert result["ok"] is True and result["changed"] is False
    assert conversation_service.ppt_handoff(conversation_with_deck)["base_revision_id"].endswith(":0")
