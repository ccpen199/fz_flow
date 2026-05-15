from __future__ import annotations

import hashlib
import os

import pymysql
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from uuid import uuid4

from ..store import persist_state
from ..services.scene_cache_service import scene_cache_service
from ..services.scene_playbooks import get_scene_playbook
from ..services.prd_scene_configs import build_prd_scene_templates
from ..services.semantic_field_cache_service import semantic_field_cache_service
from packages.shared_contracts.python_models import FieldRole, SceneDTO, SceneFieldDTO, SceneRelationDTO

router = APIRouter(prefix="/api/v1/scenes", tags=["scenes"])

SCENE_NAME_COMPAT_MAP: dict[str, str] = {
    "竞品分析": "竞品与价格分析",
    "上新趋势分析": "趋势与爆款分析",
}


def _normalize_scene_name(name: str) -> str:
    trimmed = name.strip()
    return SCENE_NAME_COMPAT_MAP.get(trimmed, trimmed)


def _mysql_config() -> dict:
    return {
        "host": os.getenv("MYSQL_HOST", "127.0.0.1"),
        "port": int(os.getenv("MYSQL_PORT", "3306")),
        "user": os.getenv("MYSQL_USER", "root"),
        "password": os.getenv("MYSQL_PASSWORD", "root"),
        "database": os.getenv("MYSQL_DATABASE", "dataservice_test_local"),
        "charset": "utf8mb4",
        "cursorclass": pymysql.cursors.DictCursor,
        "autocommit": True,
    }


PRD_SCENE_TEMPLATES: list[dict] = build_prd_scene_templates()
_SEMANTIC_SYNCED_SCENES: set[str] = set()


def _coerce_field_role(role: FieldRole | str) -> FieldRole:
    if isinstance(role, FieldRole):
        return role
    return FieldRole(str(role or FieldRole.DIMENSION.value))


def _sync_prd_semantic_cache(template: dict) -> int:
    scene_id = str(template.get("scene_id") or "").strip()
    if not scene_id or scene_id in _SEMANTIC_SYNCED_SCENES:
        return 0

    semantic_fields = list(template.get("semantic_fields") or [])
    if not semantic_fields:
        _SEMANTIC_SYNCED_SCENES.add(scene_id)
        return 0

    semantic_field_cache_service.delete_scene_fields_by_zone(scene_id, "modeled")
    semantic_field_cache_service.delete_scene_fields_by_zone(scene_id, "effective")

    upserted = 0
    for field in semantic_fields:
        semantic_name = str(field.get("semantic_name") or "").strip()
        table_name = str(field.get("table_name") or "").strip()
        field_name = str(field.get("field_name") or "").strip()
        if not semantic_name or not table_name or not field_name:
            continue
        raw_key = f"{scene_id}|modeled|{semantic_name}|{table_name}|{field_name}"
        cache_id = f"sem_{hashlib.md5(raw_key.encode('utf-8')).hexdigest()[:20]}"
        semantic_field_cache_service.upsert_field(
            scene_id,
            {
                "semantic_name": semantic_name,
                "semantic_definition": str(field.get("description") or "").strip(),
                "aliases": list(field.get("aliases") or []),
                "unit": str(field.get("unit") or "").strip(),
                "aggregation": str(field.get("aggregation") or "").strip(),
                "table_name": table_name,
                "field_name": field_name,
                "er_path": str(field.get("er_path") or "").strip(),
                "role": str(field.get("role") or FieldRole.DIMENSION.value).strip(),
                "zone": "modeled",
                "enabled": True,
            },
            cache_id=cache_id,
        )
        upserted += 1

    _SEMANTIC_SYNCED_SCENES.add(scene_id)
    return upserted


