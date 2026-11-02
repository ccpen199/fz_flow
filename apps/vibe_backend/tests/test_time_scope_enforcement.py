"""「最近一个月」= **库里的**最近一个月，而且必须真的落进 SQL。

报障（2026-10-19，用户连续两轮追问）：
1. 问「优衣库品牌最近一个月上了多少件T恤」，图上是 2025-07-01 一个点；
2. 「哪怕模型按自己最新日期回推 没得到数据 为何还有这些数据」——截图里的数
   是全期/品牌自身时间窗的数，不是最近一个月。

根因有三层，这里各锁一条：
- **转发断了**：`_run_goals` 收了 `time_window`/`time_range` 却没传给 `_run_goal`，
  于是后端算好的绝对区间连 intent 都没进；
- **口径没有结构化**：只写在 intent 散文里，模型会和 system prompt 里
  「以表内最大可用日期为锚点」对齐，自己在筛选后的子集上算 MAX(日期)；
- **没有验收**：SQL 跑出来就算成功，没人检查它到底用了哪段时间。

⚠️ 本文件不造业务数据：`_FakeQueryRun` 的空结果只是为了让 `_run_goal` 提前返回，
断言的是**传给取数侧的时间窗事实**；真实取数在 MySQL 上另行验证。
"""

from __future__ import annotations

from typing import Any

from packages.shared_contracts.python_models import SceneDTO, SceneFieldDTO
from app.services.conversation_service import ConversationService
from app.services.sql_result_agent_service import SqlResultAgentService


class _FakeQueryRun:
    """空结果 + 失败状态：让 `_run_goal` 在取数后立即返回，不进入建卡逻辑。"""

    status = "failed"
    result_preview: list[dict[str, Any]] = []
    sql_explanation = "fake"
    insight_summary: list[str] = []
    sql = ""


def _scene() -> SceneDTO:
    return SceneDTO(
        scene_id="scene_test",
        name="测试场景",
        description="结构占位",
        fields=[
            SceneFieldDTO(
                field_id="f_time",
                table_name="clothing_info",
                field_name="CreateTime",
                semantic_name="创建时间",
                description="结构占位",
                role="time",
            )
        ],
    )


def _scope() -> dict[str, Any]:
    return {
        "start": "2026-05-26",
        "end": "2026-06-25",
        "field": "创建时间",
        "column": "clothing_info.CreateTime",
        "label": "近1个月",
        "anchor": "2026-06-25",
        "anchor_scope": "database",
    }


def test_time_scope_payload_carries_absolute_bounds_and_column() -> None:
    payload = ConversationService._time_scope_payload(
        scene=_scene(),
        time_window="近1个月",
        window_range=("2026-05-26", "2026-06-25"),
        anchor=("创建时间", "2026-06-25"),
    )
    assert payload is not None
    assert payload["start"] == "2026-05-26"
    assert payload["end"] == "2026-06-25"
    assert payload["column"] == "clothing_info.CreateTime"
    assert payload["anchor_scope"] == "database"


def test_time_scope_payload_is_absent_without_a_window() -> None:
    assert (
        ConversationService._time_scope_payload(
            scene=_scene(), time_window="近1个月", window_range=None, anchor=None
        )
        is None
    )


