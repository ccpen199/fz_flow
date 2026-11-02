"""DeepFlow 多轮意图桥的单测。

原则（项目硬约束）：**打桩依赖可以，伪造业务数据不行**。
这里 stub 掉 HTTP（DeepFlow agent）这一**依赖**，断言的是桥接层自己的
解析/校验/降级行为；不构造任何画布业务数据（rows/spec）。
"""

from __future__ import annotations

import json

import pytest

from app.services.deepflow_agent_service import DeepflowAgentService


def _service() -> DeepflowAgentService:
    service = DeepflowAgentService()
    service.base_url = "http://agent.invalid"
    return service


def _stub_transport(monkeypatch: pytest.MonkeyPatch, service: DeepflowAgentService, replies: dict) -> list:
    """Stub the HTTP dependency. Returns the list of recorded calls."""
    calls: list = []

    def fake_post(path: str, body: dict, *, count_as_failure: bool = True):
        calls.append((path, body))
        if path == "/threads":
            return {}, None
        return replies.get("run_body"), None

    monkeypatch.setattr(service, "_post_json", fake_post)
    return calls


def _ai_body(content: str) -> dict:
    return {"messages": [{"type": "human", "content": "x"}, {"type": "ai", "content": content}]}


def test_parses_strict_json_judgement(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    judgement = {
        "intent": "data_query",
        "dimension_keys": ["price"],
        "time_window": "近3个月",
        "page_count": None,
        "reply": "好的，按价格带取数。",
        "needs_clarification": False,
        "clarify_question": "",
    }
    _stub_transport(monkeypatch, service, {"run_body": _ai_body(json.dumps(judgement, ensure_ascii=False))})

    result = service.plan_turn(conversation_id="conv_test1", text="看看价格带")

    assert result["ok"] is True
    assert result["intent"] == "data_query"
    assert result["dimension_keys"] == ["price"]
    assert result["time_window"] == "近3个月"
    assert result["needs_clarification"] is False


def test_parses_json_inside_markdown_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    content = '```json\n{"intent": "chat", "reply": "你好", "needs_clarification": true}\n```'
    _stub_transport(monkeypatch, service, {"run_body": _ai_body(content)})

    result = service.plan_turn(conversation_id="conv_test2", text="你好")

    assert result["ok"] is True
    assert result["intent"] == "chat"
    assert result["needs_clarification"] is True


def test_unknown_intent_string_is_normalised(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    content = json.dumps({"intent": "做个好看的表", "reply": "?"})
    _stub_transport(monkeypatch, service, {"run_body": _ai_body(content)})

    result = service.plan_turn(conversation_id="conv_test3", text="随便来点")

    assert result["ok"] is True
    assert result["intent"] == "unknown"


def test_unparsable_reply_asks_user_instead_of_guessing(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    _stub_transport(monkeypatch, service, {"run_body": _ai_body("抱歉，我不太确定。")})

    result = service.plan_turn(conversation_id="conv_test4", text="嗯")

    assert result["ok"] is True
    assert result["intent"] == "unknown"
    assert result["needs_clarification"] is True
    assert result["parse_failed"] is True
    assert result["dimension_keys"] == []


def test_transport_failure_reports_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()

    def failing_post(path: str, body: dict, *, count_as_failure: bool = True):
        if path == "/threads":
            return {}, None
        return None, "http_503"

    monkeypatch.setattr(service, "_post_json", failing_post)

    result = service.plan_turn(conversation_id="conv_test5", text="看价格带")

    assert result["ok"] is False
    assert result["error"] == "http_503"


def test_thread_id_is_a_stable_uuid() -> None:
    service = _service()
    first = service.thread_id_for("conv_abc-123")

    # LangGraph 只接受 UUID 形式；同一会话必须稳定映射到同一 thread。
    assert len(first) == 36
    assert first == service.thread_id_for("conv_abc-123")
    assert first != service.thread_id_for("conv_abc-124")
    assert service.thread_id_for("") == ""
    assert service.thread_id_for("conv_abc-123").count("-") == 4


def test_page_count_is_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    content = json.dumps({"intent": "report_build", "page_count": 99, "reply": "好"})
    _stub_transport(monkeypatch, service, {"run_body": _ai_body(content)})

    result = service.plan_turn(conversation_id="conv_test6", text="串个汇报")

    assert result["page_count"] == 12


def test_payload_carries_scene_and_canvas_state(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    calls = _stub_transport(monkeypatch, service, {"run_body": _ai_body(json.dumps({"intent": "chat"}))})

    canvas_state = {
        "artifacts": [
            {"kind": "chart_card", "title": "价格带与折扣节奏", "status": "ready",
             "detail": {"dimension_key": "price"}},
        ],
        "rollback_available": True,
    }
    last_turn = {"intent": "data_query", "system_result": "已更新画布：chart_card《价格带与折扣节奏》"}

    service.plan_turn(
        conversation_id="conv_test7",
        text="你好",
        scene_id="scene_prd_competitor",
        scene_name="竞品企划分析",
        brand="ZARA",
        canvas_state=canvas_state,
        last_turn=last_turn,
        dimension_options=[{"dimension_key": "price", "title": "价格带与折扣节奏"}],
    )

    run_body = [body for path, body in calls if path.endswith("/runs/wait")][0]
    payload = json.loads(run_body["input"]["messages"][0]["content"])
    assert payload["brand_memory"] == "ZARA"
    # 画布现状原样送达——agent 靠它判断"能不能改、改什么"。
    assert payload["canvas"] == canvas_state
    assert payload["last_turn"] == last_turn
    assert payload["dimension_options"][0]["dimension_key"] == "price"
    assert run_body["config"]["configurable"]["agent_name"] == service.agent_name


def test_payload_has_no_per_capability_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """回归守卫：不许把 has_charts / can_rollback / has_outline 这类开关加回来。

    那套设计每新增一种产物就要多一个字段，漏一个 agent 就只能猜；
    画布现状统一走 canvas.artifacts 自描述快照。
    """
    service = _service()
    calls = _stub_transport(monkeypatch, service, {"run_body": _ai_body(json.dumps({"intent": "chat"}))})

    service.plan_turn(conversation_id="conv_test9", text="你好")

    run_body = [body for path, body in calls if path.endswith("/runs/wait")][0]
    payload = json.loads(run_body["input"]["messages"][0]["content"])
    for banned in ("has_charts", "has_outline", "can_rollback", "chart_dimension_keys"):
        assert banned not in payload, f"{banned} 是能力开关，应统一由 canvas 快照表达"


def test_canvas_state_defaults_to_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """没传画布状态时必须按「画布是空的」上报，不能默认成有东西。"""
    service = _service()
    calls = _stub_transport(monkeypatch, service, {"run_body": _ai_body(json.dumps({"intent": "chat"}))})

    service.plan_turn(conversation_id="conv_test8", text="你能做什么")

    run_body = [body for path, body in calls if path.endswith("/runs/wait")][0]
    payload = json.loads(run_body["input"]["messages"][0]["content"])
    assert payload["canvas"] == {"artifacts": [], "rollback_available": False}
    assert payload["last_turn"] == {"intent": "", "system_result": ""}


def test_parses_coverage_and_control_slots(monkeypatch: pytest.MonkeyPatch) -> None:
    """「问数据覆盖」「换方向」必须有专门的任务类型，不能塞进 data_query。

    2026-10-19 报障：agent 明明理解成「看一下数据覆盖在哪些月份」，结构化字段却是
    `data_query + trend`，后端只能去取一张趋势图——用户看到的就是答非所问。
    """
    service = _service()
    judgement = {
        "intent": "coverage",
        "config_keys": [],
        "dimension_keys": [],
        "coverage": {"kind": "months", "brands": ["优衣库"]},
        "control": {},
        "reply": "好，我统计一下优衣库各个月份的覆盖情况。",
        "needs_clarification": False,
    }
    _stub_transport(monkeypatch, service, {"run_body": _ai_body(json.dumps(judgement, ensure_ascii=False))})

    result = service.plan_turn(conversation_id="conv_cov1", text="优衣库哪个月份有数据")

    assert result["intent"] == "coverage"
    assert result["coverage"] == {"kind": "months", "brands": ["优衣库"]}
    assert result["control"] == {}
    # 覆盖度不需要方向：任何维度 key 都会让后端去取图，这里必须是空的。
    assert result["dimension_keys"] == []


def test_parses_control_slot(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    judgement = {
        "intent": "control",
        "control": {"action": "switch_direction"},
        "coverage": {},
        "reply": "行，这个先放下。",
    }
    _stub_transport(monkeypatch, service, {"run_body": _ai_body(json.dumps(judgement, ensure_ascii=False))})

    result = service.plan_turn(conversation_id="conv_ctrl1", text="换个方向")

    assert result["intent"] == "control"
    assert result["control"]["action"] == "switch_direction"


def test_non_object_slots_are_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """槽位写成字符串/列表之类的形状错误 → 当没填，而不是让下游 `.get()` 崩掉。"""
    service = _service()
    judgement = {"intent": "control", "control": "switch_direction", "coverage": ["months"]}
    _stub_transport(monkeypatch, service, {"run_body": _ai_body(json.dumps(judgement, ensure_ascii=False))})

    result = service.plan_turn(conversation_id="conv_ctrl2", text="换个方向")

    assert result["control"] == {}
    assert result["coverage"] == {}


def test_parses_group_by_as_a_plain_string_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """「按颜色拆一下」的分组列必须原样带出来，否则后端只能照旧跑上一个方向。

    2026-10-19 实测：agent 嘴上说"再按颜色拆开"，结构化字段里没有分组列，
    后端就把「品类结构分布」原样重跑了一遍（`x=一级类目`、rows=1）。
    """
    service = _service()
    judgement = {
        "intent": "data_query",
        "config_keys": ["scene_prd_structure"],
        "dimension_keys": ["structure"],
        "group_by": ["颜色"],
        "filters": [{"field": "二级类目", "operator": "=", "value": "T恤"}],
        "reply": "好，按颜色拆开看。",
    }
    _stub_transport(monkeypatch, service, {"run_body": _ai_body(json.dumps(judgement, ensure_ascii=False))})

    result = service.plan_turn(conversation_id="conv_group1", text="T恤里再按颜色拆一下")

    assert result["group_by"] == ["颜色"]


def test_group_by_shape_errors_become_an_empty_list(monkeypatch: pytest.MonkeyPatch) -> None:
    service = _service()
    judgement = {
        "intent": "data_query",
        "dimension_keys": ["structure"],
        "group_by": "颜色",
        "reply": "好。",
    }
    _stub_transport(monkeypatch, service, {"run_body": _ai_body(json.dumps(judgement, ensure_ascii=False))})

    result = service.plan_turn(conversation_id="conv_group2", text="按颜色拆一下")

    assert result["group_by"] == []


def test_report_topic_is_passed_through_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    """主题是**用户原话里的那份**，照抄回传；后端不许自己加工。

    这层是"先定主题"的入口：`report_topic` 有没有值，决定后端是"先问主题"还是
    "直接按主题搭大纲"。
    """
    service = _service()
    judgement = {
        "intent": "report_build",
        "page_count": 4,
        "report_topic": "  优衣库 2026 春夏面料与价格复盘  ",
        "reply": "好，按这个主题搭大纲。",
    }
    _stub_transport(monkeypatch, service, {"run_body": _ai_body(json.dumps(judgement, ensure_ascii=False))})

    result = service.plan_turn(conversation_id="conv_topic1", text="做个《优衣库 2026 春夏面料与价格复盘》的 PPT")

    assert result["intent"] == "report_build"
    assert result["report_topic"] == "优衣库 2026 春夏面料与价格复盘"


def test_missing_report_topic_is_an_empty_string_not_a_guess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只说"串个 4 页 PPT"（没给主题）→ 空串。

    空串的语义是"用户还没给主题"，后端据此**先问他**；这里不能替它补一个
    「品牌 + 场景复盘」当默认主题。
    """
    service = _service()
    judgement = {
        "intent": "report_build",
        "page_count": 4,
        "reply": "好，先定个主题。",
        "needs_clarification": True,
        "clarify_question": "这份 PPT 想讲什么？",
    }
    _stub_transport(monkeypatch, service, {"run_body": _ai_body(json.dumps(judgement, ensure_ascii=False))})

    result = service.plan_turn(conversation_id="conv_topic2", text="帮我串个 4 页的 PPT")

    assert result["report_topic"] == ""

