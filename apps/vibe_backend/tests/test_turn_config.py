"""「配置按需加载」（轮次级配置）的单测。

原则（项目硬约束）：**打桩依赖可以，伪造业务数据不行**。
这里不构造任何配置/字段/数值——配置目录、字段并集、合并结果**全部来自真实配置中心**
（MySQL 的 vibe_scene / vibe_scene_field / vibe_scene_relation，由 scene_cache_service 读取）。
断言的是"给定 agent 的判定，后端会加载/不加载什么"。

MySQL 不可达时本模块整体 skip：宁可不判，也不要拿编造出来的配置表当真实结果断言。
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.services import conversation_service
from app.services.conversation_service import plan_turn_config, turn_runs_query
from app.services.dimension_recommender import COUNT_METRIC, dimension_recommender
from app.services.turn_config_service import TurnConfigService, turn_config_service
from packages.shared_contracts.python_models import ArtifactCardDTO

MODULE_PATH = Path(inspect.getsourcefile(conversation_service) or "")


# --------------------------------------------------------------------- fixtures


@pytest.fixture(scope="module")
def catalog() -> list[dict]:
    items = turn_config_service.catalog()
    if not items:
        pytest.skip("配置中心不可达或没有可用配置（需要真实 MySQL）")
    return items


@pytest.fixture(scope="module")
def scene_ids(catalog: list[dict]) -> list[str]:
    return [str(item["scene_id"]) for item in catalog]


# ------------------------------------------------------------- 目录来自配置中心


def test_catalog_ids_all_exist_in_config_center(catalog: list[dict]) -> None:
    """目录里的每个 scene_id 都必须能在配置中心查到——证明它不是我编的。"""
    from app.services.scene_cache_service import scene_cache_service

    real = {str(scene.scene_id) for scene in scene_cache_service.list_scenes()}
    assert real, "配置中心里一套配置都没有"
    assert {str(item["scene_id"]) for item in catalog} <= real


def test_catalog_entries_carry_what_agent_needs(catalog: list[dict]) -> None:
    """agent 靠名称/描述/样例目标做语义比对，靠字段清单知道这套配置能查什么。"""
    for item in catalog:
        assert str(item["scene_id"]).strip()
        assert str(item["name"]).strip()
        assert item["field_count"] >= 1
        assert item["fields"], f"{item['scene_id']} 没有字段清单"
        for field in item["fields"]:
            assert str(field["semantic_name"]).strip()


def test_catalog_has_no_empty_field_configs(catalog: list[dict]) -> None:
    """没有可用字段的配置不进目录——给了只会误导 agent。"""
    assert all(item["field_count"] == len(item["fields"]) for item in catalog)


# ------------------------------------------------------- 字段词表：并集 + 归属


def test_field_lexicon_marks_owning_configs(catalog: list[dict]) -> None:
    lexicon = turn_config_service.field_lexicon()
    known = {str(item["scene_id"]) for item in catalog}
    assert lexicon
    for entry in lexicon:
        assert entry["configs"], f"{entry['semantic_name']} 没有归属配置"
        assert set(entry["configs"]) <= known


def test_field_lexicon_is_a_union_not_a_single_config(catalog: list[dict]) -> None:
    """并集必须覆盖每一套配置的字段（否则 agent 在判定前根本看不到某些字段）。"""
    lexicon = turn_config_service.field_lexicon()
    names = {(entry["semantic_name"], entry["role"]) for entry in lexicon}
    for item in catalog:
        for field in item["fields"]:
            assert (field["semantic_name"], field["role"]) in names


# --------------------------------------------------------------- 白名单校验


def test_validate_keys_accepts_only_real_ids(scene_ids: list[str]) -> None:
    valid, rejected = turn_config_service.validate_keys([scene_ids[0], "scene_不存在"])
    assert valid == [scene_ids[0]]
    assert rejected == ["scene_不存在"]


def test_validate_keys_does_not_fuzzy_match(catalog: list[dict]) -> None:
    """不做大小写/前缀/近义匹配——那正是"猜"。"""
    sample = str(catalog[0]["scene_id"])
    valid, rejected = turn_config_service.validate_keys([sample.upper(), sample[:-1], f" {sample} "])
    # 前后空格是格式问题，允许 strip；大小写与缺字符一律不算命中。
    assert valid == [sample]
    assert sample.upper() in rejected and sample[:-1] in rejected


def test_validate_keys_tolerates_junk_input(catalog: list[dict]) -> None:
    assert turn_config_service.validate_keys(None) == ([], [])
    assert turn_config_service.validate_keys("scene_prd_price") == ([], [])
    assert turn_config_service.validate_keys(["", None, "  "]) == ([], [])
    valid, rejected = turn_config_service.validate_keys([catalog[0]["scene_id"], catalog[0]["scene_id"]])
    assert valid == [str(catalog[0]["scene_id"])]
    assert rejected == []


# ------------------------------------------------------------------- 加载与合并


def test_load_zero_keys_means_no_config_not_failure() -> None:
    scene, info = turn_config_service.load([])
    assert scene is None
    assert info["config_ids"] == []
    assert info["names"] == []
    assert info["merged"] is False
    assert info["source"] == "turn"


def test_load_single_key_returns_the_real_scene(catalog: list[dict]) -> None:
    from app.services.scene_cache_service import scene_cache_service

    sample = str(catalog[0]["scene_id"])
    scene, info = turn_config_service.load([sample])
    real = scene_cache_service.get_scene(sample)
    assert real is not None
    assert scene is not None
    assert scene.scene_id == real.scene_id
    assert info["config_ids"] == [sample]
    assert info["merged"] is False


def test_merge_keeps_real_scene_id_and_unions_fields(scene_ids: list[str]) -> None:
    """合并时 scene_id 必须是**首个真实配置的 id**。

    自造一个 id（如 "merged_xxx"）会让 ``get_scene_playbook(scene_id)`` 查不到配置，
    连带丢掉价格带策略与问题矩阵——那是实打实的能力损失。
    """
    from app.services.scene_cache_service import scene_cache_service

    first, second = scene_ids[0], scene_ids[1]
    a = scene_cache_service.get_scene(first)
    b = scene_cache_service.get_scene(second)
    scene, info = turn_config_service.load([first, second])

    assert scene is not None
    assert scene.scene_id == first
    assert info["config_ids"] == [first, second]
    assert info["merged"] is True
    # 字段取并集：不少于任何一套，且不超过两套之和。
    assert len(scene.fields) >= max(len(a.fields), len(b.fields))
    assert len(scene.fields) <= len(a.fields) + len(b.fields)
    # 关系同理。
    assert len(scene.relations) >= max(len(a.relations), len(b.relations))


def test_all_configs_scene_is_union_of_everything(catalog: list[dict]) -> None:
    from app.services.scene_cache_service import scene_cache_service

    overview = turn_config_service.all_configs_scene()
    assert overview is not None
    scenes = [scene_cache_service.get_scene(str(item["scene_id"])) for item in catalog]
    for scene in scenes:
        assert scene is not None
        for field in scene.fields:
            assert any(
                str(getattr(f, "table_name", "")) == str(getattr(field, "table_name", ""))
                and str(getattr(f, "field_name", "")) == str(getattr(field, "field_name", ""))
                for f in overview.fields
            )


def test_all_configs_scene_is_cached_by_signature() -> None:
    assert turn_config_service.all_configs_scene() is turn_config_service.all_configs_scene()


# ----------------------------------------------------------- 本轮该加载哪几套


def test_chat_turn_loads_nothing_even_if_agent_returns_keys(catalog: list[dict]) -> None:
    """闲聊/改画布这类轮次：即使 agent 顺手回了 key，也**一个都不导入**。"""
    scene, info, rejected = plan_turn_config(
        intent="chat", filters=[], config_keys=[str(catalog[0]["scene_id"])]
    )
    assert scene is None
    assert info["config_ids"] == []
    assert info["names"] == []
    assert "不涉及配置" in info["note"]
    assert rejected == [], "合法的 key 被忽略，不该被算作丢弃"


def test_revision_turn_loads_nothing(catalog: list[dict]) -> None:
    scene, info, _ = plan_turn_config(
        intent="canvas_revision", filters=[], config_keys=[str(catalog[0]["scene_id"])]
    )
    assert scene is None
    assert info["config_ids"] == []


def test_data_query_without_keys_asks_instead_of_picking_one(catalog: list[dict]) -> None:
    """要数据但没落到具体某一套 → 不加载、如实说明，由调用方追问。"""
    scene, info, _ = plan_turn_config(intent="data_query", filters=[], config_keys=[])
    assert scene is None
    assert info["config_ids"] == []
    assert "没确定是哪一套" in info["note"]


def test_data_query_with_real_key_loads_it(catalog: list[dict]) -> None:
    sample = str(catalog[0]["scene_id"])
    scene, info, rejected = plan_turn_config(intent="data_query", filters=[], config_keys=[sample])
    assert scene is not None
    assert scene.scene_id == sample
    assert info["config_ids"] == [sample]
    assert rejected == []


def test_unknown_key_is_reported_verbatim(catalog: list[dict]) -> None:
    scene, info, rejected = plan_turn_config(
        intent="data_query", filters=[], config_keys=["scene_prd_price", "scene_我编的"]
    )
    assert rejected == ["scene_我编的"]
    # 只剩一个合法 key 时照常加载那一套。
    assert scene is not None
    assert info["config_ids"] == ["scene_prd_price"]


def test_filters_without_keys_still_needs_config(catalog: list[dict]) -> None:
    """带筛选条件一定会查库，所以它和无方向取数一样要求配置落到具体某一套。"""
    scene, info, _ = plan_turn_config(
        intent="data_query", filters=[{"field": "价格", "operator": ">=", "value": 400}], config_keys=[]
    )
    assert scene is None
    assert "没确定是哪一套" in info["note"]


def test_runs_query_only_when_it_will_really_query() -> None:
    assert turn_runs_query(intent="data_query", filters=[], picked_keys=["price"]) is True
    assert turn_runs_query(intent="data_query", filters=[{"field": "价格"}], picked_keys=[]) is True
    assert turn_runs_query(intent="data_query", filters=[], picked_keys=[]) is False
    assert turn_runs_query(intent="chat", filters=[], picked_keys=["price"]) is False
    assert turn_runs_query(intent="canvas_revision", filters=[], picked_keys=["price"]) is False


# ------------------------------------------------------------------- 展示文案


def test_describe_reads_naturally(catalog: list[dict]) -> None:
    sample = str(catalog[0]["scene_id"])
    _, info = turn_config_service.load([sample])
    assert turn_config_service.describe(info) == str(catalog[0]["name"])
    assert turn_config_service.describe({}) == "未加载配置"
    _, merged = turn_config_service.load([sample, str(catalog[1]["scene_id"])])
    assert " + " in turn_config_service.describe(merged)


# ----------------------------------------------------- 源码级守卫：不硬编码业务


def test_service_has_no_hardcoded_scene_ids(catalog: list[dict]) -> None:
    """配置目录必须完全来自配置中心：服务代码里不允许出现任何真实配置 id 字面量，
    也不允许内嵌业务关键词表（意图关键词之类的规则参数不在此列）。"""
    source = Path(inspect.getsourcefile(TurnConfigService) or "").read_text(encoding="utf-8")
    for item in catalog:
        assert str(item["scene_id"]) not in source, f"{item['scene_id']} 被硬编码进了服务"


def test_conversation_service_has_a_single_config_choke_point() -> None:
    """对话层不许在多处各自挑配置：**判定**只能经由 plan_turn_config 这一个入口。

    出现第二处 `turn_config_service.load(` 就意味着有地方绕过了那三条规则
    （尤其是"不涉及配置的轮次一个都不导入"）。

    `reload(` 是**另一回事**，允许存在且同样只准一处：它不重新判定，只把
    `working_memory.turn_config.config_ids` 里那份**已经判定过**的配置取回来，
    好让画布动作（执行卡片 SQL / 重跑某页 / 换一批维度）与本轮取数保持同一口径
    ——否则这三处会退回读建会话时的 `conversation.scene_id`，而那个字段永不更新。
    """
    source = MODULE_PATH.read_text(encoding="utf-8")
    assert source.count("turn_config_service.load(") == 1
    assert source.count("turn_config_service.validate_keys(") == 1
    assert source.count("turn_config_service.reload(") == 1
    assert "plan_turn_config(" in source


# --------------------------------------------- handle_message 接线（打桩 LLM 依赖）
#
# 这里把多轮 agent（LLM）这一**依赖**打桩掉，跑的是真实的 handle_message：
# 真实配置中心、真实维度推荐、真实画布编排。断言的是"给定 agent 的判定，
# 这一轮加载了什么、出了什么卡"。凡是会真查库的分支（data_query + 有方向）不在此列。


@pytest.fixture
def conversation_id(monkeypatch: pytest.MonkeyPatch, catalog: list[dict]) -> str:
    # 打桩持久化：单测不该写用户的项目状态文件。配置目录走真实配置中心。
    monkeypatch.setattr(conversation_service, "persist_state", lambda: None)
    conversation = conversation_service.conversation_service.create(title="按需配置验证")
    return conversation.conversation_id


def _stub_agent(monkeypatch: pytest.MonkeyPatch, plan: dict, *, reachable: bool = True) -> list[dict]:
    """Stub the LLM dependency (the multi-turn agent). Records the payloads sent.

    ``reachable`` 打桩的是**失败现场探测 agent ``/ok`` 的结果**。它是归因文案的判据，
    所以必须一起打桩：否则测试会真的去连 127.0.0.1:2124，断言结果就会随环境漂移。
    """
    from app.services.deepflow_agent_service import deepflow_agent_service

    calls: list[dict] = []

    def fake_plan_turn(**kwargs):
        calls.append(kwargs)
        return dict(plan)

    monkeypatch.setattr(deepflow_agent_service, "plan_turn", fake_plan_turn)
    monkeypatch.setattr(deepflow_agent_service, "healthy", lambda: reachable)
    return calls


def _turn(conversation_id: str, text: str) -> tuple[dict, object]:
    result = conversation_service.conversation_service.handle_message(
        conversation_id=conversation_id, text=text
    )
    conversation = conversation_service.conversation_service.get(conversation_id)
    return result, conversation


def test_chat_turn_stores_zero_config(monkeypatch: pytest.MonkeyPatch, conversation_id: str, catalog: list[dict]) -> None:
    sample = str(catalog[0]["scene_id"])
    _stub_agent(
        monkeypatch,
        {"ok": True, "intent": "chat", "config_keys": [sample], "reply": "你好呀", "dimension_keys": []},
    )

    result, conversation = _turn(conversation_id, "你好")

    info = conversation.working_memory.turn_config
    assert info["config_ids"] == []
    assert info["note"] == "本轮不涉及配置"
    assert result["canvas"]["counts"].get("chart_card", 0) == 0


def test_agent_answers_but_gives_no_judgement_is_not_blamed_on_the_service(
    monkeypatch: pytest.MonkeyPatch, conversation_id: str
) -> None:
    """agent 服务**通**、但没回判定（`empty_reply`）→ 如实告知、零产物。

    这是 2026-10-07 线上真实踩到的形态：2124 的 LangGraph 服务健康（POST 拿到了 200），
    只是它内部调大模型失败、产出里没有 AI 文本。当时的文案写的是
    「请确认 deer-flow 的 LangGraph 服务已启动」——**归错了层**，用户去重启一个
    好好的服务，重启一百次也没用。所以这里显式断言：不许再把它说成"服务没起"。
    """
    _stub_agent(monkeypatch, {"ok": False, "error": "empty_reply"}, reachable=True)

    result, conversation = _turn(conversation_id, "看看 ZARA 的价格带分布")

    content = result["message"]["content"] or ""
    assert result["ok"] is False
    assert result["canvas"]["counts"].get("chart_card", 0) == 0
    # 说清"探测过、服务是通的"，并指向 agent 自己的日志。
    assert "是通的" in content
    assert "deerflow_agent.log" in content
    # 归错层的话一个字都不许出现。
    assert "连不上" not in content
    assert "已启动" not in content
    info = conversation.working_memory.turn_config
    assert info["config_ids"] == []
    assert info["note"] == "agent 无判定结果，本轮未判定"


def test_agent_service_unreachable_says_so_and_points_at_the_service(
    monkeypatch: pytest.MonkeyPatch, conversation_id: str
) -> None:
    """agent 服务**连不上**（现场探测 /ok 不通）→ 这时才该让用户去启动 2124 那个服务。

    与上一条互补：同样是 `ok=False`，但失败所在的那一层不同，文案必须不同。
    """
    _stub_agent(
        monkeypatch,
        {"ok": False, "error": "URLError:<urlopen error [Errno 61] Connection refused>"},
        reachable=False,
    )

    result, conversation = _turn(conversation_id, "看看 ZARA 的价格带分布")

    content = result["message"]["content"] or ""
    assert result["ok"] is False
    assert result["canvas"]["counts"].get("chart_card", 0) == 0
    assert "连不上" in content
    assert "2124" in content
    # 不许反过来说成"服务是通的"。
    assert "是通的" not in content
    info = conversation.working_memory.turn_config
    assert info["config_ids"] == []
    assert info["note"] == "agent 服务连不上，本轮未判定"


def test_timeout_is_not_blamed_on_the_service_being_down(
    monkeypatch: pytest.MonkeyPatch, conversation_id: str
) -> None:
    """**超时**不算"服务没起"——这是最容易被错误串骗到的一种。

    后端等 LangGraph 的 `runs/wait` 等超时（`TimeoutError`），错误串看起来跟"连不上"
    很像；但现场一探 /ok 是通的，说明服务在跑，只是这一轮没出结果。
    所以判据必须是探测结果，不能是错误串里的字样。
    """
    _stub_agent(monkeypatch, {"ok": False, "error": "TimeoutError:timed out"}, reachable=True)

    result, conversation = _turn(conversation_id, "看看 ZARA 的价格带分布")

    content = result["message"]["content"] or ""
    assert result["ok"] is False
    assert "是通的" in content
    assert "连不上" not in content
    assert "已启动" not in content
    info = conversation.working_memory.turn_config
    assert info["note"] == "agent 未返回结果，本轮未判定"


def test_explore_turn_loads_the_config_the_agent_picked(
    monkeypatch: pytest.MonkeyPatch, conversation_id: str, catalog: list[dict]
) -> None:
    sample = str(catalog[0]["scene_id"])
    _stub_agent(
        monkeypatch,
        {"ok": True, "intent": "explore", "config_keys": [sample], "reply": "这几个方向可以看", "dimension_keys": []},
    )

    result, conversation = _turn(conversation_id, "你能看哪些方向")

    info = conversation.working_memory.turn_config
    assert info["config_ids"] == [sample]
    assert info["names"] == [str(catalog[0]["name"])]
    # 配置真的进了下游：走配置的字段资产推荐出了切入点卡。
    assert result["canvas"]["counts"].get("dimension_card", 0) >= 1


def test_data_query_without_direction_asks_and_does_not_query(
    monkeypatch: pytest.MonkeyPatch, conversation_id: str
) -> None:
    _stub_agent(
        monkeypatch,
        {
            "ok": True,
            "intent": "data_query",
            "config_keys": [],
            "dimension_keys": [],
            "reply": "可以，不过要先定方向。",
            "needs_clarification": True,
            "clarify_question": "你想看哪个方向？",
        },
    )

    result, conversation = _turn(conversation_id, "帮我看看数")

    assert conversation.working_memory.turn_config["config_ids"] == []
    assert conversation.working_memory.turn_config["note"] == "判定需要配置，但没确定是哪一套。"
    assert result["canvas"]["counts"].get("chart_card", 0) == 0


def test_query_gate_refuses_to_run_without_a_config(
    monkeypatch: pytest.MonkeyPatch, conversation_id: str
) -> None:
    """要数据但配置没落到具体某一套 → **不取数**，如实说明并追问。"""
    _stub_agent(
        monkeypatch,
        {
            "ok": True,
            "intent": "data_query",
            "config_keys": [],
            "dimension_keys": [],
            "filters": [{"field": "价格", "operator": ">=", "value": 400}],
            "reply": "好",
        },
    )

    result, conversation = _turn(conversation_id, "只看 400 元以上的")

    assert result["canvas"]["counts"].get("chart_card", 0) == 0
    assert "没有替你取数" in (result["message"]["content"] or "")
    # 被退回的筛选条件也要如实说明，否则用户不知道为什么没有取数。
    assert "价格" in (result["message"]["content"] or "")
    assert conversation.working_memory.turn_config["config_ids"] == []


def test_payload_carries_real_config_catalog(
    monkeypatch: pytest.MonkeyPatch, conversation_id: str, catalog: list[dict]
) -> None:
    """agent 拿到的必须是**配置中心真实存在的目录**，且字段词表要标出归属配置。"""
    sample = str(catalog[0]["scene_id"])
    calls = _stub_agent(
        monkeypatch,
        {"ok": True, "intent": "explore", "config_keys": [sample], "reply": "好", "dimension_keys": []},
    )

    _turn(conversation_id, "看看有哪些方向")

    sent = calls[0]
    assert [item["scene_id"] for item in sent["config_catalog"]] == [
        str(item["scene_id"]) for item in catalog
    ]
    assert all(entry["configs"] for entry in sent["filter_fields"])
    # 首轮还没用过配置，所以「上一轮那套」是空的——不能拿会话级场景冒充。
    assert sent["scene_id"] == ""
    assert sent["scene_name"] == ""


def test_second_turn_reports_previous_turn_config_to_agent(
    monkeypatch: pytest.MonkeyPatch, conversation_id: str, catalog: list[dict]
) -> None:
    """第二轮传给 agent 的「上一轮配置」= 上一轮**真正加载过**的那套。"""
    sample = str(catalog[0]["scene_id"])
    calls = _stub_agent(
        monkeypatch,
        {"ok": True, "intent": "explore", "config_keys": [sample], "reply": "好", "dimension_keys": []},
    )

    _turn(conversation_id, "看看有哪些方向")
    # 第二轮：只说"换成折线图"这类不涉及配置的话。
    calls = _stub_agent(
        monkeypatch,
        {"ok": True, "intent": "canvas_revision", "config_keys": [], "reply": "好", "dimension_keys": []},
    )
    _, conversation = _turn(conversation_id, "换个说法再看看")

    assert calls[0]["scene_id"] == sample
    assert calls[0]["scene_name"] == str(catalog[0]["name"])
    # 而本轮自己不加载配置。
    assert conversation.working_memory.turn_config["config_ids"] == []


# ------------------------------------------- 场景随轮切换（画布动作不得读旧场景）
#
# 配置是**轮次资源**：本轮到底用了哪几套，只有 working_memory.turn_config 知道。
# conversation.scene_id 只在 create() 时由关键词猜一次、之后永不更新。任何画布动作
# 若继续读它，就会出现"按 A 套取到的数，拿 B 套的字段去重算 / 去推荐方向"。
# 下面四条锁死这条不变量：scene_for 跟本轮、scene_for_chart 跟卡片自己的清单。


def _chart_card(conversation_id: str, artifact_id: str, lineage: dict) -> ArtifactCardDTO:
    """只造一张带 lineage 的图卡壳子——不塞任何业务数值，测试也不执行它。"""
    now = datetime.now(UTC)
    return ArtifactCardDTO(
        artifact_id=artifact_id,
        conversation_id=conversation_id,
        kind="chart_card",
        title="场景重放验证卡",
        subtitle="",
        status="ready",
        payload={"status": "ready"},
        lineage=lineage,
        created_at=now,
        updated_at=now,
    )


def _picked(monkeypatch: pytest.MonkeyPatch, scene_id: str) -> None:
    _stub_agent(
        monkeypatch,
        {"ok": True, "intent": "explore", "config_keys": [scene_id], "reply": "好", "dimension_keys": []},
    )


def test_scene_for_follows_the_turn_not_the_session_scene(
    monkeypatch: pytest.MonkeyPatch, conversation_id: str, catalog: list[dict]
) -> None:
    """同一会话跨两轮换配置后，scene_for 必须给**本轮**那套，而非会话级那套。

    这是本次修复的核心不变量：旧实现 `scene_for` 直接读 conversation.scene_id，
    而那个字段建会话后永不更新，于是「换一批维度 / 重跑某页」会拿旧配置算。
    """
    first, second = str(catalog[0]["scene_id"]), str(catalog[1]["scene_id"])
    service = conversation_service.conversation_service

    _picked(monkeypatch, first)
    _turn(conversation_id, "先看 A 方向")
    conversation = service.get(conversation_id)
    # 会话级场景刻意停在第一轮那套（真实世界里它往往停在建会话时的猜测值）。
    conversation.scene_id = first
    conversation.scene_name = str(catalog[0]["name"])

    _picked(monkeypatch, second)
    _, conversation = _turn(conversation_id, "换成 B 方向")
    assert conversation.working_memory.turn_config["config_ids"] == [second]

    # 关键断言：给本轮那套，绝不退回会话级（若退回会拿到 first）。
    scene = service.scene_for(conversation_id)
    assert scene is not None
    assert scene.scene_id == second
    assert scene.scene_id != conversation.scene_id or first == second


def test_scene_for_falls_back_to_session_scene_before_any_turn(
    conversation_id: str, catalog: list[dict]
) -> None:
    """还没跑过任何一轮时，退化读会话级场景——旧行为不作废，且此时画布上也没有产物。"""
    service = conversation_service.conversation_service
    conversation = service.get(conversation_id)
    sample = str(catalog[0]["scene_id"])
    conversation.scene_id = sample

    scene = service.scene_for(conversation_id)
    assert scene is not None
    assert scene.scene_id == sample


def test_scene_for_chart_replays_the_cards_own_config(
    monkeypatch: pytest.MonkeyPatch, conversation_id: str, catalog: list[dict]
) -> None:
    """跨轮换过配置后重跑旧卡：按**卡片自己**的清单重放，而不是本轮配置。

    重放的是历轮写下的 SQL，字段口径必须跟当初一致；拿本轮配置去校验，字段对不上
    就会给用户一个指向错方向的报错。
    """
    first, second = str(catalog[0]["scene_id"]), str(catalog[1]["scene_id"])
    service = conversation_service.conversation_service

    _picked(monkeypatch, second)
    _turn(conversation_id, "本轮用 B")

    # 这张卡诞生在第一套配置下（lineage 记着），后来用户换了配置。
    card = _chart_card(conversation_id, "art_replay", {"config_ids": [first]})
    scene = service.scene_for_chart(conversation_id, card)
    assert scene is not None
    assert scene.scene_id == first
    # 而本轮配置是 B——两者互不串味。
    assert service.scene_for(conversation_id).scene_id == second


def test_scene_for_chart_falls_back_through_lineage_scene_id_to_turn(
    monkeypatch: pytest.MonkeyPatch, conversation_id: str, catalog: list[dict]
) -> None:
    """卡片只记了场景 id → 用它；什么都没记 → 才退回本轮配置。"""
    sample = str(catalog[0]["scene_id"])
    service = conversation_service.conversation_service

    _picked(monkeypatch, sample)
    _turn(conversation_id, "本轮用 A")

    by_id = service.scene_for_chart(
        conversation_id, _chart_card(conversation_id, "art_by_id", {"scene_id": sample})
    )
    assert by_id is not None and by_id.scene_id == sample

    empty = service.scene_for_chart(conversation_id, _chart_card(conversation_id, "art_empty", {}))
    assert empty is not None and empty.scene_id == sample


# --------------------------------------- 逐轮配置回显（挂在消息上，供对话流回溯）
#
# 头部胶囊只反映**最后一轮**；要让用户翻历史时也能看出"这句用了哪套"，
# 就必须把本轮配置随消息落盘。下面锁死"消息真的带上了它"。


def test_assistant_message_carries_the_turn_config(
    monkeypatch: pytest.MonkeyPatch, conversation_id: str, catalog: list[dict]
) -> None:
    """加载了配置的那一轮：消息要带上 config_ids 与名称，供页面逐轮回显。"""
    sample = str(catalog[0]["scene_id"])
    _picked(monkeypatch, sample)

    result, _ = _turn(conversation_id, "看看有哪些方向")

    tc = result["message"]["turn_config"]
    assert tc["config_ids"] == [sample]
    assert tc["names"] == [str(catalog[0]["name"])]
    assert tc["source"] == "turn"


def test_chat_message_carries_the_no_config_note(
    monkeypatch: pytest.MonkeyPatch, conversation_id: str
) -> None:
    """闲聊轮：消息要带上"本轮不涉及配置"的说明，而不是空着让人猜。"""
    _stub_agent(
        monkeypatch,
        {"ok": True, "intent": "chat", "config_keys": [], "reply": "你好呀", "dimension_keys": []},
    )

    result, _ = _turn(conversation_id, "你好")

    tc = result["message"]["turn_config"]
    assert tc["config_ids"] == []
    assert tc["names"] == []
    assert tc["note"] == "本轮不涉及配置"


def test_failed_turn_message_carries_the_failure_note(
    monkeypatch: pytest.MonkeyPatch, conversation_id: str
) -> None:
    """agent 不可用那一轮：消息也要如实带上"未判定"的原因，别让页面显示成"不需要配置"。"""
    _stub_agent(
        monkeypatch,
        {"ok": False, "error": "URLError:<urlopen error [Errno 61] Connection refused>"},
        reachable=False,
    )

    result, _ = _turn(conversation_id, "看看 ZARA 的价格带分布")

    tc = result["message"]["turn_config"]
    assert tc["config_ids"] == []
    assert tc["note"] == "agent 服务连不上，本轮未判定"


def test_user_message_carries_no_turn_config(monkeypatch: pytest.MonkeyPatch, conversation_id: str) -> None:
    """用户消息不该有配置——它是"这一轮用了什么"的记录，只属于回答。"""
    _stub_agent(
        monkeypatch,
        {"ok": True, "intent": "chat", "config_keys": [], "reply": "嗯", "dimension_keys": []},
    )

    _turn(conversation_id, "你好")
    messages = conversation_service.conversation_service.messages(conversation_id)
    user_messages = [m for m in messages if m.role == "user"]
    assert user_messages
    assert all(not (m.turn_config or {}) for m in user_messages)


# ------------------------------- 切入点卡（卡片侧）也要跟着按需配置走（2026-10-13）
#
# 真实报障（会话 conv_5152ebdb28）：纤维配置推出来的切入点卡被后续「竞品与价格分析」
# 轮次复用，价格方向写给 SQL agent 的目标仍是 `各二级类目的纤维占比分布情况`，
# 而本轮字段里根本没有纤维口径 → agent 只能回"当前 semantic_fields 中没有价格/金额字段"，
# 用户拿到一张澄清卡。根因不是需求不清，是**卡片侧没接上按需配置**：
# 旧实现的重建条件只看品牌，同一个品牌跨轮换配置时卡片原样沿用。
#
# 打桩依赖（agent / 持久化），配置与字段全部来自真实配置中心。


def _canvas_dimension_card(conversation_id: str) -> dict:
    service = conversation_service.conversation_service
    for item in service.canvas(conversation_id)["items"]:
        if item["kind"] == "dimension_card":
            return item
    raise AssertionError("画布上没有切入点卡")


def _scene_metric_names(scene) -> set[str]:
    names: set[str] = set()
    for field in scene.fields:
        role = getattr(field, "role", "")
        role = str(getattr(role, "value", role)).strip().lower()
        if role == "metric" and getattr(field, "enabled", True):
            names.add(str(getattr(field, "semantic_name", "")))
    return names


def _scene_options(scene_id: str, *, limit: int | None = None) -> list[dict]:
    """某套配置能推出的方向。

    ``limit=None`` 走推荐器默认（= 卡片上的入口数）；给 ``limit`` 时是"这套配置能服务的
    **全部**方向"（判定与取数目标用的那份，见 `_turn_dimension_options`）。
    """
    scene = turn_config_service.reload([scene_id])
    assert scene is not None, f"配置中心里读不到 {scene_id}"
    kwargs = {} if limit is None else {"limit": limit}
    return dimension_recommender.recommend(scene=scene, brand="", **kwargs)


def test_dimension_card_rebuilds_when_the_turn_config_changes(
    monkeypatch: pytest.MonkeyPatch, conversation_id: str, catalog: list[dict]
) -> None:
    """跨轮换配置 → 切入点卡按**本轮配置**重建，而不是沿用上一套。

    旧实现只在"品牌变了"时重建，于是卡片上的目标/指标/维度一直是建卡那套配置的，
    与这一轮真正加载的配置错位。
    """
    first, second = str(catalog[0]["scene_id"]), str(catalog[1]["scene_id"])
    expected_first, expected_second = _scene_options(first), _scene_options(second)
    if expected_first == expected_second:
        pytest.skip("这两套配置推出来的方向完全相同，换配置这件事观察不到")

    _picked(monkeypatch, first)
    _turn(conversation_id, "先看 A 方向")
    card = _canvas_dimension_card(conversation_id)
    assert card["payload"]["config_ids"] == [first]
    assert card["payload"]["config_source"] == "turn"
    assert card["payload"]["options"] == expected_first

    _picked(monkeypatch, second)
    _turn(conversation_id, "换成 B 方向")
    card = _canvas_dimension_card(conversation_id)
    assert card["payload"]["config_ids"] == [second]
    assert card["payload"]["options"] == expected_second
    assert card["payload"]["options"] != expected_first


def test_dimension_card_is_built_from_the_turn_config_not_the_merged_view(
    monkeypatch: pytest.MonkeyPatch, conversation_id: str, catalog: list[dict]
) -> None:
    """建卡用的是**本轮配置**，不是"全部配置的并集"。

    判定前那一步不得不用合并视图（agent 得先看到可选方向才能选），但**落卡**必须落到
    本轮判定出来的那套上——否则卡片上的口径与取数用的字段是两套。
    """
    sample = str(catalog[0]["scene_id"])
    scenes_used: list[str] = []
    original = dimension_recommender.recommend

    def counting(**kwargs):
        scenes_used.append(str(kwargs.get("scene").scene_id))
        return original(**kwargs)

    monkeypatch.setattr(conversation_service.dimension_recommender, "recommend", counting)

    _picked(monkeypatch, sample)
    _turn(conversation_id, "看看有哪些方向")
    calls_after_first = len(scenes_used)
    assert scenes_used, "建卡必须经过方向推荐"
    assert scenes_used[-1] == sample, "落卡那一次用的必须就是本轮配置"

    # 同一套配置内再问一轮：卡片身份没变 → 不再重建（"换一批"的轮换结果不许被悄悄重置）。
    _picked(monkeypatch, sample)
    _turn(conversation_id, "还有哪些方向")
    assert len(scenes_used) == calls_after_first


def test_direction_from_a_previous_config_is_refused_with_the_truth(
    monkeypatch: pytest.MonkeyPatch, conversation_id: str, catalog: list[dict]
) -> None:
    """上一套配置里的方向，在本轮配置下**不许硬凑**取数：如实说明并列出本轮可选方向。

    真实形态：纤维配置的卡上有「面料成分与纤维混纺」，用户切到价格配置后又说这个词，
    旧实现会拿价格方向的指标硬凑一个目标交给 SQL agent（字段对不上 → 废卡）。

    ⚠️ 判据是"这套配置**能服务的全部方向**"，不是"卡片上那 4 个入口"：某个方向排在第五位、
    没进卡片，不代表这一轮没有它（实测 `scene_prd_trend` 也能按品类结构分组）。
    """
    from app.services.conversation_service import MAX_DIMENSION_CANDIDATES

    scene_ids = [str(item["scene_id"]) for item in catalog]
    options_by_scene = {
        sid: _scene_options(sid, limit=MAX_DIMENSION_CANDIDATES) for sid in scene_ids
    }
    keys_by_scene = {
        sid: {str(option["dimension_key"]) for option in options}
        for sid, options in options_by_scene.items()
    }

    pair: tuple[str, str, list[str]] | None = None
    for holder in scene_ids:
        for narrow in scene_ids:
            if holder == narrow:
                continue
            exclusive = sorted(keys_by_scene[holder] - keys_by_scene[narrow])
            if exclusive:
                pair = (holder, narrow, exclusive)
                break
        if pair is not None:
            break
    if pair is None:
        pytest.skip("配置中心里没有'方向集合互不包含'的两套配置，这个形态观察不到")
    holder, narrow, exclusive = pair

    _picked(monkeypatch, holder)
    _turn(conversation_id, "看看有哪些方向")
    assert exclusive[0] in keys_by_scene[holder]

    _stub_agent(
        monkeypatch,
        {
            "ok": True,
            "intent": "data_query",
            "config_keys": [narrow],
            "dimension_keys": [exclusive[0]],
            "reply": "",
            "filters": [],
        },
    )
    result, conversation = _turn(conversation_id, f"先看{exclusive[0]}")

    assert conversation.working_memory.turn_config["config_ids"] == [narrow]
    assert result["canvas"]["counts"].get("chart_card", 0) == 0, "不许用本轮配置硬凑出图"
    content = str(result["message"]["content"] or "")
    assert exclusive[0] in content
    assert str(next(item["name"] for item in catalog if str(item["scene_id"]) == narrow)) in content


def test_direction_outside_the_card_entry_points_is_still_executable(
    monkeypatch: pytest.MonkeyPatch, conversation_id: str, catalog: list[dict]
) -> None:
    """方向没进卡片的 4 个入口时，本轮**仍然要能按它取数**，不许误判成"本轮没有"。

    页面上的卡只放 `MAX_CARDS` 个入口（版面取舍），但"这一轮能不能按某个方向取数"要按
    **这套配置能服务的全部方向**判定 —— 实测 `scene_prd_trend` 也能按品类结构分组，
    只是没进卡的 4 个入口。只按卡片判定的实现会把这种方向误拒；`allowed_keys` 那一步
    也要能看见它，所以判定前给的合并视图同样不能按页面上限截断。
    """
    from app.services.conversation_service import MAX_DIMENSION_CANDIDATES, ConversationService

    target: tuple[str, str] | None = None
    for item in catalog:
        scene_id = str(item["scene_id"])
        card_keys = {str(option["dimension_key"]) for option in _scene_options(scene_id)}
        all_keys = {
            str(option["dimension_key"])
            for option in _scene_options(scene_id, limit=MAX_DIMENSION_CANDIDATES)
        }
        hidden = sorted(all_keys - card_keys)
        if hidden:
            target = (scene_id, hidden[0])
            break
    if target is None:
        pytest.skip("配置中心里没有'方向多于卡片入口'的配置，这个形态观察不到")
    scene_id, hidden_key = target

    calls: list[str] = []
    original = ConversationService._execute_goals

    def _stub(self, **kwargs):  # noqa: ANN001, ANN202
        calls.append(",".join(str(item) for item in kwargs.get("keys") or []))
        return {"assistant_text": "（桩：未真实取数）", "artifact_refs": [], "actions": []}

    monkeypatch.setattr(ConversationService, "_execute_goals", _stub)
    _stub_agent(
        monkeypatch,
        {
            "ok": True,
            "intent": "data_query",
            "config_keys": [scene_id],
            "dimension_keys": [hidden_key],
            "reply": "",
            "filters": [],
        },
    )

    result, conversation = _turn(conversation_id, f"看看{hidden_key}")

    content = str(result["message"]["content"] or "")
    assert conversation.working_memory.turn_config["config_ids"] == [scene_id]
    assert "里没有「" not in content, f"卡外的方向被误拒了：{content}"
    assert calls == [hidden_key], f"取数没有按这个方向执行：{calls}"
    assert original is not None  # 桩只在本次用例内生效


def test_dimension_metrics_never_borrow_another_directions_measure(catalog: list[dict]) -> None:
    """方向绑定的度量必须来自**本套配置**、且属于这个方向；找不到就如实降级为「记录数」。

    旧实现 `chosen_metrics = metrics[:1]`：纤维配置（9 个字段、唯一指标「纤维占比」）
    里的「价格带与折扣节奏」被贴成 `各二级类目的纤维占比分布情况`、「品牌竞争格局」被贴成
    `按品牌统计纤维占比` —— 卡片标题写着价格、口径却是纤维，取数时字段对不上。
    这条守卫是 fail-closed 的：把 `metrics[:1]` 放回去，`纤维占比` 就会出现在 fiber 以外的方向上。
    """
    for item in catalog:
        scene_id = str(item["scene_id"])
        scene = turn_config_service.reload([scene_id])
        assert scene is not None
        own_metrics = _scene_metric_names(scene)
        for option in dimension_recommender.recommend(scene=scene, brand=""):
            assert option["metrics"], f"{scene_id}/{option['dimension_key']} 没有口径"
            for name in option["metrics"]:
                if name == COUNT_METRIC:
                    # 如实降级的计数口径：有名字、会显示在卡片上，不是从别处借来的指标。
                    continue
                assert name in own_metrics, (
                    f"{scene_id}/{option['dimension_key']} 的指标「{name}」不在本套配置的指标字段里"
                )
                # 纤维占比是纤维方向独有的度量：别的方向在本套配置里根本没有它的字段。
                if name == "纤维占比":
                    assert option["dimension_key"] == "fiber", (
                        f"{scene_id} 的「{option['title']}」绑上了纤维口径——这正是 metrics[:1] 兜底"
                    )
