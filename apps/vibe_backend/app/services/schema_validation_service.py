from __future__ import annotations

from typing import Any

from .llm_agent_service import LlmAgentService


_schema_service = LlmAgentService()


def _normalize_key(value: Any) -> str:
    return str(value or "").strip().casefold()


def load_schema_snapshot(*, force_refresh: bool = False) -> dict:
    if force_refresh:
        _schema_service.refresh_schema_cache()
    return _schema_service.schema_snapshot()


def build_schema_index(snapshot: dict | None = None, *, force_refresh: bool = False) -> dict:
    source = snapshot if isinstance(snapshot, dict) else load_schema_snapshot(force_refresh=force_refresh)
    tables_raw = source.get("tables", []) if isinstance(source, dict) else []

    tables: list[dict[str, Any]] = []
    tables_by_key: dict[str, dict[str, Any]] = {}
    fields_by_table: dict[str, dict[str, str]] = {}

    for table_meta in tables_raw:
        if not isinstance(table_meta, dict):
            continue
        table_name = str(table_meta.get("table_name") or "").strip()
        if not table_name:
            continue
        table_key = _normalize_key(table_name)
        field_map: dict[str, str] = {}
        fields: list[str] = []
        for field_meta in table_meta.get("fields", []) if isinstance(table_meta.get("fields"), list) else []:
            if not isinstance(field_meta, dict):
                continue
            field_name = str(field_meta.get("field_name") or "").strip()
            if not field_name:
                continue
            field_key = _normalize_key(field_name)
            if field_key in field_map:
                continue
            field_map[field_key] = field_name
            fields.append(field_name)
        fields_by_table[table_key] = field_map
        tables_by_key[table_key] = {
            "table_name": table_name,
            "table_comment": str(table_meta.get("table_comment") or "").strip(),
            "fields": fields,
        }
        tables.append(
            {
                "table_name": table_name,
                "table_comment": str(table_meta.get("table_comment") or "").strip(),
                "fields": fields,
            }
        )

    return {
        "tables": tables,
        "tables_by_key": tables_by_key,
        "fields_by_table": fields_by_table,
    }


def resolve_table_name(table_name: str, *, schema_index: dict | None = None, force_refresh: bool = False) -> str | None:
    index = schema_index if isinstance(schema_index, dict) else build_schema_index(force_refresh=force_refresh)
    table_key = _normalize_key(table_name)
    if not table_key:
        return None
    table_meta = index.get("tables_by_key", {}).get(table_key)
    if not isinstance(table_meta, dict):
        return None
    resolved = str(table_meta.get("table_name") or "").strip()
    return resolved or None


def resolve_table_field(
    table_name: str,
    field_name: str,
    *,
    schema_index: dict | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    index = schema_index if isinstance(schema_index, dict) else build_schema_index(force_refresh=force_refresh)
    requested_table = str(table_name or "").strip()
    requested_field = str(field_name or "").strip()
    if not requested_table:
        return {"ok": False, "reason": "table_missing", "message": "数据库中不存在表：空"}
    table_meta = index.get("tables_by_key", {}).get(_normalize_key(requested_table))
    if not isinstance(table_meta, dict):
        return {
            "ok": False,
            "reason": "table_not_found",
            "message": f"数据库中不存在表：{requested_table}",
            "table_name": requested_table,
            "field_name": requested_field,
        }
    resolved_table = str(table_meta.get("table_name") or "").strip() or requested_table
    field_map = index.get("fields_by_table", {}).get(_normalize_key(resolved_table), {})
    if not isinstance(field_map, dict):
        field_map = {}
    if not requested_field:
        return {
            "ok": False,
            "reason": "field_missing",
            "message": f"数据库中不存在字段：{resolved_table}.空",
            "table_name": resolved_table,
            "field_name": requested_field,
        }
    resolved_field = field_map.get(_normalize_key(requested_field))
    if not resolved_field:
        return {
            "ok": False,
            "reason": "field_not_found",
            "message": f"数据库中不存在字段：{resolved_table}.{requested_field}",
            "table_name": resolved_table,
            "field_name": requested_field,
        }
    return {
        "ok": True,
        "table_name": resolved_table,
        "field_name": resolved_field,
        "table_fields": list(table_meta.get("fields", []) or []),
    }
