"""确认卡片的**幂等**守卫。

背景（真实数据里发现的缺陷）：`conv_7a4cccfb49` 里同一条助手回复重复出现 17 次，
澄清卡 `version=18`。原因是 `_apply_resolution` / `_apply_clarification` 每次调用都会
重新取数并再追加一条助手消息——前端双击一次就翻倍。

原则（项目硬约束）：**打桩依赖可以，伪造业务数据不行**。这里只把取数引擎
`_execute_goals` 换成一个记录调用次数的桩，断言的是"有没有重复执行"这个事实，
不去编造任何 rows / spec。
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.services.conversation_service import ConversationService, conversation_service
from app.store import ARTIFACTS, CONVERSATIONS, MESSAGES
from packages.shared_contracts.python_models import ArtifactCardDTO, ConversationDTO, WorkingMemoryDTO


def _now() -> datetime:
    return datetime.now(UTC)


def _conversation(conversation_id: str) -> ConversationDTO:
    now = _now()
    return ConversationDTO(
        conversation_id=conversation_id,
        title="幂等用例",
        scene_id="scene_prd_competitor",
        scene_name="竞品企划分析",
        working_memory=WorkingMemoryDTO(scene_locked_by="explicit"),
        created_at=now,
        updated_at=now,
    )


def _card(conversation_id: str, *, kind: str, status: str) -> ArtifactCardDTO:
    now = _now()
    return ArtifactCardDTO(
        artifact_id=f"art_idem_{kind}",
        conversation_id=conversation_id,
        kind=kind,  # type: ignore[arg-type]
        title="品牌「THE NORTH FACE」需要确认",
        subtitle="标准字典命中 2 个候选 · 选定后立即取数",
        status=status,
        payload={
            "dedupe_key": "field:Name",
            "term": "THE NORTH FACE",
            "field": {
                "semantic_name": "品牌",
                "table_name": "dict_brand_info",
                "field_name": "Name",
            },
            "options": [
                {"value": "THE NORTH FACE（中国）", "count": 12},
                {"value": "THE NORTH FACE（日本）", "count": 11},
            ],
            "dimension_keys": ["price"],
            "time_window": "近 5 个月",
            "status": status,
            "questions": ["按哪个口径统计？"],
        },
        created_at=now,
        updated_at=now,
    )


@pytest.fixture()
def seeded(request: pytest.FixtureRequest):
    """注册一个会话 + 一张指定状态的确认卡，并把取数引擎换成计数桩。

    不传 `indirect` 参数时默认造一张「品牌消歧卡」——只有需要枚举两种卡片的
    用例才去参数化。
    """
    kind, status = getattr(request, "param", ("resolution_card", "needs_confirmation"))
    conversation_id = f"conv_idem_{kind}"
    conversation = _conversation(conversation_id)
    card = _card(conversation_id, kind=kind, status=status)
    conversation.working_memory.produced_artifact_ids.append(card.artifact_id)
    CONVERSATIONS[conversation_id] = conversation
    MESSAGES[conversation_id] = []
    ARTIFACTS[card.artifact_id] = card

    calls: list[str] = []
    original = ConversationService._execute_goals

    def _stub(self, **kwargs):  # noqa: ANN001, ANN202
        calls.append(str(kwargs.get("keys")))
        return {"assistant_text": "（桩：未真实取数）", "artifact_refs": [], "actions": []}

    ConversationService._execute_goals = _stub
    try:
        yield conversation_id, card, calls
    finally:
        ConversationService._execute_goals = original
        CONVERSATIONS.pop(conversation_id, None)
        MESSAGES.pop(conversation_id, None)
        for artifact_id in [
            aid for aid, item in ARTIFACTS.items() if item.conversation_id == conversation_id
        ]:
            ARTIFACTS.pop(artifact_id, None)


def _apply(kind: str, conversation_id: str, artifact_id: str) -> dict:
    if kind == "resolution_card":
        return conversation_service.apply_artifact_action(
            conversation_id=conversation_id,
            artifact_id=artifact_id,
            value="THE NORTH FACE（中国）",
        )
    return conversation_service.apply_artifact_action(
        conversation_id=conversation_id,
        artifact_id=artifact_id,
        answer="按吊牌口径统计",
    )


@pytest.mark.parametrize(
    "seeded",
    [("resolution_card", "needs_confirmation"), ("clarification_card", "needs_clarification")],
    indirect=True,
)
def test_first_submit_still_executes_and_leaves_a_marker(seeded) -> None:
    """第一次提交必须正常执行，并把卡片标成已结算（否则闸门永远不生效）。"""
    conversation_id, card, calls = seeded

    out = _apply(card.kind, conversation_id, card.artifact_id)

    assert out.get("ok") is True
    assert len(calls) == 1, "第一次提交应该真的跑一次"
    assert len(MESSAGES[conversation_id]) == 1
    assert conversation_service._checkpoint_settled(ARTIFACTS[card.artifact_id]) is True


@pytest.mark.parametrize(
    "seeded",
    [("resolution_card", "needs_confirmation"), ("clarification_card", "needs_clarification")],
    indirect=True,
)
def test_second_submit_is_a_no_op(seeded) -> None:
    """已确认的卡再提交一次：不能再取数，也不能再追加助手消息。"""
    conversation_id, card, calls = seeded

    _apply(card.kind, conversation_id, card.artifact_id)
    messages_after_first = len(MESSAGES[conversation_id])

    repeat = _apply(card.kind, conversation_id, card.artifact_id)

    assert repeat.get("ok") is True
    assert repeat.get("idempotent") is True
    assert len(calls) == 1, "重复提交不该再触发一次取数"
    assert len(MESSAGES[conversation_id]) == messages_after_first, "重复提交不该再追加回复"
    assert repeat.get("canvas", {}).get("items") is not None, "应回传当前画布供前端重绘"


@pytest.mark.parametrize(
    "seeded",
    [("resolution_card", "needs_confirmation"), ("clarification_card", "needs_clarification")],
    indirect=True,
)
def test_double_click_does_not_multiply_replies(seeded) -> None:
    """模拟前端连点 5 次——消息条数必须只 +1。"""
    conversation_id, card, calls = seeded

    for _ in range(5):
        _apply(card.kind, conversation_id, card.artifact_id)

    assert len(calls) == 1
    assert len(MESSAGES[conversation_id]) == 1


def test_chart_requery_is_not_blocked_by_the_gate(seeded) -> None:
    """参数抽屉是允许反复重跑的，不能被幂等闸门误伤。"""
    conversation_id, card, calls = seeded
    card.kind = "chart_card"
    card.status = "ready"
    card.payload = {**card.payload, "status": "ready", "dimension_key": "price"}

    assert conversation_service._checkpoint_settled(card) is True

    out = conversation_service.apply_artifact_action(
        conversation_id=conversation_id,
        artifact_id=card.artifact_id,
        params={"time_window": "近 3 个月"},
    )

    assert out.get("ok") is True
    assert len(calls) == 1, "chart_card 必须仍然可以重新取数"


def test_gate_does_not_fire_on_an_unanswered_card(seeded) -> None:
    """没答过的卡不能被误判成已结算。"""
    _, card, _ = seeded
    assert conversation_service._checkpoint_settled(card) is False


def test_rerun_reusing_the_same_card_keeps_the_new_question(monkeypatch: pytest.MonkeyPatch) -> None:
    """点了口径之后**又**空结果时，卡片必须留下这一轮的新问题，不能被旧的盖回去。

    真实事故（2026-10-18 会话 `conv_460d3da67b`）：
    用户点「合并标签「T恤/Polo衫/背心」」→ 重跑仍 0 行 → `_upsert_artifact` 按维度
    **复用同一张卡**、写入新的诊断问题；随后 `_apply_clarification` 用函数开头捕获的
    **旧 payload** 覆盖回去，并把状态标成 `ready`。
    结果：卡片看起来"口径没变"，再点又命中幂等分支 → **点了彻底没反应**。
    """
    conversation_id = "conv_rerun_card"
    conversation = _conversation(conversation_id)
    card = _card(conversation_id, kind="clarification_card", status="needs_clarification")
    conversation.working_memory.produced_artifact_ids.append(card.artifact_id)
    CONVERSATIONS[conversation_id] = conversation
    MESSAGES[conversation_id] = []
    ARTIFACTS[card.artifact_id] = card
    old_payload = card.payload

    def _stub(self, **kwargs):  # noqa: ANN001, ANN202
        # 复刻 `_upsert_artifact` 的复用路径：同一个卡片对象、换一份新的 payload
        card.payload = {**old_payload, "goal": "新一轮的诊断问题", "status": "needs_clarification"}
        card.status = "needs_clarification"
        return {"assistant_text": "（桩：仍未取到数据）", "artifact_refs": [card.artifact_id], "actions": []}

    monkeypatch.setattr(ConversationService, "_execute_goals", _stub)
    try:
        first = conversation_service.apply_artifact_action(
            conversation_id=conversation_id,
            artifact_id=card.artifact_id,
            answer="按吊牌口径统计",
        )
        assert first.get("ok") is True
        assert card.payload.get("goal") == "新一轮的诊断问题", "新问题被旧 payload 覆盖了"
        assert conversation_service._checkpoint_settled(card) is False, "还没答完就标成已结算 = 后续点击全被挡"

        second = conversation_service.apply_artifact_action(
            conversation_id=conversation_id,
            artifact_id=card.artifact_id,
            answer="按吊牌口径统计",
        )
        assert second.get("idempotent") is not True, "新问题必须还能再点"
    finally:
        CONVERSATIONS.pop(conversation_id, None)
        MESSAGES.pop(conversation_id, None)
        ARTIFACTS.pop(card.artifact_id, None)
