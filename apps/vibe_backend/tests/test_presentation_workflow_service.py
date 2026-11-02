from app.services.presentation_workflow_service import presentation_workflow_service
from app.routers.analysis_sessions import _presentation_turn_kind
from app.store import DECK_REVISIONS, PRESENTATION_TURNS, SLIDE_DRAFTS, SLIDE_REVISIONS
from packages.shared_contracts.python_models import DeckDTO, SlideDraftDTO


def _slide(slide_id: str, title: str, query_id: str) -> SlideDraftDTO:
    return SlideDraftDTO(
        slide_id=slide_id,
        session_id="session_presentation",
        query_id=query_id,
        page_type="overview",
        title=title,
        subtitle="原始副标题",
        chart_spec={"chart_type": "line", "rows": [{"维度": "A", "指标": 1}]},
        findings=["原始结论"],
        narrative="原始叙述",
        recommendations=["原始建议"],
    )


def _reset() -> None:
    SLIDE_DRAFTS.clear()
    PRESENTATION_TURNS.clear()
    SLIDE_REVISIONS.clear()
    DECK_REVISIONS.clear()


def test_revision_creates_new_slide_and_restore_keeps_history() -> None:
    _reset()
    deck = DeckDTO(deck_id="deck_presentation", session_id="session_presentation", title="测试 Deck")
    first = _slide("slide_original", "品牌价格分析", "query_1")
    second = _slide("slide_second", "材质结构分析", "query_2")

    first_revision = presentation_workflow_service.append_slide(
        deck=deck,
        slide=first,
        turn_id="turn_1",
        change_summary="新增第一页",
    )
    presentation_workflow_service.append_slide(
        deck=deck,
        slide=second,
        turn_id="turn_2",
        change_summary="新增第二页",
    )

    plan = presentation_workflow_service.rule_plan(
        message="第1页标题改为品牌价格结构，换成柱状图",
        deck=deck,
    )
    changed, revised_deck = presentation_workflow_service.apply_revision(
        deck=deck,
        target_slide_ids=plan["target_slide_ids"],
        patch=plan["patch"],
        turn_id="turn_3",
        change_summary="修改第一页标题和图表",
    )

    assert len(changed) == 1
    assert changed[0].slide_id != first.slide_id
    assert changed[0].query_id == "query_1"
    assert changed[0].title == "品牌价格结构"
    assert changed[0].chart_spec["chart_type"] == "bar"
    assert SLIDE_DRAFTS[first.slide_id].title == "品牌价格分析"
    assert deck.slide_ids == [changed[0].slide_id, second.slide_id]
    assert revised_deck.parent_deck_revision_id is not None

    restored = presentation_workflow_service.restore_deck_revision(
        deck=deck,
        target_revision_id=first_revision.deck_revision_id,
        turn_id="turn_4",
    )

    assert deck.slide_ids == [first.slide_id]
    assert restored.version == 4
    assert len(presentation_workflow_service.list_deck_revisions(deck.deck_id)) == 4
    assert len(presentation_workflow_service.list_slide_revisions(session_id=deck.session_id)) == 3


def test_external_plan_drops_non_presentation_fields() -> None:
    _reset()
    deck = DeckDTO(deck_id="deck_safe", session_id="session_presentation", title="测试 Deck", slide_ids=["slide_1"])
    SLIDE_DRAFTS["slide_1"] = _slide("slide_1", "原始标题", "query_1")

    plan = presentation_workflow_service.external_plan(
        raw_response='{"target_slide_index": 1, "patch": {"title": "新标题", "sql": "DELETE FROM clothing_info"}}',
        deck=deck,
    )

    assert plan is not None
    assert plan["target_slide_ids"] == ["slide_1"]
    assert plan["patch"] == {"title": "新标题"}


def test_turn_router_keeps_new_analysis_separate_from_presentation_edits() -> None:
    assert _presentation_turn_kind("第1页标题改为品牌价格结构，换成柱状图", "auto") == "presentation_revision"
    assert _presentation_turn_kind("第3页改为按品类分析", "auto") == "analysis"


