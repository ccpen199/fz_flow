"""画布上「用户直接动手」的那些动作的守卫单测。

覆盖三类前端入口对应的后端行为（P1/P2）：

1. 大纲页：直改要点/结论后，编译幻灯片**必须以用户写的为准**；
   拖拽排序要真的换序，没换序就不该推版本。
2. 图表卡：手改 SQL 是「保存」与「执行」两步，保存不推版本、不执行；
   换形态要落到 `spec.chart_type`（渲染真正读的那一份）。
3. 判不出对应维度时**不许猜**（单页重跑、换一批的边界）。

原则（项目硬约束）：**打桩依赖可以，伪造业务数据不行**。
这里一律不构造取数结果：用到的 spec 只有字段名与形态，没有任何行数据；
真正需要 MySQL 的路径（执行 SQL、重新取数）只断言「被拒绝」与拒绝原因。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.canvas_action_service import canvas_action_service
from app.services.canvas_orchestrator import canvas_orchestrator
from app.services.conversation_service import conversation_service
from app.services.report_composer import DEFAULT_PAGE_LAYOUT, report_composer
from app.store import ARTIFACTS, CONVERSATIONS, MESSAGES
from packages.shared_contracts.python_models import (
    ArtifactCardDTO,
    ConversationDTO,
    WorkingMemoryDTO,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _conversation(conversation_id: str) -> ConversationDTO:
    return ConversationDTO(
        conversation_id=conversation_id,
        title="页面动作用例",
        scene_id="scene_prd_competitor",
        scene_name="竞品企划分析",
        working_memory=WorkingMemoryDTO(scene_locked_by="explicit"),
        created_at=_now(),
        updated_at=_now(),
    )


def _page(order: int, title: str, **extra) -> dict:
    page = report_composer._page(
        order=order,
        kind=str(extra.pop("kind", "text")),
        title=title,
        objective=str(extra.pop("objective", f"{title}要回答的问题")),
    )
    page.update(extra)
    return page


def _outline(conversation_id: str, pages: list[dict]) -> ArtifactCardDTO:
    card = ArtifactCardDTO(
        artifact_id="art_page_action_outline",
        conversation_id=conversation_id,
        kind="outline_card",
        title="页面动作大纲",
        subtitle=f"{len(pages)} 页",
        status="ready",
        payload={"dedupe_key": "main", "pages": pages, "status": "ready"},
        created_at=_now(),
        updated_at=_now(),
    )
    ARTIFACTS[card.artifact_id] = card
    return card


@pytest.fixture()
def conv_with_outline() -> str:
    conversation_id = "conv_page_action"
    conversation = _conversation(conversation_id)
    card = _outline(conversation_id, [_page(1, "封面"), _page(2, "价格带"), _page(3, "结论")])
    conversation.working_memory.produced_artifact_ids.append(card.artifact_id)
    conversation.working_memory.active_outline_id = card.artifact_id
    CONVERSATIONS[conversation_id] = conversation
    MESSAGES[conversation_id] = []
    yield conversation_id

    CONVERSATIONS.pop(conversation_id, None)
    MESSAGES.pop(conversation_id, None)
    for artifact_id in [
        item for item, card_item in ARTIFACTS.items() if card_item.conversation_id == conversation_id
    ]:
        ARTIFACTS.pop(artifact_id, None)


@pytest.fixture()
def conv_with_chart() -> str:
    conversation_id = "conv_page_action_chart"
    conversation = _conversation(conversation_id)
    card = ArtifactCardDTO(
        artifact_id="art_page_action_chart",
        conversation_id=conversation_id,
        kind="chart_card",
        title="价格带分布",
        subtitle="价格带",
        status="ready",
        # 只有字段名与形态：没有任何业务数值。
        payload={
            "dimension_key": "price",
            "spec": {
                "chart_type": "bar",
                "x_field": "价格带",
                "y_field": "商品数",
                "series_field": "",
                "columns": ["价格带", "商品数"],
                "stack": True,
            },
            "sql": "SELECT 1",
            "sql_explanation": "占位查询",
            "status": "ready",
        },
        created_at=_now(),
        updated_at=_now(),
    )
    conversation.working_memory.produced_artifact_ids.append(card.artifact_id)
    CONVERSATIONS[conversation_id] = conversation
    MESSAGES[conversation_id] = []
    ARTIFACTS[card.artifact_id] = card
    yield conversation_id

    CONVERSATIONS.pop(conversation_id, None)
    MESSAGES.pop(conversation_id, None)
    for artifact_id in [
        item for item, card_item in ARTIFACTS.items() if card_item.conversation_id == conversation_id
    ]:
        ARTIFACTS.pop(artifact_id, None)


def _pages(conversation_id: str) -> list[dict]:
    return conversation_service.outline_pages(conversation_id)


# --------------------------------------------------------------- 大纲页直改


def test_page_findings_become_authoritative_over_the_chart_card() -> None:
    """用户在某页写的要点与结论，编译时必须盖过图表卡自动生成的洞察。

    否则用户在预览区改的字会被"自动洞察"顶掉——改了跟没改一样。
    """
    pages = [_page(1, "封面"), _page(2, "价格带", kind="chart", bound_artifact_id="art_bound")]
    deck_pages, _ = report_composer.set_page_content(
        pages=pages,
        page_id=pages[1]["page_id"],
        title=None,
        objective=None,
        findings=["用户自己写的要点"],
        narrative="用户自己写的结论",
    )
    _, slides = report_composer.build_deck(
        deck_id="deck_test",
        session_id="conv_test",
        title="测试汇报",
        pages=deck_pages,
        charts_by_id={
            "art_bound": {
                "spec": {"chart_type": "bar", "x_field": "价格带", "y_field": "商品数"},
                "findings": ["图表卡自动生成的洞察"],
                "narrative": "图表卡自动生成的解读",
            }
        },
    )

    assert slides[1].findings == ["用户自己写的要点"]
    assert slides[1].narrative == "用户自己写的结论"
    assert slides[1].lineage_summary["report_page_id"] == deck_pages[1]["page_id"]


def test_update_page_commits_and_reports_whether_anything_changed(
    conv_with_outline: str,
) -> None:
    page_id = _pages(conv_with_outline)[1]["page_id"]

    first = conversation_service.update_outline_page(
        conversation_id=conv_with_outline,
        page_id=page_id,
        findings=["要点 A"],
    )
    assert first["ok"] is True and first["changed"] is True
    assert _pages(conv_with_outline)[1]["findings"] == ["要点 A"]

    # 原样再提交一次：内容没变，不该再推一个版本（重复提交不能刷版本号）。
    second = conversation_service.update_outline_page(
        conversation_id=conv_with_outline,
        page_id=page_id,
        findings=["要点 A"],
    )
    assert second["ok"] is True and second["changed"] is False


def test_update_page_rejects_unknown_layout_without_silently_ignoring(
    conv_with_outline: str,
) -> None:
    page_id = _pages(conv_with_outline)[1]["page_id"]

    result = conversation_service.update_outline_page(
        conversation_id=conv_with_outline,
        page_id=page_id,
        layout="左右横跳",
    )

    assert result["ok"] is False
    assert "版式" in result["error"]
    assert _pages(conv_with_outline)[1]["layout"] == DEFAULT_PAGE_LAYOUT


def test_delete_last_page_is_refused(conv_with_outline: str) -> None:
    """删到只剩一页时必须拒绝：大纲是汇报的骨架，清空它没有可恢复的语义。"""
    pages = _pages(conv_with_outline)
    for page in pages[1:]:
        assert conversation_service.update_outline_page(
            conversation_id=conv_with_outline, page_id=page["page_id"], delete=True
        )["ok"]

    result = conversation_service.update_outline_page(
        conversation_id=conv_with_outline,
        page_id=_pages(conv_with_outline)[0]["page_id"],
        delete=True,
    )

    assert result["ok"] is False
    assert "至少" in result["error"]
    assert len(_pages(conv_with_outline)) == 1


def test_add_page_appends_a_blank_page_without_inventing_content(
    conv_with_outline: str,
) -> None:
    result = conversation_service.add_outline_page(
        conversation_id=conv_with_outline, title="", objective=""
    )

    assert result["ok"] is True
    added = _pages(conv_with_outline)[-1]
    assert added["page_id"] == result["page_id"]
    # 不替用户编内容：要点与结论保持为空，交由用户或编译流程决定。
    assert added["findings"] == []
    assert added["narrative"] == ""


def test_move_page_reorders_and_reports_no_change_when_order_is_same(
    conv_with_outline: str,
) -> None:
    pages = _pages(conv_with_outline)

    moved = conversation_service.move_outline_page(
        conversation_id=conv_with_outline, page_id=pages[2]["page_id"], to_order=1
    )
    assert moved["ok"] is True and moved["changed"] is True
    assert [page["title"] for page in _pages(conv_with_outline)] == ["结论", "封面", "价格带"]

    # 再移到它**已经在**的位置：顺序不变，就不该对外声称改动了。
    same = conversation_service.move_outline_page(
        conversation_id=conv_with_outline,
        page_id=_pages(conv_with_outline)[0]["page_id"],
        to_order=1,
    )
    assert same["ok"] is True and same["changed"] is False


# --------------------------------------------------------------- 图表卡动作


def test_chart_type_change_lands_on_the_spec_not_only_the_payload(
    conv_with_chart: str,
) -> None:
    """换形态必须落进 spec.chart_type —— 渲染读的是它，payload.chart_type 只是个标记。"""
    card = conversation_service.revise_artifact("art_page_action_chart", {"chart_type": "折线图"})

    assert card is not None
    assert card.payload["spec"]["chart_type"] == "line"
    # 形态与堆叠语义必须一起改，否则会出现"折线图却堆叠成一条"。
    assert card.payload["spec"]["stack"] is False


def test_chart_type_change_rejects_a_shape_nobody_can_draw(
    conv_with_chart: str,
) -> None:
    card = conversation_service.revise_artifact("art_page_action_chart", {"chart_type": "雷达图"})

    assert card is not None
    assert card.payload["spec"]["chart_type"] == "bar", "认不出的形态不该静默生效"


def test_manual_sql_save_does_not_run_or_push_a_version(conv_with_chart: str) -> None:
    """保存 ≠ 执行：只保存时不许跑 SQL，也不该推版本（结果没变）。"""
    before = ARTIFACTS["art_page_action_chart"].version

    result = conversation_service.save_chart_sql(
        conversation_id=conv_with_chart,
        artifact_id="art_page_action_chart",
        sql="SELECT 1",
        explanation="手改的说明",
    )

    assert result["ok"] is True
    card = ARTIFACTS["art_page_action_chart"]
    assert card.payload["sql"] == "SELECT 1"
    assert card.payload["sql_edited"] is True
    assert card.version == before
    assert card.history == []


def test_manual_sql_save_refuses_empty_sql(conv_with_chart: str) -> None:
    result = conversation_service.save_chart_sql(
        conversation_id=conv_with_chart, artifact_id="art_page_action_chart", sql="   "
    )

    assert result["ok"] is False
    assert ARTIFACTS["art_page_action_chart"].payload["sql"] == "SELECT 1"


def test_execute_refuses_when_the_conversation_has_no_scene() -> None:
    """没有场景就没有可查的库表：如实拒绝，不去猜一个数据源。"""
    conversation_id = "conv_page_action_no_scene"
    conversation = _conversation(conversation_id)
    conversation.scene_id = ""
    conversation.scene_name = ""
    CONVERSATIONS[conversation_id] = conversation
    MESSAGES[conversation_id] = []
    ARTIFACTS["art_no_scene"] = ArtifactCardDTO(
        artifact_id="art_no_scene",
        conversation_id=conversation_id,
        kind="chart_card",
        title="无场景图表",
        subtitle="",
        status="ready",
        payload={"spec": {}, "sql": "SELECT 1", "status": "ready"},
        created_at=_now(),
        updated_at=_now(),
    )
    try:
        result = conversation_service.execute_chart_sql(
            conversation_id=conversation_id, artifact_id="art_no_scene"
        )
        assert result["ok"] is False
        assert result["error"] == "scene not found"
    finally:
        ARTIFACTS.pop("art_no_scene", None)
        CONVERSATIONS.pop(conversation_id, None)
        MESSAGES.pop(conversation_id, None)


def test_regenerate_refuses_when_the_page_maps_to_no_dimension(
    conv_with_outline: str,
) -> None:
    """页面对不上任何真实维度时不许随便挑一个跑，要说清"对不上"。"""
    page_id = _pages(conv_with_outline)[0]["page_id"]

    result = conversation_service.regenerate_page(
        conversation_id=conv_with_outline, page_id=page_id
    )

    assert result["ok"] is False
    assert "没有对应到具体分析维度" in result["message"]


def test_page_dimension_key_prefers_the_bound_chart(conv_with_chart: str) -> None:
    options = [{"dimension_key": "price", "title": "价格带分布"}]

    assert (
        conversation_service._page_dimension_key(
            page={"bound_artifact_id": "art_page_action_chart", "title": "随便写的标题"},
            options=options,
        )
        == "price"
    )
    # 标题能对上也算数（用户手写的页标题常常就是维度名）。
    assert (
        conversation_service._page_dimension_key(
            page={"bound_artifact_id": None, "title": "价格带分布"}, options=options
        )
        == "price"
    )
    # 都对不上就返回空串——上层据此如实追问，而不是挑一个近似维度。
    assert (
        conversation_service._page_dimension_key(
            page={"bound_artifact_id": None, "title": "完全无关的一页"}, options=options
        )
        == ""
    )


# --------------------------------------------------------------- 槽位校验


def test_page_ops_reject_out_of_range_index() -> None:
    ops, rejected = canvas_action_service.normalize_page_ops(
        [{"op": "remove_page", "page_index": 9}], page_count=4
    )

    assert ops == []
    assert rejected and "第 9 页不存在（当前共 4 页）" in rejected[0]


def test_layout_label_matches_the_vocabulary_the_canvas_uses() -> None:
    for layout in canvas_action_service.ALLOWED_PAGE_LAYOUTS:
        assert canvas_action_service.PAGE_LAYOUT_LABELS[layout]


# ------------------------------------------------------- 点名合并（merge_pages）


def test_merge_pages_keeps_the_chart_and_folds_the_other_page() -> None:
    """「把第 2、3 页合并，突出混纺趋势」：图要留下，另一页的话要折进来。"""
    pages = [
        _page(1, "封面"),
        _page(2, "价格带", kind="chart", bound_artifact_id="art_merge_chart"),
        _page(3, "混纺趋势", objective="纤维占比的结构变化"),
    ]

    merged, info = report_composer.merge_pages(
        pages=pages, page_indexes=[2, 3], emphasis="混纺趋势"
    )

    assert len(merged) == 2
    keeper = next(page for page in merged if page["order"] == 2)
    assert keeper["bound_artifact_id"] == "art_merge_chart", "合并不能把图丢了"
    assert "混纺趋势" in keeper["objective"], "「突出 X」必须真的落到这一页上"
    assert any("纤维占比的结构变化" in item for item in keeper["findings"])
    assert info["merged_from"] == [2, 3]


def test_merge_pages_adopts_the_chart_when_the_keeper_has_none() -> None:
    """保留页是文案、被合并页有图 → 把图接过来，而不是丢掉。"""
    pages = [
        _page(1, "封面"),
        _page(2, "文字页"),
        _page(3, "图表页", kind="chart", bound_artifact_id="art_donor"),
    ]

    merged, _ = report_composer.merge_pages(pages=pages, page_indexes=[2, 3])

    keeper = next(page for page in merged if page["order"] == 2)
    assert keeper["bound_artifact_id"] == "art_donor"
    assert keeper["kind"] == "chart"


def test_merge_pages_refuses_fewer_than_two_real_pages() -> None:
    pages = [_page(1, "封面"), _page(2, "价格带")]

    merged, info = report_composer.merge_pages(pages=pages, page_indexes=[2])

    assert merged == pages
    assert info == {}


def test_normalize_merge_pages_keeps_emphasis_and_drops_out_of_range() -> None:
    ops, rejected = canvas_action_service.normalize_page_ops(
        [
            {"op": "merge_pages", "page_indexes": [2, 3], "emphasis": "混纺趋势"},
            {"op": "merge_pages", "page_indexes": [2, 9]},
            {"op": "merge_pages", "page_indexes": [3]},
        ],
        page_count=4,
    )

    assert ops == [
        {"op": "merge_pages", "page_indexes": [2, 3], "emphasis": "混纺趋势"}
    ]
    # 只有一页有效 / 越界到只剩一页 → 如实拒绝，不猜用户想合哪两页。
    assert len(rejected) == 2
    assert all("至少要说两页" in item for item in rejected)


def test_report_build_text_states_the_pages_actually_built() -> None:
    """用户要 4 页但只能搭 2 页时，回复必须说 2 页，并说清为什么不够。"""
    plan = canvas_orchestrator.plan_report_build(
        brand="优衣库",
        page_count=4,
        chart_cards=[],
        outline_preview=[
            {"order": 1, "kind": "text", "title": "复盘", "objective": "封面", "bound_artifact_id": None},
            {"order": 2, "kind": "text", "title": "建议", "objective": "落地", "bound_artifact_id": None},
        ],
    )

    text = plan["assistant_text"]
    # 大纲在**画布里**（右侧现在是 PPT 编辑器，出稿前不该说"右侧已搭好 PPT"）。
    assert "2 页的大纲" in text
    assert "你要的是 4 页" in text
    # 主题没给 → 如实说"还需要你定"，不能假装已经定了。
    assert "主题还需要你定一下" in text
    assert "确认" in text


def test_outline_edit_refreshes_an_existing_deck_preview(conv_with_outline: str) -> None:
    """画布上已有 PPT 时，大纲改完预览必须跟着变（源变了投影不能还停在旧版）。"""
    deck = ArtifactCardDTO(
        artifact_id="art_page_action_deck",
        conversation_id=conv_with_outline,
        kind="deck_card",
        title="汇报预览",
        subtitle="3 页 · 16:9",
        status="ready",
        payload={"dedupe_key": "main", "deck_id": "deck_old", "slides": [], "status": "ready"},
        created_at=_now(),
        updated_at=_now(),
    )
    ARTIFACTS[deck.artifact_id] = deck
    CONVERSATIONS[conv_with_outline].working_memory.produced_artifact_ids.append(deck.artifact_id)

    conversation_service._handle_revision(
        conv_with_outline,
        {"page_ops": [{"op": "remove_page", "page_index": 2}]},
    )

    refreshed = ARTIFACTS[deck.artifact_id]
    assert len(_pages(conv_with_outline)) == 2
    assert len(refreshed.payload["slides"]) == 2, "预览必须按新大纲重装配"
    assert refreshed.version == 2


def test_chat_merge_lands_on_the_outline_and_pushes_a_version(
    conv_with_outline: str,
) -> None:
    """对话里的合并要真的改到画布，并且留下可回退的版本。"""
    result = conversation_service._handle_revision(
        conv_with_outline,
        {
            "page_ops": [
                {"op": "merge_pages", "page_indexes": [2, 3], "emphasis": "混纺趋势"}
            ]
        },
    )

    pages = _pages(conv_with_outline)
    assert len(pages) == 2
    assert "混纺趋势" in pages[1]["objective"]
    assert "已把第 2、3 页合并" in result["assistant_text"]
    assert "混纺趋势" in result["assistant_text"]
    # 合并不是"整页替换"：措辞不能说自己把第 2 页换成了别的标题。
    assert "已替换为" not in result["assistant_text"]

    outline = conversation_service._outline_card(conv_with_outline)
    assert outline is not None and outline.history, "合并后必须能回退"
