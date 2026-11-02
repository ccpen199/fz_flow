"""画布状态投影（canvas 快照）的单测。

设计要点：这份快照必须**与产物类型无关**——新增一种产物时后端零改动，
agent 自动就能看到它。所以这里刻意用一个**还不存在的产物类型**来断言。

原则（项目硬约束）：打桩依赖可以，伪造业务数据不行。
这里构造的是"产物卡"这个**依赖对象**，断言的是投影规则本身，
不涉及任何取数结果（rows/spec）。
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from typing import get_args

import pytest

from app.services.conversation_service import conversation_service
from packages.shared_contracts.python_models import ArtifactCardDTO, ArtifactKind


def _card(*, kind: str, payload: dict) -> ArtifactCardDTO:
    now = datetime.now(UTC)
    return ArtifactCardDTO(
        artifact_id="art_digest1",
        conversation_id="conv_digest1",
        kind=kind,  # type: ignore[arg-type]
        title="某产物",
        subtitle="副标题",
        status="ready",
        payload=payload,
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize("kind", get_args(ArtifactKind))
def test_digest_treats_every_artifact_kind_the_same(kind: str) -> None:
    """每一种产物类型走的是同一条投影规则，没有任何一种被特殊对待。"""
    digest = conversation_service._artifact_digest(
        _card(kind=kind, payload={"alpha": "x", "beta": 3})
    )

    assert digest == {
        "kind": kind,
        "title": "某产物",
        "subtitle": "副标题",
        "status": "ready",
        "detail": {"alpha": "x", "beta": 3},
    }


def test_digest_source_never_names_a_kind() -> None:
    """回归守卫：投影实现里不许出现任何产物类型名。

    一旦有人写了 `if kind == "outline_card"` 这类分支，新增产物类型就又要改后端，
    就又变回"每加一个能力改一次代码"的老路。
    """
    source = inspect.getsource(conversation_service._artifact_digest)

    for kind in get_args(ArtifactKind):
        assert kind not in source, f"{kind} 被特殊对待了，投影就不再是通用规则"


def test_digest_collapses_lists_and_drops_noise() -> None:
    """列表压成「N 项」，超长字符串与空值不进快照。"""
    card = _card(
        kind="outline_card",
        payload={
            "pages": [1, 2, 3, 4],
            "options": [{"value": "a"}],
            "brand": "ZARA",
            "long_note": "很长的说明" * 30,
            "empty": "",
            "flag": True,
            "nothing": None,
        },
    )

    detail = conversation_service._artifact_digest(card)["detail"]

    assert detail["pages"] == "4 项"
    assert detail["options"] == "1 项"
    assert detail["brand"] == "ZARA"
    assert "long_note" not in detail
    assert "empty" not in detail
    assert "flag" not in detail
    assert "nothing" not in detail
