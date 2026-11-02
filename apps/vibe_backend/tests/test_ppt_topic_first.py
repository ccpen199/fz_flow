"""「先定主题」这条流程的守卫单测。

用户原话（2026-10-26）："主题和大纲在哪 而且不是应该先输入主题吗？"

TrainPPT 的顺序是「输入主题 → 生成大纲 → 确认 → 出稿」。后端替用户先起一个
「品牌 + 场景复盘」当主题看着很顺，但那不是他这次要讲的东西 —— 他得先改题、
再改页，等于白做一轮。所以没主题时必须**先问**。

覆盖：

1. 只说"串个 4 页 PPT"（没说主题）→ 出 `needs_topic` 占位卡，**一页都不许搭**；
2. 主题为空/空白 → 提交被拒（不能拿空白当主题，那是出稿依据）；
3. 给了主题 → 按主题搭大纲，**封面标题就是主题**，仍 `confirmed=False`，出稿被拒；
4. agent 直接从原话里抽到主题（`report_topic`）→ 跳过追问，直接搭页。

原则（项目硬约束）：打桩依赖可以，伪造业务数据不行。这里的图表 spec 只给字段名与
形态，**没有任何行数据**。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.conversation_service import conversation_service
from app.store import ARTIFACTS, CONVERSATIONS, MESSAGES
from packages.shared_contracts.python_models import (
    ArtifactCardDTO,
    ConversationDTO,
    WorkingMemoryDTO,
)

TOPIC = "优衣库 2026 春夏面料与价格复盘"


def _now() -> datetime:
    return datetime.now(UTC)


@pytest.fixture()
def conv_with_chart() -> str:
    conversation_id = "conv_topic_first"
    conversation = ConversationDTO(
        conversation_id=conversation_id,
        # 不挂配置：这条流程只用到「会话里已有的图表」，取数不参与。
        scene_id="",
        scene_name="",
        title="先定主题用例",
        working_memory=WorkingMemoryDTO(brand="优衣库"),
        created_at=_now(),
        updated_at=_now(),
    )
    CONVERSATIONS[conversation_id] = conversation
    MESSAGES[conversation_id] = []
    card = ArtifactCardDTO(
        artifact_id="art_topic_c1",
        conversation_id=conversation_id,
        kind="chart_card",
        title="面料成分",
        subtitle="fiber",
        status="ready",
        payload={
            "dimension_key": "fiber",
            # 只有字段名与形态：没有任何业务数值。
            "spec": {
                "chart_type": "bar",
                "x_field": "类目",
                "y_field": "数量",
                "columns": ["类目", "数量"],
            },
            "sql": "SELECT 1",
            "sql_explanation": "占位查询",
        },
        created_at=_now(),
        updated_at=_now(),
    )
    ARTIFACTS["art_topic_c1"] = card
    conversation.working_memory.chart_artifact_ids.append("art_topic_c1")
    conversation.working_memory.produced_artifact_ids.append("art_topic_c1")
    yield conversation_id

    CONVERSATIONS.pop(conversation_id, None)
    MESSAGES.pop(conversation_id, None)
    for artifact_id in [
        item for item, card_item in ARTIFACTS.items() if card_item.conversation_id == conversation_id
    ]:
        ARTIFACTS.pop(artifact_id, None)


def test_report_build_without_topic_asks_first_and_builds_no_pages(
    conv_with_chart: str,
) -> None:
    plan = conversation_service._handle_report_build(
        conv_with_chart, None, {"page_count": 4}
    )

    assert plan["needs_topic"] is True
    assert plan["outline_preview"] == []

    card = conversation_service._outline_card(conv_with_chart)
    assert card is not None
    assert card.payload["status"] == "needs_topic"
    # 一页都不许搭：主题没定之前，页序是猜的。
    assert card.payload["pages"] == []
    assert card.payload["topic"] == ""
    # 卡片标题也不能是替他起的题（"XX复盘"）。
    assert "复盘" not in card.title
    # 也不许顺手出稿。
    assert conversation_service._deck_card(conv_with_chart) is None


def test_empty_topic_is_refused(conv_with_chart: str) -> None:
    conversation_service._handle_report_build(conv_with_chart, None, {"page_count": 4})

    result = conversation_service.set_outline_topic(conv_with_chart, topic="   ")

    assert result["ok"] is False
    assert result["error"] == "topic_required"
    card = conversation_service._outline_card(conv_with_chart)
    assert card.payload["status"] == "needs_topic"
    assert card.payload["pages"] == []


def test_set_outline_topic_builds_pages_with_the_topic_as_cover(
    conv_with_chart: str,
) -> None:
    conversation_service._handle_report_build(conv_with_chart, None, {"page_count": 4})

    result = conversation_service.set_outline_topic(conv_with_chart, topic=TOPIC)

    assert result["ok"] is True
    assert result["topic"] == TOPIC
    assert result["page_count"] >= 2

    card = conversation_service._outline_card(conv_with_chart)
    pages = card.payload["pages"]
    # 第一步的输入就是这一步的标题：主题 = 封面 = 最终 deck 标题。
    assert pages[0]["title"] == TOPIC
    assert card.payload["topic"] == TOPIC
    assert card.payload["status"] == "ready"
    # 仍然待确认：主题定了不等于可以出稿。
    assert card.payload["confirmed"] is False
    assert card.title == TOPIC

    refused = conversation_service.compile_deck(conv_with_chart)
    assert refused["ok"] is False
    assert refused["error"] == "outline_not_confirmed"


def test_report_build_turn_does_not_claim_config_is_missing() -> None:
    """纯做汇报的一轮**不取数**：页头不能说"判定需要配置，但没确定是哪一套"。

    2026-10-26 实测：换成"先定主题"之后，第一轮 PPT 的页头就顶着这句话，
    用户会以为配置漏了。
    """
    from app.services.conversation_service import plan_turn_config

    scene, info, _ = plan_turn_config(intent="report_build", filters=[], config_keys=[])

    assert scene is None
    assert info["note"] == "本轮做汇报，不取数"


def test_topic_from_the_user_words_skips_the_question(conv_with_chart: str) -> None:
    plan = conversation_service._handle_report_build(
        conv_with_chart,
        None,
        {"page_count": 3, "report_topic": TOPIC},
    )

    assert not plan.get("needs_topic")
    assert plan.get("needs_outline_confirm") is True

    card = conversation_service._outline_card(conv_with_chart)
    assert card.payload["status"] == "ready"
    assert card.payload["pages"]
    assert card.payload["pages"][0]["title"] == TOPIC
    assert card.payload["confirmed"] is False
    assert conversation_service._deck_card(conv_with_chart) is None
