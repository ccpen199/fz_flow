from types import SimpleNamespace

from app.services.sql_result_agent_service import SqlResultAgentService


def test_queryable_fields_merge_current_scene_fields_with_stale_cache(monkeypatch) -> None:
    service = SqlResultAgentService()
    scene = SimpleNamespace(
        scene_id="scene_0004",
        fields=[
            SimpleNamespace(
                semantic_name="二级类目",
                table_name="clothing_info",
                field_name="SubCategory",
                role="dimension",
                enabled=True,
            ),
            SimpleNamespace(
                semantic_name="标准纤维名称",
                table_name="dict_fiber_info",
                field_name="Name",
                role="dimension",
                enabled=True,
            ),
            SimpleNamespace(
                semantic_name="已禁用字段",
                table_name="clothing_info",
                field_name="Other",
                role="dimension",
                enabled=False,
            ),
        ],
    )
    monkeypatch.setattr(
        "app.services.sql_result_agent_service.semantic_field_cache_service.get_queryable_scene_fields",
        lambda scene_id: [
            SimpleNamespace(
                semantic_name="二级类目",
                table_name="clothing_info",
                field_name="SubCategory",
                role="dimension",
            ),
            SimpleNamespace(
                semantic_name="旧叶子类目",
                table_name="clothing_info",
                field_name="LeafCategory",
                role="dimension",
            ),
        ],
    )

    fields = service._queryable_semantic_fields(scene)

    assert [(item["semantic_name"], item["table_name"], item["field_name"]) for item in fields] == [
        ("二级类目", "clothing_info", "SubCategory"),
        ("标准纤维名称", "dict_fiber_info", "Name"),
    ]