def _ensure_prd_scenes() -> dict:
    scene_cache_service.ensure_loaded()
    created = 0
    updated = 0
    changed = False
    scene_ids: list[str] = []

    for template in PRD_SCENE_TEMPLATES:
        scene_ids.append(template["scene_id"])
        scene_changed = False
        replace_existing = bool(template.get("replace_existing"))
        scene = scene_cache_service.get_scene(template["scene_id"])
        if scene is None:
            scene = SceneDTO(
                scene_id=template["scene_id"],
                name=template["name"],
                description=template["description"],
                version=1,
                sample_goals=list(template["sample_goals"]),
                fields=[],
                relations=[],
            )
            created += 1
            changed = True
            scene_changed = True
        else:
            normalized = _normalize_scene_name(scene.name)
            desired_name = _normalize_scene_name(template["name"])
            if normalized != scene.name or scene.name != desired_name:
                scene.name = desired_name
                updated += 1
                changed = True
                scene_changed = True
            if scene.description != template["description"]:
                scene.description = template["description"]
                updated += 1
                changed = True
                scene_changed = True

        if (replace_existing or not scene.sample_goals) and template["sample_goals"] and scene.sample_goals != template["sample_goals"]:
            scene.sample_goals = list(template["sample_goals"])
            updated += 1
            changed = True
            scene_changed = True

        if replace_existing:
            existing_fields = {
                (field.table_name, field.field_name, field.semantic_name): field
                for field in scene.fields
            }
            desired_fields: list[SceneFieldDTO] = []
            for semantic_name, table_name, field_name, role, description in template["fields"]:
                field_role = _coerce_field_role(role)
                key = (table_name, field_name, semantic_name)
                field = existing_fields.get(key)
                if field is None:
                    field = SceneFieldDTO(
                        field_id=f"field_{uuid4().hex[:10]}",
                        table_name=table_name,
                        field_name=field_name,
                        semantic_name=semantic_name,
                        description=description,
                        role=field_role,
                        enabled=True,
                    )
                else:
                    field.description = description
                    field.role = field_role
                    field.enabled = True
                desired_fields.append(field)
            current_field_signature = [
                (field.table_name, field.field_name, field.semantic_name, field.description, str(field.role), field.enabled)
                for field in scene.fields
            ]
            desired_field_signature = [
                (field.table_name, field.field_name, field.semantic_name, field.description, str(field.role), field.enabled)
                for field in desired_fields
            ]
            if current_field_signature != desired_field_signature:
                scene.fields = desired_fields
                updated += 1
                changed = True
                scene_changed = True

            existing_relations = {
                (rel.left_table, rel.left_field, rel.right_table, rel.right_field, rel.join_type): rel
                for rel in scene.relations
            }
            desired_relations: list[SceneRelationDTO] = []
            for left_table, left_field, right_table, right_field, join_type, note in template["relations"]:
                key = (left_table, left_field, right_table, right_field, join_type)
                relation = existing_relations.get(key)
                if relation is None:
                    relation = SceneRelationDTO(
                        relation_id=f"rel_{uuid4().hex[:10]}",
                        left_table=left_table,
                        left_field=left_field,
                        right_table=right_table,
                        right_field=right_field,
                        join_type=join_type,
                        note=note,
                    )
                else:
                    relation.note = note
                desired_relations.append(relation)
            current_relation_signature = [
                (rel.left_table, rel.left_field, rel.right_table, rel.right_field, rel.join_type, rel.note)
                for rel in scene.relations
            ]
            desired_relation_signature = [
                (rel.left_table, rel.left_field, rel.right_table, rel.right_field, rel.join_type, rel.note)
                for rel in desired_relations
            ]
            if current_relation_signature != desired_relation_signature:
                scene.relations = desired_relations
                updated += 1
                changed = True
                scene_changed = True
        else:
            field_keys = {(f.table_name, f.field_name, f.semantic_name) for f in scene.fields}
            for semantic_name, table_name, field_name, role, description in template["fields"]:
                field_role = _coerce_field_role(role)
                key = (table_name, field_name, semantic_name)
                if key in field_keys:
                    continue
                scene.fields.append(
                    SceneFieldDTO(
                        field_id=f"field_{uuid4().hex[:10]}",
                        table_name=table_name,
                        field_name=field_name,
                        semantic_name=semantic_name,
                        description=description,
                        role=field_role,
                        enabled=True,
                    )
                )
                field_keys.add(key)
                updated += 1
                changed = True
                scene_changed = True

            relation_keys = {
                (r.left_table, r.left_field, r.right_table, r.right_field, r.join_type)
                for r in scene.relations
            }
            for left_table, left_field, right_table, right_field, join_type, note in template["relations"]:
                key = (left_table, left_field, right_table, right_field, join_type)
                if key in relation_keys:
                    continue
                scene.relations.append(
                    SceneRelationDTO(
                        relation_id=f"rel_{uuid4().hex[:10]}",
                        left_table=left_table,
                        left_field=left_field,
                        right_table=right_table,
                        right_field=right_field,
                        join_type=join_type,
                        note=note,
                    )
                )
                relation_keys.add(key)
                updated += 1
                changed = True
                scene_changed = True

        if scene_changed:
            scene_cache_service.upsert_scene(scene)

        if replace_existing:
            synced_semantic_fields = _sync_prd_semantic_cache(template)
            if synced_semantic_fields:
                updated += 1
                changed = True

    if changed:
        persist_state()

    return {
        "changed": changed,
        "created": created,
        "updated": updated,
        "scene_ids": scene_ids,
    }


