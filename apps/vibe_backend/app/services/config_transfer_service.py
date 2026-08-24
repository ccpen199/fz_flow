from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from packages.shared_contracts.python_models import SceneDTO, SceneFieldDTO, SceneRelationDTO

from .scene_cache_service import scene_cache_service
from .semantic_field_cache_service import semantic_field_cache_service

ImportMode = Literal["create", "merge", "replace"]
CONFIG_BUNDLE_FORMAT = "vibe_scene_config"
CONFIG_BUNDLE_VERSION = 1


def _text(value: Any) -> str:
    return str(value or "").strip()


def _field_key(field: dict[str, Any] | SceneFieldDTO) -> tuple[str, str, str]:
    if isinstance(field, SceneFieldDTO):
        return (
            field.table_name.lower(),
            field.field_name.lower(),
            field.semantic_name.lower(),
        )
    return (
        _text(field.get("table_name")).lower(),
        _text(field.get("field_name")).lower(),
        _text(field.get("semantic_name")).lower(),
    )


def _relation_key(relation: dict[str, Any] | SceneRelationDTO) -> tuple[str, str, str, str, str]:
    if isinstance(relation, SceneRelationDTO):
        return (
            relation.left_table.lower(),
            relation.left_field.lower(),
            relation.right_table.lower(),
            relation.right_field.lower(),
            relation.join_type.upper(),
        )
    return (
        _text(relation.get("left_table")).lower(),
        _text(relation.get("left_field")).lower(),
        _text(relation.get("right_table")).lower(),
        _text(relation.get("right_field")).lower(),
        _text(relation.get("join_type") or "INNER").upper(),
    )