def test_run_goals_actually_forwards_the_time_window() -> None:
    """回归：`executor.submit` 漏传参数时，绝对区间永远到不了 `_run_goal`。"""
    service = ConversationService()
    captured: list[dict[str, Any]] = []

    def fake_run_goal(**kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {"dimension_key": kwargs["goal"]["dimension_key"], "status": "failed", "spec": {}}

    service._run_goal = fake_run_goal  # type: ignore[method-assign]
    service._run_goals(
        scene=_scene(),
        conversation_id="conv_test",
        goals=[{"dimension_key": "k1", "goal": "占位目标"}],
        brand_resolution={"confirmed": []},
        time_window="近1个月",
        time_range=("2026-05-26", "2026-06-25"),
        time_scope=_scope(),
    )
    assert len(captured) == 1
    assert captured[0]["time_window"] == "近1个月"
    assert captured[0]["time_range"] == ("2026-05-26", "2026-06-25")
    assert captured[0]["time_scope"]["start"] == "2026-05-26"


def test_run_goal_hands_the_absolute_range_to_the_query_side_as_a_fact() -> None:
    service = ConversationService()
    seen: dict[str, Any] = {}

    def fake_run(**kwargs: Any) -> dict[str, Any]:
        seen.update(kwargs)
        return {"query_run": _FakeQueryRun(), "notes": []}

    service.sql_agent.run = fake_run  # type: ignore[method-assign]
    service._run_goal(
        scene=_scene(),
        conversation_id="conv_test",
        goal={"dimension_key": "k1", "goal": "近1个月上架件数（近1个月）"},
        brand_resolution={"confirmed": []},
        time_window="近1个月",
        time_range=("2026-05-26", "2026-06-25"),
        time_scope=_scope(),
    )
    assert seen["context"]["time_scope"]["start"] == "2026-05-26"
    # intent 里也要有人话版的绝对区间（模型读 intent，结构化字段在 context 里）
    assert "2026-05-26" in seen["intent"] and "2026-06-25" in seen["intent"]
    assert "库内最新数据日期" in seen["intent"]


def test_sql_agent_lifts_time_scope_to_the_payload_top_level() -> None:
    """模型对顶层字段的关注度高于 `context.*`，绝对区间必须抬上去。"""
    service = SqlResultAgentService()
    payloads: list[dict[str, Any]] = []

    def fake_generate_plan(payload: dict[str, Any]) -> dict[str, Any]:
        payloads.append(payload)
        return {"sql": "", "sql_explanation": "", "plan": {}, "notes": []}

    service.client.generate_plan = fake_generate_plan  # type: ignore[method-assign]
    service.run(
        scene=_scene(),
        session_id="s1",
        scene_version="1",
        intent="占位意图",
        agent_prompt="",
        context={"time_scope": _scope()},
        execute=False,
    )
    assert payloads[0]["time_scope"]["end"] == "2026-06-25"


def test_absolute_range_sql_passes_the_time_gate() -> None:
    sql = (
        "SELECT DATE(ci.CreateTime) AS d, COUNT(DISTINCT ci.Id) AS n "
        "FROM clothing_info ci "
        "WHERE DATE(ci.CreateTime) >= '2026-05-26' AND DATE(ci.CreateTime) <= '2026-06-25' "
        "GROUP BY DATE(ci.CreateTime)"
    )
    assert SqlResultAgentService._time_scope_violation(sql, _scope()) == ""


def test_half_open_upper_bound_is_equivalent() -> None:
    sql = "SELECT 1 FROM clothing_info ci WHERE ci.CreateTime >= '2026-05-26' AND ci.CreateTime < '2026-06-26'"
    assert SqlResultAgentService._time_scope_violation(sql, _scope()) == ""


def test_brand_self_anchored_sql_is_rejected() -> None:
    """实测失败 SQL 的形态：品牌自身的最大日期 + DATE_SUB 回推 30 天。"""
    sql = (
        "WITH anchor AS (SELECT MAX(DATE(ci.CreateTime)) AS max_create_date FROM clothing_info ci) "
        "SELECT DATE(ci.CreateTime) AS d FROM clothing_info ci, anchor a "
        "WHERE DATE(ci.CreateTime) >= DATE_SUB(a.max_create_date, INTERVAL 30 DAY)"
    )
    violation = SqlResultAgentService._time_scope_violation(sql, _scope())
    assert violation
    assert "时间窗" in violation


def test_missing_end_boundary_is_rejected() -> None:
    sql = "SELECT 1 FROM clothing_info ci WHERE ci.CreateTime >= '2026-05-26'"
    assert SqlResultAgentService._time_scope_violation(sql, _scope())


def test_validation_blocks_a_violating_sql() -> None:
    service = SqlResultAgentService()
    error = service._validate_llm_sql_output(
        intent="近1个月上架件数",
        sql="SELECT 1 FROM clothing_info ci WHERE ci.CreateTime >= '2026-05-26'",
        plan=None,
        time_scope=_scope(),
    )
    assert error
    assert "时间窗" in error


def test_no_time_scope_keeps_the_old_behaviour() -> None:
    """没有时间窗时这条闸门必须完全静默，老链路零回归。"""
    assert SqlResultAgentService._time_scope_violation("SELECT 1", None) == ""
    service = SqlResultAgentService()
    assert (
        service._validate_llm_sql_output(
            intent="随便看看", sql="SELECT 1", plan=None, time_scope=None
        )
        == ""
    )