def _fetch_mysql_schema() -> tuple[dict[str, set[str]], dict]:
    schema: dict[str, set[str]] = {}
    mysql_cfg = _mysql_config()
    conn = pymysql.connect(**mysql_cfg)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT TABLE_NAME, COLUMN_NAME
                FROM information_schema.columns
                WHERE table_schema = %s
                """,
                (mysql_cfg["database"],),
            )
            for row in cur.fetchall():
                schema.setdefault(row["TABLE_NAME"], set()).add(row["COLUMN_NAME"])
    finally:
        conn.close()
    return schema, mysql_cfg


def _validate_scene_against_schema(scene: SceneDTO, schema: dict[str, set[str]], mysql_cfg: dict) -> dict:
    missing_tables: list[str] = []
    missing_fields: list[str] = []
    relation_checks: list[dict] = []

    for field in scene.fields:
        if field.table_name not in schema:
            missing_tables.append(field.table_name)
            continue
        if field.field_name not in schema[field.table_name]:
            missing_fields.append(f"{field.table_name}.{field.field_name}")

    conn = pymysql.connect(**mysql_cfg)
    try:
        with conn.cursor() as cur:
            for relation in scene.relations:
                exists_left = relation.left_table in schema and relation.left_field in schema[relation.left_table]
                exists_right = relation.right_table in schema and relation.right_field in schema[relation.right_table]
                if not (exists_left and exists_right):
                    relation_checks.append(
                        {
                            "relation_id": relation.relation_id,
                            "left": f"{relation.left_table}.{relation.left_field}",
                            "right": f"{relation.right_table}.{relation.right_field}",
                            "ok": False,
                            "reason": "relation columns missing",
                            "match_rows": 0,
                        }
                    )
                    continue
                sql = (
                    f"SELECT COUNT(1) AS c FROM `{relation.left_table}` l "
                    f"JOIN `{relation.right_table}` r "
                    f"ON l.`{relation.left_field}` = r.`{relation.right_field}`"
                )
                cur.execute(sql)
                count = int(cur.fetchone()["c"])
                relation_checks.append(
                    {
                        "relation_id": relation.relation_id,
                        "left": f"{relation.left_table}.{relation.left_field}",
                        "right": f"{relation.right_table}.{relation.right_field}",
                        "ok": count > 0,
                        "reason": "" if count > 0 else "no matched rows",
                        "match_rows": count,
                    }
                )
    finally:
        conn.close()

    ok = not missing_tables and not missing_fields and all(item["ok"] for item in relation_checks)
    return {
        "scene_id": scene.scene_id,
        "scene_name": scene.name,
        "ok": ok,
        "required_fields": len(scene.fields),
        "required_relations": len(scene.relations),
        "missing_tables": sorted(set(missing_tables)),
        "missing_fields": sorted(set(missing_fields)),
        "relation_checks": relation_checks,
    }


class CreateSceneRequest(BaseModel):
    # PRD 10.1.1：创建场景基础信息至少包含“场景名称、场景描述”。
    name: str = Field(..., min_length=1, description="场景名称（必填）")
    description: str = Field("", description="场景描述（可选）")


class AddSceneFieldRequest(BaseModel):
    table_name: str
    field_name: str
    semantic_name: str
    description: str = ""
    role: FieldRole
    enabled: bool = True


class AddSceneRelationRequest(BaseModel):
    left_table: str
    left_field: str
    right_table: str
    right_field: str
    join_type: str = "INNER"
    note: str = ""


def warm_scene_cache() -> None:
    try:
        _ensure_prd_scenes()
    except Exception:  # noqa: BLE001
        # Startup should not fail hard if DB is temporarily unavailable.
        scene_cache_service.ensure_loaded()


@router.get("", response_model=list[SceneDTO])
async def list_scenes() -> list[SceneDTO]:
    _ensure_prd_scenes()
    return scene_cache_service.list_scenes()


@router.get("/cache", response_model=dict)
async def scene_cache_status() -> dict:
    return scene_cache_service.cache_status()


@router.post("/cache/refresh", response_model=dict)
async def refresh_scene_cache() -> dict:
    return scene_cache_service.refresh_cache()


@router.post("", response_model=SceneDTO)
async def create_scene(body: CreateSceneRequest) -> SceneDTO:
    scene_cache_service.ensure_loaded()
    name = _normalize_scene_name(body.name)
    if not name:
        raise HTTPException(status_code=422, detail="scene name is required")
    scene = SceneDTO(
        scene_id=scene_cache_service.next_custom_scene_id(),
        name=name,
        description=body.description.strip(),
        version=1,
        sample_goals=[],
        fields=[],
        relations=[],
    )
    scene_cache_service.upsert_scene(scene)
    persist_state()
    return scene


@router.get("/{scene_id}", response_model=SceneDTO)
async def get_scene(scene_id: str) -> SceneDTO:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")
    return scene


@router.get("/{scene_id}/playbook", response_model=dict)
async def get_scene_playbook_api(scene_id: str) -> dict:
    _ensure_prd_scenes()
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")

    playbook = get_scene_playbook(scene_id=scene_id, scene_name=scene.name)
    if not playbook:
        raise HTTPException(status_code=404, detail="scene playbook not found")
    return playbook


@router.delete("/{scene_id}", response_model=dict)
async def delete_scene(scene_id: str) -> dict:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")
    if scene_id in {item["scene_id"] for item in PRD_SCENE_TEMPLATES}:
        raise HTTPException(status_code=400, detail="preset scene cannot be deleted")
    scene_cache_service.delete_scene(scene_id)
    persist_state()
    return {"ok": True, "scene_id": scene_id}


@router.post("/{scene_id}/fields", response_model=SceneFieldDTO)
async def add_scene_field(scene_id: str, body: AddSceneFieldRequest) -> SceneFieldDTO:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")

    field = SceneFieldDTO(
        field_id=f"field_{uuid4().hex[:10]}",
        table_name=body.table_name.strip(),
        field_name=body.field_name.strip(),
        semantic_name=body.semantic_name.strip(),
        description=body.description.strip(),
        role=body.role,
        enabled=body.enabled,
    )
    scene.fields.append(field)
    scene_cache_service.upsert_scene(scene)
    persist_state()
    return field


@router.post("/{scene_id}/relations", response_model=SceneRelationDTO)
async def add_scene_relation(scene_id: str, body: AddSceneRelationRequest) -> SceneRelationDTO:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")

    relation = SceneRelationDTO(
        relation_id=f"rel_{uuid4().hex[:10]}",
        left_table=body.left_table.strip(),
        left_field=body.left_field.strip(),
        right_table=body.right_table.strip(),
        right_field=body.right_field.strip(),
        join_type=body.join_type.strip().upper(),
        note=body.note.strip(),
    )
    scene.relations.append(relation)
    scene_cache_service.upsert_scene(scene)
    persist_state()
    return relation


@router.delete("/{scene_id}/relations/{relation_id}", response_model=dict)
async def delete_scene_relation(scene_id: str, relation_id: str) -> dict:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")

    before_count = len(scene.relations)
    scene.relations = [relation for relation in scene.relations if relation.relation_id != relation_id]
    if len(scene.relations) == before_count:
        raise HTTPException(status_code=404, detail="relation not found")

    scene_cache_service.upsert_scene(scene)
    persist_state()
    return {"ok": True, "scene_id": scene_id, "relation_id": relation_id}


@router.post("/{scene_id}/publish", response_model=SceneDTO)
async def publish_scene(scene_id: str) -> SceneDTO:
    scene = scene_cache_service.get_scene(scene_id)
    if not scene:
        raise HTTPException(status_code=404, detail="scene not found")
    scene.version += 1
    scene_cache_service.upsert_scene(scene)
    persist_state()
    return scene


@router.post("/bootstrap/prd", response_model=dict)
async def bootstrap_prd_scenes(validate_db: bool = True) -> dict:
    seed_result = _ensure_prd_scenes()

    if not validate_db:
        return {
            "ok": True,
            "seed": seed_result,
            "database": {"validated": False},
            "scene_checks": [],
        }

    try:
        schema, mysql_cfg = _fetch_mysql_schema()
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "seed": seed_result,
            "database": {
                "validated": False,
                "host": _mysql_config()["host"],
                "port": _mysql_config()["port"],
                "database": _mysql_config()["database"],
                "error": str(exc),
            },
            "scene_checks": [],
        }

    scene_checks: list[dict] = []
    for scene_id in seed_result["scene_ids"]:
        scene = scene_cache_service.get_scene(scene_id)
        if not scene:
            continue
        scene_checks.append(_validate_scene_against_schema(scene, schema, mysql_cfg))

    return {
        "ok": all(item["ok"] for item in scene_checks),
        "seed": seed_result,
        "database": {
            "validated": True,
            "host": mysql_cfg["host"],
            "port": mysql_cfg["port"],
            "database": mysql_cfg["database"],
            "tables": len(schema),
        },
        "scene_checks": scene_checks,
    }