def _normalize_semantic_field(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("semantic_fields must contain objects")
    semantic_name = _text(item.get("semantic_name"))
    table_name = _text(item.get("table_name"))
    field_name = _text(item.get("field_name"))
    if not semantic_name or not table_name or not field_name:
        raise ValueError("semantic field requires semantic_name, table_name and field_name")
    role = _text(item.get("role") or "dimension").lower()
    if role not in {"metric", "dimension", "time", "filter"}:
        raise ValueError(f"invalid semantic field role: {role}")
    aliases = item.get("aliases")
    if not isinstance(aliases, list):
        aliases = []
    return {
        "semantic_name": semantic_name,
        "semantic_definition": _text(item.get("semantic_definition")),
        "aliases": [value for value in (_text(alias) for alias in aliases) if value],
        "unit": _text(item.get("unit")),
        "aggregation": _text(item.get("aggregation")),
        "table_name": table_name,
        "field_name": field_name,
        "er_path": _text(item.get("er_path")),
        "role": role,
        "zone": _text(item.get("zone") or "modeled").lower(),
        "enabled": bool(item.get("enabled", True)),
    }


def _normalize_bundle(bundle: Any) -> dict[str, Any]:
    if not isinstance(bundle, dict):
        raise ValueError("configuration file must contain a JSON object")
    if bundle.get("format") != CONFIG_BUNDLE_FORMAT:
        raise ValueError(f"unsupported configuration format: {_text(bundle.get('format')) or 'missing'}")
    try:
        version = int(bundle.get("version", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("configuration version must be an integer") from exc
    if version != CONFIG_BUNDLE_VERSION:
        raise ValueError(f"unsupported configuration version: {version}")

    scene = bundle.get("scene")
    if not isinstance(scene, dict):
        raise ValueError("configuration file is missing scene")
    scene_id = _text(scene.get("scene_id"))
    name = _text(scene.get("name"))
    if not scene_id or not name:
        raise ValueError("scene requires scene_id and name")

    raw_fields = scene.get("fields")
    raw_relations = scene.get("relations")
    if not isinstance(raw_fields, list) or not isinstance(raw_relations, list):
        raise ValueError("scene.fields and scene.relations must be arrays")

    fields: list[dict[str, Any]] = []
    field_keys: set[tuple[str, str, str]] = set()
    for item in raw_fields:
        if not isinstance(item, dict):
            raise ValueError("scene.fields must contain objects")
        table_name = _text(item.get("table_name"))
        field_name = _text(item.get("field_name"))
        semantic_name = _text(item.get("semantic_name"))
        if not table_name or not field_name or not semantic_name:
            raise ValueError("scene field requires table_name, field_name and semantic_name")
        field = {
            "table_name": table_name,
            "field_name": field_name,
            "semantic_name": semantic_name,
            "description": _text(item.get("description")),
            "role": _text(item.get("role") or "dimension").lower(),
            "enabled": bool(item.get("enabled", True)),
        }
        if field["role"] not in {"metric", "dimension", "time", "filter"}:
            raise ValueError(f"invalid scene field role: {field['role']}")
        key = _field_key(field)
        if key in field_keys:
            continue
        field_keys.add(key)
        fields.append(field)

    relations: list[dict[str, Any]] = []
    relation_keys: set[tuple[str, str, str, str, str]] = set()
    for item in raw_relations:
        if not isinstance(item, dict):
            raise ValueError("scene.relations must contain objects")
        relation = {
            "left_table": _text(item.get("left_table")),
            "left_field": _text(item.get("left_field")),
            "right_table": _text(item.get("right_table")),
            "right_field": _text(item.get("right_field")),
            "join_type": _text(item.get("join_type") or "INNER").upper(),
            "note": _text(item.get("note")),
        }
        if not all(relation[key] for key in ("left_table", "left_field", "right_table", "right_field")):
            raise ValueError("scene relation requires both table.field sides")
        if relation["join_type"] not in {"INNER", "LEFT", "RIGHT", "FULL"}:
            raise ValueError(f"invalid relation join_type: {relation['join_type']}")
        key = _relation_key(relation)
        if key in relation_keys:
            continue
        relation_keys.add(key)
        relations.append(relation)

    semantic_fields_raw = bundle.get("semantic_fields", [])
    if not isinstance(semantic_fields_raw, list):
        raise ValueError("semantic_fields must be an array")
    semantic_fields: list[dict[str, Any]] = []
    semantic_keys: set[tuple[str, str, str, str]] = set()
    for item in semantic_fields_raw:
        field = _normalize_semantic_field(item)
        if field["zone"] not in {"modeled", "effective"}:
            raise ValueError(f"invalid semantic field zone: {field['zone']}")
        key = (
            field["zone"],
            field["semantic_name"].lower(),
            field["table_name"].lower(),
            field["field_name"].lower(),
        )
        if key in semantic_keys:
            continue
        semantic_keys.add(key)
        semantic_fields.append(field)

    return {
        "format": CONFIG_BUNDLE_FORMAT,
        "version": CONFIG_BUNDLE_VERSION,
        "scene": {
            "scene_id": scene_id,
            "name": name,
            "description": _text(scene.get("description")),
            "version": int(scene.get("version") or 1),
            "sample_goals": [
                value
                for value in (_text(item) for item in scene.get("sample_goals", []))
                if value
            ]
            if isinstance(scene.get("sample_goals", []), list)
            else [],
            "fields": fields,
            "relations": relations,
        },
        "semantic_fields": semantic_fields,
    }


class ConfigTransferService:
    def export_scene(self, scene_id: str) -> dict[str, Any]:
        scene = scene_cache_service.get_scene(scene_id)
        if not scene:
            raise ValueError("scene not found")
        semantic_fields: list[dict[str, Any]] = []
        for zone in ("modeled", "effective"):
            semantic_fields.extend(
                semantic_field_cache_service.list_scene_fields(
                    scene_id,
                    zone=zone,
                    include_disabled=True,
                )
            )
        return {
            "format": CONFIG_BUNDLE_FORMAT,
            "version": CONFIG_BUNDLE_VERSION,
            "exported_at": datetime.now(UTC).isoformat(),
            "scene": scene.model_dump(mode="json"),
            "semantic_fields": [
                {
                    "semantic_name": item.get("semantic_name", ""),
                    "semantic_definition": item.get("semantic_definition", ""),
                    "aliases": item.get("aliases", []),
                    "unit": item.get("unit", ""),
                    "aggregation": item.get("aggregation", ""),
                    "table_name": item.get("table_name", ""),
                    "field_name": item.get("field_name", ""),
                    "er_path": item.get("er_path", ""),
                    "role": item.get("role", "dimension"),
                    "zone": item.get("zone", "modeled"),
                    "enabled": bool(item.get("enabled", True)),
                }
                for item in semantic_fields
            ],
        }

    def preview_import(
        self,
        bundle: Any,
        *,
        target_scene_id: str | None = None,
        mode: ImportMode = "create",
    ) -> dict[str, Any]:
        normalized = _normalize_bundle(bundle)
        if mode not in {"create", "merge", "replace"}:
            raise ValueError("mode must be create, merge or replace")
        target_id = _text(target_scene_id)
        target = scene_cache_service.get_scene(target_id) if target_id else None
        if mode in {"merge", "replace"} and not target:
            raise ValueError("target scene is required for merge or replace")

        incoming_scene = normalized["scene"]
        incoming_fields = incoming_scene["fields"]
        incoming_relations = incoming_scene["relations"]
        incoming_semantic_fields = normalized["semantic_fields"]

        existing_fields = {_field_key(field) for field in target.fields} if target else set()
        existing_relations = {_relation_key(relation) for relation in target.relations} if target else set()
        existing_semantic_fields = (
            {
                (
                    _text(item.get("zone") or "modeled").lower(),
                    _text(item.get("semantic_name")).lower(),
                    _text(item.get("table_name")).lower(),
                    _text(item.get("field_name")).lower(),
                )
                for item in semantic_field_cache_service.list_scene_fields(
                    target.scene_id,
                    zone="all",
                    include_disabled=True,
                )
            }
            if target
            else set()
        )

        return {
            "ok": True,
            "mode": mode,
            "source_scene": {
                "scene_id": incoming_scene["scene_id"],
                "name": incoming_scene["name"],
            },
            "target_scene": {
                "scene_id": target.scene_id if target else None,
                "name": target.name if target else incoming_scene["name"],
                "will_create": mode == "create",
            },
            "counts": {
                "fields": len(incoming_fields),
                "relations": len(incoming_relations),
                "semantic_fields": len(incoming_semantic_fields),
                "new_fields": sum(_field_key(item) not in existing_fields for item in incoming_fields),
                "new_relations": sum(_relation_key(item) not in existing_relations for item in incoming_relations),
                "new_semantic_fields": sum(
                    (
                        _text(item.get("zone") or "modeled").lower(),
                        _text(item.get("semantic_name")).lower(),
                        _text(item.get("table_name")).lower(),
                        _text(item.get("field_name")).lower(),
                    )
                    not in existing_semantic_fields
                    for item in incoming_semantic_fields
                ),
            },
            "warnings": [
                "导入为新场景不会修改现有场景。"
                if mode == "create"
                else (
                    "合并模式只新增或更新同一配置键，不删除目标场景已有配置。"
                    if mode == "merge"
                    else "覆盖模式会替换目标场景字段、关系和语义字段。"
                )
            ],
            "bundle": normalized,
        }

    def import_bundle(
        self,
        bundle: Any,
        *,
        target_scene_id: str | None = None,
        mode: ImportMode = "create",
    ) -> dict[str, Any]:
        preview = self.preview_import(bundle, target_scene_id=target_scene_id, mode=mode)
        normalized = preview["bundle"]
        incoming = normalized["scene"]
        target = scene_cache_service.get_scene(_text(target_scene_id)) if target_scene_id else None

        if mode == "create":
            requested_id = incoming["scene_id"]
            if scene_cache_service.get_scene(requested_id):
                scene_id = scene_cache_service.next_custom_scene_id()
                scene_name = f"{incoming['name']}（导入）"
            else:
                scene_id = requested_id
                scene_name = incoming["name"]
            scene = SceneDTO(
                scene_id=scene_id,
                name=scene_name,
                description=incoming["description"],
                version=max(1, int(incoming["version"])),
                sample_goals=list(incoming["sample_goals"]),
                fields=[],
                relations=[],
            )
        else:
            if not target:
                raise ValueError("target scene is required for merge or replace")
            scene = target.model_copy(deep=True)
            if mode == "replace":
                scene.name = incoming["name"]
                scene.description = incoming["description"]
                scene.sample_goals = list(incoming["sample_goals"])
                scene.version = max(scene.version, int(incoming["version"]))
                scene.fields = []
                scene.relations = []

        field_map = {_field_key(field): field for field in scene.fields}
        for item in incoming["fields"]:
            field_map[_field_key(item)] = SceneFieldDTO(
                field_id=f"field_{uuid4().hex[:10]}",
                table_name=item["table_name"],
                field_name=item["field_name"],
                semantic_name=item["semantic_name"],
                description=item["description"],
                role=item["role"],
                enabled=item["enabled"],
            )
        scene.fields = list(field_map.values())

        relation_map = {_relation_key(relation): relation for relation in scene.relations}
        for item in incoming["relations"]:
            relation_map[_relation_key(item)] = SceneRelationDTO(
                relation_id=f"rel_{uuid4().hex[:10]}",
                left_table=item["left_table"],
                left_field=item["left_field"],
                right_table=item["right_table"],
                right_field=item["right_field"],
                join_type=item["join_type"],
                note=item["note"],
            )
        scene.relations = list(relation_map.values())

        if mode == "replace":
            for zone in ("modeled", "effective"):
                semantic_field_cache_service.delete_scene_fields_by_zone(scene.scene_id, zone)

        semantic_upserted = 0
        for item in normalized["semantic_fields"]:
            semantic_field_cache_service.upsert_field(scene.scene_id, item)
            semantic_upserted += 1

        scene_cache_service.upsert_scene(scene)
        return {
            "ok": True,
            "mode": mode,
            "scene_id": scene.scene_id,
            "scene_name": scene.name,
            "counts": {
                "fields": len(scene.fields),
                "relations": len(scene.relations),
                "semantic_fields": semantic_upserted,
            },
            "preview": {
                key: value
                for key, value in preview.items()
                if key != "bundle"
            },
        }


config_transfer_service = ConfigTransferService()
