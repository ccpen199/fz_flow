"""「主题 → 大纲 → 确认 → 成稿」与「会话内图表编号」的守卫单测。

覆盖：

1. 图表编号按**会话内创建顺序**分配，且只增不改（删图不让后面的图换号）；
   编号要同时出现在画布卡片（`chart_no`）与图表视图（`label="图N"`）上。
2. 大纲未确认时 `compile_deck` 必须拒绝出稿；确认（带主题）之后才产出 deck。
3. 没有主题不许确认——"主题"是出稿依据，不能拿空白当主题。
4. 用户用「图2」说话时，`_handle_chart_tweak` 按编号取图，不用他记维度名。

原则（项目硬约束）：打桩依赖可以，伪造业务数据不行。这里所有 spec 只给字段名与形态，
**没有任何行数据**。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.conversation_service import conversation_service
from app.services.report_composer import report_composer
from app.store import ARTIFACTS, CONVERSATIONS, MESSAGES
from packages.shared_contracts.python_models import (
    ArtifactCardDTO,
    ConversationDTO,
    WorkingMemoryDTO,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _chart(conversation_id: str, artifact_id: str, title: str, dimension_key: str) -> ArtifactCardDTO:
    return ArtifactCardDTO(
        artifact_id=artifact_id,
        conversation_id=conversation_id,
        kind="chart_card",
        title=title,
        subtitle=dimension_key,
        status="ready",
        payload={
            "dimension_key": dimension_key,
            # 只有字段名与形态：没有任何业务数值。
            "spec": {
                "chart_type": "bar",
                "x_field": "类目",
                "y_field": "数量",
                "series_field": "",
                "columns": ["类目", "数量"],
            },
            "sql": "SELECT 1",
            "sql_explanation": "占位查询",
        },
        created_at=_now(),
        updated_at=_now(),
    )


@pytest.fixture()
def conv_with_two_charts() -> str:
    conversation_id = "conv_ppt_flow"
    conversation = ConversationDTO(
        conversation_id=conversation_id,
        title="PPT 流程用例",
        scene_id="scene_prd_competitor",
        scene_name="竞品企划分析",
        working_memory=WorkingMemoryDTO(scene_locked_by="explicit"),
        created_at=_now(),
        updated_at=_now(),
    )
    CONVERSATIONS[conversation_id] = conversation
    MESSAGES[conversation_id] = []
    for index, (aid, title, key) in enumerate(
        [
            ("art_flow_c1", "面料成分", "fiber"),
            ("art_flow_c2", "价格带", "price"),
        ],
        start=1,
    ):
        card = _chart(conversation_id, aid, title, key)
        ARTIFACTS[aid] = card
        conversation.working_memory.chart_artifact_ids.append(aid)
        conversation.working_memory.produced_artifact_ids.append(aid)
    yield conversation_id

    CONVERSATIONS.pop(conversation_id, None)
    MESSAGES.pop(conversation_id, None)
    for artifact_id in [
        item for item, card_item in ARTIFACTS.items() if card_item.conversation_id == conversation_id
    ]:
        ARTIFACTS.pop(artifact_id, None)


def _save_outline(conversation_id: str, *, confirmed: bool | None = None, topic: str | None = None):
    pages = [
        report_composer._page(order=1, kind="text", title="封面", objective="说明范围"),
        report_composer._page(
            order=2,
            kind="chart",
            title="面料成分",
            objective="看结构",
            bound_artifact_id="art_flow_c1",
            bound_chart_title="面料成分",
        ),
    ]
    return conversation_service.save_outline(
        conversation_id, pages, topic=topic, confirmed=confirmed
    )


def test_chart_numbers_follow_creation_order_and_are_exposed(conv_with_two_charts: str) -> None:
    canvas = conversation_service.canvas(conv_with_two_charts)
    numbers = {
        item["artifact_id"]: item.get("chart_no")
        for item in canvas["items"]
        if item["kind"] == "chart_card"
    }
    assert numbers == {"art_flow_c1": 1, "art_flow_c2": 2}

    view = conversation_service._chart_card_view(ARTIFACTS["art_flow_c2"])
    assert view["no"] == 2
    assert view["label"] == "图2"


def test_chart_number_survives_removing_an_earlier_chart(conv_with_two_charts: str) -> None:
    """删掉图1 之后，图2 必须还叫图2（编号只增不改，否则用户记的号全错位）。"""
    # 先让编号发出去（用户已经在屏幕上看到 图1 / 图2 了）。
    conversation_service.canvas(conv_with_two_charts)

    conversation = CONVERSATIONS[conv_with_two_charts]
    conversation.working_memory.chart_artifact_ids = ["art_flow_c2"]
    conversation.working_memory.produced_artifact_ids = ["art_flow_c2"]

    canvas = conversation_service.canvas(conv_with_two_charts)
    chart_items = [item for item in canvas["items"] if item["kind"] == "chart_card"]
    assert [item["chart_no"] for item in chart_items] == [2]


def test_compile_is_blocked_until_outline_is_confirmed(conv_with_two_charts: str) -> None:
    _save_outline(conv_with_two_charts, confirmed=False, topic="竞品复盘")

    refused = conversation_service.compile_deck(conv_with_two_charts)
    assert refused["ok"] is False
    assert refused["error"] == "outline_not_confirmed"
    assert "确认" in refused["message"]


def test_confirm_without_topic_is_refused(conv_with_two_charts: str) -> None:
    _save_outline(conv_with_two_charts, confirmed=False, topic="")
    result = conversation_service.confirm_outline(conv_with_two_charts)
    assert result["ok"] is False
    assert result["error"] == "topic_missing"
    # 拒绝了就不能偷偷出稿。
    assert not conversation_service._has_deck_card(conv_with_two_charts)


def test_confirm_sets_topic_and_produces_the_deck(conv_with_two_charts: str) -> None:
    _save_outline(conv_with_two_charts, confirmed=False, topic="")

    result = conversation_service.confirm_outline(
        conv_with_two_charts, topic="优衣库 面料与价格复盘"
    )
    assert result["ok"] is True
    assert result["topic"] == "优衣库 面料与价格复盘"
    assert result["slide_count"] >= 1

    outline = conversation_service.outline_card(conv_with_two_charts)
    assert outline.payload["confirmed"] is True
    assert outline.payload["topic"] == "优衣库 面料与价格复盘"
    # 出稿后标题就是主题（PPT 标题不再是我们编的"XX汇报大纲"）。
    assert outline.title == "优衣库 面料与价格复盘"

    deck = conversation_service._deck_card(conv_with_two_charts)
    assert deck is not None
    assert deck.payload["deck_id"].startswith("deck_")


def test_chart_tweak_targets_the_chart_by_number(conv_with_two_charts: str) -> None:
    result = conversation_service._handle_chart_tweak(
        conv_with_two_charts,
        scene=None,  # 换形态不需要配置：只改画法，不重跑数
        routing={"raw": "把图2换成饼图", "chart_type": "pie"},
    )
    assert "图2" in result["assistant_text"]
    assert result["artifact_refs"] == ["art_flow_c2"]
    assert ARTIFACTS["art_flow_c2"].payload["spec"]["chart_type"] == "pie"
    # 图1 不能被动到。
    assert ARTIFACTS["art_flow_c1"].payload["spec"]["chart_type"] == "bar"


def test_chart_tweak_says_which_numbers_exist_when_number_is_unknown(
    conv_with_two_charts: str,
) -> None:
    result = conversation_service._handle_chart_tweak(
        conv_with_two_charts,
        scene=None,
        routing={"raw": "把图9换成饼图", "chart_type": "pie"},
    )
    assert "没有图 9" in result["assistant_text"]
    assert result["artifact_refs"] == []