def test_editor_document_sync_creates_revision_and_rejects_stale_base() -> None:
    _reset()
    deck = DeckDTO(deck_id="deck_editor", session_id="session_presentation", title="编辑器 Deck")
    source = _slide("slide_editor", "原始标题", "query_editor")
    initial = presentation_workflow_service.append_slide(
        deck=deck,
        slide=source,
        turn_id=None,
        change_summary="初始化",
    )
    document = presentation_workflow_service.build_editor_document(deck)
    document["slides"][0]["elements"][0]["content"] = '<p style="font-size: 28px;">画布标题</p>'
    document["slides"][0]["elements"][2]["content"] = '<p style="font-size: 13px;">• 画布结论</p>'
    document["theme"]["fontName"] = "Arial"
    changed, revision = presentation_workflow_service.apply_editor_document(
        deck=deck,
        editor_document=document,
        base_revision_id=initial.deck_revision_id,
        turn_id=None,
        change_summary="画布同步",
    )

    assert changed[0].title == "画布标题"
    assert changed[0].findings == ["画布结论"]
    assert deck.style_tokens["font_family"] == "Arial"
    assert revision.version == 2
    assert deck.slide_ids == [changed[0].slide_id]

    try:
        presentation_workflow_service.apply_editor_document(
            deck=deck,
            editor_document=document,
            base_revision_id=initial.deck_revision_id,
            turn_id=None,
            change_summary="过期同步",
        )
    except ValueError as exc:
        assert "stale" in str(exc)
    else:
        raise AssertionError("stale editor document should be rejected")


def test_switch_deck_revision_restores_existing_snapshot_without_new_revision() -> None:
    _reset()
    deck = DeckDTO(deck_id="deck_switch", session_id="session_presentation", title="初始 Deck")
    source = _slide("slide_switch", "原始标题", "query_switch")
    SLIDE_DRAFTS[deck.session_id] = source
    initial = presentation_workflow_service.append_slide(
        deck=deck,
        slide=source,
        turn_id=None,
        change_summary="初始化",
    )
    document = presentation_workflow_service.build_editor_document(deck)
    document["title"] = "修改后 Deck"
    presentation_workflow_service.apply_editor_document(
        deck=deck,
        editor_document=document,
        base_revision_id=initial.deck_revision_id,
        turn_id=None,
        change_summary="改标题",
    )

    switched = presentation_workflow_service.switch_deck_revision(
        deck=deck,
        target_revision_id=initial.deck_revision_id,
    )

    assert switched.current_revision_id == initial.deck_revision_id
    assert deck.title == "初始 Deck"
    assert deck.slide_ids == [source.slide_id]
    assert SLIDE_DRAFTS[deck.session_id].slide_id == source.slide_id
    assert len(presentation_workflow_service.list_deck_revisions(deck.deck_id)) == 2


def test_editor_document_preserves_findings_lines_and_invalidates_after_new_slide() -> None:
    _reset()
    deck = DeckDTO(deck_id="deck_editor_cache", session_id="session_presentation", title="缓存测试")
    source = _slide("slide_cache", "原始标题", "query_cache")
    initial = presentation_workflow_service.append_slide(
        deck=deck,
        slide=source,
        turn_id=None,
        change_summary="初始化",
    )
    document = presentation_workflow_service.build_editor_document(deck)
    document["slides"][0]["elements"][2]["content"] = "<p>• 结论一<br>• 结论二</p>"

    changed, _ = presentation_workflow_service.apply_editor_document(
        deck=deck,
        editor_document=document,
        base_revision_id=initial.deck_revision_id,
        turn_id=None,
        change_summary="画布同步",
    )

    assert changed[0].findings == ["结论一", "结论二"]
    assert deck.editor_document is not None

    presentation_workflow_service.append_slide(
        deck=deck,
        slide=_slide("slide_cache_second", "第二页", "query_cache_second"),
        turn_id=None,
        change_summary="新增第二页",
    )
    assert deck.editor_document is None
    rebuilt = presentation_workflow_service.build_editor_document(deck)
    assert len(rebuilt["slides"]) == 2
