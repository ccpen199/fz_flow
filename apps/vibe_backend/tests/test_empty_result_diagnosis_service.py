"""空结果诊断的回归测试（对应 2026-10-17 会话 `conv_dbc7be9bf4` 的死循环报障）。

锁住三件事：
1. 能指认出**真正**筛空结果的那个条件，并给出该字段在剩余人群里的真实取值；
2. 同族写法（`T恤` ⊂ `T恤/Polo衫/背心`）优先于"随便换个值"；
3. 判不了时一律 `checked=False`（不猜），且多跳 JOIN 不会因为边的顺序静默失败。

数据库一律打桩：`_connect` 被替换成假连接，断言的是**判定逻辑**，不是真实数值。
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import empty_result_diagnosis_service as module
from app.services.empty_result_diagnosis_service import _QueryBuilder, empty_result_diagnosis_service
from packages.shared_contracts.python_models import (
    FieldRole,
    SceneDTO,
    SceneFieldDTO,
    SceneRelationDTO,
)


# ---------------------------------------------------------------- fixtures


def _field(table: str, name: str, semantic: str, role: FieldRole = FieldRole.DIMENSION) -> SceneFieldDTO:
    return SceneFieldDTO(
        field_id=f"{table}.{name}",
        table_name=table,
        field_name=name,
        semantic_name=semantic,
        description="",
        role=role,
    )


def _scene() -> SceneDTO:
    """服装明细 + 品牌字典，关系与真实场景一致（clothing_info.BrandCode ↔ dict_brand_info.Code）。"""
    return SceneDTO(
        scene_id="scene_test",
        name="测试场景",
        description="",
        fields=[
            _field("clothing_info", "Id", "记录ID"),
            _field("clothing_info", "BrandCode", "品牌编码"),
            _field("clothing_info", "SubCategory", "二级类目"),
            _field("clothing_info", "Price", "价格", FieldRole.METRIC),
            _field("dict_brand_info", "Code", "品牌编码"),
            _field("dict_brand_info", "Name", "品牌"),
        ],
        relations=[
            SceneRelationDTO(
                relation_id="rel_brand",
                left_table="clothing_info",
                left_field="BrandCode",
                right_table="dict_brand_info",
                right_field="Code",
                join_type="LEFT",
            )
        ],
    )


def _filters() -> list[dict[str, Any]]:
    return [
        {
            "field": "品牌",
            "operator": "=",
            "value": "优衣库（官网）",
            "table_name": "dict_brand_info",
            "raw_field_name": "Name",
            "display": "品牌 = 优衣库（官网）",
        },
        {
            "field": "二级类目",
            "operator": "=",
            "value": "T恤",
            "table_name": "clothing_info",
            "raw_field_name": "SubCategory",
            "display": "二级类目 = T恤",
        },
    ]


class _FakeCursor:
    """只做一件事：按 SQL 形态回放事先准备好的计数，绝不真连库。"""

    def __init__(self, responder) -> None:
        self._responder = responder
        self._rows: list[dict[str, Any]] = []

    def execute(self, sql: str, params: list[Any] | None = None) -> None:
        self._rows = self._responder(sql, list(params or []))

    def fetchone(self) -> dict[str, Any]:
        return self._rows[0] if self._rows else {}

    def fetchall(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def __enter__(self) -> "_FakeCursor":
        return self

    def __exit__(self, *exc) -> bool:
        return False


class _FakeConnection:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def __enter__(self) -> "_FakeConnection":
        return self

    def __exit__(self, *exc) -> bool:
        return False


def _install(monkeypatch: pytest.MonkeyPatch, responder) -> None:
    cursor = _FakeCursor(responder)
    monkeypatch.setattr(module, "_connect", lambda: _FakeConnection(cursor))


def _clothing_counts(sql: str, params: list[Any]) -> list[dict[str, Any]]:
    """模拟真实数据分布：品牌 417 条，其中二级类目写作 `T恤/Polo衫/背心`（121 条），没有裸 `T恤`。"""
    if "GROUP BY" in sql:
        return [
            {"value": "T恤/Polo衫/背心", "n": 121},
            {"value": "外套", "n": 80},
        ]
    if params == ["优衣库（官网）", "T恤"]:
        return [{"n": 0}]  # 两个条件同时生效 → 空结果（真实报障现场）
    if params == ["T恤"]:
        return [{"n": 0}]  # 只剩类目条件也查不到
    if params == ["优衣库（官网）"]:
        return [{"n": 417}]  # 去掉类目就有数
    return [{"n": 417}]  # 不带条件


# ---------------------------------------------------------------- diagnose


def test_diagnose_points_at_the_filter_that_empties_the_result(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _clothing_counts)

    result = empty_result_diagnosis_service.diagnose(scene=_scene(), filters=_filters())

    assert result["checked"] is True
    blocker = result["blocker"]
    assert blocker["field"] == "二级类目"
    assert blocker["rows_if_dropped"] == 417
    # 同族写法优先：`T恤` ⊂ `T恤/Polo衫/背心`；同时必须标出这是**合并标签**
    # （含 Polo衫、背心），卡片才能如实说明"不是等价替换"
    top = result["alternatives"][0]
    assert top["field"] == "二级类目"
    assert top["value"] == "T恤/Polo衫/背心"
    assert top["rows"] == 121
    assert top["merged_parts"] == ["T恤", "Polo衫", "背心"]
    # 品牌是用户明确选的实体，不应被建议换掉
    assert all(item["field"] != "品牌" for item in result["alternatives"])


def test_diagnose_prefers_sibling_spelling_over_arbitrary_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """两个条件都能"剔出数"时，只认有同族写法的那一个。"""

    def responder(sql: str, params: list[Any]) -> list[dict[str, Any]]:
        if "GROUP BY" in sql and "SubCategory" in sql:
            return [{"value": "T恤/Polo衫/背心", "n": 121}]
        if "GROUP BY" in sql:
            # 品牌字段的真实取值里没有"包含优衣库（官网）"的同族写法
            return [{"value": "优衣库（淘宝）", "n": 100}, {"value": "优衣库（日本）", "n": 80}]
        if params == ["优衣库（官网）", "T恤"]:
            return [{"n": 0}]
        return [{"n": 417}]

    _install(monkeypatch, responder)

    result = empty_result_diagnosis_service.diagnose(scene=_scene(), filters=_filters())

    assert result["checked"] is True
    # 品牌字段的备选里没有"包含优衣库（官网）"的同族写法（只有另一个地区实体），
    # 所以它不该抢走堵点位置。
    assert result["blocker"]["field"] == "二级类目"


def test_diagnose_returns_unchecked_when_no_filter_can_be_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """剔掉任何一个条件仍然 0 行 → 不是某条筛选的锅，不许指认。"""
    _install(monkeypatch, lambda sql, params: [{"n": 0}])

    result = empty_result_diagnosis_service.diagnose(scene=_scene(), filters=_filters())

    assert result["checked"] is True
    assert result["blockers"] == []
    assert "blocker" not in result
    assert result["alternatives"] == []


def test_diagnose_returns_unchecked_without_usable_filters() -> None:
    assert empty_result_diagnosis_service.diagnose(scene=_scene(), filters=[]) == {
        "checked": False,
        "reason": "没有可诊断的结构化筛选条件",
    }


def test_diagnose_ignores_filters_outside_scene_whitelist() -> None:
    """表/列不在场景白名单里 → 不拼进 SQL（防止拼错表或注入）。"""
    filters = [
        {
            "field": "不存在的字段",
            "operator": "=",
            "value": "x",
            "table_name": "clothing_info",
            "raw_field_name": "not_a_column",
        }
    ]
    assert empty_result_diagnosis_service.diagnose(scene=_scene(), filters=filters)["checked"] is False


def test_diagnose_is_fail_closed_when_database_is_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> Any:
        raise ConnectionError("connection refused")

    monkeypatch.setattr(module, "_connect", boom)

    result = empty_result_diagnosis_service.diagnose(scene=_scene(), filters=_filters())

    assert result["checked"] is False
    assert "ConnectionError" in result["error"]


# ---------------------------------------------------------------- JOIN 拼接


def test_query_builder_orders_joins_from_main_table_outward() -> None:
    """BFS 回溯给出的边顺序可能是"远端在前"，拼接时必须按可连通的顺序来。"""
    relations = _scene().relations
    builder = _QueryBuilder(
        main_table="clothing_info",
        edges=list(reversed(relations)),  # 故意打乱：真实链路里也会出现
        columns={"clothing_info": {"Id", "BrandCode", "SubCategory"}},
    )

    assert builder.aliases == {"clothing_info": "t0", "dict_brand_info": "t1"}
    assert "JOIN `dict_brand_info` t1 ON t1.`Code` = t0.`BrandCode`" in builder.joins[0]
    assert builder.count_expr == "COUNT(DISTINCT t0.`Id`)"


def test_query_builder_raises_when_edges_cannot_be_connected() -> None:
    unrelated = SceneRelationDTO(
        relation_id="rel_x",
        left_table="table_a",
        left_field="id",
        right_table="table_b",
        right_field="id",
    )
    with pytest.raises(ValueError):
        _QueryBuilder(main_table="clothing_info", edges=[unrelated], columns={"clothing_info": {"Id"}})


def test_join_edges_returns_none_for_unconnected_tables() -> None:
    scene = _scene()
    scene.relations = []
    assert module.EmptyResultDiagnosisService._join_edges(scene, "clothing_info", {"dict_brand_info"}) is None


def test_join_edges_walks_multiple_hops() -> None:
    scene = _scene()
    scene.relations = [
        SceneRelationDTO(
            relation_id="rel_1",
            left_table="clothing_info",
            left_field="BrandCode",
            right_table="middle_table",
            right_field="code",
        ),
        SceneRelationDTO(
            relation_id="rel_2",
            left_table="middle_table",
            left_field="code",
            right_table="far_table",
            right_field="code",
        ),
    ]

    edges = module.EmptyResultDiagnosisService._join_edges(
        scene, "clothing_info", {"far_table"}
    )

    assert [edge.relation_id for edge in edges] == ["rel_2", "rel_1"]


def test_fixed_filters_constrain_counts_but_are_never_blamed(monkeypatch: pytest.MonkeyPatch) -> None:
    """已确认的品牌必须参与计数（否则备选值是全表行数），但不许被建议"去掉"。

    实测（2026-10-18）：不带品牌约束时卡片说"改用 T恤/Polo衫/背心（10814 条）"，
    而官网这批数据里真实只有 121 条 —— 数字是真的，人群却是错的。
    """
    brand = {
        "field": "品牌",
        "operator": "=",
        "value": "优衣库（官网）",
        "table_name": "dict_brand_info",
        "raw_field_name": "Name",
        "display": "品牌 =优衣库（官网）",
    }
    category = {
        "field": "二级类目",
        "operator": "=",
        "value": "T恤",
        "table_name": "clothing_info",
        "raw_field_name": "SubCategory",
        "display": "二级类目 =T恤",
    }

    def responder(sql: str, params: list[Any]) -> list[dict[str, Any]]:
        if "GROUP BY" in sql:
            return [{"value": "T恤/Polo衫/背心", "n": 121}]
        if params == ["优衣库（官网）", "T恤"]:
            return [{"n": 0}]
        if params == ["优衣库（官网）"]:
            return [{"n": 417}]
        return [{"n": 97879}]  # 全表：不约束品牌时就会拿到这个数

    _install(monkeypatch, responder)

    result = empty_result_diagnosis_service.diagnose(
        scene=_scene(), filters=[category], fixed_filters=[brand]
    )

    assert result["checked"] is True
    assert [item["field"] for item in result["blockers"]] == ["二级类目"]
    assert result["blocker"]["rows_if_dropped"] == 417
    assert result["alternatives"][0]["rows"] == 121


def test_only_fixed_filters_is_not_diagnosable(monkeypatch: pytest.MonkeyPatch) -> None:
    """只剩"不能动"的条件时没有可诊断的东西，如实返回 checked=False。"""
    _install(monkeypatch, lambda sql, params: [{"n": 0}])
    brand = {
        "field": "品牌",
        "operator": "=",
        "value": "优衣库（官网）",
        "table_name": "dict_brand_info",
        "raw_field_name": "Name",
    }

    result = empty_result_diagnosis_service.diagnose(
        scene=_scene(), filters=[], fixed_filters=[brand]
    )

    assert result["checked"] is False


def test_plain_alias_is_not_marked_as_merged(monkeypatch: pytest.MonkeyPatch) -> None:
    """同族写法（拼写/别名）不应被标成合并标签 —— 只有 `A/B/C` 这种并值才是。"""
    _install(
        monkeypatch,
        lambda sql, params: (
            [{"value": "T-shirt", "n": 88}] if "GROUP BY" in sql
            else ([{"n": 0}] if params == ["优衣库（官网）", "T恤"] else [{"n": 417}])
        ),
    )

    result = empty_result_diagnosis_service.diagnose(scene=_scene(), filters=_filters())

    alias = result["alternatives"][0]
    assert alias["value"] == "T-shirt"
    assert "merged_parts" not in alias
